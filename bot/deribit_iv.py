"""
Forward-looking implied volatility from Deribit's public REST API.

Deribit publishes free, no-auth options data. We pull the live option-chain
summary for an asset (BTC or ETH), pick the nearest-tenor at-the-money option
on each side, and read its `mark_iv` field — the broker's mid-market IV
quoted in *percent* (e.g. 65.0 for 65 % annualized vol).

We blend the call and put IV at the closest available strike to spot and the
nearest expiry beyond a configurable minimum.
"""
import logging
import time
from typing import Optional

import requests

log = logging.getLogger(__name__)

DERIBIT_BASE = "https://www.deribit.com/api/v2"

_SESSION = requests.Session()
_SESSION.headers.update({"Accept": "application/json"})

# In-process cache so we don't hammer the endpoint each cycle.
# Deribit IV updates intra-cycle but is plenty fresh at minute granularity.
_CACHE: dict[str, tuple[float, float]] = {}  # symbol -> (timestamp, iv_decimal)
_CACHE_TTL_SEC = 60


def _book_summary(currency: str) -> list[dict]:
    """Return the full option chain summary for BTC or ETH."""
    resp = _SESSION.get(
        f"{DERIBIT_BASE}/public/get_book_summary_by_currency",
        params={"currency": currency, "kind": "option"},
        timeout=10,
    )
    resp.raise_for_status()
    payload = resp.json()
    if "result" not in payload:
        raise ValueError(f"Deribit response missing 'result': {payload}")
    return payload["result"]


def _parse_instrument(name: str) -> Optional[tuple[float, str]]:
    """
    Parse a Deribit option instrument name like 'BTC-26APR26-95000-C'.

    Returns (strike, side) where side is 'C' or 'P'. Returns None on parse error.
    """
    parts = name.split("-")
    if len(parts) != 4:
        return None
    try:
        strike = float(parts[2])
    except ValueError:
        return None
    side = parts[3].upper()
    if side not in ("C", "P"):
        return None
    return strike, side


def get_atm_iv(
    symbol: str,
    spot: float,
    min_dte_hours: float = 6.0,
) -> Optional[float]:
    """
    Return the at-the-money implied vol (annualized, decimal) for the nearest
    Deribit option expiry beyond `min_dte_hours`.

    Args:
        symbol:       'BTC' or 'ETH'
        spot:         Current spot price for ATM strike selection
        min_dte_hours: Skip expiries closer than this — sub-day options are
                      noisier and don't span the typical Kalshi daily contract.

    Returns:
        Annualized IV in decimal form (e.g. 0.65 for 65%), or None if the
        chain is empty/unavailable.
    """
    sym = symbol.upper()

    cached = _CACHE.get(sym)
    now = time.time()
    if cached and (now - cached[0]) < _CACHE_TTL_SEC:
        return cached[1]

    try:
        chain = _book_summary(sym)
    except Exception as e:
        log.warning("Deribit IV fetch failed for %s: %s", sym, e)
        return None

    # Group by expiry timestamp (Deribit returns it in ms via creation_timestamp;
    # we instead derive expiry from the instrument name's date segment).
    by_expiry: dict[str, list[dict]] = {}
    for entry in chain:
        name = entry.get("instrument_name", "")
        parts = name.split("-")
        if len(parts) != 4:
            continue
        expiry_str = parts[1]
        by_expiry.setdefault(expiry_str, []).append(entry)

    if not by_expiry:
        return None

    # Sort expiries chronologically. Deribit format: e.g. '26APR26'
    def _expiry_to_epoch(exp: str) -> float:
        try:
            t = time.strptime(exp, "%d%b%y")
            return time.mktime(t)
        except ValueError:
            return float("inf")

    expiries_sorted = sorted(by_expiry.keys(), key=_expiry_to_epoch)
    min_dte_sec = min_dte_hours * 3600
    chosen: Optional[str] = None
    for exp in expiries_sorted:
        epoch = _expiry_to_epoch(exp)
        if epoch == float("inf"):
            continue
        if (epoch - now) >= min_dte_sec:
            chosen = exp
            break

    if chosen is None:
        log.debug("Deribit IV: no expiry beyond %.1fh DTE for %s", min_dte_hours, sym)
        return None

    # Find ATM call and put (strike closest to spot)
    call_iv: Optional[float] = None
    put_iv: Optional[float] = None
    call_dist = float("inf")
    put_dist = float("inf")

    for entry in by_expiry[chosen]:
        parsed = _parse_instrument(entry.get("instrument_name", ""))
        if parsed is None:
            continue
        strike, side = parsed
        mark_iv = entry.get("mark_iv")
        if mark_iv is None or mark_iv <= 0:
            continue
        dist = abs(strike - spot)
        if side == "C" and dist < call_dist:
            call_dist = dist
            call_iv = float(mark_iv)
        elif side == "P" and dist < put_dist:
            put_dist = dist
            put_iv = float(mark_iv)

    # Average call & put IV when both available; otherwise use whichever exists.
    ivs = [v for v in (call_iv, put_iv) if v is not None]
    if not ivs:
        return None
    iv_pct = sum(ivs) / len(ivs)
    iv_decimal = iv_pct / 100.0  # Deribit returns mark_iv as percent

    log.info(
        "Deribit IV %s expiry=%s ATM≈%.0f → %.2f%% (decimal %.4f)",
        sym, chosen, spot, iv_pct, iv_decimal,
    )

    _CACHE[sym] = (now, iv_decimal)
    return iv_decimal
