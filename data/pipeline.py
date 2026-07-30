"""Data ingestion pipeline for CryptoAgent (Phase 1).

Pulls free-tier data and persists it to SQLite (``data/market.db``):

* OHLCV candles (1h / 4h / 15m) via ccxt Binance public endpoints.
* Funding rate (current + 30d history) via Binance USD-M futures REST.
* Open interest history (1h period) via Binance futures data endpoint.
* Fear & Greed index via alternative.me.
* BTC dominance via CoinGecko global endpoint.

Two entry points drive the scheduler:

* :func:`run_candle_cycle` — hourly: refresh candles for all pairs/timeframes.
* :func:`run_context_cycle` — every 4h: refresh funding, OI, sentiment, dominance.

Every fetch is wrapped so that one failing source never aborts the others.
No paid API keys are required.
"""

from __future__ import annotations

import sqlite3
import time
from datetime import datetime, timezone
from typing import Iterable

import ccxt
import requests

import config

# Explicit overrides; other ``*USDT`` symbols are derived automatically.
_CCXT_SYMBOL = {
    "BTCUSDT": "BTC/USDT",
    "ETHUSDT": "ETH/USDT",
}


def to_ccxt_symbol(symbol: str) -> str:
    """Convert a compact symbol (``"SOLUSDT"``) to ccxt form (``"SOL/USDT"``).

    Uses :data:`_CCXT_SYMBOL` overrides first, then derives ``BASE/USDT`` for
    any symbol ending in a known quote currency, so new pairs need no config.
    """
    if symbol in _CCXT_SYMBOL:
        return _CCXT_SYMBOL[symbol]
    for quote in ("USDT", "USDC", "BTC", "ETH"):
        if symbol.endswith(quote) and len(symbol) > len(quote):
            return f"{symbol[:-len(quote)]}/{quote}"
    return symbol


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------
def get_connection() -> sqlite3.Connection:
    """Open a SQLite connection to the market database.

    Ensures the parent directory exists and enables WAL mode for safer
    concurrent reads. The caller is responsible for closing the connection.
    """
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(config.MARKET_DB)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.row_factory = sqlite3.Row
    return conn


def init_db(conn: sqlite3.Connection | None = None) -> None:
    """Create all tables by executing ``schema.sql`` (idempotent).

    Args:
        conn: Optional existing connection. When omitted, a temporary
            connection is opened and closed.
    """
    own = conn is None
    conn = conn or get_connection()
    try:
        with open(config.SCHEMA_SQL, "r", encoding="utf-8") as fh:
            conn.executescript(fh.read())
        _migrate(conn)
        conn.commit()
    finally:
        if own:
            conn.close()


# Columns added after the initial schema; applied idempotently to old DBs.
_MIGRATIONS = [
    ("open_positions", "r_price", "REAL DEFAULT 0"),
    ("open_positions", "be_moved", "INTEGER DEFAULT 0"),
    ("open_positions", "partial_done", "INTEGER DEFAULT 0"),
    ("open_positions", "realized_partial_usd", "REAL DEFAULT 0"),
]


def _migrate(conn: sqlite3.Connection) -> None:
    """Add any newly-introduced columns to existing tables (idempotent)."""
    for table, column, decl in _MIGRATIONS:
        try:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
        except sqlite3.OperationalError:
            pass  # column already exists


# ---------------------------------------------------------------------------
# OHLCV candles (ccxt)
# ---------------------------------------------------------------------------
def _make_exchange() -> ccxt.binance:
    """Construct a ccxt Binance client for public market data.

    Credentials are attached only if present in the environment; they are not
    required for public OHLCV and are unused in Phase 1.
    """
    params = {"enableRateLimit": True}
    if config.BINANCE_API_KEY and config.BINANCE_API_SECRET:
        params["apiKey"] = config.BINANCE_API_KEY
        params["secret"] = config.BINANCE_API_SECRET
    return ccxt.binance(params)


