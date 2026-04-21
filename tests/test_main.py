"""Tests for calibration behavior in bot/main.py."""
from dataclasses import dataclass

import bot.config as cfg
import bot.main as main_mod
from bot.kalshi_client import Order, Position
from bot.models import AssetSnapshot, SourceSnapshot


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
            "close_time": "2026-04-26T20:00:00Z",
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
