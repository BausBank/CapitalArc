"""Level 3 - Gemini 2.5 Flash as the final arbiter.

Level 3 receives the structured outputs of Level 1 and Level 2 plus a
compact market briefing and asks Gemini 2.5 Flash to classify the
current regime and emit a final risk score in `[0.0, 1.0]`.

Day 2 keeps the arbiter as a deterministic neutral placeholder so the
pipeline runs end-to-end without burning Gemini calls. Day 3 wires the
real Gemini round-trip.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from src.llm.gemini_client import GeminiClient

if TYPE_CHECKING:
    from src.core.decision_engine import LevelScore


@dataclass
class Level3Config:
    """Configuration for Level 3 (Gemini final arbiter)."""

    model: str = "gemini-2.5-flash"
    temperature: float = 0.2
    max_output_tokens: int = 1024
    timeout_seconds: int = 30


@dataclass
class ArbiterBriefing:
    """Structured payload sent to Gemini as the final-arbiter prompt."""

    level1_score: float
    level1_rationale: str
    level2_score: float
    level2_rationale: str
    market_snapshot: dict[str, Any]


class Level3:
    """Final-arbiter level of the decision engine.

    Wraps `GeminiFinalArbiter` so the engine can treat all three levels
    uniformly behind a `score(...)` interface.
    """

    LEVEL = 3

    def __init__(
        self,
        client: GeminiClient | None,
        config: Level3Config | None = None,
    ) -> None:
        self.config = config or Level3Config()
        self.client = client
        self.arbiter = GeminiFinalArbiter(client=client, config=self.config)

    async def score(self, briefing: ArbiterBriefing) -> "LevelScore":
        from src.core.decision_engine import LevelScore

        score_value, rationale = await self.arbiter.arbitrate(briefing)
        return LevelScore(
            level=self.LEVEL,
            score=score_value,
            rationale=rationale,
        )


class GeminiFinalArbiter:
    """Gemini-backed final arbiter.

    Day 2 stub: produces a neutral 0.5 score with a clear placeholder
    rationale. Day 3 will:
        1. Render `ArbiterBriefing` into the system+user prompt
           templates living in `prompts/`.
        2. Call Gemini 2.5 Flash with low temperature.
        3. Parse a strict JSON response shaped as
           `{"score": float, "regime": str, "rationale": str}`.
    """

    def __init__(
        self,
        client: GeminiClient | None,
        config: Level3Config,
    ) -> None:
        self.client = client
        self.config = config
        self._last_rationale: str = ""

    async def arbitrate(self, briefing: ArbiterBriefing) -> tuple[float, str]:
        """Return `(score, rationale)` for the given briefing."""
        rationale = (
            "L3 placeholder (Gemini 2.5 Flash not wired yet); "
            f"echoed avg of L1({briefing.level1_score:.2f}) "
            f"and L2({briefing.level2_score:.2f})"
        )
        score = 0.5 * briefing.level1_score + 0.5 * briefing.level2_score
        self._last_rationale = rationale
        return score, rationale

    async def last_rationale(self) -> str:
        return self._last_rationale
