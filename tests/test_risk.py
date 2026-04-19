"""Tests for bot/risk.py — sizing and risk gates."""
import pytest
from bot.risk import DailyRisk
from tests.conftest import make_signal


def make_risk(**kwargs) -> DailyRisk:
    defaults = dict(
        daily_spend_pct=0.10,
        daily_spend_floor=10.0,
        max_contracts_per_market=3,
        max_positions=2,
        kelly_fraction=0.10,
        max_drawdown_pct=0.20,
        bankroll_fraction=0.25,
    )
    defaults.update(kwargs)
    return DailyRisk(**defaults)


class TestCanTrade:
    def test_allowed_when_within_limits(self):
        risk = make_risk()
        assert risk.can_trade(open_positions=0) is True

    def test_blocked_at_max_positions(self):
        risk = make_risk(max_positions=2)
        assert risk.can_trade(open_positions=2) is False

    def test_blocked_when_daily_spend_reached(self):
        risk = make_risk(daily_spend_floor=5.0)
        risk._daily_spent = 5.0
        assert risk.can_trade(open_positions=0) is False

    def test_blocked_when_drawdown_halt(self):
        risk = make_risk()
        risk._drawdown_halt = True
        assert risk.can_trade(open_positions=0) is False


class TestCheckDrawdown:
    def test_no_halt_when_balance_stable(self):
        risk = make_risk(max_drawdown_pct=0.20)
        risk.set_session_balance(100.0)
        halted = risk.check_drawdown(85.0)  # -15%, within limit
        assert halted is False
        assert risk.drawdown_halted is False

    def test_halt_triggered_at_limit(self):
        risk = make_risk(max_drawdown_pct=0.20)
        risk.set_session_balance(100.0)
        halted = risk.check_drawdown(79.0)  # -21%, exceeds 20% limit
        assert halted is True
        assert risk.drawdown_halted is True

    def test_no_halt_before_session_balance_set(self):
        risk = make_risk(max_drawdown_pct=0.20)
        # session_start_balance not set → always returns False
        assert risk.check_drawdown(0.01) is False


class TestSizeOrder:
    def _sig(self, edge=0.20, price=0.45):
        return make_signal(edge=edge, price=price)

    def test_returns_positive_contracts(self):
        risk = make_risk()
        n = risk.size_order(self._sig(), current_balance=50.0, open_positions=0)
        assert n >= 0

    def test_returns_zero_for_ask_at_one(self):
        risk = make_risk()
        sig = make_signal(edge=0.20, price=1.0)
        assert risk.size_order(sig, current_balance=50.0) == 0

    def test_returns_zero_for_ask_at_zero(self):
        risk = make_risk()
        sig = make_signal(edge=0.20, price=0.0)
        assert risk.size_order(sig, current_balance=50.0) == 0

    def test_capped_at_max_contracts_per_market(self):
        risk = make_risk(max_contracts_per_market=2, daily_spend_floor=1000.0, kelly_fraction=1.0)
        n = risk.size_order(self._sig(edge=0.80, price=0.10), current_balance=10000.0)
        assert n <= 2

    def test_correlation_discount_reduces_size(self):
        risk = make_risk(daily_spend_floor=1000.0, kelly_fraction=0.50, max_contracts_per_market=1000)
        sig = self._sig(edge=0.25, price=0.30)
        n_no_pos = risk.size_order(sig, current_balance=500.0, open_positions=0)
        n_one_pos = risk.size_order(sig, current_balance=500.0, open_positions=1)
        # With one open position the 0.7^1 discount should reduce size
        assert n_one_pos < n_no_pos

    def test_returns_zero_when_budget_exhausted(self):
        risk = make_risk(daily_spend_floor=5.0)
        risk._daily_spent = 5.0
        assert risk.size_order(self._sig(), current_balance=50.0) == 0

    def test_balance_aware_limits_above_daily_cap(self):
        # balance=4.0, bankroll_fraction=0.25 → balance limit = 1.0
        # daily cap floor=10.0, so effective = min(10.0, 1.0) = 1.0
        risk = make_risk(daily_spend_floor=10.0, bankroll_fraction=0.25, max_contracts_per_market=100)
        sig = make_signal(edge=0.30, price=0.10)
        n = risk.size_order(sig, current_balance=4.0, open_positions=0)
        # Effective budget = 1.0, kelly_f ≈ 0.30/0.90 ≈ 0.33, spend ≈ 0.33 * 0.10 * 1.0 = 0.033
        # contracts = floor(0.033 / 0.10) = 0
        assert n == 0  # too small to round to even 1 contract


