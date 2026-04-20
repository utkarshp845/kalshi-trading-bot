"""
Daily trading report generator.

Queries the SQLite store and writes a markdown summary to
REPORTS_DIR/YYYY-MM-DD.md covering P&L, trade activity, fill quality,
model calibration, and market context for the chosen UTC date.

Usage:
    python -m bot.report                   # today (UTC)
    python -m bot.report --date 2026-04-15
"""
from __future__ import annotations

import argparse
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import bot.config as cfg
from bot.store import Store


# ----------------------------------------------------------------------
# Data containers
# ----------------------------------------------------------------------

@dataclass
class TradeRow:
    order_id: str
    ticker: str
    side: str
    action: str
    status: str
    count: int
    fill_count: int
    cost_dollars: float
    theo_prob: Optional[float]
    edge: Optional[float]
    fill_price: Optional[float]
    slippage: Optional[float]
    realized_edge: Optional[float]
    settled_value: Optional[float]
    logged_at: str

    @property
    def settled_pnl(self) -> Optional[float]:
        """Realized P&L in dollars once the contract has settled. None otherwise."""
        if self.settled_value is None:
            return None
        payout = self.settled_value * self.fill_count
        return payout - (self.cost_dollars or 0.0)


# ----------------------------------------------------------------------
# Queries
# ----------------------------------------------------------------------

def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _orders_opened_on(conn: sqlite3.Connection, date_str: str) -> list[TradeRow]:
    rows = conn.execute(
        """
        SELECT order_id, ticker, side, action, status, count, fill_count,
               cost_dollars, theo_prob, edge, fill_price_dollars, slippage,
               realized_edge, settled_value, logged_at
          FROM orders
         WHERE substr(logged_at, 1, 10) = ?
         ORDER BY logged_at ASC
        """,
        (date_str,),
    ).fetchall()
    return [
        TradeRow(
            order_id=r["order_id"],
            ticker=r["ticker"],
            side=r["side"],
            action=r["action"],
            status=r["status"],
            count=r["count"] or 0,
            fill_count=r["fill_count"] or 0,
            cost_dollars=r["cost_dollars"] or 0.0,
            theo_prob=r["theo_prob"],
            edge=r["edge"],
            fill_price=r["fill_price_dollars"],
            slippage=r["slippage"],
            realized_edge=r["realized_edge"],
            settled_value=r["settled_value"],
            logged_at=r["logged_at"],
        )
        for r in rows
    ]


def _orders_settled_on(conn: sqlite3.Connection, date_str: str) -> list[TradeRow]:
    rows = conn.execute(
        """
        SELECT order_id, ticker, side, action, status, count, fill_count,
               cost_dollars, theo_prob, edge, fill_price_dollars, slippage,
               realized_edge, settled_value, logged_at
          FROM orders
         WHERE settled_value IS NOT NULL
           AND substr(fill_checked_at, 1, 10) = ?
        """,
        (date_str,),
    ).fetchall()
    return [
        TradeRow(
            order_id=r["order_id"],
            ticker=r["ticker"],
            side=r["side"],
            action=r["action"],
            status=r["status"],
            count=r["count"] or 0,
            fill_count=r["fill_count"] or 0,
            cost_dollars=r["cost_dollars"] or 0.0,
            theo_prob=r["theo_prob"],
            edge=r["edge"],
            fill_price=r["fill_price_dollars"],
            slippage=r["slippage"],
            realized_edge=r["realized_edge"],
            settled_value=r["settled_value"],
            logged_at=r["logged_at"],
        )
        for r in rows
    ]


def _latest_snapshot(conn: sqlite3.Connection, date_str: str) -> Optional[sqlite3.Row]:
    return conn.execute(
        """
        SELECT balance, daily_spent, positions_count, logged_at
          FROM daily_snapshots
         WHERE snapshot_date = ?
         ORDER BY logged_at DESC
         LIMIT 1
        """,
        (date_str,),
    ).fetchone()


