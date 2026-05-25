"""Hyperliquid Intelligence Adapter — real perp metrics from Info API.

This module replaces the spot-DEX-derived proxies that Level 2 used
to pull from Dune (`dex.trades`) with **real Hyperliquid perp data**
read directly from the Hyperliquid Info API.

Why this matters
----------------
Until Day 5, Level 2's funding / open-interest / volume metrics were
labelled-but-honest proxies computed from spot DEX trades:

    funding   ≈ buy-vs-sell USD imbalance over 8h
    OI        ≈ rolling USD volume + deltas
    volume    ≈ rolling spot volume

On quiet hours these proxies could legitimately show wild swings
(e.g. ``OI delta_24h = -99%``) just because the rolling-volume
denominator was small. That's a *correct* number for what was being
measured, but it's NOT real perp open interest and was misleading
the L3 arbiter.

This adapter pulls the **actual** perp signals from Hyperliquid:

* ``meta_and_asset_ctxs`` -> live snapshot per coin:
  ``funding`` (hourly rate), ``openInterest`` (notional), ``markPx``,
  ``midPx``, ``oraclePx``, ``premium``, ``dayNtlVlm``, ``prevDayPx``.
* ``funding_history`` -> hourly funding samples for the rolling
  window (used to compute ``rate_8h_change`` and a notional
  ``cum_funding`` integration).

Open-interest history is **not** exposed natively by the Hyperliquid
Info API, so we maintain a small in-process ring buffer per coin and
compute ``delta_1h / 4h / 24h`` from it. During the first ~24h after
startup, the longer-horizon deltas surface as ``history_unavailable``
so the operator (and L3) clearly see that those values are warming up
rather than misreading them as zero.

Execution-vs-data plane separation
----------------------------------
This adapter always reads from the **market-data** Hyperliquid URL
(``HYPERLIQUID_DATA_API_URL``, mainnet by default) - never the
execution URL. That is the entire point of the dual-URL split: real
market intelligence must be honest even when our execution venue is
testnet.

Failure handling
----------------
The adapter is designed to **degrade gracefully**: if Hyperliquid is
unreachable, or one symbol is missing, the affected fields are left
blank with an ``error`` note, and Level 2 transparently falls back
to the Dune proxy for those fields. The provenance map records the
exact source per metric so the operator always sees who provided
which number.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Iterable

from src.utils.logging import logger


# ---------------------------------------------------------------------------
# Symbol mapping
# ---------------------------------------------------------------------------
# Hyperliquid uses bare coin tickers (``BTC``, ``ETH``, ``SOL``) in its
# Info API. CapitalArc's internal naming uses the ``XXX-PERP`` form
# everywhere else (it matches the legacy Arc Perp naming and is the
# more explicit "this is a perp, not a spot pair" label). The
# translation is dead simple but lives in one place so we can extend
# it later without scattering string ops.
SYMBOL_TO_HL_COIN: dict[str, str] = {
    "BTC-PERP": "BTC",
    "ETH-PERP": "ETH",
    "SOL-PERP": "SOL",
}


def hl_coin_for_symbol(symbol: str) -> str | None:
    """Translate an internal perp symbol to its Hyperliquid coin name.

    Returns ``None`` for any symbol the adapter cannot resolve. Falls
    back to stripping a ``-PERP`` suffix if the explicit map doesn't
    have the symbol - covers new listings the operator added to
    ``HYPERLIQUID_SYMBOLS`` without updating this module.
    """
    if symbol in SYMBOL_TO_HL_COIN:
        return SYMBOL_TO_HL_COIN[symbol]
    if symbol.endswith("-PERP"):
        candidate = symbol[: -len("-PERP")]
        if candidate:
            return candidate
    return None


# ---------------------------------------------------------------------------
# Snapshot dataclasses (one per metric family, mirrors Level2's shape
# so the overlay in `level2.py` is a one-to-one field copy)
# ---------------------------------------------------------------------------


@dataclass
class HLFunding:
    """Real perp funding rates from Hyperliquid.

    Hyperliquid funding settles **hourly**; our existing Level-2
    schema uses the per-8h convention (matches Binance / Bybit /
    most CEX UIs), so we publish both. ``rate_8h_change`` and
    ``weighted_average_24h`` are computed from the funding history.
    """

    current_rate_hourly: float = 0.0
    current_rate_8h: float = 0.0
    annualised_pct: float = 0.0
    rate_8h_change: float = 0.0
    weighted_average_24h: float = 0.0
    samples: int = 0


@dataclass
class HLOpenInterest:
    """Real perp open interest from Hyperliquid.

    ``current_contracts`` is in the asset's base unit (e.g. BTC),
    ``current_notional_usd`` is the same OI multiplied by mark price.
    ``delta_*_pct`` are computed from the adapter's in-process ring
    buffer; ``history_unavailable`` flips to True for any horizon
    that doesn't have at least one sample old enough yet (the typical
    first-startup state for the 4h and 24h horizons).
    """

    current_contracts: float = 0.0
    current_notional_usd: float = 0.0
    delta_1h_pct: float = 0.0
    delta_4h_pct: float = 0.0
    delta_24h_pct: float = 0.0
    history_samples: int = 0
    history_unavailable: bool = False


@dataclass
class HLVolume:
    """Real perp trade activity + price context from Hyperliquid."""

    mark_price: float = 0.0
    mid_price: float = 0.0
    oracle_price: float = 0.0
    premium_pct: float = 0.0
    price_change_pct_24h: float = 0.0
    volume_24h_usd: float = 0.0


@dataclass
class HLCumulativeFunding:
    """Notional cumulative funding integration over the window.

    Computed as ``Σ funding_rate_hourly × current_OI_usd`` over the
    history window. This is an approximation that holds OI constant
    at its current notional (Hyperliquid Info API does not expose
    historical OI, so we cannot interpolate). It still captures the
    direction and rough magnitude of who-paid-whom over the window,
    which is what L3 uses the metric for.
    """

    longs_paid_usd: float = 0.0
    shorts_paid_usd: float = 0.0
    net_flow_usd: float = 0.0
    window_hours: float = 0.0
    samples: int = 0


@dataclass
class HLSymbolSnapshot:
    """All HL-sourced perp metrics for a single symbol.

    ``error`` is set to a one-line message when the snapshot for THIS
    symbol could not be assembled (no coin mapping, coin missing from
    the universe, etc.). The Level-2 overlay leaves the Dune proxy
    in place for any symbol that errored, so per-symbol failures
    never spread across the L2 panel.
    """

    symbol: str
    coin: str
    funding: HLFunding = field(default_factory=HLFunding)
    open_interest: HLOpenInterest = field(default_factory=HLOpenInterest)
    volume: HLVolume = field(default_factory=HLVolume)
    cum_funding: HLCumulativeFunding = field(
        default_factory=HLCumulativeFunding
    )
    error: str | None = None


@dataclass
class HLIntelligenceSnapshot:
    """Full adapter result for one ``fetch_snapshot`` call.

    ``sources`` is the per-metric provenance map that Level 2 stamps
    into ``metric_status``. Each value is a stable identifier the L2
    panel renders verbatim, e.g.
    ``"hyperliquid:metaAndAssetCtxs"``.

    ``error`` is set ONLY when the whole snapshot failed (e.g. Info
    endpoint unreachable); per-symbol errors are recorded inside the
    per-symbol entry instead so callers can degrade per symbol.
    """

    per_symbol: dict[str, HLSymbolSnapshot] = field(default_factory=dict)
    sources: dict[str, str] = field(default_factory=dict)
    api_url: str = ""
    fetched_at: float = field(default_factory=time.time)
    error: str | None = None


# ---------------------------------------------------------------------------
# Adapter config
# ---------------------------------------------------------------------------


@dataclass
class HLAdapterConfig:
    """Operator-tunable knobs for the adapter.

    Defaults are sized for the 10-minute decision-cycle cadence used
    in --loop mode (so 24 h of history is ~144 samples — well within
    ``oi_cache_max_samples``).
    """

    funding_window_hours: int = 24
    oi_cache_max_samples: int = 256
    cycle_timeout_seconds: float = 15.0


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------


# Provenance strings stamped into the snapshot.sources map. Kept as
# module-level constants so the L2 panel (and tests) reference exact
# strings instead of duplicating them. The KEYS used in
# ``snapshot.sources`` MUST match the Dune metric names used in
# :class:`Level2`'s ``fetches`` dict (``funding_rates``,
# ``open_interest``, ``volume``, ``cum_funding``) so the L2 overlay
# REPLACES the Dune provenance for those metrics instead of adding
# duplicate rows to the panel.
_SRC_FUNDING = "hyperliquid:metaAndAssetCtxs+fundingHistory"
_SRC_OI = "hyperliquid:metaAndAssetCtxs+oi_cache"
_SRC_VOLUME = "hyperliquid:metaAndAssetCtxs"
_SRC_CUM_FUNDING = "hyperliquid:fundingHistory"


class HyperliquidIntelligenceAdapter:
    """Pulls real perp metrics from Hyperliquid Info API.

    Parameters
    ----------
    info_client :
        An SDK ``Info`` instance pointed at the **market-data** URL
        (``HYPERLIQUID_DATA_API_URL`` — mainnet by default). Pass
        ``None`` to disable the adapter entirely; ``fetch_snapshot``
        will return a snapshot with ``error`` set and Level 2 will
        keep using the Dune proxies. This is the rollback path.
    api_url :
        Informational only — stamped into the returned snapshot for
        log / panel rendering. Does NOT control where ``info_client``
        actually points.
    config :
        Optional :class:`HLAdapterConfig` to tune window / cache /
        timeout. Sane defaults if omitted.

    Threading / async
    -----------------
    The Hyperliquid SDK's ``Info`` client is synchronous (uses
    ``requests``). Every call is wrapped in :func:`asyncio.to_thread`
    so the executor thread does the blocking I/O while the asyncio
    loop stays responsive.
    """

    def __init__(
        self,
        info_client: Any | None,
        *,
        api_url: str = "https://api.hyperliquid.xyz",
        config: HLAdapterConfig | None = None,
    ) -> None:
        self.info = info_client
        self.api_url = api_url
        self.config = config or HLAdapterConfig()
        # In-process OI ring buffer per coin. Hyperliquid does NOT
        # expose historical OI on the public Info endpoint, so we
        # roll our own time series by sampling current OI on every
        # ``fetch_snapshot`` and dropping anything older than the
        # funding window. The buffer is bounded by
        # ``oi_cache_max_samples`` as a defence against unbounded
        # growth on very fast loops.
        self._oi_history: dict[str, deque[tuple[float, float]]] = {}

    @property
    def enabled(self) -> bool:
        """True when the adapter has a real ``info_client`` to read."""
        return self.info is not None

    # ------------------------------------------------------------------
    # Public entrypoint
    # ------------------------------------------------------------------

    async def fetch_snapshot(
        self, symbols: Iterable[str]
    ) -> HLIntelligenceSnapshot:
        """Pull a full HL snapshot for the requested symbols.

        Always returns an :class:`HLIntelligenceSnapshot` — never
        raises. Errors are recorded on ``snapshot.error`` (whole-
        request failure) or per-symbol ``HLSymbolSnapshot.error`` so
        Level 2 can blend HL data with Dune fallback per metric.
        """
        snap = HLIntelligenceSnapshot(api_url=self.api_url)
        symbols = list(symbols)
        if not self.enabled:
            snap.error = "info_client not configured"
            for sym in symbols:
                snap.per_symbol[sym] = HLSymbolSnapshot(
                    symbol=sym,
                    coin=hl_coin_for_symbol(sym) or "?",
                    error="info_client not configured",
                )
            return snap

        # Step 1 - one call returns the entire universe + current ctx
        # for every listed coin. Cheap (single HTTP round-trip).
        try:
            meta_ctxs = await asyncio.wait_for(
                asyncio.to_thread(self.info.meta_and_asset_ctxs),
                timeout=self.config.cycle_timeout_seconds,
            )
        except Exception as exc:  # noqa: BLE001 - any network/SDK failure
            logger.warning(
                "HL meta_and_asset_ctxs failed: {}", exc
            )
            snap.error = f"meta_and_asset_ctxs failed: {exc}"
            for sym in symbols:
                snap.per_symbol[sym] = HLSymbolSnapshot(
                    symbol=sym,
                    coin=hl_coin_for_symbol(sym) or "?",
                    error=str(exc),
                )
            return snap

        meta, ctxs = self._unpack_meta_ctxs(meta_ctxs)
        coin_to_ctx = self._build_coin_ctx_map(meta, ctxs)

        # Step 2 - parallel funding history fetch (one call per coin).
        coin_for = {s: hl_coin_for_symbol(s) for s in symbols}
        coins = sorted({c for c in coin_for.values() if c})
        funding_hist = await self._fetch_funding_history_batch(coins)

        # Step 3 - per-symbol assembly. Each symbol failure is
        # isolated so a single missing coin never destroys the whole
        # snapshot.
        now_ts = time.time()
        for symbol in symbols:
            coin = coin_for[symbol]
            if coin is None:
                snap.per_symbol[symbol] = HLSymbolSnapshot(
                    symbol=symbol,
                    coin="?",
                    error=f"no Hyperliquid coin mapping for {symbol}",
                )
                continue
            ctx = coin_to_ctx.get(coin)
            if ctx is None:
                snap.per_symbol[symbol] = HLSymbolSnapshot(
                    symbol=symbol,
                    coin=coin,
                    error=f"coin {coin!r} not in HL universe",
                )
                continue
            hist = funding_hist.get(coin, [])
            try:
                snap.per_symbol[symbol] = self._build_symbol_snapshot(
                    symbol=symbol,
                    coin=coin,
                    ctx=ctx,
                    funding_history=hist,
                    now_ts=now_ts,
                )
            except Exception as exc:  # noqa: BLE001 - parsing guard
                logger.warning(
                    "HL snapshot assembly failed for {} ({}): {}",
                    symbol, coin, exc,
                )
                snap.per_symbol[symbol] = HLSymbolSnapshot(
                    symbol=symbol, coin=coin, error=str(exc)
                )

        snap.sources = {
            # Keys must match the Level 2 fetches dict keys above
            # so the overlay REPLACES (not duplicates) the Dune
            # provenance row.
            "funding_rates": _SRC_FUNDING,
            "open_interest": _SRC_OI,
            "volume": _SRC_VOLUME,
            "cum_funding": _SRC_CUM_FUNDING,
        }
        logger.info(
            "HL intelligence fetched | api={} symbols={} ok={} err={}",
            self.api_url,
            len(symbols),
            sum(1 for s in snap.per_symbol.values() if s.error is None),
            sum(1 for s in snap.per_symbol.values() if s.error is not None),
        )
        return snap

    # ------------------------------------------------------------------
    # Internal helpers (each one is independently unit-testable)
    # ------------------------------------------------------------------

    @staticmethod
    def _unpack_meta_ctxs(
        payload: Any,
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """Pull ``(meta, asset_ctxs)`` out of the SDK return value.

        The SDK returns ``[meta, asset_ctxs]`` — a 2-element list.
        Defensive about future shape changes (some forks wrap it).
        """
        if isinstance(payload, (list, tuple)) and len(payload) >= 2:
            meta = payload[0] if isinstance(payload[0], dict) else {}
            ctxs = list(payload[1]) if payload[1] else []
            return meta, ctxs
        if isinstance(payload, dict):
            return (
                payload.get("meta", {}) or {},
                list(payload.get("ctx", []) or []),
            )
        return {}, []

    @staticmethod
    def _build_coin_ctx_map(
        meta: dict[str, Any], ctxs: list[dict[str, Any]]
    ) -> dict[str, dict[str, Any]]:
        """Zip ``meta.universe`` with ``ctxs`` into ``{coin: ctx}``.

        Hyperliquid guarantees the two lists are index-aligned. We
        defensively skip any out-of-bounds index.
        """
        universe = meta.get("universe") or []
        mapping: dict[str, dict[str, Any]] = {}
        for i, asset in enumerate(universe):
            if not isinstance(asset, dict):
                continue
            name = asset.get("name")
            if not isinstance(name, str):
                continue
            if i >= len(ctxs):
                break
            ctx = ctxs[i]
            if isinstance(ctx, dict):
                mapping[name] = ctx
        return mapping

    async def _fetch_funding_history_batch(
        self, coins: list[str]
    ) -> dict[str, list[dict[str, Any]]]:
        """Fan out ``funding_history`` calls in parallel, one per coin.

        Each per-coin failure is isolated and logged — the batch
        returns a dict and missing entries surface as empty lists.
        """
        end_ms = int(time.time() * 1000)
        start_ms = end_ms - self.config.funding_window_hours * 3600 * 1000

        async def _one(coin: str) -> tuple[str, list[dict[str, Any]]]:
            try:
                hist = await asyncio.wait_for(
                    asyncio.to_thread(
                        self.info.funding_history, coin, start_ms, end_ms
                    ),
                    timeout=self.config.cycle_timeout_seconds,
                )
                return coin, list(hist or [])
            except Exception as exc:  # noqa: BLE001 - per-coin guard
                logger.warning(
                    "HL funding_history({}) failed: {}", coin, exc
                )
                return coin, []

        if not coins:
            return {}
        results = await asyncio.gather(*(_one(c) for c in coins))
        return dict(results)

    def _build_symbol_snapshot(
        self,
        *,
        symbol: str,
        coin: str,
        ctx: dict[str, Any],
        funding_history: list[dict[str, Any]],
        now_ts: float,
    ) -> HLSymbolSnapshot:
        """Compose all four sub-snapshots for a single symbol."""
        oi_notional = _coerce_float(ctx.get("openInterest")) or 0.0
        mark_px = _coerce_float(ctx.get("markPx")) or 0.0
        # Hyperliquid reports openInterest in BASE asset units. We
        # convert to USD using mark price (the most stable of the
        # three prices in the ctx).
        oi_usd = oi_notional * mark_px

        # Sample current OI into the ring buffer BEFORE computing
        # deltas, so the current value is available to compute future
        # deltas (the "now" sample doesn't help this cycle but will
        # help the next one).
        self._record_oi_sample(coin, now_ts, oi_usd)

        funding = self._build_funding(ctx, funding_history)
        open_interest = self._build_open_interest(coin, oi_notional, oi_usd, now_ts)
        volume = self._build_volume(ctx)
        cum_funding = self._build_cum_funding(funding_history, oi_usd)
        return HLSymbolSnapshot(
            symbol=symbol,
            coin=coin,
            funding=funding,
            open_interest=open_interest,
            volume=volume,
            cum_funding=cum_funding,
        )

    # ---- funding ----

    @staticmethod
    def _build_funding(
        ctx: dict[str, Any], hist: list[dict[str, Any]]
    ) -> HLFunding:
        """Compose the funding snapshot from current ctx + history."""
        rate_hourly = _coerce_float(ctx.get("funding")) or 0.0
        rate_8h = rate_hourly * 8.0
        annualised_pct = rate_hourly * 24.0 * 365.0 * 100.0
        # ``rate_8h_change`` is current_8h - 8h_ago_8h. When the
        # funding history is empty (e.g. per-coin call failed) we
        # have no prior data point, so we report ``0.0`` rather than
        # synthesising a fake change. Downstream consumers can
        # distinguish "no change" from "no data" via ``samples``.
        if hist:
            prior_rate_8h = _funding_rate_at(hist, hours_ago=8) * 8.0
            rate_8h_change = rate_8h - prior_rate_8h
        else:
            rate_8h_change = 0.0
        weighted = _weighted_avg_funding(hist)
        return HLFunding(
            current_rate_hourly=rate_hourly,
            current_rate_8h=rate_8h,
            annualised_pct=annualised_pct,
            rate_8h_change=rate_8h_change,
            weighted_average_24h=weighted,
            samples=len(hist),
        )

    # ---- open interest ----

    def _record_oi_sample(
        self, coin: str, ts: float, oi_usd: float
    ) -> None:
        """Append a sample to the in-process ring buffer."""
        buf = self._oi_history.setdefault(
            coin, deque(maxlen=self.config.oi_cache_max_samples)
        )
        buf.append((ts, oi_usd))
        # Trim entries older than the funding window so the cache
        # doesn't grow forever on long-running --loop sessions. The
        # maxlen is a backstop; this is the real housekeeping.
        cutoff = ts - self.config.funding_window_hours * 3600.0
        while buf and buf[0][0] < cutoff:
            buf.popleft()

    def _build_open_interest(
        self,
        coin: str,
        oi_notional: float,
        oi_usd: float,
        now_ts: float,
    ) -> HLOpenInterest:
        """Compose OI snapshot, computing deltas from the ring buffer."""
        buf = self._oi_history.get(coin) or deque()
        delta_1h, hist_unavail_1h = _oi_delta_pct(buf, now_ts, hours_ago=1)
        delta_4h, hist_unavail_4h = _oi_delta_pct(buf, now_ts, hours_ago=4)
        delta_24h, hist_unavail_24h = _oi_delta_pct(
            buf, now_ts, hours_ago=24
        )
        return HLOpenInterest(
            current_contracts=oi_notional,
            current_notional_usd=oi_usd,
            delta_1h_pct=delta_1h,
            delta_4h_pct=delta_4h,
            delta_24h_pct=delta_24h,
            history_samples=len(buf),
            # "unavailable" = at least one of the requested horizons
            # had no old-enough sample yet. The L2 overlay uses this
            # to decide whether to fall back to the Dune-derived
            # delta or leave the HL one in place (it leaves the HL
            # one — a zero with the flag set is more honest than a
            # spot proxy).
            history_unavailable=any(
                (hist_unavail_1h, hist_unavail_4h, hist_unavail_24h)
            ),
        )

    # ---- volume / prices ----

    @staticmethod
    def _build_volume(ctx: dict[str, Any]) -> HLVolume:
        mark = _coerce_float(ctx.get("markPx")) or 0.0
        mid = _coerce_float(ctx.get("midPx")) or 0.0
        oracle = _coerce_float(ctx.get("oraclePx")) or 0.0
        prev_day = _coerce_float(ctx.get("prevDayPx")) or 0.0
        day_ntl = _coerce_float(ctx.get("dayNtlVlm")) or 0.0
        premium = _coerce_float(ctx.get("premium")) or 0.0
        # Hyperliquid sometimes reports premium as a fraction
        # (0.00012 = 1.2bps), other times the SDK already converts.
        # We assume fraction (the raw API contract) and convert to
        # percent for human-friendly display.
        premium_pct = premium * 100.0
        price_change_pct_24h = 0.0
        if prev_day > 0 and mark > 0:
            price_change_pct_24h = (mark - prev_day) / prev_day * 100.0
        return HLVolume(
            mark_price=mark,
            mid_price=mid,
            oracle_price=oracle,
            premium_pct=premium_pct,
            price_change_pct_24h=price_change_pct_24h,
            volume_24h_usd=day_ntl,
        )

    # ---- cumulative funding ----

    @staticmethod
    def _build_cum_funding(
        hist: list[dict[str, Any]], current_oi_usd: float
    ) -> HLCumulativeFunding:
        """Approximate cum-funding flow over the history window.

        Net flow = Σ funding_rate_hourly × current_OI_usd  for each
        hourly sample in the window. Holds OI constant at its current
        notional because Hyperliquid Info API doesn't expose
        historical OI. Documented as an approximation.
        """
        longs_paid = 0.0
        shorts_paid = 0.0
        n = 0
        for entry in hist:
            rate = _coerce_float(entry.get("fundingRate")) or 0.0
            n += 1
            settled = rate * current_oi_usd
            if settled > 0:
                longs_paid += settled
            elif settled < 0:
                shorts_paid += -settled
        net_flow = longs_paid - shorts_paid
        return HLCumulativeFunding(
            longs_paid_usd=longs_paid,
            shorts_paid_usd=shorts_paid,
            net_flow_usd=net_flow,
            window_hours=float(n),  # one hourly sample per hour
            samples=n,
        )


# ---------------------------------------------------------------------------
# Pure helper functions (stateless, easy to unit-test)
# ---------------------------------------------------------------------------


def _coerce_float(value: Any) -> float | None:
    """Lenient float coercion — handles None / strings / ints."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _funding_rate_at(
    hist: list[dict[str, Any]], *, hours_ago: int
) -> float:
    """Return the funding rate closest to ``hours_ago`` hours back.

    ``hist`` is the Hyperliquid funding-history list (each entry has
    ``time`` in ms and ``fundingRate`` as a string). Returns 0.0 when
    the history is empty or doesn't reach back that far.
    """
    if not hist:
        return 0.0
    target_ms = int((time.time() - hours_ago * 3600) * 1000)
    best_rate = 0.0
    best_delta = float("inf")
    found = False
    for entry in hist:
        ts = _coerce_float(entry.get("time")) or 0.0
        delta = abs(ts - target_ms)
        # Only consider entries older than the target so we get a
        # before-vs-after read (not interpolation).
        if ts <= target_ms and delta < best_delta:
            best_delta = delta
            best_rate = _coerce_float(entry.get("fundingRate")) or 0.0
            found = True
    if not found:
        # Fall back to the closest sample regardless of side; better
        # than zero when the window doesn't fully span the horizon.
        for entry in hist:
            ts = _coerce_float(entry.get("time")) or 0.0
            delta = abs(ts - target_ms)
            if delta < best_delta:
                best_delta = delta
                best_rate = (
                    _coerce_float(entry.get("fundingRate")) or 0.0
                )
    return best_rate


