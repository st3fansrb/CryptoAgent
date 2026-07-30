"""Regime router for CryptoAgent (Phase 1).

Reads the computed regime label from the feature vector and routes the trade
decision to a strategy plus a regime-specific parameter set:

* DOWNTREND → short-biased params (tighter long filters, more short room).
* SIDEWAYS  → symmetric mean-reversion params.
* UPTREND   → long-biased params (paper-trade only for now; shorts disabled).

Future strategy classes (trend-following, breakout) are declared here as stubs
so the interface is fixed; their bodies are intentionally unimplemented until
later phases.
"""

from __future__ import annotations

from typing import Any

from strategies import Signal, Strategy
from strategies.baseline_mean_reversion import BaselineMeanReversion

# Regime-specific parameter overrides for the baseline mean-reversion strategy.
REGIME_PARAMS: dict[str, dict[str, float]] = {
    # Bearish: make longs harder (deeper oversold) and enable shorts.
    "DOWNTREND": {
        "rsi_long": 28.0,
        "fg_long_max": 30.0,
        "allow_long": 1.0,
        "allow_short": 1.0,
    },
    # Range-bound: symmetric defaults.
    "SIDEWAYS": {
        "rsi_long": 32.0,
        "rsi_short": 68.0,
        "allow_long": 1.0,
        "allow_short": 0.0,  # shorts require a confirmed downtrend
    },
    # Bullish: longs only, and we paper-trade these (skip live, per plan).
    "UPTREND": {
        "rsi_long": 35.0,
        "allow_long": 1.0,
        "allow_short": 0.0,
    },
}


class TrendFollowingStrategy(Strategy):
    """Stub for a future trend-following strategy (interface only)."""

    name = "trend_following"

    def evaluate(self, features: dict[str, Any]) -> Signal | None:
        """Not implemented in Phase 1."""
        raise NotImplementedError("trend_following is a Phase 2+ strategy")


class BreakoutStrategy(Strategy):
    """Stub for a future breakout/momentum strategy (interface only)."""

    name = "breakout"

    def evaluate(self, features: dict[str, Any]) -> Signal | None:
        """Not implemented in Phase 1."""
        raise NotImplementedError("breakout is a Phase 2+ strategy")


# ---------------------------------------------------------------------------
# Strategy variants — full parameter sets used for systematic backtesting.
# Each gates LONG/SHORT by regime via long_regimes/short_regimes, so they can
# be compared head-to-head (see backtest/engine.py --variant).
# ---------------------------------------------------------------------------
# "Short the rally" thresholds: fire shorts on an RSI rebound into the upper
# band during a downtrend, WITHOUT requiring greedy sentiment/funding (which
# never occurs in a fearful bear). Negative gates effectively disable them.
_SHORT_RALLY = {
    "rsi_short": 58.0,
    "funding_short_min": -1.0,   # no funding gate
    "fg_short_min": -1.0,        # no greed gate
    "allow_short": 1.0,
    "short_regimes": ["DOWNTREND"],
}

VARIANT_PARAMS: dict[str, dict[str, Any]] = {
    # Original behavior: buy dips in downtrend+sideways, shorts need greed.
    "baseline": {},
    # Conservative: only fade dips in ranges; no downtrend longs, no shorts.
    "sideways_longs": {
        "long_regimes": ["SIDEWAYS"],
        "allow_short": 0.0,
    },
    # Go WITH the bear: keep baseline longs, add short-the-rally in downtrends.
    "short_rallies": {**_SHORT_RALLY},
    # Fully adaptive: longs only in ranges, shorts on rallies in downtrends.
    "adaptive": {
        "long_regimes": ["SIDEWAYS"],
        **_SHORT_RALLY,
    },
    # Go WITH the bear only: no longs at all, just short rallies in downtrends.
    "short_only": {
        "allow_long": 0.0,
        "long_regimes": [],
        **_SHORT_RALLY,
    },
    # Same, but more selective on the rally (deeper RSI rebound required).
    "short_only_strict": {
        "allow_long": 0.0,
        "long_regimes": [],
        **{**_SHORT_RALLY, "rsi_short": 62.0},
    },
}


def evaluate_variant(features: dict[str, Any],
                     variant: str = "baseline") -> Signal | None:
    """Evaluate a named strategy variant (for backtesting/experiments).

    Args:
        features: Feature dict for the current bar.
        variant: Key into :data:`VARIANT_PARAMS`.

    Returns:
        A :class:`Signal` or ``None``.
    """
    params = VARIANT_PARAMS.get(variant, {})
    return BaselineMeanReversion(params).evaluate(features)


def route(features: dict[str, Any]) -> tuple[Strategy, dict[str, Any]]:
    """Select a strategy + parameter set for the current regime.

    Args:
        features: Feature dict containing a ``trend_regime`` label.

    Returns:
        A ``(strategy, params)`` tuple. In Phase 1 the strategy is always the
        baseline mean-reversion model, parameterized per regime.
    """
    trend = features.get("trend_regime", "SIDEWAYS")
    params = REGIME_PARAMS.get(trend, REGIME_PARAMS["SIDEWAYS"])
    strategy = BaselineMeanReversion(params)
    return strategy, params


def evaluate(features: dict[str, Any]) -> Signal | None:
    """Route on regime and evaluate the chosen strategy in one call.

    Args:
        features: Feature dict from :func:`data.features.compute_features`.

    Returns:
        A :class:`Signal` or ``None``.
    """
    strategy, _params = route(features)
    return strategy.evaluate(features)
