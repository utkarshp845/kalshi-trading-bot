"""Tests for bot/report.py — daily report generation."""
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from bot.kalshi_client import Order
from bot.report import generate_report
from bot.store import Store


@pytest.fixture
def store(tmp_path):
    s = Store(db_path=tmp_path / "test.db", trades_csv_path=tmp_path / "trades.csv")
    s.open()
    yield s
    s.close()


def _order(**kwargs) -> Order:
    defaults = dict(
        order_id="o-001",
        client_order_id=None,
        ticker="KXBTC-26APR4PM-B95000",
        side="yes",
        action="buy",
        status="executed",
        yes_price=0.45,
        no_price=0.55,
        count=10,
        fill_count=10,
        taker_fill_cost=4.50,
        created_time="2026-04-16T12:00:00Z",
    )
    defaults.update(kwargs)
    return Order(**defaults)


class TestGenerateReport:
    def test_writes_file_for_empty_db(self, store, tmp_path):
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        out = generate_report(today, store._db_path, tmp_path / "reports")
        assert out.exists()
        content = out.read_text()
        assert f"Daily Report — {today}" in content
        assert "No orders placed" in content
        assert "No run records" in content

    def test_includes_opened_trades(self, store, tmp_path):
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        order = _order(order_id="o-1", taker_fill_cost=4.50)
        store.log_order(order, theo_prob=0.67, gross_edge=0.22, edge=0.15, fee=0.07)

        out = generate_report(today, store._db_path, tmp_path / "reports")
        content = out.read_text()
        assert "KXBTC-26APR4PM-B95000" in content
        assert "$4.50" in content
        assert "Orders placed**: 1" in content

    def test_realized_pnl_for_settled_winner(self, store, tmp_path):
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        order = _order(order_id="win-1", taker_fill_cost=4.50, fill_count=10)
        store.log_order(order, theo_prob=0.67, gross_edge=0.22, edge=0.15, fee=0.07)
        settled = replace(order, status="settled", fill_count=10, taker_fill_cost=4.50)
        store.update_order_fill(settled)

        out = generate_report(today, store._db_path, tmp_path / "reports")
        content = out.read_text()
        # Paid 4.50, 10 contracts won → payout 10.00, net +5.50
        assert "+$5.50" in content
        assert "WIN" in content

    def test_realized_pnl_for_settled_loser(self, store, tmp_path):
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        order = _order(order_id="loss-1", taker_fill_cost=4.50, fill_count=10)
        store.log_order(order, theo_prob=0.67, gross_edge=0.22, edge=0.15, fee=0.07)
        settled_loser = replace(order, status="settled", fill_count=0, taker_fill_cost=4.50)
        store.update_order_fill(settled_loser)

        out = generate_report(today, store._db_path, tmp_path / "reports")
        content = out.read_text()
        # Lost full cost of 4.50
        assert "-$4.50" in content
        assert "LOSS" in content

    def test_market_context_from_runs(self, store, tmp_path):
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        store.log_run(
            btc_price=95000.0, sigma_short=0.6, sigma_long=0.55,
            markets_scanned=20, signals_found=5, orders_placed=2,
            dry_run=False, iv_rv_ratio=1.2, adaptive_safety_margin=1.25,
        )
        store.log_run(
            btc_price=95500.0, sigma_short=0.62, sigma_long=0.56,
            markets_scanned=20, signals_found=3, orders_placed=1,
            dry_run=False, iv_rv_ratio=1.22, adaptive_safety_margin=1.25,
        )

        out = generate_report(today, store._db_path, tmp_path / "reports")
        content = out.read_text()
        assert "Cycles run: 2" in content
        assert "$95,000 – $95,500" in content
        assert "8 → 3" in content  # total signals → orders

    def test_snapshot_appears_in_summary(self, store, tmp_path):
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        store.snapshot_daily(balance=987.65, daily_spent=12.35, positions_count=3)

        out = generate_report(today, store._db_path, tmp_path / "reports")
        content = out.read_text()
        assert "$987.65" in content
        assert "$12.35" in content
        assert "Open positions**: 3" in content

    def test_other_dates_ignored(self, store, tmp_path):
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        # Manually insert an order logged on a different date
        other_date = "2020-01-01T12:00:00+00:00"
        store._conn.execute(
            "INSERT INTO orders (order_id, ticker, side, action, status, yes_price, "
            "no_price, count, fill_count, cost_dollars, theo_prob, gross_edge, edge, "
            "fee, created_time, logged_at) VALUES "
            "('old-1','TICK','yes','buy','executed',0.4,0.6,5,5,2.0,0.5,0.1,0.05,0.07,"
            "?,?)",
            (other_date, other_date),
        )
        store._conn.commit()

        out = generate_report(today, store._db_path, tmp_path / "reports")
        content = out.read_text()
        assert "old-1" not in content
        assert "No orders placed" in content
