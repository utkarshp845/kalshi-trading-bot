"""
Kalshi REST API v2 client with RSA-PSS authentication.

Every request is signed with:
  KALSHI-ACCESS-KEY:       API key ID
  KALSHI-ACCESS-TIMESTAMP: Unix timestamp in milliseconds (string)
  KALSHI-ACCESS-SIGNATURE: base64(RSA-PSS-SHA256(timestamp_ms + METHOD + /path))
"""
from __future__ import annotations

import base64
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

log = logging.getLogger(__name__)


def _money_from_dict(d: dict, dollars_key: str, cents_key: str) -> float:
    """
    Read a money field from Kalshi JSON.

    The API commonly exposes both `*_dollars` and raw cent-denominated integer
    variants. Prefer the explicit dollar field when present; otherwise treat the
    raw field as cents.
    """
    if d.get(dollars_key) is not None:
        return float(d[dollars_key])
    if d.get(cents_key) is not None:
        return float(d[cents_key]) / 100.0
    return 0.0


@dataclass
class OrderbookLevel:
    price: float
    quantity: float


@dataclass
class OrderbookSnapshot:
    ticker: str
    yes_levels: list[OrderbookLevel]
    no_levels: list[OrderbookLevel]

    @staticmethod
    def _sorted(levels: list[OrderbookLevel]) -> list[OrderbookLevel]:
        return sorted(levels, key=lambda level: level.price)

    @classmethod
    def from_dict(cls, ticker: str, d: dict) -> "OrderbookSnapshot":
        payload = d.get("orderbook_fp", d)

        def _levels(key: str) -> list[OrderbookLevel]:
            levels = []
            for raw_price, raw_qty in payload.get(key, []):
                levels.append(OrderbookLevel(price=float(raw_price), quantity=float(raw_qty)))
            return cls._sorted(levels)

        return cls(
            ticker=ticker,
            yes_levels=_levels("yes_dollars"),
            no_levels=_levels("no_dollars"),
        )

    def book_for_buy_side(self, side: str) -> list[OrderbookLevel]:
        if side == "yes":
            source = self.no_levels
        else:
            source = self.yes_levels
        derived = [
            OrderbookLevel(price=max(0.01, min(0.99, 1.0 - level.price)), quantity=level.quantity)
            for level in source
        ]
        return self._sorted(derived)

    def best_ask_for_buy_side(self, side: str) -> Optional[OrderbookLevel]:
        levels = self.book_for_buy_side(side)
        return levels[0] if levels else None

    def entry_metrics(self, side: str, ask_price: float) -> dict[str, float | Optional[float] | bool]:
        levels = self.book_for_buy_side(side)
        if not levels:
            return {
                "top_of_book_size": 0.0,
                "resting_size_at_entry": 0.0,
                "cumulative_size_at_entry": 0.0,
                "expected_fill_price": None,
                "depth_slippage": 0.0,
                "orderbook_available": False,
            }

        best = levels[0]
        resting = sum(level.quantity for level in levels if abs(level.price - ask_price) <= 1e-9)
        cumulative = sum(level.quantity for level in levels if level.price <= ask_price + 1e-9)
        expected_fill_price = best.price
        depth_slippage = max(0.0, best.price - ask_price)
        return {
            "top_of_book_size": best.quantity,
            "resting_size_at_entry": resting,
            "cumulative_size_at_entry": cumulative,
            "expected_fill_price": expected_fill_price,
            "depth_slippage": depth_slippage,
            "orderbook_available": True,
        }

    def imbalance(self) -> float:
        best_yes = self.yes_levels[-1].quantity if self.yes_levels else 0.0
        best_no = self.no_levels[-1].quantity if self.no_levels else 0.0
        denom = best_yes + best_no
        if denom <= 0:
            return 0.0
        return (best_yes - best_no) / denom


