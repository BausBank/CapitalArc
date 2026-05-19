"""Core decision engine for CapitalArc.

This package contains the three-level decision engine that produces a
single risk score in `[0.0, 1.0]` and the orchestration logic that turns
that score into an allocation action.

Levels
------
- `Level1` - fast deterministic technical rules on OHLCV / funding data.
- `Level2` - on-chain intelligence sourced via the Dune MCP server.
- `Level3` - Gemini 2.5 Flash acting as the final narrative arbiter.

The `DecisionEngine` aggregates the three level scores using configurable
weights and exposes the final action to the allocation router.
"""

from src.core.decision_engine import DecisionEngine, DecisionResult
from src.core.level1 import Level1
from src.core.level2 import Level2
from src.core.level3 import Level3, GeminiFinalArbiter

__all__ = [
    "DecisionEngine",
    "DecisionResult",
    "Level1",
    "Level2",
    "Level3",
    "GeminiFinalArbiter",
]
