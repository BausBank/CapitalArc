"""Level 3 - Claude Sonnet 4.6 (via OpenRouter) as the final arbiter.

Level 3 receives the structured outputs of Level 1 and Level 2 plus a
compact market briefing, and asks **Claude Sonnet 4.6 (via OpenRouter)**
to emit a final ``(conviction, direction, regime,
recommended_intensity)`` quadruple plus an English rationale and a
list of key factors.

Hard contract
=============
The model must reply with valid JSON matching :class:`ArbiterResponse`:

    {
      "conviction": 0.85,
      "direction": "long" | "short" | "neutral",
      "regime":    "risk_on" | "risk_off" | "hold",
      "recommended_intensity": 0.65,
      "rationale":   "<English rationale, format depends on L3_MODE>",
      "key_factors": ["...", "...", "..."]
    }

The response is validated by a strict Pydantic model. Any deviation
(missing fields, out-of-range floats, wrong enums) falls back to a
neutral hold so the agent never trades on a hallucinated decision.

Two modes
=========
``Level3Config.mode`` selects which persona Claude wears:

* ``"critical"`` (default, demo-quality) — Claude behaves as an
  *independent* senior risk manager. It is explicitly allowed to
  disagree with L1+L2 when signals are weak or contradictory, and its
  ``rationale`` follows a strict 5-section template (Market Context /
  Key Signals Analysis / Contradictions & Risks / My Independent View
  / Final Recommendation). Loaded from
  ``prompts/level3_arbiter_critical.md``.
* ``"standard"`` — legacy trader-voice prompt. 1-2 sentence rationale
  plus 2-4 short tags. Useful when the structured rationale is
  overkill (e.g. a tight high-frequency loop). Loaded from
  ``prompts/level3_arbiter_standard.md``.

The mode does not change the ``ArbiterResponse`` JSON shape or any
downstream wiring — it just swaps the system prompt and the
mode-specific suffix on the user prompt.

Design notes
============
* The briefing is intentionally rich (per-symbol funding / OI / volume,
  L2 market bias + strength, primary symbol, drawdown, ATR%). The
  arbiter is *the* level with enough context to break a tie when L1
  and L2 disagree, so it must see both their structured payloads in
  full.
* The system prompts live in ``prompts/level3_arbiter_<mode>.md`` so
  they can be tuned without touching code. Each file ships with a
  professional prompt; if a file is missing the module falls back to
  an inlined critical-mode copy so the agent still runs.
* When ``client`` is ``None`` (e.g. ``OPENROUTER_API_KEY`` unset),
  Level 3 emits a synthetic verdict that re-blends L1 + L2. The
  :class:`DecisionEngine` then redistributes L3's weight back to L1
  + L2 so the placeholder doesn't dilute the real signal. The
  synthetic and fallback rationales still follow the active mode's
  format for visual consistency.
* Provider naming uses ``openrouter`` to signal the *gateway* in
  ``raw["l3"]["provider"]``; the actual model slug
  (``anthropic/claude-sonnet-4.6``) is surfaced separately on
  ``raw["l3"]["model"]`` so the panel can render both.

Migration history
=================
This module previously used Google's ``google-generativeai`` SDK to
call Gemini 2.5 Flash. The Google AI Studio API is geo-locked away
from several user regions (returns ``400 FAILED_PRECONDITION: User
location is not supported``) even from paid tier accounts, which
made it unusable for us. OpenRouter routes through their own
infrastructure and stays accessible everywhere; the prompt contract,
:class:`ArbiterBriefing` and :class:`ArbiterResponse` are unchanged
so swapping the upstream LLM (e.g. to ``openai/gpt-4o``) is a
one-line ``.env`` change.
"""

from __future__ import annotations

import statistics
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, Field, ValidationError, field_validator

from src.llm.openrouter_client import OpenRouterClient
from src.utils.logging import logger

if TYPE_CHECKING:
    from src.core.decision_engine import LevelScore


L3Mode = Literal["critical", "standard"]
L3Aggression = Literal["conservative", "balanced", "aggressive"]

# Hard cap on the number of LLM samples the self-consistency feature
# may draw in a single arbitration, regardless of operator config.
# Protects the per-cycle / per-hour LLM budget: even a misconfigured
# ``L3_SELF_CONSISTENCY_SAMPLES=50`` can never blow the budget because
# the arbiter clamps it here AND only resamples on borderline cycles.
_SELF_CONSISTENCY_MAX: int = 3


def _clamp01(value: float) -> float:
    """Clamp a float into the closed interval ``[0.0, 1.0]``.

    The aggression-mode multipliers can push conviction / intensity
    above 1.0 (e.g. 0.95 x 1.10 = 1.045); the Pydantic contract
    requires ``[0, 1]``, so we clamp here before constructing the
    calibrated response.
    """
    if value < 0.0:
        return 0.0
    if value > 1.0:
        return 1.0
    return float(value)

_PROMPT_PATHS: dict[str, Path] = {
    "critical": Path("prompts/level3_arbiter_critical.md"),
    "standard": Path("prompts/level3_arbiter_standard.md"),
}

