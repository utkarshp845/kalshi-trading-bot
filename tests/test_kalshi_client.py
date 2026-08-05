"""Tests for money parsing and orderbook parsing in bot/kalshi_client.py."""
import requests
import pytest

from bot.kalshi_client import KalshiClient, Market, Order, OrderbookSnapshot


def _client_for_request(monkeypatch, responses):
    """Build a bare KalshiClient whose _session.request() replays `responses`
    in order — either an exception instance to raise, or a fake response."""
    client = object.__new__(KalshiClient)
    client._base_url = "https://api.example.com"
    client._base_path = ""
    client._sign = lambda method, path: {}
    monkeypatch.setattr("bot.kalshi_client.time.sleep", lambda _seconds: None)

    calls = iter(responses)

    class _FakeSession:
        def request(self, *args, **kwargs):
            item = next(calls)
            if isinstance(item, Exception):
                raise item
            return item

    client._session = _FakeSession()
    return client


class _FakeResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.exceptions.HTTPError(f"status {self.status_code}")

    def json(self):
        return self._payload


class TestRequestRetry:
    def test_retries_on_read_timeout_then_succeeds(self, monkeypatch):
        client = _client_for_request(
            monkeypatch,
            [
                requests.exceptions.ReadTimeout("timed out"),
                _FakeResponse(200, {"ok": True}),
            ],
        )

        result = client._get("/foo")

        assert result == {"ok": True}

    def test_retries_on_connection_error_then_succeeds(self, monkeypatch):
        client = _client_for_request(
            monkeypatch,
            [
                requests.exceptions.ConnectionError("refused"),
                _FakeResponse(200, {"ok": True}),
            ],
        )

        result = client._get("/foo")

        assert result == {"ok": True}

    def test_raises_after_exhausting_retries_on_timeout(self, monkeypatch):
        client = _client_for_request(
            monkeypatch,
            [requests.exceptions.ReadTimeout("timed out")] * 4,
        )

        with pytest.raises(requests.exceptions.ReadTimeout):
            client._get("/foo")


