"""Unit tests for the PositionManager.

The PositionManager is pure logic - given an :class:`AccountInfo`
snapshot and an :class:`ExecutionDirective`, it decides whether each
open position should be held, partially closed or fully closed.
Tests here cover every trigger branch in priority order, plus the
in-memory trailing-stop state across multiple cycles.

We never touch a real executor; instead a tiny ``_FakeExecutor`` (with
just ``get_mid_price``) is enough to exercise the optional mid-price
fetch the manager uses for trigger-price telemetry.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

import pytest

from src.core.decision_engine import (
    DecisionResult,
    ExecutionDirective,
    LevelScore,
)
from src.execution.arc_perp_executor import AccountInfo, Position
from src.execution.position_manager import (
    PositionManager,
    PositionManagerConfig,
    PositionReview,
)


# ---------------------------------------------------------------------------
# Test fixtures / helpers
# ---------------------------------------------------------------------------


class _FakeExecutor:
    """Minimal stand-in for an executor; only ``get_mid_price`` is used."""

    def __init__(self, mids: dict[str, Decimal | None] | None = None) -> None:
        self._mids = mids or {}

    async def get_mid_price(self, symbol: str) -> Decimal | None:
        return self._mids.get(symbol)


def _account(positions: list[Position], equity: Decimal = Decimal("1000")) -> AccountInfo:
    """Build a trivial :class:`AccountInfo` wrapping the given positions."""
    pnl_total = sum(
        (p.unrealized_pnl_usd for p in positions), Decimal("0")
    )
    return AccountInfo(
        equity_usd=equity,
        free_margin_usd=equity,
        used_margin_usd=Decimal("0"),
        total_unrealized_pnl_usd=pnl_total,
        positions=positions,
    )


def _long_btc(
    pnl_usd: Decimal,
    *,
    size_usd: Decimal = Decimal("600"),
    entry: Decimal = Decimal("60000"),
    mark: Decimal | None = None,
) -> Position:
    return Position(
        symbol="BTC-PERP",
        side="long",
        size_usd=size_usd,
        entry_price=entry,
        mark_price=mark if mark is not None else entry,
        leverage=Decimal("3"),
        unrealized_pnl_usd=pnl_usd,
    )


def _short_btc(
    pnl_usd: Decimal,
    *,
    size_usd: Decimal = Decimal("600"),
    entry: Decimal = Decimal("60000"),
    mark: Decimal | None = None,
) -> Position:
    return Position(
        symbol="BTC-PERP",
        side="short",
        size_usd=size_usd,
        entry_price=entry,
        mark_price=mark if mark is not None else entry,
        leverage=Decimal("3"),
        unrealized_pnl_usd=pnl_usd,
    )


def _directive(
    *,
    action: str = "risk_on",
    side: str | None = "long",
    conviction: float = 0.7,
    direction_strength: float = 0.7,
) -> ExecutionDirective:
    return ExecutionDirective(
        action=action,
        side=side,
        intensity=conviction,
        rationale="test",
        market_bias=(
            "bullish" if side == "long"
            else "bearish" if side == "short"
            else "neutral"
        ),
        bias_strength=direction_strength,
        conviction=conviction,
        direction_strength=direction_strength,
    )


def _decision(directive: ExecutionDirective) -> DecisionResult:
    """Decision shell - PositionManager only reads ``directive``."""
    return DecisionResult(
        final_score=directive.conviction,
        regime=directive.action.replace("_", "-"),
        directive=directive,
        level_scores=[
            LevelScore(level=1, score=directive.conviction, raw={"l1": {}}, direction_sign=1),
            LevelScore(level=2, score=directive.conviction, raw={"l2": {}}, direction_sign=1),
            LevelScore(level=3, score=directive.conviction, raw={"l3": {}}, direction_sign=1),
        ],
        weights={"level1": 0.25, "level2": 0.35, "level3": 0.40},
        final_direction=1,
        direction_strength=directive.direction_strength,
        effective_weights={"level1": 0.25, "level2": 0.35, "level3": 0.40},
        short_circuited=False,
        timestamp=datetime.now(timezone.utc),
    )


def _make_manager(
    *,
    take_profit_pct: float = 0.03,
    stop_loss_pct: float = 0.02,
    trailing_stop_pct: float = 0.01,
    min_conviction_to_hold: float = 0.45,
    re_eval_min_profit_pct: float = 0.005,
    auto_flip: bool = True,
    enable_trailing: bool = True,
    enable_reeval: bool = True,
    mids: dict[str, Decimal | None] | None = None,
    # ---- New Day-5 features default to OFF in this helper so legacy
    # tests keep their original semantics. The new feature suite at
    # the bottom of the file enables them explicitly via _make_manager_v2.
    use_dynamic_atr_tpsl: bool = False,
    enable_partial_take_profit: bool = False,
    enable_breakeven: bool = False,
    enable_vol_filter: bool = False,
    enable_time_exit: bool = False,
    enable_daily_dd_guard: bool = False,
    enable_smart_path: bool = False,
) -> PositionManager:
    cfg = PositionManagerConfig(
        take_profit_pct=Decimal(str(take_profit_pct)),
        stop_loss_pct=Decimal(str(stop_loss_pct)),
        trailing_stop_pct=Decimal(str(trailing_stop_pct)),
        min_conviction_to_hold=min_conviction_to_hold,
        re_eval_min_profit_pct=Decimal(str(re_eval_min_profit_pct)),
        auto_flip_on_side_change=auto_flip,
        enable_trailing_stop=enable_trailing,
        enable_re_evaluation=enable_reeval,
        use_dynamic_atr_tpsl=use_dynamic_atr_tpsl,
        enable_partial_take_profit=enable_partial_take_profit,
        enable_breakeven=enable_breakeven,
        enable_vol_filter=enable_vol_filter,
        enable_time_exit=enable_time_exit,
        enable_daily_dd_guard=enable_daily_dd_guard,
        enable_smart_path=enable_smart_path,
    )
    return PositionManager(config=cfg, executor=_FakeExecutor(mids=mids))


# ---------------------------------------------------------------------------
# Empty / no-position cases
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_open_positions_returns_empty_review() -> None:
    """When no positions are open we should get an empty review with a note."""
    pm = _make_manager()
    account = _account(positions=[])
    review = await pm.review_open_positions(
        account=account, directive=_directive(), decision=None,
    )

    assert review.snapshots == []
    assert review.actions == []
    assert any("no open positions" in n for n in review.notes)
    assert review.has_action is False


@pytest.mark.asyncio
async def test_flat_positions_are_filtered() -> None:
    """Positions with side='flat' or size_usd=0 are ignored."""
    pm = _make_manager()
    flat = Position(
        symbol="BTC-PERP",
        side="flat",
        size_usd=Decimal("0"),
        entry_price=Decimal("0"),
        mark_price=Decimal("0"),
        leverage=Decimal("0"),
        unrealized_pnl_usd=Decimal("0"),
    )
    review = await pm.review_open_positions(
        account=_account([flat]),
        directive=_directive(),
        decision=None,
    )
    assert review.snapshots == []
    assert review.actions == []


# ---------------------------------------------------------------------------
# Stop-loss
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stop_loss_fires_when_pnl_breaches_negative_threshold() -> None:
    pm = _make_manager(stop_loss_pct=0.02)
    pos = _long_btc(pnl_usd=Decimal("-18"))  # -3% PnL on size=600
    review = await pm.review_open_positions(
        account=_account([pos]),
        directive=_directive(),
        decision=None,
    )
    assert len(review.actions) == 1
    a = review.actions[0]
    assert a.action == "close"
    assert a.trigger == "stop_loss"
    assert a.pnl_pct == pytest.approx(-0.03, rel=1e-6)
    assert a.size_usd == Decimal("600")
    assert "stop-loss" in a.reason.lower()


@pytest.mark.asyncio
async def test_stop_loss_disabled_when_threshold_zero() -> None:
    pm = _make_manager(stop_loss_pct=0.0)
    pos = _long_btc(pnl_usd=Decimal("-60"))  # -10% PnL, very deep
    review = await pm.review_open_positions(
        account=_account([pos]),
        directive=_directive(),
        decision=None,
    )
    assert review.has_action is False


@pytest.mark.asyncio
async def test_stop_loss_price_computed_for_long_and_short() -> None:
    pm = _make_manager(stop_loss_pct=0.02)
    long_pos = _long_btc(pnl_usd=Decimal("0"), entry=Decimal("60000"))
    short_pos = Position(
        symbol="ETH-PERP",
        side="short",
        size_usd=Decimal("400"),
        entry_price=Decimal("3000"),
        mark_price=Decimal("3000"),
        leverage=Decimal("3"),
        unrealized_pnl_usd=Decimal("0"),
    )
    review = await pm.review_open_positions(
        account=_account([long_pos, short_pos]),
        directive=_directive(),
        decision=None,
    )
    snaps_by_symbol = {s.symbol: s for s in review.snapshots}
    assert snaps_by_symbol["BTC-PERP"].stop_loss_price == Decimal("58800.0000")
    assert snaps_by_symbol["ETH-PERP"].stop_loss_price == Decimal("3060.0000")


# ---------------------------------------------------------------------------
# Take-profit
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_take_profit_fires_when_pnl_breaches_positive_threshold() -> None:
    pm = _make_manager(take_profit_pct=0.03)
    pos = _long_btc(pnl_usd=Decimal("25"))  # +4.17% PnL on size=600
    review = await pm.review_open_positions(
        account=_account([pos]),
        directive=_directive(),
        decision=None,
    )
    assert len(review.actions) == 1
    a = review.actions[0]
    assert a.action == "close"
    assert a.trigger == "take_profit"
    assert a.pnl_pct == pytest.approx(25 / 600, rel=1e-6)
    assert "take-profit" in a.reason.lower()


@pytest.mark.asyncio
async def test_stop_loss_takes_priority_over_take_profit_branch() -> None:
    """Stop-loss is the first branch in the priority ladder.

    Mathematically TP and SL never both trigger on the same cycle (they
    sit on opposite sides of zero), but we still want the ladder to be
    deterministic in case a future threshold tweak inverts that.
    """
    pm = _make_manager(stop_loss_pct=0.02, take_profit_pct=0.02)
    # PnL = +30 -> +5% triggers TP; flip sign to test SL precedence on
    # the same threshold magnitude.
    losing = _long_btc(pnl_usd=Decimal("-30"))  # -5% PnL
    review = await pm.review_open_positions(
        account=_account([losing]),
        directive=_directive(),
        decision=None,
    )
    assert review.actions[0].trigger == "stop_loss"


# ---------------------------------------------------------------------------
# Trailing stop
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_trailing_stop_only_arms_after_threshold_profit() -> None:
    """A position barely in profit shouldn't trail-stop on a tiny drop."""
    pm = _make_manager(
        take_profit_pct=0.10,         # high so it can't fire on cycle 1
        stop_loss_pct=0.10,           # high so it can't fire either
        trailing_stop_pct=0.01,
    )
    # Cycle 1: small profit, trail not armed yet (peak < 1%)
    pos = _long_btc(pnl_usd=Decimal("3"))   # +0.5%
    review = await pm.review_open_positions(
        account=_account([pos]),
        directive=_directive(),
        decision=None,
    )
    assert review.actions[0].action == "hold"


@pytest.mark.asyncio
async def test_trailing_stop_fires_after_peak_then_drawback() -> None:
    """Cycle 1 puts the position at peak, cycle 2 gives back > trail %."""
    pm = _make_manager(
        take_profit_pct=0.10,
        stop_loss_pct=0.10,
        trailing_stop_pct=0.01,
    )
    # Cycle 1: position +1.5% -> peak armed, no fire yet
    pos_peak = _long_btc(pnl_usd=Decimal("9"))     # +1.5%
    r1 = await pm.review_open_positions(
        account=_account([pos_peak]),
        directive=_directive(),
        decision=None,
    )
    assert r1.actions[0].action == "hold"
    assert r1.actions[0].peak_pnl_pct == pytest.approx(0.015, rel=1e-6)

    # Cycle 2: position drops to +0.3% -> drawback 1.2% > trail 1%
    pos_drop = _long_btc(pnl_usd=Decimal("1.80"))  # +0.30%
    r2 = await pm.review_open_positions(
        account=_account([pos_drop]),
        directive=_directive(),
        decision=None,
    )
    assert r2.actions[0].trigger == "trailing_stop"
    # Peak should still be 1.5% from cycle 1 (it can only grow).
    assert r2.actions[0].peak_pnl_pct == pytest.approx(0.015, rel=1e-6)


@pytest.mark.asyncio
async def test_trailing_stop_disabled_by_flag() -> None:
    """ENABLE_TRAILING_STOP=False suppresses the trigger even if armed."""
    pm = _make_manager(
        take_profit_pct=0.10, stop_loss_pct=0.10,
        trailing_stop_pct=0.01,
        enable_trailing=False,
    )
    # Set up a peak
    await pm.review_open_positions(
        account=_account([_long_btc(Decimal("9"))]),
        directive=_directive(),
        decision=None,
    )
    # Big give-back - would normally fire trailing
    r = await pm.review_open_positions(
        account=_account([_long_btc(Decimal("1.80"))]),
        directive=_directive(),
        decision=None,
    )
    assert r.actions[0].action == "hold"


# ---------------------------------------------------------------------------
# Side flip
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_side_flip_fires_on_opposite_directive() -> None:
    pm = _make_manager()
    pos = _long_btc(pnl_usd=Decimal("0"))  # 0% PnL so no TP / SL
    review = await pm.review_open_positions(
        account=_account([pos]),
        directive=_directive(side="short", conviction=0.75),
        decision=None,
    )
    assert review.actions[0].trigger == "side_flip"
    assert "long" in review.actions[0].reason.lower()
    assert "short" in review.actions[0].reason.lower()


@pytest.mark.asyncio
async def test_side_flip_disabled_when_auto_flip_false() -> None:
    pm = _make_manager(auto_flip=False)
    pos = _long_btc(pnl_usd=Decimal("0"))
    review = await pm.review_open_positions(
        account=_account([pos]),
        directive=_directive(side="short", conviction=0.75),
        decision=None,
    )
    assert review.actions[0].action == "hold"


@pytest.mark.asyncio
async def test_side_flip_skipped_for_non_risk_on_directive() -> None:
    """Hold / risk-off directives must NOT trigger a flip."""
    pm = _make_manager()
    pos = _long_btc(pnl_usd=Decimal("0"))
    review = await pm.review_open_positions(
        account=_account([pos]),
        directive=_directive(action="hold", side=None, conviction=0.3),
        decision=None,
    )
    assert review.actions[0].action == "hold"


# ---------------------------------------------------------------------------
# Re-evaluation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_re_evaluation_locks_in_modest_profit() -> None:
    pm = _make_manager(
        take_profit_pct=0.05, stop_loss_pct=0.05,
        trailing_stop_pct=0.0,  # disable trailing to isolate the trigger
        min_conviction_to_hold=0.5,
        re_eval_min_profit_pct=0.005,
    )
    pos = _long_btc(pnl_usd=Decimal("8"))  # +1.33% PnL
    # Mid-band hold + weak conviction = lock-in
    directive = _directive(action="hold", side=None, conviction=0.40)
    review = await pm.review_open_positions(
        account=_account([pos]),
        directive=directive,
        decision=None,
    )
    assert review.actions[0].trigger == "re_evaluation"


@pytest.mark.asyncio
async def test_re_evaluation_skipped_when_loss() -> None:
    """A position underwater shouldn't re-eval out - the SL path owns losers."""
    pm = _make_manager(
        take_profit_pct=0.05, stop_loss_pct=0.05,
        trailing_stop_pct=0.0,
        min_conviction_to_hold=0.5,
        re_eval_min_profit_pct=0.005,
    )
    pos = _long_btc(pnl_usd=Decimal("-3"))   # -0.5%, below re-eval floor
    review = await pm.review_open_positions(
        account=_account([pos]),
        directive=_directive(action="hold", side=None, conviction=0.40),
        decision=None,
    )
    assert review.actions[0].action == "hold"


@pytest.mark.asyncio
async def test_re_evaluation_skipped_when_directive_risk_off() -> None:
    """The risk-off pipeline already closes positions - don't double-fire."""
    pm = _make_manager(
        take_profit_pct=0.05, stop_loss_pct=0.05,
        trailing_stop_pct=0.0,
        min_conviction_to_hold=0.5,
        re_eval_min_profit_pct=0.005,
    )
    pos = _long_btc(pnl_usd=Decimal("8"))
    review = await pm.review_open_positions(
        account=_account([pos]),
        directive=_directive(action="risk_off", side=None, conviction=0.20),
        decision=None,
    )
    assert review.actions[0].action == "hold"


