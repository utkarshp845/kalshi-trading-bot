"""Load and validate all configuration from .env / environment variables."""
import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()


def _require(key: str) -> str:
    """Return env var value; returns empty string at import time if not set (validated at runtime)."""
    return os.getenv(key, "")


def _float(key: str, default: float) -> float:
    val = os.getenv(key)
    return float(val) if val is not None else default


def _int(key: str, default: int) -> int:
    val = os.getenv(key)
    return int(val) if val is not None else default


def _bool(key: str, default: bool) -> bool:
    val = os.getenv(key)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes")


# --- Kalshi API ---
KALSHI_API_KEY_ID: str = _require("KALSHI_API_KEY_ID")
KALSHI_PRIVATE_KEY_PATH: Path = Path(_require("KALSHI_PRIVATE_KEY_PATH"))
KALSHI_BASE_URL: str = os.getenv(
    "KALSHI_BASE_URL", "https://api.elections.kalshi.com/trade-api/v2"
)
KALSHI_TAKER_FEE: float = _float("KALSHI_TAKER_FEE", 0.07)  # dollars per contract

# --- Underlyings ---
# Each enabled underlying is scanned independently per cycle. Signals are then
# combined and ordered by net edge before risk gating.
ENABLE_BTC: bool = _bool("ENABLE_BTC", True)
ENABLE_ETH: bool = _bool("ENABLE_ETH", True)

# --- Strategy ---
MIN_EDGE: float = _float("MIN_EDGE", 0.05)          # net edge required after fees; 0.05 is realistic for liquid BTC markets
TRADING_MODE: str = os.getenv("TRADING_MODE", "observe").strip().lower()
USE_DRIFT: bool = _bool("USE_DRIFT", False)         # 30-day trailing drift is too noisy to be reliable signal; disabled by default
DRIFT_LOOKBACK_DAYS: int = _int("DRIFT_LOOKBACK_DAYS", 30)  # window for the trailing log-return drift estimate
MIN_T_HOURS: float = _float("MIN_T_HOURS", 1.0)     # was 0.5 — avoid noisy near-expiry markets
VOL_SHORT_DAYS: int = _int("VOL_SHORT_DAYS", 7)      # fast vol: used for signal probability
VOL_LONG_DAYS: int = _int("VOL_LONG_DAYS", 30)       # slow vol: logged as regime reference
VOL_SAFETY_MARGIN: float = _float("VOL_SAFETY_MARGIN", 1.05)  # was 1.25 — slight inflation only; over-inflating hides real edge
MAX_VOL_RATIO: float = _float("MAX_VOL_RATIO", 2.5)  # was 1.8 — volatile regimes have the most mispricing; raised ceiling
MIN_BID_ASK_SPREAD: float = _float("MIN_BID_ASK_SPREAD", 0.0)   # minimum acceptable bid (liquidity filter)
MAX_BID_ASK_SPREAD: float = _float("MAX_BID_ASK_SPREAD", 0.12)  # skip markets where ask-bid > this (wide spread = phantom edge)
MAX_BID_ASK_PCT_SPREAD: float = _float("MAX_BID_ASK_PCT_SPREAD", 0.20)  # skip if spread > 20% of mid-price (relative illiquidity filter)
MAX_LAST_PRICE_DIVERGENCE: float = _float("MAX_LAST_PRICE_DIVERGENCE", 0.20)  # skip if last_price diverges > 0.20 from yes_mid (stale/moving market)
THEO_PROB_BAND_MIN: float = _float("THEO_PROB_BAND_MIN", 0.25)
THEO_PROB_BAND_MAX: float = _float("THEO_PROB_BAND_MAX", 0.75)
MIN_SANE_STRIKES: int = _int("MIN_SANE_STRIKES", 2)          # min liquid strikes per expiry chain; was hardcoded 4
MAX_SIGMA_DISTANCE: float = _float("MAX_SIGMA_DISTANCE", 1.5)
MAX_CHAIN_BREAK_PCT: float = _float("MAX_CHAIN_BREAK_PCT", 0.10)
IMBALANCE_SCORE_WEIGHT: float = _float("IMBALANCE_SCORE_WEIGHT", 0.03)  # orderbook imbalance contribution to signal score; max ±0.03
EDGE_LEAK_LOOKBACK_FILLS: int = _int("EDGE_LEAK_LOOKBACK_FILLS", 50)
EDGE_HURDLE_BUFFER: float = _float("EDGE_HURDLE_BUFFER", 0.02)
SETTLED_MAE_LOOKBACK_TRADES: int = _int("SETTLED_MAE_LOOKBACK_TRADES", 30)
DEFAULT_EXPECTED_SLIPPAGE: float = _float("DEFAULT_EXPECTED_SLIPPAGE", 0.03)
DEFAULT_UNCERTAINTY_PENALTY: float = _float("DEFAULT_UNCERTAINTY_PENALTY", 0.05)
MAX_DEPTH_SLIPPAGE_PER_CONTRACT: float = _float("MAX_DEPTH_SLIPPAGE_PER_CONTRACT", 0.02)
LIQUIDITY_ENTRY_MULTIPLIER: float = _float("LIQUIDITY_ENTRY_MULTIPLIER", 5.0)
ORDERBOOK_DEPTH: int = _int("ORDERBOOK_DEPTH", 20)
DATA_STALE_AFTER_SEC_KRAKEN: int = _int("DATA_STALE_AFTER_SEC_KRAKEN", 20)
DATA_STALE_AFTER_SEC_KALSHI: int = _int("DATA_STALE_AFTER_SEC_KALSHI", 20)
DATA_STALE_AFTER_SEC_DERIBIT: int = _int("DATA_STALE_AFTER_SEC_DERIBIT", 120)
LIVE_MIN_REQUIRED_EDGE: float = _float("LIVE_MIN_REQUIRED_EDGE", 0.25)
COLD_START_MIN_EDGE: float = _float("COLD_START_MIN_EDGE", 0.30)
LIVE_MIN_FILL_HISTORY: int = _int("LIVE_MIN_FILL_HISTORY", 15)
LIVE_MIN_SETTLED_HISTORY: int = _int("LIVE_MIN_SETTLED_HISTORY", 10)
LIVE_GUARD_LOOKBACK_FILLS: int = _int("LIVE_GUARD_LOOKBACK_FILLS", 20)
LIVE_GUARD_LOOKBACK_SETTLED: int = _int("LIVE_GUARD_LOOKBACK_SETTLED", 20)
LIVE_HALT_MAX_AVG_REALIZED_EDGE: float = _float("LIVE_HALT_MAX_AVG_REALIZED_EDGE", 0.0)
LIVE_HALT_MAX_SETTLED_MAE: float = _float("LIVE_HALT_MAX_SETTLED_MAE", 0.20)
LIVE_SKIP_DEGRADED_ASSETS: bool = _bool("LIVE_SKIP_DEGRADED_ASSETS", True)

