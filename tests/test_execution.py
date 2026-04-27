"""Tests for maker-first execution in bot/main.py."""

import threading
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
import requests

import bot.config as cfg
import bot.main as main_mod
from bot.kalshi_client import Market, Order


def _order(order_id: str, count: int, fill_count: int, fill_cost: float, price: float) -> Order:
    return Order(
        order_id=order_id,
        client_order_id=None,
        ticker="KXBTC-26APR4PM-B95000",
        side="yes",
        action="buy",
        status="executed" if fill_count >= count else "partial",
        yes_price=price,
        no_price=1.0 - price,
        count=count,
        fill_count=fill_count,
        taker_fill_cost=fill_cost,
        created_time="2026-04-16T12:00:00Z",
    )


class FakeKalshi:
    """Scriptable fake Kalshi client for execution tests."""

    def __init__(self, place_results, get_results=None):
        # place_results: list of Order objects or exceptions returned by place_order
        self.place_results = list(place_results)
        # get_results: dict[order_id] -> Order (post-timeout state)
        self.get_results = get_results or {}
        self.place_calls = []
        self.cancels = []

    def place_order(self, ticker, side, contracts, price, **kwargs):
        self.place_calls.append((ticker, side, contracts, price, kwargs))
        result = self.place_results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    def get_order(self, order_id):
        return self.get_results.get(order_id) or _order(order_id, 0, 0, 0.0, 0.0)

    def cancel_order(self, order_id):
        self.cancels.append(order_id)

    def get_market(self, ticker):
        return Market(
            ticker=ticker,
            event_ticker="KXBTC",
            status="open",
            close_time="2026-04-26T20:00:00Z",
            yes_ask=0.45,
            yes_bid=0.40,
            no_ask=0.55,
            no_bid=0.50,
            last_price=0.43,
        )


@pytest.fixture(autouse=True)
def _fast_price_improvement(monkeypatch):
    """Don't actually sleep or hit the network during tests."""
    monkeypatch.setattr(main_mod.time, "sleep", lambda _s: None)
    monkeypatch.setattr(main_mod, "get_spot_price", lambda symbol="BTC": 95000.0)
    monkeypatch.setattr(cfg, "ENABLE_MAKER_ORDERS", False)   # tested separately
    monkeypatch.setattr(cfg, "ENABLE_PRICE_IMPROVEMENT", True)
    monkeypatch.setattr(cfg, "PRICE_IMPROVEMENT_TIMEOUT_SEC", 0)
    monkeypatch.setattr(cfg, "MAKER_ORDER_TIMEOUT_SEC", 0)
    monkeypatch.setattr(cfg, "STALE_ORDER_POLL_SEC", 10)
    monkeypatch.setattr(cfg, "STALE_ORDER_SPOT_MOVE_PCT", 0.003)


