"""Tests for bot/store.py — persistence layer using in-memory SQLite."""
import sqlite3

import pytest
from pathlib import Path
from bot.store import Store
from bot.kalshi_client import Order
from bot.models import SignalDecision


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

    def test_returns_filled_orders_until_settlement_recorded(self, store):
        order = _make_order(status="filled")
        store.log_order(order, theo_prob=0.67, gross_edge=0.22, edge=0.15, fee=0.07)
        unfilled = store.get_unfilled_orders()
        assert order.order_id in unfilled

    def test_does_not_return_settled_orders_with_outcome(self, store):
        order = _make_order(status="settled", fill_count=1, taker_fill_cost=0.45)
        store.log_order(order, theo_prob=0.67, gross_edge=0.22, edge=0.15, fee=0.07)
        store._conn.execute(
            "UPDATE orders SET settled_value = 1.0 WHERE order_id = ?",
            (order.order_id,),
        )
        store._conn.commit()
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


class TestUpdateOrderFill:
    def test_realized_edge_for_no_side_uses_logged_contract_probability(self, store):
        order = _make_order(
            order_id="no-1",
            side="no",
            yes_price=0.0,
            no_price=0.55,
            status="resting",
        )
        store.log_order(order, theo_prob=0.82, gross_edge=0.27, edge=0.20, fee=0.07)

        filled = _make_order(
            order_id="no-1",
            side="no",
            yes_price=0.0,
            no_price=0.55,
            status="filled",
            fill_count=2,
            taker_fill_cost=1.00,
        )
        store.update_order_fill(filled)

        row = store._conn.execute(
            "SELECT fill_price_dollars, realized_edge FROM orders WHERE order_id = ?",
            ("no-1",),
        ).fetchone()
        assert row["fill_price_dollars"] == pytest.approx(0.50)
        assert row["realized_edge"] == pytest.approx(0.82 - 0.50 - 0.07)

    def test_fill_cost_includes_maker_fill_cost(self, store):
        order = _make_order(
            order_id="maker-1",
            status="resting",
            yes_price=0.44,
        )
        store.log_order(order, theo_prob=0.67, gross_edge=0.23, edge=0.23, fee=0.0)

        filled = _make_order(
            order_id="maker-1",
            status="filled",
            yes_price=0.44,
            fill_count=3,
            taker_fill_cost=0.0,
            maker_fill_cost=1.32,
        )
        store.update_order_fill(filled)

        row = store._conn.execute(
            "SELECT cost_dollars, fill_price_dollars, realized_edge FROM orders WHERE order_id = ?",
            ("maker-1",),
        ).fetchone()
        assert row["cost_dollars"] == pytest.approx(1.32)
        assert row["fill_price_dollars"] == pytest.approx(0.44)
        assert row["realized_edge"] == pytest.approx(0.67 - 0.44)

    def test_fill_cost_falls_back_to_limit_price_when_cost_fields_missing(self, store):
        order = _make_order(order_id="maker-fallback", status="resting", yes_price=0.44)
        store.log_order(order, theo_prob=0.67, gross_edge=0.23, edge=0.23, fee=0.0)

        filled = _make_order(
            order_id="maker-fallback",
            status="filled",
            yes_price=0.44,
            fill_count=3,
            taker_fill_cost=0.0,
            maker_fill_cost=0.0,
        )
        store.update_order_fill(filled)

        row = store._conn.execute(
            "SELECT cost_dollars, fill_price_dollars FROM orders WHERE order_id = ?",
            ("maker-fallback",),
        ).fetchone()
        assert row["cost_dollars"] == pytest.approx(1.32)
        assert row["fill_price_dollars"] == pytest.approx(0.44)

    def test_settlement_uses_market_outcome_not_order_fill_count(self, store):
        yes_order = _make_order(
            order_id="settle-yes",
            side="yes",
            status="filled",
            fill_count=1,
            taker_fill_cost=0.45,
        )
        no_order = _make_order(
            order_id="settle-no",
            side="no",
            yes_price=0.0,
            no_price=0.55,
            status="filled",
            fill_count=1,
            taker_fill_cost=0.55,
        )
        store.log_order(yes_order, theo_prob=0.60, gross_edge=0.15, edge=0.08, fee=0.07)
        store.log_order(no_order, theo_prob=0.40, gross_edge=-0.15, edge=-0.22, fee=0.07)
        store.upsert_market_outcome(
            ticker=yes_order.ticker,
            result="no",
            settlement_value=0.0,
            close_time="2099-04-26T20:00:00Z",
            settlement_ts="2099-04-26T20:01:00Z",
        )

        store.update_order_fill(_make_order(order_id="settle-yes", side="yes", status="settled", fill_count=1, taker_fill_cost=0.45))
        store.update_order_fill(_make_order(order_id="settle-no", side="no", yes_price=0.0, no_price=0.55, status="settled", fill_count=1, taker_fill_cost=0.55))

        rows = {
            row["order_id"]: row["settled_value"]
            for row in store._conn.execute("SELECT order_id, settled_value FROM orders").fetchall()
        }
        assert rows["settle-yes"] == pytest.approx(0.0)
        assert rows["settle-no"] == pytest.approx(1.0)