def fetch_ohlcv(symbol: str, timeframe: str, limit: int | None = None,
                exchange: ccxt.binance | None = None) -> list[list[float]]:
    """Fetch raw OHLCV candles from Binance via ccxt.

    Args:
        symbol: Compact symbol such as ``"BTCUSDT"``.
        timeframe: ccxt timeframe string, e.g. ``"1h"``, ``"4h"``, ``"15m"``.
        limit: Number of candles to request (defaults to ``CANDLE_HISTORY``).
        exchange: Optional pre-built ccxt client (to reuse rate limiting).

    Returns:
        A list of ``[open_time_ms, open, high, low, close, volume]`` rows.
    """
    limit = limit or config.CANDLE_HISTORY
    exchange = exchange or _make_exchange()
    ccxt_symbol = to_ccxt_symbol(symbol)
    return exchange.fetch_ohlcv(ccxt_symbol, timeframe=timeframe, limit=limit)


def load_candles_tail(conn: sqlite3.Connection, symbol: str,
                      timeframe: str | None = None) -> dict | None:
    """Return the most recent stored candle for a symbol/timeframe as a dict.

    Args:
        conn: Open SQLite connection.
        symbol: Compact symbol such as ``"BTCUSDT"``.
        timeframe: Timeframe label; defaults to ``config.PRIMARY_TF``.

    Returns:
        A dict with open/high/low/close/volume/open_time, or ``None`` if no
        candle is stored.
    """
    timeframe = timeframe or config.PRIMARY_TF
    row = conn.execute(
        "SELECT open_time, open, high, low, close, volume FROM candles "
        "WHERE symbol = ? AND timeframe = ? ORDER BY open_time DESC LIMIT 1",
        (symbol, timeframe),
    ).fetchone()
    return dict(row) if row is not None else None


def store_candles(symbol: str, timeframe: str, rows: Iterable[Iterable[float]],
                  conn: sqlite3.Connection | None = None) -> int:
    """Upsert OHLCV rows into the ``candles`` table (idempotent).

    Args:
        symbol: Compact symbol such as ``"BTCUSDT"``.
        timeframe: Timeframe label matching the fetch.
        rows: Iterable of ``[open_time_ms, open, high, low, close, volume]``.
        conn: Optional existing connection.

    Returns:
        The number of rows written.
    """
    own = conn is None
    conn = conn or get_connection()
    try:
        payload = [
            (symbol, timeframe, int(r[0]), float(r[1]), float(r[2]),
             float(r[3]), float(r[4]), float(r[5]))
            for r in rows
        ]
        conn.executemany(
            """
            INSERT INTO candles
                (symbol, timeframe, open_time, open, high, low, close, volume)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(symbol, timeframe, open_time) DO UPDATE SET
                open=excluded.open, high=excluded.high, low=excluded.low,
                close=excluded.close, volume=excluded.volume
            """,
            payload,
        )
        conn.commit()
        return len(payload)
    finally:
        if own:
            conn.close()


# ---------------------------------------------------------------------------
# Funding rate (Binance USD-M futures REST)
# ---------------------------------------------------------------------------
def fetch_funding(symbol: str) -> dict | None:
    """Fetch the latest funding/premium index for a symbol.

    Uses the public ``/fapi/v1/premiumIndex`` endpoint (no key).

    Args:
        symbol: Compact futures symbol such as ``"BTCUSDT"``.

    Returns:
        A dict ``{"symbol", "funding_time", "funding_rate"}`` or ``None`` on
        failure.
    """
    url = f"{config.BINANCE_FAPI}/fapi/v1/premiumIndex"
    resp = requests.get(url, params={"symbol": symbol},
                        timeout=config.HTTP_TIMEOUT)
    resp.raise_for_status()
    data = resp.json()
    return {
        "symbol": symbol,
        "funding_time": int(data.get("time", int(time.time() * 1000))),
        "funding_rate": float(data["lastFundingRate"]),
    }


def fetch_funding_history(symbol: str, days: int = config.FUNDING_ZSCORE_DAYS
                          ) -> list[dict]:
    """Fetch historical funding rates for the trailing ``days`` window.

    Uses the public ``/fapi/v1/fundingRate`` endpoint (no key). Funding is
    paid every 8h, so ``days`` maps to ``days * 3`` expected rows.

    Args:
        symbol: Compact futures symbol such as ``"BTCUSDT"``.
        days: Trailing window length in days.

    Returns:
        A list of ``{"symbol", "funding_time", "funding_rate"}`` dicts.
    """
    url = f"{config.BINANCE_FAPI}/fapi/v1/fundingRate"
    start_ms = int((time.time() - days * 86400) * 1000)
    resp = requests.get(
        url,
        params={"symbol": symbol, "startTime": start_ms, "limit": 1000},
        timeout=config.HTTP_TIMEOUT,
    )
    resp.raise_for_status()
    return [
        {
            "symbol": symbol,
            "funding_time": int(item["fundingTime"]),
            "funding_rate": float(item["fundingRate"]),
        }
        for item in resp.json()
    ]