class TestExecuteWithPriceImprovement:
    def test_full_passive_fill_returns_single_order(self):
        full = _order("o1", count=10, fill_count=10, fill_cost=4.00, price=0.40)
        kalshi = FakeKalshi(place_results=[full], get_results={"o1": full})

        orders = main_mod._execute_with_price_improvement(
            kalshi=kalshi, ticker="KXBTC-26APR4PM-B95000", side="yes",
            contracts=10, ask_price=0.45, mid_price=0.42, bid_price=0.39, dry_run=False,
        )

        assert len(orders) == 1
        assert orders[0].fill_count == 10
        assert orders[0].taker_fill_cost == pytest.approx(4.00)
        assert kalshi.cancels == []
        assert kalshi.place_calls == [("KXBTC-26APR4PM-B95000", "yes", 10, 0.40, {"post_only": True})]

    def test_partial_passive_fill_returns_partial_after_cancel(self):
        passive_partial = _order("o1", count=10, fill_count=3, fill_cost=1.20, price=0.40)
        kalshi = FakeKalshi(
            place_results=[passive_partial],
            get_results={"o1": passive_partial},
        )

        orders = main_mod._execute_with_price_improvement(
            kalshi=kalshi, ticker="KXBTC-26APR4PM-B95000", side="yes",
            contracts=10, ask_price=0.45, mid_price=0.42, bid_price=0.39, dry_run=False,
        )

        assert len(orders) == 1
        assert orders[0].order_id == "o1"
        assert orders[0].fill_count == 3
        assert kalshi.cancels == ["o1"]
        assert len(kalshi.place_calls) == 1

    def test_zero_passive_fill_returns_empty_after_cancel(self):
        passive_none = _order("o1", count=10, fill_count=0, fill_cost=0.0, price=0.40)
        kalshi = FakeKalshi(
            place_results=[passive_none],
            get_results={"o1": passive_none},
        )

        orders = main_mod._execute_with_price_improvement(
            kalshi=kalshi, ticker="KXBTC-26APR4PM-B95000", side="yes",
            contracts=10, ask_price=0.45, mid_price=0.42, dry_run=False,
        )

        assert orders == []
        assert kalshi.cancels == ["o1"]

    def test_disabled_price_improvement_still_posts_passive(self, monkeypatch):
        monkeypatch.setattr(cfg, "ENABLE_PRICE_IMPROVEMENT", False)
        passive_fill = _order("o1", count=10, fill_count=10, fill_cost=4.00, price=0.40)
        kalshi = FakeKalshi(place_results=[passive_fill])

        orders = main_mod._execute_with_price_improvement(
            kalshi=kalshi, ticker="KXBTC-26APR4PM-B95000", side="yes",
            contracts=10, ask_price=0.45, mid_price=0.42, bid_price=0.39, dry_run=False,
        )

        assert len(orders) == 1
        assert kalshi.place_calls == [("KXBTC-26APR4PM-B95000", "yes", 10, 0.40, {"post_only": True})]
        assert kalshi.cancels == []

    def test_spot_drift_cancels_and_skips_ask_fallback(self, monkeypatch):
        """If BTC spot drifts past the threshold during the mid wait, cancel and abort."""
        monkeypatch.setattr(cfg, "PRICE_IMPROVEMENT_TIMEOUT_SEC", 30)
        monkeypatch.setattr(cfg, "STALE_ORDER_POLL_SEC", 10)
        monkeypatch.setattr(cfg, "STALE_ORDER_SPOT_MOVE_PCT", 0.003)
        # Entry spot 95000; first poll sees 95500 (~0.53% drift) → cancel
        spot_sequence = iter([95000.0, 95500.0])
        monkeypatch.setattr(main_mod, "get_spot_price", lambda symbol="BTC": next(spot_sequence))

        passive_partial = _order("o1", count=10, fill_count=0, fill_cost=0.0, price=0.40)
        kalshi = FakeKalshi(
            place_results=[passive_partial],
            get_results={"o1": passive_partial},
        )

        orders = main_mod._execute_with_price_improvement(
            kalshi=kalshi, ticker="KXBTC-26APR4PM-B95000", side="yes",
            contracts=10, ask_price=0.45, mid_price=0.42, bid_price=0.39, dry_run=False,
        )

        # Passive order cancelled, no taker-style fallback placed → no fills returned
        assert orders == []
        assert kalshi.cancels == ["o1"]
        # Only one passive placement; no ask fallback
        assert len(kalshi.place_calls) == 1

    def test_spot_within_tolerance_keeps_partial_passive_fill_only(self, monkeypatch):
        """If BTC drift stays within threshold, maker-first still cancels remainder instead of crossing."""
        monkeypatch.setattr(cfg, "STALE_ORDER_POLL_SEC", 10)
        monkeypatch.setattr(cfg, "STALE_ORDER_SPOT_MOVE_PCT", 0.01)  # 1% tolerance
        # Entry 95000, poll sees 95100 (~0.1% drift, under threshold)
        spot_sequence = iter([95000.0, 95100.0])
        monkeypatch.setattr(main_mod, "get_spot_price", lambda symbol="BTC": next(spot_sequence))

        passive_partial = _order("o1", count=10, fill_count=3, fill_cost=1.20, price=0.40)
        kalshi = FakeKalshi(
            place_results=[passive_partial],
            get_results={"o1": passive_partial},
        )

        orders = main_mod._execute_with_price_improvement(
            kalshi=kalshi, ticker="KXBTC-26APR4PM-B95000", side="yes",
            contracts=10, ask_price=0.45, mid_price=0.42, bid_price=0.39, dry_run=False,
        )

        assert len(orders) == 1
        assert kalshi.cancels == ["o1"]
        assert len(kalshi.place_calls) == 1

    def test_mid_at_or_above_ask_still_uses_passive_bid(self):
        passive_fill = _order("o1", count=10, fill_count=10, fill_cost=4.00, price=0.40)
        kalshi = FakeKalshi(place_results=[passive_fill], get_results={"o1": passive_fill})

        orders = main_mod._execute_with_price_improvement(
            kalshi=kalshi, ticker="KXBTC-26APR4PM-B95000", side="yes",
            contracts=10, ask_price=0.45, mid_price=0.45, bid_price=0.41, dry_run=False,
        )

        assert len(orders) == 1
        assert kalshi.place_calls == [("KXBTC-26APR4PM-B95000", "yes", 10, 0.40, {"post_only": True})]


def _http_error(status_code: int) -> requests.exceptions.HTTPError:
    resp = MagicMock()
    resp.status_code = status_code
    return requests.exceptions.HTTPError(response=resp)