class TestMigrations:
    def test_backfills_legacy_no_side_probability_once(self, store):
        order = _make_order(
            order_id="legacy-no-1",
            side="no",
            yes_price=0.0,
            no_price=0.55,
            status="filled",
            fill_count=2,
            taker_fill_cost=1.00,
        )
        # Legacy bug: stored YES probability instead of NO contract probability.
        store.log_order(order, theo_prob=0.18, gross_edge=0.27, edge=0.20, fee=0.07)
        store._conn.execute(
            "UPDATE orders SET fill_price_dollars = 0.50, realized_edge = -0.39 WHERE order_id = ?",
            (order.order_id,),
        )
        store._conn.execute(
            "DELETE FROM meta WHERE key = 'backfill_no_side_contract_prob_v1'",
        )
        store._conn.commit()

        store._migrate()

        row = store._conn.execute(
            "SELECT theo_prob, realized_edge FROM orders WHERE order_id = ?",
            (order.order_id,),
        ).fetchone()
        assert row["theo_prob"] == pytest.approx(0.82)
        assert row["realized_edge"] == pytest.approx(0.82 - 0.50 - 0.07)

    def test_adds_bucket_columns_to_existing_signal_decisions_table(self, tmp_path):
        db_path = tmp_path / "legacy.db"
        conn = sqlite3.connect(db_path)
        conn.execute("""
            CREATE TABLE signal_decisions (
                cycle_id TEXT NOT NULL,
                symbol TEXT NOT NULL,
                ticker TEXT NOT NULL,
                side TEXT NOT NULL,
                eligible INTEGER,
                score REAL,
                required_edge REAL,
                expected_slippage REAL,
                uncertainty_penalty REAL,
                realized_edge_proxy REAL,
                reject_reason TEXT,
                theo_prob REAL,
                ask REAL,
                bid REAL,
                mid_price REAL,
                gross_edge REAL,
                edge REAL,
                fee REAL,
                hours_to_expiry REAL,
                strike REAL,
                distance_from_spot_sigma REAL,
                degraded INTEGER,
                chain_break_ratio REAL,
                top_of_book_size REAL,
                resting_size_at_entry REAL,
                cumulative_size_at_entry REAL,
                expected_fill_price REAL,
                depth_slippage REAL,
                orderbook_imbalance REAL,
                orderbook_available INTEGER,
                logged_at TEXT NOT NULL,
                PRIMARY KEY (cycle_id, ticker, side)
            )
        """)
        conn.commit()
        conn.close()

        s = Store(db_path=db_path, trades_csv_path=tmp_path / "trades.csv")
        s.open()
        try:
            cols = {row[1] for row in s._conn.execute("PRAGMA table_info(signal_decisions)").fetchall()}
            assert "bucket_avg_realized_edge" in cols
            assert "bucket_sample_size" in cols
            assert "maker_fill_prob" in cols

            s.log_signal_decision(
                "cycle-1",
                SignalDecision(
                    symbol="BTC",
                    ticker="KXBTC-26APR4PM-B95000",
                    side="yes",
                    eligible=False,
                    score=0.01,
                    required_edge=0.20,
                    expected_slippage=0.01,
                    uncertainty_penalty=0.01,
                    realized_edge_proxy=0.01,
                    reject_reason="edge_below_hurdle",
                    theo_prob=0.60,
                    ask=0.45,
                    bid=0.42,
                    mid_price=0.435,
                    gross_edge=0.15,
                    edge=0.13,
                    fee=0.02,
                    hours_to_expiry=4.0,
                    strike=95000.0,
                    distance_from_spot_sigma=0.5,
                    degraded=False,
                    chain_break_ratio=0.0,
                    bucket_avg_realized_edge=-0.01,
                    bucket_sample_size=12,
                    maker_fill_prob=0.4,
                ),
            )
            row = s._conn.execute(
                "SELECT bucket_avg_realized_edge, bucket_sample_size, maker_fill_prob FROM signal_decisions"
            ).fetchone()
            assert row["bucket_avg_realized_edge"] == pytest.approx(-0.01)
            assert row["bucket_sample_size"] == 12
            assert row["maker_fill_prob"] == pytest.approx(0.4)
        finally:
            s.close()


