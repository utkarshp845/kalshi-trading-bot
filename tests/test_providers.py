from bot.kalshi_client import Market
from bot.providers import fetch_markets_snapshot


def _market(ticker: str) -> Market:
    return Market(
        ticker=ticker,
        event_ticker="KXBTC",
        status="open",
        close_time="2026-04-26T20:00:00Z",
        yes_ask=0.45,
        no_ask=0.55,
        yes_bid=0.42,
        no_bid=0.52,
        last_price=0.44,
    )


class _KalshiWithOrderbookFailure:
    def get_open_markets(self, _series_ticker):
        return [
            _market("KXBTC-26APR4PM-B95000"),
            _market("KXBTC-26APR4PM-B96000"),
        ]

    def get_market_orderbooks(self, _tickers, depth=0):
        raise RuntimeError(f"batch orderbook endpoint unavailable (depth={depth})")


def test_fetch_markets_snapshot_tolerates_batch_orderbook_failure():
    result = fetch_markets_snapshot(
        kalshi=_KalshiWithOrderbookFailure(),
        symbol="BTC",
        series_ticker="KXBTC",
    )

    assert [m.ticker for m in result.markets] == [
        "KXBTC-26APR4PM-B95000",
        "KXBTC-26APR4PM-B96000",
    ]
    assert all(m.orderbook is None for m in result.markets)
    assert result.source.provider == "kalshi"
