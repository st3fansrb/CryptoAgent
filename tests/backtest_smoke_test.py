"""Offline smoke test for the backtest engine (no network).

Generates a long synthetic 1h series with repeated oversold dips into a
temp database, runs the backtester, and asserts it produces a coherent report
(trades happen, metrics are well-formed, no look-ahead crash).

Run:
    python tests/backtest_smoke_test.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import config  # noqa: E402


def _synthetic_cyclical_candles(n: int = 1500) -> list[list[float]]:
    """Build a long series with a downward drift plus repeated dips/rebounds.

    The oscillations periodically push RSI oversold and price below the lower
    Bollinger band, so the mean-reversion strategy finds multiple setups.
    """
    rng = np.random.default_rng(42)
    t = np.arange(n)
    drift = 120.0 - 0.015 * t                     # slow downtrend
    cycle = 6.0 * np.sin(t / 9.0) + 3.0 * np.sin(t / 31.0)
    shocks = rng.normal(0.0, 1.2, n)
    close = drift + cycle + shocks
    close = np.maximum(close, 5.0)

    open_ = np.empty(n)
    open_[0] = close[0]
    open_[1:] = close[:-1]
    high = np.maximum(open_, close) + np.abs(rng.normal(0.8, 0.3, n))
    low = np.minimum(open_, close) - np.abs(rng.normal(0.8, 0.3, n))

    start_ms = 1_650_000_000_000
    hour = 3_600_000
    return [
        [start_ms + i * hour, float(open_[i]), float(high[i]),
         float(low[i]), float(close[i]), 1000.0]
        for i in range(n)
    ]


def main() -> int:
    """Run the backtest on synthetic data and validate the report."""
    tmp = Path(tempfile.mkdtemp(prefix="cryptoagent_bt_"))
    config.DATA_DIR = tmp
    config.MARKET_DB = tmp / "market.db"
    config.CHROMA_DIR = tmp / "chroma"

    from backtest.engine import run_backtest
    from data import pipeline

    conn = pipeline.get_connection()
    pipeline.init_db(conn)
    symbol = "BTCUSDT"
    pipeline.store_candles(symbol, "1h", _synthetic_cyclical_candles(), conn=conn)
    # Provide some negative funding + low F&G so LONG filters can pass.
    candles = pipeline.load_candles_tail(conn, symbol)
    pipeline.store_fear_greed([{"ts": 1_650_000_000, "value": 18,
                                "classification": "Extreme Fear"}], conn=conn)
    print("PASS  setup: synthetic history stored")

    report = run_backtest(symbol, conn)
    assert "error" not in report, f"backtest errored: {report}"
    assert report["bars"] >= 1000  # tradable bars (full series minus warmup)
    assert report["trades"] > 0, "expected at least one trade in the backtest"
    # Metrics must be well-formed.
    assert 0.0 <= report["win_rate_pct"] <= 100.0
    assert report["max_drawdown_pct"] <= 0.0
    assert isinstance(report["total_return_pct"], float)
    print("PASS  backtest produced a coherent report:")
    for k, v in report.items():
        if k != "trades_detail":
            print(f"        {k}: {v}")

    # Per-trade detail must be captured for agent training.
    detail = report["trades_detail"]
    assert len(detail) == report["trades"], "trade detail count mismatch"
    sample = detail[0]
    for key in ("direction", "regime_label", "rsi14", "mfe_r", "mae_r",
                "exit_reason", "features", "r_multiple"):
        assert key in sample, f"missing per-trade field: {key}"
    assert isinstance(sample["features"], dict) and sample["features"]
    print(f"PASS  per-trade detail captured ({len(detail)} trades, "
          f"e.g. {sample['direction']} regime={sample['regime_label']} "
          f"R={sample['r_multiple']} MFE={sample['mfe_r']} MAE={sample['mae_r']} "
          f"exit={sample['exit_reason']})")

    # Persistence: aggregate + per-trade CSV (redirected to temp, no repo writes).
    import backtest.results as results_mod
    results_mod.RESULTS_DIR = tmp / "results"
    results_mod.HISTORY_CSV = results_mod.RESULTS_DIR / "history.csv"
    report["segment"] = "FULL"
    json_path = results_mod.save_reports([report], note="smoke")
    trades_csv = json_path.parent / "trades_log.csv"
    assert trades_csv.exists(), "per-trade CSV not written"
    print(f"PASS  results persisted (json={json_path.name}, "
          f"trades_log.csv rows={len(trades_csv.read_text().splitlines()) - 1})")

    conn.close()
    print("\nBACKTEST SMOKE TEST PASSED ✓")
    return 0


if __name__ == "__main__":
    sys.exit(main())
