"""Quick status view for CryptoAgent.

Prints the live paper portfolio, any open positions, and the completed-trade
log (each entry with its +/- outcome) from ``data/market.db``. Read-only.

Usage:
    python status.py            # full view
    python status.py --last 20  # only the 20 most recent trades
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone

from data.pipeline import get_connection


def _ts(ms: int | None) -> str:
    """Format an epoch-ms timestamp as a short UTC string."""
    if not ms:
        return "-"
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")


def main() -> None:
    """Print portfolio state, open positions, and the completed-trade log."""
    parser = argparse.ArgumentParser(description="CryptoAgent status")
    parser.add_argument("--last", type=int, default=50,
                        help="How many recent trades to show (default 50).")
    args = parser.parse_args()

    conn = get_connection()

    # --- Portfolio ------------------------------------------------------
    ps = conn.execute(
        "SELECT equity, cash, day_anchor, week_anchor, trading_halted, "
        "halt_reason FROM portfolio_state WHERE id = 1").fetchone()
    print("=" * 70)
    if ps:
        print(f"PORTFOLIO  equity ${ps['equity']:.2f} | cash ${ps['cash']:.2f} | "
              f"halted: {bool(ps['trading_halted'])} "
              f"({ps['halt_reason'] or '-'})")
    else:
        print("PORTFOLIO  (not initialized yet — run the agent once)")

    # --- Open positions -------------------------------------------------
    opens = conn.execute(
        "SELECT symbol, direction, size_usd, entry_price, stop_loss, "
        "take_profit, opened_at FROM open_positions ORDER BY opened_at").fetchall()
    print("-" * 70)
    print(f"OPEN POSITIONS: {len(opens)}")
    for o in opens:
        print(f"  {o['symbol']:<9} {o['direction']:<5} ${o['size_usd']:.2f} "
              f"entry {o['entry_price']:.4f}  SL {o['stop_loss']:.4f}  "
              f"TP {o['take_profit']:.4f}  ({_ts(o['opened_at'])})")

    # --- Completed trades ----------------------------------------------
    trades = conn.execute(
        "SELECT timestamp_entry, timestamp_exit, pair, direction, pnl_usd, "
        "pnl_pct, r_multiple, win, exit_reason FROM trades "
        "ORDER BY timestamp_entry DESC LIMIT ?", (args.last,)).fetchall()
    print("-" * 70)
    print(f"COMPLETED TRADES (last {len(trades)}):")
    if trades:
        print(f"  {'exit time':<17}{'pair':<9}{'dir':<6}{'pnl$':>8}"
              f"{'pnl%':>7}{'R':>7}  reason")
        for t in trades:
            mark = "+" if (t["pnl_usd"] or 0) > 0 else "-"
            print(f"  {_ts(t['timestamp_exit']):<17}{t['pair']:<9}"
                  f"{t['direction']:<6}{mark}{abs(t['pnl_usd'] or 0):>6.2f}"
                  f"{(t['pnl_pct'] or 0) * 100:>7.2f}{t['r_multiple'] or 0:>7.2f}"
                  f"  {t['exit_reason']}")
    else:
        print("  (none yet — the agent enters only on valid setups)")

    # --- Summary stats --------------------------------------------------
    agg = conn.execute(
        "SELECT COUNT(*) n, SUM(win) wins, SUM(pnl_usd) pnl FROM trades"
    ).fetchone()
    n = agg["n"] or 0
    print("-" * 70)
    if n:
        wins = agg["wins"] or 0
        print(f"ALL-TIME: {n} trades | win rate {100 * wins / n:.1f}% | "
              f"net PnL ${agg['pnl'] or 0:+.2f}")
    else:
        print("ALL-TIME: no completed trades yet")
    print("=" * 70)
    conn.close()


if __name__ == "__main__":
    main()
