"""Allocation router for CapitalArc.

Consumes a `DecisionResult` produced by the `DecisionEngine` and converts
it into concrete capital moves on Arc:

    risk_on  -> open / scale a perp position on Arc Perp DEX
    risk_off -> close any perp exposure, rotate USDC into USYC (Day 3)
    hold     -> no on-chain change

The router enforces hard overrides (drawdown, stale data) on top of the
engine's recommendation.
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

        if self._stale_data(decision):
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
                decision_id, directive, account, reason=directive.rationale
            )
        if directive.action == "risk_off":
            return await self._do_close(
                decision_id, account, reason=directive.rationale
            )
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
        reason: str,
    ) -> ExecutionPlan:
        size_usd = self._size_from_directive(directive)
        leverage = self._leverage_from_directive(directive)
        side = directive.side or "long"

        plan = ExecutionPlan(
            decision_id=decision_id,
            action=f"open_{side}",
            symbol=self.config.perp_symbol,
            size_usd=size_usd,
            leverage=leverage,
            rationale=reason or "risk-on directive",
        )
        result = await self.executor.open_position(
            symbol=self.config.perp_symbol,
            side=side,
            size_usd=size_usd,
            leverage=leverage,
            decision_id=decision_id,
        )
        plan.tx_results.append(result)
        return plan

    async def _do_close(
        self,
        decision_id: str,
        account: AccountInfo,
        reason: str,
    ) -> ExecutionPlan:
        plan = ExecutionPlan(
            decision_id=decision_id,
            action="close",
            symbol=self.config.perp_symbol,
            size_usd=Decimal("0"),
            leverage=Decimal("0"),
            rationale=reason or "risk-off directive",
            extra={"usyc_rotation": "deferred_to_day_3"},
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
        if directive.target_size_usd is not None:
            return min(directive.target_size_usd, self.config.max_position_usd)
        # Default: scale base size by the directive's intensity (final_score).
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
