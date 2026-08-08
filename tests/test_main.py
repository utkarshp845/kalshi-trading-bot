"""Tests for calibration behavior in bot/main.py."""
from dataclasses import dataclass

import bot.config as cfg
import bot.main as main_mod
from bot.kalshi_client import Order, Position
from bot.models import AssetSnapshot, SignalDecision, SourceSnapshot


def _decision(reject_reason: str, edge: float, ticker: str = "T") -> SignalDecision:
    """Minimal SignalDecision for near-miss selection tests; irrelevant fields
    get inert defaults."""
    return SignalDecision(
        symbol="BTC",
        ticker=ticker,
        side="yes",
        eligible=False,
        score=0.0,
        required_edge=0.025,
        expected_slippage=0.0,
        uncertainty_penalty=0.0,
        realized_edge_proxy=0.0,
        reject_reason=reject_reason,
        theo_prob=0.5,
        ask=0.5,
        bid=0.49,
        mid_price=0.495,
        gross_edge=edge,
        edge=edge,
        fee=0.0,
        hours_to_expiry=1.0,
        strike=0.0,
        distance_from_spot_sigma=0.0,
        degraded=False,
        chain_break_ratio=0.0,
    )


class TestSelectNearMisses:
    def test_prefers_threshold_rejects_over_structural_edge_inflated_ones(self):
        # A deep-ITM contract rejected on prob_band can carry edge close to 1.0
        # despite being nowhere near tradeable — it must not drown out a real
        # money-side near-miss with much smaller (but meaningful) edge.
        decisions = [
            _decision("prob_band", edge=0.9750, ticker="STRUCTURAL"),
            _decision("score_non_positive", edge=0.7141, ticker="THRESHOLD"),
        ]

        result = main_mod._select_near_misses(decisions)

        assert [d.ticker for d in result] == ["THRESHOLD"]

    def test_falls_back_to_structural_rejects_when_no_threshold_candidates(self):
        decisions = [
            _decision("prob_band", edge=0.99, ticker="A"),
            _decision("spread_too_wide", edge=0.10, ticker="B"),
        ]

        result = main_mod._select_near_misses(decisions)

        assert [d.ticker for d in result] == ["A", "B"]

    def test_excludes_already_held_from_fallback(self):
        decisions = [
            _decision("already_held", edge=0.99, ticker="HELD"),
            _decision("prob_band", edge=0.5, ticker="STRUCTURAL"),
        ]

        result = main_mod._select_near_misses(decisions)

        assert [d.ticker for d in result] == ["STRUCTURAL"]

    def test_respects_limit_and_sorts_descending_by_edge(self):
        decisions = [
            _decision("edge_below_hurdle", edge=0.01, ticker="LOW"),
            _decision("score_non_positive", edge=0.71, ticker="HIGH"),
            _decision("score_non_positive", edge=0.46, ticker="MID"),
        ]

        result = main_mod._select_near_misses(decisions, limit=2)

        assert [d.ticker for d in result] == ["HIGH", "MID"]


class _StoreWithBias:
    def __init__(self, bias):
        self.bias = bias

    def get_prob_calibration_bias(self, min_trades=10, lookback_days=30):
        return self.bias


class TestApplyCalibration:
    def test_positive_bias_does_not_mutate_vol_margin(self, monkeypatch):
        monkeypatch.setattr(cfg, "VOL_SAFETY_MARGIN", 1.25)
        main_mod._apply_calibration(_StoreWithBias(0.20))
        assert cfg.VOL_SAFETY_MARGIN == 1.25

    def test_negative_bias_does_not_mutate_vol_margin(self, monkeypatch):
        monkeypatch.setattr(cfg, "VOL_SAFETY_MARGIN", 1.25)
        main_mod._apply_calibration(_StoreWithBias(-0.20))
        assert cfg.VOL_SAFETY_MARGIN == 1.25


class _StoreForExits:
    def __init__(self):
        self.orders = []
        self.attempts = []

    def log_order(self, *args, **kwargs):
        self.orders.append((args, kwargs))

    def log_execution_attempt(self, **kwargs):
        self.attempts.append(kwargs)


@dataclass
class _KalshiForExits:
    market: object

    def get_market(self, _ticker):
        return self.market


def _asset() -> AssetSnapshot:
    source = SourceSnapshot("test", "BTC", "2026-04-20T12:00:00+00:00", 0.0, "fresh", "hash")
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


def test_check_exits_triggers_take_profit_path(monkeypatch):
    monkeypatch.setattr(cfg, "ENABLE_POSITION_EXIT", True)
    monkeypatch.setattr(cfg, "TAKE_PROFIT_TRIGGER", 1.5)
    monkeypatch.setattr(cfg, "TAKE_PROFIT_MIN_HOURS", 0.5)
    monkeypatch.setattr(cfg, "KALSHI_TAKER_FEE", 0.07)
    monkeypatch.setattr(main_mod, "calc_prob", lambda *args, **kwargs: 0.80)
    monkeypatch.setattr(
        main_mod,
        "_execute_passive_exit",
        lambda *args, **kwargs: [
            Order(
                order_id="exit-1",
                client_order_id=None,
                ticker="KXBTC-26APR4PM-B95000",
                side="yes",
                action="sell",
                status="filled",
                yes_price=0.45,
                no_price=0.55,
                count=2,
                fill_count=2,
                taker_fill_cost=0.90,
                created_time="2026-04-20T12:00:00Z",
            )
        ],
    )
    market = type(
        "Market",
        (),
        {
            "ticker": "KXBTC-26APR4PM-B95000",
            "close_time": "2099-04-26T20:00:00Z",
            "yes_bid": 0.40,
            "yes_ask": 0.45,
            "no_bid": 0.55,
            "no_ask": 0.58,
        },
    )()
    kalshi = _KalshiForExits(market=market)
    store = _StoreForExits()
    positions = [Position(ticker="KXBTC-26APR4PM-B95000", side="yes", quantity=2, cost=0.60)]

    exited = main_mod._check_exits(
        kalshi=kalshi,
        store=store,
        positions=positions,
        assets={"BTC": _asset()},
        trading_mode="live",
        cycle_id="2026-04-20T12:00:00+00:00",
    )

    assert exited == ["KXBTC-26APR4PM-B95000"]
    assert store.attempts[0]["reason"] == "take_profit"


