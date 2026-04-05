"""
Position sizing and daily risk controls.

Sizing model: fractional Kelly for binary contracts
  kelly_f   = edge / (payout_per_dollar - ask_price)
            ≈ edge / (1 - ask_price)    [since Kalshi pays $1 per contract]
  spend     = kelly_f * KELLY_FRACTION * remaining_budget
  contracts = floor(spend / ask_price)
"""
import logging
import math

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
    ):
        self.max_daily_spend = max_daily_spend
        self.max_contracts_per_market = max_contracts_per_market
        self.max_positions = max_positions
        self.kelly_fraction = kelly_fraction

        self._daily_spent: float = 0.0
        self._positions_opened: int = 0

    # ------------------------------------------------------------------
    # State management
    # ------------------------------------------------------------------

    def reset(self) -> None:
        """Call at the start of each new trading day."""
        log.info("Daily risk counters reset (spent=%.2f, positions=%d)", self._daily_spent, self._positions_opened)
        self._daily_spent = 0.0
        self._positions_opened = 0

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

    # ------------------------------------------------------------------
    # Gate and size
    # ------------------------------------------------------------------

    def can_trade(self, open_positions: int) -> bool:
        """Return True if we are within all daily limits."""
        if self._daily_spent >= self.max_daily_spend:
            log.info("Risk gate: daily spend cap reached (%.2f / %.2f)", self._daily_spent, self.max_daily_spend)
            return False
        if open_positions >= self.max_positions:
            log.info("Risk gate: max open positions reached (%d)", open_positions)
            return False
        return True

    def size_order(self, signal: Signal) -> int:
        """
        Return the number of contracts to buy for this signal.
        Returns 0 if the position would be too small to be meaningful.
        """
        remaining_budget = self.max_daily_spend - self._daily_spent
        if remaining_budget <= 0:
            return 0

        ask = signal.price
        if ask <= 0 or ask >= 1.0:
            log.warning("Unusual ask price %.4f for %s, skipping", ask, signal.ticker)
            return 0

        # Fractional Kelly sizing
        kelly_f = signal.edge / (1.0 - ask)
        spend = kelly_f * self.kelly_fraction * remaining_budget

        # Never spend more than the remaining budget on one trade
        spend = min(spend, remaining_budget)

        contracts = math.floor(spend / ask)
        contracts = min(contracts, self.max_contracts_per_market)
        contracts = max(contracts, 0)

        log.debug(
            "Sizing %s %s: edge=%.4f ask=%.2f kelly_f=%.4f spend=%.2f → %d contracts",
            signal.ticker, signal.side, signal.edge, ask, kelly_f, spend, contracts,
        )
        return contracts
