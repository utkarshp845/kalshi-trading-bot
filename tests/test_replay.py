"""Tests for persisted-cycle replay."""
from datetime import datetime, timezone

from bot.feature_builder import build_market_features
from bot.models import AssetSnapshot, SourceSnapshot
from bot.replay import replay
from bot.store import Store
from tests.conftest import make_market


def _source(symbol: str) -> SourceSnapshot:
    return SourceSnapshot("test", symbol, datetime.now(timezone.utc).isoformat(), 0.0, "fresh", "hash")


def _asset(symbol: str) -> AssetSnapshot:
    source = _source(symbol)
    return AssetSnapshot(
        symbol=symbol,
        series_ticker=f"KX{symbol}",
        spot=95000.0 if symbol == "BTC" else 3000.0,
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


def test_replay_reads_persisted_cycles(tmp_path):
    store = Store(db_path=tmp_path / "test.db", trades_csv_path=tmp_path / "trades.csv")
    store.open()
    try:
        cycle_id = datetime.now(timezone.utc).isoformat()
        btc_asset = _asset("BTC")
        eth_asset = _asset("ETH")
        store.log_asset_run(cycle_id, btc_asset)
        store.log_asset_run(cycle_id, eth_asset)

        btc_features = build_market_features(
            btc_asset,
            [
                make_market(ticker="KXBTC-26APR4PM-B93000", yes_ask=0.55, yes_bid=0.52, no_ask=0.48, no_bid=0.45),
                make_market(ticker="KXBTC-26APR4PM-B95000", yes_ask=0.47, yes_bid=0.44, no_ask=0.56, no_bid=0.53),
                make_market(ticker="KXBTC-26APR4PM-B97000", yes_ask=0.39, yes_bid=0.36, no_ask=0.64, no_bid=0.61),
                make_market(ticker="KXBTC-26APR4PM-B99000", yes_ask=0.31, yes_bid=0.28, no_ask=0.72, no_bid=0.69),
            ],
            fee=0.07,
        )
        eth_features = build_market_features(
            eth_asset,
            [
                make_market(ticker="KXETH-26APR4PM-B2800", yes_ask=0.88, yes_bid=0.85, no_ask=0.14, no_bid=0.11),
                make_market(ticker="KXETH-26APR4PM-B2900", yes_ask=0.84, yes_bid=0.81, no_ask=0.18, no_bid=0.15),
                make_market(ticker="KXETH-26APR4PM-B3000", yes_ask=0.80, yes_bid=0.77, no_ask=0.22, no_bid=0.19),
                make_market(ticker="KXETH-26APR4PM-B3100", yes_ask=0.76, yes_bid=0.73, no_ask=0.26, no_bid=0.23),
            ],
            fee=0.07,
        )
        for feature in btc_features + eth_features:
            store.log_market_snapshot(cycle_id, feature)

        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        output = replay(store, today, today, ["BTC", "ETH"])

        assert "BTC: decisions=" in output
        assert "ETH: decisions=" in output
        assert "COMBINED:" in output
    finally:
        store.close()
