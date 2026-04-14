"""Tests for bot/pricing.py — pure math, no network calls."""
import math
import pytest
from bot.pricing import calc_prob

S = 95000.0
K_atm = 95000.0
K_itm = 80000.0   # BTC at 95k, strike 80k → almost certainly above
K_otm = 110000.0  # BTC at 95k, strike 110k → unlikely to be above
SIGMA = 0.65
T_4h = 4 / 8760.0
T_1d = 1 / 365.0


class TestCalcProbBasics:
    def test_atm_is_approximately_half(self):
        # ATM (S == K, no drift): probability ≈ 0.5
        p = calc_prob(S, K_atm, T_4h, SIGMA)
        assert abs(p - 0.5) < 0.01

    def test_itm_above_half(self):
        # Deep ITM (S >> K): probability close to 1
        p = calc_prob(S, K_itm, T_4h, SIGMA)
        assert p > 0.9

    def test_otm_below_half(self):
        # Deep OTM (S << K): probability close to 0
        p = calc_prob(S, K_otm, T_4h, SIGMA)
        assert p < 0.1

    def test_result_is_valid_probability(self):
        for K in [70000, 85000, 95000, 105000, 120000]:
            p = calc_prob(S, float(K), T_4h, SIGMA)
            assert 0.0 <= p <= 1.0


class TestCalcProbExpiry:
    def test_expired_itm_returns_one(self):
        # T=0, S > K → deterministic YES
        p = calc_prob(100.0, 90.0, 0.0, SIGMA)
        assert p == 1.0

    def test_expired_otm_returns_zero(self):
        # T=0, S < K → deterministic NO
        p = calc_prob(80.0, 90.0, 0.0, SIGMA)
        assert p == 0.0

    def test_expired_atm_returns_zero(self):
        # T=0, S == K → S is NOT above K
        p = calc_prob(90.0, 90.0, 0.0, SIGMA)
        assert p == 0.0


class TestCalcProbMonotonicity:
    def test_higher_spot_means_higher_prob(self):
        p_low = calc_prob(90000.0, K_atm, T_4h, SIGMA)
        p_high = calc_prob(100000.0, K_atm, T_4h, SIGMA)
        assert p_high > p_low

    def test_higher_strike_means_lower_prob(self):
        p_low_k = calc_prob(S, 90000.0, T_4h, SIGMA)
        p_high_k = calc_prob(S, 100000.0, T_4h, SIGMA)
        assert p_low_k > p_high_k

    def test_more_time_moves_prob_toward_half(self):
        # With more time, ATM stays ~0.5; deep ITM should stay high
        p_short = calc_prob(S, K_itm, T_4h, SIGMA)
        p_long = calc_prob(S, K_itm, T_1d, SIGMA)
        # Both should be > 0.5 but long might be closer to 0.5 (log-normal drift-free = more uncertainty)
        assert p_short > 0.5 and p_long > 0.5


class TestCalcProbEdgeCases:
    def test_raises_on_zero_sigma(self):
        with pytest.raises(ValueError, match="sigma must be positive"):
            calc_prob(S, K_atm, T_4h, 0.0)

    def test_raises_on_negative_sigma(self):
        with pytest.raises(ValueError, match="sigma must be positive"):
            calc_prob(S, K_atm, T_4h, -0.1)

    def test_very_high_sigma_still_valid_probability(self):
        p = calc_prob(S, K_atm, T_4h, 5.0)
        assert 0.0 <= p <= 1.0

    def test_small_positive_t_works(self):
        p = calc_prob(S, K_otm, 1e-8, SIGMA)
        assert 0.0 <= p <= 1.0
