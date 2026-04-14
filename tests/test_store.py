"""Tests for bot/store.py — persistence layer using in-memory SQLite."""
import pytest
from pathlib import Path
from bot.store import Store
from bot.kalshi_client import Order


@pytest.fixture
def store(tmp_path):
    s = Store(
        db_path=tmp_path / "test.db",
        trades_csv_path=tmp_path / "trades.csv",
    )
    s.open()
    yield s
    s.close()


def _make_order(**kwargs) -> Order:
    defaults = dict(
        order_id="test-order-001",
        client_order_id=None,
        ticker="KXBTC-26APR4PM-B95000",
        side="yes",
        action="buy",
        status="resting",
        yes_price=0.45,
        no_price=0.0,
        count=2,
        fill_count=0,
        taker_fill_cost=0.0,
        created_time="2026-04-13T14:00:00Z",
    )
    defaults.update(kwargs)
    return Order(**defaults)


class TestGetTodaysSpend:
    def test_empty_db_returns_zero(self, store):
        assert store.get_todays_spend() == 0.0

    def test_returns_sum_after_order(self, store):
        order = _make_order(taker_fill_cost=1.35)
        store.log_order(order, theo_prob=0.67, gross_edge=0.22, edge=0.15, fee=0.07)
        spend = store.get_todays_spend()
        assert abs(spend - 1.35) < 0.001


class TestGetUnfilledOrders:
    def test_empty_db(self, store):
        assert store.get_unfilled_orders() == []

    def test_returns_unfilled_order_id(self, store):
        order = _make_order(status="resting")
        store.log_order(order, theo_prob=0.67, gross_edge=0.22, edge=0.15, fee=0.07)
        unfilled = store.get_unfilled_orders()
        assert order.order_id in unfilled

    def test_does_not_return_filled_orders(self, store):
        order = _make_order(status="filled")
        store.log_order(order, theo_prob=0.67, gross_edge=0.22, edge=0.15, fee=0.07)
        unfilled = store.get_unfilled_orders()
        assert order.order_id not in unfilled


class TestLogOrder:
    def test_order_persisted(self, store):
        order = _make_order()
        store.log_order(order, theo_prob=0.67, gross_edge=0.22, edge=0.15, fee=0.07)
        rows = store._conn.execute("SELECT order_id FROM orders").fetchall()
        assert len(rows) == 1
        assert rows[0][0] == order.order_id

    def test_csv_created(self, store, tmp_path):
        order = _make_order()
        store.log_order(order, theo_prob=0.67, gross_edge=0.22, edge=0.15, fee=0.07)
        csv = tmp_path / "trades.csv"
        assert csv.exists()
        content = csv.read_text()
        assert order.order_id in content


class TestProbCalibrationBias:
    def test_returns_none_when_no_data(self, store):
        result = store.get_prob_calibration_bias(min_trades=1)
        assert result is None

    def test_returns_none_below_min_trades(self, store):
        # Log one order and update settled_value
        order = _make_order(status="settled", fill_count=2, taker_fill_cost=0.90)
        store.log_order(order, theo_prob=0.67, gross_edge=0.22, edge=0.15, fee=0.07)
        store._conn.execute(
            "UPDATE orders SET settled_value = 1.0 WHERE order_id = ?",
            (order.order_id,)
        )
        store._conn.commit()
        # min_trades=10 → not enough data
        assert store.get_prob_calibration_bias(min_trades=10) is None

    def test_positive_bias_when_wins_exceed_probability(self, store):
        # theo_prob=0.5, settled_value=1.0 → bias = 1.0 - 0.5 = +0.5
        for i in range(5):
            order = _make_order(order_id=f"ord-{i}", status="settled", fill_count=1, taker_fill_cost=0.45)
            store.log_order(order, theo_prob=0.50, gross_edge=0.05, edge=0.0, fee=0.05)
            store._conn.execute(
                "UPDATE orders SET settled_value = 1.0 WHERE order_id = ?",
                (order.order_id,)
            )
        store._conn.commit()
        bias = store.get_prob_calibration_bias(min_trades=5)
        assert bias is not None
        assert bias > 0  # model under-predicted probability
