"""Level 2 - on-chain intelligence sourced **only** from Dune MCP.

Level 2 is the agent's main analytical pillar. By design it consults
exactly one external service - the **Dune MCP** server (via its
Bearer-authenticated REST API) - and aggregates the result into a
structured, actionable on-chain read for the two perp symbols we
trade (`BTC-PERP`, `ETH-PERP`).

Chain & data source
-------------------
The Arc Testnet isn't yet indexed by Dune, so every Level 2 metric
is computed against a live, high-liquidity EVM chain (`ethereum`
by default; `base` / `arbitrum` selectable via `Level2Config.chain`).
The SQL templates target `dex.trades` (multichain spot DEX trades)
and `erc20_<chain>.evt_Transfer`, with the BTC / ETH token
addresses passed in as Dune query parameters. The structural
signal Level 2 cares about (buy/sell imbalance, volume, whale
flows, sentiment heat) translates 1:1 from spot to perp.

What Level 2 measures
---------------------
Per symbol:

    * Funding rates       - spot-derived imbalance proxy (current +
                            8h / 24h delta, weighted average,
                            annualised %).
    * Open interest       - rolling-USD-volume proxy (total +
                            1h / 4h / 24h deltas).
    * Trading volume      - 1h / 24h spot DEX totals + spike flag.
    * Long/short ratio    - inferred from buy-vs-sell USD volume.
    * Whale activity      - large spot trades + ERC-20 transfers.
    * Cumulative funding  - scaled net aggressor flow over window.

Vault-level:

    * Vault TVL           - USDC balance of `DUNE_PERP_VAULT_ADDRESS`
    * Net deposits/withdrawals into that vault over the window.

Market-wide:

    * `market_sentiment`  - heat / regime classifier produced by a Dune
                            query that reduces the per-symbol signals
                            to a single `[0, 1]` score.

Provenance is first-class
-------------------------
Every metric reports the saved Dune query id (`dune:<id>`) that
produced it. When the corresponding `DUNE_QUERY_*_ID` is not set in
`.env`, the metric is reported as `n/a` rather than a fake zero.
This keeps demos and live runs honest about what's actually on-chain.

SQL templates for the queries live in `dune/queries/*.sql`; users
save them in their Dune workspace, then paste the resulting query
ids back into `.env`. The data path is otherwise pinned to Dune MCP.
"""

from __future__ import annotations

import asyncio
import statistics
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

from src.data.dune_mcp import DuneMCPClient, MetricFetch
from src.utils.logging import logger

if TYPE_CHECKING:
    from src.core.decision_engine import LevelScore


Regime = Literal["risk_on", "risk_off", "neutral", "transition"]
MarketBias = Literal["bullish", "bearish", "neutral"]


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass
class Level2Config:
    """Configuration for Level 2 on-chain intelligence."""

    symbols: list[str] = field(
        default_factory=lambda: ["BTC-PERP", "ETH-PERP"]
    )
    chain: str = "ethereum"              # Dune chain tag used as a query param
    lookback_hours: int = 24
    cache_ttl_seconds: int = 1800
    demo_mode: bool = True
    whale_oi_delta_pct: float = 5.0
    volume_spike_z: float = 1.5
    whale_min_usd: float = 250_000.0
    # Per-symbol token addresses on `chain`. The Level 2 queries
    # filter `dex.trades` to rows touching these addresses.
    token_addresses: dict[str, str] = field(default_factory=dict)
    # USDC token address on `chain` (used by vault_flows).
    usdc_address: str | None = None
    # Address watched by `vault_flows` for USDC deposits / withdrawals.
    vault_address: str | None = None
    # ---- Stage 7: bull-bias offset --------------------------------
    # Spot-DEX funding proxies and positive 24h drift give the bias
    # voter a structural LONG lean in trending-up regimes (funding is
    # positive most of the time, tape drifts up). This offset is
    # subtracted from the accumulated ``bull_votes`` before the
    # bull/bear margin is computed, de-biasing the voter toward
    # neutral/short. 0.0 = no-op (default); ~0.5-1.0 corrects a mild
    # persistent long lean observed in the live logs.
    bull_bias_offset: float = 0.0
    # Used by the heuristic regime classifier when the Dune-side
    # `market_sentiment` query is not configured.
    risk_on_heat: float = 0.62
    # When True AND a :class:`HyperliquidIntelligenceAdapter` is
    # wired on ``Level2(hyperliquid_intel=...)``, the per-symbol
    # funding / open_interest / volume / cum_funding values produced
    # by Dune are OVERLAID with real Hyperliquid Info API readings
    # and the corresponding ``metric_status`` entries flip from
    # ``dune:<id>`` to ``hyperliquid:<endpoint>``. Flipping this to
    # False is the operator's one-flag rollback to the pure-Dune
    # behaviour, useful for A/B comparison or while debugging the HL
    # integration. Mirrors ``HYPERLIQUID_INTELLIGENCE_ENABLED``
    # in .env.
    prefer_hyperliquid_for_perp_metrics: bool = True
    risk_off_heat: float = 0.38


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass
class MetricStatus:
    """Lightweight provenance carrier for a single metric.

    ``source`` values currently in use:

    * ``"dune:<query_id>"``                 - Dune MCP saved query.
    * ``"hyperliquid:<endpoint(s)>"``       - Real perp data from the
      Hyperliquid Info API via :class:`HyperliquidIntelligenceAdapter`
      (e.g. ``"hyperliquid:metaAndAssetCtxs"``).
    * ``"n/a"`` / ``"error"``               - missing config / failure.
    """

    name: str
    source: str          # see docstring for the enumeration
    rows: int = 0
    query_id: int | None = None
    cached: bool = False
    note: str | None = None

    @property
    def available(self) -> bool:
        # ``hyperliquid:`` is treated as "available" exactly like
        # ``dune:`` — it's a real, parameterised data source feeding
        # the L2 metric. Anything else (``n/a`` / ``error``) is
        # unavailable.
        return self.source.startswith(("dune:", "hyperliquid:"))


@dataclass
class FundingSnapshot:
    """Funding-rate snapshot for one symbol."""

    symbol: str
    current_rate: float = 0.0
    rate_8h_change: float = 0.0
    rate_24h_change: float = 0.0
    weighted_average_24h: float = 0.0
    annualised_pct: float = 0.0


