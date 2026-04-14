"""
Lightweight alerting module.

Sends structured alerts to a configured webhook URL (Slack, Discord, or any
generic HTTP POST endpoint). Falls back gracefully to log-only if not configured.

Usage:
    from bot.monitor import alert
    alert("Drawdown limit reached — halting trading", level="WARNING")
"""
import logging
import requests
import bot.config as cfg

log = logging.getLogger(__name__)


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

    # Discord uses "content"; Slack uses "text"
    url = cfg.ALERT_WEBHOOK_URL
    if "discord.com" in url:
        payload = {"content": f"**[kalshi-bot] {level}:** {message}"}
    else:
        payload = {"text": f"[kalshi-bot] {level}: {message}"}
    try:
        resp = requests.post(cfg.ALERT_WEBHOOK_URL, json=payload, timeout=5)
        resp.raise_for_status()
    except Exception as e:
        log.warning("Alert webhook delivery failed: %s", e)