# ---------------------------------------------------------------------------
# Snapshot / panel telemetry
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_snapshot_carries_full_telemetry() -> None:
    """Every snapshot row must have TP / SL prices for the panel."""
    pm = _make_manager(
        take_profit_pct=0.03, stop_loss_pct=0.02,
        trailing_stop_pct=0.01,
    )
    pos = _long_btc(pnl_usd=Decimal("12"))  # +2% PnL, below TP, above re-eval
    review = await pm.review_open_positions(
        account=_account([pos]),
        directive=_directive(conviction=0.7),  # strong conviction => no re-eval
        decision=None,
    )
    assert len(review.snapshots) == 1
    s = review.snapshots[0]
    assert s.action == "hold"
    # Day-6+: Fast-Path HOLD now carries a positive justification
    # sub-trigger (HOLD must be earned). Without ATR data on the
    # decision the Fast Path classifies this as ``hold_no_data``;
    # with ATR data the trigger would be one of ``hold_trend_intact``
    # / ``hold_ranging`` / ``hold_low_conviction_profitable``.
    assert s.trigger in {
        "hold",
        "hold_trend_intact",
        "hold_ranging",
        "hold_low_conviction_profitable",
        "hold_no_data",
        "hold_unearned",
    }
    assert s.entry_price == Decimal("60000")
    assert s.take_profit_price == Decimal("61800.0000")
    assert s.stop_loss_price == Decimal("58800.0000")
    # Peak armed at +2%, trailing price displayed once peak >= trail%
    assert s.peak_pnl_pct == pytest.approx(0.02, rel=1e-6)
    assert s.trailing_stop_price is not None


@pytest.mark.asyncio
async def test_peak_resets_when_position_closes() -> None:
    """Stale per-position state entries are garbage-collected once positions disappear.

    The peak PnL now lives on :class:`_PositionState` (`pm._states`),
    not on the legacy ``pm._peak_pnl`` dict. We assert the
    per-`(symbol, side)` state is created in cycle 1 and evicted by
    cycle 2's garbage-collect pass.
    """
    pm = _make_manager()
    await pm.review_open_positions(
        account=_account([_long_btc(Decimal("9"))]),
        directive=_directive(),
        decision=None,
    )
    assert ("BTC-PERP", "long") in pm._states
    assert pm._states[("BTC-PERP", "long")].peak_pnl_pct > 0
    await pm.review_open_positions(
        account=_account([]),
        directive=_directive(),
        decision=None,
    )
    assert ("BTC-PERP", "long") not in pm._states


# ---------------------------------------------------------------------------
# Symmetry across long / short
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_take_profit_symmetric_for_shorts() -> None:
    """A short in profit should fire TP exactly like a long."""
    pm = _make_manager(take_profit_pct=0.03)
    pos = _short_btc(pnl_usd=Decimal("25"))  # +4.17% PnL
    review = await pm.review_open_positions(
        account=_account([pos]),
        directive=_directive(side="short"),
        decision=None,
    )
    assert review.actions[0].trigger == "take_profit"


@pytest.mark.asyncio
async def test_stop_loss_symmetric_for_shorts() -> None:
    pm = _make_manager(stop_loss_pct=0.02)
    pos = _short_btc(pnl_usd=Decimal("-18"))  # -3% PnL
    review = await pm.review_open_positions(
        account=_account([pos]),
        directive=_directive(side="short"),
        decision=None,
    )
    assert review.actions[0].trigger == "stop_loss"


# ---------------------------------------------------------------------------
# from_settings convenience constructor
# ---------------------------------------------------------------------------


class _FakeSettings:
    """Duck-typed settings object - PositionManager only reads attributes."""

    TAKE_PROFIT_PCT = 4.0
    STOP_LOSS_PCT = 2.5
    TRAILING_STOP_PCT = 1.5
    MIN_CONVICTION_TO_HOLD = 0.50
    RE_EVAL_MIN_PROFIT_PCT = 0.7
    AUTO_FLIP_ON_SIDE_CHANGE = False
    ENABLE_TRAILING_STOP = True
    ENABLE_RE_EVALUATION = False


def test_from_settings_converts_percent_to_fraction() -> None:
    """Operator-friendly percentages from .env -> Decimal fractions."""
    pm = PositionManager.from_settings(_FakeSettings(), executor=None)
    assert pm.config.take_profit_pct == Decimal("0.040000")
    assert pm.config.stop_loss_pct == Decimal("0.025000")
    assert pm.config.trailing_stop_pct == Decimal("0.015000")
    assert pm.config.min_conviction_to_hold == 0.5
    assert pm.config.re_eval_min_profit_pct == Decimal("0.007000")
    assert pm.config.auto_flip_on_side_change is False
    assert pm.config.enable_trailing_stop is True
    assert pm.config.enable_re_evaluation is False


def test_from_settings_uses_defaults_for_missing_attrs() -> None:
    """Missing attributes on the settings object fall back to dataclass defaults."""
    class _BareSettings:
        pass

    pm = PositionManager.from_settings(_BareSettings(), executor=None)
    # Defaults from PositionManagerConfig
    assert pm.config.take_profit_pct == PositionManagerConfig().take_profit_pct
    assert pm.config.stop_loss_pct == PositionManagerConfig().stop_loss_pct
    assert pm.config.trailing_stop_pct == PositionManagerConfig().trailing_stop_pct


# ---------------------------------------------------------------------------
# Robustness
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_executor_mid_price_failure_does_not_crash() -> None:
    """A flaky executor.get_mid_price call must not propagate."""

    class _BrokenExecutor:
        async def get_mid_price(self, symbol: str) -> Decimal | None:
            raise RuntimeError("boom")

    pm = PositionManager(
        config=PositionManagerConfig(),
        executor=_BrokenExecutor(),
    )
    pos = _long_btc(pnl_usd=Decimal("0"))
    review = await pm.review_open_positions(
        account=_account([pos]),
        directive=_directive(),
        decision=None,
    )
    assert isinstance(review, PositionReview)
    assert review.actions[0].action == "hold"


@pytest.mark.asyncio
async def test_executor_without_mid_price_method_is_tolerated() -> None:
    """Legacy executors without ``get_mid_price`` must still work."""

    class _LegacyExecutor:
        pass

    pm = PositionManager(
        config=PositionManagerConfig(enable_daily_dd_guard=False),
        executor=_LegacyExecutor(),
    )
    pos = _long_btc(pnl_usd=Decimal("0"))
    review = await pm.review_open_positions(
        account=_account([pos]),
        directive=_directive(),
        decision=None,
    )
    assert review.actions[0].action == "hold"


# ===========================================================================
# Day-5 two-tier system tests
#
# These tests exercise the new advanced features:
#   * dynamic ATR-based TP/SL
#   * partial take-profit (with state persistence)
#   * breakeven arming + dynamic SL lift
#   * volatility spike filter (warn vs close)
#   * time-based exit
#   * daily-DD kill switch (portfolio-wide)
#   * per-asset ATR cap surfaced on snapshot
#   * Smart Path trigger detection (price move / periodic / funding)
#   * Smart Path verdict override of Fast Path
# ===========================================================================


from datetime import timedelta as _td

from src.execution.position_manager import (
    PerAssetATRCaps,
    SmartPathBriefing,
    SmartPathVerdict,
    _PositionState,
)


def _decision_with_atr(
    directive: ExecutionDirective,
    *,
    btc_atr_pct: float = 1.0,
    eth_atr_pct: float | None = None,
) -> DecisionResult:
    """A DecisionResult whose L1 raw carries an ATR% snapshot per symbol."""
    per_symbol = {"BTC-PERP": {"atr_pct_avg": btc_atr_pct}}
    if eth_atr_pct is not None:
        per_symbol["ETH-PERP"] = {"atr_pct_avg": eth_atr_pct}
    return DecisionResult(
        final_score=directive.conviction,
        regime=directive.action.replace("_", "-"),
        directive=directive,
        level_scores=[
            LevelScore(
                level=1,
                score=directive.conviction,
                raw={"l1": {"per_symbol": per_symbol}},
                direction_sign=1,
            ),
            LevelScore(
                level=2,
                score=directive.conviction,
                raw={"l2": {}},
                direction_sign=1,
            ),
            LevelScore(
                level=3,
                score=directive.conviction,
                raw={"l3": {}},
                direction_sign=1,
            ),
        ],
        weights={"level1": 0.25, "level2": 0.35, "level3": 0.40},
        final_direction=1,
        direction_strength=directive.direction_strength,
        effective_weights={"level1": 0.25, "level2": 0.35, "level3": 0.40},
        short_circuited=False,
        timestamp=datetime.now(timezone.utc),
    )


def _decision_with_l2(
    directive: ExecutionDirective,
    *,
    btc_atr_pct: float = 1.0,
    funding_rate: float | None = None,
    oi_delta_1h_pct: float | None = None,
    n_whales: int = 0,
    whale_direction: str | None = None,
) -> DecisionResult:
    """A DecisionResult carrying both L1 ATR and L2 funding/OI/whales."""
    base = _decision_with_atr(directive, btc_atr_pct=btc_atr_pct)
    base.level_scores[1] = LevelScore(
        level=2,
        score=directive.conviction,
        raw={
            "l2": {
                "per_symbol": {
                    "BTC-PERP": {
                        "funding": {"current_rate": funding_rate},
                        "open_interest": {
                            "delta_1h_pct": oi_delta_1h_pct,
                        },
                        "whales": {
                            "n_whales": n_whales,
                            "direction": whale_direction,
                        },
                    }
                }
            }
        },
        direction_sign=1,
    )
    return base


# ---- Dynamic ATR-based TP/SL ------------------------------------------------


@pytest.mark.asyncio
async def test_dynamic_atr_stop_loss_fires_when_mark_crosses_sl_price() -> None:
    """With ATR=1% and SL=1.5xATR, the dynamic SL price is entry - 1.5%."""
    pm = PositionManager(
        config=PositionManagerConfig(
            use_dynamic_atr_tpsl=True,
            sl_atr_mult=1.5,
            tp_atr_mult=3.0,
            trail_atr_mult=1.5,
            enable_daily_dd_guard=False,
            enable_partial_take_profit=False,
            enable_breakeven=False,
            enable_vol_filter=False,
            enable_time_exit=False,
            enable_smart_path=False,
        ),
        executor=_FakeExecutor(mids={"BTC-PERP": Decimal("59100")}),
    )
    pos = _long_btc(
        pnl_usd=Decimal("-9"),  # -1.5% PnL ≈ ATR*1.5
        entry=Decimal("60000"),
        mark=Decimal("59100"),
    )
    review = await pm.review_open_positions(
        account=_account([pos]),
        directive=_directive(),
        decision=_decision_with_atr(_directive(), btc_atr_pct=1.0),
    )
    assert review.actions[0].trigger == "stop_loss"
    assert "dynamic SL" in review.actions[0].reason
    snap = review.snapshots[0]
    assert snap.current_atr_pct == pytest.approx(1.0, rel=1e-6)


@pytest.mark.asyncio
async def test_dynamic_atr_take_profit_fires_at_target_multiple() -> None:
    """With ATR=1% and TP=3xATR, the dynamic TP price ≈ entry + 3xATR.

    ATR_abs is computed off the LIVE mid (so the trigger respects the
    current vol regime); on a 60000 entry with mid=62000 and ATR=1%
    that's 620 per ATR unit, so TP target = 60000 + 620*3 = 61860.
    Use mark=62000 to cleanly cross it.
    """
    pm = PositionManager(
        config=PositionManagerConfig(
            use_dynamic_atr_tpsl=True,
            sl_atr_mult=1.5,
            tp_atr_mult=3.0,
            enable_daily_dd_guard=False,
            enable_partial_take_profit=False,
            enable_breakeven=False,
            enable_vol_filter=False,
            enable_time_exit=False,
            enable_smart_path=False,
        ),
        executor=_FakeExecutor(mids={"BTC-PERP": Decimal("62000")}),
    )
    pos = _long_btc(
        pnl_usd=Decimal("20"),  # ~+3.3% PnL
        entry=Decimal("60000"),
        mark=Decimal("62000"),
    )
    review = await pm.review_open_positions(
        account=_account([pos]),
        directive=_directive(),
        decision=_decision_with_atr(_directive(), btc_atr_pct=1.0),
    )
    assert review.actions[0].trigger == "take_profit"
    assert "dynamic TP" in review.actions[0].reason


# ---- Partial take-profit ----------------------------------------------------


@pytest.mark.asyncio
async def test_partial_take_profit_fires_once_and_records_state() -> None:
    """Partial TP must fire exactly once per position lifetime.

    With ATR=1% on live mid=61000 and partial mult=1.5 the trigger
    distance is 1.5 * 610 = 915. So the partial target ≈ 60915 and a
    mark of 61000 cleanly crosses it.
    """
    pm = PositionManager(
        config=PositionManagerConfig(
            use_dynamic_atr_tpsl=True,
            sl_atr_mult=1.5,
            tp_atr_mult=3.0,
            enable_partial_take_profit=True,
            partial_tp_atr_mult=1.5,
            partial_tp_fraction=Decimal("0.50"),
            enable_breakeven=False,
            enable_vol_filter=False,
            enable_time_exit=False,
            enable_daily_dd_guard=False,
            enable_smart_path=False,
        ),
        executor=_FakeExecutor(mids={"BTC-PERP": Decimal("61000")}),
    )
    pos = _long_btc(
        pnl_usd=Decimal("10"),
        entry=Decimal("60000"),
        mark=Decimal("61000"),
    )
    # Cycle 1: partial TP fires
    review1 = await pm.review_open_positions(
        account=_account([pos]),
        directive=_directive(),
        decision=_decision_with_atr(_directive(), btc_atr_pct=1.0),
    )
    a1 = review1.actions[0]
    assert a1.trigger == "partial_take_profit"
    assert a1.action == "partial_close"
    assert a1.size_usd_to_close == Decimal("300")  # 50% of 600
    assert pm._states[("BTC-PERP", "long")].partial_tp_done is True
    # Cycle 2: still in profit, partial TP must NOT fire again
    review2 = await pm.review_open_positions(
        account=_account([pos]),
        directive=_directive(),
        decision=_decision_with_atr(_directive(), btc_atr_pct=1.0),
    )
    triggers = {a.trigger for a in review2.actions}
    assert "partial_take_profit" not in triggers


# ---- Breakeven arming -------------------------------------------------------


@pytest.mark.asyncio
async def test_breakeven_arms_and_lifts_stop_to_entry_plus_buffer() -> None:
    """Once BE arms, the dynamic SL is lifted to entry + buffer."""
    pm = PositionManager(
        config=PositionManagerConfig(
            use_dynamic_atr_tpsl=True,
            sl_atr_mult=1.5,
            tp_atr_mult=3.0,
            enable_breakeven=True,
            breakeven_trigger_atr_mult=1.0,
            breakeven_buffer_pct=Decimal("0.001"),  # +0.1%
            enable_partial_take_profit=False,
            enable_vol_filter=False,
            enable_time_exit=False,
            enable_daily_dd_guard=False,
            enable_smart_path=False,
        ),
        executor=_FakeExecutor(mids={"BTC-PERP": Decimal("60600")}),
    )
    pos = _long_btc(
        pnl_usd=Decimal("9"),  # +1.5% PnL, > breakeven trigger
        entry=Decimal("60000"),
        mark=Decimal("60600"),
    )
    # Cycle 1: BE arms (no exit yet)
    review = await pm.review_open_positions(
        account=_account([pos]),
        directive=_directive(),
        decision=_decision_with_atr(_directive(), btc_atr_pct=1.0),
    )
    assert pm._states[("BTC-PERP", "long")].breakeven_armed is True
    # Cycle 2: mark drops back to slightly above entry -> SL price should
    # be at entry + buffer = 60060 - any tick below should fire.
    pos2 = _long_btc(
        pnl_usd=Decimal("-1"),  # PnL just under entry
        entry=Decimal("60000"),
        mark=Decimal("60050"),  # below entry + buffer
    )
    pm._fake_mids = {"BTC-PERP": Decimal("60050")}
    pm.executor = _FakeExecutor(mids={"BTC-PERP": Decimal("60050")})
    review2 = await pm.review_open_positions(
        account=_account([pos2]),
        directive=_directive(),
        decision=_decision_with_atr(_directive(), btc_atr_pct=1.0),
    )
    a = review2.actions[0]
    assert a.trigger == "stop_loss"
    # The SL price MUST be the BE-lifted price (entry + 0.1% = 60060),
    # NOT the raw dynamic ATR stop (which would be entry - 1.5*ATR =
    # ~59100). The mark at 60050 < 60060 -> stop fires. That the SL
    # fired at all (entry +buffer is above the original ATR stop)
    # proves the BE lift actually engaged.
    snap = review2.snapshots[0]
    assert snap.stop_loss_price == Decimal("60060.0000")
    assert snap.breakeven_armed is True


# ---- Volatility spike filter ------------------------------------------------


