"""
Kalshi BTC Mispricing Arbitrage Bot — main entry point.

Run:
    python -m bot.main              # live trading (requires .env with real credentials)
    python -m bot.main --dry-run    # print signals only, no orders placed
"""
import argparse
import logging
import math
import statistics
import sys
import time
from datetime import date, datetime, timezone
from typing import Optional

import bot.config as cfg
from bot.implied_vol import fit_cycle_iv
from bot.kalshi_client import KalshiClient, Order, Position
from bot.monitor import alert
from bot.price_feed import get_btc_price_and_vol
from bot.pricing import calc_prob
from bot.risk import DailyRisk
from bot.store import Store
from bot.strategy import _hours_to_expiry, _parse_strike, scan_markets

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
        import datetime as _dt
        utc_now = datetime.now(timezone.utc)
        et_hour = (utc_now.hour - 4) % 24
        et_minute = utc_now.minute
        after_open   = et_hour >= _TRADING_START_HOUR
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

_consecutive_price_feed_failures = 0


def run(dry_run: bool) -> None:
    _setup_logging()
    log.info("=== Kalshi BTC Bot starting (dry_run=%s) — v2.0.0 ===", dry_run)
    log.info(
        "Config: min_edge=%.2f  min_t_hours=%.1f  max_daily_spend=$%.1f  "
        "max_positions=%d  kelly=%.2f  vol_margin=%.2f  max_vol_ratio=%.1f  "
        "max_spread=%.2f  drawdown_limit=%.0f%%  bankroll_frac=%.0f%%  "
        "exit=%s  price_improvement=%s  poll=%ds",
        cfg.MIN_EDGE, cfg.MIN_T_HOURS, cfg.MAX_DAILY_SPEND,
        cfg.MAX_POSITIONS, cfg.KELLY_FRACTION, cfg.VOL_SAFETY_MARGIN,
        cfg.MAX_VOL_RATIO, cfg.MAX_BID_ASK_SPREAD,
        cfg.MAX_DRAWDOWN_PCT * 100, cfg.BANKROLL_FRACTION * 100,
        cfg.ENABLE_POSITION_EXIT, cfg.ENABLE_PRICE_IMPROVEMENT,
        cfg.POLL_INTERVAL_SECONDS,
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
        max_drawdown_pct=cfg.MAX_DRAWDOWN_PCT,
        bankroll_fraction=cfg.BANKROLL_FRACTION,
    )

    store = Store(db_path=cfg.DB_PATH, trades_csv_path=cfg.TRADES_CSV)
    store.open()

    # Restore today's already-spent from DB (so a restart doesn't reset limits)
    risk._daily_spent = store.get_todays_spend()
    log.info("Restored today's spend from DB: $%.2f", risk.daily_spent)

    # --- Phase C6: Adaptive vol calibration from settled outcome history ---
    _apply_calibration(store)

    today_date = date.today()
    global _consecutive_price_feed_failures
    _consecutive_price_feed_failures = 0

    try:
        while True:
            # --- Day rollover ---
            if date.today() != today_date:
                today_date = date.today()
                risk.reset()
                _apply_calibration(store)  # recalibrate at start of each day

            if not cfg.FORCE_TRADING_HOURS and not _is_trading_hours():
                log.info("Outside trading hours — sleeping %ds", cfg.POLL_INTERVAL_SECONDS)
                time.sleep(cfg.POLL_INTERVAL_SECONDS)
                continue

            _run_cycle(kalshi, risk, store, dry_run)
            time.sleep(cfg.POLL_INTERVAL_SECONDS)

    except KeyboardInterrupt:
        log.info("Shutdown requested.")
    except Exception as exc:
        alert(f"Unhandled exception in main loop: {exc}", level="CRITICAL")
        raise
    finally:
        store.close()
        log.info("=== Bot stopped ===")


def _apply_calibration(store: Store) -> None:
    """
    Query settled order outcomes and nudge VOL_SAFETY_MARGIN up/down by ±5%
    if the model is systematically mis-calibrated. Clamp to [1.0, 2.5].
    """
    bias = store.get_prob_calibration_bias(min_trades=10, lookback_days=30)
    if bias is None:
        log.info("Calibration: insufficient settled trades — using static VOL_SAFETY_MARGIN=%.3f", cfg.VOL_SAFETY_MARGIN)
        return
    direction = math.copysign(1.0, bias)
    new_margin = max(1.0, min(2.5, cfg.VOL_SAFETY_MARGIN * (1.0 + 0.05 * direction)))
    log.info(
        "Calibration: prob_bias=%.4f → adjusting VOL_SAFETY_MARGIN %.3f → %.3f",
        bias, cfg.VOL_SAFETY_MARGIN, new_margin,
    )
    cfg.VOL_SAFETY_MARGIN = new_margin


