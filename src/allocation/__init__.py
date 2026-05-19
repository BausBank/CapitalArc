"""Allocation router for CapitalArc.

Turns the `DecisionResult` produced by the core engine into capital moves:

    final_score >= RISK_ON_THRESHOLD  -> open / hold leveraged perp positions
    final_score <= RISK_OFF_THRESHOLD -> rotate USDC into USYC for yield
    otherwise                         -> hold current allocation

The router is the only component allowed to instruct the execution
layer to move funds. Hard overrides (drawdown, stale data) live here.
"""
