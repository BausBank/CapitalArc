"""Data adapters for CapitalArc.

This package contains the *only* market-data sources the decision
engine is allowed to consult:

- `DuneMCPClient` - **the** Level-2 source. Speaks the same
  Bearer-authenticated REST surface the Dune MCP server exposes to
  LLMs and runs the Arc-native SQL templates stored in
  `dune/queries/*.sql`.
- `ArcMarketData` - Level-1 OHLCV reader. Reconstructs candles
  directly from Arc Perp DEX `Trade`-style events via `web3.py`
  against `ARC_RPC_URL` - no off-chain CEX feeds.
- `ArcOnchainReader` - lightweight Arc Testnet reader for the
  agent's own wallet / margin / vault TVL (used to build the market
  context and the on-chain panel - **not** as a Level-2 data path).

There is intentionally no CEX adapter (Binance, OKX, etc.). All
intelligence is Arc-native.
"""

from src.data.arc_market_data import (
    ArcMarketData,
    ArcMarketDataConfig,
    KlineFetchResult,
)
from src.data.arc_onchain import ArcOnchainConfig, ArcOnchainReader
from src.data.dune_mcp import (
    METRIC_NAMES,
    DuneMCPClient,
    DuneMCPClientConfig,
    DuneQueryResult,
    MetricFetch,
)

__all__ = [
    "ArcMarketData",
    "ArcMarketDataConfig",
    "KlineFetchResult",
    "ArcOnchainReader",
    "ArcOnchainConfig",
    "DuneMCPClient",
    "DuneMCPClientConfig",
    "DuneQueryResult",
    "MetricFetch",
    "METRIC_NAMES",
]