def _runs_on(conn: sqlite3.Connection, date_str: str) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT btc_price, sigma_short, sigma_long, markets_scanned,
               signals_found, orders_placed, dry_run, iv_rv_ratio,
               adaptive_safety_margin
          FROM runs
         WHERE substr(run_at, 1, 10) = ?
         ORDER BY run_at ASC
        """,
        (date_str,),
    ).fetchall()


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------

def _avg(values: list[float]) -> Optional[float]:
    clean = [v for v in values if v is not None]
    return sum(clean) / len(clean) if clean else None


def _fmt_money(v: Optional[float]) -> str:
    if v is None:
        return "-"
    sign = "-" if v < 0 else ""
    return f"{sign}${abs(v):.2f}"


def _fmt_signed_money(v: Optional[float]) -> str:
    if v is None:
        return "-"
    return f"{'+' if v >= 0 else '-'}${abs(v):.2f}"


def _fmt_num(v: Optional[float], digits: int = 4) -> str:
    return "-" if v is None else f"{v:.{digits}f}"


def _fmt_pct(v: Optional[float]) -> str:
    return "-" if v is None else f"{v * 100:.2f}%"


# ----------------------------------------------------------------------
# Markdown rendering
# ----------------------------------------------------------------------

def _render(
    date_str: str,
    opened: list[TradeRow],
    settled: list[TradeRow],
    snapshot: Optional[sqlite3.Row],
    runs: list[sqlite3.Row],
) -> str:
    lines: list[str] = []
    lines.append(f"# Daily Report - {date_str}")
    lines.append("")
    lines.append(f"_Generated {datetime.now(timezone.utc).isoformat(timespec='seconds')}_")
    lines.append("")

    # --- Summary ---
    realized_pnl = sum((t.settled_pnl or 0.0) for t in settled)
    opened_cost = sum(t.cost_dollars for t in opened if t.status != "canceled")
    filled_count = sum(1 for t in opened if t.fill_count > 0)
    canceled_count = sum(1 for t in opened if t.status == "canceled")

    lines.append("## Summary")
    lines.append("")
    lines.append(f"- **Realized P&L** (settled today): {_fmt_signed_money(realized_pnl)}")
    lines.append(f"- **Capital deployed** (opened today): {_fmt_money(opened_cost)}")
    lines.append(f"- **Orders placed**: {len(opened)}  (filled: {filled_count}, canceled: {canceled_count})")
    if snapshot is not None:
        lines.append(f"- **End-of-day balance**: {_fmt_money(snapshot['balance'])}")
        lines.append(f"- **Daily spent**: {_fmt_money(snapshot['daily_spent'])}")
        lines.append(f"- **Open positions**: {snapshot['positions_count']}")
    else:
        lines.append("- No balance snapshot recorded for this date.")
    lines.append("")

    # --- Trades opened today ---
    lines.append("## Trades Opened")
    lines.append("")
    if not opened:
        lines.append("_No orders placed._")
    else:
        lines.append("| Time (UTC) | Ticker | Side | Qty | Fill | Cost | Edge | Status |")
        lines.append("|---|---|---|---|---|---|---|---|")
        for t in opened:
            tstamp = t.logged_at[11:19] if len(t.logged_at) >= 19 else t.logged_at
            edge_str = _fmt_num(t.edge, 3) if t.edge is not None else "-"
            lines.append(
                f"| {tstamp} | {t.ticker} | {t.side} | {t.count} | {t.fill_count} | "
                f"{_fmt_money(t.cost_dollars)} | {edge_str} | {t.status} |"
            )
    lines.append("")

    # --- Settled trades ---
    lines.append("## Settlements")
    lines.append("")
    if not settled:
        lines.append("_No contracts settled today._")
    else:
        wins = sum(1 for t in settled if (t.settled_value or 0) >= 1.0)
        losses = len(settled) - wins
        win_rate = wins / len(settled) if settled else 0.0
        lines.append(f"- Settled: {len(settled)}  ({wins}W / {losses}L - win rate {_fmt_pct(win_rate)})")
        lines.append(f"- Realized P&L: {_fmt_signed_money(realized_pnl)}")
        lines.append("")
        lines.append("| Ticker | Side | Qty | Cost | Outcome | P&L |")
        lines.append("|---|---|---|---|---|---|")
        for t in settled:
            outcome = "WIN" if (t.settled_value or 0) >= 1.0 else "LOSS"
            lines.append(
                f"| {t.ticker} | {t.side} | {t.fill_count} | "
                f"{_fmt_money(t.cost_dollars)} | {outcome} | "
                f"{_fmt_signed_money(t.settled_pnl)} |"
            )
    lines.append("")

    # --- Fill quality ---
    lines.append("## Fill Quality")
    lines.append("")
    filled = [t for t in opened if t.fill_price is not None]
    avg_slip = _avg([t.slippage for t in filled])
    avg_pred_edge = _avg([t.edge for t in filled])
    avg_realized_edge = _avg([t.realized_edge for t in filled])
    if filled:
        lines.append(f"- Fills analyzed: {len(filled)}")
        lines.append(f"- Avg slippage vs entry ask: {_fmt_num(avg_slip, 4)}")
        lines.append(f"- Avg predicted edge: {_fmt_num(avg_pred_edge, 4)}")
        lines.append(f"- Avg realized edge (after slippage): {_fmt_num(avg_realized_edge, 4)}")
        if avg_pred_edge is not None and avg_realized_edge is not None:
            leak = avg_pred_edge - avg_realized_edge
            lines.append(f"- Edge leak (predicted - realized): {_fmt_num(leak, 4)}")
    else:
        lines.append("_No fill-quality data for this date._")
    lines.append("")

    # --- Model calibration (among settled trades today) ---
    lines.append("## Model Calibration")
    lines.append("")
    cal_samples = [
        (t.settled_value, t.theo_prob)
        for t in settled
        if t.theo_prob is not None and t.settled_value is not None
    ]
    if cal_samples:
        bias = sum(sv - tp for sv, tp in cal_samples) / len(cal_samples)
        lines.append(f"- Samples (settled today with predictions): {len(cal_samples)}")
        lines.append(f"- Avg (settled - predicted): {_fmt_num(bias, 4)}")
        if bias > 0.05:
            lines.append("  - Model is **under-predicting** probability; consider tightening safety margin.")
        elif bias < -0.05:
            lines.append("  - Model is **over-predicting** probability; consider widening safety margin.")
        else:
            lines.append("  - Model calibration within +/-5% tolerance.")
    else:
        lines.append("_Not enough settled+predicted samples for today's calibration._")
    lines.append("")

    # --- Market context ---
    lines.append("## Market Context")
    lines.append("")
    if runs:
        btc_prices = [r["btc_price"] for r in runs if r["btc_price"] is not None]
        vol_ratios = [
            r["sigma_short"] / r["sigma_long"]
            for r in runs
            if r["sigma_short"] and r["sigma_long"]
        ]
        iv_rv_ratios = [r["iv_rv_ratio"] for r in runs if r["iv_rv_ratio"] is not None]
        margins = [r["adaptive_safety_margin"] for r in runs if r["adaptive_safety_margin"] is not None]
        total_signals = sum(r["signals_found"] or 0 for r in runs)
        total_orders = sum(r["orders_placed"] or 0 for r in runs)
        conversion = (total_orders / total_signals) if total_signals > 0 else None

        lines.append(f"- Cycles run: {len(runs)}")
        if btc_prices:
            lines.append(f"- BTC range: ${min(btc_prices):,.0f} - ${max(btc_prices):,.0f}")
        lines.append(f"- Avg sigma_short/sigma_long: {_fmt_num(_avg(vol_ratios), 3)}")
        lines.append(f"- Avg IV/RV ratio: {_fmt_num(_avg(iv_rv_ratios), 3)}")
        lines.append(f"- Avg adaptive safety margin: {_fmt_num(_avg(margins), 3)}")
        lines.append(f"- Signals found -> orders placed: {total_signals} -> {total_orders}"
                     + (f" ({_fmt_pct(conversion)})" if conversion is not None else ""))
    else:
        lines.append("_No run records for this date._")
    lines.append("")

    # --- Best / worst ---
    realized_trades = [t for t in settled if t.settled_pnl is not None]
    if realized_trades:
        lines.append("## Notable Trades")
        lines.append("")
        best = max(realized_trades, key=lambda t: t.settled_pnl)
        worst = min(realized_trades, key=lambda t: t.settled_pnl)
        lines.append(f"- **Best**: {best.ticker} ({best.side}) -> {_fmt_signed_money(best.settled_pnl)}")
        lines.append(f"- **Worst**: {worst.ticker} ({worst.side}) -> {_fmt_signed_money(worst.settled_pnl)}")
        lines.append("")

    return "\n".join(lines)


# ----------------------------------------------------------------------
# Public API
# ----------------------------------------------------------------------

def generate_report(
    date_str: str,
    db_path: Path,
    reports_dir: Path,
) -> Path:
    """
    Generate a markdown daily report for date_str (YYYY-MM-DD, UTC).
    Writes reports_dir/YYYY-MM-DD.md and returns the path.
    """
    reports_dir.mkdir(parents=True, exist_ok=True)

    # Ensure the DB schema is migrated to the latest columns the report expects.
    # Store.open() runs additive migrations idempotently.
    migrator = Store(db_path=db_path, trades_csv_path=db_path.parent / ".report_noop.csv")
    migrator.open()
    migrator.close()

    conn = _connect(db_path)
    try:
        opened = _orders_opened_on(conn, date_str)
        settled = _orders_settled_on(conn, date_str)
        snapshot = _latest_snapshot(conn, date_str)
        runs = _runs_on(conn, date_str)
    finally:
        conn.close()

    content = _render(date_str, opened, settled, snapshot, runs)
    out_path = reports_dir / f"{date_str}.md"
    out_path.write_text(content, encoding="utf-8")
    return out_path


# ----------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------

def _parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Generate a daily trading report.")
    ap.add_argument(
        "--date",
        help="UTC date to report on (YYYY-MM-DD). Defaults to today.",
        default=None,
    )
    ap.add_argument(
        "--db",
        help="Path to SQLite DB. Defaults to config DB_PATH.",
        default=None,
    )
    ap.add_argument(
        "--out",
        help="Reports output directory. Defaults to config REPORTS_DIR.",
        default=None,
    )
    return ap.parse_args()


def main() -> None:
    args = _parse_args()
    date_str = args.date or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    db_path = Path(args.db) if args.db else cfg.DB_PATH
    reports_dir = Path(args.out) if args.out else cfg.REPORTS_DIR

    if not db_path.exists():
        raise SystemExit(f"Database not found: {db_path}")

    out = generate_report(date_str, db_path, reports_dir)
    print(f"Report written: {out}")


if __name__ == "__main__":
    main()
