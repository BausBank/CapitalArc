"""DecisionEngine - cascading Level 1 -> Level 2 (-> Level 3 future).

Conviction vs direction
=======================
This engine separates two orthogonal questions that older versions
collapsed into a single Long-biased scalar:

* **Conviction** (`score`, 0..1, unsigned) - "how strongly do we want
  to act at all?". Aggregated across levels into `final_score`.
* **Direction** (`direction_sign`, -1 / 0 / +1) - "if we act, which
  side?". Aggregated across levels (weighted by conviction so weak
  votes can't drag the side) into `final_direction`.

Why this split matters: previously a bearish market with low heat
(`market_heat = 0.15`) emitted a low aggregated score, which the
router interpreted as "low conviction -> close everything" even though
on-chain it was actually a *high-conviction SHORT setup*. The new
model lets that exact case open a SHORT at full conviction, because
direction and conviction are tracked independently.

Cascade
-------
1. **Level 1 (technicals)** is consulted first. If L1 blocks the trade
   (any `severity="block"` reason), the engine *short-circuits*:
   - Level 2 is NOT called (we save the API budget and stay honest).
   - Level 3 is skipped.
   - Final conviction forced to `0.0` -> Risk-OFF.
2. If L1 passes, **Level 2 (on-chain)** is called.
3. If a real Level 3 client is wired, the engine asks the arbiter for
   a final conviction + direction from the combined briefing.
   Otherwise the engine emits a *synthetic* L3 (re-weight of L1+L2)
   AND - critically - redistributes the synthetic L3 weight back to
   L1+L2 proportionally during aggregation, so the placeholder doesn't
   silently dilute the real signal back into itself.

Decision rules (against aggregated `(conviction, direction)`)
-------------------------------------------------------------
* `conviction >= risk_on_threshold` + `direction != 0`
    -> open in `direction` at full intensity.
* `conviction <= risk_off_threshold` (any direction, incl. neutral)
    -> close all exposure.
* Mid-band (`risk_off < conviction < risk_on`):
    - `|weighted_direction_strength| >= strong_bias_open_strength`
      AND `direction != 0` -> open in `direction` at *reduced*
      intensity (`0.5 * conviction * direction_strength`).
    - Otherwise -> hold.

The L1 short-circuit still takes precedence in every case. All
downstream risk controls (drawdown haircut, vol-targeted sizing,
max-leverage cap) apply identically to longs and shorts.

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
    """Score returned by a single level.

    The `score` field is the level's **conviction** in `[0, 1]` -
    how strongly this level wants the agent to act, *independent of
    direction*. The `direction_sign` field is the level's directional
    vote: `+1 = long`, `-1 = short`, `0 = no opinion`.

    Splitting these two makes the engine symmetric for longs and
    shorts: a low conviction means "stand down", not "be bearish".
    """

    level: int
    score: float  # conviction in [0, 1]
    rationale: str = ""
    raw: dict[str, Any] = field(default_factory=dict)
    direction_sign: int = 0  # -1 (short) | 0 (neutral) | +1 (long)

    @property
    def conviction(self) -> float:
        """Alias of `score`. Keeps reader intent obvious at call-sites."""
        return self.score


@dataclass
class ExecutionDirective:
    """Concrete instruction the engine hands to the allocation router."""

    action: str  # "risk_on" | "risk_off" | "hold"
    side: str | None = None  # "long" | "short" | None
    intensity: float = 0.0  # in [0, 1], how strong the conviction is
    target_size_usd: Decimal | None = None
    target_leverage: Decimal | None = None
    rationale: str = ""
    # Directional read from L2. Carried alongside the directive so the
    # router and the console can render *why* this side was chosen
    # without re-deriving the bias themselves.
    market_bias: str = "neutral"      # "bullish" | "bearish" | "neutral"
    bias_strength: float = 0.0        # 0..1
    # Aggregated conviction (== final_score) and the weighted
    # direction strength `|sum(w_i * conv_i * dir_i) / sum(w_i *
    # conv_i)|` in [0, 1]. Both are surfaced on the directive so the
    # router (vol-scaling) and the console panels can read them
    # without re-deriving from level_scores.
    conviction: float = 0.0
    direction_strength: float = 0.0


@dataclass
class DecisionResult:
    """Final aggregated decision produced by the engine."""

    final_score: float                # aggregate conviction in [0, 1]
    regime: str
    directive: ExecutionDirective
    level_scores: list[LevelScore]
    weights: dict[str, float]         # ORIGINAL weights (pre-redistribution)
    final_direction: int = 0           # -1 / 0 / +1, weighted direction vote
    direction_strength: float = 0.0    # |weighted direction score| in [0, 1]
    # Effective weights used in aggregation. When synthetic L3 is
    # active these differ from the configured `weights` because the
    # placeholder L3's allocation is re-distributed to L1+L2.
    effective_weights: dict[str, float] = field(default_factory=dict)
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
    """Cascading three-level decision engine.

    Combines per-level `(conviction, direction_sign)` votes into a
    single `(final_conviction, final_direction)` pair, then maps that
    pair onto an `ExecutionDirective` (open long / short / close /
    hold) at the configured thresholds.
    """

    def __init__(
        self,
        level1: Level1,
        level2: Level2,
        level3: Level3 | None = None,
        weights: dict[str, float] | None = None,
        risk_on_threshold: float = 0.6,
        risk_off_threshold: float = 0.4,
        short_bias_min_strength: float = 0.35,
        strong_bias_open_strength: float = 0.6,
        redistribute_synthetic_l3_weight: bool = True,
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
        # Minimum directional vote strength (0..1) required to flip a
        # would-be risk-off into a SHORT open and to qualify a
        # mid-band override. The same threshold is reused for shorts
        # and longs since the engine is now symmetric.
        self.short_bias_min_strength = short_bias_min_strength
        # Minimum |weighted_direction| required to open from the
        # mid-band (conviction in (risk_off, risk_on)). Lower => more
        # trades on ambiguous heat; higher => only the cleanest
        # directional setups override hold.
        self.strong_bias_open_strength = strong_bias_open_strength
        # When True and L3 is the synthetic placeholder (no real
        # Gemini wiring), redistribute L3's weight proportionally to
        # L1 + L2 in `_aggregate`. Without this, the placeholder L3
        # silently dilutes the real signal back into itself - 40% of
        # the weight is wasted on a function of L1 + L2 we already
        # have. Defaults to True; flip to False if you want to keep
        # the legacy "synthetic L3 carries its weight" behaviour.
        self.redistribute_synthetic_l3_weight = redistribute_synthetic_l3_weight
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
        effective_weights = self._effective_weights(scores)
        final_score = self._aggregate(scores, effective_weights)
        final_direction, direction_strength = self._aggregate_direction(
            scores, effective_weights
        )
        directive = self._build_directive(
            final_score,
            final_direction,
            direction_strength,
            scores,
        )
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
            final_direction=final_direction,
            direction_strength=direction_strength,
            effective_weights=effective_weights,
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
            conviction=0.0,
            direction_strength=0.0,
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
            final_direction=0,
            direction_strength=0.0,
            effective_weights=dict(self.weights),
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
            # Synthetic L3 = conviction-weighted re-blend of L1 + L2.
            # We compute and surface it for UI/aggregation consistency,
            # but `_effective_weights` will redistribute the L3 weight
            # back to L1+L2 (unless explicitly disabled) so this
            # placeholder doesn't silently dilute the real signal.
            l1c, l2c = float(l1_score.score), float(l2_score.score)
            value = 0.5 * l1c + 0.5 * l2c
            # Direction follows the conviction-weighted sign of L1+L2.
            l1d = int(getattr(l1_score, "direction_sign", 0) or 0)
            l2d = int(getattr(l2_score, "direction_sign", 0) or 0)
            weighted_dir = l1c * l1d + l2c * l2d
            if weighted_dir > 1e-6:
                dsign = 1
            elif weighted_dir < -1e-6:
                dsign = -1
            else:
                dsign = 0
            rationale_tag = (
                "synthetic re-weight of L1+L2 (Gemini arbiter wired Day 4)"
            )
            if self.redistribute_synthetic_l3_weight:
                rationale_tag += " - 0% effective weight (redistributed)"
            return LevelScore(
                level=3,
                score=value,
                rationale=rationale_tag,
                raw={"l3": {"synthetic": True}},
                direction_sign=dsign,
            )

        briefing = ArbiterBriefing(
            level1_score=l1_score.score,
            level1_rationale=l1_score.rationale,
            level2_score=l2_score.score,
            level2_rationale=l2_score.rationale,
            market_snapshot=context,
        )
        return await self.level3.score(briefing)

    def _is_synthetic_l3(self, scores: list[LevelScore]) -> bool:
        for s in scores:
            if s.level == 3 and bool(
                (s.raw.get("l3") or {}).get("synthetic")
            ):
                return True
        return False

    def _effective_weights(
        self, scores: list[LevelScore]
    ) -> dict[str, float]:
        """Return the weights actually used in aggregation.

        When L3 is the synthetic placeholder and redistribution is
        enabled, L3's weight is reallocated proportionally to L1+L2.
        Otherwise, returns the configured weights unchanged.
        """
        w = dict(self.weights)
        if not self.redistribute_synthetic_l3_weight:
            return w
        if not self._is_synthetic_l3(scores):
            return w
        l1w, l2w, l3w = w["level1"], w["level2"], w["level3"]
        denom = l1w + l2w
        if denom <= 0:
            return w
        return {
            "level1": l1w + l3w * (l1w / denom),
            "level2": l2w + l3w * (l2w / denom),
            "level3": 0.0,
        }

    def _aggregate(
        self, scores: list[LevelScore], weights: dict[str, float]
    ) -> float:
        weights_by_level = {
            1: weights["level1"],
            2: weights["level2"],
            3: weights["level3"],
        }
        return float(
            sum(s.score * weights_by_level[s.level] for s in scores)
        )

    def _aggregate_direction(
        self, scores: list[LevelScore], weights: dict[str, float]
    ) -> tuple[int, float]:
        """Weighted directional vote across levels.

        Each level contributes `weight * conviction * direction_sign`.
        Multiplying by conviction means a level that's unsure ("score
        = 0.2") drags the direction less than one that's confident
        ("score = 0.9"). The resulting weighted vote is normalised by
        the sum of `weight * conviction` to produce a value in
        `[-1, +1]`; we then bucket into a sign + magnitude.

        Returns (direction_sign, direction_strength) where
        direction_sign is -1/0/+1 and direction_strength is the
        absolute magnitude of the normalised vote in [0, 1].
        """
        weights_by_level = {
            1: weights["level1"],
            2: weights["level2"],
            3: weights["level3"],
        }
        weighted_vote = 0.0
        weighted_conv = 0.0
        for s in scores:
            w = weights_by_level.get(s.level, 0.0)
            if w <= 0:
                continue
            dsign = int(getattr(s, "direction_sign", 0) or 0)
            weighted_vote += w * s.score * dsign
            weighted_conv += w * s.score
        if weighted_conv <= 1e-9:
            return 0, 0.0
        normalised = weighted_vote / weighted_conv  # in [-1, +1]
        strength = float(min(1.0, abs(normalised)))
        if normalised > self.short_bias_min_strength:
            return 1, strength
        if normalised < -self.short_bias_min_strength:
            return -1, strength
        return 0, strength

    def _build_directive(
        self,
        final_score: float,
        final_direction: int,
        direction_strength: float,
        scores: list[LevelScore],
    ) -> ExecutionDirective:
        rationale = " | ".join(
            f"L{s.level}=conv{s.score:.2f},dir{s.direction_sign:+d} "
            f"({s.rationale})"
            for s in scores
        )
        # L2 bias is still surfaced verbatim on the directive so the
        # console / router can render the on-chain narrative without
        # re-deriving it.
        bias, bias_strength = self._market_bias(scores)
        side_from_dir: str | None = None
        if final_direction > 0:
            side_from_dir = "long"
        elif final_direction < 0:
            side_from_dir = "short"
        dir_tag = (
            f"dir={side_from_dir or 'neutral'}"
            f"(strength={direction_strength:.2f})"
            f" bias={bias}({bias_strength:.2f})"
        )

        # ---- High conviction risk-ON (open / scale a perp position) ----
        # The direction comes from the conviction-weighted vote across
        # all levels, not from L2 alone. If direction is neutral we
        # cannot pick a side - fall through to hold.
        if final_score >= self.risk_on_threshold and side_from_dir is not None:
            intensity = min(
                1.0,
                (final_score - self.risk_on_threshold)
                / max(1e-6, 1.0 - self.risk_on_threshold),
            )
            return ExecutionDirective(
                action="risk_on",
                side=side_from_dir,
                intensity=intensity,
                rationale=f"{dir_tag} | {rationale}",
                market_bias=bias,
                bias_strength=bias_strength,
                conviction=final_score,
                direction_strength=direction_strength,
            )

        # ---- Low conviction risk-OFF: close exposure (side-agnostic) ----
        # "Low conviction" now genuinely means "stand down". A bearish
        # setup with high *on-chain conviction* will have lifted
        # final_score (L2.conviction = max(2*|heat-0.5|, bias_strength))
        # above risk_off and will land in the risk_on branch above with
        # direction = -1. So the only way to get here is a market the
        # engine is honestly unsure about - close everything.
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
                rationale=f"{dir_tag} | {rationale}",
                market_bias=bias,
                bias_strength=bias_strength,
                conviction=final_score,
                direction_strength=direction_strength,
            )

        # ---- Mid-band: strong-direction override --------------------
        # Even when aggregated conviction is mid-band, a sufficiently
        # unanimous directional vote can justify a *reduced-size*
        # entry rather than sitting idle. Intensity is deliberately
        # conservative: `0.5 * conviction * direction_strength`.
        if (
            side_from_dir is not None
            and direction_strength >= self.strong_bias_open_strength
        ):
            intensity = float(
                min(1.0, 0.5 * final_score * direction_strength)
            )
            return ExecutionDirective(
                action="risk_on",
                side=side_from_dir,
                intensity=intensity,
                rationale=(
                    f"STRONG-DIRECTION override {dir_tag} mid-band | "
                    f"{rationale}"
                ),
                market_bias=bias,
                bias_strength=bias_strength,
                conviction=final_score,
                direction_strength=direction_strength,
            )

        # ---- Mid-band: hold current allocation ------------------------
        return ExecutionDirective(
            action="hold",
            side=None,
            intensity=0.0,
            rationale=f"{dir_tag} | {rationale}",
            market_bias=bias,
            bias_strength=bias_strength,
            conviction=final_score,
            direction_strength=direction_strength,
        )

    def _market_bias(
        self, scores: list[LevelScore]
    ) -> tuple[str, float]:
        """Read the directional bias produced by Level 2.

        Returns `(bias, strength)` where `bias` is one of
        `"bullish" | "bearish" | "neutral"` and `strength` is in
        `[0, 1]`. When Level 2 was skipped (L1 short-circuit) or no
        L2 score exists, returns the conservative `("neutral", 0.0)`.
        """
        for s in scores:
            if s.level != 2:
                continue
            l2 = s.raw.get("l2", {}) or {}
            if l2.get("skipped"):
                return "neutral", 0.0
            bias = str(l2.get("market_bias", "neutral"))
            strength = float(l2.get("bias_strength", 0.0) or 0.0)
            if bias not in {"bullish", "bearish", "neutral"}:
                bias = "neutral"
            return bias, max(0.0, min(1.0, strength))
        return "neutral", 0.0
