from typing import Optional
"""
SQLite persistence layer.

Tables:
  orders          — every order placed (keyed by order_id)
  daily_snapshots — balance snapshot once per poll cycle
  runs            — one row per main-loop execution for audit
"""
import csv
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from bot.kalshi_client import Order

log = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class Store:
    def __init__(self, db_path: Path, trades_csv_path: Path):
        self._db_path = db_path
        self._trades_csv = trades_csv_path
        self._conn: Optional[sqlite3.Connection] = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def open(self) -> None:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._trades_csv.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self._db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._create_schema()
        log.info("Store opened: %s", self._db_path)

    def close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None

    def _create_schema(self) -> None:
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS orders (
                order_id            TEXT PRIMARY KEY,
                client_order_id     TEXT,
                ticker              TEXT NOT NULL,
                side                TEXT NOT NULL,
                action              TEXT NOT NULL,
                status              TEXT NOT NULL,
                yes_price           REAL,
                no_price            REAL,
                count               INTEGER,
                fill_count          INTEGER,
                cost_dollars        REAL,
                theo_prob           REAL,
                gross_edge          REAL,
                edge                REAL,
                fee                 REAL,
                fill_price_dollars  REAL,
                slippage            REAL,
                realized_edge       REAL,
                fill_checked_at     TEXT,
                created_time        TEXT,
                hours_to_expiry     REAL,
                logged_at           TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS daily_snapshots (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                snapshot_date   TEXT NOT NULL,
                balance         REAL,
                daily_spent     REAL,
                positions_count INTEGER,
                logged_at       TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS runs (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                run_at          TEXT NOT NULL,
                btc_price       REAL,
                sigma_short     REAL,
                sigma_long      REAL,
                markets_scanned INTEGER,
                signals_found   INTEGER,
                orders_placed   INTEGER,
                dry_run         INTEGER
            );
        """)
        # Migrate existing databases that predate the new columns
        self._migrate()
        self._conn.commit()

    def _migrate(self) -> None:
        """Add columns introduced after initial schema — safe to run repeatedly."""
        new_order_cols = {
            "gross_edge":         "REAL",
            "fee":                "REAL",
            "fill_price_dollars": "REAL",
            "slippage":           "REAL",
            "realized_edge":      "REAL",
            "fill_checked_at":    "TEXT",
        }
        existing = {
            row[1]
            for row in self._conn.execute("PRAGMA table_info(orders)").fetchall()
        }
        for col, col_type in new_order_cols.items():
            if col not in existing:
                self._conn.execute(f"ALTER TABLE orders ADD COLUMN {col} {col_type}")

        new_order_cols_2 = {
            "settled_value":    "REAL",  # 1.0=won, 0.0=lost, NULL=not yet settled
            "hours_to_expiry":  "REAL",
        }
        for col, col_type in new_order_cols_2.items():
            if col not in existing:
                self._conn.execute(f"ALTER TABLE orders ADD COLUMN {col} {col_type}")

        run_cols = {
            "sigma_short":           "REAL",
            "sigma_long":            "REAL",
            "iv_rv_ratio":           "REAL",  # cycle IV/RV ratio from market prices
            "adaptive_safety_margin": "REAL", # actual vol margin used this cycle
        }
        existing_runs = {
            row[1]
            for row in self._conn.execute("PRAGMA table_info(runs)").fetchall()
        }
        for col, col_type in run_cols.items():
            if col not in existing_runs:
                self._conn.execute(f"ALTER TABLE runs ADD COLUMN {col} {col_type}")

    # ------------------------------------------------------------------
    # Write methods
    # ------------------------------------------------------------------

    def log_order(
        self,
        order: Order,
        theo_prob: float,
        gross_edge: float,
        edge: float,
        fee: float,
        hours_to_expiry: Optional[float] = None,
    ) -> None:
        now = _now_iso()
        self._conn.execute("""
            INSERT OR REPLACE INTO orders
              (order_id, client_order_id, ticker, side, action, status,
               yes_price, no_price, count, fill_count, cost_dollars,
               theo_prob, gross_edge, edge, fee, hours_to_expiry, created_time, logged_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            order.order_id,
            order.client_order_id,
            order.ticker,
            order.side,
            order.action,
            order.status,
            order.yes_price,
            order.no_price,
            order.count,
            order.fill_count,
            order.taker_fill_cost,
            theo_prob,
            gross_edge,
            edge,
            fee,
            hours_to_expiry,
            order.created_time,
            now,
        ))
        self._conn.commit()
        self._append_trades_csv(order, theo_prob, gross_edge, edge, fee, hours_to_expiry, now)

    def update_order_fill(self, order: Order) -> None:
        """Update fill status and compute fill quality metrics for a previously logged order."""
        fill_price: Optional[float] = None
        slippage: Optional[float] = None
        realized_edge: Optional[float] = None
        settled_value: Optional[float] = None

        if order.fill_count > 0 and order.taker_fill_cost > 0:
            fill_price = order.taker_fill_cost / order.fill_count
            # Fetch theo_prob and fee stored at order entry time
            row = self._conn.execute(
                "SELECT theo_prob, fee, yes_price, no_price FROM orders WHERE order_id = ?",
                (order.order_id,),
            ).fetchone()
            if row:
                theo_prob, fee, yes_price, no_price = row
                entry_ask = yes_price if order.side == "yes" else no_price
                slippage = fill_price - (entry_ask or fill_price)
                if theo_prob is not None and fee is not None:
                    realized_edge = theo_prob - fill_price - (fee or 0.0)

        # Detect settlement and record outcome for probability calibration.
        # At settlement: status is 'settled' or 'expired'.
        # A winning YES contract: fill_count > 0 after settlement (Kalshi settles by filling).
        # A losing YES contract: fill_count == 0 at settlement (no payout).
        if order.status in ("settled", "expired"):
            if order.action == "buy":
                if order.side == "yes":
                    settled_value = 1.0 if order.fill_count > 0 else 0.0
                else:  # side == "no"
                    settled_value = 1.0 if order.fill_count > 0 else 0.0
                log.info(
                    "Settlement %s: side=%s fill_count=%d → settled_value=%.1f",
                    order.order_id[:8], order.side, order.fill_count, settled_value,
                )

        self._conn.execute("""
            UPDATE orders
               SET status             = ?,
                   fill_count         = ?,
                   cost_dollars       = ?,
                   fill_price_dollars = ?,
                   slippage           = ?,
                   realized_edge      = ?,
                   settled_value      = COALESCE(?, settled_value),
                   fill_checked_at    = ?
             WHERE order_id = ?
        """, (
            order.status,
            order.fill_count,
            order.taker_fill_cost,
            fill_price,
            slippage,
            realized_edge,
            settled_value,
            _now_iso(),
            order.order_id,
        ))
        self._conn.commit()

        if fill_price is not None:
            log.info(
                "Fill quality %s: fill_price=%.4f  slippage=%+.4f  realized_edge=%.4f",
                order.order_id[:8], fill_price,
                slippage or 0.0, realized_edge or 0.0,
            )

    def snapshot_daily(self, balance: float, daily_spent: float, positions_count: int) -> None:
        now = _now_iso()
        today = now[:10]
        self._conn.execute("""
            INSERT INTO daily_snapshots
              (snapshot_date, balance, daily_spent, positions_count, logged_at)
            VALUES (?,?,?,?,?)
        """, (today, balance, daily_spent, positions_count, now))
        self._conn.commit()

    def log_run(
        self,
        btc_price: float,
        sigma_short: float,
        sigma_long: float,
        markets_scanned: int,
        signals_found: int,
        orders_placed: int,
        dry_run: bool,
        iv_rv_ratio: Optional[float] = None,
        adaptive_safety_margin: Optional[float] = None,
    ) -> None:
        self._conn.execute("""
            INSERT INTO runs
              (run_at, btc_price, sigma_short, sigma_long,
               markets_scanned, signals_found, orders_placed, dry_run,
               iv_rv_ratio, adaptive_safety_margin)
            VALUES (?,?,?,?,?,?,?,?,?,?)
        """, (
            _now_iso(), btc_price, sigma_short, sigma_long,
            markets_scanned, signals_found, orders_placed, int(dry_run),
            iv_rv_ratio, adaptive_safety_margin,
        ))
        self._conn.commit()

    # ------------------------------------------------------------------
    # Read methods
    # ------------------------------------------------------------------

    def get_unfilled_orders(self, max_age_hours: int = 48) -> list[str]:
        """Return order_ids placed within the last max_age_hours that are not yet fully filled."""
        cutoff = datetime.now(timezone.utc).isoformat()[:10]  # today's date prefix
        rows = self._conn.execute("""
            SELECT order_id FROM orders
             WHERE status NOT IN ('filled', 'canceled', 'expired')
               AND logged_at >= ?
        """, (cutoff,)).fetchall()
        return [r[0] for r in rows]

    def get_recent_iv_rv_ratios(self, n: int = 20) -> list[float]:
        """Return the last n iv_rv_ratio values from the runs table (non-NULL only)."""
        rows = self._conn.execute("""
            SELECT iv_rv_ratio FROM runs
             WHERE iv_rv_ratio IS NOT NULL
             ORDER BY run_at DESC
             LIMIT ?
        """, (n,)).fetchall()
        return [float(r[0]) for r in rows]

    def get_prob_calibration_bias(
        self,
        min_trades: int = 10,
        lookback_days: int = 30,
    ) -> Optional[float]:
        """
        Return the average (settled_value - theo_prob) for settled trades in the
        last lookback_days. Returns None if fewer than min_trades are available.

        Positive bias → model under-predicts probability (increase safety margin).
        Negative bias → model over-predicts probability (decrease safety margin).
        """
        cutoff = datetime.now(timezone.utc).isoformat()[:10]  # YYYY-MM-DD format
        from datetime import timedelta as _td
        lookback_date = (datetime.now(timezone.utc) - _td(days=lookback_days)).isoformat()[:10]
        row = self._conn.execute("""
            SELECT AVG(settled_value - theo_prob), COUNT(*)
              FROM orders
             WHERE settled_value IS NOT NULL
               AND theo_prob IS NOT NULL
               AND logged_at >= ?
               AND action = 'buy'
        """, (lookback_date,)).fetchone()
        if row is None or row[1] is None or row[1] < min_trades:
            return None
        return float(row[0])

    def get_slippage_factor(
        self,
        min_trades: int = 10,
        lookback_days: int = 14,
    ) -> Optional[float]:
        """
        Return avg(realized_edge) / avg(predicted_edge) over recent buy fills.

        Returns None if fewer than min_trades are available. Otherwise clamped
        to [0.3, 1.0]: never boosts sizing above 1.0; floors at 0.3 to prevent
        a few bad cycles from silencing the bot entirely.
        """
        from datetime import timedelta as _td
        lookback_date = (
            datetime.now(timezone.utc) - _td(days=lookback_days)
        ).isoformat()[:10]
        row = self._conn.execute("""
            SELECT AVG(realized_edge), AVG(edge), COUNT(*)
              FROM orders
             WHERE realized_edge IS NOT NULL
               AND edge IS NOT NULL
               AND edge > 0
               AND action = 'buy'
               AND logged_at >= ?
        """, (lookback_date,)).fetchone()
        if row is None or row[2] is None or row[2] < min_trades:
            return None
        avg_realized, avg_predicted, _ = row
        if avg_predicted is None or avg_predicted <= 0:
            return None
        ratio = float(avg_realized) / float(avg_predicted)
        return max(0.3, min(1.0, ratio))

    def get_todays_spend(self) -> float:
        today = _now_iso()[:10]
        row = self._conn.execute(
            "SELECT SUM(cost_dollars) FROM orders WHERE logged_at LIKE ? AND status != 'canceled'",
            (f"{today}%",),
        ).fetchone()
        return float(row[0] or 0)

    # ------------------------------------------------------------------
    # CSV append
    # ------------------------------------------------------------------

    def _append_trades_csv(
        self, order: Order, theo_prob: float, gross_edge: float,
        edge: float, fee: float, hours_to_expiry: Optional[float], logged_at: str,
    ) -> None:
        write_header = not self._trades_csv.exists() or self._trades_csv.stat().st_size == 0
        with open(self._trades_csv, "a", newline="") as f:
            writer = csv.writer(f)
            if write_header:
                writer.writerow([
                    "logged_at", "order_id", "ticker", "side", "action", "status",
                    "count", "fill_count", "cost_dollars",
                    "theo_prob", "gross_edge", "edge", "fee", "hours_to_expiry",
                ])
            writer.writerow([
                logged_at, order.order_id, order.ticker, order.side, order.action, order.status,
                order.count, order.fill_count, order.taker_fill_cost,
                theo_prob, gross_edge, edge, fee, hours_to_expiry,
            ])