@pytest.mark.asyncio
async def test_vol_spike_close_action_forces_immediate_exit() -> None:
    """When VOL_SPIKE_ACTION=close, a 2x ATR jump exits the position."""
    pm = PositionManager(
        config=PositionManagerConfig(
            use_dynamic_atr_tpsl=True,
            enable_vol_filter=True,
            vol_spike_mult=2.0,
            vol_spike_action="close",
            enable_partial_take_profit=False,
            enable_breakeven=False,
            enable_time_exit=False,
            enable_daily_dd_guard=False,
            enable_smart_path=False,
        ),
        executor=_FakeExecutor(mids={"BTC-PERP": Decimal("60000")}),
    )
    pos = _long_btc(
        pnl_usd=Decimal("0"),
        entry=Decimal("60000"),
        mark=Decimal("60000"),
    )
    # Cycle 1: open at ATR=1% (sets entry_atr_pct=1%)
    await pm.review_open_positions(
        account=_account([pos]),
        directive=_directive(),
        decision=_decision_with_atr(_directive(), btc_atr_pct=1.0),
    )
    # Cycle 2: ATR explodes to 2.5% (2.5x entry ATR) -> close
    review = await pm.review_open_positions(
        account=_account([pos]),
        directive=_directive(),
        decision=_decision_with_atr(_directive(), btc_atr_pct=2.5),
    )
    assert review.actions[0].trigger == "vol_spike_close"


@pytest.mark.asyncio
async def test_vol_spike_warn_action_is_advisory_only() -> None:
    """When VOL_SPIKE_ACTION=tighten_stop, a 2x spike must NOT close."""
    pm = PositionManager(
        config=PositionManagerConfig(
            use_dynamic_atr_tpsl=True,
            sl_atr_mult=1.5,
            tp_atr_mult=3.0,
            enable_vol_filter=True,
            vol_spike_mult=2.0,
            vol_spike_action="tighten_stop",
            enable_partial_take_profit=False,
            enable_breakeven=False,
            enable_time_exit=False,
            enable_daily_dd_guard=False,
            enable_smart_path=False,
        ),
        executor=_FakeExecutor(mids={"BTC-PERP": Decimal("60000")}),
    )
    pos = _long_btc(
        pnl_usd=Decimal("0"),
        entry=Decimal("60000"),
        mark=Decimal("60000"),
    )
    await pm.review_open_positions(
        account=_account([pos]),
        directive=_directive(),
        decision=_decision_with_atr(_directive(), btc_atr_pct=1.0),
    )
    review = await pm.review_open_positions(
        account=_account([pos]),
        directive=_directive(),
        decision=_decision_with_atr(_directive(), btc_atr_pct=2.5),
    )
    closes = [a for a in review.actions if a.action == "close"]
    assert closes == []


# ---- Time-based exit --------------------------------------------------------


@pytest.mark.asyncio
async def test_time_exit_fires_when_position_held_too_long() -> None:
    """A position open longer than MAX_POSITION_HOLD_HOURS must close."""
    pm = PositionManager(
        config=PositionManagerConfig(
            use_dynamic_atr_tpsl=False,
            enable_time_exit=True,
            max_position_hold_hours=1.0,
            enable_partial_take_profit=False,
            enable_breakeven=False,
            enable_vol_filter=False,
            enable_daily_dd_guard=False,
            enable_smart_path=False,
        ),
        executor=_FakeExecutor(),
    )
    pos = _long_btc(pnl_usd=Decimal("0"))
    state = pm._ensure_state(pos, {})
    state.opened_at = datetime.now(timezone.utc) - _td(hours=2)
    review = await pm.review_open_positions(
        account=_account([pos]),
        directive=_directive(),
        decision=None,
    )
    assert review.actions[0].trigger == "time_exit"


# ---- Daily-DD kill switch ---------------------------------------------------


@pytest.mark.asyncio
async def test_daily_dd_guard_fires_when_session_loss_crosses_limit() -> None:
    """A 5% daily loss crosses the default 5% limit -> all positions flatten."""
    pm = PositionManager(
        config=PositionManagerConfig(
            enable_daily_dd_guard=True,
            daily_loss_limit_pct=5.0,
            enable_partial_take_profit=False,
            enable_breakeven=False,
            enable_vol_filter=False,
            enable_time_exit=False,
            enable_smart_path=False,
        ),
        executor=_FakeExecutor(),
    )
    pos = _long_btc(pnl_usd=Decimal("-60"))  # -6% on 1000 equity
    review = await pm.review_open_positions(
        account=_account([pos], equity=Decimal("1000")),
        directive=_directive(),
        decision=None,
    )
    assert review.daily_dd_breached is True
    assert review.actions[0].trigger == "daily_dd_guard"


@pytest.mark.asyncio
async def test_daily_dd_guard_blocks_new_opens_until_session_reset() -> None:
    """Once breached, the daily-DD flag stays True even after PnL recovers."""
    pm = PositionManager(
        config=PositionManagerConfig(
            enable_daily_dd_guard=True,
            daily_loss_limit_pct=5.0,
            enable_partial_take_profit=False,
            enable_breakeven=False,
            enable_vol_filter=False,
            enable_time_exit=False,
            enable_smart_path=False,
        ),
        executor=_FakeExecutor(),
    )
    # Trip the guard
    await pm.review_open_positions(
        account=_account([_long_btc(Decimal("-60"))], equity=Decimal("1000")),
        directive=_directive(),
        decision=None,
    )
    # Subsequent cycle: even with zero open positions, the flag persists
    review = await pm.review_open_positions(
        account=_account([], equity=Decimal("1000")),
        directive=_directive(),
        decision=None,
    )
    assert review.daily_dd_breached is True


@pytest.mark.asyncio
async def test_daily_dd_guard_does_not_fire_on_flat_portfolio() -> None:
    """Sanity gate: a flat portfolio with positive equity must NEVER fire
    the kill-switch on the first cycle.

    Regression for the "agent auto-drained the testnet vault on a buggy
    PnL read" incident: a USDC-only wallet (no positions) is the
    *worst* place for the kill-switch to fire because there is
    nothing to flatten - the only effect is to run the risk-off
    pipeline and drain the venue. The tracker must re-baseline its
    starting equity to ``equity`` whenever ``position_count == 0``,
    regardless of what the upstream PnL read says.
    """
    pm = PositionManager(
        config=PositionManagerConfig(
            enable_daily_dd_guard=True,
            daily_loss_limit_pct=5.0,
            enable_partial_take_profit=False,
            enable_breakeven=False,
            enable_vol_filter=False,
            enable_time_exit=False,
            enable_smart_path=False,
        ),
        executor=_FakeExecutor(),
    )
    # Simulate the live bug: a flat wallet that the upstream
    # AccountInfo read reports as "equity=900, pnl=-900" (the old
    # broken `totalNtlPos - totalRawUsd` formula).
    bad_account = AccountInfo(
        equity_usd=Decimal("900"),
        free_margin_usd=Decimal("900"),
        used_margin_usd=Decimal("0"),
        total_unrealized_pnl_usd=Decimal("-900"),
        positions=[],
    )
    review = await pm.review_open_positions(
        account=bad_account,
        directive=_directive(),
        decision=None,
    )
    assert review.daily_dd_breached is False
    assert review.daily_pnl_pct == 0.0


@pytest.mark.asyncio
async def test_daily_dd_guard_resets_when_portfolio_goes_flat_before_first_breach() -> None:
    """When positions get closed BEFORE the kill-switch ever fired,
    the next flat cycle must NOT inherit a ghost drawdown from a
    transient mid-cycle equity dip.
    """
    pm = PositionManager(
        config=PositionManagerConfig(
            enable_daily_dd_guard=True,
            daily_loss_limit_pct=5.0,
            enable_partial_take_profit=False,
            enable_breakeven=False,
            enable_vol_filter=False,
            enable_time_exit=False,
            enable_smart_path=False,
        ),
        executor=_FakeExecutor(),
    )
    # Cycle 1: equity 1000, position with -2% PnL (below 5% limit -> not breached)
    await pm.review_open_positions(
        account=_account([_long_btc(Decimal("-20"))], equity=Decimal("980")),
        directive=_directive(),
        decision=None,
    )
    # Cycle 2: position closed (flat), equity remains 980 (-2% on starting 1000)
    review = await pm.review_open_positions(
        account=_account([], equity=Decimal("980")),
        directive=_directive(),
        decision=None,
    )
    assert review.daily_dd_breached is False
    # Cycle 3: a fresh -2% equity move from THIS new baseline (980 -> 960)
    # is only ~2%, well within the 5% limit; the sanity gate must
    # have re-baselined to 980, not kept the old 1000 starting point
    # (which would have made cycle 3 read as -4% and still pass, but
    # any worse cycle would falsely fire).
    review = await pm.review_open_positions(
        account=_account([_long_btc(Decimal("-20"))], equity=Decimal("960")),
        directive=_directive(),
        decision=None,
    )
    assert review.daily_dd_breached is False


# ---- Per-asset ATR cap surfaced on snapshot --------------------------------


@pytest.mark.asyncio
async def test_atr_cap_breached_flag_surfaces_on_snapshot() -> None:
    """When live ATR% > per-asset cap, snapshot must mark cap as breached."""
    pm = PositionManager(
        config=PositionManagerConfig(
            atr_caps=PerAssetATRCaps(btc_1h=3.0, eth_1h=4.0, default_1h=3.0),
            enable_daily_dd_guard=False,
            enable_partial_take_profit=False,
            enable_breakeven=False,
            enable_vol_filter=False,
            enable_time_exit=False,
            enable_smart_path=False,
        ),
        executor=_FakeExecutor(),
    )
    pos = _long_btc(pnl_usd=Decimal("0"))
    review = await pm.review_open_positions(
        account=_account([pos]),
        directive=_directive(),
        decision=_decision_with_atr(_directive(), btc_atr_pct=5.0),  # > 3% cap
    )
    snap = review.snapshots[0]
    assert snap.current_atr_pct == pytest.approx(5.0, rel=1e-6)
    assert snap.atr_cap_pct == pytest.approx(3.0, rel=1e-6)
    assert snap.atr_cap_breached is True


# ---- Smart Path ------------------------------------------------------------


@pytest.mark.asyncio
async def test_smart_path_periodic_review_triggers_after_max_interval() -> None:
    """When wall-clock since last review > max_interval, force an L3 call."""
    calls: list[SmartPathBriefing] = []

    async def _capture_arbiter(briefing: SmartPathBriefing) -> SmartPathVerdict:
        calls.append(briefing)
        return SmartPathVerdict(
            action="hold", rationale="all good", confidence=0.8,
        )

    pm = PositionManager(
        config=PositionManagerConfig(
            enable_smart_path=True,
            first_review_delay_minutes=0.0,
            l3_review_min_interval_minutes=0.0,  # no min throttle
            l3_review_max_interval_minutes=1.0,  # force after 1 minute
            l3_review_trigger_price_atr_mult=999.0,  # disable price gate
            l3_review_trigger_funding_rate=999.0,
            l3_review_trigger_whale_count=999,
            l3_review_trigger_oi_delta_pct=999.0,
            l3_can_override_fast_path=True,
            enable_daily_dd_guard=False,
            enable_partial_take_profit=False,
            enable_breakeven=False,
            enable_vol_filter=False,
            enable_time_exit=False,
        ),
        executor=_FakeExecutor(),
        position_arbiter=_capture_arbiter,
    )
    pos = _long_btc(pnl_usd=Decimal("0"))
    state = pm._ensure_state(pos, {})
    # Last check must be >= the 25-min HARD cooldown floor for the
    # periodic refresh to fire (the manager enforces a per-position
    # Smart-Path cooldown floor regardless of .env to bound LLM cost).
    state.last_l3_check_at = datetime.now(timezone.utc) - _td(minutes=30)
    review = await pm.review_open_positions(
        account=_account([pos]),
        directive=_directive(),
        decision=_decision_with_atr(_directive(), btc_atr_pct=1.0),
    )
    assert len(calls) == 1
    snap = review.snapshots[0]
    assert snap.smart_path_invoked is True
    assert snap.smart_path_verdict == "hold"


@pytest.mark.asyncio
async def test_smart_path_close_full_overrides_fast_path_hold() -> None:
    """L3 verdict=close_full must override a Fast-Path HOLD when override on."""

    async def _close_arbiter(briefing: SmartPathBriefing) -> SmartPathVerdict:
        return SmartPathVerdict(
            action="close_full",
            rationale="funding flipping hostile + whales distributing",
            confidence=0.85,
        )

    pm = PositionManager(
        config=PositionManagerConfig(
            enable_smart_path=True,
            first_review_delay_minutes=0.0,
            l3_review_min_interval_minutes=0.0,
            l3_review_max_interval_minutes=0.0,  # always invoke
            l3_review_trigger_price_atr_mult=999.0,
            l3_review_trigger_funding_rate=999.0,
            l3_review_trigger_whale_count=999,
            l3_review_trigger_oi_delta_pct=999.0,
            l3_can_override_fast_path=True,
            enable_daily_dd_guard=False,
            enable_partial_take_profit=False,
            enable_breakeven=False,
            enable_vol_filter=False,
            enable_time_exit=False,
        ),
        executor=_FakeExecutor(),
        position_arbiter=_close_arbiter,
    )
    pos = _long_btc(pnl_usd=Decimal("3"))  # +0.5% PnL - too small to TP/SL
    review = await pm.review_open_positions(
        account=_account([pos]),
        directive=_directive(),
        decision=_decision_with_atr(_directive(), btc_atr_pct=1.0),
    )
    a = review.actions[0]
    assert a.action == "close"
    assert a.trigger == "l3_smart_close"
    assert "funding" in (a.smart_snippet or "").lower()


@pytest.mark.asyncio
async def test_smart_path_close_partial_translates_to_partial_close() -> None:
    """L3 verdict=close_partial(0.4) must produce a partial close at 40%."""

    async def _partial_arbiter(briefing: SmartPathBriefing) -> SmartPathVerdict:
        return SmartPathVerdict(
            action="close_partial",
            rationale="taking some risk off here",
            confidence=0.7,
            close_fraction=Decimal("0.40"),
        )

    pm = PositionManager(
        config=PositionManagerConfig(
            enable_smart_path=True,
            first_review_delay_minutes=0.0,
            l3_review_min_interval_minutes=0.0,
            l3_review_max_interval_minutes=0.0,
            l3_review_trigger_price_atr_mult=999.0,
            l3_review_trigger_funding_rate=999.0,
            l3_review_trigger_whale_count=999,
            l3_review_trigger_oi_delta_pct=999.0,
            l3_can_override_fast_path=True,
            enable_daily_dd_guard=False,
            enable_partial_take_profit=False,
            enable_breakeven=False,
            enable_vol_filter=False,
            enable_time_exit=False,
        ),
        executor=_FakeExecutor(),
        position_arbiter=_partial_arbiter,
    )
    pos = _long_btc(pnl_usd=Decimal("3"))
    review = await pm.review_open_positions(
        account=_account([pos]),
        directive=_directive(),
        decision=_decision_with_atr(_directive(), btc_atr_pct=1.0),
    )
    a = review.actions[0]
    assert a.action == "partial_close"
    assert a.trigger == "l3_smart_partial"
    # 40% of 600 = 240
    assert a.size_usd_to_close == Decimal("240.0")


@pytest.mark.asyncio
async def test_smart_path_disabled_when_no_arbiter_present() -> None:
    """Without an arbiter, smart_path_invoked must be False on every snapshot."""
    pm = PositionManager(
        config=PositionManagerConfig(
            enable_smart_path=True,
            first_review_delay_minutes=0.0,
            l3_review_max_interval_minutes=0.0,
            enable_daily_dd_guard=False,
            enable_partial_take_profit=False,
            enable_breakeven=False,
            enable_vol_filter=False,
            enable_time_exit=False,
        ),
        executor=_FakeExecutor(),
        position_arbiter=None,
    )
    pos = _long_btc(pnl_usd=Decimal("0"))
    review = await pm.review_open_positions(
        account=_account([pos]),
        directive=_directive(),
        decision=_decision_with_atr(_directive(), btc_atr_pct=1.0),
    )
    snap = review.snapshots[0]
    assert snap.smart_path_invoked is False


@pytest.mark.asyncio
async def test_smart_path_override_off_treats_verdict_as_advisory() -> None:
    """When override is OFF, L3 close vote must NOT close the position."""

    async def _close_arbiter(briefing: SmartPathBriefing) -> SmartPathVerdict:
        return SmartPathVerdict(
            action="close_full", rationale="risk-off", confidence=0.9,
        )

    pm = PositionManager(
        config=PositionManagerConfig(
            enable_smart_path=True,
            first_review_delay_minutes=0.0,
            l3_review_min_interval_minutes=0.0,
            l3_review_max_interval_minutes=0.0,
            l3_review_trigger_price_atr_mult=999.0,
            l3_review_trigger_funding_rate=999.0,
            l3_review_trigger_whale_count=999,
            l3_review_trigger_oi_delta_pct=999.0,
            l3_can_override_fast_path=False,  # advisory only
            enable_daily_dd_guard=False,
            enable_partial_take_profit=False,
            enable_breakeven=False,
            enable_vol_filter=False,
            enable_time_exit=False,
        ),
        executor=_FakeExecutor(),
        position_arbiter=_close_arbiter,
    )
    pos = _long_btc(pnl_usd=Decimal("3"))
    review = await pm.review_open_positions(
        account=_account([pos]),
        directive=_directive(),
        decision=_decision_with_atr(_directive(), btc_atr_pct=1.0),
    )
    a = review.actions[0]
    assert a.action == "hold"
    snap = review.snapshots[0]
    assert snap.smart_path_invoked is True
    assert snap.smart_path_verdict == "close_full"


# ---- Per-asset ATR caps cap_for() ------------------------------------------


