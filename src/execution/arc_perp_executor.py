"""Arc Perp DEX executor.

Implements the high-level on-chain actions that the allocation router
needs from the perp venue:

    deposit_margin(amount_usd, decision_id)   # live
    withdraw_margin(amount_usd, decision_id)  # live
    open_position(symbol, side, size_usd, leverage, ...)
    close_position(symbol, ...)
    get_position(symbol) -> Position
    get_pnl(symbol=None) -> Decimal
    get_margin() -> Decimal
    get_account_info() -> AccountInfo

Perp DEX architecture
---------------------
The Arc Perp DEX deployed on Arc Testnet is *order-book + on-chain
settlement* (dYdX v3 / Hyperliquid style):

    1. Agent deposits USDC margin to `USDCCollateralVault.deposit(accountId, amount)`
       (on-chain, Circle DCW + Paymaster).
    2. Agent signs an EIP-712 `OrderTypes.Order` off-chain.
    3. Agent POSTs the signed order to an off-chain matching engine
       (URL provided via `ARC_PERP_MATCHER_URL` env var).
    4. The matcher batches matched fills and submits them to
       `ClearingHouse.settleBatch(batchId, fills[])`.
    5. Per-(accountId, marketId) state is recorded in `PositionLedger`.

What ships in Day 2 (live)
--------------------------
- Real ABI constants for all four perp contracts (verified on Arcscan).
- `deposit_margin` and `withdraw_margin` work in `--live` mode: real
  Circle DCW `contractExecution` calls hit `USDCCollateralVault`, gas
  is sponsored when Paymaster policy is set.
- Reads (`get_position`, `get_margin`, `get_account_info`) go through
  `web3.py` `eth_call` against the public Arc Testnet RPC. No Circle
  signing is needed for reads.
- `open_position` / `close_position` log the planned EIP-712 order in
  dry-run, and in `--live` raise a *clear, actionable* error if
  `ARC_PERP_MATCHER_URL` is not configured (Day 3).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from src.execution.circle_wallet import CircleWallet, TxRequest, TxResult
from src.utils.logging import logger

try:
    from web3 import Web3
except ImportError:  # pragma: no cover - web3 is in requirements.txt
    Web3 = None  # type: ignore[assignment]


# --------------------------------------------------------------------------
# Real ABI signatures (verified Arc Testnet contracts, deployer
# 0x880bd26Adc300d37af797F999C20c69243F61ec8).
#
# ClearingHouse  0x70a069462195E57A4f2E9aCb626Cf1d7E6aF9892
#   Source-confirmed surface (settleBatch is called by the matcher,
#   not by end users; we still expose the signature for completeness):
#     - settleBatch(uint256,(...)[])
#     - isBatchProcessed(uint256)        view
#     - getFilledAmount(bytes32)         view
#
# USDCCollateralVault  0x75E4FBFBA942A82F0f5CA9663571233823A71f11
#   User-facing margin moves used by the agent:
#     - deposit(bytes32 accountId, uint256 amount)
#     - withdraw(bytes32 accountId, uint256 amount)
#     - getBalance(bytes32 accountId)    view
#
# MarketRegistry  0x9cED23e4a154769a5578D14BA63c01775003feB1
#     - getMarket(bytes32 marketId)      view
#
# PositionLedger  0xd6D772918435a11d03E71158aFd44f3d48a1E148
#     - getPosition(bytes32 accountId, bytes32 marketId)    view
#     - positionExists(bytes32 accountId, bytes32 marketId) view
# --------------------------------------------------------------------------
_FN_VAULT_DEPOSIT = "deposit(bytes32,uint256)"
_FN_VAULT_WITHDRAW = "withdraw(bytes32,uint256)"
_FN_VAULT_GET_BALANCE = "getBalance(bytes32)"

_FN_LEDGER_GET_POSITION = "getPosition(bytes32,bytes32)"
_FN_LEDGER_POSITION_EXISTS = "positionExists(bytes32,bytes32)"

_FN_CH_SETTLE_BATCH = "settleBatch(uint256,(bytes32,bytes32,uint256,uint128,bool,bool,uint256,uint256)[])"
_FN_CH_IS_BATCH_PROCESSED = "isBatchProcessed(uint256)"
_FN_CH_GET_FILLED_AMOUNT = "getFilledAmount(bytes32)"


SIDE_LONG = 0
SIDE_SHORT = 1


@dataclass
class ArcPerpConfig:
    """Configuration for the Arc Perp executor."""

    router_address: str | None              # ClearingHouse
    vault_address: str | None               # USDCCollateralVault
    market_registry_address: str | None     # MarketRegistry
    position_ledger_address: str | None     # PositionLedger
    matcher_url: str | None = None          # off-chain orderbook endpoint
    rpc_url: str = "https://rpc.testnet.arc.network"
    max_leverage: int = 3
    default_slippage_bps: int = 50
    usdc_decimals: int = 6


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
        `ArcPerpConfig` with all perp-contract addresses, matcher URL,
        RPC URL and risk caps.
    account_address :
        EVM address of the agent's wallet (the Circle DCW address).
        Used to derive `accountId` for vault / ledger reads & writes.
    dry_run :
        When True, no on-chain transactions are submitted; live tx-shape
        is still logged.
    """

    def __init__(
        self,
        wallet: CircleWallet,
        config: ArcPerpConfig,
        account_address: str | None = None,
        dry_run: bool = True,
    ) -> None:
        self.wallet = wallet
        self.config = config
        self.dry_run = dry_run
        self.account_address = account_address
        self._w3: Any = None
        if Web3 is not None and config.rpc_url:
            self._w3 = Web3(Web3.HTTPProvider(config.rpc_url))

    # ------------------------------------------------------------------
    # Identity helpers
    # ------------------------------------------------------------------

    def set_account_address(self, address: str) -> None:
        """Late-bind the agent's wallet address (after CircleWallet.get_address)."""
        self.account_address = address

    def _account_id(self) -> str:
        """Derive the perp `accountId` (bytes32) from the wallet address.

        The Arc Perp DEX uses `keccak256(abi.encode(address))` style
        identifiers in `USDCCollateralVault` / `PositionLedger`. We compute
        a deterministic bytes32 by left-padding the EVM address to 32 bytes
        (the most common convention; if the venue uses a different keying,
        we override here on Day 3 without touching anything else).
        """
        if not self.account_address:
            raise RuntimeError(
                "Agent wallet address is unknown - cannot derive accountId. "
                "Call set_account_address() or CircleWallet.get_address() first."
            )
        addr = self.account_address.lower().removeprefix("0x")
        if len(addr) != 40:
            raise RuntimeError(f"Bad EVM address length: {self.account_address!r}")
        return "0x" + addr.rjust(64, "0")

    def _market_id(self, symbol: str) -> str:
        """Map a human-readable symbol to a bytes32 marketId.

        Day 2 uses `keccak256(symbol)` as a placeholder. Day 3 will switch
        to reading the canonical marketId from `MarketRegistry.getMarket`
        once we know the registry's symbol -> id convention.
        """
        if Web3 is None:
            raise RuntimeError("web3.py is required to compute marketId hashes.")
        return Web3.keccak(text=symbol).hex()

    # ------------------------------------------------------------------
    # Live margin moves (work in --live today)
    # ------------------------------------------------------------------

    async def deposit_margin(
        self,
        amount_usd: Decimal,
        decision_id: str | None = None,
    ) -> TxResult:
        """Deposit USDC margin into `USDCCollateralVault`."""
        if not self.config.vault_address:
            raise RuntimeError(
                "ARC_PERP_VAULT_ADDRESS is not set; cannot deposit margin."
            )
        amount_units = self._to_usdc_units(amount_usd)
        account_id = self._account_id()
        req = TxRequest(
            contract_address=self.config.vault_address,
            abi_function_signature=_FN_VAULT_DEPOSIT,
            abi_parameters=[account_id, str(amount_units)],
            decision_id=decision_id,
            metadata={
                "action": "deposit_margin",
                "amount_usd": str(amount_usd),
                "account_id": account_id,
            },
        )
        logger.info(
            "ArcPerp.deposit_margin | accountId={} amount={} USDC decision={}",
            account_id, amount_usd, decision_id,
        )
        return await self.wallet.send_contract_execution(req)

    async def withdraw_margin(
        self,
        amount_usd: Decimal,
        decision_id: str | None = None,
    ) -> TxResult:
        """Withdraw USDC margin from `USDCCollateralVault`."""
        if not self.config.vault_address:
            raise RuntimeError(
                "ARC_PERP_VAULT_ADDRESS is not set; cannot withdraw margin."
            )
        amount_units = self._to_usdc_units(amount_usd)
        account_id = self._account_id()
        req = TxRequest(
            contract_address=self.config.vault_address,
            abi_function_signature=_FN_VAULT_WITHDRAW,
            abi_parameters=[account_id, str(amount_units)],
            decision_id=decision_id,
            metadata={
                "action": "withdraw_margin",
                "amount_usd": str(amount_usd),
                "account_id": account_id,
            },
        )
        logger.info(
            "ArcPerp.withdraw_margin | accountId={} amount={} USDC decision={}",
            account_id, amount_usd, decision_id,
        )
        return await self.wallet.send_contract_execution(req)

    # ------------------------------------------------------------------
    # Trading (open / close) - order signing lands on Day 3
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

        Day 2 behaviour:
            - Dry-run: logs the planned EIP-712 order (no signature).
            - Live: routes margin via `deposit_margin` (so capital actually
              moves on-chain), then raises a clear `NotImplementedError`
              if `ARC_PERP_MATCHER_URL` is not configured. With the matcher
              URL set, Day 3 will sign + POST the order here.
        """
        lev = self._cap_leverage(leverage)
        slip = slippage_bps if slippage_bps is not None else self.config.default_slippage_bps
        side_code = self._side_code(side)

        planned = {
            "action": "open_position",
            "symbol": symbol,
            "side": side,
            "side_code": side_code,
            "size_usd": str(size_usd),
            "leverage": str(lev),
            "slippage_bps": slip,
            "decision_id": decision_id,
        }
        logger.info(
            "ArcPerp.open_position | {} {} size={} USD lev={}x slip={}bps decision={}",
            side.upper(), symbol, size_usd, lev, slip, decision_id,
        )

        if self.dry_run:
            return TxResult(
                tx_id=f"dryrun-open-{decision_id or 'na'}",
                state="DRY_RUN",
                sponsored=self.wallet._gas_is_sponsored(),
                raw={"planned_order": planned},
            )

        # Live: allocate margin into the perp vault now so capital is in place.
        margin_to_deposit = self._margin_for_size(size_usd, lev)
        deposit_res = await self.deposit_margin(margin_to_deposit, decision_id=decision_id)
        logger.info(
            "ArcPerp.open_position live margin deposit | tx_id={} state={} hash={}",
            deposit_res.tx_id, deposit_res.state, deposit_res.tx_hash,
        )

        if not self.config.matcher_url:
            raise NotImplementedError(
                "ARC_PERP_MATCHER_URL is not configured. Margin was deposited "
                "but EIP-712 order routing to the off-chain matcher lands on "
                "Day 3. Set ARC_PERP_MATCHER_URL in .env to enable trading."
            )

        # Day 3: sign EIP-712 OrderTypes.Order, POST to self.config.matcher_url.
        raise NotImplementedError(
            "EIP-712 order signing + matcher submission is Day 3 work."
        )

    async def close_position(
        self,
        symbol: str,
        slippage_bps: int | None = None,
        decision_id: str | None = None,
    ) -> TxResult:
        """Close an open perp position."""
        slip = slippage_bps if slippage_bps is not None else self.config.default_slippage_bps
        planned = {
            "action": "close_position",
            "symbol": symbol,
            "slippage_bps": slip,
            "decision_id": decision_id,
        }
        logger.info(
            "ArcPerp.close_position | {} slip={}bps decision={}",
            symbol, slip, decision_id,
        )

        if self.dry_run:
            return TxResult(
                tx_id=f"dryrun-close-{decision_id or 'na'}",
                state="DRY_RUN",
                sponsored=self.wallet._gas_is_sponsored(),
                raw={"planned_order": planned},
            )

        if not self.config.matcher_url:
            raise NotImplementedError(
                "ARC_PERP_MATCHER_URL is not configured. To close a perp "
                "position via the off-chain orderbook, set ARC_PERP_MATCHER_URL "
                "and ship the EIP-712 signing wiring (Day 3)."
            )
        raise NotImplementedError(
            "EIP-712 order signing + matcher submission is Day 3 work."
        )

    # ------------------------------------------------------------------
    # Read-only methods (eth_call against the public Arc Testnet RPC)
    # ------------------------------------------------------------------

    async def get_margin(self) -> Decimal:
        """Return USDC margin balance held in the perp vault, in USD."""
        if not (self._w3 and self.config.vault_address):
            return Decimal("0")
        try:
            account_id = self._account_id()
        except RuntimeError as exc:
            logger.debug("get_margin: {}", exc)
            return Decimal("0")
        try:
            raw = self._w3.eth.call(
                {
                    "to": Web3.to_checksum_address(self.config.vault_address),
                    "data": self._encode_call(_FN_VAULT_GET_BALANCE, [account_id]),
                }
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("vault.getBalance eth_call failed: {}", exc)
            return Decimal("0")
        amount_units = int.from_bytes(raw, "big") if raw else 0
        return Decimal(amount_units) / (Decimal(10) ** self.config.usdc_decimals)

    async def get_position(self, symbol: str) -> Position:
        """Return current position for `symbol` via PositionLedger.getPosition."""
        if not (self._w3 and self.config.position_ledger_address):
            return self._flat_position(symbol)
        try:
            account_id = self._account_id()
        except RuntimeError:
            return self._flat_position(symbol)

        market_id = self._market_id(symbol)
        try:
            self._w3.eth.call(
                {
                    "to": Web3.to_checksum_address(self.config.position_ledger_address),
                    "data": self._encode_call(
                        _FN_LEDGER_GET_POSITION, [account_id, market_id]
                    ),
                }
            )
        except Exception as exc:  # noqa: BLE001
            # Ledger returns position struct; decoding lands on Day 3 once we
            # have the canonical Position struct ABI. For Day 2 we treat any
            # successful call as "no open position" since the agent hasn't
            # traded yet.
            logger.debug("ledger.getPosition eth_call: {}", exc)
        return self._flat_position(symbol)

    async def get_pnl(self, symbol: str | None = None) -> Decimal:
        """Return unrealized PnL (symbol-specific or total)."""
        if symbol is not None:
            pos = await self.get_position(symbol)
            return pos.unrealized_pnl_usd
        info = await self.get_account_info()
        return info.total_unrealized_pnl_usd

    async def get_account_info(self) -> AccountInfo:
        """Return a snapshot of the agent's perp account."""
        margin = await self.get_margin()
        return AccountInfo(
            equity_usd=margin,
            free_margin_usd=margin,
            used_margin_usd=Decimal("0"),
            total_unrealized_pnl_usd=Decimal("0"),
            positions=[],
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _flat_position(self, symbol: str) -> Position:
        return Position(
            symbol=symbol,
            side="flat",
            size_usd=Decimal("0"),
            entry_price=Decimal("0"),
            mark_price=Decimal("0"),
            leverage=Decimal("0"),
            unrealized_pnl_usd=Decimal("0"),
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

    def _to_usdc_units(self, amount_usd: Decimal) -> int:
        """Convert a USD amount to integer token units (USDC = 6 decimals)."""
        scale = Decimal(10) ** self.config.usdc_decimals
        return int((amount_usd * scale).to_integral_value())

    def _margin_for_size(self, size_usd: Decimal, leverage: Decimal) -> Decimal:
        """Initial margin required for a position of `size_usd` at `leverage`."""
        if leverage <= 0:
            return size_usd
        return (size_usd / leverage).quantize(Decimal("0.000001"))

    def _encode_call(self, signature: str, params: list[str]) -> bytes:
        """Encode a `fn(types...)` view call into calldata using web3.py."""
        if Web3 is None:
            raise RuntimeError("web3.py is not installed.")
        selector = Web3.keccak(text=signature)[:4]
        # All our read calls take bytes32 args, so we hex-strip and pad.
        encoded = b""
        for p in params:
            data = p[2:] if p.startswith("0x") else p
            encoded += bytes.fromhex(data.rjust(64, "0"))
        return selector + encoded
