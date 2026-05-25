"""Allocation router for CapitalArc.

Takes the `DecisionResult` produced by `src.core.DecisionEngine` and
translates it into concrete capital moves on Arc:

    risk_on  + side != neutral  -> open / scale perp position;
                                   redeem USYC to fund margin if short.
    risk_off                    -> close every perp position, withdraw
                                   margin back to the wallet, mint USYC
                                   with the freed USDC (minus reserve).
    hold                        -> no on-chain change.

The router is the only component allowed to instruct the execution layer
to move funds; hard overrides (drawdown breach, stale data, leverage cap,
max-position cap) all live here. The USYC leg is optional - when
``USYC_*`` env vars aren't wired the router skips rotation gracefully
and the demo keeps running.
"""

from src.allocation.allocation_router import (
    AllocationConfig,
    AllocationRouter,
    ExecutionPlan,
)

__all__ = ["AllocationRouter", "AllocationConfig", "ExecutionPlan"]
