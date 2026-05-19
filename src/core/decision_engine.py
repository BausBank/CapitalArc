"""DecisionEngine - aggregates Level 1, Level 2 and Level 3 into a directive.

The engine is intentionally side-effect free: it takes a `MarketContext`,
asks each level for a `LevelScore`, blends them with configured weights,
maps the final score to a concrete `ExecutionDirective`, and returns a
`DecisionResult`. The `AllocationRouter` then turns that directive into
on-chain transactions.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from src.core.level1 import Level1
from src.core.level2 import Level2
from src.core.level3 import ArbiterBriefing, Level3


@dataclass
class LevelScore:
    """Score returned by a single level."""

    level: int
    score: float
    rationale: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class ExecutionDirective:
    """Concrete instruction the engine hands to the allocation router."""

    action: str  # "risk_on" | "risk_off" | "hold"
    side: str | None = None  # "long" | "short" | None
    intensity: float = 0.0  # in [0, 1], how strong the conviction is
    target_size_usd: Decimal | None = None
    target_leverage: Decimal | None = None
    rationale: str = ""


@dataclass
class DecisionResult:
    """Final aggregated decision produced by the engine."""

    final_score: float
    regime: str
    directive: ExecutionDirective
    level_scores: list[LevelScore]
    weights: dict[str, float]
    timestamp: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )

    @property
    def action(self) -> str:
        """Convenience accessor matching the directive's action."""
        return self.directive.action


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

    async def decide(self, context: dict[str, Any]) -> DecisionResult:
        """Produce a `DecisionResult` for the given market context."""
        l1 = await self.level1.score(context)
        l2 = await self.level2.score(context)

        briefing = ArbiterBriefing(
            level1_score=l1.score,
            level1_rationale=l1.rationale,
            level2_score=l2.score,
            level2_rationale=l2.rationale,
            market_snapshot=context,
        )
        l3 = await self.level3.score(briefing)

        scores = [l1, l2, l3]
        final_score = self._aggregate(scores)
        directive = self._build_directive(final_score, scores)

        return DecisionResult(
            final_score=final_score,
            regime=directive.action.replace("_", "-"),
            directive=directive,
            level_scores=scores,
            weights=self.weights,
        )

    def _aggregate(self, scores: list[LevelScore]) -> float:
        weights_by_level = {
            1: self.weights["level1"],
            2: self.weights["level2"],
            3: self.weights["level3"],
        }
        return sum(s.score * weights_by_level[s.level] for s in scores)

    def _build_directive(
        self, final_score: float, scores: list[LevelScore]
    ) -> ExecutionDirective:
        rationale = " | ".join(
            f"L{s.level}={s.score:.2f} ({s.rationale})" for s in scores
        )
        if final_score >= self.risk_on_threshold:
            intensity = min(
                1.0,
                (final_score - self.risk_on_threshold)
                / max(1e-6, 1.0 - self.risk_on_threshold),
            )
            return ExecutionDirective(
                action="risk_on",
                side="long",
                intensity=intensity,
                rationale=rationale,
            )
        if final_score <= self.risk_off_threshold:
            intensity = min(
                1.0,
                (self.risk_off_threshold - final_score)
                / max(1e-6, self.risk_off_threshold),
            )
            return ExecutionDirective(
                action="risk_off",
                side=None,
                intensity=intensity,
                rationale=rationale,
            )
        return ExecutionDirective(
            action="hold",
            side=None,
            intensity=0.0,
            rationale=rationale,
        )
