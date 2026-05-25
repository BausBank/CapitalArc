"""Execution layer for CapitalArc.

Modules
-------
- `circle_wallet`        - Circle Developer-Controlled Wallets + Paymaster client
                           (used for USYC mint/redeem, CCTP and Arc treasury).
- `arc_perp_executor`    - Legacy on-Arc perp executor. Retained only for
                           dry-run telemetry and Arc treasury moves; live
                           trading has migrated to Hyperliquid Testnet.
- `usyc_executor`        - USYC mint/redeem (yield-bearing risk-off leg).
- `hyperliquid_executor` - **Primary trading venue.** EIP-712-signed perp
                           orders submitted directly to Hyperliquid Testnet's
                           `/exchange` endpoint via the official Python SDK.
- `position_manager`     - Per-cycle stewardship of open positions
                           (TP / SL / trailing / re-evaluation / side-flip).
                           Consumed by :class:`AllocationRouter` before
                           the regime dispatch on every cycle.
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
from src.execution.hyperliquid_executor import (
    HyperliquidConfig,
    HyperliquidExecutor,
)
from src.execution.position_manager import (
    PositionAction,
    PositionManager,
    PositionManagerConfig,
    PositionReview,
    PositionSnapshot,
    PositionTrigger,
)
from src.execution.usyc_executor import (
    USYCExecutor,
    USYCExecutorConfig,
    USYCSnapshot,
)

__all__ = [
    # Circle DCW + Paymaster
    "CircleWallet",
    "CircleWalletConfig",
    "TxRequest",
    "TxResult",
    # Arc Perp DEX (legacy; treasury / dry-run only)
    "ArcPerpExecutor",
    "ArcPerpConfig",
    "Position",
    "AccountInfo",
    # USYC yield leg
    "USYCExecutor",
    "USYCExecutorConfig",
    "USYCSnapshot",
    # Hyperliquid Testnet (primary trading venue)
    "HyperliquidExecutor",
    "HyperliquidConfig",
    # Per-position stewardship (TP / SL / trailing / flip / re-eval)
    "PositionManager",
    "PositionManagerConfig",
    "PositionAction",
    "PositionReview",
    "PositionSnapshot",
    "PositionTrigger",
]
