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
    # 4000 chars fits the critical-mode 5-section rationale (~1500-2500
    # chars typical) with room for verbose signal tables; the standard-
    # mode rationale (1-2 sentences) is unaffected.
    rationale: str = Field(..., min_length=1, max_length=4000)
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
            response = ArbiterResponse.model_validate(raw_dict)
            elapsed_ms = (time.time() - started) * 1000.0
            logger.info(
                "L3 OpenRouter OK | conv={:.2f} dir={} regime={} "
                "intensity={:.2f} ({:.0f}ms)",
                response.conviction,
                response.direction,
                response.regime,
                response.recommended_intensity,
                elapsed_ms,
            )
            payload: dict[str, Any] = {
                "provider": "openrouter",
                "model": self.config.model,
                "mode": self.config.mode,
                "latency_ms": elapsed_ms,
                "synthetic": False,
                "fallback": False,
                "raw_response": raw_dict,
                "response": response.model_dump(),
            }
            return response, payload
        except ValidationError as exc:
            elapsed_ms = (time.time() - started) * 1000.0
            logger.error(
                "L3 arbiter returned malformed JSON ({:.0f}ms) - falling "
                "back to neutral hold. Errors: {}",
                elapsed_ms,
                exc.errors()[:3],
            )
            response, payload = self._fallback_hold(
                briefing,
                reason="OpenRouter schema validation failed",
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
            lines.append(
                f"  - OI_usd={oi.get('current_value_usd', 0):,.0f}  "
                f"OI(1h)={oi.get('delta_1h_pct', 0):+.2f}%  "
                f"OI(24h)={oi.get('delta_24h_pct', 0):+.2f}%"
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

    def _fallback_hold(
        self,
        briefing: ArbiterBriefing,
        *,
        reason: str,
        latency_ms: float,
        errors: str,
    ) -> tuple[ArbiterResponse, dict[str, Any]]:
        """Conservative fallback when the arbiter errors or returns junk."""
        response = ArbiterResponse(
            conviction=0.0,
            direction="neutral",
            regime="hold",
            recommended_intensity=0.0,
            rationale=self._fallback_rationale(briefing, reason),
            key_factors=[
                "arbiter call failed (safe-hold engaged)",
                f"reason: {reason[:120]}",
                "neutral fallback; engine holds flat",
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
