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
directly with the same `DUNE_API_KEY` token. The interface still
mirrors the MCP tools (`execute_query`, `latest_results`, `ping`,
plus a high-level `fetch_metric`), so callers reason about it as
"Dune MCP".

Per-metric saved-query-id wiring
--------------------------------
Level 2 consumes named metrics: `funding_rates`, `open_interest`,
`volume`, `vault_flows`, `whale_activity`, `long_short_ratio`,
`cum_funding`, `market_sentiment`. Each maps to a saved Dune query
whose id is read from `Settings.dune_query_ids[metric_name]`.

Workflow:
    1. The user saves the SQL templates from `dune/queries/*.sql`
       into their Dune workspace. Each save gives a query id.
    2. The user sets `DUNE_QUERY_<METRIC>_ID` in `.env`.
    3. `fetch_metric()` executes that saved query, parses the rows
       and returns them with `MetricFetch.source = "dune:<id>"`.

If a query id is not configured, `fetch_metric()` returns a
`MetricFetch(source="n/a", ...)` so Level 2 can render the metric
as "not configured" rather than 0/false-confident. **No fallback to
non-Dune sources happens inside this client by design** - we keep
Level 2 honest about provenance.
"""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable

import httpx

from src.utils.logging import logger


# Lifecycle events fired by the client for every metric fetch. Used
# by the rich-progress reporter in `main.py` to render real-time
# per-metric progress bars without piggy-backing on stderr logs.
#   "start"      - fetch_metric() entered for this metric
#   "submitted"  - Dune accepted the /execute POST (or cache hit / latest_results landed)
#   "completed"  - rows are in hand
#   "cached"     - the metric came back from the in-process cache
#   "n/a"        - the metric has no DUNE_QUERY_*_ID configured
#   "error"      - Dune returned an error / no rows / unknown metric
MetricEvent = str
MetricEventCallback = Callable[[str, MetricEvent], None]


# Dune's REST API returns HTTP 400 with a body like
# `{"error":"unknown parameters (intervals, lookback_hours)"}` when the
# caller sends params that aren't declared on the saved query. We parse
# those names so we can transparently retry without them - that lets the
# agent stay compatible with whichever subset of parameters the user
# happened to define in their Dune workspace.
_UNKNOWN_PARAMS_RE = re.compile(r"unknown parameters?\s*\(([^)]*)\)", re.IGNORECASE)


# Canonical metric names used by Level 1 + Level 2.
METRIC_NAMES = (
    # Level 1
    "ohlcv",            # used to reconstruct candles for the TA rules
    # Level 2
    "funding_rates",
    "open_interest",
    "volume",
    "vault_flows",
    "whale_activity",
    "long_short_ratio",
    "cum_funding",
    "market_sentiment",
)


@dataclass
class DuneMCPClientConfig:
    """Configuration for the Dune MCP-compatible client."""

    api_key: str
    api_base_url: str = "https://api.dune.com/api/v1"
    mcp_url: str = "https://mcp.dune.com/sse"
    cache_ttl_seconds: int = 1800
    timeout_seconds: float = 30.0
    poll_interval_seconds: float = 1.0
    max_poll_seconds: float = 60.0
    # Free-tier Dune limits concurrent executions per API key. L2
    # fans out 8 metric calls in parallel and L1 fans out 1 OHLCV
    # call, so without throttling we hammer Dune with ~9 concurrent
    # /execute POSTs on a cold cycle and get HTTP 429s back. This
    # semaphore caps the live request count to a safe number; 3 is
    # a comfortable default for the free tier.
    max_concurrent_requests: int = 3
    # Retry budget for HTTP 429 (rate-limit) responses. Each retry
    # waits `rate_limit_backoff_seconds * (2 ** attempt)` so a
    # rate-limited burst recovers naturally instead of being mapped
    # to `error` in the L2 panel.
    rate_limit_max_retries: int = 4
    rate_limit_backoff_seconds: float = 1.5
    # Per-metric saved Dune query ids. Empty / missing -> "not configured".
    query_ids: dict[str, int] = field(default_factory=dict)


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


@dataclass
class MetricFetch:
    """Outcome of a high-level Level-2 metric fetch."""

    metric: str
    source: str                  # "dune:<query_id>" | "n/a" | "error"
    rows: list[dict[str, Any]] = field(default_factory=list)
    query_id: int | None = None
    note: str | None = None
    cached: bool = False
    fetched_at: float = field(default_factory=time.time)

    @property
    def available(self) -> bool:
        return self.source.startswith("dune:")


class DuneMCPClient:
    """Async Dune client matching the MCP tool surface."""

    def __init__(self, config: DuneMCPClientConfig) -> None:
        self.config = config
        self._client: httpx.AsyncClient | None = None
        self._lock = asyncio.Lock()
        # query_id -> (result, expiry_ts)
        self._cache: dict[int, tuple[DuneQueryResult, float]] = {}
        self._healthy: bool | None = None
        # Caps concurrent HTTP POSTs against Dune so we don't trip
        # the free-tier rate limit on cold cycles (L2 fans out 8
        # metrics + L1 1 OHLCV in parallel).
        self._semaphore = asyncio.Semaphore(
            max(1, int(config.max_concurrent_requests))
        )
        # Optional progress hook fired at every metric lifecycle event;
        # the clean-mode CLI in `main.py` plugs into this to render
        # rich-progress bars per metric without grepping stderr logs.
        self.on_metric_event: MetricEventCallback | None = None

    def _fire(self, metric: str | None, event: MetricEvent) -> None:
        if metric and self.on_metric_event is not None:
            try:
                self.on_metric_event(metric, event)
            except Exception:  # noqa: BLE001 - never let UI break a fetch
                logger.debug("metric event hook raised for {} ({})", metric, event)

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
    # MCP-style tools (low-level)
    # ------------------------------------------------------------------

    async def ping(self) -> bool:
        """Probe the Dune REST endpoint with the cheapest call available."""
        if self._healthy is True:
            return True
        if not self.config.api_key:
            self._healthy = False
            return False
        try:
            client = await self._ensure_client()
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
        metric: str | None = None,
    ) -> DuneQueryResult | None:
        """Return the latest cached execution of `query_id`."""
        if use_cache:
            hit = self._cache_get(query_id)
            if hit is not None:
                self._fire(metric, "cached")
                return hit
        client = await self._ensure_client()
        resp = await self._get_with_rate_limit(
            client, f"/query/{query_id}/results", params={"limit": limit}
        )
        if resp is None:
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
        metric: str | None = None,
    ) -> DuneQueryResult | None:
        """Execute `query_id` and (optionally) wait for results."""
        if use_cache:
            hit = self._cache_get(query_id)
            if hit is not None:
                logger.debug(
                    "Dune execute_query({}) cache hit ({} rows)",
                    query_id,
                    hit.row_count,
                )
                self._fire(metric, "cached")
                return hit

        # Dune's `query_parameters` map expects string values
        # (parameters are textually substituted into the SQL).
        # Coercing here lets callers pass native Python ints / floats
        # without surprising the API.
        query_params: dict[str, str] = {}
        if params:
            query_params = {
                str(k): _stringify_param(v) for k, v in params.items()
            }

        execution_id = await self._submit_execution(query_id, query_params)
        if execution_id is None:
            return None
        # Fire "submitted" only once we have an execution_id so a
        # rejected /execute (HTTP 400 / 401 / 5xx) doesn't bump the
        # progress bar past the running state.
        self._fire(metric, "submitted")
        if not wait:
            return DuneQueryResult(
                query_id=query_id,
                rows=[],
                fetched_at=time.time(),
            )
        return await self._poll_execution(query_id, execution_id)

    # How many "unknown-parameter" 400 responses we tolerate before
    # giving up on a single execute. Saved queries on Dune sometimes
    # surface their unknowns in multiple chunks (e.g. a query rejects
    # `whale_min_usd` first, then on retry rejects `usdc_address`),
    # so we converge in a short loop rather than after a single retry.
    _MAX_UNKNOWN_PARAM_RETRIES = 4

    async def _post_with_rate_limit(
        self,
        client: httpx.AsyncClient,
        url: str,
        *,
        json: dict[str, Any] | None = None,
    ) -> httpx.Response | None:
        """POST with concurrency cap + HTTP 429 backoff.

        Dune's free tier returns HTTP 429
        `{"error":"Too many requests. Please upgrade your..."}`
        when too many executions land in parallel. The fix is two-
        sided: (a) cap how many requests we have in flight via the
        instance semaphore, and (b) on a 429, sleep and retry with
        exponential backoff instead of letting the metric collapse
        to `error` in the L2 panel for 30 minutes.
        """
        max_retries = max(0, int(self.config.rate_limit_max_retries))
        backoff = max(0.0, float(self.config.rate_limit_backoff_seconds))
        attempt = 0
        while True:
            async with self._semaphore:
                try:
                    resp = await client.post(url, json=json or {})
                except Exception as exc:  # noqa: BLE001
                    logger.warning("Dune POST {} failed: {}", url, exc)
                    return None
            if resp.status_code != 429 or attempt >= max_retries:
                return resp
            # Honour Retry-After header when Dune provides one;
            # otherwise back off exponentially from `backoff`.
            retry_after = resp.headers.get("Retry-After")
            try:
                sleep_for = (
                    float(retry_after)
                    if retry_after is not None
                    else backoff * (2 ** attempt)
                )
            except ValueError:
                sleep_for = backoff * (2 ** attempt)
            logger.warning(
                "Dune POST {} rate-limited (429); "
                "sleeping {:.2f}s before retry {}/{}",
                url, sleep_for, attempt + 1, max_retries,
            )
            await asyncio.sleep(sleep_for)
            attempt += 1

    async def _get_with_rate_limit(
        self,
        client: httpx.AsyncClient,
        url: str,
        *,
        params: dict[str, Any] | None = None,
    ) -> httpx.Response | None:
        """Mirror of `_post_with_rate_limit` for GETs (used by polling)."""
        max_retries = max(0, int(self.config.rate_limit_max_retries))
        backoff = max(0.0, float(self.config.rate_limit_backoff_seconds))
        attempt = 0
        while True:
            async with self._semaphore:
                try:
                    resp = await client.get(url, params=params or {})
                except Exception as exc:  # noqa: BLE001
                    logger.warning("Dune GET {} failed: {}", url, exc)
                    return None
            if resp.status_code != 429 or attempt >= max_retries:
                return resp
            retry_after = resp.headers.get("Retry-After")
            try:
                sleep_for = (
                    float(retry_after)
                    if retry_after is not None
                    else backoff * (2 ** attempt)
                )
            except ValueError:
                sleep_for = backoff * (2 ** attempt)
            logger.warning(
                "Dune GET {} rate-limited (429); "
                "sleeping {:.2f}s before retry {}/{}",
                url, sleep_for, attempt + 1, max_retries,
            )
            await asyncio.sleep(sleep_for)
            attempt += 1

    async def _submit_execution(
        self, query_id: int, query_params: dict[str, str]
    ) -> str | None:
        """POST /query/{id}/execute, retrying past unknown-parameter rejects.

        Dune rejects requests that carry parameter names the saved query
        didn't declare with a HTTP 400 body shaped like
        `{"error":"unknown parameters (a, b, c)"}`. We strip the
        rejected names and retry, repeating until either:

          * Dune accepts the request (200 / 201), or
          * the unknown set stops shrinking the param map (i.e. the
            same param is rejected twice, or the error is no longer
            a recognisable unknown-parameter notice), or
          * we hit `_MAX_UNKNOWN_PARAM_RETRIES` (defence in depth).

        This is what makes the agent resilient to the common case where
        a saved query on Dune declares a *subset* of the parameters
        the local SQL template ships with - e.g. `vault_flows`,
        `cum_funding`, `market_sentiment` saved without
        `whale_min_usd` / token addresses / vault address. The retry
        used to be single-shot which left an entire 30-minute cycle
        marked as `error` whenever Dune surfaced its unknowns in two
        waves.
        """
        client = await self._ensure_client()
        body: dict[str, Any] = {}
        if query_params:
            body["query_parameters"] = dict(query_params)
        logger.info(
            "Dune POST /query/{}/execute | body={}",
            query_id,
            body if body else "{}",
        )
        url = f"/query/{query_id}/execute"
        resp = await self._post_with_rate_limit(client, url, json=body)
        if resp is None:
            return None

        attempts = 0
        while (
            resp.status_code == 400
            and query_params
            and attempts < self._MAX_UNKNOWN_PARAM_RETRIES
        ):
            unknown = _parse_unknown_params(resp.text)
            if not unknown:
                break
            remaining = {
                k: v for k, v in query_params.items() if k not in unknown
            }
            if len(remaining) == len(query_params):
                # Dune flagged names we never sent - stop, this is a
                # different 400 (e.g. parameter type / value error).
                break
            logger.warning(
                "Dune query {} rejected unknown parameters {}; "
                "retrying with {} (attempt {}/{})",
                query_id,
                sorted(unknown),
                sorted(remaining),
                attempts + 1,
                self._MAX_UNKNOWN_PARAM_RETRIES,
            )
            query_params = remaining
            retry_body: dict[str, Any] = {}
            if remaining:
                retry_body["query_parameters"] = remaining
            resp = await self._post_with_rate_limit(client, url, json=retry_body)
            if resp is None:
                return None
            attempts += 1

        if resp.status_code not in (200, 201):
            logger.warning(
                "Dune execute_query({}) HTTP {} after {} retries: {}",
                query_id,
                resp.status_code,
                attempts,
                resp.text[:200],
            )
            return None
        execution_id = resp.json().get("execution_id")
        logger.info(
            "Dune execute_query({}) accepted | execution_id={} (after {} retries)",
            query_id,
            execution_id,
            attempts,
        )
        return execution_id

    async def _poll_execution(
        self, query_id: int, execution_id: str
    ) -> DuneQueryResult | None:
        client = await self._ensure_client()
        deadline = time.time() + self.config.max_poll_seconds
        while time.time() < deadline:
            resp = await self._get_with_rate_limit(
                client, f"/execution/{execution_id}/results"
            )
            if resp is None:
                return None
            if resp.status_code == 200:
                payload = resp.json()
                state = payload.get("state")
                if state in {"QUERY_STATE_COMPLETED", "completed"}:
                    rows = self._extract_rows(payload)
                    logger.info(
                        "Dune execution {} completed | query_id={} rows={}",
                        execution_id,
                        query_id,
                        len(rows),
                    )
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
    # High-level Level-2 metrics
    # ------------------------------------------------------------------

    async def fetch_metric(
        self,
        metric: str,
        *,
        params: dict[str, Any] | None = None,
        execute: bool = False,
    ) -> MetricFetch:
        """Fetch a Level-2 metric by name.

        Parameters
        ----------
        metric :
            One of `METRIC_NAMES`.
        params :
            Optional Dune query parameters (only used when `execute`).
        execute :
            When True, calls `execute_query` (with optional parameters)
            instead of `latest_results`. Default is False because most
            of our queries are scheduled and `latest_results` is much
            cheaper.
        """
        self._fire(metric, "start")
        if metric not in METRIC_NAMES:
            self._fire(metric, "error")
            return MetricFetch(
                metric=metric,
                source="error",
                note=f"unknown metric {metric!r}",
            )
        query_id = self.config.query_ids.get(metric)
        if not query_id:
            self._fire(metric, "n/a")
            return MetricFetch(
                metric=metric,
                source="n/a",
                note=(
                    f"DUNE_QUERY_{metric.upper()}_ID is not configured; "
                    "save dune/queries/{m}.sql to your Dune workspace "
                    "and set the id in .env."
                    .format(m=metric)
                ),
            )

        if execute:
            result = await self.execute_query(
                query_id, params=params or {}, metric=metric
            )
        else:
            result = await self.latest_results(query_id, metric=metric)
        if result is None:
            self._fire(metric, "error")
            return MetricFetch(
                metric=metric,
                source="error",
                query_id=query_id,
                note=f"Dune query {query_id} returned an error or no data",
            )
        self._fire(metric, "completed")
        return MetricFetch(
            metric=metric,
            source=f"dune:{query_id}",
            rows=result.rows,
            query_id=query_id,
            cached=result.is_cached,
            fetched_at=result.fetched_at,
        )

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


def _stringify_param(value: Any) -> str:
    """Coerce a parameter value to the string form Dune expects.

    Dune's `query_parameters` are substituted into the SQL as text, so
    the REST API canonically wants string values. Booleans become
    lowercase JSON-ish so a "text" parameter doesn't accidentally land
    as Python's title-cased "True".
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return ""
    return str(value)


def _parse_unknown_params(body: str) -> set[str]:
    """Pull parameter names out of a Dune `unknown parameters (...)` error."""
    if not body:
        return set()
    match = _UNKNOWN_PARAMS_RE.search(body)
    if not match:
        return set()
    inner = match.group(1)
    return {p.strip() for p in inner.split(",") if p.strip()}


__all__ = [
    "DuneMCPClient",
    "DuneMCPClientConfig",
    "DuneQueryResult",
    "MetricFetch",
    "METRIC_NAMES",
]
