"""Circle Developer-Controlled Wallets (DCW) client + Paymaster integration.

This module talks to Circle's W3S REST API to:
    - Read balances of an agent wallet on Arc
    - Submit contract-execution transactions (deposit/withdraw USDC margin,
      transfer, approve, perp-DEX calls, etc.)
    - Sponsor gas through Circle Paymaster / Gas Station when configured

It is the on-chain execution surface used by `ArcPerpExecutor` and (later)
by the USYC rotation in `AllocationRouter`.

Endpoints
---------
- Public key:      GET   /v1/w3s/config/entity/publicKey
- Contract exec:   POST  /v1/w3s/developer/transactions/contractExecution
- Transactions:    GET   /v1/w3s/transactions/{id}
- Wallet:          GET   /v1/w3s/wallets/{id}
- Balances:        GET   /v1/w3s/wallets/{id}/balances

Entity-secret encryption
------------------------
Every mutating Circle request must carry a unique `entitySecretCiphertext`
produced by **RSA-OAEP-SHA256** encryption of the 32-byte entity secret
with Circle's entity public key (a PEM fetched once from the public-key
endpoint above). OAEP injects fresh randomness per call, so calling the
encryption twice with the same plaintext yields different ciphertexts -
this is exactly what Circle requires to prevent replay.
"""

from __future__ import annotations

import asyncio
import base64
import uuid
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicKey

from src.utils.logging import logger


@dataclass
class CircleWalletConfig:
    """Configuration for a single agent wallet on Circle DCW."""

    api_key: str
    wallet_id: str
    entity_secret: str | None = None  # 64-hex-char string (32 bytes)
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
    state: str  # INITIATED | PENDING | CONFIRMED | COMPLETE | FAILED | DRY_RUN
    tx_hash: str | None = None
    sponsored: bool = False
    raw: dict[str, Any] = field(default_factory=dict)


class CircleWallet:
    """Async client over Circle Developer-Controlled Wallets API.

    Parameters
    ----------
    config :
        `CircleWalletConfig` with API key, wallet id, entity secret, and
        optional Paymaster / Gas Station policy.
    dry_run :
        When True every state-changing call is logged but NOT sent to Circle.
        Read-only calls (balance, address) still hit the API if credentials
        are present.
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
        self._public_key: RSAPublicKey | None = None
        self._public_key_lock = asyncio.Lock()

    async def aclose(self) -> None:
        await self._client.aclose()

    # ------------------------------------------------------------------
    # Entity-secret encryption (RSA-OAEP-SHA256)
    # ------------------------------------------------------------------

    async def _get_public_key(self) -> RSAPublicKey:
        """Fetch and cache Circle's entity public key."""
        if self._public_key is not None:
            return self._public_key
        async with self._public_key_lock:
            if self._public_key is not None:
                return self._public_key
            resp = await self._client.get("/config/entity/publicKey")
            resp.raise_for_status()
            pem = resp.json().get("data", {}).get("publicKey", "")
            if not pem:
                raise RuntimeError(
                    "Circle returned empty public key from /config/entity/publicKey"
                )
            key = serialization.load_pem_public_key(pem.encode("ascii"))
            if not isinstance(key, RSAPublicKey):
                raise RuntimeError(
                    "Circle entity public key is not RSA - unexpected format"
                )
            self._public_key = key
            logger.debug(
                "CircleWallet: cached entity public key ({}-bit RSA)", key.key_size
            )
            return key

    async def _encrypt_entity_secret(self) -> str:
        """Encrypt the 32-byte entity secret with Circle's RSA public key.

        Uses RSA-OAEP with SHA-256 for both the hash and MGF1, matching
        Circle's published encryption spec. Returns base64 ciphertext.
        Each call produces a fresh ciphertext (OAEP padding is randomised).
        """
        if not self.config.entity_secret:
            raise RuntimeError(
                "CIRCLE_ENTITY_SECRET is not configured; cannot sign Circle requests."
            )
        try:
            raw = bytes.fromhex(self.config.entity_secret)
        except ValueError as exc:
            raise RuntimeError(
                "CIRCLE_ENTITY_SECRET is not valid hex (expected 64 hex chars)."
            ) from exc
        if len(raw) != 32:
            raise RuntimeError(
                f"CIRCLE_ENTITY_SECRET must be 32 bytes, got {len(raw)} bytes."
            )

        key = await self._get_public_key()
        ciphertext = key.encrypt(
            raw,
            padding.OAEP(
                mgf=padding.MGF1(algorithm=hashes.SHA256()),
                algorithm=hashes.SHA256(),
                label=None,
            ),
        )
        return base64.b64encode(ciphertext).decode("ascii")

    # ------------------------------------------------------------------
    # Read-only methods
    # ------------------------------------------------------------------

    async def get_address(self) -> str | None:
        """Return the on-chain address for the configured wallet, if any."""
        if not (self.config.api_key and self.config.wallet_id):
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
        if not (self.config.api_key and self.config.wallet_id):
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

        Dry-run mode logs the would-be payload and returns `state="DRY_RUN"`.
        Live mode:
            1. Encrypts the entity secret freshly (RSA-OAEP-SHA256).
            2. POSTs to `/developer/transactions/contractExecution`.
            3. Returns `TxResult` with Circle's tx id, state and hash.
        """
        idempotency_key = req.decision_id or str(uuid.uuid4())
        sponsored = self._gas_is_sponsored()

        if self.dry_run:
            body_preview = self._build_contract_execution_body(
                req, idempotency_key, ciphertext=None
            )
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
                raw=body_preview,
            )

        if not self.config.wallet_id:
            raise RuntimeError(
                "CIRCLE_AGENT_WALLET_ID is not set; cannot submit live tx."
            )

        ciphertext = await self._encrypt_entity_secret()
        body = self._build_contract_execution_body(
            req, idempotency_key, ciphertext=ciphertext
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

    async def wait_for_tx(
        self,
        tx_id: str,
        poll_seconds: float = 2.0,
        timeout_seconds: float = 180.0,
    ) -> TxResult:
        """Poll a Circle transaction until terminal state or timeout."""
        if tx_id.startswith("dryrun-"):
            return TxResult(tx_id=tx_id, state="DRY_RUN")

        terminal = {"COMPLETE", "CONFIRMED", "FAILED", "DENIED", "CANCELLED"}
        elapsed = 0.0
        while elapsed < timeout_seconds:
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
            elapsed += poll_seconds
        logger.warning("wait_for_tx timeout for {}", tx_id)
        return TxResult(tx_id=tx_id, state="PENDING")

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _gas_is_sponsored(self) -> bool:
        return bool(self.config.paymaster_policy_id or self.config.paymaster_url)

    def _build_contract_execution_body(
        self,
        req: TxRequest,
        idempotency_key: str,
        ciphertext: str | None,
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
            body["amount"] = str(req.value_wei)

        if ciphertext is not None:
            body["entitySecretCiphertext"] = ciphertext

        # Paymaster / Gas Station: when a policy ID is configured, Circle
        # sponsors gas via the policy attached to the wallet's Gas Station.
        if self.config.paymaster_policy_id:
            body["gasPolicyId"] = self.config.paymaster_policy_id
        if self.config.paymaster_url:
            # Supplementary hint for paymaster URL overrides (rarely needed).
            body.setdefault("paymaster", {})["url"] = self.config.paymaster_url

        if req.metadata:
            body["metadata"] = req.metadata

        return body
