"""
Lightweight alerting module.

Sends structured alerts to a configured webhook URL (Slack, Discord, or any
generic HTTP POST endpoint). Falls back gracefully to log-only if not configured.

Usage:
    from bot.monitor import alert
    alert("Drawdown limit reached — halting trading", level="WARNING")
"""
import logging
import time

import requests
import bot.config as cfg

log = logging.getLogger(__name__)
_last_sent_at: dict[tuple[str, str], float] = {}


def _webhook_numeric_level(level: str) -> int:
    return getattr(logging, level.upper(), logging.ERROR)


def alert(message: str, level: str = "ERROR") -> None:
    """
    Emit an alert at the given log level and, if configured, POST to webhook.

    Args:
        message: Human-readable alert message
        level:   Log level string ("INFO", "WARNING", "ERROR", "CRITICAL")
    """
    numeric_level = getattr(logging, level.upper(), logging.ERROR)
    log.log(numeric_level, "ALERT: %s", message)

    if not cfg.ALERT_WEBHOOK_URL:
        return
    if numeric_level < _webhook_numeric_level(cfg.ALERT_WEBHOOK_MIN_LEVEL):
        return

    dedup_key = (level.upper(), message)
    now = time.time()
    last_sent = _last_sent_at.get(dedup_key)
    if last_sent is not None and (now - last_sent) < max(0, cfg.ALERT_DEDUP_SECONDS):
        log.debug("Suppressing duplicate webhook alert: %s", message)
        return

    # Discord uses "content"; Slack uses "text"
    url = cfg.ALERT_WEBHOOK_URL
    if "discord.com" in url:
        payload = {"content": f"**[kalshi-bot] {level}:** {message}"}
    else:
        payload = {"text": f"[kalshi-bot] {level}: {message}"}
    try:
        resp = requests.post(cfg.ALERT_WEBHOOK_URL, json=payload, timeout=5)
        resp.raise_for_status()
        _last_sent_at[dedup_key] = now
    except Exception as e:
        log.warning("Alert webhook delivery failed: %s", e)