class TestTakerFallback:
    def test_post_only_rejected_falls_back_to_taker(self):
        taker_fill = _order("o2", count=5, fill_count=5, fill_cost=2.25, price=0.45)
        kalshi = FakeKalshi(place_results=[_http_error(400), taker_fill])

        orders = main_mod._execute_with_price_improvement(
            kalshi=kalshi, ticker="KXBTC-26APR4PM-B95000", side="yes",
            contracts=5, ask_price=0.45, mid_price=0.42, dry_run=False,
            taker_edge=0.30, required_edge=0.25,
        )

        assert len(orders) == 1
        assert orders[0].order_id == "o2"
        assert orders[0].fill_count == 5
        # First call: post_only maker; second call: taker at ask_price (no post_only kwarg)
        assert kalshi.place_calls[0][4] == {"post_only": True}
        assert "post_only" not in kalshi.place_calls[1][4]
        assert kalshi.place_calls[1][3] == pytest.approx(0.45)

    def test_post_only_rejected_skips_taker_when_edge_no_longer_clears_hurdle(self):
        kalshi = FakeKalshi(place_results=[_http_error(400)])

        orders = main_mod._execute_with_price_improvement(
            kalshi=kalshi, ticker="KXBTC-26APR4PM-B95000", side="yes",
            contracts=5, ask_price=0.45, mid_price=0.42, dry_run=False,
            taker_edge=0.20, required_edge=0.25,
        )

        assert orders == []
        assert len(kalshi.place_calls) == 1

    def test_post_only_rejected_taker_also_fails_returns_empty(self):
        kalshi = FakeKalshi(place_results=[_http_error(400), _http_error(422)])

        orders = main_mod._execute_with_price_improvement(
            kalshi=kalshi, ticker="KXBTC-26APR4PM-B95000", side="yes",
            contracts=5, ask_price=0.45, mid_price=0.42, dry_run=False,
            taker_edge=0.30, required_edge=0.25,
        )

        assert orders == []
        assert len(kalshi.place_calls) == 2

    def test_5xx_error_does_not_trigger_taker_fallback(self):
        """Network/server errors should abort — not risk a double order."""
        kalshi = FakeKalshi(place_results=[_http_error(503)])

        orders = main_mod._execute_with_price_improvement(
            kalshi=kalshi, ticker="KXBTC-26APR4PM-B95000", side="yes",
            contracts=5, ask_price=0.45, mid_price=0.42, dry_run=False,
        )

        assert orders == []
        assert len(kalshi.place_calls) == 1  # only the initial post_only attempt


class TestSigtermDuringMakerWait:
    def test_stop_event_cancels_resting_order_and_returns_partial(self, monkeypatch):
        monkeypatch.setattr(cfg, "MAKER_ENTRY_TIMEOUT_SEC", 60)
        monkeypatch.setattr(cfg, "STALE_ORDER_POLL_SEC", 1)

        passive_partial = _order("o1", count=10, fill_count=3, fill_cost=1.20, price=0.40)
        kalshi = FakeKalshi(
            place_results=[passive_partial],
            get_results={"o1": passive_partial},
        )

        call_count = [0]

        def _sleep_then_stop(s):
            call_count[0] += 1
            if call_count[0] >= 1:
                main_mod._stop_event.set()

        monkeypatch.setattr(main_mod.time, "sleep", _sleep_then_stop)
        main_mod._stop_event.clear()

        try:
            orders = main_mod._execute_with_price_improvement(
                kalshi=kalshi, ticker="KXBTC-26APR4PM-B95000", side="yes",
                contracts=10, ask_price=0.45, mid_price=0.42, dry_run=False,
            )
        finally:
            main_mod._stop_event.clear()

        assert len(orders) == 1
        assert orders[0].fill_count == 3
        assert kalshi.cancels == ["o1"]

    def test_stop_event_zero_fill_returns_empty(self, monkeypatch):
        monkeypatch.setattr(cfg, "MAKER_ENTRY_TIMEOUT_SEC", 60)
        monkeypatch.setattr(cfg, "STALE_ORDER_POLL_SEC", 1)

        unfilled = _order("o1", count=10, fill_count=0, fill_cost=0.0, price=0.40)
        kalshi = FakeKalshi(place_results=[unfilled], get_results={"o1": unfilled})

        def _sleep_and_stop(s):
            main_mod._stop_event.set()

        monkeypatch.setattr(main_mod.time, "sleep", _sleep_and_stop)
        main_mod._stop_event.clear()

        try:
            orders = main_mod._execute_with_price_improvement(
                kalshi=kalshi, ticker="KXBTC-26APR4PM-B95000", side="yes",
                contracts=10, ask_price=0.45, mid_price=0.42, dry_run=False,
            )
        finally:
            main_mod._stop_event.clear()

        assert orders == []
        assert kalshi.cancels == ["o1"]
