"""Dune MCP-compatible client used by Level 2.

Background
----------
Dune exposes two surfaces that share authentication (`DUNE_API_KEY`):

1. **Dune MCP** (https://mcp.dune.com/sse) - the Model-Context-Protocol
   server that lets an LLM run / inspect Dune queries autonomously.
2. **Dune REST API** (https://api.dune.com/api/v1/...) - the underlying
   programmatic API the MCP server wraps.

For a deterministic agent loop, *we* are the planner (not the LLM), so
we know which queries to run. We therefore talk to the REST endpoint
directly with the same `DUNE_API_KEY` token. The interface still mirrors
the MCP tools (`execute_query`, `latest_results`, `list_queries`,
`ping`), so callers reason about it as "Dune MCP".

This client adds:
- A small TTL cache so demo loops don't hammer the API.
- `ping()` that reports whether the credentials are healthy.
- Graceful degradation: if the API is unreachable or the credentials
  are missing, methods return `None` and log a warning rather than
  raising - Level 2 is allowed to operate on partial data.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any

import httpx

from src.utils.logging import logger


@dataclass
class DuneMCPClientConfig:
    """Configuration for the Dune MCP-compatible client."""

    api_key: str
    api_base_url: str = "https://api.dune.com/api/v1"
    mcp_url: str = "https://mcp.dune.com/sse"
    cache_ttl_seconds: int = 300
    timeout_seconds: float = 30.0
    poll_interval_seconds: float = 1.0
    max_poll_seconds: float = 60.0


@dataclass
class DuneQueryResult:
    """Compact wrapper for a Dune query result."""

    query_id: int
    rows: list[dict[str, Any]]
    fetched_at: float
    is_cached: bool = False

    @property
    def row_count(self) -> int:
        return len(self.rows)


class DuneMCPClient:
    """Async Dune client matching the MCP tool surface."""

    def __init__(self, config: DuneMCPClientConfig) -> None:
        self.config = config
        self._client: httpx.AsyncClient | None = None
        self._lock = asyncio.Lock()
        # query_id -> (result, expiry_ts)
        self._cache: dict[int, tuple[DuneQueryResult, float]] = {}
        self._healthy: bool | None = None

    async def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            async with self._lock:
                if self._client is None:
                    self._client = httpx.AsyncClient(
                        base_url=self.config.api_base_url,
                        timeout=self.config.timeout_seconds,
                        headers={
                            "X-Dune-API-Key": self.config.api_key,
                            "User-Agent": "CapitalArc/0.3 Dune-MCP-compatible",
                        },
                    )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # ------------------------------------------------------------------
    # MCP-style tools
    # ------------------------------------------------------------------

    async def ping(self) -> bool:
        """Probe the Dune REST endpoint with the cheapest call available.

        Returns `True` if the API key is healthy. Cached after the first
        successful call so we don't burn requests on every decision loop.
        """
        if self._healthy is True:
            return True
        if not self.config.api_key:
            self._healthy = False
            return False
        try:
            client = await self._ensure_client()
            # `/v1/eolas/health` and similar admin endpoints aren't public,
            # so the lightest probe is to ask for the well-known "Hello"
            # query (id=1) latest results. It always exists and is small.
            resp = await client.get("/query/1/results", params={"limit": 1})
            ok = resp.status_code in (200, 404)
            self._healthy = ok
            if not ok:
                logger.warning(
                    "Dune MCP ping returned HTTP {}: {}",
                    resp.status_code,
                    resp.text[:200],
                )
            return ok
        except Exception as exc:  # noqa: BLE001
            logger.warning("Dune MCP ping failed: {}", exc)
            self._healthy = False
            return False

    async def latest_results(
        self,
        query_id: int,
        *,
        limit: int = 1000,
        use_cache: bool = True,
    ) -> DuneQueryResult | None:
        """Return the latest cached execution of `query_id`."""
        if use_cache:
            hit = self._cache_get(query_id)
            if hit is not None:
                return hit
        client = await self._ensure_client()
        try:
            resp = await client.get(
                f"/query/{query_id}/results", params={"limit": limit}
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Dune latest_results({}) failed: {}", query_id, exc)
            return None
        if resp.status_code != 200:
            logger.warning(
                "Dune latest_results({}) HTTP {}: {}",
                query_id,
                resp.status_code,
                resp.text[:200],
            )
            return None
        rows = self._extract_rows(resp.json())
        out = DuneQueryResult(
            query_id=query_id,
            rows=rows,
            fetched_at=time.time(),
        )
        self._cache_put(query_id, out)
        return out

    async def execute_query(
        self,
        query_id: int,
        *,
        params: dict[str, Any] | None = None,
        wait: bool = True,
        use_cache: bool = True,
    ) -> DuneQueryResult | None:
        """Execute `query_id` and (optionally) wait for results."""
        if use_cache:
            hit = self._cache_get(query_id)
            if hit is not None:
                return hit
        client = await self._ensure_client()
        body: dict[str, Any] = {}
        if params:
            body["query_parameters"] = params
        try:
            resp = await client.post(f"/query/{query_id}/execute", json=body)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Dune execute_query({}) failed: {}", query_id, exc)
            return None
        if resp.status_code not in (200, 201):
            logger.warning(
                "Dune execute_query({}) HTTP {}: {}",
                query_id,
                resp.status_code,
                resp.text[:200],
            )
            return None
        execution_id = resp.json().get("execution_id")
        if not execution_id or not wait:
            return DuneQueryResult(
                query_id=query_id,
                rows=[],
                fetched_at=time.time(),
            )
        return await self._poll_execution(query_id, execution_id)

    async def _poll_execution(
        self, query_id: int, execution_id: str
    ) -> DuneQueryResult | None:
        client = await self._ensure_client()
        deadline = time.time() + self.config.max_poll_seconds
        while time.time() < deadline:
            try:
                resp = await client.get(
                    f"/execution/{execution_id}/results"
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("Dune poll failed: {}", exc)
                return None
            if resp.status_code == 200:
                payload = resp.json()
                state = payload.get("state")
                if state in {"QUERY_STATE_COMPLETED", "completed"}:
                    rows = self._extract_rows(payload)
                    out = DuneQueryResult(
                        query_id=query_id,
                        rows=rows,
                        fetched_at=time.time(),
                    )
                    self._cache_put(query_id, out)
                    return out
                if state in {"QUERY_STATE_FAILED", "failed"}:
                    logger.warning(
                        "Dune execution {} failed: {}",
                        execution_id,
                        payload.get("error"),
                    )
                    return None
            await asyncio.sleep(self.config.poll_interval_seconds)
        logger.warning("Dune execution {} timed out", execution_id)
        return None

    # ------------------------------------------------------------------
    # Cache helpers
    # ------------------------------------------------------------------

    def _cache_get(self, query_id: int) -> DuneQueryResult | None:
        entry = self._cache.get(query_id)
        if entry is None:
            return None
        result, expiry = entry
        if time.time() > expiry:
            self._cache.pop(query_id, None)
            return None
        cached = DuneQueryResult(
            query_id=result.query_id,
            rows=result.rows,
            fetched_at=result.fetched_at,
            is_cached=True,
        )
        return cached

    def _cache_put(self, query_id: int, result: DuneQueryResult) -> None:
        expiry = time.time() + self.config.cache_ttl_seconds
        self._cache[query_id] = (result, expiry)

    def invalidate_cache(self) -> None:
        """Drop all cached Dune results (e.g. between demo runs)."""
        self._cache.clear()

    # ------------------------------------------------------------------
    # Misc helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
        if not isinstance(payload, dict):
            return []
        result = payload.get("result", {})
        rows = result.get("rows") if isinstance(result, dict) else None
        if not isinstance(rows, list):
            return []
        return [r for r in rows if isinstance(r, dict)]
