"""Central configuration for CryptoAgent (Phase 1).

Single source of truth for capital, pairs, timeframes, costs, risk limits,
data paths, and macro no-trade windows. All values live here so the rest of
the codebase never hardcodes parameters.

API credentials are read from the environment (`.env`) and are NEVER
hardcoded. Phase 1 uses only public market data, so credentials are optional.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv

# Load .env from the project root (no-op if the file is absent).
PROJECT_ROOT = Path(__file__).resolve().parent
load_dotenv(PROJECT_ROOT / ".env")

# ---------------------------------------------------------------------------
# Credentials (optional in Phase 1 — public data needs no key)
# ---------------------------------------------------------------------------
BINANCE_API_KEY = os.getenv("BINANCE_API_KEY", "")
BINANCE_API_SECRET = os.getenv("BINANCE_API_SECRET", "")

# Optional Telegram alerts (leave blank to disable).
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# ---------------------------------------------------------------------------
# Capital & universe
# ---------------------------------------------------------------------------
STARTING_CAPITAL = 500.0           # USD
# Pairs the live agent trades. Set to the pairs where the short-rally edge
# validated out-of-sample (ETH/SOL/XRP/AVAX); BTC/DOGE backtested negative.
PAIRS = ["ETHUSDT", "SOLUSDT", "XRPUSDT", "AVAXUSDT"]
PRIMARY_TF = "1h"                  # entry/exit logic timeframe
CONTEXT_TF = "4h"                  # higher-timeframe context
FEATURE_TF = "15m"                 # microstructure features only

# Strategy variant the LIVE agent runs (see strategies/regime_router.py).
LIVE_VARIANT = "short_only"

# How many candles to fetch/keep per timeframe (>=200 for EMA200).
CANDLE_HISTORY = 400

# Milliseconds per supported timeframe (for pagination and window scaling).
TIMEFRAME_MS = {
    "15m": 900_000,
    "1h": 3_600_000,
    "4h": 14_400_000,
    "1d": 86_400_000,
}


def bars_per_day(timeframe: str) -> int:
    """Return the number of candles per 24h for a given timeframe."""
    return max(1, int(86_400_000 / TIMEFRAME_MS[timeframe]))

# ---------------------------------------------------------------------------
# Execution costs (paper engine)
# ---------------------------------------------------------------------------
TAKER_FEE = 0.001     # 0.1% per side  -> 0.2% round trip
SLIPPAGE = 0.0005     # 0.05% adverse price move per fill
DEFAULT_LEVERAGE = 1.0

# ---------------------------------------------------------------------------
# Risk limits (hard-coded; the strategy/agent can never bypass these)
# ---------------------------------------------------------------------------
MAX_POSITION_PCT = 0.03     # 3% of equity risked/sized per trade
SL_ATR_MULT = 1.5           # stop-loss distance = 1.5 * ATR (this is "1R")
TP_ATR_MULT = 3.0           # final take-profit distance = 3.0 * ATR (= 2R)
MAX_OPEN_POSITIONS = 2
DAILY_LOSS_LIMIT = 0.03     # -3% from day-open equity -> flatten + halt for day
WEEKLY_LOSS_LIMIT = 0.08    # -8% from week-open equity -> halt, manual reset

# Active trade management (data-driven: most trades reach +1R then revert).
# Set a value to None to disable that behavior.
PARTIAL_TP_AT_R = 1.0       # take a partial profit when favorable excursion hits +1R
PARTIAL_TP_FRACTION = 0.5   # fraction of the position closed at the partial level
BREAKEVEN_AT_R = 1.0        # move the stop to entry once +1R is reached

# ---------------------------------------------------------------------------
# Feature thresholds shared with strategies
# ---------------------------------------------------------------------------
RSI_PERIOD = 14
RSI_FAST_PERIOD = 7
BB_PERIOD = 20
BB_STD = 2.0
ATR_PERIOD = 14
EMA_PERIODS = (20, 50, 200)
FUNDING_ZSCORE_DAYS = 30

# ---------------------------------------------------------------------------
# Data paths
# ---------------------------------------------------------------------------
DATA_DIR = PROJECT_ROOT / "data"
MARKET_DB = DATA_DIR / "market.db"
SCHEMA_SQL = PROJECT_ROOT / "schema.sql"
CHROMA_DIR = PROJECT_ROOT / "chroma"

# ---------------------------------------------------------------------------
# External free endpoints (no key required)
# ---------------------------------------------------------------------------
BINANCE_FAPI = "https://fapi.binance.com"
FEAR_GREED_URL = "https://api.alternative.me/fng/"
COINGECKO_GLOBAL_URL = "https://api.coingecko.com/api/v3/global"
HTTP_TIMEOUT = 15  # seconds

# ---------------------------------------------------------------------------
# Macro no-trade windows (brief Section 1.6)
# ---------------------------------------------------------------------------
# High-impact events around which the agent must not open new positions.
# Times are UTC. Extend this list as new dates are confirmed.
NO_TRADE_WINDOW_HOURS = 24

NO_TRADE_EVENTS = [
    # (label, UTC datetime)
    ("FOMC_2026_07_29", datetime(2026, 7, 29, 18, 0, tzinfo=timezone.utc)),
    ("US_CPI_2026_07", datetime(2026, 7, 14, 12, 30, tzinfo=timezone.utc)),
    ("US_NFP_2026_07", datetime(2026, 7, 2, 12, 30, tzinfo=timezone.utc)),
]


def is_no_trade_window(now: datetime | None = None) -> tuple[bool, str | None]:
    """Return whether ``now`` falls within a macro no-trade window.

    Args:
        now: Reference time (UTC). Defaults to the current UTC time.

    Returns:
        A ``(blocked, label)`` tuple. ``blocked`` is True when ``now`` is
        within ``NO_TRADE_WINDOW_HOURS`` of any event in ``NO_TRADE_EVENTS``;
        ``label`` names the triggering event (or None when not blocked).
    """
    if now is None:
        now = datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    window = timedelta(hours=NO_TRADE_WINDOW_HOURS)
    for label, event_time in NO_TRADE_EVENTS:
        if abs(now - event_time) <= window:
            return True, label
    return False, None
