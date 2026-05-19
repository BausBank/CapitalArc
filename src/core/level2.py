"""Level 2 - on-chain intelligence via Dune MCP.

Level 2 queries Dune Analytics through the Model Context Protocol (MCP)
to read stablecoin flows, DEX volumes, funding rates across venues,
whale positioning and bridge activity. These signals describe how
*capital* is moving, regardless of price action.

The Day 1 stub only fixes the interface. The actual MCP client wiring
and the SQL/Dune-query catalog land on Day 2.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Level2Config:
    """Configuration for Level 2 on-chain analysis."""

    dune_mcp_url: str = "https://mcp.dune.com/sse"
    dune_api_key: str = ""
    cache_ttl_seconds: int = 300
    # Named Dune queries that the agent depends on.
    queries: dict[str, int] = field(default_factory=dict)


class Level2:
    """On-chain intelligence level of the decision engine.

    Uses Dune MCP to fetch a fixed set of named queries and turns the
    resulting time series into a single risk-on score.
    """

    LEVEL = 2

    def __init__(self, config: Level2Config) -> None:
        self.config = config
        self._client: Any | None = None  # MCP client, lazily initialised

    async def connect(self) -> None:
        """Initialise the Dune MCP client. Implemented on Day 2."""
        raise NotImplementedError("Level2.connect will be implemented on Day 2")

    async def score(self, market: dict[str, Any]) -> float:
        """Return a risk-on score in `[0.0, 1.0]` based on on-chain flows.

        Day 2 wiring will compute it from:
            - Stablecoin net flows into Arc / major DEXes
            - Perp open interest delta
            - Whale wallet rotations
            - Bridge volume (CCTP, others)
        """
        raise NotImplementedError("Level2.score will be implemented on Day 2")

    async def explain(self, market: dict[str, Any]) -> str:
        """Return a short rationale for the last on-chain score."""
        return "Level 2 rationale not implemented yet"
