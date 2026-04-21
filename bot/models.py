"""Shared data models for the multi-asset trading pipeline."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class SourceSnapshot:
    provider: str
    symbol: str
    fetched_at: str
    freshness_sec: float
    status: str
    payload_hash: str


@dataclass(frozen=True)
class AssetSnapshot:
    symbol: str
    series_ticker: str
    spot: float
    sigma_short: float
    sigma_long: float
    sigma_adjusted: float
    mu: float
    iv_rv_ratio: Optional[float]
    adaptive_margin: float
    spot_source: SourceSnapshot
    markets_source: SourceSnapshot
    iv_source: SourceSnapshot
    degraded: bool
    health_status: str
    open_positions: int = 0

    @property
    def tradeable(self) -> bool:
        return self.health_status == "healthy"


@dataclass(frozen=True)
class MarketFeature:
    symbol: str
    ticker: str
    close_time: str
    expiry_bucket: str
    strike: float
    side: str
    contract_theo_prob: float
    yes_theo_prob: float
    ask: float
    bid: float
    mid: float
    yes_bid: float
    yes_ask: float
    no_bid: float
    no_ask: float
    spread_abs: float
    spread_pct: float
    gross_edge: float
    edge: float
    fee: float
    hours_to_expiry: float
    distance_from_spot_sigma: float
    last_price_divergence: Optional[float]
    chain_break_ratio: float
    chain_ok: bool
    enough_sane_strikes: bool
    spread_ok: bool
    last_price_ok: bool
    top_of_book_size: float = 0.0
    resting_size_at_entry: float = 0.0
    cumulative_size_at_entry: float = 0.0
    expected_fill_price: Optional[float] = None
    depth_slippage: float = 0.0
    orderbook_imbalance: float = 0.0
    orderbook_available: bool = False


@dataclass(frozen=True)
class SignalDecision:
    symbol: str
    ticker: str
    side: str
    eligible: bool
    score: float
    required_edge: float
    expected_slippage: float
    uncertainty_penalty: float
    realized_edge_proxy: float
    reject_reason: str
    theo_prob: float
    ask: float
    bid: float
    mid_price: float
    gross_edge: float
    edge: float
    fee: float
    hours_to_expiry: float
    strike: float
    distance_from_spot_sigma: float
    degraded: bool
    chain_break_ratio: float
    top_of_book_size: float = 0.0
    resting_size_at_entry: float = 0.0
    cumulative_size_at_entry: float = 0.0
    expected_fill_price: Optional[float] = None
    depth_slippage: float = 0.0
    orderbook_imbalance: float = 0.0
    orderbook_available: bool = False

    @property
    def cost_estimate(self) -> float:
        return self.ask