def test_per_asset_atr_caps_resolve_symbol_correctly() -> None:
    """``cap_for`` must pick the right bucket per-symbol or fall back."""
    caps = PerAssetATRCaps(btc_1h=3.0, eth_1h=4.0, default_1h=5.0)
    assert caps.cap_for("BTC-PERP") == 3.0
    assert caps.cap_for("BTC-USD") == 3.0
    assert caps.cap_for("btc-perp") == 3.0
    assert caps.cap_for("ETH-PERP") == 4.0
    assert caps.cap_for("SOL-PERP") == 5.0
    assert caps.cap_for("") == 5.0


# ===========================================================================
# Day-5+ follow-up: HOLD-rescue, L3_AGGRESSION calibration, risk-per-trade %
# ===========================================================================
#
# These tests cover the four critical follow-ups added on top of the
# Day-5 two-tier PositionManager:
#
#   1. RISK_PER_TRADE_PCT is the operator-friendly knob and wins over
#      the legacy TARGET_RISK_PCT fraction.
#   2. L3_AGGRESSION in {conservative, balanced, aggressive} modulates
#      both the override threshold and the partial-close fraction.
#   3. HOLD-rescue rule: a Fast-Path HOLD with high opposing L2
#      conviction forces a Smart-Path review with a dedicated trigger.
#   4. Fast / Smart Path stay cleanly separated - the Smart Path is
#      ONLY invoked on a fired gate.


def _decision_with_l2_conviction(
    directive: ExecutionDirective,
    *,
    btc_atr_pct: float = 1.0,
    l2_conviction: float = 0.0,
    l2_direction_sign: int = 0,
) -> DecisionResult:
    """A DecisionResult carrying explicit L2 conviction + direction.

    Used by HOLD-rescue tests; the L2 LevelScore's ``score`` and
    ``direction_sign`` are the source of truth ``_extract_l2_signals``
    falls back to when per-symbol attribution isn't present.
    """
    base = _decision_with_atr(directive, btc_atr_pct=btc_atr_pct)
    base.level_scores[1] = LevelScore(
        level=2,
        score=l2_conviction,
        raw={"l2": {}},
        direction_sign=l2_direction_sign,
    )
    return base


# ---- Settings.resolved_risk_per_trade_frac ---------------------------------


def test_settings_resolved_risk_per_trade_pct_wins_over_legacy_fraction() -> None:
    """RISK_PER_TRADE_PCT (1.0 = 1%) wins over TARGET_RISK_PCT (0.02)."""
    from src.utils.config import Settings

    s = Settings(
        OPENROUTER_API_KEY="dummy",
        RISK_PER_TRADE_PCT=1.0,
        TARGET_RISK_PCT=0.02,
    )
    assert s.resolved_risk_per_trade_frac == pytest.approx(0.01, rel=1e-6)


def test_settings_resolved_risk_per_trade_falls_back_to_legacy_target() -> None:
    """When RISK_PER_TRADE_PCT is None we fall back to TARGET_RISK_PCT."""
    from src.utils.config import Settings

    s = Settings(
        OPENROUTER_API_KEY="dummy",
        RISK_PER_TRADE_PCT=None,
        TARGET_RISK_PCT=0.015,
    )
    assert s.resolved_risk_per_trade_frac == pytest.approx(0.015, rel=1e-6)


def test_settings_risk_per_trade_clamped_to_sane_range() -> None:
    """A stray RISK_PER_TRADE_PCT=50 in .env mustn't blow up sizing."""
    from src.utils.config import Settings

    s = Settings(
        OPENROUTER_API_KEY="dummy",
        RISK_PER_TRADE_PCT=50.0,
    )
    # Hard ceiling at 5% (0.05).
    assert s.resolved_risk_per_trade_frac == pytest.approx(0.05, rel=1e-6)


def test_settings_risk_multiplier_for_symbol_resolves_buckets() -> None:
    """BTC/ETH/default risk multipliers resolve per-symbol."""
    from src.utils.config import Settings

    s = Settings(
        OPENROUTER_API_KEY="dummy",
        RISK_PER_TRADE_MULT_BTC=1.0,
        RISK_PER_TRADE_MULT_ETH=0.75,
        RISK_PER_TRADE_MULT_DEFAULT=0.5,
    )
    assert s.risk_multiplier_for_symbol("BTC-PERP") == 1.0
    assert s.risk_multiplier_for_symbol("ETH-PERP") == 0.75
    assert s.risk_multiplier_for_symbol("btc-perp") == 1.0   # case-insensitive
    assert s.risk_multiplier_for_symbol("SOL-PERP") == 0.5
    assert s.risk_multiplier_for_symbol("") == 0.5


# ---- L3_AGGRESSION calibration ---------------------------------------------


@pytest.mark.asyncio
async def test_l3_aggression_conservative_raises_override_threshold() -> None:
    """Conservative mode requires confidence >= 0.75 to override Fast Path."""
    async def _arbiter(briefing: SmartPathBriefing) -> SmartPathVerdict:
        # Smart Path votes close_full but at 0.60 confidence - below
        # the conservative override threshold of 0.75.
        return SmartPathVerdict(
            action="close_full",
            rationale="medium-confidence close",
            confidence=0.60,
        )

    pm = PositionManager(
        config=PositionManagerConfig(
            l3_aggression="conservative",
            enable_smart_path=True,
            first_review_delay_minutes=0.0,
            l3_review_min_interval_minutes=0.0,
            l3_review_max_interval_minutes=0.0,
            l3_review_trigger_price_atr_mult=999.0,
            l3_review_trigger_funding_rate=999.0,
            l3_review_trigger_whale_count=999,
            l3_review_trigger_oi_delta_pct=999.0,
            l3_can_override_fast_path=True,
            enable_daily_dd_guard=False,
            enable_partial_take_profit=False,
            enable_breakeven=False,
            enable_vol_filter=False,
            enable_time_exit=False,
            use_dynamic_atr_tpsl=False,
        ),
        executor=_FakeExecutor(),
        position_arbiter=_arbiter,
    )
    pos = _long_btc(pnl_usd=Decimal("3"))
    review = await pm.review_open_positions(
        account=_account([pos]),
        directive=_directive(),
        decision=_decision_with_atr(_directive(), btc_atr_pct=1.0),
    )
    a = review.actions[0]
    # 0.60 confidence < 0.75 override floor under conservative ->
    # Smart verdict is advisory only, Fast Path wins.
    assert a.action == "hold"
    assert a.source == "fast"


@pytest.mark.asyncio
async def test_l3_aggression_aggressive_lowers_override_threshold() -> None:
    """Aggressive mode lets confidence=0.50 close override the Fast Path."""
    async def _arbiter(briefing: SmartPathBriefing) -> SmartPathVerdict:
        return SmartPathVerdict(
            action="close_full",
            rationale="medium-confidence close",
            confidence=0.50,
        )

    pm = PositionManager(
        config=PositionManagerConfig(
            l3_aggression="aggressive",
            enable_smart_path=True,
            first_review_delay_minutes=0.0,
            l3_review_min_interval_minutes=0.0,
            l3_review_max_interval_minutes=0.0,
            l3_review_trigger_price_atr_mult=999.0,
            l3_review_trigger_funding_rate=999.0,
            l3_review_trigger_whale_count=999,
            l3_review_trigger_oi_delta_pct=999.0,
            l3_can_override_fast_path=True,
            enable_daily_dd_guard=False,
            enable_partial_take_profit=False,
            enable_breakeven=False,
            enable_vol_filter=False,
            enable_time_exit=False,
            use_dynamic_atr_tpsl=False,
        ),
        executor=_FakeExecutor(),
        position_arbiter=_arbiter,
    )
    pos = _long_btc(pnl_usd=Decimal("3"))
    review = await pm.review_open_positions(
        account=_account([pos]),
        directive=_directive(),
        decision=_decision_with_atr(_directive(), btc_atr_pct=1.0),
    )
    a = review.actions[0]
    assert a.action == "close"
    assert a.source == "smart"
    assert a.trigger == "l3_smart_close"


@pytest.mark.asyncio
async def test_l3_aggression_aggressive_scales_partial_fraction_up() -> None:
    """Aggressive mode scales close_partial fraction by ~1.15x."""
    async def _arbiter(briefing: SmartPathBriefing) -> SmartPathVerdict:
        return SmartPathVerdict(
            action="close_partial",
            rationale="scale out 40%",
            confidence=0.70,
            close_fraction=Decimal("0.40"),
        )

    pm = PositionManager(
        config=PositionManagerConfig(
            l3_aggression="aggressive",
            enable_smart_path=True,
            first_review_delay_minutes=0.0,
            l3_review_min_interval_minutes=0.0,
            l3_review_max_interval_minutes=0.0,
            l3_review_trigger_price_atr_mult=999.0,
            l3_review_trigger_funding_rate=999.0,
            l3_review_trigger_whale_count=999,
            l3_review_trigger_oi_delta_pct=999.0,
            l3_can_override_fast_path=True,
            enable_daily_dd_guard=False,
            enable_partial_take_profit=False,
            enable_breakeven=False,
            enable_vol_filter=False,
            enable_time_exit=False,
            use_dynamic_atr_tpsl=False,
        ),
        executor=_FakeExecutor(),
        position_arbiter=_arbiter,
    )
    pos = _long_btc(pnl_usd=Decimal("5"), size_usd=Decimal("1000"))
    review = await pm.review_open_positions(
        account=_account([pos]),
        directive=_directive(),
        decision=_decision_with_atr(_directive(), btc_atr_pct=1.0),
    )
    a = review.actions[0]
    assert a.action == "partial_close"
    # 0.40 base * 1.15 mult = 0.46, applied to 1000 USD = 460 USD.
    assert a.size_usd_to_close is not None
    assert float(a.size_usd_to_close) == pytest.approx(460.0, abs=2.0)


# ---- HOLD-rescue rule -------------------------------------------------------


@pytest.mark.asyncio
async def test_hold_rescue_fires_when_l2_conviction_opposes_long_position() -> None:
    """Fast HOLD + L2 bearish 0.7 conviction on a LONG triggers Smart review."""
    captured: list[SmartPathBriefing] = []

    async def _arbiter(briefing: SmartPathBriefing) -> SmartPathVerdict:
        captured.append(briefing)
        # Confirm the rescue context is wired into the briefing.
        return SmartPathVerdict(
            action="close_full",
            rationale="L2 bearish, exit long",
            confidence=0.75,
        )

    pm = PositionManager(
        config=PositionManagerConfig(
            # Force the position into a clean Fast-Path HOLD (no fast
            # trigger should fire on a +0% / quiet position).
            l3_aggression="balanced",
            enable_smart_path=True,
            first_review_delay_minutes=0.0,
            enable_hold_rescue=True,
            hold_rescue_l2_min=0.65,
            l3_review_min_interval_minutes=0.0,
            l3_review_max_interval_minutes=999.0,
            l3_review_trigger_price_atr_mult=999.0,
            l3_review_trigger_funding_rate=999.0,
            l3_review_trigger_whale_count=999,
            l3_review_trigger_oi_delta_pct=999.0,
            l3_can_override_fast_path=True,
            enable_re_evaluation=False,
            enable_trailing_stop=False,
            enable_daily_dd_guard=False,
            enable_partial_take_profit=False,
            enable_breakeven=False,
            enable_vol_filter=False,
            enable_time_exit=False,
            use_dynamic_atr_tpsl=False,
        ),
        executor=_FakeExecutor(),
        position_arbiter=_arbiter,
    )
    pos = _long_btc(pnl_usd=Decimal("0"))
    decision = _decision_with_l2_conviction(
        _directive(),
        btc_atr_pct=1.0,
        l2_conviction=0.75,
        l2_direction_sign=-1,  # bearish vs a LONG -> rescue
    )
    review = await pm.review_open_positions(
        account=_account([pos]),
        directive=_directive(),
        decision=decision,
    )
    a = review.actions[0]
    assert len(captured) == 1
    assert captured[0].hold_rescue_active is True
    assert captured[0].l2_conviction == pytest.approx(0.75, rel=1e-6)
    assert captured[0].l2_direction_sign == -1
    assert a.action == "close"
    assert a.trigger == "l3_hold_rescue"
    assert a.source == "smart"


@pytest.mark.asyncio
async def test_hold_rescue_does_not_fire_when_l2_agrees_with_position_side() -> None:
    """A bullish L2 + LONG position should NOT trigger HOLD-rescue."""
    fired: list[bool] = []

    async def _arbiter(briefing: SmartPathBriefing) -> SmartPathVerdict:
        fired.append(briefing.hold_rescue_active)
        return SmartPathVerdict(action="hold", confidence=0.0)

    pm = PositionManager(
        config=PositionManagerConfig(
            l3_aggression="balanced",
            enable_smart_path=True,
            first_review_delay_minutes=0.0,
            enable_hold_rescue=True,
            hold_rescue_l2_min=0.65,
            l3_review_min_interval_minutes=0.0,
            l3_review_max_interval_minutes=999.0,  # no forced review
            l3_review_trigger_price_atr_mult=999.0,
            l3_review_trigger_funding_rate=999.0,
            l3_review_trigger_whale_count=999,
            l3_review_trigger_oi_delta_pct=999.0,
            l3_can_override_fast_path=True,
            enable_re_evaluation=False,
            enable_trailing_stop=False,
            enable_daily_dd_guard=False,
            enable_partial_take_profit=False,
            enable_breakeven=False,
            enable_vol_filter=False,
            enable_time_exit=False,
            use_dynamic_atr_tpsl=False,
        ),
        executor=_FakeExecutor(),
        position_arbiter=_arbiter,
    )
    pos = _long_btc(pnl_usd=Decimal("0"))
    # Pre-seed state so the FIRST-review-rule doesn't trigger on its own.
    pm._ensure_state(pos, {})
    pm._states[(pos.symbol, pos.side)].last_l3_check_at = datetime.now(timezone.utc)
    pm._states[(pos.symbol, pos.side)].last_l3_check_price = pos.entry_price

    decision = _decision_with_l2_conviction(
        _directive(),
        btc_atr_pct=1.0,
        l2_conviction=0.75,
        l2_direction_sign=+1,  # bullish vs LONG -> NO rescue
    )
    await pm.review_open_positions(
        account=_account([pos]),
        directive=_directive(),
        decision=decision,
    )
    # No fired gates -> arbiter never invoked.
    assert fired == []


@pytest.mark.asyncio
async def test_hold_rescue_aggressive_rewrites_hold_reply_to_partial_close() -> None:
    """Aggressive mode auto-rewrites a HOLD reply to a defensive partial."""
    async def _arbiter(briefing: SmartPathBriefing) -> SmartPathVerdict:
        # LLM ducks the rescue call by voting HOLD.
        return SmartPathVerdict(
            action="hold",
            rationale="prefer to hold",
            confidence=0.40,
        )

    pm = PositionManager(
        config=PositionManagerConfig(
            l3_aggression="aggressive",
            enable_smart_path=True,
            first_review_delay_minutes=0.0,
            enable_hold_rescue=True,
            hold_rescue_l2_min=0.65,
            l3_review_min_interval_minutes=0.0,
            l3_review_max_interval_minutes=999.0,
            l3_review_trigger_price_atr_mult=999.0,
            l3_review_trigger_funding_rate=999.0,
            l3_review_trigger_whale_count=999,
            l3_review_trigger_oi_delta_pct=999.0,
            l3_can_override_fast_path=True,
            enable_re_evaluation=False,
            enable_trailing_stop=False,
            enable_daily_dd_guard=False,
            enable_partial_take_profit=False,
            enable_breakeven=False,
            enable_vol_filter=False,
            enable_time_exit=False,
            use_dynamic_atr_tpsl=False,
        ),
        executor=_FakeExecutor(),
        position_arbiter=_arbiter,
    )
    pos = _long_btc(pnl_usd=Decimal("0"), size_usd=Decimal("1000"))
    decision = _decision_with_l2_conviction(
        _directive(),
        btc_atr_pct=1.0,
        l2_conviction=0.75,
        l2_direction_sign=-1,
    )
    review = await pm.review_open_positions(
        account=_account([pos]),
        directive=_directive(),
        decision=decision,
    )
    a = review.actions[0]
    # Aggressive rescue safety net -> 25% defensive close, scaled by
    # 1.15x = 0.2875 ~ 287.5 USD on a 1000 USD position.
    assert a.action == "partial_close"
    assert a.trigger == "l3_hold_rescue"
    assert a.size_usd_to_close is not None
    assert float(a.size_usd_to_close) == pytest.approx(287.5, abs=5.0)


