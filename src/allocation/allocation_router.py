"""Allocation router for CapitalArc.

Consumes a `DecisionResult` produced by the `DecisionEngine` and converts
it into concrete capital moves on Arc:

    risk_on  -> open / scale a perp position on Arc Perp DEX
                (side comes from `directive.side`: "long" or "short")
    risk_off -> close ALL perp exposure (long or short), rotate USDC
                into USYC (Day 3)
    hold     -> no on-chain change

Long / short symmetry
---------------------
The router treats SHORT directives as full first-class citizens, not
"sell to close" syntactic sugar. The same sizing pipeline, leverage cap
and drawdown haircut apply to both sides.

Position sizing pipeline (Day 3.5)
----------------------------------
Sizing is no longer "base * (1 + intensity)" - that was a flat-USD
allocation that ignored *how volatile the market is* and *how close to
the drawdown limit we are*. The new pipeline:

    1. **Vol-targeted base size.** Using the primary symbol's ATR%
       from Level 1 we solve for the notional that puts a fixed
       fraction of equity at risk per trade:

           size = (equity * target_risk_pct) / (stop_atr_mult * atr_pct/100)

       So if equity=$1k, target_risk=2%, stop=1.5*ATR, ATR%=2% ->
       size = $1000 * 0.02 / (1.5 * 0.02) = $667. ATR% doubles ->
       size halves. This is the vol-targeting recipe used by every
       systematic CTA shop in the world.

       If equity is unknown (dry-run with zero margin) we fall back
       to `base_position_usd * (1 + intensity)` so the demo path keeps
       producing meaningful sizes.

    2. **Conviction scaling.** Multiply by `intensity` (already in
       [0, 1] from the engine), so a mid-band override entry is
       smaller than a full risk-on entry.

    3. **Drawdown haircut.** Multiply by
       `(1 - (drawdown_pct / max_drawdown_pct) ** dd_haircut_exponent)`.
       Gradient, not cliff: 5% drawdown trims ~50% of the size,
       9% trims ~90%, 10% = hard stop (see `_drawdown_breached`).
       Replaces the legacy "all systems normal up to 9.99%, then
       slammed flat at 10%" behaviour.

    4. **Cap at `max_position_usd`.**

The router enforces hard overrides (drawdown breach, stale data) on
top of the engine's recommendation - these apply identically to longs
and shorts.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from src.core.decision_engine import DecisionResult, ExecutionDirective
from src.execution.arc_perp_executor import ArcPerpExecutor, AccountInfo
from src.execution.circle_wallet import TxResult
from src.utils.logging import logger


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
    target_risk_pct: float = 0.02
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


@dataclass
class ExecutionPlan:
    """What the router decided to do for a given decision."""

    decision_id: str
    action: str  # "open_long" | "open_short" | "close" | "hold" | "deny"
    symbol: str | None
    size_usd: Decimal
    leverage: Decimal
    rationale: str
    timestamp: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    tx_results: list[TxResult] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)


class AllocationRouter:
    """Turns a `DecisionResult` into perp / yield actions.

    Day 2 covers the perp leg end-to-end (open / close via `ArcPerpExecutor`).
    The USYC rotation leg lands on Day 3 once the USYC mint contract is
    wired.
    """

    def __init__(
        self,
        executor: ArcPerpExecutor,
        config: AllocationConfig,
        dry_run: bool = True,
    ) -> None:
        self.executor = executor
        self.config = config
        self.dry_run = dry_run

    async def route(self, decision: DecisionResult) -> ExecutionPlan:
        """Translate a `DecisionResult` into concrete execution."""
        decision_id = self._mk_decision_id(decision)
        directive = decision.directive

        # Genuine stale data = all levels returned zero score *without*
        # an explicit short-circuit by the engine. A short-circuit means
        # the engine intentionally chose risk-off and we should honour it.
        if not decision.short_circuited and self._stale_data(decision):
            return self._deny(
                decision_id, "stale or incomplete data - forcing hold"
            )

        account = await self.executor.get_account_info()
        if self._drawdown_breached(account):
            logger.warning(
                "Drawdown breach detected (pnl={}); forcing risk-off close",
                account.total_unrealized_pnl_usd,
            )
            return await self._do_close(decision_id, account, reason="drawdown")

        if directive.action == "risk_on":
            return await self._do_open(
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
            return await self._do_close(decision_id, account, reason=reason)
        return ExecutionPlan(
            decision_id=decision_id,
            action="hold",
            symbol=self.config.perp_symbol,
            size_usd=Decimal("0"),
            leverage=Decimal("0"),
            rationale=directive.rationale or "neutral - hold current allocation",
        )

    # ------------------------------------------------------------------
    # Concrete actions
    # ------------------------------------------------------------------

    async def _do_open(
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

        # In live mode, full perp open requires matcher URL (Day 3). If it's
        # not configured, gracefully fall back to just allocating margin into
        # the perp vault so capital is on-venue and ready for Day-3 trading.
        executor_matcher = getattr(self.executor.config, "matcher_url", None)
        live = not self.dry_run

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
            if not live or executor_matcher:
                raise
            logger.warning(
                "Perp open not yet wired (matcher URL missing); "
                "falling back to margin deposit. Reason: {}",
                exc,
            )
            margin = max(Decimal("1"), size_usd / max(leverage, Decimal("1")))
            margin = margin.quantize(Decimal("0.000001"))
            plan.action = "deposit_margin"
            plan.size_usd = margin
            plan.extra["fallback_reason"] = str(exc)
            deposit_res = await self.executor.deposit_margin(
                amount_usd=margin, decision_id=decision_id
            )
            plan.tx_results.append(deposit_res)
        return plan

    async def _do_close(
        self,
        decision_id: str,
        account: AccountInfo,
        reason: str,
    ) -> ExecutionPlan:
        # close_position on the executor is side-agnostic: it submits
        # the opposite-side market order for whatever the PositionLedger
        # currently shows, so a single call unwinds longs AND shorts.
        # We tag the plan with which side(s) we currently hold (best-
        # effort - the AccountInfo snapshot doesn't always carry the
        # full position list, that's a Day-3+ wiring task on the
        # executor) for the Execution Plan panel.
        held_sides = sorted({p.side for p in account.positions if p.side != "flat"})
        plan = ExecutionPlan(
            decision_id=decision_id,
            action="close",
            symbol=self.config.perp_symbol,
            size_usd=Decimal("0"),
            leverage=Decimal("0"),
            rationale=reason or "risk-off directive",
            extra={
                "usyc_rotation": "deferred_to_day_3",
                "held_sides": ",".join(held_sides) or "unknown",
            },
        )
        result = await self.executor.close_position(
            symbol=self.config.perp_symbol,
            decision_id=decision_id,
        )
        plan.tx_results.append(result)
        return plan

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

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

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

    def _size_from_directive(self, directive: ExecutionDirective) -> Decimal:
        """Legacy intensity-only sizing path (kept for back-compat)."""
        if directive.target_size_usd is not None:
            return min(directive.target_size_usd, self.config.max_position_usd)
        intensity = Decimal(str(directive.intensity))
        size = self.config.base_position_usd * (Decimal("1") + intensity)
        return min(size, self.config.max_position_usd)

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
          1. Explicit `directive.target_size_usd` wins (engine override).
          2. Else: vol-targeted notional from equity + ATR%, then scaled
             by intensity, then haircut by drawdown gradient, finally
             capped at `max_position_usd`.
          3. Fallback: legacy `base * (1 + intensity)` when neither
             equity nor ATR% is available (typical for the first dry-run
             cycle before margin is deposited).
        """
        breakdown: dict[str, Any] = {
            "method": "vol_targeted",
            "atr_pct": None,
            "atr_pct_floored": None,
            "equity_usd": float(account.equity_usd),
            "target_risk_pct": self.config.target_risk_pct,
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
            # vol-target solves: equity * target_risk = size * (stop_atr_mult * atr_pct/100)
            stop_dist = self.config.stop_atr_mult * (atr_floored / 100.0)
            if stop_dist <= 0:
                vol_target = float(self.config.base_position_usd)
            else:
                vol_target = (equity * self.config.target_risk_pct) / stop_dist
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
        `max_drawdown_pct` exactly, returns 0 (and the breach guard
        in `_drawdown_breached` will have already forced a close).
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
