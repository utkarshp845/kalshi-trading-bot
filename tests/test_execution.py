"""Tests for _execute_with_price_improvement in bot/main.py."""
from dataclasses import replace

import pytest

import bot.config as cfg
import bot.main as main_mod
from bot.kalshi_client import Order


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
        # place_results: list of Order objects returned in order by place_order
        self.place_results = list(place_results)
        # get_results: dict[order_id] -> Order (post-timeout state)
        self.get_results = get_results or {}
        self.place_calls = []
        self.cancels = []

    def place_order(self, ticker, side, contracts, price):
        self.place_calls.append((ticker, side, contracts, price))
        return self.place_results.pop(0)

    def get_order(self, order_id):
        return self.get_results.get(order_id) or _order(order_id, 0, 0, 0.0, 0.0)

    def cancel_order(self, order_id):
        self.cancels.append(order_id)


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
    def test_full_mid_fill_returns_single_order(self):
        full = _order("o1", count=10, fill_count=10, fill_cost=4.20, price=0.42)
        kalshi = FakeKalshi(place_results=[full], get_results={"o1": full})

        orders = main_mod._execute_with_price_improvement(
            kalshi=kalshi, ticker="KXBTC-26APR4PM-B95000", side="yes",
            contracts=10, ask_price=0.45, mid_price=0.42, bid_price=0.39, dry_run=False,
        )

        assert len(orders) == 1
        assert orders[0].fill_count == 10
        assert orders[0].taker_fill_cost == pytest.approx(4.20)
        assert kalshi.cancels == []

    def test_partial_mid_then_ask_returns_both_orders(self):
        """The bug this fixes: partial mid + ask fallback must account for BOTH fills."""
        mid_partial = _order("o1", count=10, fill_count=3, fill_cost=1.26, price=0.42)
        ask_fill = _order("o2", count=7, fill_count=7, fill_cost=3.15, price=0.45)
        kalshi = FakeKalshi(
            place_results=[mid_partial, ask_fill],
            get_results={"o1": mid_partial},
        )

        orders = main_mod._execute_with_price_improvement(
            kalshi=kalshi, ticker="KXBTC-26APR4PM-B95000", side="yes",
            contracts=10, ask_price=0.45, mid_price=0.42, bid_price=0.39, dry_run=False,
        )

        assert len(orders) == 2
        total_filled = sum(o.fill_count for o in orders)
        total_cost = sum(o.taker_fill_cost for o in orders)
        assert total_filled == 10
        assert total_cost == pytest.approx(4.41)
        # Cancel the unfilled remainder of the mid order before re-placing at ask.
        assert kalshi.cancels == ["o1"]
        # Second placement is the remainder at ask.
        assert kalshi.place_calls[1] == ("KXBTC-26APR4PM-B95000", "yes", 7, 0.45)

    def test_zero_mid_fill_then_full_ask_returns_only_ask(self):
        """When mid fills nothing, only the ask order should be logged (no phantom order1)."""
        mid_none = _order("o1", count=10, fill_count=0, fill_cost=0.0, price=0.42)
        ask_fill = _order("o2", count=10, fill_count=10, fill_cost=4.50, price=0.45)
        kalshi = FakeKalshi(
            place_results=[mid_none, ask_fill],
            get_results={"o1": mid_none},
        )

        orders = main_mod._execute_with_price_improvement(
            kalshi=kalshi, ticker="KXBTC-26APR4PM-B95000", side="yes",
            contracts=10, ask_price=0.45, mid_price=0.42, bid_price=0.39, dry_run=False,
        )

        assert len(orders) == 1
        assert orders[0].order_id == "o2"
        assert orders[0].taker_fill_cost == pytest.approx(4.50)
        assert kalshi.cancels == ["o1"]

    def test_disabled_price_improvement_skips_mid(self, monkeypatch):
        monkeypatch.setattr(cfg, "ENABLE_PRICE_IMPROVEMENT", False)
        ask_fill = _order("o1", count=10, fill_count=10, fill_cost=4.50, price=0.45)
        kalshi = FakeKalshi(place_results=[ask_fill])

        orders = main_mod._execute_with_price_improvement(
            kalshi=kalshi, ticker="KXBTC-26APR4PM-B95000", side="yes",
            contracts=10, ask_price=0.45, mid_price=0.42, bid_price=0.39, dry_run=False,
        )

        assert len(orders) == 1
        assert kalshi.place_calls == [("KXBTC-26APR4PM-B95000", "yes", 10, 0.45)]
        assert kalshi.cancels == []

    def test_spot_drift_cancels_and_skips_ask_fallback(self, monkeypatch):
        """If BTC spot drifts past the threshold during the mid wait, cancel and abort."""
        monkeypatch.setattr(cfg, "PRICE_IMPROVEMENT_TIMEOUT_SEC", 30)
        monkeypatch.setattr(cfg, "STALE_ORDER_POLL_SEC", 10)
        monkeypatch.setattr(cfg, "STALE_ORDER_SPOT_MOVE_PCT", 0.003)
        # Entry spot 95000; first poll sees 95500 (~0.53% drift) → cancel
        spot_sequence = iter([95000.0, 95500.0])
        monkeypatch.setattr(main_mod, "get_spot_price", lambda symbol="BTC": next(spot_sequence))

        mid_partial = _order("o1", count=10, fill_count=0, fill_cost=0.0, price=0.42)
        kalshi = FakeKalshi(
            place_results=[mid_partial],
            get_results={"o1": mid_partial},
        )

        orders = main_mod._execute_with_price_improvement(
            kalshi=kalshi, ticker="KXBTC-26APR4PM-B95000", side="yes",
            contracts=10, ask_price=0.45, mid_price=0.42, bid_price=0.39, dry_run=False,
        )

        # Mid order cancelled, no ask fallback placed → no fills returned
        assert orders == []
        assert kalshi.cancels == ["o1"]
        # Only one place_order call (the mid); no ask fallback
        assert len(kalshi.place_calls) == 1

    def test_spot_within_tolerance_continues_to_ask_fallback(self, monkeypatch):
        """If BTC drift stays within threshold, normal ask fallback proceeds on remainder."""
        monkeypatch.setattr(cfg, "PRICE_IMPROVEMENT_TIMEOUT_SEC", 10)
        monkeypatch.setattr(cfg, "STALE_ORDER_POLL_SEC", 10)
        monkeypatch.setattr(cfg, "STALE_ORDER_SPOT_MOVE_PCT", 0.01)  # 1% tolerance
        # Entry 95000, poll sees 95100 (~0.1% drift, under threshold)
        spot_sequence = iter([95000.0, 95100.0])
        monkeypatch.setattr(main_mod, "get_spot_price", lambda symbol="BTC": next(spot_sequence))

        mid_partial = _order("o1", count=10, fill_count=3, fill_cost=1.26, price=0.42)
        ask_fill = _order("o2", count=7, fill_count=7, fill_cost=3.15, price=0.45)
        kalshi = FakeKalshi(
            place_results=[mid_partial, ask_fill],
            get_results={"o1": mid_partial},
        )

        orders = main_mod._execute_with_price_improvement(
            kalshi=kalshi, ticker="KXBTC-26APR4PM-B95000", side="yes",
            contracts=10, ask_price=0.45, mid_price=0.42, bid_price=0.39, dry_run=False,
        )

        assert len(orders) == 2
        assert kalshi.cancels == ["o1"]
        assert kalshi.place_calls[1] == ("KXBTC-26APR4PM-B95000", "yes", 7, 0.45)

    def test_mid_at_or_above_ask_skips_improvement(self):
        ask_fill = _order("o1", count=10, fill_count=10, fill_cost=4.50, price=0.45)
        kalshi = FakeKalshi(place_results=[ask_fill])

        orders = main_mod._execute_with_price_improvement(
            kalshi=kalshi, ticker="KXBTC-26APR4PM-B95000", side="yes",
            contracts=10, ask_price=0.45, mid_price=0.45, bid_price=0.41, dry_run=False,
        )

        assert len(orders) == 1
        assert kalshi.place_calls == [("KXBTC-26APR4PM-B95000", "yes", 10, 0.45)]


