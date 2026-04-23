"""Tests for bot/monitor.py."""
from types import SimpleNamespace

import bot.monitor as monitor


class _FakeResponse:
    def raise_for_status(self):
        return None


class TestAlertWebhook:
    def test_info_does_not_post_when_min_level_is_warning(self, monkeypatch):
        posts = []
        monkeypatch.setattr(monitor, "_last_sent_at", {})
        monkeypatch.setattr(monitor, "cfg", SimpleNamespace(
            ALERT_WEBHOOK_URL="https://discord.com/api/webhooks/test",
            ALERT_WEBHOOK_MIN_LEVEL="WARNING",
            ALERT_DEDUP_SECONDS=900,
        ))
        monkeypatch.setattr(monitor.requests, "post", lambda *args, **kwargs: posts.append((args, kwargs)) or _FakeResponse())

        monitor.alert("profit exit", level="INFO")

        assert posts == []

    def test_warning_posts_to_discord(self, monkeypatch):
        posts = []
        monkeypatch.setattr(monitor, "_last_sent_at", {})
        monkeypatch.setattr(monitor, "cfg", SimpleNamespace(
            ALERT_WEBHOOK_URL="https://discord.com/api/webhooks/test",
            ALERT_WEBHOOK_MIN_LEVEL="WARNING",
            ALERT_DEDUP_SECONDS=900,
        ))
        monkeypatch.setattr(monitor.requests, "post", lambda *args, **kwargs: posts.append((args, kwargs)) or _FakeResponse())

        monitor.alert("drawdown halt", level="WARNING")

        assert len(posts) == 1
        assert posts[0][1]["json"]["content"] == "**[kalshi-bot] WARNING:** drawdown halt"

    def test_duplicate_warning_is_deduplicated(self, monkeypatch):
        posts = []
        now = iter([1000.0, 1005.0, 1010.0, 1015.0])
        monkeypatch.setattr(monitor, "_last_sent_at", {})
        monkeypatch.setattr(monitor, "cfg", SimpleNamespace(
            ALERT_WEBHOOK_URL="https://discord.com/api/webhooks/test",
            ALERT_WEBHOOK_MIN_LEVEL="WARNING",
            ALERT_DEDUP_SECONDS=900,
        ))
        monkeypatch.setattr(monitor.time, "time", lambda: next(now))
        monkeypatch.setattr(monitor.requests, "post", lambda *args, **kwargs: posts.append((args, kwargs)) or _FakeResponse())

        monitor.alert("drawdown halt", level="WARNING")
        monitor.alert("drawdown halt", level="WARNING")

        assert len(posts) == 1