# --- Risk ---
DAILY_SPEND_PCT: float = _float("DAILY_SPEND_PCT", 0.15)      # was 0.10 — 15% of balance per day
DAILY_SPEND_FLOOR: float = _float("DAILY_SPEND_FLOOR", 5.0)   # minimum daily cap regardless of balance
MAX_CONTRACTS_PER_MARKET: int = _int("MAX_CONTRACTS_PER_MARKET", 15)  # was 3 — 3 contracts = $3 max payout, too small
MAX_POSITIONS: int = _int("MAX_POSITIONS", 5)              # was 2 — allows more diversification across strikes
MAX_SYMBOL_DAILY_SPEND_PCT: float = _float("MAX_SYMBOL_DAILY_SPEND_PCT", 0.08)
MAX_SYMBOL_POSITIONS: int = _int("MAX_SYMBOL_POSITIONS", 3)
KELLY_FRACTION: float = _float("KELLY_FRACTION", 0.20)    # was 0.10 — stacked discounts reduced to near-zero; raised
MAX_DRAWDOWN_PCT: float = _float("MAX_DRAWDOWN_PCT", 0.20)  # stop trading if account drops 20% from session start
BANKROLL_FRACTION: float = _float("BANKROLL_FRACTION", 0.40)  # was 0.25 — allow more of balance to be deployed
CORRELATION_DISCOUNT_FACTOR: float = _float("CORRELATION_DISCOUNT_FACTOR", 0.85)  # was hardcoded 0.70 — 0.70^n was too aggressive

# --- Implied Vol Calibration ---
IV_CALIBRATION_MIN_OBS: int = _int("IV_CALIBRATION_MIN_OBS", 10)     # min cycles before using adaptive margin
IV_SAFETY_MARGIN_MIN: float = _float("IV_SAFETY_MARGIN_MIN", 1.00)   # was 1.05 — allow margin to reach 1.0 when well-calibrated
IV_SAFETY_MARGIN_MAX: float = _float("IV_SAFETY_MARGIN_MAX", 1.80)   # was 3.0 — 3x vol turns every market into 50/50; capped at 1.8

