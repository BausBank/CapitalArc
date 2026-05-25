"""Smoke tests for the Level 3 OpenRouter / Claude final arbiter.

The tests don't actually call OpenRouter - they exercise the arbiter's
prompt rendering, response validation and the two safety paths:

* Synthetic placeholder when no client is wired.
* Fallback HOLD when the client returns malformed / partial JSON.

We also exercise the full cascade through `DecisionEngine.decide` with
stubbed L1 / L2 / L3 to make sure the engine consumes Level 3's verdict
and aggregates `(conviction, direction)` correctly across all three
levels - and that a real arbiter call no longer triggers L3 weight
redistribution.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from src.core.decision_engine import DecisionEngine, LevelScore
from src.core.level3 import (
    ArbiterBriefing,
    ArbiterResponse,
    Level3,
    Level3Arbiter,
    Level3Config,
)
from src.llm.openrouter_client import OpenRouterClient


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _briefing(
    primary_symbol: str = "BTC-PERP",
    l1_conv: float = 0.7,
    l1_dir: int = 1,
    l2_conv: float = 0.6,
    l2_dir: int = 1,
    bias: str = "bullish",
    bias_strength: float = 0.7,
) -> ArbiterBriefing:
    return ArbiterBriefing(
        primary_symbol=primary_symbol,
        level1_conviction=l1_conv,
        level1_direction_sign=l1_dir,
        level1_rationale=f"L1 OK trend={'up' if l1_dir > 0 else 'down'}",
        level1_passes=True,
        level1_raw={
            "passes": True,
            "score": l1_conv,
            "per_symbol": {
                primary_symbol: {
                    "trend": "up" if l1_dir > 0 else "down",
                    "passes": True,
                    "atr_pct_avg": 1.2,
                }
            },
        },
        level2_conviction=l2_conv,
        level2_direction_sign=l2_dir,
        level2_rationale=f"L2 bias={bias}",
        level2_market_bias=bias,
        level2_bias_strength=bias_strength,
        level2_market_heat=0.7 if l2_dir > 0 else 0.3,
        level2_regime="risk_on",
        level2_raw={
            "market_bias": bias,
            "bias_strength": bias_strength,
            "market_heat": 0.7 if l2_dir > 0 else 0.3,
            "regime": "risk_on",
            "per_symbol": {
                primary_symbol: {
                    "funding": {
                        "current_rate": 0.0001,
                        "annualised_pct": 10.5,
                        "rate_8h_change": 0.00002,
                    },
                    "open_interest": {
                        "current_value_usd": 1_000_000.0,
                        "delta_1h_pct": 1.5,
                        "delta_24h_pct": 4.0,
                    },
                    "volume": {
                        "last_price": 60000.0,
                        "price_change_pct_24h": 2.5,
                        "spike_detected": False,
                    },
                    "long_short": {
                        "long_short_ratio": 1.3,
                        "long_account_pct": 0.56,
                        "inferred_bias": "long",
                    },
                    "whales": {
                        "flagged": True,
                        "direction": "accumulating",
                        "n_whales": 3,
                    },
                }
            },
            "vault_flow": {
                "tvl_usdc": 2_500_000.0,
                "net_flow_usdc": 50_000.0,
                "window_hours": 24,
            },
        },
        market_snapshot={
            "symbol": primary_symbol,
            "symbols": ["BTC-PERP", "ETH-PERP"],
            "account_margin_usdc": 1000.0,
            "account_unrealized_pnl_usdc": -5.0,
            "account_drawdown_pct": 0.5,
            "dune_chain": "ethereum",
        },
    )


# ---------------------------------------------------------------------------
# ArbiterResponse validation
# ---------------------------------------------------------------------------


def test_arbiter_response_validates_and_exposes_direction_sign() -> None:
    r = ArbiterResponse(
        conviction=0.82,
        direction="short",
        regime="risk_on",
        recommended_intensity=0.6,
        rationale="L2 shows a clear bearish bias.",
        key_factors=["bearish bias", "negative funding"],
    )
    assert r.direction_sign == -1
    assert 0.0 <= r.conviction <= 1.0
    assert r.key_factors == ["bearish bias", "negative funding"]


def test_arbiter_response_rejects_out_of_range_conviction() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        ArbiterResponse(
            conviction=1.5,
            direction="long",
            regime="risk_on",
            recommended_intensity=0.5,
            rationale="x",
            key_factors=[],
        )


def test_arbiter_response_rejects_unknown_direction() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        ArbiterResponse(
            conviction=0.5,
            direction="sideways",  # type: ignore[arg-type]
            regime="hold",
            recommended_intensity=0.5,
            rationale="x",
            key_factors=[],
        )


def test_arbiter_response_clamps_key_factors_to_six() -> None:
    r = ArbiterResponse(
        conviction=0.5,
        direction="long",
        regime="hold",
        recommended_intensity=0.5,
        rationale="x",
        key_factors=[f"factor_{i}" for i in range(10)],
    )
    assert len(r.key_factors) == 6


# ---------------------------------------------------------------------------
# Synthetic placeholder (no client)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_level3_synthetic_without_client_blends_l1_l2() -> None:
    """When `client=None`, L3 should re-blend L1+L2 deterministically."""
    level3 = Level3(client=None)
    score = await level3.score(_briefing(l1_conv=0.8, l1_dir=1, l2_conv=0.4, l2_dir=1))
    assert score.level == 3
    assert score.raw["l3"]["synthetic"] is True
    assert score.direction_sign == 1
    # 0.5*0.8 + 0.5*0.4 = 0.6
    assert score.score == pytest.approx(0.6)


@pytest.mark.asyncio
async def test_level3_synthetic_neutral_when_directions_cancel() -> None:
    level3 = Level3(client=None)
    score = await level3.score(_briefing(l1_conv=0.5, l1_dir=1, l2_conv=0.5, l2_dir=-1))
    assert score.direction_sign == 0


# ---------------------------------------------------------------------------
# Real client path (mocked Gemini round-trip)
# ---------------------------------------------------------------------------


def _mock_openrouter_client(payload: dict[str, Any]) -> OpenRouterClient:
    """Build an OpenRouterClient whose ``generate_json`` returns ``payload``.

    Bypasses ``__init__`` (which would try to open an httpx pool) so the
    test never touches the network.
    """
    client = OpenRouterClient.__new__(OpenRouterClient)  # type: ignore[call-arg]
    client.config = type(
        "C",
        (),
        {
            "model": "anthropic/claude-sonnet-4.6",
            "max_retries": 0,
            "backoff_seconds": 0.0,
            "timeout_seconds": 5,
        },
    )()
    client.generate_json = AsyncMock(return_value=payload)  # type: ignore[attr-defined]
    return client


@pytest.mark.asyncio
async def test_level3_uses_arbiter_response_when_client_returns_valid_json() -> None:
    payload = {
        "conviction": 0.82,
        "direction": "short",
        "regime": "risk_on",
        "recommended_intensity": 0.6,
        "rationale": "L2 is decisively bearish, funding is negative.",
        "key_factors": ["bearish bias", "negative funding"],
    }
    client = _mock_openrouter_client(payload)
    level3 = Level3(client=client)
    score = await level3.score(_briefing(bias="bearish", l2_dir=-1))
    assert score.score == pytest.approx(0.82)
    assert score.direction_sign == -1
    assert score.raw["l3"]["provider"] == "openrouter"
    assert score.raw["l3"]["model"] == "anthropic/claude-sonnet-4.6"
    assert score.raw["l3"]["synthetic"] is False
    assert score.raw["l3"]["fallback"] is False
    assert score.raw["l3"]["response"]["regime"] == "risk_on"


@pytest.mark.asyncio
async def test_level3_falls_back_to_hold_on_invalid_arbiter_payload() -> None:
    """Pydantic rejects, arbiter must emit a safe neutral hold."""
    client = _mock_openrouter_client({"this": "is", "not": "valid"})
    level3 = Level3(client=client)
    score = await level3.score(_briefing())
    assert score.score == 0.0
    assert score.direction_sign == 0
    assert score.raw["l3"]["fallback"] is True
    assert score.raw["l3"]["response"]["regime"] == "hold"


@pytest.mark.asyncio
async def test_level3_falls_back_to_hold_when_arbiter_raises() -> None:
    client = OpenRouterClient.__new__(OpenRouterClient)  # type: ignore[call-arg]
    client.config = type(
        "C",
        (),
        {
            "model": "anthropic/claude-sonnet-4.6",
            "max_retries": 0,
            "backoff_seconds": 0.0,
        },
    )()
    client.generate_json = AsyncMock(side_effect=RuntimeError("boom"))  # type: ignore[attr-defined]
    level3 = Level3(client=client)
    score = await level3.score(_briefing())
    assert score.score == 0.0
    assert score.raw["l3"]["fallback"] is True
    assert "boom" in score.raw["l3"]["error"]


# ---------------------------------------------------------------------------
# Prompt rendering
# ---------------------------------------------------------------------------


def test_user_prompt_mentions_primary_symbol_and_metrics() -> None:
    arbiter = Level3Arbiter(client=None, config=Level3Config())
    text = arbiter._render_user_prompt(_briefing(primary_symbol="ETH-PERP"))
    # Should mention the primary symbol, L1+L2 sections and the arbiter task.
    assert "ETH-PERP" in text
    assert "Level 1" in text
    assert "Level 2" in text
    assert "JSON" in text
    # Should expose at least one of the on-chain metric labels.
    assert "funding" in text.lower()
    assert "oi(1h)" in text.lower() or "open_interest" in text.lower()


def test_system_prompt_loads_from_disk_when_present() -> None:
    arbiter = Level3Arbiter(client=None, config=Level3Config())
    prompt = arbiter._load_system_prompt()
    # Either the on-disk prompt or the inline fallback must be present
    # and mention the agent's purpose.
    assert "CapitalArc" in prompt or "arbiter" in prompt.lower()


# ---------------------------------------------------------------------------
# Mode resolution (critical vs standard)
# ---------------------------------------------------------------------------


def test_level3config_defaults_to_critical_mode() -> None:
    """Critical is the demo-default persona; do not regress this silently."""
    cfg = Level3Config()
    assert cfg.mode == "critical"
    assert cfg.resolved_prompt_path().name == "level3_arbiter_critical.md"


def test_level3config_resolves_standard_mode_prompt_path() -> None:
    cfg = Level3Config(mode="standard")
    assert cfg.resolved_prompt_path().name == "level3_arbiter_standard.md"


def test_level3config_explicit_path_overrides_mode() -> None:
    """An explicit ``system_prompt_path`` wins over mode-based resolution.

    This is the unit-test escape hatch — lets us point the arbiter at a
    custom prompt without monkey-patching ``_PROMPT_PATHS``.
    """
    from pathlib import Path

    custom = Path("/tmp/custom_prompt.md")
    cfg = Level3Config(mode="critical", system_prompt_path=custom)
    assert cfg.resolved_prompt_path() == custom


def test_critical_mode_loads_5_section_template_from_disk() -> None:
    """The on-disk critical prompt must advertise the 5-section template."""
    arbiter = Level3Arbiter(client=None, config=Level3Config(mode="critical"))
    prompt = arbiter._load_system_prompt()
    # The critical prompt must spell out every section so Claude can
    # follow the template verbatim.
    for header in (
        "Market Context:",
        "Key Signals Analysis:",
        "Contradictions & Risks:",
        "My Independent View:",
        "Final Recommendation:",
    ):
        assert header in prompt, f"missing section header: {header!r}"


def test_standard_mode_loads_trader_voice_prompt() -> None:
    arbiter = Level3Arbiter(client=None, config=Level3Config(mode="standard"))
    prompt = arbiter._load_system_prompt()
    # Standard prompt advertises the trader-voice format, not the
    # critical-mode template.
    assert "STANDARD" in prompt or "trader voice" in prompt.lower()


def test_user_prompt_includes_mode_specific_instruction_critical() -> None:
    arbiter = Level3Arbiter(client=None, config=Level3Config(mode="critical"))
    text = arbiter._render_user_prompt(_briefing())
    assert "CRITICAL MODE" in text
    # The mode reminder echoes every section name so Claude can't
    # silently collapse the rationale.
    for header_name in (
        "Market Context",
        "Key Signals Analysis",
        "Contradictions & Risks",
        "My Independent View",
        "Final Recommendation",
    ):
        assert header_name in text


def test_user_prompt_includes_mode_specific_instruction_standard() -> None:
    arbiter = Level3Arbiter(client=None, config=Level3Config(mode="standard"))
    text = arbiter._render_user_prompt(_briefing())
    assert "STANDARD MODE" in text
    assert "1-2 sentences" in text or "1-2 sentence" in text


# ---------------------------------------------------------------------------
# ArbiterResponse accepts the longer critical-mode rationale
# ---------------------------------------------------------------------------


def test_arbiter_response_accepts_5_section_critical_rationale() -> None:
    """A realistic 5-section rationale (~1500 chars) must validate."""
    structured = (
        "Market Context:\n"
        "BTC-PERP at $62,400 with -2.5% over 24h, ATR% 1.2%, drawdown 0.3%.\n\n"
        "Key Signals Analysis:\n"
        "- L1 trend = down on both 15m and 1h, EMA9 < EMA21.\n"
        "- L2 market_bias = bearish (strength=0.86) with convergent on-chain signals.\n"
        "- Funding -0.015% per 8h annualises to -16.4% APR (longs paying).\n"
        "- Whale flow distributing (3 whales, -50k USDC net).\n"
        "- OI 24h delta -4% confirming directional unwind.\n\n"
        "Contradictions & Risks:\n"
        "No material contradictions; signals converge on the bearish side.\n\n"
        "My Independent View:\n"
        "I fully agree with L2's bearish read. Funding, OI and whales paint "
        "a consistent picture; L1 confirms via trend.\n\n"
        "Final Recommendation:\n"
        "Open SHORT at intensity 0.6 - strong on-chain bearish convergence "
        "with low drawdown leaves room to add."
    )
    r = ArbiterResponse(
        conviction=0.86,
        direction="short",
        regime="risk_on",
        recommended_intensity=0.6,
        rationale=structured,
        key_factors=[
            "converging bearish on-chain signals (funding -16.4%, OI -4%)",
            "L1 trend down on both timeframes",
            "whale distribution (3 whales, -50k USDC)",
        ],
    )
    assert r.direction_sign == -1
    assert "Market Context:" in r.rationale
    assert "Final Recommendation:" in r.rationale


def test_arbiter_response_accepts_long_critical_rationale_up_to_8000() -> None:
    """A 6000-char rationale (chatty critical-mode output) must validate.

    Regression guard for the failure mode where Claude returns a
    detailed override audit longer than the original 4000-char cap.
    """
    long_rationale = (
        "Market Context:\n"
        + ("BTC-PERP trending up; ATR% 1.2%. " * 60)
        + "\n\nKey Signals Analysis:\n"
        + ("- bullet point with concrete numeric reference.\n" * 40)
        + "\nContradictions & Risks:\nNo material contradictions.\n\n"
        + "My Independent View:\n"
        + ("Agree with upstream signals on balance. " * 30)
        + "\n\nFinal Recommendation:\nOpen LONG at intensity 0.6."
    )
    assert 3500 < len(long_rationale) <= 8000, (
        f"test fixture must straddle the new ceiling, got {len(long_rationale)}"
    )
    r = ArbiterResponse(
        conviction=0.7,
        direction="long",
        regime="risk_on",
        recommended_intensity=0.6,
        rationale=long_rationale,
        key_factors=["multi-timeframe trend confirmed"],
    )
    assert r.direction_sign == 1


def test_arbiter_response_rejects_rationale_above_8000_chars() -> None:
    """The 8000-char ceiling is a defence-in-depth backstop and must hold."""
    overflow = "x" * 8001
    with pytest.raises(ValidationError):
        ArbiterResponse(
            conviction=0.5,
            direction="neutral",
            regime="hold",
            recommended_intensity=0.0,
            rationale=overflow,
        )


def test_describe_validation_error_pinpoints_rationale_length_overrun() -> None:
    """The fallback diagnosis must name the field, actual length, ceiling."""
    overflow = "x" * 9000  # well over the 8000 ceiling
    try:
        ArbiterResponse(
            conviction=0.5,
            direction="neutral",
            regime="hold",
            recommended_intensity=0.0,
            rationale=overflow,
        )
    except ValidationError as exc:
        diagnosis = Level3Arbiter._describe_validation_error(
            exc, {"rationale": overflow, "conviction": 0.5}
        )
    else:  # pragma: no cover - defensive
        raise AssertionError("expected ValidationError for 9000-char rationale")

    assert "rationale" in diagnosis
    assert "9000" in diagnosis
    assert "8000" in diagnosis
    assert "ceiling" in diagnosis.lower()


def test_describe_validation_error_handles_missing_field() -> None:
    """Missing-field errors produce a clear 'field X missing' diagnosis."""
    try:
        ArbiterResponse(
            direction="neutral",
            regime="hold",
            recommended_intensity=0.0,
            rationale="x",
        )  # type: ignore[call-arg]
    except ValidationError as exc:
        diagnosis = Level3Arbiter._describe_validation_error(exc, {})
    else:  # pragma: no cover
        raise AssertionError("expected ValidationError for missing conviction")

    assert "conviction" in diagnosis
    assert "missing" in diagnosis.lower()


def test_describe_validation_error_handles_out_of_range_float() -> None:
    try:
        ArbiterResponse(
            conviction=1.5,  # out of [0, 1]
            direction="neutral",
            regime="hold",
            recommended_intensity=0.0,
            rationale="x",
        )
    except ValidationError as exc:
        diagnosis = Level3Arbiter._describe_validation_error(
            exc, {"conviction": 1.5}
        )
    else:  # pragma: no cover
        raise AssertionError("expected ValidationError for conviction>1")

    assert "conviction" in diagnosis
    assert "out of range" in diagnosis.lower() or "less than" in diagnosis.lower()


# ---------------------------------------------------------------------------
# Cascade integration
# ---------------------------------------------------------------------------


class _StubLevel1:
    def __init__(self, score: float, direction: int, passes: bool = True) -> None:
        self._score = score
        self._direction = direction
        self._passes = passes

    async def score(self, market: dict[str, Any]) -> LevelScore:  # noqa: ARG002
        return LevelScore(
            level=1,
            score=self._score,
            rationale="stub L1",
            raw={
                "l1": {
                    "passes": self._passes,
                    "score": self._score,
                    "per_symbol": {
                        "BTC-PERP": {
                            "trend": "up" if self._direction > 0 else "down",
                            "passes": self._passes,
                            "atr_pct_avg": 1.0,
                        }
                    },
                }
            },
            direction_sign=self._direction,
        )


class _StubLevel2:
    def __init__(
        self, score: float, direction: int, bias: str, strength: float
    ) -> None:
        self._score = score
        self._direction = direction
        self._bias = bias
        self._strength = strength

    async def score(self, market: dict[str, Any]) -> LevelScore:  # noqa: ARG002
        return LevelScore(
            level=2,
            score=self._score,
            rationale="stub L2",
            raw={
                "l2": {
                    "market_bias": self._bias,
                    "bias_strength": self._strength,
                    "market_heat": 0.7 if self._direction > 0 else 0.3,
                    "regime": "risk_on",
                    "per_symbol": {},
                    "vault_flow": {},
                }
            },
            direction_sign=self._direction,
        )


@pytest.mark.asyncio
async def test_cascade_with_real_l3_aggregates_all_three_levels() -> None:
    """When L3 is real (non-synthetic), its weight is *not* redistributed."""
    payload = {
        "conviction": 0.9,
        "direction": "long",
        "regime": "risk_on",
        "recommended_intensity": 0.8,
        "rationale": "Strong bullish setup.",
        "key_factors": ["L1 trend up", "L2 bullish bias"],
    }
    level3 = Level3(client=_mock_openrouter_client(payload))

    engine = DecisionEngine(
        level1=_StubLevel1(score=0.7, direction=1),  # type: ignore[arg-type]
        level2=_StubLevel2(  # type: ignore[arg-type]
            score=0.7, direction=1, bias="bullish", strength=0.7
        ),
        level3=level3,
        weights={"level1": 0.25, "level2": 0.35, "level3": 0.40},
        risk_on_threshold=0.6,
        risk_off_threshold=0.4,
    )
    decision = await engine.decide({"symbols": ["BTC-PERP"], "symbol": "BTC-PERP"})

    # Effective weights should NOT be redistributed: real L3 voted.
    assert decision.effective_weights["level3"] == pytest.approx(0.40)
    assert decision.effective_weights["level1"] == pytest.approx(0.25)
    # Aggregate conviction = 0.25*0.7 + 0.35*0.7 + 0.40*0.9 = 0.78
    assert decision.final_score == pytest.approx(0.78, abs=1e-6)
    assert decision.final_direction == 1
    assert decision.directive.action == "risk_on"
    assert decision.directive.side == "long"


@pytest.mark.asyncio
async def test_cascade_with_synthetic_l3_redistributes_weight() -> None:
    """No client -> synthetic L3 -> L3 weight redistributed to L1+L2."""
    engine = DecisionEngine(
        level1=_StubLevel1(score=0.7, direction=1),  # type: ignore[arg-type]
        level2=_StubLevel2(  # type: ignore[arg-type]
            score=0.7, direction=1, bias="bullish", strength=0.7
        ),
        level3=None,
        weights={"level1": 0.25, "level2": 0.35, "level3": 0.40},
        risk_on_threshold=0.6,
        risk_off_threshold=0.4,
        redistribute_synthetic_l3_weight=True,
    )
    decision = await engine.decide({"symbols": ["BTC-PERP"], "symbol": "BTC-PERP"})
    assert decision.effective_weights["level3"] == pytest.approx(0.0)
    # Conviction collapses to the L1+L2 blend (== 0.7 since both equal).
    assert decision.final_score == pytest.approx(0.7, abs=1e-6)


# ---------------------------------------------------------------------------
# L1-block override: briefing transparency + L3 override path (Day 5)
# ---------------------------------------------------------------------------
# These tests pin down the "L3 sees why L1 blocked and can override
# soft vetoes" contract added in Day 5. They cover four cases:
#   1. The briefing carries `l1_blocked` + reasons + per-(symbol, tf)
#      indicators, and the user prompt actually renders them.
#   2. Soft L1 block + strong L3 conviction -> override fires; the
#      decision opens at L3's verdict and is flagged `l1_overridden_by_l3`.
#   3. Hard L1 block (drawdown_breach) -> override is NEVER attempted,
#      even with a confident L3 - drawdown is sacred.
#   4. Soft L1 block + L3 returns a hold / low conviction -> engine
#      respects L1 and short-circuits as legacy.


class _BlockedStubLevel1:
    """Stub Level 1 that ALWAYS returns a BLOCKED verdict.

    Knobs:
        ``hard``: when True, the block is `drawdown_breach` (a HARD
                  block - never overrideable). When False, the block
                  is `rsi_overbought` (a SOFT block - eligible for
                  L3 override).
        ``rsi``:  RSI value reported in the indicator row so the
                  prompt-rendering test can assert it shows up.
        ``extra_soft_codes``: optional list of extra SOFT block codes
                  to stack into the payload. Used by the stacked-veto
                  haircut tests.

    The stub mirrors real L1's contract: every blocking reason
    carries an explicit ``is_hard`` flag, and soft blocks include the
    marginality metadata (``severity_label`` / ``margin`` /
    ``margin_pct_of_threshold``) that L1's ``_margin_metadata`` helper
    produces.
    """

    def __init__(
        self,
        *,
        hard: bool = False,
        rsi: float = 72.4,
        extra_soft_codes: list[str] | None = None,
    ) -> None:
        self._hard = hard
        self._rsi = rsi
        self._extra_soft_codes = list(extra_soft_codes or [])

    @staticmethod
    def _soft_margin(value: float, threshold: float) -> dict[str, Any]:
        margin = value - threshold
        margin_pct = (margin / threshold * 100.0) if threshold else 0.0
        if abs(margin_pct) < 5.0:
            label = "marginal"
        elif abs(margin_pct) < 20.0:
            label = "moderate"
        else:
            label = "decisive"
        return {
            "value": value,
            "threshold": threshold,
            "margin": margin,
            "margin_pct_of_threshold": margin_pct,
            "severity_label": label,
        }

    def _build_blocking_reasons(self) -> list[dict[str, Any]]:
        """Construct the list of blocking reasons matching real L1."""
        if self._hard:
            return [
                {
                    "code": "drawdown_breach",
                    "severity": "block",
                    "message": "drawdown 11.5% >= 10% limit",
                    "symbol": None,
                    "timeframe": None,
                    "is_hard": True,
                    "metadata": {
                        "drawdown_pct": 11.5,
                        "max_drawdown_pct": 10.0,
                        "breach_margin_pct": 1.5,
                    },
                },
            ]
        reasons: list[dict[str, Any]] = [
            {
                "code": "rsi_overbought",
                "severity": "block",
                "message": f"BTC-PERP@1h RSI={self._rsi:.1f} >= 70",
                "symbol": "BTC-PERP",
                "timeframe": "1h",
                "is_hard": False,
                "metadata": {
                    "rsi": self._rsi,
                    **self._soft_margin(value=self._rsi, threshold=70.0),
                },
            }
        ]
        # Extra soft codes for stacked-veto tests.
        for code in self._extra_soft_codes:
            if code == "atr_too_high":
                reasons.append({
                    "code": "atr_too_high",
                    "severity": "block",
                    "message": "BTC-PERP@15m ATR%=6.20% > 6.00%",
                    "symbol": "BTC-PERP",
                    "timeframe": "15m",
                    "is_hard": False,
                    "metadata": {
                        "atr_pct": 6.2,
                        **self._soft_margin(value=6.2, threshold=6.0),
                    },
                })
            elif code == "trend_mixed":
                reasons.append({
                    "code": "trend_mixed",
                    "severity": "block",
                    "message": "BTC-PERP: trend disagrees across 15m, 1h",
                    "symbol": "BTC-PERP",
                    "timeframe": None,
                    "is_hard": False,
                    "metadata": {
                        "per_tf_trend": ["up", "down"],
                        "n_timeframes": 2,
                        "n_dissenters": 1,
                        "dominant_count": 1,
                        "dissent_pct": 50.0,
                        "severity_label": "decisive",
                    },
                })
        return reasons

    async def score(self, market: dict[str, Any]) -> LevelScore:  # noqa: ARG002
        reasons = self._build_blocking_reasons()
        # Mirror the dict produced by `_decision_to_dict` so the
        # engine's `_extract_l1_block_info` finds everything it needs.
        per_symbol = {
            "BTC-PERP": {
                "trend": "up",
                "passes": False,
                "atr_pct_avg": 1.2,
                "rows": [
                    {
                        "timeframe": "15m",
                        "close": 62000.0,
                        "ema_fast": 61800.0,
                        "ema_slow": 61400.0,
                        "rsi": self._rsi - 4.0,
                        "atr": 60.0,
                        "atr_pct": 0.10,
                        "trend": "up",
                    },
                    {
                        "timeframe": "1h",
                        "close": 62000.0,
                        "ema_fast": 61500.0,
                        "ema_slow": 60800.0,
                        "rsi": self._rsi,
                        "atr": 120.0,
                        "atr_pct": 0.20,
                        "trend": "up",
                    },
                ],
                "blocking_reasons": [
                    r for r in reasons if r.get("symbol") == "BTC-PERP"
                ],
                "informational_reasons": [],
            }
        }
        primary_code = reasons[0]["code"]
        return LevelScore(
            level=1,
            score=0.0,
            rationale=f"L1 BLOCKED on primary=BTC-PERP: {primary_code}",
            raw={
                "l1": {
                    "passes": False,
                    "score": 0.0,
                    "rationale": f"L1 BLOCKED: {primary_code}",
                    "reasons": reasons,
                    "per_symbol": per_symbol,
                }
            },
            direction_sign=0,
        )


def test_arbiter_briefing_carries_l1_block_payload_and_prompt_renders_it() -> None:
    """When `l1_blocked=True`, the user prompt must surface the audit packet.

    Specifically the rendered Markdown must contain:
      * the front-loaded **DECISION TASK** header so Claude sees the
        audit framing FIRST, before any data
      * the `⚠ Level 1 BLOCKED` section header
      * the soft block code (so Claude knows what to argue with)
      * the pre-computed marginality label (so Claude doesn't have
        to compute "+X.X pts beyond" mentally)
      * the indicator value (RSI) so Claude can second-guess
      * the `l1_indicators` table header
    """
    arbiter = Level3Arbiter(client=None, config=Level3Config(mode="critical"))
    briefing = _briefing(primary_symbol="BTC-PERP")
    briefing.l1_blocked = True
    briefing.l1_blocked_reasons = [
        {
            "code": "rsi_overbought",
            "severity": "block",
            "message": "BTC-PERP@1h RSI=72.4 >= 70",
            "symbol": "BTC-PERP",
            "timeframe": "1h",
            "metadata": {
                "rsi": 72.4,
                "value": 72.4,
                "threshold": 70.0,
                "margin": 2.4,
                "margin_pct_of_threshold": 3.43,
                "severity_label": "marginal",
            },
            "is_hard": False,
        },
    ]
    briefing.l1_indicators = [
        {
            "symbol": "BTC-PERP",
            "timeframe": "1h",
            "close": 62000.0,
            "ema_fast": 61500.0,
            "ema_slow": 60800.0,
            "rsi": 72.4,
            "atr": 120.0,
            "atr_pct": 0.20,
            "trend": "up",
        }
    ]
    text = arbiter._render_user_prompt(briefing)
    # DECISION TASK header is at the TOP of the prompt and frames the
    # entire arbitration as an audit task before any data appears.
    assert "DECISION TASK" in text
    assert "AUDIT each block" in text
    # The old per-block section is still rendered with all the
    # marginality data Claude needs.
    assert "Level 1 BLOCKED" in text
    assert "rsi_overbought" in text
    assert "OVERRIDE AUTHORITY" in text
    assert "l1_indicators" in text
    assert "72.4" in text  # the actual RSI is visible to Claude
    assert "ema9=61500" in text or "ema9=61500.00" in text
    # Marginality label is rendered INLINE so Claude doesn't have to
    # subtract threshold from value himself.
    assert "marginal" in text
    assert "+2.40" in text or "+2.4" in text


def test_arbiter_briefing_marks_hard_block_with_no_override_authority() -> None:
    arbiter = Level3Arbiter(client=None, config=Level3Config(mode="critical"))
    briefing = _briefing(primary_symbol="BTC-PERP")
    briefing.l1_blocked = True
    briefing.l1_blocked_reasons = [
        {
            "code": "drawdown_breach",
            "severity": "block",
            "message": "drawdown 11.5% >= 10% limit",
            "symbol": None,
            "timeframe": None,
            "metadata": {"drawdown_pct": 11.5},
            "is_hard": True,
        },
    ]
    text = arbiter._render_user_prompt(briefing)
    assert "drawdown_breach" in text
    # When a hard block is present the renderer should NOT advertise
    # override authority - the prompt must order a HOLD.
    assert "OVERRIDE AUTHORITY" not in text
    assert "HARD block is present" in text
    # The DECISION TASK header for the hard-block path must be
    # explicit about producing a thoughtful HOLD.
    assert "DECISION TASK" in text
    assert "HARD" in text
    assert "immutable" in text


def test_arbiter_briefing_stacked_soft_blocks_warns_about_intensity_haircut() -> None:
    """Two+ soft blocks -> the DECISION TASK header must warn about the
    engine's stacked-veto intensity haircut so Claude can size correctly."""
    arbiter = Level3Arbiter(client=None, config=Level3Config(mode="critical"))
    briefing = _briefing(primary_symbol="BTC-PERP")
    briefing.l1_blocked = True
    briefing.l1_blocked_reasons = [
        {
            "code": "rsi_overbought",
            "is_hard": False,
            "severity": "block",
            "message": "BTC-PERP@1h RSI=71 >= 70",
            "symbol": "BTC-PERP",
            "timeframe": "1h",
            "metadata": {"severity_label": "marginal"},
        },
        {
            "code": "atr_too_high",
            "is_hard": False,
            "severity": "block",
            "message": "ATR too high",
            "symbol": "BTC-PERP",
            "timeframe": "15m",
            "metadata": {"severity_label": "marginal"},
        },
        {
            "code": "trend_mixed",
            "is_hard": False,
            "severity": "block",
            "message": "trends disagree",
            "symbol": "BTC-PERP",
            "timeframe": None,
            "metadata": {"severity_label": "decisive"},
        },
    ]
    text = arbiter._render_user_prompt(briefing)
    # With 3 stacked soft blocks the DECISION TASK warning must
    # explicitly mention the intensity haircut so Claude doesn't get
    # confused when the engine clamps its number.
    assert "stacked" in text.lower()
    assert "halve" in text.lower() or "halv" in text.lower()


