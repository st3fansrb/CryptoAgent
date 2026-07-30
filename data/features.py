"""Feature engineering for CryptoAgent (Phase 1).

Computes the per-1h-candle feature vector described in the brief (Sections 5
and 8): price-based indicators, derivatives context, sentiment/macro, and a
*computed* regime label used to route strategies.

The public entry point is :func:`compute_features`, which loads candles and
context data from ``data/market.db`` and returns a flat dict of features for
the latest closed 1h candle. Pure helpers operate on DataFrames so they are
easy to unit-test offline with synthetic data.
"""

from __future__ import annotations

import sqlite3
from typing import Any

import numpy as np
import pandas as pd
from ta.momentum import RSIIndicator
from ta.trend import EMAIndicator
from ta.volatility import AverageTrueRange, BollingerBands

import config

# Order-stable list of NUMERIC feature keys used for ChromaDB embeddings.
# Keep additions append-only so historical embeddings stay comparable.
NUMERIC_FEATURE_KEYS = [
    "rsi14", "rsi7",
    "bb_pct_b", "bb_width",
    "atr", "atr_pct",
    "price_vs_ema20", "price_vs_ema50", "price_vs_ema200",
    "ema50_slope",
    "ret_1h", "ret_4h", "ret_24h",
    "realized_vol_24h", "realized_vol_30d",
    "funding_rate", "funding_zscore", "funding_sign_change",
    "oi_change_pct",
    "fear_greed", "btc_dominance", "dominance_7d_trend",
]


# ---------------------------------------------------------------------------
# Candle loading
# ---------------------------------------------------------------------------
def load_candles(symbol: str, timeframe: str, limit: int,
                 conn: sqlite3.Connection) -> pd.DataFrame:
    """Load the most recent ``limit`` candles for a symbol/timeframe.

    Args:
        symbol: Compact symbol such as ``"BTCUSDT"``.
        timeframe: Timeframe label, e.g. ``"1h"``.
        limit: Maximum number of candles (most recent).
        conn: Open SQLite connection.

    Returns:
        A DataFrame sorted ascending by ``open_time`` with columns
        ``[open_time, open, high, low, close, volume]``.
    """
    query = (
        "SELECT open_time, open, high, low, close, volume FROM candles "
        "WHERE symbol = ? AND timeframe = ? "
        "ORDER BY open_time DESC LIMIT ?"
    )
    df = pd.read_sql_query(query, conn, params=(symbol, timeframe, limit))
    return df.sort_values("open_time").reset_index(drop=True)


# ---------------------------------------------------------------------------
# Price-based features
# ---------------------------------------------------------------------------
def price_features(df: pd.DataFrame) -> dict[str, float]:
    """Compute price/indicator features from a 1h OHLCV DataFrame.

    Expects an ascending-time DataFrame with at least ``EMA200`` periods of
    history for stable values. Returns scalar features for the latest bar.

    Args:
        df: Ascending OHLCV DataFrame (columns open/high/low/close/volume).

    Returns:
        A dict of float features (see :data:`NUMERIC_FEATURE_KEYS`).
    """
    close = df["close"].astype(float)
    high = df["high"].astype(float)
    low = df["low"].astype(float)
    last = float(close.iloc[-1])

    rsi14 = RSIIndicator(close, window=config.RSI_PERIOD).rsi()
    rsi7 = RSIIndicator(close, window=config.RSI_FAST_PERIOD).rsi()

    bb = BollingerBands(close, window=config.BB_PERIOD, window_dev=config.BB_STD)
    bb_high = bb.bollinger_hband()
    bb_low = bb.bollinger_lband()
    bb_mid = bb.bollinger_mavg()
    upper = float(bb_high.iloc[-1])
    lower = float(bb_low.iloc[-1])
    mid = float(bb_mid.iloc[-1])
    band = max(upper - lower, 1e-9)
    # %b: 0 at lower band, 1 at upper band (can exceed [0,1] on breakouts).
    bb_pct_b = (last - lower) / band
    bb_width = band / mid if mid else 0.0

    atr = AverageTrueRange(high, low, close, window=config.ATR_PERIOD)\
        .average_true_range()
    atr_val = float(atr.iloc[-1])
    atr_pct = atr_val / last if last else 0.0

    feats: dict[str, float] = {
        "rsi14": _safe(rsi14.iloc[-1], 50.0),
        "rsi7": _safe(rsi7.iloc[-1], 50.0),
        "bb_upper": upper,
        "bb_lower": lower,
        "bb_mid": mid,
        "bb_pct_b": _safe(bb_pct_b, 0.5),
        "bb_width": _safe(bb_width, 0.0),
        "atr": _safe(atr_val, 0.0),
        "atr_pct": _safe(atr_pct, 0.0),
        "close": last,
    }

    # EMAs and price-relative ratios ((price/ema) - 1).
    ema_values: dict[int, float] = {}
    for period in config.EMA_PERIODS:
        ema = EMAIndicator(close, window=period).ema_indicator()
        ema_val = float(ema.iloc[-1]) if not np.isnan(ema.iloc[-1]) else last
        ema_values[period] = ema_val
        feats[f"ema{period}"] = ema_val
        feats[f"price_vs_ema{period}"] = (last / ema_val - 1.0) if ema_val else 0.0

    # EMA50 slope over the last 5 bars (normalized by price).
    ema50_series = EMAIndicator(close, window=50).ema_indicator()
    if len(ema50_series.dropna()) >= 6:
        slope = (float(ema50_series.iloc[-1]) - float(ema50_series.iloc[-6])) / 5.0
        feats["ema50_slope"] = slope / last if last else 0.0
    else:
        feats["ema50_slope"] = 0.0

    # Returns over 1h / 4h / 24h (in 1h-bar units).
    feats["ret_1h"] = _pct_return(close, 1)
    feats["ret_4h"] = _pct_return(close, 4)
    feats["ret_24h"] = _pct_return(close, 24)

    # Realized volatility: std of log returns, 24h window and 30d (720h) window.
    log_ret = np.log(close / close.shift(1))
    feats["realized_vol_24h"] = _safe(log_ret.tail(24).std(), 0.0)
    feats["realized_vol_30d"] = _safe(log_ret.tail(720).std(), 0.0)

    return feats


