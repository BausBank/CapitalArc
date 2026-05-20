"""Level 1 - fast deterministic technical rules ("защита от дурака").

Level 1 is the *gate* in front of the decision engine. It does not
predict the market; it refuses to let the agent trade in obviously
hostile conditions:

    - Trend filter      EMA9 vs EMA21 alignment on 15m AND 1h.
    - RSI extreme       skip when RSI >= L1_RSI_OVERBOUGHT or
                        RSI <= L1_RSI_OVERSOLD on any timeframe.
    - Volatility band   ATR% must lie inside [L1_ATR_PCT_MIN,
                        L1_ATR_PCT_MAX]; outside that band the market
                        is either too dead or too unstable.
    - Account drawdown  refuse when the live account drawdown
                        breaches `max_drawdown_pct`.

Returned `Level1Decision` has:
    - `passes`           bool, did we let the trade through.
    - `score`            in [0,1]; 0.0 when blocked, otherwise weighted
                         strength of the surviving signals.
    - `reasons`          list of `Level1Reason` (kind, code, message,
                         severity) so the UI can render them per symbol.
    - `per_symbol`       map of `SymbolReadout` for each requested perp.

The level wraps that into a `LevelScore` for the engine; the structured
detail lives in `LevelScore.raw["l1"]` for downstream consumers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

import numpy as np
import pandas as pd

from src.data.arc_market_data import ArcMarketData, KlineFetchResult
from src.utils.logging import logger

if TYPE_CHECKING:
    from src.core.decision_engine import LevelScore


# ---------------------------------------------------------------------------
# Configuration & result types
# ---------------------------------------------------------------------------


@dataclass
class Level1Config:
    """Configuration for Level 1 hard rules."""

    timeframes: list[str] = field(default_factory=lambda: ["15m", "1h"])
    klines_limit: int = 150
    ema_fast: int = 9
    ema_slow: int = 21
    rsi_period: int = 14
    atr_period: int = 14
    rsi_overbought: float = 70.0
    rsi_oversold: float = 30.0
    atr_pct_min: float = 0.15
    atr_pct_max: float = 6.0
    max_drawdown_pct: float = 10.0
    require_tf_agreement: bool = True


Severity = Literal["info", "warn", "block"]


@dataclass
class Level1Reason:
    """A single rule outcome (passed / warned / blocked)."""

    code: str          # short stable identifier, e.g. "rsi_overbought"
    severity: Severity
    message: str       # human-friendly one-liner
    symbol: str | None = None
    timeframe: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class IndicatorRow:
    """Indicator snapshot for one (symbol, timeframe)."""

    symbol: str
    timeframe: str
    close: float
    ema_fast: float
    ema_slow: float
    rsi: float
    atr: float
    atr_pct: float
    trend: Literal["up", "down", "flat"]
    last_candles: int


@dataclass
class SymbolReadout:
    """Aggregated Level-1 view of one symbol across all timeframes."""

    symbol: str
    passes: bool
    trend: Literal["up", "down", "mixed", "flat"]
    rows: list[IndicatorRow]
    blocking_reasons: list[Level1Reason] = field(default_factory=list)
    informational_reasons: list[Level1Reason] = field(default_factory=list)

    @property
    def strength(self) -> float:
        """0..1 score reflecting how convincingly the trend is aligned."""
        if not self.rows:
            return 0.0
        if self.trend == "flat" or self.trend == "mixed":
            return 0.25
        # How "stretched" EMA-fast is above/below EMA-slow on average,
        # normalised by the per-candle ATR (so 1.0 means roughly 1x ATR
        # of trend separation - a clear, sustainable trend).
        separations = []
        for r in self.rows:
            if r.atr <= 0:
                continue
            sep = abs(r.ema_fast - r.ema_slow) / r.atr
            separations.append(min(1.0, sep))
        if not separations:
            return 0.5
        return float(min(1.0, sum(separations) / len(separations)))


@dataclass
class Level1Decision:
    """Top-level Level-1 outcome."""

    passes: bool
    score: float
    rationale: str
    reasons: list[Level1Reason]
    per_symbol: dict[str, SymbolReadout]
    raw: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Implementation
# ---------------------------------------------------------------------------


class Level1:
    """Technical hard-rules level of the decision engine."""

    LEVEL = 1

    def __init__(
        self,
        config: Level1Config | None = None,
        *,
        market_data: ArcMarketData,
    ) -> None:
        self.config = config or Level1Config()
        self.market_data = market_data

    async def score(self, market: dict[str, Any]) -> "LevelScore":
        from src.core.decision_engine import LevelScore

        decision = await self.evaluate(market)
        return LevelScore(
            level=self.LEVEL,
            score=decision.score,
            rationale=decision.rationale,
            raw={"l1": _decision_to_dict(decision)},
        )

    async def evaluate(self, market: dict[str, Any]) -> Level1Decision:
        symbols: list[str] = list(market.get("symbols") or [market.get("symbol", "BTC-PERP")])
        symbols = [s for s in symbols if s]
        primary_symbol = market.get("symbol") or (symbols[0] if symbols else None)

        reasons: list[Level1Reason] = []
        account_blocks: list[Level1Reason] = []
        per_symbol: dict[str, SymbolReadout] = {}

        # ---- Account-level guard (drawdown) -----------------------------
        drawdown_pct = float(market.get("account_drawdown_pct") or 0.0)
        if drawdown_pct >= self.config.max_drawdown_pct:
            r = Level1Reason(
                code="drawdown_breach",
                severity="block",
                message=(
                    f"Account drawdown {drawdown_pct:.2f}% >= "
                    f"limit {self.config.max_drawdown_pct:.2f}% - "
                    "forcing flat."
                ),
                metadata={"drawdown_pct": drawdown_pct},
            )
            account_blocks.append(r)
            reasons.append(r)
        # ---- Per-symbol technical checks --------------------------------
        for sym in symbols:
            readout = await self._evaluate_symbol(sym)
            per_symbol[sym] = readout
            reasons.extend(readout.blocking_reasons)
            reasons.extend(readout.informational_reasons)

        if not symbols:
            return Level1Decision(
                passes=True,
                score=0.5,
                rationale="L1 has no symbols configured; defaulting to neutral.",
                reasons=reasons,
                per_symbol=per_symbol,
            )

        # The agent acts on the *primary* symbol; that symbol's verdict
        # plus the account-level guard decide whether L1 passes overall.
        # Other symbols are still evaluated (for the panel + L2 / Gemini
        # context), but their per-symbol blocks don't veto the trade on
        # the primary symbol.
        primary_readout = per_symbol.get(primary_symbol) if primary_symbol else None
        primary_blocked = (
            primary_readout is not None and not primary_readout.passes
        )
        passes = not account_blocks and not primary_blocked

        if not passes:
            blocking_for_msg = list(account_blocks)
            if primary_readout is not None:
                blocking_for_msg.extend(primary_readout.blocking_reasons)
            kinds = sorted({r.code for r in blocking_for_msg}) or ["unknown"]
            tag = ", ".join(kinds[:3]) + ("..." if len(kinds) > 3 else "")
            score = 0.0
            rationale = (
                f"L1 BLOCKED on primary={primary_symbol}: {tag}"
            )
        else:
            # When passing, score reflects average per-symbol strength of
            # the symbols that *individually* passed (so a clear majority
            # of aligned symbols boosts conviction).
            strengths = [
                ro.strength for ro in per_symbol.values() if ro.passes
            ]
            avg = sum(strengths) / len(strengths) if strengths else 0.5
            score = float(0.5 + 0.5 * avg)
            trends = {sym: ro.trend for sym, ro in per_symbol.items()}
            rationale = (
                f"L1 OK (primary={primary_symbol}): trends={trends}, "
                f"avg-strength={avg:.2f}."
            )

        return Level1Decision(
            passes=passes,
            score=score,
            rationale=rationale,
            reasons=reasons,
            per_symbol=per_symbol,
            raw={
                "config": {
                    "timeframes": self.config.timeframes,
                    "ema_fast": self.config.ema_fast,
                    "ema_slow": self.config.ema_slow,
                    "rsi_period": self.config.rsi_period,
                    "atr_period": self.config.atr_period,
                    "rsi_overbought": self.config.rsi_overbought,
                    "rsi_oversold": self.config.rsi_oversold,
                    "atr_pct_min": self.config.atr_pct_min,
                    "atr_pct_max": self.config.atr_pct_max,
                    "max_drawdown_pct": self.config.max_drawdown_pct,
                }
            },
        )

    async def _evaluate_symbol(self, symbol: str) -> SymbolReadout:
        rows: list[IndicatorRow] = []
        blocking: list[Level1Reason] = []
        informational: list[Level1Reason] = []

        per_tf_trend: list[str] = []

        for tf in self.config.timeframes:
            fetch = await self._safe_klines(symbol, tf, self.config.klines_limit)
            df = fetch.df if fetch is not None else None
            min_required = (
                max(
                    self.config.ema_slow,
                    self.config.rsi_period,
                    self.config.atr_period,
                )
                + 5
            )
            if df is None or df.empty or len(df) < min_required:
                note_hint = ""
                if fetch is not None:
                    fills = fetch.fills
                    note_hint = (
                        f" (Arc RPC: fills={fills}, "
                        f"blocks={fetch.from_block}..{fetch.to_block})"
                    )
                blocking.append(
                    Level1Reason(
                        code="ohlcv_unavailable",
                        severity="block",
                        message=(
                            f"OHLCV for {symbol}@{tf} unavailable or too short "
                            "to apply technical filters" + note_hint + "."
                        ),
                        symbol=symbol,
                        timeframe=tf,
                        metadata={
                            "fills": fetch.fills if fetch else 0,
                            "have_bars": int(0 if df is None else len(df)),
                            "min_required_bars": min_required,
                            "notes": list(fetch.notes) if fetch else [],
                        },
                    )
                )
                continue

            row = self._compute_indicators(df, symbol, tf)
            rows.append(row)
            per_tf_trend.append(row.trend)

            # RSI extreme filter
            if row.rsi >= self.config.rsi_overbought:
                blocking.append(
                    Level1Reason(
                        code="rsi_overbought",
                        severity="block",
                        message=(
                            f"{symbol}@{tf} RSI={row.rsi:.1f} >= "
                            f"{self.config.rsi_overbought:.0f} (overbought)."
                        ),
                        symbol=symbol,
                        timeframe=tf,
                        metadata={"rsi": row.rsi},
                    )
                )
            elif row.rsi <= self.config.rsi_oversold:
                blocking.append(
                    Level1Reason(
                        code="rsi_oversold",
                        severity="block",
                        message=(
                            f"{symbol}@{tf} RSI={row.rsi:.1f} <= "
                            f"{self.config.rsi_oversold:.0f} (oversold)."
                        ),
                        symbol=symbol,
                        timeframe=tf,
                        metadata={"rsi": row.rsi},
                    )
                )

            # Volatility band filter (ATR%)
            if row.atr_pct < self.config.atr_pct_min:
                blocking.append(
                    Level1Reason(
                        code="atr_too_low",
                        severity="block",
                        message=(
                            f"{symbol}@{tf} ATR%={row.atr_pct:.2f}% < "
                            f"{self.config.atr_pct_min:.2f}% (market too dead)."
                        ),
                        symbol=symbol,
                        timeframe=tf,
                        metadata={"atr_pct": row.atr_pct},
                    )
                )
            elif row.atr_pct > self.config.atr_pct_max:
                blocking.append(
                    Level1Reason(
                        code="atr_too_high",
                        severity="block",
                        message=(
                            f"{symbol}@{tf} ATR%={row.atr_pct:.2f}% > "
                            f"{self.config.atr_pct_max:.2f}% (volatility regime hostile)."
                        ),
                        symbol=symbol,
                        timeframe=tf,
                        metadata={"atr_pct": row.atr_pct},
                    )
                )
            else:
                informational.append(
                    Level1Reason(
                        code="atr_band_ok",
                        severity="info",
                        message=(
                            f"{symbol}@{tf} ATR%={row.atr_pct:.2f}% inside "
                            f"[{self.config.atr_pct_min:.2f}%, "
                            f"{self.config.atr_pct_max:.2f}%]."
                        ),
                        symbol=symbol,
                        timeframe=tf,
                    )
                )

        # Trend alignment across timeframes
        trend_summary: Literal["up", "down", "mixed", "flat"]
        if not rows:
            trend_summary = "flat"
        elif self.config.require_tf_agreement:
            if all(t == "up" for t in per_tf_trend):
                trend_summary = "up"
                informational.append(
                    Level1Reason(
                        code="trend_aligned_up",
                        severity="info",
                        message=(
                            f"{symbol}: trend aligned UP on "
                            f"{', '.join(self.config.timeframes)}."
                        ),
                        symbol=symbol,
                    )
                )
            elif all(t == "down" for t in per_tf_trend):
                trend_summary = "down"
                informational.append(
                    Level1Reason(
                        code="trend_aligned_down",
                        severity="info",
                        message=(
                            f"{symbol}: trend aligned DOWN on "
                            f"{', '.join(self.config.timeframes)}."
                        ),
                        symbol=symbol,
                    )
                )
            else:
                trend_summary = "mixed"
                blocking.append(
                    Level1Reason(
                        code="trend_mixed",
                        severity="block",
                        message=(
                            f"{symbol}: trend disagrees across "
                            f"{', '.join(self.config.timeframes)} "
                            f"(per-TF: {per_tf_trend}) - skipping trade."
                        ),
                        symbol=symbol,
                        metadata={"per_tf_trend": per_tf_trend},
                    )
                )
        else:
            if all(t == "up" for t in per_tf_trend):
                trend_summary = "up"
            elif all(t == "down" for t in per_tf_trend):
                trend_summary = "down"
            elif any(t == "up" for t in per_tf_trend) and any(
                t == "down" for t in per_tf_trend
            ):
                trend_summary = "mixed"
            else:
                trend_summary = "flat"

        passes = len(blocking) == 0
        return SymbolReadout(
            symbol=symbol,
            passes=passes,
            trend=trend_summary,
            rows=rows,
            blocking_reasons=blocking,
            informational_reasons=informational,
        )

    # ------------------------------------------------------------------
    # Indicator math
    # ------------------------------------------------------------------

    def _compute_indicators(
        self, df: pd.DataFrame, symbol: str, tf: str
    ) -> IndicatorRow:
        cfg = self.config
        close = df["close"].astype(float)
        high = df["high"].astype(float)
        low = df["low"].astype(float)

        ema_fast = _ema(close, cfg.ema_fast)
        ema_slow = _ema(close, cfg.ema_slow)
        rsi = _rsi(close, cfg.rsi_period)
        atr = _atr(high, low, close, cfg.atr_period)

        last_close = float(close.iloc[-1])
        last_ema_fast = float(ema_fast.iloc[-1])
        last_ema_slow = float(ema_slow.iloc[-1])
        last_rsi = float(rsi.iloc[-1])
        last_atr = float(atr.iloc[-1])
        atr_pct = (last_atr / last_close * 100.0) if last_close > 0 else 0.0

        trend: Literal["up", "down", "flat"]
        if last_close > last_ema_fast > last_ema_slow:
            trend = "up"
        elif last_close < last_ema_fast < last_ema_slow:
            trend = "down"
        else:
            trend = "flat"

        return IndicatorRow(
            symbol=symbol,
            timeframe=tf,
            close=last_close,
            ema_fast=last_ema_fast,
            ema_slow=last_ema_slow,
            rsi=last_rsi,
            atr=last_atr,
            atr_pct=atr_pct,
            trend=trend,
            last_candles=len(df),
        )

    async def _safe_klines(
        self, symbol: str, tf: str, limit: int
    ) -> KlineFetchResult | None:
        try:
            return await self.market_data.get_klines(symbol, tf, limit)
        except Exception as exc:  # noqa: BLE001 - data degrades to a block
            logger.warning(
                "Level1: Arc RPC klines fetch failed for {}@{}: {!r}",
                symbol,
                tf,
                exc,
            )
            return None

# ---------------------------------------------------------------------------
# Indicator primitives (no `ta` dep required - keep numerics in-house)
# ---------------------------------------------------------------------------


def _ema(series: pd.Series, span: int) -> pd.Series:
    return series.ewm(span=span, adjust=False, min_periods=span).mean()


def _rsi(close: pd.Series, period: int) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)
    # Wilder's smoothing == EMA with alpha = 1/period
    avg_gain = gain.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    rsi = 100.0 - (100.0 / (1.0 + rs))
    return rsi.fillna(50.0)


def _atr(
    high: pd.Series, low: pd.Series, close: pd.Series, period: int
) -> pd.Series:
    prev_close = close.shift(1)
    tr = pd.concat(
        [
            high - low,
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return tr.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()


# ---------------------------------------------------------------------------
# Serialisation
# ---------------------------------------------------------------------------


def _decision_to_dict(decision: Level1Decision) -> dict[str, Any]:
    return {
        "passes": decision.passes,
        "score": decision.score,
        "rationale": decision.rationale,
        "reasons": [_reason_to_dict(r) for r in decision.reasons],
        "per_symbol": {
            sym: {
                "symbol": ro.symbol,
                "passes": ro.passes,
                "trend": ro.trend,
                "strength": ro.strength,
                "rows": [
                    {
                        "timeframe": r.timeframe,
                        "close": r.close,
                        "ema_fast": r.ema_fast,
                        "ema_slow": r.ema_slow,
                        "rsi": r.rsi,
                        "atr": r.atr,
                        "atr_pct": r.atr_pct,
                        "trend": r.trend,
                    }
                    for r in ro.rows
                ],
                "blocking_reasons": [_reason_to_dict(r) for r in ro.blocking_reasons],
                "informational_reasons": [
                    _reason_to_dict(r) for r in ro.informational_reasons
                ],
            }
            for sym, ro in decision.per_symbol.items()
        },
        "raw": decision.raw,
    }


def _reason_to_dict(reason: Level1Reason) -> dict[str, Any]:
    return {
        "code": reason.code,
        "severity": reason.severity,
        "message": reason.message,
        "symbol": reason.symbol,
        "timeframe": reason.timeframe,
        "metadata": reason.metadata,
    }


__all__ = [
    "Level1",
    "Level1Config",
    "Level1Decision",
    "Level1Reason",
    "SymbolReadout",
    "IndicatorRow",
]
