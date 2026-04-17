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

# --- Strategy ---
MIN_EDGE: float = _float("MIN_EDGE", 0.15)          # was 0.08 — raised to require stronger edges
MIN_T_HOURS: float = _float("MIN_T_HOURS", 1.0)     # was 0.5 — avoid noisy near-expiry markets
VOL_SHORT_DAYS: int = _int("VOL_SHORT_DAYS", 7)      # fast vol: used for signal probability
VOL_LONG_DAYS: int = _int("VOL_LONG_DAYS", 30)       # slow vol: logged as regime reference
VOL_SAFETY_MARGIN: float = _float("VOL_SAFETY_MARGIN", 1.25)  # inflate vol estimate by 25% to account for implied > realized
MAX_VOL_RATIO: float = _float("MAX_VOL_RATIO", 1.8)  # skip trading when short/long vol ratio exceeds this (unstable regime)
MIN_BID_ASK_SPREAD: float = _float("MIN_BID_ASK_SPREAD", 0.0)   # minimum acceptable bid (liquidity filter)
MAX_BID_ASK_SPREAD: float = _float("MAX_BID_ASK_SPREAD", 0.25)  # skip markets where ask-bid > this (wide spread = phantom edge)
MAX_BID_ASK_PCT_SPREAD: float = _float("MAX_BID_ASK_PCT_SPREAD", 0.30)  # skip if spread > 30% of mid-price (relative illiquidity filter)
MAX_LAST_PRICE_DIVERGENCE: float = _float("MAX_LAST_PRICE_DIVERGENCE", 0.15)  # skip if last_price diverges > 0.15 from yes_mid (stale/moving market)

# --- Risk ---
MAX_DAILY_SPEND: float = _float("MAX_DAILY_SPEND", 5.0)   # was 100 — protect small bankrolls
MAX_CONTRACTS_PER_MARKET: int = _int("MAX_CONTRACTS_PER_MARKET", 3)  # was 10
MAX_POSITIONS: int = _int("MAX_POSITIONS", 2)              # was 5 — fewer correlated positions
KELLY_FRACTION: float = _float("KELLY_FRACTION", 0.10)    # was 0.25 — much more conservative
MAX_DRAWDOWN_PCT: float = _float("MAX_DRAWDOWN_PCT", 0.20)  # stop trading if account drops 20% from session start
BANKROLL_FRACTION: float = _float("BANKROLL_FRACTION", 0.25)  # never risk more than 25% of actual balance per day

# --- Implied Vol Calibration ---
IV_CALIBRATION_MIN_OBS: int = _int("IV_CALIBRATION_MIN_OBS", 10)     # min cycles before using adaptive margin
IV_SAFETY_MARGIN_MIN: float = _float("IV_SAFETY_MARGIN_MIN", 1.05)   # clamp adaptive margin to this floor
IV_SAFETY_MARGIN_MAX: float = _float("IV_SAFETY_MARGIN_MAX", 3.0)    # clamp adaptive margin to this ceiling

# --- Position Exit ---
ENABLE_POSITION_EXIT: bool = _bool("ENABLE_POSITION_EXIT", True)
EXIT_LOSS_TRIGGER: float = _float("EXIT_LOSS_TRIGGER", 0.40)  # exit when theoretical value drops to 40% of entry price

# --- Smart Order Placement ---
ENABLE_PRICE_IMPROVEMENT: bool = _bool("ENABLE_PRICE_IMPROVEMENT", True)
PRICE_IMPROVEMENT_TIMEOUT_SEC: int = _int("PRICE_IMPROVEMENT_TIMEOUT_SEC", 45)  # wait this long for mid-price fill
# Stale order monitoring: during the mid-price wait, poll BTC spot; cancel if it drifts past this threshold
STALE_ORDER_POLL_SEC: int = _int("STALE_ORDER_POLL_SEC", 10)
STALE_ORDER_SPOT_MOVE_PCT: float = _float("STALE_ORDER_SPOT_MOVE_PCT", 0.003)  # 0.3% BTC move invalidates the mid quote

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

# --- Execution ---
POLL_INTERVAL_SECONDS: int = _int("POLL_INTERVAL_SECONDS", 120)  # was 300 — react faster to opportunities
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