@pytest.mark.asyncio
async def test_hold_rescue_balanced_keeps_hold_reply_as_is() -> None:
    """Balanced mode does NOT rewrite a HOLD reply (operator chose not to)."""
    async def _arbiter(briefing: SmartPathBriefing) -> SmartPathVerdict:
        return SmartPathVerdict(
            action="hold",
            rationale="prefer to hold",
            confidence=0.30,
        )

    pm = PositionManager(
        config=PositionManagerConfig(
            l3_aggression="balanced",
            enable_smart_path=True,
            first_review_delay_minutes=0.0,
            enable_hold_rescue=True,
            hold_rescue_l2_min=0.65,
            l3_review_min_interval_minutes=0.0,
            l3_review_max_interval_minutes=999.0,
            l3_review_trigger_price_atr_mult=999.0,
            l3_review_trigger_funding_rate=999.0,
            l3_review_trigger_whale_count=999,
            l3_review_trigger_oi_delta_pct=999.0,
            l3_can_override_fast_path=True,
            enable_re_evaluation=False,
            enable_trailing_stop=False,
            enable_daily_dd_guard=False,
            enable_partial_take_profit=False,
            enable_breakeven=False,
            enable_vol_filter=False,
            enable_time_exit=False,
            use_dynamic_atr_tpsl=False,
        ),
        executor=_FakeExecutor(),
        position_arbiter=_arbiter,
    )
    pos = _long_btc(pnl_usd=Decimal("0"))
    decision = _decision_with_l2_conviction(
        _directive(),
        btc_atr_pct=1.0,
        l2_conviction=0.75,
        l2_direction_sign=-1,
    )
    review = await pm.review_open_positions(
        account=_account([pos]),
        directive=_directive(),
        decision=decision,
    )
    # Balanced respects the HOLD vote.
    assert review.actions[0].action == "hold"


# ---- Fast / Smart Path separation ------------------------------------------


@pytest.mark.asyncio
async def test_smart_path_never_invoked_when_no_gate_fires() -> None:
    """A quiet cycle with no fired gate must NOT invoke the LLM arbiter."""
    calls: list[SmartPathBriefing] = []

    async def _arbiter(briefing: SmartPathBriefing) -> SmartPathVerdict:
        calls.append(briefing)
        return SmartPathVerdict(action="hold", confidence=0.0)

    pm = PositionManager(
        config=PositionManagerConfig(
            l3_aggression="balanced",
            enable_smart_path=True,
            first_review_delay_minutes=0.0,
            enable_hold_rescue=False,
            l3_review_min_interval_minutes=0.0,
            # all gates set super-high so nothing fires this cycle:
            l3_review_max_interval_minutes=999.0,
            l3_review_trigger_price_atr_mult=999.0,
            l3_review_trigger_funding_rate=999.0,
            l3_review_trigger_whale_count=999,
            l3_review_trigger_oi_delta_pct=999.0,
            l3_can_override_fast_path=True,
            enable_re_evaluation=False,
            enable_trailing_stop=False,
            enable_daily_dd_guard=False,
            enable_partial_take_profit=False,
            enable_breakeven=False,
            enable_vol_filter=False,
            enable_time_exit=False,
            use_dynamic_atr_tpsl=False,
        ),
        executor=_FakeExecutor(),
        position_arbiter=_arbiter,
    )
    pos = _long_btc(pnl_usd=Decimal("0"))
    # Pre-seed state to side-step the FIRST-review rule.
    pm._ensure_state(pos, {})
    pm._states[(pos.symbol, pos.side)].last_l3_check_at = datetime.now(timezone.utc)
    pm._states[(pos.symbol, pos.side)].last_l3_check_price = pos.entry_price

    await pm.review_open_positions(
        account=_account([pos]),
        directive=_directive(),
        decision=_decision_with_atr(_directive(), btc_atr_pct=1.0),
    )
    assert calls == []


# ---- Allocation router risk_per_trade integration --------------------------


def test_allocation_router_uses_resolved_risk_per_trade_and_per_asset_mult() -> None:
    """Sizing must reflect equity * effective_risk_pct / (stop * atr_pct).

    We build a real AllocationRouter with a fake executor and verify
    that the breakdown stamped on the ExecutionPlan honours both the
    resolved per-trade risk (1%) and the per-asset multiplier (BTC at
    0.5x = 0.5% effective risk).
    """
    from src.allocation.allocation_router import (
        AllocationConfig,
        AllocationRouter,
    )

    class _FakeExec:
        class _Cfg:
            max_leverage = 5
            symbol = "BTC-PERP"

        config = _Cfg()

        async def get_account_info(self) -> AccountInfo:
            return AccountInfo(
                equity_usd=Decimal("10000"),
                free_margin_usd=Decimal("10000"),
                used_margin_usd=Decimal("0"),
                total_unrealized_pnl_usd=Decimal("0"),
                positions=[],
            )

    cfg = AllocationConfig(
        perp_symbol="BTC-PERP",
        target_risk_pct=0.01,        # operator picked 1%
        risk_multiplier_per_symbol={"BTC": 0.5, "ETH": 1.0},
        risk_multiplier_default=1.0,
        stop_atr_mult=1.5,
        min_atr_pct_for_sizing=0.25,
    )
    router = AllocationRouter(
        executor=_FakeExec(),
        position_manager=_make_manager(),
        config=cfg,
    )

    directive = _directive(action="risk_on", side="long", conviction=1.0)
    decision = _decision_with_atr(directive, btc_atr_pct=2.0)
    account = AccountInfo(
        equity_usd=Decimal("10000"),
        free_margin_usd=Decimal("10000"),
        used_margin_usd=Decimal("0"),
        total_unrealized_pnl_usd=Decimal("0"),
        positions=[],
    )
    sizing = router._compute_size(directive, account, decision)
    # equity * effective_risk / (stop * atr%) ; intensity=1.0 ; no DD.
    # equity = 10000 ; effective_risk = 0.01 * 0.5 = 0.005 ;
    # stop_dist = 1.5 * (2.0 / 100) = 0.03 ;
    # vol_target = 10000 * 0.005 / 0.03 = 1666.67
    assert sizing["risk_multiplier"] == 0.5
    assert sizing["effective_risk_pct"] == pytest.approx(0.005, rel=1e-6)
    assert sizing["vol_target_size_usd"] == pytest.approx(1666.67, abs=0.5)
    assert float(sizing["size_usd"]) == pytest.approx(1666.67, abs=2.0)


# ===========================================================================
# Polish: hard cooldown floor, first-review delay, aggression cooldown,
# HOLD-must-be-earned, rescue any-direction mode, LLM telemetry
# ===========================================================================


def test_effective_min_cooldown_enforces_hard_floor() -> None:
    """Even with min=1 in .env, the floor stays at 25 min (balanced)."""
    from src.execution.position_manager import _HARD_COOLDOWN_FLOOR_MINUTES

    pm = PositionManager(
        config=PositionManagerConfig(
            l3_aggression="balanced",
            l3_review_min_interval_minutes=1.0,  # operator typo
        ),
        executor=_FakeExecutor(),
    )
    assert pm._effective_min_cooldown_minutes() == _HARD_COOLDOWN_FLOOR_MINUTES


def test_effective_min_cooldown_conservative_extends_floor() -> None:
    """Conservative mode adds +15 min on top of the configured cooldown."""
    from src.execution.position_manager import _HARD_COOLDOWN_FLOOR_MINUTES

    pm = PositionManager(
        config=PositionManagerConfig(
            l3_aggression="conservative",
            l3_review_min_interval_minutes=30.0,
        ),
        executor=_FakeExecutor(),
    )
    # 30 + 15 = 45 (above the 25 floor)
    assert pm._effective_min_cooldown_minutes() == 45.0
    # Sanity: hard floor still wins when the configured value is tiny.
    pm.config.l3_review_min_interval_minutes = 1.0
    # 1 + 15 = 16 < floor=25 -> floor wins.
    assert pm._effective_min_cooldown_minutes() == _HARD_COOLDOWN_FLOOR_MINUTES


def test_effective_min_cooldown_aggressive_shortens_but_respects_floor() -> None:
    """Aggressive subtracts 10 min but never below the hard floor."""
    from src.execution.position_manager import _HARD_COOLDOWN_FLOOR_MINUTES

    pm = PositionManager(
        config=PositionManagerConfig(
            l3_aggression="aggressive",
            l3_review_min_interval_minutes=40.0,
        ),
        executor=_FakeExecutor(),
    )
    # 40 - 10 = 30 (above floor) -> 30
    assert pm._effective_min_cooldown_minutes() == 30.0
    # 30 - 10 = 20 < floor -> floor wins
    pm.config.l3_review_min_interval_minutes = 30.0
    assert pm._effective_min_cooldown_minutes() == _HARD_COOLDOWN_FLOOR_MINUTES


@pytest.mark.asyncio
async def test_first_review_delay_blocks_brand_new_positions() -> None:
    """A brand-new position must NOT be reviewed inside the delay window."""
    calls: list[Any] = []

    async def _arbiter(briefing: SmartPathBriefing) -> SmartPathVerdict:
        calls.append(briefing)
        return SmartPathVerdict(action="hold", confidence=0.0)

    pm = PositionManager(
        config=PositionManagerConfig(
            l3_aggression="balanced",
            enable_smart_path=True,
            first_review_delay_minutes=10.0,  # 10-min grace period
            l3_review_min_interval_minutes=0.0,
            l3_review_max_interval_minutes=999.0,
            l3_review_trigger_price_atr_mult=999.0,
            l3_review_trigger_funding_rate=999.0,
            l3_review_trigger_whale_count=999,
            l3_review_trigger_oi_delta_pct=999.0,
            l3_can_override_fast_path=True,
            enable_daily_dd_guard=False,
            enable_partial_take_profit=False,
            enable_breakeven=False,
            enable_vol_filter=False,
            enable_time_exit=False,
            use_dynamic_atr_tpsl=False,
        ),
        executor=_FakeExecutor(),
        position_arbiter=_arbiter,
    )
    pos = _long_btc(pnl_usd=Decimal("0"))
    await pm.review_open_positions(
        account=_account([pos]),
        directive=_directive(),
        decision=_decision_with_atr(_directive(), btc_atr_pct=1.0),
    )
    assert calls == []
    assert pm.stats.smart_skipped_first_review_delay == 1


@pytest.mark.asyncio
async def test_first_review_fires_after_position_ages_past_delay() -> None:
    """Once position age >= delay, first review fires."""
    calls: list[Any] = []

    async def _arbiter(briefing: SmartPathBriefing) -> SmartPathVerdict:
        calls.append(briefing)
        return SmartPathVerdict(action="hold", confidence=0.0)

    pm = PositionManager(
        config=PositionManagerConfig(
            l3_aggression="balanced",
            enable_smart_path=True,
            first_review_delay_minutes=10.0,
            l3_review_min_interval_minutes=0.0,
            l3_review_max_interval_minutes=999.0,
            l3_review_trigger_price_atr_mult=999.0,
            l3_review_trigger_funding_rate=999.0,
            l3_review_trigger_whale_count=999,
            l3_review_trigger_oi_delta_pct=999.0,
            enable_daily_dd_guard=False,
            enable_partial_take_profit=False,
            enable_breakeven=False,
            enable_vol_filter=False,
            enable_time_exit=False,
            use_dynamic_atr_tpsl=False,
        ),
        executor=_FakeExecutor(),
        position_arbiter=_arbiter,
    )
    pos = _long_btc(pnl_usd=Decimal("0"))
    state = pm._ensure_state(pos, {})
    # Backdate the open so the position is now 15 min old.
    state.opened_at = datetime.now(timezone.utc) - _td(minutes=15)
    await pm.review_open_positions(
        account=_account([pos]),
        directive=_directive(),
        decision=_decision_with_atr(_directive(), btc_atr_pct=1.0),
    )
    assert len(calls) == 1
    assert pm.stats.smart_skipped_first_review_delay == 0


@pytest.mark.asyncio
async def test_hold_rescue_any_direction_fires_on_same_side_under_aggressive() -> None:
    """Aggressive direction_mode='any' fires rescue even on same-side L2."""
    calls: list[SmartPathBriefing] = []

    async def _arbiter(briefing: SmartPathBriefing) -> SmartPathVerdict:
        calls.append(briefing)
        return SmartPathVerdict(
            action="raise_target",
            rationale="L2 confirms continuation - widen target",
            confidence=0.70,
        )

    pm = PositionManager(
        config=PositionManagerConfig(
            l3_aggression="aggressive",
            enable_smart_path=True,
            enable_hold_rescue=True,
            hold_rescue_l2_min=0.65,
            hold_rescue_direction_mode="any",  # aggressive default
            first_review_delay_minutes=0.0,
            l3_review_min_interval_minutes=0.0,
            l3_review_max_interval_minutes=999.0,
            l3_review_trigger_price_atr_mult=999.0,
            l3_review_trigger_funding_rate=999.0,
            l3_review_trigger_whale_count=999,
            l3_review_trigger_oi_delta_pct=999.0,
            l3_can_override_fast_path=True,
            enable_re_evaluation=False,
            enable_trailing_stop=False,
            enable_daily_dd_guard=False,
            enable_partial_take_profit=False,
            enable_breakeven=False,
            enable_vol_filter=False,
            enable_time_exit=False,
            use_dynamic_atr_tpsl=False,
        ),
        executor=_FakeExecutor(),
        position_arbiter=_arbiter,
    )
    pos = _long_btc(pnl_usd=Decimal("0"))
    # Pre-age + record a prior L3 check so we're well past first-review.
    state = pm._ensure_state(pos, {})
    state.last_l3_check_at = datetime.now(timezone.utc) - _td(minutes=30)
    state.last_l3_check_price = pos.entry_price
    decision = _decision_with_l2_conviction(
        _directive(),
        btc_atr_pct=1.0,
        l2_conviction=0.80,
        l2_direction_sign=+1,  # bullish AGREES with LONG -> rescue under "any"
    )
    await pm.review_open_positions(
        account=_account([pos]),
        directive=_directive(),
        decision=decision,
    )
    assert len(calls) == 1
    assert calls[0].hold_rescue_active is True
    # Stamped via the SAME direction stance string.
    assert "SAME direction" in next(
        r for r in calls[0].invocation_reasons if r.startswith("HOLD-rescue:")
    )


@pytest.mark.asyncio
async def test_hold_rescue_cooldown_suppresses_repeat_invocation() -> None:
    """Two cycles in a row with the same trigger -> only first fires."""
    calls: list[Any] = []

    async def _arbiter(briefing: SmartPathBriefing) -> SmartPathVerdict:
        calls.append(briefing)
        return SmartPathVerdict(action="hold", confidence=0.30)

    pm = PositionManager(
        config=PositionManagerConfig(
            l3_aggression="balanced",
            enable_smart_path=True,
            enable_hold_rescue=True,
            hold_rescue_l2_min=0.65,
            hold_rescue_direction_mode="opposite",
            hold_rescue_cooldown_minutes=30.0,
            first_review_delay_minutes=0.0,
            l3_review_min_interval_minutes=0.0,
            l3_review_max_interval_minutes=999.0,
            l3_review_trigger_price_atr_mult=999.0,
            l3_review_trigger_funding_rate=999.0,
            l3_review_trigger_whale_count=999,
            l3_review_trigger_oi_delta_pct=999.0,
            l3_can_override_fast_path=True,
            enable_re_evaluation=False,
            enable_trailing_stop=False,
            enable_daily_dd_guard=False,
            enable_partial_take_profit=False,
            enable_breakeven=False,
            enable_vol_filter=False,
            enable_time_exit=False,
            use_dynamic_atr_tpsl=False,
        ),
        executor=_FakeExecutor(),
        position_arbiter=_arbiter,
    )
    pos = _long_btc(pnl_usd=Decimal("0"))
    state = pm._ensure_state(pos, {})
    state.last_l3_check_at = datetime.now(timezone.utc) - _td(minutes=30)
    state.last_l3_check_price = pos.entry_price
    decision = _decision_with_l2_conviction(
        _directive(),
        btc_atr_pct=1.0,
        l2_conviction=0.80,
        l2_direction_sign=-1,
    )
    # Cycle 1: rescue fires.
    await pm.review_open_positions(
        account=_account([pos]),
        directive=_directive(),
        decision=decision,
    )
    assert len(calls) == 1
    assert pm.stats.hold_rescues_fired == 1
    # Cycle 2: same trigger conditions; rescue cooldown should suppress.
    await pm.review_open_positions(
        account=_account([pos]),
        directive=_directive(),
        decision=decision,
    )
    assert len(calls) == 1  # no new invocation
    assert pm.stats.hold_rescues_suppressed >= 1