def _check_fills(kalshi: KalshiClient, store: Store) -> None:
    """Re-fetch open orders from Kalshi and update fill quality metrics in the DB."""
    order_ids = store.get_unfilled_orders()
    if not order_ids:
        return
    for order_id in order_ids:
        try:
            order = kalshi.get_order(order_id)
            store.update_order_fill(order)
        except Exception as e:
            log.warning("Could not check fill for %s: %s", order_id[:8], e)


def _check_exits(
    kalshi: KalshiClient,
    store: Store,
    positions: list[Position],
    spot_price: float,
    sigma_adjusted: float,
    dry_run: bool,
) -> list[str]:
    """
    Re-evaluate each open position and exit if the theoretical value has
    dropped to EXIT_LOSS_TRIGGER fraction of entry price.

    Returns list of tickers that were exited (so they can be skipped for new entries).
    """
    if not cfg.ENABLE_POSITION_EXIT or not positions:
        return []

    exited_tickers: list[str] = []

    for pos in positions:
        strike = _parse_strike(pos.ticker)
        if strike is None:
            continue
        if pos.quantity <= 0 or pos.cost <= 0:
            continue

        entry_price_per_contract = pos.cost / pos.quantity

        # Fetch current market data for this position
        try:
            market = kalshi.get_market(pos.ticker)
        except Exception as e:
            log.warning("Exit check: could not fetch market %s: %s", pos.ticker, e)
            continue

        T_hours = _hours_to_expiry(market.close_time)
        if T_hours <= 0:
            continue  # already expired

        T_years = T_hours / 8760.0
        theo_prob = calc_prob(spot_price, strike, T_years, sigma_adjusted)
        current_value = theo_prob if pos.side == "yes" else (1.0 - theo_prob)

        if current_value < entry_price_per_contract * cfg.EXIT_LOSS_TRIGGER:
            current_bid = market.yes_bid if pos.side == "yes" else market.no_bid
            log.warning(
                "EXIT TRIGGER %s %s: current_value=%.4f < %.0f%% of entry=%.4f  "
                "bid=%.4f  [%s]",
                pos.ticker, pos.side, current_value,
                cfg.EXIT_LOSS_TRIGGER * 100, entry_price_per_contract,
                current_bid, "DRY RUN" if dry_run else "EXECUTING",
            )

            if not dry_run and current_bid >= 0.01:
                try:
                    sell_price = max(0.01, current_bid)
                    order = kalshi.sell_position(pos.ticker, pos.side, pos.quantity, sell_price)
                    store.log_order(
                        order,
                        theo_prob=theo_prob,
                        gross_edge=current_value - sell_price,
                        edge=current_value - sell_price - cfg.KALSHI_TAKER_FEE,
                        fee=cfg.KALSHI_TAKER_FEE,
                    )
                    exited_tickers.append(pos.ticker)
                    alert(
                        f"Exit triggered: {pos.ticker} {pos.side} x{pos.quantity} "
                        f"@ ${sell_price:.2f} (value dropped to {current_value:.2%} of entry)",
                        level="WARNING",
                    )
                except Exception as e:
                    log.error("Exit order failed for %s: %s", pos.ticker, e)
            elif dry_run:
                exited_tickers.append(pos.ticker)  # simulate exit in dry-run

    return exited_tickers