class TestMoneyParsing:
    def test_market_parses_cent_fields_as_dollars(self):
        market = Market.from_dict({
            "ticker": "KXBTC-26APR4PM-B95000",
            "event_ticker": "KXBTC",
            "status": "open",
            "close_time": "2026-04-26T20:00:00Z",
            "yes_ask": 45,
            "no_ask": 55,
            "yes_bid": 40,
            "no_bid": 50,
            "last_price": 43,
        })

        assert market.yes_ask == 0.45
        assert market.no_ask == 0.55
        assert market.yes_bid == 0.40
        assert market.no_bid == 0.50
        assert market.last_price == 0.43

    def test_order_parses_cent_fields_as_dollars(self):
        order = Order.from_dict({
            "order_id": "o-1",
            "ticker": "KXBTC-26APR4PM-B95000",
            "side": "yes",
            "action": "buy",
            "status": "filled",
            "yes_price": 45,
            "no_price": 55,
            "initial_count_fp": "2",
            "fill_count_fp": "2",
            "taker_fill_cost": 90,
            "maker_fill_cost": 10,
            "taker_fees": 4,
            "maker_fees": 1,
            "created_time": "2026-04-16T12:00:00Z",
        })

        assert order.yes_price == 0.45
        assert order.no_price == 0.55
        assert order.taker_fill_cost == 0.90
        assert order.maker_fill_cost == 0.10
        assert order.fill_cost == 1.00
        assert order.fees == 0.05
        assert order.total_cost == 1.05

    def test_order_fill_cost_falls_back_to_limit_price_when_cost_fields_missing(self):
        order = Order.from_dict({
            "order_id": "o-1",
            "ticker": "KXBTC-26APR4PM-B95000",
            "side": "yes",
            "action": "buy",
            "status": "filled",
            "yes_price": 45,
            "no_price": 55,
            "initial_count_fp": "2",
            "fill_count_fp": "2",
            "created_time": "2026-04-16T12:00:00Z",
        })

        assert order.fill_cost == 0.90

    def test_balance_parses_cent_field_as_dollars(self):
        client = object.__new__(KalshiClient)
        client._get = lambda _path: {"balance": 1755}

        assert client.get_balance() == 17.55

    def test_balance_prefers_explicit_dollar_field(self):
        client = object.__new__(KalshiClient)
        client._get = lambda _path: {"balance_dollars": "20.12", "balance": 2012}

        assert client.get_balance() == 20.12

    def test_orderbook_snapshot_derives_buy_side_asks(self):
        snapshot = OrderbookSnapshot.from_dict(
            "KXBTC-26APR4PM-B95000",
            {
                "orderbook_fp": {
                    "yes_dollars": [["0.4200", "30.00"]],
                    "no_dollars": [["0.5500", "10.00"], ["0.5400", "20.00"]],
                }
            },
        )

        yes_asks = snapshot.book_for_buy_side("yes")

        assert yes_asks[0].price == pytest.approx(0.45)
        assert yes_asks[0].quantity == 10.0
        assert yes_asks[1].price == pytest.approx(0.46)
        assert snapshot.entry_metrics("yes", 0.45)["cumulative_size_at_entry"] == 10.0

    def test_get_market_orderbooks_passes_tickers_as_list(self):
        client = object.__new__(KalshiClient)
        captured = {}

        def _get(_path, params=None):
            captured["params"] = params
            return {"orderbooks": []}

        client._get = _get

        result = client.get_market_orderbooks(
            ["KXBTC-26APR4PM-B95000", "KXBTC-26APR4PM-B96000"],
            depth=20,
        )

        assert result == {}
        assert captured["params"]["tickers"] == [
            "KXBTC-26APR4PM-B95000",
            "KXBTC-26APR4PM-B96000",
        ]
        assert captured["params"]["depth"] == 20


def _order_stub(order_id="o-1", status="resting") -> dict:
    return {
        "order_id": order_id,
        "ticker": "KXBTC-26APR4PM-B95000",
        "side": "yes",
        "action": "buy",
        "status": status,
        "yes_price": 45,
        "no_price": 55,
        "initial_count_fp": "10",
        "fill_count_fp": "0",
        "created_time": "2026-04-16T12:00:00Z",
    }


def _capture_body_and_return_order_id(captured: dict, order_id: str = "o-1"):
    """A fake `_post` that records the request body it was called with and
    returns a minimal CreateOrderV2Response — distinct objects, so a lambda
    can't just echo the request body back as if it were the response."""
    def _post(path, body):
        captured["path"] = path
        captured["body"] = body
        return {"order_id": order_id}
    return _post


class TestBookSideAndPrice:
    """The (side, action) -> (book_side, yes_price) mapping V2 order creation
    depends on. See the Kalshi outcome_side/book_side truth table quoted in
    KalshiClient._book_side_and_price's docstring."""

    def test_buy_yes_is_a_bid_at_the_yes_price(self):
        assert KalshiClient._book_side_and_price("yes", "buy", 0.45) == ("bid", 0.45)

    def test_buy_no_is_an_ask_at_the_complementary_price(self):
        side, price = KalshiClient._book_side_and_price("no", "buy", 0.30)
        assert side == "ask"
        assert price == pytest.approx(0.70)

    def test_sell_yes_is_an_ask_at_the_yes_price(self):
        side, price = KalshiClient._book_side_and_price("yes", "sell", 0.60)
        assert side == "ask"
        assert price == pytest.approx(0.60)

    def test_sell_no_is_a_bid_at_the_complementary_price(self):
        side, price = KalshiClient._book_side_and_price("no", "sell", 0.25)
        assert side == "bid"
        assert price == pytest.approx(0.75)


