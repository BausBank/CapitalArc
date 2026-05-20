"""Level 2 - on-chain intelligence (the agent's main analytical pillar).

Level 2 fuses three data sources into one structured market read:

    1. **Dune MCP** (`DuneMCPClient`)         - on-chain analytics layer.
    2. **Binance perp public API**            - real-time funding, OI,
                                                volume and long/short
                                                ratio used as a high-
                                                quality proxy for the
                                                three perp symbols.
    3. **Arc on-chain reader**                - direct reads against
                                                `USDCCollateralVault` on
                                                Arc Testnet.

The output is a structured, actionable `Level2Intelligence` object
covering, per perp symbol:
    - funding rate (current + 8h / 24h delta) and weighted-average.
    - open interest (current + 1h / 4h / 24h delta) and volume spikes.
    - long/short ratio and inferred bias.
    - cumulative funding paid / received over the recent window.
    - whale-activity flag derived from OI deltas.
    - Arc vault TVL + net deposits / withdrawals + Dune MCP health.
    - a market-wide "heat" score that summarises overall risk-on /
      risk-off pressure across all three perps.

Caching: in `demo` mode every produced `Level2Intelligence` is cached
for `cache_ttl_seconds` so demo iterations are cheap and idempotent.
"""

from __future__ import annotations

import asyncio
import statistics
import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Literal

from src.data.arc_onchain import ArcOnchainReader, ArcOnchainSnapshot
from src.data.binance_client import BinanceClient
from src.data.dune_mcp import DuneMCPClient
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
    funding_history_limit: int = 6        # last 6 funding events ~= 48h
    oi_period: str = "1h"
    oi_history_limit: int = 30            # 30 hours of 1h OI samples
    volume_spike_z: float = 1.5           # 1.5 sigma above 24h mean = spike
    whale_oi_delta_pct: float = 5.0       # |OI 1h-delta| >= 5% -> whale flag
    funding_alarm_bps: float = 5.0        # |annualised funding| > 50% triggers warn
    cache_ttl_seconds: int = 1800
    demo_mode: bool = True
    dune_enabled: bool = True


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass
class FundingSnapshot:
    """Funding-rate snapshot for one symbol."""

    symbol: str
    current_rate: float = 0.0           # per-funding-event rate (8h)
    rate_8h_change: float = 0.0          # current - previous (raw rate, not bps)
    rate_24h_change: float = 0.0
    weighted_average_24h: float = 0.0
    annualised_pct: float = 0.0          # rate * 3 * 365 * 100, just a hint
    next_funding_ms: int = 0
    history: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class OpenInterestSnapshot:
    """Open-interest snapshot for one symbol (contracts and USD value)."""

    symbol: str
    current_contracts: float = 0.0
    current_value_usd: float = 0.0
    delta_1h_pct: float = 0.0
    delta_4h_pct: float = 0.0
    delta_24h_pct: float = 0.0
    history: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class VolumeSnapshot:
    """Recent volume snapshot for one symbol (24h ticker + spike flag)."""

    symbol: str
    last_price: float = 0.0
    price_change_pct_24h: float = 0.0
    base_volume_24h: float = 0.0
    quote_volume_24h: float = 0.0
    trades_24h: int = 0
    spike_detected: bool = False
    spike_zscore: float = 0.0


@dataclass
class LongShortSnapshot:
    """Long/short account ratio + inferred bias."""

    symbol: str
    long_short_ratio: float = 1.0
    long_account_pct: float = 0.5
    short_account_pct: float = 0.5
    inferred_bias: Literal["long", "short", "balanced"] = "balanced"


@dataclass
class WhaleSnapshot:
    """Whale-activity inference based on rapid OI moves."""

    symbol: str
    flagged: bool = False
    direction: Literal["accumulating", "distributing", "neutral"] = "neutral"
    notional_usd_change: float = 0.0
    rationale: str = ""


@dataclass
class CumulativeFundingSnapshot:
    """Cumulative funding paid / received in the recent window."""

    symbol: str
    longs_paid_usd: float = 0.0          # positive funding -> longs pay shorts
    shorts_paid_usd: float = 0.0
    net_flow_usd: float = 0.0
    window_hours: float = 0.0


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
class OnchainSummary:
    """Aggregated on-chain (Arc + Dune) layer of Level 2."""

    arc: ArcOnchainSnapshot | None
    dune_healthy: bool
    dune_notes: list[str] = field(default_factory=list)


