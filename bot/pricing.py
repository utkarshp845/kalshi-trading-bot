"""
Log-normal binary option pricer for Kalshi BTC/ETH daily price-level contracts.

Base model: P(asset_close > K) = Φ(d) where d = ln(S/K) / (σ·√T).
This is the drift-free / median-anchored form (it implicitly assumes the
log-price has zero expected drift, equivalent to μ = σ²/2 in the strict
log-normal formula). It pairs cleanly with the implied-vol back-out used by
fit_cycle_iv and was the model's original behavior.

When `mu` is passed (annualized empirical drift), the formula becomes
    d = ln(S/K) / (σ·√T) + μ·√T / σ
which is a strict additive shift on top of the base. With μ = 0 the result
is bit-for-bit identical to the original drift-free model so existing tests
and the IV back-out remain consistent.
"""
import math
import logging
from scipy.stats import norm

log = logging.getLogger(__name__)


def calc_prob(S: float, K: float, T: float, sigma: float, mu: float = 0.0) -> float:
    """
    Return the theoretical probability that the asset closes ABOVE strike K.

    Args:
        S:     Current spot price (USD)
        K:     Strike price (USD)
        T:     Time to expiry as a fraction of a year (e.g. 4 hours → 4/8760)
        sigma: Annualized volatility (e.g. 0.65 = 65%)
        mu:    Annualized drift (e.g. 0.50 = +50% trailing annualized return).
               Default 0 reproduces the original drift-free model exactly.

    Returns:
        Probability in [0, 1] that the asset closes above K.
    """
    if T <= 0:
        return 1.0 if S > K else 0.0

    if sigma <= 0:
        raise ValueError(f"sigma must be positive, got {sigma}")

    sqrt_T = math.sqrt(T)
    d = math.log(S / K) / (sigma * sqrt_T)
    if mu != 0.0:
        d += mu * sqrt_T / sigma
    prob = float(norm.cdf(d))
    log.debug(
        "calc_prob S=%.0f K=%.0f T=%.6f σ=%.4f μ=%.4f → d=%.4f → P=%.4f",
        S, K, T, sigma, mu, d, prob,
    )
    return prob