@pytest.mark.asyncio
async def test_l3_overrides_soft_l1_block_when_claude_returns_strong_verdict() -> None:
    """SOFT L1 block + strong real-L3 verdict -> trade opens on L3's authority."""
    payload = {
        "conviction": 0.78,
        "direction": "long",
        "regime": "risk_on",
        "recommended_intensity": 0.5,
        "rationale": (
            "Market Context:\nBTC-PERP at $62k, ATR% 0.20.\n\n"
            "Key Signals Analysis:\n- RSI 72.4 only marginally above 70.\n"
            "- L2 funding +0.01%, OI +1.5%, whales accumulating.\n\n"
            "Contradictions & Risks:\nL1 vetoed on rsi_overbought, but "
            "on-chain flow + indicator table clearly bullish.\n\n"
            "My Independent View:\nI am overriding L1's rsi_overbought "
            "veto because the on-chain mix dominates.\n\n"
            "Final Recommendation:\nOpen LONG at intensity 0.5 - override "
            "of soft L1 block justified by convergent on-chain bullish mix."
        ),
        "key_factors": [
            "L3 override of L1 rsi_overbought (on-chain bullish convergence)",
            "marginal RSI 72.4 vs threshold 70",
            "funding + OI + whales aligned long",
        ],
    }
    level3 = Level3(client=_mock_openrouter_client(payload))
    engine = DecisionEngine(
        level1=_BlockedStubLevel1(hard=False),  # type: ignore[arg-type]
        level2=_StubLevel2(  # type: ignore[arg-type]
            score=0.65, direction=1, bias="bullish", strength=0.7
        ),
        level3=level3,
        weights={"level1": 0.25, "level2": 0.35, "level3": 0.40},
        risk_on_threshold=0.6,
        risk_off_threshold=0.4,
        allow_l3_to_override_l1=True,
        l3_override_min_conviction=0.55,
    )
    decision = await engine.decide({"symbols": ["BTC-PERP"], "symbol": "BTC-PERP"})

    assert decision.l1_overridden_by_l3 is True
    assert decision.short_circuited is False
    assert decision.directive.action == "risk_on"
    assert decision.directive.side == "long"
    assert decision.final_direction == 1
    assert decision.final_score == pytest.approx(0.78, abs=1e-6)
    # The override path advertises L3 as the sole decision-maker
    # (level1 / level2 weights surface as zero in effective weights).
    assert decision.effective_weights["level3"] == pytest.approx(1.0)
    assert decision.effective_weights["level1"] == pytest.approx(0.0)
    # Single soft block => stacked-veto cap is 1.0 (no haircut).
    meta = decision.l1_override_meta
    assert meta is not None
    assert meta["status"] == "executed"
    assert meta["n_soft_blocks"] == 1
    assert meta["soft_block_codes"] == ["rsi_overbought"]
    assert meta["stacked_veto_cap"] == pytest.approx(1.0)
    assert meta["raw_intensity"] == pytest.approx(0.5)
    assert meta["calibrated_intensity"] == pytest.approx(0.5)