@dataclass
class Market:
    ticker: str
    event_ticker: str
    status: str
    close_time: str          # ISO-8601 string
    yes_ask: float           # dollars (0.01 – 0.99)
    no_ask: float
    yes_bid: float
    no_bid: float
    last_price: Optional[float]
    orderbook: Optional[OrderbookSnapshot] = None

    @classmethod
    def from_dict(cls, d: dict) -> "Market":
        return cls(
            ticker=d["ticker"],
            event_ticker=d.get("event_ticker", ""),
            status=d.get("status", ""),
            close_time=d.get("close_time", ""),
            yes_ask=_money_from_dict(d, "yes_ask_dollars", "yes_ask"),
            no_ask=_money_from_dict(d, "no_ask_dollars", "no_ask"),
            yes_bid=_money_from_dict(d, "yes_bid_dollars", "yes_bid"),
            no_bid=_money_from_dict(d, "no_bid_dollars", "no_bid"),
            last_price=(
                _money_from_dict(d, "last_price_dollars", "last_price")
                if d.get("last_price_dollars") is not None or d.get("last_price") is not None
                else None
            ),
            orderbook=None,
        )


@dataclass
class Order:
    order_id: str
    client_order_id: Optional[str]
    ticker: str
    side: str        # "yes" or "no"
    action: str      # "buy" or "sell"
    status: str
    yes_price: float
    no_price: float
    count: int
    fill_count: int
    taker_fill_cost: float
    created_time: str
    maker_fill_cost: float = 0.0
    taker_fees: float = 0.0
    maker_fees: float = 0.0

    @property
    def contract_price(self) -> float:
        return self.yes_price if self.side == "yes" else self.no_price

    @property
    def fill_cost(self) -> float:
        """
        Total cost/proceeds reported for filled contracts.

        Kalshi reports maker and taker fill costs separately. If an older
        response omits them despite a fill, fall back to the submitted limit
        price so risk and fill-quality accounting do not record a free fill.
        """
        explicit_cost = self.taker_fill_cost + self.maker_fill_cost
        if explicit_cost > 0 or self.fill_count <= 0:
            return explicit_cost
        return self.contract_price * self.fill_count

    @property
    def fees(self) -> float:
        return self.taker_fees + self.maker_fees

    @classmethod
    def from_dict(cls, d: dict) -> "Order":
        return cls(
            order_id=d.get("order_id", ""),
            client_order_id=d.get("client_order_id"),
            ticker=d.get("ticker", ""),
            side=d.get("side", ""),
            action=d.get("action", ""),
            status=d.get("status", ""),
            yes_price=_money_from_dict(d, "yes_price_dollars", "yes_price"),
            no_price=_money_from_dict(d, "no_price_dollars", "no_price"),
            count=int(float(d.get("initial_count_fp") or 0)),
            fill_count=int(float(d.get("fill_count_fp") or 0)),
            taker_fill_cost=_money_from_dict(d, "taker_fill_cost_dollars", "taker_fill_cost"),
            created_time=d.get("created_time", ""),
            maker_fill_cost=_money_from_dict(d, "maker_fill_cost_dollars", "maker_fill_cost"),
            taker_fees=_money_from_dict(d, "taker_fees_dollars", "taker_fees"),
            maker_fees=_money_from_dict(d, "maker_fees_dollars", "maker_fees"),
        )


@dataclass
class Position:
    ticker: str
    side: str           # "yes" or "no"
    quantity: int
    cost: float


