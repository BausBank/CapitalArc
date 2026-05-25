"""USYC (Hashnote Short-Duration Yield Coin) executor.

USYC is the **risk-off leg** of CapitalArc: a yield-bearing tokenised
exposure to a basket of short-duration US Treasuries. When the
decision engine flips to ``risk_off`` (low conviction, neutral
direction) the agent unwinds its perp position(s), pulls margin out
of `USDCCollateralVault`, and rotates the freed USDC into USYC so
the capital keeps earning ~T-bill yield while we wait for the next
risk-on setup. When the engine flips back to ``risk_on`` we redeem
just enough USYC to fund the new perp position, leaving the rest
parked in yield.

What this module provides
=========================
A small, focused executor mirroring the shape of
:class:`ArcPerpExecutor`:

    mint(amount_usdc, decision_id)     # USDC -> USYC (risk-off)
    redeem(amount_usyc, decision_id)   # USYC -> USDC (risk-on)
    get_usyc_balance()                 # wallet's USYC balance
    get_usyc_value_usd()               # USYC balance valued at $1 per share
    get_snapshot() -> USYCSnapshot
    is_configured  -> bool             # gating for graceful no-op fallback

Live transactions go through :class:`CircleWallet` (Developer-Controlled
Wallet + Paymaster on Arc); dry-run mode logs the would-be payload and
returns a ``state="DRY_RUN"`` :class:`TxResult` exactly like the perp
executor. **No real on-chain action ever happens unless ``dry_run`` is
False AND ``USYC_MINT_CONTRACT_ADDRESS`` + ``USYC_TOKEN_ADDRESS`` are
both configured in ``.env``** - the AllocationRouter is explicitly
designed to degrade gracefully when USYC isn't wired (it just skips
the rotation leg with an explanatory note in the ExecutionPlan).

USYC ABI surface
================
Most USYC-style RWA tokens (Hashnote USYC, Ondo USDY, Backed bIBTA) use
a vanilla mint / redeem pair priced 1:1 against USDC at mint:

    function mint(uint256 usdcAmount)     external returns (uint256 shares)
    function redeem(uint256 usycAmount)   external returns (uint256 usdcOut)
    function balanceOf(address holder)    external view returns (uint256)

The exact function names occasionally differ across implementations
(``deposit`` / ``withdraw``, ``subscribe`` / ``unsubscribe``) so this
module exposes both signatures as ``USYCExecutorConfig`` fields,
overridable from ``.env``. We pre-approve USDC via the standard ERC-20
``approve(spender, amount)`` before minting, since the mint contract
needs to ``transferFrom`` the user's USDC.

Pricing
=======
USYC is yield-bearing, so its on-chain share price drifts above $1.00
over time. For the simple, safety-first sizing CapitalArc does today
we treat USYC at $1.00/share (a small but conservative under-estimate
of the true mark - real value is always ≥ $1). The deviation is
fractions of a percent over normal rotation horizons and is dwarfed
by the perp position sizing decisions; if/when we wire the actual
``pricePerShare()`` reader (Day 6+) the rest of the pipeline doesn't
need to change.
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
# Default function signatures (overridable via USYCExecutorConfig)
# --------------------------------------------------------------------------
_FN_USYC_MINT = "mint(uint256)"
_FN_USYC_REDEEM = "redeem(uint256)"
_FN_USYC_BALANCE_OF = "balanceOf(address)"
_FN_USDC_APPROVE = "approve(address,uint256)"
_FN_USDC_BALANCE_OF = "balanceOf(address)"


@dataclass
class USYCExecutorConfig:
    """Configuration for the USYC mint / redeem executor."""

    # Token + mint contract addresses (both required for live ops).
    usyc_token_address: str | None = None
    usyc_mint_address: str | None = None
    # USDC token address (paying token for mint, payout for redeem).
    usdc_address: str | None = None

    # Function signatures. Tweak these via ``.env`` if the deployed
    # USYC contract on Arc uses non-standard names
    # (``deposit`` / ``withdraw``, ``subscribe`` / ``unsubscribe``).
    mint_signature: str = _FN_USYC_MINT
    redeem_signature: str = _FN_USYC_REDEEM

    # Decimals - USYC + USDC are both 6 decimals on the major
    # deployments, but kept overridable.
    usdc_decimals: int = 6
    usyc_decimals: int = 6

    # Smallest USDC amount we'll actually rotate. Anything below
    # is logged + skipped to avoid spamming the chain (and Circle's
    # rate limits) with dust transactions.
    min_rotation_usdc: Decimal = Decimal("100")
    # Hard cap on a single USYC mint - safety net, makes a runaway
    # decision-engine loop financially harmless on testnet.
    max_rotation_usdc: Decimal = Decimal("100000")
    # Reserve we keep in the wallet's free USDC at all times, so the
    # agent can pay gas in non-sponsored fallbacks and meet the next
    # perp position's initial-margin call without a redeem dance.
    usdc_reserve_usd: Decimal = Decimal("50")

    # RPC for read-only balance / price queries.
    rpc_url: str = "https://rpc.testnet.arc.network"

    # ---- Live tx-settlement controls ----
    # In ``--live`` the USYC mint sequence is two transactions
    # (approve -> mint). We MUST wait for the approve to confirm on
    # chain before submitting the mint, otherwise the mint reverts
    # with "ERC20: insufficient allowance". These knobs cap how long
    # we'll wait and how often we poll Circle's transaction endpoint.
    approve_wait_timeout_seconds: float = 90.0
    approve_wait_poll_seconds: float = 3.0


@dataclass
class USYCSnapshot:
    """Aggregated USYC + paying-USDC state for a single account."""

    usyc_balance: Decimal           # tokens held
    usyc_value_usd: Decimal         # tokens * price_per_share (USD)
    usdc_balance: Decimal           # free USDC sitting in the wallet
    configured: bool                # USYC contracts wired?
    notes: list[str] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)


class USYCExecutor:
    """High-level USYC mint / redeem executor backed by Circle DCW.

    Parameters
    ----------
    wallet :
        Circle DCW wallet used to sign contract-execution calls.
    config :
        :class:`USYCExecutorConfig` with token + mint addresses,
        ABI signatures and risk caps.
    account_address :
        EVM address of the agent wallet (Circle DCW). Late-bind via
        :meth:`set_account_address` if you don't know it at construction.
    dry_run :
        When True (default), no on-chain tx is submitted - we log the
        planned payload and return ``state="DRY_RUN"``.
    """

    def __init__(
        self,
        wallet: CircleWallet,
        config: USYCExecutorConfig,
        account_address: str | None = None,
        dry_run: bool = True,
    ) -> None:
        self.wallet = wallet
        self.config = config
        self.dry_run = dry_run
        self.account_address = account_address
        self._w3: Any = None
        if Web3 is not None and config.rpc_url:
            try:
                self._w3 = Web3(Web3.HTTPProvider(config.rpc_url))
            except Exception as exc:  # noqa: BLE001 - keep init robust
                logger.warning("USYC RPC init failed: {}", exc)
                self._w3 = None

    # ------------------------------------------------------------------
    # Identity / health helpers
    # ------------------------------------------------------------------

    def set_account_address(self, address: str) -> None:
        """Late-bind the agent's wallet address."""
        self.account_address = address

    @property
    def is_configured(self) -> bool:
        """``True`` when both the USYC token and mint contract are wired.

        The AllocationRouter checks this flag and skips the rotation
        leg with an explanatory note when USYC isn't configured, rather
        than failing the cycle. Letting the demo run without USYC is
        intentional - real USYC contracts on Arc Testnet are still
        being deployed.
        """
        return bool(
            self.config.usyc_token_address
            and self.config.usyc_mint_address
            and self.config.usdc_address
        )

    # ------------------------------------------------------------------
    # Live mint / redeem
    # ------------------------------------------------------------------

    async def mint(
        self,
        amount_usdc: Decimal,
        decision_id: str | None = None,
    ) -> TxResult:
        """Convert ``amount_usdc`` USDC into USYC by calling the mint contract.

        Live behaviour (two-step, **ordered**):
            1. Pre-approves the mint contract to pull ``amount_usdc``
               from the wallet's USDC balance (``USDC.approve``).
            2. **Waits for the approve tx to confirm on chain.**
               Without this wait the subsequent mint call reverts with
               ``ERC20: insufficient allowance`` because Circle has
               submitted but not yet settled the approve.
            3. Calls ``USYC.mint(uint256 usdcAmount)`` on the mint
               contract. Circle DCW + Paymaster cover gas.

        If the approve fails (FAILED / DENIED / CANCELLED), the mint
        is **NOT** submitted - we surface the failed approve result so
        the AllocationRouter can record the rotation leg as failed
        instead of silently sending a doomed mint.

        Dry-run logs the would-be payload and returns ``DRY_RUN``.
        Raises if USYC isn't configured - callers should gate on
        :attr:`is_configured` first.

        Returns
        -------
        TxResult
            The mint result on success, or the approve TxResult when
            we never made it that far. ``raw["approve_tx_id"]`` always
            carries the approve id so the panel can trace the chain.
        """
        if not self.is_configured:
            raise RuntimeError(
                "USYC executor not configured - set USYC_TOKEN_ADDRESS, "
                "USYC_MINT_CONTRACT_ADDRESS and USDC_TOKEN_ADDRESS in .env."
            )
        amount = self._sanitise_amount(amount_usdc, side="mint")
        if amount <= 0:
            return self._noop_tx(
                state="DRY_RUN" if self.dry_run else "SKIPPED",
                reason="rotation amount below minimum",
                decision_id=decision_id,
                action="usyc_mint_skipped",
                amount_usd=amount_usdc,
            )

        units = self._to_units(amount, self.config.usdc_decimals)
        logger.info(
            "USYC.mint | amount={} USDC ({} units) decision={} dry_run={}",
            amount, units, decision_id, self.dry_run,
        )

        # ---- 1. approve(spender = mint contract, amount) -------------
        approve_req = TxRequest(
            contract_address=self.config.usdc_address,  # type: ignore[arg-type]
            abi_function_signature=_FN_USDC_APPROVE,
            abi_parameters=[self.config.usyc_mint_address, str(units)],
            decision_id=(decision_id or "usyc-mint") + "-approve",
            metadata={
                "action": "usyc_mint.approve",
                "amount_usd": str(amount),
                "spender": self.config.usyc_mint_address,
            },
        )
        approve_res = await self.wallet.send_contract_execution(approve_req)
        logger.info(
            "USYC.mint approve tx | id={} state={} hash={} sponsored={}",
            approve_res.tx_id, approve_res.state, approve_res.tx_hash,
            approve_res.sponsored,
        )

        # ---- 2. Wait for approve to confirm (live only) --------------
        # In dry-run the tx is synthetic (state="DRY_RUN") and we skip
        # the wait so the demo stays instant. In live we MUST wait or
        # the mint will revert on insufficient-allowance.
        if not self.dry_run and approve_res.state not in {"DRY_RUN", "SKIPPED"}:
            settled_approve = await self.wallet.wait_for_tx(
                approve_res.tx_id,
                poll_seconds=self.config.approve_wait_poll_seconds,
                timeout_seconds=self.config.approve_wait_timeout_seconds,
            )
            logger.info(
                "USYC.mint approve settled | id={} state={} hash={}",
                settled_approve.tx_id,
                settled_approve.state,
                settled_approve.tx_hash,
            )
            if settled_approve.state not in {"CONFIRMED", "COMPLETE", "COMPLETED"}:
                logger.error(
                    "USYC.mint aborting - approve did not confirm "
                    "(state={}). Mint will NOT be submitted to avoid a "
                    "guaranteed revert (ERC20: insufficient allowance).",
                    settled_approve.state,
                )
                # Surface the failed approve back to the caller so the
                # router records the leg as failed.
                return TxResult(
                    tx_id=settled_approve.tx_id,
                    state=settled_approve.state or "FAILED",
                    tx_hash=settled_approve.tx_hash,
                    sponsored=approve_res.sponsored,
                    raw={
                        "action": "usyc_mint.approve_failed",
                        "amount_usd": str(amount),
                        "approve_tx_id": approve_res.tx_id,
                        "approve_state": settled_approve.state,
                    },
                )

        # ---- 3. mint(usdcAmount) -------------------------------------
        mint_req = TxRequest(
            contract_address=self.config.usyc_mint_address,  # type: ignore[arg-type]
            abi_function_signature=self.config.mint_signature,
            abi_parameters=[str(units)],
            decision_id=decision_id,
            metadata={
                "action": "usyc_mint",
                "amount_usd": str(amount),
                "approve_tx_id": approve_res.tx_id,
            },
        )
        mint_res = await self.wallet.send_contract_execution(mint_req)
        # Stamp the approve tx id on the mint raw so the panel can
        # render the full chain (approve -> mint) at a glance.
        if isinstance(mint_res.raw, dict):
            mint_res.raw = {**mint_res.raw, "approve_tx_id": approve_res.tx_id}
        logger.info(
            "USYC.mint mint tx | id={} state={} hash={} approve_tx={}",
            mint_res.tx_id, mint_res.state, mint_res.tx_hash, approve_res.tx_id,
        )
        return mint_res

    async def redeem(
        self,
        amount_usyc: Decimal,
        decision_id: str | None = None,
    ) -> TxResult:
        """Redeem ``amount_usyc`` USYC tokens for USDC.

        Dry-run logs and returns ``DRY_RUN``. Live submits a single
        ``USYC.redeem(uint256 usycAmount)`` transaction via Circle DCW.
        """
        if not self.is_configured:
            raise RuntimeError(
                "USYC executor not configured - set USYC_TOKEN_ADDRESS, "
                "USYC_MINT_CONTRACT_ADDRESS and USDC_TOKEN_ADDRESS in .env."
            )
        amount = self._sanitise_amount(amount_usyc, side="redeem")
        if amount <= 0:
            return self._noop_tx(
                state="DRY_RUN" if self.dry_run else "SKIPPED",
                reason="redeem amount below minimum",
                decision_id=decision_id,
                action="usyc_redeem_skipped",
                amount_usd=amount_usyc,
            )

        units = self._to_units(amount, self.config.usyc_decimals)
        logger.info(
            "USYC.redeem | amount={} USYC ({} units) decision={} dry_run={}",
            amount, units, decision_id, self.dry_run,
        )
        redeem_req = TxRequest(
            contract_address=self.config.usyc_mint_address,  # type: ignore[arg-type]
            abi_function_signature=self.config.redeem_signature,
            abi_parameters=[str(units)],
            decision_id=decision_id,
            metadata={
                "action": "usyc_redeem",
                "amount_usyc": str(amount),
            },
        )
        result = await self.wallet.send_contract_execution(redeem_req)
        logger.info(
            "USYC.redeem tx | id={} state={} hash={} sponsored={}",
            result.tx_id, result.state, result.tx_hash, result.sponsored,
        )
        return result

    # ------------------------------------------------------------------
    # Read-only state
    # ------------------------------------------------------------------

    async def get_usyc_balance(self) -> Decimal:
        """Return the wallet's USYC balance in *tokens*, not USD."""
        if not (
            self._w3
            and self.config.usyc_token_address
            and self.account_address
        ):
            return Decimal("0")
        try:
            raw = self._w3.eth.call(
                {
                    "to": Web3.to_checksum_address(
                        self.config.usyc_token_address
                    ),
                    "data": self._encode_balance_of(self.account_address),
                }
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("USYC.balanceOf eth_call failed: {}", exc)
            return Decimal("0")
        amount_units = int.from_bytes(raw, "big") if raw else 0
        return Decimal(amount_units) / (
            Decimal(10) ** self.config.usyc_decimals
        )

    async def get_usyc_value_usd(self) -> Decimal:
        """USYC balance valued in USD.

        We mark each share at $1.00 (a conservative under-estimate of
        the real, yield-drift-inclusive value). Day-6+ work will wire
        the actual ``pricePerShare()`` reader; until then the rest of
        the pipeline is unaffected because sizing always uses USYC
        value *only* as a redeemable-capital ceiling.
        """
        tokens = await self.get_usyc_balance()
        return tokens  # ~1.0 USD per share (conservative)

    async def get_usdc_balance(self) -> Decimal:
        """Return the wallet's free USDC balance in USD."""
        if not (
            self._w3
            and self.config.usdc_address
            and self.account_address
        ):
            return Decimal("0")
        try:
            raw = self._w3.eth.call(
                {
                    "to": Web3.to_checksum_address(self.config.usdc_address),
                    "data": self._encode_balance_of(self.account_address),
                }
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("USDC.balanceOf eth_call failed: {}", exc)
            return Decimal("0")
        amount_units = int.from_bytes(raw, "big") if raw else 0
        return Decimal(amount_units) / (
            Decimal(10) ** self.config.usdc_decimals
        )

    async def get_snapshot(self) -> USYCSnapshot:
        """Aggregate USYC + free-USDC state for the panel + the router."""
        notes: list[str] = []
        if not self.is_configured:
            notes.append(
                "USYC executor not wired: USYC_TOKEN_ADDRESS / "
                "USYC_MINT_CONTRACT_ADDRESS / USDC_TOKEN_ADDRESS missing - "
                "risk-off rotation skipped with a note."
            )
        if not self.account_address:
            notes.append(
                "Agent wallet address unknown - cannot read USYC / USDC "
                "balance until Circle DCW resolves the address."
            )

        usyc_balance = await self.get_usyc_balance()
        usyc_value = await self.get_usyc_value_usd()
        usdc_balance = await self.get_usdc_balance()
        return USYCSnapshot(
            usyc_balance=usyc_balance,
            usyc_value_usd=usyc_value,
            usdc_balance=usdc_balance,
            configured=self.is_configured,
            notes=notes,
            raw={
                "token": self.config.usyc_token_address,
                "mint": self.config.usyc_mint_address,
                "usdc": self.config.usdc_address,
            },
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _sanitise_amount(self, amount: Decimal, *, side: str) -> Decimal:
        """Clamp ``amount`` to ``[0, max_rotation_usdc]`` and floor at zero.

        Sub-``min_rotation_usdc`` requests are floored to ``0`` so the
        caller can rely on a single comparison to detect "skipped".
        """
        if amount <= 0:
            return Decimal("0")
        if amount < self.config.min_rotation_usdc:
            logger.info(
                "USYC.{} amount {} below MIN_ROTATION {} - skipping.",
                side, amount, self.config.min_rotation_usdc,
            )
            return Decimal("0")
        if amount > self.config.max_rotation_usdc:
            logger.warning(
                "USYC.{} amount {} above MAX_ROTATION {} - capping.",
                side, amount, self.config.max_rotation_usdc,
            )
            return self.config.max_rotation_usdc
        return amount.quantize(Decimal("0.000001"))

    def _noop_tx(
        self,
        *,
        state: str,
        reason: str,
        decision_id: str | None,
        action: str,
        amount_usd: Decimal,
    ) -> TxResult:
        """Build a synthetic ``TxResult`` for skipped (sub-minimum) rotations."""
        return TxResult(
            tx_id=f"noop-{action}-{decision_id or 'na'}",
            state=state,
            sponsored=self.wallet._gas_is_sponsored(),
            raw={
                "action": action,
                "reason": reason,
                "amount_usd": str(amount_usd),
            },
        )

    def _to_units(self, amount: Decimal, decimals: int) -> int:
        """Convert a Decimal USD/token amount to integer token units."""
        scale = Decimal(10) ** decimals
        return int((amount * scale).to_integral_value())

    @staticmethod
    def _encode_balance_of(address: str) -> bytes:
        """Encode ``balanceOf(address)`` calldata for an ERC-20 view call."""
        if Web3 is None:
            raise RuntimeError("web3.py is not installed.")
        selector = Web3.keccak(text=_FN_USDC_BALANCE_OF)[:4]
        addr = address.lower().removeprefix("0x").rjust(64, "0")
        return selector + bytes.fromhex(addr)


__all__ = [
    "USYCExecutor",
    "USYCExecutorConfig",
    "USYCSnapshot",
]
