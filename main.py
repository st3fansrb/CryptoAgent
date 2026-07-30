"""Orchestrator for CryptoAgent (Phase 1) — run-once design.

Executes exactly one decision cycle and exits, so hourly cadence is delegated
to an external scheduler (cron / n8n). All mutable state lives in SQLite, so
repeated invocations resume seamlessly.

One cycle:
    1. Initialize the database and load the paper engine state.
    2. Refresh candles (hourly) and, on a 4h boundary, context data.
    3. Enforce risk limits; bail out early if trading is halted.
    4. For each pair: compute features, check SL/TP exits on the latest bar.
    5. Route on regime, evaluate the strategy, and open a position on a signal
       (subject to no-trade windows and the engine's risk checks).
    6. Print an equity summary.

Usage:
    python main.py --once        # default: one cycle, then exit
    python main.py --dry-run     # compute + evaluate, but place no orders
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone

import config
import notifier
from data import features as feat
from data import pipeline
from trading.paper_engine import PaperEngine


def _is_4h_boundary(now: datetime) -> bool:
    """Return True if ``now`` is on a 4-hour UTC boundary (hour % 4 == 0)."""
    return now.hour % 4 == 0


def run_cycle(dry_run: bool = False, fetch: bool = True,
              now: datetime | None = None) -> dict:
    """Run a single decision cycle and return a summary dict.

    Args:
        dry_run: When True, evaluate signals but never open positions.
        fetch: When True, refresh data from live APIs. Set False for offline
            runs against already-stored data.
        now: Optional injected UTC time (for tests/determinism).

    Returns:
        A summary dict with data, risk, per-pair, and portfolio sections.
    """
    now = now or datetime.now(timezone.utc)
    summary: dict = {"timestamp": now.isoformat(), "pairs": {}}

    conn = pipeline.get_connection()
    pipeline.init_db(conn)
    engine = PaperEngine(conn=conn, now=now)

    # --- 2. Data refresh -------------------------------------------------
    if fetch:
        summary["candles"] = pipeline.run_candle_cycle(conn=conn)
        if _is_4h_boundary(now):
            summary["context"] = pipeline.run_context_cycle(conn=conn)
    else:
        summary["candles"] = "skipped(offline)"

    # --- 3. Risk enforcement --------------------------------------------
    risk = engine.enforce_risk_limits()
    summary["risk"] = risk
    if risk.get("action") == "HALT":
        notifier.notify(
            f"🛑 CryptoAgent HALTED: {risk.get('reason')} "
            f"(drawdown {risk.get('drawdown', 0) * 100:.1f}%). "
            f"Equity ${engine.get_equity():.2f}.")
    if engine.trading_halted:
        summary["halted"] = True
        summary["portfolio"] = engine.summary()
        _print_summary(summary, engine)
        conn.close()
        return summary

    blocked, label = config.is_no_trade_window(now)
    summary["no_trade_window"] = {"blocked": blocked, "label": label}

    # --- 4 & 5. Per-pair: features, exits, signal, entry -----------------
    for symbol in config.PAIRS:
        pair_out: dict = {}
        try:
            f = feat.compute_features(symbol, conn)
        except Exception as exc:  # noqa: BLE001 - skip pair on data shortfall
            pair_out["error"] = f"feature_error: {exc}"
            summary["pairs"][symbol] = pair_out
            continue

        pair_out["regime"] = f.get("combined_regime")
        pair_out["close"] = round(float(f.get("close", 0.0)), 2)

        # Check exits against the latest closed bar's range.
        latest = pipeline.load_candles_tail(conn, symbol)
        if latest is not None:
            exits = engine.check_exits(symbol, latest["high"], latest["low"],
                                       latest["close"])
            pair_out["exits"] = [
                {"reason": e.get("reason"), "pnl_usd": round(e.get("pnl_usd", 0), 2)}
                for e in exits if e.get("status") == "CLOSED"
            ]
            for e in exits:
                if e.get("status") == "CLOSED":
                    icon = "✅" if e.get("pnl_usd", 0) > 0 else "❌"
                    notifier.notify(
                        f"{icon} {symbol} closed ({e.get('reason')}): "
                        f"{e.get('pnl_usd', 0):+.2f} USD "
                        f"({e.get('r_multiple', 0):+.2f}R). "
                        f"Equity ${engine.get_equity():.2f}.")
            # Active management (partial TP + breakeven) on surviving positions.
            engine.manage_open_positions(symbol, latest["high"], latest["low"],
                                         latest["close"])

        # Evaluate the live strategy variant (regime-gated internally).
        from strategies import regime_router
        signal = regime_router.evaluate_variant(f, config.LIVE_VARIANT)
        if signal is None:
            pair_out["signal"] = None
        else:
            pair_out["signal"] = {"direction": signal.direction,
                                  "reason": signal.reason}
            if dry_run:
                pair_out["order"] = {"status": "DRY_RUN"}
            elif blocked:
                pair_out["order"] = {"status": "REJECTED",
                                     "reason": f"no_trade_window:{label}"}
            else:
                result = engine.open_position(
                    symbol=signal.symbol, direction=signal.direction,
                    price=signal.price, atr=signal.atr, features=f,
                )
                pair_out["order"] = result
                if result.get("status") == "OPENED":
                    icon = "🔻" if signal.direction == "SHORT" else "🔺"
                    notifier.notify(
                        f"{icon} {symbol} {signal.direction} opened @ "
                        f"{result['entry_price']:.4f} "
                        f"(SL {result['stop_loss']:.4f} / "
                        f"TP {result['take_profit']:.4f}, "
                        f"${result['size_usd']:.2f}). regime={f.get('combined_regime')}")

        summary["pairs"][symbol] = pair_out

    summary["portfolio"] = engine.summary()
    _print_summary(summary, engine)
    conn.close()
    return summary


def _print_summary(summary: dict, engine: PaperEngine) -> None:
    """Print a human-readable cycle summary to the console."""
    p = engine.summary()
    print("=" * 64)
    print(f"CryptoAgent cycle @ {summary['timestamp']}")
    print(f"  Equity: ${p['equity']:.2f} | Cash: ${p['cash']:.2f} | "
          f"Open: {p['open_positions']}")
    print(f"  Day PnL: {p['daily_pnl_pct']:+.2f}% | "
          f"Week PnL: {p['weekly_pnl_pct']:+.2f}% | "
          f"Halted: {p['trading_halted']} ({p['halt_reason']})")
    ntw = summary.get("no_trade_window", {})
    if ntw.get("blocked"):
        print(f"  NO-TRADE WINDOW active: {ntw.get('label')}")
    for symbol, out in summary.get("pairs", {}).items():
        if "error" in out:
            print(f"  {symbol}: {out['error']}")
            continue
        sig = out.get("signal")
        sig_txt = sig["direction"] if sig else "none"
        order = out.get("order", {})
        order_txt = order.get("status", "-")
        print(f"  {symbol}: regime={out.get('regime')} close={out.get('close')} "
              f"signal={sig_txt} order={order_txt}")
    print("=" * 64)


def main() -> None:
    """CLI entry point: parse args and run a single cycle."""
    parser = argparse.ArgumentParser(description="CryptoAgent run-once orchestrator")
    parser.add_argument("--once", action="store_true",
                        help="Run a single cycle then exit (default behavior).")
    parser.add_argument("--dry-run", action="store_true",
                        help="Evaluate signals but place no orders.")
    parser.add_argument("--no-fetch", action="store_true",
                        help="Skip live API fetches; use stored data only.")
    args = parser.parse_args()

    run_cycle(dry_run=args.dry_run, fetch=not args.no_fetch)


if __name__ == "__main__":
    main()
