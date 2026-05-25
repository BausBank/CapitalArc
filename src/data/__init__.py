"""Data adapters for CapitalArc.

This package contains the *only* market-data sources the decision
engine is allowed to consult:

- `DuneMCPClient` - **the single source of truth** for every market
  signal. Speaks the same Bearer-authenticated REST surface the Dune
  MCP server exposes to LLMs, and runs the Arc-native SQL templates
  stored in `dune/queries/*.sql`.
- `DuneMarketData` - Level-1 OHLCV adapter built on top of
  `DuneMCPClient`. Wraps the `ohlcv` saved query so Level 1 can ask
  for candles per `(symbol, interval)` without knowing about Dune at
  all.
- `ArcOnchainReader` - thin Arc Testnet reader for **account state
  only** (agent wallet balance, vault TVL, agent margin). It is
  **not** a market-data source: the Decision Engine never reads
  trading signals through it.

There is intentionally no CEX adapter (Binance, OKX, etc.). All
analysis is on-chain via Dune.
"""

from src.data.arc_onchain import ArcOnchainConfig, ArcOnchainReader
from src.data.dune_market_data import (
    DuneMarketData,
    DuneMarketDataConfig,
    KlineFetchResult,
)
from src.data.dune_mcp import (
    METRIC_NAMES,
    DuneMCPClient,
    DuneMCPClientConfig,
    DuneQueryResult,
    MetricFetch,
)

__all__ = [
    "ArcOnchainReader",
    "ArcOnchainConfig",
    "DuneMCPClient",
    "DuneMCPClientConfig",
    "DuneQueryResult",
    "MetricFetch",
    "METRIC_NAMES",
    "DuneMarketData",
    "DuneMarketDataConfig",
    "KlineFetchResult",
]