@pytest.mark.asyncio
async def test_hard_l1_block_invokes_l3_but_engine_still_short_circuits() -> None:
    """Drawdown breach + confident L3 -> engine vetoes L3, short-circuits.

    Day 5+: the arbiter is ALWAYS invoked when L1 blocks (so it gets
    the full briefing). The engine then enforces the hard-block
    guardrail regardless of L3's verdict - and logs a WARNING when L3
    attempts an override on a hard block.
    """
    payload = {
        "conviction": 0.95,
        "direction": "long",
        "regime": "risk_on",
        "recommended_intensity": 0.9,
        "rationale": "Even Claude can't override drawdown.",
        "key_factors": ["any factor"],
    }
    level3 = Level3(client=_mock_openrouter_client(payload))
    engine = DecisionEngine(
        level1=_BlockedStubLevel1(hard=True),  # type: ignore[arg-type]
        level2=_StubLevel2(  # type: ignore[arg-type]
            score=0.8, direction=1, bias="bullish", strength=0.9
        ),
        level3=level3,
        weights={"level1": 0.25, "level2": 0.35, "level3": 0.40},
        risk_on_threshold=0.6,
        risk_off_threshold=0.4,
        allow_l3_to_override_l1=True,
        l3_override_min_conviction=0.55,
    )
    decision = await engine.decide({"symbols": ["BTC-PERP"], "symbol": "BTC-PERP"})

    assert decision.short_circuited is True
    assert decision.l1_overridden_by_l3 is False
    assert decision.directive.action == "risk_off"
    assert decision.final_score == 0.0
    # L3 IS called even on hard blocks now - the arbiter always gets
    # the full briefing.
    level3.client.generate_json.assert_awaited_once()  # type: ignore[attr-defined]
    # The override-attempt is recorded for operator visibility.
    meta = decision.l1_override_meta
    assert meta is not None
    assert meta["status"] == "hard_block_uphold"
    assert meta["hard_block_codes"] == ["drawdown_breach"]
    assert meta["l3_conviction"] == pytest.approx(0.95)
    assert meta["l3_direction"] == "long"


