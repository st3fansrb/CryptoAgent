"""Baseline mean-reversion strategy for CryptoAgent (parameter-driven).

Fades extremes within the current regime. Exits are handled by the paper
engine (ATR stop/target + partial/breakeven management), so this strategy
emits entries only.

All thresholds AND the regimes in which each side is allowed are parameters,
so the regime router can express several strategy *variants* (baseline,
sideways-only longs, short-the-rally in downtrends, fully adaptive) without
changing this code.

LONG  (when trend regime in ``long_regimes``):
    RSI(14) < ``rsi_long`` AND price below lower BB (%b < 0)
    AND funding < ``funding_long_max`` AND Fear&Greed < ``fg_long_max``.

SHORT (when trend regime in ``short_regimes``):
    RSI(14) > ``rsi_short`` AND price above upper BB (%b > 1)
    AND funding > ``funding_short_min`` AND Fear&Greed > ``fg_short_min``.

To "short the rally" in a downtrend regardless of sentiment, set
``funding_short_min`` / ``fg_short_min`` to very negative values so only the
RSI + upper-band conditions gate the entry.
"""

from __future__ import annotations

from typing import Any

from strategies import Signal, Strategy

# Defaults reproduce the original baseline behavior.
DEFAULT_PARAMS: dict[str, Any] = {
    "long_regimes": ["DOWNTREND", "SIDEWAYS"],
    "rsi_long": 32.0,
    "funding_long_max": 0.0001,    # +0.01%
    "fg_long_max": 35.0,
    "allow_long": 1.0,
    "short_regimes": ["DOWNTREND"],
    "rsi_short": 68.0,
    "funding_short_min": 0.00015,  # +0.015%
    "fg_short_min": 55.0,
    "allow_short": 1.0,
}


class BaselineMeanReversion(Strategy):
    """Parameter-driven mean-reversion entry strategy (entries only)."""

    name = "baseline_mean_reversion"

    def __init__(self, params: dict[str, Any] | None = None) -> None:
        """Merge any overrides onto :data:`DEFAULT_PARAMS`."""
        merged = dict(DEFAULT_PARAMS)
        if params:
            merged.update(params)
        super().__init__(merged)

    def evaluate(self, features: dict[str, Any]) -> Signal | None:
        """Return a LONG/SHORT :class:`Signal` if entry rules are met, else None.

        Args:
            features: Feature dict from :func:`data.features.compute_features`.

        Returns:
            A :class:`Signal` or ``None``.
        """
        p = self.params
        trend = features.get("trend_regime", "SIDEWAYS")
        rsi14 = float(features.get("rsi14", 50.0))
        pct_b = float(features.get("bb_pct_b", 0.5))
        funding = float(features.get("funding_rate", 0.0))
        fear_greed = float(features.get("fear_greed", 50.0))
        price = float(features.get("close", 0.0))
        atr = float(features.get("atr", 0.0))

        # LONG: fade oversold extremes in the allowed regimes.
        if p.get("allow_long", 1.0) and trend in p.get("long_regimes", []):
            if (rsi14 < p["rsi_long"]
                    and pct_b < 0.0
                    and funding < p["funding_long_max"]
                    and fear_greed < p["fg_long_max"]):
                return Signal(
                    symbol=features.get("symbol", ""),
                    direction="LONG", price=price, atr=atr,
                    reason=(f"LONG: RSI14={rsi14:.1f} %b={pct_b:.2f} "
                            f"funding={funding:.5f} F&G={fear_greed:.0f} "
                            f"regime={trend}"),
                    features=features,
                )

        # SHORT: fade overbought rallies in the allowed regimes.
        if p.get("allow_short", 1.0) and trend in p.get("short_regimes", []):
            if (rsi14 > p["rsi_short"]
                    and pct_b > 1.0
                    and funding > p["funding_short_min"]
                    and fear_greed > p["fg_short_min"]):
                return Signal(
                    symbol=features.get("symbol", ""),
                    direction="SHORT", price=price, atr=atr,
                    reason=(f"SHORT: RSI14={rsi14:.1f} %b={pct_b:.2f} "
                            f"funding={funding:.5f} F&G={fear_greed:.0f} "
                            f"regime={trend}"),
                    features=features,
                )

        return None
