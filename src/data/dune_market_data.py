"""OHLCV adapter for Level 1, sourced **only** from Dune MCP.

This module is the Day-3 follow-up that consolidates *every* market
signal onto Dune MCP. Previously Level 1 reconstructed candles by
scanning Arc Perp DEX trade events through Arc RPC; that path is now
replaced by a single Dune saved query whose SQL template lives at
`dune/queries/ohlcv.sql`.

Chain & data source
-------------------
The Arc Testnet isn't indexed by Dune yet, so the saved query
reconstructs OHLCV from `dex.trades` on a live, high-liquidity EVM
chain (`ethereum` by default; `base` / `arbitrum` are trivially
selectable via `DuneMarketDataConfig.chain`). The agent's two
symbols map to the canonical on-chain wraps of BTC / ETH:

    BTC-PERP -> WBTC  on ethereum / cbBTC on base
    ETH-PERP -> WETH  on every chain

The token-address mapping is supplied here at runtime so the SQL
template stays chain-agnostic.

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

    chain: str = "ethereum"
    symbols: list[str] = field(
        default_factory=lambda: ["BTC-PERP", "ETH-PERP"]
    )
    intervals: list[str] = field(default_factory=lambda: ["15m", "1h"])
    lookback_hours: int = 48
    cache_ttl_seconds: int = 1800
    # OHLCV is parameterised (chain + per-symbol token addresses are
    # supplied at runtime from `.env`), so reading the saved query's
    # `latest_results` is *wrong*: that endpoint returns whatever was
    # cached on Dune with whatever params the user happened to pick
    # last time they ran the query in the UI. We need our own params
    # to flow through, which only happens via `POST /execute`. The
    # `DuneMCPClient` already memoises the result for
    # `cache_ttl_seconds`, so one execution per cycle is the right
    # cost/freshness trade-off for Level 1.
    execute_each_cycle: bool = True
    # Per-symbol token addresses on `chain` (e.g. WBTC / WETH / SOL
    # wraps). The OHLCV SQL filters `dex.trades` to rows touching
    # these addresses. Empty values get treated as "metric n/a" so
    # the agent honestly blocks on `ohlcv_unavailable`.
    token_addresses: dict[str, str] = field(default_factory=dict)
    min_trade_usd: float = 1000.0


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

        token_map = {
            k.upper(): v for k, v in (self.config.token_addresses or {}).items()
        }
        # NOTE: `intervals` is intentionally NOT sent to Dune. The SQL
        # template hard-codes '15m' / '1h' in the trades_15m / trades_1h
        # CTEs and never references `{{intervals}}`, so passing it would
        # only make Dune complain about an unknown parameter and force a
        # redundant retry. `self.config.intervals` is consumed Python-side
        # in `_rows_to_frames` for the per-(symbol, interval) split.
        params = {
            "chain": self.config.chain,
            "lookback_hours": self.config.lookback_hours,
            "btc_token_address": token_map.get("BTC-PERP", ""),
            "eth_token_address": token_map.get("ETH-PERP", ""),
            "min_trade_usd": self.config.min_trade_usd,
        }
        # Surface every parameter we're about to send to Dune so a missing
        # token address or wrong chain is visible in the log even before
        # the response comes back. The `ohlcv` saved query refuses to
        # produce rows if e.g. `btc_token_address` is empty (no JOIN match),
        # so this log is the quickest way to spot a misconfiguration.
        query_id = (self.dune.config.query_ids or {}).get("ohlcv")
        logger.info(
            "Dune OHLCV call | query_id={} | execute={} | params={}",
            query_id,
            self.config.execute_each_cycle,
            params,
        )
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
        logger.info(
            "Dune OHLCV result | source={} query_id={} rows={} cached={} note={}",
            fetch.source,
            fetch.query_id,
            len(fetch.rows),
            fetch.cached,
            fetch.note,
        )
        if not fetch.available:
            return
        self._frames = self._rows_to_frames(fetch.rows)
        # One more line so it's obvious how many bars landed per
        # (symbol, interval) - this is what Level 1 actually consumes.
        if self._frames:
            shape = {
                f"{sym}/{tf}": int(len(df)) for (sym, tf), df in self._frames.items()
            }
            logger.info("Dune OHLCV frames | bars_per_pair={}", shape)
        else:
            logger.warning(
                "Dune OHLCV returned {} rows but no (symbol, interval) "
                "buckets parsed - check SQL output column names "
                "(symbol / interval / bucket_time / open / high / low / "
                "close / volume).",
                len(fetch.rows),
            )

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
    """Best-effort coercion to a Unix-epoch *seconds* int.

    Handles the timestamp shapes Dune returns:
    - ISO-8601 strings ("2026-05-20T12:00:00Z" / "2026-05-20T12:00:00+00:00");
    - Dune's default timestamp render ("2026-05-20 12:00:00.000 UTC")
      which is *not* valid ISO-8601 because of the " UTC" suffix;
    - epoch seconds / milliseconds as int or float;
    - python `datetime` (in case future callers hand us parsed values).
    """
    if value is None:
        return None
    if isinstance(value, (int, float)):
        ts = float(value)
        # Heuristic: > 1e12 means it's already milliseconds.
        if ts > 1e12:
            ts /= 1000.0
        return int(ts)
    if isinstance(value, str):
        normalised = value.strip()
        # Dune renders trino timestamps as "2026-05-18 17:15:00.000 UTC".
        # Drop the trailing tz tag and substitute "+00:00" so
        # `datetime.fromisoformat` (Python 3.10) accepts it.
        if normalised.upper().endswith(" UTC"):
            normalised = normalised[:-4].rstrip() + "+00:00"
        normalised = normalised.replace("Z", "+00:00")
        try:
            return int(
                datetime.fromisoformat(normalised)
                .astimezone(timezone.utc)
                .timestamp()
            )
        except ValueError:
            try:
                return int(float(normalised))
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