@pytest.mark.asyncio
async def test_l3_declines_to_override_when_returning_hold() -> None:
    """SOFT L1 block + L3 returns hold -> respect the L1 block."""
    payload = {
        "conviction": 0.3,
        "direction": "neutral",
        "regime": "hold",
        "recommended_intensity": 0.0,
        "rationale": (
            "Market Context:\nBTC-PERP near range top.\n\n"
            "Key Signals Analysis:\n- RSI 72.4 truly overbought.\n"
            "- L2 bias only mildly bullish.\n\n"
            "Contradictions & Risks:\nNo material override case.\n\n"
            "My Independent View:\nI agree with L1's rsi_overbought veto.\n\n"
            "Final Recommendation:\nHOLD - respect L1 block."
        ),
        "key_factors": ["L3 declines override", "L2 too weak vs RSI extreme"],
    }
    level3 = Level3(client=_mock_openrouter_client(payload))
    engine = DecisionEngine(
        level1=_BlockedStubLevel1(hard=False),  # type: ignore[arg-type]
        level2=_StubLevel2(  # type: ignore[arg-type]
            score=0.4, direction=1, bias="bullish", strength=0.3
        ),
        level3=level3,
        weights={"level1": 0.25, "level2": 0.35, "level3": 0.40},
        allow_l3_to_override_l1=True,
        l3_override_min_conviction=0.55,
    )
    decision = await engine.decide({"symbols": ["BTC-PERP"], "symbol": "BTC-PERP"})

    assert decision.short_circuited is True
    assert decision.l1_overridden_by_l3 is False
    assert decision.directive.action == "risk_off"
    # But the L2 / L3 telemetry should be retained for the panel even
    # on the decline-to-override path (so the operator can see *why*
    # L3 declined).
    l3_score = decision.level_score(3)
    assert l3_score is not None
    assert l3_score.score == pytest.approx(0.3)
    assert l3_score.raw["l3"]["response"]["regime"] == "hold"
    # And the override meta records the decline + the reason so the
    # CLI banner can explain it to the operator without re-running L3.
    meta = decision.l1_override_meta
    assert meta is not None
    assert meta["status"] == "declined"
    assert meta["soft_block_codes"] == ["rsi_overbought"]
    assert "hold" in meta["decline_reason"]


