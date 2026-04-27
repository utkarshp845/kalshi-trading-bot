"""Kalshi fee helpers."""
from __future__ import annotations

import math


def kalshi_fee(price: float, contracts: int, rate: float, *, round_up: bool = True) -> float:
    """
    Return total Kalshi fee in dollars for a fill.

    Standard Kalshi fee math is rate * contracts * price * (1 - price),
    rounded up to the next cent for the whole fill.
    """
    if contracts <= 0 or price <= 0:
        return 0.0
    p = max(0.01, min(0.99, float(price)))
    raw = max(0.0, float(rate)) * contracts * p * (1.0 - p)
    if not round_up:
        return raw
    return math.ceil((raw - 1e-12) * 100.0) / 100.0


def fee_per_contract(price: float, contracts: int, rate: float) -> float:
    if contracts <= 0:
        return 0.0
    return kalshi_fee(price, contracts, rate) / contracts
