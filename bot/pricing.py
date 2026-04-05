"""
Log-normal binary option pricer for Kalshi BTC daily price-level contracts.

Models: P(BTC_close > K) = Φ(d)
where  d = ln(S/K) / (σ * sqrt(T))
       S = current BTC spot price
       K = strike price
       T = time to expiry in years (hours_remaining / 8760)
       σ = annualized realized volatility

No drift term is included — appropriate for short intraday horizons where
the drift contribution is negligible compared to the volatility term.
"""
import math
import logging
from scipy.stats import norm

log = logging.getLogger(__name__)


def calc_prob(S: float, K: float, T: float, sigma: float) -> float:
    """
    Return the theoretical probability that BTC closes ABOVE strike K.

    Args:
        S:     Current BTC spot price (USD)
        K:     Strike price (USD)
        T:     Time to expiry as a fraction of a year (e.g. 4 hours → 4/8760)
        sigma: Annualized realized volatility (e.g. 0.65 = 65%)

    Returns:
        Probability in [0, 1] that BTC closes above K.
    """
    if T <= 0:
        # Already expired — deterministic outcome
        return 1.0 if S > K else 0.0

    if sigma <= 0:
        raise ValueError(f"sigma must be positive, got {sigma}")

    d = math.log(S / K) / (sigma * math.sqrt(T))
    prob = float(norm.cdf(d))
    log.debug("calc_prob S=%.0f K=%.0f T=%.6f σ=%.4f → d=%.4f → P=%.4f", S, K, T, sigma, d, prob)
    return prob