@pytest.mark.asyncio
async def test_synthetic_l3_cannot_override_l1_block() -> None:
    """Without a real OpenRouter client, the override path is disabled.

    A synthetic L3 is just a function of L1+L2 - it can never honestly
    audit an L1 veto, so the engine must short-circuit as legacy.
    """
    engine = DecisionEngine(
        level1=_BlockedStubLevel1(hard=False),  # type: ignore[arg-type]
        level2=_StubLevel2(  # type: ignore[arg-type]
            score=0.8, direction=1, bias="bullish", strength=0.9
        ),
        level3=None,  # <-- synthetic / no real client
        weights={"level1": 0.25, "level2": 0.35, "level3": 0.40},
        allow_l3_to_override_l1=True,
        l3_override_min_conviction=0.55,
    )
    decision = await engine.decide({"symbols": ["BTC-PERP"], "symbol": "BTC-PERP"})
    assert decision.short_circuited is True
    assert decision.l1_overridden_by_l3 is False
    meta = decision.l1_override_meta
    assert meta is not None
    assert meta["status"] == "declined"
    assert "synthetic" in meta["decline_reason"].lower()


@pytest.mark.asyncio
async def test_stacked_soft_blocks_apply_intensity_haircut_on_override() -> None:
    """3 stacked soft blocks + L3 override at intensity=0.9 ->
    engine clamps calibrated intensity to 0.45 (cap = 0.5).

    Defensive risk-mgmt: three independent technical vetoes firing at
    once is qualitatively riskier than one. L3's directional read is
    respected, but the engine sizes down.
    """
    payload = {
        "conviction": 0.80,
        "direction": "long",
        "regime": "risk_on",
        "recommended_intensity": 0.9,
        "rationale": (
            "Market Context:\nBTC-PERP at $62k.\n\n"
            "Key Signals Analysis:\n- All three blocks marginal.\n\n"
            "Contradictions & Risks:\nrsi_overbought, atr_too_high, "
            "trend_mixed all soft and marginal vs decisive on-chain.\n\n"
            "My Independent View:\nI am overriding all three soft "
            "blocks because the on-chain mix is decisive.\n\n"
            "Final Recommendation:\nOpen LONG; intensity will be "
            "stacked-veto-clamped by the engine."
        ),
        "key_factors": [
            "3-block stacked override",
            "decisive on-chain bullish",
            "all 3 L1 blocks marginal",
        ],
    }
    level3 = Level3(client=_mock_openrouter_client(payload))
    engine = DecisionEngine(
        level1=_BlockedStubLevel1(  # type: ignore[arg-type]
            hard=False,
            extra_soft_codes=["atr_too_high", "trend_mixed"],
        ),
        level2=_StubLevel2(  # type: ignore[arg-type]
            score=0.75, direction=1, bias="bullish", strength=0.85
        ),
        level3=level3,
        weights={"level1": 0.25, "level2": 0.35, "level3": 0.40},
        allow_l3_to_override_l1=True,
        l3_override_min_conviction=0.55,
    )
    decision = await engine.decide({"symbols": ["BTC-PERP"], "symbol": "BTC-PERP"})

    assert decision.l1_overridden_by_l3 is True
    assert decision.directive.action == "risk_on"
    # Conviction is L3's full read (not clamped).
    assert decision.final_score == pytest.approx(0.80)
    # But intensity is clamped: 0.9 * 0.5 (stacked-veto cap @ 3+
    # blocks) = 0.45.
    meta = decision.l1_override_meta
    assert meta is not None
    assert meta["status"] == "executed"
    assert meta["n_soft_blocks"] == 3
    assert meta["stacked_veto_cap"] == pytest.approx(0.5)
    assert meta["raw_intensity"] == pytest.approx(0.9)
    assert meta["calibrated_intensity"] == pytest.approx(0.45)
    assert decision.directive.intensity == pytest.approx(0.45)


