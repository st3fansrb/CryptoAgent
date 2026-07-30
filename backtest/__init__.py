"""Backtesting layer: historical download + bar-by-bar replay engine.

Reuses the SAME feature math, regime router, strategy, and paper-engine risk
logic as live trading, so a positive backtest is a meaningful signal about the
live system (not a separate, divergent code path).
"""