@pytest.mark.asyncio
async def test_hold_must_be_earned_re_audits_long_held_position() -> None:
    """A position HOLDing for > N min forces a fresh review."""
    calls: list[SmartPathBriefing] = []

    async def _arbiter(briefing: SmartPathBriefing) -> SmartPathVerdict:
        calls.append(briefing)
        return SmartPathVerdict(action="close_full", confidence=0.70)

    pm = PositionManager(
        config=PositionManagerConfig(
            l3_aggression="balanced",
            enable_smart_path=True,
            hold_must_be_earned=True,
            hold_must_be_earned_minutes=45.0,
            first_review_delay_minutes=0.0,
            l3_review_min_interval_minutes=0.0,
            l3_review_max_interval_minutes=999.0,
            l3_review_trigger_price_atr_mult=999.0,
            l3_review_trigger_funding_rate=999.0,
            l3_review_trigger_whale_count=999,
            l3_review_trigger_oi_delta_pct=999.0,
            l3_can_override_fast_path=True,
            enable_re_evaluation=False,
            enable_trailing_stop=False,
            enable_daily_dd_guard=False,
            enable_partial_take_profit=False,
            enable_breakeven=False,
            enable_vol_filter=False,
            enable_time_exit=False,
            use_dynamic_atr_tpsl=False,
        ),
        executor=_FakeExecutor(),
        position_arbiter=_arbiter,
    )
    pos = _long_btc(pnl_usd=Decimal("0"))
    # Position is 60 min old, last L3 check was 30 min ago (>= floor 25).
    state = pm._ensure_state(pos, {})
    state.opened_at = datetime.now(timezone.utc) - _td(minutes=60)
    state.last_l3_check_at = datetime.now(timezone.utc) - _td(minutes=30)
    state.last_l3_check_price = pos.entry_price
    state.last_smart_verdict = SmartPathVerdict(action="close_partial", confidence=0.6)
    await pm.review_open_positions(
        account=_account([pos]),
        directive=_directive(),
        decision=_decision_with_atr(_directive(), btc_atr_pct=1.0),
    )
    assert len(calls) == 1
    assert any(
        r.startswith("HOLD-must-be-earned") for r in calls[0].invocation_reasons
    )


@pytest.mark.asyncio
async def test_hold_must_be_earned_suppressed_when_last_verdict_was_hold() -> None:
    """Avoid hitting the LLM with the same answer twice in a row."""
    calls: list[Any] = []

    async def _arbiter(briefing: SmartPathBriefing) -> SmartPathVerdict:
        calls.append(briefing)
        return SmartPathVerdict(action="hold", confidence=0.50)

    pm = PositionManager(
        config=PositionManagerConfig(
            l3_aggression="balanced",
            enable_smart_path=True,
            hold_must_be_earned=True,
            hold_must_be_earned_minutes=45.0,
            first_review_delay_minutes=0.0,
            l3_review_min_interval_minutes=0.0,
            l3_review_max_interval_minutes=999.0,
            l3_review_trigger_price_atr_mult=999.0,
            l3_review_trigger_funding_rate=999.0,
            l3_review_trigger_whale_count=999,
            l3_review_trigger_oi_delta_pct=999.0,
            l3_can_override_fast_path=False,  # advisory only
            enable_re_evaluation=False,
            enable_trailing_stop=False,
            enable_daily_dd_guard=False,
            enable_partial_take_profit=False,
            enable_breakeven=False,
            enable_vol_filter=False,
            enable_time_exit=False,
            use_dynamic_atr_tpsl=False,
        ),
        executor=_FakeExecutor(),
        position_arbiter=_arbiter,
    )
    pos = _long_btc(pnl_usd=Decimal("0"))
    state = pm._ensure_state(pos, {})
    state.opened_at = datetime.now(timezone.utc) - _td(minutes=60)
    state.last_l3_check_at = datetime.now(timezone.utc) - _td(minutes=30)
    state.last_l3_check_price = pos.entry_price
    # Last smart verdict was already a HOLD -> rule must suppress.
    state.last_smart_verdict = SmartPathVerdict(action="hold", confidence=0.6)
    await pm.review_open_positions(
        account=_account([pos]),
        directive=_directive(),
        decision=_decision_with_atr(_directive(), btc_atr_pct=1.0),
    )
    assert calls == []  # gate didn't fire


@pytest.mark.asyncio
async def test_llm_call_stats_track_invocations_and_skips() -> None:
    """Smart-Path telemetry must count invocations and skip reasons."""
    async def _arbiter(briefing: SmartPathBriefing) -> SmartPathVerdict:
        return SmartPathVerdict(action="hold", confidence=0.20)

    pm = PositionManager(
        config=PositionManagerConfig(
            l3_aggression="balanced",
            enable_smart_path=True,
            first_review_delay_minutes=10.0,
            l3_review_min_interval_minutes=0.0,
            l3_review_max_interval_minutes=999.0,
            l3_review_trigger_price_atr_mult=999.0,
            l3_review_trigger_funding_rate=999.0,
            l3_review_trigger_whale_count=999,
            l3_review_trigger_oi_delta_pct=999.0,
            l3_can_override_fast_path=True,
            enable_daily_dd_guard=False,
            enable_partial_take_profit=False,
            enable_breakeven=False,
            enable_vol_filter=False,
            enable_time_exit=False,
            use_dynamic_atr_tpsl=False,
        ),
        executor=_FakeExecutor(),
        position_arbiter=_arbiter,
    )
    pos = _long_btc(pnl_usd=Decimal("0"))
    # Cycle 1: brand-new position -> blocked by first_review_delay.
    await pm.review_open_positions(
        account=_account([pos]),
        directive=_directive(),
        decision=_decision_with_atr(_directive(), btc_atr_pct=1.0),
    )
    assert pm.stats.cycles_total == 1
    assert pm.stats.cycles_with_positions == 1
    assert pm.stats.smart_invocations == 0
    assert pm.stats.smart_skipped_first_review_delay == 1
    # Cycle 2: age the position past the delay; first review fires.
    pm._states[(pos.symbol, pos.side)].opened_at = (
        datetime.now(timezone.utc) - _td(minutes=15)
    )
    await pm.review_open_positions(
        account=_account([pos]),
        directive=_directive(),
        decision=_decision_with_atr(_directive(), btc_atr_pct=1.0),
    )
    assert pm.stats.smart_invocations == 1
    assert pm.stats.cycles_total == 2
    assert pm.stats.llm_call_rate == pytest.approx(0.5, rel=1e-6)


@pytest.mark.asyncio
async def test_hold_taxonomy_classifies_default_vs_deliberate_holds() -> None:
    """Stats must distinguish "no gate" HOLDs from "L3 said HOLD" HOLDs."""
    pm = PositionManager(
        config=PositionManagerConfig(
            l3_aggression="balanced",
            enable_smart_path=False,
            first_review_delay_minutes=0.0,
            enable_re_evaluation=False,
            enable_trailing_stop=False,
            enable_daily_dd_guard=False,
            enable_partial_take_profit=False,
            enable_breakeven=False,
            enable_vol_filter=False,
            enable_time_exit=False,
            use_dynamic_atr_tpsl=False,
        ),
        executor=_FakeExecutor(),
    )
    pos = _long_btc(pnl_usd=Decimal("0"))
    await pm.review_open_positions(
        account=_account([pos]),
        directive=_directive(),
        decision=_decision_with_atr(_directive(), btc_atr_pct=1.0),
    )
    # No Smart Path -> the HOLD is "default".
    assert pm.stats.holds_default == 1
    assert pm.stats.holds_deliberate == 0
    assert pm.stats.holds_rescue_overridden == 0


def test_aggression_auto_wires_hold_rescue_under_from_settings() -> None:
    """Settings(L3_AGGRESSION=aggressive) auto-enables HOLD-rescue + any-dir."""
    from src.utils.config import Settings

    s = Settings(
        OPENROUTER_API_KEY="dummy",
        L3_AGGRESSION="aggressive",
    )
    pm = PositionManager.from_settings(s)
    assert pm.config.l3_aggression == "aggressive"
    assert pm.config.enable_hold_rescue is True
    assert pm.config.hold_rescue_direction_mode == "any"
    assert pm.config.hold_must_be_earned is True

    # Balanced should NOT auto-enable rescue.
    s_bal = Settings(OPENROUTER_API_KEY="dummy", L3_AGGRESSION="balanced")
    pm_bal = PositionManager.from_settings(s_bal)
    assert pm_bal.config.enable_hold_rescue is False
    assert pm_bal.config.hold_rescue_direction_mode == "opposite"
    assert pm_bal.config.hold_must_be_earned is False


# ===========================================================================
# Day-6+ polish: global LLM budget, veto-only conservative, HOLD
# classification ("HOLD must be earned"), multi-trigger conservative
# ===========================================================================
#
# Four orthogonal hardening features:
#   1. Global LLM budget (_LLMBudget): caps AGGREGATE Smart-Path call
#      volume per cycle + per rolling hour, regardless of per-position
#      cooldowns. The single most impactful "stricter cooldown".
#   2. Veto-only under conservative: LLM cannot UPGRADE a Fast-Path
#      HOLD into a close - only allowed to act as a brake.
#   3. HOLD classification in Fast Path: every HOLD carries a positive
#      justification (hold_trend_intact / hold_ranging / hold_low_
#      conviction_profitable / hold_no_data) or is flagged as
#      hold_unearned. Makes "HOLD must be earned" a principle, not a
#      rescue.
#   4. Multi-trigger AND requirement under conservative: Smart Path
#      needs >= 2 corroborating gates on the same position.


# ---- Global LLM budget -----------------------------------------------------


def test_llm_budget_per_cycle_limit_drops_lowest_priority() -> None:
    """The per-cycle budget keeps the highest-priority candidates."""
    from src.execution.position_manager import (
        _LLMBudget,
        _smart_reason_priority,
    )

    budget = _LLMBudget(max_per_cycle=2, max_per_hour=99)
    budget.reset_cycle()
    now = datetime.now(timezone.utc)

    # Three sets of reasons of decreasing priority:
    #  - rescue (priority 1)
    #  - close-audit (priority 2)
    #  - price-move only (priority 5)
    rescue = ["HOLD-rescue: L2 strongly disagrees"]
    close_audit = ["fast-path 'take_profit' wants to close"]
    price_only = ["price moved 1.80x ATR since last L3 (60100 -> 60500)"]

    # The reasons map to the documented priority ladder.
    assert _smart_reason_priority(rescue) == 1
    assert _smart_reason_priority(close_audit) == 2
    assert _smart_reason_priority(price_only) == 5

    # Two slots available -> first two records succeed.
    ok, _ = budget.can_invoke(now); assert ok
    budget.record(now)
    ok, _ = budget.can_invoke(now); assert ok
    budget.record(now)
    # Third record denied with per_cycle reason.
    ok, why = budget.can_invoke(now)
    assert ok is False
    assert why == "per_cycle"


def test_llm_budget_hourly_rolling_window() -> None:
    """The rolling-hour window evicts entries older than 1h."""
    from src.execution.position_manager import _LLMBudget

    budget = _LLMBudget(max_per_cycle=99, max_per_hour=2)
    now = datetime.now(timezone.utc)
    # Pre-seed two old invocations that should age out of the window.
    budget.invocations = [now - _td(hours=2), now - _td(hours=1, minutes=5)]
    ok, _ = budget.can_invoke(now)
    assert ok is True  # both pre-seeded entries are > 1h ago
    budget.record(now)
    budget.record(now)
    # Now in-window: both freshly recorded -> next call denied.
    ok, why = budget.can_invoke(now)
    assert ok is False
    assert why == "per_hour"


@pytest.mark.asyncio
async def test_llm_budget_per_cycle_caps_multi_position_storm() -> None:
    """3 positions all wanting Smart Path get throttled to budget=2."""
    calls: list[SmartPathBriefing] = []

    async def _arbiter(briefing: SmartPathBriefing) -> SmartPathVerdict:
        calls.append(briefing)
        return SmartPathVerdict(action="hold", confidence=0.0)

    pm = PositionManager(
        config=PositionManagerConfig(
            l3_aggression="balanced",
            enable_smart_path=True,
            llm_max_per_cycle=2,            # cap = 2 calls
            llm_max_per_hour=99,
            first_review_delay_minutes=0.0,
            l3_review_min_interval_minutes=0.0,
            l3_review_max_interval_minutes=0.0,   # always forces periodic
            l3_review_trigger_price_atr_mult=999.0,
            l3_review_trigger_funding_rate=999.0,
            l3_review_trigger_whale_count=999,
            l3_review_trigger_oi_delta_pct=999.0,
            l3_can_override_fast_path=True,
            enable_re_evaluation=False,
            enable_trailing_stop=False,
            enable_daily_dd_guard=False,
            enable_partial_take_profit=False,
            enable_breakeven=False,
            enable_vol_filter=False,
            enable_time_exit=False,
            use_dynamic_atr_tpsl=False,
        ),
        executor=_FakeExecutor(),
        position_arbiter=_arbiter,
    )
    # Three positions on three symbols. We synthesise them by giving
    # the manager three Position objects of different sides + symbols.
    pos1 = _long_btc(pnl_usd=Decimal("0"), size_usd=Decimal("500"))
    pos2 = Position(
        symbol="ETH-PERP", side="long",
        size_usd=Decimal("500"), entry_price=Decimal("3000"),
        mark_price=Decimal("3000"), leverage=Decimal("3"),
        unrealized_pnl_usd=Decimal("0"),
    )
    pos3 = Position(
        symbol="SOL-PERP", side="long",
        size_usd=Decimal("500"), entry_price=Decimal("100"),
        mark_price=Decimal("100"), leverage=Decimal("3"),
        unrealized_pnl_usd=Decimal("0"),
    )
    # Backdate the open so first-review gate fires.
    review = await pm.review_open_positions(
        account=_account([pos1, pos2, pos3]),
        directive=_directive(),
        decision=_decision_with_atr(_directive(), btc_atr_pct=1.0),
    )
    # Budget was 2/cycle and 3 candidates fired -> only 2 LLM round-
    # trips actually happen.
    assert len(calls) == 2
    assert pm.stats.smart_invocations == 2
    assert pm.stats.smart_skipped_per_cycle_budget == 1


@pytest.mark.asyncio
async def test_llm_budget_per_cycle_prioritises_rescue_over_first_review() -> None:
    """When budget is tight, rescue beats first-review priority."""
    calls: list[SmartPathBriefing] = []

    async def _arbiter(briefing: SmartPathBriefing) -> SmartPathVerdict:
        calls.append(briefing)
        return SmartPathVerdict(action="hold", confidence=0.0)

    pm = PositionManager(
        config=PositionManagerConfig(
            l3_aggression="balanced",
            enable_smart_path=True,
            llm_max_per_cycle=1,            # only ONE LLM call this cycle
            llm_max_per_hour=99,
            enable_hold_rescue=True,
            hold_rescue_l2_min=0.65,
            first_review_delay_minutes=0.0,
            l3_review_min_interval_minutes=0.0,
            l3_review_max_interval_minutes=999.0,
            l3_review_trigger_price_atr_mult=999.0,
            l3_review_trigger_funding_rate=999.0,
            l3_review_trigger_whale_count=999,
            l3_review_trigger_oi_delta_pct=999.0,
            l3_can_override_fast_path=True,
            enable_re_evaluation=False,
            enable_trailing_stop=False,
            enable_daily_dd_guard=False,
            enable_partial_take_profit=False,
            enable_breakeven=False,
            enable_vol_filter=False,
            enable_time_exit=False,
            use_dynamic_atr_tpsl=False,
        ),
        executor=_FakeExecutor(),
        position_arbiter=_arbiter,
    )
    # Two positions:
    #  * BTC: first-review only (low priority)
    #  * ETH: rescue trigger (highest priority)
    pos_btc = _long_btc(pnl_usd=Decimal("0"), size_usd=Decimal("500"))
    pos_eth = Position(
        symbol="ETH-PERP", side="long",
        size_usd=Decimal("500"), entry_price=Decimal("3000"),
        mark_price=Decimal("3000"), leverage=Decimal("3"),
        unrealized_pnl_usd=Decimal("0"),
    )
    # ETH carries the L2 rescue signal; BTC has no L2 signal.
    decision = _decision_with_l2_conviction(
        _directive(),
        btc_atr_pct=1.0,
        l2_conviction=0.80,
        l2_direction_sign=-1,
    )
    await pm.review_open_positions(
        account=_account([pos_btc, pos_eth]),
        directive=_directive(),
        decision=decision,
    )
    # Budget = 1 -> rescue won the slot, first-review for BTC was
    # dropped. The single surviving call is ETH-PERP with rescue
    # context (aggregate L2 falls back since we don't supply per-
    # symbol attribution; aggregate has direction -1 which DOES
    # oppose a LONG, so the gate fires on both - but rescue wins).
    assert len(calls) == 1
    assert calls[0].hold_rescue_active is True
    assert pm.stats.smart_skipped_per_cycle_budget == 1


@pytest.mark.asyncio
async def test_llm_budget_hourly_blocks_all_calls_when_exhausted() -> None:
    """Once the hourly budget is spent, every Smart-Path call is skipped."""
    calls: list[Any] = []

    async def _arbiter(briefing: SmartPathBriefing) -> SmartPathVerdict:
        calls.append(briefing)
        return SmartPathVerdict(action="hold", confidence=0.0)

    pm = PositionManager(
        config=PositionManagerConfig(
            l3_aggression="balanced",
            enable_smart_path=True,
            llm_max_per_cycle=99,
            llm_max_per_hour=1,             # only 1 call per hour
            first_review_delay_minutes=0.0,
            l3_review_min_interval_minutes=0.0,
            l3_review_max_interval_minutes=999.0,
            l3_review_trigger_price_atr_mult=999.0,
            l3_review_trigger_funding_rate=999.0,
            l3_review_trigger_whale_count=999,
            l3_review_trigger_oi_delta_pct=999.0,
            l3_can_override_fast_path=True,
            enable_re_evaluation=False,
            enable_trailing_stop=False,
            enable_daily_dd_guard=False,
            enable_partial_take_profit=False,
            enable_breakeven=False,
            enable_vol_filter=False,
            enable_time_exit=False,
            use_dynamic_atr_tpsl=False,
        ),
        executor=_FakeExecutor(),
        position_arbiter=_arbiter,
    )
    # Pre-record one invocation in the budget so it starts exhausted.
    pm.llm_budget.invocations.append(datetime.now(timezone.utc))
    pos = _long_btc(pnl_usd=Decimal("0"))
    state = pm._ensure_state(pos, {})
    state.opened_at = datetime.now(timezone.utc) - _td(minutes=15)
    await pm.review_open_positions(
        account=_account([pos]),
        directive=_directive(),
        decision=_decision_with_atr(_directive(), btc_atr_pct=1.0),
    )
    assert calls == []
    assert pm.stats.smart_skipped_global_budget == 1


