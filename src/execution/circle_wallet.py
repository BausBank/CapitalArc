"""Circle Developer-Controlled Wallets (DCW) client + Paymaster integration.

This module talks to Circle's W3S REST API to:
    - Read balances of an agent wallet on Arc
    - Submit contract-execution transactions (open/close perp position,
      mint/redeem USYC, approve, transfer, etc.)
    - Optionally route gas through Circle Paymaster for gasless UX

References
----------
- Wallets:         POST/GET  https://api.circle.com/v1/w3s/wallets
- Contract exec:   POST      https://api.circle.com/v1/w3s/developer/transactions/contractExecution
- Transactions:    GET       https://api.circle.com/v1/w3s/transactions/{id}
- Balances:        GET       https://api.circle.com/v1/w3s/wallets/{id}/balances

Notes
-----
- Circle requires every signed operation to carry an `entitySecretCiphertext`
  produced by RSA-encrypting the entity secret with Circle's public key
  (see Circle docs). For Day 2 the encryption step is wired but optional;
  the wallet operates in `dry_run` mode by default, in which it logs the
  payload it WOULD have sent instead of hitting the API.
- Paymaster: when `paymaster_url` (or policy id) is configured, the
  contractExecution body includes the gas-station hints so Circle sponsors
  the gas via the configured policy.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

import httpx

from src.utils.logging import logger


@dataclass
class CircleWalletConfig:
    """Configuration for a single agent wallet on Circle DCW."""

    api_key: str
    wallet_id: str
    entity_secret: str | None = None
    base_url: str = "https://api.circle.com/v1/w3s"
    paymaster_url: str | None = None
    paymaster_policy_id: str | None = None
    timeout_seconds: float = 30.0


@dataclass
class TxRequest:
    """Inputs needed to submit a contractExecution to Circle."""

    contract_address: str
    abi_function_signature: str
    abi_parameters: list[Any]
    value_wei: int = 0
    decision_id: str | None = None
    fee_level: str = "MEDIUM"  # LOW | MEDIUM | HIGH
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class TxResult:
    """Result returned by `CircleWallet.send_contract_execution`."""

    tx_id: str
    state: str  # e.g. "INITIATED", "PENDING", "CONFIRMED", "FAILED", "DRY_RUN"
    tx_hash: str | None = None
    sponsored: bool = False
    raw: dict[str, Any] = field(default_factory=dict)


class CircleWallet:
    """Thin async client over the Circle Developer-Controlled Wallets API.

    Parameters
    ----------
    config :
        `CircleWalletConfig` with API key, wallet id and optional Paymaster
        configuration.
    dry_run :
        When True (default for Day 2 demos), every state-changing call is
        logged but NOT sent to Circle. Read-only calls (balances) still hit
        the API if credentials are present, otherwise they return stubbed
        zeros.
    """

    def __init__(self, config: CircleWalletConfig, dry_run: bool = True) -> None:
        self.config = config
        self.dry_run = dry_run
        self._client = httpx.AsyncClient(
            base_url=config.base_url,
            timeout=config.timeout_seconds,
            headers={
                "Authorization": f"Bearer {config.api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    # ------------------------------------------------------------------
    # Read-only methods
    # ------------------------------------------------------------------

    async def get_address(self) -> str | None:
        """Return the on-chain address for the configured wallet, if any."""
        if not self.config.api_key:
            logger.warning("CircleWallet.get_address: no API key, returning None")
            return None
        try:
            resp = await self._client.get(f"/wallets/{self.config.wallet_id}")
            resp.raise_for_status()
            data = resp.json().get("data", {}).get("wallet", {})
            return data.get("address")
        except httpx.HTTPError as exc:
            logger.error("CircleWallet.get_address failed: {}", exc)
            return None

    async def get_balance(self, token_address: str | None = None) -> Decimal:
        """Return wallet balance for `token_address` (or native if None)."""
        if not self.config.api_key:
            return Decimal("0")
        try:
            resp = await self._client.get(
                f"/wallets/{self.config.wallet_id}/balances"
            )
            resp.raise_for_status()
            balances = resp.json().get("data", {}).get("tokenBalances", [])
            for entry in balances:
                tok = entry.get("token", {})
                addr = (tok.get("tokenAddress") or "").lower()
                if token_address and addr == token_address.lower():
                    return Decimal(str(entry.get("amount", "0")))
                if token_address is None and tok.get("isNative"):
                    return Decimal(str(entry.get("amount", "0")))
            return Decimal("0")
        except httpx.HTTPError as exc:
            logger.error("CircleWallet.get_balance failed: {}", exc)
            return Decimal("0")

    # ------------------------------------------------------------------
    # State-changing methods
    # ------------------------------------------------------------------

    async def send_contract_execution(self, req: TxRequest) -> TxResult:
        """Submit a contract-call transaction via Circle DCW.

        In dry-run mode this only logs the would-be payload and returns a
        `TxResult` with `state="DRY_RUN"`.
        """
        idempotency_key = req.decision_id or str(uuid.uuid4())
        body = self._build_contract_execution_body(req, idempotency_key)
        sponsored = self._gas_is_sponsored()

        if self.dry_run:
            logger.info(
                "[dry-run] Circle DCW contractExecution would be sent | "
                "wallet={} contract={} fn={} sponsored={} key={}",
                self.config.wallet_id,
                req.contract_address,
                req.abi_function_signature,
                sponsored,
                idempotency_key,
            )
            return TxResult(
                tx_id=f"dryrun-{idempotency_key}",
                state="DRY_RUN",
                sponsored=sponsored,
                raw=body,
            )

        try:
            resp = await self._client.post(
                "/developer/transactions/contractExecution",
                json=body,
            )
            resp.raise_for_status()
            data = resp.json().get("data", {})
            return TxResult(
                tx_id=data.get("id", ""),
                state=data.get("state", "INITIATED"),
                tx_hash=data.get("txHash"),
                sponsored=sponsored,
                raw=data,
            )
        except httpx.HTTPError as exc:
            logger.error("CircleWallet.send_contract_execution failed: {}", exc)
            raise

    async def wait_for_tx(self, tx_id: str, poll_seconds: float = 2.0) -> TxResult:
        """Poll a Circle transaction until it leaves the pending states."""
        import asyncio

        if tx_id.startswith("dryrun-"):
            return TxResult(tx_id=tx_id, state="DRY_RUN")

        terminal = {"CONFIRMED", "COMPLETE", "FAILED", "DENIED", "CANCELLED"}
        while True:
            try:
                resp = await self._client.get(f"/transactions/{tx_id}")
                resp.raise_for_status()
                data = resp.json().get("data", {}).get("transaction", {})
                state = data.get("state", "PENDING")
                if state in terminal:
                    return TxResult(
                        tx_id=tx_id,
                        state=state,
                        tx_hash=data.get("txHash"),
                        raw=data,
                    )
            except httpx.HTTPError as exc:
                logger.warning("wait_for_tx poll error: {}", exc)
            await asyncio.sleep(poll_seconds)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _gas_is_sponsored(self) -> bool:
        return bool(
            self.config.paymaster_url or self.config.paymaster_policy_id
        )

    def _build_contract_execution_body(
        self,
        req: TxRequest,
        idempotency_key: str,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "idempotencyKey": idempotency_key,
            "walletId": self.config.wallet_id,
            "contractAddress": req.contract_address,
            "abiFunctionSignature": req.abi_function_signature,
            "abiParameters": req.abi_parameters,
            "feeLevel": req.fee_level,
        }
        if req.value_wei:
            body["amounts"] = [str(req.value_wei)]

        # Circle requires entitySecretCiphertext for state-changing calls.
        # The real implementation RSA-encrypts `entity_secret` with Circle's
        # public key. Wired here so Day 3 can drop in the encryption step.
        if self.config.entity_secret:
            body["entitySecretCiphertext"] = self._encrypt_entity_secret()

        # Paymaster: ask Circle to sponsor gas via the configured policy.
        if self._gas_is_sponsored():
            gas_meta: dict[str, Any] = {}
            if self.config.paymaster_policy_id:
                gas_meta["gasPolicyId"] = self.config.paymaster_policy_id
            if self.config.paymaster_url:
                gas_meta["paymasterUrl"] = self.config.paymaster_url
            body["gasStation"] = gas_meta

        if req.metadata:
            body["metadata"] = req.metadata

        return body

    def _encrypt_entity_secret(self) -> str:
        """Placeholder for RSA-encrypting the Circle entity secret.

        Day 3 wires this up with Circle's public key (PEM fetched from
        `/config/entity/publicKey`). For Day 2 dry-run we never reach this
        path; if a caller forces a live call without implementing this,
        Circle will reject it.
        """
        if self.config.entity_secret is None:
            return ""
        # Marker so accidental live calls fail loudly server-side.
        return "ENTITY_SECRET_NOT_ENCRYPTED_YET"