# Inline critical-mode prompt used only when the on-disk file is
# missing (CI / packaged distributions). The full, demo-quality
# version lives in ``prompts/level3_arbiter_critical.md``.
_FALLBACK_SYSTEM_PROMPT = (
    "You are an INDEPENDENT, SENIOR RISK MANAGER (Claude Sonnet 4.6), "
    "the final arbiter of the CapitalArc trading agent on Arc Perp DEX "
    "(only BTC-PERP and ETH-PERP).\n\n"
    "You receive structured data from Level 1 (technicals) and Level 2 "
    "(on-chain metrics + market bias) plus market context. You are "
    "allowed to disagree with both upstream levels when signals are "
    "weak, contradictory or fragile. Skepticism is your baseline.\n\n"
    "When the briefing contains a '⚠ Level 1 BLOCKED' section, you "
    "may override SOFT block codes (rsi_overbought/oversold, "
    "atr_too_low/high, trend_mixed) if the on-chain mix and the "
    "indicator table clearly contradict the L1 veto - return "
    "`regime=\"risk_on\"` with conviction >= 0.55 to actually open "
    "the trade, otherwise return a HOLD. HARD blocks "
    "(drawdown_breach, ohlcv_unavailable) are NEVER overrideable.\n\n"
    "Respond with VALID JSON ONLY, no markdown fences:\n"
    "{\n"
    '  "conviction": 0.0-1.0,\n'
    '  "direction": "long" | "short" | "neutral",\n'
    '  "regime": "risk_on" | "risk_off" | "hold",\n'
    '  "recommended_intensity": 0.0-1.0,\n'
    '  "rationale": "<5-section structured rationale, English>",\n'
    '  "key_factors": ["meaningful phrase", "meaningful phrase", ...]\n'
    "}\n\n"
    "The rationale MUST follow this 5-section template, separated by "
    "blank lines:\n"
    "  Market Context:\n  [1-2 sentences]\n\n"
    "  Key Signals Analysis:\n  - Signal 1 - why + strength + number\n"
    "  - Signal 2 - ...\n\n"
    "  Contradictions & Risks:\n  [explicit; 'no material' is allowed]\n\n"
    "  My Independent View:\n  [first-person opinion]\n\n"
    "  Final Recommendation:\n  [verdict + intensity rationale]\n\n"
    "key_factors must contain 2-4 descriptive English phrases (not "
    "single words, never empty). Be conservative at high ATR% and "
    "drawdown."
)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass
class Level3Config:
    """Configuration for Level 3 (Claude Sonnet 4.6 via OpenRouter).

    ``mode`` selects the persona Claude wears:

    * ``"critical"`` (default) — independent risk-manager voice, 5-
      section structured rationale, allowed to disagree with L1+L2.
    * ``"standard"`` — concise trader voice, 1-2 sentence rationale.

    When ``system_prompt_path`` is ``None`` (the default) the path is
    derived from ``mode`` via :data:`_PROMPT_PATHS`. Pass an explicit
    path to override (e.g. unit tests, custom prompt tuning).
    """

    model: str = "anthropic/claude-sonnet-4.6"
    temperature: float = 0.2
    max_output_tokens: int = 2048
    timeout_seconds: int = 60
    mode: L3Mode = "critical"
    system_prompt_path: Path | None = None

    # ---- Day-6 aggression calibration ------------------------------
    # ``aggression`` is the post-validation calibration knob:
    #
    #   * ``"conservative"`` - conviction x0.90, intensity x0.90,
    #     no HOLD-rescue. Reproduces the pre-Day-6 "skeptical-by-
    #     default" behaviour for safety drills.
    #   * ``"balanced"`` (default) - no multiplier; verdict as-is.
    #     Pairs with the Day-6 decision-matrix prompt.
    #   * ``"aggressive"`` - conviction x1.10, intensity x1.15,
    #     plus a HOLD-rescue rule: when Claude returns
    #     ``regime="hold"`` but L1 passes AND L2 conviction
    #     >= ``hold_rescue_l2_min``, the engine flips the verdict
    #     to a low-intensity OPEN aligned with L2's direction.
    #
    # Calibration happens AFTER ``ArbiterResponse`` validation so the
    # raw LLM output is preserved (the panel shows both raw +
    # calibrated values + the calibration metadata).
    aggression: L3Aggression = "balanced"
    hold_rescue_l2_min: float = 0.65
    hold_rescue_intensity: float = 0.30

    # ---- Day-3 multi-sample self-consistency (borderline only) ------
    # When enabled, the arbiter draws additional LLM samples ONLY when
    # the first sample's conviction lands in the borderline band
    # ``[sc_borderline_low, sc_borderline_high]`` (i.e. the decision is
    # genuinely on the fence). It then takes the majority direction and
    # median conviction / intensity across all samples, and records the
    # sample agreement so the EntryQualityGate can downgrade contested
    # opens. Disabled by default (samples=1) to protect the LLM budget;
    # ``samples`` is hard-capped at 3 (see ``_SELF_CONSISTENCY_MAX``).
    self_consistency_enabled: bool = False
    self_consistency_samples: int = 1
    sc_borderline_low: float = 0.50
    sc_borderline_high: float = 0.65
    # Higher temperature for the extra samples so they actually vary
    # (the primary call stays at the deterministic ``temperature``).
    sc_temperature: float = 0.50

    def resolved_prompt_path(self) -> Path:
        """Return the system-prompt path to load for this config."""
        if self.system_prompt_path is not None:
            return self.system_prompt_path
        return _PROMPT_PATHS.get(self.mode, _PROMPT_PATHS["critical"])


# ---------------------------------------------------------------------------
# Briefing & response shapes
# ---------------------------------------------------------------------------


@dataclass
class ArbiterBriefing:
    """Structured payload sent to the arbiter as the final-arbiter prompt.

    All fields are pre-computed by the :class:`DecisionEngine` before
    the arbiter is called so the LLM never has to re-derive numbers
    from raw rows.

    L1-block transparency (Day 5)
    -----------------------------
    When Level 1 blocks the trade we still want Level 3 to inspect
    *why* and decide whether the block is genuinely justified. The
    three ``l1_blocked*`` fields make that audit possible:

    * ``l1_blocked`` -- True if the upstream L1 verdict was a BLOCK.
    * ``l1_blocked_reasons`` -- the full list of L1 reasons (each is a
      ``dict`` with ``code``, ``severity``, ``message``, ``symbol``,
      ``timeframe``, ``metadata`` and a ``is_hard`` flag pre-computed
      by the engine). Hard blocks (``drawdown_breach``,
      ``ohlcv_unavailable``) are NEVER overrideable; soft blocks
      (``rsi_extreme``, ``atr_out_of_band``, ``trend_mixed``) are
      eligible for an L3 override.
    * ``l1_indicators`` -- the per-``(symbol, timeframe)`` indicator
      snapshot L1 actually saw (close, EMA9, EMA21, RSI, ATR, ATR%,
      trend). Lets Claude compute its own opinion on whether the
      block was warranted rather than trusting the rule-based gate.
    """

    primary_symbol: str
    # Level 1
    level1_conviction: float
    level1_direction_sign: int
    level1_rationale: str
    level1_passes: bool
    level1_raw: dict[str, Any]
    # Level 2
    level2_conviction: float
    level2_direction_sign: int
    level2_rationale: str
    level2_market_bias: str
    level2_bias_strength: float
    level2_market_heat: float
    level2_regime: str
    level2_raw: dict[str, Any]
    # Market context (symbols, account, drawdown, ATR%, RPC, ...)
    market_snapshot: dict[str, Any] = field(default_factory=dict)
    # L1-block transparency (Day 5) - populated by the engine when L1
    # has blocked and L3 is being given a chance to override.
    l1_blocked: bool = False
    l1_blocked_reasons: list[dict[str, Any]] = field(default_factory=list)
    l1_indicators: list[dict[str, Any]] = field(default_factory=list)