@dataclass
class OpenInterestSnapshot:
    """Open-interest snapshot for one symbol."""

    symbol: str
    current_contracts: float = 0.0
    current_value_usd: float = 0.0
    delta_1h_pct: float = 0.0
    delta_4h_pct: float = 0.0
    delta_24h_pct: float = 0.0
    # ``True`` means ``delta_1h_pct`` / ``delta_24h_pct`` are real
    # measurements and may feed bias / heat calculations. ``False``
    # means the deltas are unavailable (HL ring buffer warming up,
    # spot-volume proxy denominator unreliable, etc.) and downstream
    # voters MUST skip them entirely instead of treating zero - or an
    # extreme placeholder value - as a directional signal. Defaults to
    # ``True`` so the Dune-derived path (which has no warming-up
    # concept) is unaffected.
    oi_delta_available: bool = True


@dataclass
class VolumeSnapshot:
    """Trading-volume snapshot for one symbol."""

    symbol: str
    last_price: float = 0.0
    price_change_pct_24h: float = 0.0
    volume_24h_usd: float = 0.0
    volume_1h_usd: float = 0.0
    spike_detected: bool = False
    spike_zscore: float = 0.0


@dataclass
class LongShortSnapshot:
    """Long/short ratio + inferred bias."""

    symbol: str
    long_short_ratio: float = 1.0
    long_account_pct: float = 0.5
    short_account_pct: float = 0.5
    inferred_bias: Literal["long", "short", "balanced"] = "balanced"


@dataclass
class WhaleSnapshot:
    """Whale-activity inference for one symbol."""

    symbol: str
    flagged: bool = False
    direction: Literal["accumulating", "distributing", "neutral"] = "neutral"
    notional_usd_change: float = 0.0
    n_whales: int = 0
    rationale: str = ""


@dataclass
class CumulativeFundingSnapshot:
    """Cumulative funding paid / received in the recent window."""

    symbol: str
    longs_paid_usd: float = 0.0
    shorts_paid_usd: float = 0.0
    net_flow_usd: float = 0.0
    window_hours: float = 0.0


@dataclass
class VaultFlowSnapshot:
    """Aggregate vault TVL + net flow snapshot."""

    tvl_usdc: float = 0.0
    deposits_usdc: float = 0.0
    withdrawals_usdc: float = 0.0
    deposit_events: int = 0
    withdrawal_events: int = 0
    window_hours: float = 0.0

    @property
    def net_flow_usdc(self) -> float:
        return self.deposits_usdc - self.withdrawals_usdc


@dataclass
class SymbolIntel:
    """All Level 2 metrics for a single symbol."""

    symbol: str
    funding: FundingSnapshot
    open_interest: OpenInterestSnapshot
    volume: VolumeSnapshot
    long_short: LongShortSnapshot
    whales: WhaleSnapshot
    cum_funding: CumulativeFundingSnapshot
    notes: list[str] = field(default_factory=list)


@dataclass
class Level2Intelligence:
    """Top-level Level-2 result."""

    score: float
    regime: Regime
    rationale: str
    market_heat: float
    per_symbol: dict[str, SymbolIntel]
    vault_flow: VaultFlowSnapshot
    metric_status: dict[str, MetricStatus]
    dune_healthy: bool
    heat_source: str = "heuristic"  # "dune:<query_id>" | "heuristic"
    # Directional inference derived from the per-symbol on-chain signals
    # (funding sign, OI delta, whale direction, L/S ratio, price change,
    # market heat). Distinct from `regime` which is a *conviction*
    # bucket - `market_bias` answers "which side?", `regime` answers
    # "how strongly?". The decision engine maps these onto long/short.
    market_bias: MarketBias = "neutral"
    bias_strength: float = 0.0          # 0..1, magnitude of the side vote
    bias_rationale: str = ""            # short, human-readable summary
    generated_at: float = field(default_factory=time.time)
    cached: bool = False
    notes: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Implementation
# ---------------------------------------------------------------------------


