"""Portfolio-aware risk controls for multi-asset trading."""
from __future__ import annotations

import logging
import math
from collections import defaultdict
from typing import Optional

import bot.config as cfg
from bot.fees import kalshi_fee
from bot.models import SignalDecision
from bot.risk import DailyRisk

log = logging.getLogger(__name__)

_SAME_ASSET_FACTOR = 0.60
_CROSS_ASSET_FACTOR = 0.85
_DEGRADED_CAP_MULTIPLIER = 0.50
_MIN_LOG_GROWTH = 1e-9


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

        entry_price = self._entry_price(signal)
        if entry_price <= 0 or entry_price >= 1.0:
            self._last_size_reason = "invalid_price"
            return 0

        kelly_f = signal.edge / (1.0 - entry_price)
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
        kelly_contract_ceiling = max(0, math.floor(spend / entry_price))
        max_contracts = min(kelly_contract_ceiling, self.max_contracts_per_market)
        if max_contracts <= 0:
            self._last_size_reason = "size_zero"
            return 0

        if signal.cumulative_size_at_entry > 0:
            max_by_liquidity = int(signal.cumulative_size_at_entry / cfg.LIQUIDITY_ENTRY_MULTIPLIER)
            if max_by_liquidity < max_contracts:
                log.info(
                    "Liquidity gate: %s capping %d→%d contracts (visible=%.0f multiplier=%.1f)",
                    signal.ticker, max_contracts, max_by_liquidity,
                    signal.cumulative_size_at_entry, cfg.LIQUIDITY_ENTRY_MULTIPLIER,
                )
                max_contracts = max_by_liquidity
        if max_contracts <= 0:
            self._last_size_reason = "thin_book"
            return 0

        budget_contract_ceiling = self._max_affordable_contracts(
            entry_price=entry_price,
            remaining_budget=remaining_budget,
            max_contracts=max_contracts,
            fee_rate=self._entry_fee_rate(signal, entry_price),
        )
        if budget_contract_ceiling <= 0:
            self._last_size_reason = "budget_after_fees"
            return 0

        contracts = self._best_log_growth_size(
            signal=signal,
            entry_price=entry_price,
            current_balance=current_balance,
            max_contracts=budget_contract_ceiling,
            remaining_budget=remaining_budget,
        )
        if contracts <= 0:
            self._last_size_reason = "non_positive_growth"
            return 0
        self._last_size_reason = ""
        return contracts

    def _entry_price(self, signal: SignalDecision) -> float:
        # gross_edge = theo_prob - modeled entry price
        modeled_entry = signal.theo_prob - signal.gross_edge
        if 0 < modeled_entry < 1:
            return modeled_entry
        return signal.bid if cfg.ENABLE_MAKER_ORDERS else signal.ask

    def _entry_fee_rate(self, signal: SignalDecision, entry_price: float) -> float:
        if cfg.ENABLE_MAKER_ORDERS and abs(entry_price - signal.bid) <= abs(entry_price - signal.ask):
            return cfg.KALSHI_MAKER_FEE
        return cfg.KALSHI_TAKER_FEE

    def _total_entry_cost(self, entry_price: float, contracts: int, fee_rate: float) -> float:
        return entry_price * contracts + kalshi_fee(entry_price, contracts, fee_rate)

    def _max_affordable_contracts(
        self,
        entry_price: float,
        remaining_budget: float,
        max_contracts: int,
        fee_rate: float,
    ) -> int:
        affordable = 0
        for contracts in range(1, max_contracts + 1):
            if self._total_entry_cost(entry_price, contracts, fee_rate) <= remaining_budget + 1e-9:
                affordable = contracts
            else:
                break
        return affordable

    def _best_log_growth_size(
        self,
        signal: SignalDecision,
        entry_price: float,
        current_balance: float,
        max_contracts: int,
        remaining_budget: float,
    ) -> int:
        bankroll = current_balance if current_balance > 0 else max(remaining_budget, self._max_daily_spend)
        if bankroll <= 0:
            return 0

        p = max(0.0, min(1.0, signal.theo_prob))
        fee_rate = self._entry_fee_rate(signal, entry_price)
        best_contracts = 0
        best_growth = 0.0
        for contracts in range(1, max_contracts + 1):
            cost = self._total_entry_cost(entry_price, contracts, fee_rate)
            if cost > remaining_budget + 1e-9 or cost >= bankroll:
                break
            win_bankroll = bankroll - cost + contracts
            lose_bankroll = bankroll - cost
            if win_bankroll <= 0 or lose_bankroll <= 0:
                continue
            growth = p * math.log(win_bankroll / bankroll) + (1.0 - p) * math.log(lose_bankroll / bankroll)
            if growth > best_growth + _MIN_LOG_GROWTH:
                best_growth = growth
                best_contracts = contracts
        return best_contracts
