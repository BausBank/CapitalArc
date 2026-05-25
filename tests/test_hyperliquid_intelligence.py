"""Tests for :class:`HyperliquidIntelligenceAdapter`.

Covers the pieces that matter for correctness of the L2 overlay:

* Symbol -> HL coin name mapping (including the ``-PERP`` suffix
  fallback).
* ``metaAndAssetCtxs`` parsing + per-coin ctx zip with the universe.
* Funding-rate unit conversion (hourly -> 8h, annualised).
* Funding-history helpers (closest-sample-at, weighted-avg).
* In-process OI ring buffer: empty -> ``history_unavailable``,
  warm -> correct ``delta_*_pct``.
* Per-symbol error isolation (one coin missing, others succeed).
* Whole-snapshot failure surfaces an ``error`` and zero per-symbol
  data — the L2 overlay treats this as "stay on Dune".
* Provenance map (``snapshot.sources``) is stamped with the correct
  ``hyperliquid:...`` strings.
"""

from __future__ import annotations

import time

import pytest

from src.data.hyperliquid_intelligence import (
    HLAdapterConfig,
    HyperliquidIntelligenceAdapter,
    _funding_rate_at,
    _oi_delta_pct,
    _weighted_avg_funding,
    hl_coin_for_symbol,
)


# ---------------------------------------------------------------------------
# Fake SDK Info client
# ---------------------------------------------------------------------------


class _FakeInfo:
    """Stand-in for the Hyperliquid SDK's ``Info`` class.

    Captures call counts so tests can assert "we made exactly one
    snapshot call" and "we fanned out funding_history per coin".
    """

    def __init__(
        self,
        *,
        meta_ctxs_payload=None,
        funding_history_payload=None,
        meta_ctxs_exc: Exception | None = None,
        funding_history_exc: Exception | None = None,
    ) -> None:
        self._meta_ctxs_payload = meta_ctxs_payload
        self._funding_history_payload = funding_history_payload or {}
        self._meta_ctxs_exc = meta_ctxs_exc
        self._funding_history_exc = funding_history_exc
        self.meta_ctxs_calls = 0
        self.funding_history_calls: list[tuple[str, int, int]] = []

    def meta_and_asset_ctxs(self):
        self.meta_ctxs_calls += 1
        if self._meta_ctxs_exc is not None:
            raise self._meta_ctxs_exc
        return self._meta_ctxs_payload

    def funding_history(self, coin, start_ms, end_ms):
        self.funding_history_calls.append((coin, start_ms, end_ms))
        if self._funding_history_exc is not None:
            raise self._funding_history_exc
        # Return the per-coin payload, or an empty list if not seeded.
        return self._funding_history_payload.get(coin, [])


def _meta_ctxs(*coins: tuple[str, dict]) -> list:
    """Helper: build a fake ``[meta, ctxs]`` payload from coin specs."""
    universe = [{"name": name, "szDecimals": 5} for name, _ in coins]
    ctxs = [ctx for _, ctx in coins]
    return [{"universe": universe}, ctxs]


# ---------------------------------------------------------------------------
# Symbol mapping
# ---------------------------------------------------------------------------


def test_hl_coin_for_symbol_known_perp():
    assert hl_coin_for_symbol("BTC-PERP") == "BTC"
    assert hl_coin_for_symbol("ETH-PERP") == "ETH"
    assert hl_coin_for_symbol("SOL-PERP") == "SOL"


def test_hl_coin_for_symbol_unknown_perp_fallback():
    # Operator-added symbol that's not in the explicit map: strip
    # `-PERP` and try the bare coin name.
    assert hl_coin_for_symbol("ARB-PERP") == "ARB"


def test_hl_coin_for_symbol_unmappable():
    assert hl_coin_for_symbol("RANDOM") is None
    assert hl_coin_for_symbol("") is None


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def test_funding_rate_at_returns_closest_older_sample():
    now_ms = int(time.time() * 1000)
    hist = [
        {"time": now_ms - 9 * 3600 * 1000, "fundingRate": "0.0001"},
        {"time": now_ms - 8 * 3600 * 1000, "fundingRate": "0.0002"},
        {"time": now_ms - 1 * 3600 * 1000, "fundingRate": "0.0003"},
    ]
    # 8 hours ago — best match is the 8h-old sample (0.0002).
    assert _funding_rate_at(hist, hours_ago=8) == pytest.approx(0.0002)


def test_funding_rate_at_empty_returns_zero():
    assert _funding_rate_at([], hours_ago=8) == 0.0


