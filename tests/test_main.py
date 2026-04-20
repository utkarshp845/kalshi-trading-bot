"""Tests for calibration behavior in bot/main.py."""
import bot.config as cfg
import bot.main as main_mod


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
