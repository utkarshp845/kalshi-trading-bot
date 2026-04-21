"""Portfolio-aware risk controls for multi-asset trading."""
from __future__ import annotations

import logging
import math
from collections import defaultdict
from typing import Optional

import bot.config as cfg
from bot.models import SignalDecision
from bot.risk import DailyRisk

log = logging.getLogger(__name__)

_SAME_ASSET_FACTOR = 0.60
_CROSS_ASSET_FACTOR = 0.85
_DEGRADED_CAP_MULTIPLIER = 0.50


class PortfolioRisk(DailyRisk):
    def __init__(self, *args, max_symbol_daily_spend_pct: float, max_symbol_positions: int, **kwargs):
        super().__init__(*args, **kwargs)
        self.max_symbol_daily_spend_pct = max_symbol_daily_spend_pct
        self.max_symbol_positions = max_symbol_positions
        self._symbol_spent: dict[str, float] = defaultdict(float)
        self._last_size_reason: str = ""

    def restore_symbol_spend(self, spent_by_symbol: dict[str, float]) -> None:
        self._symbol_spent = defaultdict(float, spent_by_symbol)
        self._last_size_reason = ""

    def reset(self) -> None:
        super().reset()
        self._symbol_spent = defaultdict(float)
        self._last_size_reason = ""

    def record_fill(self, cost: float, symbol: Optional[str] = None) -> None:
        super().record_fill(cost)
        if symbol:
            self._symbol_spent[symbol] += cost

    def symbol_spent(self, symbol: str) -> float:
        return float(self._symbol_spent.get(symbol, 0.0))

    @property
    def last_size_reason(self) -> str:
        return self._last_size_reason

    def can_trade_symbol(self, symbol: str, open_positions_by_symbol: dict[str, int]) -> bool:
        if not self.can_trade(sum(open_positions_by_symbol.values())):
            return False
        if open_positions_by_symbol.get(symbol, 0) >= self.max_symbol_positions:
            log.info("Risk gate: max open positions reached for %s (%d)", symbol, open_positions_by_symbol.get(symbol, 0))
            return False
        return True

    def size_order(
        self,
        signal: SignalDecision,
        current_balance: float = 0.0,
        open_positions_by_symbol: Optional[dict[str, int]] = None,
    ) -> int:
        open_positions_by_symbol = open_positions_by_symbol or {}
        remaining_daily = self._max_daily_spend - self._daily_spent
        if remaining_daily <= 0:
            self._last_size_reason = "daily_budget"
            return 0

        if current_balance > 0:
            balance_limit = self.bankroll_fraction * current_balance
            remaining_budget = min(remaining_daily, balance_limit)
        else:
            remaining_budget = remaining_daily

        symbol_cap = max(self.daily_spend_floor, self.max_symbol_daily_spend_pct * current_balance) if current_balance > 0 else self.daily_spend_floor
        if signal.degraded:
            symbol_cap *= _DEGRADED_CAP_MULTIPLIER
        remaining_symbol_budget = symbol_cap - self.symbol_spent(signal.symbol)
        remaining_budget = min(remaining_budget, remaining_symbol_budget)
        if remaining_budget <= 0:
            self._last_size_reason = "symbol_budget"
            return 0

        ask = signal.ask
        if ask <= 0 or ask >= 1.0:
            self._last_size_reason = "invalid_price"
            return 0

        kelly_f = signal.edge / (1.0 - ask)
        spend = kelly_f * self.kelly_fraction * remaining_budget
        spend *= self._slippage_factor
        spend *= self._drawdown_scale

        same_asset = open_positions_by_symbol.get(signal.symbol, 0)
        cross_asset = max(0, sum(open_positions_by_symbol.values()) - same_asset)
        if same_asset > 0:
            spend *= _SAME_ASSET_FACTOR ** same_asset
        if cross_asset > 0:
            spend *= _CROSS_ASSET_FACTOR ** cross_asset

        spend = min(spend, remaining_budget)
        contracts = math.floor(spend / ask)
        contracts = min(contracts, self.max_contracts_per_market)
        contracts = max(contracts, 0)
        if contracts <= 0:
            self._last_size_reason = "size_zero"
            return 0

        required_visible_size = cfg.LIQUIDITY_ENTRY_MULTIPLIER * contracts
        if signal.cumulative_size_at_entry < required_visible_size:
            self._last_size_reason = "thin_book"
            log.info(
                "Liquidity gate: %s visible_size=%.2f required=%.2f contracts=%d",
                signal.ticker,
                signal.cumulative_size_at_entry,
                required_visible_size,
                contracts,
            )
            return 0
        self._last_size_reason = ""
        return contracts