def test_weighted_avg_funding_emphasises_decisive_rates():
    # Weighted by |rate|: the decisive negative samples dominate.
    hist = [
        {"fundingRate": "0.00001"},
        {"fundingRate": "-0.001"},
        {"fundingRate": "-0.002"},
    ]
    avg = _weighted_avg_funding(hist)
    # Numer = 0.00001*1e-5 + 1e-3*-1e-3 + 2e-3*-2e-3 ≈ -5e-6
    # Denom = 1e-5 + 1e-3 + 2e-3 ≈ 3.01e-3
    # avg ≈ -1.66e-3 -> negative, dominated by the -0.002 sample.
    assert avg < 0
    assert avg < -0.001


def test_oi_delta_pct_empty_buffer_returns_unavailable():
    from collections import deque

    delta, unavailable = _oi_delta_pct(
        deque(), now_ts=time.time(), hours_ago=1
    )
    assert unavailable is True
    assert delta == 0.0


def test_oi_delta_pct_warm_buffer_computes_delta():
    from collections import deque

    now = time.time()
    buf: "deque[tuple[float, float]]" = deque(
        [
            (now - 3700, 1_000_000.0),  # 1h+ ago
            (now - 100, 1_100_000.0),   # current-ish
        ]
    )
    delta, unavailable = _oi_delta_pct(buf, now_ts=now, hours_ago=1)
    assert unavailable is False
    # (1_100_000 - 1_000_000) / 1_000_000 * 100 = 10.0
    assert delta == pytest.approx(10.0)


def test_oi_delta_pct_no_old_enough_sample_returns_unavailable():
    from collections import deque

    now = time.time()
    # Only have a 30-minute-old sample — can't compute 1h delta yet.
    buf: "deque[tuple[float, float]]" = deque(
        [(now - 1800, 1_000_000.0)]
    )
    delta, unavailable = _oi_delta_pct(buf, now_ts=now, hours_ago=1)
    assert unavailable is True
    assert delta == 0.0


# ---------------------------------------------------------------------------
# Adapter — happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_adapter_disabled_when_info_client_none():
    adapter = HyperliquidIntelligenceAdapter(None)
    assert adapter.enabled is False
    snap = await adapter.fetch_snapshot(["BTC-PERP"])
    assert snap.error is not None
    assert snap.per_symbol["BTC-PERP"].error == "info_client not configured"


@pytest.mark.asyncio
async def test_adapter_happy_path_produces_real_perp_metrics():
    now_ms = int(time.time() * 1000)
    fake = _FakeInfo(
        meta_ctxs_payload=_meta_ctxs(
            (
                "BTC",
                {
                    "funding": "0.0000125",       # per-hour
                    "openInterest": "1000.5",     # BTC units
                    "markPx": "70000",
                    "midPx": "70001",
                    "oraclePx": "69990",
                    "premium": "0.000142",        # fraction
                    "dayNtlVlm": "1500000000",    # $1.5B
                    "prevDayPx": "69000",
                },
            ),
            (
                "ETH",
                {
                    "funding": "-0.00002",
                    "openInterest": "20000",
                    "markPx": "3500",
                    "midPx": "3501",
                    "oraclePx": "3500.5",
                    "premium": "-0.00005",
                    "dayNtlVlm": "800000000",
                    "prevDayPx": "3550",
                },
            ),
        ),
        funding_history_payload={
            "BTC": [
                {"time": now_ms - 8 * 3600 * 1000, "fundingRate": "0.0000100"},
                {"time": now_ms - 1 * 3600 * 1000, "fundingRate": "0.0000125"},
            ],
            "ETH": [
                {"time": now_ms - 1 * 3600 * 1000, "fundingRate": "-0.00002"},
            ],
        },
    )
    adapter = HyperliquidIntelligenceAdapter(
        fake, api_url="https://api.hyperliquid.xyz"
    )
    snap = await adapter.fetch_snapshot(["BTC-PERP", "ETH-PERP"])

    assert snap.error is None
    assert fake.meta_ctxs_calls == 1
    # fan-out: one funding_history call per coin
    assert sorted(c for c, _, _ in fake.funding_history_calls) == ["BTC", "ETH"]

    btc = snap.per_symbol["BTC-PERP"]
    assert btc.error is None
    assert btc.coin == "BTC"
    # Funding: hourly -> 8h conversion
    assert btc.funding.current_rate_hourly == pytest.approx(0.0000125)
    assert btc.funding.current_rate_8h == pytest.approx(0.0000125 * 8)
    assert btc.funding.annualised_pct == pytest.approx(
        0.0000125 * 24 * 365 * 100
    )
    # OI in USD = 1000.5 * 70000
    assert btc.open_interest.current_contracts == pytest.approx(1000.5)
    assert btc.open_interest.current_notional_usd == pytest.approx(
        1000.5 * 70000
    )
    # Volume metrics
    assert btc.volume.mark_price == pytest.approx(70000)
    assert btc.volume.volume_24h_usd == pytest.approx(1_500_000_000)
    # price_change_24h = (70000-69000)/69000*100 ~= 1.449%
    assert btc.volume.price_change_pct_24h == pytest.approx(
        (70000 - 69000) / 69000 * 100
    )

    eth = snap.per_symbol["ETH-PERP"]
    assert eth.error is None
    # Negative funding -> shorts paid > 0
    assert eth.funding.current_rate_8h < 0


