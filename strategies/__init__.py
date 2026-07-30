"""Strategy layer: rule-based signals and regime-based routing."""

from dataclasses import dataclass
from typing import Any


@dataclass
class Signal:
    """A trade signal emitted by a strategy.

    Attributes:
        symbol: Compact symbol such as ``"BTCUSDT"``.
        direction: ``"LONG"`` or ``"SHORT"``.
        price: Reference price at signal time (latest close).
        atr: ATR(14) value, used by the engine for SL/TP distances.
        reason: Human-readable explanation of why the signal fired.
        features: Feature snapshot at signal time.
    """

    symbol: str
    direction: str
    price: float
    atr: float
    reason: str
    features: dict[str, Any]


class Strategy:
    """Interface every strategy implements.

    Subclasses override :meth:`evaluate` to return a :class:`Signal` or
    ``None``. Parameters may be supplied per-regime by the router.
    """

    name = "base"

    def __init__(self, params: dict[str, Any] | None = None) -> None:
        """Store the (optionally regime-specific) parameter set."""
        self.params = params or {}

    def evaluate(self, features: dict[str, Any]) -> "Signal | None":
        """Evaluate features and return a Signal or None. Override in subclass."""
        raise NotImplementedError
