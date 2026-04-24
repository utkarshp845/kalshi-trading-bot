"""Pure feature construction for multi-asset strategy decisions."""
from __future__ import annotations

import math
from collections import defaultdict
from typing import Optional

import bot.config as cfg
from bot.implied_vol import fit_cycle_iv
from bot.kalshi_client import Market
from bot.models import AssetSnapshot, MarketFeature
from bot.pricing import calc_prob
from bot.strategy import _hours_to_expiry, _parse_strike

_MIN_SANE_STRIKES = cfg.MIN_SANE_STRIKES
_MAX_NEIGHBOR_MID_JUMP = 0.12


def _pct_spread(bid: float, ask: float) -> float:
    mid = (ask + bid) / 2.0
    if mid <= 0.01:
        return 99.0
    return (ask - bid) / mid


def _spread_ok(bid: float, ask: float) -> bool:
    spread = ask - bid
    return spread <= cfg.MAX_BID_ASK_SPREAD and _pct_spread(bid, ask) <= cfg.MAX_BID_ASK_PCT_SPREAD


def build_asset_snapshot(
    symbol: str,
    series_ticker: str,
    price_result,
    markets_result,
    iv_result,
    store,
    open_positions: int = 0,
) -> AssetSnapshot:
    sigma_short = price_result.sigma_short
    sigma_long = price_result.sigma_long
    vol_ratio = sigma_short / sigma_long if sigma_long > 0 else 99.0

    if price_result.source.freshness_sec > cfg.DATA_STALE_AFTER_SEC_KRAKEN:
        health_status = "stale_spot"
    elif markets_result.source.freshness_sec > cfg.DATA_STALE_AFTER_SEC_KALSHI:
        health_status = "stale_markets"
    elif vol_ratio > cfg.MAX_VOL_RATIO:
        health_status = "unstable_vol_regime"
    else:
        health_status = "healthy"

    sigma_blended = sigma_short
    degraded = False
    if iv_result.iv is not None and iv_result.source.freshness_sec <= cfg.DATA_STALE_AFTER_SEC_DERIBIT:
        w = max(0.0, min(1.0, cfg.DERIBIT_IV_WEIGHT))
        sigma_blended = (1.0 - w) * sigma_short + w * iv_result.iv
    else:
        degraded = True

    T_hours_by_ticker = {m.ticker: _hours_to_expiry(m.close_time) for m in markets_result.markets}
    iv_rv_ratio, _ = fit_cycle_iv(markets_result.markets, price_result.spot, sigma_blended, T_hours_by_ticker)
    recent_ratios = store.get_recent_iv_rv_ratios(n=cfg.IV_CALIBRATION_MIN_OBS)
    if len(recent_ratios) >= cfg.IV_CALIBRATION_MIN_OBS and iv_rv_ratio is not None:
        all_ratios = recent_ratios + [iv_rv_ratio]
        adaptive_margin = sorted(all_ratios)[len(all_ratios) // 2]
        adaptive_margin = max(cfg.IV_SAFETY_MARGIN_MIN, min(cfg.IV_SAFETY_MARGIN_MAX, adaptive_margin))
    else:
        adaptive_margin = cfg.VOL_SAFETY_MARGIN
    sigma_adjusted = sigma_blended * adaptive_margin
    mu = price_result.mu if cfg.USE_DRIFT else 0.0
    if health_status != "healthy":
        degraded = True

    return AssetSnapshot(
        symbol=symbol,
        series_ticker=series_ticker,
        spot=price_result.spot,
        sigma_short=sigma_short,
        sigma_long=sigma_long,
        sigma_adjusted=sigma_adjusted,
        mu=mu,
        iv_rv_ratio=iv_rv_ratio,
        adaptive_margin=adaptive_margin,
        spot_source=price_result.source,
        markets_source=markets_result.source,
        iv_source=iv_result.source,
        degraded=degraded,
        health_status=health_status,
        open_positions=open_positions,
    )


def build_market_features(
    asset: AssetSnapshot,
    markets: list[Market],
    fee: float,
    maker_entry: bool = False,
) -> list[MarketFeature]:
    grouped: dict[str, list[dict]] = defaultdict(list)
    raw: list[dict] = []

    for market in markets:
        strike = _parse_strike(market.ticker)
        if strike is None:
            continue
        hours = _hours_to_expiry(market.close_time)
        if hours <= 0:
            continue
        T_years = hours / 8760.0
        yes_theo = calc_prob(asset.spot, strike, T_years, asset.sigma_adjusted, mu=asset.mu)
        no_theo = 1.0 - yes_theo

        # Maker orders fill at the bid with $0 fee; taker orders fill at ask and pay the fee.
        if maker_entry:
            yes_gross = yes_theo - market.yes_bid
            no_gross = no_theo - market.no_bid
            effective_fee = 0.0
        else:
            yes_gross = yes_theo - market.yes_ask
            no_gross = no_theo - market.no_ask
            effective_fee = fee

        yes_net = yes_gross - effective_fee
        no_net = no_gross - effective_fee

        side = "yes" if yes_net >= no_net else "no"
        ask = market.yes_ask if side == "yes" else market.no_ask
        bid = market.yes_bid if side == "yes" else market.no_bid
        mid = (ask + bid) / 2.0
        spread_abs = ask - bid
        spread_pct = _pct_spread(bid, ask)
        orderbook_metrics = (
            market.orderbook.entry_metrics(side, ask)
            if market.orderbook is not None
            else {
                "top_of_book_size": 0.0,
                "resting_size_at_entry": 0.0,
                "cumulative_size_at_entry": 0.0,
                "expected_fill_price": None,
                "depth_slippage": 0.0,
                "orderbook_available": False,
            }
        )
        orderbook_imbalance = market.orderbook.imbalance() if market.orderbook is not None else 0.0
        sqrt_t = math.sqrt(T_years)
        sigma_distance = 99.0
        if asset.sigma_adjusted > 0 and sqrt_t > 0:
            sigma_distance = abs(math.log(asset.spot / strike)) / (asset.sigma_adjusted * sqrt_t)
        yes_mid = (market.yes_ask + market.yes_bid) / 2.0
        raw_item = {
            "symbol": asset.symbol,
            "ticker": market.ticker,
            "close_time": market.close_time,
            "expiry_bucket": market.close_time[:10],
            "strike": strike,
            "side": side,
            "contract_theo_prob": yes_theo if side == "yes" else no_theo,
            "yes_theo_prob": yes_theo,
            "ask": ask,
            "bid": bid,
            "mid": mid,
            "yes_bid": market.yes_bid,
            "yes_ask": market.yes_ask,
            "no_bid": market.no_bid,
            "no_ask": market.no_ask,
            "spread_abs": spread_abs,
            "spread_pct": spread_pct,
            "gross_edge": yes_gross if side == "yes" else no_gross,
            "edge": yes_net if side == "yes" else no_net,
            "fee": effective_fee,
            "hours_to_expiry": hours,
            "distance_from_spot_sigma": sigma_distance,
            "last_price_divergence": (
                abs(market.last_price - yes_mid) if market.last_price is not None else None
            ),
            "spread_ok": _spread_ok(bid, ask),
            "last_price_ok": (
                market.last_price is None
                or abs(market.last_price - yes_mid) <= cfg.MAX_LAST_PRICE_DIVERGENCE
            ),
            "chain_break_ratio": 0.0,
            "chain_ok": True,
            "enough_sane_strikes": True,
            "top_of_book_size": float(orderbook_metrics["top_of_book_size"]),
            "resting_size_at_entry": float(orderbook_metrics["resting_size_at_entry"]),
            "cumulative_size_at_entry": float(orderbook_metrics["cumulative_size_at_entry"]),
            "expected_fill_price": (
                float(orderbook_metrics["expected_fill_price"])
                if orderbook_metrics["expected_fill_price"] is not None
                else None
            ),
            "depth_slippage": float(orderbook_metrics["depth_slippage"]),
            "orderbook_imbalance": orderbook_imbalance,
            "orderbook_available": bool(orderbook_metrics["orderbook_available"]),
        }
        raw.append(raw_item)
        grouped[raw_item["expiry_bucket"]].append(raw_item)

    for expiry_bucket, rows in grouped.items():
        rows.sort(key=lambda r: r["strike"])
        n = len(rows)

        # Compute per-adjacent-pair break flags
        pair_breaks: list[bool] = []
        for prev, cur in zip(rows, rows[1:]):
            theo_break = cur["yes_theo_prob"] > prev["yes_theo_prob"] + 1e-9
            prev_mid = (prev["yes_bid"] + prev["yes_ask"]) / 2.0
            cur_mid = (cur["yes_bid"] + cur["yes_ask"]) / 2.0
            mid_break = abs(cur_mid - prev_mid) > _MAX_NEIGHBOR_MID_JUMP
            pair_breaks.append(theo_break or mid_break)

        chain_break_ratio = sum(pair_breaks) / len(pair_breaks) if pair_breaks else 0.0
        sane_count = sum(1 for r in rows if r["spread_ok"] and r["last_price_ok"])
        enough_sane = sane_count >= _MIN_SANE_STRIKES

        for i, row in enumerate(rows):
            # Per-contract chain_ok: only False when BOTH neighbors are broken.
            # Edge contracts (index 0 or n-1) need only their one neighbor to be intact.
            left_break = pair_breaks[i - 1] if i > 0 else False
            right_break = pair_breaks[i] if i < n - 1 else False
            if n == 1:
                row_chain_ok = True
            elif i == 0:
                row_chain_ok = not right_break
            elif i == n - 1:
                row_chain_ok = not left_break
            else:
                row_chain_ok = not (left_break and right_break)
            row["chain_break_ratio"] = chain_break_ratio
            row["chain_ok"] = row_chain_ok
            row["enough_sane_strikes"] = enough_sane

    return [MarketFeature(**row) for row in raw]
