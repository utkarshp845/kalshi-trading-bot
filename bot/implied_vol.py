"""
Implied volatility back-out from Kalshi market prices.

Each KXBTC market's mid-price encodes the market's consensus probability.
Inverting the log-normal formula reveals the implied vol that market makers
are pricing in. Comparing this to realized vol (from Kraken OHLC) gives a
data-driven IV/RV ratio — used to replace the static VOL_SAFETY_MARGIN.

Key function:
  fit_cycle_iv(markets, spot, sigma_realized) → (iv_rv_ratio, per_market_sigmas)
"""
import math
import logging
import statistics
from typing import Optional

from scipy.special import ndtri  # inverse normal CDF — already installed via scipy

from bot.pricing import calc_prob
from bot.kalshi_client import Market

log = logging.getLogger(__name__)

# Bounds for valid implied vol results
_SIGMA_MIN = 0.10
_SIGMA_MAX = 8.00

# Markets with mid-price too close to 0 or 1 are unreliable for IV back-out
_MID_CLIP_LOW = 0.03
_MID_CLIP_HIGH = 0.97


def backout_sigma(
    S: float,
    K: float,
    T: float,
    market_mid: float,
) -> Optional[float]:
    """
    Invert the log-normal formula to recover the implied volatility
    that would price a binary contract at `market_mid`.

    P = Φ(ln(S/K) / (σ × √T))  ⟹  σ = ln(S/K) / (Φ⁻¹(p) × √T)

    Returns None if:
    - market_mid is at the extremes (ATM → d ≈ 0 → division by zero)
    - ln(S/K) and d_implied have opposite signs (nonsensical result)
    - the result is outside [SIGMA_MIN, SIGMA_MAX]
    - T <= 0

    Args:
        S:           Current BTC spot price
        K:           Strike price
        T:           Time to expiry in years
        market_mid:  (bid + ask) / 2 for the YES side

    Returns:
        Implied annualized volatility, or None if unreliable.
    """
    if T <= 0 or K <= 0 or S <= 0:
        return None

    # Reject extreme probabilities before clipping — IV is ill-conditioned there.
    # Deep ITM/OTM markets (mid near 0 or 1) have many (sigma, K) pairs that
    # produce the same extreme probability; you cannot recover sigma reliably.
    if not (_MID_CLIP_LOW <= market_mid <= _MID_CLIP_HIGH):
        return None

    try:
        d_implied = float(ndtri(market_mid))
    except Exception:
        return None

    if abs(d_implied) < 0.10:
        # Too close to ATM — result is highly sensitive to small price moves
        return None

    log_moneyness = math.log(S / K)

    # Sign check: if S > K (ITM), log_moneyness > 0 and we expect high mid (d > 0).
    # If the sign of d disagrees with log_moneyness, the market price is inconsistent.
    if log_moneyness * d_implied < 0:
        return None

    sigma = log_moneyness / (d_implied * math.sqrt(T))

    if not (_SIGMA_MIN <= sigma <= _SIGMA_MAX):
        return None

    # Sanity check: reproduce market_mid within a tight tolerance
    check_prob = calc_prob(S, K, T, sigma)
    if abs(check_prob - market_mid) > 0.02:
        log.debug("IV back-out sanity fail: S=%.0f K=%.0f T=%.4f σ=%.4f → p=%.4f ≠ mid=%.4f",
                  S, K, T, sigma, check_prob, market_mid)
        return None

    return sigma


def fit_cycle_iv(
    markets: list[Market],
    spot: float,
    sigma_realized: float,
    T_hours_by_ticker: dict[str, float],
    max_pct_spread: float = 0.30,
) -> tuple[Optional[float], dict[str, float]]:
    """
    Back out implied vol from each market's mid-price and compute the cycle-level
    IV/RV ratio.

    Markets with wide percentage spreads are excluded (unreliable mid).
    Markets are weighted by 1/spread (tighter spreads → more reliable).

    Args:
        markets:            List of open KXBTC markets
        spot:               Current BTC spot price
        sigma_realized:     Annualized realized vol (short window, unscaled)
        T_hours_by_ticker:  Dict mapping ticker → hours to expiry (pre-computed)
        max_pct_spread:     Skip markets where pct spread > this

    Returns:
        (iv_rv_ratio, per_market_sigmas):
          iv_rv_ratio       = weighted median(sigma_impl) / sigma_realized
                              None if fewer than 3 valid markets
          per_market_sigmas = {ticker: sigma_impl} for valid markets
    """
    from bot.strategy import _parse_strike  # local import to avoid circular

    per_market: dict[str, float] = {}
    weights: list[float] = []
    sigmas: list[float] = []

    for market in markets:
        strike = _parse_strike(market.ticker)
        if strike is None:
            continue

        T_hours = T_hours_by_ticker.get(market.ticker, 0.0)
        if T_hours < 0.5:
            continue  # too close to expiry — IV unreliable

        T_years = T_hours / 8760.0

        yes_mid = (market.yes_ask + market.yes_bid) / 2
        yes_spread = market.yes_ask - market.yes_bid
        pct_spread = yes_spread / yes_mid if yes_mid > 0.01 else 99.0

        if pct_spread > max_pct_spread:
            log.debug("IV skip %s: pct_spread %.0f%% too wide", market.ticker, pct_spread * 100)
            continue

        sigma_impl = backout_sigma(spot, strike, T_years, yes_mid)
        if sigma_impl is None:
            continue

        per_market[market.ticker] = sigma_impl
        weight = 1.0 / max(yes_spread, 0.01)
        weights.append(weight)
        sigmas.append(sigma_impl)

    if len(sigmas) < 3:
        log.debug("IV fit: only %d valid markets, need ≥3 — skipping", len(sigmas))
        return None, per_market

    # Weighted median: sort by sigma, pick the value at the weighted 50th percentile
    total_weight = sum(weights)
    sorted_pairs = sorted(zip(sigmas, weights), key=lambda x: x[0])
    cumulative = 0.0
    median_sigma = sorted_pairs[0][0]
    for s, w in sorted_pairs:
        cumulative += w
        if cumulative >= total_weight / 2:
            median_sigma = s
            break

    if sigma_realized <= 0:
        return None, per_market

    iv_rv_ratio = median_sigma / sigma_realized
    log.info(
        "IV fit: %d markets, median_sigma=%.4f, RV=%.4f, IV/RV=%.3f",
        len(sigmas), median_sigma, sigma_realized, iv_rv_ratio,
    )
    return iv_rv_ratio, per_market