# ---- Veto-only under conservative ------------------------------------------


@pytest.mark.asyncio
async def test_conservative_veto_only_blocks_positive_override() -> None:
    """Conservative + veto-only: smart cannot UPGRADE a HOLD to a close."""
    async def _arbiter(briefing: SmartPathBriefing) -> SmartPathVerdict:
        # High-confidence close - would override under conservative
        # even at the 0.75 floor, but veto-only blocks it because
        # Fast Path was HOLD.
        return SmartPathVerdict(
            action="close_full",
            rationale="strong on-chain reversal",
            confidence=0.85,
        )

    pm = PositionManager(
        config=PositionManagerConfig(
            l3_aggression="conservative",
            l3_conservative_veto_only=True,
            enable_smart_path=True,
            first_review_delay_minutes=0.0,
            l3_review_min_interval_minutes=0.0,
            l3_review_max_interval_minutes=0.0,
            l3_review_trigger_price_atr_mult=999.0,
            l3_review_trigger_funding_rate=999.0,
            l3_review_trigger_whale_count=999,
            l3_review_trigger_oi_delta_pct=999.0,
            l3_can_override_fast_path=True,
            enable_re_evaluation=False,
            enable_trailing_stop=False,
            enable_daily_dd_guard=False,
            enable_partial_take_profit=False,
            enable_breakeven=False,
            enable_vol_filter=False,
            enable_time_exit=False,
            use_dynamic_atr_tpsl=False,
        ),
        executor=_FakeExecutor(),
        position_arbiter=_arbiter,
    )
    pos = _long_btc(pnl_usd=Decimal("0"))
    review = await pm.review_open_positions(
        account=_account([pos]),
        directive=_directive(),
        decision=_decision_with_atr(_directive(), btc_atr_pct=1.0),
    )
    a = review.actions[0]
    # The LLM voted close at 0.85 confidence but conservative veto-
    # only mode kept the Fast-Path HOLD. The advisory snippet still
    # surfaces what the LLM said.
    assert a.action == "hold"
    assert a.source == "fast"
    assert a.smart_snippet is not None
    assert "[BLOCKED veto-only]" in a.smart_snippet
    assert pm.stats.smart_blocked_positive_override == 1


@pytest.mark.asyncio
async def test_conservative_veto_only_still_allows_veto_of_close() -> None:
    """Conservative + veto-only still allows hold -> downgrades a close."""
    async def _arbiter(briefing: SmartPathBriefing) -> SmartPathVerdict:
        # Smart says HOLD with high confidence - veto-only allows
        # this because the LLM is BRAKING the Fast Path, not
        # accelerating.
        return SmartPathVerdict(
            action="hold",
            rationale="don't close yet, signals are still mixed",
            confidence=0.85,
        )

    pm = PositionManager(
        config=PositionManagerConfig(
            l3_aggression="conservative",
            l3_conservative_veto_only=True,
            enable_smart_path=True,
            first_review_delay_minutes=0.0,
            stop_loss_pct=Decimal("0.01"),  # SL at -1%
            l3_review_min_interval_minutes=0.0,
            l3_review_max_interval_minutes=0.0,
            l3_review_trigger_price_atr_mult=999.0,
            l3_review_trigger_funding_rate=999.0,
            l3_review_trigger_whale_count=999,
            l3_review_trigger_oi_delta_pct=999.0,
            l3_can_override_fast_path=True,
            enable_re_evaluation=False,
            enable_trailing_stop=False,
            enable_daily_dd_guard=False,
            enable_partial_take_profit=False,
            enable_breakeven=False,
            enable_vol_filter=False,
            enable_time_exit=False,
            use_dynamic_atr_tpsl=False,
        ),
        executor=_FakeExecutor(),
        position_arbiter=_arbiter,
    )
    # Position losing 1.5% -> Fast Path wants stop_loss close.
    pos = _long_btc(pnl_usd=Decimal("-9"))  # -1.5% on 600
    review = await pm.review_open_positions(
        account=_account([pos]),
        directive=_directive(),
        decision=_decision_with_atr(_directive(), btc_atr_pct=1.0),
    )
    a = review.actions[0]
    # The veto wins: Smart says hold at 0.85 > 0.75 threshold,
    # downgrades the Fast-Path stop_loss close to a HOLD.
    assert a.action == "hold"
    assert a.source == "smart"
    assert a.trigger == "l3_smart_hold"
    # NOT counted as a positive override (the LLM downgraded an
    # action, didn't upgrade a hold).
    assert pm.stats.smart_blocked_positive_override == 0


@pytest.mark.asyncio
async def test_conservative_veto_only_disabled_allows_positive_override() -> None:
    """With veto-only OFF, conservative still allows positive overrides."""
    async def _arbiter(briefing: SmartPathBriefing) -> SmartPathVerdict:
        return SmartPathVerdict(
            action="close_full",
            rationale="strong reversal",
            confidence=0.85,
        )

    pm = PositionManager(
        config=PositionManagerConfig(
            l3_aggression="conservative",
            l3_conservative_veto_only=False,  # explicitly disabled
            enable_smart_path=True,
            first_review_delay_minutes=0.0,
            l3_review_min_interval_minutes=0.0,
            l3_review_max_interval_minutes=0.0,
            l3_review_trigger_price_atr_mult=999.0,
            l3_review_trigger_funding_rate=999.0,
            l3_review_trigger_whale_count=999,
            l3_review_trigger_oi_delta_pct=999.0,
            l3_can_override_fast_path=True,
            enable_re_evaluation=False,
            enable_trailing_stop=False,
            enable_daily_dd_guard=False,
            enable_partial_take_profit=False,
            enable_breakeven=False,
            enable_vol_filter=False,
            enable_time_exit=False,
            use_dynamic_atr_tpsl=False,
        ),
        executor=_FakeExecutor(),
        position_arbiter=_arbiter,
    )
    pos = _long_btc(pnl_usd=Decimal("0"))
    review = await pm.review_open_positions(
        account=_account([pos]),
        directive=_directive(),
        decision=_decision_with_atr(_directive(), btc_atr_pct=1.0),
    )
    a = review.actions[0]
    assert a.action == "close"
    assert a.source == "smart"
    assert pm.stats.smart_blocked_positive_override == 0


def test_conservative_mode_auto_wires_veto_only_and_multi_trigger() -> None:
    """Settings(L3_AGGRESSION=conservative) auto-enables both hardening flags."""
    from src.utils.config import Settings

    s = Settings(OPENROUTER_API_KEY="dummy", L3_AGGRESSION="conservative")
    pm = PositionManager.from_settings(s)
    assert pm.config.l3_aggression == "conservative"
    assert pm.config.l3_conservative_veto_only is True
    assert pm.config.l3_require_multi_trigger is True

    # Balanced / aggressive must NOT auto-wire them.
    for mode in ("balanced", "aggressive"):
        s_other = Settings(OPENROUTER_API_KEY="dummy", L3_AGGRESSION=mode)
        pm_other = PositionManager.from_settings(s_other)
        assert pm_other.config.l3_conservative_veto_only is False
        assert pm_other.config.l3_require_multi_trigger is False


# ---- HOLD classification ("HOLD must be earned") ---------------------------


@pytest.mark.asyncio
async def test_hold_classification_trend_intact_when_directive_aligns() -> None:
    """Aligned directive + healthy conviction = hold_trend_intact."""
    pm = PositionManager(
        config=PositionManagerConfig(
            l3_aggression="balanced",
            enable_smart_path=False,
            first_review_delay_minutes=0.0,
            min_conviction_to_hold=0.45,
            enable_re_evaluation=False,
            enable_trailing_stop=False,
            enable_daily_dd_guard=False,
            enable_partial_take_profit=False,
            enable_breakeven=False,
            enable_vol_filter=False,
            enable_time_exit=False,
            use_dynamic_atr_tpsl=False,
        ),
        executor=_FakeExecutor(),
    )
    pos = _long_btc(pnl_usd=Decimal("0"))
    # LONG directive at conviction 0.7 > 0.45 hold floor.
    review = await pm.review_open_positions(
        account=_account([pos]),
        directive=_directive(side="long", conviction=0.7),
        decision=_decision_with_atr(_directive(side="long", conviction=0.7), btc_atr_pct=1.0),
    )
    a = review.actions[0]
    assert a.action == "hold"
    assert a.trigger == "hold_trend_intact"
    assert "EARNED HOLD" in a.reason
    assert pm.stats.holds_trend_intact == 1


@pytest.mark.asyncio
async def test_hold_classification_ranging_within_atr_band() -> None:
    """No directional vote but price within 1x ATR = hold_ranging."""
    pm = PositionManager(
        config=PositionManagerConfig(
            l3_aggression="balanced",
            enable_smart_path=False,
            first_review_delay_minutes=0.0,
            min_conviction_to_hold=0.99,    # never satisfied
            enable_re_evaluation=False,
            enable_trailing_stop=False,
            enable_daily_dd_guard=False,
            enable_partial_take_profit=False,
            enable_breakeven=False,
            enable_vol_filter=False,
            enable_time_exit=False,
            use_dynamic_atr_tpsl=False,
        ),
        executor=_FakeExecutor(
            mids={"BTC-PERP": Decimal("60100")},  # 0.17% from 60000
        ),
    )
    pos = _long_btc(pnl_usd=Decimal("0"), mark=Decimal("60100"))
    # Hold/neutral directive: no trend-intact, no profitable holdup.
    review = await pm.review_open_positions(
        account=_account([pos]),
        directive=_directive(action="hold", side=None, conviction=0.3),
        decision=_decision_with_atr(_directive(), btc_atr_pct=1.0),  # ATR=1%
    )
    a = review.actions[0]
    # Price moved 0.17% (< 1.00% ATR) so ranging-classified HOLD.
    assert a.action == "hold"
    assert a.trigger == "hold_ranging"
    assert pm.stats.holds_ranging == 1


@pytest.mark.asyncio
async def test_hold_classification_low_conviction_profitable_locks_in() -> None:
    """In profit + conviction collapsed but >0 = hold_low_conviction_profitable."""
    pm = PositionManager(
        config=PositionManagerConfig(
            l3_aggression="balanced",
            enable_smart_path=False,
            first_review_delay_minutes=0.0,
            min_conviction_to_hold=0.50,
            re_eval_min_profit_pct=Decimal("0.005"),  # 0.5%
            enable_re_evaluation=False,    # don't fire re-eval - hold instead
            enable_trailing_stop=False,
            enable_daily_dd_guard=False,
            enable_partial_take_profit=False,
            enable_breakeven=False,
            enable_vol_filter=False,
            enable_time_exit=False,
            use_dynamic_atr_tpsl=False,
        ),
        executor=_FakeExecutor(),
    )
    # +1.5% profit on a 600 USD long: 9 USD PnL.
    pos = _long_btc(pnl_usd=Decimal("9"))
    review = await pm.review_open_positions(
        account=_account([pos]),
        directive=_directive(side="long", conviction=0.30),
        decision=_decision_with_atr(_directive(), btc_atr_pct=1.0),
    )
    a = review.actions[0]
    assert a.action == "hold"
    assert a.trigger == "hold_low_conviction_profitable"
    assert pm.stats.holds_low_conviction_profitable == 1


@pytest.mark.asyncio
async def test_hold_classification_no_data_when_atr_missing() -> None:
    """Missing ATR data = defensive hold_no_data."""
    pm = PositionManager(
        config=PositionManagerConfig(
            l3_aggression="balanced",
            enable_smart_path=False,
            first_review_delay_minutes=0.0,
            enable_re_evaluation=False,
            enable_trailing_stop=False,
            enable_daily_dd_guard=False,
            enable_partial_take_profit=False,
            enable_breakeven=False,
            enable_vol_filter=False,
            enable_time_exit=False,
            use_dynamic_atr_tpsl=False,
        ),
        executor=_FakeExecutor(),
    )
    pos = _long_btc(pnl_usd=Decimal("0"))
    review = await pm.review_open_positions(
        account=_account([pos]),
        directive=_directive(action="hold", side=None, conviction=0.3),
        decision=None,                    # no ATR available
    )
    a = review.actions[0]
    assert a.action == "hold"
    assert a.trigger == "hold_no_data"
    assert "DEFENSIVE HOLD" in a.reason
    assert pm.stats.holds_no_data == 1


@pytest.mark.asyncio
async def test_hold_classification_unearned_when_no_positive_reason() -> None:
    """Drifted directive, flat PnL, big price move = hold_unearned."""
    pm = PositionManager(
        config=PositionManagerConfig(
            l3_aggression="balanced",
            enable_smart_path=False,
            first_review_delay_minutes=0.0,
            min_conviction_to_hold=0.50,
            re_eval_min_profit_pct=Decimal("0.005"),
            enable_re_evaluation=False,
            enable_trailing_stop=False,
            enable_side_flip=False,         # avoid flipping the position
            auto_flip_on_side_change=False,
            enable_daily_dd_guard=False,
            enable_partial_take_profit=False,
            enable_breakeven=False,
            enable_vol_filter=False,
            enable_time_exit=False,
            use_dynamic_atr_tpsl=False,
        ),
        executor=_FakeExecutor(
            # Price moved 2% from entry: well beyond 1% ATR -> not
            # ranging. Note: mark vs mid - we use mark_price on the
            # Position for the ranging classifier.
            mids={"BTC-PERP": Decimal("61200")},
        ),
    )
    pos = _long_btc(
        pnl_usd=Decimal("0"),
        # Mark on the position is what _justify_hold reads; force
        # it past the ranging band.
        mark=Decimal("61200"),
    )
    review = await pm.review_open_positions(
        account=_account([pos]),
        # Neutral directive (no side) + low conviction so trend-intact
        # cannot fire; PnL ~0 so low_conv_profitable cannot fire;
        # price moved 2% > 1% ATR so ranging cannot fire. The only
        # remaining classification is UNEARNED.
        directive=_directive(action="hold", side=None, conviction=0.30),
        decision=_decision_with_atr(_directive(), btc_atr_pct=1.0),
    )
    a = review.actions[0]
    assert a.action == "hold"
    assert a.trigger == "hold_unearned"
    assert "UNEARNED HOLD" in a.reason
    assert pm.stats.holds_unearned == 1


# ---- Multi-trigger AND requirement under conservative ----------------------


@pytest.mark.asyncio
async def test_conservative_multi_trigger_blocks_single_gate() -> None:
    """A single non-rescue gate under conservative + multi-trig is muted."""
    calls: list[Any] = []

    async def _arbiter(briefing: SmartPathBriefing) -> SmartPathVerdict:
        calls.append(briefing)
        return SmartPathVerdict(action="hold", confidence=0.0)

    pm = PositionManager(
        config=PositionManagerConfig(
            l3_aggression="conservative",
            l3_require_multi_trigger=True,
            enable_smart_path=True,
            first_review_delay_minutes=0.0,
            l3_review_min_interval_minutes=0.0,
            # Only ONE gate fires this cycle (forced periodic):
            l3_review_max_interval_minutes=0.0,
            l3_review_trigger_price_atr_mult=999.0,
            l3_review_trigger_funding_rate=999.0,
            l3_review_trigger_whale_count=999,
            l3_review_trigger_oi_delta_pct=999.0,
            l3_can_override_fast_path=True,
            enable_re_evaluation=False,
            enable_trailing_stop=False,
            enable_daily_dd_guard=False,
            enable_partial_take_profit=False,
            enable_breakeven=False,
            enable_vol_filter=False,
            enable_time_exit=False,
            use_dynamic_atr_tpsl=False,
        ),
        executor=_FakeExecutor(),
        position_arbiter=_arbiter,
    )
    pos = _long_btc(pnl_usd=Decimal("0"))
    # Backdate so first-review isn't the gate.
    state = pm._ensure_state(pos, {})
    state.last_l3_check_at = datetime.now(timezone.utc) - _td(minutes=30)
    state.last_l3_check_price = pos.entry_price
    await pm.review_open_positions(
        account=_account([pos]),
        directive=_directive(),
        decision=_decision_with_atr(_directive(), btc_atr_pct=1.0),
    )
    assert calls == []
    assert pm.stats.smart_skipped_conservative_multi_trigger == 1


