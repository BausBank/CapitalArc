"""Level 1 - fast deterministic technical rules.

Level 1 is intentionally simple and cheap. It consumes recent OHLCV plus
perp-specific data (funding rate, open interest) and emits a `LevelScore`
in `[0.0, 1.0]`, where higher means a stronger risk-on signal.

Day 2 keeps Level 1 as a neutral placeholder so the end-to-end pipeline
can run in dry-run mode. Day 3 wires in the real EMA / RSI / ATR stack.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from src.core.decision_engine import LevelScore


@dataclass
class Level1Config:
    """Configuration for Level 1 technical rules."""

    ema_fast: int = 12
    ema_slow: int = 26
    rsi_period: int = 14
    atr_period: int = 14
    funding_long_cap_bps: float = 50.0


class Level1:
    """Technical-rules level of the decision engine."""

    LEVEL = 1

    def __init__(self, config: Level1Config | None = None) -> None:
        self.config = config or Level1Config()

    async def score(self, market: dict[str, Any]) -> "LevelScore":
        """Return a `LevelScore` for Level 1.

        Day 2 stub: returns a neutral 0.5 score with a rationale that
        clearly states the real indicators are not wired yet.
        """
        from src.core.decision_engine import LevelScore

        return LevelScore(
            level=self.LEVEL,
            score=0.5,
            rationale="L1 placeholder (EMA/RSI/ATR not wired yet)",
            raw={"config": self.config.__dict__},
        )
