"""OHLCV adapter for Level 1, sourced **only** from Dune MCP.

This module is the Day-3 follow-up that consolidates *every* market
signal onto Dune MCP. Previously Level 1 reconstructed candles by
scanning Arc Perp DEX trade events through Arc RPC; that path is now
replaced by a single Dune saved query whose SQL template lives at
`dune/queries/ohlcv.sql`.

Architecture
------------
`DuneMarketData` wraps a `DuneMCPClient` and exposes a small surface
shaped exactly like the previous Arc-RPC reader so Level 1 doesn't
need to know where candles come from:

    fetch = await market_data.get_klines("BTC-PERP", "15m", limit=150)
    fetch.df       # pandas DataFrame indexed by close_time (UTC)
    fetch.source   # "dune:<query_id>" | "n/a" | "error"
    fetch.notes    # any Dune-side notes the panel can render

Behaviour
---------
- Per cycle the adapter executes the Dune `ohlcv` query exactly *once*
  (it asks for every `(symbol, interval)` pair in one shot), then
  fan-outs the rows to per-symbol DataFrames. This keeps Dune
  execution costs to one call per decision cycle.
- Results are memoised for `cache_ttl_seconds` so repeated demo cycles
  are cheap and idempotent (demo-mode requirement).
- When `DUNE_QUERY_OHLCV_ID` isn't configured, `get_klines` returns
  an empty `KlineFetchResult` with `source="n/a"` and a clear note so
  Level 1 honestly blocks on `ohlcv_unavailable`.

The expected Dune row schema is documented in
`dune/queries/ohlcv.sql`:

    symbol      text  (BTC-PERP / ETH-PERP / SOL-PERP)
    interval    text  (15m / 1h / ...)
    bucket_time timestamp or epoch seconds
    open, high, low, close, volume   float

All Arc-side reads (wallet, vault TVL, agent margin) keep going
through `ArcOnchainReader` against Arc RPC - that's the only RPC the
agent talks to, and only for non-market account state.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import pandas as pd

from src.data.dune_mcp import DuneMCPClient, MetricFetch
from src.utils.logging import logger


@dataclass
class DuneMarketDataConfig:
    """Configuration for the Dune-backed OHLCV adapter."""

    chain: str = "arc"
    symbols: list[str] = field(
        default_factory=lambda: ["BTC-PERP", "ETH-PERP", "SOL-PERP"]
    )
    intervals: list[str] = field(default_factory=lambda: ["15m", "1h"])
    lookback_hours: int = 48
    cache_ttl_seconds: int = 1800
    # When True, executes the query (`POST /execute`) instead of just
    # reading the cached `latest_results`. Use sparingly - executions
    # consume Dune query credits.
    execute_each_cycle: bool = False


@dataclass
class KlineFetchResult:
    """OHLCV result + provenance metadata (Dune-flavoured)."""

    symbol: str
    interval: str
    df: pd.DataFrame
    source: str = "n/a"          # "dune:<id>" | "n/a" | "error"
    query_id: int | None = None
    rows: int = 0
    cached: bool = False
    notes: list[str] = field(default_factory=list)
    # The two `block_*` fields are kept (with neutral values) so the
    # downstream Level-1 panel - which used to render Arc-RPC block
    # ranges - continues to work after the migration.
    from_block: int | None = None
    to_block: int | None = None

    # Kept for backwards compatibility with the old Arc-RPC reader.
    @property
    def fills(self) -> int:
        return self.rows


class DuneMarketData:
    """Dune-MCP-only market-data adapter for Level 1."""

    def __init__(
        self,
        dune: DuneMCPClient | None,
        config: DuneMarketDataConfig | None = None,
    ) -> None:
        self.dune = dune
        self.config = config or DuneMarketDataConfig()
        self._lock = asyncio.Lock()
        # cache: (symbol_upper, interval) -> DataFrame
        self._frames: dict[tuple[str, str], pd.DataFrame] = {}
        self._cache_expiry: float = 0.0
        self._last_fetch: MetricFetch | None = None

    @property
    def available(self) -> bool:
        return self.dune is not None

    async def get_klines(
        self,
        symbol: str,
        interval: str = "15m",
        limit: int = 150,
    ) -> KlineFetchResult:
        await self._ensure_loaded()
        key = (symbol.upper(), interval)
        df = self._frames.get(key, pd.DataFrame()).tail(limit)
        last = self._last_fetch
        if last is None:
            return KlineFetchResult(
                symbol=symbol,
                interval=interval,
                df=df,
                source="n/a",
                notes=["Dune MCP client unavailable (DUNE_API_KEY missing)."],
            )
        notes = list(filter(None, [last.note]))
        return KlineFetchResult(
            symbol=symbol,
            interval=interval,
            df=df,
            source=last.source,
            query_id=last.query_id,
            rows=int(len(df)),
            cached=last.cached,
            notes=notes,
        )

    async def _ensure_loaded(self) -> None:
        async with self._lock:
            if time.time() < self._cache_expiry and self._frames:
                return
            await self._refresh_locked()

    async def _refresh_locked(self) -> None:
        self._frames.clear()
        self._cache_expiry = time.time() + self.config.cache_ttl_seconds

        if self.dune is None:
            self._last_fetch = MetricFetch(
                metric="ohlcv",
                source="n/a",
                note="Dune MCP client unavailable (DUNE_API_KEY missing).",
            )
            return

        params = {
            "chain": self.config.chain,
            "symbols": ",".join(self.config.symbols),
            "intervals": ",".join(self.config.intervals),
            "lookback_hours": self.config.lookback_hours,
        }
        try:
            fetch = await self.dune.fetch_metric(
                "ohlcv",
                params=params,
                execute=self.config.execute_each_cycle,
            )
        except Exception as exc:  # noqa: BLE001 - degrade to empty
            logger.warning("DuneMarketData fetch failed: {!r}", exc)
            self._last_fetch = MetricFetch(
                metric="ohlcv",
                source="error",
                note=f"Dune fetch error: {exc!r}",
            )
            return

        self._last_fetch = fetch
        if not fetch.available:
            return
        self._frames = self._rows_to_frames(fetch.rows)

    @staticmethod
    def _rows_to_frames(
        rows: list[dict[str, Any]],
    ) -> dict[tuple[str, str], pd.DataFrame]:
        """Group raw Dune rows into per-(symbol, interval) DataFrames."""
        buckets: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for r in rows:
            symbol = str(r.get("symbol") or "").upper()
            interval = str(r.get("interval") or "").lower()
            if not symbol or not interval:
                continue
            ts = _coerce_timestamp(r.get("bucket_time") or r.get("ts"))
            if ts is None:
                continue
            try:
                bucket = {
                    "ts": ts,
                    "open": float(r.get("open") or 0.0),
                    "high": float(r.get("high") or 0.0),
                    "low": float(r.get("low") or 0.0),
                    "close": float(r.get("close") or 0.0),
                    "volume": float(r.get("volume") or 0.0),
                    "fills": int(r.get("fills") or r.get("trades") or 0),
                }
            except (TypeError, ValueError):
                continue
            buckets.setdefault((symbol, interval), []).append(bucket)

        frames: dict[tuple[str, str], pd.DataFrame] = {}
        for (symbol, interval), bars in buckets.items():
            df = pd.DataFrame(bars).sort_values("ts")
            df = df[~df["ts"].duplicated(keep="last")]
            df["close_time"] = pd.to_datetime(
                df["ts"].astype("int64"), unit="s", utc=True
            )
            df.set_index("close_time", inplace=True)
            df = df[["open", "high", "low", "close", "volume", "fills"]]
            frames[(symbol, interval)] = df
        return frames


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _coerce_timestamp(value: Any) -> int | None:
    """Best-effort coercion to a Unix-epoch *seconds* int."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        ts = float(value)
        # Heuristic: > 1e12 means it's already milliseconds.
        if ts > 1e12:
            ts /= 1000.0
        return int(ts)
    if isinstance(value, str):
        # Accept "2026-05-20 12:00:00" / ISO-8601 / "2026-05-20T12:00:00Z"
        try:
            return int(
                datetime.fromisoformat(value.replace("Z", "+00:00"))
                .astimezone(timezone.utc)
                .timestamp()
            )
        except ValueError:
            try:
                return int(float(value))
            except ValueError:
                return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return int(value.timestamp())
    return None


__all__ = [
    "DuneMarketData",
    "DuneMarketDataConfig",
    "KlineFetchResult",
]
