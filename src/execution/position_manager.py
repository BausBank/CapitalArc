"""PositionManager - two-tier per-cycle stewardship of open perp positions.

Once :class:`AllocationRouter` opens a position the *allocation* job is
done; from that point on the position is governed by a separate
concern: **stewardship**. CapitalArc's stewardship runs in TWO
independent tiers on every cycle, designed so that the cheap, fast
rules can run constantly while the expensive LLM only fires on
material change.

Tier 1 - Fast Path  (~ milliseconds, no LLM)
--------------------------------------------
Runs unconditionally on every cycle. Deterministic, ATR-aware,
operator-tunable. Trigger ladder (priority order, first match wins):

    1. ``daily_dd_guard``         portfolio-wide daily loss-limit (kill-switch)
    2. ``stop_loss``              dynamic ATR stop or fallback fixed %
    3. ``vol_spike_close``        live ATR jumped >= VOL_SPIKE_MULT x entry ATR
    4. ``time_exit``              position held longer than MAX_POSITION_HOLD_HOURS
    5. ``take_profit``            dynamic ATR full-target or fallback fixed %
    6. ``partial_take_profit``    scale out PARTIAL_TP_FRACTION at the first ATR
                                  target; only fires once per position lifetime
    7. ``trailing_stop``          ATR-trail from peak PnL (per-side)
    8. ``side_flip``              engine flipped to opposite side
    9. ``re_evaluation``          conviction collapsed below MIN_CONVICTION_TO_HOLD
                                  while position is in modest profit

Plus state-only triggers (advisory, don't close on their own):

    * ``breakeven_arm``  arms the BE flag once profit >= breakeven trigger;
      subsequent SL evaluation reads the BE state and lifts the
      effective stop to entry + buffer.
    * ``vol_spike_warn``  current ATR jumped past spike threshold but
      VOL_SPIKE_ACTION="tighten_stop" - the SL branch on the next
      cycle reads the tighter ATR snapshot and exits earlier.

Per-asset ATR caps (BTC <= 3%, ETH <= 4% on 1h by default) are
surfaced on the snapshot panel and used by the router to refuse new
opens in dead-vol or runaway-vol regimes.

Tier 2 - Smart Path  (~1 second, Claude Sonnet 4.6)
---------------------------------------------------
A per-position LLM review that fires ONLY when a material trigger
hits - we never spend an OpenRouter call on a quiet cycle. Gates
(OR-combined; any single trigger fires the review):

    * Price moved >= L3_REVIEW_TRIGGER_PRICE_ATR_MULT x ATR since the
      last L3 check on this position.
    * Funding rate magnitude >= L3_REVIEW_TRIGGER_FUNDING_RATE.
    * Whale-activity count >= L3_REVIEW_TRIGGER_WHALE_COUNT.
    * OI delta (1h) magnitude >= L3_REVIEW_TRIGGER_OI_DELTA_PCT.
    * Wall-clock since last L3 review >=
      L3_REVIEW_MAX_INTERVAL_MINUTES (forced periodic re-arbitration).

The Smart Path returns a :class:`SmartPathVerdict` (HOLD / CLOSE_FULL
/ CLOSE_PARTIAL / TIGHTEN_STOP / RAISE_TARGET / IGNORE) plus a short
rationale. When ``L3_CAN_OVERRIDE_FAST_PATH`` is True the verdict
can override the Fast Path decision; otherwise it is recorded as
advisory metadata on the snapshot.

The Smart Path *executor* itself is plug-and-play - the manager
accepts any callable that takes a :class:`SmartPathBriefing` and
returns a :class:`SmartPathVerdict`. The default implementation is a
synthetic stub so the agent still runs end-to-end without an
OpenRouter key wired; production runs swap it for a real Claude
arbiter via :func:`build_default_position_arbiter`.

Per-position state
------------------
We keep one :class:`_PositionState` per ``(symbol, side)`` open
position carrying:

    * ``opened_at`` - first-seen timestamp (used by time-based exit)
    * ``entry_atr_pct`` / ``entry_atr_abs`` - ATR snapshot at open
      (used by the vol-spike filter)
    * ``peak_pnl_pct`` - running peak PnL fraction (trailing stop)
    * ``breakeven_armed`` - BE flag (lifts the dynamic SL to entry)
    * ``partial_tp_done`` - scale-out flag (ensures one fire only)
    * ``last_l3_check_at`` / ``last_l3_check_price`` - Smart Path gate state
    * ``last_l3_verdict`` - last verdict (for the panel)

State is in-process only. Daily-DD tracker lives at the
:class:`PositionManager` level (one per process), session-start
equity captured on first call.

This module is intentionally framework-agnostic - it takes a
:class:`PerpExecutorProtocol`-shaped executor, an
:class:`AccountInfo` snapshot and a :class:`DecisionResult`
(for ATR%, L2 funding / OI / whale data), and returns a
:class:`PositionReview`. The router consumes the review and chooses
how to act on it - the router still owns the decision to actually
call ``executor.close_position``.

Extending the manager
=====================
New Fast-Path triggers should:

* Add a new branch to :meth:`PositionManager._evaluate_position`.
* Return a :class:`PositionAction` with a clear ``trigger`` string so
  the console panel can colour-code it and the logs stay greppable.
* Update ``_TRIGGER_PRIORITY`` so the new trigger is evaluated in the
  right priority order relative to the existing ladder.

New Smart-Path triggers should:

* Add a check in :meth:`PositionManager._should_invoke_smart_path`.
* Document the rationale in the docstring above.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import (
    Any,
    Awaitable,
    Callable,
    Literal,
    Protocol,
    runtime_checkable,
)

from src.core.decision_engine import DecisionResult, ExecutionDirective
from src.execution.arc_perp_executor import AccountInfo, Position
from src.utils.logging import logger


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


class _L2SignalMap(dict):
    """``dict``-like with an aggregate L2 fallback for unknown symbols.

    The HOLD-rescue gate and the Smart-Path briefing want to read
    ``l2_conviction`` / ``l2_direction_sign`` for every position even
    when L2 only emitted aggregate scoring (no ``per_symbol``
    attribution). Rather than scatter `.get(..., fallback)` chains we
    centralise that behaviour here: any `.get(symbol)` / `[symbol]`
    on an unknown symbol returns a sensible default carrying the
    aggregate read, and `__contains__` reports True for any symbol
    (so callers don't surprise-iterate the empty dict).
    """

    def __init__(
        self,
        *args: Any,
        default_conviction: float = 0.0,
        default_direction: int = 0,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._default_conv = float(default_conviction or 0.0)
        self._default_dir = int(default_direction or 0)

    def _default_payload(self) -> dict[str, Any]:
        return {
            "funding_rate": None,
            "oi_delta_1h_pct": None,
            "n_whales": 0,
            "whale_direction": None,
            "l2_conviction": self._default_conv,
            "l2_direction_sign": self._default_dir,
        }

    def get(self, key: Any, default: Any = None) -> Any:  # type: ignore[override]
        # Always return the aggregate payload for unknown symbols,
        # regardless of the user-supplied `default` - the whole point
        # of this map is to make the HOLD-rescue gate work even when
        # callers pass `{}` as a defensive fallback. We deliberately
        # ignore `default` because mixing it in would silently break
        # the fallback contract.
        if dict.__contains__(self, key):
            return super().__getitem__(key)
        return self._default_payload()

    def __getitem__(self, key: Any) -> Any:
        if dict.__contains__(self, key):
            return super().__getitem__(key)
        return self._default_payload()


# ---------------------------------------------------------------------------
# Public dataclasses
# ---------------------------------------------------------------------------


@dataclass
class PerAssetATRCaps:
    """Per-symbol ATR%-on-1h hard ceiling.

    Mirrors the per-asset bands the L3 critical-mode prompt enforces
    so Fast Path + LLM agree on what counts as "too volatile". When
    a position's primary symbol crosses its cap the manager flags
    ``atr_cap_breached`` on the snapshot; the router treats that as
    a hint to refuse new opens (it does not, on its own, close an
    already-open position - we leave that judgement to L3).
    """

    btc_1h: float = 3.0
    eth_1h: float = 4.0
    default_1h: float = 4.0

    def cap_for(self, symbol: str) -> float:
        sym = symbol.upper()
        if sym.startswith("BTC"):
            return self.btc_1h
        if sym.startswith("ETH"):
            return self.eth_1h
        return self.default_1h


@dataclass
class PositionManagerConfig:
    """Tunable thresholds for the per-position stewardship rules.

    All percentage fields are stored as :class:`Decimal` **fractions**
    (so ``0.03`` means 3%). When loading from ``.env`` we accept the
    operator-friendly ``TAKE_PROFIT_PCT=3.0`` form and divide by 100
    in :meth:`from_settings`.

    The dynamic ATR-based knobs (``sl_atr_mult`` / ``tp_atr_mult`` /
    etc.) are *multipliers* (already unitless), so they stay on raw
    float; the manager combines them with the live ATR fraction
    (ATR/price) to compute trigger prices at runtime.
    """

    # ---- Legacy fixed-pct triggers (fallback when no ATR available) ----
    take_profit_pct: Decimal = Decimal("0.03")
    stop_loss_pct: Decimal = Decimal("0.02")
    trailing_stop_pct: Decimal = Decimal("0.01")
    min_conviction_to_hold: float = 0.45
    re_eval_min_profit_pct: Decimal = Decimal("0.005")
    auto_flip_on_side_change: bool = True

    # ---- Dynamic ATR-based TP/SL ----
    use_dynamic_atr_tpsl: bool = True
    sl_atr_mult: float = 1.2
    tp_atr_mult: float = 3.0
    trail_atr_mult: float = 1.5

    # ---- Partial take-profit (scaling out) ----
    enable_partial_take_profit: bool = True
    partial_tp_atr_mult: float = 1.5
    partial_tp_fraction: Decimal = Decimal("0.50")

    # ---- Breakeven move ----
    enable_breakeven: bool = True
    breakeven_trigger_atr_mult: float = 1.0
    breakeven_buffer_pct: Decimal = Decimal("0.0005")  # 0.05%

    # ---- Volatility filter ----
    enable_vol_filter: bool = True
    vol_spike_mult: float = 1.8
    vol_spike_action: Literal["close", "tighten_stop"] = "tighten_stop"

    # ---- Time-based exit ----
    max_position_hold_hours: float = 24.0

    # ---- Daily / global drawdown guard ----
    enable_daily_dd_guard: bool = True
    daily_loss_limit_pct: float = 5.0

    # ---- Per-asset ATR caps ----
    atr_caps: PerAssetATRCaps = field(default_factory=PerAssetATRCaps)

    # ---- Smart Path (L3 review) ----
    enable_smart_path: bool = True
    l3_review_trigger_price_atr_mult: float = 1.5
    l3_review_trigger_funding_rate: float = 0.0005
    l3_review_trigger_whale_count: int = 5
    l3_review_trigger_oi_delta_pct: float = 4.0
    l3_review_min_interval_minutes: float = 30.0
    l3_review_max_interval_minutes: float = 120.0
    l3_can_override_fast_path: bool = True

    # ---- Smart Path - L3_AGGRESSION calibration ----
    # Same three-mode knob as the main L3 arbiter, applied to the
    # per-position Smart Path:
    #
    #   conservative -> override threshold raised to 0.75; partial /
    #                   tighten verdicts shrink by 0.85; HOLD-rescue
    #                   rule is OFF; close verdicts still respected.
    #   balanced (DEFAULT) -> override threshold 0.55; multipliers
    #                   at 1.0; HOLD-rescue OFF (relies on regular
    #                   gates).
    #   aggressive   -> override threshold 0.45; partial / tighten
    #                   verdicts scale 1.15x; HOLD-rescue rule ON.
    #
    # The calibration is applied AFTER the LLM verdict so the audit
    # trail is deterministic and reversible. See
    # :meth:`_calibrate_smart_verdict` for the math.
    l3_aggression: Literal["conservative", "balanced", "aggressive"] = (
        "balanced"
    )

    # ---- Smart Path - HOLD-rescue rule ----
    # When True (default under aggressive), a Fast-Path HOLD on a
    # position whose primary symbol has L2 conviction >=
    # `hold_rescue_l2_min` forces a Smart-Path review with a
    # dedicated "hold_rescue" trigger. The LLM is shown the
    # contradiction explicitly: "Fast Path wants HOLD but L2 says
    # X conviction - should we close, scale out, or stay the
    # course?". This lets the LLM rescue a position that the
    # deterministic ladder is about to hold through a strong
    # on-chain reversal (or a valid continuation it could lean
    # into).
    #
    # ``hold_rescue_direction_mode`` chooses when the rescue fires:
    #   * "opposite" (default for balanced) - rescue fires only
    #     when L2 conviction OPPOSES the position side. We trust
    #     same-direction conviction = HOLD is correct.
    #   * "any" (auto-set under aggressive) - rescue ALSO fires
    #     when L2 conviction agrees with the position side. This
    #     gives the LLM a chance to scale into a strong trend or
    #     raise targets, not just bail. The trade-off is a few
    #     more LLM calls per cycle.
    enable_hold_rescue: bool = False
    hold_rescue_l2_min: float = 0.65
    hold_rescue_direction_mode: Literal["opposite", "any"] = "opposite"
    # Dedicated cooldown floor for the rescue trigger so an L2
    # conviction that lingers high across many cycles doesn't burn
    # an LLM call every single cycle. Defaults to 20min (slightly
    # below the generic 25min floor because rescue is by design a
    # "wake the LLM up now" trigger). The hard cooldown floor
    # below still bounds the generic Smart-Path interval.
    hold_rescue_cooldown_minutes: float = 20.0

    # ---- Smart Path - first-review delay ----
    # A brand-new position is NOT eligible for a Smart-Path review
    # in its first ``first_review_delay_minutes`` of life - let the
    # Fast Path observe the entry first. Stops "L3 reviews every
    # new open immediately" thrash on a multi-symbol cycle.
    first_review_delay_minutes: float = 10.0
    # ---- Smart Path - HOLD-must-be-earned periodic review ----
    # When True (auto-on under aggressive), a Fast Path that has
    # been emitting HOLD for >= ``hold_must_be_earned_minutes`` on
    # a single position forces a Smart-Path review even if no other
    # gate has fired. The intent: HOLD shouldn't be a "we forgot
    # about you" default - if we're sitting on a position we
    # should be regularly auditing whether it deserves the slot.
    # Set to 0 to disable.
    hold_must_be_earned: bool = False
    hold_must_be_earned_minutes: float = 45.0

    # ---- Master switches for individual triggers ----
    enable_take_profit: bool = True
    enable_stop_loss: bool = True
    enable_trailing_stop: bool = True
    enable_re_evaluation: bool = True
    enable_side_flip: bool = True
    enable_time_exit: bool = True

    # ---- Global LLM budget (cost-safety rails, Day-6+) ----------
    # Hard caps on Smart-Path invocations REGARDLESS of per-position
    # gates / cooldowns. The per-position cooldown ladder already
    # bounds how often a SINGLE position can wake the LLM; these
    # ceilings bound the AGGREGATE call volume so a multi-position
    # cycle with simultaneous fired gates can never produce an
    # N-positions-wide LLM storm.
    #
    #   * ``llm_max_per_cycle`` (default 2) - hard cap per
    #     ``review_open_positions`` call. When more positions have
    #     fired gates than the budget allows, we keep the highest-
    #     priority candidates (see :func:`_smart_reason_priority`)
    #     and skip the rest.
    #   * ``llm_max_per_hour`` (default 8) - rolling-hour ceiling
    #     across the whole manager. Bounds OpenRouter cost
    #     predictably even under a trigger storm.
    #
    # Set either to 0 to disable that ceiling entirely (NOT
    # recommended in production).
    llm_max_per_cycle: int = 2
    llm_max_per_hour: int = 8

    # ---- Conservative-mode hardening (Day-6+) -------------------
    # Two flags that make conservative mode meaningfully more
    # cautious than just "raise the override threshold":
    #
    # ``l3_conservative_veto_only`` (default True under conservative):
    # When the LLM verdict tries to UPGRADE a Fast-Path HOLD into a
    # close / partial close ("positive override"), we BLOCK it and
    # keep the HOLD. The LLM can still VETO a Fast-Path close
    # (downgrade close -> hold) and can still issue advisory
    # tighten / raise. This makes conservative mode "LLM can only
    # be a brake, never an accelerator".
    #
    # ``l3_require_multi_trigger`` (default True under conservative):
    # Smart Path requires >= 2 fired gates on the same position
    # before invocation. A single price-move or single funding
    # spike isn't enough; we want corroboration. Rescue is
    # exempted because it has its own cooldown bookkeeping.
    #
    # Both are False by default and auto-True under conservative
    # via ``from_settings``. Operators can override either via
    # explicit .env settings.
    l3_conservative_veto_only: bool = False
    l3_require_multi_trigger: bool = False


# Type alias for trigger names. Kept as a Literal so static analysers
# can flag typos at call-sites (e.g. "trailling_stop").
PositionTrigger = Literal[
    # ----- Legacy "no positive justification" HOLD ---------------
    # Kept for backward compatibility; ``hold`` now means "HOLD
    # without any positive justification" (the fallthrough case).
    # New code should prefer one of the ``hold_*`` sub-triggers
    # below so the panel can show WHY the position is being held.
    "hold",
    "take_profit",
    "partial_take_profit",
    "stop_loss",
    "trailing_stop",
    "side_flip",
    "re_evaluation",
    "vol_spike_close",
    "vol_spike_warn",
    "time_exit",
    "daily_dd_guard",
    "breakeven_arm",
    "l3_smart_close",
    "l3_smart_partial",
    "l3_smart_tighten",
    "l3_smart_hold",
    "l3_smart_raise",
    # HOLD-rescue: Fast Path wanted HOLD but L2 conviction was high
    # enough to force a Smart-Path review which then overrode the
    # HOLD. The action it produces is one of close / partial_close /
    # tighten - the trigger label just records WHY the LLM was
    # invoked.
    "l3_hold_rescue",
    # ---- "HOLD must be earned" classification (Day-6+) ----------
    # Every Fast-Path HOLD now carries a positive justification
    # (one of the four below) or is flagged as ``hold_unearned``.
    # The console panel paints earned-HOLDs in calm tones and
    # unearned-HOLDs in attention-grabbing yellow so operators can
    # see at-a-glance whether the agent is actively choosing to
    # hold or just defaulting through inertia.
    "hold_trend_intact",            # directive agrees + healthy conviction
    "hold_ranging",                 # within ATR breakeven band, no clear vote
    "hold_low_conviction_profitable",  # mild profit + conviction faded but
                                       # not low enough to lock in
    "hold_no_data",                 # missing ATR / L2 - defensive HOLD
    "hold_unearned",                # NONE of the above; HOLD without a
                                    # positive reason; surfaces as a panel
                                    # warning and is the first thing
                                    # HOLD-must-be-earned reviews
]


# Action verbs the router executes. We extend the previous shape with
# ``partial_close`` (carries ``size_usd_to_close``) and the advisory
# state-only verbs (``arm_breakeven``, ``tighten_stop``) that don't
# touch the venue but update Fast-Path state.
PositionVerb = Literal[
    "hold",
    "close",
    "partial_close",
    "arm_breakeven",
    "tighten_stop",
]


@dataclass
class PositionAction:
    """A management decision recommended for a single open position."""

    symbol: str
    side: str
    action: PositionVerb
    trigger: PositionTrigger
    reason: str
    pnl_pct: float                       # current unrealised PnL / notional
    pnl_usd: Decimal                     # current unrealised PnL in USD
    size_usd: Decimal                    # position notional at decision time
    # Optional: amount to close on a partial close (USD notional).
    # Routed to ``executor.close_position(..., size_usd_to_close=)``
    # when the executor supports partial closes; otherwise full close.
    size_usd_to_close: Decimal | None = None
    # Threshold context (the prices / fractions that armed the trigger).
    take_profit_price: Decimal | None = None
    stop_loss_price: Decimal | None = None
    trailing_stop_price: Decimal | None = None
    peak_pnl_pct: float | None = None
    # Source of this action ("fast" | "smart"). Lets the panel paint
    # a "[L3]" badge on Smart-Path overrides.
    source: Literal["fast", "smart"] = "fast"
    # When set, ``trigger`` was raised by the Smart Path; the panel
    # surfaces this short snippet as the L3 audit explanation.
    smart_snippet: str | None = None


@dataclass
class PositionSnapshot:
    """Per-position telemetry surfaced into the Position Management panel.

    Stamped onto every :class:`PositionReview` whether or not the
    manager fired an action - the panel paints a HOLD row for steady
    positions so the operator can see TP / SL levels at a glance.
    """

    symbol: str
    side: str
    size_usd: Decimal
    entry_price: Decimal
    mark_price: Decimal
    leverage: Decimal
    unrealized_pnl_usd: Decimal
    pnl_pct: float
    take_profit_price: Decimal | None
    stop_loss_price: Decimal | None
    trailing_stop_price: Decimal | None
    peak_pnl_pct: float | None
    action: PositionVerb
    trigger: PositionTrigger
    reason: str
    # New telemetry surfaced by the two-tier design.
    current_atr_pct: float | None = None
    entry_atr_pct: float | None = None
    atr_cap_pct: float | None = None
    atr_cap_breached: bool = False
    age_minutes: float | None = None
    breakeven_armed: bool = False
    partial_tp_done: bool = False
    smart_path_invoked: bool = False
    smart_path_verdict: str | None = None
    smart_path_rationale: str | None = None
    source: Literal["fast", "smart"] = "fast"


@dataclass
class PositionReview:
    """Aggregate review across every open position in one cycle.

    The router checks :attr:`actions` first - any non-"hold" action
    means the regime dispatch should be pre-empted. Either way the
    :attr:`snapshots` list always represents the FULL set of positions
    so the panel can render hold rows alongside trigger rows.
    """

    actions: list[PositionAction] = field(default_factory=list)
    snapshots: list[PositionSnapshot] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    # Portfolio-wide kill switch fired by the Daily-DD guard.
    # When True, the router MUST flatten everything and refuse
    # new opens until the operator resets state (process restart).
    daily_dd_breached: bool = False
    daily_pnl_pct: float | None = None
    timestamp: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )

    @property
    def has_action(self) -> bool:
        """Is there any verb that the router must act on?"""
        # ``arm_breakeven`` and ``tighten_stop`` are state-only verbs:
        # they update Fast-Path state but the router doesn't need to
        # do anything on-chain. ``has_action`` returns True only for
        # verbs that change exposure.
        return any(
            a.action in {"close", "partial_close"} for a in self.actions
        )


# ---------------------------------------------------------------------------
# Smart-Path types (L3 per-position arbiter)
# ---------------------------------------------------------------------------


SmartPathVerdictAction = Literal[
    "hold",
    "close_full",
    "close_partial",
    "tighten_stop",
    "raise_target",
]


@dataclass
class SmartPathBriefing:
    """Compact payload sent to the per-position Smart-Path arbiter.

    Contains everything the LLM needs to issue a one-position verdict
    without having to re-derive any number. Built once per cycle
    (per fired trigger) by the manager; consumed by an arbitrary
    arbiter callable so the LLM choice stays pluggable.
    """

    symbol: str
    side: str
    entry_price: Decimal
    mark_price: Decimal
    size_usd: Decimal
    leverage: Decimal
    pnl_usd: Decimal
    pnl_pct: float
    age_minutes: float
    peak_pnl_pct: float | None
    breakeven_armed: bool
    partial_tp_done: bool
    current_atr_pct: float | None
    entry_atr_pct: float | None
    atr_cap_pct: float | None
    # Surfaces the trigger(s) that fired the Smart Path on this cycle
    # so the LLM knows WHY it was called.
    invocation_reasons: list[str] = field(default_factory=list)
    # On-chain context from L2 (funding, OI delta, whale activity).
    funding_rate: float | None = None
    oi_delta_1h_pct: float | None = None
    whale_count: int = 0
    whale_direction: str | None = None
    # L2 aggregate conviction + direction (used by the HOLD-rescue
    # rule and as soft additional context for the LLM). When the L2
    # raw payload exposes per-symbol attribution we use that; else
    # we fall back to the aggregate L2 vote.
    l2_conviction: float = 0.0
    l2_direction_sign: int = 0
    # Engine-level directive at this cycle (so L3 doesn't second-guess
    # a side-flip already in progress).
    engine_directive_action: str = "hold"
    engine_directive_side: str | None = None
    engine_directive_conviction: float = 0.0
    # When True, this Smart-Path invocation was triggered by the
    # HOLD-rescue rule (Fast Path wanted HOLD but L2 conviction was
    # high on the opposite side). The arbiter is expected to give an
    # actionable verdict; under L3_AGGRESSION=aggressive a HOLD
    # response is rewritten to a small defensive partial close.
    hold_rescue_active: bool = False
    # The manager's current L3 aggression mode, surfaced to the
    # arbiter so the LLM can self-calibrate (e.g. dial up partial
    # fractions under aggressive). Purely informational - the
    # post-validation calibration in _calibrate_smart_verdict is the
    # source of truth.
    l3_aggression: str = "balanced"


@dataclass
class SmartPathVerdict:
    """Structured reply from the per-position arbiter.

    ``confidence`` is in ``[0, 1]`` - the manager treats anything
    below ``0.5`` as "L3 had no strong opinion" and falls back to
    the Fast Path verdict (defensive default).
    """

    action: SmartPathVerdictAction = "hold"
    rationale: str = ""
    confidence: float = 0.0
    # Optional close fraction when ``action=close_partial``. Defaults
    # to ``PositionManagerConfig.partial_tp_fraction`` when None.
    close_fraction: Decimal | None = None


PositionArbiterCallable = Callable[
    [SmartPathBriefing], Awaitable[SmartPathVerdict]
]


async def _null_arbiter(briefing: SmartPathBriefing) -> SmartPathVerdict:
    """Default Smart-Path arbiter - returns a deferential HOLD.

    The synthetic stub is intentionally inert so the manager runs
    end-to-end without an OpenRouter key. Production deployments
    override this with :func:`build_default_position_arbiter` (or
    a custom callable) when constructing the manager.
    """
    return SmartPathVerdict(
        action="hold",
        rationale=(
            "synthetic position-arbiter (no LLM wired) - deferring "
            "to the Fast Path verdict."
        ),
        confidence=0.0,
    )


# ---------------------------------------------------------------------------
# Optional executor surfaces used by the manager
# ---------------------------------------------------------------------------


@runtime_checkable
class _ExecutorWithMid(Protocol):
    """Minimal protocol for executors that expose a mid-price feed."""

    async def get_mid_price(self, symbol: str) -> Decimal | None: ...


# ---------------------------------------------------------------------------
# Internal state
# ---------------------------------------------------------------------------


@dataclass
class _PositionState:
    """Per-position bookkeeping carried across cycles.

    Garbage-collected when the underlying position disappears (see
    :meth:`PositionManager._gc_states`).
    """

    opened_at: datetime
    entry_price: Decimal
    entry_atr_pct: float | None
    entry_atr_abs: Decimal | None
    peak_pnl_pct: float | None = None
    breakeven_armed: bool = False
    partial_tp_done: bool = False
    last_l3_check_at: datetime | None = None
    last_l3_check_price: Decimal | None = None
    last_smart_verdict: SmartPathVerdict | None = None
    # Anti-thrash: timestamp of the last HOLD-rescue invocation.
    # Even if the rescue gate keeps firing (e.g. L2 stays high in
    # the opposite direction across multiple cycles), we don't burn
    # an LLM call on the same position more often than
    # ``hold_rescue_cooldown_minutes``. Bounded separately from the
    # generic Smart-Path cooldown because rescue is by design a
    # "wake the LLM up now" trigger.
    last_hold_rescue_at: datetime | None = None
    # Anti-thrash: last rescue verdict action so we can suppress an
    # immediate re-fire that would just hit the same answer.
    last_hold_rescue_action: str | None = None


@dataclass
class _LLMCallStats:
    """Process-lifetime telemetry on Smart-Path LLM usage.

    Surfaces a single counter the operator can read off the
    PositionReview panel to verify "Smart Path is rare" - the whole
    point of the two-tier architecture is to keep LLM round-trips
    bounded. Reset on process restart.
    """

    cycles_total: int = 0
    cycles_with_positions: int = 0
    smart_invocations: int = 0
    smart_skipped_cooldown: int = 0
    smart_skipped_first_review_delay: int = 0
    smart_skipped_no_gate: int = 0
    hold_rescues_fired: int = 0
    hold_rescues_suppressed: int = 0
    # Hold-reason taxonomy: every cycle where Fast Path returned
    # 'hold', classified by *why* (default = no gate, deliberate =
    # Smart Path actively voted hold, rescue-overridden = HOLD that
    # was upgraded to a close by the rescue rule).
    holds_default: int = 0
    holds_deliberate: int = 0
    holds_rescue_overridden: int = 0
    # Day-6+ polish: hard caps on LLM invocations regardless of
    # per-position gates / cooldowns. These exist so a multi-position
    # cycle with N simultaneous fired gates can never produce N LLM
    # round-trips - the global budget always wins.
    smart_skipped_per_cycle_budget: int = 0
    smart_skipped_global_budget: int = 0
    # Conservative multi-trigger requirement: at least 2 gates must
    # fire on the same position before the LLM is invoked.
    smart_skipped_conservative_multi_trigger: int = 0
    # Veto-only mode: count of positive overrides the LLM tried to
    # initiate from a Fast-Path HOLD that were blocked under
    # conservative + ``l3_conservative_veto_only=True``.
    smart_blocked_positive_override: int = 0
    # Fast-Path HOLD justification taxonomy ("HOLD must be earned"):
    # every Fast-Path HOLD now carries a positive justification or
    # is flagged as unearned. ``holds_unearned`` is the subset of
    # ``holds_default`` that the Fast Path could NOT justify
    # positively (no trend alignment, no profit lock-in, etc.).
    holds_unearned: int = 0
    holds_trend_intact: int = 0
    holds_ranging: int = 0
    holds_low_conviction_profitable: int = 0
    holds_no_data: int = 0
    # Position-state resync counters. ``resync_fresh_pulls`` counts
    # every successful executor.get_open_positions() call at the top
    # of review_open_positions. ``resync_recovered_missing`` is the
    # *important* one: it counts cycles where the fresh pull surfaced
    # a position the caller's AccountInfo snapshot was missing - i.e.
    # cycles where the desync bug WOULD have caused PM to ignore an
    # open position. ``resync_phantom_in_snapshot`` is the opposite:
    # the snapshot had a position that the fresh pull doesn't (we
    # closed it via a side-channel or it really vanished). Both are
    # logged at WARNING when they happen.
    resync_fresh_pulls: int = 0
    resync_recovered_missing: int = 0
    resync_phantom_in_snapshot: int = 0
    resync_fallbacks_to_snapshot: int = 0

    @property
    def llm_call_rate(self) -> float:
        """LLM round-trips per cycle-with-positions (0.0 ... 1.0+)."""
        if self.cycles_with_positions == 0:
            return 0.0
        return self.smart_invocations / float(self.cycles_with_positions)


@dataclass
class _LLMBudget:
    """Process-lifetime global cap on Smart-Path LLM invocations.

    The per-position cooldown ladder
    (``_HARD_COOLDOWN_FLOOR_MINUTES``, first-review delay,
    aggression shift, rescue cooldown) bounds how often a *single*
    position can wake the LLM. But none of those checks bound the
    *aggregate* call volume across all positions in one cycle: with
    N open positions all hitting gates simultaneously, the manager
    would fire N round-trips before this budget existed. That's the
    "stricter cooldown" the operator asked for.

    Two independent ceilings:

    * ``max_per_cycle`` (default 2). Hard cap on Smart-Path
      invocations within a single ``review_open_positions`` call.
      When more than ``max_per_cycle`` positions have fired gates,
      we keep the highest-priority candidates (see
      :func:`_smart_reason_priority`) and skip the rest with
      ``smart_skipped_per_cycle_budget``.
    * ``max_per_hour`` (default 8). Rolling-hour cap across the
      whole manager. When exhausted, no Smart-Path call goes out
      regardless of trigger severity - the Fast Path carries the
      cycle and ``smart_skipped_global_budget`` increments.

    Invocations are recorded in :attr:`invocations` as wall-clock
    timestamps; the hour window is garbage-collected on every
    :meth:`can_invoke` call so memory stays O(8) in steady state.

    Both ceilings are *operator-tunable* via :class:`PositionManagerConfig`
    (``llm_max_per_cycle`` / ``llm_max_per_hour``); they default to
    safety-first values that bound cost predictably even under a
    multi-position trigger storm. Set either to ``0`` to disable
    that ceiling entirely (not recommended).
    """

    max_per_cycle: int = 2
    max_per_hour: int = 8
    invocations: list[datetime] = field(default_factory=list)
    # Per-cycle counter; reset by ``reset_cycle`` at the top of
    # every ``review_open_positions`` call.
    _cycle_count: int = 0

    def reset_cycle(self) -> None:
        """Start a fresh per-cycle counter (called at cycle entry)."""
        self._cycle_count = 0

    def _gc_hour(self, now: datetime) -> None:
        cutoff = now - timedelta(hours=1)
        self.invocations = [t for t in self.invocations if t >= cutoff]

    def can_invoke(self, now: datetime) -> tuple[bool, str]:
        """Return ``(allowed, reason_if_denied)``.

        ``reason_if_denied`` is one of ``"per_cycle"`` / ``"per_hour"``
        / ``""`` (when allowed) so the caller can bump the right
        telemetry counter.
        """
        if self.max_per_cycle > 0 and self._cycle_count >= self.max_per_cycle:
            return False, "per_cycle"
        if self.max_per_hour > 0:
            self._gc_hour(now)
            if len(self.invocations) >= self.max_per_hour:
                return False, "per_hour"
        return True, ""

    def record(self, now: datetime) -> None:
        """Stamp an invocation against both windows."""
        self._cycle_count += 1
        if self.max_per_hour > 0:
            self.invocations.append(now)


# Hard floor on the per-position Smart-Path cooldown, enforced
# regardless of `.env`. This is a safety rail - operators may set
# L3_REVIEW_MIN_INTERVAL_MINUTES below this floor by mistake (or
# during a debugging session) but the manager will silently uphold
# the floor. Keeps OpenRouter cost predictable even if the operator
# misconfigures the cooldown.
_HARD_COOLDOWN_FLOOR_MINUTES: float = 25.0
# Per-aggression cooldown shift (minutes). Conservative wants the
# LLM as rarely as possible; aggressive is happy with more LLM
# calls. The mode shift is ADDED to the configured cooldown, then
# clamped against the hard floor above.
_AGGRESSION_COOLDOWN_SHIFT: dict[str, float] = {
    "conservative": +15.0,
    "balanced":       0.0,
    "aggressive":   -10.0,
}


def _smart_reason_priority(reasons: list[str]) -> int:
    """Return the highest-priority numeric rank across a reason list.

    Lower number = higher priority. The global LLM budget uses this
    to keep the most important Smart-Path invocations when more
    positions fire gates than the per-cycle budget allows.

    Priority ladder (top = most important):

      1. HOLD-rescue (operator-chosen contradiction with L2)
      2. Fast-Path close-audit (about to close - LLM may veto)
      3. HOLD-must-be-earned (long-held position re-audit)
      4. Forced periodic refresh (gap since last review)
      5. Price-move / funding / whale / OI gates
      6. First review (brand-new position)
      99. (no reasons)
    """
    if not reasons:
        return 99
    best = 99
    for r in reasons:
        if r.startswith("HOLD-rescue:"):
            best = min(best, 1)
        elif r.startswith("fast-path") and (
            "close" in r or "partial_close" in r
        ):
            best = min(best, 2)
        elif r.startswith("HOLD-must-be-earned"):
            best = min(best, 3)
        elif r.startswith("forced periodic review"):
            best = min(best, 4)
        elif r.startswith(("price moved", "funding rate", "whale activity", "OI(1h)")):
            best = min(best, 5)
        elif r.startswith("first review"):
            best = min(best, 6)
    return best


def _is_corroborating_reason(reason: str) -> bool:
    """Does this reason count toward conservative multi-trigger?

    Under conservative + ``l3_require_multi_trigger=True`` the
    Smart Path needs >= 2 independent gates. Rescue is exempt
    (already gated by its own cooldown) and first-review doesn't
    count as "evidence" (it's just a "new position, take a look"
    bootstrap). Everything else corroborates.
    """
    if reason.startswith("HOLD-rescue:"):
        return False
    if reason.startswith("first review"):
        return False
    return True


@dataclass
class _DailyDDTracker:
    """Process-lifetime portfolio PnL tracker (kill-switch).

    Captures the session-starting equity on the first call, then
    accumulates realised+unrealised PnL each cycle. Triggers a
    flatten when the relative loss crosses
    ``daily_loss_limit_pct``. Resets across UTC midnight rollovers
    so a long-running process gets a fresh budget every day.
    """

    starting_equity: Decimal | None = None
    session_started: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    breached: bool = False

    def update(
        self,
        equity: Decimal,
        pnl: Decimal,
        limit_pct: float,
        *,
        position_count: int = 0,
    ) -> tuple[bool, float]:
        """Return ``(breached, current_loss_pct)`` for this cycle.

        ``position_count`` is a sanity gate: a flat portfolio
        (``position_count == 0``) cannot physically have lost money
        from the previous cycle, so any non-zero ``session_pnl`` we
        compute against ``starting_equity`` MUST be either a deposit /
        withdraw (legitimate equity change unrelated to trading) or a
        bad equity / PnL read upstream. In both cases the safe move
        is to *re-baseline* the session to the current equity and
        clear ``breached`` rather than fire the kill-switch on a
        portfolio that has nothing to flatten. This is the second
        line of defence behind a clean ``equity / PnL`` formula -
        it's specifically designed to prevent a single bad read
        from auto-draining the venue when no positions are open.
        """
        now = datetime.now(timezone.utc)
        # Reset on UTC midnight rollover so a 24/7 process gets a
        # fresh budget every day.
        if (
            self.starting_equity is None
            or now.date() != self.session_started.date()
        ):
            self.starting_equity = equity - pnl  # equity at session start
            self.session_started = now
            self.breached = False
        # Sanity gate: flat portfolio cannot have realised losses
        # this cycle. Re-baseline silently and short-circuit any
        # latent ``breached`` flag so we never drive a flat account
        # through the risk-off pipeline.
        if position_count == 0:
            self.starting_equity = equity
            self.breached = False
            return False, 0.0
        # Equity already includes unrealised PnL on Hyperliquid; we
        # work with the implied session-PnL = current_equity - starting.
        if self.starting_equity is None or self.starting_equity <= 0:
            return self.breached, 0.0
        session_pnl = equity - self.starting_equity
        loss_pct = (
            float(-session_pnl / self.starting_equity * 100)
            if session_pnl < 0
            else 0.0
        )
        if loss_pct >= limit_pct:
            self.breached = True
        return self.breached, loss_pct


# ---------------------------------------------------------------------------
# Manager
# ---------------------------------------------------------------------------


# Priority order: first hit wins. Daily DD is the kill-switch; it
# must fire before any other branch can mutate exposure. Stop-loss
# comes next because losses are the most urgent state to react to.
_TRIGGER_PRIORITY: tuple[PositionTrigger, ...] = (
    "daily_dd_guard",
    "stop_loss",
    "vol_spike_close",
    "time_exit",
    "take_profit",
    "partial_take_profit",
    "trailing_stop",
    "side_flip",
    "re_evaluation",
)


class PositionManager:
    """Per-cycle two-tier stewardship of open perp positions.

    Parameters
    ----------
    config :
        :class:`PositionManagerConfig` with thresholds and feature flags.
    executor :
        Optional executor reference, used to fetch mid-prices and
        (when supported) request partial closes. The manager NEVER
        submits on-chain transactions itself - the router does that
        based on the :class:`PositionReview` it receives.
    position_arbiter :
        Optional async callable implementing the Smart Path. Defaults
        to the inert :func:`_null_arbiter` (always HOLD); production
        runs pass :func:`build_default_position_arbiter`.
    """

    def __init__(
        self,
        config: PositionManagerConfig,
        executor: Any | None = None,
        position_arbiter: PositionArbiterCallable | None = None,
    ) -> None:
        self.config = config
        self.executor = executor
        self._arbiter: PositionArbiterCallable = (
            position_arbiter or _null_arbiter
        )
        # Track whether we have a real (non-stub) arbiter so the
        # Smart-Path overlay can skip the round-trip entirely when no
        # LLM is wired - the synthetic arbiter is a no-op pass-through
        # and we shouldn't paint "L3 reviewed" badges or burn cycles
        # for it.
        self._arbiter_is_real = (
            position_arbiter is not None and position_arbiter is not _null_arbiter
        )
        self._states: dict[tuple[str, str], _PositionState] = {}
        self._daily_dd = _DailyDDTracker()
        # Soft block for the "daily DD breached" kill switch - the
        # router reads this to refuse new opens until the operator
        # restarts. Kept on the manager so panels can show the badge.
        self._daily_dd_active_block = False
        # Process-lifetime LLM-usage telemetry. Read off the
        # PositionReview panel header so the operator can verify the
        # Smart Path is staying rare (the architectural contract).
        self.stats = _LLMCallStats()
        # Global LLM budget enforced inside review_open_positions.
        # Per-position cooldowns bound how often a single position
        # can wake the LLM; this budget bounds the AGGREGATE volume
        # across all positions so a trigger storm can never produce
        # an N-positions-wide LLM round-trip storm.
        self.llm_budget = _LLMBudget(
            max_per_cycle=int(config.llm_max_per_cycle),
            max_per_hour=int(config.llm_max_per_hour),
        )

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    async def review_open_positions(
        self,
        *,
        account: AccountInfo,
        decision: DecisionResult | None = None,
        directive: ExecutionDirective | None = None,
    ) -> PositionReview:
        """Review every open position and emit a structured verdict.

        Walks the freshly-pulled open positions, evaluates the
        priority-ordered Fast-Path trigger ladder against each one,
        optionally invokes the Smart Path (L3) when one of the
        trigger gates fires, and returns a :class:`PositionReview`
        covering BOTH the actions the router should take and the
        telemetry the panel renders.

        **Resync-first.** Before doing ANY work, we ask the executor
        for the canonical list of currently-open positions via
        :meth:`_resync_open_positions` (which calls
        ``executor.get_open_positions()`` directly). This defeats the
        position-state desync that was observed on Hyperliquid
        Testnet, where ``account.positions`` could be silently empty
        even when the agent had just opened a short. See the
        docstring on :meth:`_resync_open_positions` for the failure
        modes the resync layer guards against. Legacy executors that
        don't expose ``get_open_positions`` fall back transparently
        to ``account.positions``.
        """
        # Resync-first: always trust the executor's fresh pull over
        # the (possibly stale) AccountInfo snapshot the router built.
        open_positions = await self._resync_open_positions(
            account.positions
        )
        review = PositionReview()

        # Telemetry: count every cycle (rare LLM = whole point).
        self.stats.cycles_total += 1
        if open_positions:
            self.stats.cycles_with_positions += 1

        # Update Daily-DD tracker every cycle, even when there are no
        # open positions, so the kill-switch keeps tracking equity
        # across flat-to-open transitions. ``position_count`` is the
        # sanity gate: a flat portfolio cannot have realised losses,
        # so the tracker re-baselines instead of firing for the
        # first time. ``_daily_dd_active_block`` (sticky) preserves
        # the "blocks new opens until process restart" semantics
        # even when the next cycle is flat - we want a triggered
        # kill-switch to NEVER silently disarm itself.
        breached, loss_pct = (False, 0.0)
        if self.config.enable_daily_dd_guard:
            breached, loss_pct = self._daily_dd.update(
                equity=account.equity_usd,
                pnl=account.total_unrealized_pnl_usd,
                limit_pct=self.config.daily_loss_limit_pct,
                position_count=len(open_positions),
            )
            if breached and not self._daily_dd_active_block:
                self._daily_dd_active_block = True
                logger.warning(
                    "DAILY-DD GUARD FIRED | session loss {:.2f}% >= "
                    "limit {:.2f}% - portfolio kill-switch engaged. "
                    "Router will flatten + block new opens until "
                    "process restart.",
                    loss_pct, self.config.daily_loss_limit_pct,
                )
            # Surface the sticky flag so a sanity-gated tracker
            # update doesn't silently undo a previous breach.
            effective_breached = breached or self._daily_dd_active_block
            review.daily_pnl_pct = -loss_pct if loss_pct > 0 else 0.0
            review.daily_dd_breached = effective_breached
            breached = effective_breached

        # Garbage-collect state for positions that no longer exist
        # (so the in-memory dict doesn't grow unbounded across cycles).
        self._gc_states(open_positions)

        if not open_positions:
            review.notes.append("no open positions to manage")
            if breached:
                review.notes.append(
                    f"daily-DD guard active "
                    f"(session loss={loss_pct:.2f}%); new opens blocked"
                )
            return review

        # Best-effort mid-price + ATR snapshot for the cycle. Mid
        # comes from the executor; ATR% comes from L1's per-symbol
        # raw payload on the decision (see _primary_atr_pct).
        mids: dict[str, Decimal | None] = {
            p.symbol: await self._safe_mid_price(p.symbol)
            for p in open_positions
        }
        atr_data = self._extract_atr_data(decision)
        l2_signals = self._extract_l2_signals(decision)

        # Refresh per-position state (ATR snapshot, opened_at, etc.)
        for pos in open_positions:
            self._ensure_state(pos, atr_data)

        # ---- Fast-Path evaluation -----------------------------------
        fast_actions: list[PositionAction] = []
        for pos in open_positions:
            action = self._fast_path_evaluate(
                pos,
                mid=mids.get(pos.symbol),
                directive=directive,
                decision=decision,
                atr_data=atr_data,
                daily_dd_breached=breached,
            )
            fast_actions.append(action)

        # ---- Smart-Path overlay (only on fired gates) ---------------
        # Three-phase pipeline:
        #   1. COLLECT: for every position, gather candidate reasons.
        #   2. BUDGET: enforce per-cycle / per-hour global ceilings -
        #      drop the lowest-priority candidates when more positions
        #      fire than fit in the budget.
        #   3. DISPATCH: invoke arbiter in parallel for survivors only.
        smart_verdicts: dict[tuple[str, str], SmartPathVerdict] = {}
        smart_briefings: dict[tuple[str, str], SmartPathBriefing] = {}
        rescue_flags: dict[tuple[str, str], bool] = {}
        if self.config.enable_smart_path and self._arbiter_is_real:
            # Phase 0: reset the per-cycle budget counter.
            self.llm_budget.reset_cycle()
            # Phase 1: collect every (pos, fast_action, reasons) tuple.
            candidates: list[
                tuple[Position, PositionAction, list[str]]
            ] = []
            for pos, fa in zip(open_positions, fast_actions):
                reasons = self._smart_path_reasons(
                    pos,
                    mid=mids.get(pos.symbol),
                    fast_action=fa,
                    atr_data=atr_data,
                    l2_signals=l2_signals,
                )
                if reasons:
                    candidates.append((pos, fa, reasons))
            # Phase 2: enforce the global budget. Sort by priority
            # (lowest numeric rank = most important) so the highest-
            # priority gates always make it through first.
            candidates.sort(key=lambda c: _smart_reason_priority(c[2]))
            tasks: list[tuple[tuple[str, str], asyncio.Task[SmartPathVerdict]]] = []
            now = datetime.now(timezone.utc)
            for pos, fa, reasons in candidates:
                allowed, deny_reason = self.llm_budget.can_invoke(now)
                if not allowed:
                    # Out of budget for this cycle / hour - skip with
                    # the right telemetry bucket and continue so the
                    # lower-priority candidates still get *counted*
                    # as skipped (visible in the panel).
                    if deny_reason == "per_cycle":
                        self.stats.smart_skipped_per_cycle_budget += 1
                        logger.info(
                            "Smart-Path per-cycle budget exhausted ({}/{}) "
                            "- skipping {}/{}; reasons={}",
                            self.llm_budget._cycle_count,
                            self.llm_budget.max_per_cycle,
                            pos.symbol, pos.side, reasons,
                        )
                    else:
                        self.stats.smart_skipped_global_budget += 1
                        logger.warning(
                            "Smart-Path hourly budget exhausted "
                            "({} calls in last hour, max={}) - "
                            "skipping {}/{}; reasons={}. Reduce "
                            "trigger sensitivity or raise budget.",
                            len(self.llm_budget.invocations),
                            self.llm_budget.max_per_hour,
                            pos.symbol, pos.side, reasons,
                        )
                    continue
                # Phase 3: dispatch.
                state = self._states[(pos.symbol, pos.side)]
                # Mark the L3 review as performed BEFORE awaiting -
                # avoids re-firing the same gate on the next cycle
                # while the LLM is still running.
                state.last_l3_check_at = now
                state.last_l3_check_price = mids.get(pos.symbol) or pos.mark_price
                # HOLD-rescue marker: any reason starting with
                # "HOLD-rescue:" is a rescue trigger.
                is_rescue = any(
                    r.startswith("HOLD-rescue:") for r in reasons
                )
                rescue_flags[(pos.symbol, pos.side)] = is_rescue
                if is_rescue:
                    state.last_hold_rescue_at = now
                    self.stats.hold_rescues_fired += 1
                self.llm_budget.record(now)
                self.stats.smart_invocations += 1
                briefing = self._build_smart_briefing(
                    pos,
                    mid=mids.get(pos.symbol),
                    fast_action=fa,
                    invocation_reasons=reasons,
                    directive=directive,
                    atr_data=atr_data,
                    l2_signals=l2_signals,
                )
                smart_briefings[(pos.symbol, pos.side)] = briefing
                tasks.append(
                    (
                        (pos.symbol, pos.side),
                        asyncio.create_task(self._safe_arbitrate(briefing)),
                    )
                )
            for key, task in tasks:
                try:
                    verdict = await task
                except Exception as exc:  # noqa: BLE001 - manager guards
                    logger.warning(
                        "Smart Path arbiter raised for {}: {} - "
                        "falling back to Fast Path verdict.",
                        key, exc,
                    )
                    verdict = SmartPathVerdict(
                        action="hold",
                        rationale=f"arbiter raised: {exc}",
                        confidence=0.0,
                    )
                smart_verdicts[key] = verdict
                self._states[key].last_smart_verdict = verdict

        # ---- Merge: Smart Path can override Fast Path ---------------
        final_actions: list[PositionAction] = []
        for pos, fa in zip(open_positions, fast_actions):
            key = (pos.symbol, pos.side)
            sv = smart_verdicts.get(key)
            if sv is not None and self.config.l3_can_override_fast_path:
                merged = self._merge_smart_over_fast(
                    pos, fa, sv,
                    hold_rescue_active=rescue_flags.get(key, False),
                )
                final_actions.append(merged)
            elif sv is not None:
                # Smart Path ran but is advisory - keep the Fast verdict
                # and stamp the L3 verdict as advisory metadata.
                fa.smart_snippet = sv.rationale[:240] if sv.rationale else None
                final_actions.append(fa)
            else:
                final_actions.append(fa)

        # ---- Render snapshots ---------------------------------------
        for pos, action, fa in zip(open_positions, final_actions, fast_actions):
            review.actions.append(action)
            sv = smart_verdicts.get((pos.symbol, pos.side))
            briefing = smart_briefings.get((pos.symbol, pos.side))
            # Classify HOLD: was it default (no gate), deliberate (LLM
            # actively voted HOLD with confidence) or rescue-overridden
            # (would have been HOLD but rescue closed/partial). Updates
            # the LLM stats counters used by the panel header.
            self._classify_hold(action, fa, sv)
            review.snapshots.append(
                self._snapshot_for(
                    pos,
                    action,
                    mid=mids.get(pos.symbol),
                    atr_data=atr_data,
                    smart_verdict=sv,
                    smart_briefing=briefing,
                )
            )
            if action.action != "hold":
                logger.info(
                    "PositionManager TRIGGER {} on {}/{} "
                    "| pnl={:.2f}% size={} source={} reason={}",
                    action.trigger.upper(),
                    action.symbol,
                    action.side.upper(),
                    action.pnl_pct * 100,
                    action.size_usd,
                    action.source.upper(),
                    action.reason,
                )

        return review

    def _classify_hold(
        self,
        final: PositionAction,
        fast: PositionAction,
        smart: SmartPathVerdict | None,
    ) -> None:
        """Bump the HOLD-reason taxonomy counters.

        Three buckets:
          * ``holds_default``         - Fast Path emitted HOLD AND no
                                        Smart Path verdict overrode it.
          * ``holds_deliberate``      - Fast Path was HOLD, Smart Path
                                        also voted HOLD at >= override
                                        confidence - the LLM actively
                                        confirmed the hold.
          * ``holds_rescue_overridden`` - Fast Path was HOLD but final
                                          action is non-hold (rescue
                                          closed/partial); not actually
                                          a HOLD, but tracked so the
                                          panel can show "HOLD was
                                          earned today" stats.

        Note: with the "HOLD must be earned" Day-6+ rewrite, Fast Path
        no longer emits a bare ``"hold"`` trigger - every HOLD now
        carries one of ``hold_trend_intact`` / ``hold_ranging`` /
        ``hold_low_conviction_profitable`` / ``hold_no_data`` /
        ``hold_unearned``. We classify by ``fast.action == "hold"``
        rather than by trigger so the taxonomy stays consistent.
        """
        if fast.action != "hold":
            return  # Fast Path didn't hold; not a hold case.
        if final.action != "hold":
            # Rescue/override turned a Fast-Path HOLD into a non-HOLD.
            self.stats.holds_rescue_overridden += 1
            return
        if smart is not None and smart.action == "hold" and smart.confidence >= 0.5:
            self.stats.holds_deliberate += 1
        else:
            self.stats.holds_default += 1

    # ------------------------------------------------------------------
    # Fast Path evaluation
    # ------------------------------------------------------------------

    def _fast_path_evaluate(
        self,
        pos: Position,
        *,
        mid: Decimal | None,
        directive: ExecutionDirective | None,
        decision: DecisionResult | None,
        atr_data: dict[str, dict[str, float]],
        daily_dd_breached: bool,
    ) -> PositionAction:
        """Run the priority-ordered Fast-Path trigger ladder."""
        pnl_frac = self._pnl_fraction(pos)
        key = (pos.symbol, pos.side)
        state = self._states[key]

        # Update trailing peak BEFORE evaluating triggers so the
        # trailing branch can compare to a fresh peak.
        prev_peak = state.peak_pnl_pct
        new_peak = max(pnl_frac, prev_peak) if prev_peak is not None else pnl_frac
        state.peak_pnl_pct = new_peak

        # Compute live ATR snapshots for this symbol; everything that
        # follows uses them.
        sym_atr = atr_data.get(pos.symbol, {})
        atr_pct_live = sym_atr.get("atr_pct_avg")
        atr_abs_live: Decimal | None = None
        ref_price = mid if mid is not None and mid > 0 else pos.mark_price
        if (
            atr_pct_live is not None
            and atr_pct_live > 0
            and ref_price > 0
        ):
            atr_abs_live = (
                ref_price * Decimal(str(atr_pct_live)) / Decimal("100")
            ).quantize(Decimal("0.0001"))

        # Pre-compute trigger PRICES for the snapshot (the panel needs
        # them whether or not a trigger fires).
        tp_px = self._tp_price(pos, atr_abs_live)
        sl_px = self._sl_price(pos, atr_abs_live, state=state)
        trail_px = self._trailing_price(
            pos,
            atr_abs_live,
            peak_pnl_frac=new_peak,
        )

        # Arm breakeven *before* the SL branch reads state, so the BE
        # lift applies on the same cycle the trigger fires.
        breakeven_armed_this_cycle = False
        if (
            self.config.enable_breakeven
            and not state.breakeven_armed
            and self._breakeven_should_arm(pos, atr_abs_live, pnl_frac)
        ):
            state.breakeven_armed = True
            breakeven_armed_this_cycle = True
            # Recompute SL with BE-armed state.
            sl_px = self._sl_price(pos, atr_abs_live, state=state)

        # ---- 1. Daily-DD kill switch ---------------------------------
        if daily_dd_breached:
            return PositionAction(
                symbol=pos.symbol, side=pos.side,
                action="close",
                trigger="daily_dd_guard",
                reason=(
                    "Daily-DD guard active: session PnL "
                    "breached the loss limit. Flatten."
                ),
                pnl_pct=pnl_frac, pnl_usd=pos.unrealized_pnl_usd,
                size_usd=pos.size_usd,
                take_profit_price=tp_px, stop_loss_price=sl_px,
                trailing_stop_price=trail_px, peak_pnl_pct=new_peak,
            )

        # ---- 2. Stop-loss (dynamic ATR or fallback fixed pct) --------
        sl_hit, sl_reason = self._stop_loss_hit(
            pos, pnl_frac=pnl_frac, atr_abs_live=atr_abs_live, mid=mid,
            sl_px=sl_px,
        )
        if sl_hit:
            return PositionAction(
                symbol=pos.symbol, side=pos.side,
                action="close",
                trigger="stop_loss",
                reason=sl_reason,
                pnl_pct=pnl_frac, pnl_usd=pos.unrealized_pnl_usd,
                size_usd=pos.size_usd,
                take_profit_price=tp_px, stop_loss_price=sl_px,
                trailing_stop_price=trail_px, peak_pnl_pct=new_peak,
            )

        # ---- 3. Vol-spike filter -------------------------------------
        spike = self._vol_spike_ratio(state, atr_pct_live)
        if (
            self.config.enable_vol_filter
            and spike is not None
            and spike >= self.config.vol_spike_mult
            and self.config.vol_spike_action == "close"
        ):
            return PositionAction(
                symbol=pos.symbol, side=pos.side,
                action="close",
                trigger="vol_spike_close",
                reason=(
                    f"ATR% jumped {spike:.2f}x vs entry "
                    f"(entry={state.entry_atr_pct:.2f}%, "
                    f"live={atr_pct_live:.2f}%) - vol-spike close."
                ),
                pnl_pct=pnl_frac, pnl_usd=pos.unrealized_pnl_usd,
                size_usd=pos.size_usd,
                take_profit_price=tp_px, stop_loss_price=sl_px,
                trailing_stop_price=trail_px, peak_pnl_pct=new_peak,
            )

        # ---- 4. Time exit --------------------------------------------
        if (
            self.config.enable_time_exit
            and self.config.max_position_hold_hours > 0
        ):
            age_hours = self._age_hours(state)
            if age_hours >= self.config.max_position_hold_hours:
                return PositionAction(
                    symbol=pos.symbol, side=pos.side,
                    action="close",
                    trigger="time_exit",
                    reason=(
                        f"position age {age_hours:.1f}h >= max "
                        f"{self.config.max_position_hold_hours:.1f}h "
                        "- thesis time-out, flatten."
                    ),
                    pnl_pct=pnl_frac, pnl_usd=pos.unrealized_pnl_usd,
                    size_usd=pos.size_usd,
                    take_profit_price=tp_px, stop_loss_price=sl_px,
                    trailing_stop_price=trail_px, peak_pnl_pct=new_peak,
                )

        # ---- 5. Take-profit (full, dynamic ATR or fallback) ----------
        tp_hit, tp_reason = self._take_profit_hit(
            pos, pnl_frac=pnl_frac, atr_abs_live=atr_abs_live, mid=mid,
            tp_px=tp_px,
        )
        if tp_hit:
            return PositionAction(
                symbol=pos.symbol, side=pos.side,
                action="close",
                trigger="take_profit",
                reason=tp_reason,
                pnl_pct=pnl_frac, pnl_usd=pos.unrealized_pnl_usd,
                size_usd=pos.size_usd,
                take_profit_price=tp_px, stop_loss_price=sl_px,
                trailing_stop_price=trail_px, peak_pnl_pct=new_peak,
            )

        # ---- 6. Partial take-profit (scale out once) -----------------
        if (
            self.config.enable_partial_take_profit
            and not state.partial_tp_done
        ):
            ptp_hit, ptp_reason, ptp_size = self._partial_tp_hit(
                pos, pnl_frac=pnl_frac, atr_abs_live=atr_abs_live, mid=mid,
            )
            if ptp_hit:
                state.partial_tp_done = True
                # When partial close fires we also arm BE on the
                # remainder - the operator wanted a *scale out and
                # let the rest ride*; BE protects against a give-back.
                state.breakeven_armed = True
                breakeven_armed_this_cycle = True
                return PositionAction(
                    symbol=pos.symbol, side=pos.side,
                    action="partial_close",
                    trigger="partial_take_profit",
                    reason=ptp_reason,
                    pnl_pct=pnl_frac, pnl_usd=pos.unrealized_pnl_usd,
                    size_usd=pos.size_usd, size_usd_to_close=ptp_size,
                    take_profit_price=tp_px, stop_loss_price=sl_px,
                    trailing_stop_price=trail_px, peak_pnl_pct=new_peak,
                )

        # ---- 7. Trailing stop ----------------------------------------
        trail_hit, trail_reason = self._trailing_stop_hit(
            pos, pnl_frac=pnl_frac, peak=new_peak,
            atr_abs_live=atr_abs_live, mid=mid,
        )
        if trail_hit:
            return PositionAction(
                symbol=pos.symbol, side=pos.side,
                action="close",
                trigger="trailing_stop",
                reason=trail_reason,
                pnl_pct=pnl_frac, pnl_usd=pos.unrealized_pnl_usd,
                size_usd=pos.size_usd,
                take_profit_price=tp_px, stop_loss_price=sl_px,
                trailing_stop_price=trail_px, peak_pnl_pct=new_peak,
            )

        # ---- 8. Side flip (engine flipped direction) -----------------
        if (
            self.config.enable_side_flip
            and self.config.auto_flip_on_side_change
            and directive is not None
            and directive.action == "risk_on"
            and directive.side
            and directive.side != "none"
            and directive.side != pos.side
        ):
            return PositionAction(
                symbol=pos.symbol, side=pos.side,
                action="close",
                trigger="side_flip",
                reason=(
                    f"directive flipped {pos.side.upper()} -> "
                    f"{directive.side.upper()} (conviction="
                    f"{directive.conviction:.2f}, strength="
                    f"{directive.direction_strength:.2f})"
                ),
                pnl_pct=pnl_frac, pnl_usd=pos.unrealized_pnl_usd,
                size_usd=pos.size_usd,
                take_profit_price=tp_px, stop_loss_price=sl_px,
                trailing_stop_price=trail_px, peak_pnl_pct=new_peak,
            )

        # ---- 9. Re-evaluation ----------------------------------------
        if (
            self.config.enable_re_evaluation
            and directive is not None
            and directive.action not in {"risk_off"}
            and directive.conviction < self.config.min_conviction_to_hold
            and pnl_frac >= float(self.config.re_eval_min_profit_pct)
        ):
            return PositionAction(
                symbol=pos.symbol, side=pos.side,
                action="close",
                trigger="re_evaluation",
                reason=(
                    f"conviction {directive.conviction:.2f} < "
                    f"min_to_hold {self.config.min_conviction_to_hold:.2f} "
                    f"while in modest profit ({pnl_frac * 100:+.2f}%) - "
                    "lock in gains"
                ),
                pnl_pct=pnl_frac, pnl_usd=pos.unrealized_pnl_usd,
                size_usd=pos.size_usd,
                take_profit_price=tp_px, stop_loss_price=sl_px,
                trailing_stop_price=trail_px, peak_pnl_pct=new_peak,
            )

        # ---- State-only verbs (no on-chain change) -------------------
        if breakeven_armed_this_cycle:
            return PositionAction(
                symbol=pos.symbol, side=pos.side,
                action="arm_breakeven",
                trigger="breakeven_arm",
                reason=(
                    "profit >= breakeven trigger; lifting effective SL "
                    "to entry + buffer"
                ),
                pnl_pct=pnl_frac, pnl_usd=pos.unrealized_pnl_usd,
                size_usd=pos.size_usd,
                take_profit_price=tp_px, stop_loss_price=sl_px,
                trailing_stop_price=trail_px, peak_pnl_pct=new_peak,
            )
        if (
            self.config.enable_vol_filter
            and spike is not None
            and spike >= self.config.vol_spike_mult
            and self.config.vol_spike_action == "tighten_stop"
        ):
            return PositionAction(
                symbol=pos.symbol, side=pos.side,
                action="tighten_stop",
                trigger="vol_spike_warn",
                reason=(
                    f"ATR% jumped {spike:.2f}x vs entry "
                    f"(entry={state.entry_atr_pct:.2f}%, "
                    f"live={atr_pct_live:.2f}%) - SL widened by live ATR; "
                    "consider closing if vol persists."
                ),
                pnl_pct=pnl_frac, pnl_usd=pos.unrealized_pnl_usd,
                size_usd=pos.size_usd,
                take_profit_price=tp_px, stop_loss_price=sl_px,
                trailing_stop_price=trail_px, peak_pnl_pct=new_peak,
            )

        # ---- Hold (must be earned) -----------------------------------
        # No earlier trigger fired. We now classify the HOLD into one
        # of four positive-justification buckets or flag it as
        # ``hold_unearned``. This makes HOLD a *conscious* verdict
        # rather than a fall-through default - any HOLD without a
        # positive reason becomes a panel warning + a candidate for
        # the next Smart-Path review.
        justification, justification_reason = self._justify_hold(
            pos,
            pnl_frac=pnl_frac,
            directive=directive,
            atr_data=atr_data,
            atr_pct_live=atr_pct_live,
            state=state,
        )
        # Bump the matching taxonomy counter (used by the panel).
        if justification == "hold_trend_intact":
            self.stats.holds_trend_intact += 1
        elif justification == "hold_ranging":
            self.stats.holds_ranging += 1
        elif justification == "hold_low_conviction_profitable":
            self.stats.holds_low_conviction_profitable += 1
        elif justification == "hold_no_data":
            self.stats.holds_no_data += 1
        elif justification == "hold_unearned":
            self.stats.holds_unearned += 1
        return PositionAction(
            symbol=pos.symbol, side=pos.side,
            action="hold",
            trigger=justification,
            reason=justification_reason,
            pnl_pct=pnl_frac, pnl_usd=pos.unrealized_pnl_usd,
            size_usd=pos.size_usd,
            take_profit_price=tp_px, stop_loss_price=sl_px,
            trailing_stop_price=trail_px, peak_pnl_pct=new_peak,
        )

    def _justify_hold(
        self,
        pos: Position,
        *,
        pnl_frac: float,
        directive: ExecutionDirective | None,
        atr_data: dict[str, dict[str, float]],
        atr_pct_live: float | None,
        state: _PositionState,
    ) -> tuple[PositionTrigger, str]:
        """Classify a Fast-Path HOLD with a POSITIVE justification.

        Returns ``(trigger, reason_string)``. ``trigger`` is one of:

        * ``hold_trend_intact`` - directive aligns with position side
          AND conviction stays at or above ``min_conviction_to_hold``.
          The agent is *deliberately* holding because the trend it
          opened on hasn't broken.
        * ``hold_ranging`` - we have live ATR data but no directional
          signal (directive is hold / neutral / aligned with low
          conviction) AND price sits within a small ATR band of
          entry. A textbook "let the market breathe" hold.
        * ``hold_low_conviction_profitable`` - position is in modest
          profit AND conviction is in the gap between
          ``re_eval_min_profit_pct`` triggering and
          ``min_conviction_to_hold`` collapsing. Conservative choice
          to let a winner ride for one more cycle.
        * ``hold_no_data`` - missing ATR / L2 data. Defensive hold
          because we can't decide either way. The panel paints this
          in a distinct colour so the operator knows to investigate
          the data feed.
        * ``hold_unearned`` - none of the above apply. This is the
          "we have no good reason to hold but no good reason to
          close either" case. Surfaced loudly in the panel and is
          the first thing HOLD-must-be-earned reviews when it fires.

        This is the structural answer to "HOLD must be earned": every
        HOLD now carries a verdict on *why*, not just an empty fall-
        through. The :class:`_LLMCallStats` taxonomy makes the
        distribution visible (e.g. "70% of HOLDs are trend_intact, 5%
        unearned" vs "60% unearned" - the latter is a flashing red
        light).
        """
        sym_atr = atr_data.get(pos.symbol, {})
        atr_pct_avg = sym_atr.get("atr_pct_avg")

        # 1) No data: defensive HOLD. Both signals missing -> we just
        #    don't know enough to act.
        if atr_pct_live is None and atr_pct_avg is None:
            return (
                "hold_no_data",
                "DEFENSIVE HOLD: no live ATR/L2 data for this symbol "
                "this cycle - holding until the next cycle's data lands.",
            )

        # 2) Trend intact: directive agrees AND conviction healthy.
        pos_sign = 1 if pos.side == "long" else (-1 if pos.side == "short" else 0)
        directive_side_sign = 0
        directive_conv = 0.0
        if directive is not None and directive.side:
            d_side = directive.side.lower()
            if d_side == "long":
                directive_side_sign = 1
            elif d_side == "short":
                directive_side_sign = -1
            directive_conv = float(directive.conviction or 0.0)
        if (
            directive_side_sign != 0
            and directive_side_sign == pos_sign
            and directive_conv >= self.config.min_conviction_to_hold
        ):
            return (
                "hold_trend_intact",
                (
                    f"EARNED HOLD: directive still aligns with "
                    f"{pos.side.upper()} (conviction "
                    f"{directive_conv:.2f} >= "
                    f"{self.config.min_conviction_to_hold:.2f}); "
                    f"trend intact - let the position breathe."
                ),
            )

        # 3) Low conviction but in profit: conservative ride-along.
        if (
            pnl_frac >= float(self.config.re_eval_min_profit_pct)
            and 0 < directive_conv < self.config.min_conviction_to_hold
        ):
            return (
                "hold_low_conviction_profitable",
                (
                    f"EARNED HOLD: in modest profit ({pnl_frac * 100:+.2f}%) "
                    f"with conviction {directive_conv:.2f} below the hold "
                    f"floor {self.config.min_conviction_to_hold:.2f} - "
                    f"letting the winner ride for one more cycle before "
                    f"re-evaluation."
                ),
            )

        # 4) Ranging: ATR data available, price within ~1x ATR of
        #    entry (a quiet market that hasn't committed). We accept
        #    this as a HOLD because no exit signal has fired and the
        #    market hasn't moved enough to invalidate the thesis.
        if (
            atr_pct_live is not None
            and atr_pct_live > 0
            and pos.entry_price > 0
        ):
            entry = pos.entry_price
            mark = pos.mark_price if pos.mark_price > 0 else entry
            move_pct = abs(float(mark - entry) / float(entry) * 100)
            if move_pct < atr_pct_live:
                return (
                    "hold_ranging",
                    (
                        f"EARNED HOLD: ranging market - price moved "
                        f"{move_pct:.2f}% (< 1.00x ATR={atr_pct_live:.2f}%) "
                        f"from entry; thesis intact, no exit signal."
                    ),
                )

        # 5) None of the above -> unearned HOLD.
        return (
            "hold_unearned",
            (
                "UNEARNED HOLD: no positive justification - directive "
                "drifted away from this side, position is not meaningfully "
                "profitable, and price has moved beyond the ranging band. "
                "HOLD-must-be-earned will review this on the next eligible "
                "cycle."
            ),
        )

    # ------------------------------------------------------------------
    # Smart Path (L3 per-position arbiter)
    # ------------------------------------------------------------------

    def _effective_min_cooldown_minutes(self) -> float:
        """Resolve the per-position Smart-Path cooldown floor.

        Combines three sources, with the LARGEST always winning:

          * the hard floor constant ``_HARD_COOLDOWN_FLOOR_MINUTES``
            (25 min) - cannot be overridden by ``.env``
          * the configured ``l3_review_min_interval_minutes``
          * the aggression-mode shift (conservative adds 15 min,
            aggressive subtracts 10 min)

        This is the contract behind "Smart Path must be maximally
        rare": even a buggy ``.env`` with
        ``L3_REVIEW_MIN_INTERVAL_MINUTES=1`` cannot push the LLM
        below the floor.
        """
        configured = float(self.config.l3_review_min_interval_minutes or 0.0)
        shift = _AGGRESSION_COOLDOWN_SHIFT.get(self.config.l3_aggression, 0.0)
        with_shift = configured + shift
        return max(_HARD_COOLDOWN_FLOOR_MINUTES, with_shift)

    def _smart_path_reasons(
        self,
        pos: Position,
        *,
        mid: Decimal | None,
        fast_action: PositionAction,
        atr_data: dict[str, dict[str, float]],
        l2_signals: dict[str, dict[str, float]],
    ) -> list[str]:
        """Decide whether to invoke the LLM Smart-Path on this position.

        Returns a list of reasons (strings) - empty list = skip. Each
        gate is OR-combined and we surface ALL fired gates so the
        Smart-Path briefing can show the LLM *why* it was called.

        Gate evaluation order:

          0. Hard cooldown floor + first-review delay (kill switches;
             return [] immediately when violated, regardless of any
             other trigger).
          1. Fast-Path close-audit (any close → review).
          2. Periodic forced refresh.
          3. Price-move / funding / whale / OI gates.
          4. HOLD-rescue rule (with its own short cooldown).
          5. HOLD-must-be-earned periodic re-audit.
        """
        state = self._states[(pos.symbol, pos.side)]
        reasons: list[str] = []
        now = datetime.now(timezone.utc)
        sym_l2 = l2_signals.get(pos.symbol, {})
        effective_min = self._effective_min_cooldown_minutes()

        # ---- Kill switch 1: first-review delay --------------------
        # Brand-new position: don't invoke L3 in the first
        # ``first_review_delay_minutes``. Let Fast Path observe the
        # entry first - the LLM has nothing useful to say about a
        # 30-second-old position.
        age_minutes = (now - state.opened_at).total_seconds() / 60.0
        if (
            state.last_l3_check_at is None
            and age_minutes < self.config.first_review_delay_minutes
        ):
            self.stats.smart_skipped_first_review_delay += 1
            return []

        # ---- Kill switch 2: hard cooldown floor -------------------
        # When the last L3 check is recent enough to be inside the
        # effective cooldown (hard floor + aggression shift), we skip
        # the entire round of gates - even close-audit gates. The only
        # exceptions are the explicit always-fires conditions below
        # (close-audit + rescue), which each carry their own cooldown.
        if state.last_l3_check_at is not None:
            minutes_since = (
                (now - state.last_l3_check_at).total_seconds() / 60.0
            )
        else:
            minutes_since = None  # never reviewed

        if minutes_since is not None and minutes_since < effective_min:
            # Generic gates are silenced; ONLY the close-audit and
            # rescue gates can break through (they each carry their
            # own cooldown bookkeeping).
            if fast_action.action in {"close", "partial_close"}:
                reasons.append(
                    f"fast-path '{fast_action.trigger}' wants to "
                    f"{fast_action.action} (close-audit always invokes L3)"
                )
            # Rescue stays evaluated below; everything else is muted.
        else:
            # Generic gates fully active.
            if fast_action.action in {"close", "partial_close"}:
                reasons.append(
                    f"fast-path '{fast_action.trigger}' wants to "
                    f"{fast_action.action}"
                )

            # Gate: first review or forced periodic refresh.
            if minutes_since is None:
                reasons.append(
                    f"first review (position age "
                    f"{age_minutes:.1f}min >= first_review_delay)"
                )
            elif minutes_since >= self.config.l3_review_max_interval_minutes:
                reasons.append(
                    f"forced periodic review ({minutes_since:.0f}min >= "
                    f"{self.config.l3_review_max_interval_minutes:.0f}min)"
                )

            # Gate: price moved >= N x ATR since last L3 check.
            atr_abs = self._atr_abs_for(pos, mid, atr_data)
            if state.last_l3_check_at is not None and atr_abs and atr_abs > 0:
                last_px = state.last_l3_check_price or pos.entry_price
                cur_px = mid if mid is not None and mid > 0 else pos.mark_price
                move = abs(cur_px - last_px)
                move_in_atrs = float(move / atr_abs) if atr_abs > 0 else 0.0
                if move_in_atrs >= self.config.l3_review_trigger_price_atr_mult:
                    reasons.append(
                        f"price moved {move_in_atrs:.2f}x ATR since last L3 "
                        f"({last_px} -> {cur_px})"
                    )

            # Gate: funding spike.
            funding = sym_l2.get("funding_rate")
            if (
                funding is not None
                and abs(float(funding)) >= self.config.l3_review_trigger_funding_rate
            ):
                reasons.append(
                    f"funding rate {float(funding) * 100:.4f}% >= "
                    f"trigger {self.config.l3_review_trigger_funding_rate * 100:.4f}%"
                )
            # Gate: whale activity.
            n_whales = int(sym_l2.get("n_whales", 0) or 0)
            if n_whales >= self.config.l3_review_trigger_whale_count:
                reasons.append(
                    f"whale activity n={n_whales} >= "
                    f"trigger {self.config.l3_review_trigger_whale_count}"
                )
            # Gate: OI delta.
            oi_1h = sym_l2.get("oi_delta_1h_pct")
            if (
                oi_1h is not None
                and abs(float(oi_1h)) >= self.config.l3_review_trigger_oi_delta_pct
            ):
                reasons.append(
                    f"OI(1h) delta {float(oi_1h):+.2f}% >= trigger "
                    f"{self.config.l3_review_trigger_oi_delta_pct:.2f}%"
                )

            # Gate: HOLD-must-be-earned periodic re-audit. When a
            # single position has been HOLDing for a long time the
            # operator wants a fresh L3 verdict even if no on-chain
            # signal moved. Disabled by default; auto-on under
            # aggressive.
            if (
                self.config.hold_must_be_earned
                and self.config.hold_must_be_earned_minutes > 0
                and fast_action.action == "hold"
                and age_minutes >= self.config.hold_must_be_earned_minutes
            ):
                last_smart_action = (
                    state.last_smart_verdict.action
                    if state.last_smart_verdict is not None
                    else None
                )
                # Only fire if the LAST smart verdict wasn't already
                # an explicit hold - otherwise we'd hit the same
                # answer immediately after the cooldown.
                if last_smart_action != "hold":
                    reasons.append(
                        f"HOLD-must-be-earned: position age "
                        f"{age_minutes:.0f}min >= "
                        f"{self.config.hold_must_be_earned_minutes:.0f}min; "
                        "re-auditing whether HOLD is still the right call"
                    )

        # ---- Rescue gate: own cooldown, evaluated even mid-cooldown -
        if (
            self.config.enable_hold_rescue
            and fast_action.action == "hold"
        ):
            l2_conv = float(sym_l2.get("l2_conviction") or 0.0)
            l2_dir = int(sym_l2.get("l2_direction_sign") or 0)
            pos_dir = 1 if pos.side == "long" else (-1 if pos.side == "short" else 0)
            mode = self.config.hold_rescue_direction_mode
            # Direction filter:
            #   - "opposite" -> rescue only when L2 contradicts position
            #   - "any"      -> rescue on EITHER side (close-or-scale)
            if mode == "opposite":
                direction_match = l2_dir != 0 and pos_dir != 0 and l2_dir != pos_dir
            else:  # "any" (aggressive default)
                direction_match = l2_dir != 0 and pos_dir != 0
            # Rescue's own cooldown: don't fire more than once every
            # hold_rescue_cooldown_minutes per position. We always
            # respect the hard floor too.
            rescue_cool = max(
                _HARD_COOLDOWN_FLOOR_MINUTES * 0.8,  # 20min if floor=25
                self.config.hold_rescue_cooldown_minutes,
            )
            recent_rescue = (
                state.last_hold_rescue_at is not None
                and (now - state.last_hold_rescue_at).total_seconds() / 60.0
                < rescue_cool
            )
            if (
                l2_conv >= self.config.hold_rescue_l2_min
                and direction_match
                and not recent_rescue
            ):
                stance = "OPPOSITE" if (pos_dir and l2_dir != pos_dir) else "SAME"
                reasons.append(
                    f"HOLD-rescue: fast path wants HOLD but L2 conviction "
                    f"{l2_conv:.2f} >= {self.config.hold_rescue_l2_min:.2f} "
                    f"in {stance} direction "
                    f"({'bullish' if l2_dir > 0 else 'bearish'} vs "
                    f"{pos.side.upper()} position; mode={mode})"
                )
            elif recent_rescue and l2_conv >= self.config.hold_rescue_l2_min:
                # Trigger would have fired but cooldown suppressed it -
                # log for telemetry so the operator can audit.
                self.stats.hold_rescues_suppressed += 1

        # ---- Conservative multi-trigger gate ------------------------
        # Under conservative + l3_require_multi_trigger, we demand
        # >= 2 *corroborating* gates before invoking the LLM. A
        # single price-move or single funding spike isn't enough; we
        # want at least two independent signals before paying the
        # OpenRouter round-trip. Rescue triggers are exempt because
        # they have their own dedicated cooldown bookkeeping and
        # represent an explicit operator-chosen "L2 strongly
        # disagrees" alarm. First-review doesn't count as evidence
        # (it's a bootstrap). Close-audit is corroboration because
        # it represents Fast Path consensus to act.
        if (
            self.config.l3_aggression == "conservative"
            and self.config.l3_require_multi_trigger
            and reasons
        ):
            corroborating = [r for r in reasons if _is_corroborating_reason(r)]
            has_rescue = any(r.startswith("HOLD-rescue:") for r in reasons)
            # Rescue alone is allowed (it has its own cooldown).
            # Corroborating-alone needs >= 2.
            if not has_rescue and len(corroborating) < 2:
                self.stats.smart_skipped_conservative_multi_trigger += 1
                logger.info(
                    "Conservative multi-trigger gate: only {} corroborating "
                    "gate(s) fired for {}/{} (need >= 2); skipping. "
                    "Reasons: {}",
                    len(corroborating),
                    pos.symbol, pos.side, reasons,
                )
                return []

        # ---- Telemetry: classify the skip --------------------------
        if not reasons:
            if minutes_since is not None and minutes_since < effective_min:
                self.stats.smart_skipped_cooldown += 1
            else:
                self.stats.smart_skipped_no_gate += 1

        return reasons

    def _build_smart_briefing(
        self,
        pos: Position,
        *,
        mid: Decimal | None,
        fast_action: PositionAction,
        invocation_reasons: list[str],
        directive: ExecutionDirective | None,
        atr_data: dict[str, dict[str, float]],
        l2_signals: dict[str, dict[str, float]],
    ) -> SmartPathBriefing:
        state = self._states[(pos.symbol, pos.side)]
        sym_atr = atr_data.get(pos.symbol, {})
        sym_l2 = l2_signals.get(pos.symbol, {})
        ref_price = mid if mid is not None and mid > 0 else pos.mark_price
        hold_rescue = any(
            r.startswith("HOLD-rescue:") for r in invocation_reasons
        )
        return SmartPathBriefing(
            symbol=pos.symbol,
            side=pos.side,
            entry_price=pos.entry_price,
            mark_price=ref_price,
            size_usd=pos.size_usd,
            leverage=pos.leverage,
            pnl_usd=pos.unrealized_pnl_usd,
            pnl_pct=self._pnl_fraction(pos),
            age_minutes=self._age_minutes(state),
            peak_pnl_pct=state.peak_pnl_pct,
            breakeven_armed=state.breakeven_armed,
            partial_tp_done=state.partial_tp_done,
            current_atr_pct=sym_atr.get("atr_pct_avg"),
            entry_atr_pct=state.entry_atr_pct,
            atr_cap_pct=self.config.atr_caps.cap_for(pos.symbol),
            invocation_reasons=list(invocation_reasons),
            funding_rate=sym_l2.get("funding_rate"),
            oi_delta_1h_pct=sym_l2.get("oi_delta_1h_pct"),
            whale_count=int(sym_l2.get("n_whales", 0) or 0),
            whale_direction=sym_l2.get("whale_direction"),
            l2_conviction=float(sym_l2.get("l2_conviction") or 0.0),
            l2_direction_sign=int(sym_l2.get("l2_direction_sign") or 0),
            engine_directive_action=(
                directive.action if directive is not None else "hold"
            ),
            engine_directive_side=(
                directive.side if directive is not None else None
            ),
            engine_directive_conviction=(
                directive.conviction if directive is not None else 0.0
            ),
            hold_rescue_active=hold_rescue,
            l3_aggression=self.config.l3_aggression,
        )

    async def _safe_arbitrate(
        self, briefing: SmartPathBriefing
    ) -> SmartPathVerdict:
        """Call the arbiter, returning a defensive default on raise."""
        verdict = await self._arbiter(briefing)
        if not isinstance(verdict, SmartPathVerdict):
            # Defensive: arbiter contract violation - log and fall back.
            logger.warning(
                "Smart-Path arbiter for {}/{} returned non-SmartPathVerdict "
                "({}); coercing to HOLD.",
                briefing.symbol, briefing.side, type(verdict).__name__,
            )
            return SmartPathVerdict(action="hold", confidence=0.0)
        return verdict

    # ------------------------------------------------------------------
    # L3_AGGRESSION calibration constants
    # ------------------------------------------------------------------
    # The tuple is (override_confidence_threshold, size_multiplier).
    # override_threshold: minimum confidence required for the Smart
    #     Path verdict to actually override the Fast Path action.
    #     Conservative raises the bar; aggressive lowers it.
    # size_multiplier: applied to close_partial fraction and to
    #     advisory snippet text emphasis - it doesn't grow the
    #     position, only modulates the *aggressiveness of close*.
    _AGGRESSION_CALIBRATION = {
        "conservative": (0.75, 0.85),
        "balanced":     (0.55, 1.00),
        "aggressive":   (0.45, 1.15),
    }

    def _calibrate_smart_verdict(
        self,
        smart: SmartPathVerdict,
        *,
        fast_was_hold: bool,
        hold_rescue_active: bool,
    ) -> tuple[SmartPathVerdict, float, float]:
        """Apply the L3_AGGRESSION calibration to a raw verdict.

        Returns ``(calibrated_smart, override_threshold, size_mult)``.

        * The override threshold is the minimum confidence the smart
          verdict needs for ``_merge_smart_over_fast`` to actually
          take it (regardless of confidence the verdict is always
          stamped as advisory metadata on the snapshot).
        * The size multiplier scales the partial-close fraction
          when ``smart.action == "close_partial"``. We never grow
          the fraction beyond [0, 1].
        * Under ``aggressive`` mode, a HOLD-rescue invocation that
          comes back with ``action="hold"`` is REWRITTEN to a small
          partial close - we don't let the LLM duck the rescue
          rule. This mirrors the engine-level HOLD-rescue.

        The original ``smart`` is never mutated in-place; we return
        a fresh dataclass.
        """
        mode = self.config.l3_aggression
        override_thresh, size_mult = self._AGGRESSION_CALIBRATION.get(
            mode, (0.55, 1.0)
        )

        calibrated = SmartPathVerdict(
            action=smart.action,
            rationale=smart.rationale,
            confidence=smart.confidence,
            close_fraction=smart.close_fraction,
        )

        # HOLD-rescue safety net: under aggressive mode, an LLM HOLD
        # response to a rescue trigger is rewritten to a small
        # partial close (default 25% of the position). The Fast Path
        # was about to HOLD anyway, so refusing to act would just
        # waste the rescue invocation. We DON'T do this under
        # balanced / conservative (operators chose those modes to be
        # less interventionist).
        if (
            hold_rescue_active
            and fast_was_hold
            and smart.action == "hold"
            and mode == "aggressive"
        ):
            calibrated = SmartPathVerdict(
                action="close_partial",
                rationale=(
                    f"[HOLD-rescue safety net] LLM voted HOLD on "
                    f"rescue trigger; aggressive mode forces a "
                    f"defensive partial close. Original rationale: "
                    f"{smart.rationale or '(none)'}"
                ),
                confidence=max(smart.confidence, override_thresh),
                close_fraction=Decimal("0.25"),
            )

        # Scale the partial-close fraction by the size multiplier.
        if calibrated.action == "close_partial":
            base_frac = calibrated.close_fraction
            if base_frac is None or base_frac <= 0 or base_frac >= 1:
                base_frac = self.config.partial_tp_fraction
            scaled = base_frac * Decimal(str(size_mult))
            scaled = max(Decimal("0.05"), min(Decimal("0.95"), scaled))
            calibrated = SmartPathVerdict(
                action=calibrated.action,
                rationale=calibrated.rationale,
                confidence=calibrated.confidence,
                close_fraction=scaled.quantize(Decimal("0.0001")),
            )

        return calibrated, override_thresh, size_mult

    def _merge_smart_over_fast(
        self,
        pos: Position,
        fast: PositionAction,
        smart: SmartPathVerdict,
        *,
        hold_rescue_active: bool = False,
    ) -> PositionAction:
        """Apply a Smart-Path verdict on top of the Fast-Path action.

        Honours :attr:`PositionManagerConfig.l3_aggression`:

        * **conservative** - high override bar (0.75 confidence),
          smaller partial closes (0.85x). The LLM has to be *sure*
          to overrule the deterministic Fast Path.
        * **balanced** (DEFAULT) - 0.55 override bar, no size
          scaling. Mirrors the historical behaviour.
        * **aggressive** - 0.45 override bar, partial closes scale
          1.15x, AND a HOLD-rescue invocation that comes back with
          ``action="hold"`` is rewritten to a small defensive
          partial close.

        The Smart Path can:

        * **VETO** a Fast-Path close by returning ``action="hold"``
          at >= override_threshold confidence. In that case we
          downgrade the action to a HOLD with the L3 rationale.
        * **UPGRADE** a Fast-Path hold to a close by returning
          ``close_full`` or ``close_partial``.
        * **REQUEST** a state-only change (``tighten_stop`` /
          ``raise_target``) - those are applied as advisory metadata
          today; future work can resize on-venue triggers from them.

        Low-confidence verdicts get stamped on ``smart_snippet`` but
        don't change the Fast Path verdict.
        """
        smart, override_thresh, _size_mult = self._calibrate_smart_verdict(
            smart,
            fast_was_hold=(fast.action == "hold"),
            hold_rescue_active=hold_rescue_active,
        )

        # ---- Conservative veto-only gate ----------------------------
        # When operating under conservative + l3_conservative_veto_only,
        # the LLM is allowed to be a BRAKE on the Fast Path but never
        # an ACCELERATOR:
        #
        #   * VETO of a Fast-Path close (smart=hold) -> ALLOWED
        #   * DOWNGRADE close -> partial_close       -> ALLOWED
        #   * Advisory tighten_stop / raise_target   -> ALLOWED
        #   * POSITIVE OVERRIDE of a Fast-Path HOLD into close /
        #     close_partial                          -> BLOCKED here
        #
        # The HOLD-rescue path is exempt because rescue is, by design,
        # the operator explicitly asking the LLM to act on L2
        # contradiction - we won't block what the operator opted into.
        # Outside of rescue, conservative + veto-only means "trust
        # the Fast Path's HOLD; the LLM doesn't get to overrule it
        # into action".
        if (
            self.config.l3_aggression == "conservative"
            and self.config.l3_conservative_veto_only
            and not hold_rescue_active
            and fast.action == "hold"
            and smart.action in {"close_full", "close_partial"}
            and smart.confidence >= override_thresh
        ):
            self.stats.smart_blocked_positive_override += 1
            logger.info(
                "Conservative veto-only: BLOCKED positive override on "
                "{}/{} (smart=`{}` conf={:.2f} >= {:.2f}). Fast-Path "
                "HOLD stands. Rationale snippet: {}",
                pos.symbol, pos.side,
                smart.action, smart.confidence, override_thresh,
                (smart.rationale or "")[:160],
            )
            fast.smart_snippet = (
                f"[BLOCKED veto-only] LLM voted {smart.action} "
                f"(conf={smart.confidence:.2f}): "
                f"{smart.rationale or '(no rationale)'}"
            )[:240]
            return fast

        # Rescue-mode trigger label takes precedence over the regular
        # l3_smart_* labels so the panel highlights this lineage.
        def _trigger(default: PositionTrigger) -> PositionTrigger:
            return "l3_hold_rescue" if hold_rescue_active else default

        # Defensive defaults: a quiet / low-confidence verdict never
        # overrides the Fast Path.
        if smart.confidence < override_thresh or smart.action == "hold":
            # If the Fast Path was about to close and Smart actively
            # said HOLD with sufficient confidence, that's a veto.
            if (
                smart.action == "hold"
                and smart.confidence >= override_thresh
                and fast.action in {"close", "partial_close"}
            ):
                return PositionAction(
                    symbol=pos.symbol, side=pos.side,
                    action="hold",
                    trigger=_trigger("l3_smart_hold"),
                    reason=(
                        f"L3 vetoed Fast-Path '{fast.trigger}' "
                        f"(mode={self.config.l3_aggression}, "
                        f"override_thresh={override_thresh:.2f}): "
                        f"{smart.rationale or 'no rationale'}"
                    ),
                    pnl_pct=fast.pnl_pct,
                    pnl_usd=fast.pnl_usd,
                    size_usd=fast.size_usd,
                    take_profit_price=fast.take_profit_price,
                    stop_loss_price=fast.stop_loss_price,
                    trailing_stop_price=fast.trailing_stop_price,
                    peak_pnl_pct=fast.peak_pnl_pct,
                    source="smart",
                    smart_snippet=smart.rationale[:240] if smart.rationale else None,
                )
            fast.smart_snippet = smart.rationale[:240] if smart.rationale else None
            return fast

        if smart.action == "close_full":
            return PositionAction(
                symbol=pos.symbol, side=pos.side,
                action="close",
                trigger=_trigger("l3_smart_close"),
                reason=f"L3 smart-close: {smart.rationale or 'no rationale'}",
                pnl_pct=fast.pnl_pct,
                pnl_usd=fast.pnl_usd,
                size_usd=fast.size_usd,
                take_profit_price=fast.take_profit_price,
                stop_loss_price=fast.stop_loss_price,
                trailing_stop_price=fast.trailing_stop_price,
                peak_pnl_pct=fast.peak_pnl_pct,
                source="smart",
                smart_snippet=smart.rationale[:240] if smart.rationale else None,
            )
        if smart.action == "close_partial":
            frac = smart.close_fraction
            if frac is None or frac <= 0 or frac >= 1:
                frac = self.config.partial_tp_fraction
            size_to_close = (pos.size_usd * frac).quantize(Decimal("0.01"))
            return PositionAction(
                symbol=pos.symbol, side=pos.side,
                action="partial_close",
                trigger=_trigger("l3_smart_partial"),
                reason=(
                    f"L3 smart partial close ({float(frac)*100:.0f}%, "
                    f"mode={self.config.l3_aggression}): "
                    f"{smart.rationale or 'no rationale'}"
                ),
                pnl_pct=fast.pnl_pct,
                pnl_usd=fast.pnl_usd,
                size_usd=fast.size_usd,
                size_usd_to_close=size_to_close,
                take_profit_price=fast.take_profit_price,
                stop_loss_price=fast.stop_loss_price,
                trailing_stop_price=fast.trailing_stop_price,
                peak_pnl_pct=fast.peak_pnl_pct,
                source="smart",
                smart_snippet=smart.rationale[:240] if smart.rationale else None,
            )
        if smart.action == "tighten_stop":
            # State-only verb - the router doesn't act on it directly;
            # next cycle's SL evaluation reads tighter ATR snapshot.
            # We surface it as an advisory action so the panel paints
            # the L3 reason but exposure stays unchanged.
            return PositionAction(
                symbol=pos.symbol, side=pos.side,
                action="tighten_stop",
                trigger=_trigger("l3_smart_tighten"),
                reason=f"L3 advisory tighten: {smart.rationale or 'no rationale'}",
                pnl_pct=fast.pnl_pct,
                pnl_usd=fast.pnl_usd,
                size_usd=fast.size_usd,
                take_profit_price=fast.take_profit_price,
                stop_loss_price=fast.stop_loss_price,
                trailing_stop_price=fast.trailing_stop_price,
                peak_pnl_pct=fast.peak_pnl_pct,
                source="smart",
                smart_snippet=smart.rationale[:240] if smart.rationale else None,
            )
        if smart.action == "raise_target":
            # State-only verb. The Fast Path will keep the position
            # open; next cycle's TP recalculation just gives the LLM
            # a wider target band. We surface it advisory-only.
            return PositionAction(
                symbol=pos.symbol, side=pos.side,
                action="hold",
                trigger=_trigger("l3_smart_raise"),
                reason=f"L3 advisory raise target: {smart.rationale or 'no rationale'}",
                pnl_pct=fast.pnl_pct,
                pnl_usd=fast.pnl_usd,
                size_usd=fast.size_usd,
                take_profit_price=fast.take_profit_price,
                stop_loss_price=fast.stop_loss_price,
                trailing_stop_price=fast.trailing_stop_price,
                peak_pnl_pct=fast.peak_pnl_pct,
                source="smart",
                smart_snippet=smart.rationale[:240] if smart.rationale else None,
            )
        # Unknown action -> hold but stamp the advice.
        fast.smart_snippet = smart.rationale[:240] if smart.rationale else None
        return fast

    # ------------------------------------------------------------------
    # Trigger primitives (called by Fast Path)
    # ------------------------------------------------------------------

    def _stop_loss_hit(
        self,
        pos: Position,
        *,
        pnl_frac: float,
        atr_abs_live: Decimal | None,
        mid: Decimal | None,
        sl_px: Decimal | None,
    ) -> tuple[bool, str]:
        """Decide whether the dynamic / fallback stop-loss fires.

        Dynamic path: the SL PRICE is recomputed each cycle off the
        live ATR (and lifted to entry+buffer when BE is armed). We
        fire when the live mark crosses that price in the adverse
        direction. Fallback path: classical fixed-% PnL check.
        """
        if not self.config.enable_stop_loss:
            return False, ""
        if (
            self.config.use_dynamic_atr_tpsl
            and sl_px is not None
            and atr_abs_live is not None
            and atr_abs_live > 0
        ):
            ref = mid if mid is not None and mid > 0 else pos.mark_price
            if pos.side == "long" and ref <= sl_px:
                return True, (
                    f"dynamic SL hit | mark {ref} <= sl_price {sl_px} "
                    f"(entry={pos.entry_price}, ATR={atr_abs_live}, "
                    f"mult={self.config.sl_atr_mult}x)"
                )
            if pos.side == "short" and ref >= sl_px:
                return True, (
                    f"dynamic SL hit | mark {ref} >= sl_price {sl_px} "
                    f"(entry={pos.entry_price}, ATR={atr_abs_live}, "
                    f"mult={self.config.sl_atr_mult}x)"
                )
            return False, ""
        # Fallback path - fixed pct on PnL fraction.
        if (
            self.config.stop_loss_pct > 0
            and pnl_frac <= -float(self.config.stop_loss_pct)
        ):
            return True, (
                f"fallback stop-loss hit | unrealised "
                f"{pnl_frac * 100:+.2f}% <= "
                f"-{float(self.config.stop_loss_pct) * 100:.2f}%"
            )
        return False, ""

    def _take_profit_hit(
        self,
        pos: Position,
        *,
        pnl_frac: float,
        atr_abs_live: Decimal | None,
        mid: Decimal | None,
        tp_px: Decimal | None,
    ) -> tuple[bool, str]:
        if not self.config.enable_take_profit:
            return False, ""
        if (
            self.config.use_dynamic_atr_tpsl
            and tp_px is not None
            and atr_abs_live is not None
            and atr_abs_live > 0
        ):
            ref = mid if mid is not None and mid > 0 else pos.mark_price
            if pos.side == "long" and ref >= tp_px:
                return True, (
                    f"dynamic TP hit | mark {ref} >= tp_price {tp_px} "
                    f"(entry={pos.entry_price}, ATR={atr_abs_live}, "
                    f"mult={self.config.tp_atr_mult}x)"
                )
            if pos.side == "short" and ref <= tp_px:
                return True, (
                    f"dynamic TP hit | mark {ref} <= tp_price {tp_px} "
                    f"(entry={pos.entry_price}, ATR={atr_abs_live}, "
                    f"mult={self.config.tp_atr_mult}x)"
                )
            return False, ""
        if (
            self.config.take_profit_pct > 0
            and pnl_frac >= float(self.config.take_profit_pct)
        ):
            return True, (
                f"fallback take-profit hit | unrealised "
                f"{pnl_frac * 100:+.2f}% >= "
                f"+{float(self.config.take_profit_pct) * 100:.2f}%"
            )
        return False, ""

    def _partial_tp_hit(
        self,
        pos: Position,
        *,
        pnl_frac: float,
        atr_abs_live: Decimal | None,
        mid: Decimal | None,
    ) -> tuple[bool, str, Decimal | None]:
        """Did the position reach the first ATR-scale-out target?"""
        if (
            not self.config.enable_partial_take_profit
            or self.config.partial_tp_fraction <= 0
            or self.config.partial_tp_fraction >= 1
        ):
            return False, "", None

        if atr_abs_live is not None and atr_abs_live > 0:
            ref = mid if mid is not None and mid > 0 else pos.mark_price
            target_distance = (
                atr_abs_live * Decimal(str(self.config.partial_tp_atr_mult))
            )
            target_price = (
                pos.entry_price + target_distance
                if pos.side == "long"
                else pos.entry_price - target_distance
            )
            hit = (
                (pos.side == "long" and ref >= target_price)
                or (pos.side == "short" and ref <= target_price)
            )
            if not hit:
                return False, "", None
            size_to_close = (
                pos.size_usd * self.config.partial_tp_fraction
            ).quantize(Decimal("0.01"))
            return True, (
                f"partial TP hit ({float(self.config.partial_tp_fraction) * 100:.0f}%) "
                f"| mark {ref} crossed {target_price} "
                f"({self.config.partial_tp_atr_mult}x ATR={atr_abs_live} "
                f"from entry {pos.entry_price}). Banking partial, BE armed."
            ), size_to_close

        # Fallback: half the full TP threshold (pct form).
        ptp_threshold = (
            float(self.config.take_profit_pct) / 2
            if self.config.take_profit_pct > 0
            else 0.0
        )
        if ptp_threshold > 0 and pnl_frac >= ptp_threshold:
            size_to_close = (
                pos.size_usd * self.config.partial_tp_fraction
            ).quantize(Decimal("0.01"))
            return True, (
                f"partial TP hit (fallback) | unrealised "
                f"{pnl_frac * 100:+.2f}% >= +{ptp_threshold * 100:.2f}% "
                f"(half full TP). Banking "
                f"{float(self.config.partial_tp_fraction) * 100:.0f}%."
            ), size_to_close
        return False, "", None

    def _trailing_stop_hit(
        self,
        pos: Position,
        *,
        pnl_frac: float,
        peak: float,
        atr_abs_live: Decimal | None,
        mid: Decimal | None,
    ) -> tuple[bool, str]:
        if not self.config.enable_trailing_stop:
            return False, ""
        # Dynamic ATR trail: position must have made at least one ATR
        # of profit before the trail arms; then it fires when the mark
        # gives back > trail_atr_mult * ATR from the peak.
        if (
            self.config.use_dynamic_atr_tpsl
            and atr_abs_live is not None
            and atr_abs_live > 0
        ):
            ref = mid if mid is not None and mid > 0 else pos.mark_price
            # Convert ATR distance into a PnL fraction so we can
            # compare peak vs current PnL in like units (one ATR =
            # ATR/entry of price -> mapped to fraction of size).
            if pos.entry_price <= 0:
                return False, ""
            atr_pnl_frac = float(atr_abs_live / pos.entry_price)
            arm_threshold = atr_pnl_frac  # at least +1 ATR before arming
            trail_distance_pnl = atr_pnl_frac * self.config.trail_atr_mult
            if peak < arm_threshold:
                return False, ""
            give_back = peak - pnl_frac
            if give_back >= trail_distance_pnl:
                return True, (
                    f"ATR trailing stop | peak {peak * 100:+.2f}% then "
                    f"gave back {give_back * 100:.2f}% >= "
                    f"{self.config.trail_atr_mult}x ATR "
                    f"({trail_distance_pnl * 100:.2f}%)"
                )
            return False, ""
        # Fallback fixed-pct trail.
        trail = float(self.config.trailing_stop_pct)
        if trail <= 0:
            return False, ""
        if peak < trail:
            return False, ""
        give_back = peak - pnl_frac
        if give_back >= trail:
            return True, (
                f"peak {peak * 100:+.2f}% then gave back "
                f"{give_back * 100:.2f}% >= trailing {trail * 100:.2f}%"
            )
        return False, ""

    # ------------------------------------------------------------------
    # Price / PnL helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _pnl_fraction(pos: Position) -> float:
        if pos.size_usd <= 0:
            return 0.0
        return float(pos.unrealized_pnl_usd / pos.size_usd)

    def _atr_abs_for(
        self,
        pos: Position,
        mid: Decimal | None,
        atr_data: dict[str, dict[str, float]],
    ) -> Decimal | None:
        sym_atr = atr_data.get(pos.symbol, {})
        atr_pct = sym_atr.get("atr_pct_avg")
        ref = mid if mid is not None and mid > 0 else pos.mark_price
        if atr_pct is None or atr_pct <= 0 or ref <= 0:
            return None
        return (ref * Decimal(str(atr_pct)) / Decimal("100")).quantize(
            Decimal("0.0001")
        )

    def _tp_price(
        self,
        pos: Position,
        atr_abs_live: Decimal | None,
    ) -> Decimal | None:
        """Compute the take-profit trigger PRICE (dynamic or fallback)."""
        if not self.config.enable_take_profit:
            return None
        # Dynamic ATR path - preferred.
        if (
            self.config.use_dynamic_atr_tpsl
            and atr_abs_live is not None
            and atr_abs_live > 0
        ):
            distance = atr_abs_live * Decimal(str(self.config.tp_atr_mult))
            if pos.side == "long":
                return (pos.entry_price + distance).quantize(Decimal("0.0001"))
            if pos.side == "short":
                return (pos.entry_price - distance).quantize(Decimal("0.0001"))
        # Fallback fixed-pct path.
        if self.config.take_profit_pct <= 0 or pos.entry_price <= 0:
            return None
        if pos.side == "long":
            return (
                pos.entry_price * (Decimal("1") + self.config.take_profit_pct)
            ).quantize(Decimal("0.0001"))
        if pos.side == "short":
            return (
                pos.entry_price * (Decimal("1") - self.config.take_profit_pct)
            ).quantize(Decimal("0.0001"))
        return None

    def _sl_price(
        self,
        pos: Position,
        atr_abs_live: Decimal | None,
        *,
        state: _PositionState | None = None,
    ) -> Decimal | None:
        """Compute the stop-loss trigger PRICE.

        Honours the breakeven flag - when armed, the SL is lifted to
        entry +/- breakeven_buffer (long-side buffer is positive,
        short-side is negative) regardless of the dynamic ATR distance.
        Whichever produces the LESS adverse stop wins (we never widen
        the SL just because BE armed; we only tighten).
        """
        if not self.config.enable_stop_loss:
            return None
        entry = pos.entry_price
        if entry <= 0:
            return None
        atr_sl: Decimal | None = None
        if (
            self.config.use_dynamic_atr_tpsl
            and atr_abs_live is not None
            and atr_abs_live > 0
        ):
            distance = atr_abs_live * Decimal(str(self.config.sl_atr_mult))
            atr_sl = (
                entry - distance if pos.side == "long" else entry + distance
            )
        elif self.config.stop_loss_pct > 0:
            atr_sl = (
                entry * (Decimal("1") - self.config.stop_loss_pct)
                if pos.side == "long"
                else entry * (Decimal("1") + self.config.stop_loss_pct)
            )

        # Apply breakeven lift when armed.
        if (
            state is not None
            and state.breakeven_armed
            and self.config.enable_breakeven
        ):
            be_buffer = self.config.breakeven_buffer_pct
            be_sl = (
                entry * (Decimal("1") + be_buffer)
                if pos.side == "long"
                else entry * (Decimal("1") - be_buffer)
            )
            if atr_sl is None:
                atr_sl = be_sl
            elif pos.side == "long":
                # Long: a HIGHER SL is tighter (smaller loss).
                atr_sl = max(atr_sl, be_sl)
            else:
                # Short: a LOWER SL is tighter.
                atr_sl = min(atr_sl, be_sl)
        if atr_sl is None:
            return None
        return atr_sl.quantize(Decimal("0.0001"))

    def _trailing_price(
        self,
        pos: Position,
        atr_abs_live: Decimal | None,
        *,
        peak_pnl_frac: float,
    ) -> Decimal | None:
        """Approximate the trailing-stop *price* for the panel."""
        if not self.config.enable_trailing_stop:
            return None
        ref = pos.mark_price if pos.mark_price > 0 else pos.entry_price
        if ref <= 0:
            return None
        # Dynamic ATR trail visualisation.
        if (
            self.config.use_dynamic_atr_tpsl
            and atr_abs_live is not None
            and atr_abs_live > 0
            and pos.entry_price > 0
        ):
            atr_pnl_frac = float(atr_abs_live / pos.entry_price)
            if peak_pnl_frac < atr_pnl_frac:
                return None
            distance = atr_abs_live * Decimal(str(self.config.trail_atr_mult))
            if pos.side == "long":
                return (ref - distance).quantize(Decimal("0.0001"))
            if pos.side == "short":
                return (ref + distance).quantize(Decimal("0.0001"))
        # Fallback fixed-pct trail.
        if self.config.trailing_stop_pct <= 0:
            return None
        if peak_pnl_frac < float(self.config.trailing_stop_pct):
            return None
        if pos.side == "long":
            return (
                ref * (Decimal("1") - self.config.trailing_stop_pct)
            ).quantize(Decimal("0.0001"))
        if pos.side == "short":
            return (
                ref * (Decimal("1") + self.config.trailing_stop_pct)
            ).quantize(Decimal("0.0001"))
        return None

    def _breakeven_should_arm(
        self,
        pos: Position,
        atr_abs_live: Decimal | None,
        pnl_frac: float,
    ) -> bool:
        if pos.entry_price <= 0:
            return False
        # Prefer ATR-based trigger; fall back to a pnl-fraction
        # threshold equal to half the fallback TP %.
        if atr_abs_live is not None and atr_abs_live > 0:
            atr_pnl_frac = float(atr_abs_live / pos.entry_price)
            return pnl_frac >= (
                atr_pnl_frac * self.config.breakeven_trigger_atr_mult
            )
        # Fallback: half-way to the fallback TP.
        if self.config.take_profit_pct > 0:
            return pnl_frac >= float(self.config.take_profit_pct) / 2
        return False

    # ------------------------------------------------------------------
    # State helpers
    # ------------------------------------------------------------------

    def _ensure_state(
        self,
        pos: Position,
        atr_data: dict[str, dict[str, float]],
    ) -> _PositionState:
        key = (pos.symbol, pos.side)
        if key in self._states:
            return self._states[key]
        sym_atr = atr_data.get(pos.symbol, {})
        entry_atr_pct = sym_atr.get("atr_pct_avg")
        entry_atr_abs: Decimal | None = None
        if (
            entry_atr_pct is not None
            and entry_atr_pct > 0
            and pos.entry_price > 0
        ):
            entry_atr_abs = (
                pos.entry_price * Decimal(str(entry_atr_pct)) / Decimal("100")
            ).quantize(Decimal("0.0001"))
        state = _PositionState(
            opened_at=datetime.now(timezone.utc),
            entry_price=pos.entry_price,
            entry_atr_pct=entry_atr_pct,
            entry_atr_abs=entry_atr_abs,
        )
        self._states[key] = state
        return state

    def _gc_states(self, open_positions: list[Position]) -> None:
        live_keys = {(p.symbol, p.side) for p in open_positions}
        for key in list(self._states):
            if key not in live_keys:
                self._states.pop(key, None)

    def _vol_spike_ratio(
        self,
        state: _PositionState,
        atr_pct_live: float | None,
    ) -> float | None:
        if (
            state.entry_atr_pct is None
            or state.entry_atr_pct <= 0
            or atr_pct_live is None
            or atr_pct_live <= 0
        ):
            return None
        return float(atr_pct_live / state.entry_atr_pct)

    def _age_hours(self, state: _PositionState) -> float:
        return (
            (datetime.now(timezone.utc) - state.opened_at).total_seconds()
            / 3600.0
        )

    def _age_minutes(self, state: _PositionState) -> float:
        return (
            (datetime.now(timezone.utc) - state.opened_at).total_seconds()
            / 60.0
        )

    # ------------------------------------------------------------------
    # Data extraction (ATR + L2 funding/OI/whales out of DecisionResult)
    # ------------------------------------------------------------------

    def _extract_atr_data(
        self, decision: DecisionResult | None
    ) -> dict[str, dict[str, float]]:
        """Extract ``{symbol: {"atr_pct_avg": 1.23}}`` from L1's raw."""
        if decision is None:
            return {}
        l1_score = decision.level_score(1)
        if l1_score is None:
            return {}
        per_sym = (l1_score.raw.get("l1", {}) or {}).get("per_symbol") or {}
        out: dict[str, dict[str, float]] = {}
        for sym, ro in per_sym.items():
            atr_pct = ro.get("atr_pct_avg")
            if atr_pct is None:
                rows = ro.get("rows") or []
                vals = [
                    float(r.get("atr_pct", 0))
                    for r in rows
                    if float(r.get("atr_pct", 0)) > 0
                ]
                atr_pct = sum(vals) / len(vals) if vals else None
            if atr_pct is not None:
                try:
                    out[sym] = {"atr_pct_avg": float(atr_pct)}
                except (TypeError, ValueError):
                    pass
        return out

    def _extract_l2_signals(
        self, decision: DecisionResult | None
    ) -> dict[str, dict[str, Any]]:
        """Extract per-symbol funding / OI / whale signals from L2's raw.

        Augmented in Day-5+ to also carry the L2 aggregate
        conviction + direction (used by the HOLD-rescue rule). Both
        live on the L2 LevelScore itself (``score`` and
        ``direction_sign``) and on the L2 raw ``per_symbol`` payload
        (``conviction`` / ``direction``); we prefer the per-symbol
        figure when present, else fall back to the aggregate so the
        rule still works on the primary symbol when L2 doesn't
        attribute per-symbol.

        Returns a defaultdict-like view: ``out[symbol]`` is always at
        least a dict with the aggregate L2 conviction + direction, so
        downstream consumers (HOLD-rescue gate) work on any symbol
        even when L2 only emitted aggregate scoring.
        """
        if decision is None:
            return {}
        l2_score = decision.level_score(2)
        if l2_score is None:
            return {}
        per_sym = (l2_score.raw.get("l2", {}) or {}).get("per_symbol") or {}
        agg_conv = float(l2_score.score or 0.0)
        agg_dir = int(getattr(l2_score, "direction_sign", 0) or 0)

        def _norm_dir(raw: Any) -> int | None:
            if raw is None:
                return None
            try:
                return int(raw)
            except (TypeError, ValueError):
                s = str(raw).strip().lower()
                if s in {"long", "bullish", "up"}:
                    return 1
                if s in {"short", "bearish", "down"}:
                    return -1
                return 0

        # Defaultdict-ish: any symbol lookup falls back to the
        # aggregate L2 read. Lets the HOLD-rescue gate work for any
        # symbol even when L2 only emitted aggregate scoring.
        out: _L2SignalMap = _L2SignalMap(
            default_conviction=agg_conv,
            default_direction=agg_dir,
        )

        for sym, intel in per_sym.items():
            f = intel.get("funding") or {}
            oi = intel.get("open_interest") or {}
            wh = intel.get("whales") or {}
            per_conv = intel.get("conviction")
            per_dir_val = _norm_dir(
                intel.get("direction_sign") or intel.get("direction")
            )
            out[sym] = {
                "funding_rate": f.get("current_rate"),
                "oi_delta_1h_pct": oi.get("delta_1h_pct"),
                "n_whales": int(wh.get("n_whales", 0) or 0),
                "whale_direction": wh.get("direction"),
                "l2_conviction": (
                    float(per_conv) if per_conv is not None else agg_conv
                ),
                "l2_direction_sign": (
                    per_dir_val if per_dir_val is not None else agg_dir
                ),
            }
        return out

    # ------------------------------------------------------------------
    # Position-state resync (fresh pull from the executor)
    # ------------------------------------------------------------------

    async def _resync_open_positions(
        self,
        snapshot: list[Position],
    ) -> list[Position]:
        """Return the freshest possible set of open positions.

        Calls ``executor.get_open_positions()`` directly so the
        manager always operates on the canonical on-chain state -
        never on a possibly-stale :class:`AccountInfo` snapshot that
        the router built earlier in the cycle. Reconciles the result
        with the caller's snapshot and logs every mismatch.

        Three failure modes the snapshot path silently swallowed
        before this resync existed:

        1. **Silent empty AccountInfo.** Hyperliquid Info SDK glitch
           / network blip during ``get_account_info`` -> snapshot
           returns ``positions=[]`` and PM says "nothing to manage".
        2. **Eventual-consistency race.** Snapshot taken just after
           ``exchange.market_open`` -> Hyperliquid hasn't yet
           propagated the fill -> snapshot is the pre-fill state.
        3. **Snapshot drift across cycles.** Anything else upstream
           that loses positions between the snapshot and PM.

        The fresh pull defeats all three: even if the snapshot is
        wrong, the stewardship layer sees real positions and can
        actually close / TP / SL them.

        Returns
        -------
        list[Position]
            The freshest set we can produce. Prefers the fresh pull;
            falls back to ``snapshot`` when the executor does not
            expose ``get_open_positions`` (legacy executors, test
            doubles) or when the fresh-pull call raises.
        """
        # Only non-flat positions in the snapshot count as "open".
        snap_open = [
            p for p in snapshot
            if p.side != "flat" and p.size_usd > 0
        ]
        if self.executor is None:
            return snap_open

        getter = getattr(self.executor, "get_open_positions", None)
        if getter is None:
            # Legacy executors / test doubles - quietly fall back to
            # the snapshot. Don't bump the warning counter; this is
            # an expected configuration, not a failure.
            return snap_open

        try:
            fresh = await getter()
        except Exception as exc:  # noqa: BLE001 - manager guards
            self.stats.resync_fallbacks_to_snapshot += 1
            logger.warning(
                "PositionManager: executor.get_open_positions() raised "
                "({}); falling back to AccountInfo snapshot with "
                "{} position(s).",
                exc, len(snap_open),
            )
            return snap_open

        if fresh is None:
            self.stats.resync_fallbacks_to_snapshot += 1
            logger.warning(
                "PositionManager: executor.get_open_positions() returned "
                "None; falling back to AccountInfo snapshot with "
                "{} position(s).",
                len(snap_open),
            )
            return snap_open

        # Belt-and-suspenders: filter out anything that came back
        # flat or zero-sized (HyperliquidExecutor.get_open_positions
        # already filters, but a custom executor might not).
        fresh_open = [
            p for p in fresh
            if p.side != "flat" and p.size_usd > 0
        ]
        self.stats.resync_fresh_pulls += 1

        # Reconciliation - log every mismatch loudly so the operator
        # has forensic evidence the next time the desync recurs.
        snap_keys = {(p.symbol, p.side) for p in snap_open}
        fresh_keys = {(p.symbol, p.side) for p in fresh_open}

        missing_in_snapshot = fresh_keys - snap_keys
        if missing_in_snapshot:
            # THIS is the bug we're guarding against: the cached
            # AccountInfo snapshot doesn't contain a position that
            # the fresh pull just confirmed is live on-chain.
            self.stats.resync_recovered_missing += len(missing_in_snapshot)
            logger.warning(
                "PositionManager RESYNC | snapshot was MISSING {} "
                "position(s) that fresh-pull found on Hyperliquid: "
                "{}. Acting on fresh-pull state. (snapshot had {}; "
                "fresh has {}.)",
                len(missing_in_snapshot),
                sorted(missing_in_snapshot),
                sorted(snap_keys),
                sorted(fresh_keys),
            )

        phantom_in_snapshot = snap_keys - fresh_keys
        if phantom_in_snapshot:
            # The snapshot has positions that the fresh pull doesn't.
            # Usually benign (we closed them mid-cycle via a
            # side-channel) but worth logging so the operator can
            # spot legitimate races vs accounting bugs.
            self.stats.resync_phantom_in_snapshot += len(phantom_in_snapshot)
            logger.info(
                "PositionManager RESYNC | snapshot had {} position(s) "
                "the fresh-pull doesn't: {}. Treating as already "
                "closed. (snapshot had {}; fresh has {}.)",
                len(phantom_in_snapshot),
                sorted(phantom_in_snapshot),
                sorted(snap_keys),
                sorted(fresh_keys),
            )

        return fresh_open

    # ------------------------------------------------------------------
    # Mid-price / snapshot helpers
    # ------------------------------------------------------------------

    async def _safe_mid_price(self, symbol: str) -> Decimal | None:
        if self.executor is None:
            return None
        get_mid = getattr(self.executor, "get_mid_price", None)
        if get_mid is None:
            return None
        try:
            value = await get_mid(symbol)
        except Exception as exc:  # noqa: BLE001
            logger.debug("get_mid_price({}) failed: {}", symbol, exc)
            return None
        if value is None:
            return None
        try:
            decimal_value = Decimal(str(value))
        except (TypeError, ValueError):
            return None
        return decimal_value if decimal_value > 0 else None

    def _snapshot_for(
        self,
        pos: Position,
        action: PositionAction,
        *,
        mid: Decimal | None,
        atr_data: dict[str, dict[str, float]],
        smart_verdict: SmartPathVerdict | None,
        smart_briefing: SmartPathBriefing | None,
    ) -> PositionSnapshot:
        state = self._states.get((pos.symbol, pos.side))
        sym_atr = atr_data.get(pos.symbol, {})
        current_atr_pct = sym_atr.get("atr_pct_avg")
        cap = self.config.atr_caps.cap_for(pos.symbol)
        cap_breached = (
            current_atr_pct is not None and current_atr_pct > cap
        )
        age_minutes = self._age_minutes(state) if state is not None else None
        return PositionSnapshot(
            symbol=pos.symbol,
            side=pos.side,
            size_usd=pos.size_usd,
            entry_price=pos.entry_price,
            mark_price=pos.mark_price if pos.mark_price > 0 else (mid or pos.entry_price),
            leverage=pos.leverage,
            unrealized_pnl_usd=pos.unrealized_pnl_usd,
            pnl_pct=action.pnl_pct,
            take_profit_price=action.take_profit_price,
            stop_loss_price=action.stop_loss_price,
            trailing_stop_price=action.trailing_stop_price,
            peak_pnl_pct=action.peak_pnl_pct,
            action=action.action,
            trigger=action.trigger,
            reason=action.reason,
            current_atr_pct=current_atr_pct,
            entry_atr_pct=state.entry_atr_pct if state is not None else None,
            atr_cap_pct=cap,
            atr_cap_breached=cap_breached,
            age_minutes=age_minutes,
            breakeven_armed=state.breakeven_armed if state is not None else False,
            partial_tp_done=state.partial_tp_done if state is not None else False,
            smart_path_invoked=smart_briefing is not None,
            smart_path_verdict=(
                smart_verdict.action if smart_verdict is not None else None
            ),
            smart_path_rationale=(
                smart_verdict.rationale[:240]
                if smart_verdict is not None and smart_verdict.rationale
                else None
            ),
            source=action.source,
        )

    # ------------------------------------------------------------------
    # Convenience constructors
    # ------------------------------------------------------------------

    @classmethod
    def from_settings(
        cls,
        settings: Any,
        executor: Any | None = None,
        position_arbiter: PositionArbiterCallable | None = None,
    ) -> "PositionManager":
        """Build a :class:`PositionManager` straight from a ``Settings``."""
        def _pct_to_frac(value: float | None, default: Decimal) -> Decimal:
            if value is None:
                return default
            try:
                return (Decimal(str(value)) / Decimal("100")).quantize(
                    Decimal("0.000001")
                )
            except (TypeError, ValueError):
                return default

        def _frac_to_frac(value: float | None, default: Decimal) -> Decimal:
            if value is None:
                return default
            try:
                return Decimal(str(value)).quantize(Decimal("0.000001"))
            except (TypeError, ValueError):
                return default

        atr_caps = PerAssetATRCaps(
            btc_1h=float(
                getattr(
                    settings, "PER_ASSET_ATR_CAP_BTC_1H",
                    PerAssetATRCaps.btc_1h,
                )
            ),
            eth_1h=float(
                getattr(
                    settings, "PER_ASSET_ATR_CAP_ETH_1H",
                    PerAssetATRCaps.eth_1h,
                )
            ),
            default_1h=float(
                getattr(
                    settings, "PER_ASSET_ATR_CAP_DEFAULT_1H",
                    PerAssetATRCaps.default_1h,
                )
            ),
        )

        cfg = PositionManagerConfig(
            take_profit_pct=_pct_to_frac(
                getattr(settings, "TAKE_PROFIT_PCT", None),
                PositionManagerConfig.take_profit_pct,
            ),
            stop_loss_pct=_pct_to_frac(
                getattr(settings, "STOP_LOSS_PCT", None),
                PositionManagerConfig.stop_loss_pct,
            ),
            trailing_stop_pct=_pct_to_frac(
                getattr(settings, "TRAILING_STOP_PCT", None),
                PositionManagerConfig.trailing_stop_pct,
            ),
            min_conviction_to_hold=float(
                getattr(
                    settings,
                    "MIN_CONVICTION_TO_HOLD",
                    PositionManagerConfig.min_conviction_to_hold,
                )
            ),
            re_eval_min_profit_pct=_pct_to_frac(
                getattr(settings, "RE_EVAL_MIN_PROFIT_PCT", None),
                PositionManagerConfig.re_eval_min_profit_pct,
            ),
            auto_flip_on_side_change=bool(
                getattr(
                    settings,
                    "AUTO_FLIP_ON_SIDE_CHANGE",
                    PositionManagerConfig.auto_flip_on_side_change,
                )
            ),
            # ---- Dynamic ATR ----
            use_dynamic_atr_tpsl=bool(
                getattr(settings, "USE_DYNAMIC_ATR_TPSL", True)
            ),
            sl_atr_mult=float(getattr(settings, "SL_ATR_MULT", 1.2)),
            tp_atr_mult=float(getattr(settings, "TP_ATR_MULT", 3.0)),
            trail_atr_mult=float(getattr(settings, "TRAIL_ATR_MULT", 1.5)),
            # ---- Partial TP ----
            enable_partial_take_profit=bool(
                getattr(settings, "ENABLE_PARTIAL_TAKE_PROFIT", True)
            ),
            partial_tp_atr_mult=float(
                getattr(settings, "PARTIAL_TP_ATR_MULT", 1.5)
            ),
            partial_tp_fraction=_frac_to_frac(
                getattr(settings, "PARTIAL_TP_FRACTION", None),
                PositionManagerConfig.partial_tp_fraction,
            ),
            # ---- Breakeven ----
            enable_breakeven=bool(getattr(settings, "ENABLE_BREAKEVEN", True)),
            breakeven_trigger_atr_mult=float(
                getattr(settings, "BREAKEVEN_TRIGGER_ATR_MULT", 1.0)
            ),
            breakeven_buffer_pct=_pct_to_frac(
                getattr(settings, "BREAKEVEN_BUFFER_PCT", None),
                PositionManagerConfig.breakeven_buffer_pct,
            ),
            # ---- Vol filter ----
            enable_vol_filter=bool(getattr(settings, "ENABLE_VOL_FILTER", True)),
            vol_spike_mult=float(getattr(settings, "VOL_SPIKE_MULT", 1.8)),
            vol_spike_action=(
                getattr(settings, "VOL_SPIKE_ACTION", "tighten_stop") or "tighten_stop"
            ),
            # ---- Time exit ----
            max_position_hold_hours=float(
                getattr(settings, "MAX_POSITION_HOLD_HOURS", 24.0) or 0.0
            ),
            # ---- Daily DD ----
            enable_daily_dd_guard=bool(
                getattr(settings, "ENABLE_DAILY_DD_GUARD", True)
            ),
            daily_loss_limit_pct=float(
                getattr(settings, "DAILY_LOSS_LIMIT_PCT", 5.0)
            ),
            atr_caps=atr_caps,
            # ---- Smart Path ----
            enable_smart_path=bool(
                getattr(settings, "ENABLE_SMART_PATH", True)
            ),
            l3_review_trigger_price_atr_mult=float(
                getattr(settings, "L3_REVIEW_TRIGGER_PRICE_ATR_MULT", 1.5)
            ),
            l3_review_trigger_funding_rate=float(
                getattr(settings, "L3_REVIEW_TRIGGER_FUNDING_RATE", 0.0005)
            ),
            l3_review_trigger_whale_count=int(
                getattr(settings, "L3_REVIEW_TRIGGER_WHALE_COUNT", 5)
            ),
            l3_review_trigger_oi_delta_pct=float(
                getattr(settings, "L3_REVIEW_TRIGGER_OI_DELTA_PCT", 4.0)
            ),
            l3_review_min_interval_minutes=float(
                getattr(settings, "L3_REVIEW_MIN_INTERVAL_MINUTES", 30.0)
            ),
            l3_review_max_interval_minutes=float(
                getattr(settings, "L3_REVIEW_MAX_INTERVAL_MINUTES", 120.0)
            ),
            l3_can_override_fast_path=bool(
                getattr(settings, "L3_CAN_OVERRIDE_FAST_PATH", True)
            ),
            # ---- Smart Path - L3_AGGRESSION + HOLD-rescue ----
            # The PM honours the same L3_AGGRESSION knob as the main
            # arbiter so an operator only sets it once. Aggressive
            # mode also auto-enables the HOLD-rescue rule (and the
            # "any direction" rescue mode) unless the operator has
            # explicitly forced it off; balanced / conservative
            # respect the explicit flag (default off).
            l3_aggression=str(
                getattr(settings, "L3_AGGRESSION", "balanced") or "balanced"
            ),
            enable_hold_rescue=bool(
                getattr(
                    settings,
                    "ENABLE_HOLD_RESCUE",
                    # Auto-on under aggressive when the explicit flag
                    # is absent (mirrors the main-engine semantics).
                    str(getattr(settings, "L3_AGGRESSION", "balanced") or "").lower()
                    == "aggressive",
                )
            ),
            hold_rescue_l2_min=float(
                getattr(
                    settings,
                    "HOLD_RESCUE_L2_MIN",
                    getattr(settings, "L3_HOLD_RESCUE_L2_MIN", 0.65),
                )
            ),
            hold_rescue_direction_mode=(
                getattr(
                    settings,
                    "HOLD_RESCUE_DIRECTION_MODE",
                    # Auto-set: aggressive = "any" (rescue on either
                    # side, allows scale-up); balanced/conservative
                    # = "opposite" (rescue only on contradiction).
                    "any"
                    if str(getattr(settings, "L3_AGGRESSION", "balanced") or "").lower()
                    == "aggressive"
                    else "opposite",
                )
            ),
            hold_rescue_cooldown_minutes=float(
                getattr(settings, "HOLD_RESCUE_COOLDOWN_MINUTES", 20.0)
            ),
            # ---- Smart Path - first-review delay + HOLD-must-be-earned ----
            first_review_delay_minutes=float(
                getattr(settings, "L3_FIRST_REVIEW_DELAY_MINUTES", 10.0)
            ),
            hold_must_be_earned=bool(
                getattr(
                    settings,
                    "HOLD_MUST_BE_EARNED",
                    # Auto-on under aggressive (operator chose to be
                    # interventionist; HOLD shouldn't be a default).
                    str(getattr(settings, "L3_AGGRESSION", "balanced") or "").lower()
                    == "aggressive",
                )
            ),
            hold_must_be_earned_minutes=float(
                getattr(settings, "HOLD_MUST_BE_EARNED_MINUTES", 45.0)
            ),
            # ---- Global LLM budget (Day-6+ cost-safety rails) ----
            # Hard ceilings on Smart-Path call volume regardless of
            # per-position cooldowns. Defaults of 2/cycle + 8/hour
            # are safety-first; raise them only after observing
            # actual call rates via the panel's `Smart-Path usage`
            # row.
            llm_max_per_cycle=int(
                getattr(settings, "LLM_MAX_PER_CYCLE", 2)
            ),
            llm_max_per_hour=int(
                getattr(settings, "LLM_MAX_PER_HOUR", 8)
            ),
            # ---- Conservative-mode hardening (Day-6+) ----
            # Auto-on under conservative so the mode becomes
            # meaningfully different from balanced ("LLM as brake,
            # never accelerator"), not just a higher confidence
            # threshold. Operators can still force either off via
            # explicit .env settings.
            l3_conservative_veto_only=bool(
                getattr(
                    settings,
                    "L3_CONSERVATIVE_VETO_ONLY",
                    str(getattr(settings, "L3_AGGRESSION", "balanced") or "").lower()
                    == "conservative",
                )
            ),
            l3_require_multi_trigger=bool(
                getattr(
                    settings,
                    "L3_REQUIRE_MULTI_TRIGGER",
                    str(getattr(settings, "L3_AGGRESSION", "balanced") or "").lower()
                    == "conservative",
                )
            ),
            # ---- Per-trigger feature flags ----
            enable_trailing_stop=bool(
                getattr(
                    settings,
                    "ENABLE_TRAILING_STOP",
                    PositionManagerConfig.enable_trailing_stop,
                )
            ),
            enable_re_evaluation=bool(
                getattr(
                    settings,
                    "ENABLE_RE_EVALUATION",
                    PositionManagerConfig.enable_re_evaluation,
                )
            ),
        )
        return cls(
            config=cfg,
            executor=executor,
            position_arbiter=position_arbiter,
        )


# ---------------------------------------------------------------------------
# Default arbiter built on the OpenRouter / Claude stack
# ---------------------------------------------------------------------------


def build_default_position_arbiter(
    openrouter_client: Any | None,
    *,
    model: str = "anthropic/claude-sonnet-4.6",
    timeout_seconds: int = 30,
) -> PositionArbiterCallable:
    """Build a Smart-Path arbiter backed by Claude (via OpenRouter).

    This wires the per-position Smart Path to the same OpenRouter
    client used by Level 3 for the main decision arbitration, so the
    operator only configures ``OPENROUTER_API_KEY`` once.

    Returns the inert :func:`_null_arbiter` when ``openrouter_client``
    is None - the manager still runs end-to-end (Fast Path only).

    The prompt is intentionally compact (one position, structured
    inputs, tightly scoped 4-line JSON contract) so the round-trip
    is cheap. Returns a HOLD with confidence=0 on any LLM error so
    the Fast Path always carries the cycle.
    """
    if openrouter_client is None:
        return _null_arbiter

    async def _arbiter(briefing: SmartPathBriefing) -> SmartPathVerdict:
        # The system prompt encodes the contract; the aggression mode
        # is folded in dynamically so the LLM knows the operator's
        # risk posture without us having to maintain three prompts.
        aggression_brief = {
            "conservative": (
                "RISK POSTURE: CONSERVATIVE. Bias heavily toward HOLD. "
                "Only override the Fast Path on overwhelming evidence "
                "(confidence >= 0.80). Prefer tighten_stop over "
                "close_partial; prefer close_partial over close_full."
            ),
            "balanced": (
                "RISK POSTURE: BALANCED (default). Override the Fast "
                "Path on clear contradictions (confidence >= 0.60). "
                "Treat HOLD as a real decision, not the default."
            ),
            "aggressive": (
                "RISK POSTURE: AGGRESSIVE. Act decisively on material "
                "on-chain shifts (confidence >= 0.50). HOLD must be "
                "EARNED, not assumed - if the on-chain picture has "
                "shifted, prefer close_partial or close_full. Under "
                "this mode a HOLD reply to a HOLD-rescue trigger is "
                "auto-rewritten to a defensive partial close."
            ),
        }.get(briefing.l3_aggression, "")

        rescue_block = (
            (
                "\n\nHOLD-RESCUE ACTIVE: The Fast Path wanted HOLD on "
                "this position, but L2 conviction "
                f"({briefing.l2_conviction:.2f}) is high in the "
                "OPPOSITE direction. The operator is asking you "
                "specifically: 'should we close, scale out or stay "
                "the course?'. A HOLD reply here is the LEAST "
                "preferred outcome - if you cannot justify holding "
                "against L2's strong opposing read, return close_full "
                "or close_partial."
            )
            if briefing.hold_rescue_active
            else ""
        )

        system_prompt = (
            "You are a per-position risk-management arbiter for "
            "CapitalArc, embedded in the Fast-Path loop on top of "
            "Hyperliquid Testnet. The Fast Path has ALREADY computed a "
            "deterministic verdict for this position from ATR-based "
            "TP/SL, trailing stops, vol spikes, time limits and a "
            "daily drawdown guard.\n\n"
            f"{aggression_brief}\n\n"
            "Your job: REVIEW the situation in light of the live "
            "on-chain context (funding, OI delta, whale count, L2 "
            "conviction + direction) and either:\n"
            "  * confirm the position is fine -> action=\"hold\"\n"
            "  * vote to close fully -> action=\"close_full\"\n"
            "  * vote to scale out -> action=\"close_partial\"\n"
            "  * vote to tighten the stop -> action=\"tighten_stop\"\n"
            "  * vote to extend the target -> action=\"raise_target\"\n\n"
            "Return STRICT JSON only, no markdown:\n"
            "{\n"
            "  \"action\": \"hold|close_full|close_partial|tighten_stop|raise_target\",\n"
            "  \"rationale\": \"<<=200 chars, concise, cite numbers from the briefing>\",\n"
            "  \"confidence\": 0.0-1.0,\n"
            "  \"close_fraction\": null OR 0.0-1.0 (only when action=close_partial)\n"
            "}\n\n"
            "The 5%% daily-DD kill switch and time-exit limit are "
            "SACRED - never override them."
            f"{rescue_block}"
        )

        l2_dir_str = (
            "bullish" if briefing.l2_direction_sign > 0
            else "bearish" if briefing.l2_direction_sign < 0
            else "neutral"
        )
        user_prompt = (
            f"# Position\n"
            f"- symbol: {briefing.symbol}\n"
            f"- side: {briefing.side}\n"
            f"- entry: {briefing.entry_price}\n"
            f"- mark: {briefing.mark_price}\n"
            f"- size_usd: {briefing.size_usd}\n"
            f"- leverage: {briefing.leverage}\n"
            f"- pnl: {briefing.pnl_usd} USD ({briefing.pnl_pct * 100:+.2f}%)\n"
            f"- peak_pnl: {briefing.peak_pnl_pct}\n"
            f"- age: {briefing.age_minutes:.1f} minutes\n"
            f"- breakeven_armed: {briefing.breakeven_armed}\n"
            f"- partial_tp_done: {briefing.partial_tp_done}\n\n"
            f"# Volatility\n"
            f"- current_atr_pct: {briefing.current_atr_pct}\n"
            f"- entry_atr_pct: {briefing.entry_atr_pct}\n"
            f"- atr_cap_pct (per-asset): {briefing.atr_cap_pct}\n\n"
            f"# On-chain context\n"
            f"- funding_rate: {briefing.funding_rate}\n"
            f"- oi_delta_1h_pct: {briefing.oi_delta_1h_pct}\n"
            f"- whale_count: {briefing.whale_count}\n"
            f"- whale_direction: {briefing.whale_direction}\n"
            f"- l2_conviction: {briefing.l2_conviction:.2f}\n"
            f"- l2_direction: {l2_dir_str} ({briefing.l2_direction_sign:+d})\n\n"
            f"# Engine directive on this cycle\n"
            f"- action: {briefing.engine_directive_action}\n"
            f"- side: {briefing.engine_directive_side}\n"
            f"- conviction: {briefing.engine_directive_conviction:.2f}\n"
            f"- l3_aggression: {briefing.l3_aggression}\n"
            f"- hold_rescue_active: {briefing.hold_rescue_active}\n\n"
            f"# WHY the Smart Path was invoked\n"
            f"- " + "\n- ".join(briefing.invocation_reasons) + "\n\n"
            f"Return JSON only."
        )
        try:
            raw_dict = await openrouter_client.generate_json(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Smart-Path LLM call failed for {}/{}: {} - defaulting to HOLD",
                briefing.symbol, briefing.side, exc,
            )
            return SmartPathVerdict(
                action="hold",
                rationale=f"LLM error: {exc}",
                confidence=0.0,
            )
        try:
            action = str(raw_dict.get("action", "hold")).lower()
            if action not in {
                "hold", "close_full", "close_partial", "tighten_stop", "raise_target",
            }:
                action = "hold"
            confidence = float(raw_dict.get("confidence", 0.0) or 0.0)
            confidence = max(0.0, min(1.0, confidence))
            rationale = str(raw_dict.get("rationale", "") or "")[:240]
            raw_frac = raw_dict.get("close_fraction")
            close_fraction: Decimal | None = None
            if raw_frac is not None:
                try:
                    cf = float(raw_frac)
                    if 0.0 < cf < 1.0:
                        close_fraction = Decimal(str(cf))
                except (TypeError, ValueError):
                    pass
            return SmartPathVerdict(
                action=action,  # type: ignore[arg-type]
                rationale=rationale,
                confidence=confidence,
                close_fraction=close_fraction,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Smart-Path response parse failed for {}/{}: {} - HOLD",
                briefing.symbol, briefing.side, exc,
            )
            return SmartPathVerdict(
                action="hold",
                rationale=f"parse error: {exc}",
                confidence=0.0,
            )

    return _arbiter


__all__ = [
    "PositionManager",
    "PositionManagerConfig",
    "PerAssetATRCaps",
    "PositionAction",
    "PositionSnapshot",
    "PositionReview",
    "PositionTrigger",
    "PositionVerb",
    "SmartPathBriefing",
    "SmartPathVerdict",
    "SmartPathVerdictAction",
    "PositionArbiterCallable",
    "build_default_position_arbiter",
]
