"""Backtest driver + CLI (Stage 1).

Runs the honest baseline two ways:

* **Aggregate** - one continuous pass over the whole 1h timeline (the row
  that lands in ``docs/PLAN.md`` Table of Results).
* **Per-fold** - the same scenario re-run on each purged & embargoed
  walk-forward TEST block (fresh flat book per fold), so metric stability
  across market regimes is visible. The walk-forward splitter is generic
  infra reused by Stage 7; the leakage guarantees are asserted in
  ``tests/test_walkforward_split.py``.

Usage (from project root):
    python -m core.backtest.runner            # cached data + baseline
    python -m core.backtest.runner --refresh  # force re-download (12mo 1h)
    python -m core.backtest.runner --splits 6 # walk-forward fold count
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from typing import Any

import pandas as pd

# --- bootstrap: project root on sys.path so `src` + `core` import -----------
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from core.backtest import config as C  # noqa: E402
from core.backtest.config import SimConfig, cfg_baseline  # noqa: E402
from core.backtest.costs import CostModel  # noqa: E402
from core.backtest.data import (  # noqa: E402
    DATA_DIR,
    SYMBOLS,
    build_frames,
    load_market_data,
)
from core.backtest.engine_adapter import (  # noqa: E402
    BacktestMarketData,
    build_engine,
    build_risk_engine,
)
from core.backtest.metrics import aggregate_folds, compute_metrics  # noqa: E402
from core.backtest.simulator import PortfolioSimulator  # noqa: E402
from core.backtest.walkforward import walk_forward_splits  # noqa: E402

WARMUP_BARS = 150


def _primary_atr(decision: Any, symbol: str) -> float | None:
    l1 = decision.level_score(1)
    if l1 is None:
        return None
    per = (l1.raw.get("l1", {}) or {}).get("per_symbol") or {}
    ro = per.get(symbol) or next(iter(per.values()), None)
    if not ro:
        return None
    v = ro.get("atr_pct_avg")
    try:
        return float(v) if v else None
    except (TypeError, ValueError):
        return None


def _funding_rate_at(fdf: pd.DataFrame | None, ts: pd.Timestamp) -> float:
    """Latest funding rate with time <= ts (anti-look-ahead)."""
    if fdf is None or fdf.empty:
        return 0.0
    fsub = fdf[fdf.index <= ts]
    if fsub.empty:
        return 0.0
    return float(fsub["fundingRate"].iloc[-1])


async def run_scenario(
    sim_cfg: SimConfig,
    cost_model: CostModel,
    frames: dict[tuple[str, str], pd.DataFrame],
    funding: dict[str, pd.DataFrame],
    timeline: list[pd.Timestamp],
) -> dict[str, Any]:
    market_data = BacktestMarketData(frames)
    engine, l2 = build_engine(market_data, sim_cfg)
    risk = build_risk_engine(sim_cfg, round_trip_cost_bps=cost_model.round_trip_bps)
    portf = PortfolioSimulator(sim_cfg, risk, cost_model)

    df1h = {s: frames[(s, "1h")] for s in SYMBOLS}

    for ts in timeline:
        ts_unix = int(ts.timestamp())
        market_data.cutoff_ts = ts_unix
        when = ts.to_pydatetime()

        bars: dict[str, dict[str, float]] = {}
        for s in SYMBOLS:
            d = df1h[s]
            if ts in d.index:
                row = d.loc[ts]
                bars[s] = {
                    "open": float(row["open"]), "high": float(row["high"]),
                    "low": float(row["low"]), "close": float(row["close"]),
                    "volume": float(row["volume"]),
                }
        if len(bars) < len(SYMBOLS):
            continue
        marks = {s: bars[s]["close"] for s in SYMBOLS}

        # --- build L2 feed + funding marks (all data <= ts) ---
        l2.feed = {}
        funding_marks: dict[str, float] = {}
        for s in SYMBOLS:
            d = df1h[s]
            sub = d[d.index <= ts]
            if len(sub) < 25:
                continue
            close_now = float(sub["close"].iloc[-1])
            close_24 = float(sub["close"].iloc[-25])
            pch = (close_now / close_24 - 1.0) * 100.0 if close_24 > 0 else 0.0
            frate = _funding_rate_at(funding.get(s), ts)
            funding_marks[s] = frate
            vol24 = float(sub["volume"].iloc[-24:].sum()) * close_now
            vol1 = float(sub["volume"].iloc[-1]) * close_now
            l2.feed[s] = {
                "funding_rate": frate,
                "price_change_pct_24h": pch,
                "last_price": close_now,
                "volume_24h_usd": vol24,
                "volume_1h_usd": vol1,
            }

        # --- run decision engine per symbol ---
        conv_by_sym: dict[str, float] = {}
        flips: dict[str, bool] = {}
        directives: dict[str, Any] = {}
        dd_now = portf.drawdown_pct(marks)
        for s in SYMBOLS:
            ctx = {"symbol": s, "symbols": SYMBOLS, "account_drawdown_pct": dd_now}
            decision = await engine.decide(ctx)
            d = decision.directive
            directives[s] = decision
            conv_by_sym[s] = float(decision.final_score)
            held = portf.positions.get(s)
            new_side = d.side if d.action == "risk_on" else None
            flips[s] = bool(
                held is not None and new_side is not None and new_side != held.side
            )

        # --- review existing positions (Fast Path + funding accrual) ---
        portf.review_positions(when, bars, conv_by_sym, flips, funding_marks)

        # --- dispatch new opens / closes ---
        for s in SYMBOLS:
            decision = directives[s]
            d = decision.directive
            atr_pct = _primary_atr(decision, s)
            if d.action == "risk_off":
                held = portf.positions.get(s)
                if held is not None:
                    portf._close(held, marks[s], when, "risk_off")
            elif d.action == "risk_on" and d.side in ("long", "short") and atr_pct:
                portf.try_open(
                    when=when, symbol=s, side=d.side, intensity=float(d.intensity),
                    atr_pct=atr_pct, conviction=float(decision.final_score),
                    marks=marks, price=marks[s],
                )

    metrics = compute_metrics(portf)
    metrics["scenario"] = sim_cfg.name
    metrics["eq_engine_stats"] = (
        engine.entry_quality_gate.stats if engine.entry_quality_gate else {}
    )
    metrics["risk_stats"] = risk.stats
    return metrics


def _build_timeline(
    frames: dict[tuple[str, str], pd.DataFrame],
) -> list[pd.Timestamp]:
    idx_btc = frames[("BTC-PERP", "1h")].index
    idx_eth = frames[("ETH-PERP", "1h")].index
    common = idx_btc.intersection(idx_eth)
    return list(common[WARMUP_BARS:])


# ---------------------------------------------------------------------------
# Reporting (retro terminal)
# ---------------------------------------------------------------------------


def _fmt_pf(pf: float) -> str:
    return "inf" if pf == float("inf") else f"{pf:.2f}"


def _print_aggregate(res: dict[str, Any], period: str, costs: CostModel) -> None:
    w = 64
    print("\n" + "=" * w)
    print("=== CapitalArc BACKTEST :: HONEST BASELINE (aggregate) ===".center(w))
    print(f"{period}".center(w))
    print(
        f"costs: taker {costs.taker_bps}bp/leg + slip {costs.slippage_bps}bp"
        f" | funding {'ON' if costs.apply_funding else 'OFF'}"
        f" | round-trip {costs.round_trip_bps:.1f}bp".center(w)
    )
    print("=" * w)
    rows = [
        ("Total Return %", f"{res['total_return_pct']:+.2f}"),
        ("Final Equity $", f"{res['final_equity']:,.0f}"),
        ("Profit Factor", _fmt_pf(res["profit_factor"])),
        ("Win Rate %", f"{res['win_rate']:.1f}"),
        ("Max Drawdown %", f"{res['max_drawdown_pct']:.2f}"),
        ("Sharpe (annualised)", f"{res['sharpe']:.2f}"),
        ("Sortino (annualised)", f"{res['sortino']:.2f}"),
        ("Deflated Sharpe", f"{res['deflated_sharpe']:.2f}"),
        ("Trades / Opens", f"{res['n_trades']} / {res['n_opens']}"),
        ("Avg Duration (h)", f"{res['avg_duration_h']:.1f}"),
        ("Total Fees $", f"{res['total_fees']:,.0f}"),
        ("Total Funding $", f"{res['total_funding']:+,.0f}"),
    ]
    for label, val in rows:
        print(f"  {label:<26}: {val:>14}")
    print("-" * w)
    exits = [
        ("stop_loss", res["n_stop_loss"]), ("take_profit", res["n_take_profit"]),
        ("partial_tp", res["n_partial_tp"]), ("trailing", res["n_trailing"]),
        ("time_exit", res["n_time_exit"]), ("side_flip", res["n_side_flip"]),
        ("re_eval", res["n_re_eval"]), ("daily_dd", res["n_daily_dd"]),
    ]
    print("  exits: " + " ".join(f"{k}={v}" for k, v in exits))
    print("=" * w)


def _print_folds(folds: list[dict[str, Any]], agg: dict[str, Any]) -> None:
    w = 64
    print("\n" + "=" * w)
    print("=== WALK-FORWARD :: per-fold (purged & embargoed) ===".center(w))
    print("=" * w)
    print(f"  {'fold':<5}{'ret%':>9}{'sharpe':>9}{'sortino':>9}{'maxDD%':>9}{'opens':>8}")
    print("-" * w)
    for m in folds:
        print(
            f"  {m['fold']:<5}{m['total_return_pct']:>9.2f}{m['sharpe']:>9.2f}"
            f"{m['sortino']:>9.2f}{m['max_drawdown_pct']:>9.2f}{m['n_opens']:>8}"
        )
    print("-" * w)
    if "sharpe" in agg:
        s = agg["sharpe"]
        r = agg["total_return_pct"]
        print(
            f"  agg   sharpe mean={s['mean']:.2f} std={s['std']:.2f} "
            f"[{s['min']:.2f},{s['max']:.2f}] | ret mean={r['mean']:.2f}%"
        )
    print("=" * w)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--refresh", action="store_true", help="re-download market data")
    parser.add_argument("--splits", type=int, default=5, help="walk-forward fold count")
    parser.add_argument("--embargo", type=int, default=24, help="embargo bars")
    parser.add_argument("--purge", type=int, default=24, help="purge bars")
    args = parser.parse_args()

    # silence the agent's per-cycle loguru INFO spam during thousands of cycles
    try:
        from src.utils.logging import logger as _lg

        _lg.remove()
    except Exception:  # noqa: BLE001
        pass

    blob = load_market_data(refresh=args.refresh)
    frames, funding = build_frames(blob)
    timeline = _build_timeline(frames)
    if len(timeline) < 50:
        raise SystemExit(f"[ERR] timeline too short ({len(timeline)} bars)")
    p0 = timeline[0].strftime("%Y-%m-%d")
    p1 = timeline[-1].strftime("%Y-%m-%d")
    period = f"{p0} -> {p1} ({len(timeline)} x 1h, {len(SYMBOLS)} symbols)"
    print(f"[OK] Timeline: {period}")

    cost_model = CostModel()  # taker/taker + 1.5bp slippage + funding on
    sim_cfg = cfg_baseline()

    # --- aggregate pass ---
    print(f"[>>>] Aggregate baseline: {sim_cfg.name} ...")
    agg_res = asyncio.run(run_scenario(sim_cfg, cost_model, frames, funding, timeline))
    _print_aggregate(agg_res, period, cost_model)

    # --- per-fold walk-forward ---
    splits = walk_forward_splits(
        len(timeline), n_splits=args.splits,
        embargo_bars=args.embargo, purge_bars=args.purge, anchored=True,
    )
    fold_metrics: list[dict[str, Any]] = []
    for sp in splits:
        sub = [timeline[i] for i in sp.test_idx]
        print(
            f"[>>>] Fold {sp.fold}: test bars={len(sub)} "
            f"(train={sp.n_train}, gap={sp.gap_bars}) ..."
        )
        m = asyncio.run(run_scenario(sim_cfg, cost_model, frames, funding, sub))
        m["fold"] = sp.fold
        fold_metrics.append(m)
    agg = aggregate_folds(fold_metrics)
    if fold_metrics:
        _print_folds(fold_metrics, agg)

    # --- dump results JSON ---
    out = os.path.join(DATA_DIR, "results.json")
    payload = {
        "period": period,
        "costs": {
            "taker_bps": cost_model.taker_bps,
            "maker_bps": cost_model.maker_bps,
            "slippage_bps": cost_model.slippage_bps,
            "round_trip_bps": cost_model.round_trip_bps,
            "funding": cost_model.apply_funding,
        },
        "aggregate": {
            k: v for k, v in agg_res.items()
            if k not in ("blocks", "eq_engine_stats", "risk_stats")
        },
        "folds": [
            {k: v for k, v in m.items()
             if k not in ("blocks", "eq_engine_stats", "risk_stats")}
            for m in fold_metrics
        ],
        "fold_aggregate": agg,
    }
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, default=str)
    print(f"\n[OK] Results JSON -> {out}")
    print(
        f"  guards: blocks={agg_res.get('blocks')} | "
        f"entry_gate={agg_res.get('eq_engine_stats')} | risk={agg_res.get('risk_stats')}"
    )


if __name__ == "__main__":
    main()
