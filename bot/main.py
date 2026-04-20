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
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Optional

import bot.config as cfg
from bot.deribit_iv import get_atm_iv
from bot.implied_vol import fit_cycle_iv
from bot.kalshi_client import KalshiClient, Market, Order, Position
from bot.monitor import alert
from bot.price_feed import get_price_vol_drift, get_spot_price
from bot.pricing import calc_prob
from bot.report import generate_report
from bot.risk import DailyRisk
from bot.store import Store
from bot.strategy import Signal, _hours_to_expiry, _parse_strike, scan_markets


# Map Kalshi ticker prefix → underlying symbol used by the price feed.
_TICKER_PREFIX_TO_SYMBOL = {
    "KXBTC": "BTC",
    "KXETH": "ETH",
}


def _symbol_for_ticker(ticker: str) -> Optional[str]:
    """Return 'BTC' or 'ETH' (or None) from a Kalshi ticker like KXBTC-26APR-B95000."""
    prefix = ticker.split("-", 1)[0].upper()
    return _TICKER_PREFIX_TO_SYMBOL.get(prefix)


@dataclass
class UnderlyingState:
    """Per-cycle state for one underlying (BTC or ETH)."""
    symbol: str               # "BTC" or "ETH"
    series_ticker: str        # "KXBTC" or "KXETH"
    spot: float
    sigma_short: float
    sigma_long: float
    sigma_adjusted: float
    mu: float
    iv_rv_ratio: Optional[float]
    adaptive_margin: float
    markets: list[Market] = field(default_factory=list)

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

