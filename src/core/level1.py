"""Level 1 - fast deterministic technical rules.

Level 1 is intentionally simple and cheap. It consumes recent OHLCV plus
perp-specific data (funding rate, open interest) and emits a score in
`[0.0, 1.0]`, where higher means a stronger risk-on signal.

The Day 1 stub fixes the public surface; concrete indicators are wired
in on Day 2.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class Level1Config:
    """Configuration for Level 1 technical rules."""

    ema_fast: int = 12
    ema_slow: int = 26
    rsi_period: int = 14
    atr_period: int = 14
    # Funding rate above which the agent should be cautious about longs
    funding_long_cap_bps: float = 50.0


class Level1:
    """Technical-rules level of the decision engine."""

    LEVEL = 1

    def __init__(self, config: Level1Config | None = None) -> None:
        self.config = config or Level1Config()

    def score(self, market: dict[str, Any]) -> float:
        """Return a risk-on score in `[0.0, 1.0]`.

        Expected `market` keys (Day 2):
            - "ohlcv": pandas.DataFrame with columns [open, high, low, close, volume]
            - "funding_rate": float (in bps)
            - "open_interest": float (USD notional)
        """
        raise NotImplementedError("Level1.score will be implemented on Day 2")

    def explain(self, market: dict[str, Any]) -> str:
        """Return a short human-readable rationale for the last score."""
        return "Level 1 rationale not implemented yet"