@pytest.mark.asyncio
async def test_conservative_multi_trigger_allows_when_two_gates_fire() -> None:
    """Two gates firing = corroboration -> Smart Path invoked."""
    calls: list[Any] = []

    async def _arbiter(briefing: SmartPathBriefing) -> SmartPathVerdict:
        calls.append(briefing)
        return SmartPathVerdict(action="hold", confidence=0.0)

    pm = PositionManager(
        config=PositionManagerConfig(
            l3_aggression="conservative",
            l3_require_multi_trigger=True,
            enable_smart_path=True,
            first_review_delay_minutes=0.0,
            l3_review_min_interval_minutes=0.0,
            l3_review_max_interval_minutes=0.0,   # gate 1: periodic
            l3_review_trigger_price_atr_mult=0.5,  # gate 2: tiny move
            l3_review_trigger_funding_rate=999.0,
            l3_review_trigger_whale_count=999,
            l3_review_trigger_oi_delta_pct=999.0,
            l3_can_override_fast_path=True,
            enable_re_evaluation=False,
            enable_trailing_stop=False,
            enable_daily_dd_guard=False,
            enable_partial_take_profit=False,
            enable_breakeven=False,
            enable_vol_filter=False,
            enable_time_exit=False,
            use_dynamic_atr_tpsl=False,
        ),
        executor=_FakeExecutor(
            mids={"BTC-PERP": Decimal("60800")},  # +1.33% from 60000
        ),
        position_arbiter=_arbiter,
    )
    pos = _long_btc(pnl_usd=Decimal("0"))
    state = pm._ensure_state(pos, {})
    state.last_l3_check_at = datetime.now(timezone.utc) - _td(minutes=30)
    state.last_l3_check_price = Decimal("60000")
    await pm.review_open_positions(
        account=_account([pos]),
        directive=_directive(),
        decision=_decision_with_atr(_directive(), btc_atr_pct=1.0),
    )
    assert len(calls) == 1
    assert pm.stats.smart_skipped_conservative_multi_trigger == 0


@pytest.mark.asyncio
async def test_conservative_multi_trigger_exempts_rescue() -> None:
    """HOLD-rescue alone bypasses the multi-trigger requirement."""
    calls: list[Any] = []

    async def _arbiter(briefing: SmartPathBriefing) -> SmartPathVerdict:
        calls.append(briefing)
        return SmartPathVerdict(action="hold", confidence=0.0)

    pm = PositionManager(
        config=PositionManagerConfig(
            l3_aggression="conservative",
            l3_require_multi_trigger=True,
            enable_smart_path=True,
            enable_hold_rescue=True,
            hold_rescue_l2_min=0.65,
            first_review_delay_minutes=0.0,
            l3_review_min_interval_minutes=0.0,
            l3_review_max_interval_minutes=999.0,
            l3_review_trigger_price_atr_mult=999.0,
            l3_review_trigger_funding_rate=999.0,
            l3_review_trigger_whale_count=999,
            l3_review_trigger_oi_delta_pct=999.0,
            l3_can_override_fast_path=True,
            enable_re_evaluation=False,
            enable_trailing_stop=False,
            enable_daily_dd_guard=False,
            enable_partial_take_profit=False,
            enable_breakeven=False,
            enable_vol_filter=False,
            enable_time_exit=False,
            use_dynamic_atr_tpsl=False,
        ),
        executor=_FakeExecutor(),
        position_arbiter=_arbiter,
    )
    pos = _long_btc(pnl_usd=Decimal("0"))
    state = pm._ensure_state(pos, {})
    state.last_l3_check_at = datetime.now(timezone.utc) - _td(minutes=30)
    state.last_l3_check_price = pos.entry_price
    decision = _decision_with_l2_conviction(
        _directive(),
        btc_atr_pct=1.0,
        l2_conviction=0.80,
        l2_direction_sign=-1,
    )
    await pm.review_open_positions(
        account=_account([pos]),
        directive=_directive(),
        decision=decision,
    )
    assert len(calls) == 1
    assert calls[0].hold_rescue_active is True


@pytest.mark.asyncio
async def test_conservative_multi_trigger_disabled_allows_single_gate() -> None:
    """With multi-trigger OFF, a single gate fires Smart Path normally."""
    calls: list[Any] = []

    async def _arbiter(briefing: SmartPathBriefing) -> SmartPathVerdict:
        calls.append(briefing)
        return SmartPathVerdict(action="hold", confidence=0.0)

    pm = PositionManager(
        config=PositionManagerConfig(
            l3_aggression="conservative",
            l3_require_multi_trigger=False,  # explicitly disabled
            enable_smart_path=True,
            first_review_delay_minutes=0.0,
            l3_review_min_interval_minutes=0.0,
            l3_review_max_interval_minutes=0.0,
            l3_review_trigger_price_atr_mult=999.0,
            l3_review_trigger_funding_rate=999.0,
            l3_review_trigger_whale_count=999,
            l3_review_trigger_oi_delta_pct=999.0,
            l3_can_override_fast_path=True,
            enable_re_evaluation=False,
            enable_trailing_stop=False,
            enable_daily_dd_guard=False,
            enable_partial_take_profit=False,
            enable_breakeven=False,
            enable_vol_filter=False,
            enable_time_exit=False,
            use_dynamic_atr_tpsl=False,
        ),
        executor=_FakeExecutor(),
        position_arbiter=_arbiter,
    )
    pos = _long_btc(pnl_usd=Decimal("0"))
    state = pm._ensure_state(pos, {})
    state.last_l3_check_at = datetime.now(timezone.utc) - _td(minutes=30)
    state.last_l3_check_price = pos.entry_price
    await pm.review_open_positions(
        account=_account([pos]),
        directive=_directive(),
        decision=_decision_with_atr(_directive(), btc_atr_pct=1.0),
    )
    assert len(calls) == 1
    assert pm.stats.smart_skipped_conservative_multi_trigger == 0


# ===========================================================================
# Position-state RESYNC (the fresh-pull defence against the desync bug)
# ===========================================================================
#
# Background: PositionManager used to read positions ONLY from the
# AccountInfo snapshot the caller (router) passed in. When that
# snapshot was wrong - silently-empty AccountInfo from a transient
# user_state failure, eventual-consistency right after market_open,
# or any other source of staleness - PM said "no open positions to
# manage" and a real on-chain short was left unmanaged.
#
# The fix: PM now calls executor.get_open_positions() at the top of
# every review_open_positions cycle. The tests below pin down that
# (1) the fresh pull is preferred over the snapshot, (2) the
# mismatch is logged at WARNING with full forensic detail, (3) we
# fall back to the snapshot only when the executor doesn't expose
# the method or the fresh-pull call raises.


class _FakeExecutorWithFreshPull:
    """Executor stub that supports both get_mid_price AND
    get_open_positions, so we can drive the resync logic deterministically.
    """

    def __init__(
        self,
        fresh_positions: list[Position] | None = None,
        mids: dict[str, Decimal | None] | None = None,
        raise_on_fresh: BaseException | None = None,
        return_none_on_fresh: bool = False,
    ) -> None:
        self._fresh = fresh_positions or []
        self._mids = mids or {}
        self._raise = raise_on_fresh
        self._return_none = return_none_on_fresh
        self.fresh_pull_calls = 0

    async def get_mid_price(self, symbol: str) -> Decimal | None:
        return self._mids.get(symbol)

    async def get_open_positions(self) -> list[Position] | None:
        self.fresh_pull_calls += 1
        if self._raise is not None:
            raise self._raise
        if self._return_none:
            return None  # type: ignore[return-value]
        return list(self._fresh)


@pytest.mark.asyncio
async def test_resync_recovers_position_missing_from_snapshot() -> None:
    """The bug we are fixing: snapshot says empty, fresh pull finds a short.

    PM must:
      * use the fresh-pull position for stewardship,
      * NOT emit "no open positions to manage",
      * increment ``resync_recovered_missing``,
      * log a WARNING.
    """
    short = _short_btc(pnl_usd=Decimal("-18"))  # -3% on size=600 -> SL fire
    executor = _FakeExecutorWithFreshPull(
        fresh_positions=[short],
        mids={"BTC-PERP": Decimal("60000")},
    )
    pm = PositionManager(
        config=PositionManagerConfig(
            stop_loss_pct=Decimal("0.02"),
            enable_re_evaluation=False,
            enable_trailing_stop=False,
            enable_daily_dd_guard=False,
            enable_smart_path=False,
            auto_flip_on_side_change=False,
        ),
        executor=executor,
    )

    # Snapshot is the bug: empty, no positions.
    empty_account = _account(positions=[])
    review = await pm.review_open_positions(
        account=empty_account,
        directive=_directive(side="short"),
        decision=None,
    )

    assert executor.fresh_pull_calls == 1, "fresh pull must be issued"
    assert pm.stats.resync_fresh_pulls == 1
    assert pm.stats.resync_recovered_missing == 1
    # And critically: PM operated on the fresh-pull position, NOT
    # the empty snapshot - so it actually closed the SL'd short.
    assert len(review.actions) == 1
    assert review.actions[0].action == "close"
    assert review.actions[0].trigger == "stop_loss"
    # And the "no open positions" misleading note must be ABSENT.
    assert not any("no open positions" in n for n in review.notes)


@pytest.mark.asyncio
async def test_resync_logs_warning_with_position_keys(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Mismatch must produce an operator-visible WARNING with the keys."""
    import logging

    short = _short_btc(pnl_usd=Decimal("0"))
    executor = _FakeExecutorWithFreshPull(fresh_positions=[short])
    pm = PositionManager(
        config=PositionManagerConfig(
            enable_daily_dd_guard=False,
            enable_smart_path=False,
        ),
        executor=executor,
    )

    # Loguru -> caplog bridge so pytest.caplog can see warnings.
    from loguru import logger as _logger
    handler_id = _logger.add(
        caplog.handler, format="{message}", level="WARNING"
    )
    try:
        with caplog.at_level(logging.WARNING):
            await pm.review_open_positions(
                account=_account([]),
                directive=_directive(),
                decision=None,
            )
    finally:
        _logger.remove(handler_id)

    relevant = [
        r for r in caplog.records
        if "RESYNC" in r.getMessage() and "MISSING" in r.getMessage()
    ]
    assert relevant, (
        f"Expected a RESYNC WARNING; got records: "
        f"{[r.getMessage() for r in caplog.records]}"
    )
    # The log line must name the missing (symbol, side) so the
    # operator can grep the next incident.
    assert "BTC-PERP" in relevant[0].getMessage()
    assert "short" in relevant[0].getMessage()


@pytest.mark.asyncio
async def test_resync_uses_fresh_pull_when_both_have_positions() -> None:
    """When both have positions, fresh pull wins as source of truth."""
    # Snapshot says we are LONG at -50; fresh pull says we are
    # actually SHORT at -18 (e.g. snapshot is from a previous cycle
    # before we flipped sides via a side-channel). PM must act on
    # the SHORT, not the LONG.
    snap_long = _long_btc(pnl_usd=Decimal("-50"))
    fresh_short = _short_btc(pnl_usd=Decimal("-18"))
    executor = _FakeExecutorWithFreshPull(fresh_positions=[fresh_short])
    pm = PositionManager(
        config=PositionManagerConfig(
            stop_loss_pct=Decimal("0.02"),
            enable_re_evaluation=False,
            enable_trailing_stop=False,
            enable_daily_dd_guard=False,
            enable_smart_path=False,
            auto_flip_on_side_change=False,
        ),
        executor=executor,
    )
    review = await pm.review_open_positions(
        account=_account([snap_long]),
        directive=_directive(side="short"),
        decision=None,
    )
    # PM closes the SHORT (the actual on-chain position), not the
    # phantom LONG from the snapshot.
    assert len(review.actions) == 1
    assert review.snapshots[0].side == "short"
    # Both sides of the mismatch are recorded.
    assert pm.stats.resync_recovered_missing == 1   # short was missing
    assert pm.stats.resync_phantom_in_snapshot == 1  # long was phantom


@pytest.mark.asyncio
async def test_resync_falls_back_to_snapshot_when_executor_raises() -> None:
    """get_open_positions raising must not break the cycle.

    PM falls back to ``account.positions``, bumps the fallback
    counter, and emits a WARNING.
    """
    snap_short = _short_btc(pnl_usd=Decimal("0"))
    executor = _FakeExecutorWithFreshPull(
        raise_on_fresh=RuntimeError("hyperliquid timeout"),
    )
    pm = PositionManager(
        config=PositionManagerConfig(
            enable_daily_dd_guard=False,
            enable_smart_path=False,
        ),
        executor=executor,
    )
    review = await pm.review_open_positions(
        account=_account([snap_short]),
        directive=_directive(side="short"),
        decision=None,
    )
    assert pm.stats.resync_fallbacks_to_snapshot == 1
    assert pm.stats.resync_fresh_pulls == 0
    # PM still managed the position from the snapshot.
    assert len(review.snapshots) == 1
    assert review.snapshots[0].side == "short"


@pytest.mark.asyncio
async def test_resync_falls_back_to_snapshot_when_executor_returns_none(
) -> None:
    """An executor returning None instead of [] also triggers fallback."""
    snap_short = _short_btc(pnl_usd=Decimal("0"))
    executor = _FakeExecutorWithFreshPull(return_none_on_fresh=True)
    pm = PositionManager(
        config=PositionManagerConfig(
            enable_daily_dd_guard=False,
            enable_smart_path=False,
        ),
        executor=executor,
    )
    review = await pm.review_open_positions(
        account=_account([snap_short]),
        directive=_directive(side="short"),
        decision=None,
    )
    assert pm.stats.resync_fallbacks_to_snapshot == 1
    assert len(review.snapshots) == 1
    assert review.snapshots[0].side == "short"


@pytest.mark.asyncio
async def test_resync_legacy_executor_without_method_uses_snapshot() -> None:
    """Legacy executors (no get_open_positions) silently use the snapshot.

    This is the backwards-compat path - the _FakeExecutor at the top
    of this file is one such legacy executor, and ALL existing tests
    must keep working.
    """
    snap_short = _short_btc(pnl_usd=Decimal("0"))
    # _FakeExecutor (the original one, defined at the top of the file)
    # has NO get_open_positions method.
    pm = PositionManager(
        config=PositionManagerConfig(
            enable_daily_dd_guard=False,
            enable_smart_path=False,
        ),
        executor=_FakeExecutor(),
    )
    review = await pm.review_open_positions(
        account=_account([snap_short]),
        directive=_directive(side="short"),
        decision=None,
    )
    # No fresh-pull telemetry should fire (legacy path).
    assert pm.stats.resync_fresh_pulls == 0
    assert pm.stats.resync_recovered_missing == 0
    assert pm.stats.resync_fallbacks_to_snapshot == 0
    # ... but PM still managed the snapshot position normally.
    assert len(review.snapshots) == 1
    assert review.snapshots[0].side == "short"


@pytest.mark.asyncio
async def test_resync_no_mismatch_when_snapshot_and_fresh_pull_agree(
) -> None:
    """Happy path: snapshot and fresh pull agree -> only counter bumped."""
    pos = _short_btc(pnl_usd=Decimal("0"))
    executor = _FakeExecutorWithFreshPull(fresh_positions=[pos])
    pm = PositionManager(
        config=PositionManagerConfig(
            enable_daily_dd_guard=False,
            enable_smart_path=False,
        ),
        executor=executor,
    )
    await pm.review_open_positions(
        account=_account([pos]),
        directive=_directive(side="short"),
        decision=None,
    )
    assert pm.stats.resync_fresh_pulls == 1
    assert pm.stats.resync_recovered_missing == 0
    assert pm.stats.resync_phantom_in_snapshot == 0


@pytest.mark.asyncio
async def test_resync_filters_flats_from_fresh_pull() -> None:
    """A misbehaving executor returning flats must be filtered out."""
    flat = Position(
        symbol="BTC-PERP",
        side="flat",
        size_usd=Decimal("0"),
        entry_price=Decimal("60000"),
        mark_price=Decimal("60000"),
        leverage=Decimal("0"),
        unrealized_pnl_usd=Decimal("0"),
    )
    short = _short_btc(pnl_usd=Decimal("0"))
    executor = _FakeExecutorWithFreshPull(fresh_positions=[flat, short])
    pm = PositionManager(
        config=PositionManagerConfig(
            enable_daily_dd_guard=False,
            enable_smart_path=False,
        ),
        executor=executor,
    )
    review = await pm.review_open_positions(
        account=_account([]),
        directive=_directive(side="short"),
        decision=None,
    )
    # Only the short comes through; the flat is dropped.
    assert len(review.snapshots) == 1
    assert review.snapshots[0].side == "short"


@pytest.mark.asyncio
async def test_resync_empty_fresh_with_empty_snapshot_is_quiet() -> None:
    """No positions either side -> no warning, no counters except the pull."""
    executor = _FakeExecutorWithFreshPull(fresh_positions=[])
    pm = PositionManager(
        config=PositionManagerConfig(
            enable_daily_dd_guard=False,
            enable_smart_path=False,
        ),
        executor=executor,
    )
    review = await pm.review_open_positions(
        account=_account([]),
        directive=_directive(),
        decision=None,
    )
    assert pm.stats.resync_fresh_pulls == 1
    assert pm.stats.resync_recovered_missing == 0
    assert pm.stats.resync_phantom_in_snapshot == 0
    assert any("no open positions" in n for n in review.notes)
