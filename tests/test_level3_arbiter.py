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
        rationale="L2 явный медвежий bias.",
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
        "rationale": "L2 однозначно медвежий, funding отрицательный.",
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
    assert "CapitalArc" in prompt or "арбитр" in prompt.lower()


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
        "rationale": "Сильный bullish сетап.",
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
