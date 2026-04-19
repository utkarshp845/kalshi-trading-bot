"""
Mispricing arbitrage strategy for Kalshi BTC/ETH daily price-level markets.

Parses the strike from each market ticker, computes the theoretical
probability via the log-normal model (with optional drift), and returns a
trade signal when the edge exceeds the configured minimum.
"""
from typing import Optional
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone

from bot.kalshi_client import Market
from bot.pricing import calc_prob

log = logging.getLogger(__name__)

# Ticker format examples:
#   KXBTC-26APR4PM-B95000   (above $95,000)
#   KXBTC-26APR-B100000     (above $100,000)
_STRIKE_RE = re.compile(r"-B(\d+)$", re.IGNORECASE)


@dataclass
class Signal:
    ticker: str
    side: str         # "yes" (buy YES) or "no" (buy NO)
    price: float      # ask price to pay (dollars)
    gross_edge: float # edge before fees
    edge: float       # net edge after taker fee
    fee: float        # taker fee deducted (dollars per contract)
    theo_prob: float
    strike: float
    mid_price: float = 0.0      # bid-ask midpoint for price-improvement orders
    hours_to_expiry: float = 0.0


def _parse_strike(ticker: str) -> Optional[float]:
    m = _STRIKE_RE.search(ticker)
    if not m:
        return None
    return float(m.group(1))


