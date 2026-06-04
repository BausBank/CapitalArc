"""CapitalArc backtest harness (Stage 1 - honest measurement).

Promoted from the root ``backtest_runner.py`` throwaway script into a
trusted, reusable package. It walks the REAL decision stack
(`DecisionEngine` + `Level1` + data-injected `Level2` + synthetic L3 +
`EntryQualityGate` + `RiskEngine`) candle-by-candle over real Hyperliquid
mainnet 1h OHLCV + funding, and routes the resulting directives through a
faithful execution simulator that mirrors the production sizing pipeline
and the `PositionManager` Fast-Path trigger ladder.

Stage-1 honesty upgrades over the throwaway script:

* :mod:`core.backtest.costs` - maker/taker fees split per leg, a fixed-bps
  slippage charge on every fill, and per-bar funding cashflow on open
  positions (previously funding was fetched but never applied to PnL).
* :mod:`core.backtest.walkforward` - a generic purged & embargoed
  walk-forward splitter (reused by Stage 7 meta-labeling) plus a baseline
  that runs per fold + one continuous aggregate pass.
* :mod:`core.backtest.metrics` - risk-free=0 hourly Sharpe, Sortino,
  MaxDD, profit factor and a deflated-Sharpe stub for Stage 9.

The package imports the genuine production modules from ``src`` - it never
re-implements signal logic. L3 runs as the deterministic synthetic
placeholder (no OpenRouter call) so the backtest stays free + reproducible.
"""

from core.backtest.costs import CostModel
from core.backtest.walkforward import WalkForwardSplit, walk_forward_splits

__all__ = ["CostModel", "WalkForwardSplit", "walk_forward_splits"]
