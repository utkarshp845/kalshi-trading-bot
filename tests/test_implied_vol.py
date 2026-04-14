"""Tests for bot/implied_vol.py — IV back-out math."""
import math
import pytest
from bot.pricing import calc_prob
from bot.implied_vol import backout_sigma, fit_cycle_iv
from tests.conftest import make_market

S = 95000.0
SIGMA = 0.65

# Use a long enough T so near-ATM strikes have mid-prices in a usable range
# At T=8h, σ√T = 0.65 * sqrt(8/8760) ≈ 0.0196
# Strike at 94000: d = ln(95000/94000)/0.0196 ≈ 0.54, prob ≈ 0.705
# Strike at 96000: d = ln(95000/96000)/0.0196 ≈ -0.53, prob ≈ 0.298
T_HOURS = 8.0
T = T_HOURS / 8760.0


class TestBackoutSigma:
    def test_round_trip_near_atm(self):
        """IV back-out is reliable only for near-ATM strikes where mid ∈ (0.03, 0.97)."""
        K = 94000.0
        mid = calc_prob(S, K, T, SIGMA)
        assert 0.03 < mid < 0.97, f"Precondition: mid={mid:.4f} must be in (0.03, 0.97)"
        sigma_out = backout_sigma(S, K, T, mid)
        assert sigma_out is not None
        assert abs(sigma_out - SIGMA) < 0.01

    def test_round_trip_slightly_otm(self):
        K = 96500.0
        mid = calc_prob(S, K, T, SIGMA)
        assert 0.03 < mid < 0.97
        sigma_out = backout_sigma(S, K, T, mid)
        assert sigma_out is not None
        assert abs(sigma_out - SIGMA) < 0.01

    def test_deep_itm_returns_none(self):
        # Deep ITM (S >> K at short T): mid ≈ 1.0 → rejected before clipping
        K = 80000.0
        mid = calc_prob(S, K, T, SIGMA)
        result = backout_sigma(S, K, T, mid)
        # mid is essentially 1.0 (extreme) → should be None
        if mid >= 0.97:
            assert result is None

    def test_atm_returns_none(self):
        # ATM: d ≈ 0, division by zero territory
        K = S
        mid = 0.50
        result = backout_sigma(S, K, T, mid)
        assert result is None

    def test_extreme_mid_returns_none(self):
        # Mid outside [0.03, 0.97] should be rejected outright
        assert backout_sigma(S, 80000.0, T, 0.001) is None
        assert backout_sigma(S, 80000.0, T, 0.999) is None

    def test_returns_none_for_expired(self):
        assert backout_sigma(S, 80000.0, 0.0, 0.70) is None

    def test_returns_none_for_out_of_range_sigma(self):
        # Very short T → sigma would be astronomically large → clamped to None
        result = backout_sigma(S, 94900.0, 1e-10, 0.60)
        assert result is None  # sigma >> 8.0

    def test_sign_mismatch_returns_none(self):
        # S > K (ITM → should be high prob) but we pass mid=0.20 → inconsistent
        result = backout_sigma(S, 80000.0, T, 0.20)
        assert result is None


class TestFitCycleIv:
    def _make_markets_with_known_sigma(self, sigma=0.65):
        """Create near-ATM markets priced at a known sigma for round-trip testing."""
        markets = []
        T_by_ticker = {}
        # Use strikes near ATM so mid stays in the usable (0.03, 0.97) range
        T_years = T_HOURS / 8760.0
        # Generate strikes at ±1% to ±3% of spot
        offsets = [-0.025, -0.015, -0.005, 0.005, 0.015, 0.025]
        for off in offsets:
            K = round(S * (1 + off) / 1000) * 1000  # round to nearest 1000
            yes_mid = calc_prob(S, float(K), T_years, sigma)
            if not (0.05 < yes_mid < 0.95):
                continue
            half_spread = 0.01
            ticker = f"KXBTC-26APR4PM-B{int(K)}"
            m = make_market(
                ticker=ticker,
                yes_ask=min(0.99, yes_mid + half_spread),
                yes_bid=max(0.01, yes_mid - half_spread),
                no_ask=min(0.99, (1 - yes_mid) + half_spread),
                no_bid=max(0.01, (1 - yes_mid) - half_spread),
            )
            markets.append(m)
            T_by_ticker[ticker] = T_HOURS

        return markets, T_by_ticker

    def test_round_trip_iv_rv_ratio(self):
        markets, T_by_ticker = self._make_markets_with_known_sigma(SIGMA)
        assert len(markets) >= 3, "Need at least 3 near-ATM markets for this test"
        ratio, per_market = fit_cycle_iv(markets, S, SIGMA, T_by_ticker)
        assert ratio is not None
        # IV/RV should be ≈ 1.0 when markets are priced at the same sigma as realized
        assert 0.80 <= ratio <= 1.20

    def test_returns_none_with_too_few_markets(self):
        markets = [make_market()]  # only 1 market
        ratio, per_market = fit_cycle_iv(markets, S, SIGMA, {})
        assert ratio is None

    def test_per_market_dict_populated(self):
        markets, T_by_ticker = self._make_markets_with_known_sigma(SIGMA)
        _, per_market = fit_cycle_iv(markets, S, SIGMA, T_by_ticker)
        assert len(per_market) >= 3

    def test_zero_sigma_realized_returns_none(self):
        markets, T_by_ticker = self._make_markets_with_known_sigma(SIGMA)
        ratio, _ = fit_cycle_iv(markets, S, 0.0, T_by_ticker)
        assert ratio is None
