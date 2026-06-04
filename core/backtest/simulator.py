"""Portfolio execution simulator (Stage 1).

Promoted from the throwaway script. Mirrors the production sizing pipeline
(`AllocationRouter._compute_size`) + the `PositionManager` Fast-Path trigger
ladder + Day-1 post-stop cooldown + Day-2 risk-engine controls.

Stage-1 honesty upgrades:
* All fees + slippage go through :class:`core.backtest.costs.CostModel`
  (taker/taker + fixed-bps slippage, split per leg) instead of one flat bp.
* Per-bar funding cashflow is accrued on every open position in
  :meth:`review_positions` - the DoD's missing funding leg.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from core.backtest import config as C
from core.backtest.config import SimConfig
from core.backtest.costs import CostModel
from src.allocation.risk_engine import RiskEngine


@dataclass
class Position:
    symbol: str
    side: str  # long | short
    size_usd: float
    entry_price: float
    entry_atr_pct: float
    leverage: float
    opened_at: datetime
    peak_pnl_frac: float = 0.0
    breakeven_armed: bool = False
    breakeven_sl_price: float | None = None
    partial_done: bool = False


@dataclass
class Trade:
    symbol: str
    side: str
    entry_price: float
    exit_price: float
    size_usd: float
    pnl_usd: float
    fee_usd: float
    funding_usd: float
    opened_at: datetime
    closed_at: datetime
    trigger: str

    @property
    def duration_h(self) -> float:
        return (self.closed_at - self.opened_at).total_seconds() / 3600.0

    @property
    def net_pnl(self) -> float:
        # funding is booked to cash as it accrues (not per-trade); net here
        # is gross PnL minus the trade's own entry/exit costs.
        return self.pnl_usd - self.fee_usd


class PortfolioSimulator:
    """Shared-equity simulator trading both perps with one risk book."""

    def __init__(
        self, sim: SimConfig, risk_engine: RiskEngine, cost_model: CostModel
    ) -> None:
        self.sim = sim
        self.risk = risk_engine
        self.costs = cost_model
        self.cash = C.INITIAL_EQUITY
        self.peak_equity = C.INITIAL_EQUITY
        self.positions: dict[str, Position] = {}
        self.trades: list[Trade] = []
        self.equity_curve: list[tuple[datetime, float]] = []
        self.cooldowns: dict[str, datetime] = {}
        self.session_day: datetime | None = None
        self.session_start_equity = C.INITIAL_EQUITY
        self.daily_dd_tripped = False
        self.trigger_counts: dict[str, int] = {}
        self.blocks: dict[str, int] = {}
        self.total_funding: float = 0.0
        self.total_fees: float = 0.0

    # ---- equity helpers ----
    def unrealized(self, marks: dict[str, float]) -> float:
        tot = 0.0
        for p in self.positions.values():
            mk = marks.get(p.symbol)
            if mk is None:
                continue
            sgn = 1.0 if p.side == "long" else -1.0
            tot += p.size_usd * (mk / p.entry_price - 1.0) * sgn
        return tot

    def equity(self, marks: dict[str, float]) -> float:
        return self.cash + self.unrealized(marks)

    def drawdown_pct(self, marks: dict[str, float]) -> float:
        eq = self.equity(marks)
        if self.peak_equity <= 0:
            return 0.0
        return max(0.0, (self.peak_equity - eq) / self.peak_equity * 100.0)

    def _bump(self, d: dict[str, int], k: str) -> None:
        d[k] = d.get(k, 0) + 1

    @staticmethod
    def _atr_abs(entry: float, atr_pct: float) -> float:
        return entry * atr_pct / 100.0

    # ---- close logic ----
    def _close(
        self,
        p: Position,
        exit_price: float,
        when: datetime,
        trigger: str,
        fraction: float = 1.0,
    ) -> None:
        close_size = p.size_usd * fraction
        sgn = 1.0 if p.side == "long" else -1.0
        pnl = close_size * (exit_price / p.entry_price - 1.0) * sgn
        fee = self.costs.exit_cost_usd(close_size)
        self.cash += pnl - fee
        self.total_fees += fee
        self.trades.append(
            Trade(
                symbol=p.symbol, side=p.side, entry_price=p.entry_price,
                exit_price=exit_price, size_usd=close_size, pnl_usd=pnl,
                fee_usd=fee, funding_usd=0.0, opened_at=p.opened_at,
                closed_at=when, trigger=trigger,
            )
        )
        self._bump(self.trigger_counts, trigger)
        if fraction >= 1.0:
            self.positions.pop(p.symbol, None)
        else:
            p.size_usd -= close_size
        if trigger in ("stop_loss", "daily_dd_guard"):
            if self.sim.post_stop_cooldown_minutes > 0:
                self.cooldowns[p.symbol] = when + timedelta(
                    minutes=self.sim.post_stop_cooldown_minutes
                )
        self.risk.note_close_event(
            trigger=trigger, pnl_usd=Decimal(str(pnl - fee)),
            equity_usd=Decimal(str(max(1.0, self.cash))), now=when,
        )

    # ---- per-bar funding accrual on open positions ----
    def _accrue_funding(self, when: datetime, funding_marks: dict[str, float]) -> None:
        if not self.costs.apply_funding:
            return
        for p in self.positions.values():
            rate = funding_marks.get(p.symbol)
            if rate is None:
                continue
            cash_flow = self.costs.funding_cashflow_usd(
                side=p.side, notional_usd=p.size_usd, hourly_rate=rate
            )
            self.cash += cash_flow
            self.total_funding += cash_flow

    # ---- Fast-Path review on a single candle (intrabar high/low) ----
    def review_positions(
        self,
        when: datetime,
        bars: dict[str, dict[str, float]],
        conv: dict[str, float],
        flips: dict[str, bool],
        funding_marks: dict[str, float] | None = None,
    ) -> None:
        marks = {s: b["close"] for s, b in bars.items()}

        # funding accrues every bar on whatever is open at bar open
        self._accrue_funding(when, funding_marks or {})

        # --- daily-DD session rollover + kill switch ---
        day = when.replace(hour=0, minute=0, second=0, microsecond=0)
        if self.session_day != day:
            self.session_day = day
            self.session_start_equity = self.equity(marks)
            self.daily_dd_tripped = False
        eq_now = self.equity(marks)
        session_loss_pct = (
            (self.session_start_equity - eq_now)
            / max(1.0, self.session_start_equity)
            * 100.0
        )
        if (
            not self.daily_dd_tripped
            and session_loss_pct >= C.DAILY_LOSS_LIMIT_PCT
            and self.positions
        ):
            for p in list(self.positions.values()):
                b = bars.get(p.symbol)
                if b:
                    self._close(p, b["close"], when, "daily_dd_guard")
            self.daily_dd_tripped = True
            self._mark_curve(when, marks)
            return

        for sym in list(self.positions.keys()):
            p = self.positions.get(sym)
            if p is None:
                continue
            b = bars.get(sym)
            if b is None:
                continue
            atr_abs = self._atr_abs(p.entry_price, p.entry_atr_pct)
            if atr_abs <= 0:
                continue
            sgn = 1.0 if p.side == "long" else -1.0
            high, low, close = b["high"], b["low"], b["close"]

            if p.side == "long":
                sl_price = p.entry_price - C.SL_ATR_MULT * atr_abs
                tp_price = p.entry_price + C.TP_ATR_MULT * atr_abs
                partial_price = p.entry_price + C.PARTIAL_TP_ATR_MULT * atr_abs
                be_trigger = p.entry_price + C.BREAKEVEN_TRIGGER_ATR_MULT * atr_abs
            else:
                sl_price = p.entry_price + C.SL_ATR_MULT * atr_abs
                tp_price = p.entry_price - C.TP_ATR_MULT * atr_abs
                partial_price = p.entry_price - C.PARTIAL_TP_ATR_MULT * atr_abs
                be_trigger = p.entry_price - C.BREAKEVEN_TRIGGER_ATR_MULT * atr_abs

            if p.breakeven_armed and p.breakeven_sl_price is not None:
                if p.side == "long":
                    sl_price = max(sl_price, p.breakeven_sl_price)
                else:
                    sl_price = min(sl_price, p.breakeven_sl_price)

            # Priority ladder (first match wins). Intrabar: SL before TP
            # (pessimistic standard convention when a bar straddles both).
            if p.side == "long" and low <= sl_price:
                self._close(p, sl_price, when, "stop_loss")
                continue
            if p.side == "short" and high >= sl_price:
                self._close(p, sl_price, when, "stop_loss")
                continue
            age_h = (when - p.opened_at).total_seconds() / 3600.0
            if C.MAX_POSITION_HOLD_HOURS > 0 and age_h >= C.MAX_POSITION_HOLD_HOURS:
                self._close(p, close, when, "time_exit")
                continue
            if p.side == "long" and high >= tp_price:
                self._close(p, tp_price, when, "take_profit")
                continue
            if p.side == "short" and low <= tp_price:
                self._close(p, tp_price, when, "take_profit")
                continue
            if not p.partial_done and 0 < C.PARTIAL_TP_FRACTION < 1:
                hit_partial = (p.side == "long" and high >= partial_price) or (
                    p.side == "short" and low <= partial_price
                )
                if hit_partial:
                    self._close(
                        p, partial_price, when, "partial_take_profit",
                        fraction=C.PARTIAL_TP_FRACTION,
                    )
                    p.partial_done = True
                    p = self.positions.get(sym)
                    if p is None:
                        continue

            cur_pnl_frac = (close / p.entry_price - 1.0) * sgn
            p.peak_pnl_frac = max(p.peak_pnl_frac, cur_pnl_frac)

            armed_now = (p.side == "long" and high >= be_trigger) or (
                p.side == "short" and low <= be_trigger
            )
            if armed_now and not p.breakeven_armed:
                p.breakeven_armed = True
                buf = p.entry_price * C.BREAKEVEN_BUFFER_PCT
                p.breakeven_sl_price = (
                    p.entry_price + buf if p.side == "long" else p.entry_price - buf
                )
                self._bump(self.trigger_counts, "breakeven_arm")

            trail_giveback_frac = C.TRAIL_ATR_MULT * atr_abs / p.entry_price
            if (
                p.peak_pnl_frac > 0
                and (p.peak_pnl_frac - cur_pnl_frac) >= trail_giveback_frac
                and p.peak_pnl_frac >= trail_giveback_frac
            ):
                self._close(p, close, when, "trailing_stop")
                continue
            if flips.get(sym):
                self._close(p, close, when, "side_flip")
                continue
            c = conv.get(sym, 1.0)
            if c < C.MIN_CONVICTION_TO_HOLD and 0 < cur_pnl_frac < 0.02:
                self._close(p, close, when, "re_evaluation")
                continue

        self._mark_curve(when, marks)

    def _mark_curve(self, when: datetime, marks: dict[str, float]) -> None:
        eq = self.equity(marks)
        self.peak_equity = max(self.peak_equity, eq)
        self.equity_curve.append((when, eq))

    # ---- open logic (mirrors router risk_on path) ----
    def try_open(
        self,
        when: datetime,
        symbol: str,
        side: str,
        intensity: float,
        atr_pct: float,
        conviction: float,
        marks: dict[str, float],
        price: float,
    ) -> None:
        if self.daily_dd_tripped:
            return
        if symbol in self.positions:
            self._bump(self.blocks, "duplicate_or_held")
            return
        eq = self.equity(marks)
        block = self.risk.equity_gate(Decimal(str(eq)))
        if block is not None:
            self._bump(self.blocks, "equity_gate")
            return
        cd = self.cooldowns.get(symbol)
        if cd is not None:
            if when < cd:
                self._bump(self.blocks, "post_stop_cooldown")
                return
            else:
                self.cooldowns.pop(symbol, None)
        cap = C.PER_ASSET_ATR_CAP.get(symbol, 4.0)
        if atr_pct > cap:
            self._bump(self.blocks, "atr_cap")
            return
        tp_reward_pct = C.TP_ATR_MULT * atr_pct
        ev_ok, _ = self.risk.ev_ok(tp_reward_pct=tp_reward_pct)
        if not ev_ok:
            self._bump(self.blocks, "ev_filter")
            return

        # --- sizing pipeline (router._compute_size) ---
        atr_floored = max(atr_pct, C.MIN_ATR_PCT_FOR_SIZING)
        stop_dist = C.STOP_ATR_MULT * (atr_floored / 100.0)
        vol_target = (eq * C.RISK_PER_TRADE_FRAC) / stop_dist if stop_dist > 0 else 0.0
        after_intensity = vol_target * max(0.0, min(1.0, intensity))
        dd = self.drawdown_pct(marks)
        haircut = (
            max(0.0, 1.0 - (dd / C.MAX_DRAWDOWN_PCT) ** C.DD_HAIRCUT_EXPONENT)
            if dd > 0
            else 1.0
        )
        sized = min(after_intensity * haircut, C.MAX_POSITION_USD)
        wm = self.risk.warmup_multiplier(now=when)
        sized *= wm
        adj, _info = self.risk.correlation_adjust(
            symbol=symbol, side=side,
            proposed_size_usd=Decimal(str(round(sized, 2))),
            open_positions=[
                type("P", (), {"symbol": q.symbol, "side": q.side, "size_usd": q.size_usd})()
                for q in self.positions.values()
            ],
            equity_usd=Decimal(str(eq)),
        )
        sized = float(adj)
        if sized <= 1.0:
            self._bump(self.blocks, "correlation_cap")
            return

        fee = self.costs.entry_cost_usd(sized)
        self.cash -= fee
        self.total_fees += fee
        self.positions[symbol] = Position(
            symbol=symbol, side=side, size_usd=sized, entry_price=price,
            entry_atr_pct=atr_pct, leverage=C.MAX_LEVERAGE, opened_at=when,
        )
        self._bump(self.trigger_counts, "open")


__all__ = ["Position", "Trade", "PortfolioSimulator"]
