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
from src.utils.logging import logger


# ---------------------------------------------------------------------------
# L1 block taxonomy - DEFENSIVE FALLBACK ONLY
# ---------------------------------------------------------------------------
# As of Day 5+, hard-vs-soft is declared at the SOURCE: each
# :class:`Level1Reason` carries its own ``is_hard`` flag set by the
# rule that raised it (see ``src/core/level1.py``). The engine reads
# that flag directly; there is no longer a centralised lookup.
#
# This set is kept as a defensive fallback for payloads that arrive
# WITHOUT the ``is_hard`` flag (older test fixtures, third-party
# Level-1 implementations that bypass the dataclass). When the flag
# is present, it ALWAYS wins.
_LEGACY_HARD_BLOCK_CODES: frozenset[str] = frozenset(
    {
        "drawdown_breach",
        "ohlcv_unavailable",
    }
)


def _reason_is_hard(reason: dict[str, Any]) -> bool:
    """Read ``is_hard`` from a serialised L1 reason, with safe fallback.

    Prefer the explicit flag (set by L1 at the source); fall back to
    the legacy code set if the flag is missing. Anything else is
    treated as a soft block - so an unknown future block code is
    eligible for L3 override by default, which is the right
    permissive bias for a system designed to evolve.
    """
    if "is_hard" in reason:
        return bool(reason["is_hard"])
    return str(reason.get("code")) in _LEGACY_HARD_BLOCK_CODES