class ArbiterResponse(BaseModel):
    """Strict schema for the arbiter's reply.

    Anything outside the contract (missing fields, out-of-range floats,
    unknown enum value, wrong type) raises a Pydantic
    :class:`ValidationError`; the arbiter catches it and falls back
    to a safe neutral hold.
    """

    conviction: float = Field(..., ge=0.0, le=1.0)
    direction: Literal["long", "short", "neutral"]
    regime: Literal["risk_on", "risk_off", "hold"]
    recommended_intensity: float = Field(..., ge=0.0, le=1.0)
    # Rationale ceiling sized to the 99th-percentile Claude
    # Sonnet-4.6 critical-mode output we have observed in production:
    #
    #   * Typical critical-mode 5-section rationale: 1500-2500 chars.
    #   * Verbose tail (worked-example walkthroughs, multi-asset
    #     justifications, override audits): up to ~3500 chars - this
    #     is what the prompt's STRICT RESPONSE LENGTH RULES asks for.
    #   * 99p observed: ~6800 chars when Claude got "creative" with
    #     bullet structure even after the explicit cap.
    #
    # We allow 8000 chars to keep a ~15% safety margin above the
    # 99p so a single chatty cycle doesn't trigger the safe-hold
    # fallback for a length-only reason (defeats the whole purpose
    # of L3). The prompt still asks for <= 3500 chars; the model
    # ceiling is the defence-in-depth layer.
    #
    # Standard-mode rationale (1-2 sentences, ~200-400 chars) is
    # unaffected.
    rationale: str = Field(..., min_length=1, max_length=8000)
    key_factors: list[str] = Field(default_factory=list)

    @field_validator("key_factors")
    @classmethod
    def _clamp_factors(cls, v: list[str]) -> list[str]:
        # Keep the top 6 factors max - prevents noisy LLM outputs.
        cleaned = [str(item).strip() for item in v if str(item).strip()]
        return cleaned[:6]

    @property
    def direction_sign(self) -> int:
        return {"long": 1, "short": -1, "neutral": 0}[self.direction]


# ---------------------------------------------------------------------------
# Level 3 - public API
# ---------------------------------------------------------------------------


class Level3:
    """Final-arbiter level of the decision engine.

    Wraps :class:`Level3Arbiter` behind the same ``async def
    score(...)`` interface used by Level 1 and Level 2 so the
    :class:`DecisionEngine` can treat all three levels uniformly.

    Pass ``client=None`` to run as a synthetic placeholder (the engine
    then redistributes L3's weight back to L1 + L2). Pass an
    :class:`OpenRouterClient` to use real Claude arbitration.
    """

    LEVEL = 3

    def __init__(
        self,
        client: OpenRouterClient | None,
        config: Level3Config | None = None,
    ) -> None:
        self.config = config or Level3Config()
        self.client = client
        self.arbiter = Level3Arbiter(client=client, config=self.config)

    async def score(self, briefing: ArbiterBriefing) -> "LevelScore":
        from src.core.decision_engine import LevelScore

        response, raw_payload = await self.arbiter.arbitrate(briefing)
        return LevelScore(
            level=self.LEVEL,
            score=float(response.conviction),
            rationale=response.rationale,
            raw={"l3": raw_payload},
            direction_sign=response.direction_sign,
        )


# ---------------------------------------------------------------------------
# Telemetry — in-process counter for over-hold detection
# ---------------------------------------------------------------------------


@dataclass
class _L3Telemetry:
    """In-process counter tracking L3 verdict mix per session.

    The single most important question this answers: **how often is
    L3 holding when the upstream cascade said "open"?** If that
    ratio is more than a few percent, the prompt is over-conservative
    and the calibration knobs need adjusting.

    Numbers reset on process restart - they're a live operational
    health signal, not a backtest record.
    """

    total: int = 0
    held: int = 0           # final verdict was regime="hold"
    opened: int = 0         # final verdict was regime="risk_on"
    rescued_holds: int = 0  # HOLD-rescue rule fired and flipped to open
    raw_holds: int = 0      # Claude returned HOLD pre-calibration

    def record(
        self,
        raw: ArbiterResponse,
        calibrated: ArbiterResponse,
        calibration: dict[str, Any],
    ) -> None:
        self.total += 1
        if raw.regime == "hold":
            self.raw_holds += 1
        if calibrated.regime == "hold":
            self.held += 1
        else:
            self.opened += 1
        if calibration.get("hold_rescue_fired"):
            self.rescued_holds += 1

    def log_summary(self) -> None:
        if self.total == 0:
            return
        hold_pct = self.held / self.total * 100.0
        raw_hold_pct = self.raw_holds / self.total * 100.0
        logger.info(
            "L3 | cycles={} open={} hold={:.0f}% rescues={}",
            self.total, self.opened, hold_pct, self.rescued_holds,
        )


# ---------------------------------------------------------------------------
# OpenRouter-backed arbiter
# ---------------------------------------------------------------------------


