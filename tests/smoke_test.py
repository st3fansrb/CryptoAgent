"""Offline end-to-end smoke test for CryptoAgent (Phase 1).

Runs the full pipeline with *synthetic* data and NO network access:

    synthetic candles -> features -> regime router -> baseline strategy
                      -> paper engine (open) -> SL/TP exit -> trade log
                      -> ChromaDB ingest -> risk-limit halt

It redirects all persistence to a throwaway temp directory so the project's
real ``market.db`` / ``chroma`` are never touched. Exit code 0 means pass.

Run:
    python tests/smoke_test.py
"""

from __future__ import annotations

import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

# Make the project root importable when run directly.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import config  # noqa: E402


def _redirect_storage_to_temp() -> Path:
    """Point config storage paths at a fresh temp dir and return it."""
    tmp = Path(tempfile.mkdtemp(prefix="cryptoagent_smoke_"))
    config.DATA_DIR = tmp
    config.MARKET_DB = tmp / "market.db"
    config.CHROMA_DIR = tmp / "chroma"
    return tmp


def _synthetic_downtrend_candles(n: int = 260) -> list[list[float]]:
    """Build a declining OHLCV series ending in an oversold capitulation.

    Designed to yield a DOWNTREND regime, RSI(14) < 28, and a close below the
    lower Bollinger Band so the baseline LONG rule fires.
    """
    rng = np.random.default_rng(7)
    trend = np.linspace(120.0, 82.0, n)
    noise = rng.normal(0.0, 0.25, n)
    close = trend + noise
    # Accelerating drop over the final bars (capitulation).
    close[-4:] = close[-5] - np.array([2.0, 4.5, 8.0, 13.0])

    open_ = np.empty(n)
    open_[0] = close[0]
    open_[1:] = close[:-1]
    high = np.maximum(open_, close) + 0.6
    low = np.minimum(open_, close) - 0.6

    start_ms = 1_700_000_000_000  # arbitrary fixed epoch ms
    hour = 3_600_000
    rows = []
    for i in range(n):
        rows.append([
            start_ms + i * hour,
            float(open_[i]), float(high[i]), float(low[i]), float(close[i]),
            1000.0,
        ])
    return rows


