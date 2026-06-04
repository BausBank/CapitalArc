"""Market-data layer for the backtester (Stage 1).

Fetches real Hyperliquid mainnet 1h OHLCV + funding history from the public
``/info`` endpoint, caches the raw blob to disk, and builds the pandas
frames the harness drives Level 1 / Level 2 off.

Stage-1 changes vs the throwaway script:
* Default lookback extended to **365 days** (DoD: 6-12 months of 1h). HL's
  ``candleSnapshot`` only retains ~52d of 15m candles, so the backtest runs
  off the 1h tape (the agent's primary decision timeframe) and 15m is no
  longer fetched - it was never used downstream and only bloated the cache.
* Cache filename encodes the lookback so 90d and 365d blobs coexist.
"""

from __future__ import annotations

import json
import os
import time
import urllib.request
from typing import Any

import pandas as pd

HL_API = "https://api.hyperliquid.xyz/info"
# core/backtest/data.py -> project root is two levels up
_PROJECT_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)
DATA_DIR = os.path.join(_PROJECT_ROOT, "backtest_data")

SYMBOLS = ["BTC-PERP", "ETH-PERP"]
COIN_OF = {"BTC-PERP": "BTC", "ETH-PERP": "ETH"}
LOOKBACK_DAYS = 365


def _post(payload: dict[str, Any], retries: int = 4) -> Any:
    data = json.dumps(payload).encode()
    last_exc: Exception | None = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(
                HL_API, data=data, headers={"Content-Type": "application/json"}
            )
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.load(r)
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"HL request failed after {retries} tries: {last_exc!r}")


def _fetch_candles(coin: str, interval: str, start_ms: int, end_ms: int) -> list[dict]:
    """Fetch candles, paginating in windows sized below the 5000-row cap."""
    out: list[dict] = []
    step_ms = {"15m": 40 * 24 * 3600 * 1000, "1h": 180 * 24 * 3600 * 1000}[interval]
    cur = start_ms
    while cur < end_ms:
        chunk_end = min(cur + step_ms, end_ms)
        rows = _post(
            {
                "type": "candleSnapshot",
                "req": {
                    "coin": coin,
                    "interval": interval,
                    "startTime": cur,
                    "endTime": chunk_end,
                },
            }
        )
        if rows:
            out.extend(rows)
        cur = chunk_end
    seen: dict[int, dict] = {}
    for c in out:
        seen[int(c["t"])] = c
    return [seen[k] for k in sorted(seen)]


def _fetch_funding(coin: str, start_ms: int, end_ms: int) -> list[dict]:
    """Funding history (capped at 500 rows/request -> paginate forward)."""
    out: list[dict] = []
    cur = start_ms
    while cur < end_ms:
        rows = _post(
            {"type": "fundingHistory", "coin": coin, "startTime": cur, "endTime": end_ms}
        )
        if not rows:
            break
        out.extend(rows)
        last_t = int(rows[-1]["time"])
        if len(rows) < 500 or last_t <= cur:
            break
        cur = last_t + 1
    seen: dict[int, dict] = {}
    for f in out:
        seen[int(f["time"])] = f
    return [seen[k] for k in sorted(seen)]


def load_market_data(refresh: bool = False, lookback_days: int = LOOKBACK_DAYS) -> dict[str, Any]:
    """Load (or fetch + cache) 1h candles + funding for both perps.

    HL may return fewer 1h bars than requested for a 12-month window; the
    harness honours whatever depth comes back (it trims a warmup prefix and
    reports the realised period).
    """
    os.makedirs(DATA_DIR, exist_ok=True)
    cache = os.path.join(DATA_DIR, f"hl_{lookback_days}d.json")
    if os.path.exists(cache) and not refresh:
        with open(cache, "r", encoding="utf-8") as fh:
            print(f"[OK] Loaded cached market data: {cache}")
            return json.load(fh)

    now = int(time.time() * 1000)
    start = now - lookback_days * 24 * 3600 * 1000
    blob: dict[str, Any] = {"fetched_at": now, "start": start, "end": now, "data": {}}
    for sym, coin in COIN_OF.items():
        print(f"[>>>] Fetching {coin} 1h candles + funding ({lookback_days}d) ...")
        c1h = _fetch_candles(coin, "1h", start, now)
        fund = _fetch_funding(coin, start, now)
        print(f"      {coin}: 1h={len(c1h)} funding={len(fund)}")
        blob["data"][sym] = {"1h": c1h, "funding": fund}
    with open(cache, "w", encoding="utf-8") as fh:
        json.dump(blob, fh)
    print(f"[OK] Cached market data -> {cache}")
    return blob


def candles_to_df(rows: list[dict]) -> pd.DataFrame:
    recs = []
    for c in rows:
        recs.append(
            {
                "ts": int(c["t"]) // 1000,
                "open": float(c["o"]),
                "high": float(c["h"]),
                "low": float(c["l"]),
                "close": float(c["c"]),
                "volume": float(c["v"]),
                "fills": int(c.get("n", 0)),
            }
        )
    df = pd.DataFrame(recs).sort_values("ts")
    df = df[~df["ts"].duplicated(keep="last")]
    df["close_time"] = pd.to_datetime(df["ts"].astype("int64"), unit="s", utc=True)
    df.set_index("close_time", inplace=True)
    return df[["open", "high", "low", "close", "volume", "fills"]]


def build_frames(
    blob: dict[str, Any],
) -> tuple[dict[tuple[str, str], pd.DataFrame], dict[str, pd.DataFrame]]:
    """Turn the cached blob into 1h OHLCV frames + funding frames."""
    frames: dict[tuple[str, str], pd.DataFrame] = {}
    funding: dict[str, pd.DataFrame] = {}
    for sym in SYMBOLS:
        sym_blob = blob["data"][sym]
        frames[(sym, "1h")] = candles_to_df(sym_blob["1h"])
        frows = sym_blob.get("funding") or []
        if frows:
            fdf = pd.DataFrame(
                [
                    {"ts": int(r["time"]) // 1000, "fundingRate": float(r["fundingRate"])}
                    for r in frows
                ]
            ).sort_values("ts")
            fdf["t"] = pd.to_datetime(fdf["ts"].astype("int64"), unit="s", utc=True)
            fdf.set_index("t", inplace=True)
            funding[sym] = fdf[["fundingRate"]]
    return frames, funding


__all__ = [
    "SYMBOLS",
    "COIN_OF",
    "LOOKBACK_DAYS",
    "DATA_DIR",
    "load_market_data",
    "candles_to_df",
    "build_frames",
]
