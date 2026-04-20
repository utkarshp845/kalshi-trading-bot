"""Tests for multi-asset decision scoring."""
from bot.models import AssetSnapshot, MarketFeature, SourceSnapshot
from bot.strategy_engine import decide_signal


class _Store:
    def __init__(self, edge_leaks=None, slippages=None, errors=None, realized_edges=None):
        self.edge_leaks = edge_leaks or []
        self.slippages = slippages or []
        self.errors = errors or []
        self.realized_edges = realized_edges or []

    def get_recent_edge_leaks(self, symbol, n, before_iso=None):
        return self.edge_leaks[:n]

    def get_recent_positive_slippages(self, symbol, n, before_iso=None):
        return self.slippages[:n]

    def get_recent_realized_edges(self, symbol, n, before_iso=None):
        return self.realized_edges[:n]

    def get_recent_settled_abs_errors(self, symbol, n, before_iso=None):
        return self.errors[:n]


def _source() -> SourceSnapshot:
    return SourceSnapshot("test", "BTC", "2026-04-20T12:00:00+00:00", 0.0, "fresh", "hash")


def _asset() -> AssetSnapshot:
    source = _source()
    return AssetSnapshot(
        symbol="BTC",
        series_ticker="KXBTC",
        spot=95000.0,
        sigma_short=0.60,
        sigma_long=0.55,
        sigma_adjusted=0.70,
        mu=0.0,
        iv_rv_ratio=1.2,
        adaptive_margin=1.25,
        spot_source=source,
        markets_source=source,
        iv_source=source,
        degraded=False,
        health_status="healthy",
    )


def _feature(**kwargs) -> MarketFeature:
    defaults = dict(
        symbol="BTC",
        ticker="KXBTC-26APR4PM-B95000",
        close_time="2026-04-26T20:00:00Z",
        expiry_bucket="2026-04-26",
        strike=95000.0,
        side="yes",
        contract_theo_prob=0.67,
        yes_theo_prob=0.67,
        ask=0.45,
        bid=0.42,
        mid=0.435,
        yes_bid=0.42,
        yes_ask=0.45,
        no_bid=0.55,
        no_ask=0.58,
        spread_abs=0.03,
        spread_pct=0.068,
        gross_edge=0.22,
        edge=0.15,
        fee=0.07,
        hours_to_expiry=4.0,
        distance_from_spot_sigma=0.8,
        last_price_divergence=0.01,
        chain_break_ratio=0.0,
        chain_ok=True,
        enough_sane_strikes=True,
        spread_ok=True,
        last_price_ok=True,
    )
    defaults.update(kwargs)
    return MarketFeature(**defaults)


def test_decision_rejects_outside_probability_band():
    decision = decide_signal(_Store(), _asset(), _feature(contract_theo_prob=0.90), held_tickers=set())
    assert decision.eligible is False
    assert decision.reject_reason == "prob_band"


def test_decision_rejects_sigma_distance():
    decision = decide_signal(_Store(), _asset(), _feature(distance_from_spot_sigma=2.0), held_tickers=set())
    assert decision.eligible is False
    assert decision.reject_reason == "sigma_distance"


def test_decision_uses_dynamic_hurdle_from_recent_edge_leak():
    store = _Store(edge_leaks=[0.20, 0.30, 0.40, 0.50])
    decision = decide_signal(store, _asset(), _feature(edge=0.30), held_tickers=set())
    assert decision.required_edge > 0.30
    assert decision.reject_reason == "edge_below_hurdle"


def test_decision_uses_uncertainty_penalty_in_score():
    store = _Store(slippages=[0.10], errors=[0.15, 0.15, 0.15])
    decision = decide_signal(store, _asset(), _feature(edge=0.20), held_tickers=set())
    assert decision.score < 0
    assert decision.reject_reason == "score_non_positive"


def test_live_mode_requires_higher_cold_start_edge():
    decision = decide_signal(_Store(), _asset(), _feature(edge=0.26), held_tickers=set(), trading_mode="live")
    assert decision.eligible is False
    assert decision.reject_reason == "edge_below_hurdle"
    assert decision.required_edge >= 0.30


def test_live_mode_rejects_negative_recent_realized_edge():
    store = _Store(realized_edges=[-0.05] * 20, errors=[0.05] * 20)
    decision = decide_signal(store, _asset(), _feature(edge=0.40), held_tickers=set(), trading_mode="live")
    assert decision.eligible is False
    assert decision.reject_reason == "negative_recent_realized_edge"


def test_live_mode_rejects_degraded_asset():
    asset = _asset()
    asset = AssetSnapshot(
        symbol=asset.symbol,
        series_ticker=asset.series_ticker,
        spot=asset.spot,
        sigma_short=asset.sigma_short,
        sigma_long=asset.sigma_long,
        sigma_adjusted=asset.sigma_adjusted,
        mu=asset.mu,
        iv_rv_ratio=asset.iv_rv_ratio,
        adaptive_margin=asset.adaptive_margin,
        spot_source=asset.spot_source,
        markets_source=asset.markets_source,
        iv_source=asset.iv_source,
        degraded=True,
        health_status=asset.health_status,
    )
    decision = decide_signal(_Store(), asset, _feature(edge=0.40), held_tickers=set(), trading_mode="live")
    assert decision.eligible is False
    assert decision.reject_reason == "degraded_asset"