def main() -> int:
    """Execute the smoke test, printing PASS/FAIL for each stage."""
    _redirect_storage_to_temp()

    # Imports happen AFTER redirecting paths so they read the temp config.
    from data import pipeline, features as feat
    from data.logger import get_chroma_collection
    from strategies import regime_router
    from trading.paper_engine import PaperEngine

    now = datetime(2026, 6, 23, 10, 0, tzinfo=timezone.utc)  # not a no-trade day
    symbol = "BTCUSDT"

    conn = pipeline.get_connection()
    pipeline.init_db(conn)

    # --- Stage 1: store synthetic data -----------------------------------
    candles = _synthetic_downtrend_candles()
    pipeline.store_candles(symbol, config.PRIMARY_TF, candles, conn=conn)
    # Funding history: flat/negative so the LONG funding filter passes.
    funding_rows = [
        {"symbol": symbol, "funding_time": candles[i][0],
         "funding_rate": -0.00002}
        for i in range(0, len(candles), 8)
    ]
    pipeline.store_funding(funding_rows, conn=conn)
    # Sentiment: Extreme Fear so F&G filter passes (< 30).
    pipeline.store_fear_greed([{"ts": 1_700_000_000, "value": 14,
                                "classification": "Extreme Fear"}], conn=conn)
    print("PASS  stage 1: synthetic data stored")

    # --- Stage 2: features ------------------------------------------------
    f = feat.compute_features(symbol, conn)
    missing = [k for k in feat.NUMERIC_FEATURE_KEYS if k not in f]
    assert not missing, f"missing feature keys: {missing}"
    assert f["trend_regime"] in ("DOWNTREND", "SIDEWAYS", "UPTREND")
    print(f"PASS  stage 2: features computed "
          f"(regime={f['combined_regime']} rsi14={f['rsi14']:.1f} "
          f"%b={f['bb_pct_b']:.2f} atr={f['atr']:.2f})")

    # --- Stage 3: router + strategy fire a LONG signal -------------------
    signal = regime_router.evaluate(f)
    assert signal is not None, (
        "expected a LONG signal but got None. "
        f"regime={f['trend_regime']} rsi14={f['rsi14']:.2f} "
        f"bb_pct_b={f['bb_pct_b']:.3f} funding={f['funding_rate']:.6f} "
        f"fg={f['fear_greed']}"
    )
    assert signal.direction == "LONG"
    assert signal.atr > 0
    print(f"PASS  stage 3: strategy fired {signal.direction} "
          f"(atr={signal.atr:.2f})")

    # --- Stage 4: paper engine opens, sizing respected -------------------
    engine = PaperEngine(conn=conn, now=now)
    start_equity = engine.get_equity()
    order = engine.open_position(symbol, signal.direction, signal.price,
                                 signal.atr, f, now_ms=int(now.timestamp() * 1000))
    assert order["status"] == "OPENED", f"open rejected: {order}"
    assert order["size_usd"] <= start_equity * config.MAX_POSITION_PCT + 1e-6
    assert len(engine.get_open_positions()) == 1
    assert order["take_profit"] > order["entry_price"] > order["stop_loss"]
    print(f"PASS  stage 4: position opened "
          f"(size=${order['size_usd']:.2f} entry={order['entry_price']:.2f} "
          f"sl={order['stop_loss']:.2f} tp={order['take_profit']:.2f})")

    # --- Stage 5: take-profit hit closes the trade -----------------------
    tp = order["take_profit"]
    closes = engine.check_exits(symbol, high=tp + 1.0, low=tp - 5.0, close=tp)
    assert closes and closes[0]["reason"] == "TAKE_PROFIT", f"no TP exit: {closes}"
    assert closes[0]["pnl_usd"] > 0
    assert len(engine.get_open_positions()) == 0
    print(f"PASS  stage 5: TP exit realized "
          f"(pnl=${closes[0]['pnl_usd']:.2f} R={closes[0]['r_multiple']:.2f})")

    # --- Stage 6: trade persisted to SQLite + ChromaDB -------------------
    trade_count = conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
    assert trade_count == 1, f"expected 1 logged trade, got {trade_count}"
    win = conn.execute("SELECT win FROM trades LIMIT 1").fetchone()[0]
    assert win == 1
    chroma_count = get_chroma_collection().count()
    assert chroma_count == 1, f"expected 1 chroma embedding, got {chroma_count}"
    print(f"PASS  stage 6: trade logged (sqlite={trade_count}, "
          f"chroma={chroma_count})")

    # --- Stage 7: daily loss limit halts trading -------------------------
    engine.equity = engine.day_anchor * 0.95  # -5% intraday
    engine._save_state()
    risk = engine.enforce_risk_limits()
    assert risk["action"] == "HALT" and risk["reason"] == "DAILY_LOSS_LIMIT", risk
    assert engine.trading_halted is True
    blocked = engine.open_position(symbol, "LONG", 100.0, 1.0, f)
    assert blocked["status"] == "REJECTED", "halted engine still opened a trade"
    print("PASS  stage 7: daily loss limit halted trading")

    # --- Stage 8: macro no-trade window blocks entries -------------------
    engine.reset_halt()
    fomc = config.NO_TRADE_EVENTS[0][1]
    engine._now = fomc
    rej = engine.open_position(symbol, "LONG", 100.0, 1.0, f,
                               now_ms=int(fomc.timestamp() * 1000))
    assert rej["status"] == "REJECTED" and "no_trade_window" in rej["reason"], rej
    print("PASS  stage 8: macro no-trade window blocked entry")

    conn.close()
    print("\nALL SMOKE TESTS PASSED ✓")
    return 0


if __name__ == "__main__":
    sys.exit(main())