@pytest.mark.asyncio
async def test_is_hard_flag_read_from_reason_not_legacy_lookup() -> None:
    """A reason carrying `is_hard=True` with a non-legacy code must
    be treated as a hard block by the engine.

    Validates that the source-of-truth is the reason itself, not the
    engine's defensive legacy lookup table.
    """

    class _CustomHardBlockL1:
        async def score(self, _market: dict[str, Any]) -> LevelScore:
            return LevelScore(
                level=1,
                score=0.0,
                rationale="L1 BLOCKED: custom_hard_rule",
                raw={
                    "l1": {
                        "passes": False,
                        "score": 0.0,
                        "rationale": "L1 BLOCKED: custom_hard_rule",
                        "reasons": [
                            {
                                "code": "custom_hard_rule",  # not in legacy set
                                "severity": "block",
                                "message": "custom hard rule fired",
                                "symbol": "BTC-PERP",
                                "timeframe": None,
                                "metadata": {},
                                "is_hard": True,  # source declares HARD
                            }
                        ],
                        "per_symbol": {},
                    }
                },
                direction_sign=0,
            )

    payload = {
        "conviction": 0.95,
        "direction": "long",
        "regime": "risk_on",
        "recommended_intensity": 0.9,
        "rationale": "Trying to override.",
        "key_factors": ["a", "b"],
    }
    level3 = Level3(client=_mock_openrouter_client(payload))
    engine = DecisionEngine(
        level1=_CustomHardBlockL1(),  # type: ignore[arg-type]
        level2=_StubLevel2(  # type: ignore[arg-type]
            score=0.8, direction=1, bias="bullish", strength=0.8
        ),
        level3=level3,
        weights={"level1": 0.25, "level2": 0.35, "level3": 0.40},
        allow_l3_to_override_l1=True,
        l3_override_min_conviction=0.55,
    )
    decision = await engine.decide({"symbols": ["BTC-PERP"], "symbol": "BTC-PERP"})
    assert decision.short_circuited is True
    assert decision.l1_overridden_by_l3 is False
    meta = decision.l1_override_meta
    assert meta is not None
    assert meta["status"] == "hard_block_uphold"
    assert meta["hard_block_codes"] == ["custom_hard_rule"]


