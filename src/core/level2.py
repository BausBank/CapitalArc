"""Level 2 - on-chain intelligence sourced **only** from Dune MCP.

Level 2 is the agent's main analytical pillar. By design it consults
exactly one external service - the **Dune MCP** server (via its
Bearer-authenticated REST API) - and aggregates the result into a
structured, actionable on-chain read for the three perp symbols we
trade (`BTC-PERP`, `ETH-PERP`, `SOL-PERP`).

Chain & data source
-------------------
The Arc Testnet isn't yet indexed by Dune, so every Level 2 metric
is computed against a live, high-liquidity EVM chain (`ethereum`
by default; `base` / `arbitrum` selectable via `Level2Config.chain`).
The SQL templates target `dex.trades` (multichain spot DEX trades)
and `erc20_<chain>.evt_Transfer`, with the BTC / ETH / SOL token
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


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass
class Level2Config:
    """Configuration for Level 2 on-chain intelligence."""

    symbols: list[str] = field(
        default_factory=lambda: ["BTC-PERP", "ETH-PERP", "SOL-PERP"]
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
    # Used by the heuristic regime classifier when the Dune-side
    # `market_sentiment` query is not configured.
    risk_on_heat: float = 0.62
    risk_off_heat: float = 0.38


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass
class MetricStatus:
    """Lightweight provenance carrier for a single metric."""

    name: str
    source: str          # "dune:<id>" | "n/a" | "error"
    rows: int = 0
    query_id: int | None = None
    cached: bool = False
    note: str | None = None

    @property
    def available(self) -> bool:
        return self.source.startswith("dune:")


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
    generated_at: float = field(default_factory=time.time)
    cached: bool = False
    notes: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Implementation
# ---------------------------------------------------------------------------


class Level2:
    """On-chain intelligence level, sourced only from Dune MCP."""

    LEVEL = 2

    def __init__(
        self,
        config: Level2Config | None = None,
        dune: DuneMCPClient | None = None,
    ) -> None:
        self.config = config or Level2Config()
        self.dune = dune
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
        ok = await self.dune.ping()
        logger.info("Dune MCP {}", "connected" if ok else "unavailable")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def score(self, market: dict[str, Any]) -> "LevelScore":
        from src.core.decision_engine import LevelScore

        intel = await self.evaluate(market)
        return LevelScore(
            level=self.LEVEL,
            score=intel.score,
            rationale=intel.rationale,
            raw={"l2": _intel_to_dict(intel)},
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

        # ---- regime / heat --------------------------------------------
        sentiment_row = self._first_row(fetches["market_sentiment"].rows)
        heat = _coerce_float(sentiment_row.get("heat") if sentiment_row else None)
        if heat is None:
            heat = self._heuristic_heat(per_symbol)
        score = float(min(1.0, max(0.0, heat)))
        regime = self._classify_regime(score)
        rationale = self._build_rationale(score, regime, heat, per_symbol)

        # ---- metric_status (provenance map) ---------------------------
        metric_status = {
            name: _to_status(name, fetch) for name, fetch in fetches.items()
        }

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
            per_symbol=per_symbol,
            vault_flow=vault_flow,
            metric_status=metric_status,
            dune_healthy=dune_healthy,
            notes=notes,
        )
        if self.config.demo_mode:
            self._cache = (intel, time.time() + self.config.cache_ttl_seconds)
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
            "symbols": ",".join(symbols),
            "btc_token_address": token_map.get("BTC-PERP", ""),
            "eth_token_address": token_map.get("ETH-PERP", ""),
            "sol_token_address": token_map.get("SOL-PERP", ""),
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

        async def _fetch(name: str) -> tuple[str, MetricFetch]:
            if self.dune is None:
                return name, MetricFetch(
                    metric=name,
                    source="n/a",
                    note="DuneMCPClient is None (DUNE_API_KEY missing)",
                )
            return name, await self.dune.fetch_metric(
                name, params=common_params
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
            # Fallback: derive whale flag from OI 1h delta.
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
        return WhaleSnapshot(
            symbol=symbol,
            flagged=bool(row.get("flagged") or row.get("whale_flagged")),
            direction=str(row.get("direction") or "neutral"),  # type: ignore[arg-type]
            notional_usd_change=_coerce_float(
                row.get("notional_usd_change")
            )
            or 0.0,
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
            base += max(-0.10, min(0.10, intel.open_interest.delta_1h_pct / 100.0 * 2))
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
    return {
        "score": intel.score,
        "market_heat": intel.market_heat,
        "regime": intel.regime,
        "rationale": intel.rationale,
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
