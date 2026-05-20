"""Core decision engine for CapitalArc.

This package contains the three-level decision engine that produces a
single risk score in `[0.0, 1.0]` plus a concrete `ExecutionDirective`,
and the orchestration logic that turns that score into an allocation
action via `src.allocation.AllocationRouter`.

Levels
------
- `Level1` - fast deterministic technical rules on OHLCV / funding data.
- `Level2` - on-chain intelligence sourced via the Dune MCP server.
- `Level3` - Gemini 2.5 Flash acting as the final narrative arbiter.
"""

from src.core.decision_engine import (
    DecisionEngine,
    DecisionResult,
    ExecutionDirective,
    LevelScore,
)
from src.core.level1 import (
    IndicatorRow,
    Level1,
    Level1Config,
    Level1Decision,
    Level1Reason,
    SymbolReadout,
)
from src.core.level2 import (
    CumulativeFundingSnapshot,
    FundingSnapshot,
    Level2,
    Level2Config,
    Level2Intelligence,
    LongShortSnapshot,
    OnchainSummary,
    OpenInterestSnapshot,
    SymbolIntel,
    VolumeSnapshot,
    WhaleSnapshot,
)
from src.core.level3 import (
    ArbiterBriefing,
    GeminiFinalArbiter,
    Level3,
    Level3Config,
)

__all__ = [
    "DecisionEngine",
    "DecisionResult",
    "ExecutionDirective",
    "LevelScore",
    "Level1",
    "Level1Config",
    "Level1Decision",
    "Level1Reason",
    "SymbolReadout",
    "IndicatorRow",
    "Level2",
    "Level2Config",
    "Level2Intelligence",
    "SymbolIntel",
    "FundingSnapshot",
    "OpenInterestSnapshot",
    "VolumeSnapshot",
    "LongShortSnapshot",
    "WhaleSnapshot",
    "CumulativeFundingSnapshot",
    "OnchainSummary",
    "Level3",
    "Level3Config",
    "ArbiterBriefing",
    "GeminiFinalArbiter",
]
