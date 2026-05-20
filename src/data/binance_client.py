"""Async Binance USDT-M Perp public-API client.

Why Binance here?
-----------------
Arc Perp DEX uses an off-chain matcher and does not yet expose a public
market-data API. For Level 1 (technicals) and Level 2 (funding / OI /
long-short ratio) we proxy via **Binance USDT-M Perp** which trades
nearly-identical underlying contracts (BTCUSDT, ETHUSDT, SOLUSDT).

This is a documented Day-3 fallback: the moment Arc publishes its own
market endpoints, we drop in a sibling client and `Level1`/`Level2`
keep working unchanged.

All endpoints used are PUBLIC (no API key needed) and rate-limited
generously. We keep one shared `httpx.AsyncClient` per process.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

import httpx
import pandas as pd

from src.utils.logging import logger


@dataclass
class BinanceClientConfig:
    """Configuration for the Binance perp client."""

    base_url: str = "https://fapi.binance.com"
    timeout_seconds: float = 15.0
    max_retries: int = 2
    retry_backoff_seconds: float = 0.6
    symbol_map: dict[str, str] = field(
        default_factory=lambda: {
            "BTC-PERP": "BTCUSDT",
            "ETH-PERP": "ETHUSDT",
            "SOL-PERP": "SOLUSDT",
        }
    )


class BinanceClient:
    """Thin async client over Binance USDT-M Perp public endpoints.

    All methods accept the canonical CapitalArc symbol (e.g. `BTC-PERP`)
    and internally map to the venue's symbol (`BTCUSDT`).
    """

    def __init__(self, config: BinanceClientConfig | None = None) -> None:
        self.config = config or BinanceClientConfig()
        self._client: httpx.AsyncClient | None = None
        self._lock = asyncio.Lock()

    async def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            async with self._lock:
                if self._client is None:
                    self._client = httpx.AsyncClient(
                        base_url=self.config.base_url,
                        timeout=self.config.timeout_seconds,
                        headers={"User-Agent": "CapitalArc/0.3 (+arc-perp-agent)"},
                    )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _request_json(
        self, method: str, path: str, **kwargs: Any
    ) -> Any:
        """`httpx` request with a small retry budget for transient errors."""
        client = await self._ensure_client()
        last_exc: Exception | None = None
        for attempt in range(self.config.max_retries + 1):
            try:
                resp = await client.request(method, path, **kwargs)
                resp.raise_for_status()
                return resp.json()
            except (httpx.TimeoutException, httpx.ConnectError) as exc:
                last_exc = exc
                if attempt >= self.config.max_retries:
                    break
                await asyncio.sleep(
                    self.config.retry_backoff_seconds * (attempt + 1)
                )
            except httpx.HTTPStatusError as exc:
                # 429 / 5xx are also transient; retry once.
                last_exc = exc
                status = exc.response.status_code
                if status not in (429, 500, 502, 503, 504) or attempt >= self.config.max_retries:
                    raise
                await asyncio.sleep(
                    self.config.retry_backoff_seconds * (attempt + 1)
                )
        assert last_exc is not None
        raise last_exc

    # ------------------------------------------------------------------
    # Symbol helpers
    # ------------------------------------------------------------------

    def to_venue_symbol(self, symbol: str) -> str:
        """Map `BTC-PERP` -> `BTCUSDT` (or pass through if already mapped)."""
        if symbol in self.config.symbol_map:
            return self.config.symbol_map[symbol]
        # Heuristic: strip "-PERP" and uppercase, then append USDT.
        if symbol.endswith("-PERP"):
            return symbol[: -len("-PERP")].upper() + "USDT"
        return symbol.upper()

    # ------------------------------------------------------------------
    # Public REST endpoints
    # ------------------------------------------------------------------

    async def get_klines(
        self,
        symbol: str,
        interval: str = "15m",
        limit: int = 150,
    ) -> pd.DataFrame:
        """Return a DataFrame indexed by close-time with OHLCV columns.

        Columns: `open`, `high`, `low`, `close`, `volume`, `quote_volume`,
        `trades`. All numeric columns are `float64`.
        """
        venue = self.to_venue_symbol(symbol)
        params = {"symbol": venue, "interval": interval, "limit": limit}
        rows: list[list[Any]] = await self._request_json(
            "GET", "/fapi/v1/klines", params=params
        )
        if not rows:
            return pd.DataFrame(
                columns=[
                    "open",
                    "high",
                    "low",
                    "close",
                    "volume",
                    "quote_volume",
                    "trades",
                ]
            )
        df = pd.DataFrame(
            rows,
            columns=[
                "open_time",
                "open",
                "high",
                "low",
                "close",
                "volume",
                "close_time",
                "quote_volume",
                "trades",
                "taker_buy_base",
                "taker_buy_quote",
                "ignore",
            ],
        )
        numeric_cols = [
            "open",
            "high",
            "low",
            "close",
            "volume",
            "quote_volume",
            "taker_buy_base",
            "taker_buy_quote",
        ]
        for col in numeric_cols:
            df[col] = pd.to_numeric(df[col], errors="coerce")
        df["trades"] = pd.to_numeric(df["trades"], errors="coerce").astype("Int64")
        df["close_time"] = pd.to_datetime(df["close_time"], unit="ms", utc=True)
        df = df.set_index("close_time")
        return df[
            ["open", "high", "low", "close", "volume", "quote_volume", "trades"]
        ]

    async def get_funding_rate_history(
        self, symbol: str, limit: int = 8
    ) -> list[dict[str, Any]]:
        """Return the last `limit` funding events (most recent last).

        Each entry: `{"fundingTime": int_ms, "fundingRate": float}`.
        Binance settles funding every 8h, so `limit=3` ~= last 24h.
        """
        venue = self.to_venue_symbol(symbol)
        rows = await self._request_json(
            "GET",
            "/fapi/v1/fundingRate",
            params={"symbol": venue, "limit": limit},
        )
        out: list[dict[str, Any]] = []
        for r in rows:
            try:
                out.append(
                    {
                        "fundingTime": int(r["fundingTime"]),
                        "fundingRate": float(r["fundingRate"]),
                    }
                )
            except (KeyError, TypeError, ValueError):
                continue
        return out

    async def get_premium_index(self, symbol: str) -> dict[str, Any]:
        """Return the current premium / mark-price snapshot."""
        venue = self.to_venue_symbol(symbol)
        data = await self._request_json(
            "GET", "/fapi/v1/premiumIndex", params={"symbol": venue}
        )
        return {
            "symbol": symbol,
            "venue_symbol": venue,
            "markPrice": float(data.get("markPrice", 0.0)),
            "indexPrice": float(data.get("indexPrice", 0.0)),
            "lastFundingRate": float(data.get("lastFundingRate", 0.0)),
            "nextFundingTime": int(data.get("nextFundingTime", 0)),
        }

    async def get_open_interest(self, symbol: str) -> Decimal:
        """Return current open interest in base units (contracts)."""
        venue = self.to_venue_symbol(symbol)
        data = await self._request_json(
            "GET", "/fapi/v1/openInterest", params={"symbol": venue}
        )
        try:
            return Decimal(str(data["openInterest"]))
        except (KeyError, ValueError):
            return Decimal("0")

    async def get_open_interest_history(
        self, symbol: str, period: str = "1h", limit: int = 30
    ) -> list[dict[str, Any]]:
        """Return open-interest history. `period` in {5m, 15m, 30m, 1h, 4h, 1d}.

        Each entry: `{"timestamp": ms, "sumOpenInterest": float,
        "sumOpenInterestValue": float}` (most recent last).
        """
        venue = self.to_venue_symbol(symbol)
        rows = await self._request_json(
            "GET",
            "/futures/data/openInterestHist",
            params={"symbol": venue, "period": period, "limit": limit},
        )
        out: list[dict[str, Any]] = []
        for r in rows:
            try:
                out.append(
                    {
                        "timestamp": int(r["timestamp"]),
                        "sumOpenInterest": float(r["sumOpenInterest"]),
                        "sumOpenInterestValue": float(r["sumOpenInterestValue"]),
                    }
                )
            except (KeyError, TypeError, ValueError):
                continue
        return out

    async def get_long_short_ratio(
        self,
        symbol: str,
        period: str = "1h",
        limit: int = 1,
        kind: str = "global",
    ) -> dict[str, Any]:
        """Return the most recent long/short ratio (account-based).

        `kind`:
            * `"global"`  - `globalLongShortAccountRatio`
            * `"top_acct"` - `topLongShortAccountRatio`
            * `"top_pos"`  - `topLongShortPositionRatio`
        """
        venue = self.to_venue_symbol(symbol)
        path = {
            "global": "/futures/data/globalLongShortAccountRatio",
            "top_acct": "/futures/data/topLongShortAccountRatio",
            "top_pos": "/futures/data/topLongShortPositionRatio",
        }.get(kind, "/futures/data/globalLongShortAccountRatio")
        rows = await self._request_json(
            "GET",
            path,
            params={"symbol": venue, "period": period, "limit": limit},
        )
        if not rows:
            return {
                "symbol": symbol,
                "longShortRatio": 1.0,
                "longAccount": 0.5,
                "shortAccount": 0.5,
                "timestamp": 0,
            }
        last = rows[-1]
        try:
            return {
                "symbol": symbol,
                "longShortRatio": float(last["longShortRatio"]),
                "longAccount": float(last.get("longAccount", 0.5)),
                "shortAccount": float(last.get("shortAccount", 0.5)),
                "timestamp": int(last.get("timestamp", 0)),
            }
        except (KeyError, ValueError):
            return {
                "symbol": symbol,
                "longShortRatio": 1.0,
                "longAccount": 0.5,
                "shortAccount": 0.5,
                "timestamp": 0,
            }

    async def get_24h_ticker(self, symbol: str) -> dict[str, Any]:
        """Return the 24h rolling stats for `symbol`."""
        venue = self.to_venue_symbol(symbol)
        data = await self._request_json(
            "GET", "/fapi/v1/ticker/24hr", params={"symbol": venue}
        )
        try:
            return {
                "symbol": symbol,
                "venue_symbol": venue,
                "priceChangePct": float(data["priceChangePercent"]),
                "lastPrice": float(data["lastPrice"]),
                "weightedAvgPrice": float(data["weightedAvgPrice"]),
                "volume": float(data["volume"]),
                "quoteVolume": float(data["quoteVolume"]),
                "count": int(data.get("count", 0)),
            }
        except (KeyError, ValueError) as exc:
            logger.warning("Bad 24h ticker payload for {}: {}", symbol, exc)
            return {"symbol": symbol, "venue_symbol": venue}

    # ------------------------------------------------------------------
    # Convenience: parallel fetch for a list of symbols
    # ------------------------------------------------------------------

    async def gather_per_symbol(
        self,
        symbols: list[str],
        coro_factory,
    ) -> dict[str, Any]:
        """Run `coro_factory(symbol)` for each symbol concurrently.

        Errors for any single symbol degrade to `None` (with a logged
        warning), so partial data never fails the whole pipeline.
        """

        async def _safe(sym: str):
            try:
                return sym, await coro_factory(sym)
            except Exception as exc:  # noqa: BLE001 - best-effort
                logger.warning("BinanceClient call failed for {}: {}", sym, exc)
                return sym, None

        out: dict[str, Any] = {}
        results = await asyncio.gather(*(_safe(s) for s in symbols))
        for sym, value in results:
            out[sym] = value
        return out
