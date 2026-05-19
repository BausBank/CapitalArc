"""Arc Perp DEX executor.

Implements the high-level on-chain actions that the allocation router
needs from the perp venue:

    - open_position(symbol, side, size_usd, leverage, slippage_bps)
    - close_position(symbol)
    - get_position(symbol)
    - get_pnl(symbol=None)
    - get_margin()
    - get_account_info()

All state-changing calls are routed through `CircleWallet.send_contract_execution`,
so gas is sponsored by Circle Paymaster when configured. Read-only calls
go directly through `web3.py` against the Arc RPC.

ABI placeholders
----------------
The exact `abi_function_signature` strings are placeholders that match the
typical perp-DEX router surface. As soon as the hackathon publishes the
real Arc Perp ABI, only the `_FN_*` constants below need to be updated;
the rest of the executor stays the same.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from src.execution.circle_wallet import CircleWallet, TxRequest, TxResult
from src.utils.logging import logger


# --------------------------------------------------------------------------
# Placeholder ABI signatures - replace with real Arc Perp DEX ABI on Day 3
# --------------------------------------------------------------------------
_FN_OPEN_POSITION = (
    "openPosition(string,uint8,uint256,uint256,uint256)"
)
_FN_CLOSE_POSITION = "closePosition(string,uint256)"
_FN_GET_POSITION = "getPosition(address,string)"
_FN_GET_ACCOUNT = "getAccountInfo(address)"

SIDE_LONG = 0
SIDE_SHORT = 1


@dataclass
class ArcPerpConfig:
    """Configuration for the Arc Perp executor."""

    router_address: str | None
    max_leverage: int = 3
    default_slippage_bps: int = 50
    usd_decimals: int = 6  # USDC margin decimals on Arc


@dataclass
class Position:
    """Snapshot of an open perp position."""

    symbol: str
    side: str  # "long" | "short" | "flat"
    size_usd: Decimal
    entry_price: Decimal
    mark_price: Decimal
    leverage: Decimal
    unrealized_pnl_usd: Decimal
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class AccountInfo:
    """High-level summary of the agent's perp account."""

    equity_usd: Decimal
    free_margin_usd: Decimal
    used_margin_usd: Decimal
    total_unrealized_pnl_usd: Decimal
    positions: list[Position]