class TestMakerBidPhase:
    """Tests for the Phase 0 maker-bid logic."""

    @pytest.fixture(autouse=True)
    def _enable_maker(self, monkeypatch):
        monkeypatch.setattr(cfg, "ENABLE_MAKER_ORDERS", True)
        monkeypatch.setattr(cfg, "MAKER_ORDER_TIMEOUT_SEC", 0)

    def test_full_maker_fill_no_further_phases(self):
        full = _order("o1", count=10, fill_count=10, fill_cost=3.90, price=0.39)
        kalshi = FakeKalshi(place_results=[full], get_results={"o1": full})

        orders = main_mod._execute_with_price_improvement(
            kalshi=kalshi, ticker="KXBTC-26APR4PM-B95000", side="yes",
            contracts=10, ask_price=0.45, mid_price=0.42, bid_price=0.39, dry_run=False,
        )

        assert len(orders) == 1
        assert orders[0].order_id == "o1"
        assert orders[0].taker_fill_cost == pytest.approx(3.90)
        # Only one order placed (the maker bid); no mid or ask needed
        assert len(kalshi.place_calls) == 1
        assert kalshi.place_calls[0][3] == 0.39  # bid price

    def test_maker_unfilled_escalates_to_mid(self):
        maker_none = _order("o1", count=10, fill_count=0, fill_cost=0.0, price=0.39)
        mid_full = _order("o2", count=10, fill_count=10, fill_cost=4.20, price=0.42)
        kalshi = FakeKalshi(
            place_results=[maker_none, mid_full],
            get_results={"o1": maker_none, "o2": mid_full},
        )

        orders = main_mod._execute_with_price_improvement(
            kalshi=kalshi, ticker="KXBTC-26APR4PM-B95000", side="yes",
            contracts=10, ask_price=0.45, mid_price=0.42, bid_price=0.39, dry_run=False,
        )

        assert len(orders) == 1
        assert orders[0].order_id == "o2"
        assert kalshi.cancels == ["o1"]  # maker order cancelled
        assert kalshi.place_calls[1][3] == 0.42  # mid price used next
