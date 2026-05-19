"""Execution layer for CapitalArc.

This package wraps the Arc Perp DEX integration:
    - Order placement, modification and cancellation
    - Position and margin queries
    - Pre-trade risk checks (leverage cap, liquidation buffer)
    - Idempotent submission keyed by `decision_id`

All on-chain calls go through a Circle Developer-Controlled Wallet and
use the Circle Paymaster for gasless UX where supported.
"""
