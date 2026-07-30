"""Bar-by-bar backtest engine for CryptoAgent.

Replays historical 1h candles through the EXACT live decision path:

    vectorized features -> regime_router -> baseline strategy
                        -> PaperEngine (same sizing / SL-TP / fees / halts)

Design choices for correctness and speed:

* **No look-ahead.** All indicators (RSI/BB/ATR/EMA/returns/vol) are causal —
  the value at bar ``i`` uses only bars ``<= i``. We precompute them once
  (vectorized) and then read row ``i`` while iterating, which is both fast and
  free of future leakage. Funding and Fear & Greed are joined with a *backward*
  as-of merge (last known value at or before the bar's time).
* **Same risk engine.** Trades go through :class:`PaperEngine` (ChromaDB
  disabled, in-memory SQLite), so backtest PnL reflects the live fee, slippage,
  sizing, and daily/weekly-halt logic — not a re-implementation.

Run:
    python -m backtest.engine                 # all pairs, full stored history
    python -m backtest.engine --symbol BTCUSDT
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timezone

import numpy as np
import pandas as pd
from ta.momentum import RSIIndicator
from ta.trend import EMAIndicator
from ta.volatility import AverageTrueRange, BollingerBands

import config
from data import pipeline
from data.features import fear_greed_bucket
from strategies import regime_router
from trading.paper_engine import PaperEngine

WARMUP_BARS = 210  # need EMA200 + a little slope history before trading


def compute_feature_frame(df: pd.DataFrame, bars_per_day: int = 24) -> pd.DataFrame:
    """Vectorize all price/regime features over the full candle DataFrame.

    Every column is causal (uses only past/current bars), so reading row ``i``
    during the backtest introduces no look-ahead. The realized-volatility
    windows scale with ``bars_per_day`` so the regime labels mean the same
    thing (24h vs 30d) across timeframes.

    Args:
        df: Ascending OHLCV DataFrame (open_time/open/high/low/close/volume).
        bars_per_day: Candles per 24h for the timeframe (24 for 1h, 96 for 15m).

    Returns:
        The same DataFrame with feature + regime columns appended.
    """
    out = df.copy()
    close, high, low = out["close"], out["high"], out["low"]

    out["rsi14"] = RSIIndicator(close, window=config.RSI_PERIOD).rsi()
    out["rsi7"] = RSIIndicator(close, window=config.RSI_FAST_PERIOD).rsi()

    bb = BollingerBands(close, window=config.BB_PERIOD, window_dev=config.BB_STD)
    upper, lower = bb.bollinger_hband(), bb.bollinger_lband()
    band = (upper - lower).replace(0, np.nan)
    out["bb_pct_b"] = ((close - lower) / band).fillna(0.5)

    out["atr"] = AverageTrueRange(high, low, close,
                                  window=config.ATR_PERIOD).average_true_range()
    out["atr"] = out["atr"].fillna(0.0)

    ema50 = EMAIndicator(close, window=50).ema_indicator()
    ema200 = EMAIndicator(close, window=200).ema_indicator()
    out["price_vs_ema200"] = (close / ema200 - 1.0).fillna(0.0)
    out["ema50_slope"] = ((ema50 - ema50.shift(5)) / 5.0 / close).fillna(0.0)

    logret = np.log(close / close.shift(1))
    out["realized_vol_24h"] = logret.rolling(bars_per_day).std().fillna(0.0)
    out["realized_vol_30d"] = logret.rolling(30 * bars_per_day).std().fillna(0.0)

    # Trend regime (vectorized version of features.trend_regime).
    above = out["price_vs_ema200"] > 0.005
    below = out["price_vs_ema200"] < -0.005
    rising = out["ema50_slope"] > 1e-4
    falling = out["ema50_slope"] < -1e-4
    out["trend_regime"] = np.select(
        [above & rising, below & falling], ["UPTREND", "DOWNTREND"], "SIDEWAYS")

    # Volatility regime (vectorized version of features.volatility_regime).
    ratio = out["realized_vol_24h"] / out["realized_vol_30d"].replace(0, np.nan)
    out["vol_regime"] = np.select(
        [ratio >= 1.3, ratio <= 0.7], ["HIGH", "LOW"], "NORMAL")
    out["combined_regime"] = out["trend_regime"] + "_" + out["vol_regime"]
    return out


def _attach_funding_and_sentiment(df: pd.DataFrame, conn: sqlite3.Connection,
                                  symbol: str) -> pd.DataFrame:
    """Backward as-of join funding rate and Fear & Greed onto each candle.

    Uses the last known value at or before each candle's open time, so there is
    no future leakage. Missing values default to neutral (funding 0, F&G 50).
    """
    funding = pd.read_sql_query(
        "SELECT funding_time, funding_rate FROM funding WHERE symbol = ? "
        "ORDER BY funding_time", conn, params=(symbol,))
    fng = pd.read_sql_query(
        "SELECT ts, value FROM sentiment ORDER BY ts", conn)

    df = df.sort_values("open_time").reset_index(drop=True)
    if not funding.empty:
        df = pd.merge_asof(df, funding.rename(columns={"funding_time": "open_time"}),
                           on="open_time", direction="backward")
        df["funding_rate"] = df["funding_rate"].fillna(0.0)
    else:
        df["funding_rate"] = 0.0

    if not fng.empty:
        fng["open_time"] = fng["ts"] * 1000  # sentiment ts is epoch seconds
        fng = fng[["open_time", "value"]].rename(columns={"value": "fear_greed"})
        df = pd.merge_asof(df, fng, on="open_time", direction="backward")
        df["fear_greed"] = df["fear_greed"].fillna(50.0)
    else:
        df["fear_greed"] = 50.0
    return df


def _prepare_frame(symbol: str, conn: sqlite3.Connection,
                   timeframe: str) -> pd.DataFrame | None:
    """Load candles and attach all features for a symbol/timeframe.

    Returns None if there is not enough history (< WARMUP_BARS + 50 bars).
    """
    df = pd.read_sql_query(
        "SELECT open_time, open, high, low, close, volume FROM candles "
        "WHERE symbol = ? AND timeframe = ? ORDER BY open_time", conn,
        params=(symbol, timeframe))
    if len(df) < WARMUP_BARS + 50:
        return None
    df = compute_feature_frame(df, bars_per_day=config.bars_per_day(timeframe))
    return _attach_funding_and_sentiment(df, conn, symbol)


def run_backtest(symbol: str, conn: sqlite3.Connection,
                 start_equity: float = config.STARTING_CAPITAL,
                 start_ms: int | None = None, end_ms: int | None = None,
                 variant: str = "baseline", timeframe: str = "1h") -> dict:
    """Replay stored history for one symbol and return a performance report.

    Indicators are always computed over the FULL stored history so that a
    windowed run (e.g. an out-of-sample period) still has correct lookback —
    only the *trading* loop is restricted to ``[start_ms, end_ms]``.

    Args:
        symbol: Compact symbol such as ``"BTCUSDT"``.
        conn: Connection to the historical database (``market.db``).
        start_equity: Starting paper capital.
        start_ms: Optional inclusive lower bound (epoch ms) for trading.
        end_ms: Optional inclusive upper bound (epoch ms) for trading.

    Returns:
        A report dict (metrics + equity curve summary).
    """
    df = _prepare_frame(symbol, conn, timeframe)
    if df is None:
        return {"symbol": symbol, "timeframe": timeframe,
                "error": f"insufficient {timeframe} history"}

    # Trading window: respect warmup, then clamp to [start_ms, end_ms].
    times = df["open_time"].to_numpy()
    i_start = WARMUP_BARS
    if start_ms is not None:
        i_start = max(i_start, int(np.searchsorted(times, start_ms, side="left")))
    i_end = len(df)
    if end_ms is not None:
        i_end = int(np.searchsorted(times, end_ms, side="right"))
    if i_end - i_start < 50:
        return {"symbol": symbol,
                "error": f"window too small ({i_end - i_start} tradable bars)"}

    # Isolated in-memory engine: same risk logic, no ChromaDB, simulated clock.
    mem = sqlite3.connect(":memory:")
    mem.row_factory = sqlite3.Row
    engine = PaperEngine(conn=mem, enable_chroma=False,
                         now=_bar_dt(int(times[i_start])))
    engine.equity = engine.cash = engine.day_anchor = engine.week_anchor = start_equity
    engine._save_state()

    equity_curve = []
    for i in range(i_start, i_end):
        row = df.iloc[i]
        ts = int(row["open_time"])
        engine._now = _bar_dt(ts)

        engine.enforce_risk_limits()
        engine.check_exits(symbol, float(row["high"]), float(row["low"]),
                           float(row["close"]), now_ms=ts)
        # Active management (partial TP + breakeven) for any surviving position.
        engine.manage_open_positions(symbol, float(row["high"]),
                                     float(row["low"]), float(row["close"]))

        if not engine.trading_halted and not engine.has_position(symbol):
            feats = _row_to_features(row, symbol)
            signal = regime_router.evaluate_variant(feats, variant)
            if signal is not None and signal.atr > 0:
                engine.open_position(symbol, signal.direction, signal.price,
                                     signal.atr, feats, now_ms=ts)

        equity_curve.append(engine.get_equity())

    # Close anything still open at the final bar of the window.
    last = df.iloc[i_end - 1]
    engine.check_exits(symbol, float(last["high"]), float(last["low"]),
                       float(last["close"]), now_ms=int(last["open_time"]))
    for pos in engine.get_open_positions():
        engine.close_position(pos["id"], float(last["close"]), "MANUAL",
                              int(last["open_time"]))
    equity_curve.append(engine.get_equity())

    report = _build_report(symbol, mem, equity_curve, start_equity,
                           df.iloc[i_start:i_end])
    report["variant"] = variant
    report["timeframe"] = timeframe
    report["trades_detail"] = _collect_trades(mem, df, symbol)
    mem.close()
    return report


def _collect_trades(mem: sqlite3.Connection, df: pd.DataFrame,
                    symbol: str) -> list[dict]:
    """Extract per-trade records enriched with the 'why' (MFE/MAE, hold time).

    For each completed trade we attach:

    * the key entry features (regime, RSI, %b, funding, F&G) — *why it entered*;
    * ``exit_reason`` and ``r_multiple`` — *how it ended*;
    * ``mfe_r`` / ``mae_r`` — max favorable / adverse excursion in R units
      while the trade was open, i.e. how far it ran for us before reversing
      (the clearest signal of *why it worked or not*);
    * ``bars_held``.

    This is the labeled training data the learning loop (ChromaDB + Qwen) will
    consume — generated in seconds instead of months of live trading.
    """
    rows = mem.execute("SELECT * FROM trades ORDER BY timestamp_entry").fetchall()
    if not rows:
        return []

    times = df["open_time"].to_numpy()
    highs = df["high"].to_numpy()
    lows = df["low"].to_numpy()

    out = []
    for r in rows:
        rec = dict(r)
        feats = json.loads(rec["features_json"])
        entry_px = float(feats.get("close", 0.0))
        atr = float(feats.get("atr", 0.0))
        one_r = config.SL_ATR_MULT * atr  # price distance of 1R
        direction = rec["direction"]

        a = int(np.searchsorted(times, int(rec["timestamp_entry"]), "left"))
        b = int(np.searchsorted(times, int(rec["timestamp_exit"] or times[-1]), "right"))
        b = max(b, a + 1)
        hi = float(highs[a:b].max())
        lo = float(lows[a:b].min())

        if one_r > 0 and entry_px > 0:
            if direction == "LONG":
                mfe_r = (hi - entry_px) / one_r
                mae_r = (entry_px - lo) / one_r
            else:  # SHORT
                mfe_r = (entry_px - lo) / one_r
                mae_r = (hi - entry_px) / one_r
        else:
            mfe_r = mae_r = 0.0

        out.append({
            "symbol": symbol,
            "direction": direction,
            "regime_label": rec["regime_label"],
            "entry_time": _bar_dt(int(rec["timestamp_entry"])).isoformat(),
            "exit_time": (_bar_dt(int(rec["timestamp_exit"])).isoformat()
                          if rec["timestamp_exit"] else None),
            "bars_held": b - a,
            "rsi14": round(float(feats.get("rsi14", 0.0)), 1),
            "bb_pct_b": round(float(feats.get("bb_pct_b", 0.0)), 3),
            "funding_rate": float(feats.get("funding_rate", 0.0)),
            "fear_greed": float(feats.get("fear_greed", 0.0)),
            "pnl_pct": round(float(rec["pnl_pct"] or 0.0) * 100, 3),
            "r_multiple": round(float(rec["r_multiple"] or 0.0), 3),
            "mfe_r": round(mfe_r, 2),
            "mae_r": round(mae_r, 2),
            "win": int(rec["win"] or 0),
            "exit_reason": rec["exit_reason"],
            "features": feats,
        })
    return out


def run_portfolio_backtest(symbols: list[str], conn: sqlite3.Connection,
                           variant: str = "short_only", timeframe: str = "1h",
                           start_ms: int | None = None,
                           end_ms: int | None = None,
                           start_equity: float = config.STARTING_CAPITAL) -> dict:
    """Backtest ALL symbols on ONE shared account (realistic, correlation-aware).

    Unlike :func:`run_backtest` (which gives each symbol its own capital), this
    runs a single :class:`PaperEngine` across a unified timeline, so the
    ``MAX_OPEN_POSITIONS`` cap, shared equity, and daily/weekly halts apply
    jointly. This reveals the true combined drawdown when correlated alt-shorts
    move together — the risk a per-pair backtest hides.

    Returns a portfolio report with combined metrics, per-pair trade counts,
    peak concurrent positions, and how many signals were blocked by the
    position cap (a crowding/correlation proxy).
    """
    # Precompute per-symbol feature frames keyed by open_time for fast lookup.
    frames: dict[str, dict[int, dict]] = {}
    bps = config.bars_per_day(timeframe)
    for s in symbols:
        df = _prepare_frame(s, conn, timeframe)
        if df is None:
            continue
        recs = df.iloc[WARMUP_BARS:].to_dict("records")
        frames[s] = {int(r["open_time"]): r for r in recs}
    if not frames:
        return {"symbols": symbols, "error": "no symbol had enough history"}

    # Unified timeline across all symbols, clamped to [start_ms, end_ms].
    timeline = sorted({t for d in frames.values() for t in d})
    if start_ms is not None:
        timeline = [t for t in timeline if t >= start_ms]
    if end_ms is not None:
        timeline = [t for t in timeline if t <= end_ms]
    if len(timeline) < 50:
        return {"symbols": symbols, "error": "window too small"}

    mem = sqlite3.connect(":memory:")
    mem.row_factory = sqlite3.Row
    engine = PaperEngine(conn=mem, enable_chroma=False, now=_bar_dt(timeline[0]))
    engine.equity = engine.cash = engine.day_anchor = engine.week_anchor = start_equity
    engine._save_state()

    equity_curve = []
    peak_concurrent = 0
    blocked_by_cap = 0
    for t in timeline:
        engine._now = _bar_dt(t)
        engine.enforce_risk_limits()
        # Manage existing positions on every symbol that printed this bar.
        for s in frames:
            row = frames[s].get(t)
            if row is None:
                continue
            engine.check_exits(s, float(row["high"]), float(row["low"]),
                               float(row["close"]), now_ms=t)
            engine.manage_open_positions(s, float(row["high"]),
                                         float(row["low"]), float(row["close"]))
        # Consider new entries (deterministic symbol order).
        if not engine.trading_halted:
            for s in symbols:
                row = frames.get(s, {}).get(t)
                if row is None or engine.has_position(s):
                    continue
                feats = _row_to_features(row, s)
                signal = regime_router.evaluate_variant(feats, variant)
                if signal is None or signal.atr <= 0:
                    continue
                res = engine.open_position(s, signal.direction, signal.price,
                                           signal.atr, feats, now_ms=t)
                if res.get("reason") == "max_positions":
                    blocked_by_cap += 1
        peak_concurrent = max(peak_concurrent, len(engine.get_open_positions()))
        equity_curve.append(engine.get_equity())

    # Close everything at each symbol's last bar in the window.
    for s in frames:
        times = [t for t in frames[s] if t <= timeline[-1]]
        if not times:
            continue
        row = frames[s][max(times)]
        engine.check_exits(s, float(row["high"]), float(row["low"]),
                           float(row["close"]), now_ms=max(times))
        for pos in engine.get_open_positions():
            if pos["symbol"] == s:
                engine.close_position(pos["id"], float(row["close"]), "MANUAL",
                                      max(times))
    equity_curve.append(engine.get_equity())

    report = _build_portfolio_report(mem, equity_curve, start_equity, timeline,
                                     variant, timeframe, list(frames))
    report["peak_concurrent_positions"] = peak_concurrent
    report["signals_blocked_by_cap"] = blocked_by_cap
    mem.close()
    return report


def _build_portfolio_report(mem, equity_curve, start_equity, timeline, variant,
                            timeframe, symbols) -> dict:
    """Compute combined portfolio metrics + per-pair trade counts."""
    trades = pd.read_sql_query(
        "SELECT pair, pnl_usd, win, r_multiple FROM trades", mem)
    n = len(trades)
    final_equity = equity_curve[-1] if equity_curve else start_equity
    eq = np.array(equity_curve, dtype=float)
    running_max = np.maximum.accumulate(eq) if len(eq) else np.array([start_equity])
    max_dd = float(((eq - running_max) / running_max).min()) if len(eq) else 0.0
    wins = int(trades["win"].sum()) if n else 0
    gp = float(trades.loc[trades["pnl_usd"] > 0, "pnl_usd"].sum()) if n else 0.0
    gl = float(-trades.loc[trades["pnl_usd"] < 0, "pnl_usd"].sum()) if n else 0.0
    pf = (gp / gl) if gl > 0 else float("inf")
    per_pair = (trades.groupby("pair").size().to_dict() if n else {})
    span = (timeline[-1] - timeline[0]) / 86_400_000
    return {
        "mode": "PORTFOLIO", "variant": variant, "timeframe": timeframe,
        "symbols": symbols, "span_days": round(span, 1), "trades": n,
        "per_pair_trades": per_pair,
        "win_rate_pct": round(100 * wins / n, 1) if n else 0.0,
        "avg_r": round(float(trades["r_multiple"].mean()), 3) if n else 0.0,
        "profit_factor": round(pf, 2) if np.isfinite(pf) else None,
        "start_equity": round(start_equity, 2),
        "final_equity": round(final_equity, 2),
        "total_return_pct": round(100 * (final_equity - start_equity) / start_equity, 2),
        "max_drawdown_pct": round(100 * max_dd, 2),
    }


def _row_to_features(row: pd.Series, symbol: str) -> dict:
    """Assemble the feature dict the strategy expects from a precomputed row."""
    fg = float(row["fear_greed"])
    return {
        "symbol": symbol,
        "close": float(row["close"]),
        "atr": float(row["atr"]),
        "rsi14": float(row["rsi14"]) if pd.notna(row["rsi14"]) else 50.0,
        "bb_pct_b": float(row["bb_pct_b"]),
        "funding_rate": float(row["funding_rate"]),
        "fear_greed": fg,
        "fear_greed_bucket": fear_greed_bucket(fg),
        "trend_regime": str(row["trend_regime"]),
        "vol_regime": str(row["vol_regime"]),
        "combined_regime": str(row["combined_regime"]),
        "price_vs_ema200": float(row["price_vs_ema200"]),
        "ema50_slope": float(row["ema50_slope"]),
    }


def _bar_dt(open_time_ms: int) -> datetime:
    """Convert an epoch-ms candle open time to a UTC datetime."""
    return datetime.fromtimestamp(open_time_ms / 1000, tz=timezone.utc)


def _build_report(symbol: str, mem: sqlite3.Connection, equity_curve: list[float],
                  start_equity: float, df: pd.DataFrame) -> dict:
    """Compute summary performance metrics from the in-memory trade log."""
    trades = pd.read_sql_query(
        "SELECT pnl_usd, pnl_pct, win, r_multiple, direction FROM trades", mem)
    n = len(trades)
    final_equity = equity_curve[-1] if equity_curve else start_equity

    eq = np.array(equity_curve, dtype=float)
    running_max = np.maximum.accumulate(eq) if len(eq) else np.array([start_equity])
    max_dd = float(((eq - running_max) / running_max).min()) if len(eq) else 0.0

    wins = int(trades["win"].sum()) if n else 0
    gross_profit = float(trades.loc[trades["pnl_usd"] > 0, "pnl_usd"].sum()) if n else 0.0
    gross_loss = float(-trades.loc[trades["pnl_usd"] < 0, "pnl_usd"].sum()) if n else 0.0
    profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else float("inf")

    span_days = (int(df["open_time"].iloc[-1]) - int(df["open_time"].iloc[0])) / 86_400_000
    return {
        "symbol": symbol,
        "bars": len(df),
        "span_days": round(span_days, 1),
        "trades": n,
        "longs": int((trades["direction"] == "LONG").sum()) if n else 0,
        "shorts": int((trades["direction"] == "SHORT").sum()) if n else 0,
        "win_rate_pct": round(100 * wins / n, 1) if n else 0.0,
        "avg_pnl_pct": round(100 * float(trades["pnl_pct"].mean()), 3) if n else 0.0,
        "avg_r": round(float(trades["r_multiple"].mean()), 3) if n else 0.0,
        "profit_factor": round(profit_factor, 2) if np.isfinite(profit_factor) else None,
        "start_equity": round(start_equity, 2),
        "final_equity": round(final_equity, 2),
        "total_return_pct": round(100 * (final_equity - start_equity) / start_equity, 2),
        "max_drawdown_pct": round(100 * max_dd, 2),
    }


def _print_report(report: dict) -> None:
    """Pretty-print a single-symbol backtest report to the console."""
    print("=" * 60)
    if "error" in report:
        print(f"{report['symbol']}: {report['error']}")
        print("=" * 60)
        return
    r = report
    print(f"Backtest {r['symbol']} [{r.get('variant', 'baseline')} @ "
          f"{r.get('timeframe', '1h')}]  ({r['bars']} bars / {r['span_days']} days)")
    print(f"  Trades: {r['trades']}  (L:{r['longs']} S:{r['shorts']})  "
          f"Win rate: {r['win_rate_pct']}%")
    print(f"  Avg PnL/trade: {r['avg_pnl_pct']}%   Avg R: {r['avg_r']}   "
          f"Profit factor: {r['profit_factor']}")
    print(f"  Equity: ${r['start_equity']} -> ${r['final_equity']}  "
          f"({r['total_return_pct']:+}%)")
    print(f"  Max drawdown: {r['max_drawdown_pct']}%")
    print("=" * 60)


def _run_portfolio_segments(symbols, conn, variant, tf, args, collected) -> None:
    """Run the shared-account portfolio backtest (with optional split) + print."""
    if args.split:
        split_ms = _date_to_ms(args.split)
        print(f"\n### PORTFOLIO {symbols} [{variant} @ {tf}] — IN-SAMPLE ###")
        r_in = run_portfolio_backtest(symbols, conn, variant, tf, end_ms=split_ms)
        r_in["segment"] = "IN_SAMPLE"
        _print_portfolio_report(r_in)
        print(f"### PORTFOLIO [{variant} @ {tf}] — OUT-OF-SAMPLE ###")
        r_out = run_portfolio_backtest(symbols, conn, variant, tf, start_ms=split_ms)
        r_out["segment"] = "OUT_OF_SAMPLE"
        _print_portfolio_report(r_out)
        collected += [r_in, r_out]
    else:
        rep = run_portfolio_backtest(symbols, conn, variant, tf,
                                     start_ms=_date_to_ms(args.from_date),
                                     end_ms=_date_to_ms(args.to_date))
        rep["segment"] = "FULL"
        _print_portfolio_report(rep)
        collected.append(rep)


def _print_portfolio_report(r: dict) -> None:
    """Pretty-print a shared-account portfolio report."""
    print("=" * 60)
    if "error" in r:
        print(f"PORTFOLIO {r.get('symbols')}: {r['error']}")
        print("=" * 60)
        return
    print(f"PORTFOLIO [{r['variant']} @ {r['timeframe']}]  "
          f"({r['span_days']} days, {len(r['symbols'])} pairs)")
    print(f"  Trades: {r['trades']}  per-pair: {r['per_pair_trades']}")
    print(f"  Win rate: {r['win_rate_pct']}%   Avg R: {r['avg_r']}   "
          f"Profit factor: {r['profit_factor']}")
    print(f"  Equity: ${r['start_equity']} -> ${r['final_equity']}  "
          f"({r['total_return_pct']:+}%)")
    print(f"  Max drawdown: {r['max_drawdown_pct']}%   "
          f"Peak concurrent: {r['peak_concurrent_positions']}   "
          f"Blocked by 2-pos cap: {r['signals_blocked_by_cap']}")
    print("=" * 60)


def _date_to_ms(date_str: str | None) -> int | None:
    """Parse a ``YYYY-MM-DD`` (UTC) string to epoch milliseconds, or None."""
    if not date_str:
        return None
    dt = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def main() -> None:
    """CLI: run the backtest for one or all configured pairs.

    With ``--split YYYY-MM-DD`` each pair is evaluated twice: an in-sample run
    up to the split date (for tuning) and an out-of-sample run from the split
    date to the present (the honest test of whether the edge still holds now).
    """
    parser = argparse.ArgumentParser(description="CryptoAgent backtester")
    parser.add_argument("--symbol", default=None,
                        help="Single pair (default: all configured pairs).")
    parser.add_argument("--symbols", default=None,
                        help="Comma-separated pairs to test (overrides --symbol).")
    parser.add_argument("--from", dest="from_date", default=None,
                        help="Trade only from this UTC date (YYYY-MM-DD).")
    parser.add_argument("--to", dest="to_date", default=None,
                        help="Trade only up to this UTC date (YYYY-MM-DD).")
    parser.add_argument("--split", default=None,
                        help="Split date (YYYY-MM-DD): run in-sample before and "
                             "out-of-sample after, separately.")
    parser.add_argument("--variant", default="baseline",
                        help="Strategy variant, or 'all' to run every variant.")
    parser.add_argument("--timeframes", default="1h",
                        help="Comma-separated timeframes to test (e.g. 15m,1h,4h).")
    parser.add_argument("--note", default="",
                        help="Free-text label saved with the run.")
    parser.add_argument("--no-save", action="store_true",
                        help="Do not persist results to backtest/results/.")
    parser.add_argument("--to-chroma", action="store_true",
                        help="Also embed backtest trades into ChromaDB (training "
                             "data for the learning loop).")
    parser.add_argument("--portfolio", action="store_true",
                        help="Trade all pairs on ONE shared account (correlation-"
                             "aware), instead of one independent account per pair.")
    args = parser.parse_args()

    if args.symbols:
        symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    elif args.symbol:
        symbols = [args.symbol]
    else:
        symbols = config.PAIRS
    variants = (list(regime_router.VARIANT_PARAMS) if args.variant == "all"
                else [args.variant])
    timeframes = [t.strip() for t in args.timeframes.split(",") if t.strip()]
    conn = pipeline.get_connection()
    pipeline.init_db(conn)

    collected: list[dict] = []
    for tf in timeframes:
        for variant in variants:
            if args.portfolio:
                _run_portfolio_segments(symbols, conn, variant, tf, args, collected)
                continue
            for symbol in symbols:
                tag = f"{symbol} [{variant} @ {tf}]"
                if args.split:
                    split_ms = _date_to_ms(args.split)
                    print(f"\n### {tag} — IN-SAMPLE (before {args.split}) ###")
                    r_in = run_backtest(symbol, conn, end_ms=split_ms,
                                        variant=variant, timeframe=tf)
                    r_in["segment"] = "IN_SAMPLE"
                    _print_report(r_in)
                    print(f"### {tag} — OUT-OF-SAMPLE (from {args.split}) ###")
                    r_out = run_backtest(symbol, conn, start_ms=split_ms,
                                         variant=variant, timeframe=tf)
                    r_out["segment"] = "OUT_OF_SAMPLE"
                    _print_report(r_out)
                    collected += [r_in, r_out]
                else:
                    rep = run_backtest(symbol, conn, variant=variant, timeframe=tf,
                                       start_ms=_date_to_ms(args.from_date),
                                       end_ms=_date_to_ms(args.to_date))
                    rep["segment"] = "FULL"
                    _print_report(rep)
                    collected.append(rep)
    conn.close()

    if not args.no_save:
        from backtest.results import push_trades_to_chroma, save_reports
        note = args.note or (f"variants={args.variant}")
        path = save_reports(collected, note=note, split=args.split)
        n_trades = sum(len(r.get("trades_detail", [])) for r in collected)
        print(f"\nResults saved  -> {path}")
        print("Aggregate log  -> backtest/results/history.csv")
        print(f"Per-trade log  -> backtest/results/trades_log.csv ({n_trades} trades)")
        if args.to_chroma:
            n = push_trades_to_chroma(path.stem.replace("backtest_", ""), collected)
            print(f"ChromaDB       -> embedded {n} backtest trades for retrieval")


if __name__ == "__main__":
    main()
