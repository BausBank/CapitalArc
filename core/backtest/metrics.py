"""Performance metrics for the backtester (Stage 1).

Computes the honest, gate-relevant statistics from a finished
:class:`PortfolioSimulator`: total return, profit factor, win rate, MaxDD,
risk-free=0 annualised Sharpe + Sortino on the per-bar (1h) equity returns,
plus a deflated-Sharpe *stub* wired for Stage 9 (where the number of trials
becomes known). Also aggregates a list of per-fold metric dicts into
mean / std / min / max so walk-forward stability is visible at a glance.
"""

from __future__ import annotations

import math
import statistics
from typing import Any

from core.backtest import config as C

# Bars are 1h; annualise per-bar stats with sqrt(hours per year).
_ANNUALISATION = math.sqrt(24 * 365)


def _equity_returns(equity_curve: list[tuple[Any, float]]) -> list[float]:
    eqs = [e for _, e in equity_curve]
    rets = []
    for i in range(1, len(eqs)):
        if eqs[i - 1] > 0:
            rets.append(eqs[i] / eqs[i - 1] - 1.0)
    return rets


def _sharpe(rets: list[float]) -> float:
    if len(rets) <= 2:
        return 0.0
    mean = statistics.fmean(rets)
    std = statistics.stdev(rets)
    if std <= 0:
        return 0.0
    return mean / std * _ANNUALISATION


def _sortino(rets: list[float]) -> float:
    if len(rets) <= 2:
        return 0.0
    mean = statistics.fmean(rets)
    downside = [r for r in rets if r < 0]
    if len(downside) < 2:
        return 0.0
    dd = math.sqrt(statistics.fmean([r * r for r in downside]))
    if dd <= 0:
        return 0.0
    return mean / dd * _ANNUALISATION


def deflated_sharpe(observed_sharpe: float, n_trials: int = 1) -> float:
    """Stub for Stage 9's deflated Sharpe.

    With a single trial it returns the observed Sharpe unchanged. The real
    haircut (which discounts for multiple-testing / selection across
    ``n_trials`` configurations) lands in Stage 9 once the trial count is
    known; the signature is fixed now so callers don't change later.
    """
    if n_trials <= 1:
        return observed_sharpe
    return observed_sharpe  # TODO(Stage 9): apply DSR multiple-testing haircut


def compute_metrics(sim: Any) -> dict[str, Any]:
    """Compute the metric dict for a finished PortfolioSimulator."""
    trades = sim.trades
    n = len(trades)
    nets = [t.net_pnl for t in trades]
    wins = [x for x in nets if x > 0]
    losses = [x for x in nets if x <= 0]
    gross_profit = sum(wins)
    gross_loss = -sum(losses)

    # realised equity = initial + sum(trade nets) + total funding cashflow
    total_funding = float(getattr(sim, "total_funding", 0.0))
    total_pnl = sum(nets)
    final_equity = C.INITIAL_EQUITY + total_pnl + total_funding
    total_return_pct = (final_equity / C.INITIAL_EQUITY - 1.0) * 100.0
    win_rate = (len(wins) / n * 100.0) if n else 0.0
    profit_factor = (
        (gross_profit / gross_loss)
        if gross_loss > 0
        else (float("inf") if gross_profit > 0 else 0.0)
    )
    avg_dur = (sum(t.duration_h for t in trades) / n) if n else 0.0

    peak = C.INITIAL_EQUITY
    max_dd = 0.0
    for _, eq in sim.equity_curve:
        peak = max(peak, eq)
        dd = (peak - eq) / peak * 100.0 if peak > 0 else 0.0
        max_dd = max(max_dd, dd)

    rets = _equity_returns(sim.equity_curve)
    sharpe = _sharpe(rets)
    sortino = _sortino(rets)

    tc = sim.trigger_counts
    return {
        "total_return_pct": total_return_pct,
        "final_equity": final_equity,
        "profit_factor": profit_factor,
        "win_rate": win_rate,
        "max_drawdown_pct": max_dd,
        "sharpe": sharpe,
        "sortino": sortino,
        "deflated_sharpe": deflated_sharpe(sharpe, n_trials=1),
        "n_trades": n,
        "n_opens": tc.get("open", 0),
        "avg_duration_h": avg_dur,
        "n_stop_loss": tc.get("stop_loss", 0),
        "n_take_profit": tc.get("take_profit", 0),
        "n_partial_tp": tc.get("partial_take_profit", 0),
        "n_trailing": tc.get("trailing_stop", 0),
        "n_time_exit": tc.get("time_exit", 0),
        "n_side_flip": tc.get("side_flip", 0),
        "n_re_eval": tc.get("re_evaluation", 0),
        "n_daily_dd": tc.get("daily_dd_guard", 0),
        "total_fees": float(getattr(sim, "total_fees", 0.0)),
        "total_funding": total_funding,
        "blocks": dict(sim.blocks),
    }


_AGG_KEYS = (
    "total_return_pct",
    "sharpe",
    "sortino",
    "max_drawdown_pct",
    "win_rate",
    "profit_factor",
    "n_opens",
)


def aggregate_folds(fold_metrics: list[dict[str, Any]]) -> dict[str, Any]:
    """Mean / std / min / max of key metrics across walk-forward folds."""
    out: dict[str, Any] = {"n_folds": len(fold_metrics)}
    for key in _AGG_KEYS:
        vals = [
            float(m[key])
            for m in fold_metrics
            if key in m and m[key] != float("inf")
        ]
        if not vals:
            continue
        out[key] = {
            "mean": statistics.fmean(vals),
            "std": statistics.stdev(vals) if len(vals) > 1 else 0.0,
            "min": min(vals),
            "max": max(vals),
        }
    return out


__all__ = ["compute_metrics", "aggregate_folds", "deflated_sharpe"]