@pytest.mark.asyncio
async def test_adapter_stamps_provenance_sources():
    fake = _FakeInfo(
        meta_ctxs_payload=_meta_ctxs(
            ("BTC", {"funding": "0", "openInterest": "0", "markPx": "0",
                     "midPx": "0", "oraclePx": "0", "premium": "0",
                     "dayNtlVlm": "0", "prevDayPx": "0"}),
        ),
    )
    adapter = HyperliquidIntelligenceAdapter(fake)
    snap = await adapter.fetch_snapshot(["BTC-PERP"])
    # Provenance map populated even when values are zero (the source
    # exists and is real, just the market is quiet). Keys must match
    # the Level 2 fetches-dict names so the overlay REPLACES the
    # Dune provenance row instead of duplicating it.
    assert snap.sources["funding_rates"].startswith("hyperliquid:")
    assert snap.sources["open_interest"].startswith("hyperliquid:")
    assert snap.sources["volume"].startswith("hyperliquid:")
    assert snap.sources["cum_funding"].startswith("hyperliquid:")
    # We MUST NOT use the bare ``funding`` key any more — would
    # produce a duplicate row alongside Dune's ``funding_rates``.
    assert "funding" not in snap.sources


# ---------------------------------------------------------------------------
# Adapter — error isolation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_adapter_whole_snapshot_failure_isolated():
    fake = _FakeInfo(
        meta_ctxs_exc=RuntimeError("HL Info unreachable")
    )
    adapter = HyperliquidIntelligenceAdapter(fake)
    snap = await adapter.fetch_snapshot(["BTC-PERP", "ETH-PERP"])
    assert snap.error is not None
    assert "HL Info unreachable" in snap.error
    # Both symbols carry the error so the L2 overlay knows to keep
    # the Dune proxy in place for both.
    assert snap.per_symbol["BTC-PERP"].error is not None
    assert snap.per_symbol["ETH-PERP"].error is not None
    # No provenance was stamped — L2 stays fully on Dune.
    assert snap.sources == {}


@pytest.mark.asyncio
async def test_adapter_per_symbol_error_isolation():
    """One symbol missing from HL universe doesn't poison the others."""
    fake = _FakeInfo(
        meta_ctxs_payload=_meta_ctxs(
            (
                "BTC",
                {
                    "funding": "0.00001",
                    "openInterest": "100",
                    "markPx": "70000",
                    "midPx": "70000",
                    "oraclePx": "70000",
                    "premium": "0",
                    "dayNtlVlm": "1",
                    "prevDayPx": "70000",
                },
            ),
            # No ETH on this venue — only BTC is in the universe.
        ),
    )
    adapter = HyperliquidIntelligenceAdapter(fake)
    snap = await adapter.fetch_snapshot(["BTC-PERP", "ETH-PERP"])

    assert snap.error is None
    btc = snap.per_symbol["BTC-PERP"]
    eth = snap.per_symbol["ETH-PERP"]
    assert btc.error is None
    assert btc.coin == "BTC"
    assert eth.error is not None and "not in HL universe" in eth.error
    # Provenance still publishes — the L2 overlay applies HL where it
    # can and leaves Dune in place for the missing slot.
    assert snap.sources != {}


@pytest.mark.asyncio
async def test_adapter_unmappable_symbol_records_error():
    fake = _FakeInfo(meta_ctxs_payload=_meta_ctxs())
    adapter = HyperliquidIntelligenceAdapter(fake)
    snap = await adapter.fetch_snapshot(["RANDOM"])
    assert snap.per_symbol["RANDOM"].error is not None
    assert "no Hyperliquid coin mapping" in snap.per_symbol["RANDOM"].error


