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


class OrderVerificationError(Exception):
    """
    An order was successfully created (the V2 create POST succeeded and
    returned an order_id) but its status could not be confirmed afterward —
    GET /portfolio/orders/{id} kept 404ing even after retries, most likely a
    read-after-write propagation delay on Kalshi's side rather than the
    order not existing.

    This is deliberately NOT a subclass of requests.exceptions.HTTPError:
    callers must be able to tell "the order was never created, safe to try
    something else" apart from "the order almost certainly exists, do NOT
    place another one for the same signal." Treating this the same as an
    outright creation failure is what caused a real incident — a maker
    order that filled but couldn't be verified was followed by a taker
    fallback for the same signal, doubling the position.
    """

    def __init__(self, order_id: str, ticker: str, message: str):
        self.order_id = order_id
        self.ticker = ticker
        super().__init__(message)


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

    @property
    def total_cost(self) -> float:
        return self.fill_cost + self.fees

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
            try:
                resp = self._session.request(
                    method, url, headers=headers, params=params, json=body, timeout=15
                )
            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
                if attempt >= 3:
                    raise
                wait = 2 ** attempt
                log.warning(
                    "Network error (%s) on %s %s — retrying in %ds (attempt %d/4)",
                    e.__class__.__name__, method, path, wait, attempt + 1,
                )
                time.sleep(wait)
                continue
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

    def get_market_raw(self, ticker: str) -> dict[str, Any]:
        """Raw (unparsed) /markets/{ticker} response dict.

        Unlike get_market(), which returns the typed Market dataclass, this
        keeps every field Kalshi sends — including `result`,
        `settlement_value_dollars`, and `settlement_ts`, none of which
        Market.from_dict carries. Outcome backfill needs those settlement
        fields; using get_market().__dict__ silently drops them and makes
        every settlement lookup fail with no error (see outcome-backfill
        incident, Aug 2026 — Market never had settlement fields, and
        /historical/markets/{ticker} 404s for anything not yet archived,
        so this raw live-market path is the only route that actually works
        for recently-closed markets).
        """
        path = f"/markets/{ticker}"
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

    @staticmethod
    def _book_side_and_price(side: str, action: str, price_dollars: float) -> tuple[str, float]:
        """
        Map this client's (side: "yes"/"no", action: "buy"/"sell") vocabulary
        onto Kalshi's V2 order API, which has no separate NO leg — it quotes
        everything from the YES side as a single book: `bid` buys YES, `ask`
        sells YES (equivalently: buys NO at `1 - price`).

        Truth table (from Kalshi's Order schema docs — outcome_side/book_side):
            buy  yes -> outcome_side yes -> book_side bid, price = yes price
            sell no  -> outcome_side yes -> book_side bid, price = 1 - no_price
            buy  no  -> outcome_side no  -> book_side ask, price = 1 - no_price
            sell yes -> outcome_side no  -> book_side ask, price = yes price

        `price_dollars` is always in this client's existing vocabulary: the
        price of the *side* being traded (a YES price when side=="yes", a NO
        price when side=="no"). It's converted to the YES-side price V2
        requires since no_price and yes_price are complements in a binary
        market (no_price = 1 - yes_price).
        """
        is_yes = side == "yes"
        is_buy = action == "buy"
        book_side = "bid" if is_yes == is_buy else "ask"
        yes_price = price_dollars if is_yes else (1.0 - price_dollars)
        return book_side, yes_price

    def _create_order_v2(
        self,
        ticker: str,
        side: str,
        action: str,
        count: int,
        price_dollars: float,
        client_order_id: Optional[str] = None,
        post_only: bool = False,
        time_in_force: Optional[str] = None,
    ) -> Order:
        """
        Submit an order via Kalshi's V2 order-creation endpoint (the legacy
        POST /portfolio/orders is deprecated and now returns HTTP 410).

        The V2 create response carries no `status` field (just order_id,
        fill_count, remaining_count, ...), so this immediately follows up
        with GET /portfolio/orders/{id} — that endpoint is unaffected by the
        V2 migration and still returns the same shape (status,
        yes_price_dollars, fill_count_fp, ...) every existing caller already
        depends on. This keeps the migration's blast radius limited to order
        creation; nothing downstream of place_order/sell_position needs to
        change.

        Confirmed in production (2026-08-05): immediately after a real V2
        create, this GET can 404 for several seconds — a read-after-write
        propagation delay, not the order actually being missing. The order
        itself was already live and filling. Retries below absorb that. If
        it's still unconfirmed after retries, raises OrderVerificationError
        rather than a generic error — see that class's docstring for why
        callers must not treat this the same as an outright creation
        failure and must not retry/fallback into placing a second order for
        the same signal.
        """
        book_side, yes_price = self._book_side_and_price(side, action, price_dollars)
        yes_price = round(max(0.01, min(0.99, yes_price)), 2)
        tif = time_in_force or "good_till_canceled"

        body: dict[str, Any] = {
            "ticker": ticker,
            "side": book_side,
            "count": f"{count:.2f}",
            "price": f"{yes_price:.2f}",
            "time_in_force": tif,
            "self_trade_prevention_type": "taker_at_cross",
        }
        if client_order_id:
            body["client_order_id"] = client_order_id
        if post_only:
            body["post_only"] = True

        path = "/portfolio/events/orders"
        data = self._post(path, body)
        order_id = data.get("order_id") or (data.get("order") or {}).get("order_id")
        if not order_id:
            raise ValueError(f"CreateOrderV2 response missing order_id: {data}")

        # The order now exists on Kalshi's book — a 404 here means GET
        # hasn't caught up yet, not that the order is missing. Retry before
        # giving up rather than surfacing a false "order failed" to the
        # caller (see OrderVerificationError's docstring for why that
        # distinction matters).
        last_error: Optional[Exception] = None
        for wait in (0, 1, 2, 4, 8):
            if wait:
                time.sleep(wait)
            try:
                return self.get_order(order_id)
            except requests.exceptions.HTTPError as e:
                if e.response is not None and e.response.status_code == 404:
                    last_error = e
                    continue
                raise
        raise OrderVerificationError(
            order_id, ticker,
            f"Order {order_id} was created on {ticker} but could not be "
            f"confirmed via GET after retries: {last_error}",
        )

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
        order = self._create_order_v2(
            ticker, side, "buy", count, price_dollars,
            client_order_id=client_order_id, post_only=post_only, time_in_force=time_in_force,
        )
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
        order = self._create_order_v2(ticker, side, "sell", count, price_dollars)
        log.info(
            "Exit order placed: %s %s sell x%d @ $%.2f → id=%s status=%s",
            ticker, side, count, price_dollars, order.order_id, order.status,
        )
        return order

    def cancel_order(self, order_id: str) -> None:
        """Cancel an open order. Silently ignores 404 (already filled/cancelled)."""
        try:
            self._delete(f"/portfolio/events/orders/{order_id}")
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