# --- Deribit IV blend ---
# When enabled, the cycle pulls live ATM IV from Deribit's public option chain
# and blends it with realized vol: σ_blended = (1-w)·σ_realized + w·σ_deribit.
# Deribit is forward-looking; RV is backward-looking. The blend defaults to
# 60/40 in favor of Deribit (when available). Falls back to RV silently on error.
ENABLE_DERIBIT_IV: bool = _bool("ENABLE_DERIBIT_IV", True)
DERIBIT_IV_WEIGHT: float = _float("DERIBIT_IV_WEIGHT", 0.60)
DERIBIT_MIN_DTE_HOURS: float = _float("DERIBIT_MIN_DTE_HOURS", 6.0)

# --- Position Exit ---
ENABLE_POSITION_EXIT: bool = _bool("ENABLE_POSITION_EXIT", True)
EXIT_LOSS_TRIGGER: float = _float("EXIT_LOSS_TRIGGER", 0.40)  # exit when theoretical value drops to 40% of entry price
TAKE_PROFIT_TRIGGER: float = _float("TAKE_PROFIT_TRIGGER", 2.0)  # exit when theoretical value rises to ≥ 2.0× entry price
TAKE_PROFIT_MIN_HOURS: float = _float("TAKE_PROFIT_MIN_HOURS", 0.5)  # only take profit if there are still ≥ this many hours of decay risk left

# --- Smart Order Placement ---
ENABLE_MAKER_ORDERS: bool = _bool("ENABLE_MAKER_ORDERS", True)          # post at bid first (maker, $0 fee) before trying mid/ask
MAKER_ORDER_TIMEOUT_SEC: int = _int("MAKER_ORDER_TIMEOUT_SEC", 120)     # seconds to wait for a maker-bid fill
ENABLE_PRICE_IMPROVEMENT: bool = _bool("ENABLE_PRICE_IMPROVEMENT", True)
PRICE_IMPROVEMENT_TIMEOUT_SEC: int = _int("PRICE_IMPROVEMENT_TIMEOUT_SEC", 90)  # was 45 — more time at mid before falling back
MAKER_ENTRY_TIMEOUT_SEC: int = _int("MAKER_ENTRY_TIMEOUT_SEC", 30)              # wait for passive (post_only) bid fill
# Stale order monitoring: during waits, poll underlying spot; cancel if it drifts past this threshold
STALE_ORDER_POLL_SEC: int = _int("STALE_ORDER_POLL_SEC", 10)
STALE_ORDER_SPOT_MOVE_PCT: float = _float("STALE_ORDER_SPOT_MOVE_PCT", 0.003)  # 0.3% BTC move invalidates the quote

# --- Slippage-Adjusted Sizing ---
# Scale Kelly bet size by empirical avg(realized_edge) / avg(predicted_edge) from recent fills
SLIPPAGE_ADJUSTMENT_MIN_TRADES: int = _int("SLIPPAGE_ADJUSTMENT_MIN_TRADES", 10)
SLIPPAGE_ADJUSTMENT_LOOKBACK_DAYS: int = _int("SLIPPAGE_ADJUSTMENT_LOOKBACK_DAYS", 14)

# --- Graduated Drawdown ---
# Scale position sizing down as drawdown grows, before the hard halt at MAX_DRAWDOWN_PCT
DRAWDOWN_TIER_1_PCT: float = _float("DRAWDOWN_TIER_1_PCT", 0.10)    # 10% drawdown
DRAWDOWN_TIER_1_SCALE: float = _float("DRAWDOWN_TIER_1_SCALE", 0.50)  # scale sizing to 50%
DRAWDOWN_TIER_2_PCT: float = _float("DRAWDOWN_TIER_2_PCT", 0.15)    # 15% drawdown
DRAWDOWN_TIER_2_SCALE: float = _float("DRAWDOWN_TIER_2_SCALE", 0.25)  # scale sizing to 25%

# --- Monitoring ---
ALERT_WEBHOOK_URL: str = os.getenv("ALERT_WEBHOOK_URL", "")  # Slack/Discord webhook; empty = log only
ALERT_WEBHOOK_MIN_LEVEL: str = os.getenv("ALERT_WEBHOOK_MIN_LEVEL", "WARNING").upper()
ALERT_DEDUP_SECONDS: int = _int("ALERT_DEDUP_SECONDS", 900)

# --- Execution ---
POLL_INTERVAL_SECONDS: int = _int("POLL_INTERVAL_SECONDS", 60)   # was 120 — faster reaction to mispricings
DRY_RUN: bool = _bool("DRY_RUN", False)
FORCE_TRADING_HOURS: bool = _bool("FORCE_TRADING_HOURS", False)

# --- Paths ---
BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
LOGS_DIR = BASE_DIR / "logs"
REPORTS_DIR = BASE_DIR / "reports"
DB_PATH = DATA_DIR / "bot.db"
LOG_PATH = LOGS_DIR / "bot.log"
TRADES_CSV = LOGS_DIR / "trades.csv"