class TestPlaceOrderV2:
    def test_buy_yes_sends_bid_at_yes_price_to_v2_endpoint(self):
        client = object.__new__(KalshiClient)
        captured = {}

        def _post(path, body):
            captured["path"] = path
            captured["body"] = body
            return {"order_id": "o-123", "fill_count": "0.00", "remaining_count": "10.00", "ts_ms": 1}

        client._post = _post
        client.get_order = lambda order_id: Order.from_dict(_order_stub(order_id))

        order = client.place_order("KXBTC-26APR4PM-B95000", "yes", 10, 0.45)

        assert captured["path"] == "/portfolio/events/orders"
        body = captured["body"]
        assert body["ticker"] == "KXBTC-26APR4PM-B95000"
        assert body["side"] == "bid"
        assert body["count"] == "10.00"
        assert body["price"] == "0.45"
        assert body["time_in_force"] == "good_till_canceled"
        assert body["self_trade_prevention_type"] == "taker_at_cross"
        assert "post_only" not in body
        assert order.order_id == "o-123"

    def test_buy_no_sends_ask_at_complementary_price(self):
        client = object.__new__(KalshiClient)
        captured = {}
        client._post = _capture_body_and_return_order_id(captured)
        client.get_order = lambda order_id: Order.from_dict(_order_stub(order_id))

        client.place_order("KXBTC-26APR4PM-B95000", "no", 5, 0.30)

        assert captured["body"]["side"] == "ask"
        assert captured["body"]["price"] == "0.70"

    def test_post_only_included_only_when_true(self):
        client = object.__new__(KalshiClient)
        captured = {}
        client._post = _capture_body_and_return_order_id(captured)
        client.get_order = lambda order_id: Order.from_dict(_order_stub(order_id))

        client.place_order("KXBTC-26APR4PM-B95000", "yes", 10, 0.45, post_only=True)

        assert captured["body"]["post_only"] is True

    def test_fetches_full_order_via_get_after_create(self):
        client = object.__new__(KalshiClient)
        client._post = lambda path, body: {"order_id": "o-777"}
        seen_ids = []

        def _get_order(order_id):
            seen_ids.append(order_id)
            return Order.from_dict(_order_stub(order_id))

        client.get_order = _get_order

        order = client.place_order("KXBTC-26APR4PM-B95000", "yes", 10, 0.45)

        assert seen_ids == ["o-777"]
        assert order.order_id == "o-777"

    def test_missing_order_id_in_create_response_raises(self):
        client = object.__new__(KalshiClient)
        client._post = lambda path, body: {"fill_count": "0.00"}  # malformed/unexpected response

        with pytest.raises(ValueError):
            client.place_order("KXBTC-26APR4PM-B95000", "yes", 10, 0.45)


class TestSellPositionV2:
    def test_sell_yes_sends_ask_at_yes_price(self):
        client = object.__new__(KalshiClient)
        captured = {}
        client._post = _capture_body_and_return_order_id(captured)
        client.get_order = lambda order_id: Order.from_dict(_order_stub(order_id))

        client.sell_position("KXBTC-26APR4PM-B95000", "yes", 10, 0.60)

        assert captured["body"]["side"] == "ask"
        assert captured["body"]["price"] == "0.60"

    def test_sell_no_sends_bid_at_complementary_price(self):
        client = object.__new__(KalshiClient)
        captured = {}
        client._post = _capture_body_and_return_order_id(captured)
        client.get_order = lambda order_id: Order.from_dict(_order_stub(order_id))

        client.sell_position("KXBTC-26APR4PM-B95000", "no", 10, 0.25)

        assert captured["body"]["side"] == "bid"
        assert captured["body"]["price"] == "0.75"


class TestCancelOrderV2:
    def test_deletes_via_v2_events_orders_path(self):
        client = object.__new__(KalshiClient)
        captured = {}
        client._delete = lambda path: captured.setdefault("path", path)

        client.cancel_order("o-123")

        assert captured["path"] == "/portfolio/events/orders/o-123"
