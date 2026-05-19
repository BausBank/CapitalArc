"""Level 2 - on-chain intelligence via Dune MCP.

Level 2 queries Dune Analytics through the Model Context Protocol (MCP)
to read stablecoin flows, DEX volumes, perp open interest, whale wallet
rotations and bridge activity, then turns those signals into a single
risk-on score.

Day 2 keeps Level 2 as a neutral placeholder so the end-to-end pipeline
can run in dry-run mode. Day 3 wires the actual MCP client + Dune queries.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from src.core.decision_engine import LevelScore


@dataclass
class Level2Config:
    """Configuration for Level 2 on-chain analysis."""

    dune_mcp_url: str = "https://mcp.dune.com/sse"
    dune_api_key: str = ""
    cache_ttl_seconds: int = 300
    queries: dict[str, int] = field(default_factory=dict)


class Level2:
    """On-chain intelligence level of the decision engine."""

    LEVEL = 2

    def __init__(self, config: Level2Config) -> None:
        self.config = config
        self._client: Any | None = None

    async def connect(self) -> None:
        """Initialise the Dune MCP client. Implemented on Day 3."""
        return None

    async def score(self, market: dict[str, Any]) -> "LevelScore":
        """Return a `LevelScore` for Level 2.

        Day 2 stub: returns a neutral 0.5 score with a rationale that
        clearly states Dune MCP is not wired yet.
        """
        from src.core.decision_engine import LevelScore

        return LevelScore(
            level=self.LEVEL,
            score=0.5,
            rationale="L2 placeholder (Dune MCP not wired yet)",
            raw={"dune_mcp_url": self.config.dune_mcp_url},
        )
