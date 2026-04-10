"""
Kalshi BTC Mispricing Arbitrage Bot — main entry point.

Run:
    python -m bot.main              # live trading (requires .env with real credentials)
    python -m bot.main --dry-run    # print signals only, no orders placed
"""
import argparse
import logging
import sys
import time
from datetime import date, datetime, timezone

import bot.config as cfg
from bot.kalshi_client import KalshiClient
from bot.price_feed import get_btc_price_and_vol
from bot.risk import DailyRisk
from bot.store import Store
from bot.strategy import scan_markets

# ------------------------------------------------------------------
# Logging setup
# ------------------------------------------------------------------

def _setup_logging() -> None:
    cfg.LOGS_DIR.mkdir(parents=True, exist_ok=True)
    fmt = "%(asctime)s %(levelname)-8s %(name)s — %(message)s"
    handlers: list[logging.Handler] = [
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(cfg.LOG_PATH),
    ]
    logging.basicConfig(level=logging.INFO, format=fmt, handlers=handlers)
    # Quiet noisy third-party loggers
    logging.getLogger("urllib3").setLevel(logging.WARNING)


# ------------------------------------------------------------------
# Trading hours guard
# ------------------------------------------------------------------

_TRADING_START_HOUR = 9    # 9:00 AM ET
_TRADING_END_HOUR   = 15   # 3:30 PM ET (Kalshi daily BTC markets close at 4pm ET)
_TRADING_END_MINUTE = 30


def _is_trading_hours() -> bool:
    """Return True if current ET time is within active trading window."""
    try:
        from zoneinfo import ZoneInfo
        et = ZoneInfo("America/New_York")
    except ImportError:
        # Python < 3.9 fallback — assume UTC offset -4 or -5 (approximate)
        # For production use, install the tzdata package
        import datetime as _dt
        utc_now = datetime.now(timezone.utc)
        # Rough ET approximation (EST = UTC-5, EDT = UTC-4)
        et_hour = (utc_now.hour - 4) % 24
        et_minute = utc_now.minute
        after_open  = (et_hour > _TRADING_START_HOUR) or (et_hour == _TRADING_START_HOUR)
        before_close = (et_hour < _TRADING_END_HOUR) or (et_hour == _TRADING_END_HOUR and et_minute <= _TRADING_END_MINUTE)
        return after_open and before_close

    now_et = datetime.now(et)
    after_open   = now_et.hour >= _TRADING_START_HOUR
    before_close = (now_et.hour < _TRADING_END_HOUR) or (
        now_et.hour == _TRADING_END_HOUR and now_et.minute <= _TRADING_END_MINUTE
    )
    return after_open and before_close


# ------------------------------------------------------------------
# Main loop
# ------------------------------------------------------------------

log = logging.getLogger(__name__)


def run(dry_run: bool) -> None:
    _setup_logging()
    log.info("=== Kalshi BTC Bot starting (dry_run=%s) — v1.1.0 ===", dry_run)
    log.info(
        "Config: min_edge=%.2f  min_t_hours=%.1f  max_daily_spend=$%.0f  "
        "max_positions=%d  kelly=%.2f  poll=%ds",
        cfg.MIN_EDGE, cfg.MIN_T_HOURS, cfg.MAX_DAILY_SPEND,
        cfg.MAX_POSITIONS, cfg.KELLY_FRACTION, cfg.POLL_INTERVAL_SECONDS,
    )

    if not cfg.KALSHI_API_KEY_ID:
        log.error("KALSHI_API_KEY_ID is not set. Copy .env.example to .env and fill in your credentials.")
        sys.exit(1)
    if not cfg.KALSHI_PRIVATE_KEY_PATH.exists():
        log.error("Private key file not found: %s", cfg.KALSHI_PRIVATE_KEY_PATH)
        sys.exit(1)

    kalshi = KalshiClient(
        api_key_id=cfg.KALSHI_API_KEY_ID,
        private_key_path=cfg.KALSHI_PRIVATE_KEY_PATH,
        base_url=cfg.KALSHI_BASE_URL,
    )

    risk = DailyRisk(
        max_daily_spend=cfg.MAX_DAILY_SPEND,
        max_contracts_per_market=cfg.MAX_CONTRACTS_PER_MARKET,
        max_positions=cfg.MAX_POSITIONS,
        kelly_fraction=cfg.KELLY_FRACTION,
    )

    store = Store(db_path=cfg.DB_PATH, trades_csv_path=cfg.TRADES_CSV)
    store.open()

    # Restore today's already-spent from DB (so a restart doesn't reset limits)
    risk._daily_spent = store.get_todays_spend()
    log.info("Restored today's spend from DB: $%.2f", risk.daily_spent)

    today_date = date.today()

    try:
        while True:
            # --- Day rollover ---
            if date.today() != today_date:
                today_date = date.today()
                risk.reset()

            if not cfg.FORCE_TRADING_HOURS and not _is_trading_hours():
                log.info("Outside trading hours — sleeping %ds", cfg.POLL_INTERVAL_SECONDS)
                time.sleep(cfg.POLL_INTERVAL_SECONDS)
                continue

            _run_cycle(kalshi, risk, store, dry_run)
            time.sleep(cfg.POLL_INTERVAL_SECONDS)

    except KeyboardInterrupt:
        log.info("Shutdown requested.")
    finally:
        store.close()
        log.info("=== Bot stopped ===")


