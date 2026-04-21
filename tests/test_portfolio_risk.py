"""Tests for portfolio-aware sizing."""
from bot.models import SignalDecision
from bot.portfolio_risk import PortfolioRisk


def _decision(symbol="BTC", degraded=False) -> SignalDecision:
    return SignalDecision(
        symbol=symbol,
        ticker=f"KX{symbol}-26APR4PM-B95000",
        side="yes",
        eligible=True,
        score=0.10,
        required_edge=0.15,
        expected_slippage=0.01,
        uncertainty_penalty=0.01,
        realized_edge_proxy=0.23,
        reject_reason="",
        theo_prob=0.67,
        ask=0.30,
        bid=0.28,
        mid_price=0.29,
        gross_edge=0.30,
        edge=0.25,
        fee=0.07,
        hours_to_expiry=4.0,
        strike=95000.0,
        distance_from_spot_sigma=0.5,
        degraded=degraded,
        chain_break_ratio=0.0,
        cumulative_size_at_entry=500.0,
        top_of_book_size=250.0,
        resting_size_at_entry=250.0,
        expected_fill_price=0.30,
        depth_slippage=0.0,
        orderbook_available=True,
    )


def _risk() -> PortfolioRisk:
    risk = PortfolioRisk(
        daily_spend_pct=0.10,
        daily_spend_floor=10.0,
        max_contracts_per_market=100,
        max_positions=3,
        max_symbol_daily_spend_pct=0.05,
        max_symbol_positions=1,
        kelly_fraction=1.0,
        max_drawdown_pct=0.20,
        bankroll_fraction=0.25,
    )
    risk.set_session_balance(1000.0)
    return risk


def test_same_asset_discount_is_stronger_than_cross_asset_discount():
    risk = _risk()
    baseline = risk.size_order(_decision("BTC"), current_balance=1000.0, open_positions_by_symbol={})
    same_asset = risk.size_order(_decision("BTC"), current_balance=1000.0, open_positions_by_symbol={"BTC": 1})
    cross_asset = risk.size_order(_decision("BTC"), current_balance=1000.0, open_positions_by_symbol={"ETH": 1})

    assert baseline > cross_asset > same_asset


def test_degraded_symbol_cap_reduces_size():
    risk = _risk()
    normal = risk.size_order(_decision("BTC", degraded=False), current_balance=1000.0, open_positions_by_symbol={})
    degraded = risk.size_order(_decision("BTC", degraded=True), current_balance=1000.0, open_positions_by_symbol={})

    assert degraded < normal


def test_symbol_position_cap_blocks_new_trade():
    risk = _risk()
    assert risk.can_trade_symbol("BTC", {"BTC": 1}) is False
