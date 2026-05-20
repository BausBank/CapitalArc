"""Arc Perp DEX market-data reader.

This is the **only** market-data source allowed for Level 1: it
reconstructs OHLCV (open / high / low / close / volume) from on-chain
trade events emitted by the Arc Perp DEX `ClearingHouse` contract,
using nothing but Arc RPC + `web3.py`.

How OHLCV is reconstructed
--------------------------
The Arc Perp DEX matches orders off-chain and batches fills to
`ClearingHouse.settleBatch(uint256, (...)[])`. Every fill is expected
to emit a "Trade"-style event with at minimum:

    event Trade(bytes32 indexed marketId,
                uint256 price,
                uint256 size,
                uint8   side,
                uint64  timestamp);

The exact signature is configurable via `ARC_PERP_TRADE_EVENT_SIG` -
the moment the Arc team publishes the canonical event the agent picks
it up by changing one line in `.env`.

For each requested `(symbol, interval)`:
1. Resolve a `bytes32` market id (`keccak256(symbol)` for now, will
   switch to `MarketRegistry.getMarket` when its ABI is published).
2. Pull all Trade logs whose `topic[1] == marketId` in the recent
   block window via `eth_getLogs`.
3. Decode `(price, size)` from each log's `data` field.
4. Bucket fills into time intervals (15m / 1h) using each log's
   block timestamp.
5. Return a `pandas.DataFrame` with OHLCV columns.

If the chain returns no fills in the lookback window (or the event
signature is wrong), the DataFrame is empty - which Level 1 treats
as `ohlcv_unavailable` and blocks the trade. That's the right answer
on a quiet testnet.
"""

from __future__ import annotations

import asyncio
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

import pandas as pd

from src.utils.logging import logger

try:
    from web3 import Web3
except ImportError:  # pragma: no cover - web3 is in requirements.txt
    Web3 = None  # type: ignore[assignment]


# How many seconds each named interval covers.
_INTERVAL_SECONDS = {
    "1m": 60,
    "3m": 180,
    "5m": 300,
    "15m": 900,
    "30m": 1800,
    "1h": 3600,
    "4h": 14400,
    "1d": 86400,
}


@dataclass
class ArcMarketDataConfig:
    """Configuration for the Arc market-data reader."""

    rpc_url: str
    clearinghouse_address: str | None = None
    market_registry_address: str | None = None
    # Event signature used to recognise perp fills.
    # The default mirrors a typical perp-DEX Trade event; override via
    # `.env` once the Arc team publishes the canonical one.
    trade_event_sig: str = "Trade(bytes32,uint256,uint256,uint8,uint64)"
    # Decimals used for `price` and `size` fields in the trade payload.
    price_decimals: int = 8
    size_decimals: int = 6
    # Maximum block lookback window per OHLCV fetch.
    max_lookback_blocks: int = 100_000
    # Cap the number of bars returned (protects against giant windows).
    max_bars: int = 500


@dataclass
class KlineFetchResult:
    """OHLCV result + provenance metadata."""

    symbol: str
    interval: str
    df: pd.DataFrame
    from_block: int | None
    to_block: int | None
    fills: int
    source: str = "arc-rpc"  # always Arc RPC by design
    notes: list[str] = field(default_factory=list)


