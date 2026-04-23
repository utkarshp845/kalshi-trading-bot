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
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Optional

import bot.config as cfg
from bot.deribit_iv import get_atm_iv
from bot.feature_builder import build_asset_snapshot, build_market_features
from bot.implied_vol import fit_cycle_iv
from bot.kalshi_client import KalshiClient, Market, Order, Position
from bot.models import AssetSnapshot
from bot.monitor import alert
from bot.portfolio_risk import PortfolioRisk
from bot.price_feed import get_price_vol_drift, get_spot_price
from bot.pricing import calc_prob
from bot.providers import fetch_deribit_iv_snapshot, fetch_markets_snapshot, fetch_price_snapshot
from bot.report import generate_report
from bot.risk import DailyRisk
from bot.store import Store
from bot.strategy import Signal, _hours_to_expiry, _parse_strike, scan_markets
from bot.strategy_engine import decide_signal


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
    trading_mode = _resolved_trading_mode(dry_run)
    enabled_underlyings = ",".join(s for s, _ in _enabled_underlyings()) or "NONE"
    log.info(
        "=== Kalshi Bot starting (dry_run=%s trading_mode=%s) — v2.2.0 underlyings=[%s] ===",
        dry_run, trading_mode, enabled_underlyings,
    )
    log.info(
        "Config: min_edge=%.2f  min_t_hours=%.1f  daily_spend=%.0f%%/floor=$%.0f  "
        "max_positions=%d  max_contracts=%d  kelly=%.2f  corr_discount=%.2f  "
        "vol_margin=%.2f  iv_margin=[%.2f,%.2f]  max_vol_ratio=%.1f  "
        "max_spread=%.2f  drawdown_limit=%.0f%%  bankroll_frac=%.0f%%  "
        "exit=%s  take_profit=%.1fx  drift=%s  deribit_iv=%s(w=%.2f)  "
        "maker=%s(%ds)  price_improvement=%s(%ds)  poll=%ds",
        cfg.MIN_EDGE, cfg.MIN_T_HOURS, cfg.DAILY_SPEND_PCT * 100, cfg.DAILY_SPEND_FLOOR,
        cfg.MAX_POSITIONS, cfg.MAX_CONTRACTS_PER_MARKET,
        cfg.KELLY_FRACTION, cfg.CORRELATION_DISCOUNT_FACTOR,
        cfg.VOL_SAFETY_MARGIN, cfg.IV_SAFETY_MARGIN_MIN, cfg.IV_SAFETY_MARGIN_MAX,
        cfg.MAX_VOL_RATIO, cfg.MAX_BID_ASK_SPREAD,
        cfg.MAX_DRAWDOWN_PCT * 100, cfg.BANKROLL_FRACTION * 100,
        cfg.ENABLE_POSITION_EXIT, cfg.TAKE_PROFIT_TRIGGER, cfg.USE_DRIFT,
        cfg.ENABLE_DERIBIT_IV, cfg.DERIBIT_IV_WEIGHT,
        cfg.ENABLE_MAKER_ORDERS, cfg.MAKER_ORDER_TIMEOUT_SEC,
        cfg.ENABLE_PRICE_IMPROVEMENT, cfg.PRICE_IMPROVEMENT_TIMEOUT_SEC,
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

    risk = PortfolioRisk(
        daily_spend_pct=cfg.DAILY_SPEND_PCT,
        daily_spend_floor=cfg.DAILY_SPEND_FLOOR,
        max_contracts_per_market=cfg.MAX_CONTRACTS_PER_MARKET,
        max_positions=cfg.MAX_POSITIONS,
        max_symbol_daily_spend_pct=cfg.MAX_SYMBOL_DAILY_SPEND_PCT,
        max_symbol_positions=cfg.MAX_SYMBOL_POSITIONS,
        kelly_fraction=cfg.KELLY_FRACTION,
        max_drawdown_pct=cfg.MAX_DRAWDOWN_PCT,
        bankroll_fraction=cfg.BANKROLL_FRACTION,
        drawdown_tier_1_pct=cfg.DRAWDOWN_TIER_1_PCT,
        drawdown_tier_1_scale=cfg.DRAWDOWN_TIER_1_SCALE,
        drawdown_tier_2_pct=cfg.DRAWDOWN_TIER_2_PCT,
        drawdown_tier_2_scale=cfg.DRAWDOWN_TIER_2_SCALE,
        correlation_discount_factor=cfg.CORRELATION_DISCOUNT_FACTOR,
    )

    store = Store(db_path=cfg.DB_PATH, trades_csv_path=cfg.TRADES_CSV)
    store.open()

    # Restore today's already-spent from DB (so a restart doesn't reset limits)
    risk._daily_spent = store.get_todays_spend()
    risk.restore_symbol_spend(store.get_todays_spend_by_symbol())
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


def _backfill_market_outcomes(kalshi: KalshiClient, store: Store, before_iso: str) -> None:
    tickers = store.get_unlabeled_market_tickers(before_iso=before_iso, limit=100)
    for ticker in tickers:
        try:
            market = kalshi.get_historical_market(ticker)
        except Exception:
            try:
                market = kalshi.get_market(ticker).__dict__
            except Exception as exc:
                log.debug("Outcome backfill skipped for %s: %s", ticker, exc)
                continue

        result = str(market.get("result") or "").strip().lower()
        settlement_value = market.get("settlement_value_dollars")
        if settlement_value is not None:
            settlement_value = float(settlement_value)
        elif result in {"yes", "no"}:
            settlement_value = 1.0 if result == "yes" else 0.0
        else:
            settlement_value = None
        if settlement_value is None:
            continue
        store.upsert_market_outcome(
            ticker=ticker,
            result=result or ("yes" if settlement_value >= 0.5 else "no"),
            settlement_value=settlement_value,
            close_time=str(market.get("close_time") or ""),
            settlement_ts=str(market.get("settlement_ts") or market.get("expiration_time") or ""),
        )


def _execute_passive_exit(
    kalshi: KalshiClient,
    ticker: str,
    side: str,
    quantity: int,
    passive_price: float,
    symbol: str,
) -> list[Order]:
    if quantity <= 0 or passive_price < 0.01:
        return []
    try:
        entry_spot = get_spot_price(symbol)
    except Exception:
        entry_spot = None

    order = kalshi.sell_position(ticker, side, quantity, passive_price)
    total_wait = max(0, cfg.MAKER_ENTRY_TIMEOUT_SEC)
    poll = max(1, cfg.STALE_ORDER_POLL_SEC)
    elapsed = 0
    current = order
    while elapsed < total_wait:
        step = min(poll, total_wait - elapsed)
        time.sleep(step)
        elapsed += step
        try:
            current = kalshi.get_order(order.order_id)
        except Exception:
            current = order
        if current.fill_count >= quantity:
            return [current]
        if entry_spot is not None:
            try:
                current_spot = get_spot_price(symbol)
                drift = abs(current_spot - entry_spot) / entry_spot
                if drift > cfg.STALE_ORDER_SPOT_MOVE_PCT:
                    break
            except Exception:
                pass

    kalshi.cancel_order(order.order_id)
    return [current] if current.fill_count > 0 else []


def _check_exits(
    kalshi: KalshiClient,
    store: Store,
    positions: list[Position],
    assets: dict[str, AssetSnapshot],
    trading_mode: str,
    cycle_id: Optional[str] = None,
) -> list[str]:
    """
    Re-evaluate each open position and exit on take-profit, loss, or near-expiry.

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
        if symbol is None or symbol not in assets:
            log.debug("Exit check: skipping %s (no underlying state)", pos.ticker)
            continue
        asset = assets[symbol]

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
        theo_prob = calc_prob(asset.spot, strike, T_years, asset.sigma_adjusted, mu=asset.mu)
        current_value = theo_prob if pos.side == "yes" else (1.0 - theo_prob)
        current_bid = market.yes_bid if pos.side == "yes" else market.no_bid
        current_ask = market.yes_ask if pos.side == "yes" else market.no_ask
        liquidation_edge = current_value - current_bid - cfg.KALSHI_TAKER_FEE
        near_expiry = T_hours < (20.0 / 60.0)
        take_profit_hit = (
            current_value >= (cfg.TAKE_PROFIT_TRIGGER * entry_price_per_contract)
            and T_hours >= cfg.TAKE_PROFIT_MIN_HOURS
            and liquidation_edge >= 0.0
        )

        loss_trigger_hit = liquidation_edge < -0.02
        force_exit_hit = near_expiry and liquidation_edge <= 0

        if take_profit_hit or loss_trigger_hit or force_exit_hit:
            if take_profit_hit:
                reason = "TAKE_PROFIT"
                log_fn = log.info
            elif loss_trigger_hit:
                reason = "LOSS"
                log_fn = log.warning
            else:
                reason = "FORCE"
                log_fn = log.info
            log_fn(
                "%s EXIT %s %s: current_value=%.4f vs entry=%.4f  bid=%.4f  "
                "liq_edge=%.4f  T=%.2fh  [%s]",
                reason, pos.ticker, pos.side, current_value, entry_price_per_contract,
                current_bid, liquidation_edge, T_hours, trading_mode.upper(),
            )

            if trading_mode == "live" and (current_bid >= 0.01 or current_ask >= 0.01):
                try:
                    passive_exit = take_profit_hit or not near_expiry
                    sell_price = max(0.01, current_ask if passive_exit else current_bid)
                    if passive_exit:
                        orders = _execute_passive_exit(
                            kalshi,
                            pos.ticker,
                            pos.side,
                            pos.quantity,
                            sell_price,
                            symbol,
                        )
                    else:
                        orders = [kalshi.sell_position(pos.ticker, pos.side, pos.quantity, sell_price)]
                    total_filled = sum(order.fill_count for order in orders)
                    total_cost = sum(order.taker_fill_cost for order in orders)
                    for order in orders:
                        store.log_order(
                            order,
                            theo_prob=theo_prob,
                            gross_edge=current_value - sell_price,
                            edge=current_value - sell_price - cfg.KALSHI_TAKER_FEE,
                            fee=cfg.KALSHI_TAKER_FEE,
                            hours_to_expiry=T_hours,
                        )
                    if cycle_id:
                        store.log_execution_attempt(
                            cycle_id=cycle_id,
                            symbol=symbol,
                            ticker=pos.ticker,
                            side=pos.side,
                            trading_mode=trading_mode,
                            requested_contracts=pos.quantity,
                            filled_contracts=total_filled,
                            ask_price=sell_price,
                            mid_price=sell_price,
                            estimated_cost=sell_price * pos.quantity,
                            actual_cost=total_cost,
                            status="exit",
                            reason=reason.lower(),
                        )
                    if total_filled > 0:
                        exited_tickers.append(pos.ticker)
                    alert(
                        f"{reason} exit: {pos.ticker} {pos.side} x{pos.quantity} "
                        f"@ ${sell_price:.2f} (value {current_value:.2%} of entry)",
                        level="WARNING",
                    )
                except Exception as e:
                    log.error("Exit order failed for %s: %s", pos.ticker, e)
                    alert(f"Exit order failed for {pos.ticker}: {e}", level="ERROR")
            else:
                if cycle_id:
                    store.log_execution_attempt(
                        cycle_id=cycle_id,
                        symbol=symbol,
                        ticker=pos.ticker,
                        side=pos.side,
                        trading_mode=trading_mode,
                        requested_contracts=pos.quantity,
                        filled_contracts=0,
                        ask_price=current_bid,
                        mid_price=current_bid,
                        estimated_cost=current_bid * pos.quantity,
                        actual_cost=0.0,
                        status="exit_skipped",
                        reason=f"{reason.lower()}_{trading_mode}",
                    )
                exited_tickers.append(pos.ticker)

    return exited_tickers


def _resolved_trading_mode(dry_run: bool) -> str:
    if dry_run:
        return "observe"
    if cfg.TRADING_MODE not in {"live", "paper", "observe"}:
        return "observe"
    return cfg.TRADING_MODE


def _positions_by_symbol(positions: list[Position]) -> dict[str, int]:
    counts: Counter[str] = Counter()
    for pos in positions:
        symbol = _symbol_for_ticker(pos.ticker)
        if symbol:
            counts[symbol] += 1
    return dict(counts)


def _build_cycle_assets(
    kalshi: KalshiClient,
    store: Store,
    open_positions_by_symbol: dict[str, int],
) -> tuple[dict[str, AssetSnapshot], dict[str, list], int]:
    assets: dict[str, AssetSnapshot] = {}
    features_by_symbol: dict[str, list] = {}
    total_markets = 0
    for symbol, series in _enabled_underlyings():
        try:
            price_result = fetch_price_snapshot(
                symbol=symbol,
                short_days=cfg.VOL_SHORT_DAYS,
                long_days=cfg.VOL_LONG_DAYS,
                drift_days=cfg.DRIFT_LOOKBACK_DAYS,
            )
            markets_result = fetch_markets_snapshot(kalshi, symbol, series)
            if cfg.ENABLE_DERIBIT_IV:
                iv_result = fetch_deribit_iv_snapshot(symbol, price_result.spot, cfg.DERIBIT_MIN_DTE_HOURS)
            else:
                from bot.models import SourceSnapshot
                iv_result = type("DeribitIVResult", (), {
                    "source": SourceSnapshot(
                        provider="deribit",
                        symbol=symbol,
                        fetched_at=datetime.now(timezone.utc).isoformat(),
                        freshness_sec=0.0,
                        status="disabled",
                        payload_hash="disabled",
                    ),
                    "iv": None,
                })()
            asset = build_asset_snapshot(
                symbol=symbol,
                series_ticker=series,
                price_result=price_result,
                markets_result=markets_result,
                iv_result=iv_result,
                store=store,
                open_positions=open_positions_by_symbol.get(symbol, 0),
            )
            assets[symbol] = asset
            features = build_market_features(
                asset, markets_result.markets,
                fee=cfg.KALSHI_TAKER_FEE,
                maker_entry=cfg.ENABLE_MAKER_ORDERS,
            )
            features_by_symbol[symbol] = features
            total_markets += len(markets_result.markets)
        except Exception as e:
            log.error("Cycle build failed for %s: %s", symbol, e)
    return assets, features_by_symbol, total_markets


def _execute_with_price_improvement(
    kalshi: KalshiClient,
    ticker: str,
    side: str,
    contracts: int,
    ask_price: float,
    mid_price: float,
    bid_price: float = 0.0,
    dry_run: bool = False,
    symbol: str = "BTC",
) -> list[Order]:
    """
    Maker-first entry: post at the live market bid with post_only=True and
    cancel rather than cross the book. Uses MAKER_ENTRY_TIMEOUT_SEC to wait
    for a passive fill before giving up.
    """
    passive_price = max(0.01, min(0.99, mid_price if mid_price < ask_price else ask_price - 0.01))
    if passive_price <= 0:
        return []

    try:
        entry_market = kalshi.get_market(ticker)
        passive_price = entry_market.yes_bid if side == "yes" else entry_market.no_bid
    except Exception:
        pass
    passive_price = max(0.01, min(0.99, passive_price))

    if not cfg.ENABLE_PRICE_IMPROVEMENT:
        order = kalshi.place_order(ticker, side, contracts, passive_price, post_only=True)
        return [order] if order is not None else []

    try:
        entry_spot = get_spot_price(symbol)
    except Exception as e:
        log.debug("Spot snapshot before maker-first order failed: %s", e)
        entry_spot = None

    try:
        order1 = kalshi.place_order(ticker, side, contracts, passive_price, post_only=True)
        log.info("Maker-first entry: placed %d @ passive=%.3f (ask=%.3f)", contracts, passive_price, ask_price)
    except Exception as e:
        log.warning("Maker-first order failed for %s: %s", ticker, e)
        return []

    total_wait = max(0, cfg.MAKER_ENTRY_TIMEOUT_SEC)
    poll = max(1, cfg.STALE_ORDER_POLL_SEC)
    spot_moved = False
    book_deteriorated = False
    elapsed = 0

    while elapsed < total_wait:
        step = min(poll, total_wait - elapsed)
        time.sleep(step)
        elapsed += step

        try:
            order1 = kalshi.get_order(order1.order_id)
        except Exception:
            pass

        if order1.fill_count >= contracts:
            log.info("Maker-first entry: fully filled %d/%d after %ds", order1.fill_count, contracts, elapsed)
            return [order1]

        if entry_spot is not None:
            try:
                current_spot = get_spot_price(symbol)
                drift = abs(current_spot - entry_spot) / entry_spot
                if drift > cfg.STALE_ORDER_SPOT_MOVE_PCT:
                    log.warning(
                        "%s spot moved %.3f%% (%.0f → %.0f) during maker wait — cancelling %s",
                        symbol, drift * 100, entry_spot, current_spot, order1.order_id[:8],
                    )
                    spot_moved = True
                    break
            except Exception as e:
                log.debug("Spot price poll failed: %s", e)

        try:
            market = kalshi.get_market(ticker)
            current_bid = market.yes_bid if side == "yes" else market.no_bid
            current_ask = market.yes_ask if side == "yes" else market.no_ask
            if current_bid + 1e-9 < passive_price or current_ask > ask_price + cfg.MAX_DEPTH_SLIPPAGE_PER_CONTRACT:
                book_deteriorated = True
                break
        except Exception:
            pass

    filled = order1.fill_count
    kalshi.cancel_order(order1.order_id)
    if spot_moved or book_deteriorated:
        log.info(
            "Maker-first entry canceled for %s: spot_moved=%s book_deteriorated=%s",
            ticker, spot_moved, book_deteriorated,
        )
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
    trading_mode = _resolved_trading_mode(dry_run)
    cycle_id = datetime.now(timezone.utc).isoformat()
    log.info("--- Cycle start ---")
    orders_placed = 0

    enabled = _enabled_underlyings()
    if not enabled:
        log.error("No underlyings enabled — set ENABLE_BTC and/or ENABLE_ETH")
        return

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
    open_positions_by_symbol = _positions_by_symbol(positions)

    assets, features_by_symbol, total_markets = _build_cycle_assets(kalshi, store, open_positions_by_symbol)
    for asset in assets.values():
        store.log_asset_run(cycle_id, asset)
    for features in features_by_symbol.values():
        for feature in features:
            store.log_market_snapshot(cycle_id, feature)

    if not assets:
        _consecutive_price_feed_failures += 1
        log.error("No underlyings produced usable state this cycle")
        if _consecutive_price_feed_failures == 3 or _consecutive_price_feed_failures % 10 == 0:
            alert(
                f"All underlyings failed for {_consecutive_price_feed_failures} consecutive cycles",
                level="ERROR",
            )
        return
    _consecutive_price_feed_failures = 0

    # --- Empirical slippage adjustment ---
    slippage_factor = store.get_slippage_factor(
        min_trades=cfg.SLIPPAGE_ADJUSTMENT_MIN_TRADES,
        lookback_days=cfg.SLIPPAGE_ADJUSTMENT_LOOKBACK_DAYS,
    )
    risk.set_slippage_factor(slippage_factor)
    if slippage_factor is not None:
        log.info("Empirical slippage factor: %.2f", slippage_factor)

    # --- Drawdown guard ---
    already_halted = risk.drawdown_halted
    if risk.check_drawdown(balance):
        log.warning("Drawdown limit reached — halting all trading for today")
        if not already_halted:
            session_start = risk.session_start_balance
            actual_dd = (1.0 - balance / session_start) * 100 if session_start > 0 else 0.0
            alert(
                f"Trading halted: balance dropped to ${balance:,.2f} "
                f"({actual_dd:.1f}% below session start of ${session_start:,.2f}), "
                f"exceeding the {cfg.MAX_DRAWDOWN_PCT:.0%} drawdown limit. "
                f"No new trades until tomorrow.",
                level="WARNING",
            )
        store.snapshot_daily(balance, risk.daily_spent, len(positions))
        # Log the runs row using BTC's stats if available, else first underlying.
        primary = assets.get("BTC") or next(iter(assets.values()))
        store.log_run(
            btc_price=primary.spot, sigma_short=primary.sigma_short, sigma_long=primary.sigma_long,
            markets_scanned=total_markets, signals_found=0, orders_placed=0,
            dry_run=trading_mode != "live", iv_rv_ratio=primary.iv_rv_ratio,
            adaptive_safety_margin=primary.adaptive_margin,
            cycle_id=cycle_id,
        )
        return

    # --- Fill quality check ---
    _check_fills(kalshi, store)
    _backfill_market_outcomes(kalshi, store, before_iso=cycle_id)

    # --- Position exit check (loss + take-profit, routed by underlying) ---
    exited_tickers = _check_exits(kalshi, store, positions, assets, trading_mode, cycle_id=cycle_id)
    if exited_tickers:
        if trading_mode == "live":
            try:
                positions = kalshi.get_positions()
                balance = kalshi.get_balance()
            except Exception:
                pass  # use stale data; exits already logged
        else:
            positions = [p for p in positions if p.ticker not in set(exited_tickers)]
        open_positions_by_symbol = _positions_by_symbol(positions)

    held_tickers = {p.ticker for p in positions}
    open_count = len(positions)

    # --- Signal scan across all underlyings ---
    all_decisions = []
    reject_counts: Counter[str] = Counter()
    for symbol, asset in assets.items():
        for feature in features_by_symbol.get(symbol, []):
            decision = decide_signal(
                store,
                asset,
                feature,
                held_tickers,
                before_iso=cycle_id,
                trading_mode=trading_mode,
            )
            store.log_signal_decision(cycle_id, decision)
            all_decisions.append(decision)
            if not decision.eligible:
                reject_counts[decision.reject_reason] += 1

    eligible_decisions = sorted(
        [d for d in all_decisions if d.eligible],
        key=lambda d: d.score,
        reverse=True,
    )
    log.info(
        "Combined signals: %d eligible / %d total across %d underlying(s) (%s)%s",
        len(eligible_decisions), len(all_decisions), len(assets),
        ",".join(sorted(assets)),
        f" rejects={dict(reject_counts)}" if reject_counts else "",
    )

    # --- Order placement ---
    for decision in eligible_decisions:
        if not risk.can_trade_symbol(decision.symbol, open_positions_by_symbol):
            break

        contracts = risk.size_order(
            decision,
            current_balance=balance,
            open_positions_by_symbol=open_positions_by_symbol,
        )
        if contracts < 1:
            store.log_execution_attempt(
                cycle_id=cycle_id,
                symbol=decision.symbol,
                ticker=decision.ticker,
                side=decision.side,
                trading_mode=trading_mode,
                requested_contracts=0,
                filled_contracts=0,
                ask_price=decision.ask,
                mid_price=decision.mid_price,
                estimated_cost=0.0,
                actual_cost=0.0,
                status="skipped",
                reason=risk.last_size_reason or "size_zero",
            )
            log.info("Signal %s %s: sized to 0 contracts, skipping", decision.ticker, decision.side)
            continue

        cost_estimate = contracts * decision.ask
        log.info(
            "SIGNAL [%s] %s %s: theo=%.4f ask=%.2f mid=%.2f gross_edge=%.4f net_edge=%.4f "
            "required=%.4f score=%.4f proxy=%.4f depth_slip=%.4f fee=%.2f  "
            "→  %d contracts (~$%.2f)  balance=$%.2f%s",
            decision.symbol, decision.ticker, decision.side, decision.theo_prob, decision.ask, decision.mid_price,
            decision.gross_edge, decision.edge, decision.required_edge, decision.score,
            decision.realized_edge_proxy, decision.depth_slippage, decision.fee,
            contracts, cost_estimate, balance, "  [SIMULATED]" if trading_mode != "live" else "",
        )

        if trading_mode == "observe":
            store.log_execution_attempt(
                cycle_id=cycle_id,
                symbol=decision.symbol,
                ticker=decision.ticker,
                side=decision.side,
                trading_mode=trading_mode,
                requested_contracts=contracts,
                filled_contracts=0,
                ask_price=decision.ask,
                mid_price=decision.mid_price,
                estimated_cost=cost_estimate,
                actual_cost=0.0,
                status="observe",
                reason="observe_mode",
            )
            continue
        if trading_mode == "paper":
            risk.record_fill(cost_estimate, decision.symbol)
            balance -= cost_estimate
            open_positions_by_symbol[decision.symbol] = open_positions_by_symbol.get(decision.symbol, 0) + 1
            open_count += 1
            orders_placed += 1
            store.log_execution_attempt(
                cycle_id=cycle_id,
                symbol=decision.symbol,
                ticker=decision.ticker,
                side=decision.side,
                trading_mode=trading_mode,
                requested_contracts=contracts,
                filled_contracts=contracts,
                ask_price=decision.ask,
                mid_price=decision.mid_price,
                estimated_cost=cost_estimate,
                actual_cost=cost_estimate,
                status="paper_fill",
                reason="paper_mode",
            )
            continue

        try:
            orders = _execute_with_price_improvement(
                kalshi=kalshi,
                ticker=decision.ticker,
                side=decision.side,
                contracts=contracts,
                ask_price=decision.ask,
                mid_price=decision.mid_price,
                bid_price=getattr(decision, "bid_price", 0.0),
                dry_run=False,
                symbol=decision.symbol,
            )
            if not orders:
                store.log_execution_attempt(
                    cycle_id=cycle_id,
                    symbol=decision.symbol,
                    ticker=decision.ticker,
                    side=decision.side,
                    trading_mode=trading_mode,
                    requested_contracts=contracts,
                    filled_contracts=0,
                    ask_price=decision.ask,
                    mid_price=decision.mid_price,
                    estimated_cost=cost_estimate,
                    actual_cost=0.0,
                    status="no_fill",
                    reason="execution_no_fill",
                )
                log.warning("Order execution returned no fill for %s", decision.ticker)
                continue

            total_filled = sum(o.fill_count for o in orders)
            total_cost = sum(o.taker_fill_cost for o in orders)
            if total_filled > 0:
                # For maker fills taker_fill_cost may be 0; fall back to estimate
                fill_cost = total_cost if total_cost > 0 else cost_estimate
                risk.record_fill(fill_cost, decision.symbol)
                balance -= fill_cost
            else:
                fill_cost = 0.0
            for o in orders:
                store.log_order(
                    o,
                    theo_prob=decision.theo_prob,
                    gross_edge=decision.gross_edge,
                    edge=decision.edge,
                    fee=decision.fee,
                    hours_to_expiry=decision.hours_to_expiry,
                )
            store.log_execution_attempt(
                cycle_id=cycle_id,
                symbol=decision.symbol,
                ticker=decision.ticker,
                side=decision.side,
                trading_mode=trading_mode,
                requested_contracts=contracts,
                filled_contracts=total_filled,
                ask_price=decision.ask,
                mid_price=decision.mid_price,
                estimated_cost=cost_estimate,
                actual_cost=fill_cost,
                status="live_fill" if total_filled > 0 else "live_no_fill",
                reason="live_execution",
            )
            if total_filled > 0:
                open_positions_by_symbol[decision.symbol] = open_positions_by_symbol.get(decision.symbol, 0) + 1
                open_count += 1
                orders_placed += 1
            time.sleep(0.5)  # avoid burst rate-limiting between orders
        except Exception as e:
            store.log_execution_attempt(
                cycle_id=cycle_id,
                symbol=decision.symbol,
                ticker=decision.ticker,
                side=decision.side,
                trading_mode=trading_mode,
                requested_contracts=contracts,
                filled_contracts=0,
                ask_price=decision.ask,
                mid_price=decision.mid_price,
                estimated_cost=cost_estimate,
                actual_cost=0.0,
                status="error",
                reason=str(e),
            )
            log.error("Order failed for %s: %s", decision.ticker, e)

    store.snapshot_daily(balance, risk.daily_spent, open_count)

    # Persist a single runs row anchored on BTC (or the first available underlying).
    primary = assets.get("BTC") or next(iter(assets.values()))
    store.log_run(
        btc_price=primary.spot,
        sigma_short=primary.sigma_short,
        sigma_long=primary.sigma_long,
        markets_scanned=total_markets,
        signals_found=len(eligible_decisions),
        orders_placed=orders_placed,
        dry_run=trading_mode != "live",
        iv_rv_ratio=primary.iv_rv_ratio,
        adaptive_safety_margin=primary.adaptive_margin,
        cycle_id=cycle_id,
    )

    try:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        generate_report(today, cfg.DB_PATH, cfg.REPORTS_DIR)
    except Exception as e:
        log.warning("Daily report generation failed: %s", e)

    log.info("--- Cycle end: %d eligible signal(s), %d order(s) placed ---", len(eligible_decisions), orders_placed)


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
