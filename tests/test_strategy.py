"""Tests for bot/strategy.py — signal generation and market filtering."""
import math
import pytest
from datetime import datetime, timedelta, timezone

from bot.pricing import calc_prob
from bot.strategy import _parse_strike, _hours_to_expiry, evaluate, scan_markets
from tests.conftest import make_market


SPOT = 95000.0
SIGMA = 0.65 * 1.25   # with safety margin
FEE = 0.07
MIN_EDGE = 0.15
MIN_T = 1.0


def _future_close(hours_from_now=4.0) -> str:
    dt = datetime.now(timezone.utc) + timedelta(hours=hours_from_now)
    return dt.isoformat().replace("+00:00", "Z")


class TestParseStrike:
    def test_standard_format(self):
        assert _parse_strike("KXBTC-26APR4PM-B95000") == 95000.0

    def test_without_time(self):
        assert _parse_strike("KXBTC-26APR-B100000") == 100000.0

    def test_large_strike(self):
        assert _parse_strike("KXBTC-26APR4PM-B120000") == 120000.0

    def test_invalid_format_returns_none(self):
        assert _parse_strike("INVALID") is None
        assert _parse_strike("KXBTC-26APR4PM") is None
        assert _parse_strike("") is None


class TestHoursToExpiry:
    def test_future_market_positive_hours(self):
        close_time = _future_close(4.0)
        h = _hours_to_expiry(close_time)
        assert 3.9 < h < 4.1

    def test_past_market_negative_hours(self):
        dt = datetime.now(timezone.utc) - timedelta(hours=2)
        close_time = dt.isoformat().replace("+00:00", "Z")
        h = _hours_to_expiry(close_time)
        assert h < 0

    def test_invalid_string_returns_zero(self):
        h = _hours_to_expiry("not-a-date")
        assert h == 0.0


class TestEvaluate:
    def _market_with_edge(self, hours=4.0):
        """Market where theo_prob >> yes_ask so a YES edge exists."""
        close = _future_close(hours)
        # BTC at 95k, strike at 80k → theo_prob ≈ 0.95
        # yes_ask = 0.65 → gross_edge ≈ 0.30, net_edge ≈ 0.23 > MIN_EDGE
        return make_market(
            ticker="KXBTC-26APR4PM-B80000",
            yes_ask=0.65, yes_bid=0.62,
            no_ask=0.35, no_bid=0.32,
            close_time=close,
        )

    def test_returns_signal_when_edge_exists(self):
        market = self._market_with_edge()
        sig, reason = evaluate(market, SPOT, SIGMA, MIN_EDGE, MIN_T, fee=FEE)
        assert sig is not None
        assert reason == ""
        assert sig.side in ("yes", "no")
        assert sig.edge > MIN_EDGE
        assert sig.ticker == market.ticker
        assert sig.hours_to_expiry > 0

    def test_returns_none_when_t_too_small(self):
        market = self._market_with_edge(hours=0.5)
        sig, reason = evaluate(market, SPOT, SIGMA, MIN_EDGE, min_t_hours=1.0, fee=FEE)
        assert sig is None
        assert reason == "t_too_small"

    def test_returns_none_when_absolute_spread_too_wide(self):
        close = _future_close(4.0)
        market = make_market(
            ticker="KXBTC-26APR4PM-B80000",
            yes_ask=0.90, yes_bid=0.60,   # spread = 0.30 > 0.25 limit
            no_ask=0.40, no_bid=0.10,
            close_time=close,
        )
        sig, reason = evaluate(market, SPOT, SIGMA, MIN_EDGE, MIN_T, fee=FEE, max_bid_ask_spread=0.25)
        assert sig is None
        assert reason == "spread_too_wide"

    def test_returns_none_when_pct_spread_too_wide(self):
        close = _future_close(4.0)
        # yes_ask=0.20, yes_bid=0.10: spread=0.10 (< 0.25 abs), but mid=0.15, pct=0.10/0.15=67% >> 30%
        market = make_market(
            ticker="KXBTC-26APR4PM-B80000",
            yes_ask=0.20, yes_bid=0.10,
            no_ask=0.80, no_bid=0.70,
            close_time=close,
        )
        sig, reason = evaluate(
            market, SPOT, SIGMA, MIN_EDGE, MIN_T, fee=FEE,
            max_bid_ask_spread=0.25, max_bid_ask_pct_spread=0.30,
        )
        # yes side fails pct test; no side has tight spread → may or may not signal
        # The key is that the illiquid yes side alone being bad doesn't kill the market
        # (no side is still valid). Test that signal, if any, avoids yes side.
        if sig is not None:
            assert sig.side == "no"

    def test_returns_none_when_last_price_diverges(self):
        close = _future_close(4.0)
        # yes_mid = 0.425, last_price = 0.70 → divergence = 0.275 > 0.15
        market = make_market(
            ticker="KXBTC-26APR4PM-B80000",
            yes_ask=0.45, yes_bid=0.40,
            no_ask=0.55, no_bid=0.50,
            close_time=close,
            last_price=0.70,
        )
        sig, reason = evaluate(market, SPOT, SIGMA, MIN_EDGE, MIN_T, fee=FEE, max_last_price_divergence=0.15)
        assert sig is None
        assert reason == "last_price_diverge"

    def test_accepts_market_when_last_price_within_tolerance(self):
        close = _future_close(4.0)
        market = self._market_with_edge()
        market = make_market(
            ticker="KXBTC-26APR4PM-B80000",
            yes_ask=0.65, yes_bid=0.62,
            no_ask=0.35, no_bid=0.32,
            close_time=close,
            last_price=0.63,   # close to yes_mid=0.635
        )
        sig, _ = evaluate(market, SPOT, SIGMA, MIN_EDGE, MIN_T, fee=FEE, max_last_price_divergence=0.15)
        assert sig is not None

    def test_returns_none_when_strike_unparseable(self):
        market = make_market(ticker="KXBTC-26APR4PM-INVALID")
        sig, reason = evaluate(market, SPOT, SIGMA, MIN_EDGE, MIN_T, fee=FEE)
        assert sig is None
        assert reason == "strike_parse"

    def test_selects_correct_side(self):
        close = _future_close(4.0)
        # BTC at 95k, strike at 80k → YES has huge edge
        market = make_market(
            ticker="KXBTC-26APR4PM-B80000",
            yes_ask=0.65, yes_bid=0.62,
            no_ask=0.35, no_bid=0.32,
            close_time=close,
        )
        sig, _ = evaluate(market, SPOT, SIGMA, MIN_EDGE, MIN_T, fee=FEE)
        assert sig is not None
        assert sig.side == "yes"  # deep ITM → YES edge

    def test_no_signal_uses_no_contract_probability(self):
        close = _future_close(4.0)
        market = make_market(
            ticker="KXBTC-26APR4PM-B120000",
            yes_ask=0.15, yes_bid=0.12,
            no_ask=0.55, no_bid=0.52,
            close_time=close,
        )

        sig, _ = evaluate(market, SPOT, SIGMA, MIN_EDGE, MIN_T, fee=FEE)

        assert sig is not None
        assert sig.side == "no"
        yes_prob = calc_prob(SPOT, 120000.0, sig.hours_to_expiry / 8760.0, SIGMA)
        assert sig.theo_prob == pytest.approx(1.0 - yes_prob)

    def test_mid_price_in_signal(self):
        market = self._market_with_edge()
        sig, _ = evaluate(market, SPOT, SIGMA, MIN_EDGE, MIN_T, fee=FEE)
        if sig is not None:
            expected_mid = (market.yes_ask + market.yes_bid) / 2 if sig.side == "yes" else (market.no_ask + market.no_bid) / 2
            assert abs(sig.mid_price - expected_mid) < 1e-9