class _StoreForBackfill:
    def __init__(self, tickers):
        self.tickers = tickers
        self.outcomes = []

    def get_unlabeled_market_tickers(self, before_iso=None, limit=100):
        return list(self.tickers)

    def upsert_market_outcome(self, **kwargs):
        self.outcomes.append(kwargs)


class _KalshiForBackfill:
    def __init__(self, settled, historical_only=frozenset()):
        self.settled = settled  # tickers that have a settlement available
        self.historical_only = historical_only  # tickers only reachable via /historical
        self.fetches = []

    def get_market_raw(self, ticker):
        self.fetches.append(("live", ticker))
        if ticker in self.historical_only:
            raise RuntimeError("404 not found")
        if ticker in self.settled:
            return {"result": "yes", "close_time": "2026-07-12T00:00:00Z", "settlement_ts": "2026-07-12T00:05:00Z"}
        return {"result": "", "settlement_value_dollars": None}

    def get_historical_market(self, ticker):
        self.fetches.append(("historical", ticker))
        if ticker in self.settled:
            return {"result": "yes", "close_time": "2026-07-12T00:00:00Z", "settlement_ts": "2026-07-12T00:05:00Z"}
        return {"result": "", "settlement_value_dollars": None}


class TestBackfillThrottle:
    def setup_method(self):
        main_mod._outcome_backfill_next_attempt.clear()

    def test_unsettled_tickers_not_refetched_until_retry_window(self):
        store = _StoreForBackfill(["T-UNSETTLED"])
        kalshi = _KalshiForBackfill(settled=set())

        main_mod._backfill_market_outcomes(kalshi, store, before_iso="2026-07-12T01:00:00+00:00")
        main_mod._backfill_market_outcomes(kalshi, store, before_iso="2026-07-12T01:01:00+00:00")

        assert kalshi.fetches == [("live", "T-UNSETTLED")]  # second cycle skips it
        assert store.outcomes == []

    def test_settled_ticker_upserted_and_throttle_entry_cleared(self):
        store = _StoreForBackfill(["T-SETTLED"])
        kalshi = _KalshiForBackfill(settled={"T-SETTLED"})

        main_mod._backfill_market_outcomes(kalshi, store, before_iso="2026-07-12T01:00:00+00:00")

        assert len(store.outcomes) == 1
        assert store.outcomes[0]["result"] == "yes"
        assert "T-SETTLED" not in main_mod._outcome_backfill_next_attempt

    def test_per_cycle_fetch_cap(self, monkeypatch):
        monkeypatch.setattr(cfg, "OUTCOME_BACKFILL_MAX_PER_CYCLE", 3)
        store = _StoreForBackfill([f"T-{i}" for i in range(10)])
        kalshi = _KalshiForBackfill(settled=set())

        main_mod._backfill_market_outcomes(kalshi, store, before_iso="2026-07-12T01:00:00+00:00")

        assert len(kalshi.fetches) == 3

    def test_settled_ticker_only_reachable_via_historical_fallback(self):
        # Regression: /historical/markets/{ticker} 404s for anything not yet
        # archived (confirmed against prod), so the live endpoint must be
        # tried first and the historical endpoint used as a fallback for
        # genuinely old/archived tickers — not the other way around.
        store = _StoreForBackfill(["T-OLD-SETTLED"])
        kalshi = _KalshiForBackfill(settled={"T-OLD-SETTLED"}, historical_only={"T-OLD-SETTLED"})

        main_mod._backfill_market_outcomes(kalshi, store, before_iso="2026-07-12T01:00:00+00:00")

        assert kalshi.fetches == [("live", "T-OLD-SETTLED"), ("historical", "T-OLD-SETTLED")]
        assert len(store.outcomes) == 1
        assert store.outcomes[0]["settlement_value"] == 1.0

    def test_typed_market_dataclass_would_drop_settlement_fields(self):
        # Regression for the real incident: kalshi.get_market(ticker).__dict__
        # (the typed Market dataclass) never carried result/
        # settlement_value_dollars/settlement_ts, so every backfill attempt
        # that fell through to it silently recorded nothing, forever. Assert
        # the dataclass still doesn't have these fields, so nobody
        # accidentally routes backfill through it again without noticing.
        from bot.kalshi_client import Market
        m = Market(
            ticker="X", event_ticker="X-EVT", status="finalized", close_time="2026-01-01T00:00:00Z",
            yes_ask=1.0, no_ask=0.0, yes_bid=1.0, no_bid=0.0, last_price=1.0,
        )
        assert "result" not in m.__dict__
        assert "settlement_value_dollars" not in m.__dict__

    def test_time_budget_stops_fetching(self, monkeypatch):
        monkeypatch.setattr(cfg, "OUTCOME_BACKFILL_TIME_BUDGET_SEC", -1.0)  # budget already exhausted
        store = _StoreForBackfill(["T-1", "T-2"])
        kalshi = _KalshiForBackfill(settled=set())

        main_mod._backfill_market_outcomes(kalshi, store, before_iso="2026-07-12T01:00:00+00:00")

        assert kalshi.fetches == []
