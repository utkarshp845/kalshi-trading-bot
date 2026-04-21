"""
Kalshi REST API v2 client with RSA-PSS authentication.

Every request is signed with:
  KALSHI-ACCESS-KEY:       API key ID
  KALSHI-ACCESS-TIMESTAMP: Unix timestamp in milliseconds (string)
  KALSHI-ACCESS-SIGNATURE: base64(RSA-PSS-SHA256(timestamp_ms + METHOD + /path))
"""
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

    @classmethod
    def from_dict(cls, d: dict) -> "Market":
        return cls(
            ticker=d["ticker"],
            event_ticker=d.get("event_ticker", ""),
            status=d.get("status", ""),
            close_time=d.get("close_time", ""),
            yes_ask=float(d.get("yes_ask_dollars") or d.get("yes_ask", 0) / 100),
            no_ask=float(d.get("no_ask_dollars") or d.get("no_ask", 0) / 100),
            yes_bid=float(d.get("yes_bid_dollars") or d.get("yes_bid", 0) / 100),
            no_bid=float(d.get("no_bid_dollars") or d.get("no_bid", 0) / 100),
            last_price=float(d["last_price_dollars"]) if d.get("last_price_dollars") is not None else None,
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

    @classmethod
    def from_dict(cls, d: dict) -> "Order":
        return cls(
            order_id=d.get("order_id", ""),
            client_order_id=d.get("client_order_id"),
            ticker=d.get("ticker", ""),
            side=d.get("side", ""),
            action=d.get("action", ""),
            status=d.get("status", ""),
            yes_price=float(d.get("yes_price_dollars") or 0),
            no_price=float(d.get("no_price_dollars") or 0),
            count=int(float(d.get("initial_count_fp") or 0)),
            fill_count=int(float(d.get("fill_count_fp") or 0)),
            taker_fill_cost=float(d.get("taker_fill_cost_dollars") or 0),
            created_time=d.get("created_time", ""),
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

    # ------------------------------------------------------------------
    # Portfolio
    # ------------------------------------------------------------------

    def get_balance(self) -> float:
        """Return available balance in USD."""
        path = "/portfolio/balance"
        data = self._get(path)
        balance_cents = float(data.get("balance", data.get("available_balance", 0)))
        balance = balance_cents / 100.0
        log.debug("Account balance: $%.2f (raw cents: %.0f)", balance, balance_cents)
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
                    cost=float(p.get("cost_basis_yes_dollars", 0)),
                ))
            if qty_no > 0:
                positions.append(Position(
                    ticker=p["ticker"],
                    side="no",
                    quantity=qty_no,
                    cost=float(p.get("cost_basis_no_dollars", 0)),
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

        path = "/portfolio/orders"
        data = self._post(path, body)
        order = Order.from_dict(data.get("order", data))
        log.info(
            "Order placed: %s %s %s x%d @ $%.2f → id=%s status=%s",
            ticker, side, "buy", count, price_dollars, order.order_id, order.status,
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
