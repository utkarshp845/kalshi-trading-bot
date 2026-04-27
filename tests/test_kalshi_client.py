"""Tests for money parsing and orderbook parsing in bot/kalshi_client.py."""
import pytest

from bot.kalshi_client import KalshiClient, Market, Order, OrderbookSnapshot


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