class ArcPerpExecutor:
    """High-level perp executor backed by Circle DCW.

    Parameters
    ----------
    wallet :
        Circle DCW wallet used to sign and submit on-chain transactions.
    config :
        `ArcPerpConfig` with router address and risk caps.
    dry_run :
        Inherited semantics from `CircleWallet.dry_run`. When True the
        executor builds the exact contract call it would have made and
        logs it instead of broadcasting.
    """

    def __init__(
        self,
        wallet: CircleWallet,
        config: ArcPerpConfig,
        dry_run: bool = True,
    ) -> None:
        self.wallet = wallet
        self.config = config
        self.dry_run = dry_run

    # ------------------------------------------------------------------
    # State-changing methods
    # ------------------------------------------------------------------

    async def open_position(
        self,
        symbol: str,
        side: str,
        size_usd: Decimal,
        leverage: Decimal | None = None,
        slippage_bps: int | None = None,
        decision_id: str | None = None,
    ) -> TxResult:
        """Open a perp position.

        Parameters
        ----------
        symbol :
            E.g. ``"BTC-PERP"``.
        side :
            ``"long"`` or ``"short"``.
        size_usd :
            Notional position size in USD.
        leverage :
            Target leverage. Capped at `config.max_leverage`.
        slippage_bps :
            Max allowed slippage in basis points.
        decision_id :
            Idempotency key from the DecisionEngine. Lets retries be safe.
        """
        self._require_router()
        lev = self._cap_leverage(leverage)
        slip = slippage_bps if slippage_bps is not None else self.config.default_slippage_bps
        side_code = self._side_code(side)

        size_units = self._to_usd_units(size_usd)
        lev_bps = int(lev * Decimal(10000))

        req = TxRequest(
            contract_address=self.config.router_address or "",
            abi_function_signature=_FN_OPEN_POSITION,
            abi_parameters=[symbol, side_code, str(size_units), str(lev_bps), str(slip)],
            decision_id=decision_id,
            metadata={
                "action": "open_position",
                "symbol": symbol,
                "side": side,
                "size_usd": str(size_usd),
                "leverage": str(lev),
            },
        )
        logger.info(
            "ArcPerp.open_position | {} {} size={} USD lev={}x slip={}bps decision={}",
            side.upper(), symbol, size_usd, lev, slip, decision_id,
        )
        return await self.wallet.send_contract_execution(req)

    async def close_position(
        self,
        symbol: str,
        slippage_bps: int | None = None,
        decision_id: str | None = None,
    ) -> TxResult:
        """Close an open perp position by symbol."""
        self._require_router()
        slip = slippage_bps if slippage_bps is not None else self.config.default_slippage_bps

        req = TxRequest(
            contract_address=self.config.router_address or "",
            abi_function_signature=_FN_CLOSE_POSITION,
            abi_parameters=[symbol, str(slip)],
            decision_id=decision_id,
            metadata={"action": "close_position", "symbol": symbol},
        )
        logger.info(
            "ArcPerp.close_position | {} slip={}bps decision={}",
            symbol, slip, decision_id,
        )
        return await self.wallet.send_contract_execution(req)

    # ------------------------------------------------------------------
    # Read-only methods
    # ------------------------------------------------------------------

    async def get_position(self, symbol: str) -> Position:
        """Return current position for `symbol`.

        Day 2 stub: returns a flat position. Day 3 wires this to a real
        eth_call against the Arc Perp router.
        """
        logger.debug("ArcPerp.get_position({}) - stub", symbol)
        return Position(
            symbol=symbol,
            side="flat",
            size_usd=Decimal("0"),
            entry_price=Decimal("0"),
            mark_price=Decimal("0"),
            leverage=Decimal("0"),
            unrealized_pnl_usd=Decimal("0"),
        )

    async def get_pnl(self, symbol: str | None = None) -> Decimal:
        """Return unrealized PnL for `symbol`, or total if `symbol is None`."""
        if symbol is not None:
            pos = await self.get_position(symbol)
            return pos.unrealized_pnl_usd
        info = await self.get_account_info()
        return info.total_unrealized_pnl_usd

    async def get_margin(self) -> Decimal:
        """Return free margin (USD) available for new positions."""
        info = await self.get_account_info()
        return info.free_margin_usd

    async def get_account_info(self) -> AccountInfo:
        """Return a full snapshot of the perp account.

        Day 2 stub: returns zeros with no open positions. Day 3 wires this
        to the Arc Perp router via `web3.py` `eth_call`.
        """
        logger.debug("ArcPerp.get_account_info - stub")
        return AccountInfo(
            equity_usd=Decimal("0"),
            free_margin_usd=Decimal("0"),
            used_margin_usd=Decimal("0"),
            total_unrealized_pnl_usd=Decimal("0"),
            positions=[],
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _require_router(self) -> None:
        if not self.config.router_address:
            if self.dry_run:
                logger.warning(
                    "ArcPerp router address is not configured; "
                    "dry-run will log intent but produce no usable tx."
                )
            else:
                raise RuntimeError(
                    "ARC_PERP_ROUTER_ADDRESS is not set; cannot submit "
                    "perp tx in live mode."
                )

    def _cap_leverage(self, leverage: Decimal | None) -> Decimal:
        lev = leverage or Decimal(self.config.max_leverage)
        return min(lev, Decimal(self.config.max_leverage))

    def _side_code(self, side: str) -> int:
        s = side.lower()
        if s == "long":
            return SIDE_LONG
        if s == "short":
            return SIDE_SHORT
        raise ValueError(f"Unknown perp side: {side!r}")

    def _to_usd_units(self, amount_usd: Decimal) -> int:
        """Convert a USD amount to integer token units (USDC = 6 decimals)."""
        scale = Decimal(10) ** self.config.usd_decimals
        return int((amount_usd * scale).to_integral_value())