@pytest.mark.asyncio
async def test_adapter_funding_history_per_coin_failure_isolated():
    """Per-coin funding_history failure leaves snapshot OTHERWISE valid."""
    fake = _FakeInfo(
        meta_ctxs_payload=_meta_ctxs(
            (
                "BTC",
                {
                    "funding": "0.00001",
                    "openInterest": "100",
                    "markPx": "70000",
                    "midPx": "70000",
                    "oraclePx": "70000",
                    "premium": "0",
                    "dayNtlVlm": "1",
                    "prevDayPx": "70000",
                },
            ),
        ),
        funding_history_exc=TimeoutError("funding hist timeout"),
    )
    adapter = HyperliquidIntelligenceAdapter(fake)
    snap = await adapter.fetch_snapshot(["BTC-PERP"])

    btc = snap.per_symbol["BTC-PERP"]
    assert btc.error is None  # ctx data still valid
    # rate_8h_change collapses to zero (history empty) but
    # current_rate is still real from the ctx.
    assert btc.funding.current_rate_hourly == pytest.approx(0.00001)
    assert btc.funding.rate_8h_change == 0.0
    assert btc.cum_funding.samples == 0


# ---------------------------------------------------------------------------
# Adapter — OI ring buffer / warm-up behaviour
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_adapter_oi_history_warming_up_on_first_fetch():
    """First fetch: no old samples yet -> history_unavailable=True."""
    fake = _FakeInfo(
        meta_ctxs_payload=_meta_ctxs(
            (
                "BTC",
                {
                    "funding": "0",
                    "openInterest": "1000",
                    "markPx": "70000",
                    "midPx": "70000",
                    "oraclePx": "70000",
                    "premium": "0",
                    "dayNtlVlm": "0",
                    "prevDayPx": "70000",
                },
            ),
        ),
    )
    adapter = HyperliquidIntelligenceAdapter(fake)
    snap = await adapter.fetch_snapshot(["BTC-PERP"])
    btc = snap.per_symbol["BTC-PERP"]
    assert btc.open_interest.history_unavailable is True
    assert btc.open_interest.delta_1h_pct == 0.0
    # Current OI is still real even when deltas are warming.
    assert btc.open_interest.current_notional_usd == pytest.approx(
        1000 * 70000
    )


@pytest.mark.asyncio
async def test_adapter_oi_history_warm_after_manual_seed():
    """Seed the cache by hand to simulate a 1h-warm buffer."""
    fake = _FakeInfo(
        meta_ctxs_payload=_meta_ctxs(
            (
                "BTC",
                {
                    "funding": "0",
                    "openInterest": "1100",        # +10% vs seeded prior
                    "markPx": "70000",
                    "midPx": "70000",
                    "oraclePx": "70000",
                    "premium": "0",
                    "dayNtlVlm": "0",
                    "prevDayPx": "70000",
                },
            ),
        ),
    )
    from collections import deque

    adapter = HyperliquidIntelligenceAdapter(fake)
    # Seed BTC ring buffer with a sample from 1.5h ago: 1000 BTC * 70000 USD.
    now = time.time()
    adapter._oi_history["BTC"] = deque(
        [(now - 5400.0, 1000.0 * 70000.0)],
        maxlen=adapter.config.oi_cache_max_samples,
    )

    snap = await adapter.fetch_snapshot(["BTC-PERP"])
    btc = snap.per_symbol["BTC-PERP"]
    # 1h delta: 1100*70000 vs 1000*70000 = +10%. This is REAL even
    # though the 4h / 24h horizons are still warming up (history is
    # only 1.5h deep), which is exactly the partial-warm state we
    # want to verify here.
    assert btc.open_interest.delta_1h_pct == pytest.approx(10.0)
    assert btc.open_interest.delta_4h_pct == 0.0
    assert btc.open_interest.delta_24h_pct == 0.0
    # The flag is "at least one horizon is warming" - so True is the
    # honest answer even when 1h is valid.
    assert btc.open_interest.history_unavailable is True


# ---------------------------------------------------------------------------
# Adapter config
# ---------------------------------------------------------------------------


def test_adapter_config_defaults_are_sane():
    config = HLAdapterConfig()
    assert config.funding_window_hours == 24
    assert config.oi_cache_max_samples >= 200
    assert config.cycle_timeout_seconds > 0