@pytest.mark.asyncio
async def test_l3_invoked_on_soft_block_even_when_override_disabled() -> None:
    """Even with override disabled, L3 must still be called on an L1
    block (so its rationale is available to the operator).

    The engine then short-circuits, but ``l1_override_meta`` records
    the decline reason so the CLI panel can explain why.
    """
    payload = {
        "conviction": 0.85,
        "direction": "long",
        "regime": "risk_on",
        "recommended_intensity": 0.6,
        "rationale": "Override would be appropriate.",
        "key_factors": ["override case", "L2 strongly bullish"],
    }
    level3 = Level3(client=_mock_openrouter_client(payload))
    engine = DecisionEngine(
        level1=_BlockedStubLevel1(hard=False),  # type: ignore[arg-type]
        level2=_StubLevel2(  # type: ignore[arg-type]
            score=0.8, direction=1, bias="bullish", strength=0.8
        ),
        level3=level3,
        weights={"level1": 0.25, "level2": 0.35, "level3": 0.40},
        allow_l3_to_override_l1=False,  # <-- override DISABLED
    )
    decision = await engine.decide({"symbols": ["BTC-PERP"], "symbol": "BTC-PERP"})

    # L3 was still consulted (full briefing always reaches the arbiter).
    level3.client.generate_json.assert_awaited_once()  # type: ignore[attr-defined]
    # But the engine still short-circuits because override is disabled.
    assert decision.short_circuited is True
    assert decision.l1_overridden_by_l3 is False
    meta = decision.l1_override_meta
    assert meta is not None
    assert meta["status"] == "declined"
    assert "disabled" in meta["decline_reason"]


# ---------------------------------------------------------------------------
# Day 6+ — aggression calibration & HOLD-rescue
# ---------------------------------------------------------------------------


def _make_response(
    *,
    conviction: float,
    regime: str,
    direction: str,
    intensity: float,
    rationale: str = "Market Context:\n- ok\n\nFinal Recommendation: OPEN",
) -> ArbiterResponse:
    """Helper: build a valid ArbiterResponse with the given verdict."""
    return ArbiterResponse(
        conviction=conviction,
        direction=direction,
        regime=regime,
        recommended_intensity=intensity,
        rationale=rationale,
        key_factors=["test"],
    )


def test_calibrate_balanced_is_a_noop() -> None:
    """`balanced` mode (the default) MUST NOT mutate the verdict."""
    arbiter = Level3Arbiter(
        client=None,
        config=Level3Config(aggression="balanced"),
    )
    raw = _make_response(
        conviction=0.6, regime="risk_on", direction="long", intensity=0.5
    )
    calibrated, meta = arbiter._calibrate(raw, _briefing())
    assert calibrated.conviction == pytest.approx(0.6)
    assert calibrated.recommended_intensity == pytest.approx(0.5)
    assert calibrated.direction == "long"
    assert calibrated.regime == "risk_on"
    assert meta["aggression"] == "balanced"
    assert meta["hold_rescue_fired"] is False
    # Multipliers are 1.0 for balanced.
    assert meta["multipliers"] == {"conviction": 1.0, "intensity": 1.0}