@dataclass
class Level2Intelligence:
    """Top-level result of Level 2."""

    score: float
    regime: Regime
    rationale: str
    market_heat: float                   # 0..1 aggregated heat
    per_symbol: dict[str, SymbolIntel]
    onchain: OnchainSummary
    generated_at: float = field(default_factory=time.time)
    cached: bool = False
    notes: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Implementation
# ---------------------------------------------------------------------------


class Level2:
    """On-chain + market intelligence level."""

    LEVEL = 2

    def __init__(
        self,
        config: Level2Config | None = None,
        binance: BinanceClient | None = None,
        dune: DuneMCPClient | None = None,
        onchain: ArcOnchainReader | None = None,
    ) -> None:
        self.config = config or Level2Config()
        self.binance = binance or BinanceClient()
        self.dune = dune
        self.onchain = onchain
        self._cache: tuple[Level2Intelligence, float] | None = None

    async def connect(self) -> None:
        """Probe Dune MCP once so callers know whether it's healthy."""
        if self.dune is not None and self.config.dune_enabled:
            ok = await self.dune.ping()
            logger.info("Dune MCP {}", "connected" if ok else "unavailable")

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
        # ---- cache ------------------------------------------------------
        if self.config.demo_mode and self._cache is not None:
            cached, expiry = self._cache
            if time.time() < expiry:
                cached.cached = True
                return cached

        symbols = list(market.get("symbols") or self.config.symbols)
        symbols = [s for s in symbols if s] or self.config.symbols

        # ---- per-symbol fetches in parallel ----------------------------
        per_symbol = await self._gather_symbols(symbols)

        # ---- on-chain + dune readouts in parallel ----------------------
        onchain_task = asyncio.create_task(self._collect_onchain(market))
        dune_task = asyncio.create_task(self._collect_dune())
        arc_snapshot, dune_status = await asyncio.gather(onchain_task, dune_task)

        onchain_summary = OnchainSummary(
            arc=arc_snapshot,
            dune_healthy=dune_status["healthy"],
            dune_notes=dune_status["notes"],
        )

        score, heat, regime, rationale = self._aggregate(per_symbol, onchain_summary)
        intel = Level2Intelligence(
            score=score,
            regime=regime,
            rationale=rationale,
            market_heat=heat,
            per_symbol=per_symbol,
            onchain=onchain_summary,
            notes=[],
        )
        if self.config.demo_mode:
            self._cache = (intel, time.time() + self.config.cache_ttl_seconds)
        return intel

    # ------------------------------------------------------------------
    # Data collection helpers
    # ------------------------------------------------------------------

    async def _gather_symbols(self, symbols: list[str]) -> dict[str, SymbolIntel]:
        tasks = {sym: asyncio.create_task(self._collect_symbol(sym)) for sym in symbols}
        out: dict[str, SymbolIntel] = {}
        for sym, task in tasks.items():
            try:
                out[sym] = await task
            except Exception as exc:  # noqa: BLE001
                logger.warning("L2 collect {} failed: {}", sym, exc)
                out[sym] = _empty_symbol_intel(sym, note=f"fetch failed: {exc}")
        return out

    async def _collect_symbol(self, symbol: str) -> SymbolIntel:
        funding_task = asyncio.create_task(
            self.binance.get_funding_rate_history(
                symbol, limit=self.config.funding_history_limit
            )
        )
        premium_task = asyncio.create_task(self.binance.get_premium_index(symbol))
        oi_task = asyncio.create_task(self.binance.get_open_interest(symbol))
        oi_hist_task = asyncio.create_task(
            self.binance.get_open_interest_history(
                symbol,
                period=self.config.oi_period,
                limit=self.config.oi_history_limit,
            )
        )
        ticker_task = asyncio.create_task(self.binance.get_24h_ticker(symbol))
        lsr_task = asyncio.create_task(
            self.binance.get_long_short_ratio(symbol, period="1h", limit=1)
        )

        funding_hist, premium, oi_current, oi_hist, ticker, lsr = await asyncio.gather(
            funding_task,
            premium_task,
            oi_task,
            oi_hist_task,
            ticker_task,
            lsr_task,
            return_exceptions=False,
        )

        funding = self._build_funding(symbol, funding_hist, premium)
        oi = self._build_oi(symbol, oi_current, oi_hist, ticker.get("lastPrice", 0.0))
        volume = self._build_volume(symbol, ticker, oi_hist)
        long_short = self._build_long_short(symbol, lsr)
        whales = self._build_whales(symbol, oi)
        cum_funding = self._build_cumulative_funding(symbol, funding_hist, oi)

        return SymbolIntel(
            symbol=symbol,
            funding=funding,
            open_interest=oi,
            volume=volume,
            long_short=long_short,
            whales=whales,
            cum_funding=cum_funding,
        )

    # ---- builders ----------------------------------------------------

    def _build_funding(
        self,
        symbol: str,
        history: list[dict[str, Any]],
        premium: dict[str, Any],
    ) -> FundingSnapshot:
        if not history:
            current_rate = float(premium.get("lastFundingRate", 0.0))
            history = []
        else:
            current_rate = float(history[-1]["fundingRate"])
        rate_8h_change = (
            current_rate - float(history[-2]["fundingRate"])
            if len(history) >= 2
            else 0.0
        )
        # 24h window = last 3 events (Binance settles every 8h).
        last24 = history[-3:]
        if len(last24) >= 1:
            avg = sum(float(h["fundingRate"]) for h in last24) / len(last24)
        else:
            avg = current_rate
        rate_24h_change = (
            current_rate - float(history[-4]["fundingRate"])
            if len(history) >= 4
            else 0.0
        )
        annualised = current_rate * 3 * 365 * 100  # %/yr
        return FundingSnapshot(
            symbol=symbol,
            current_rate=current_rate,
            rate_8h_change=rate_8h_change,
            rate_24h_change=rate_24h_change,
            weighted_average_24h=avg,
            annualised_pct=annualised,
            next_funding_ms=int(premium.get("nextFundingTime", 0)),
            history=history,
        )

    def _build_oi(
        self,
        symbol: str,
        current_contracts: Decimal,
        history: list[dict[str, Any]],
        last_price: float,
    ) -> OpenInterestSnapshot:
        cur = float(current_contracts)
        cur_value_usd = cur * float(last_price)
        if history:
            # The value entries are USD value directly (sumOpenInterestValue).
            latest = float(history[-1].get("sumOpenInterest", cur))
            cur_value_usd = float(history[-1].get("sumOpenInterestValue", cur_value_usd))
            # 1h, 4h, 24h deltas (Binance points are spaced by `period`).
            def _delta(n: int) -> float:
                if len(history) <= n:
                    return 0.0
                prev = float(history[-1 - n].get("sumOpenInterest", 0.0))
                if prev == 0:
                    return 0.0
                return (latest - prev) / prev * 100.0

            d1 = _delta(1)
            d4 = _delta(4)
            d24 = _delta(24)
            current = latest
        else:
            current = cur
            d1 = d4 = d24 = 0.0
        return OpenInterestSnapshot(
            symbol=symbol,
            current_contracts=current,
            current_value_usd=cur_value_usd,
            delta_1h_pct=d1,
            delta_4h_pct=d4,
            delta_24h_pct=d24,
            history=history,
        )

    def _build_volume(
        self,
        symbol: str,
        ticker: dict[str, Any],
        oi_hist: list[dict[str, Any]],
    ) -> VolumeSnapshot:
        quote_vol = float(ticker.get("quoteVolume", 0.0))
        base_vol = float(ticker.get("volume", 0.0))
        # Use OI USD-value series as a cheap proxy "activity" baseline.
        # If volume in the latest period is meaningfully above the mean,
        # we flag a spike.
        spike = False
        z = 0.0
        if oi_hist and len(oi_hist) >= 5:
            values = [float(r.get("sumOpenInterestValue", 0.0)) for r in oi_hist]
            mean = statistics.fmean(values) if values else 0.0
            stdev = statistics.pstdev(values) if len(values) > 1 else 0.0
            if stdev > 0:
                z = (values[-1] - mean) / stdev
                spike = abs(z) >= self.config.volume_spike_z
        return VolumeSnapshot(
            symbol=symbol,
            last_price=float(ticker.get("lastPrice", 0.0)),
            price_change_pct_24h=float(ticker.get("priceChangePct", 0.0)),
            base_volume_24h=base_vol,
            quote_volume_24h=quote_vol,
            trades_24h=int(ticker.get("count", 0)),
            spike_detected=spike,
            spike_zscore=z,
        )

    def _build_long_short(
        self, symbol: str, lsr: dict[str, Any]
    ) -> LongShortSnapshot:
        ratio = float(lsr.get("longShortRatio", 1.0))
        long_pct = float(lsr.get("longAccount", 0.5))
        short_pct = float(lsr.get("shortAccount", 0.5))
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

    def _build_whales(
        self, symbol: str, oi: OpenInterestSnapshot
    ) -> WhaleSnapshot:
        thresh = self.config.whale_oi_delta_pct
        if abs(oi.delta_1h_pct) < thresh:
            return WhaleSnapshot(symbol=symbol)
        if oi.delta_1h_pct > 0:
            direction = "accumulating"
            msg = (
                f"OI grew {oi.delta_1h_pct:.2f}% in 1h on {symbol} - "
                "large position open."
            )
        else:
            direction = "distributing"
            msg = (
                f"OI fell {oi.delta_1h_pct:.2f}% in 1h on {symbol} - "
                "large position close."
            )
        notional_change = oi.current_value_usd * oi.delta_1h_pct / 100.0
        return WhaleSnapshot(
            symbol=symbol,
            flagged=True,
            direction=direction,
            notional_usd_change=notional_change,
            rationale=msg,
        )

    def _build_cumulative_funding(
        self,
        symbol: str,
        history: list[dict[str, Any]],
        oi: OpenInterestSnapshot,
    ) -> CumulativeFundingSnapshot:
        if not history:
            return CumulativeFundingSnapshot(symbol=symbol)
        notional = oi.current_value_usd
        longs_paid = 0.0
        shorts_paid = 0.0
        for h in history:
            r = float(h.get("fundingRate", 0.0))
            # Positive funding => longs pay shorts at notional * rate.
            if r >= 0:
                longs_paid += notional * r
            else:
                shorts_paid += notional * abs(r)
        net = longs_paid - shorts_paid
        # Each Binance funding event covers 8 hours.
        window_hours = float(len(history) * 8)
        return CumulativeFundingSnapshot(
            symbol=symbol,
            longs_paid_usd=longs_paid,
            shorts_paid_usd=shorts_paid,
            net_flow_usd=net,
            window_hours=window_hours,
        )

    # ---- on-chain + dune --------------------------------------------

    async def _collect_onchain(
        self, market: dict[str, Any]
    ) -> ArcOnchainSnapshot | None:
        if self.onchain is None:
            return None
        account_id = market.get("account_id")
        try:
            return await self.onchain.snapshot(account_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Arc on-chain snapshot failed: {}", exc)
            return None

    async def _collect_dune(self) -> dict[str, Any]:
        notes: list[str] = []
        if self.dune is None or not self.config.dune_enabled:
            notes.append("Dune MCP disabled in config.")
            return {"healthy": False, "notes": notes}
        try:
            healthy = await self.dune.ping()
        except Exception as exc:  # noqa: BLE001
            notes.append(f"Dune MCP ping raised {exc!r}.")
            healthy = False
        if healthy:
            notes.append("Dune MCP reachable; using cached query surface.")
        else:
            notes.append("Dune MCP unreachable; on-chain layer degrades gracefully.")
        return {"healthy": healthy, "notes": notes}

    # ------------------------------------------------------------------
    # Aggregation
    # ------------------------------------------------------------------

    def _aggregate(
        self,
        per_symbol: dict[str, SymbolIntel],
        onchain: OnchainSummary,
    ) -> tuple[float, float, Regime, str]:
        if not per_symbol:
            return 0.5, 0.5, "neutral", "L2 has no symbols to analyse."

        symbol_scores: list[float] = []
        for intel in per_symbol.values():
            symbol_scores.append(self._score_symbol(intel))

        heat = float(sum(symbol_scores) / len(symbol_scores))
        # On-chain modifier: vault net-inflow bumps risk-on slightly;
        # large net outflow dampens score.
        modifier = 0.0
        if onchain.arc and onchain.arc.flow:
            net = float(onchain.arc.flow.net_flow_usdc)
            tvl = float(onchain.arc.vault_tvl_usdc or 1.0)
            if tvl > 0:
                rel = max(-0.2, min(0.2, net / tvl))
                modifier = 0.1 * rel / 0.2  # max +/- 0.1
        score = max(0.0, min(1.0, heat + modifier))

        regime: Regime
        if score >= 0.62:
            regime = "risk_on"
        elif score <= 0.38:
            regime = "risk_off"
        elif abs(score - 0.5) <= 0.05:
            regime = "neutral"
        else:
            regime = "transition"

        # Compact rationale.
        bits: list[str] = []
        for intel in per_symbol.values():
            bits.append(
                f"{intel.symbol}: fund={intel.funding.current_rate*100:.4f}% "
                f"OI(1h)={intel.open_interest.delta_1h_pct:+.2f}% "
                f"L/S={intel.long_short.long_short_ratio:.2f}"
            )
        rationale = (
            f"L2 regime={regime} score={score:.2f} heat={heat:.2f} | "
            + " | ".join(bits)
        )
        return float(score), float(heat), regime, rationale

    def _score_symbol(self, intel: SymbolIntel) -> float:
        """Heuristic per-symbol score in [0,1].

        Combines:
            * Funding direction (positive funding -> shorts paid =>
              market net-long => slight risk-on bias).
            * OI 1h delta sign.
            * 24h price change.
            * Long/short ratio.
            * Volume spike (small positive contribution to magnitude).
        """
        base = 0.5
        # Funding contribution (bounded).
        f = intel.funding.current_rate
        base += max(-0.10, min(0.10, f * 20))  # 0.01% rate ~= +0.002
        # OI delta contribution.
        d = intel.open_interest.delta_1h_pct / 100.0
        base += max(-0.10, min(0.10, d * 2))
        # Price change contribution.
        p = intel.volume.price_change_pct_24h / 100.0
        base += max(-0.15, min(0.15, p))
        # Long/short bias.
        lsr = intel.long_short.long_short_ratio
        if lsr > 1.0:
            base += min(0.05, (lsr - 1.0) * 0.05)
        else:
            base -= min(0.05, (1.0 - lsr) * 0.05)
        return float(max(0.0, min(1.0, base)))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _empty_symbol_intel(symbol: str, note: str = "") -> SymbolIntel:
    return SymbolIntel(
        symbol=symbol,
        funding=FundingSnapshot(symbol=symbol),
        open_interest=OpenInterestSnapshot(symbol=symbol),
        volume=VolumeSnapshot(symbol=symbol),
        long_short=LongShortSnapshot(symbol=symbol),
        whales=WhaleSnapshot(symbol=symbol),
        cum_funding=CumulativeFundingSnapshot(symbol=symbol),
        notes=[note] if note else [],
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
        "onchain": {
            "arc": _arc_snapshot_to_dict(intel.onchain.arc),
            "dune_healthy": intel.onchain.dune_healthy,
            "dune_notes": intel.onchain.dune_notes,
        },
        "per_symbol": {
            sym: {
                "funding": {
                    "current_rate": s.funding.current_rate,
                    "rate_8h_change": s.funding.rate_8h_change,
                    "rate_24h_change": s.funding.rate_24h_change,
                    "weighted_average_24h": s.funding.weighted_average_24h,
                    "annualised_pct": s.funding.annualised_pct,
                    "next_funding_ms": s.funding.next_funding_ms,
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
                    "base_volume_24h": s.volume.base_volume_24h,
                    "quote_volume_24h": s.volume.quote_volume_24h,
                    "trades_24h": s.volume.trades_24h,
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


def _arc_snapshot_to_dict(snap: ArcOnchainSnapshot | None) -> dict[str, Any] | None:
    if snap is None:
        return None
    flow = snap.flow
    return {
        "rpc_url": snap.rpc_url,
        "chain_id": snap.chain_id,
        "latest_block": snap.latest_block,
        "vault_tvl_usdc": float(snap.vault_tvl_usdc),
        "vault_address": snap.vault_address,
        "usdc_address": snap.usdc_address,
        "agent_margin_usdc": float(snap.agent_margin_usdc)
        if snap.agent_margin_usdc is not None
        else None,
        "flow": (
            None
            if flow is None
            else {
                "block_window": flow.block_window,
                "deposits_usdc": float(flow.deposits_usdc),
                "withdrawals_usdc": float(flow.withdrawals_usdc),
                "net_flow_usdc": float(flow.net_flow_usdc),
                "deposit_events": flow.deposit_events,
                "withdrawal_events": flow.withdrawal_events,
            }
        ),
        "healthy": snap.healthy,
        "notes": snap.notes,
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
    "OnchainSummary",
]
