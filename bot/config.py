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
MIN_EDGE: float = _float("MIN_EDGE", 0.08)
MIN_T_HOURS: float = _float("MIN_T_HOURS", 0.5)
VOL_SHORT_DAYS: int = _int("VOL_SHORT_DAYS", 7)   # fast vol: used for signal probability
VOL_LONG_DAYS: int = _int("VOL_LONG_DAYS", 30)    # slow vol: logged as regime reference

# --- Risk ---
MAX_DAILY_SPEND: float = _float("MAX_DAILY_SPEND", 100.0)
MAX_CONTRACTS_PER_MARKET: int = _int("MAX_CONTRACTS_PER_MARKET", 10)
MAX_POSITIONS: int = _int("MAX_POSITIONS", 5)
KELLY_FRACTION: float = _float("KELLY_FRACTION", 0.25)

# --- Execution ---
POLL_INTERVAL_SECONDS: int = _int("POLL_INTERVAL_SECONDS", 300)
DRY_RUN: bool = _bool("DRY_RUN", False)
FORCE_TRADING_HOURS: bool = _bool("FORCE_TRADING_HOURS", False)

# --- Paths ---
BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
LOGS_DIR = BASE_DIR / "logs"
DB_PATH = DATA_DIR / "bot.db"
LOG_PATH = LOGS_DIR / "bot.log"
TRADES_CSV = LOGS_DIR / "trades.csv"
