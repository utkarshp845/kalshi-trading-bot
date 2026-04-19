"""
Spot price, realized volatility, and trailing drift for BTC and ETH.

Primary source: Kraken public REST API (no auth required, globally accessible).
Each underlying has a Kraken pair name and a result key (Kraken renames symbols
internally — XBT for BTC, ETH for ETH — so we keep an explicit mapping).
"""
import math
import logging
import time
from dataclasses import dataclass
from typing import Optional

import requests

log = logging.getLogger(__name__)

KRAKEN_BASE = "https://api.kraken.com/0/public"

_SESSION = requests.Session()
_SESSION.headers.update({"Accept": "application/json"})


@dataclass(frozen=True)
class _Pair:
    """Kraken pair definition. `result_key` is the symbol Kraken returns in payloads."""
    name: str          # query value, e.g. "XBTUSD"
    result_key: str    # response key, e.g. "XXBTZUSD"


_PAIRS: dict[str, _Pair] = {
    "BTC": _Pair(name="XBTUSD", result_key="XXBTZUSD"),
    "ETH": _Pair(name="ETHUSD", result_key="XETHZUSD"),
}


def _pair_for(symbol: str) -> _Pair:
    sym = symbol.upper()
    if sym not in _PAIRS:
        raise ValueError(f"Unsupported symbol: {symbol!r}. Known: {list(_PAIRS)}")
    return _PAIRS[sym]


def get_spot_price(symbol: str = "BTC") -> float:
    """Return current spot price (USD) for the given asset symbol."""
    pair = _pair_for(symbol)
    resp = _SESSION.get(f"{KRAKEN_BASE}/Ticker", params={"pair": pair.name}, timeout=10)
    resp.raise_for_status()
    data = resp.json()
    if data.get("error"):
        raise ValueError(f"Kraken error: {data['error']}")
    price = float(data["result"][pair.result_key]["c"][0])
    log.debug("%s spot price (Kraken): %.2f", symbol, price)
    return price


def _fetch_daily_closes(symbol: str, lookback_days: int) -> list[float]:
    """Fetch daily closes from Kraken. Returns the most recent `lookback_days + 1` closes."""
    pair = _pair_for(symbol)
    since = int(time.time()) - (lookback_days + 2) * 86400
    resp = _SESSION.get(
        f"{KRAKEN_BASE}/OHLC",
        params={"pair": pair.name, "interval": 1440, "since": since},
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("error"):
        raise ValueError(f"Kraken OHLC error: {data['error']}")

    candles = data["result"][pair.result_key]
    if len(candles) < 2:
        raise ValueError(f"Not enough candles from Kraken for {symbol} (got {len(candles)})")

    # Kraken OHLC row: [time, open, high, low, close, vwap, volume, count]
    return [float(c[4]) for c in candles[-lookback_days - 1:]]


def get_realized_vol(lookback_days: int = 30, symbol: str = "BTC") -> float:
    """Return annualized realized volatility from the last `lookback_days` daily closes."""
    closes = _fetch_daily_closes(symbol, lookback_days)
    log_returns = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes))]
    n = len(log_returns)
    mean = sum(log_returns) / n
    variance = sum((r - mean) ** 2 for r in log_returns) / (n - 1)
    daily_std = math.sqrt(variance)
    annual_vol = daily_std * math.sqrt(365)
    log.debug("%s realized vol (annualized, %d-day): %.4f", symbol, lookback_days, annual_vol)
    return annual_vol


def get_trailing_drift(lookback_days: int = 30, symbol: str = "BTC") -> float:
    """
    Return the annualized trailing log-return — used as the drift term μ.

    drift_annualized = mean(daily_log_returns) * 365

    For a binary contract on a 4–16 hour horizon, this shifts the theoretical
    probability by a few percentage points away from the drift-free baseline.
    """
    closes = _fetch_daily_closes(symbol, lookback_days)
    log_returns = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes))]
    n = len(log_returns)
    mean_daily = sum(log_returns) / n
    annual_drift = mean_daily * 365
    log.debug(
        "%s trailing drift (%d-day annualized): %.4f", symbol, lookback_days, annual_drift,
    )
    return annual_drift


def get_price_vol_drift(
    short_days: int = 7,
    long_days: int = 30,
    drift_days: int = 30,
    symbol: str = "BTC",
) -> tuple[float, float, float, float]:
    """
    Return (spot_price, sigma_short, sigma_long, mu).

    sigma_short: realized vol over the last `short_days` days — responsive to
                 current regime, used for signal probability calculation.
    sigma_long:  realized vol over the last `long_days` days — stable baseline,
                 logged as a regime reference.
    mu:          annualized trailing log-return (drift) over `drift_days`.
    """
    price = get_spot_price(symbol)
    sigma_short = get_realized_vol(short_days, symbol)
    sigma_long = get_realized_vol(long_days, symbol)
    mu = get_trailing_drift(drift_days, symbol)
    log.info(
        "%s vol/drift: σ_%dd=%.4f  σ_%dd=%.4f  (ratio %.2fx)  μ_%dd=%+.4f",
        symbol, short_days, sigma_short, long_days, sigma_long,
        sigma_short / sigma_long if sigma_long > 0 else 0,
        drift_days, mu,
    )
    return price, sigma_short, sigma_long, mu


# ------------------------------------------------------------------
# Backwards-compatible wrappers (BTC-only, no drift)
# ------------------------------------------------------------------

def get_btc_price_and_vol(
    short_days: int = 7,
    long_days: int = 30,
) -> tuple[float, float, float]:
    """Backwards-compatible BTC-only fetch — returns (spot, sigma_short, sigma_long)."""
    price, sigma_short, sigma_long, _ = get_price_vol_drift(short_days, long_days, symbol="BTC")
    return price, sigma_short, sigma_long
