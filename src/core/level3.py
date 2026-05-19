"""Level 3 - Gemini 2.5 Flash as the final arbiter.

Level 3 receives the structured outputs of Level 1 and Level 2 plus a
compact market briefing and asks Gemini 2.5 Flash to classify the
current regime and emit a final risk score in `[0.0, 1.0]`.

Gemini is the *arbiter*, not just another signal: it can overrule the
naive blend by adjusting its score based on macro context, news, or
inter-level inconsistencies it spots in the briefing.

The Day 1 stub only fixes the public surface and the prompt contract.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from src.llm.gemini_client import GeminiClient


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
        client: GeminiClient,
        config: Level3Config | None = None,
    ) -> None:
        self.config = config or Level3Config()
        self.arbiter = GeminiFinalArbiter(client=client, config=self.config)

    async def score(self, briefing: ArbiterBriefing) -> float:
        return await self.arbiter.arbitrate(briefing)

    async def explain(self, briefing: ArbiterBriefing) -> str:
        return await self.arbiter.last_rationale()


class GeminiFinalArbiter:
    """The actual Gemini-backed arbiter.

    Day 1 stub only - on Day 2 this will:
        1. Render `ArbiterBriefing` into the system+user prompt
           templates living in `prompts/`.
        2. Call Gemini 2.5 Flash with low temperature.
        3. Parse a strict JSON response shaped as
           `{"score": float, "regime": str, "rationale": str}`.
        4. Cache the rationale for `explain()`.
    """

    def __init__(
        self,
        client: GeminiClient,
        config: Level3Config,
    ) -> None:
        self.client = client
        self.config = config
        self._last_rationale: str = ""

    async def arbitrate(self, briefing: ArbiterBriefing) -> float:
        """Return a final risk-on score in `[0.0, 1.0]`."""
        raise NotImplementedError(
            "GeminiFinalArbiter.arbitrate will be implemented on Day 2"
        )

    async def last_rationale(self) -> str:
        return self._last_rationale