def _pct_return(close: pd.Series, periods: int) -> float:
    """Return the percentage change of ``close`` over ``periods`` bars."""
    if len(close) <= periods:
        return 0.0
    prev = float(close.iloc[-1 - periods])
    return (float(close.iloc[-1]) / prev - 1.0) if prev else 0.0


def _safe(value: Any, default: float) -> float:
    """Coerce a possibly-NaN/None value to a finite float, else ``default``."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return default
    return v if np.isfinite(v) else default


# ---------------------------------------------------------------------------
# Derivatives features
# ---------------------------------------------------------------------------
def derivatives_features(symbol: str, conn: sqlite3.Connection) -> dict[str, float]:
    """Compute funding/open-interest features from stored derivatives data.

    Args:
        symbol: Compact futures symbol such as ``"BTCUSDT"``.
        conn: Open SQLite connection.

    Returns:
        Dict with ``funding_rate``, ``funding_zscore``,
        ``funding_sign_change``, ``oi_change_pct``.
    """
    funding = pd.read_sql_query(
        "SELECT funding_time, funding_rate FROM funding "
        "WHERE symbol = ? ORDER BY funding_time DESC LIMIT 200",
        conn, params=(symbol,),
    ).sort_values("funding_time").reset_index(drop=True)

    out = {
        "funding_rate": 0.0,
        "funding_zscore": 0.0,
        "funding_sign_change": 0.0,
        "oi_change_pct": 0.0,
    }
    if not funding.empty:
        rates = funding["funding_rate"].astype(float)
        current = float(rates.iloc[-1])
        out["funding_rate"] = current
        mean, std = float(rates.mean()), float(rates.std())
        out["funding_zscore"] = (current - mean) / std if std > 1e-12 else 0.0
        if len(rates) >= 2:
            prev = float(rates.iloc[-2])
            out["funding_sign_change"] = 1.0 if (prev * current) < 0 else 0.0

    oi = pd.read_sql_query(
        "SELECT ts, open_interest FROM open_interest "
        "WHERE symbol = ? ORDER BY ts DESC LIMIT 2",
        conn, params=(symbol,),
    ).sort_values("ts").reset_index(drop=True)
    if len(oi) >= 2:
        prev_oi = float(oi["open_interest"].iloc[0])
        cur_oi = float(oi["open_interest"].iloc[1])
        out["oi_change_pct"] = (cur_oi / prev_oi - 1.0) if prev_oi else 0.0

    return out


# ---------------------------------------------------------------------------
# Sentiment / macro features
# ---------------------------------------------------------------------------
def fear_greed_bucket(value: float) -> str:
    """Map a Fear & Greed value to a categorical bucket.

    Buckets: 0-24 Extreme Fear, 25-49 Fear, 50-74 Neutral/Greed,
    75-100 Extreme Greed.
    """
    if value < 25:
        return "EXTREME_FEAR"
    if value < 50:
        return "FEAR"
    if value < 75:
        return "NEUTRAL_GREED"
    return "EXTREME_GREED"


def sentiment_features(conn: sqlite3.Connection) -> dict[str, Any]:
    """Compute Fear & Greed and BTC-dominance features from stored data.

    Returns:
        Dict with ``fear_greed``, ``fear_greed_bucket``, ``btc_dominance``,
        ``dominance_7d_trend`` (-1/0/+1), ``dominance_trend_label``.
    """
    out: dict[str, Any] = {
        "fear_greed": 50.0,
        "fear_greed_bucket": "NEUTRAL_GREED",
        "btc_dominance": 0.0,
        "dominance_7d_trend": 0.0,
        "dominance_trend_label": "FLAT",
    }

    fg = pd.read_sql_query(
        "SELECT value FROM sentiment ORDER BY ts DESC LIMIT 1", conn,
    )
    if not fg.empty:
        value = float(fg["value"].iloc[0])
        out["fear_greed"] = value
        out["fear_greed_bucket"] = fear_greed_bucket(value)

    dom = pd.read_sql_query(
        "SELECT ts, btc_dom FROM dominance ORDER BY ts DESC LIMIT 200",
        conn,
    ).sort_values("ts").reset_index(drop=True)
    if not dom.empty:
        out["btc_dominance"] = float(dom["btc_dom"].iloc[-1])
        # Compare latest snapshot to the oldest within ~7 days, if available.
        cutoff = int(dom["ts"].iloc[-1]) - 7 * 86400
        window = dom[dom["ts"] >= cutoff]
        if len(window) >= 2:
            change = float(window["btc_dom"].iloc[-1]) - float(window["btc_dom"].iloc[0])
            if change > 0.3:
                out["dominance_7d_trend"], out["dominance_trend_label"] = 1.0, "RISING"
            elif change < -0.3:
                out["dominance_7d_trend"], out["dominance_trend_label"] = -1.0, "FALLING"

    return out


# ---------------------------------------------------------------------------
# Regime labels (computed, not external)
# ---------------------------------------------------------------------------
def volatility_regime(realized_vol_24h: float, realized_vol_30d: float) -> str:
    """Classify the volatility regime from 24h vs 30d realized vol.

    HIGH when 24h vol exceeds 1.3x the 30d average, LOW when below 0.7x,
    else NORMAL.
    """
    if realized_vol_30d <= 1e-9:
        return "NORMAL"
    ratio = realized_vol_24h / realized_vol_30d
    if ratio >= 1.3:
        return "HIGH"
    if ratio <= 0.7:
        return "LOW"
    return "NORMAL"


def trend_regime(price_vs_ema200: float, ema50_slope: float) -> str:
    """Classify the trend regime from price-vs-EMA200 and EMA50 slope.

    UPTREND: price above EMA200 and EMA50 sloping up.
    DOWNTREND: price below EMA200 and EMA50 sloping down.
    Otherwise SIDEWAYS.
    """
    above = price_vs_ema200 > 0.005
    below = price_vs_ema200 < -0.005
    rising = ema50_slope > 1e-4
    falling = ema50_slope < -1e-4
    if above and rising:
        return "UPTREND"
    if below and falling:
        return "DOWNTREND"
    return "SIDEWAYS"


def regime_label(feats: dict[str, Any]) -> dict[str, str]:
    """Derive volatility, trend, and combined regime labels from features.

    Returns:
        Dict with ``vol_regime``, ``trend_regime``, ``combined_regime``
        (e.g. ``"DOWNTREND_HIGH"``).
    """
    vol = volatility_regime(feats.get("realized_vol_24h", 0.0),
                            feats.get("realized_vol_30d", 0.0))
    trend = trend_regime(feats.get("price_vs_ema200", 0.0),
                         feats.get("ema50_slope", 0.0))
    return {
        "vol_regime": vol,
        "trend_regime": trend,
        "combined_regime": f"{trend}_{vol}",
    }


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------
def compute_features(symbol: str, conn: sqlite3.Connection) -> dict[str, Any]:
    """Assemble the full feature vector for the latest closed 1h candle.

    Combines price, derivatives, sentiment, and computed regime features.
    Raises ``ValueError`` if there is insufficient candle history.

    Args:
        symbol: Compact symbol such as ``"BTCUSDT"``.
        conn: Open SQLite connection to ``market.db``.

    Returns:
        A flat dict of features. ``symbol`` and ``open_time`` are included as
        metadata; the regime keys carry the routing label.
    """
    df = load_candles(symbol, config.PRIMARY_TF, config.CANDLE_HISTORY, conn)
    min_bars = max(config.EMA_PERIODS) + 5
    if len(df) < min_bars:
        raise ValueError(
            f"Not enough {config.PRIMARY_TF} candles for {symbol}: "
            f"have {len(df)}, need >= {min_bars}"
        )

    feats: dict[str, Any] = {
        "symbol": symbol,
        "open_time": int(df["open_time"].iloc[-1]),
    }
    feats.update(price_features(df))
    feats.update(derivatives_features(symbol, conn))
    feats.update(sentiment_features(conn))
    feats.update(regime_label(feats))
    return feats


def build_feature_frame(df: pd.DataFrame) -> dict[str, Any]:
    """Compute price + regime features directly from an OHLCV DataFrame.

    Convenience helper for offline testing without a database: it runs the
    price feature pipeline and attaches regime labels, leaving derivatives and
    sentiment at their neutral defaults so strategies can still be exercised.

    Args:
        df: Ascending OHLCV DataFrame.

    Returns:
        A feature dict (price + regime + neutral derivatives/sentiment).
    """
    feats: dict[str, Any] = {"symbol": "SYNTH", "open_time": 0}
    feats.update(price_features(df))
    feats.update({
        "funding_rate": 0.0, "funding_zscore": 0.0,
        "funding_sign_change": 0.0, "oi_change_pct": 0.0,
        "fear_greed": 50.0, "fear_greed_bucket": "NEUTRAL_GREED",
        "btc_dominance": 0.0, "dominance_7d_trend": 0.0,
        "dominance_trend_label": "FLAT",
    })
    feats.update(regime_label(feats))
    return feats
