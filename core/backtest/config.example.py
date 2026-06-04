"""Backtest scenario config - TEMPLATE.

Copy this file to ``core/backtest/config.py`` and dial in your own operating
point::

    cp core/backtest/config.example.py core/backtest/config.py

The real ``config.py`` is git-ignored (same pattern as ``.env`` / ``.env.example``)
because the tuned ``cfg_baseline()`` knob values are strategy parameters.
This template ships a VANILLA baseline (no tuning: every control off / at its
no-op floor) so the public framework runs end-to-end out of the box while the
tuned operating point stays private.

The module-level constants below are framework scaffolding (standard ATR
multiples, vol-target risk %, drawdown guard) - not alpha. The thing that
makes a strategy live is which controls you enable in ``cfg_baseline()``.
"""

from __future__ import annotations

from dataclasses import dataclass

# --- engine ---
RISK_ON_THRESHOLD = 0.6
RISK_OFF_THRESHOLD = 0.4
WEIGHTS = {"level1": 0.25, "level2": 0.35, "level3": 0.40}
L1_ATR_PCT_MIN = 0.05
L1_ATR_PCT_MAX = 12.0
L1_REQUIRE_TF_AGREEMENT = False
MAX_DRAWDOWN_PCT = 10.0

# --- sizing (router) ---
RISK_PER_TRADE_FRAC = 0.01  # 1%
STOP_ATR_MULT = 1.5
MIN_ATR_PCT_FOR_SIZING = 0.25
DD_HAIRCUT_EXPONENT = 2.0
MAX_POSITION_USD = 10000.0
MAX_LEVERAGE = 5.0

# --- PositionManager Fast-Path ---
SL_ATR_MULT = 1.2
TP_ATR_MULT = 3.0
TRAIL_ATR_MULT = 1.5
PARTIAL_TP_ATR_MULT = 1.5
PARTIAL_TP_FRACTION = 0.50
BREAKEVEN_TRIGGER_ATR_MULT = 1.0
BREAKEVEN_BUFFER_PCT = 0.0005
MAX_POSITION_HOLD_HOURS = 24.0
DAILY_LOSS_LIMIT_PCT = 5.0
MIN_CONVICTION_TO_HOLD = 0.45
RE_EVAL_MIN_PROFIT_FRAC = 0.005
PER_ASSET_ATR_CAP = {"BTC-PERP": 3.0, "ETH-PERP": 4.0}

INITIAL_EQUITY = 10000.0


@dataclass
class SimConfig:
    """Toggle set for a backtest scenario.

    Every field defaults to a NO-OP so the template baseline is a vanilla,
    untuned run. Dial these up in your private ``config.py``.
    """

    name: str
    # post-stop-out cooldown (minutes); 0 disables
    post_stop_cooldown_minutes: float = 0.0
    # portfolio risk-engine controls
    enable_loss_warmup: bool = False
    enable_correlation_cap: bool = False
    enable_ev_filter: bool = False
    min_equity_to_trade_usd: float = 0.0
    # entry-quality gate + market detectors (floors at 0.0 == off)
    entry_min_direction_strength: float = 0.0
    entry_min_level_agreement: float = 0.0
    entry_block_below_agreement: float = 0.0
    entry_dissent_intensity_mult: float = 1.0
    flat_market_detect: bool = False
    bull_bias_offset: float = 0.0


def cfg_baseline() -> SimConfig:
    """TEMPLATE baseline = vanilla engine, no tuning.

    Returns the raw cascade with every control at its no-op default. Replace
    the body in your private ``config.py`` with your tuned operating point.
    """
    return SimConfig(name="BASELINE (template / untuned)")


__all__ = ["SimConfig", "cfg_baseline"]