def _execute_with_price_improvement(
    kalshi: KalshiClient,
    ticker: str,
    side: str,
    contracts: int,
    ask_price: float,
    mid_price: float,
    dry_run: bool,
) -> list[Order]:
    """
    Attempt to fill at the mid-price first (price improvement).
    After PRICE_IMPROVEMENT_TIMEOUT_SEC, cancel any unfilled portion
    and re-fill at the ask price.

    Returns the list of Orders that resulted in fills (may be 0, 1, or 2).
    Callers must sum taker_fill_cost across returned orders for accurate
    balance and risk accounting.
    """
    if not cfg.ENABLE_PRICE_IMPROVEMENT or mid_price >= ask_price:
        # No improvement possible — just fill at ask
        order = kalshi.place_order(ticker, side, contracts, ask_price)
        return [order] if order is not None else []

    # Phase 1: try mid-price
    try:
        order1 = kalshi.place_order(ticker, side, contracts, mid_price)
        log.info("Price improvement: placed %d @ mid=%.3f (ask=%.3f)", contracts, mid_price, ask_price)
    except Exception as e:
        log.warning("Mid-price order failed, falling back to ask: %s", e)
        order = kalshi.place_order(ticker, side, contracts, ask_price)
        return [order] if order is not None else []

    time.sleep(cfg.PRICE_IMPROVEMENT_TIMEOUT_SEC)

    # Check fill
    try:
        order1 = kalshi.get_order(order1.order_id)
    except Exception:
        pass  # use last known state

    filled = order1.fill_count
    if filled >= contracts:
        log.info("Price improvement: fully filled %d/%d @ mid", filled, contracts)
        return [order1]

    # Phase 2: cancel remainder and fill at ask
    kalshi.cancel_order(order1.order_id)
    remaining = contracts - filled
    if remaining <= 0:
        return [order1] if filled > 0 else []

    try:
        order2 = kalshi.place_order(ticker, side, remaining, ask_price)
        log.info(
            "Price improvement: partial %d filled @ mid, %d filled @ ask",
            filled, remaining,
        )
        # Return both orders so caller can account for the full blended cost.
        results = []
        if filled > 0:
            results.append(order1)
        if order2 is not None:
            results.append(order2)
        return results
    except Exception as e:
        log.error("Fallback ask order failed for %s: %s", ticker, e)
        return [order1] if filled > 0 else []