def store_funding(rows: Iterable[dict], conn: sqlite3.Connection | None = None
                  ) -> int:
    """Upsert funding rows into the ``funding`` table (idempotent)."""
    own = conn is None
    conn = conn or get_connection()
    try:
        payload = [
            (r["symbol"], int(r["funding_time"]), float(r["funding_rate"]))
            for r in rows
        ]
        conn.executemany(
            """
            INSERT INTO funding (symbol, funding_time, funding_rate)
            VALUES (?, ?, ?)
            ON CONFLICT(symbol, funding_time) DO UPDATE SET
                funding_rate=excluded.funding_rate
            """,
            payload,
        )
        conn.commit()
        return len(payload)
    finally:
        if own:
            conn.close()


# ---------------------------------------------------------------------------
# Open interest (Binance futures data endpoint)
# ---------------------------------------------------------------------------
def fetch_open_interest_hist(symbol: str, period: str = "1h",
                             limit: int = 48) -> list[dict]:
    """Fetch open interest history via ``/futures/data/openInterestHist``.

    Args:
        symbol: Compact futures symbol such as ``"BTCUSDT"``.
        period: Aggregation period (``"5m"``..``"1d"``); ``"1h"`` by default.
        limit: Number of points (max 500).

    Returns:
        A list of ``{"symbol", "ts", "open_interest", "open_interest_usd"}``.
    """
    url = f"{config.BINANCE_FAPI}/futures/data/openInterestHist"
    resp = requests.get(
        url,
        params={"symbol": symbol, "period": period, "limit": limit},
        timeout=config.HTTP_TIMEOUT,
    )
    resp.raise_for_status()
    out = []
    for item in resp.json():
        out.append({
            "symbol": symbol,
            "ts": int(item["timestamp"]),
            "open_interest": float(item["sumOpenInterest"]),
            "open_interest_usd": float(item.get("sumOpenInterestValue", 0.0)),
        })
    return out


def store_open_interest(rows: Iterable[dict],
                        conn: sqlite3.Connection | None = None) -> int:
    """Upsert open-interest rows into the ``open_interest`` table."""
    own = conn is None
    conn = conn or get_connection()
    try:
        payload = [
            (r["symbol"], int(r["ts"]), float(r["open_interest"]),
             float(r.get("open_interest_usd") or 0.0))
            for r in rows
        ]
        conn.executemany(
            """
            INSERT INTO open_interest (symbol, ts, open_interest, open_interest_usd)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(symbol, ts) DO UPDATE SET
                open_interest=excluded.open_interest,
                open_interest_usd=excluded.open_interest_usd
            """,
            payload,
        )
        conn.commit()
        return len(payload)
    finally:
        if own:
            conn.close()


# ---------------------------------------------------------------------------
# Fear & Greed (alternative.me)
# ---------------------------------------------------------------------------
def fetch_fear_greed(limit: int = 30) -> list[dict]:
    """Fetch the Fear & Greed index history from alternative.me (free).

    Args:
        limit: Number of daily readings to fetch.

    Returns:
        A list of ``{"ts", "value", "classification"}`` dicts (epoch seconds).
    """
    resp = requests.get(config.FEAR_GREED_URL, params={"limit": limit},
                        timeout=config.HTTP_TIMEOUT)
    resp.raise_for_status()
    out = []
    for item in resp.json().get("data", []):
        out.append({
            "ts": int(item["timestamp"]),
            "value": int(item["value"]),
            "classification": item.get("value_classification"),
        })
    return out


def store_fear_greed(rows: Iterable[dict],
                     conn: sqlite3.Connection | None = None) -> int:
    """Upsert Fear & Greed rows into the ``sentiment`` table."""
    own = conn is None
    conn = conn or get_connection()
    try:
        payload = [
            (int(r["ts"]), int(r["value"]), r.get("classification"))
            for r in rows
        ]
        conn.executemany(
            """
            INSERT INTO sentiment (ts, value, classification)
            VALUES (?, ?, ?)
            ON CONFLICT(ts) DO UPDATE SET
                value=excluded.value, classification=excluded.classification
            """,
            payload,
        )
        conn.commit()
        return len(payload)
    finally:
        if own:
            conn.close()


