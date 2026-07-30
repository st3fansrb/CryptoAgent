"""Paper trading engine for CryptoAgent (Phase 1).

Simulates Binance *spot* execution with realistic frictions and enforces a set
of hard-coded risk limits that the strategy/agent can never bypass.

Shorts: real spot cannot short, but the brief calls for a regime-adaptive
long/short agent. SHORT is therefore modelled as a *simulated directional*
paper position (PnL inverts vs LONG). This is clearly a simulation that real
spot could not execute and would require margin/perps live.

All mutable state (equity, cash, open positions, day/week anchors, halt flags)
is persisted to SQLite so the run-once orchestrator survives between hourly
invocations.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any

import config
from data.logger import log_completed_trade, log_trade
from data.pipeline import get_connection, init_db


def _utc_now_ms() -> int:
    """Current UTC time in epoch milliseconds."""
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def _day_key(dt: datetime) -> str:
    """UTC calendar-day key, e.g. ``"2026-06-23"``."""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d")


def _week_key(dt: datetime) -> str:
    """UTC ISO week key, e.g. ``"2026-W26"``."""
    iso = dt.astimezone(timezone.utc).isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


class PaperEngine:
    """Stateful paper-trading engine backed by SQLite.

    The engine loads (or initializes) portfolio state on construction and
    writes it back after every mutating operation, so a fresh instance in the
    next run-once cycle resumes exactly where the previous one left off.
    """

    def __init__(self, conn: sqlite3.Connection | None = None,
                 now: datetime | None = None,
                 enable_chroma: bool = True) -> None:
        """Load or initialize portfolio state.

        Args:
            conn: Optional shared SQLite connection (one is opened if omitted).
            now: Optional injected UTC time (for deterministic tests).
            enable_chroma: When False, completed trades are written to SQLite
                only (no ChromaDB). Used by the backtester for speed/isolation.
        """
        self._own_conn = conn is None
        self.conn = conn or get_connection()
        init_db(self.conn)
        self._now = now or datetime.now(timezone.utc)
        self.enable_chroma = enable_chroma
        self._load_state()

    # -- state management ---------------------------------------------------
    def _load_state(self) -> None:
        """Load portfolio state from SQLite, seeding defaults on first run."""
        row = self.conn.execute(
            "SELECT * FROM portfolio_state WHERE id = 1"
        ).fetchone()
        if row is None:
            self.equity = config.STARTING_CAPITAL
            self.cash = config.STARTING_CAPITAL
            self.day_anchor = config.STARTING_CAPITAL
            self.week_anchor = config.STARTING_CAPITAL
            self.day_key = _day_key(self._now)
            self.week_key = _week_key(self._now)
            self.trading_halted = False
            self.halt_reason = None
            self._save_state()
        else:
            self.equity = float(row["equity"])
            self.cash = float(row["cash"])
            self.day_anchor = float(row["day_anchor"])
            self.week_anchor = float(row["week_anchor"])
            self.day_key = row["day_key"]
            self.week_key = row["week_key"]
            self.trading_halted = bool(row["trading_halted"])
            self.halt_reason = row["halt_reason"]
        self._roll_periods()

    def _save_state(self) -> None:
        """Persist the current portfolio state to SQLite (single-row upsert)."""
        self.conn.execute(
            """
            INSERT INTO portfolio_state (
                id, equity, cash, day_anchor, week_anchor, day_key, week_key,
                trading_halted, halt_reason, updated_at
            ) VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                equity=excluded.equity, cash=excluded.cash,
                day_anchor=excluded.day_anchor, week_anchor=excluded.week_anchor,
                day_key=excluded.day_key, week_key=excluded.week_key,
                trading_halted=excluded.trading_halted,
                halt_reason=excluded.halt_reason, updated_at=excluded.updated_at
            """,
            (
                self.equity, self.cash, self.day_anchor, self.week_anchor,
                self.day_key, self.week_key, int(self.trading_halted),
                self.halt_reason, _utc_now_ms(),
            ),
        )
        self.conn.commit()

    def _roll_periods(self) -> None:
        """Reset day/week anchors and clear the daily halt on period rollover.

        A new UTC day re-anchors the daily baseline and clears a *daily* halt
        (weekly halts persist until :meth:`reset_halt`). A new ISO week
        re-anchors the weekly baseline.
        """
        cur_day, cur_week = _day_key(self._now), _week_key(self._now)
        changed = False
        if cur_day != self.day_key:
            self.day_key = cur_day
            self.day_anchor = self.equity
            if self.halt_reason == "DAILY_LOSS_LIMIT":
                self.trading_halted = False
                self.halt_reason = None
            changed = True
        if cur_week != self.week_key:
            self.week_key = cur_week
            self.week_anchor = self.equity
            changed = True
        if changed:
            self._save_state()

    # -- accessors ----------------------------------------------------------
    def get_equity(self) -> float:
        """Return current total equity (cash + unrealized handled at close)."""
        return self.equity

    def get_open_positions(self) -> list[dict[str, Any]]:
        """Return all currently open positions as dicts."""
        rows = self.conn.execute(
            "SELECT * FROM open_positions ORDER BY opened_at ASC"
        ).fetchall()
        return [dict(r) for r in rows]

    def has_position(self, symbol: str) -> bool:
        """Return True if there is an open position for ``symbol``."""
        row = self.conn.execute(
            "SELECT 1 FROM open_positions WHERE symbol = ? LIMIT 1", (symbol,)
        ).fetchone()
        return row is not None

    # -- trade lifecycle ----------------------------------------------------
    def open_position(self, symbol: str, direction: str, price: float,
                      atr: float, features: dict[str, Any],
                      now_ms: int | None = None) -> dict[str, Any]:
        """Open a paper position with risk-checked sizing and ATR-based SL/TP.

        Sizing risks ``MAX_POSITION_PCT`` of equity. Stop-loss is
        ``SL_ATR_MULT * atr`` from entry and take-profit is ``TP_ATR_MULT *
        atr`` from entry (direction-aware). A taker fee and adverse slippage
        are applied to the entry fill.

        The order is REJECTED (no state change) when trading is halted, when a
        macro no-trade window is active, when ``MAX_OPEN_POSITIONS`` is
        reached, when a position already exists for the symbol, or when ATR is
        non-positive.

        Args:
            symbol: Compact symbol such as ``"BTCUSDT"``.
            direction: ``"LONG"`` or ``"SHORT"``.
            price: Reference (mid) price for the fill.
            atr: ATR(14) value used for stop/target distances.
            features: Feature snapshot stored with the position.
            now_ms: Optional injected entry timestamp (ms) for tests.

        Returns:
            A result dict ``{"status": "OPENED"|"REJECTED", ...}``.
        """
        self._roll_periods()
        if self.trading_halted:
            return {"status": "REJECTED", "reason": f"halted:{self.halt_reason}"}

        blocked, label = config.is_no_trade_window(self._now)
        if blocked:
            return {"status": "REJECTED", "reason": f"no_trade_window:{label}"}

        if direction not in ("LONG", "SHORT"):
            return {"status": "REJECTED", "reason": "bad_direction"}
        if atr <= 0 or price <= 0:
            return {"status": "REJECTED", "reason": "invalid_atr_or_price"}
        if len(self.get_open_positions()) >= config.MAX_OPEN_POSITIONS:
            return {"status": "REJECTED", "reason": "max_positions"}
        if self.has_position(symbol):
            return {"status": "REJECTED", "reason": "position_exists"}

        # Apply slippage adversely: pay up on LONG, sell lower on SHORT.
        slip = config.SLIPPAGE
        fill = price * (1 + slip) if direction == "LONG" else price * (1 - slip)

        # Position sizing: notional = equity * MAX_POSITION_PCT (1x leverage).
        size_usd = self.equity * config.MAX_POSITION_PCT
        if size_usd > self.cash:
            size_usd = self.cash
        if size_usd <= 0:
            return {"status": "REJECTED", "reason": "no_cash"}
        qty = size_usd / fill
        entry_fee = size_usd * config.TAKER_FEE

        sl_dist = config.SL_ATR_MULT * atr
        tp_dist = config.TP_ATR_MULT * atr
        if direction == "LONG":
            stop_loss = fill - sl_dist
            take_profit = fill + tp_dist
        else:
            stop_loss = fill + sl_dist
            take_profit = fill - tp_dist
        risk_usd = abs(fill - stop_loss) * qty  # 1R in USD

        ts = now_ms if now_ms is not None else _utc_now_ms()

        # Pay entry fee from cash immediately; reduce equity by the fee.
        self.cash -= entry_fee
        self.equity -= entry_fee

        cur = self.conn.execute(
            """
            INSERT INTO open_positions (
                symbol, direction, size_usd, qty, leverage, entry_price,
                stop_loss, take_profit, entry_fee_usd, risk_usd, opened_at,
                features_json, regime_label, r_price
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                symbol, direction, size_usd, qty, config.DEFAULT_LEVERAGE, fill,
                stop_loss, take_profit, entry_fee, risk_usd, ts,
                json.dumps(features, default=float),
                features.get("combined_regime"), sl_dist,
            ),
        )
        self.conn.commit()
        self._save_state()
        return {
            "status": "OPENED", "position_id": int(cur.lastrowid),
            "symbol": symbol, "direction": direction, "entry_price": fill,
            "size_usd": size_usd, "stop_loss": stop_loss,
            "take_profit": take_profit, "risk_usd": risk_usd,
        }

    def close_position(self, position_id: int, price: float, reason: str,
                       now_ms: int | None = None) -> dict[str, Any]:
        """Close an open position, realize PnL, and log the completed trade.

        Slippage and a taker exit fee are applied to the exit fill. SHORT PnL
        is inverted (simulated directional short). The realized trade is
        written to SQLite + ChromaDB via the logger.

        Args:
            position_id: ``open_positions.id`` of the position to close.
            price: Reference exit price.
            reason: ``"STOP_LOSS"`` | ``"TAKE_PROFIT"`` | ``"MANUAL"`` |
                ``"RISK_HALT"``.
            now_ms: Optional injected exit timestamp (ms) for tests.

        Returns:
            A result dict with realized PnL, or ``{"status": "NOT_FOUND"}``.
        """
        row = self.conn.execute(
            "SELECT * FROM open_positions WHERE id = ?", (position_id,)
        ).fetchone()
        if row is None:
            return {"status": "NOT_FOUND", "position_id": position_id}

        pos = dict(row)
        direction = pos["direction"]
        qty = float(pos["qty"])
        entry = float(pos["entry_price"])

        # Adverse slippage on exit: sell lower on LONG, buy higher on SHORT.
        slip = config.SLIPPAGE
        fill = price * (1 - slip) if direction == "LONG" else price * (1 + slip)
        exit_notional = qty * fill
        exit_fee = exit_notional * config.TAKER_FEE

        if direction == "LONG":
            gross = (fill - entry) * qty
        else:  # SHORT: inverse PnL
            gross = (entry - fill) * qty

        final_net = gross - exit_fee  # entry fee already deducted at open
        # Fold in any PnL already booked from partial exits (net of its fee).
        realized_partial = float(pos["realized_partial_usd"] or 0.0)
        total_pnl = final_net + realized_partial

        size_usd = float(pos["size_usd"])
        pnl_pct = total_pnl / size_usd if size_usd else 0.0
        risk_usd = float(pos["risk_usd"]) or 1e-9
        r_multiple = total_pnl / risk_usd

        # Realize only the remaining leg into cash/equity (partial already booked).
        self.cash += final_net
        self.equity += final_net

        ts_exit = now_ms if now_ms is not None else _utc_now_ms()
        features = json.loads(pos["features_json"])
        total_fee = float(pos["entry_fee_usd"]) + exit_fee
        if pos["partial_done"]:
            reason = f"{reason}+PARTIAL"

        trade_row = {
            "timestamp_entry": int(pos["opened_at"]),
            "timestamp_exit": ts_exit,
            "pair": pos["symbol"],
            "direction": direction,
            "size_usd": size_usd,
            "leverage": float(pos["leverage"]),
            "fee_usd": total_fee,
            "pnl_usd": total_pnl,
            "pnl_pct": pnl_pct,
            "features": features,
            "regime_label": pos["regime_label"],
            "win": 1 if total_pnl > 0 else 0,
            "r_multiple": r_multiple,
            "exit_reason": reason,
        }
        if self.enable_chroma:
            trade_id = log_completed_trade(trade_row, conn=self.conn)
        else:
            trade_id = log_trade(trade_row, conn=self.conn)

        self.conn.execute("DELETE FROM open_positions WHERE id = ?",
                          (position_id,))
        self.conn.commit()
        self._save_state()
        return {
            "status": "CLOSED", "trade_id": trade_id, "position_id": position_id,
            "exit_price": fill, "pnl_usd": total_pnl, "pnl_pct": pnl_pct,
            "r_multiple": r_multiple, "reason": reason,
        }

    def check_exits(self, symbol: str, high: float, low: float,
                    close: float, now_ms: int | None = None
                    ) -> list[dict[str, Any]]:
        """Close any open positions for ``symbol`` hit by their SL/TP this bar.

        Uses the bar's high/low to detect touches. The stop-loss is evaluated
        before the take-profit (conservative: assume the worst path within the
        bar). Returns the list of close results.

        Args:
            symbol: Compact symbol such as ``"BTCUSDT"``.
            high: Bar high.
            low: Bar low.
            close: Bar close (fallback exit reference).
            now_ms: Optional injected exit timestamp (ms) for backtests.

        Returns:
            A list of close-result dicts (possibly empty).
        """
        results = []
        for pos in self.get_open_positions():
            if pos["symbol"] != symbol:
                continue
            direction = pos["direction"]
            sl, tp = float(pos["stop_loss"]), float(pos["take_profit"])
            if direction == "LONG":
                if low <= sl:
                    results.append(self.close_position(pos["id"], sl, "STOP_LOSS", now_ms))
                elif high >= tp:
                    results.append(self.close_position(pos["id"], tp, "TAKE_PROFIT", now_ms))
            else:  # SHORT
                if high >= sl:
                    results.append(self.close_position(pos["id"], sl, "STOP_LOSS", now_ms))
                elif low <= tp:
                    results.append(self.close_position(pos["id"], tp, "TAKE_PROFIT", now_ms))
        return results

    def manage_open_positions(self, symbol: str, high: float, low: float,
                              close: float) -> list[dict[str, Any]]:
        """Apply active management (partial take-profit + breakeven stop).

        Call this AFTER :meth:`check_exits` on the same bar, so the original
        stop/target is resolved first (conservative: never assume we reached
        +1R on a bar that also tagged the original stop). For each still-open
        position whose favorable excursion reaches ``PARTIAL_TP_AT_R`` /
        ``BREAKEVEN_AT_R``:

        * book a partial profit on ``PARTIAL_TP_FRACTION`` of the position at
          the +R level, and
        * move the stop to breakeven on the remainder.

        Both behaviors are independent and disabled when their config value is
        None. Returns a list of management actions taken (for logging).

        Args:
            symbol: Compact symbol such as ``"BTCUSDT"``.
            high: Bar high.
            low: Bar low.
            close: Bar close (unused; kept for signature symmetry).

        Returns:
            A list of action dicts (possibly empty).
        """
        actions = []
        for pos in self.get_open_positions():
            if pos["symbol"] != symbol:
                continue
            r_price = float(pos["r_price"] or 0.0)
            if r_price <= 0:
                continue
            direction = pos["direction"]
            entry = float(pos["entry_price"])
            fav_px = high if direction == "LONG" else low
            fav_r = ((fav_px - entry) if direction == "LONG"
                     else (entry - fav_px)) / r_price

            # 1) Partial take-profit at the +R level.
            if (config.PARTIAL_TP_AT_R is not None and not pos["partial_done"]
                    and fav_r >= config.PARTIAL_TP_AT_R):
                self._take_partial(pos, r_price)
                actions.append({"action": "PARTIAL", "position_id": pos["id"]})
                pos = dict(self.conn.execute(
                    "SELECT * FROM open_positions WHERE id = ?",
                    (pos["id"],)).fetchone())

            # 2) Move stop to breakeven once +R is reached.
            if (config.BREAKEVEN_AT_R is not None and not pos["be_moved"]
                    and fav_r >= config.BREAKEVEN_AT_R):
                self.conn.execute(
                    "UPDATE open_positions SET stop_loss = ?, be_moved = 1 "
                    "WHERE id = ?", (entry, pos["id"]))
                self.conn.commit()
                actions.append({"action": "BREAKEVEN", "position_id": pos["id"]})
        return actions

    def _take_partial(self, pos: dict[str, Any], r_price: float) -> None:
        """Book a partial exit: close ``PARTIAL_TP_FRACTION`` at the +R level.

        Reduces the position's quantity, records the (fee-adjusted) realized
        partial PnL on the row, and credits cash/equity. The remainder stays
        open and is closed later by :meth:`check_exits`.
        """
        direction = pos["direction"]
        entry = float(pos["entry_price"])
        qty = float(pos["qty"])
        level = config.PARTIAL_TP_AT_R * r_price
        exit_px = entry + level if direction == "LONG" else entry - level

        part_qty = qty * config.PARTIAL_TP_FRACTION
        gross = ((exit_px - entry) if direction == "LONG"
                 else (entry - exit_px)) * part_qty
        fee = part_qty * exit_px * config.TAKER_FEE
        net = gross - fee

        self.cash += net
        self.equity += net
        new_qty = qty - part_qty
        new_realized = float(pos["realized_partial_usd"] or 0.0) + net
        self.conn.execute(
            "UPDATE open_positions SET qty = ?, partial_done = 1, "
            "realized_partial_usd = ? WHERE id = ?",
            (new_qty, new_realized, pos["id"]))
        self.conn.commit()
        self._save_state()

    # -- risk enforcement ---------------------------------------------------
    def enforce_risk_limits(self) -> dict[str, Any]:
        """Apply hard-coded daily/weekly loss limits. Always runs each cycle.

        * Daily: equity down > ``DAILY_LOSS_LIMIT`` from the day anchor →
          flatten all positions at their last entry price proxy and halt until
          the next UTC day.
        * Weekly: equity down > ``WEEKLY_LOSS_LIMIT`` from the week anchor →
          halt and require a manual :meth:`reset_halt`.

        Returns:
            A status dict describing any action taken.
        """
        self._roll_periods()
        if self.trading_halted:
            return {"action": "ALREADY_HALTED", "reason": self.halt_reason}

        weekly_dd = (self.equity - self.week_anchor) / self.week_anchor \
            if self.week_anchor else 0.0
        daily_dd = (self.equity - self.day_anchor) / self.day_anchor \
            if self.day_anchor else 0.0

        if weekly_dd <= -config.WEEKLY_LOSS_LIMIT:
            self._flatten_all("RISK_HALT")
            self.trading_halted = True
            self.halt_reason = "WEEKLY_LOSS_LIMIT"
            self._save_state()
            return {"action": "HALT", "reason": "WEEKLY_LOSS_LIMIT",
                    "drawdown": weekly_dd, "manual_reset_required": True}

        if daily_dd <= -config.DAILY_LOSS_LIMIT:
            self._flatten_all("RISK_HALT")
            self.trading_halted = True
            self.halt_reason = "DAILY_LOSS_LIMIT"
            self._save_state()
            return {"action": "HALT", "reason": "DAILY_LOSS_LIMIT",
                    "drawdown": daily_dd, "manual_reset_required": False}

        return {"action": "OK", "daily_dd": daily_dd, "weekly_dd": weekly_dd}

    def _flatten_all(self, reason: str) -> None:
        """Close every open position at its entry price (risk-halt flatten).

        Without a live price feed at halt time we close at entry price as a
        neutral proxy; SL/TP exits during normal bars already realize PnL.
        """
        for pos in self.get_open_positions():
            self.close_position(pos["id"], float(pos["entry_price"]), reason)

    def reset_halt(self) -> None:
        """Manually clear a trading halt (required after a weekly-limit halt)."""
        self.trading_halted = False
        self.halt_reason = None
        self._save_state()

    def summary(self) -> dict[str, Any]:
        """Return a console-friendly snapshot of portfolio state."""
        daily_dd = (self.equity - self.day_anchor) / self.day_anchor \
            if self.day_anchor else 0.0
        weekly_dd = (self.equity - self.week_anchor) / self.week_anchor \
            if self.week_anchor else 0.0
        return {
            "equity": round(self.equity, 2),
            "cash": round(self.cash, 2),
            "open_positions": len(self.get_open_positions()),
            "daily_pnl_pct": round(daily_dd * 100, 2),
            "weekly_pnl_pct": round(weekly_dd * 100, 2),
            "trading_halted": self.trading_halted,
            "halt_reason": self.halt_reason,
        }

    def close(self) -> None:
        """Close the underlying SQLite connection if this engine owns it."""
        if self._own_conn:
            self.conn.close()
