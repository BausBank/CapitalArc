"""Execution layer for CapitalArc.

Modules
-------
- `circle_wallet`     - Circle Developer-Controlled Wallets + Paymaster client.
- `arc_perp_executor` - High-level perp open/close/read against Arc Perp DEX.
"""

from src.execution.arc_perp_executor import (
    AccountInfo,
    ArcPerpConfig,
    ArcPerpExecutor,
    Position,
)
from src.execution.circle_wallet import (
    CircleWallet,
    CircleWalletConfig,
    TxRequest,
    TxResult,
)

__all__ = [
    "CircleWallet",
    "CircleWalletConfig",
    "TxRequest",
    "TxResult",
    "ArcPerpExecutor",
    "ArcPerpConfig",
    "Position",
    "AccountInfo",
]
