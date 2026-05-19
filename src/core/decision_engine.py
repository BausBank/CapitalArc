"""DecisionEngine - aggregates Level 1, Level 2 and Level 3 into a single action.

The engine is intentionally synchronous and side-effect free: it takes a
`MarketContext`, asks each level for a score in `[0.0, 1.0]`, blends them
with configured weights, and returns a `DecisionResult`. The allocation
router is responsible for turning that result into on-chain transactions.

This module is a Day 1 stub - method bodies will be filled in over the
following days. The class shape, however, is the public contract that
the rest of the system can already start coding against.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from src.core.level1 import Level1
from src.core.level2 import Level2
from src.core.level3 import Level3


@dataclass
class LevelScore:
    """Score returned by a single level."""

    level: int
    score: float
    rationale: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class DecisionResult:
    """Final aggregated decision produced by the engine."""

    final_score: float
    action: str  # "risk_on" | "risk_off" | "hold"
    regime: str  # human-readable label e.g. "risk-on", "transition", "chop"
    level_scores: list[LevelScore]
    weights: dict[str, float]
    timestamp: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )


class DecisionEngine:
    """Three-level decision engine.

    Parameters
    ----------
    level1, level2, level3 :
        Concrete level implementations. Injected so they can be swapped or
        mocked in tests.
    weights :
        Mapping `{"level1": w1, "level2": w2, "level3": w3}`. Must sum to 1.0.
    risk_on_threshold, risk_off_threshold :
        Cut-offs that map `final_score` to an action.
    """

    def __init__(
        self,
        level1: Level1,
        level2: Level2,
        level3: Level3,
        weights: dict[str, float] | None = None,
        risk_on_threshold: float = 0.6,
        risk_off_threshold: float = 0.4,
    ) -> None:
        self.level1 = level1
        self.level2 = level2
        self.level3 = level3
        self.weights = weights or {
            "level1": 0.25,
            "level2": 0.35,
            "level3": 0.40,
        }
        self.risk_on_threshold = risk_on_threshold
        self.risk_off_threshold = risk_off_threshold
        self._validate_weights()

    def _validate_weights(self) -> None:
        total = sum(self.weights.values())
        if abs(total - 1.0) > 1e-6:
            raise ValueError(
                f"DecisionEngine weights must sum to 1.0, got {total:.4f}"
            )

    def decide(self, context: dict[str, Any]) -> DecisionResult:
        """Produce a `DecisionResult` for the given market context.

        Day 1 stub - real implementation will:
        1. Query each level (potentially in parallel for L2/L3).
        2. Apply hard overrides (drawdown, stale data).
        3. Blend the scores with `self.weights`.
        4. Map the final score to an action using the thresholds.
        """
        raise NotImplementedError(
            "DecisionEngine.decide will be implemented on Day 2"
        )

    def _aggregate(self, scores: list[LevelScore]) -> float:
        weights_by_level = {
            1: self.weights["level1"],
            2: self.weights["level2"],
            3: self.weights["level3"],
        }
        return sum(s.score * weights_by_level[s.level] for s in scores)

    def _score_to_action(self, final_score: float) -> tuple[str, str]:
        if final_score >= self.risk_on_threshold:
            return "risk_on", "risk-on"
        if final_score <= self.risk_off_threshold:
            return "risk_off", "risk-off"
        return "hold", "transition"
