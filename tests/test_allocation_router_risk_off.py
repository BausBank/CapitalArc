"""Tests for the ``RISK_OFF_WITHDRAW`` safety gate.

The risk-off pipeline used to ALWAYS execute the full
``close -> withdraw -> mint USYC`` sequence, which made a single bad
equity / PnL read enough to auto-drain the venue
(``withdraw_from_bridge``).  The gate splits the pipeline into two
distinct steps:

    * **close positions** - always runs (cheap, reversible);
    * **withdraw margin** - only when ``risk_off_withdraw=True`` (capital
      movement; the actual safety-relevant action).

Default is ``False`` so a buggy kill-switch can never silently bridge
USDC back to Arbitrum.  The USYC mint leg is implicitly skipped when
withdraws are gated off (there is nothing on Arc to mint with).
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from src.allocation.allocation_router import (
    AllocationConfig,
    AllocationRouter,
)
from src.core.decision_engine import (
    DecisionResult,
    ExecutionDirective,
    LevelScore,
)
from src.execution.arc_perp_executor import AccountInfo, Position
from src.execution.circle_wallet import TxResult


class _FakePerpExecutor:
    """Records every call so we can assert which legs ran on a risk-off."""

    def __init__(self, *, equity: Decimal = Decimal("900")) -> None:
        self.equity = equity
        self.config = type("Cfg", (), {"max_leverage": Decimal("5")})()
        self.close_calls: list[str] = []
        self.withdraw_calls: list[str] = []

    async def get_account_info(self) -> AccountInfo:
        return AccountInfo(
            equity_usd=self.equity,
            free_margin_usd=self.equity,
            used_margin_usd=Decimal("0"),
            total_unrealized_pnl_usd=Decimal("0"),
            positions=[],
        )

    async def get_position(self, symbol: str) -> Position:
        return Position(
            symbol=symbol,
            side="flat",
            size_usd=Decimal("0"),
            entry_price=Decimal("0"),
            mark_price=Decimal("0"),
            leverage=Decimal("0"),
            unrealized_pnl_usd=Decimal("0"),
        )

    async def get_open_positions(self) -> list[Position]:
        return []

    async def get_margin(self) -> Decimal:
        return self.equity

    async def get_pnl(self, symbol: str | None = None) -> Decimal:
        return Decimal("0")

    async def get_mid_price(self, symbol: str) -> Decimal | None:
        return Decimal("60000")

    async def open_position(self, *args, **kwargs) -> TxResult:
        raise AssertionError("open_position must not be called on risk_off")

    async def close_position(self, symbol: str, **kwargs) -> TxResult:
        self.close_calls.append(symbol)
        return TxResult(tx_id=f"close-{symbol}", state="CONFIRMED", raw={})

    async def close_all_positions(self, decision_id: str | None = None) -> list[TxResult]:
        self.close_calls.append(decision_id or "all")
        return []  # no positions to close

    async def withdraw_all_margin(
        self, decision_id: str | None = None
    ) -> TxResult:
        self.withdraw_calls.append(decision_id or "default")
        return TxResult(
            tx_id=f"hl-withdraw-{decision_id or 'na'}",
            state="CONFIRMED",
            raw={"action": "withdraw_all_margin"},
        )

    async def deposit_margin(self, *args, **kwargs) -> TxResult:
        return TxResult(tx_id="noop", state="SKIPPED", raw={})


def _risk_off_decision() -> DecisionResult:
    return DecisionResult(
        final_score=0.1,
        regime="risk-off",
        directive=ExecutionDirective(
            action="risk_off",
            side=None,
            intensity=1.0,
            rationale="test risk-off",
        ),
        level_scores=[
            LevelScore(level=1, score=0.1, rationale="L1"),
            LevelScore(level=2, score=0.1, rationale="L2"),
            LevelScore(level=3, score=0.1, rationale="L3"),
        ],
        weights={"L1": 0.25, "L2": 0.35, "L3": 0.40},
        effective_weights={"L1": 0.25, "L2": 0.35, "L3": 0.40},
        timestamp=datetime.now(timezone.utc),
    )


@pytest.mark.asyncio
async def test_risk_off_withdraw_false_skips_withdraw_leg() -> None:
    """Default RISK_OFF_WITHDRAW=false: close runs, withdraw does NOT."""
    executor = _FakePerpExecutor(equity=Decimal("900"))
    router = AllocationRouter(
        executor=executor,
        config=AllocationConfig(
            usyc_enabled=False,
            risk_off_withdraw=False,
        ),
        dry_run=False,
    )
    plan = await router.route(_risk_off_decision())
    # Close was attempted (returned [] because no positions)
    assert executor.close_calls, "close_all_positions must always run on risk_off"
    # WITHDRAW was NOT called - the safety gate held.
    assert executor.withdraw_calls == [], (
        "withdraw_all_margin must NOT be called when "
        "RISK_OFF_WITHDRAW=false"
    )
    # Plan reports the gated state for operator visibility.
    assert plan.extra.get("risk_off_withdraw") is False
    assert "RISK_OFF_WITHDRAW=false" in plan.extra.get(
        "withdraw_skipped_reason", ""
    )
    # USYC mint leg also skipped (nothing on Arc to mint with).
    assert any(
        leg.get("action") == "usyc_mint_skipped"
        and "RISK_OFF_WITHDRAW=false" in str(leg.get("reason", ""))
        for leg in plan.rotation_legs
    )


@pytest.mark.asyncio
async def test_risk_off_withdraw_true_executes_withdraw_leg() -> None:
    """Operator-enabled gate: close + withdraw both run."""
    executor = _FakePerpExecutor(equity=Decimal("900"))
    router = AllocationRouter(
        executor=executor,
        config=AllocationConfig(
            usyc_enabled=False,
            risk_off_withdraw=True,
        ),
        dry_run=False,
    )
    plan = await router.route(_risk_off_decision())
    assert executor.close_calls
    assert executor.withdraw_calls, (
        "withdraw_all_margin must run when RISK_OFF_WITHDRAW=true "
        "and equity > 0"
    )
    assert plan.extra.get("risk_off_withdraw") is True
    assert "withdraw_skipped_reason" not in plan.extra


@pytest.mark.asyncio
async def test_risk_off_withdraw_true_but_zero_equity_still_skips() -> None:
    """RISK_OFF_WITHDRAW=true is necessary but not sufficient: a zero
    vault balance still skips the withdraw to avoid a noop tx."""
    executor = _FakePerpExecutor(equity=Decimal("0"))
    router = AllocationRouter(
        executor=executor,
        config=AllocationConfig(
            usyc_enabled=False,
            risk_off_withdraw=True,
        ),
        dry_run=False,
    )
    plan = await router.route(_risk_off_decision())
    assert executor.close_calls
    assert executor.withdraw_calls == [], (
        "withdraw_all_margin must skip when equity is 0 even with "
        "RISK_OFF_WITHDRAW=true"
    )
    # Gate is still True in plan extra - the skip is for the equity
    # reason, not for the safety reason.
    assert plan.extra.get("risk_off_withdraw") is True
