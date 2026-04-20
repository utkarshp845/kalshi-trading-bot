"""Pure multi-asset strategy scoring and rejection logic."""
from __future__ import annotations

from typing import Optional

import bot.config as cfg
from bot.models import AssetSnapshot, MarketFeature, SignalDecision


def _p75(values: list[float]) -> float:
    if not values:
        return 0.0
    vals = sorted(values)
    idx = min(len(vals) - 1, max(0, int(round(0.75 * (len(vals) - 1)))))
    return vals[idx]


def _required_edge(store, symbol: str, before_iso: Optional[str] = None) -> float:
    edge_leaks = store.get_recent_edge_leaks(symbol, cfg.EDGE_LEAK_LOOKBACK_FILLS, before_iso=before_iso)
    return max(cfg.MIN_EDGE, _p75(edge_leaks) + cfg.EDGE_HURDLE_BUFFER)


def _expected_slippage(store, symbol: str, before_iso: Optional[str] = None) -> float:
    slippages = store.get_recent_positive_slippages(symbol, cfg.EDGE_LEAK_LOOKBACK_FILLS, before_iso=before_iso)
    return _p75(slippages) if slippages else cfg.DEFAULT_EXPECTED_SLIPPAGE


def _uncertainty_penalty(store, symbol: str, before_iso: Optional[str] = None) -> float:
    errors = store.get_recent_settled_abs_errors(symbol, cfg.SETTLED_MAE_LOOKBACK_TRADES, before_iso=before_iso)
    return max(0.01, sum(errors) / len(errors)) if errors else cfg.DEFAULT_UNCERTAINTY_PENALTY


def decide_signal(
    store,
    asset: AssetSnapshot,
    feature: MarketFeature,
    held_tickers: set[str],
    before_iso: Optional[str] = None,
    trading_mode: str = "observe",
) -> SignalDecision:
    required_edge = _required_edge(store, asset.symbol, before_iso=before_iso)
    expected_slippage = _expected_slippage(store, asset.symbol, before_iso=before_iso)
    uncertainty_penalty = _uncertainty_penalty(store, asset.symbol, before_iso=before_iso)
    recent_realized_edges = store.get_recent_realized_edges(
        asset.symbol,
        cfg.LIVE_GUARD_LOOKBACK_FILLS,
        before_iso=before_iso,
    )
    recent_settled_errors = store.get_recent_settled_abs_errors(
        asset.symbol,
        cfg.LIVE_GUARD_LOOKBACK_SETTLED,
        before_iso=before_iso,
    )
    avg_recent_realized = (
        sum(recent_realized_edges) / len(recent_realized_edges)
        if recent_realized_edges else None
    )
    avg_recent_error = (
        sum(recent_settled_errors) / len(recent_settled_errors)
        if recent_settled_errors else None
    )

    if trading_mode == "live":
        required_edge = max(required_edge, cfg.LIVE_MIN_REQUIRED_EDGE)
        if (
            len(recent_realized_edges) < cfg.LIVE_MIN_FILL_HISTORY
            or len(recent_settled_errors) < cfg.LIVE_MIN_SETTLED_HISTORY
        ):
            required_edge = max(required_edge, cfg.COLD_START_MIN_EDGE)

    score = feature.edge - expected_slippage - uncertainty_penalty

    reject_reason = ""
    if not asset.tradeable:
        reject_reason = asset.health_status
    elif (
        trading_mode == "live"
        and asset.degraded
        and cfg.LIVE_SKIP_DEGRADED_ASSETS
    ):
        reject_reason = "degraded_asset"
    elif (
        trading_mode == "live"
        and len(recent_realized_edges) >= cfg.LIVE_MIN_FILL_HISTORY
        and avg_recent_realized is not None
        and avg_recent_realized <= cfg.LIVE_HALT_MAX_AVG_REALIZED_EDGE
    ):
        reject_reason = "negative_recent_realized_edge"
    elif (
        trading_mode == "live"
        and len(recent_settled_errors) >= cfg.LIVE_MIN_SETTLED_HISTORY
        and avg_recent_error is not None
        and avg_recent_error > cfg.LIVE_HALT_MAX_SETTLED_MAE
    ):
        reject_reason = "high_recent_model_error"
    elif feature.ticker in held_tickers:
        reject_reason = "already_held"
    elif feature.hours_to_expiry < cfg.MIN_T_HOURS:
        reject_reason = "t_too_small"
    elif not feature.spread_ok:
        reject_reason = "spread_too_wide"
    elif not feature.last_price_ok:
        reject_reason = "last_price_diverge"
    elif not feature.chain_ok:
        reject_reason = "chain_inconsistent"
    elif not feature.enough_sane_strikes:
        reject_reason = "insufficient_sane_strikes"
    elif not (cfg.THEO_PROB_BAND_MIN <= feature.contract_theo_prob <= cfg.THEO_PROB_BAND_MAX):
        reject_reason = "prob_band"
    elif feature.distance_from_spot_sigma > cfg.MAX_SIGMA_DISTANCE:
        reject_reason = "sigma_distance"
    elif feature.edge < required_edge:
        reject_reason = "edge_below_hurdle"
    elif score <= 0:
        reject_reason = "score_non_positive"

    return SignalDecision(
        symbol=asset.symbol,
        ticker=feature.ticker,
        side=feature.side,
        eligible=reject_reason == "",
        score=score,
        required_edge=required_edge,
        expected_slippage=expected_slippage,
        uncertainty_penalty=uncertainty_penalty,
        reject_reason=reject_reason,
        theo_prob=feature.contract_theo_prob,
        ask=feature.ask,
        bid=feature.bid,
        mid_price=feature.mid,
        gross_edge=feature.gross_edge,
        edge=feature.edge,
        fee=feature.fee,
        hours_to_expiry=feature.hours_to_expiry,
        strike=feature.strike,
        distance_from_spot_sigma=feature.distance_from_spot_sigma,
        degraded=asset.degraded,
        chain_break_ratio=feature.chain_break_ratio,
    )
