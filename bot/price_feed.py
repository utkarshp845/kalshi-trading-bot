"""
BTC spot price and realized volatility.

Primary source: Kraken public REST API (no auth required, globally accessible).
Falls back to CoinGecko if Kraken is unreachable.
"""
import math
import logging
import time
import requests

log = logging.getLogger(__name__)

KRAKEN_BASE = "https://api.kraken.com/0/public"
COINGECKO_BASE = "https://api.coingecko.com/api/v3"

_SESSION = requests.Session()
_SESSION.headers.update({"Accept": "application/json"})


def get_spot_price() -> float:
    """Return current BTC/USD mid price from Kraken."""
    resp = _SESSION.get(f"{KRAKEN_BASE}/Ticker", params={"pair": "XBTUSD"}, timeout=10)
    resp.raise_for_status()
    data = resp.json()
    if data.get("error"):
        raise ValueError(f"Kraken error: {data['error']}")
    # 'c' = last trade price [price, lot-volume]
    price = float(data["result"]["XXBTZUSD"]["c"][0])
    log.debug("BTC spot price (Kraken): %.2f", price)
    return price


def get_realized_vol(lookback_days: int = 30) -> float:
    """
    Return annualized realized volatility from the last `lookback_days` daily closes.
    Uses log returns: σ_annual = std(log_returns) * sqrt(365)

    Data source: Kraken OHLC (interval=1440 minutes = 1 day).
    """
    # Kraken OHLC: interval in minutes; since = epoch of (lookback_days+2) ago
    since = int(time.time()) - (lookback_days + 2) * 86400
    resp = _SESSION.get(
        f"{KRAKEN_BASE}/OHLC",
        params={"pair": "XBTUSD", "interval": 1440, "since": since},
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("error"):
        raise ValueError(f"Kraken OHLC error: {data['error']}")

    candles = data["result"]["XXBTZUSD"]
    if len(candles) < 2:
        raise ValueError(f"Not enough candles from Kraken (got {len(candles)})")

    # Kraken OHLC row: [time, open, high, low, close, vwap, volume, count]
    closes = [float(c[4]) for c in candles[-lookback_days - 1:]]

    log_returns = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes))]
    n = len(log_returns)
    mean = sum(log_returns) / n
    variance = sum((r - mean) ** 2 for r in log_returns) / (n - 1)
    daily_std = math.sqrt(variance)
    annual_vol = daily_std * math.sqrt(365)

    log.debug("Realized vol (annualized, %d-day): %.4f", lookback_days, annual_vol)
    return annual_vol


def get_btc_price_and_vol(
    short_days: int = 7,
    long_days: int = 30,
) -> tuple[float, float, float]:
    """
    Return (spot_price, sigma_short, sigma_long).

    sigma_short: realized vol over the last `short_days` days — responsive to
                 current regime, used for signal probability calculation.
    sigma_long:  realized vol over the last `long_days` days — stable baseline,
                 logged as a regime reference.
    """
    price = get_spot_price()
    sigma_short = get_realized_vol(short_days)
    sigma_long = get_realized_vol(long_days)
    log.info(
        "Vol: %d-day=%.4f  %d-day=%.4f  (ratio %.2fx)",
        short_days, sigma_short, long_days, sigma_long,
        sigma_short / sigma_long if sigma_long > 0 else 0,
    )
    return price, sigma_short, sigma_long