def _run_cycle(kalshi: KalshiClient, risk: DailyRisk, store: Store, dry_run: bool) -> None:
    log.info("--- Cycle start ---")
    orders_placed = 0

    try:
        spot_price, sigma = get_btc_price_and_vol()
        log.info("BTC spot=%.2f  σ=%.4f", spot_price, sigma)
    except Exception as e:
        log.error("Price feed error: %s", e)
        return

    try:
        markets = kalshi.get_open_btc_markets()
        positions = kalshi.get_positions()
        balance = kalshi.get_balance()
    except Exception as e:
        log.error("Kalshi API error: %s", e)
        return

    held_tickers = {p.ticker for p in positions}
    open_count = len(positions)

    signals = scan_markets(
        markets=markets,
        spot_price=spot_price,
        sigma=sigma,
        min_edge=cfg.MIN_EDGE,
        min_t_hours=cfg.MIN_T_HOURS,
        held_tickers=held_tickers,
    )

    for sig in signals:
        if not risk.can_trade(open_count):
            break

        contracts = risk.size_order(sig)
        if contracts < 1:
            log.info("Signal %s %s: sized to 0 contracts, skipping", sig.ticker, sig.side)
            continue

        cost_estimate = contracts * sig.price
        log.info(
            "SIGNAL %s %s: theo=%.4f ask=%.2f edge=%.4f  →  %d contracts (~$%.2f)%s",
            sig.ticker, sig.side, sig.theo_prob, sig.price, sig.edge,
            contracts, cost_estimate, "  [DRY RUN]" if dry_run else "",
        )

        if dry_run:
            continue

        try:
            order = kalshi.place_order(
                ticker=sig.ticker,
                side=sig.side,
                count=contracts,
                price_dollars=sig.price,
            )
            risk.record_fill(order.taker_fill_cost or cost_estimate)
            store.log_order(order, theo_prob=sig.theo_prob, edge=sig.edge)
            open_count += 1
            orders_placed += 1
            time.sleep(0.5)  # avoid burst rate-limiting
        except Exception as e:
            log.error("Order failed for %s: %s", sig.ticker, e)

    store.snapshot_daily(balance, risk.daily_spent, open_count)
    store.log_run(
        btc_price=spot_price,
        sigma=sigma,
        markets_scanned=len(markets),
        signals_found=len(signals),
        orders_placed=orders_placed,
        dry_run=dry_run,
    )
    log.info("--- Cycle end: %d signal(s), %d order(s) placed ---", len(signals), orders_placed)


# ------------------------------------------------------------------
# Entry point
# ------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Kalshi BTC Mispricing Arbitrage Bot")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=cfg.DRY_RUN,
        help="Log signals without placing real orders",
    )
    args = parser.parse_args()
    run(dry_run=args.dry_run)