def test_calibrate_conservative_dampens_conviction_and_intensity() -> None:
    """`conservative` mode applies x0.90 to both conviction & intensity."""
    arbiter = Level3Arbiter(
        client=None,
        config=Level3Config(aggression="conservative"),
    )
    raw = _make_response(
        conviction=0.80, regime="risk_on", direction="long", intensity=0.60
    )
    calibrated, meta = arbiter._calibrate(raw, _briefing())
    assert calibrated.conviction == pytest.approx(0.72)
    assert calibrated.recommended_intensity == pytest.approx(0.54)
    # Raw values stay reported in the audit trail.
    assert meta["raw_conviction"] == pytest.approx(0.80)
    assert meta["raw_intensity"] == pytest.approx(0.60)


def test_calibrate_aggressive_boosts_and_clamps_to_one() -> None:
    """`aggressive` boosts conviction (x1.10) and intensity (x1.15)."""
    arbiter = Level3Arbiter(
        client=None,
        config=Level3Config(aggression="aggressive"),
    )
    raw = _make_response(
        conviction=0.60, regime="risk_on", direction="long", intensity=0.50
    )
    calibrated, meta = arbiter._calibrate(raw, _briefing())
    assert calibrated.conviction == pytest.approx(0.66)
    assert calibrated.recommended_intensity == pytest.approx(0.575)
    # Clamp safety check - 0.95 x 1.10 = 1.045 must clamp to 1.0.
    raw_high = _make_response(
        conviction=0.95, regime="risk_on", direction="long", intensity=0.95
    )
    calibrated_high, _ = arbiter._calibrate(raw_high, _briefing())
    assert calibrated_high.conviction == pytest.approx(1.0)
    assert calibrated_high.recommended_intensity == pytest.approx(1.0)
    assert meta["hold_rescue_fired"] is False  # not a HOLD verdict


def test_hold_rescue_fires_on_aggressive_with_strong_l2() -> None:
    """The pathological case: Claude HELD but L1+L2 converged strongly."""
    arbiter = Level3Arbiter(
        client=None,
        config=Level3Config(
            aggression="aggressive",
            hold_rescue_l2_min=0.65,
            hold_rescue_intensity=0.30,
        ),
    )
    raw = _make_response(
        conviction=0.30, regime="hold", direction="neutral", intensity=0.0
    )
    briefing = _briefing(l2_conv=0.75, l2_dir=1, bias="bullish")
    calibrated, meta = arbiter._calibrate(raw, briefing)
    # The rescue flipped the verdict.
    assert calibrated.regime == "risk_on"
    assert calibrated.direction == "long"
    assert calibrated.recommended_intensity >= 0.30
    assert meta["hold_rescue_fired"] is True
    assert "L1 passes" in meta["hold_rescue_reason"]
    assert "0.75" in meta["hold_rescue_reason"]


def test_hold_rescue_uses_l2_direction_for_short() -> None:
    """If L2's bias is short, the rescue opens a short, not a long."""
    arbiter = Level3Arbiter(
        client=None,
        config=Level3Config(aggression="aggressive"),
    )
    raw = _make_response(
        conviction=0.30, regime="hold", direction="neutral", intensity=0.0
    )
    briefing = _briefing(l2_conv=0.80, l2_dir=-1, bias="bearish")
    calibrated, _ = arbiter._calibrate(raw, briefing)
    assert calibrated.direction == "short"
    assert calibrated.regime == "risk_on"


def test_hold_rescue_does_NOT_fire_under_balanced() -> None:
    """`balanced` mode must NEVER fire the HOLD-rescue rule."""
    arbiter = Level3Arbiter(
        client=None,
        config=Level3Config(aggression="balanced"),
    )
    raw = _make_response(
        conviction=0.30, regime="hold", direction="neutral", intensity=0.0
    )
    briefing = _briefing(l2_conv=0.85, l2_dir=1, bias="bullish")
    calibrated, meta = arbiter._calibrate(raw, briefing)
    assert calibrated.regime == "hold"
    assert meta["hold_rescue_fired"] is False


def test_hold_rescue_does_NOT_fire_when_l2_conviction_below_threshold() -> None:
    """L2 conviction must clear `hold_rescue_l2_min` for rescue to fire."""
    arbiter = Level3Arbiter(
        client=None,
        config=Level3Config(
            aggression="aggressive", hold_rescue_l2_min=0.70
        ),
    )
    raw = _make_response(
        conviction=0.30, regime="hold", direction="neutral", intensity=0.0
    )
    briefing = _briefing(l2_conv=0.50, l2_dir=1)  # below threshold
    calibrated, meta = arbiter._calibrate(raw, briefing)
    assert calibrated.regime == "hold"
    assert meta["hold_rescue_fired"] is False


def test_hold_rescue_does_NOT_fire_when_l1_blocked() -> None:
    """The HOLD-rescue rule requires L1.passes to be True."""
    arbiter = Level3Arbiter(
        client=None,
        config=Level3Config(aggression="aggressive"),
    )
    raw = _make_response(
        conviction=0.30, regime="hold", direction="neutral", intensity=0.0
    )
    briefing = _briefing(l2_conv=0.85, l2_dir=1)
    briefing.level1_passes = False  # L1 vetoed
    calibrated, meta = arbiter._calibrate(raw, briefing)
    assert calibrated.regime == "hold"
    assert meta["hold_rescue_fired"] is False


def test_telemetry_counts_holds_and_opens_correctly() -> None:
    """The telemetry counter must distinguish raw vs calibrated holds."""
    arbiter = Level3Arbiter(
        client=None,
        config=Level3Config(aggression="aggressive"),
    )
    # Cycle 1: Claude returned a clean OPEN.
    raw1 = _make_response(
        conviction=0.7, regime="risk_on", direction="long", intensity=0.6
    )
    cal1, meta1 = arbiter._calibrate(raw1, _briefing(l2_conv=0.6))
    arbiter._telemetry.record(raw1, cal1, meta1)
    # Cycle 2: Claude held, rescue fired (aggressive + strong L2).
    raw2 = _make_response(
        conviction=0.3, regime="hold", direction="neutral", intensity=0.0
    )
    cal2, meta2 = arbiter._calibrate(raw2, _briefing(l2_conv=0.75, l2_dir=1))
    arbiter._telemetry.record(raw2, cal2, meta2)
    # Cycle 3: Claude held, rescue did NOT fire (L2 too weak).
    raw3 = _make_response(
        conviction=0.3, regime="hold", direction="neutral", intensity=0.0
    )
    cal3, meta3 = arbiter._calibrate(raw3, _briefing(l2_conv=0.4, l2_dir=1))
    arbiter._telemetry.record(raw3, cal3, meta3)

    t = arbiter._telemetry
    assert t.total == 3
    assert t.raw_holds == 2          # cycles 2 and 3 held pre-calibration
    assert t.held == 1               # only cycle 3 held post-calibration
    assert t.opened == 2             # cycles 1 (real) and 2 (rescued)
    assert t.rescued_holds == 1      # cycle 2


def test_telemetry_log_summary_is_safe_with_zero_cycles(caplog) -> None:
    """Zero-cycle summary must not crash or log a divide-by-zero line."""
    arbiter = Level3Arbiter(
        client=None,
        config=Level3Config(aggression="balanced"),
    )
    arbiter._telemetry.log_summary()  # must not raise
    # Nothing logged because there's nothing to summarise.
    assert arbiter._telemetry.total == 0