class KalshiClient:
    def __init__(self, api_key_id: str, private_key_path: Path, base_url: str):
        self._api_key_id = api_key_id
        self._base_url = base_url.rstrip("/")
        self._base_path = urlparse(self._base_url).path  # e.g. "/trade-api/v2"
        self._private_key = self._load_key(private_key_path)
        self._session = requests.Session()
        self._session.headers.update({"Content-Type": "application/json", "Accept": "application/json"})

    # ------------------------------------------------------------------
    # Auth
    # ------------------------------------------------------------------

    @staticmethod
    def _load_key(path: Path):
        pem = Path(path).read_bytes()
        return serialization.load_pem_private_key(pem, password=None)

    def _sign(self, method: str, path: str) -> dict:
        """Return the three auth headers required by every Kalshi request."""
        ts_ms = str(int(time.time() * 1000))
        msg = (ts_ms + method.upper() + path).encode("utf-8")
        sig = self._private_key.sign(
            msg,
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.MAX_LENGTH,
            ),
            hashes.SHA256(),
        )
        return {
            "KALSHI-ACCESS-KEY": self._api_key_id,
            "KALSHI-ACCESS-TIMESTAMP": ts_ms,
            "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode(),
        }

    def _request(self, method: str, path: str, params=None, body=None) -> Any:
        url = self._base_url + path
        for attempt in range(4):
            headers = self._sign(method, self._base_path + path)
            resp = self._session.request(
                method, url, headers=headers, params=params, json=body, timeout=15
            )
            if resp.status_code == 429:
                wait = 2 ** attempt
                log.warning("Rate limited (429) on %s %s — retrying in %ds", method, path, wait)
                time.sleep(wait)
                continue
            if resp.status_code in (500, 502, 503, 504) and attempt < 3:
                wait = 2 ** attempt
                log.warning("Server error (%d) on %s %s — retrying in %ds", resp.status_code, method, path, wait)
                time.sleep(wait)
                continue
            resp.raise_for_status()
            return resp.json()
        resp.raise_for_status()  # raise after exhausting retries

    def _get(self, path: str, params: Optional[dict] = None) -> Any:
        return self._request("GET", path, params=params)

    def _post(self, path: str, body: dict) -> Any:
        return self._request("POST", path, body=body)

    def _delete(self, path: str) -> Any:
        return self._request("DELETE", path)

    # ------------------------------------------------------------------
    # Markets
    # ------------------------------------------------------------------

    def get_open_markets(self, series_ticker: str) -> list[Market]:
        """Return all open Kalshi markets for a given series (e.g. 'KXBTC', 'KXETH')."""
        path = "/markets"
        data = self._get(path, params={"series_ticker": series_ticker, "status": "open"})
        markets = [Market.from_dict(m) for m in data.get("markets", [])]
        log.debug("Found %d open %s markets", len(markets), series_ticker)
        return markets

    def get_open_btc_markets(self) -> list[Market]:
        """Return all open Kalshi BTC daily price-level markets (series KXBTC)."""
        return self.get_open_markets("KXBTC")

    def get_market(self, ticker: str) -> Market:
        path = f"/markets/{ticker}"
        data = self._get(path)
        return Market.from_dict(data.get("market", data))

    def get_market_orderbook(self, ticker: str, depth: int = 0) -> OrderbookSnapshot:
        path = f"/markets/{ticker}/orderbook"
        data = self._get(path, params={"depth": depth})
        return OrderbookSnapshot.from_dict(ticker, data)

    def get_market_orderbooks(self, tickers: list[str], depth: int = 0) -> dict[str, OrderbookSnapshot]:
        if not tickers:
            return {}
        path = "/markets/orderbooks"
        # Kalshi documents `tickers` as a string[] query param. Passing the raw
        # list lets `requests` encode repeated `tickers=` keys instead of relying
        # on undocumented CSV parsing.
        data = self._get(path, params={"tickers": tickers, "depth": depth})
        out: dict[str, OrderbookSnapshot] = {}
        for item in data.get("orderbooks", []):
            ticker = item.get("ticker", "")
            if not ticker:
                continue
            out[ticker] = OrderbookSnapshot.from_dict(ticker, item)
        return out

    def get_historical_market(self, ticker: str) -> dict[str, Any]:
        path = f"/historical/markets/{ticker}"
        data = self._get(path)
        return data.get("market", data)

    # ------------------------------------------------------------------
    # Portfolio
    # ------------------------------------------------------------------

    def get_balance(self) -> float:
        """Return available balance in USD."""
        path = "/portfolio/balance"
        data = self._get(path)
        balance = (
            _money_from_dict(data, "balance_dollars", "balance")
            if data.get("balance_dollars") is not None or data.get("balance") is not None
            else _money_from_dict(data, "available_balance_dollars", "available_balance")
        )
        log.debug("Account balance: $%.2f", balance)
        return balance

    def get_positions(self) -> list[Position]:
        """Return all non-zero positions."""
        path = "/portfolio/positions"
        data = self._get(path, params={"filter_by_non_zero": "true"})
        positions = []
        for p in data.get("market_positions", []):
            qty_yes = int(p.get("position", 0))
            qty_no = int(p.get("no_position", 0))
            if qty_yes > 0:
                positions.append(Position(
                    ticker=p["ticker"],
                    side="yes",
                    quantity=qty_yes,
                    cost=_money_from_dict(p, "cost_basis_yes_dollars", "cost_basis_yes"),
                ))
            if qty_no > 0:
                positions.append(Position(
                    ticker=p["ticker"],
                    side="no",
                    quantity=qty_no,
                    cost=_money_from_dict(p, "cost_basis_no_dollars", "cost_basis_no"),
                ))
        log.debug("Open positions: %d", len(positions))
        return positions

    # ------------------------------------------------------------------
    # Orders
    # ------------------------------------------------------------------

    def place_order(
        self,
        ticker: str,
        side: str,
        count: int,
        price_dollars: float,
        client_order_id: Optional[str] = None,
        *,
        post_only: bool = False,
        time_in_force: Optional[str] = None,
    ) -> Order:
        """
        Place a limit buy order.

        Args:
            ticker:          Kalshi market ticker
            side:            "yes" or "no"
            count:           Number of contracts
            price_dollars:   Limit price in dollars (0.01 – 0.99)
            client_order_id: Optional idempotency key
        """
        body: dict[str, Any] = {
            "ticker": ticker,
            "side": side,
            "action": "buy",
            "type": "limit",
            "count": count,
        }
        # Send price as cents integer (Kalshi accepts 1–99)
        price_cents = max(1, min(99, round(price_dollars * 100)))
        if side == "yes":
            body["yes_price"] = price_cents
        else:
            body["no_price"] = price_cents

        if client_order_id:
            body["client_order_id"] = client_order_id
        if post_only:
            body["post_only"] = True
        if time_in_force:
            body["time_in_force"] = time_in_force

        path = "/portfolio/orders"
        data = self._post(path, body)
        order = Order.from_dict(data.get("order", data))
        log.info(
            "Order placed: %s %s %s x%d @ $%.2f post_only=%s tif=%s → id=%s status=%s",
            ticker, side, "buy", count, price_dollars, post_only, time_in_force,
            order.order_id, order.status,
        )
        return order

    def get_order(self, order_id: str) -> Order:
        path = f"/portfolio/orders/{order_id}"
        data = self._get(path)
        return Order.from_dict(data.get("order", data))

    def sell_position(
        self,
        ticker: str,
        side: str,
        count: int,
        price_dollars: float,
    ) -> Order:
        """
        Sell (exit) an existing position by placing a limit sell order.

        Args:
            ticker:        Market ticker
            side:          "yes" or "no" (must match the held position side)
            count:         Number of contracts to sell
            price_dollars: Limit price (at or above current bid for immediate fill)
        """
        body: dict[str, Any] = {
            "ticker": ticker,
            "side": side,
            "action": "sell",
            "type": "limit",
            "count": count,
        }
        price_cents = max(1, min(99, round(price_dollars * 100)))
        if side == "yes":
            body["yes_price"] = price_cents
        else:
            body["no_price"] = price_cents

        path = "/portfolio/orders"
        data = self._post(path, body)
        order = Order.from_dict(data.get("order", data))
        log.info(
            "Exit order placed: %s %s sell x%d @ $%.2f → id=%s status=%s",
            ticker, side, count, price_dollars, order.order_id, order.status,
        )
        return order

    def cancel_order(self, order_id: str) -> None:
        """Cancel an open order. Silently ignores 404 (already filled/cancelled)."""
        try:
            self._delete(f"/portfolio/orders/{order_id}")
            log.info("Order cancelled: %s", order_id)
        except Exception as e:
            log.warning("Cancel failed for %s: %s", order_id[:8], e)

    def get_orders(self, ticker: Optional[str] = None, status: Optional[str] = None) -> list[Order]:
        path = "/portfolio/orders"
        params: dict = {}
        if ticker:
            params["ticker"] = ticker
        if status:
            params["status"] = status
        data = self._get(path, params=params)
        return [Order.from_dict(o) for o in data.get("orders", [])]