def _run_cycle(kalshi: KalshiClient, risk: DailyRisk, store: Store, dry_run: bool) -> None:
    global _consecutive_price_feed_failures
    log.info("--- Cycle start ---")
    orders_placed = 0

    # --- Price and vol fetch ---
    try:
        spot_price, sigma_short, sigma_long = get_btc_price_and_vol(
            short_days=cfg.VOL_SHORT_DAYS,
            long_days=cfg.VOL_LONG_DAYS,
        )
        log.info(
            "BTC spot=%.2f  σ_%dd=%.4f  σ_%dd=%.4f",
            spot_price, cfg.VOL_SHORT_DAYS, sigma_short, cfg.VOL_LONG_DAYS, sigma_long,
        )
        _consecutive_price_feed_failures = 0
    except Exception as e:
        _consecutive_price_feed_failures += 1
        log.error("Price feed error: %s", e)
        if _consecutive_price_feed_failures >= 3:
            alert(f"Price feed failing for {_consecutive_price_feed_failures} consecutive cycles: {e}", level="ERROR")
        return

    # --- Vol regime check ---
    vol_ratio = sigma_short / sigma_long if sigma_long > 0 else 1.0
    if vol_ratio > cfg.MAX_VOL_RATIO:
        log.warning(
            "VOL REGIME SKIP: σ_short/σ_long = %.2f exceeds max %.2f — model unreliable, skipping cycle",
            vol_ratio, cfg.MAX_VOL_RATIO,
        )
        return

    # --- Fetch Kalshi data ---
    try:
        markets = kalshi.get_open_btc_markets()
        positions = kalshi.get_positions()
        balance = kalshi.get_balance()
    except Exception as e:
        log.error("Kalshi API error: %s", e)
        alert(f"Kalshi API error: {e}", level="ERROR")
        return

    # --- Phase B4: Compute adaptive vol safety margin from implied vols ---
    T_hours_by_ticker = {m.ticker: _hours_to_expiry(m.close_time) for m in markets}
    iv_rv_ratio, _ = fit_cycle_iv(markets, spot_price, sigma_short, T_hours_by_ticker)

    recent_ratios = store.get_recent_iv_rv_ratios(n=cfg.IV_CALIBRATION_MIN_OBS)
    if len(recent_ratios) >= cfg.IV_CALIBRATION_MIN_OBS and iv_rv_ratio is not None:
        # Use trailing median of observed IV/RV ratios as the adaptive safety margin
        all_ratios = recent_ratios + [iv_rv_ratio]
        adaptive_margin = max(
            cfg.IV_SAFETY_MARGIN_MIN,
            min(cfg.IV_SAFETY_MARGIN_MAX, statistics.median(all_ratios)),
        )
        log.info(
            "Adaptive vol margin: IV/RV=%.3f  trailing_median=%.3f  (static=%.3f)",
            iv_rv_ratio, adaptive_margin, cfg.VOL_SAFETY_MARGIN,
        )
    else:
        adaptive_margin = cfg.VOL_SAFETY_MARGIN
        log.info(
            "Adaptive vol margin: cold start (n=%d < %d) — using static %.3f",
            len(recent_ratios), cfg.IV_CALIBRATION_MIN_OBS, cfg.VOL_SAFETY_MARGIN,
        )

    sigma_adjusted = sigma_short * adaptive_margin
    log.info("σ_adjusted = %.4f × %.3f = %.4f", sigma_short, adaptive_margin, sigma_adjusted)

    # --- Set session balance for drawdown tracking ---
    risk.set_session_balance(balance)

    # --- Drawdown guard ---
    if risk.check_drawdown(balance):
        log.warning("Drawdown limit reached — halting all trading for today")
        alert(
            f"Drawdown halt: balance=${balance:.2f} exceeded {cfg.MAX_DRAWDOWN_PCT:.0%} limit",
            level="WARNING",
        )
        store.snapshot_daily(balance, risk.daily_spent, len(positions))
        store.log_run(
            btc_price=spot_price, sigma_short=sigma_short, sigma_long=sigma_long,
            markets_scanned=len(markets), signals_found=0, orders_placed=0,
            dry_run=dry_run, iv_rv_ratio=iv_rv_ratio, adaptive_safety_margin=adaptive_margin,
        )
        return

    # --- Fill quality check ---
    _check_fills(kalshi, store)

    # --- Phase C5: Position exit check ---
    exited_tickers = _check_exits(kalshi, store, positions, spot_price, sigma_adjusted, dry_run)
    # Re-fetch positions if any exits occurred (to avoid double-counting)
    if exited_tickers:
        try:
            positions = kalshi.get_positions()
            balance = kalshi.get_balance()
        except Exception:
            pass  # use stale data; exits already logged

    held_tickers = {p.ticker for p in positions}
    open_count = len(positions)

    # --- Signal scan ---
    signals = scan_markets(
        markets=markets,
        spot_price=spot_price,
        sigma=sigma_adjusted,
        min_edge=cfg.MIN_EDGE,
        min_t_hours=cfg.MIN_T_HOURS,
        held_tickers=held_tickers,
        fee=cfg.KALSHI_TAKER_FEE,
        max_bid_ask_spread=cfg.MAX_BID_ASK_SPREAD,
        max_bid_ask_pct_spread=cfg.MAX_BID_ASK_PCT_SPREAD,
        max_last_price_divergence=cfg.MAX_LAST_PRICE_DIVERGENCE,
    )

    # --- Order placement ---
    for sig in signals:
        if not risk.can_trade(open_count):
            break

        contracts = risk.size_order(sig, current_balance=balance, open_positions=open_count)
        if contracts < 1:
            log.info("Signal %s %s: sized to 0 contracts, skipping", sig.ticker, sig.side)
            continue

        cost_estimate = contracts * sig.price
        log.info(
            "SIGNAL %s %s: theo=%.4f ask=%.2f mid=%.2f gross_edge=%.4f net_edge=%.4f fee=%.2f  "
            "→  %d contracts (~$%.2f)  balance=$%.2f%s",
            sig.ticker, sig.side, sig.theo_prob, sig.price, sig.mid_price,
            sig.gross_edge, sig.edge, sig.fee,
            contracts, cost_estimate, balance, "  [DRY RUN]" if dry_run else "",
        )

        if dry_run:
            continue

        try:
            # --- Phase C7: Smart order placement (mid-price with ask fallback) ---
            orders = _execute_with_price_improvement(
                kalshi=kalshi,
                ticker=sig.ticker,
                side=sig.side,
                contracts=contracts,
                ask_price=sig.price,
                mid_price=sig.mid_price,
                dry_run=dry_run,
            )
            if not orders:
                log.warning("Order execution returned no fill for %s", sig.ticker)
                continue

            total_filled = sum(o.fill_count for o in orders)
            total_cost = sum(o.taker_fill_cost for o in orders)
            # If the broker hasn't reported fill cost yet, fall back proportionally.
            fill_cost = total_cost if total_cost > 0 else cost_estimate
            risk.record_fill(fill_cost)
            balance -= fill_cost
            for o in orders:
                store.log_order(
                    o,
                    theo_prob=sig.theo_prob,
                    gross_edge=sig.gross_edge,
                    edge=sig.edge,
                    fee=sig.fee,
                )
            if total_filled > 0:
                open_count += 1
                orders_placed += 1
            time.sleep(0.5)  # avoid burst rate-limiting between orders
        except Exception as e:
            log.error("Order failed for %s: %s", sig.ticker, e)

    store.snapshot_daily(balance, risk.daily_spent, open_count)
    store.log_run(
        btc_price=spot_price,
        sigma_short=sigma_short,
        sigma_long=sigma_long,
        markets_scanned=len(markets),
        signals_found=len(signals),
        orders_placed=orders_placed,
        dry_run=dry_run,
        iv_rv_ratio=iv_rv_ratio,
        adaptive_safety_margin=adaptive_margin,
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
