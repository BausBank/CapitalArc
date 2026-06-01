"""RiskEngine - the portfolio-level risk brain for CapitalArc (Day-2).

The :class:`AllocationRouter` already solved *per-trade* sizing
(vol-target -> intensity -> drawdown haircut -> cap). The RiskEngine
layers the *portfolio-level* risk controls on top of that pipeline, so
the router stays focused on "translate a decision into a trade" while
the RiskEngine answers the harder questions:

    1. **Should we be trading at all right now?**
       - ``equity_gate`` - park in cash/USYC below a minimum equity
         (a $79 testnet account churning fees is worse than sitting
         out; Stage 6a from the 2026-05-26 handoff).

    2. **Have we just been hurt - should we throttle?**
       - ``warmup_multiplier`` - after a realised loss (stop-out or a
         loss beyond a threshold) we shrink new size and ramp it back
         linearly over a warm-up window. Complements the per-symbol
         post-stop-out *cooldown* (Stage 4): the cooldown blocks
         re-entry on the stopped symbol for 30 min; the warm-up makes
         EVERY new entry smaller for a while because a fresh loss is
         evidence the regime is hostile.

    3. **Are we doubling the same bet under two tickers?**
       - ``correlation_adjust`` - BTC and ETH move together ~0.8; a
         long on both is ~1.6x the directional risk the sizing model
         thinks it took. We cap aggregate *same-side* notional across
         a correlation group and shrink (or refuse) the new open.

    4. **Does the trade even clear its own costs?**
       - ``ev_ok`` - a pre-trade expected-value gate: the take-profit
         reward must beat the round-trip cost (fees + slippage) by a
         configurable ratio (Stage 6b). Kills "scalp a 0.05% ATR
         range" entries that can't pay for themselves.

    5. **Is one asset allowed more notional than another?**
       - ``per_asset_max_position`` - a per-symbol hard cap on top of
         the global ``MAX_POSITION_USD`` so e.g. BTC can run bigger
         than an ALT.

Design notes
============
* The RiskEngine holds the small amount of cross-cycle STATE the
  warm-up ramp needs (when the last loss happened). Everything else is
  a pure function of its inputs, so the gates are trivially testable.
* Like the PositionManager, the RiskEngine NEVER executes anything -
  it returns verdicts / adjusted sizes and the router acts on them.
* Every default is chosen so that, with no operator tuning and a flat
  account, the RiskEngine is a NO-OP (multiplier 1.0, no block). This
  keeps it safe to drop into the existing sizing pipeline without
  changing behaviour until the operator opts into tighter limits.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from src.utils.logging import logger


def _symbol_head(symbol: str) -> str:
    """Return the leading token of a perp symbol (``BTC-PERP`` -> ``BTC``)."""
    if not symbol:
        return ""
    return symbol.strip().upper().split("-")[0].split("/")[0]


@dataclass
class RiskEngineConfig:
    """Tunable knobs for the portfolio-level risk controls.

    All percentage fields are stored in operator-friendly form
    (``1.0`` means 1%, ``150.0`` means 150%) and converted to
    fractions internally - matching the rest of the project's
    ``.env`` conventions.
    """

    # ---- (1) Minimum-equity-to-trade gate (Stage 6a) ----
    # When account equity drops below this, the RiskEngine refuses
    # new risk-on opens (the router holds cash / parks in USYC).
    # 0 disables the gate.
    min_equity_to_trade_usd: Decimal = Decimal("0")

    # ---- (2) Post-loss warm-up ramp ----
    enable_loss_warmup: bool = True
    # Duration of the warm-up ramp after a triggering loss.
    warmup_minutes: float = 60.0
    # Size multiplier at the instant of the loss; ramps linearly back
    # to 1.0 over ``warmup_minutes``. 0.5 = "half size, recover over
    # an hour".
    warmup_size_mult: float = 0.5
    # A close whose realised loss is >= this % of equity arms the
    # warm-up. A ``stop_loss`` / ``daily_dd_guard`` close ALWAYS arms
    # it regardless of magnitude (those are by definition "we were
    # wrong" events).
    warmup_trigger_loss_pct: float = 1.0

    # ---- (3) Correlation / aggregate-exposure cap ----
    enable_correlation_cap: bool = True
    # Max aggregate SAME-SIDE notional across a correlation group,
    # expressed as a % of equity. 150% with 5x leverage still leaves
    # head-room; lower it to decorrelate harder.
    max_correlated_exposure_pct: float = 150.0
    # Maps a symbol head -> correlation-group id. Symbols in the same
    # group are treated as one directional bet for the exposure cap.
    correlation_groups: dict[str, str] = field(
        default_factory=lambda: {"BTC": "crypto_majors", "ETH": "crypto_majors"}
    )

    # ---- (4) Pre-trade expected-value (EV) filter (Stage 6b) ----
    enable_ev_filter: bool = True
    # Estimated round-trip cost in basis points (taker in + taker out
    # + slippage both legs). Hyperliquid taker ~3.5bp/leg; 12bp total
    # is a conservative all-in default.
    round_trip_cost_bps: float = 12.0
    # The take-profit reward must beat the round-trip cost by at least
    # this ratio. 2.0 = "TP must be worth >= 2x what it costs to get
    # in and out".
    ev_min_reward_to_cost: float = 2.0

    # ---- (5) Per-asset max position cap ----
    # Per-symbol-head hard cap on notional (USD). 0 / missing => use
    # the router's global ``MAX_POSITION_USD``.
    max_position_usd_per_symbol: dict[str, Decimal] = field(default_factory=dict)

    @property
    def warmup_trigger_loss_frac(self) -> float:
        return self.warmup_trigger_loss_pct / 100.0

    @property
    def max_correlated_exposure_frac(self) -> float:
        return self.max_correlated_exposure_pct / 100.0

    def group_for(self, symbol: str) -> str | None:
        return self.correlation_groups.get(_symbol_head(symbol))


@dataclass
class _WarmupState:
    """Cross-cycle warm-up bookkeeping (the only mutable RiskEngine state)."""

    started_at: datetime | None = None
    last_trigger: str | None = None
    triggers_total: int = 0


class RiskEngine:
    """Portfolio-level risk controls layered over per-trade sizing.

    See the module docstring for the five controls. Construct via
    :meth:`from_settings` in production; tests build the config
    directly for full control over each knob.
    """

    def __init__(self, config: RiskEngineConfig | None = None) -> None:
        self.config = config or RiskEngineConfig()
        self._warmup = _WarmupState()
        # Lightweight telemetry the panel / post-mortem can read.
        self.stats: dict[str, int] = {
            "equity_gate_blocks": 0,
            "warmups_armed": 0,
            "correlation_shrinks": 0,
            "correlation_blocks": 0,
            "ev_blocks": 0,
            "per_asset_cap_hits": 0,
        }

    # ------------------------------------------------------------------
    # (1) Minimum-equity-to-trade gate
    # ------------------------------------------------------------------

    def equity_gate(self, equity_usd: Decimal) -> str | None:
        """Return a block reason when equity is too low to trade, else None.

        Below ``min_equity_to_trade_usd`` the marginal fee + slippage
        drag dominates any edge - we'd rather sit in cash / USYC than
        churn a tiny account to zero.
        """
        floor = self.config.min_equity_to_trade_usd
        if floor <= 0:
            return None
        # equity_usd <= 0 typically means "no margin deposited yet"
        # (first dry-run cycle); don't fire the gate on that - the
        # sizing fallback path handles the no-equity demo case.
        if equity_usd <= 0:
            return None
        if equity_usd < floor:
            self.stats["equity_gate_blocks"] += 1
            return (
                f"equity ${equity_usd:.2f} < MIN_EQUITY_TO_TRADE_USD "
                f"${floor:.2f} - parking in cash/USYC instead of churning "
                "a sub-scale account through fees."
            )
        return None

    # ------------------------------------------------------------------
    # (2) Post-loss warm-up ramp
    # ------------------------------------------------------------------

    def note_close_event(
        self,
        *,
        trigger: str,
        pnl_usd: Decimal,
        equity_usd: Decimal,
        now: datetime | None = None,
    ) -> bool:
        """Record a position close; arm the warm-up ramp on a real loss.

        Arms (or restarts) the warm-up when EITHER:
          * the close trigger is a "we were wrong" event
            (``stop_loss`` / ``daily_dd_guard``), OR
          * the realised loss is >= ``warmup_trigger_loss_pct`` of
            equity.

        A take-profit / break-even / trailing close in profit does
        NOT arm the warm-up. Returns True when the warm-up was armed.
        """
        if not self.config.enable_loss_warmup:
            return False
        stamp = now or datetime.now(timezone.utc)
        hard_loss_trigger = trigger in {"stop_loss", "daily_dd_guard"}
        loss_pct_equity = 0.0
        if pnl_usd < 0 and equity_usd > 0:
            loss_pct_equity = float(-pnl_usd / equity_usd * 100)
        big_loss = loss_pct_equity >= self.config.warmup_trigger_loss_pct
        if not (hard_loss_trigger or big_loss):
            return False
        # Arm / restart the ramp from now (consecutive losses keep
        # size suppressed - the clock resets on each fresh hit).
        self._warmup.started_at = stamp
        self._warmup.last_trigger = trigger
        self._warmup.triggers_total += 1
        self.stats["warmups_armed"] += 1
        logger.info(
            "Risk warm-up ARMED ({}) | loss={:.2f}% of equity -> new "
            "entries sized x{:.2f}, ramping to 1.0 over {:.0f}m.",
            trigger, loss_pct_equity, self.config.warmup_size_mult,
            self.config.warmup_minutes,
        )
        return True

    def warmup_multiplier(self, now: datetime | None = None) -> float:
        """Current size multiplier in ``[warmup_size_mult, 1.0]``.

        1.0 when no warm-up is active. Right after a loss it equals
        ``warmup_size_mult`` and ramps LINEARLY back to 1.0 across
        ``warmup_minutes``.
        """
        if not self.config.enable_loss_warmup:
            return 1.0
        started = self._warmup.started_at
        if started is None:
            return 1.0
        window = self.config.warmup_minutes
        if window <= 0:
            return 1.0
        stamp = now or datetime.now(timezone.utc)
        elapsed_min = (stamp - started).total_seconds() / 60.0
        if elapsed_min >= window:
            # Expired - clear the state so we stop computing.
            self._warmup.started_at = None
            return 1.0
        if elapsed_min < 0:
            elapsed_min = 0.0
        frac = elapsed_min / window
        base = self.config.warmup_size_mult
        return base + (1.0 - base) * frac

    def warmup_remaining_minutes(self, now: datetime | None = None) -> float:
        """Minutes left in the warm-up ramp (0.0 when inactive)."""
        started = self._warmup.started_at
        if started is None or not self.config.enable_loss_warmup:
            return 0.0
        stamp = now or datetime.now(timezone.utc)
        elapsed = (stamp - started).total_seconds() / 60.0
        return max(0.0, self.config.warmup_minutes - elapsed)

    # ------------------------------------------------------------------
    # (3) Correlation / aggregate-exposure cap
    # ------------------------------------------------------------------

    def correlation_adjust(
        self,
        *,
        symbol: str,
        side: str,
        proposed_size_usd: Decimal,
        open_positions: list[Any],
        equity_usd: Decimal,
    ) -> tuple[Decimal, dict[str, Any]]:
        """Shrink (or zero) a new open to respect the correlated-exposure cap.

        Sums existing SAME-SIDE notional across every open position in
        the new trade's correlation group, then limits the new open so
        the group total stays under ``max_correlated_exposure_pct`` of
        equity. Returns ``(adjusted_size, info)``; ``adjusted_size`` is
        ``0`` when there is no room left (the router treats that as a
        deny).

        No-op (returns the proposal unchanged) when the feature is off,
        the symbol is ungrouped, or equity is unavailable.
        """
        info: dict[str, Any] = {"applied": False}
        if not self.config.enable_correlation_cap:
            return proposed_size_usd, info
        group = self.config.group_for(symbol)
        if group is None or equity_usd <= 0:
            return proposed_size_usd, info
        side_norm = (side or "").lower()
        existing = Decimal("0")
        for pos in open_positions:
            psym = getattr(pos, "symbol", "")
            pside = (getattr(pos, "side", "") or "").lower()
            if pside != side_norm:
                continue
            if self.config.group_for(psym) != group:
                continue
            existing += abs(Decimal(str(getattr(pos, "size_usd", 0) or 0)))
        cap_usd = (
            equity_usd * Decimal(str(self.config.max_correlated_exposure_frac))
        ).quantize(Decimal("0.01"))
        room = cap_usd - existing
        info.update(
            applied=True,
            group=group,
            side=side_norm,
            existing_same_side_usd=float(existing),
            cap_usd=float(cap_usd),
            room_usd=float(room),
            proposed_usd=float(proposed_size_usd),
        )
        if room <= 0:
            self.stats["correlation_blocks"] += 1
            info["result"] = "blocked"
            logger.warning(
                "Correlation cap BLOCK | group={} side={} existing=${} "
                "cap=${} - no room for a new {} open.",
                group, side_norm, existing, cap_usd, symbol,
            )
            return Decimal("0"), info
        if proposed_size_usd > room:
            self.stats["correlation_shrinks"] += 1
            adjusted = room.quantize(Decimal("0.01"))
            info["result"] = "shrunk"
            info["adjusted_usd"] = float(adjusted)
            logger.info(
                "Correlation cap SHRINK | group={} side={} ${} -> ${} "
                "(existing=${}, cap=${}).",
                group, side_norm, proposed_size_usd, adjusted,
                existing, cap_usd,
            )
            return adjusted, info
        info["result"] = "ok"
        return proposed_size_usd, info

    # ------------------------------------------------------------------
    # (4) Pre-trade expected-value (EV) filter
    # ------------------------------------------------------------------

    def ev_ok(
        self,
        *,
        tp_reward_pct: float | None,
    ) -> tuple[bool, dict[str, Any]]:
        """Return ``(ok, info)`` for the pre-trade expected-value gate.

        ``tp_reward_pct`` is the expected take-profit move expressed as
        a percent of entry (e.g. ``1.5`` for a 1.5% TP). The trade
        passes when that reward, in basis points, clears
        ``ev_min_reward_to_cost x round_trip_cost_bps``. When the
        reward is unknown (no TP configured / no ATR) we fail OPEN
        (allow) - the EV gate only blocks trades it can prove are
        sub-economic.
        """
        info: dict[str, Any] = {"applied": False}
        if not self.config.enable_ev_filter:
            return True, info
        if tp_reward_pct is None or tp_reward_pct <= 0:
            # Can't evaluate -> don't block (other gates still apply).
            info["reason"] = "no_tp_reward_estimate"
            return True, info
        reward_bps = tp_reward_pct * 100.0
        required_bps = (
            self.config.ev_min_reward_to_cost * self.config.round_trip_cost_bps
        )
        ok = reward_bps >= required_bps
        info.update(
            applied=True,
            tp_reward_bps=round(reward_bps, 2),
            round_trip_cost_bps=self.config.round_trip_cost_bps,
            required_bps=round(required_bps, 2),
            ratio=round(reward_bps / required_bps, 2) if required_bps else None,
            ok=ok,
        )
        if not ok:
            self.stats["ev_blocks"] += 1
            logger.warning(
                "EV filter BLOCK | TP reward {:.1f}bp < required {:.1f}bp "
                "({:.1f}x round-trip cost {:.1f}bp). Trade can't pay for "
                "itself.",
                reward_bps, required_bps,
                self.config.ev_min_reward_to_cost,
                self.config.round_trip_cost_bps,
            )
        return ok, info

    # ------------------------------------------------------------------
    # (5) Per-asset max position cap
    # ------------------------------------------------------------------

    def per_asset_max_position(
        self,
        symbol: str,
        global_max_usd: Decimal,
    ) -> Decimal:
        """Return the effective max notional for ``symbol``.

        Takes the tighter of the per-asset cap (when configured for
        this symbol head) and the global cap. A missing / non-positive
        per-asset entry means "use the global cap".
        """
        head = _symbol_head(symbol)
        per = self.config.max_position_usd_per_symbol.get(head)
        if per is None or per <= 0:
            return global_max_usd
        return min(per, global_max_usd)

    def apply_per_asset_cap(
        self,
        *,
        symbol: str,
        size_usd: Decimal,
        global_max_usd: Decimal,
    ) -> tuple[Decimal, Decimal]:
        """Clip ``size_usd`` to the per-asset cap; return (size, cap)."""
        cap = self.per_asset_max_position(symbol, global_max_usd)
        if size_usd > cap:
            self.stats["per_asset_cap_hits"] += 1
            return cap, cap
        return size_usd, cap

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    @classmethod
    def from_settings(cls, settings: Any) -> "RiskEngine":
        """Build a RiskEngine from a :class:`Settings`-like object."""

        def _get(name: str, default: Any) -> Any:
            val = getattr(settings, name, default)
            return default if val is None else val

        per_asset: dict[str, Decimal] = {}
        for head in ("BTC", "ETH", "DEFAULT"):
            raw = _get(f"MAX_POSITION_USD_{head}", 0.0)
            try:
                amt = Decimal(str(raw))
            except (TypeError, ValueError):
                amt = Decimal("0")
            if amt > 0:
                per_asset[head] = amt

        cfg = RiskEngineConfig(
            min_equity_to_trade_usd=Decimal(
                str(_get("MIN_EQUITY_TO_TRADE_USD", 0.0))
            ),
            enable_loss_warmup=bool(_get("ENABLE_LOSS_WARMUP", True)),
            warmup_minutes=float(_get("WARMUP_AFTER_LOSS_MINUTES", 60.0)),
            warmup_size_mult=float(_get("WARMUP_SIZE_MULT", 0.5)),
            warmup_trigger_loss_pct=float(_get("WARMUP_TRIGGER_LOSS_PCT", 1.0)),
            enable_correlation_cap=bool(_get("ENABLE_CORRELATION_CAP", True)),
            max_correlated_exposure_pct=float(
                _get("MAX_CORRELATED_EXPOSURE_PCT", 150.0)
            ),
            enable_ev_filter=bool(_get("ENABLE_PRETRADE_EV_FILTER", True)),
            round_trip_cost_bps=float(_get("ROUND_TRIP_COST_BPS", 12.0)),
            ev_min_reward_to_cost=float(_get("EV_MIN_REWARD_TO_COST", 2.0)),
            max_position_usd_per_symbol=per_asset,
        )
        return cls(config=cfg)
