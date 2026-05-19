"""Allocation router for CapitalArc.

Takes the `DecisionResult` produced by `src.core.DecisionEngine` and
translates it into concrete capital moves on Arc:

    final_score >= RISK_ON_THRESHOLD  -> open / scale perp position
    final_score <= RISK_OFF_THRESHOLD -> close perp + rotate into USYC
    otherwise                         -> hold current allocation

The router is the only component allowed to instruct the execution layer
to move funds; hard overrides (drawdown, stale data) also live here.
"""

from src.allocation.allocation_router import (
    AllocationConfig,
    AllocationRouter,
    ExecutionPlan,
)

__all__ = ["AllocationRouter", "AllocationConfig", "ExecutionPlan"]