# ---------------------------------------------------------------------------
# Stacked-veto intensity haircut
# ---------------------------------------------------------------------------
# When Level 3 overrides Level 1, the engine applies a defensive cap
# on the resulting position intensity that scales with the NUMBER of
# soft blocks being overridden. The intuition is purely risk-mgmt:
# three independent technical vetoes triggering simultaneously is a
# qualitatively different setup from one. The arbiter's *conviction*
# read is respected (we don't second-guess Claude's directional call),
# but the *size* we put on is clamped because compound vetoes compound
# risk.
#
# Cap is multiplied INTO the L3-supplied ``recommended_intensity``;
# we never expand it. So 1 block + intensity=0.9 -> 0.9; 3 blocks +
# intensity=0.9 -> 0.5*0.9 = 0.45. Operators can disable the
# haircut by tuning ``L3_STACKED_VETO_CAPS`` in the engine config.
_DEFAULT_STACKED_VETO_CAPS: dict[int, float] = {
    1: 1.00,   # single soft block - L3 fully trusted
    2: 0.70,   # two stacked blocks - moderate haircut
    3: 0.50,   # three stacked blocks - sharp haircut
    # 4 or more - keep clamping (handled by max() in the helper).
}
_FOUR_PLUS_BLOCKS_CAP: float = 0.35


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
    # True when Level 1 BLOCKED the trade but Level 3 (the Claude
    # arbiter) affirmatively overrode the veto and the engine is now
    # opening a position on L3's authority alone. Used by the console
    # panels to render an explicit "L1 OVERRIDDEN BY L3" badge so the
    # operator never sees a mysterious risk_on after an L1 block.
    l1_overridden_by_l3: bool = False
    # Override audit trail. Populated whenever:
    #   * L3 overrode an L1 block          -> ``status="executed"``
    #   * L3 was invoked but declined      -> ``status="declined"``
    #   * L3 was invoked but L1 was hard   -> ``status="hard_block_uphold"``
    #
    # Contents (informational, all optional):
    #   status, n_soft_blocks, n_hard_blocks, soft_block_codes,
    #   hard_block_codes, raw_intensity, calibrated_intensity,
    #   stacked_veto_cap, l3_conviction, l3_direction,
    #   l3_rationale_snippet.
    #
    # Lets the CLI panel render a self-explanatory override banner
    # and lets backtests reconstruct every L3 override decision
    # without re-running the LLM.
    l1_override_meta: dict[str, Any] | None = None
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
        strong_direction_l1_corroboration_min: float = 0.40,
        redistribute_synthetic_l3_weight: bool = True,
        allow_l3_to_override_l1: bool = True,
        l3_override_min_conviction: float = 0.55,
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
        # ---------- STRONG-DIRECTION L1 corroboration floor ----------
        # In ranging markets L1 emits a HARD-CODED conviction of 0.25
        # for ``trend in {flat, mixed}`` (level1.py:148-154). That is
        # not a real signal - it just means "I am not blocking, but
        # I have no opinion." Without this floor, L2 + L3 alone can
        # drag the mid-band STRONG-DIRECTION override into a LONG
        # entry on flat tape just because positive funding + mild
        # up-drift skew bullish (Day-6 anti-chop diagnosis).
        # When set to a value > 0.25, a mid-band STRONG-DIRECTION
        # entry requires L1 to have moved OFF the flat/mixed plateau
        # (i.e. trend is actually up/down with measurable strength).
        # Default 0.40 is comfortably above the 0.25 plateau but
        # below the typical 0.5-1.0 trend-confirmed band, so a real
        # but weak trend still qualifies. Set to 0.0 to disable.
        self.strong_direction_l1_corroboration_min = float(
            strong_direction_l1_corroboration_min
        )
        # When True and L3 is the synthetic placeholder (no real
        # Gemini wiring), redistribute L3's weight proportionally to
        # L1 + L2 in `_aggregate`. Without this, the placeholder L3
        # silently dilutes the real signal back into itself - 40% of
        # the weight is wasted on a function of L1 + L2 we already
        # have. Defaults to True; flip to False if you want to keep
        # the legacy "synthetic L3 carries its weight" behaviour.
        self.redistribute_synthetic_l3_weight = redistribute_synthetic_l3_weight
        # When True (default) and a real Level 3 is wired, a SOFT L1
        # block does NOT immediately short-circuit the cascade. The
        # engine instead runs L2 + L3 with the full L1 block payload
        # in the briefing (`l1_blocked=True`, reasons, indicators) and
        # only short-circuits if L3 also declines the trade. HARD L1
        # blocks (drawdown / missing data) ignore this flag entirely.
        self.allow_l3_to_override_l1 = allow_l3_to_override_l1
        # Minimum L3 conviction (0..1) required to actually override
        # an L1 block. Anything below this is treated as a decline
        # and the engine short-circuits as if L1 had been honoured.
        self.l3_override_min_conviction = float(l3_override_min_conviction)
        self._validate_weights()

    def _validate_weights(self) -> None:
        total = sum(self.weights.values())
        if abs(total - 1.0) > 1e-6:
            raise ValueError(
                f"DecisionEngine weights must sum to 1.0, got {total:.4f}"
            )

    async def decide(self, context: dict[str, Any]) -> DecisionResult:
        """Produce a `DecisionResult` for the given market context.

        Cascade (Day 5+ - "always invite L3" architecture)
        --------------------------------------------------
        1. Run Level 1.
        2. ALWAYS run Level 2 + Level 3 next, regardless of L1's
           verdict. When L1 blocked, the L3 briefing is enriched with
           the full block reasons + marginality + per-(symbol, tf)
           indicator table so Claude can audit the veto.

           Why "always": the user's design goal is for the arbiter to
           have full situational awareness on every cycle. Even on a
           hard block, Claude's rationale becomes useful operator
           context ("yes, I'm holding because drawdown 11.5% > 10%
           limit; the on-chain mix would have favoured a long").

        3. Engine-level guardrails AFTER L3 runs:
           * **Hard block** -> short-circuit to risk-off regardless
             of L3's verdict. Drawdown is sacred and missing data
             means no honest signal. If L3 tried to risk_on anyway,
             a WARNING is logged ("L3 attempted to override a hard
             block - IGNORED") for operator visibility.
           * **Soft block + override disabled or no real L3 client**
             -> short-circuit. Synthetic L3 can't honestly audit L1
             (it's a function of L1+L2); we never trust it on this
             path.
           * **Soft block + valid L3 override** -> bypass the
             weighted aggregator and hand control to L3's verdict
             directly. Intensity is calibrated by the stacked-veto
             haircut (see ``_l3_overrode_l1``).
           * **Soft block + L3 declined / fell back** -> short-circuit.

        4. L1 passes -> weighted aggregate across all three levels
           (legacy / steady-state path).
        """
        l1_score = await self.level1.score(context)
        l1_raw = l1_score.raw.get("l1", {}) or {}
        l1_blocked = bool(l1_raw.get("passes") is False)
        l1_block_info = self._extract_l1_block_info(l1_raw)

        if l1_blocked:
            # Diagnostic only - the real branching happens in
            # ``_resolve_l1_block`` AFTER L2/L3 have run.
            soft_codes = sorted(
                {
                    r["code"]
                    for r in l1_block_info["reasons"]
                    if not r.get("is_hard")
                }
            )
            hard_codes = sorted(
                {
                    r["code"]
                    for r in l1_block_info["reasons"]
                    if r.get("is_hard")
                }
            )
            logger.info(
                "L1 BLOCKED | hard={} soft={} -> running L2 + L3 anyway "
                "so the arbiter receives the full briefing.",
                hard_codes or "-",
                soft_codes or "-",
            )

        # ---- Level 2 + Level 3: always run -----------------------------
        # We invoke L2 and L3 unconditionally so the arbiter always
        # has full situational awareness. The engine's guardrails
        # below decide what to *do* with L3's verdict.
        l2_score = await self.level2.score(context)
        l3_score = await self._maybe_level3(
            l1_score, l2_score, context, l1_block_info=l1_block_info
        )

        # ---- L1-block guardrails (run AFTER L3 so we have its read) ----
        if l1_blocked:
            return self._resolve_l1_block(
                l1_score=l1_score,
                l2_score=l2_score,
                l3_score=l3_score,
                l1_block_info=l1_block_info,
            )

        # ---- Normal cascade aggregation ---------------------------------
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

    # ------------------------------------------------------------------
    # L1-block resolution
    # ------------------------------------------------------------------

    def _resolve_l1_block(
        self,
        *,
        l1_score: LevelScore,
        l2_score: LevelScore,
        l3_score: LevelScore,
        l1_block_info: dict[str, Any],
    ) -> DecisionResult:
        """Decide what to do once L3 has weighed in on an L1 block.

        Order of precedence (top wins):

        1. **Hard block** -> short-circuit, regardless of L3. Log a
           WARNING when L3 attempted a risk_on anyway so operators
           can spot prompt-following regressions.
        2. **Override disabled** OR **synthetic / fallback L3** ->
           short-circuit. We never let a synthetic placeholder veto
           a rule-based safety net.
        3. **Valid override** -> hand control to L3 (with the
           stacked-veto intensity haircut applied).
        4. **L3 declined** (hold / low conviction) -> short-circuit.
        """
        reasons = l1_block_info["reasons"]
        has_hard_block = any(r.get("is_hard") for r in reasons)
        soft_codes = sorted(
            {r["code"] for r in reasons if not r.get("is_hard")}
        )
        hard_codes = sorted(
            {r["code"] for r in reasons if r.get("is_hard")}
        )
        l3_raw = (l3_score.raw or {}).get("l3", {}) or {}
        l3_response = l3_raw.get("response") or {}

        # --- 1. Hard block guardrail --------------------------------
        if has_hard_block:
            if str(l3_response.get("regime")) == "risk_on":
                # Belt-and-braces: the prompt orders Claude to HOLD
                # on hard blocks; if it nevertheless tried to override
                # we shout and ignore. This makes prompt-drift loud.
                logger.warning(
                    "L3 attempted to override a HARD L1 block "
                    "(hard_codes={}, l3_conv={:.2f}, l3_dir={}). "
                    "IGNORED - hard blocks are immutable.",
                    hard_codes,
                    float(l3_response.get("conviction", 0.0) or 0.0),
                    str(l3_response.get("direction", "?")),
                )
            override_meta = {
                "status": "hard_block_uphold",
                "n_soft_blocks": len(soft_codes),
                "n_hard_blocks": len(hard_codes),
                "soft_block_codes": soft_codes,
                "hard_block_codes": hard_codes,
                "l3_conviction": float(
                    l3_response.get("conviction", 0.0) or 0.0
                ),
                "l3_direction": str(l3_response.get("direction", "neutral")),
                "l3_rationale_snippet": str(
                    l3_response.get("rationale", "")
                )[:240],
            }
            return self._short_circuited(
                l1_score,
                l2_score=l2_score,
                l3_score=l3_score,
                l1_override_meta=override_meta,
            )

        # --- 2. Override disabled or synthetic / fallback L3 --------
        if not self.allow_l3_to_override_l1 or self.level3 is None:
            return self._short_circuited(
                l1_score,
                l2_score=l2_score,
                l3_score=l3_score,
                l1_override_meta={
                    "status": "declined",
                    "n_soft_blocks": len(soft_codes),
                    "n_hard_blocks": 0,
                    "soft_block_codes": soft_codes,
                    "hard_block_codes": [],
                    "decline_reason": (
                        "override disabled in settings"
                        if not self.allow_l3_to_override_l1
                        else "no real L3 client wired (synthetic placeholder)"
                    ),
                },
            )

        # --- 3 & 4. Valid override? --------------------------------
        override = self._evaluate_l3_override(l3_score)
        if override is None:
            decline_reason = self._explain_decline(l3_raw, l3_response)
            logger.info(
                "L3 declined to override L1 (soft={}) - {}",
                soft_codes,
                decline_reason,
            )
            return self._short_circuited(
                l1_score,
                l2_score=l2_score,
                l3_score=l3_score,
                l1_override_meta={
                    "status": "declined",
                    "n_soft_blocks": len(soft_codes),
                    "n_hard_blocks": 0,
                    "soft_block_codes": soft_codes,
                    "hard_block_codes": [],
                    "decline_reason": decline_reason,
                    "l3_conviction": float(
                        l3_response.get("conviction", 0.0) or 0.0
                    ),
                    "l3_direction": str(
                        l3_response.get("direction", "neutral")
                    ),
                    "l3_rationale_snippet": str(
                        l3_response.get("rationale", "")
                    )[:240],
                },
            )
        return self._l3_overrode_l1(
            l1_score=l1_score,
            l2_score=l2_score,
            l3_score=l3_score,
            override=override,
            l1_block_info=l1_block_info,
        )

    @staticmethod
    def _explain_decline(
        l3_raw: dict[str, Any], l3_response: dict[str, Any]
    ) -> str:
        """Human-readable reason for an L3 override decline."""
        if l3_raw.get("fallback"):
            return "L3 arbiter fell back to safe-HOLD (LLM error)"
        if l3_raw.get("synthetic"):
            return "L3 is the synthetic placeholder (no real OpenRouter)"
        regime = str(l3_response.get("regime", "hold"))
        direction = str(l3_response.get("direction", "neutral"))
        conviction = float(l3_response.get("conviction", 0.0) or 0.0)
        if regime != "risk_on":
            return f"L3 chose regime={regime!r}"
        if direction not in {"long", "short"}:
            return f"L3 returned neutral direction (direction={direction!r})"
        return (
            f"L3 conviction {conviction:.2f} below override floor"
        )

    @staticmethod
    def _stacked_veto_cap(n_soft_blocks: int) -> float:
        """Defensive intensity cap that scales with the number of
        soft blocks being overridden simultaneously.

        Risk-mgmt intuition: three independent technical vetoes
        firing at once is qualitatively different from one. We
        respect L3's conviction (its directional read), but clamp
        the size we put on. Returns a multiplier in (0, 1].
        """
        if n_soft_blocks <= 0:
            return 1.0
        if n_soft_blocks >= 4:
            return _FOUR_PLUS_BLOCKS_CAP
        return _DEFAULT_STACKED_VETO_CAPS.get(n_soft_blocks, _FOUR_PLUS_BLOCKS_CAP)

    def _short_circuited(
        self,
        l1_score: LevelScore,
        *,
        l2_score: LevelScore | None = None,
        l3_score: LevelScore | None = None,
        l1_override_meta: dict[str, Any] | None = None,
    ) -> DecisionResult:
        """Build the safe-HOLD result returned on any L1-driven shutdown.

        When ``l2_score`` / ``l3_score`` are supplied, the engine had
        already run those levels (e.g. on the L3-declined-to-override
        path) and we keep their telemetry in ``level_scores`` so the
        panels can render the full picture. Otherwise we synthesise
        zeroed-out skipped placeholders.
        """
        l1_block_msg = l1_score.rationale or "Level 1 blocked the trade"
        directive = ExecutionDirective(
            action="risk_off",
            side=None,
            intensity=1.0,
            rationale=f"L1 short-circuit: {l1_block_msg}",
            conviction=0.0,
            direction_strength=0.0,
        )
        scores = [
            l1_score,
            l2_score
            or LevelScore(
                level=2,
                score=0.0,
                rationale="skipped (L1 short-circuit)",
                raw={"l2": {"skipped": True}},
            ),
            l3_score
            or LevelScore(
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
            l1_override_meta=l1_override_meta,
        )

    def _extract_l1_block_info(
        self, l1_raw: dict[str, Any]
    ) -> dict[str, Any]:
        """Distil L1's raw payload into the L3-friendly audit packet.

        Returns ``{"reasons": [...], "indicators": [...]}`` where each
        reason carries an ``is_hard`` flag (read directly from the L1
        source via :func:`_reason_is_hard`, with a defensive legacy
        fallback) and each indicator row is the flat dict L3 needs to
        second-guess the technical veto.
        """
        if not l1_raw:
            return {"reasons": [], "indicators": []}
        reasons: list[dict[str, Any]] = []
        per_symbol = l1_raw.get("per_symbol") or {}

        def _normalise(r: dict[str, Any], default_symbol: str | None = None) -> dict[str, Any]:
            return {
                "code": r.get("code"),
                "severity": r.get("severity"),
                "message": r.get("message"),
                "symbol": r.get("symbol") or default_symbol,
                "timeframe": r.get("timeframe"),
                "metadata": r.get("metadata") or {},
                "is_hard": _reason_is_hard(r),
            }

        for r in l1_raw.get("reasons") or []:
            if r.get("severity") != "block":
                continue
            reasons.append(_normalise(r))
        # Deduplicate by (code, symbol, timeframe) - the per-symbol
        # block lists already exist in `reasons` at the top level, but
        # walk per_symbol to make sure we don't miss anything when the
        # raw payload was assembled from a different source.
        seen_keys = {
            (r["code"], r.get("symbol"), r.get("timeframe"))
            for r in reasons
        }
        indicators: list[dict[str, Any]] = []
        for sym, ro in per_symbol.items():
            for br in ro.get("blocking_reasons") or []:
                key = (
                    br.get("code"),
                    br.get("symbol") or sym,
                    br.get("timeframe"),
                )
                if key in seen_keys:
                    continue
                seen_keys.add(key)
                reasons.append(_normalise(br, default_symbol=sym))
            for row in ro.get("rows") or []:
                indicators.append(
                    {
                        "symbol": sym,
                        "timeframe": row.get("timeframe"),
                        "close": float(row.get("close", 0) or 0),
                        "ema_fast": float(row.get("ema_fast", 0) or 0),
                        "ema_slow": float(row.get("ema_slow", 0) or 0),
                        "rsi": float(row.get("rsi", 0) or 0),
                        "atr": float(row.get("atr", 0) or 0),
                        "atr_pct": float(row.get("atr_pct", 0) or 0),
                        "trend": row.get("trend", "?"),
                    }
                )
        return {"reasons": reasons, "indicators": indicators}

    def _evaluate_l3_override(
        self, l3_score: LevelScore
    ) -> dict[str, Any] | None:
        """Return L3's override verdict, or ``None`` if it declined.

        L3 only counts as a valid override when:

        * It was a *real* arbiter call (not the synthetic placeholder
          - which is just a function of L1+L2 and can't honestly
          contradict an L1 block - and not the safe-HOLD fallback,
          which means the LLM errored).
        * It returned ``regime="risk_on"`` with a non-neutral
          ``direction``.
        * Its conviction cleared the operator-configured floor
          (``l3_override_min_conviction``).

        Anything else is treated as "L3 declined to override" and the
        engine respects the L1 block.
        """
        l3_raw = (l3_score.raw or {}).get("l3", {}) or {}
        if l3_raw.get("synthetic") or l3_raw.get("fallback"):
            return None
        response = l3_raw.get("response") or {}
        regime = str(response.get("regime", "hold"))
        direction = str(response.get("direction", "neutral"))
        conviction = float(response.get("conviction", 0.0) or 0.0)
        intensity = float(response.get("recommended_intensity", 0.0) or 0.0)
        if regime != "risk_on":
            return None
        if direction not in {"long", "short"}:
            return None
        if conviction < self.l3_override_min_conviction:
            return None
        return {
            "conviction": conviction,
            "direction": direction,
            "regime": regime,
            "intensity": intensity,
            "rationale": str(response.get("rationale", ""))[:600],
        }

    def _l3_overrode_l1(
        self,
        *,
        l1_score: LevelScore,
        l2_score: LevelScore,
        l3_score: LevelScore,
        override: dict[str, Any],
        l1_block_info: dict[str, Any],
    ) -> DecisionResult:
        """Build the DecisionResult for the L3-overrides-L1 path.

        Two design decisions worth their own paragraphs:

        1. **We bypass the weighted aggregator.** L1 voted
           ``score=0`` (it blocked!), so its 0.25 weight would drag
           the weighted aggregate well below ``RISK_ON_THRESHOLD``
           and the engine would refuse to open even though Claude is
           championing the trade. On the override path we let L3
           carry the decision directly (conviction, direction,
           intensity all come from Claude).

        2. **We apply a stacked-veto intensity haircut.** Three
           independent technical vetoes firing at once is
           qualitatively riskier than one - so the engine clamps
           ``intensity`` by a multiplier that decays with
           ``n_soft_blocks``. Conviction is NOT clamped: we trust
           Claude's directional read but defensively size down.
        """
        l3_dir_sign = 1 if override["direction"] == "long" else -1
        # The L2 panel still needs the bias/strength for the rationale
        # tag - read it directly from L2's raw payload.
        l2_raw = (l2_score.raw or {}).get("l2", {}) or {}
        bias = str(l2_raw.get("market_bias", "neutral"))
        bias_strength = float(l2_raw.get("bias_strength", 0.0) or 0.0)

        soft_codes = sorted(
            {
                str(r["code"])
                for r in l1_block_info["reasons"]
                if not r.get("is_hard")
            }
        )
        hard_codes = sorted(
            {
                str(r["code"])
                for r in l1_block_info["reasons"]
                if r.get("is_hard")
            }
        )
        n_soft = len(soft_codes)
        stacked_cap = self._stacked_veto_cap(n_soft)
        raw_intensity = float(override["intensity"])
        # ``stacked_cap`` is a MULTIPLIER in (0, 1] on Claude's
        # intensity; we then clamp back into [0, 1] for safety.
        calibrated_intensity = max(0.0, min(1.0, stacked_cap * raw_intensity))
        if calibrated_intensity != raw_intensity:
            logger.info(
                "L3 override intensity haircut | n_soft={} cap={:.2f} "
                "raw={:.2f} -> calibrated={:.2f}",
                n_soft,
                stacked_cap,
                raw_intensity,
                calibrated_intensity,
            )

        directive = ExecutionDirective(
            action="risk_on",
            side=override["direction"],
            intensity=calibrated_intensity,
            rationale=(
                f"L3-OVERRIDE-L1 (soft={','.join(soft_codes) or '-'}, "
                f"stacked_cap={stacked_cap:.2f}): {override['rationale']}"
            ),
            market_bias=bias,
            bias_strength=bias_strength,
            conviction=float(override["conviction"]),
            direction_strength=1.0,  # L3 took full authority
        )
        # We DO emit effective_weights but they're cosmetic on this
        # path; L3 is the sole decision-maker so we surface that
        # explicitly (level3=1.0, others=0.0).
        effective_weights = {"level1": 0.0, "level2": 0.0, "level3": 1.0}
        override_meta = {
            "status": "executed",
            "n_soft_blocks": n_soft,
            "n_hard_blocks": len(hard_codes),
            "soft_block_codes": soft_codes,
            "hard_block_codes": hard_codes,
            "raw_intensity": raw_intensity,
            "calibrated_intensity": calibrated_intensity,
            "stacked_veto_cap": stacked_cap,
            "l3_conviction": float(override["conviction"]),
            "l3_direction": override["direction"],
            "l3_rationale_snippet": str(override.get("rationale", ""))[:240],
        }
        return DecisionResult(
            final_score=float(override["conviction"]),
            regime="risk_on",
            directive=directive,
            level_scores=[l1_score, l2_score, l3_score],
            weights=self.weights,
            final_direction=l3_dir_sign,
            direction_strength=1.0,
            effective_weights=effective_weights,
            short_circuited=False,
            short_circuit_reason=None,
            l1_overridden_by_l3=True,
            l1_override_meta=override_meta,
        )

    async def _maybe_level3(
        self,
        l1_score: LevelScore,
        l2_score: LevelScore,
        context: dict[str, Any],
        *,
        l1_block_info: dict[str, Any] | None = None,
    ) -> LevelScore:
        """Build the arbiter briefing and call Level 3.

        When ``self.level3 is None`` we synthesise a placeholder
        directly here so the cascade always emits three `LevelScore`s.
        `_effective_weights` then redistributes the placeholder's
        weight back to L1+L2 (unless explicitly disabled).

        When ``self.level3`` is a real :class:`Level3`, we hand it a
        richly-populated :class:`ArbiterBriefing` built from the L1/L2
        ``raw`` payloads + the current market context. The arbiter
        itself decides whether to call Gemini, validate the response
        and how to fall back on errors - the engine only consumes the
        resulting :class:`LevelScore`.
        """
        if self.level3 is None:
            # Synthetic L3 = conviction-weighted re-blend of L1 + L2.
            l1c, l2c = float(l1_score.score), float(l2_score.score)
            value = 0.5 * l1c + 0.5 * l2c
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
                "synthetic re-weight of L1+L2 (no Gemini client wired)"
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

        # ---- Build the rich briefing for Gemini -------------------------
        l1_raw = (l1_score.raw or {}).get("l1", {}) or {}
        l2_raw = (l2_score.raw or {}).get("l2", {}) or {}
        primary_symbol = (
            context.get("symbol")
            or (context.get("symbols") or ["BTC-PERP"])[0]
        )
        l1_passes = bool(l1_raw.get("passes", True))
        # If the caller didn't pre-compute the block info we derive it
        # on the fly - this keeps test paths simple. On the live L1-
        # blocked path the caller will have populated it already.
        if l1_block_info is None:
            l1_block_info = self._extract_l1_block_info(l1_raw)
        briefing = ArbiterBriefing(
            primary_symbol=primary_symbol,
            level1_conviction=float(l1_score.score),
            level1_direction_sign=int(
                getattr(l1_score, "direction_sign", 0) or 0
            ),
            level1_rationale=l1_score.rationale,
            level1_passes=l1_passes,
            level1_raw=l1_raw,
            level2_conviction=float(l2_score.score),
            level2_direction_sign=int(
                getattr(l2_score, "direction_sign", 0) or 0
            ),
            level2_rationale=l2_score.rationale,
            level2_market_bias=str(l2_raw.get("market_bias", "neutral")),
            level2_bias_strength=float(l2_raw.get("bias_strength", 0.0) or 0.0),
            level2_market_heat=float(l2_raw.get("market_heat", 0.5) or 0.5),
            level2_regime=str(l2_raw.get("regime", "neutral")),
            level2_raw=l2_raw,
            market_snapshot=context,
            l1_blocked=not l1_passes,
            l1_blocked_reasons=l1_block_info["reasons"],
            l1_indicators=l1_block_info["indicators"],
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
        #
        # Day-6+ L1-corroboration gate (anti-chop):
        #   L1 emits hard-coded conviction = 0.25 for trend in
        #   {flat, mixed}. Without a corroboration floor, L2 + L3 alone
        #   can swing this override into a LONG on flat tape (positive
        #   funding + mild up-drift bias L2 bull). We require L1 to
        #   have moved OFF that plateau (default >= 0.40) before
        #   allowing the override - so a real but weak trend qualifies
        #   while pure chop is held.
        if (
            side_from_dir is not None
            and direction_strength >= self.strong_bias_open_strength
        ):
            l1_score_value = next(
                (s.score for s in scores if s.level == 1), 0.0
            )
            l1_floor = self.strong_direction_l1_corroboration_min
            if l1_score_value < l1_floor:
                # L1 has no opinion (flat/mixed plateau). Refuse the
                # mid-band override even though L2 + L3 lean.
                return ExecutionDirective(
                    action="hold",
                    side=None,
                    intensity=0.0,
                    rationale=(
                        f"STRONG-DIRECTION suppressed: L1 conv "
                        f"{l1_score_value:.2f} < floor {l1_floor:.2f} "
                        f"({dir_tag}, mid-band hold) | {rationale}"
                    ),
                    market_bias=bias,
                    bias_strength=bias_strength,
                    conviction=final_score,
                    direction_strength=direction_strength,
                )
            intensity = float(
                min(1.0, 0.5 * final_score * direction_strength)
            )
            return ExecutionDirective(
                action="risk_on",
                side=side_from_dir,
                intensity=intensity,
                rationale=(
                    f"STRONG-DIRECTION override (L1 corroborated "
                    f"conv={l1_score_value:.2f}>=floor{l1_floor:.2f}) "
                    f"{dir_tag} mid-band | {rationale}"
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