class TestGetSlippageFactor:
    def test_returns_none_when_insufficient_data(self, store):
        assert store.get_slippage_factor(min_trades=5) is None

    def test_ratio_clamped_to_one_when_realized_exceeds_predicted(self, store):
        # predicted edge=0.10, realized edge=0.20 → ratio 2.0, clamped to 1.0
        for i in range(5):
            order = _make_order(order_id=f"ord-{i}", status="filled", fill_count=1, taker_fill_cost=0.45)
            store.log_order(order, theo_prob=0.67, gross_edge=0.15, edge=0.10, fee=0.05)
            store._conn.execute(
                "UPDATE orders SET realized_edge = 0.20 WHERE order_id = ?",
                (order.order_id,),
            )
        store._conn.commit()
        factor = store.get_slippage_factor(min_trades=5)
        assert factor == 1.0

    def test_ratio_below_one_when_realized_is_less_than_predicted(self, store):
        # predicted edge=0.20, realized edge=0.10 → ratio 0.5
        for i in range(5):
            order = _make_order(order_id=f"ord-{i}", status="filled", fill_count=1, taker_fill_cost=0.45)
            store.log_order(order, theo_prob=0.67, gross_edge=0.25, edge=0.20, fee=0.05)
            store._conn.execute(
                "UPDATE orders SET realized_edge = 0.10 WHERE order_id = ?",
                (order.order_id,),
            )
        store._conn.commit()
        factor = store.get_slippage_factor(min_trades=5)
        assert factor == pytest.approx(0.5)

    def test_ratio_floored_at_0_3(self, store):
        # predicted=0.20, realized=0.01 → ratio ~0.05, floored to 0.3
        for i in range(5):
            order = _make_order(order_id=f"ord-{i}", status="filled", fill_count=1, taker_fill_cost=0.45)
            store.log_order(order, theo_prob=0.67, gross_edge=0.25, edge=0.20, fee=0.05)
            store._conn.execute(
                "UPDATE orders SET realized_edge = 0.01 WHERE order_id = ?",
                (order.order_id,),
            )
        store._conn.commit()
        factor = store.get_slippage_factor(min_trades=5)
        assert factor == 0.3


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