def _weighted_avg_funding(hist: list[dict[str, Any]]) -> float:
    """|rate|-weighted average funding rate across the window.

    Same convention as Level 2's old Dune-derived metric — emphasises
    decisive funding regimes over noisy near-zero hours. Returns the
    raw hourly rate (not annualised); callers convert if needed.
    """
    if not hist:
        return 0.0
    numer = 0.0
    denom = 0.0
    for entry in hist:
        rate = _coerce_float(entry.get("fundingRate")) or 0.0
        weight = abs(rate)
        numer += weight * rate
        denom += weight
    return numer / denom if denom > 0 else 0.0


def _oi_delta_pct(
    buf: "deque[tuple[float, float]]",
    now_ts: float,
    *,
    hours_ago: int,
) -> tuple[float, bool]:
    """Compute ``(delta_pct, history_unavailable)`` for the OI ring buffer.

    Looks up the OI sample closest to ``(now_ts - hours_ago*3600)``,
    requiring the matched sample to be at least ``hours_ago`` seconds
    old (so we read a true before-vs-after, not interpolation). When
    no such sample exists yet (typical for the first few cycles
    after startup) returns ``(0.0, True)`` so the caller can render
    a "warming" badge.
    """
    if not buf:
        return 0.0, True
    target_ts = now_ts - hours_ago * 3600.0
    current = buf[-1][1] if buf else 0.0
    # Find the oldest sample at or before the target ts.
    matched: tuple[float, float] | None = None
    for ts, oi in buf:
        if ts <= target_ts:
            matched = (ts, oi)
        else:
            break
    if matched is None or matched[1] <= 0:
        return 0.0, True
    prior = matched[1]
    if current <= 0:
        return 0.0, True
    return (current - prior) / prior * 100.0, False