# BTC doesn't sleep — trade essentially the entire day. The window below covers
# midnight ET (when new daily markets open and prices are least efficient) through
# ~5 minutes before the 4pm ET daily close.
_TRADING_START_HOUR = 0     # midnight ET — catch new market opens
_TRADING_END_HOUR   = 15    # final partial hour before the 4pm close
_TRADING_END_MINUTE = 55    # stop ~5 min before close to avoid stale-quote fills


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
    enabled_underlyings = ",".join(s for s, _ in _enabled_underlyings()) or "NONE"
    log.info(
        "=== Kalshi Bot starting (dry_run=%s) — v2.1.0 underlyings=[%s] ===",
        dry_run, enabled_underlyings,
    )
    log.info(
        "Config: min_edge=%.2f  min_t_hours=%.1f  daily_spend=%.0f%%/floor=$%.0f  "
        "max_positions=%d  kelly=%.2f  vol_margin=%.2f  max_vol_ratio=%.1f  "
        "max_spread=%.2f  drawdown_limit=%.0f%%  bankroll_frac=%.0f%%  "
        "exit=%s  take_profit=%.1fx  drift=%s  deribit_iv=%s(w=%.2f)  "
        "price_improvement=%s  poll=%ds",
        cfg.MIN_EDGE, cfg.MIN_T_HOURS, cfg.DAILY_SPEND_PCT * 100, cfg.DAILY_SPEND_FLOOR,
        cfg.MAX_POSITIONS, cfg.KELLY_FRACTION, cfg.VOL_SAFETY_MARGIN,
        cfg.MAX_VOL_RATIO, cfg.MAX_BID_ASK_SPREAD,
        cfg.MAX_DRAWDOWN_PCT * 100, cfg.BANKROLL_FRACTION * 100,
        cfg.ENABLE_POSITION_EXIT, cfg.TAKE_PROFIT_TRIGGER, cfg.USE_DRIFT,
        cfg.ENABLE_DERIBIT_IV, cfg.DERIBIT_IV_WEIGHT,
        cfg.ENABLE_PRICE_IMPROVEMENT,
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
        daily_spend_pct=cfg.DAILY_SPEND_PCT,
        daily_spend_floor=cfg.DAILY_SPEND_FLOOR,
        max_contracts_per_market=cfg.MAX_CONTRACTS_PER_MARKET,
        max_positions=cfg.MAX_POSITIONS,
        kelly_fraction=cfg.KELLY_FRACTION,
        max_drawdown_pct=cfg.MAX_DRAWDOWN_PCT,
        bankroll_fraction=cfg.BANKROLL_FRACTION,
        drawdown_tier_1_pct=cfg.DRAWDOWN_TIER_1_PCT,
        drawdown_tier_1_scale=cfg.DRAWDOWN_TIER_1_SCALE,
        drawdown_tier_2_pct=cfg.DRAWDOWN_TIER_2_PCT,
        drawdown_tier_2_scale=cfg.DRAWDOWN_TIER_2_SCALE,
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
    Log settled-trade calibration bias for visibility.

    We do not mutate VOL_SAFETY_MARGIN here. Settled trade outcomes are a
    selection-biased sample of only the contracts we chose to trade, and a
    single global vol nudge moves YES/NO probabilities in different directions
    across strikes. The market-implied IV/RV margin remains the only automatic
    volatility calibration mechanism.
    """
    bias = store.get_prob_calibration_bias(min_trades=10, lookback_days=30)
    if bias is None:
        log.info("Calibration: insufficient settled trades — using static VOL_SAFETY_MARGIN=%.3f", cfg.VOL_SAFETY_MARGIN)
        return
    log.info(
        "Calibration: prob_bias=%.4f (informational only; VOL_SAFETY_MARGIN unchanged at %.3f)",
        bias, cfg.VOL_SAFETY_MARGIN,
    )


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
    underlyings: dict[str, UnderlyingState],
    dry_run: bool,
) -> list[str]:
    """
    Re-evaluate each open position and exit if the theoretical value has
    dropped to EXIT_LOSS_TRIGGER (loss exit) or risen to TAKE_PROFIT_TRIGGER
    (profit exit) of the entry price.

    Each position's underlying (BTC vs ETH) is detected from its ticker prefix
    and uses that underlying's spot, vol, and drift. Positions on a disabled
    underlying are skipped.

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

        symbol = _symbol_for_ticker(pos.ticker)
        if symbol is None or symbol not in underlyings:
            log.debug("Exit check: skipping %s (no underlying state)", pos.ticker)
            continue
        u = underlyings[symbol]

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
        theo_prob = calc_prob(u.spot, strike, T_years, u.sigma_adjusted, mu=u.mu)
        current_value = theo_prob if pos.side == "yes" else (1.0 - theo_prob)

        loss_trigger_hit = current_value < entry_price_per_contract * cfg.EXIT_LOSS_TRIGGER
        # Take-profit: only fire if the position has appreciated meaningfully AND
        # there's enough time left for the gain to evaporate (otherwise just hold to expiry).
        take_profit_hit = (
            current_value >= entry_price_per_contract * cfg.TAKE_PROFIT_TRIGGER
            and T_hours >= cfg.TAKE_PROFIT_MIN_HOURS
        )

        if loss_trigger_hit or take_profit_hit:
            current_bid = market.yes_bid if pos.side == "yes" else market.no_bid
            reason = "LOSS" if loss_trigger_hit else "PROFIT"
            log_fn = log.warning if loss_trigger_hit else log.info
            log_fn(
                "%s EXIT %s %s: current_value=%.4f vs entry=%.4f (%.0f%% of entry)  "
                "bid=%.4f  T=%.2fh  [%s]",
                reason, pos.ticker, pos.side, current_value, entry_price_per_contract,
                (current_value / entry_price_per_contract) * 100,
                current_bid, T_hours, "DRY RUN" if dry_run else "EXECUTING",
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
                        hours_to_expiry=T_hours,
                    )
                    exited_tickers.append(pos.ticker)
                    alert(
                        f"{reason} exit: {pos.ticker} {pos.side} x{pos.quantity} "
                        f"@ ${sell_price:.2f} (value {current_value:.2%} of entry)",
                        level="WARNING" if loss_trigger_hit else "INFO",
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
    symbol: str = "BTC",
) -> list[Order]:
    """
    Attempt to fill at the mid-price first (price improvement).
    During the PRICE_IMPROVEMENT_TIMEOUT_SEC window, poll the underlying's spot
    every STALE_ORDER_POLL_SEC seconds; if spot moves more than
    STALE_ORDER_SPOT_MOVE_PCT away from the entry spot, cancel the
    mid-price order (and skip the ask fallback) since the edge has
    likely evaporated. Otherwise, after the timeout, cancel any
    unfilled portion and re-fill the remainder at the ask price.

    Returns the list of Orders that resulted in fills (may be 0, 1, or 2).
    Callers must sum taker_fill_cost across returned orders for accurate
    balance and risk accounting.
    """
    if not cfg.ENABLE_PRICE_IMPROVEMENT or mid_price >= ask_price:
        # No improvement possible — just fill at ask
        order = kalshi.place_order(ticker, side, contracts, ask_price)
        return [order] if order is not None else []

    # Snapshot the underlying's spot at order-entry time for drift detection.
    try:
        entry_spot = get_spot_price(symbol)
    except Exception as e:
        log.debug("Spot snapshot before mid-price order failed: %s", e)
        entry_spot = None

    # Phase 1: try mid-price
    try:
        order1 = kalshi.place_order(ticker, side, contracts, mid_price)
        log.info("Price improvement: placed %d @ mid=%.3f (ask=%.3f)", contracts, mid_price, ask_price)
    except Exception as e:
        log.warning("Mid-price order failed, falling back to ask: %s", e)
        order = kalshi.place_order(ticker, side, contracts, ask_price)
        return [order] if order is not None else []

    total_wait = max(0, cfg.PRICE_IMPROVEMENT_TIMEOUT_SEC)
    poll = max(1, cfg.STALE_ORDER_POLL_SEC)
    spot_moved = False
    elapsed = 0

    while elapsed < total_wait:
        step = min(poll, total_wait - elapsed)
        time.sleep(step)
        elapsed += step

        # Refresh order state
        try:
            order1 = kalshi.get_order(order1.order_id)
        except Exception:
            pass  # use last known state

        if order1.fill_count >= contracts:
            log.info(
                "Price improvement: fully filled %d/%d @ mid (after %ds)",
                order1.fill_count, contracts, elapsed,
            )
            return [order1]

        # Drift guard: cancel if the underlying has moved away from the entry spot
        if entry_spot is not None:
            try:
                current_spot = get_spot_price(symbol)
                drift = abs(current_spot - entry_spot) / entry_spot
                if drift > cfg.STALE_ORDER_SPOT_MOVE_PCT:
                    log.warning(
                        "%s spot moved %.3f%% (%.0f → %.0f) during mid-price wait — cancelling %s",
                        symbol, drift * 100, entry_spot, current_spot, order1.order_id[:8],
                    )
                    spot_moved = True
                    break
            except Exception as e:
                log.debug("Spot price poll failed: %s", e)

    filled = order1.fill_count
    if filled >= contracts:
        log.info("Price improvement: fully filled %d/%d @ mid", filled, contracts)
        return [order1]

    # Phase 2: cancel remainder
    kalshi.cancel_order(order1.order_id)
    remaining = contracts - filled
    if remaining <= 0:
        return [order1] if filled > 0 else []

    if spot_moved:
        # Abort ask fallback: spot has moved, so the original edge estimate is stale.
        log.info(
            "Skipping ask fallback after spot drift: filled=%d/%d",
            filled, contracts,
        )
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


def _enabled_underlyings() -> list[tuple[str, str]]:
    """Return [(symbol, series_ticker), ...] for underlyings enabled in config."""
    out: list[tuple[str, str]] = []
    if cfg.ENABLE_BTC:
        out.append(("BTC", "KXBTC"))
    if cfg.ENABLE_ETH:
        out.append(("ETH", "KXETH"))
    return out


def _build_underlying_state(
    kalshi: KalshiClient,
    symbol: str,
    series_ticker: str,
    store: Store,
) -> Optional[UnderlyingState]:
    """
    Fetch all per-underlying inputs needed for a cycle: spot, vols, drift,
    Deribit IV blend, market list, IV/RV adaptive margin.

    Returns None if the price feed or markets fetch fails — the cycle will
    skip this underlying but continue with any others.
    """
    # Spot, vol, drift
    try:
        spot, sigma_short, sigma_long, mu_raw = get_price_vol_drift(
            short_days=cfg.VOL_SHORT_DAYS,
            long_days=cfg.VOL_LONG_DAYS,
            drift_days=cfg.DRIFT_LOOKBACK_DAYS,
            symbol=symbol,
        )
    except Exception as e:
        log.error("Price feed error for %s: %s", symbol, e)
        return None

    # Vol regime check is per-underlying — skip just this asset on instability.
    vol_ratio = sigma_short / sigma_long if sigma_long > 0 else 1.0
    if vol_ratio > cfg.MAX_VOL_RATIO:
        log.warning(
            "VOL REGIME SKIP %s: σ_short/σ_long = %.2f exceeds max %.2f",
            symbol, vol_ratio, cfg.MAX_VOL_RATIO,
        )
        return None

    # Deribit IV blend (forward-looking). On any failure or if disabled, skip silently.
    sigma_realized = sigma_short
    sigma_blended = sigma_realized
    if cfg.ENABLE_DERIBIT_IV:
        try:
            iv_deribit = get_atm_iv(symbol, spot, min_dte_hours=cfg.DERIBIT_MIN_DTE_HOURS)
        except Exception as e:
            log.debug("Deribit IV unavailable for %s: %s", symbol, e)
            iv_deribit = None
        if iv_deribit is not None and iv_deribit > 0:
            w = max(0.0, min(1.0, cfg.DERIBIT_IV_WEIGHT))
            sigma_blended = (1.0 - w) * sigma_realized + w * iv_deribit
            log.info(
                "%s vol blend: RV=%.4f  Deribit_IV=%.4f  w=%.2f → σ_blended=%.4f",
                symbol, sigma_realized, iv_deribit, w, sigma_blended,
            )

    # Markets
    try:
        markets = kalshi.get_open_markets(series_ticker)
    except Exception as e:
        log.error("Kalshi markets fetch failed for %s: %s", series_ticker, e)
        return None

    # IV/RV adaptive safety margin from market mid-prices.
    # Note: the trailing-median history is shared across underlyings since
    # crypto IV/RV regimes generally co-move; this avoids splitting an already
    # small dataset.
    T_hours_by_ticker = {m.ticker: _hours_to_expiry(m.close_time) for m in markets}
    iv_rv_ratio, _ = fit_cycle_iv(markets, spot, sigma_blended, T_hours_by_ticker)

    recent_ratios = store.get_recent_iv_rv_ratios(n=cfg.IV_CALIBRATION_MIN_OBS)
    if len(recent_ratios) >= cfg.IV_CALIBRATION_MIN_OBS and iv_rv_ratio is not None:
        all_ratios = recent_ratios + [iv_rv_ratio]
        adaptive_margin = max(
            cfg.IV_SAFETY_MARGIN_MIN,
            min(cfg.IV_SAFETY_MARGIN_MAX, statistics.median(all_ratios)),
        )
        log.info(
            "%s adaptive margin: IV/RV=%.3f  trailing_median=%.3f  (static=%.3f)",
            symbol, iv_rv_ratio, adaptive_margin, cfg.VOL_SAFETY_MARGIN,
        )
    else:
        adaptive_margin = cfg.VOL_SAFETY_MARGIN
        log.info(
            "%s adaptive margin: cold start (n=%d < %d) — using static %.3f",
            symbol, len(recent_ratios), cfg.IV_CALIBRATION_MIN_OBS, cfg.VOL_SAFETY_MARGIN,
        )

    sigma_adjusted = sigma_blended * adaptive_margin
    mu = mu_raw if cfg.USE_DRIFT else 0.0
    log.info(
        "%s σ_adjusted = %.4f × %.3f = %.4f  μ=%+.4f",
        symbol, sigma_blended, adaptive_margin, sigma_adjusted, mu,
    )

    return UnderlyingState(
        symbol=symbol,
        series_ticker=series_ticker,
        spot=spot,
        sigma_short=sigma_short,
        sigma_long=sigma_long,
        sigma_adjusted=sigma_adjusted,
        mu=mu,
        iv_rv_ratio=iv_rv_ratio,
        adaptive_margin=adaptive_margin,
        markets=markets,
    )


def _run_cycle(kalshi: KalshiClient, risk: DailyRisk, store: Store, dry_run: bool) -> None:
    global _consecutive_price_feed_failures
    log.info("--- Cycle start ---")
    orders_placed = 0

    enabled = _enabled_underlyings()
    if not enabled:
        log.error("No underlyings enabled — set ENABLE_BTC and/or ENABLE_ETH")
        return

    # --- Build per-underlying state ---
    underlyings: dict[str, UnderlyingState] = {}
    for symbol, series in enabled:
        state = _build_underlying_state(kalshi, symbol, series, store)
        if state is not None:
            underlyings[symbol] = state

    if not underlyings:
        _consecutive_price_feed_failures += 1
        log.error("No underlyings produced usable state this cycle")
        if _consecutive_price_feed_failures == 3 or _consecutive_price_feed_failures % 10 == 0:
            alert(
                f"All underlyings failed for {_consecutive_price_feed_failures} consecutive cycles",
                level="ERROR",
            )
        return
    _consecutive_price_feed_failures = 0

    # --- Account-level fetches (positions, balance) ---
    try:
        positions = kalshi.get_positions()
        balance = kalshi.get_balance()
    except Exception as e:
        log.error("Kalshi account fetch error: %s", e)
        alert(f"Kalshi account fetch error: {e}", level="ERROR")
        return

    # --- Set session balance for drawdown tracking ---
    risk.set_session_balance(balance)

    # --- Empirical slippage adjustment ---
    slippage_factor = store.get_slippage_factor(
        min_trades=cfg.SLIPPAGE_ADJUSTMENT_MIN_TRADES,
        lookback_days=cfg.SLIPPAGE_ADJUSTMENT_LOOKBACK_DAYS,
    )
    risk.set_slippage_factor(slippage_factor)
    if slippage_factor is not None:
        log.info("Empirical slippage factor: %.2f", slippage_factor)

    total_markets = sum(len(u.markets) for u in underlyings.values())

    # --- Drawdown guard ---
    if risk.check_drawdown(balance):
        log.warning("Drawdown limit reached — halting all trading for today")
        alert(
            f"Drawdown halt: balance=${balance:.2f} exceeded {cfg.MAX_DRAWDOWN_PCT:.0%} limit",
            level="WARNING",
        )
        store.snapshot_daily(balance, risk.daily_spent, len(positions))
        # Log the runs row using BTC's stats if available, else first underlying.
        primary = underlyings.get("BTC") or next(iter(underlyings.values()))
        store.log_run(
            btc_price=primary.spot, sigma_short=primary.sigma_short, sigma_long=primary.sigma_long,
            markets_scanned=total_markets, signals_found=0, orders_placed=0,
            dry_run=dry_run, iv_rv_ratio=primary.iv_rv_ratio,
            adaptive_safety_margin=primary.adaptive_margin,
        )
        return

    # --- Fill quality check ---
    _check_fills(kalshi, store)

    # --- Position exit check (loss + take-profit, routed by underlying) ---
    exited_tickers = _check_exits(kalshi, store, positions, underlyings, dry_run)
    if exited_tickers:
        try:
            positions = kalshi.get_positions()
            balance = kalshi.get_balance()
        except Exception:
            pass  # use stale data; exits already logged

    held_tickers = {p.ticker for p in positions}
    open_count = len(positions)

    # --- Signal scan across all underlyings ---
    all_signals: list[tuple[Signal, UnderlyingState]] = []
    for u in underlyings.values():
        sigs = scan_markets(
            markets=u.markets,
            spot_price=u.spot,
            sigma=u.sigma_adjusted,
            min_edge=cfg.MIN_EDGE,
            min_t_hours=cfg.MIN_T_HOURS,
            held_tickers=held_tickers,
            fee=cfg.KALSHI_TAKER_FEE,
            max_bid_ask_spread=cfg.MAX_BID_ASK_SPREAD,
            max_bid_ask_pct_spread=cfg.MAX_BID_ASK_PCT_SPREAD,
            max_last_price_divergence=cfg.MAX_LAST_PRICE_DIVERGENCE,
            mu=u.mu,
        )
        for s in sigs:
            all_signals.append((s, u))

    # Combine and sort across underlyings by net edge (best opportunities first).
    all_signals.sort(key=lambda pair: pair[0].edge, reverse=True)
    log.info(
        "Combined signals: %d across %d underlying(s) (%s)",
        len(all_signals), len(underlyings),
        ",".join(sorted(underlyings)),
    )

    # --- Order placement ---
    for sig, u in all_signals:
        if not risk.can_trade(open_count):
            break

        contracts = risk.size_order(sig, current_balance=balance, open_positions=open_count)
        if contracts < 1:
            log.info("Signal %s %s: sized to 0 contracts, skipping", sig.ticker, sig.side)
            continue

        cost_estimate = contracts * sig.price
        log.info(
            "SIGNAL [%s] %s %s: theo=%.4f ask=%.2f mid=%.2f gross_edge=%.4f net_edge=%.4f fee=%.2f  "
            "→  %d contracts (~$%.2f)  balance=$%.2f%s",
            u.symbol, sig.ticker, sig.side, sig.theo_prob, sig.price, sig.mid_price,
            sig.gross_edge, sig.edge, sig.fee,
            contracts, cost_estimate, balance, "  [DRY RUN]" if dry_run else "",
        )

        if dry_run:
            continue

        try:
            orders = _execute_with_price_improvement(
                kalshi=kalshi,
                ticker=sig.ticker,
                side=sig.side,
                contracts=contracts,
                ask_price=sig.price,
                mid_price=sig.mid_price,
                dry_run=dry_run,
                symbol=u.symbol,
            )
            if not orders:
                log.warning("Order execution returned no fill for %s", sig.ticker)
                continue

            total_filled = sum(o.fill_count for o in orders)
            total_cost = sum(o.taker_fill_cost for o in orders)
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
                    hours_to_expiry=sig.hours_to_expiry,
                )
            if total_filled > 0:
                open_count += 1
                orders_placed += 1
            time.sleep(0.5)  # avoid burst rate-limiting between orders
        except Exception as e:
            log.error("Order failed for %s: %s", sig.ticker, e)

    store.snapshot_daily(balance, risk.daily_spent, open_count)

    # Persist a single runs row anchored on BTC (or the first available underlying).
    primary = underlyings.get("BTC") or next(iter(underlyings.values()))
    store.log_run(
        btc_price=primary.spot,
        sigma_short=primary.sigma_short,
        sigma_long=primary.sigma_long,
        markets_scanned=total_markets,
        signals_found=len(all_signals),
        orders_placed=orders_placed,
        dry_run=dry_run,
        iv_rv_ratio=primary.iv_rv_ratio,
        adaptive_safety_margin=primary.adaptive_margin,
    )

    try:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        generate_report(today, cfg.DB_PATH, cfg.REPORTS_DIR)
    except Exception as e:
        log.warning("Daily report generation failed: %s", e)

    log.info("--- Cycle end: %d signal(s), %d order(s) placed ---", len(all_signals), orders_placed)


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
