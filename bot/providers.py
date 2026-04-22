"""Provider wrappers that return typed source snapshots."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Optional

import bot.config as cfg
from bot.deribit_iv import get_atm_iv
from bot.kalshi_client import KalshiClient, Market
from bot.models import SourceSnapshot
from bot.price_feed import get_price_vol_drift


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _hash_payload(payload: object) -> str:
    raw = json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


@dataclass(frozen=True)
class PriceFeedResult:
    source: SourceSnapshot
    spot: float
    sigma_short: float
    sigma_long: float
    mu: float


@dataclass(frozen=True)
class DeribitIVResult:
    source: SourceSnapshot
    iv: Optional[float]


@dataclass(frozen=True)
class MarketsResult:
    source: SourceSnapshot
    markets: list[Market]


def fetch_price_snapshot(
    symbol: str,
    short_days: int,
    long_days: int,
    drift_days: int,
) -> PriceFeedResult:
    fetched_at = _now()
    spot, sigma_short, sigma_long, mu = get_price_vol_drift(
        short_days=short_days,
        long_days=long_days,
        drift_days=drift_days,
        symbol=symbol,
    )
    payload = {
        "spot": spot,
        "sigma_short": sigma_short,
        "sigma_long": sigma_long,
        "mu": mu,
    }
    return PriceFeedResult(
        source=SourceSnapshot(
            provider="kraken",
            symbol=symbol,
            fetched_at=fetched_at.isoformat(),
            freshness_sec=0.0,
            status="fresh",
            payload_hash=_hash_payload(payload),
        ),
        spot=spot,
        sigma_short=sigma_short,
        sigma_long=sigma_long,
        mu=mu,
    )


def fetch_deribit_iv_snapshot(symbol: str, spot: float, min_dte_hours: float) -> DeribitIVResult:
    fetched_at = _now()
    iv = get_atm_iv(symbol, spot, min_dte_hours=min_dte_hours)
    status = "fresh" if iv is not None else "unavailable"
    payload = {"iv": iv}
    return DeribitIVResult(
        source=SourceSnapshot(
            provider="deribit",
            symbol=symbol,
            fetched_at=fetched_at.isoformat(),
            freshness_sec=0.0,
            status=status,
            payload_hash=_hash_payload(payload),
        ),
        iv=iv,
    )


def fetch_markets_snapshot(kalshi: KalshiClient, symbol: str, series_ticker: str) -> MarketsResult:
    fetched_at = _now()
    markets = kalshi.get_open_markets(series_ticker)
    try:
        orderbooks = kalshi.get_market_orderbooks([m.ticker for m in markets], depth=cfg.ORDERBOOK_DEPTH)
    except Exception as e:
        # Orderbook depth enriches sizing and liquidity checks, but a transient
        # failure here should not make the entire underlying unusable.
        orderbooks = {}
        if markets:
            fallback_tickers = ",".join(m.ticker for m in markets[:3])
            if len(markets) > 3:
                fallback_tickers += ",..."
        else:
            fallback_tickers = "<none>"
        from logging import getLogger
        getLogger(__name__).warning(
            "Batch orderbook fetch failed for %s (%s): %s",
            symbol, fallback_tickers, e,
        )
    markets = [replace(m, orderbook=orderbooks.get(m.ticker)) for m in markets]
    payload = {
        "tickers": [m.ticker for m in markets],
        "n": len(markets),
        "orderbooks": len(orderbooks),
    }
    return MarketsResult(
        source=SourceSnapshot(
            provider="kalshi",
            symbol=symbol,
            fetched_at=fetched_at.isoformat(),
            freshness_sec=0.0,
            status="fresh",
            payload_hash=_hash_payload(payload),
        ),
        markets=markets,
    )
