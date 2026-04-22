"""Tests for multi-asset feature building."""
from dataclasses import dataclass
from typing import Optional

import pytest

from bot.feature_builder import build_asset_snapshot, build_market_features
from bot.kalshi_client import OrderbookSnapshot
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
    iv: Optional[float]


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


def test_market_features_include_orderbook_depth_metrics():
    asset = build_asset_snapshot(
        symbol="BTC",
        series_ticker="KXBTC",
        price_result=_PriceResult(_source("kraken", "BTC"), 95000.0, 0.60, 0.55, 0.0),
        markets_result=_MarketsResult(_source("kalshi", "BTC"), []),
        iv_result=_IVResult(_source("deribit", "BTC"), 0.65),
        store=_Store(),
        open_positions=0,
    )
    market = make_market(ticker="KXBTC-26APR4PM-B95000", yes_ask=0.45, yes_bid=0.42, no_ask=0.58, no_bid=0.55)
    market.orderbook = OrderbookSnapshot.from_dict(
        market.ticker,
        {
            "orderbook_fp": {
                "yes_dollars": [["0.4200", "30.00"]],
                "no_dollars": [["0.5500", "10.00"]],
            }
        },
    )

    feature = build_market_features(asset, [market], fee=0.07)[0]

    assert feature.orderbook_available is True
    assert feature.top_of_book_size == pytest.approx(10.0)
    assert feature.resting_size_at_entry == pytest.approx(10.0)
    assert feature.cumulative_size_at_entry == pytest.approx(10.0)
    assert feature.expected_fill_price == pytest.approx(0.45)
    assert feature.depth_slippage == pytest.approx(0.0)
    assert feature.orderbook_imbalance == pytest.approx(0.5)


def test_market_features_maker_entry_uses_bid_and_zero_fee():
    asset = build_asset_snapshot(
        symbol="BTC",
        series_ticker="KXBTC",
        price_result=_PriceResult(_source("kraken", "BTC"), 95000.0, 0.60, 0.55, 0.0),
        markets_result=_MarketsResult(_source("kalshi", "BTC"), []),
        iv_result=_IVResult(_source("deribit", "BTC"), 0.65),
        store=_Store(),
        open_positions=0,
    )
    market = make_market(ticker="KXBTC-26APR4PM-B95000", yes_ask=0.50, yes_bid=0.44, no_ask=0.52, no_bid=0.46)

    taker_feature = build_market_features(asset, [market], fee=0.07, maker_entry=False)[0]
    maker_feature = build_market_features(asset, [market], fee=0.07, maker_entry=True)[0]

    # Maker: fee=0, entry at bid
    assert maker_feature.fee == pytest.approx(0.0)
    assert maker_feature.edge > taker_feature.edge  # bid < ask, so maker edge is strictly higher
    assert maker_feature.gross_edge == pytest.approx(maker_feature.edge)  # no fee means gross==net

    # Taker: fee=0.07, entry at ask
    assert taker_feature.fee == pytest.approx(0.07)
    assert taker_feature.gross_edge == pytest.approx(taker_feature.edge + 0.07)
