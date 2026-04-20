"""Tests for money parsing in bot/kalshi_client.py."""
from bot.kalshi_client import KalshiClient, Market, Order


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
            "created_time": "2026-04-16T12:00:00Z",
        })

        assert order.yes_price == 0.45
        assert order.no_price == 0.55
        assert order.taker_fill_cost == 0.90

    def test_balance_parses_cent_field_as_dollars(self):
        client = object.__new__(KalshiClient)
        client._get = lambda _path: {"balance": 1755}

        assert client.get_balance() == 17.55

    def test_balance_prefers_explicit_dollar_field(self):
        client = object.__new__(KalshiClient)
        client._get = lambda _path: {"balance_dollars": "20.12", "balance": 2012}

        assert client.get_balance() == 20.12
