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
    return _p75(slippages) if slippages else 0.01


def _uncertainty_penalty(store, symbol: str, before_iso: Optional[str] = None) -> float:
    errors = store.get_recent_settled_abs_errors(symbol, cfg.SETTLED_MAE_LOOKBACK_TRADES, before_iso=before_iso)
    return max(0.01, sum(errors) / len(errors)) if errors else 0.01


def decide_signal(
    store,
    asset: AssetSnapshot,
    feature: MarketFeature,
    held_tickers: set[str],
    before_iso: Optional[str] = None,
) -> SignalDecision:
    required_edge = _required_edge(store, asset.symbol, before_iso=before_iso)
    expected_slippage = _expected_slippage(store, asset.symbol, before_iso=before_iso)
    uncertainty_penalty = _uncertainty_penalty(store, asset.symbol, before_iso=before_iso)
    score = feature.edge - expected_slippage - uncertainty_penalty

    reject_reason = ""
    if not asset.tradeable:
        reject_reason = asset.health_status
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
