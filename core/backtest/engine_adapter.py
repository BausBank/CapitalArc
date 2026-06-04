"""Adapters wiring the REAL decision stack onto pre-loaded frames.

`BacktestMarketData` stands in for `DuneMarketData` (Level 1 reads it),
`BacktestLevel2` is the genuine production Level 2 with its Dune fetch
swapped for an injected HL funding + candle-derived feed. `build_engine` /
`build_risk_engine` assemble the same `DecisionEngine` + `RiskEngine` the
agent ships, so the backtest exercises production code, not a re-implementation.
"""

from __future__ import annotations

from decimal import Decimal

import pandas as pd

from src.core.decision_engine import DecisionEngine
from src.core.entry_quality import EntryQualityConfig, EntryQualityGate
from src.core.level1 import Level1, Level1Config
from src.core.level2 import Level2, Level2Config
from src.data.dune_market_data import KlineFetchResult
from src.data.dune_mcp import MetricFetch
from src.allocation.risk_engine import RiskEngine, RiskEngineConfig

from core.backtest import config as C
from core.backtest.config import SimConfig
from core.backtest.data import SYMBOLS


class BacktestMarketData:
    """Stand-in for `DuneMarketData` driving Level 1 off pre-loaded frames.

    `cutoff_ts` (unix seconds) is advanced by the harness each step; only
    candles whose close-time <= cutoff are visible, so there is no
    look-ahead. This is the single anti-leakage chokepoint for L1.
    """

    def __init__(self, frames: dict[tuple[str, str], pd.DataFrame]) -> None:
        self._frames = frames
        self.cutoff_ts: int = 0

    async def get_klines(
        self, symbol: str, interval: str = "15m", limit: int = 150
    ) -> KlineFetchResult:
        key = (symbol.upper(), interval)
        df = self._frames.get(key)
        if df is None or df.empty:
            return KlineFetchResult(
                symbol=symbol, interval=interval, df=pd.DataFrame(), source="n/a"
            )
        cutoff = pd.to_datetime(self.cutoff_ts, unit="s", utc=True)
        visible = df[df.index <= cutoff].tail(limit)
        return KlineFetchResult(
            symbol=symbol,
            interval=interval,
            df=visible,
            source="dune:backtest",
            rows=int(len(visible)),
        )


class BacktestLevel2(Level2):
    """Real Level 2 with `_fetch_all_metrics` fed from injected HL data.

    Everything downstream (heat heuristic, market-bias voter incl. the
    bull-bias offset, conviction calc) is the genuine production path.
    """

    def __init__(self, config: Level2Config) -> None:
        super().__init__(config=config, dune=None, hyperliquid_intel=None)
        self.feed: dict[str, dict[str, float]] = {}

    async def _dune_health(self) -> bool:
        return True

    async def _fetch_all_metrics(self, symbols: list[str]) -> dict[str, MetricFetch]:
        funding_rows = []
        volume_rows = []
        for sym in symbols:
            f = self.feed.get(sym)
            if not f:
                continue
            funding_rows.append({"symbol": sym, "current_rate": f["funding_rate"]})
            volume_rows.append(
                {
                    "symbol": sym,
                    "last_price": f["last_price"],
                    "price_change_pct_24h": f["price_change_pct_24h"],
                    "volume_24h_usd": f["volume_24h_usd"],
                    "volume_1h_usd": f["volume_1h_usd"],
                }
            )

        def mf(name: str, rows: list[dict]) -> MetricFetch:
            if rows:
                return MetricFetch(metric=name, source="dune:backtest", rows=rows)
            return MetricFetch(metric=name, source="n/a", rows=[])

        return {
            "funding_rates": mf("funding_rates", funding_rows),
            "volume": mf("volume", volume_rows),
            "open_interest": mf("open_interest", []),
            "vault_flows": mf("vault_flows", []),
            "whale_activity": mf("whale_activity", []),
            "long_short_ratio": mf("long_short_ratio", []),
            "cum_funding": mf("cum_funding", []),
            "market_sentiment": mf("market_sentiment", []),
        }


def build_engine(
    market_data: BacktestMarketData, sim: SimConfig
) -> tuple[DecisionEngine, BacktestLevel2]:
    l1 = Level1(
        config=Level1Config(
            # 1h only: the agent's primary decision timeframe, and the only
            # timeframe HL retains for a multi-month window.
            timeframes=["1h"],
            atr_pct_min=C.L1_ATR_PCT_MIN,
            atr_pct_max=C.L1_ATR_PCT_MAX,
            max_drawdown_pct=C.MAX_DRAWDOWN_PCT,
            require_tf_agreement=C.L1_REQUIRE_TF_AGREEMENT,
            flat_market_detect=sim.flat_market_detect,
        ),
        market_data=market_data,
    )
    l2 = BacktestLevel2(
        config=Level2Config(
            symbols=list(SYMBOLS),
            demo_mode=False,
            prefer_hyperliquid_for_perp_metrics=False,
            bull_bias_offset=sim.bull_bias_offset,
        )
    )
    gate = EntryQualityGate(
        config=EntryQualityConfig(
            enabled=True,
            min_direction_strength_to_open=sim.entry_min_direction_strength,
            min_level_agreement=sim.entry_min_level_agreement,
            block_below_agreement=sim.entry_block_below_agreement,
            dissent_intensity_mult=sim.entry_dissent_intensity_mult,
        )
    )
    engine = DecisionEngine(
        level1=l1,
        level2=l2,
        level3=None,  # synthetic, deterministic, free
        weights=dict(C.WEIGHTS),
        risk_on_threshold=C.RISK_ON_THRESHOLD,
        risk_off_threshold=C.RISK_OFF_THRESHOLD,
        redistribute_synthetic_l3_weight=True,
        allow_l3_to_override_l1=True,
        entry_quality_gate=gate,
    )
    return engine, l2


def build_risk_engine(sim: SimConfig, round_trip_cost_bps: float) -> RiskEngine:
    """Build the production RiskEngine; EV filter reads the SAME round-trip
    cost the simulator charges (one source of truth)."""
    return RiskEngine(
        config=RiskEngineConfig(
            min_equity_to_trade_usd=Decimal(str(sim.min_equity_to_trade_usd)),
            enable_loss_warmup=sim.enable_loss_warmup,
            warmup_minutes=60.0,
            warmup_size_mult=0.5,
            warmup_trigger_loss_pct=1.0,
            enable_correlation_cap=sim.enable_correlation_cap,
            max_correlated_exposure_pct=150.0,
            enable_ev_filter=sim.enable_ev_filter,
            round_trip_cost_bps=round_trip_cost_bps,
            ev_min_reward_to_cost=2.0,
        )
    )


__all__ = [
    "BacktestMarketData",
    "BacktestLevel2",
    "build_engine",
    "build_risk_engine",
]
