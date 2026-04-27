import pytest

from bot.fees import fee_per_contract, kalshi_fee


def test_kalshi_taker_fee_uses_price_dependent_formula():
    assert kalshi_fee(price=0.50, contracts=100, rate=0.07) == pytest.approx(1.75)
    assert fee_per_contract(price=0.50, contracts=100, rate=0.07) == pytest.approx(0.0175)


def test_kalshi_fee_rounds_up_to_next_cent_for_small_orders():
    assert kalshi_fee(price=0.50, contracts=1, rate=0.07) == pytest.approx(0.02)
    assert kalshi_fee(price=0.90, contracts=1, rate=0.07) == pytest.approx(0.01)


def test_maker_fee_rate_is_supported():
    assert kalshi_fee(price=0.50, contracts=100, rate=0.0175) == pytest.approx(0.44)
