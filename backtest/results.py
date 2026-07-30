"""Persist backtest reports so results survive across runs and sessions.

Every backtest run is written to ``backtest/results/`` as:

* a timestamped JSON file containing the full report list + run metadata
  (the authoritative record of that run), and
* an appended row in ``history.csv`` for quick scanning / comparison of how
  metrics evolve as the strategy is iterated.

This gives both the user and the assistant a durable, reviewable history of
every experiment — essential once we start changing the strategy and need to
know whether a change actually helped.
"""

from __future__ import annotations

import csv
import json
from datetime import datetime, timezone
from pathlib import Path

RESULTS_DIR = Path(__file__).resolve().parent / "results"
HISTORY_CSV = RESULTS_DIR / "history.csv"

_CSV_FIELDS = [
    "run_id", "run_time_utc", "note", "variant", "timeframe", "symbol",
    "segment", "split", "bars", "span_days", "trades", "longs", "shorts",
    "win_rate_pct", "avg_pnl_pct", "avg_r", "profit_factor",
    "total_return_pct", "max_drawdown_pct",
]


def save_reports(reports: list[dict], note: str = "",
                 split: str | None = None) -> Path:
    """Write a backtest run to a JSON file and append rows to history.csv.

    Args:
        reports: List of per-segment report dicts. Each may carry a
            ``"segment"`` key (e.g. ``"FULL"``, ``"IN_SAMPLE"``).
        note: Optional free-text label for the run (e.g. what was changed).
        split: The split date used, if any.

    Returns:
        The path of the JSON file written.
    """
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)
    run_id = now.strftime("%Y%m%dT%H%M%SZ")

    payload = {
        "run_id": run_id,
        "run_time_utc": now.isoformat(),
        "note": note,
        "split": split,
        "reports": reports,
    }
    json_path = RESULTS_DIR / f"backtest_{run_id}.json"
    json_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    write_header = not HISTORY_CSV.exists()
    with HISTORY_CSV.open("a", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=_CSV_FIELDS, extrasaction="ignore")
        if write_header:
            writer.writeheader()
        for rep in reports:
            if "error" in rep:
                continue
            writer.writerow({
                "run_id": run_id, "run_time_utc": now.isoformat(),
                "note": note, "split": split or "",
                "segment": rep.get("segment", "FULL"), **rep,
            })

    save_trades(run_id, reports)
    return json_path


_TRADE_FIELDS = [
    "run_id", "symbol", "segment", "direction", "regime_label",
    "entry_time", "exit_time", "bars_held", "rsi14", "bb_pct_b",
    "funding_rate", "fear_greed", "pnl_pct", "r_multiple", "mfe_r", "mae_r",
    "win", "exit_reason",
]


def save_trades(run_id: str, reports: list[dict]) -> Path | None:
    """Write every individual trade across all segments to a flat CSV.

    This is the per-trade training/inspection record (one row per trade, with
    entry context + outcome + MFE/MAE). Appends to ``trades_log.csv`` so the
    history of all backtests accumulates over time.

    Returns:
        The CSV path, or None if there were no trades.
    """
    rows = []
    for rep in reports:
        for tr in rep.get("trades_detail", []):
            row = {k: tr.get(k) for k in _TRADE_FIELDS if k != "run_id"
                   and k != "segment"}
            row["run_id"] = run_id
            row["segment"] = rep.get("segment", "FULL")
            rows.append(row)
    if not rows:
        return None

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    path = RESULTS_DIR / "trades_log.csv"
    write_header = not path.exists()
    with path.open("a", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=_TRADE_FIELDS, extrasaction="ignore")
        if write_header:
            writer.writeheader()
        writer.writerows(rows)
    return path


def push_trades_to_chroma(run_id: str, reports: list[dict]) -> int:
    """Push backtest trades into the ChromaDB ``trades`` collection.

    Builds the same numeric embedding as live trades so the agent can later
    retrieve similar past situations. IDs are prefixed ``bt-`` to distinguish
    backtest-sourced records from live ones.

    Returns:
        The number of trades embedded.
    """
    from data.logger import log_to_chromadb

    n = 0
    for rep in reports:
        seg = rep.get("segment", "FULL")
        for i, tr in enumerate(rep.get("trades_detail", [])):
            trade_row = {
                "features": tr.get("features", {}),
                "pair": tr.get("symbol", ""),
                "direction": tr.get("direction", ""),
                "regime_label": tr.get("regime_label", ""),
                "pnl_usd": 0.0,
                "pnl_pct": tr.get("pnl_pct", 0.0),
                "win": tr.get("win", 0),
                "exit_reason": tr.get("exit_reason", ""),
                "timestamp_entry": 0,
            }
            log_to_chromadb(trade_row, f"bt-{run_id}-{tr.get('symbol')}-{seg}-{i}")
            n += 1
    return n
