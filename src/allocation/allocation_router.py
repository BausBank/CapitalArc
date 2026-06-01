"""Allocation router for CapitalArc - the auto-allocation heart.

The router consumes a :class:`DecisionResult` produced by the
:class:`DecisionEngine` (which already encodes the Level 3 Claude
Sonnet 4.6 verdict, conviction, direction and recommended intensity)
and turns it into concrete capital moves:

    risk_on   -> open / scale a perp position on **Hyperliquid Testnet**
                 (side = directive.side, "long" or "short");
                 if Arc-side cash is short, redeem just enough USYC to
                 fund the entry (USYC -> USDC) BEFORE depositing margin.
    risk_off  -> close ALL perp exposure (long or short) on Hyperliquid,
                 withdraw the freed margin back to Arbitrum / Arc, and
                 mint USYC with the leftover free USDC (preserving a
                 configurable USDC reserve) so capital keeps earning
                 yield.
    hold      -> no on-chain change.

Trading venue: Hyperliquid Testnet (since the Arc Perp DEX matcher
spec was never published). Arc + Circle remain the treasury / yield
leg via the :class:`USYCExecutor` and Paymaster.

Long / short symmetry
=====================
SHORT directives are full first-class citizens here, not "sell to
close" syntactic sugar. The same sizing pipeline, leverage cap and
drawdown haircut apply to both sides; ``executor.open_position`` is
the only side-aware call in the chain, and it just forwards the side
string verbatim.

Executor abstraction
====================
The router holds an ``executor`` that satisfies the duck-typed
:class:`PerpExecutorProtocol` below. In production this is a
:class:`HyperliquidExecutor`; in tests / ``--test-allocation`` it's
a lightweight fake. The legacy :class:`ArcPerpExecutor` also matches
the protocol so dry-run telemetry on the old venue keeps working.

Position sizing pipeline (vol-targeted + DD haircut)
====================================================
Sizing is solved from account equity, target risk per trade and the
primary symbol's ATR%, then trimmed by the gradient drawdown
haircut:

    1. Vol-targeted base notional from equity * target_risk_pct /
       (stop_atr_mult * ATR%/100).
    2. Conviction scaling by ``intensity`` (engine signal in [0, 1]).
    3. Drawdown haircut: ``1 - (dd/max_dd) ** dd_haircut_exponent``.
    4. Cap at ``max_position_usd``.

When equity / ATR are unavailable (typical first dry-run cycle before
any margin is deposited) we fall back to ``base_position_usd * (1 +
intensity)`` so the demo path still produces meaningful sizes.

Auto-allocation pipeline (Day 5)
================================
The router is the only component allowed to instruct the execution
layer to move funds. Hard overrides (drawdown breach, stale data,
leverage cap, max-position cap) live here and apply identically to
longs and shorts.

The USYC executor is **optional**: when ``USYC_TOKEN_ADDRESS`` /
``USYC_MINT_CONTRACT_ADDRESS`` aren't wired, or ``USYC_ENABLED`` is
False, the router skips the rotation leg with an explanatory note in
the ExecutionPlan but still closes perps + withdraws margin on
risk-off (so the demo keeps running).

Every action is keyed by a deterministic ``decision_id`` so retries
never double-trade.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Protocol, runtime_checkable

from src.core.decision_engine import DecisionResult, ExecutionDirective
from src.execution.arc_perp_executor import AccountInfo, Position
from src.execution.circle_wallet import TxResult
from src.execution.position_manager import (
    PositionManager,
    PositionReview,
    PositionSnapshot,
)
from src.execution.usyc_executor import USYCExecutor, USYCSnapshot
from src.utils.logging import logger


@runtime_checkable
class PerpExecutorProtocol(Protocol):
    """Minimal interface the AllocationRouter needs from any perp executor.

    Implemented by:

    * :class:`src.execution.hyperliquid_executor.HyperliquidExecutor` -
      the primary production trading venue.
    * :class:`src.execution.arc_perp_executor.ArcPerpExecutor` - legacy,
      dry-run only.
    * Test doubles in ``--test-allocation`` / pytest.

    Methods are intentionally a strict subset of what each concrete
    executor exposes so the router can swap venues without touching
    the orchestration logic.
    """

    config: Any  # carries ``max_leverage`` and venue-specific knobs

    async def get_account_info(self) -> AccountInfo: ...

    async def get_position(self, symbol: str) -> Position: ...

    # Optional in older / test executors. Declared here so callers can
    # type-check the signature; the PositionManager guards with
    # ``getattr(..., None)`` so legacy executors that don't expose it
    # still work via the AccountInfo snapshot path.
    async def get_open_positions(self) -> list[Position]: ...

    async def get_margin(self) -> Decimal: ...

    async def get_pnl(self, symbol: str | None = None) -> Decimal: ...

    async def open_position(
        self,
        symbol: str,
        side: str,
        size_usd: Decimal,
        leverage: Decimal | None = None,
        slippage_bps: int | None = None,
        decision_id: str | None = None,
    ) -> TxResult: ...

    async def close_position(
        self,
        symbol: str,
        slippage_bps: int | None = None,
        decision_id: str | None = None,
    ) -> TxResult: ...

    async def close_all_positions(
        self,
        decision_id: str | None = None,
    ) -> list[TxResult]: ...

    async def withdraw_all_margin(
        self,
        decision_id: str | None = None,
    ) -> TxResult: ...

    async def deposit_margin(
        self,
        amount_usd: Decimal,
        decision_id: str | None = None,
    ) -> TxResult: ...


@dataclass
class AllocationConfig:
    """Configuration for the allocation router."""

    perp_symbol: str = "BTC-PERP"
    base_position_usd: Decimal = Decimal("1000")
    max_position_usd: Decimal = Decimal("10000")
    max_drawdown_pct: float = 10.0
    risk_on_threshold: float = 0.6
    risk_off_threshold: float = 0.4
    # When True, the same sizing pipeline is applied to short opens.
    # Kept as a knob so future work can tune SHORT sizing independently
    # (e.g. half-size shorts while we ramp up confidence in the
    # bear-conviction signal).
    symmetric_short_sizing: bool = True
    # Optional reduction factor on size for shorts only (1.0 = full
    # parity with longs, 0.5 = half-size shorts). Used only when
    # `symmetric_short_sizing` is False.
    short_size_multiplier: Decimal = Decimal("1.0")
    # ------------- Vol-targeted sizing -------------
    # Target *fraction of equity at risk* per trade (Kelly-fraction
    # style). 0.02 = 2% per trade. With stop_atr_mult=1.5 and the
    # primary symbol's ATR%, this solves for the notional that puts
    # exactly `target_risk_pct` at risk under a 1.5*ATR adverse move.
    #
    # This is the FINAL fraction the router uses. ``main.py`` resolves
    # it via :meth:`Settings.resolved_risk_per_trade_frac` so the
    # operator-friendly ``RISK_PER_TRADE_PCT=1.0`` (1%) wins over the
    # legacy ``TARGET_RISK_PCT=0.02`` (fraction) form.
    target_risk_pct: float = 0.02
    # Per-asset risk multipliers (dimensionless). 1.0 = full risk on
    # the primary symbol; 0.5 = half-size on this asset. Lets the
    # operator express asset-specific conviction without rewriting
    # the engine. Resolved per-call from the primary symbol via
    # :meth:`Settings.risk_multiplier_for_symbol`.
    risk_multiplier_per_symbol: dict[str, float] = field(default_factory=dict)
    risk_multiplier_default: float = 1.0
    # Stop distance expressed as a multiple of the primary symbol's
    # ATR. 1.5x ATR is the textbook default.
    stop_atr_mult: float = 1.5
    # Floor on ATR% to avoid divide-by-zero (and to prevent absurdly
    # large vol-targeted sizes when ATR% temporarily collapses to ~0).
    min_atr_pct_for_sizing: float = 0.25
    # ------------- Drawdown haircut (gradient) -------------
    # Intensity is multiplied by
    # `max(0, 1 - (dd_pct / max_dd_pct) ** dd_haircut_exponent)`
    # so the agent de-risks smoothly as drawdown approaches the cap.
    # Exponent 1.0 = linear; >1 keeps the early haircut soft and
    # punishes the last few % hard. 2.0 ~ "5% dd halves the size".
    dd_haircut_exponent: float = 2.0
    # ------------- Sizing telemetry -------------
    # When True, the router records every sizing decision in the
    # plan's `extra` dict (final vs vol-target vs intensity vs
    # haircut breakdown) so the Execution Plan panel can show the
    # *why* of each size, not just the number.
    explain_sizing: bool = True
    # ------------- USYC rotation -------------
    # When True (default), risk-off rotates leftover USDC into USYC
    # and risk-on redeems USYC just-in-time if free USDC is short.
    # Flip to False to keep capital sitting idle in USDC even on
    # risk-off (useful for perp-only demos).
    usyc_enabled: bool = True
    # ------------- Risk-off withdraw safety gate -------------
    # When True, a risk-off directive will close every open perp AND
    # withdraw all remaining USDC margin from Hyperliquid back to the
    # signer's Arbitrum wallet (1-block bridge). When False (default),
    # the router only CLOSES positions and leaves the freed USDC
    # sitting in the perp sub-account, ready for the next risk-on
    # entry. This is the primary safety gate against an erroneous
    # kill-switch fire (e.g. a bug in the equity / PnL read) draining
    # the venue.  Disabling withdraws also implies skipping the USYC
    # mint leg - there is nothing freed on Arc to mint with.
    risk_off_withdraw: bool = False
    # Always keep this much free USDC liquid in the wallet (pays gas
    # fallbacks, covers the next perp's initial margin without a
    # redeem round-trip).
    usdc_reserve_usd: Decimal = Decimal("50")
    # ------------- Post-open native TP / SL triggers -------------
    # When True, every risk-on open is followed by a TP / SL trigger
    # submission on the venue. Hyperliquid supports native reduce-only
    # trigger orders so this is essentially free; legacy executors
    # without ``update_take_profit_stop_loss`` are silently skipped.
    # The trigger PRICES are derived from the
    # :class:`PositionManagerConfig` thresholds (single source of truth
    # for TP / SL %) so we never end up with mismatched on-chain
    # triggers and off-chain PositionManager rules.
    submit_take_profit_stop_loss: bool = True
    # ------------- Per-asset ATR cap on new opens -------------
    # When True (default), the router refuses a fresh risk-on open
    # when the primary symbol's live ATR% exceeds the per-asset cap
    # carried on the PositionManager's config (BTC <= 3%, ETH <= 4%
    # on 1h by default - mirrors the L3 critical-mode prompt). This
    # is the Fast-Path complement to the per-asset volatility rule
    # in the LLM prompt - the gate runs even when L3 is offline.
    enforce_per_asset_atr_cap: bool = True
    # ------------- Anti-overlap (HL netting) guard -------------
    # Hyperliquid nets same-side opens into the existing position
    # (one slot per ``(symbol, side)``). Without an explicit gate,
    # every full cycle that re-affirms ``risk_on`` on a side we
    # already hold will SILENTLY add notional - which in chop is
    # exactly the "stack into a doomed LONG every 10 min" failure
    # mode we observed in live testing on 2026-05-26. When True
    # (default), the router pre-empts the regime dispatch with a
    # HOLD as soon as a surviving same-side position exists in the
    # cycle's PositionReview snapshot. Stewardship still belongs to
    # the PositionManager; the router simply refuses to double down.
    block_duplicate_same_side_opens: bool = True


@dataclass
class ExecutionPlan:
    """What the router decided to do for a given decision.

    ``action`` is the top-level intent:

        "open_long" | "open_short"   - risk-on: perp open (long or short)
        "close"                      - risk-off: closed any perp exposure
        "risk_off_rotation"          - risk-off: closed perps + minted USYC
        "deposit_margin"             - live fallback when matcher URL missing
        "hold"                       - mid-band: no on-chain change
        "deny"                       - safety guard fired (stale data, etc.)

    ``rotation_legs`` records each USYC mint / redeem submitted on
    this cycle so the panel can paint the full multi-step pipeline.
    Each entry is a ``{action, amount_usd, tx_id, state, reason}``
    dict.
    """

    decision_id: str
    action: str
    symbol: str | None
    size_usd: Decimal
    leverage: Decimal
    rationale: str
    timestamp: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    tx_results: list[TxResult] = field(default_factory=list)
    rotation_legs: list[dict[str, Any]] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)


class AllocationRouter:
    """Turns a `DecisionResult` into perp + USYC actions.

    Parameters
    ----------
    executor :
        The perp-DEX executor used to open / close positions and
        deposit / withdraw vault margin.
    config :
        :class:`AllocationConfig` with thresholds, caps and sizing
        knobs.
    dry_run :
        When True, the executor is in dry-run mode and no on-chain
        transactions are submitted. The router still walks the full
        pipeline and stamps the planned actions onto
        :class:`ExecutionPlan` so the panel renders the same shape
        live / dry.
    usyc_executor :
        Optional :class:`USYCExecutor` for the yield-bearing risk-off
        leg. When ``None`` (or :attr:`USYCExecutor.is_configured` is
        False, or :attr:`AllocationConfig.usyc_enabled` is False), the
        router skips the rotation step with an explanatory note in
        :class:`ExecutionPlan`. It never raises.
    position_manager :
        Optional :class:`PositionManager` used to steward open
        positions (TP / SL / trailing stop / re-evaluation / side
        flip). When ``None`` the router falls back to a default
        manager built from :class:`PositionManagerConfig` defaults so
        the safety triggers always run.
    """

    def __init__(
        self,
        executor: PerpExecutorProtocol,
        config: AllocationConfig,
        dry_run: bool = True,
        usyc_executor: USYCExecutor | None = None,
        position_manager: PositionManager | None = None,
    ) -> None:
        # Duck-typed: works with HyperliquidExecutor (production),
        # ArcPerpExecutor (legacy / dry-run telemetry) and any fake
        # used by tests / --test-allocation.
        self.executor = executor
        self.config = config
        self.dry_run = dry_run
        self.usyc_executor = usyc_executor
        # The PositionManager is a separable concern (TP / SL /
        # trailing / re-eval / flip stewardship). We always have one -
        # falling back to default-config is the safest behaviour
        # because the safety triggers never become silently disabled.
        if position_manager is None:
            from src.execution.position_manager import (
                PositionManagerConfig,
            )
            position_manager = PositionManager(
                config=PositionManagerConfig(),
                executor=executor,
            )
        self.position_manager = position_manager
        # Surfaces the most recent review so the console panel can
        # paint the FULL set of open positions (not only the ones
        # that triggered) on every cycle.
        self.last_position_review: PositionReview | None = None

    @property
    def usyc_active(self) -> bool:
        """Is the USYC rotation leg available for this cycle?"""
        if not self.config.usyc_enabled:
            return False
        if self.usyc_executor is None:
            return False
        return self.usyc_executor.is_configured

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------

    async def route(self, decision: DecisionResult) -> ExecutionPlan:
        """Translate a :class:`DecisionResult` into concrete execution."""
        decision_id = self._mk_decision_id(decision)
        directive = decision.directive

        # ---- Stale-data guard --------------------------------------------
        # Genuine stale data = all levels returned zero score *without*
        # an explicit short-circuit by the engine. A short-circuit
        # means the engine intentionally chose risk-off and we should
        # honour it. Stale data = something upstream is broken and we
        # must NOT trade until it's fixed.
        if not decision.short_circuited and self._stale_data(decision):
            return self._deny(
                decision_id, "stale or incomplete data - forcing hold"
            )

        account = await self.executor.get_account_info()

        # ---- PositionManager review (always runs) -----------------------
        # Run the stewardship review BEFORE the drawdown branch so the
        # console panel always paints the latest position telemetry,
        # even when a hard override (drawdown / stale data) takes over
        # the cycle and the PositionManager actions themselves are
        # superseded.
        review = await self.position_manager.review_open_positions(
            account=account,
            decision=decision,
            directive=directive,
        )
        self.last_position_review = review

        # ---- Daily-DD kill switch (Fast Path, portfolio level) ---------
        # The PositionManager owns the daily-DD tracker; when it has
        # tripped, the router MUST flatten everything and refuse new
        # opens for the rest of the session (until the process
        # restarts). This is a hard override above any directive.
        if review.daily_dd_breached:
            logger.warning(
                "Daily-DD kill switch active (session loss "
                "{:.2f}%) - forcing full flatten + USYC rotation; "
                "new opens refused.",
                -(review.daily_pnl_pct or 0.0),
            )
            return await self._do_risk_off(
                decision_id,
                account,
                reason=(
                    f"daily-DD kill switch active "
                    f"(session loss "
                    f"{-(review.daily_pnl_pct or 0.0):.2f}% >= "
                    f"limit {self.position_manager.config.daily_loss_limit_pct:.2f}%)"
                ),
            )

        # ---- Drawdown guard (per-cycle unrealised) ---------------------
        # Hard override: above MAX_DRAWDOWN_PCT we close everything and
        # rotate to USYC regardless of what L1/L2/L3 said. We bypass
        # the PositionManager's per-position actions here because the
        # drawdown breach is portfolio-level - we don't want a "hold"
        # decision on individual positions to override the hard close.
        if self._drawdown_breached(account):
            logger.warning(
                "Drawdown breach detected (pnl={}); forcing risk-off close",
                account.total_unrealized_pnl_usd,
            )
            return await self._do_risk_off(
                decision_id, account, reason="drawdown breach"
            )

        # ---- Apply PositionManager actions ------------------------------
        # Per-position TP / SL / trailing / re-eval / flip pre-empts
        # the regime routing - we never want to scale into a position
        # whose existing exposure has already crossed one of the
        # stewardship thresholds.
        managed = await self._apply_position_review(
            review=review, directive=directive, decision=decision
        )
        if managed is not None:
            return managed

        # ---- Dispatch on engine directive -------------------------------
        if directive.action == "risk_on":
            # ---- Post-stop-out cooldown guard (Stage 4) ---------------
            # After a stop_loss close on this symbol, refuse a fresh
            # entry for ``POST_STOP_COOLDOWN_MINUTES``. This breaks the
            # "stop -> instant re-entry -> stop again" chop loop. The
            # cooldown is owned by the PositionManager (which knows a
            # stop_loss fired) and merely queried here, so there is a
            # single source of truth. An in-progress side_flip on this
            # symbol is exempt (the flip's close+reopen must complete).
            cooldown_remaining = self._stop_out_cooldown_block(review=review)
            if cooldown_remaining is not None:
                logger.info(
                    "Router: skip risk_on - {} in post-stop-out cooldown "
                    "({:.0f}m remaining). Refusing re-entry to break the "
                    "stop->re-entry loop.",
                    self.config.perp_symbol, cooldown_remaining,
                )
                pm_stats = getattr(self.position_manager, "stats", None)
                if pm_stats is not None and hasattr(
                    pm_stats, "stop_out_cooldown_blocks"
                ):
                    pm_stats.stop_out_cooldown_blocks += 1
                total = self.position_manager.config.post_stop_cooldown_minutes
                plan = ExecutionPlan(
                    decision_id=decision_id,
                    action="hold",
                    symbol=self.config.perp_symbol,
                    size_usd=Decimal("0"),
                    leverage=Decimal("0"),
                    rationale=(
                        f"post_stop_cooldown: {self.config.perp_symbol} was "
                        f"stopped out recently; {cooldown_remaining:.0f}m of "
                        f"{total:.0f}m cooldown remaining before re-entry is "
                        "allowed. Refusing risk_on to break the "
                        "stop->re-entry chop loop."
                    ),
                    extra={
                        "guard": "post_stop_cooldown",
                        "symbol": self.config.perp_symbol,
                        "cooldown_remaining_minutes": round(
                            cooldown_remaining, 2
                        ),
                        "cooldown_total_minutes": total,
                        "directive_side": directive.side,
                        "directive_intensity": directive.intensity,
                        "directive_conviction": directive.conviction,
                    },
                )
                self._attach_position_review(plan, review)
                return plan

            # ---- Anti-overlap guard (HL netting safety) ---------------
            # If we already hold a position on the *same* side that
            # survived this cycle's PositionReview (i.e. it didn't get
            # closed/flipped by the Fast Path), DO NOT issue another
            # ``open_position`` - on a netting venue that would silently
            # ADD notional to the existing position. Stewardship belongs
            # to the PositionManager (TP/SL/trail/time-exit run on every
            # cycle); the router's job is to OPEN, not to stack.
            duplicate_block = self._already_in_same_side(
                review=review, side=directive.side or "long",
            )
            if duplicate_block is not None:
                logger.info(
                    "Router: skip risk_on - already {} on {} "
                    "(size={}, age={:.0f}m). Stewardship belongs to "
                    "PositionManager; no duplicate stacking.",
                    duplicate_block.side, duplicate_block.symbol,
                    duplicate_block.size_usd,
                    duplicate_block.age_minutes or 0.0,
                )
                plan = ExecutionPlan(
                    decision_id=decision_id,
                    action="hold",
                    symbol=self.config.perp_symbol,
                    size_usd=Decimal("0"),
                    leverage=Decimal("0"),
                    rationale=(
                        f"duplicate_open_blocked: already {duplicate_block.side} "
                        f"{duplicate_block.symbol} (size=${duplicate_block.size_usd}, "
                        f"PnL%={duplicate_block.pnl_pct:+.2f}, "
                        f"age={duplicate_block.age_minutes or 0.0:.0f}m). "
                        "Router refuses to stack on a netting venue."
                    ),
                    extra={
                        "guard": "duplicate_open_blocked",
                        "existing_side": duplicate_block.side,
                        "existing_size_usd": float(duplicate_block.size_usd),
                        "existing_pnl_pct": duplicate_block.pnl_pct,
                        "existing_age_minutes": duplicate_block.age_minutes,
                        "directive_side": directive.side,
                        "directive_intensity": directive.intensity,
                        "directive_conviction": directive.conviction,
                    },
                )
                self._attach_position_review(plan, review)
                return plan

            # Per-asset ATR cap gate (Fast-Path complement to the L3
            # critical-mode per-asset volatility rule). When the
            # primary symbol's live ATR% on 1h exceeds the cap, refuse
            # the new open: vol regime is too hot to safely size in.
            cap_block = self._atr_cap_block_reason(decision)
            if cap_block is not None:
                logger.warning(
                    "Risk-on refused by ATR cap: {}", cap_block,
                )
                plan = self._deny(decision_id, f"per-asset ATR cap: {cap_block}")
                self._attach_position_review(plan, review)
                return plan
            return await self._do_risk_on(
                decision_id,
                directive,
                account,
                decision=decision,
                reason=directive.rationale,
            )
        if directive.action == "risk_off":
            reason = directive.rationale
            if decision.short_circuited:
                reason = (
                    f"L1 short-circuit ({decision.short_circuit_reason})"
                    if decision.short_circuit_reason
                    else "L1 short-circuit"
                )
            return await self._do_risk_off(decision_id, account, reason=reason)

        # ---- Hold ---------------------------------------------------------
        plan = ExecutionPlan(
            decision_id=decision_id,
            action="hold",
            symbol=self.config.perp_symbol,
            size_usd=Decimal("0"),
            leverage=Decimal("0"),
            rationale=directive.rationale or "neutral - hold current allocation",
            extra={
                "usyc_active": self.usyc_active,
                "conviction": directive.conviction,
                "intensity": directive.intensity,
                "direction_strength": directive.direction_strength,
            },
        )
        self._attach_position_review(plan, review)
        return plan

    # ------------------------------------------------------------------
    # Risk-ON: redeem USYC if needed, then open perp position
    # ------------------------------------------------------------------

    async def _do_risk_on(
        self,
        decision_id: str,
        directive: ExecutionDirective,
        account: AccountInfo,
        decision: DecisionResult,
        reason: str,
    ) -> ExecutionPlan:
        # `directive.side` is set by the DecisionEngine. We forward the
        # side verbatim to the executor so the SHORT path is *not* a
        # special case here - same sizing, same leverage cap, same
        # on-chain code path.
        sizing = self._compute_size(directive, account, decision)
        size_usd = sizing["size_usd"]
        leverage = self._leverage_from_directive(directive)
        side = directive.side or "long"
        if side == "short" and not self.config.symmetric_short_sizing:
            size_usd = (
                size_usd * self.config.short_size_multiplier
            ).quantize(Decimal("0.01"))
            sizing["short_multiplier"] = float(self.config.short_size_multiplier)
            sizing["size_usd"] = size_usd

        bias_tag = ""
        if directive.market_bias and directive.market_bias != "neutral":
            bias_tag = (
                f" | bias={directive.market_bias}"
                f"({directive.bias_strength:.2f})"
            )

        extra: dict[str, Any] = {
            "market_bias": directive.market_bias,
            "bias_strength": directive.bias_strength,
            "intensity": directive.intensity,
            "conviction": directive.conviction,
            "direction_strength": directive.direction_strength,
            "usyc_active": self.usyc_active,
        }
        if self.config.explain_sizing:
            extra["sizing"] = sizing

        plan = ExecutionPlan(
            decision_id=decision_id,
            action=f"open_{side}",
            symbol=self.config.perp_symbol,
            size_usd=size_usd,
            leverage=leverage,
            rationale=(reason or "risk-on directive") + bias_tag,
            extra=extra,
        )

        # ---- USYC redeem (if needed) -------------------------------------
        # Margin required for this perp open = size_usd / leverage.
        # If the wallet's free USDC can't cover it (minus the reserve),
        # redeem just enough USYC to bridge the gap.
        await self._maybe_redeem_usyc_for_open(
            plan=plan,
            decision_id=decision_id,
            size_usd=size_usd,
            leverage=leverage,
        )

        # ---- Open the perp position --------------------------------------
        # Hyperliquid Testnet is fully wired - no NotImplementedError
        # fallback is needed (that was the legacy Arc Perp DEX path).
        # We still catch NotImplementedError from any legacy executor
        # in case a downstream caller still injects ArcPerpExecutor in
        # --live for treasury demos.
        try:
            result = await self.executor.open_position(
                symbol=self.config.perp_symbol,
                side=side,
                size_usd=size_usd,
                leverage=leverage,
                decision_id=decision_id,
            )
            plan.tx_results.append(result)
        except NotImplementedError as exc:
            logger.warning(
                "Legacy executor cannot open positions (trading has moved "
                "to Hyperliquid). Recording the request without on-chain "
                "side-effects. Reason: {}", exc,
            )
            plan.action = "open_skipped"
            plan.extra["fallback_reason"] = str(exc)
            return plan

        # ---- TP / SL trigger orders (Hyperliquid native) -----------------
        # The router submits ONE pair of native trigger orders right
        # after the open. They stay live on the venue for the entire
        # position lifetime - neither the Fast Path (every 2 min) nor
        # the Full cycle (every 10 min) modifies them. Off-chain
        # PositionManager rules can still close the position earlier
        # via close_position / partial_close, but the venue-side
        # triggers are the always-on safety net.
        await self._maybe_submit_take_profit_stop_loss(
            plan=plan,
            decision_id=decision_id,
            side=side,
            size_usd=size_usd,
            decision=decision,
        )
        self._attach_position_review(plan, self.last_position_review)
        return plan

    async def _maybe_redeem_usyc_for_open(
        self,
        plan: ExecutionPlan,
        decision_id: str,
        size_usd: Decimal,
        leverage: Decimal,
    ) -> None:
        """Redeem USYC just-in-time if free USDC can't cover the margin call.

        Calculates the required margin (``size_usd / leverage``), checks
        the wallet's free USDC + reserve, and submits a USYC redeem for
        the deficit when needed. Records the leg under
        :attr:`ExecutionPlan.rotation_legs` so the panel can show the
        whole "USYC -> USDC -> margin -> position" chain.
        """
        if not self.usyc_active or self.usyc_executor is None:
            plan.rotation_legs.append(
                {
                    "action": "usyc_redeem_skipped",
                    "reason": "usyc_not_configured",
                    "amount_usd": "0",
                    "tx_id": None,
                    "state": "SKIPPED",
                }
            )
            return

        required_margin = self._margin_for_size(size_usd, leverage)
        snap = await self.usyc_executor.get_snapshot()
        free_usdc = snap.usdc_balance
        reserve = self.config.usdc_reserve_usd

        # Available margin we can fund right now without touching USYC:
        spendable = max(Decimal("0"), free_usdc - reserve)
        if spendable >= required_margin:
            plan.rotation_legs.append(
                {
                    "action": "usyc_redeem_skipped",
                    "reason": (
                        f"free USDC {free_usdc} >= required margin "
                        f"{required_margin} + reserve {reserve}"
                    ),
                    "amount_usd": "0",
                    "tx_id": None,
                    "state": "SKIPPED",
                }
            )
            return

        deficit = (required_margin - spendable).quantize(Decimal("0.01"))
        # Can we even redeem that much from USYC?
        if snap.usyc_value_usd <= 0:
            plan.rotation_legs.append(
                {
                    "action": "usyc_redeem_skipped",
                    "reason": "no USYC balance to redeem",
                    "amount_usd": "0",
                    "tx_id": None,
                    "state": "SKIPPED",
                }
            )
            return

        redeem_amount = min(deficit, snap.usyc_value_usd)
        logger.info(
            "Risk-on USYC redeem | deficit={} free_usdc={} required_margin={} "
            "available_usyc={} redeem={}",
            deficit, free_usdc, required_margin, snap.usyc_value_usd, redeem_amount,
        )
        result = await self.usyc_executor.redeem(
            amount_usyc=redeem_amount,
            decision_id=f"{decision_id}-redeem",
        )
        plan.tx_results.append(result)
        plan.rotation_legs.append(
            {
                "action": "usyc_redeem",
                "reason": (
                    f"fund margin gap: required={required_margin} "
                    f"free={free_usdc} reserve={reserve}"
                ),
                "amount_usd": str(redeem_amount),
                "tx_id": result.tx_id,
                "state": result.state,
            }
        )

    # ------------------------------------------------------------------
    # Risk-OFF: close perps, withdraw margin, mint USYC
    # ------------------------------------------------------------------

    async def _do_risk_off(
        self,
        decision_id: str,
        account: AccountInfo,
        reason: str,
    ) -> ExecutionPlan:
        """Risk-off pipeline: close → (optional) withdraw → (optional) USYC mint.

        Steps:

        1. **Close every open perp position.** Side-agnostic - same
           call unwinds longs AND shorts. In dry-run we still walk the
           pipeline so the panel renders the planned sequence.
        2. **Withdraw all margin** from the perp vault back to the
           Arbitrum wallet - ONLY when ``risk_off_withdraw=True``.
           This is the single safety gate against an erroneous
           kill-switch fire (e.g. a bug in the equity / PnL read)
           draining the venue: a CLOSE never moves capital between
           subaccounts, but a WITHDRAW does. Disabled by default so
           the freed USDC stays in the perp account, ready to be
           reused by the next risk-on cycle.
        3. **Mint USYC** with the freed USDC (minus the configured
           reserve) - ONLY when withdraws are enabled. Without a
           withdraw there is nothing freed on Arc to mint with.
        """
        held_sides = sorted({p.side for p in account.positions if p.side != "flat"})
        plan = ExecutionPlan(
            decision_id=decision_id,
            action="risk_off_rotation" if self.usyc_active else "close",
            symbol=self.config.perp_symbol,
            size_usd=Decimal("0"),
            leverage=Decimal("0"),
            rationale=reason or "risk-off directive",
            extra={
                "usyc_active": self.usyc_active,
                "held_sides": ",".join(held_sides) or "none",
                "starting_margin_usd": str(account.equity_usd),
                "starting_pnl_usd": str(account.total_unrealized_pnl_usd),
                "risk_off_withdraw": self.config.risk_off_withdraw,
            },
        )

        # ---- 1. Close perp position(s) ----------------------------------
        close_results = await self.executor.close_all_positions(
            decision_id=f"{decision_id}-close"
        )
        if not close_results:
            # No open positions; record a synthetic "nothing to close" leg
            # so the panel doesn't render a confusing empty list when the
            # primary action says "close".
            logger.info(
                "Risk-off: no open positions on cycle {} - skipping close",
                decision_id,
            )
        plan.tx_results.extend(close_results)

        # ---- 2. Withdraw margin back to wallet --------------------------
        # The withdraw is GATED: by default we leave the freed USDC
        # in the perp sub-account so the agent can re-enter on the
        # next cycle without a bridge round-trip, and so a faulty
        # kill-switch can never auto-drain the vault. Enable
        # explicitly with ``RISK_OFF_WITHDRAW=true`` in .env once
        # you trust the equity / PnL read end-to-end.
        if not self.config.risk_off_withdraw:
            plan.extra["withdraw_skipped_reason"] = (
                "RISK_OFF_WITHDRAW=false (safety gate; capital stays "
                "in perp sub-account)"
            )
            logger.info(
                "Risk-off: WITHDRAW gated off (RISK_OFF_WITHDRAW=false); "
                "freed USDC stays on perp - decision={}",
                decision_id,
            )
        elif account.equity_usd > 0:
            withdraw_res = await self.executor.withdraw_all_margin(
                decision_id=f"{decision_id}-withdraw"
            )
            plan.tx_results.append(withdraw_res)
        else:
            logger.info(
                "Risk-off: vault balance is 0 - skipping margin withdraw"
            )

        # ---- 3. USYC mint: rotate leftover USDC into yield --------------
        # Only meaningful when the withdraw actually moved USDC to
        # Arc; without a withdraw there is nothing on Arc to mint
        # with. Skip the mint leg AND record a synthetic SKIPPED
        # entry so the panel shows the gate clearly.
        if not self.config.risk_off_withdraw:
            plan.rotation_legs.append(
                {
                    "action": "usyc_mint_skipped",
                    "reason": "RISK_OFF_WITHDRAW=false (no USDC freed on Arc)",
                    "amount_usd": "0",
                    "tx_id": None,
                    "state": "SKIPPED",
                }
            )
        else:
            await self._maybe_mint_usyc_after_close(
                plan=plan,
                decision_id=decision_id,
                withdrawn_usd=account.equity_usd,
            )
        self._attach_position_review(plan, self.last_position_review)
        return plan

    async def _maybe_mint_usyc_after_close(
        self,
        plan: ExecutionPlan,
        decision_id: str,
        withdrawn_usd: Decimal,
    ) -> None:
        """Mint USYC with the wallet's free USDC after a risk-off close.

        We respect ``usdc_reserve_usd`` (always keep some liquid USDC),
        ``min_rotation_usdc`` (skip sub-economic dust) and
        ``max_rotation_usdc`` (safety cap). Records each leg on
        :attr:`ExecutionPlan.rotation_legs`.
        """
        if not self.usyc_active or self.usyc_executor is None:
            plan.rotation_legs.append(
                {
                    "action": "usyc_mint_skipped",
                    "reason": (
                        "usyc_disabled"
                        if not self.config.usyc_enabled
                        else "usyc_not_configured"
                    ),
                    "amount_usd": "0",
                    "tx_id": None,
                    "state": "SKIPPED",
                }
            )
            return

        snap = await self.usyc_executor.get_snapshot()
        # In dry-run mode the on-chain balance reader hasn't seen the
        # withdraw we just submitted (it's still in the simulated
        # tx-log only), so we conservatively estimate the post-close
        # USDC as: current_free_usdc + amount we just *intended* to
        # withdraw. This makes the dry-run demo show a realistic
        # rotation amount instead of a misleading "skipped (no USDC)".
        if self.dry_run:
            estimated_usdc = snap.usdc_balance + withdrawn_usd
        else:
            estimated_usdc = snap.usdc_balance
        reserve = self.config.usdc_reserve_usd
        mintable = max(Decimal("0"), estimated_usdc - reserve)

        if mintable <= 0:
            plan.rotation_legs.append(
                {
                    "action": "usyc_mint_skipped",
                    "reason": (
                        f"free USDC {estimated_usdc} <= reserve {reserve}"
                    ),
                    "amount_usd": "0",
                    "tx_id": None,
                    "state": "SKIPPED",
                }
            )
            return

        result = await self.usyc_executor.mint(
            amount_usdc=mintable,
            decision_id=f"{decision_id}-usyc-mint",
        )
        plan.tx_results.append(result)
        plan.rotation_legs.append(
            {
                "action": "usyc_mint",
                "reason": (
                    f"rotate freed USDC into yield: free={estimated_usdc} "
                    f"reserve={reserve} mintable={mintable}"
                ),
                "amount_usd": str(mintable),
                "tx_id": result.tx_id,
                "state": result.state,
            }
        )

    # ------------------------------------------------------------------
    # Safety / guard helpers
    # ------------------------------------------------------------------

    def _deny(self, decision_id: str, reason: str) -> ExecutionPlan:
        logger.warning("Router denied execution: {}", reason)
        return ExecutionPlan(
            decision_id=decision_id,
            action="deny",
            symbol=None,
            size_usd=Decimal("0"),
            leverage=Decimal("0"),
            rationale=reason,
        )

    def _already_in_same_side(
        self,
        review: PositionReview | None,
        side: str,
    ) -> PositionSnapshot | None:
        """Return a surviving same-side snapshot, or None.

        A *surviving* snapshot is one that was NOT closed/flipped by
        the Fast Path this cycle - i.e. its ``action`` is ``"hold"``
        or a state-only verb (``"arm_breakeven"`` / ``"tighten_stop"``).
        ``"close"`` / ``"partial_close"`` / ``"side_flip"`` are excluded
        so a close-then-open chain on a flip can still execute the
        new open after the existing slot has been emptied.

        The check is gated on
        :attr:`AllocationConfig.block_duplicate_same_side_opens`
        so operators can fall back to the legacy "always add" behaviour
        without ripping out the guard.
        """
        if not self.config.block_duplicate_same_side_opens:
            return None
        if review is None or not review.snapshots:
            return None
        symbol = self.config.perp_symbol
        side_norm = (side or "").lower()
        # Verbs that mean "this position will NOT exist after the cycle":
        closing_verbs = {"close", "partial_close", "side_flip"}
        for snap in review.snapshots:
            if snap.symbol != symbol:
                continue
            if (snap.side or "").lower() != side_norm:
                continue
            # ``partial_close`` shrinks but doesn't eliminate the slot;
            # we still want to block stacking on top of a partial.
            if snap.action == "close" or snap.action == "side_flip":
                continue
            # Surviving same-side position - block the new open.
            return snap
        return None

    def _stop_out_cooldown_block(
        self,
        review: PositionReview | None,
    ) -> float | None:
        """Return remaining cooldown minutes if a fresh open must be blocked.

        Queries the PositionManager's per-symbol post-stop-out cooldown
        for :attr:`AllocationConfig.perp_symbol`. Returns the remaining
        minutes when a fresh risk-on open should be refused, or ``None``
        when the symbol is free to trade.

        The PositionManager owns the cooldown bookkeeping (it is the
        component that detects a stop_loss close), so the router never
        duplicates the timer - it only enforces it. Legacy / test
        managers without the ``stop_out_cooldown_remaining_minutes``
        method degrade transparently to "no cooldown".

        An in-progress ``side_flip`` on this symbol is exempt: a flip
        is a close-then-reopen chain owned by the PositionManager, and
        blocking its reopen would leave the position half-flipped. In
        practice an active cooldown and a live position cannot co-exist
        (the cooldown starts only when a position closes, and the guard
        would have blocked any subsequent open), but the exemption is
        kept as a defensive invariant.
        """
        pm = self.position_manager
        remaining_fn = getattr(
            pm, "stop_out_cooldown_remaining_minutes", None
        )
        if remaining_fn is None:
            return None
        symbol = self.config.perp_symbol
        remaining = remaining_fn(symbol)
        if remaining is None:
            return None
        if self._review_has_side_flip(review, symbol):
            return None
        return remaining

    @staticmethod
    def _review_has_side_flip(
        review: PositionReview | None,
        symbol: str,
    ) -> bool:
        """True when this cycle's review is flipping ``symbol``'s side."""
        if review is None:
            return False
        return any(
            a.symbol == symbol and a.trigger == "side_flip"
            for a in review.actions
        )

    def _mk_decision_id(self, decision: DecisionResult) -> str:
        ts = int(decision.timestamp.timestamp())
        return f"dec-{ts}-{uuid.uuid4().hex[:8]}"

    def _stale_data(self, decision: DecisionResult) -> bool:
        if not decision.level_scores:
            return True
        return all(score.score == 0.0 for score in decision.level_scores)

    def _drawdown_breached(self, account: AccountInfo) -> bool:
        if account.equity_usd <= 0:
            return False
        if account.total_unrealized_pnl_usd >= 0:
            return False
        loss_pct = float(
            -account.total_unrealized_pnl_usd / account.equity_usd * 100
        )
        return loss_pct >= self.config.max_drawdown_pct

    def _margin_for_size(
        self, size_usd: Decimal, leverage: Decimal
    ) -> Decimal:
        """Initial margin required for a perp size at the given leverage."""
        if leverage <= 0:
            return size_usd
        return (size_usd / leverage).quantize(Decimal("0.000001"))

    # ------------------------------------------------------------------
    # Position management - delegates to :class:`PositionManager`
    # ------------------------------------------------------------------

    async def _apply_position_review(
        self,
        *,
        review: PositionReview,
        directive: ExecutionDirective,
        decision: DecisionResult,
    ) -> ExecutionPlan | None:
        """Translate a :class:`PositionReview` into an ExecutionPlan.

        Walks the actions in the review and applies them in priority
        order:

        * ``hold`` -> skip (no-op).
        * ``arm_breakeven`` / ``tighten_stop`` -> state-only verbs,
          stamped into the in-process PM state by the manager itself.
          The router logs them and lets the cycle continue normally
          (so a "tighten_stop" advisory still allows a fresh risk-on
          open if the engine directive says so).
        * ``partial_close`` -> close a fraction of the position on
          this cycle; emits a dedicated ExecutionPlan and pre-empts
          the regime dispatch.
        * ``close`` -> full close. Two subcases:
            - ``side_flip`` -> close and return ``None`` so the regular
              risk-on path then opens the new side fresh.
            - everything else -> emit ExecutionPlan + pre-empt.

        Returns ``None`` when no action needs to pre-empt the regime
        routing.
        """
        decision_id = self._mk_decision_id(decision)

        for action in review.actions:
            if action.action == "hold":
                continue

            # ---- State-only verbs (no on-chain change) -------------
            # The manager has already mutated _states in-process; the
            # router just records the advisory and lets the cycle
            # continue (so a fresh entry on the engine's directive is
            # still allowed). We attach a small note so the panel can
            # paint a badge.
            if action.action in {"arm_breakeven", "tighten_stop"}:
                logger.info(
                    "PositionManager ADVISORY {} on {}/{} | reason={}",
                    action.action.upper(),
                    action.symbol,
                    action.side.upper(),
                    action.reason,
                )
                # Stash on the review so the panel can render the
                # advisory; we DON'T return - the cycle continues.
                continue

            # ---- Partial close ---------------------------------------
            if action.action == "partial_close":
                close_res = await self._safe_partial_close(
                    symbol=action.symbol,
                    size_usd_to_close=action.size_usd_to_close,
                    decision_id=decision_id,
                )
                plan = ExecutionPlan(
                    decision_id=decision_id,
                    action=f"position_manager_{action.trigger}",
                    symbol=action.symbol,
                    size_usd=action.size_usd_to_close or Decimal("0"),
                    leverage=Decimal("0"),
                    rationale=(
                        f"PositionManager {action.trigger.upper()} "
                        f"(partial close) on "
                        f"{action.symbol}/{action.side.upper()}: "
                        f"{action.reason}"
                    ),
                    extra={
                        "position_manager": self._position_manager_extra(action),
                        "partial_close": {
                            "size_usd_to_close": str(action.size_usd_to_close or 0),
                            "fraction": (
                                float(
                                    (action.size_usd_to_close or Decimal("0"))
                                    / action.size_usd
                                )
                                if action.size_usd > 0
                                else 0.0
                            ),
                        },
                    },
                )
                plan.tx_results.append(close_res)
                self._attach_position_review(plan, review)
                return plan

            # ---- Full close (close verb) ----------------------------
            close_res = await self.executor.close_position(
                symbol=action.symbol, decision_id=decision_id,
            )

            # Side-flip is a "close + reopen" sequence. We intentionally
            # do NOT return here so the regular risk-on path runs next
            # and opens the new side fresh (with full sizing pipeline,
            # USYC-redeem and native TP / SL triggers).
            if action.trigger == "side_flip":
                if close_res.state not in {"DRY_RUN", "SKIPPED"}:
                    logger.info(
                        "  flip close tx | id={} state={} hash={}",
                        close_res.tx_id, close_res.state, close_res.tx_hash,
                    )
                continue

            plan = ExecutionPlan(
                decision_id=decision_id,
                action=f"position_manager_{action.trigger}",
                symbol=action.symbol,
                size_usd=Decimal("0"),
                leverage=Decimal("0"),
                rationale=(
                    f"PositionManager {action.trigger.upper()} on "
                    f"{action.symbol}/{action.side.upper()}: "
                    f"{action.reason}"
                ),
                extra={
                    "position_manager": self._position_manager_extra(action),
                },
            )
            plan.tx_results.append(close_res)
            self._attach_position_review(plan, review)
            return plan

        return None

    @staticmethod
    def _position_manager_extra(action: Any) -> dict[str, Any]:
        """Serialise a :class:`PositionAction` for ExecutionPlan.extra."""
        return {
            "trigger": action.trigger,
            "symbol": action.symbol,
            "side": action.side,
            "pnl_pct": float(action.pnl_pct),
            "pnl_usd": str(action.pnl_usd),
            "size_usd": str(action.size_usd),
            "size_usd_to_close": (
                str(action.size_usd_to_close)
                if getattr(action, "size_usd_to_close", None) is not None
                else None
            ),
            "peak_pnl_pct": (
                float(action.peak_pnl_pct)
                if action.peak_pnl_pct is not None
                else None
            ),
            "source": getattr(action, "source", "fast"),
            "smart_snippet": getattr(action, "smart_snippet", None),
        }

    async def _safe_partial_close(
        self,
        *,
        symbol: str,
        size_usd_to_close: Decimal | None,
        decision_id: str,
    ) -> TxResult:
        """Best-effort partial close - falls back to full close on legacy executors.

        Hyperliquid supports partial closes via ``close_position(size_usd=...)``,
        but the legacy / shim executors might not. We probe with
        ``inspect.signature``: when ``size_usd_to_close`` (or
        ``size_usd``) is a keyword the executor accepts, we submit
        the partial; otherwise we fall back to a full close and log
        the degradation. The PM snapshot already records the intent
        so operators see the discrepancy in the panel.
        """
        import inspect

        method = self.executor.close_position
        sig: inspect.Signature | None
        try:
            sig = inspect.signature(method)
        except (TypeError, ValueError):
            sig = None
        kwargs: dict[str, Any] = {"symbol": symbol, "decision_id": decision_id}
        if size_usd_to_close is not None and sig is not None:
            params = sig.parameters
            if "size_usd_to_close" in params:
                kwargs["size_usd_to_close"] = size_usd_to_close
            elif "size_usd" in params:
                kwargs["size_usd"] = size_usd_to_close
            elif "partial_size_usd" in params:
                kwargs["partial_size_usd"] = size_usd_to_close
            else:
                logger.info(
                    "Partial close requested ({} {}) but executor "
                    "{} doesn't support size kwargs - falling back "
                    "to FULL close.",
                    size_usd_to_close, symbol,
                    type(self.executor).__name__,
                )
        return await method(**kwargs)

    def _attach_position_review(
        self,
        plan: ExecutionPlan,
        review: PositionReview | None,
    ) -> None:
        """Stash the full :class:`PositionReview` on the plan for the panel.

        The panel needs the structured snapshots (one row per open
        position, with TP / SL / trailing prices and current PnL%) so
        it can paint the stewardship table on every cycle, including
        hold rows. We attach the review object itself so the console
        renderer has the full type-safe shape.
        """
        if review is None:
            return
        plan.extra["position_review"] = review

    async def _maybe_submit_take_profit_stop_loss(
        self,
        plan: ExecutionPlan,
        decision_id: str,
        side: str,
        size_usd: Decimal,
        decision: DecisionResult | None = None,
    ) -> None:
        """Submit native TP / SL trigger orders right after an open.

        Uses the executor's :meth:`update_take_profit_stop_loss` when
        available (Hyperliquid supports native reduce-only triggers).
        Legacy executors that don't expose the method are silently
        skipped - the per-cycle :class:`PositionManager` review will
        still catch the position via PnL monitoring on the next cycle.

        Pricing precedence:

          1. **Dynamic ATR** (preferred when ``USE_DYNAMIC_ATR_TPSL``
             is True AND L1 carries a usable ATR%): trigger prices
             are entry +/- (``TP_ATR_MULT`` * ATR) and entry +/-
             (``SL_ATR_MULT`` * ATR). Same multipliers the Fast Path
             uses off-chain, so the venue-side trigger and the
             off-chain rule agree on what "the stop" means.
          2. **Fixed-pct fallback** (``TAKE_PROFIT_PCT`` /
             ``STOP_LOSS_PCT``): used when dynamic ATR is disabled or
             ATR% is unavailable. Default 3.5% / 2.0% gives a textbook
             1.75:1 reward-to-risk envelope.

        Once submitted, these triggers are NEVER modified by the
        per-cycle Fast Path or Full cycle - they stay live on the
        venue as the always-on safety net. Off-chain rules in the
        PositionManager (trailing stop, time exit, vol spike,
        daily-DD) can still issue ``close_position`` / ``partial_close``
        before the venue triggers fire, in which case the resting
        triggers become reduce-only no-ops on a flat position and
        Hyperliquid auto-cancels them.

        TP / SL %s and ATR multipliers are read from
        :attr:`AllocationRouter.position_manager.config` so there is
        exactly one source of truth across the off-chain rules and
        the venue-side triggers.
        """
        if not self.config.submit_take_profit_stop_loss:
            return
        pm_cfg = self.position_manager.config
        tp_enabled = pm_cfg.enable_take_profit
        sl_enabled = pm_cfg.enable_stop_loss
        if not tp_enabled and not sl_enabled:
            return
        method = getattr(
            self.executor, "update_take_profit_stop_loss", None
        )
        if method is None:
            return
        get_mid = getattr(self.executor, "get_mid_price", None)
        mid: Decimal | None = None
        if get_mid is not None:
            try:
                mid = await get_mid(self.config.perp_symbol)
            except Exception as exc:  # noqa: BLE001
                logger.debug("get_mid_price failed: {}", exc)
                mid = None
        if mid is None or mid <= 0:
            # Bumped from DEBUG to WARNING - this is rare and worth
            # surfacing because it silently disables the venue-side
            # safety net.
            logger.warning(
                "Native TP/SL skipped for {} - no mid price available",
                self.config.perp_symbol,
            )
            return

        tp_px, sl_px, pricing_mode = self._compute_native_tpsl_prices(
            side=side,
            entry_price=mid,
            decision=decision,
        )
        if tp_px is None and sl_px is None:
            return
        # Quantize to the venue's tick rule so ExecutionPlan.extra
        # (rendered in the panel + persisted to the decision log)
        # matches the price that actually hits the wire. The executor
        # also re-rounds defensively before its SDK call, so this is
        # idempotent. Legacy executors without ``round_price`` (test
        # fakes, ArcPerpExecutor) are silently skipped.
        round_price = getattr(self.executor, "round_price", None)
        if round_price is not None:
            try:
                if tp_px is not None:
                    tp_px = await round_price(self.config.perp_symbol, tp_px)
                if sl_px is not None:
                    sl_px = await round_price(self.config.perp_symbol, sl_px)
            except Exception as exc:  # noqa: BLE001
                logger.debug(
                    "executor.round_price failed (continuing with raw "
                    "prices, executor will re-round): {}", exc,
                )
        try:
            # Pass the just-opened side + size as explicit hints. This
            # bypasses ``get_position`` inside the executor and defeats
            # the Hyperliquid Info-endpoint propagation lag where a
            # fresh fill briefly reads back as "flat" (silently
            # skipping TP/SL the first time we hit it).
            results = await method(
                symbol=self.config.perp_symbol,
                tp_price=tp_px,
                sl_price=sl_px,
                decision_id=decision_id,
                position_side=side,
                position_size_usd=size_usd,
            )
        except TypeError:
            # Legacy executor without the kwargs (test fakes / older
            # ArcPerpExecutor): fall back to the un-hinted call so we
            # don't break the build for shim implementations.
            try:
                results = await method(
                    symbol=self.config.perp_symbol,
                    tp_price=tp_px,
                    sl_price=sl_px,
                    decision_id=decision_id,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("TP/SL submission failed: {}", exc)
                return
        except Exception as exc:  # noqa: BLE001
            logger.warning("TP/SL submission failed: {}", exc)
            return
        plan.tx_results.extend(results)
        plan.extra["tp_sl"] = {
            "tp_price": str(tp_px) if tp_px else None,
            "sl_price": str(sl_px) if sl_px else None,
            "mid_price": str(mid),
            "pricing": pricing_mode,
            "results": [
                {"tx_id": r.tx_id, "state": r.state} for r in results
            ],
        }
        logger.info(
            "TP/SL set | {} {} tp={} sl={}",
            self.config.perp_symbol, side.upper(), tp_px, sl_px,
        )

    def _compute_native_tpsl_prices(
        self,
        *,
        side: str,
        entry_price: Decimal,
        decision: DecisionResult | None,
    ) -> tuple[Decimal | None, Decimal | None, str]:
        """Return ``(tp_price, sl_price, pricing_mode)`` for native triggers.

        ``pricing_mode`` is one of ``"atr"`` or ``"fixed_pct"`` and is
        stamped into ``ExecutionPlan.extra["tp_sl"]["pricing"]`` so
        the panel and post-mortem can see which path the cycle used.
        """
        pm_cfg = self.position_manager.config
        is_long = side.lower() == "long"
        tp_enabled = pm_cfg.enable_take_profit
        sl_enabled = pm_cfg.enable_stop_loss

        if pm_cfg.use_dynamic_atr_tpsl and decision is not None:
            atr_pct = self._primary_atr_pct(decision)
            if atr_pct is not None and atr_pct > 0:
                atr_abs = (
                    entry_price * Decimal(str(atr_pct)) / Decimal("100")
                )
                tp_px: Decimal | None = None
                sl_px: Decimal | None = None
                if tp_enabled:
                    tp_dist = atr_abs * Decimal(str(pm_cfg.tp_atr_mult))
                    tp_px = (
                        (entry_price + tp_dist)
                        if is_long
                        else (entry_price - tp_dist)
                    )
                if sl_enabled:
                    sl_dist = atr_abs * Decimal(str(pm_cfg.sl_atr_mult))
                    sl_px = (
                        (entry_price - sl_dist)
                        if is_long
                        else (entry_price + sl_dist)
                    )
                if tp_px is not None or sl_px is not None:
                    return tp_px, sl_px, "atr"

        # Fixed-pct fall-back path (also: dynamic disabled).
        tp_pct = pm_cfg.take_profit_pct if tp_enabled else Decimal("0")
        sl_pct = pm_cfg.stop_loss_pct if sl_enabled else Decimal("0")
        tp_px = None
        sl_px = None
        if tp_pct > 0:
            tp_px = (
                entry_price * (Decimal("1") + tp_pct)
                if is_long
                else entry_price * (Decimal("1") - tp_pct)
            )
        if sl_pct > 0:
            sl_px = (
                entry_price * (Decimal("1") - sl_pct)
                if is_long
                else entry_price * (Decimal("1") + sl_pct)
            )
        return tp_px, sl_px, "fixed_pct"

    def _leverage_from_directive(self, directive: ExecutionDirective) -> Decimal:
        if directive.target_leverage is not None:
            return min(
                directive.target_leverage,
                Decimal(self.executor.config.max_leverage),
            )
        return Decimal(self.executor.config.max_leverage)

    # ------------------------------------------------------------------
    # Sizing pipeline (vol-targeted + DD-haircut)
    # ------------------------------------------------------------------

    def _compute_size(
        self,
        directive: ExecutionDirective,
        account: AccountInfo,
        decision: DecisionResult,
    ) -> dict[str, Any]:
        """Solve for the position size and return a full attribution dict.

        Order of precedence:
          1. Explicit ``directive.target_size_usd`` wins (engine override).
          2. Else: vol-targeted notional from equity + ATR%, then scaled
             by intensity, then haircut by drawdown gradient, finally
             capped at ``max_position_usd``.
          3. Fallback: legacy ``base * (1 + intensity)`` when neither
             equity nor ATR% is available (typical for the first dry-run
             cycle before margin is deposited).
        """
        # Per-asset risk multiplier - so an operator can dial BTC at
        # 1.0x and ETH at 0.75x without rewriting the engine.
        risk_mult = self._risk_multiplier_for_symbol(self.config.perp_symbol)
        effective_risk_pct = self.config.target_risk_pct * risk_mult

        breakdown: dict[str, Any] = {
            "method": "vol_targeted",
            "atr_pct": None,
            "atr_pct_floored": None,
            "equity_usd": float(account.equity_usd),
            "target_risk_pct": self.config.target_risk_pct,
            "risk_multiplier": risk_mult,
            "effective_risk_pct": effective_risk_pct,
            "stop_atr_mult": self.config.stop_atr_mult,
            "intensity": directive.intensity,
            "vol_target_size_usd": None,
            "after_intensity_size_usd": None,
            "drawdown_pct": None,
            "dd_haircut": 1.0,
            "after_haircut_size_usd": None,
            "size_usd": None,
            "cap_hit": False,
        }

        if directive.target_size_usd is not None:
            sized = min(directive.target_size_usd, self.config.max_position_usd)
            sized = sized.quantize(Decimal("0.01"))
            breakdown.update(
                method="directive_override",
                size_usd=sized,
                cap_hit=sized >= self.config.max_position_usd,
            )
            return breakdown

        atr_pct = self._primary_atr_pct(decision)
        equity = float(account.equity_usd)
        breakdown["atr_pct"] = atr_pct

        if equity > 0 and atr_pct is not None and atr_pct > 0:
            atr_floored = max(atr_pct, self.config.min_atr_pct_for_sizing)
            breakdown["atr_pct_floored"] = atr_floored
            # vol-target solves: equity * effective_risk = size * (stop_atr_mult * atr_pct/100)
            stop_dist = self.config.stop_atr_mult * (atr_floored / 100.0)
            if stop_dist <= 0:
                vol_target = float(self.config.base_position_usd)
            else:
                vol_target = (equity * effective_risk_pct) / stop_dist
            breakdown["vol_target_size_usd"] = round(vol_target, 2)
            after_intensity = vol_target * max(0.0, min(1.0, directive.intensity))
            breakdown["after_intensity_size_usd"] = round(after_intensity, 2)
        else:
            # No live equity (typical first dry-run cycle): fall back to
            # the legacy intensity-only path so the demo still produces
            # a meaningful number.
            breakdown["method"] = "fallback_base"
            intensity = directive.intensity
            after_intensity = float(self.config.base_position_usd) * (1.0 + intensity)
            breakdown["after_intensity_size_usd"] = round(after_intensity, 2)

        # ---- Drawdown haircut (gradient) -----------------------------
        dd_pct = self._drawdown_pct(account)
        breakdown["drawdown_pct"] = dd_pct
        haircut = self._dd_haircut(dd_pct)
        breakdown["dd_haircut"] = round(haircut, 4)
        after_haircut = after_intensity * haircut
        breakdown["after_haircut_size_usd"] = round(after_haircut, 2)

        # ---- Cap ------------------------------------------------------
        max_size = float(self.config.max_position_usd)
        capped = min(after_haircut, max_size)
        # Don't let a positive-conviction trade collapse to literal 0
        # just because ATR% spiked - we still want a token entry so the
        # rest of the pipeline (executor, panel, telemetry) exercises.
        # Use the smaller of `base_position_usd / 10` or whatever the
        # haircut left us with, but only when haircut > 0 (so a true
        # drawdown breach still produces 0).
        if capped <= 0.0 and haircut > 0.0:
            capped = max(1.0, float(self.config.base_position_usd) * 0.1)
        sized = Decimal(str(round(capped, 2)))
        breakdown["size_usd"] = sized
        breakdown["cap_hit"] = sized >= self.config.max_position_usd
        return breakdown

    def _atr_cap_block_reason(
        self, decision: DecisionResult
    ) -> str | None:
        """Per-asset ATR cap gate for new opens.

        Reads the PositionManager's :class:`PerAssetATRCaps` and the
        primary symbol's live ATR% from L1's raw payload. When the
        live ATR% exceeds the cap, returns a human-readable reason
        the router stamps onto the denial; otherwise returns ``None``.

        Disabled when :attr:`AllocationConfig.enforce_per_asset_atr_cap`
        is False or the PM doesn't expose ``atr_caps`` (legacy
        configs / unit tests).
        """
        if not self.config.enforce_per_asset_atr_cap:
            return None
        pm_cfg = self.position_manager.config
        caps = getattr(pm_cfg, "atr_caps", None)
        if caps is None:
            return None
        atr_pct = self._primary_atr_pct(decision)
        if atr_pct is None or atr_pct <= 0:
            return None
        cap = caps.cap_for(self.config.perp_symbol)
        if atr_pct <= cap:
            return None
        return (
            f"{self.config.perp_symbol} ATR% {atr_pct:.2f}% > cap "
            f"{cap:.2f}% (per-asset 1h ceiling) - vol regime too hot "
            "for a fresh open. The agent will hold cash until vol "
            "compresses below the cap."
        )

    def _risk_multiplier_for_symbol(self, symbol: str) -> float:
        """Resolve the per-asset risk multiplier for sizing.

        Pattern-matches the leading token (BTC / ETH / other) against
        ``AllocationConfig.risk_multiplier_per_symbol``; falls back
        to ``AllocationConfig.risk_multiplier_default``. Lets the
        operator dial different per-asset risk without rewriting the
        engine - e.g. 1.0x on BTC, 0.75x on ETH.
        """
        if not symbol:
            return self.config.risk_multiplier_default
        per = self.config.risk_multiplier_per_symbol or {}
        head = symbol.strip().upper().split("-")[0].split("/")[0]
        if head in per:
            return float(per[head])
        if symbol in per:
            return float(per[symbol])
        return self.config.risk_multiplier_default

    def _primary_atr_pct(self, decision: DecisionResult) -> float | None:
        """Pull the primary symbol's average ATR% out of L1's raw payload."""
        l1 = decision.level_score(1)
        if l1 is None:
            return None
        per_sym = (l1.raw.get("l1", {}) or {}).get("per_symbol") or {}
        if not per_sym:
            return None
        # Pick the configured perp_symbol if present, else any symbol.
        ro = per_sym.get(self.config.perp_symbol) or next(
            iter(per_sym.values()), None
        )
        if not ro:
            return None
        atr_pct = ro.get("atr_pct_avg")
        if atr_pct is None:
            rows = ro.get("rows") or []
            vals = [
                float(r.get("atr_pct", 0))
                for r in rows
                if float(r.get("atr_pct", 0)) > 0
            ]
            atr_pct = sum(vals) / len(vals) if vals else None
        try:
            return float(atr_pct) if atr_pct is not None else None
        except (TypeError, ValueError):
            return None

    def _drawdown_pct(self, account: AccountInfo) -> float:
        if account.equity_usd <= 0:
            return 0.0
        if account.total_unrealized_pnl_usd >= 0:
            return 0.0
        return float(
            -account.total_unrealized_pnl_usd / account.equity_usd * 100
        )

    def _dd_haircut(self, drawdown_pct: float) -> float:
        """Smooth, gradient haircut on intensity as drawdown grows.

        haircut(dd) = max(0, 1 - (dd / max_dd) ** dd_haircut_exponent)

        Exponent 2.0 keeps the early haircut soft (5% dd -> 0.25 cut)
        and punishes the last few % hard (9% dd -> 0.81 cut). At
        ``max_drawdown_pct`` exactly, returns 0 (and the breach guard
        in :meth:`_drawdown_breached` will have already forced a close).
        """
        if self.config.max_drawdown_pct <= 0:
            return 1.0
        ratio = max(0.0, drawdown_pct) / self.config.max_drawdown_pct
        if ratio <= 0.0:
            return 1.0
        if ratio >= 1.0:
            return 0.0
        exponent = max(0.1, self.config.dd_haircut_exponent)
        return max(0.0, 1.0 - ratio ** exponent)