class ArcMarketData:
    """Read-only Arc Perp DEX market-data adapter."""

    def __init__(self, config: ArcMarketDataConfig) -> None:
        self.config = config
        self._w3: Any = None
        if Web3 is not None and config.rpc_url:
            try:
                self._w3 = Web3(Web3.HTTPProvider(config.rpc_url))
            except Exception as exc:  # noqa: BLE001
                logger.warning("ArcMarketData RPC init failed: {}", exc)
                self._w3 = None

    @property
    def available(self) -> bool:
        return self._w3 is not None

    def market_id(self, symbol: str) -> str:
        """Return the bytes32 market id for `symbol`.

        Day 3 uses `keccak256(symbol)` (matches what `ArcPerpExecutor`
        does). When `MarketRegistry.getMarket` is documented, this is
        the only place we need to update.
        """
        if Web3 is None:
            raise RuntimeError("web3.py is required to compute market ids.")
        return Web3.keccak(text=symbol).hex()

    # ------------------------------------------------------------------
    # OHLCV
    # ------------------------------------------------------------------

    async def get_klines(
        self,
        symbol: str,
        interval: str = "15m",
        limit: int = 150,
    ) -> KlineFetchResult:
        """Return OHLCV for `(symbol, interval)` reconstructed from on-chain fills.

        Always returns a `KlineFetchResult`; if no fills were found the
        embedded DataFrame is empty (and Level 1 treats that as a
        block-worthy `ohlcv_unavailable`).
        """
        empty = pd.DataFrame(
            columns=["open", "high", "low", "close", "volume", "fills"]
        )
        if not self.available or not self.config.clearinghouse_address:
            return KlineFetchResult(
                symbol=symbol,
                interval=interval,
                df=empty,
                from_block=None,
                to_block=None,
                fills=0,
                notes=["Arc RPC or ClearingHouse address unavailable."],
            )
        return await asyncio.to_thread(
            self._get_klines_sync, symbol, interval, limit
        )

    def _get_klines_sync(
        self, symbol: str, interval: str, limit: int
    ) -> KlineFetchResult:
        empty = pd.DataFrame(
            columns=["open", "high", "low", "close", "volume", "fills"]
        )
        if interval not in _INTERVAL_SECONDS:
            return KlineFetchResult(
                symbol=symbol,
                interval=interval,
                df=empty,
                from_block=None,
                to_block=None,
                fills=0,
                notes=[f"Unsupported interval {interval!r}"],
            )
        seconds_per_bar = _INTERVAL_SECONDS[interval]
        target_bars = min(limit, self.config.max_bars)

        try:
            latest_block = int(self._w3.eth.block_number)
        except Exception as exc:  # noqa: BLE001
            logger.warning("ArcMarketData latest_block failed: {}", exc)
            return KlineFetchResult(
                symbol=symbol,
                interval=interval,
                df=empty,
                from_block=None,
                to_block=None,
                fills=0,
                notes=[f"latest_block failed: {exc!r}"],
            )

        # Estimate the lookback block window. Arc Testnet block time
        # is ~1s; we widen by 2x to be safe and cap at config.max_lookback_blocks.
        approx_seconds = target_bars * seconds_per_bar
        approx_blocks = int(approx_seconds * 2)
        lookback = min(approx_blocks, self.config.max_lookback_blocks)
        from_block = max(0, latest_block - lookback)

        ch = Web3.to_checksum_address(self.config.clearinghouse_address)
        topic0 = "0x" + Web3.keccak(
            text=self.config.trade_event_sig
        ).hex().removeprefix("0x")
        market_id = self.market_id(symbol)
        # `market_id` may already be "0x..."; we feed it as-is.
        try:
            logs = self._w3.eth.get_logs(
                {
                    "address": ch,
                    "fromBlock": from_block,
                    "toBlock": latest_block,
                    "topics": [topic0, market_id],
                }
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "ArcMarketData get_logs failed for {}@{}: {!r}",
                symbol, interval, exc,
            )
            return KlineFetchResult(
                symbol=symbol,
                interval=interval,
                df=empty,
                from_block=from_block,
                to_block=latest_block,
                fills=0,
                notes=[f"eth_getLogs failed: {exc!r}"],
            )

        if not logs:
            return KlineFetchResult(
                symbol=symbol,
                interval=interval,
                df=empty,
                from_block=from_block,
                to_block=latest_block,
                fills=0,
                notes=["No Trade events found in the lookback window."],
            )

        fills = self._decode_fills(logs)
        df = self._fills_to_ohlcv(fills, seconds_per_bar, target_bars)
        return KlineFetchResult(
            symbol=symbol,
            interval=interval,
            df=df,
            from_block=from_block,
            to_block=latest_block,
            fills=len(fills),
        )

    # ------------------------------------------------------------------
    # Decoding helpers
    # ------------------------------------------------------------------

    def _decode_fills(self, logs: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Decode `(price, size, side?, ts?)` from each Trade log.

        We expect the event to pack non-indexed fields in `data` in
        the order they appear in the signature. For the default
        signature `Trade(bytes32 indexed marketId, uint256 price,
        uint256 size, uint8 side, uint64 timestamp)`:

            data = price (32B) || size (32B) || side (32B padded) || ts (32B padded)

        We tolerate shorter payloads (only price+size) and degrade to
        the block timestamp when no event-level ts is present.
        """
        out: list[dict[str, Any]] = []
        price_div = Decimal(10) ** self.config.price_decimals
        size_div = Decimal(10) ** self.config.size_decimals
        for log in logs:
            data = log.get("data") or "0x"
            if isinstance(data, bytes):
                data_hex = data.hex()
            else:
                data_hex = data[2:] if data.startswith("0x") else data
            if len(data_hex) < 128:  # need at least price + size
                continue
            try:
                price_raw = int(data_hex[0:64], 16)
                size_raw = int(data_hex[64:128], 16)
            except ValueError:
                continue
            ts: int | None = None
            if len(data_hex) >= 256:
                try:
                    ts = int(data_hex[192:256], 16)
                except ValueError:
                    ts = None
            block_number = log.get("blockNumber")
            if ts in (None, 0) and block_number is not None:
                try:
                    blk = self._w3.eth.get_block(block_number)
                    ts = int(blk["timestamp"])
                except Exception:  # noqa: BLE001 - last resort
                    ts = None
            if ts is None:
                continue
            out.append(
                {
                    "ts": int(ts),
                    "price": float(Decimal(price_raw) / price_div),
                    "size": float(Decimal(size_raw) / size_div),
                }
            )
        return out

    def _fills_to_ohlcv(
        self,
        fills: list[dict[str, Any]],
        seconds_per_bar: int,
        target_bars: int,
    ) -> pd.DataFrame:
        if not fills:
            return pd.DataFrame(
                columns=["open", "high", "low", "close", "volume", "fills"]
            )
        rows: list[dict[str, Any]] = []
        for f in fills:
            bucket = (f["ts"] // seconds_per_bar) * seconds_per_bar
            rows.append(
                {
                    "bucket": bucket,
                    "price": f["price"],
                    "size": f["size"],
                    "ts": f["ts"],
                }
            )
        df = pd.DataFrame(rows).sort_values("ts")
        ohlcv = df.groupby("bucket").agg(
            open=("price", "first"),
            high=("price", "max"),
            low=("price", "min"),
            close=("price", "last"),
            volume=("size", "sum"),
            fills=("price", "count"),
        )
        ohlcv = ohlcv.tail(target_bars).copy()
        ohlcv.index = pd.to_datetime(
            ohlcv.index.astype("int64"), unit="s", utc=True
        )
        ohlcv.index.name = "close_time"
        return ohlcv


__all__ = ["ArcMarketData", "ArcMarketDataConfig", "KlineFetchResult"]