class Level3Arbiter:
    """Renders the briefing into a prompt, calls the LLM, validates JSON."""

    def __init__(
        self,
        client: OpenRouterClient | None,
        config: Level3Config,
    ) -> None:
        self.client = client
        self.config = config
        self._system_prompt: str | None = None
        # In-process telemetry counter — tracks how often L3 holds
        # vs opens, including the pathological "strong L2 but L3
        # held" cycles that motivated the Day-6 calibration work.
        # Reset between processes (no persistence).
        self._telemetry = _L3Telemetry()

    # ------------------------------------------------------------------
    # Public introspection (panels / tests)
    # ------------------------------------------------------------------

    @property
    def telemetry(self) -> "_L3Telemetry":
        return self._telemetry

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    async def arbitrate(
        self, briefing: ArbiterBriefing
    ) -> tuple[ArbiterResponse, dict[str, Any]]:
        """Return ``(validated_response, raw_payload)`` for the briefing.

        ``raw_payload`` is the dict that ends up on
        ``LevelScore.raw["l3"]``; it contains everything the panel and
        downstream consumers need (provider, latency, error flags,
        the validated response, the raw model text on failure).
        """
        # ---- No client -> synthetic L3 (placeholder) ------------------
        if self.client is None:
            response, payload = self._synthetic(briefing)
            return response, payload

        system_prompt = self._load_system_prompt()
        user_prompt = self._render_user_prompt(briefing)

        started = time.time()
        try:
            raw_dict = await self.client.generate_json(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
            )
            raw_response = ArbiterResponse.model_validate(raw_dict)

            # ---- Day-3 borderline self-consistency ----------------
            # Only resamples when the primary verdict's conviction is
            # on the fence; otherwise this is a no-op (one call). Takes
            # the majority direction + median conviction / intensity
            # across samples and records the sample agreement.
            raw_response, sc_meta = await self._maybe_self_consistency(
                raw_response,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
            )

            elapsed_ms = (time.time() - started) * 1000.0

            # ---- Day-6 post-validation calibration ----------------
            # Applies the aggression-mode multipliers and the
            # HOLD-rescue rule (if enabled). Returns the calibrated
            # response + a metadata dict describing every change.
            # When aggression="balanced" this is a no-op (raw == cal).
            response, calibration = self._calibrate(raw_response, briefing)
            self._telemetry.record(raw_response, response, calibration)

            logger.info(
                "L3 OK | conv={:.2f} dir={} regime={} intensity={:.2f} ({}ms)",
                response.conviction, response.direction,
                response.regime, response.recommended_intensity,
                int(elapsed_ms),
            )
            self._telemetry.log_summary()
            payload: dict[str, Any] = {
                "provider": "openrouter",
                "model": self.config.model,
                "mode": self.config.mode,
                "aggression": self.config.aggression,
                "latency_ms": elapsed_ms,
                "synthetic": False,
                "fallback": False,
                "raw_response": raw_dict,
                "raw_response_validated": raw_response.model_dump(),
                "response": response.model_dump(),
                "calibration": calibration,
                "self_consistency": sc_meta,
            }
            return response, payload
        except ValidationError as exc:
            elapsed_ms = (time.time() - started) * 1000.0
            # Build a human-readable, field-aware diagnosis so the
            # operator (and the panel) immediately sees WHICH field
            # of the response broke and BY HOW MUCH. Without this
            # the fallback just says "schema validation failed" and
            # the operator has to dig through the raw JSON to find
            # out (e.g.) that ``rationale`` was 4123 chars vs the
            # 8000 ceiling - which is the single most common
            # failure mode in critical mode.
            diagnosis = self._describe_validation_error(exc, raw_dict)
            logger.error(
                "L3 arbiter returned malformed JSON ({:.0f}ms) - "
                "falling back to neutral hold. Diagnosis: {}",
                elapsed_ms,
                diagnosis,
            )
            response, payload = self._fallback_hold(
                briefing,
                reason=f"schema validation failed: {diagnosis}",
                latency_ms=elapsed_ms,
                errors=str(exc)[:500],
            )
            return response, payload
        except Exception as exc:  # noqa: BLE001 - keep agent alive on any failure
            elapsed_ms = (time.time() - started) * 1000.0
            logger.error(
                "L3 OpenRouter call failed ({:.0f}ms): {} - falling back "
                "to neutral hold.",
                elapsed_ms,
                exc,
            )
            response, payload = self._fallback_hold(
                briefing,
                reason=f"OpenRouter call failed: {exc}",
                latency_ms=elapsed_ms,
                errors=str(exc)[:500],
            )
            return response, payload

    # ------------------------------------------------------------------
    # Day-3 borderline self-consistency
    # ------------------------------------------------------------------

    async def _maybe_self_consistency(
        self,
        primary: ArbiterResponse,
        *,
        system_prompt: str,
        user_prompt: str,
    ) -> tuple[ArbiterResponse, dict[str, Any]]:
        """Optionally resample the arbiter on borderline conviction.

        Returns ``(aggregated_response, sc_meta)``. When the feature is
        off, the primary verdict is non-borderline, or every extra
        sample failed, this is a no-op: it returns ``primary`` with
        ``sc_meta["applied"] = False`` (agreement 1.0).

        Aggregation across the valid samples:
          * direction - majority vote (ties keep the primary's side).
          * conviction / recommended_intensity - median.
          * regime - majority vote (ties keep the primary's regime).
          * rationale / key_factors - taken from the primary, with a
            short self-consistency note appended to the rationale.
        """
        cfg = self.config
        n_samples = min(int(cfg.self_consistency_samples), _SELF_CONSISTENCY_MAX)
        sc_meta: dict[str, Any] = {"applied": False, "direction_agreement": 1.0}
        if (
            not cfg.self_consistency_enabled
            or n_samples <= 1
            or self.client is None
        ):
            return primary, sc_meta
        # Only resample when the primary verdict is genuinely on the
        # fence - this is the budget-protection gate.
        if not (
            cfg.sc_borderline_low <= primary.conviction <= cfg.sc_borderline_high
        ):
            sc_meta["reason"] = "primary conviction outside borderline band"
            return primary, sc_meta

        samples: list[ArbiterResponse] = [primary]
        for i in range(n_samples - 1):
            try:
                extra_dict = await self.client.generate_json(
                    system_prompt=system_prompt,
                    user_prompt=user_prompt,
                    temperature=cfg.sc_temperature,
                )
                samples.append(ArbiterResponse.model_validate(extra_dict))
            except Exception as exc:  # noqa: BLE001 - extra samples are best-effort
                logger.warning(
                    "L3 self-consistency extra sample {}/{} failed: {} "
                    "(continuing with fewer samples)",
                    i + 2, n_samples, exc,
                )

        if len(samples) <= 1:
            sc_meta["reason"] = "no valid extra samples"
            return primary, sc_meta

        directions = [s.direction for s in samples]
        convictions = [s.conviction for s in samples]
        intensities = [s.recommended_intensity for s in samples]
        regimes = [s.regime for s in samples]

        majority_direction = self._majority(directions, fallback=primary.direction)
        majority_regime = self._majority(regimes, fallback=primary.regime)
        median_conviction = float(statistics.median(convictions))
        median_intensity = float(statistics.median(intensities))
        agreement = directions.count(majority_direction) / len(directions)

        aggregated = ArbiterResponse(
            conviction=median_conviction,
            direction=majority_direction,
            regime=majority_regime,
            recommended_intensity=median_intensity,
            rationale=(
                primary.rationale
                + f"\n\n[Self-consistency: {len(samples)} samples, "
                f"direction agreement {agreement:.0%}, "
                f"median conviction {median_conviction:.2f}.]"
            ),
            key_factors=list(primary.key_factors),
        )
        sc_meta = {
            "applied": True,
            "n_samples": len(samples),
            "directions": directions,
            "convictions": [round(c, 3) for c in convictions],
            "regimes": regimes,
            "majority_direction": majority_direction,
            "majority_regime": majority_regime,
            "median_conviction": round(median_conviction, 3),
            "median_intensity": round(median_intensity, 3),
            "direction_agreement": round(agreement, 3),
            "conviction_spread": round(
                max(convictions) - min(convictions), 3
            ),
        }
        logger.info(
            "L3 self-consistency | {} samples, dir-agreement {:.0%}, "
            "median conv {:.2f} (primary dir={} conv={:.2f})",
            len(samples), agreement, median_conviction,
            primary.direction, primary.conviction,
        )
        return aggregated, sc_meta

    @staticmethod
    def _majority(values: list[Any], *, fallback: Any) -> Any:
        """Return the most common value; ``fallback`` on an empty tie."""
        if not values:
            return fallback
        counts: dict[Any, int] = {}
        for v in values:
            counts[v] = counts.get(v, 0) + 1
        top = max(counts.values())
        winners = [v for v, c in counts.items() if c == top]
        if len(winners) == 1:
            return winners[0]
        # Tie - prefer the fallback (the primary verdict) if it's among
        # the winners, otherwise the first sample's value.
        return fallback if fallback in winners else winners[0]

    # ------------------------------------------------------------------
    # Prompt rendering
    # ------------------------------------------------------------------

    def _load_system_prompt(self) -> str:
        """Read the system prompt once per process (cached).

        Resolves the path via :meth:`Level3Config.resolved_prompt_path`
        so the active ``L3_MODE`` (critical / standard) picks up the
        matching prompt file. Falls back to the inlined critical-mode
        prompt if the file is missing so packaged distributions
        without the ``prompts/`` directory still run.
        """
        if self._system_prompt is not None:
            return self._system_prompt
        path = self.config.resolved_prompt_path()
        try:
            if path.exists():
                self._system_prompt = path.read_text(encoding="utf-8").strip()
                logger.debug(
                    "Loaded L3 system prompt from {} (mode={})",
                    path, self.config.mode,
                )
            else:
                logger.warning(
                    "L3 system prompt {} not found (mode={}); using "
                    "inlined critical-mode fallback.",
                    path, self.config.mode,
                )
                self._system_prompt = _FALLBACK_SYSTEM_PROMPT
        except OSError as exc:
            logger.warning(
                "Could not read L3 system prompt at {}: {}; using "
                "inlined critical-mode fallback.",
                path, exc,
            )
            self._system_prompt = _FALLBACK_SYSTEM_PROMPT
        return self._system_prompt

    def _render_user_prompt(self, b: ArbiterBriefing) -> str:
        """Render the structured briefing into a compact Markdown payload.

        Plain Markdown is friendlier to Claude than nested JSON because
        the surrounding section titles act as natural attention
        anchors. We still keep the numeric values inline so the model
        never has to re-derive anything.
        """
        per_symbol_l1 = b.level1_raw.get("per_symbol", {}) or {}
        per_symbol_l2 = b.level2_raw.get("per_symbol", {}) or {}
        vault = b.level2_raw.get("vault_flow", {}) or {}
        market = b.market_snapshot or {}

        lines: list[str] = []
        lines.append("# CapitalArc - Final Arbiter Briefing")

        # ============================================================
        # DECISION-TASK HEADER (front-loaded when L1 blocked)
        # ============================================================
        # When L1 has vetoed the trade we frame the entire arbitration
        # as an *audit task* up-front rather than burying the framing
        # at the bottom of the prompt. The hard-vs-soft taxonomy and
        # the override-floor are stated explicitly so Claude's first
        # tokens consume the actual question being asked.
        if b.l1_blocked:
            has_hard = any(r.get("is_hard") for r in b.l1_blocked_reasons)
            n_soft = sum(
                1 for r in b.l1_blocked_reasons if not r.get("is_hard")
            )
            lines.append("")
            lines.append("## ⚡ DECISION TASK")
            if has_hard:
                lines.append(
                    "Level 1 raised a **HARD** block. Your task is to "
                    "produce a thoughtful HOLD verdict: explain the "
                    "block to the operator, summarise the on-chain "
                    "picture, and confirm `regime=\"hold\"` / "
                    "`direction=\"neutral\"` / `conviction <= 0.4`. "
                    "Hard blocks (drawdown / missing OHLCV) are "
                    "**immutable**; even a strong on-chain setup does "
                    "not override them."
                )
            else:
                stacked_note = ""
                if n_soft >= 3:
                    stacked_note = (
                        "  ⚠ Three or more stacked soft blocks fired "
                        "simultaneously - the engine will defensively "
                        "halve your intensity if you override, so "
                        "size up only if the on-chain evidence is "
                        "decisively contradicting *all* of them."
                    )
                elif n_soft == 2:
                    stacked_note = (
                        "  ⚠ Two stacked soft blocks fired - the "
                        "engine will trim your intensity by ~30% if "
                        "you override."
                    )
                lines.append(
                    "Level 1 raised one or more **SOFT** technical "
                    "blocks. Your task: AUDIT each block against the "
                    "raw indicator table and the Level 2 on-chain "
                    "picture, then decide:"
                )
                lines.append(
                    "  1. **OVERRIDE** -> return `regime=\"risk_on\"`, "
                    "non-neutral `direction`, `conviction >= 0.55`. "
                    "Only when the on-chain mix decisively "
                    "contradicts the technical veto."
                )
                lines.append(
                    "  2. **HOLD** -> return `regime=\"hold\"` / "
                    "`direction=\"neutral\"`. The default; pick this "
                    "whenever the override case is not clearly "
                    "stronger than the block." + stacked_note
                )
        lines.append("")
        lines.append("## Market context")
        lines.append(f"- primary_symbol: {b.primary_symbol}")
        symbols = market.get("symbols") or []
        lines.append(f"- symbols: {symbols}")
        lines.append(
            f"- account_margin_usdc: {market.get('account_margin_usdc', 0):.2f}"
        )
        lines.append(
            f"- account_drawdown_pct: {market.get('account_drawdown_pct', 0):.2f}%"
        )
        lines.append(
            f"- account_unrealized_pnl_usdc: "
            f"{market.get('account_unrealized_pnl_usdc', 0):.2f}"
        )
        lines.append(f"- dune_chain: {market.get('dune_chain', 'n/a')}")

        # ---- Level 1 ----------------------------------------------------
        lines.append("")
        lines.append("## Level 1 - Technical hard rules (OHLCV via Dune)")
        lines.append(
            f"- passes: {b.level1_passes}  "
            f"|  conviction: {b.level1_conviction:.3f}  "
            f"|  direction_sign: {b.level1_direction_sign:+d}"
        )
        lines.append(f"- rationale: {b.level1_rationale}")
        if per_symbol_l1:
            lines.append("- per_symbol:")
            for sym, ro in per_symbol_l1.items():
                trend = ro.get("trend", "?")
                passes = ro.get("passes", "?")
                atr_pct_avg = ro.get("atr_pct_avg", 0.0)
                lines.append(
                    f"  - {sym}: trend={trend} passes={passes} "
                    f"atr_pct_avg={atr_pct_avg:.3f}%"
                )

        # Per-(symbol, timeframe) indicator rows L1 actually computed.
        # Rendered so Claude can form its own opinion on whether the
        # technical setup is hostile - especially important on the
        # L1-block-override path.
        if b.l1_indicators:
            lines.append("- l1_indicators (per symbol & timeframe):")
            for row in b.l1_indicators:
                lines.append(
                    f"  - {row.get('symbol', '?')}@{row.get('timeframe', '?')}: "
                    f"close={row.get('close', 0):.2f} "
                    f"ema9={row.get('ema_fast', 0):.2f} "
                    f"ema21={row.get('ema_slow', 0):.2f} "
                    f"rsi={row.get('rsi', 0):.1f} "
                    f"atr%={row.get('atr_pct', 0):.3f} "
                    f"trend={row.get('trend', '?')}"
                )

        # L1 BLOCK section: lets Claude critically audit the veto.
        # Surfaced as its own section so it's impossible to miss in
        # the rendered Markdown, and tagged hard vs soft so Claude
        # knows which blocks it can argue with.
        if b.l1_blocked:
            lines.append("")
            lines.append(
                "## ⚠ Level 1 BLOCKED the trade - audit required"
            )
            hard_codes = sorted(
                {
                    str(r.get("code"))
                    for r in b.l1_blocked_reasons
                    if r.get("is_hard")
                }
            )
            soft_codes = sorted(
                {
                    str(r.get("code"))
                    for r in b.l1_blocked_reasons
                    if not r.get("is_hard")
                }
            )
            lines.append(
                f"- hard_block_codes (NEVER overrideable): "
                f"{hard_codes or 'none'}"
            )
            lines.append(
                f"- soft_block_codes (may be overrideable if data "
                f"justifies it): {soft_codes or 'none'}"
            )
            lines.append("- reasons (with pre-computed marginality):")
            for r in b.l1_blocked_reasons:
                tag = "HARD" if r.get("is_hard") else "soft"
                sym = r.get("symbol") or "-"
                tf = r.get("timeframe") or "-"
                md = r.get("metadata") or {}
                # Render the marginality label inline so Claude can
                # see at a glance how decisive (or fragile) the
                # violation is, without having to compute "+X.X pts
                # beyond threshold" mentally each time. L1 stamps
                # ``severity_label`` and ``margin_pct_of_threshold``
                # in the reason's metadata - see
                # ``src/core/level1.py::_margin_metadata``.
                margin_bits: list[str] = []
                if "severity_label" in md:
                    margin_bits.append(str(md["severity_label"]))
                if "margin" in md:
                    margin_bits.append(f"+{float(md['margin']):.2f}")
                if "margin_pct_of_threshold" in md:
                    margin_bits.append(
                        f"{float(md['margin_pct_of_threshold']):+.1f}% beyond"
                    )
                if "dissent_pct" in md:
                    margin_bits.append(
                        f"{int(md.get('n_dissenters', 0))}/"
                        f"{int(md.get('n_timeframes', 0))} TF dissent"
                    )
                margin_tag = (
                    f" [{', '.join(margin_bits)}]" if margin_bits else ""
                )
                lines.append(
                    f"  - [{tag}]{margin_tag} {r.get('code', '?')} "
                    f"({sym}@{tf}): {r.get('message', '')}"
                )
            if hard_codes:
                lines.append(
                    "- NOTE: a HARD block is present. You MUST return "
                    "`regime=\"hold\"` / `direction=\"neutral\"` / "
                    "`conviction<=0.4`. Hard blocks are not "
                    "overrideable under any circumstances."
                )
            else:
                lines.append(
                    "- OVERRIDE AUTHORITY: only SOFT blocks present. "
                    "You MAY open the trade anyway IF (and only if) "
                    "the on-chain picture + the indicator table "
                    "above clearly contradict the L1 veto. To do so, "
                    "return `regime=\"risk_on\"` with a non-neutral "
                    "`direction` and `conviction >= the operator's "
                    "override floor (0.55 by default)`. Otherwise "
                    "respect the block and return a HOLD."
                )

        # ---- Level 2 ----------------------------------------------------
        lines.append("")
        lines.append("## Level 2 - On-chain intelligence (Dune MCP)")
        lines.append(
            f"- conviction: {b.level2_conviction:.3f}  |  "
            f"direction_sign: {b.level2_direction_sign:+d}  |  "
            f"market_bias: {b.level2_market_bias} "
            f"(strength={b.level2_bias_strength:.2f})"
        )
        lines.append(
            f"- regime: {b.level2_regime}  |  market_heat: {b.level2_market_heat:.3f}"
        )
        lines.append(f"- rationale: {b.level2_rationale}")
        for sym, intel in per_symbol_l2.items():
            f = intel.get("funding") or {}
            oi = intel.get("open_interest") or {}
            vol = intel.get("volume") or {}
            lsr = intel.get("long_short") or {}
            wh = intel.get("whales") or {}
            lines.append(f"- {sym}:")
            lines.append(
                f"  - price={vol.get('last_price', 0):.2f}  "
                f"24h={vol.get('price_change_pct_24h', 0):+.2f}%  "
                f"vol1h_spike={vol.get('spike_detected', False)}"
            )
            lines.append(
                f"  - funding={f.get('current_rate', 0)*100:.4f}%  "
                f"APR={f.get('annualised_pct', 0):+.1f}%  "
                f"d8h={f.get('rate_8h_change', 0)*100:+.4f}%"
            )
            if oi.get("oi_delta_available", True):
                lines.append(
                    f"  - OI_usd={oi.get('current_value_usd', 0):,.0f}  "
                    f"OI(1h)={oi.get('delta_1h_pct', 0):+.2f}%  "
                    f"OI(24h)={oi.get('delta_24h_pct', 0):+.2f}%"
                )
            else:
                # Honest signal to the arbiter: the HL OI ring buffer
                # has not yet accumulated old-enough samples (typical
                # for the first cycles after a restart). Tell Claude
                # explicitly that 1h / 24h deltas are MISSING DATA,
                # not a measured-flat 0.00% — otherwise it reads them
                # as "no momentum" and downgrades the setup.
                lines.append(
                    f"  - OI_usd={oi.get('current_value_usd', 0):,.0f}  "
                    "OI(1h)=warming OI(24h)=warming  "
                    "(history not yet available — treat as MISSING, "
                    "NOT as flat 0%)"
                )
            lines.append(
                f"  - LSR={lsr.get('long_short_ratio', 1.0):.2f}  "
                f"long%={lsr.get('long_account_pct', 0.5)*100:.1f}  "
                f"inferred_bias={lsr.get('inferred_bias', 'balanced')}"
            )
            lines.append(
                f"  - whale_flagged={wh.get('flagged', False)}  "
                f"direction={wh.get('direction', 'neutral')}  "
                f"n_whales={wh.get('n_whales', 0)}"
            )
        if vault:
            lines.append(
                f"- vault_flow: tvl={vault.get('tvl_usdc', 0):,.0f}USDC  "
                f"net_flow={vault.get('net_flow_usdc', 0):+,.0f}USDC  "
                f"window={vault.get('window_hours', 0):.0f}h"
            )

        # ---- Task --------------------------------------------------------
        lines.append("")
        lines.append("## Your task")
        lines.append(
            "Return the final verdict as JSON matching the schema in the "
            "system prompt. Conviction and direction are independent. Be "
            "conservative when L1 and L2 disagree, at high ATR% or at "
            f"drawdown >= 5% (current drawdown = "
            f"{market.get('account_drawdown_pct', 0):.2f}%)."
        )
        lines.append("")
        lines.append(self._mode_instruction())
        return "\n".join(lines)

    def _mode_instruction(self) -> str:
        """Mode-specific reminder appended to every user prompt.

        Belt-and-braces: the system prompt already enforces the format,
        but echoing the instruction in the user message dramatically
        reduces drift on long briefings (Claude occasionally compresses
        the rationale when it sees a lot of input data).
        """
        if self.config.mode == "critical":
            return (
                "**CRITICAL MODE** - You are an independent risk manager. "
                "You may disagree with L1 and L2 when justified. Write "
                "`rationale` in **English** following the mandatory "
                "5-section template from the system prompt:\n"
                "  1. `Market Context:` (1-2 sentences)\n"
                "  2. `Key Signals Analysis:` (3-5 bullets, each "
                "citing a concrete number)\n"
                "  3. `Contradictions & Risks:` (explicit; write \"No "
                "material contradictions; signals converge.\" only if "
                "true)\n"
                "  4. `My Independent View:` (first person)\n"
                "  5. `Final Recommendation:` (verdict + intensity)\n"
                "Separate sections with a blank line. `key_factors` "
                "must be 2-4 **descriptive phrases** (not single "
                "words, never empty, never \"-\")."
            )
        # standard
        return (
            "**STANDARD MODE** - Write `rationale` in **English** "
            "(1-2 sentences, trader voice, plain prose). Always "
            "populate `key_factors` with 2-4 short English tags - "
            "never leave it empty."
        )

    # ------------------------------------------------------------------
    # Post-validation calibration (Day 6+)
    # ------------------------------------------------------------------

    # Aggression-mode multipliers. Kept as a class constant so tests
    # can pin a known table without monkey-patching settings.
    _AGGRESSION_MULTIPLIERS: dict[L3Aggression, dict[str, float]] = {
        "conservative": {"conviction": 0.90, "intensity": 0.90},
        "balanced":     {"conviction": 1.00, "intensity": 1.00},
        "aggressive":   {"conviction": 1.10, "intensity": 1.15},
    }

    def _calibrate(
        self,
        raw_response: ArbiterResponse,
        briefing: ArbiterBriefing,
    ) -> tuple[ArbiterResponse, dict[str, Any]]:
        """Apply aggression-mode calibration on top of Claude's verdict.

        Two transformations, in order:

        1. **Multiplier pass.** Scale ``conviction`` and
           ``recommended_intensity`` by the aggression-mode
           multipliers (no-op for ``balanced``). Values are clamped
           back into ``[0, 1]`` to honour the Pydantic contract.

        2. **HOLD-rescue pass** (``aggressive`` only). When Claude
           returns ``regime="hold"`` but the upstream cascade
           strongly converges (L1 passes AND L2 conviction
           >= ``hold_rescue_l2_min``), flip the verdict to a
           low-intensity OPEN aligned with L2's direction. This is
           the explicit "lean-in" rule the operator opted into via
           ``L3_AGGRESSION=aggressive``.

        The raw response is NEVER mutated; we always return a new
        ``ArbiterResponse``. Calibration metadata is returned so the
        Final Decision panel can show "raw -> calibrated" with the
        exact multipliers and rescue rationale.
        """
        mults = self._AGGRESSION_MULTIPLIERS.get(
            self.config.aggression,
            self._AGGRESSION_MULTIPLIERS["balanced"],
        )
        cal_conviction = _clamp01(raw_response.conviction * mults["conviction"])
        cal_intensity = _clamp01(
            raw_response.recommended_intensity * mults["intensity"]
        )
        cal_direction = raw_response.direction
        cal_regime = raw_response.regime
        rescue_fired = False
        rescue_reason: str | None = None

        # ---- HOLD-rescue (aggressive only) -----------------------
        if (
            self.config.aggression == "aggressive"
            and raw_response.regime == "hold"
            and briefing.level1_passes
            and briefing.level2_conviction >= self.config.hold_rescue_l2_min
            and briefing.level2_direction_sign != 0
        ):
            # Convert L2's direction sign into the matching string.
            cal_direction = (
                "long" if briefing.level2_direction_sign > 0 else "short"
            )
            cal_regime = "risk_on"
            cal_intensity = max(
                cal_intensity,
                _clamp01(self.config.hold_rescue_intensity * mults["intensity"]),
            )
            # Conviction stays at Claude's reported level, but with
            # a small floor so the engine actually opens (the router
            # ignores zero-conviction opens elsewhere).
            cal_conviction = max(
                cal_conviction,
                briefing.level2_conviction * 0.5,
            )
            rescue_fired = True
            rescue_reason = (
                f"HOLD rescued: L1 passes + L2 conviction "
                f"{briefing.level2_conviction:.2f} >= "
                f"{self.config.hold_rescue_l2_min:.2f}; "
                f"opening {cal_direction} at intensity {cal_intensity:.2f}"
            )
            logger.warning(
                "L3 HOLD-rescue | L2 conv={:.2f} dir={}; opening {} intensity={:.2f}",
                briefing.level2_conviction,
                briefing.level2_direction_sign,
                cal_direction, cal_intensity,
            )

        calibrated = ArbiterResponse(
            conviction=cal_conviction,
            direction=cal_direction,
            regime=cal_regime,
            recommended_intensity=cal_intensity,
            rationale=raw_response.rationale,
            key_factors=list(raw_response.key_factors),
        )
        calibration = {
            "aggression": self.config.aggression,
            "multipliers": dict(mults),
            "raw_conviction": raw_response.conviction,
            "raw_direction": raw_response.direction,
            "raw_regime": raw_response.regime,
            "raw_intensity": raw_response.recommended_intensity,
            "calibrated_conviction": cal_conviction,
            "calibrated_direction": cal_direction,
            "calibrated_regime": cal_regime,
            "calibrated_intensity": cal_intensity,
            "hold_rescue_fired": rescue_fired,
            "hold_rescue_reason": rescue_reason,
        }
        return calibrated, calibration

    # ------------------------------------------------------------------
    # Synthetic / fallback helpers
    # ------------------------------------------------------------------

    def _synthetic(
        self, briefing: ArbiterBriefing
    ) -> tuple[ArbiterResponse, dict[str, Any]]:
        """L3 placeholder when no OpenRouter client is wired.

        The engine then redistributes L3's weight back to L1 + L2, so
        this is purely cosmetic (for the panel + for ArbiterResponse-
        shape symmetry). The rationale follows the active mode's
        format so the demo panel stays visually consistent whether
        Claude is wired or not.
        """
        l1c = float(briefing.level1_conviction)
        l2c = float(briefing.level2_conviction)
        conviction = 0.5 * l1c + 0.5 * l2c
        weighted = (
            l1c * briefing.level1_direction_sign
            + l2c * briefing.level2_direction_sign
        )
        if weighted > 1e-6:
            direction: Literal["long", "short", "neutral"] = "long"
        elif weighted < -1e-6:
            direction = "short"
        else:
            direction = "neutral"
        regime: Literal["risk_on", "risk_off", "hold"] = "hold"
        if conviction >= 0.6 and direction != "neutral":
            regime = "risk_on"
        elif conviction <= 0.4:
            regime = "risk_off"
        response = ArbiterResponse(
            conviction=conviction,
            direction=direction,
            regime=regime,
            recommended_intensity=conviction,
            rationale=self._synthetic_rationale(
                briefing, conviction, direction, regime
            ),
            key_factors=[
                "synthetic placeholder (no OPENROUTER_API_KEY)",
                f"L1+L2 re-blend (conv={conviction:.2f})",
                f"L3 weight redistributed to L1+L2 in aggregator",
            ],
        )
        payload: dict[str, Any] = {
            "provider": "synthetic",
            "model": None,
            "mode": self.config.mode,
            "latency_ms": 0.0,
            "synthetic": True,
            "fallback": False,
            "response": response.model_dump(),
        }
        return response, payload

    def _synthetic_rationale(
        self,
        b: ArbiterBriefing,
        conviction: float,
        direction: str,
        regime: str,
    ) -> str:
        """Render a synthetic L3 rationale in the active mode's format."""
        if self.config.mode == "standard":
            return (
                "Synthetic L3 (OPENROUTER_API_KEY not configured). The "
                "verdict is a weighted re-blend of L1 and L2; L3's "
                "weight is redistributed back to L1+L2 in the aggregator."
            )
        # critical mode -> 5-section template (so the panel renders the
        # same structure whether real Claude is wired or not).
        return (
            "Market Context:\n"
            f"Synthetic L3 verdict on {b.primary_symbol}. "
            "Real Claude arbitration is disabled (no OPENROUTER_API_KEY). "
            f"L1 conviction={b.level1_conviction:.2f}, "
            f"L2 conviction={b.level2_conviction:.2f}.\n\n"
            "Key Signals Analysis:\n"
            f"- L1 direction sign = {b.level1_direction_sign:+d} "
            f"(trend signal from technicals).\n"
            f"- L2 market bias = {b.level2_market_bias} "
            f"(strength={b.level2_bias_strength:.2f}).\n"
            f"- Blended conviction = 0.5*L1 + 0.5*L2 = {conviction:.2f}.\n\n"
            "Contradictions & Risks:\n"
            "No real arbitration was performed; this is a deterministic "
            "re-blend of upstream levels. The engine redistributes L3's "
            "weight back to L1+L2 so this placeholder does not dilute "
            "the real signal.\n\n"
            "My Independent View:\n"
            "I cannot offer an independent view here - I am the "
            "synthetic placeholder. Set OPENROUTER_API_KEY in .env to "
            "enable real Claude Sonnet 4.6 arbitration.\n\n"
            "Final Recommendation:\n"
            f"{regime.upper()} / direction={direction} at conviction "
            f"{conviction:.2f}. Intensity mirrors conviction; L3's "
            "weight is redistributed to L1+L2 in aggregation."
        )

    @staticmethod
    def _describe_validation_error(
        exc: ValidationError, raw: dict[str, Any] | None
    ) -> str:
        """Render a Pydantic ValidationError as a one-line diagnosis.

        Tailored to the failure modes we see in practice — string
        length overruns (the dominant case in critical mode),
        out-of-range floats, unknown enum values, missing fields.
        Falls back to the raw error list when the shape is anything
        else so the operator still gets something actionable.
        """
        errors = exc.errors() or []
        if not errors:
            return "no error details"
        first = errors[0]
        loc = ".".join(str(p) for p in first.get("loc", ())) or "<root>"
        etype = str(first.get("type", "unknown"))
        msg = str(first.get("msg", "")).strip()

        # Length overruns get a precise "X vs allowed Y" measurement
        # because that's the one and only thing the operator needs
        # to know (and the prompt's STRICT RESPONSE LENGTH RULES
        # exists specifically to prevent it).
        if etype.startswith("string_too_long") and isinstance(raw, dict):
            actual_field = raw.get(loc)
            actual_len = (
                len(actual_field) if isinstance(actual_field, str) else 0
            )
            ctx = first.get("ctx") or {}
            ceiling = ctx.get("max_length") or "?"
            return (
                f"field '{loc}' is {actual_len} chars vs ceiling "
                f"{ceiling} (raise rationale ceiling or tighten "
                "the system prompt)"
            )
        if etype.startswith("string_too_short"):
            return f"field '{loc}' is empty ({msg})"
        if etype.startswith("less_than") or etype.startswith(
            "greater_than"
        ):
            return f"field '{loc}' out of range ({msg})"
        if etype.startswith("literal_error") or etype == "enum":
            return f"field '{loc}' has unknown value ({msg})"
        if etype.startswith("missing"):
            return f"field '{loc}' missing from response"
        # Generic fallback - first two errors compactly listed.
        snippets = []
        for err in errors[:2]:
            loc_x = ".".join(str(p) for p in err.get("loc", ())) or "<root>"
            snippets.append(f"{loc_x}={err.get('msg', '?')}")
        return "; ".join(snippets)

    def _fallback_hold(
        self,
        briefing: ArbiterBriefing,
        *,
        reason: str,
        latency_ms: float,
        errors: str,
    ) -> tuple[ArbiterResponse, dict[str, Any]]:
        """Conservative fallback when the arbiter errors or returns junk."""
        # Trim the human-readable reason for the key_factors slot
        # (panel shows 1-line bullets). The full untruncated text
        # lives in payload["fallback_reason"] for log forensics.
        short_reason = reason if len(reason) <= 160 else reason[:157] + "..."
        response = ArbiterResponse(
            conviction=0.0,
            direction="neutral",
            regime="hold",
            recommended_intensity=0.0,
            rationale=self._fallback_rationale(briefing, reason),
            key_factors=[
                "arbiter call failed (safe-hold engaged)",
                short_reason,
                "neutral fallback; engine holds flat - investigate "
                "fallback_reason + check L3 system prompt vs the "
                "rationale length ceiling",
            ],
        )
        payload: dict[str, Any] = {
            "provider": "openrouter",
            "model": self.config.model,
            "mode": self.config.mode,
            "latency_ms": latency_ms,
            "synthetic": False,
            "fallback": True,
            "error": errors,
            "fallback_reason": reason,
            "response": response.model_dump(),
        }
        return response, payload

    def _fallback_rationale(self, b: ArbiterBriefing, reason: str) -> str:
        """Render the fallback rationale in the active mode's format."""
        if self.config.mode == "standard":
            return (
                "Arbiter returned an invalid response or the call failed - "
                "falling back to a neutral HOLD for safety. "
                f"Reason: {reason}"
            )
        return (
            "Market Context:\n"
            f"Arbiter call for {b.primary_symbol} did not produce a "
            "usable verdict, so the engine is safely holding flat.\n\n"
            "Key Signals Analysis:\n"
            "- Upstream signals could not be evaluated by the LLM on "
            "this cycle.\n"
            f"- Failure mode: {reason}\n"
            "- Fallback contract: conviction=0, direction=neutral, "
            "regime=hold, intensity=0.\n\n"
            "Contradictions & Risks:\n"
            "Trading on a hallucinated or partial LLM verdict is the "
            "primary risk; the fallback removes that risk entirely by "
            "holding flat.\n\n"
            "My Independent View:\n"
            "I am the safe-hold fallback. The right action under a "
            "failed arbitration is *no action* - the engine will retry "
            "on the next cycle.\n\n"
            "Final Recommendation:\n"
            "HOLD flat. Conviction 0.0, intensity 0.0. Investigate the "
            "underlying error before the next cycle."
        )


__all__ = [
    "Level3",
    "Level3Config",
    "L3Mode",
    "Level3Arbiter",
    "ArbiterBriefing",
    "ArbiterResponse",
]
