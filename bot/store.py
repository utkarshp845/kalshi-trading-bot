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
                order_id        TEXT PRIMARY KEY,
                client_order_id TEXT,
                ticker          TEXT NOT NULL,
                side            TEXT NOT NULL,
                action          TEXT NOT NULL,
                status          TEXT NOT NULL,
                yes_price       REAL,
                no_price        REAL,
                count           INTEGER,
                fill_count      INTEGER,
                cost_dollars    REAL,
                theo_prob       REAL,
                edge            REAL,
                created_time    TEXT,
                logged_at       TEXT NOT NULL
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
                sigma           REAL,
                markets_scanned INTEGER,
                signals_found   INTEGER,
                orders_placed   INTEGER,
                dry_run         INTEGER
            );
        """)
        self._conn.commit()

    # ------------------------------------------------------------------
    # Write methods
    # ------------------------------------------------------------------

    def log_order(self, order: Order, theo_prob: float, edge: float) -> None:
        now = _now_iso()
        self._conn.execute("""
            INSERT OR REPLACE INTO orders
              (order_id, client_order_id, ticker, side, action, status,
               yes_price, no_price, count, fill_count, cost_dollars,
               theo_prob, edge, created_time, logged_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
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
            edge,
            order.created_time,
            now,
        ))
        self._conn.commit()
        self._append_trades_csv(order, theo_prob, edge, now)

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
        sigma: float,
        markets_scanned: int,
        signals_found: int,
        orders_placed: int,
        dry_run: bool,
    ) -> None:
        self._conn.execute("""
            INSERT INTO runs
              (run_at, btc_price, sigma, markets_scanned, signals_found, orders_placed, dry_run)
            VALUES (?,?,?,?,?,?,?)
        """, (_now_iso(), btc_price, sigma, markets_scanned, signals_found, orders_placed, int(dry_run)))
        self._conn.commit()

    # ------------------------------------------------------------------
    # Read methods
    # ------------------------------------------------------------------

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

    def _append_trades_csv(self, order: Order, theo_prob: float, edge: float, logged_at: str) -> None:
        write_header = not self._trades_csv.exists() or self._trades_csv.stat().st_size == 0
        with open(self._trades_csv, "a", newline="") as f:
            writer = csv.writer(f)
            if write_header:
                writer.writerow([
                    "logged_at", "order_id", "ticker", "side", "action", "status",
                    "count", "fill_count", "cost_dollars", "theo_prob", "edge",
                ])
            writer.writerow([
                logged_at, order.order_id, order.ticker, order.side, order.action, order.status,
                order.count, order.fill_count, order.taker_fill_cost, theo_prob, edge,
            ])
