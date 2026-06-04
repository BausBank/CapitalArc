"""Honest cost model for the CapitalArc backtester (Stage 1).

Replaces the throwaway script's single flat ``ROUND_TRIP_COST_BPS = 12``
with a transparent, auditable breakdown that the plan's DoD requires:

    1. **Maker / taker fees, split per leg.** Hyperliquid tier-0 charges a
       taker ~4.5 bp and a maker ~1.5 bp. The baseline models BOTH legs as
       *taker* (market entries + market exits) - the conservative, honest
       assumption. ``maker_bps`` is carried so Stage 8 can flip a leg to
       post-only without touching this module.

    2. **Slippage**, a fixed basis-point charge applied adversely on EVERY
       fill (entry and exit). Folded into the per-leg cost in USD; this is
       equivalent to worsening the fill price by ``slippage_bps``.

    3. **Funding cashflow**, accrued per bar against every open position.
       Hyperliquid funds hourly; the backtest timeline is 1h, so funding
       is applied once per bar per open position. A long PAYS funding when
       the rate is positive (and receives when negative); a short is the
       mirror.

Design notes
============
* All fees / slippage are expressed in basis points (1 bp = 0.01%).
* ``round_trip_bps`` is exposed so the :class:`RiskEngine` EV filter reads
  the SAME cost the simulator charges - one source of truth. With the
  taker/taker + 1.5 bp slippage defaults this lands at 12.0 bp, matching
  the engine's historical EV default, but now honestly decomposed.
* Pure value object: no I/O, no state, trivially unit-testable.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CostModel:
    """Per-leg fee + slippage + funding cost model.

    Attributes
    ----------
    taker_bps:
        Taker (market-order) fee per leg, in basis points. HL tier-0 ~4.5.
    maker_bps:
        Maker (post-only) fee per leg, in basis points. HL tier-0 ~1.5.
        Unused while both legs are taker; reserved for Stage 8.
    slippage_bps:
        Adverse slippage applied on every fill (entry AND exit), in bp.
    entry_liquidity / exit_liquidity:
        ``"taker"`` (default both) or ``"maker"``. Selects which fee rate
        applies to that leg.
    apply_funding:
        When True, :meth:`funding_cashflow_usd` returns the per-bar funding
        cashflow; when False it is a no-op (returns 0.0).
    """

    taker_bps: float = 4.5
    maker_bps: float = 1.5
    slippage_bps: float = 1.5
    entry_liquidity: str = "taker"
    exit_liquidity: str = "taker"
    apply_funding: bool = True

    # ---- fees ----
    def _fee_bps(self, liquidity: str) -> float:
        return self.maker_bps if liquidity == "maker" else self.taker_bps

    def entry_cost_usd(self, notional_usd: float) -> float:
        """Fee + slippage charged when opening ``notional_usd`` of exposure."""
        bps = self._fee_bps(self.entry_liquidity) + self.slippage_bps
        return abs(notional_usd) * bps / 10_000.0

    def exit_cost_usd(self, notional_usd: float) -> float:
        """Fee + slippage charged when closing ``notional_usd`` of exposure."""
        bps = self._fee_bps(self.exit_liquidity) + self.slippage_bps
        return abs(notional_usd) * bps / 10_000.0

    @property
    def round_trip_bps(self) -> float:
        """Total entry+exit cost in bp (fees both legs + slippage both legs).

        Wired into ``RiskEngineConfig.round_trip_cost_bps`` so the pre-trade
        EV filter and the simulator charge the exact same round-trip cost.
        """
        return (
            self._fee_bps(self.entry_liquidity)
            + self._fee_bps(self.exit_liquidity)
            + 2.0 * self.slippage_bps
        )

    # ---- funding ----
    def funding_cashflow_usd(
        self, *, side: str, notional_usd: float, hourly_rate: float
    ) -> float:
        """Per-bar (hourly) funding cashflow for an open position.

        Sign convention: a LONG pays funding when ``hourly_rate > 0`` (cash
        out, negative cashflow) and receives it when negative; a SHORT is
        the mirror image. Returns 0.0 when funding is disabled.
        """
        if not self.apply_funding:
            return 0.0
        side_sign = 1.0 if str(side).lower() == "long" else -1.0
        return -side_sign * abs(notional_usd) * float(hourly_rate)


__all__ = ["CostModel"]
