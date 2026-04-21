"""Execution helpers for live and simulated trading."""
from __future__ import annotations

import logging
import time

import bot.config as cfg
from bot.kalshi_client import KalshiClient, Order
from bot.price_feed import get_spot_price

log = logging.getLogger(__name__)


def execute_with_price_improvement(
    kalshi: KalshiClient,
    ticker: str,
    side: str,
    contracts: int,
    ask_price: float,
    mid_price: float,
    symbol: str,
) -> tuple[list[Order], str, bool]:
    """
    Execute a live order using mid-price improvement and ask fallback.

    Returns `(orders, status, stale_cancelled)`.
    """
    stale_cancelled = False
    if not cfg.ENABLE_PRICE_IMPROVEMENT or mid_price >= ask_price:
        order = kalshi.place_order(ticker, side, contracts, ask_price)
        return ([order] if order is not None else []), "ask_only", False

    try:
        entry_spot = get_spot_price(symbol)
    except Exception as e:
        log.debug("Spot snapshot before mid-price order failed: %s", e)
        entry_spot = None

    try:
        order1 = kalshi.place_order(ticker, side, contracts, mid_price)
    except Exception as e:
        log.warning("Mid-price order failed, falling back to ask: %s", e)
        order = kalshi.place_order(ticker, side, contracts, ask_price)
        return ([order] if order is not None else []), "ask_fallback", False

    total_wait = max(0, cfg.PRICE_IMPROVEMENT_TIMEOUT_SEC)
    poll = max(1, cfg.STALE_ORDER_POLL_SEC)
    elapsed = 0
    while elapsed < total_wait:
        step = min(poll, total_wait - elapsed)
        time.sleep(step)
        elapsed += step
        try:
            order1 = kalshi.get_order(order1.order_id)
        except Exception:
            pass
        if order1.fill_count >= contracts:
            return [order1], "mid_full", False
        if entry_spot is not None:
            try:
                current_spot = get_spot_price(symbol)
                drift = abs(current_spot - entry_spot) / entry_spot
                if drift > cfg.STALE_ORDER_SPOT_MOVE_PCT:
                    stale_cancelled = True
                    break
            except Exception as e:
                log.debug("Spot price poll failed: %s", e)

    filled = order1.fill_count
    if filled >= contracts:
        return [order1], "mid_full", False

    kalshi.cancel_order(order1.order_id)
    remaining = contracts - filled
    if remaining <= 0:
        return ([order1] if filled > 0 else []), "mid_partial", False
    if stale_cancelled:
        return ([order1] if filled > 0 else []), "stale_cancelled", True

    order2 = kalshi.place_order(ticker, side, remaining, ask_price)
    orders = []
    if filled > 0:
        orders.append(order1)
    if order2 is not None:
        orders.append(order2)
    return orders, ("mid_partial_ask" if filled > 0 else "ask_after_mid"), False