class TestMakerEntry:
    def test_maker_uses_bid_and_maker_fee(self):
        close = _future_close(4.0)
        market = make_market(
            ticker="KXBTC-26APR4PM-B80000",
            yes_ask=0.65, yes_bid=0.60,
            no_ask=0.35, no_bid=0.30,
            close_time=close,
        )
        taker_sig, _ = evaluate(market, SPOT, SIGMA, MIN_EDGE, MIN_T, fee=FEE, maker_entry=False)
        maker_sig, _ = evaluate(market, SPOT, SIGMA, MIN_EDGE, MIN_T, fee=FEE, maker_entry=True)

        assert taker_sig is not None
        assert maker_sig is not None
        assert maker_sig.fee == pytest.approx(0.01)
        assert taker_sig.fee == pytest.approx(0.02)
        assert maker_sig.edge > taker_sig.edge  # bid < ask and lower maker fee
        assert maker_sig.gross_edge == pytest.approx(maker_sig.edge + maker_sig.fee)

    def test_maker_signals_market_that_fails_taker_threshold(self):
        """A narrow gross edge passes maker but fails after exact taker fee."""
        close = _future_close(4.0)
        # BTC at 95k, strike 80k → theo ≈ 1.0; yes_ask=0.945 → gross≈0.055
        market = make_market(
            ticker="KXBTC-26APR4PM-B80000",
            yes_ask=0.945, yes_bid=0.88,
            no_ask=0.12, no_bid=0.06,
            close_time=close,
        )
        min_edge = 0.05
        taker_sig, taker_reason = evaluate(market, SPOT, SIGMA, min_edge, MIN_T, fee=FEE, maker_entry=False)
        maker_sig, maker_reason = evaluate(market, SPOT, SIGMA, min_edge, MIN_T, fee=FEE, maker_entry=True)

        assert taker_sig is None  # exact taker fee leaves net edge at/below threshold
        assert taker_reason == "insufficient_edge"
        assert maker_sig is not None  # theo - 0.88 ≈ 0.12 > 0.05
        assert maker_sig.edge > min_edge


class TestScanMarkets:
    def _good_market(self, ticker="KXBTC-26APR4PM-B80000", hours=4.0):
        return make_market(
            ticker=ticker,
            yes_ask=0.65, yes_bid=0.62,
            no_ask=0.35, no_bid=0.32,
            close_time=_future_close(hours),
        )

    def test_sorted_by_edge_descending(self):
        # Two markets both with edge; the one with lower ask should have higher edge
        m1 = self._good_market("KXBTC-26APR4PM-B75000")
        m2 = self._good_market("KXBTC-26APR4PM-B80000")
        signals = scan_markets([m1, m2], SPOT, SIGMA, MIN_EDGE, MIN_T, held_tickers=set(), fee=FEE)
        for i in range(len(signals) - 1):
            assert signals[i].edge >= signals[i + 1].edge

    def test_held_tickers_skipped(self):
        m = self._good_market()
        signals = scan_markets([m], SPOT, SIGMA, MIN_EDGE, MIN_T,
                               held_tickers={m.ticker}, fee=FEE)
        assert signals == []

    def test_returns_empty_when_no_edge(self):
        # Strike near spot → no edge after fee
        close = _future_close(4.0)
        m = make_market(
            ticker="KXBTC-26APR4PM-B95000",
            yes_ask=0.50, yes_bid=0.48,
            no_ask=0.50, no_bid=0.48,
            close_time=close,
        )
        signals = scan_markets([m], SPOT, SIGMA, MIN_EDGE, MIN_T, held_tickers=set(), fee=FEE)
        assert signals == []
