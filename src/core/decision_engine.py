"""DecisionEngine - cascading Level 1 -> Level 2 (-> Level 3 future).

Day 3 cascade
-------------
1. **Level 1 (technicals)** is consulted first. If L1 blocks the trade
   (any `severity="block"` reason), the engine *short-circuits*:
   - Level 2 is NOT called (we save the API budget and stay honest).
   - Level 3 is skipped if not configured.
   - Final score forced to `0.0` -> Risk-OFF.
2. If L1 passes, **Level 2 (on-chain)** is called.
3. If a real Level 3 client is wired, the engine asks the arbiter for
   a final score from the combined briefing; otherwise the engine
   blends L1 and L2 with the configured weights and a synthetic L3
   that simply re-weights what we already know.

The engine is intentionally side-effect free: it takes a `MarketContext`
dict, returns a `DecisionResult`. The `AllocationRouter` then turns
that decision into on-chain transactions.
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
    short_circuited: bool = False
    short_circuit_reason: str | None = None
    timestamp: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )

    @property
    def action(self) -> str:
        """Convenience accessor matching the directive's action."""
        return self.directive.action

    def level_score(self, level: int) -> LevelScore | None:
        for s in self.level_scores:
            if s.level == level:
                return s
        return None


class DecisionEngine:
    """Cascading three-level decision engine."""

    def __init__(
        self,
        level1: Level1,
        level2: Level2,
        level3: Level3 | None = None,
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
        l1_score = await self.level1.score(context)

        # ---- Short-circuit: L1 blocked -> force risk-off, skip L2/L3 ----
        l1_blocked = bool(l1_score.raw.get("l1", {}).get("passes") is False)
        if l1_blocked:
            return self._short_circuited(l1_score)

        # ---- Level 2 -----------------------------------------------------
        l2_score = await self.level2.score(context)

        # ---- Level 3 (optional today) -----------------------------------
        l3_score = await self._maybe_level3(l1_score, l2_score, context)

        scores = [l1_score, l2_score, l3_score]
        final_score = self._aggregate(scores)
        directive = self._build_directive(final_score, scores)
        regime = (
            l2_score.raw.get("l2", {}).get("regime")
            or directive.action.replace("_", "-")
        )
        return DecisionResult(
            final_score=final_score,
            regime=regime,
            directive=directive,
            level_scores=scores,
            weights=self.weights,
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _short_circuited(self, l1_score: LevelScore) -> DecisionResult:
        l1_block_msg = l1_score.rationale or "Level 1 blocked the trade"
        directive = ExecutionDirective(
            action="risk_off",
            side=None,
            intensity=1.0,
            rationale=f"L1 short-circuit: {l1_block_msg}",
        )
        # Carry an explicit zero L2 + L3 to keep DecisionResult.level_scores
        # well-typed for downstream consumers / panels.
        scores = [
            l1_score,
            LevelScore(
                level=2,
                score=0.0,
                rationale="skipped (L1 short-circuit)",
                raw={"l2": {"skipped": True}},
            ),
            LevelScore(
                level=3,
                score=0.0,
                rationale="skipped (L1 short-circuit)",
                raw={"l3": {"skipped": True}},
            ),
        ]
        return DecisionResult(
            final_score=0.0,
            regime="risk-off",
            directive=directive,
            level_scores=scores,
            weights=self.weights,
            short_circuited=True,
            short_circuit_reason=l1_block_msg,
        )

    async def _maybe_level3(
        self,
        l1_score: LevelScore,
        l2_score: LevelScore,
        context: dict[str, Any],
    ) -> LevelScore:
        if self.level3 is None:
            # Synthetic L3 = re-weight of L1 + L2 so the aggregation math
            # still has three terms summing to 1.0 of weight.
            value = 0.5 * l1_score.score + 0.5 * l2_score.score
            return LevelScore(
                level=3,
                score=value,
                rationale="L3 placeholder (Gemini arbiter wired Day 4)",
                raw={"l3": {"synthetic": True}},
            )

        briefing = ArbiterBriefing(
            level1_score=l1_score.score,
            level1_rationale=l1_score.rationale,
            level2_score=l2_score.score,
            level2_rationale=l2_score.rationale,
            market_snapshot=context,
        )
        return await self.level3.score(briefing)

    def _aggregate(self, scores: list[LevelScore]) -> float:
        weights_by_level = {
            1: self.weights["level1"],
            2: self.weights["level2"],
            3: self.weights["level3"],
        }
        return float(
            sum(s.score * weights_by_level[s.level] for s in scores)
        )

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
            # Side comes from L2's market heat tilt: positive 24h price
            # change on majority of symbols -> long; negative -> short.
            side = self._infer_side(scores) or "long"
            return ExecutionDirective(
                action="risk_on",
                side=side,
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

    def _infer_side(self, scores: list[LevelScore]) -> str | None:
        for s in scores:
            if s.level != 2:
                continue
            per_symbol = s.raw.get("l2", {}).get("per_symbol", {}) or {}
            bull = 0
            bear = 0
            for entry in per_symbol.values():
                change = (entry.get("volume") or {}).get("price_change_pct_24h", 0.0)
                if change > 0:
                    bull += 1
                elif change < 0:
                    bear += 1
            if bull > bear:
                return "long"
            if bear > bull:
                return "short"
        return None
