"""Tests for multi-asset feature building."""
from dataclasses import dataclass

import pytest

from bot.feature_builder import build_asset_snapshot, build_market_features
from bot.models import SourceSnapshot
from tests.conftest import make_market


@dataclass(frozen=True)
class _PriceResult:
    source: SourceSnapshot
    spot: float
    sigma_short: float
    sigma_long: float
    mu: float


@dataclass(frozen=True)
class _IVResult:
    source: SourceSnapshot
    iv: float | None


@dataclass(frozen=True)
class _MarketsResult:
    source: SourceSnapshot
    markets: list


class _Store:
    def get_recent_iv_rv_ratios(self, n=10):
        return []


def _source(provider: str, symbol: str, freshness: float = 0.0, status: str = "fresh") -> SourceSnapshot:
    return SourceSnapshot(provider, symbol, "2026-04-20T12:00:00+00:00", freshness, status, "hash")


def test_asset_snapshot_marks_stale_required_source_as_unhealthy():
    asset = build_asset_snapshot(
        symbol="BTC",
        series_ticker="KXBTC",
        price_result=_PriceResult(_source("kraken", "BTC", freshness=30.0), 95000.0, 0.60, 0.55, 0.0),
        markets_result=_MarketsResult(_source("kalshi", "BTC"), []),
        iv_result=_IVResult(_source("deribit", "BTC", status="unavailable"), None),
        store=_Store(),
        open_positions=0,
    )

    assert asset.health_status == "stale_spot"
    assert asset.degraded is True
    assert asset.tradeable is False


def test_market_features_mark_chain_inconsistency_on_large_neighbor_jump():
    asset = build_asset_snapshot(
        symbol="BTC",
        series_ticker="KXBTC",
        price_result=_PriceResult(_source("kraken", "BTC"), 95000.0, 0.60, 0.55, 0.0),
        markets_result=_MarketsResult(_source("kalshi", "BTC"), []),
        iv_result=_IVResult(_source("deribit", "BTC"), 0.65),
        store=_Store(),
        open_positions=0,
    )
    markets = [
        make_market(ticker="KXBTC-26APR4PM-B90000", yes_ask=0.82, yes_bid=0.80, no_ask=0.20, no_bid=0.18),
        make_market(ticker="KXBTC-26APR4PM-B92000", yes_ask=0.70, yes_bid=0.68, no_ask=0.32, no_bid=0.30),
        make_market(ticker="KXBTC-26APR4PM-B94000", yes_ask=0.50, yes_bid=0.48, no_ask=0.52, no_bid=0.50),
        make_market(ticker="KXBTC-26APR4PM-B96000", yes_ask=0.10, yes_bid=0.08, no_ask=0.92, no_bid=0.90),
    ]

    features = build_market_features(asset, markets, fee=0.07)

    assert features
    assert all(feature.chain_ok is False for feature in features)

