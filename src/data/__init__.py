"""Data adapters for CapitalArc.

This package contains async clients that feed the decision engine:

- `BinanceClient` - public Binance perp endpoints (klines, funding,
  open interest, long/short ratio). Used as the OHLCV / funding proxy
  for the Arc Perp DEX until the venue exposes a public market API.
- `DuneMCPClient` - Dune Analytics integration. Speaks the same
  Bearer-authenticated REST surface the Dune MCP server exposes to LLMs
  (https://mcp.dune.com/sse), with a TTL cache.
- `ArcOnchainReader` - thin wrapper around `web3.py` for reading state
  from the Arc Perp DEX contracts (vault balance, position ledger).
"""

from src.data.binance_client import BinanceClient, BinanceClientConfig
from src.data.dune_mcp import DuneMCPClient, DuneMCPClientConfig
from src.data.arc_onchain import ArcOnchainReader, ArcOnchainConfig

__all__ = [
    "BinanceClient",
    "BinanceClientConfig",
    "DuneMCPClient",
    "DuneMCPClientConfig",
    "ArcOnchainReader",
    "ArcOnchainConfig",
]