def _hours_to_expiry(close_time: str) -> float:
    """Return hours from now until the market's close_time (ISO-8601 UTC string)."""
    try:
        expiry = datetime.fromisoformat(close_time.replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        delta = (expiry - now).total_seconds()
        return delta / 3600.0
    except Exception:
        log.warning("Could not parse close_time %r", close_time)
        return 0.0


def evaluate(
    market: Market,
    spot_price: float,
    sigma: float,
    min_edge: float,
    min_t_hours: float,
    fee: float = 0.0,
    max_bid_ask_spread: float = 0.25,
    max_bid_ask_pct_spread: float = 0.30,
    max_last_price_divergence: float = 0.15,
    mu: float = 0.0,
) -> tuple[Optional["Signal"], str]:
    """
    Evaluate one Kalshi market and return a Signal if an edge exists, else None.

    Args:
        market:             The Kalshi market to evaluate
        spot_price:         Current BTC/USD spot price
        sigma:              Annualized realized volatility (already safety-margin adjusted)
        min_edge:           Minimum NET edge threshold to act (e.g. 0.15)
        min_t_hours:        Skip markets expiring sooner than this (e.g. 1.0)
        fee:                Taker fee in dollars per contract (deducted from edge)
        max_bid_ask_spread: Skip markets with bid-ask spread wider than this
    """
    strike = _parse_strike(market.ticker)
    if strike is None:
        log.debug("Skipping %s: cannot parse strike", market.ticker)
        return None, "strike_parse"

    T_hours = _hours_to_expiry(market.close_time)
    if T_hours < min_t_hours:
        log.debug("Skipping %s: T=%.2fh < min %.2fh", market.ticker, T_hours, min_t_hours)
        return None, "t_too_small"

    # Bid-ask spread filter: two conditions — absolute and percentage-of-mid
    yes_spread = market.yes_ask - market.yes_bid
    no_spread = market.no_ask - market.no_bid
    yes_mid = (market.yes_ask + market.yes_bid) / 2
    no_mid = (market.no_ask + market.no_bid) / 2
    yes_pct = yes_spread / yes_mid if yes_mid > 0.01 else 99.0
    no_pct = no_spread / no_mid if no_mid > 0.01 else 99.0
    # A side is tradeable only if it passes both absolute and percentage spread tests
    yes_ok = yes_spread <= max_bid_ask_spread and yes_pct <= max_bid_ask_pct_spread
    no_ok = no_spread <= max_bid_ask_spread and no_pct <= max_bid_ask_pct_spread
    if not yes_ok and not no_ok:
        log.debug(
            "Skipping %s: spreads too wide (yes=%.3f/%.0f%%, no=%.3f/%.0f%%)",
            market.ticker, yes_spread, yes_pct * 100, no_spread, no_pct * 100,
        )
        return None, "spread_too_wide"

    # last_price divergence filter: stale or rapidly-moving markets have unreliable edges
    if market.last_price is not None:
        if abs(market.last_price - yes_mid) > max_last_price_divergence:
            log.debug(
                "Skipping %s: last_price %.2f diverges %.2f from yes_mid %.2f",
                market.ticker, market.last_price, abs(market.last_price - yes_mid), yes_mid,
            )
            return None, "last_price_diverge"

    T_years = T_hours / 8760.0
    theo_prob = calc_prob(spot_price, strike, T_years, sigma, mu=mu)

    # Gross edge = theoretical value minus ask price
    # Net edge = gross edge minus taker fee (fee is per contract in dollar terms)
    gross_yes = theo_prob - market.yes_ask
    gross_no = (1.0 - theo_prob) - market.no_ask
    net_yes = gross_yes - fee
    net_no = gross_no - fee

    log.debug(
        "%s K=%.0f S=%.0f T=%.2fh σ=%.4f → theo=%.4f  "
        "yes_ask=%.2f (gross %.4f net %.4f)  no_ask=%.2f (gross %.4f net %.4f)",
        market.ticker, strike, spot_price, T_hours, sigma, theo_prob,
        market.yes_ask, gross_yes, net_yes, market.no_ask, gross_no, net_no,
    )

    best_side: Optional[str] = None
    best_gross = -999.0
    best_net = min_edge  # net edge must exceed threshold

    # Only consider a side if it passes spread filters
    if net_yes > best_net and yes_ok:
        best_side = "yes"
        best_net = net_yes
        best_gross = gross_yes
    if net_no > best_net and no_ok:
        best_side = "no"
        best_net = net_no
        best_gross = gross_no

    if best_side is None:
        return None, "insufficient_edge"

    ask_price = market.yes_ask if best_side == "yes" else market.no_ask
    mid = yes_mid if best_side == "yes" else no_mid

    return Signal(
        ticker=market.ticker,
        side=best_side,
        price=ask_price,
        gross_edge=best_gross,
        edge=best_net,
        fee=fee,
        theo_prob=theo_prob,
        strike=strike,
        mid_price=mid,
        hours_to_expiry=T_hours,
    ), ""


def scan_markets(
    markets: list[Market],
    spot_price: float,
    sigma: float,
    min_edge: float,
    min_t_hours: float,
    held_tickers: set[str],
    fee: float = 0.0,
    max_bid_ask_spread: float = 0.25,
    max_bid_ask_pct_spread: float = 0.30,
    max_last_price_divergence: float = 0.15,
    mu: float = 0.0,
) -> list[Signal]:
    """
    Evaluate all markets and return signals sorted by net edge (highest first).
    Markets already held are skipped.
    """
    signals: list[Signal] = []
    rejection_counts: dict[str, int] = {}

    for market in markets:
        if market.ticker in held_tickers:
            rejection_counts["already_held"] = rejection_counts.get("already_held", 0) + 1
            log.debug("Skipping %s: already held", market.ticker)
            continue
        sig, reason = evaluate(
            market, spot_price, sigma, min_edge, min_t_hours,
            fee=fee, max_bid_ask_spread=max_bid_ask_spread,
            max_bid_ask_pct_spread=max_bid_ask_pct_spread,
            max_last_price_divergence=max_last_price_divergence,
            mu=mu,
        )
        if sig:
            signals.append(sig)
        else:
            rejection_counts[reason] = rejection_counts.get(reason, 0) + 1

    signals.sort(key=lambda s: s.edge, reverse=True)

    rejected_total = sum(rejection_counts.values())
    if rejected_total:
        breakdown = ", ".join(
            f"{r}={c}"
            for r, c in sorted(rejection_counts.items(), key=lambda x: -x[1])
        )
        log.info(
            "Found %d signal(s) out of %d markets — %d rejected: %s",
            len(signals), len(markets), rejected_total, breakdown,
        )
    else:
        log.info("Found %d signal(s) out of %d markets", len(signals), len(markets))

    return signals
