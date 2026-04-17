"""
Position sizing and daily risk controls.

Sizing model: fractional Kelly for binary contracts with balance awareness
  kelly_f   = edge / (1 - ask_price)    [since Kalshi pays $1 per contract]
  spend     = kelly_f * KELLY_FRACTION * effective_budget
  contracts = floor(spend / ask_price)

Balance-aware: effective_budget = min(remaining_daily_cap, bankroll_fraction * actual_balance)
Correlation discount: each additional open position reduces sizing by 30%
Slippage factor: scales spend by empirical realized/predicted edge ratio (≤ 1.0)
Drawdown guard:
  * Graduated scaling — position size is scaled down at drawdown tiers (e.g. 10%, 15%)
  * Hard halt — stops trading entirely when drawdown reaches MAX_DRAWDOWN_PCT
"""
import logging
import math
from typing import Optional

from bot.strategy import Signal

log = logging.getLogger(__name__)


class DailyRisk:
    """
    Tracks spending and position count for the current trading day.
    Call reset() at the start of each new calendar day.
    """

    def __init__(
        self,
        max_daily_spend: float,
        max_contracts_per_market: int,
        max_positions: int,
        kelly_fraction: float,
        max_drawdown_pct: float = 0.20,
        bankroll_fraction: float = 0.25,
        drawdown_tier_1_pct: float = 0.10,
        drawdown_tier_1_scale: float = 0.50,
        drawdown_tier_2_pct: float = 0.15,
        drawdown_tier_2_scale: float = 0.25,
    ):
        self.max_daily_spend = max_daily_spend
        self.max_contracts_per_market = max_contracts_per_market
        self.max_positions = max_positions
        self.kelly_fraction = kelly_fraction
        self.max_drawdown_pct = max_drawdown_pct
        self.bankroll_fraction = bankroll_fraction
        self.drawdown_tier_1_pct = drawdown_tier_1_pct
        self.drawdown_tier_1_scale = drawdown_tier_1_scale
        self.drawdown_tier_2_pct = drawdown_tier_2_pct
        self.drawdown_tier_2_scale = drawdown_tier_2_scale

        self._daily_spent: float = 0.0
        self._positions_opened: int = 0
        self._session_start_balance: float = 0.0
        self._drawdown_halt: bool = False
        self._drawdown_scale: float = 1.0
        self._slippage_factor: float = 1.0

    # ------------------------------------------------------------------
    # State management
    # ------------------------------------------------------------------

    def set_session_balance(self, balance: float) -> None:
        """Set the starting balance for drawdown tracking."""
        if self._session_start_balance <= 0:
            self._session_start_balance = balance
            log.info("Session start balance set: $%.2f", balance)

    def reset(self) -> None:
        """Call at the start of each new trading day."""
        log.info("Daily risk counters reset (spent=%.2f, positions=%d)", self._daily_spent, self._positions_opened)
        self._daily_spent = 0.0
        self._positions_opened = 0
        self._drawdown_halt = False
        self._drawdown_scale = 1.0

    def set_slippage_factor(self, factor: Optional[float]) -> None:
        """
        Set empirical Kelly slippage multiplier. None resets to 1.0 (no adjustment).
        Clamped to [0.3, 1.0] — we never boost sizing above Kelly, and we floor
        the factor so a small bad patch doesn't silence the bot entirely.
        """
        if factor is None:
            if self._slippage_factor != 1.0:
                log.info("Slippage factor cleared (was %.2f)", self._slippage_factor)
            self._slippage_factor = 1.0
            return
        clamped = max(0.3, min(1.0, float(factor)))
        if abs(clamped - self._slippage_factor) > 1e-6:
            log.info("Slippage factor: %.2f → %.2f", self._slippage_factor, clamped)
        self._slippage_factor = clamped

    def record_fill(self, cost: float) -> None:
        """Record a completed order fill."""
        self._daily_spent += cost
        self._positions_opened += 1
        log.info("Fill recorded: cost=%.2f  day_total=%.2f  positions=%d",
                 cost, self._daily_spent, self._positions_opened)

    @property
    def daily_spent(self) -> float:
        return self._daily_spent

    @property
    def positions_opened(self) -> float:
        return self._positions_opened

    @property
    def drawdown_halted(self) -> bool:
        return self._drawdown_halt

    @property
    def drawdown_scale(self) -> float:
        return self._drawdown_scale

    @property
    def slippage_factor(self) -> float:
        return self._slippage_factor

    # ------------------------------------------------------------------
    # Gate and size
    # ------------------------------------------------------------------

    def check_drawdown(self, current_balance: float) -> bool:
        """
        Update drawdown state from the current balance.

        Returns True only when the hard halt limit is breached. Below the halt
        threshold, updates _drawdown_scale to apply graduated sizing cuts at
        tier_1 and tier_2 drawdown levels (applied inside size_order).
        """
        if self._session_start_balance <= 0:
            return False
        drawdown = 1.0 - (current_balance / self._session_start_balance)
        if drawdown >= self.max_drawdown_pct:
            if not self._drawdown_halt:
                log.warning(
                    "DRAWDOWN HALT: balance $%.2f is %.1f%% below session start $%.2f (limit %.0f%%)",
                    current_balance, drawdown * 100, self._session_start_balance, self.max_drawdown_pct * 100,
                )
            self._drawdown_halt = True
            self._drawdown_scale = 0.0
            return True

        # Graduated tiers (highest drawdown tier takes precedence)
        if drawdown >= self.drawdown_tier_2_pct:
            new_scale = self.drawdown_tier_2_scale
        elif drawdown >= self.drawdown_tier_1_pct:
            new_scale = self.drawdown_tier_1_scale
        else:
            new_scale = 1.0

        if abs(new_scale - self._drawdown_scale) > 1e-6:
            log.info(
                "Drawdown scale %.2f → %.2f (drawdown=%.1f%%, session start $%.2f)",
                self._drawdown_scale, new_scale, drawdown * 100, self._session_start_balance,
            )
            self._drawdown_scale = new_scale
        return False

    def can_trade(self, open_positions: int) -> bool:
        """Return True if we are within all daily limits."""
        if self._drawdown_halt:
            log.info("Risk gate: drawdown halt active — no new trades")
            return False
        if self._daily_spent >= self.max_daily_spend:
            log.info("Risk gate: daily spend cap reached (%.2f / %.2f)", self._daily_spent, self.max_daily_spend)
            return False
        if open_positions >= self.max_positions:
            log.info("Risk gate: max open positions reached (%d)", open_positions)
            return False
        return True

    def size_order(self, signal: Signal, current_balance: float = 0.0, open_positions: int = 0) -> int:
        """
        Return the number of contracts to buy for this signal.
        Returns 0 if the position would be too small to be meaningful.

        Uses balance-aware sizing: effective budget is the lesser of the
        remaining daily cap and a fraction of actual account balance.
        Applies a correlation discount for multiple open positions.
        """
        remaining_daily = self.max_daily_spend - self._daily_spent
        if remaining_daily <= 0:
            return 0

        # Balance-aware: never risk more than bankroll_fraction of actual balance
        if current_balance > 0:
            balance_limit = self.bankroll_fraction * current_balance
            remaining_budget = min(remaining_daily, balance_limit)
        else:
            remaining_budget = remaining_daily

        if remaining_budget <= 0:
            return 0

        ask = signal.price
        if ask <= 0 or ask >= 1.0:
            log.warning("Unusual ask price %.4f for %s, skipping", ask, signal.ticker)
            return 0

        # Fractional Kelly sizing
        kelly_f = signal.edge / (1.0 - ask)
        spend = kelly_f * self.kelly_fraction * remaining_budget

        # Empirical slippage adjustment: if past fills delivered less edge than
        # predicted, scale Kelly down proportionally so we don't bet the phantom portion.
        spend *= self._slippage_factor

        # Graduated drawdown scaling: shrink sizing at tier thresholds before the hard halt.
        spend *= self._drawdown_scale

        # Correlation discount: each existing position reduces sizing by 30%
        # (all KXBTC positions bet on the same underlying)
        if open_positions > 0:
            correlation_discount = 0.7 ** open_positions
            spend *= correlation_discount
            log.debug("Correlation discount: %.2f (open_positions=%d)", correlation_discount, open_positions)

        # Never spend more than the remaining budget on one trade
        spend = min(spend, remaining_budget)

        contracts = math.floor(spend / ask)
        contracts = min(contracts, self.max_contracts_per_market)
        contracts = max(contracts, 0)

        log.debug(
            "Sizing %s %s: edge=%.4f ask=%.2f kelly_f=%.4f slip=%.2f dd=%.2f "
            "spend=%.2f balance=$%.2f → %d contracts",
            signal.ticker, signal.side, signal.edge, ask, kelly_f,
            self._slippage_factor, self._drawdown_scale,
            spend, current_balance, contracts,
        )
        return contracts