class TestReset:
    def test_reset_clears_state(self):
        risk = make_risk()
        risk._daily_spent = 5.0
        risk._positions_opened = 3
        risk._drawdown_halt = True
        risk.reset()
        assert risk.daily_spent == 0.0
        assert risk.positions_opened == 0
        assert risk.drawdown_halted is False


class TestGraduatedDrawdown:
    def _sig(self, edge=0.30, price=0.30):
        return make_signal(edge=edge, price=price)

    def test_no_scale_below_tier_1(self):
        risk = make_risk()
        risk.set_session_balance(100.0)
        risk.check_drawdown(95.0)  # -5%
        assert risk.drawdown_scale == 1.0

    def test_tier_1_scale_at_10_percent(self):
        risk = make_risk()
        risk.set_session_balance(100.0)
        risk.check_drawdown(89.0)  # -11%
        assert risk.drawdown_scale == 0.50
        assert risk.drawdown_halted is False

    def test_tier_2_scale_at_15_percent(self):
        risk = make_risk()
        risk.set_session_balance(100.0)
        risk.check_drawdown(84.0)  # -16%
        assert risk.drawdown_scale == 0.25
        assert risk.drawdown_halted is False

    def test_hard_halt_zeros_scale(self):
        risk = make_risk()
        risk.set_session_balance(100.0)
        risk.check_drawdown(79.0)  # -21%
        assert risk.drawdown_halted is True
        assert risk.drawdown_scale == 0.0

    def test_drawdown_scale_reduces_size_order(self):
        # Large budget so Kelly-based sizing dominates
        risk = make_risk(daily_spend_floor=1000.0, kelly_fraction=1.0, max_contracts_per_market=1000)
        risk.set_session_balance(1000.0)
        sig = self._sig(edge=0.30, price=0.30)

        # Baseline
        baseline = risk.size_order(sig, current_balance=1000.0, open_positions=0)

        # Tier 1 (10%): scale 0.5 → roughly half the contracts
        risk2 = make_risk(daily_spend_floor=1000.0, kelly_fraction=1.0, max_contracts_per_market=1000)
        risk2.set_session_balance(1000.0)
        risk2.check_drawdown(900.0)  # -10%
        reduced = risk2.size_order(sig, current_balance=900.0, open_positions=0)

        assert baseline > 0
        assert reduced < baseline

    def test_reset_restores_scale(self):
        risk = make_risk()
        risk.set_session_balance(100.0)
        risk.check_drawdown(84.0)
        assert risk.drawdown_scale == 0.25
        risk.reset()
        assert risk.drawdown_scale == 1.0


class TestSlippageFactor:
    def _sig(self, edge=0.30, price=0.30):
        return make_signal(edge=edge, price=price)

    def test_default_factor_is_one(self):
        risk = make_risk()
        assert risk.slippage_factor == 1.0

    def test_set_factor_clamps_above_one(self):
        risk = make_risk()
        risk.set_slippage_factor(1.5)
        assert risk.slippage_factor == 1.0

    def test_set_factor_clamps_below_floor(self):
        risk = make_risk()
        risk.set_slippage_factor(0.1)
        assert risk.slippage_factor == 0.3

    def test_none_resets_factor(self):
        risk = make_risk()
        risk.set_slippage_factor(0.5)
        risk.set_slippage_factor(None)
        assert risk.slippage_factor == 1.0

    def test_slippage_factor_reduces_size_order(self):
        risk = make_risk(daily_spend_floor=1000.0, kelly_fraction=1.0, max_contracts_per_market=1000)
        sig = self._sig(edge=0.30, price=0.30)

        baseline = risk.size_order(sig, current_balance=1000.0, open_positions=0)
        risk.set_slippage_factor(0.5)
        reduced = risk.size_order(sig, current_balance=1000.0, open_positions=0)

        assert baseline > 0
        assert reduced < baseline