# ---------------------------------------------------------------------------
# BTC dominance (CoinGecko)
# ---------------------------------------------------------------------------
def fetch_btc_dominance() -> dict | None:
    """Fetch the current BTC/ETH market-cap dominance from CoinGecko (free).

    Returns:
        A dict ``{"ts", "btc_dom", "eth_dom"}`` (epoch seconds) or ``None``.
    """
    resp = requests.get(config.COINGECKO_GLOBAL_URL, timeout=config.HTTP_TIMEOUT)
    resp.raise_for_status()
    pct = resp.json().get("data", {}).get("market_cap_percentage", {})
    return {
        "ts": int(datetime.now(timezone.utc).timestamp()),
        "btc_dom": float(pct.get("btc", 0.0)),
        "eth_dom": float(pct.get("eth", 0.0)),
    }


def store_dominance(row: dict, conn: sqlite3.Connection | None = None) -> int:
    """Append a dominance snapshot into the ``dominance`` table."""
    own = conn is None
    conn = conn or get_connection()
    try:
        conn.execute(
            """
            INSERT INTO dominance (ts, btc_dom, eth_dom)
            VALUES (?, ?, ?)
            ON CONFLICT(ts) DO UPDATE SET
                btc_dom=excluded.btc_dom, eth_dom=excluded.eth_dom
            """,
            (int(row["ts"]), float(row["btc_dom"]),
             float(row.get("eth_dom") or 0.0)),
        )
        conn.commit()
        return 1
    finally:
        if own:
            conn.close()


# ---------------------------------------------------------------------------
# Scheduler entry points
# ---------------------------------------------------------------------------
def run_candle_cycle(conn: sqlite3.Connection | None = None) -> dict:
    """Hourly job: refresh OHLCV candles for all pairs and timeframes.

    Each pair/timeframe fetch is isolated so a single failure does not abort
    the rest. Returns a per-key summary of rows written or error strings.
    """
    own = conn is None
    conn = conn or get_connection()
    summary: dict[str, object] = {}
    try:
        exchange = _make_exchange()
        for symbol in config.PAIRS:
            for tf in (config.PRIMARY_TF, config.CONTEXT_TF, config.FEATURE_TF):
                key = f"{symbol}:{tf}"
                try:
                    rows = fetch_ohlcv(symbol, tf, exchange=exchange)
                    summary[key] = store_candles(symbol, tf, rows, conn=conn)
                except Exception as exc:  # noqa: BLE001 - isolate per source
                    summary[key] = f"ERROR: {exc}"
        return summary
    finally:
        if own:
            conn.close()


def run_context_cycle(conn: sqlite3.Connection | None = None) -> dict:
    """4-hourly job: refresh funding, open interest, sentiment, dominance.

    Each source is isolated; failures are reported per key without aborting
    the cycle. Returns a summary of rows written or error strings.
    """
    own = conn is None
    conn = conn or get_connection()
    summary: dict[str, object] = {}
    try:
        for symbol in config.PAIRS:
            try:
                hist = fetch_funding_history(symbol)
                latest = fetch_funding(symbol)
                if latest:
                    hist.append(latest)
                summary[f"funding:{symbol}"] = store_funding(hist, conn=conn)
            except Exception as exc:  # noqa: BLE001
                summary[f"funding:{symbol}"] = f"ERROR: {exc}"
            try:
                oi = fetch_open_interest_hist(symbol)
                summary[f"oi:{symbol}"] = store_open_interest(oi, conn=conn)
            except Exception as exc:  # noqa: BLE001
                summary[f"oi:{symbol}"] = f"ERROR: {exc}"

        try:
            summary["fear_greed"] = store_fear_greed(fetch_fear_greed(),
                                                     conn=conn)
        except Exception as exc:  # noqa: BLE001
            summary["fear_greed"] = f"ERROR: {exc}"

        try:
            dom = fetch_btc_dominance()
            summary["dominance"] = store_dominance(dom, conn=conn) if dom else 0
        except Exception as exc:  # noqa: BLE001
            summary["dominance"] = f"ERROR: {exc}"

        return summary
    finally:
        if own:
            conn.close()