class Level2:
    """On-chain intelligence level.

    Reads come from TWO sources, blended per-metric so each number
    in the L2 panel has clearly stamped provenance:

    * **Hyperliquid Info API** (via
      :class:`HyperliquidIntelligenceAdapter`) for real perp signals:
      ``funding``, ``open_interest``, ``volume``, ``cum_funding``.
      Pinned to mainnet by default (see ``HYPERLIQUID_DATA_API_URL``)
      so the data plane stays honest even when execution is on
      testnet.
    * **Dune MCP** (saved queries against ``dex.trades``) for
      signals Hyperliquid doesn't expose: ``long_short_ratio``,
      ``whale_activity``, ``vault_flows``, ``market_sentiment``.
      Also serves as the **fallback** for the HL-sourced metrics
      whenever the adapter is disabled, errored, or warming up.
    """

    LEVEL = 2

    def __init__(
        self,
        config: Level2Config | None = None,
        dune: DuneMCPClient | None = None,
        hyperliquid_intel: Any | None = None,
    ) -> None:
        self.config = config or Level2Config()
        self.dune = dune
        # Optional :class:`HyperliquidIntelligenceAdapter`. When set
        # AND ``self.config.prefer_hyperliquid_for_perp_metrics`` is
        # True, the per-symbol funding / OI / volume / cum_funding
        # values returned by Dune are overlaid with the live HL Info
        # API readings, and the corresponding entries in
        # ``metric_status`` flip from ``dune:<id>`` to
        # ``hyperliquid:<endpoint>``. Operators can disable the
        # overlay by setting ``HYPERLIQUID_INTELLIGENCE_ENABLED=false``
        # in .env — Level 2 then degrades back to the pure-Dune path.
        self.hyperliquid_intel = hyperliquid_intel
        self._cache: tuple[Level2Intelligence, float] | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def connect(self) -> None:
        """Probe Dune MCP once so callers know whether it's healthy."""
        if self.dune is None:
            logger.warning(
                "Level2: DuneMCPClient is None - Level 2 will return all "
                "metrics as n/a (DUNE_API_KEY missing in .env)."
            )
            return
        # Per-metric saved query ids: log every loaded id (so adding a new
        # DUNE_QUERY_<METRIC>_ID to .env shows up at startup automatically)
        # and warn for the ones still missing.
        loaded = self.dune.config.query_ids
        for metric_name in (
            "funding_rates",
            "open_interest",
            "volume",
            "vault_flows",
            "whale_activity",
            "long_short_ratio",
            "cum_funding",
            "market_sentiment",
        ):
            qid = loaded.get(metric_name)
            if qid:
                logger.info("Loaded {} query ID = {}", metric_name, qid)
            else:
                logger.warning(
                    "DUNE_QUERY_{}_ID not set - {} will be n/a",
                    metric_name.upper(),
                    metric_name,
                )
        ok = await self.dune.ping()
        logger.info("Dune MCP {}", "connected" if ok else "unavailable")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def score(self, market: dict[str, Any]) -> "LevelScore":
        from src.core.decision_engine import LevelScore

        intel = await self.evaluate(market)
        # ---- L2 conviction (vs L2 heat) ---------------------------------
        # `intel.score` is the on-chain *heat* in [0, 1] - directional
        # (1.0 = max-bullish, 0.0 = max-bearish, 0.5 = neutral). That
        # number is great for the panel but a poor *conviction* metric
        # because both `0.85` (strong bull) and `0.15` (strong bear)
        # are equally decisive on-chain. We fold heat into a symmetric
        # conviction `2*|heat - 0.5|` and then take the max with the
        # on-chain `bias_strength` (which captures funding / OI /
        # whale / LSR conviction directly even when heat sits near
        # 0.5). The directional sign is carried in `direction_sign`.
        heat_conviction = float(2.0 * abs(intel.market_heat - 0.5))
        l2_conviction = float(
            min(1.0, max(heat_conviction, intel.bias_strength))
        )
        direction_sign = _bias_to_sign(intel.market_bias)
        rationale = (
            f"{intel.rationale} | conviction={l2_conviction:.2f} "
            f"(heat_conv={heat_conviction:.2f}, "
            f"bias_strength={intel.bias_strength:.2f}) "
            f"direction={intel.market_bias}({direction_sign:+d})"
        )
        return LevelScore(
            level=self.LEVEL,
            score=l2_conviction,
            rationale=rationale,
            raw={"l2": _intel_to_dict(intel)},
            direction_sign=direction_sign,
        )

    async def evaluate(self, market: dict[str, Any]) -> Level2Intelligence:
        # ---- demo cache -----------------------------------------------
        if self.config.demo_mode and self._cache is not None:
            cached, expiry = self._cache
            if time.time() < expiry:
                cached.cached = True
                return cached

        symbols = list(market.get("symbols") or self.config.symbols)
        symbols = [s for s in symbols if s] or self.config.symbols
        dune_healthy = await self._dune_health()

        # ---- fetch every metric from Dune in parallel -----------------
        fetches = await self._fetch_all_metrics(symbols)

        # ---- map raw rows -> structured per-symbol intel --------------
        per_symbol: dict[str, SymbolIntel] = {}
        for sym in symbols:
            per_symbol[sym] = self._build_symbol_intel(sym, fetches)
        vault_flow = self._build_vault_flow(fetches["vault_flows"])

        # ---- Hyperliquid overlay (Day 6+) -----------------------------
        # If a HyperliquidIntelligenceAdapter is wired AND the operator
        # hasn't disabled the overlay, replace the spot-DEX-derived
        # perp proxies (funding / OI / volume / cum_funding) with real
        # Hyperliquid Info-API data. The overlay is per-symbol and
        # per-metric: any failure (whole HL outage, single coin
        # missing) transparently leaves the Dune proxy in place for
        # the affected slot. ``hl_provenance`` carries the source
        # strings the panel will stamp into ``metric_status`` below.
        hl_provenance: dict[str, str] = {}
        if (
            self.hyperliquid_intel is not None
            and self.config.prefer_hyperliquid_for_perp_metrics
        ):
            hl_provenance = await self._apply_hyperliquid_overlay(
                symbols, per_symbol
            )

        # ---- regime / heat --------------------------------------------
        # Branch on whether Dune returned a row at all (not on whether heat
        # is null): if the query ran and gave us a row, the Dune result is
        # authoritative even when `heat` is null (empty trade window →
        # treat as neutral 0.5 rather than falling to the heuristic).
        sentinel_fetch = fetches["market_sentiment"]
        sentiment_row = self._first_row(sentinel_fetch.rows)
        if sentiment_row is not None:
            heat: float = float(
                _coerce_float(sentiment_row.get("heat")) or 0.5
            )
            heat_source = sentinel_fetch.source  # "dune:<query_id>"
            logger.info(
                "Market heat from Dune = {:.3f} (query {})",
                heat,
                sentinel_fetch.query_id,
            )
        else:
            heat = self._heuristic_heat(per_symbol)
            heat_source = "heuristic"
            logger.info(
                "Market heat from heuristic = {:.3f} ({})",
                heat,
                sentinel_fetch.note or "market_sentiment not configured",
            )
        score = float(min(1.0, max(0.0, heat)))
        regime = self._classify_regime(score)
        rationale = self._build_rationale(score, regime, heat, per_symbol)

        # ---- Directional bias (bullish / bearish / neutral) ----------
        # Side detection lives in Level 2 because every input is on-chain
        # intelligence (funding, OI, whales, L/S ratio, price change).
        # The decision engine consumes `market_bias` + `bias_strength`
        # to decide *which way* to open a position when the score is
        # decisive enough to act.
        market_bias, bias_strength, bias_rationale = self._compute_market_bias(
            per_symbol, score
        )

        # ---- metric_status (provenance map) ---------------------------
        metric_status = {
            name: _to_status(name, fetch) for name, fetch in fetches.items()
        }
        # Stamp HL provenance on top of Dune's: any metric the HL
        # overlay successfully provided should show up in the panel
        # as ``hyperliquid:...`` instead of ``dune:<id>``. We preserve
        # the original row count from the Dune fetch (it's still the
        # underlying parameterised query the operator wired) so an
        # operator who switches the overlay off later sees the same
        # provenance row count.
        for metric_name, hl_source in hl_provenance.items():
            existing = metric_status.get(metric_name)
            metric_status[metric_name] = MetricStatus(
                name=metric_name,
                source=hl_source,
                rows=existing.rows if existing else 0,
                query_id=existing.query_id if existing else None,
                cached=False,
                note="overlaid by HyperliquidIntelligenceAdapter",
            )

        notes: list[str] = []
        if not dune_healthy:
            notes.append(
                "Dune MCP unreachable; Level 2 reports last-cached or n/a values."
            )
        missing = [name for name, st in metric_status.items() if not st.available]
        if missing:
            notes.append(
                "Dune query ids missing for: "
                + ", ".join(missing)
                + " (see dune/queries/ for the SQL templates)."
            )

        intel = Level2Intelligence(
            score=score,
            regime=regime,
            rationale=rationale,
            market_heat=float(heat),
            heat_source=heat_source,
            per_symbol=per_symbol,
            vault_flow=vault_flow,
            metric_status=metric_status,
            dune_healthy=dune_healthy,
            market_bias=market_bias,
            bias_strength=bias_strength,
            bias_rationale=bias_rationale,
            notes=notes,
        )
        # Demo mode caches Level 2 results so repeated demo runs are
        # fast, but a transient Dune failure on one metric used to be
        # "frozen" into the cache for the full TTL (30min by default),
        # leaving the agent stuck rendering `error` long after Dune
        # recovered. We only cache when EVERY metric returned a real
        # Dune row (source starts with "dune:") - any error / n/a
        # forces the next cycle to re-fetch.
        if self.config.demo_mode:
            errored = [
                name for name, st in metric_status.items()
                if st.source == "error"
            ]
            if errored:
                logger.warning(
                    "Skipping L2 demo cache because {} metric(s) errored: {}",
                    len(errored), ", ".join(errored),
                )
            else:
                self._cache = (
                    intel, time.time() + self.config.cache_ttl_seconds
                )
        return intel

    # ------------------------------------------------------------------
    # Dune fetches
    # ------------------------------------------------------------------

    async def _dune_health(self) -> bool:
        if self.dune is None:
            return False
        try:
            return await self.dune.ping()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Dune ping failed: {}", exc)
            return False

    async def _fetch_all_metrics(
        self, symbols: list[str]
    ) -> dict[str, MetricFetch]:
        """Run every named metric query in parallel."""
        token_map = {
            k.upper(): v for k, v in (self.config.token_addresses or {}).items()
        }
        common_params: dict[str, Any] = {
            "chain": self.config.chain,
            "lookback_hours": self.config.lookback_hours,
            "btc_token_address": token_map.get("BTC-PERP", ""),
            "eth_token_address": token_map.get("ETH-PERP", ""),
            "whale_min_usd": self.config.whale_min_usd,
            "vault_address": self.config.vault_address or "",
            "usdc_address": self.config.usdc_address or "",
        }
        metric_names = (
            "funding_rates",
            "open_interest",
            "volume",
            "vault_flows",
            "whale_activity",
            "long_short_ratio",
            "cum_funding",
            "market_sentiment",
        )
        logger.info(
            "Dune L2 batch | chain={} lookback={}h symbols={} | params={}",
            self.config.chain,
            self.config.lookback_hours,
            symbols,
            {k: v for k, v in common_params.items() if k != "chain"},
        )

        async def _fetch(name: str) -> tuple[str, MetricFetch]:
            if self.dune is None:
                return name, MetricFetch(
                    metric=name,
                    source="n/a",
                    note="DuneMCPClient is None (DUNE_API_KEY missing)",
                )
            # `execute=True` is required for every Level-2 metric:
            # the saved Dune queries are parameterised (chain + per-
            # symbol token addresses + lookback window), so reading
            # the `latest_results` snapshot would silently ignore our
            # parameters and return whatever the user happened to run
            # last in the Dune UI. `_submit_execution` in the client
            # transparently retries without unknown params, so this is
            # robust even when the user's saved query declares a
            # subset of the template's parameters.
            return name, await self.dune.fetch_metric(
                name, params=common_params, execute=True
            )

        results = await asyncio.gather(*(_fetch(n) for n in metric_names))
        return dict(results)

    # ------------------------------------------------------------------
    # Per-symbol mapping
    # ------------------------------------------------------------------

    def _build_symbol_intel(
        self,
        symbol: str,
        fetches: dict[str, MetricFetch],
    ) -> SymbolIntel:
        funding = self._extract_funding(symbol, fetches["funding_rates"])
        oi = self._extract_open_interest(symbol, fetches["open_interest"])
        vol = self._extract_volume(symbol, fetches["volume"])
        lsr = self._extract_long_short(symbol, fetches["long_short_ratio"])
        whales = self._extract_whales(symbol, fetches["whale_activity"], oi)
        cf = self._extract_cum_funding(
            symbol, fetches["cum_funding"], oi, funding
        )
        return SymbolIntel(
            symbol=symbol,
            funding=funding,
            open_interest=oi,
            volume=vol,
            long_short=lsr,
            whales=whales,
            cum_funding=cf,
        )

    # ------------------------------------------------------------------
    # Hyperliquid intelligence overlay (Day 6+)
    # ------------------------------------------------------------------

    async def _apply_hyperliquid_overlay(
        self,
        symbols: list[str],
        per_symbol: dict[str, SymbolIntel],
    ) -> dict[str, str]:
        """Overlay real HL Info-API readings on the Dune-derived intel.

        Mutates ``per_symbol`` in-place: each symbol's ``funding``,
        ``open_interest``, ``volume`` and ``cum_funding`` are
        replaced with HL data whenever the adapter returned a clean
        (non-errored) reading for that symbol. Failed symbols and
        failed snapshots leave the Dune proxy untouched.

        Returns the per-metric provenance map ("metric_name" ->
        "hyperliquid:<endpoint>") for the L2 ``metric_status`` table.
        Returns an empty dict when the whole HL fetch failed and
        Level 2 should remain fully on the Dune path.
        """
        snap = await self.hyperliquid_intel.fetch_snapshot(symbols)
        if snap.error:
            # Whole-snapshot failure: leave Dune values in place and
            # publish nothing in the provenance map so the panel
            # keeps showing ``dune:<id>``. The operator sees the
            # warning in the logs.
            logger.warning(
                "HL overlay skipped (snapshot error): {}", snap.error
            )
            return {}

        any_symbol_overlaid = False
        for sym in symbols:
            hl_sym = snap.per_symbol.get(sym)
            if hl_sym is None or hl_sym.error is not None:
                # Per-symbol failure (no coin mapping, coin missing
                # from HL universe, parsing error). Keep the Dune
                # proxy for THIS symbol; HL data may still apply to
                # the other symbol in the batch.
                continue
            intel = per_symbol.get(sym)
            if intel is None:
                continue
            any_symbol_overlaid = True

            # Funding overlay: copy HL fields onto the FundingSnapshot
            # the L2 panel renders. We keep the same field names so
            # downstream consumers (panel, L3 briefing, market-bias
            # voter) don't care which source provided the number.
            intel.funding = FundingSnapshot(
                symbol=sym,
                current_rate=hl_sym.funding.current_rate_8h,
                rate_8h_change=hl_sym.funding.rate_8h_change,
                # 24h delta of funding rates is not a standard HL
                # metric; keep the 8h-change as a stand-in (same
                # sign / scale) so existing bias logic still works.
                rate_24h_change=hl_sym.funding.rate_8h_change,
                weighted_average_24h=(
                    hl_sym.funding.weighted_average_24h * 8.0
                ),  # weighted_average is hourly; convert to 8h scale
                annualised_pct=hl_sym.funding.annualised_pct,
            )

            # Open-interest overlay. ``delta_*_pct`` come from the
            # adapter's in-process ring buffer; ``history_unavailable``
            # = True means we don't yet have a sample old enough for
            # this horizon (typical for the first cycle after a
            # restart). We still publish HL data: zeroed deltas with
            # a real current OI is more honest than a spot-derived
            # proxy.
            if hl_sym.open_interest.history_unavailable:
                intel.notes.append(
                    f"HL OI history warming up for {sym} "
                    f"(samples={hl_sym.open_interest.history_samples})"
                )
            intel.open_interest = OpenInterestSnapshot(
                symbol=sym,
                current_contracts=hl_sym.open_interest.current_contracts,
                current_value_usd=hl_sym.open_interest.current_notional_usd,
                delta_1h_pct=hl_sym.open_interest.delta_1h_pct,
                delta_4h_pct=hl_sym.open_interest.delta_4h_pct,
                delta_24h_pct=hl_sym.open_interest.delta_24h_pct,
                oi_delta_available=not hl_sym.open_interest.history_unavailable,
            )

            # Volume overlay. We use HL's mark price as ``last_price``
            # (the field downstream renderers expect) and HL's
            # 24h-vs-prev-day price change for the ``price_change_*``
            # field. Volume-spike z-score is not a HL metric and is
            # preserved from the Dune proxy (existing intel.volume).
            old_spike = intel.volume.spike_detected
            old_z = intel.volume.spike_zscore
            old_v1h = intel.volume.volume_1h_usd
            intel.volume = VolumeSnapshot(
                symbol=sym,
                last_price=hl_sym.volume.mark_price,
                price_change_pct_24h=hl_sym.volume.price_change_pct_24h,
                volume_24h_usd=hl_sym.volume.volume_24h_usd,
                # HL Info doesn't expose 1h notional volume; preserve
                # the Dune-derived value so the spike heuristic
                # remains useful.
                volume_1h_usd=old_v1h,
                spike_detected=old_spike,
                spike_zscore=old_z,
            )

            # Cumulative funding overlay - approximate (HL doesn't
            # expose historical OI, see HLCumulativeFunding docstring).
            intel.cum_funding = CumulativeFundingSnapshot(
                symbol=sym,
                longs_paid_usd=hl_sym.cum_funding.longs_paid_usd,
                shorts_paid_usd=hl_sym.cum_funding.shorts_paid_usd,
                net_flow_usd=hl_sym.cum_funding.net_flow_usd,
                window_hours=hl_sym.cum_funding.window_hours,
            )

        if not any_symbol_overlaid:
            # The fetch succeeded but every symbol was missing /
            # un-mappable - report no provenance so the L2 panel
            # transparently keeps Dune attribution.
            logger.warning(
                "HL overlay produced no usable symbol data; staying on Dune."
            )
            return {}
        return dict(snap.sources)

    def _extract_funding(
        self, symbol: str, fetch: MetricFetch
    ) -> FundingSnapshot:
        row = self._row_for_symbol(fetch.rows, symbol)
        if row is None:
            return FundingSnapshot(symbol=symbol)
        cur = _coerce_float(row.get("current_rate")) or 0.0
        return FundingSnapshot(
            symbol=symbol,
            current_rate=cur,
            rate_8h_change=_coerce_float(row.get("rate_8h_change")) or 0.0,
            rate_24h_change=_coerce_float(row.get("rate_24h_change")) or 0.0,
            weighted_average_24h=_coerce_float(
                row.get("weighted_average_24h")
            )
            or 0.0,
            annualised_pct=_coerce_float(row.get("annualised_pct"))
            or cur * 3 * 365 * 100,
        )

    def _extract_open_interest(
        self, symbol: str, fetch: MetricFetch
    ) -> OpenInterestSnapshot:
        row = self._row_for_symbol(fetch.rows, symbol)
        if row is None:
            return OpenInterestSnapshot(symbol=symbol)
        return OpenInterestSnapshot(
            symbol=symbol,
            current_contracts=_coerce_float(row.get("current_contracts")) or 0.0,
            current_value_usd=_coerce_float(row.get("current_value_usd")) or 0.0,
            delta_1h_pct=_coerce_float(row.get("delta_1h_pct")) or 0.0,
            delta_4h_pct=_coerce_float(row.get("delta_4h_pct")) or 0.0,
            delta_24h_pct=_coerce_float(row.get("delta_24h_pct")) or 0.0,
        )

    def _extract_volume(
        self, symbol: str, fetch: MetricFetch
    ) -> VolumeSnapshot:
        row = self._row_for_symbol(fetch.rows, symbol)
        if row is None:
            return VolumeSnapshot(symbol=symbol)
        vol_24h = _coerce_float(row.get("volume_24h_usd")) or 0.0
        vol_1h = _coerce_float(row.get("volume_1h_usd")) or 0.0
        # Spike detection: if the 1h rate exceeds the 24h-implied hourly
        # mean by `volume_spike_z` standard deviations, flag it. When
        # only totals are available we approximate stdev as 0.4 * mean.
        spike, z = False, 0.0
        if vol_24h > 0 and vol_1h > 0:
            mean_hourly = vol_24h / 24.0
            stdev_proxy = mean_hourly * 0.4
            if stdev_proxy > 0:
                z = (vol_1h - mean_hourly) / stdev_proxy
                spike = abs(z) >= self.config.volume_spike_z
        return VolumeSnapshot(
            symbol=symbol,
            last_price=_coerce_float(row.get("last_price")) or 0.0,
            price_change_pct_24h=_coerce_float(row.get("price_change_pct_24h"))
            or 0.0,
            volume_24h_usd=vol_24h,
            volume_1h_usd=vol_1h,
            spike_detected=spike,
            spike_zscore=z,
        )

    def _extract_long_short(
        self, symbol: str, fetch: MetricFetch
    ) -> LongShortSnapshot:
        row = self._row_for_symbol(fetch.rows, symbol)
        if row is None:
            return LongShortSnapshot(symbol=symbol)
        ratio = _coerce_float(row.get("long_short_ratio")) or 1.0
        long_pct = _coerce_float(row.get("long_account_pct")) or 0.5
        short_pct = _coerce_float(row.get("short_account_pct")) or 0.5
        if ratio >= 1.5:
            bias: Literal["long", "short", "balanced"] = "long"
        elif ratio <= 0.66:
            bias = "short"
        else:
            bias = "balanced"
        return LongShortSnapshot(
            symbol=symbol,
            long_short_ratio=ratio,
            long_account_pct=long_pct,
            short_account_pct=short_pct,
            inferred_bias=bias,
        )

    def _extract_whales(
        self,
        symbol: str,
        fetch: MetricFetch,
        oi: OpenInterestSnapshot,
    ) -> WhaleSnapshot:
        row = self._row_for_symbol(fetch.rows, symbol)
        if row is None:
            # Fallback: derive whale flag from OI 1h delta. Skip
            # entirely when the OI delta is not a real measurement
            # (warming up / unreliable proxy) - otherwise an extreme
            # placeholder like -99% would manufacture a phantom whale
            # vote.
            if not oi.oi_delta_available:
                return WhaleSnapshot(symbol=symbol)
            thresh = self.config.whale_oi_delta_pct
            if abs(oi.delta_1h_pct) < thresh:
                return WhaleSnapshot(symbol=symbol)
            direction = (
                "accumulating" if oi.delta_1h_pct > 0 else "distributing"
            )
            notional = oi.current_value_usd * oi.delta_1h_pct / 100.0
            return WhaleSnapshot(
                symbol=symbol,
                flagged=True,
                direction=direction,
                notional_usd_change=notional,
                rationale=(
                    f"OI 1h move {oi.delta_1h_pct:+.2f}% on {symbol} - "
                    "treated as whale activity (Dune whale_activity query "
                    "not configured)."
                ),
            )
        # `flagged` may arrive as a bool, an int (0/1) or a varchar
        # ("true"/"false") depending on the Dune engine version, so
        # coerce defensively. We also use `n_whales > 0` as a backup
        # truth source when the column itself is missing.
        n_whales = int(_coerce_float(row.get("n_whales")) or 0)
        raw_flag = row.get("flagged", row.get("whale_flagged"))
        if isinstance(raw_flag, str):
            flagged = raw_flag.strip().lower() in {"true", "1", "yes"}
        elif raw_flag is None:
            flagged = n_whales > 0
        else:
            flagged = bool(raw_flag)
        return WhaleSnapshot(
            symbol=symbol,
            flagged=flagged,
            direction=str(row.get("direction") or "neutral"),  # type: ignore[arg-type]
            notional_usd_change=_coerce_float(row.get("notional_usd_change"))
            or 0.0,
            n_whales=n_whales,
            rationale=str(row.get("rationale") or ""),
        )

    def _extract_cum_funding(
        self,
        symbol: str,
        fetch: MetricFetch,
        oi: OpenInterestSnapshot,
        funding: FundingSnapshot,
    ) -> CumulativeFundingSnapshot:
        row = self._row_for_symbol(fetch.rows, symbol)
        if row is not None:
            return CumulativeFundingSnapshot(
                symbol=symbol,
                longs_paid_usd=_coerce_float(row.get("longs_paid_usd")) or 0.0,
                shorts_paid_usd=_coerce_float(row.get("shorts_paid_usd")) or 0.0,
                net_flow_usd=_coerce_float(row.get("net_flow_usd")) or 0.0,
                window_hours=_coerce_float(row.get("window_hours"))
                or float(self.config.lookback_hours),
            )
        # Fallback: rough estimate from funding rate * OI value * window.
        if oi.current_value_usd <= 0 or funding.current_rate == 0:
            return CumulativeFundingSnapshot(
                symbol=symbol, window_hours=float(self.config.lookback_hours)
            )
        events = max(1, self.config.lookback_hours // 8)
        notional = oi.current_value_usd
        total = notional * funding.current_rate * events
        longs_paid = total if total >= 0 else 0.0
        shorts_paid = -total if total < 0 else 0.0
        return CumulativeFundingSnapshot(
            symbol=symbol,
            longs_paid_usd=longs_paid,
            shorts_paid_usd=shorts_paid,
            net_flow_usd=longs_paid - shorts_paid,
            window_hours=float(self.config.lookback_hours),
        )

    def _build_vault_flow(self, fetch: MetricFetch) -> VaultFlowSnapshot:
        row = self._first_row(fetch.rows)
        if row is None:
            return VaultFlowSnapshot(
                window_hours=float(self.config.lookback_hours)
            )
        return VaultFlowSnapshot(
            tvl_usdc=_coerce_float(row.get("tvl_usdc")) or 0.0,
            deposits_usdc=_coerce_float(row.get("deposits_usdc")) or 0.0,
            withdrawals_usdc=_coerce_float(row.get("withdrawals_usdc")) or 0.0,
            deposit_events=int(row.get("deposit_events") or 0),
            withdrawal_events=int(row.get("withdrawal_events") or 0),
            window_hours=_coerce_float(row.get("window_hours"))
            or float(self.config.lookback_hours),
        )

    # ------------------------------------------------------------------
    # Regime / heat
    # ------------------------------------------------------------------

    def _heuristic_heat(
        self, per_symbol: dict[str, SymbolIntel]
    ) -> float:
        """Fallback heat formula when `market_sentiment` isn't configured."""
        if not per_symbol:
            return 0.5
        scores: list[float] = []
        for intel in per_symbol.values():
            base = 0.5
            base += max(-0.10, min(0.10, intel.funding.current_rate * 20))
            if intel.open_interest.oi_delta_available:
                base += max(
                    -0.10,
                    min(0.10, intel.open_interest.delta_1h_pct / 100.0 * 2),
                )
            base += max(
                -0.15,
                min(0.15, intel.volume.price_change_pct_24h / 100.0),
            )
            lsr = intel.long_short.long_short_ratio
            if lsr > 1.0:
                base += min(0.05, (lsr - 1.0) * 0.05)
            else:
                base -= min(0.05, (1.0 - lsr) * 0.05)
            scores.append(max(0.0, min(1.0, base)))
        return float(statistics.fmean(scores))

    def _classify_regime(self, score: float) -> Regime:
        if score >= self.config.risk_on_heat:
            return "risk_on"
        if score <= self.config.risk_off_heat:
            return "risk_off"
        if abs(score - 0.5) <= 0.05:
            return "neutral"
        return "transition"

    def _compute_market_bias(
        self,
        per_symbol: dict[str, SymbolIntel],
        market_heat: float,
    ) -> tuple[MarketBias, float, str]:
        """Infer directional bias from the per-symbol on-chain signals.

        Returns
        -------
        (bias, strength, rationale)
            * bias - "bullish" | "bearish" | "neutral"
            * strength - in [0, 1]; how decisively one side outweighs
              the other (0 = perfectly balanced, 1 = unanimous one-side).
            * rationale - short human-readable summary listing the
              dominant signals on each side.

        Signal weights (each contributes up to 1.0 of "vote"):
          * funding rate sign + magnitude   - longs vs shorts paying
          * OI 1h / 24h delta sign          - capital flowing in/out
          * 24h price change                - directional tape
          * L/S ratio vs 1.0                - aggregator positioning
          * whale direction                 - large-wallet flow
          * market_heat extremes (>=0.6 / <=0.4) - tie-breaker

        Bias verdict:
          * bull_pct  = bull_votes / (bull_votes + bear_votes)
          * >= 0.65   -> bullish (longs are clearly dominant)
          * <= 0.35   -> bearish (shorts are clearly dominant)
          * else      -> neutral

        Strength is the *margin* between the two sides scaled to 0..1
        so a 9:1 vote yields ~0.8 and a 5:5 vote yields 0.0. This is
        what the decision engine compares against
        `MARKET_BIAS_MIN_STRENGTH` to decide whether to open a short.
        """
        if not per_symbol:
            return "neutral", 0.0, "no per-symbol data"

        bull_votes: float = 0.0
        bear_votes: float = 0.0
        bull_hits: list[str] = []
        bear_hits: list[str] = []

        for sym, intel in per_symbol.items():
            funding = float(intel.funding.current_rate)
            oi_1h = float(intel.open_interest.delta_1h_pct)
            oi_24h = float(intel.open_interest.delta_24h_pct)
            price_change = float(intel.volume.price_change_pct_24h)
            ls_ratio = float(intel.long_short.long_short_ratio)
            whale_dir = intel.whales.direction

            # --- Funding: positive = longs pay shorts = bullish positioning;
            # negative = shorts pay longs = bearish positioning. Scaled
            # at 5000x so a +0.01% rate adds ~0.5 to the bull side.
            if funding > 1e-6:
                bull_votes += min(1.0, funding * 5000.0)
                bull_hits.append(f"{sym} fund=+{funding*100:.4f}%")
            elif funding < -1e-6:
                bear_votes += min(1.0, -funding * 5000.0)
                bear_hits.append(f"{sym} fund={funding*100:.4f}%")

            # --- OI 1h delta: > +1% = capital flowing into perps quickly.
            # Skip the OI vote entirely when the delta is not a real
            # measurement (HL ring buffer warming up, Dune spot-volume
            # denominator unreliable, etc.) - we can't tell a flat OI
            # from "no data yet" or from a -99% placeholder.
            if intel.open_interest.oi_delta_available:
                if oi_1h > 1.0:
                    bull_votes += min(1.0, oi_1h / 5.0)
                    bull_hits.append(f"{sym} OI(1h)=+{oi_1h:.2f}%")
                elif oi_1h < -1.0:
                    bear_votes += min(1.0, -oi_1h / 5.0)
                    bear_hits.append(f"{sym} OI(1h)={oi_1h:.2f}%")

            # --- 24h price change is the tape itself.
            if price_change > 0.5:
                bull_votes += min(1.0, price_change / 5.0)
                bull_hits.append(f"{sym} 24h=+{price_change:.2f}%")
            elif price_change < -0.5:
                bear_votes += min(1.0, -price_change / 5.0)
                bear_hits.append(f"{sym} 24h={price_change:.2f}%")

            # --- L/S ratio: >1.1 = aggregate longs dominate.
            if ls_ratio > 1.1:
                bull_votes += min(1.0, (ls_ratio - 1.0))
                bull_hits.append(f"{sym} L/S={ls_ratio:.2f}")
            elif ls_ratio < 0.9:
                bear_votes += min(1.0, (1.0 - ls_ratio))
                bear_hits.append(f"{sym} L/S={ls_ratio:.2f}")

            # --- Whale direction (only when the metric actually fired).
            if intel.whales.flagged:
                if whale_dir == "accumulating":
                    bull_votes += 1.0
                    bull_hits.append(f"{sym} whales=accum")
                elif whale_dir == "distributing":
                    bear_votes += 1.0
                    bear_hits.append(f"{sym} whales=distrib")

            # --- 24h OI delta (smaller weight than 1h - confirms regime).
            # Same availability guard as the 1h block: skip when the
            # underlying OI delta isn't a real measurement.
            if intel.open_interest.oi_delta_available:
                if oi_24h > 2.0:
                    bull_votes += min(0.5, oi_24h / 20.0)
                elif oi_24h < -2.0:
                    bear_votes += min(0.5, -oi_24h / 20.0)

        # --- Market heat extremes are a tie-breaker.
        if market_heat >= 0.6:
            bull_votes += (market_heat - 0.5) * 2.0
            bull_hits.append(f"heat={market_heat:.2f}")
        elif market_heat <= 0.4:
            bear_votes += (0.5 - market_heat) * 2.0
            bear_hits.append(f"heat={market_heat:.2f}")

        # --- Stage 7: bull-bias offset (de-bias structural long lean).
        # Applied AFTER all raw votes are tallied and BEFORE the
        # bull/bear balance is computed, so it shifts the verdict
        # toward neutral/short without touching individual signals.
        offset = float(getattr(self.config, "bull_bias_offset", 0.0) or 0.0)
        if offset > 0.0 and bull_votes > 0.0:
            adjusted = max(0.0, bull_votes - offset)
            if adjusted != bull_votes:
                bull_hits.append(f"bull_bias_offset -{offset:.2f}")
            bull_votes = adjusted

        total = bull_votes + bear_votes
        if total <= 1e-6:
            return "neutral", 0.0, "no decisive on-chain signals"

        bull_pct = bull_votes / total
        margin = abs(bull_votes - bear_votes) / total  # 0..1

        bias: MarketBias
        if bull_pct >= 0.65:
            bias = "bullish"
        elif bull_pct <= 0.35:
            bias = "bearish"
        else:
            bias = "neutral"

        # Cap strength at 1.0 (margin already in [0, 1]). Use a slight
        # square-root taper so small margins still register but don't
        # punch above their weight (e.g. 0.4 raw -> 0.63 strength).
        strength = float(min(1.0, margin ** 0.5))

        hits = bull_hits if bias == "bullish" else bear_hits if bias == "bearish" else (
            bull_hits + bear_hits
        )
        rationale = (
            f"bull={bull_votes:.2f} bear={bear_votes:.2f} "
            f"margin={margin:.2f} | " + ", ".join(hits[:6])
        )
        return bias, strength, rationale

    def _build_rationale(
        self,
        score: float,
        regime: Regime,
        heat: float,
        per_symbol: dict[str, SymbolIntel],
    ) -> str:
        bits: list[str] = []
        for intel in per_symbol.values():
            bits.append(
                f"{intel.symbol}: fund={intel.funding.current_rate*100:.4f}% "
                f"OI(1h)={intel.open_interest.delta_1h_pct:+.2f}% "
                f"L/S={intel.long_short.long_short_ratio:.2f}"
            )
        return (
            f"L2 regime={regime} score={score:.2f} heat={heat:.2f} | "
            + " | ".join(bits)
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _first_row(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
        return rows[0] if rows else None

    @staticmethod
    def _row_for_symbol(
        rows: list[dict[str, Any]], symbol: str
    ) -> dict[str, Any] | None:
        for r in rows:
            if str(r.get("symbol") or "").upper() == symbol.upper():
                return r
        return None


# ---------------------------------------------------------------------------
# Helpers (module-private)
# ---------------------------------------------------------------------------


def _coerce_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _bias_to_sign(bias: str) -> int:
    """Map an L2 `market_bias` label to a -1/0/+1 directional vote."""
    if bias == "bullish":
        return 1
    if bias == "bearish":
        return -1
    return 0


def _to_status(name: str, fetch: MetricFetch) -> MetricStatus:
    return MetricStatus(
        name=name,
        source=fetch.source,
        rows=len(fetch.rows),
        query_id=fetch.query_id,
        cached=fetch.cached,
        note=fetch.note,
    )


def _intel_to_dict(intel: Level2Intelligence) -> dict[str, Any]:
    heat_conviction = float(2.0 * abs(intel.market_heat - 0.5))
    l2_conviction = float(
        min(1.0, max(heat_conviction, intel.bias_strength))
    )
    return {
        "score": intel.score,
        "market_heat": intel.market_heat,
        "heat_source": intel.heat_source,
        "regime": intel.regime,
        "rationale": intel.rationale,
        # Conviction / direction breakdown used by the engine. `score`
        # above stays as the historical heat for backwards-compat in
        # the L2 panel; `conviction` is what the engine actually votes
        # with downstream.
        "conviction": l2_conviction,
        "heat_conviction": heat_conviction,
        "direction_sign": _bias_to_sign(intel.market_bias),
        "market_bias": intel.market_bias,
        "bias_strength": intel.bias_strength,
        "bias_rationale": intel.bias_rationale,
        "generated_at": intel.generated_at,
        "cached": intel.cached,
        "notes": intel.notes,
        "dune_healthy": intel.dune_healthy,
        "vault_flow": {
            "tvl_usdc": intel.vault_flow.tvl_usdc,
            "deposits_usdc": intel.vault_flow.deposits_usdc,
            "withdrawals_usdc": intel.vault_flow.withdrawals_usdc,
            "deposit_events": intel.vault_flow.deposit_events,
            "withdrawal_events": intel.vault_flow.withdrawal_events,
            "net_flow_usdc": intel.vault_flow.net_flow_usdc,
            "window_hours": intel.vault_flow.window_hours,
        },
        "metric_status": {
            name: {
                "name": st.name,
                "source": st.source,
                "rows": st.rows,
                "query_id": st.query_id,
                "cached": st.cached,
                "note": st.note,
                "available": st.available,
            }
            for name, st in intel.metric_status.items()
        },
        "per_symbol": {
            sym: {
                "funding": {
                    "current_rate": s.funding.current_rate,
                    "rate_8h_change": s.funding.rate_8h_change,
                    "rate_24h_change": s.funding.rate_24h_change,
                    "weighted_average_24h": s.funding.weighted_average_24h,
                    "annualised_pct": s.funding.annualised_pct,
                },
                "open_interest": {
                    "current_contracts": s.open_interest.current_contracts,
                    "current_value_usd": s.open_interest.current_value_usd,
                    "delta_1h_pct": s.open_interest.delta_1h_pct,
                    "delta_4h_pct": s.open_interest.delta_4h_pct,
                    "delta_24h_pct": s.open_interest.delta_24h_pct,
                    "oi_delta_available": s.open_interest.oi_delta_available,
                },
                "volume": {
                    "last_price": s.volume.last_price,
                    "price_change_pct_24h": s.volume.price_change_pct_24h,
                    "volume_24h_usd": s.volume.volume_24h_usd,
                    "volume_1h_usd": s.volume.volume_1h_usd,
                    "spike_detected": s.volume.spike_detected,
                    "spike_zscore": s.volume.spike_zscore,
                },
                "long_short": {
                    "long_short_ratio": s.long_short.long_short_ratio,
                    "long_account_pct": s.long_short.long_account_pct,
                    "short_account_pct": s.long_short.short_account_pct,
                    "inferred_bias": s.long_short.inferred_bias,
                },
                "whales": {
                    "flagged": s.whales.flagged,
                    "direction": s.whales.direction,
                    "notional_usd_change": s.whales.notional_usd_change,
                    "n_whales": s.whales.n_whales,
                    "rationale": s.whales.rationale,
                },
                "cum_funding": {
                    "longs_paid_usd": s.cum_funding.longs_paid_usd,
                    "shorts_paid_usd": s.cum_funding.shorts_paid_usd,
                    "net_flow_usd": s.cum_funding.net_flow_usd,
                    "window_hours": s.cum_funding.window_hours,
                },
                "notes": s.notes,
            }
            for sym, s in intel.per_symbol.items()
        },
    }


__all__ = [
    "Level2",
    "Level2Config",
    "Level2Intelligence",
    "SymbolIntel",
    "FundingSnapshot",
    "OpenInterestSnapshot",
    "VolumeSnapshot",
    "LongShortSnapshot",
    "WhaleSnapshot",
    "CumulativeFundingSnapshot",
    "VaultFlowSnapshot",
    "MetricStatus",
]
