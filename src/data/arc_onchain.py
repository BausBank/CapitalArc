"""Read-only on-chain helpers for the Arc Perp DEX.

Level 2 uses this client to enrich its on-chain intelligence with the
actual state of the Arc Testnet perp stack:

- USDC TVL held in `USDCCollateralVault` (the vault's USDC balance).
- The agent's own margin balance (`vault.getBalance(accountId)`).
- Net flow into the vault over a recent block window (deposit -
  withdraw, derived from `Transfer` events on the USDC token).
- Whether `PositionLedger` reports any open position for the agent
  on a given market.

All reads go through `web3.py` against `ARC_RPC_URL`. The client is
synchronous under the hood (web3 6.x has limited async support), but
we wrap the heavy calls in `asyncio.to_thread` so the engine loop is
never blocked.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from src.utils.logging import logger

try:
    from web3 import Web3
except ImportError:  # pragma: no cover - web3 is in requirements.txt
    Web3 = None  # type: ignore[assignment]


# Standard ERC-20 minimal ABI bits we need (balanceOf + Transfer topic0).
_ERC20_BALANCE_OF_SIG = "balanceOf(address)"
# keccak256("Transfer(address,address,uint256)")
_ERC20_TRANSFER_TOPIC = (
    "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
)

_VAULT_BALANCE_OF_SIG = "getBalance(bytes32)"


@dataclass
class ArcOnchainConfig:
    """Configuration for the Arc on-chain reader."""

    rpc_url: str
    vault_address: str | None = None
    usdc_address: str | None = None
    usdc_decimals: int = 6
    flow_lookback_blocks: int = 5_000
    chain_id: int | None = None


@dataclass
class VaultFlowSnapshot:
    """Recent net-flow snapshot for `USDCCollateralVault`."""

    block_window: int
    deposits_usdc: Decimal = Decimal("0")
    withdrawals_usdc: Decimal = Decimal("0")
    deposit_events: int = 0
    withdrawal_events: int = 0
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def net_flow_usdc(self) -> Decimal:
        return self.deposits_usdc - self.withdrawals_usdc


@dataclass
class ArcOnchainSnapshot:
    """Aggregated on-chain reading at a moment in time."""

    rpc_url: str
    chain_id: int | None
    latest_block: int | None
    vault_tvl_usdc: Decimal
    vault_address: str | None
    usdc_address: str | None
    agent_margin_usdc: Decimal | None = None
    flow: VaultFlowSnapshot | None = None
    healthy: bool = True
    notes: list[str] = field(default_factory=list)


class ArcOnchainReader:
    """Read-only Arc Testnet client wrapping `web3.py`."""

    def __init__(self, config: ArcOnchainConfig) -> None:
        self.config = config
        self._w3: Any = None
        if Web3 is not None and config.rpc_url:
            try:
                self._w3 = Web3(Web3.HTTPProvider(config.rpc_url))
            except Exception as exc:  # noqa: BLE001
                logger.warning("Arc RPC init failed: {}", exc)
                self._w3 = None

    # ------------------------------------------------------------------
    # Health
    # ------------------------------------------------------------------

    @property
    def available(self) -> bool:
        return self._w3 is not None

    async def latest_block(self) -> int | None:
        if not self.available:
            return None
        return await asyncio.to_thread(self._latest_block_sync)

    def _latest_block_sync(self) -> int | None:
        try:
            return int(self._w3.eth.block_number)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Arc latest_block failed: {}", exc)
            return None

    # ------------------------------------------------------------------
    # Vault TVL & flow
    # ------------------------------------------------------------------

    async def get_vault_tvl_usdc(self) -> Decimal:
        if not (
            self.available
            and self.config.usdc_address
            and self.config.vault_address
        ):
            return Decimal("0")
        return await asyncio.to_thread(self._get_vault_tvl_sync)

    def _get_vault_tvl_sync(self) -> Decimal:
        try:
            usdc = Web3.to_checksum_address(self.config.usdc_address)
            vault = Web3.to_checksum_address(self.config.vault_address)
            selector = Web3.keccak(text=_ERC20_BALANCE_OF_SIG)[:4]
            addr_bytes = bytes.fromhex(vault[2:].rjust(64, "0"))
            data = selector + addr_bytes
            raw = self._w3.eth.call({"to": usdc, "data": data})
            amount_units = int.from_bytes(raw, "big") if raw else 0
            return Decimal(amount_units) / (
                Decimal(10) ** self.config.usdc_decimals
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Arc vault TVL read failed: {}", exc)
            return Decimal("0")

    async def get_account_margin_usdc(
        self, account_id: str | None
    ) -> Decimal | None:
        """Read the agent's margin balance from the vault, if known."""
        if not (account_id and self.available and self.config.vault_address):
            return None
        return await asyncio.to_thread(self._get_margin_sync, account_id)

    def _get_margin_sync(self, account_id: str) -> Decimal | None:
        try:
            vault = Web3.to_checksum_address(self.config.vault_address)
            selector = Web3.keccak(text=_VAULT_BALANCE_OF_SIG)[:4]
            acc = account_id[2:] if account_id.startswith("0x") else account_id
            data = selector + bytes.fromhex(acc.rjust(64, "0"))
            raw = self._w3.eth.call({"to": vault, "data": data})
            amount_units = int.from_bytes(raw, "big") if raw else 0
            return Decimal(amount_units) / (
                Decimal(10) ** self.config.usdc_decimals
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("Arc vault margin read failed: {}", exc)
            return None

    async def get_vault_flow(
        self, lookback_blocks: int | None = None
    ) -> VaultFlowSnapshot | None:
        """Compute net deposits / withdrawals to the vault over a recent window."""
        if not (
            self.available
            and self.config.usdc_address
            and self.config.vault_address
        ):
            return None
        window = lookback_blocks or self.config.flow_lookback_blocks
        return await asyncio.to_thread(self._vault_flow_sync, window)

    def _vault_flow_sync(self, window: int) -> VaultFlowSnapshot | None:
        try:
            latest = int(self._w3.eth.block_number)
            from_block = max(0, latest - window)
            vault = Web3.to_checksum_address(self.config.vault_address)
            vault_topic = (
                "0x"
                + vault[2:].lower().rjust(64, "0")
            )
            usdc = Web3.to_checksum_address(self.config.usdc_address)
            # Deposits: Transfer(*, vault, *)
            in_logs = self._w3.eth.get_logs(
                {
                    "address": usdc,
                    "fromBlock": from_block,
                    "toBlock": latest,
                    "topics": [_ERC20_TRANSFER_TOPIC, None, vault_topic],
                }
            )
            # Withdrawals: Transfer(vault, *, *)
            out_logs = self._w3.eth.get_logs(
                {
                    "address": usdc,
                    "fromBlock": from_block,
                    "toBlock": latest,
                    "topics": [_ERC20_TRANSFER_TOPIC, vault_topic, None],
                }
            )
            decimals = Decimal(10) ** self.config.usdc_decimals
            in_sum = sum(
                (Decimal(int(l["data"], 16)) for l in in_logs),
                start=Decimal("0"),
            )
            out_sum = sum(
                (Decimal(int(l["data"], 16)) for l in out_logs),
                start=Decimal("0"),
            )
            return VaultFlowSnapshot(
                block_window=latest - from_block,
                deposits_usdc=in_sum / decimals,
                withdrawals_usdc=out_sum / decimals,
                deposit_events=len(in_logs),
                withdrawal_events=len(out_logs),
                raw={"from_block": from_block, "to_block": latest},
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("Arc vault flow read failed: {}", exc)
            return None

    # ------------------------------------------------------------------
    # Aggregate snapshot
    # ------------------------------------------------------------------

    async def snapshot(self, account_id: str | None = None) -> ArcOnchainSnapshot:
        notes: list[str] = []
        latest = await self.latest_block()
        tvl = await self.get_vault_tvl_usdc()
        margin = await self.get_account_margin_usdc(account_id)
        flow = await self.get_vault_flow()
        if not self.available:
            notes.append("RPC unavailable - read-only metrics unavailable")
        if not self.config.usdc_address:
            notes.append("USDC_TOKEN_ADDRESS not set - TVL skipped")
        if not self.config.vault_address:
            notes.append("ARC_PERP_VAULT_ADDRESS not set - vault reads skipped")
        return ArcOnchainSnapshot(
            rpc_url=self.config.rpc_url,
            chain_id=self.config.chain_id,
            latest_block=latest,
            vault_tvl_usdc=tvl,
            vault_address=self.config.vault_address,
            usdc_address=self.config.usdc_address,
            agent_margin_usdc=margin,
            flow=flow,
            healthy=self.available and (latest is not None),
            notes=notes,
        )
