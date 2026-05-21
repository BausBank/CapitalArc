# CapitalArc — Project Context

**Project:** Autonomous on-chain agent, Agora Hackathon (RFB-04: Adaptive Portfolio Manager)
**Stack:** Python 3.10, Dune MCP REST API, Circle DCW + Paymaster, Arc Perp DEX
**Branch:** main | Last commit: 3cc2acb

## Current status: Day 3 in progress (wrapping up L2)
- L1 (TA hard rules, Dune OHLCV) ✅
- L2 (on-chain intelligence, Dune MCP) ✅ — all 8 metric queries live on Dune,
  every metric in the L2 panel shows `dune:<id>` provenance with real rows
- L3 (Gemini 2.5 Flash arbiter) — synthetic stub (deterministic re-weight of
  L1 + L2); real Gemini wiring is the Day-4 task
- Execution (Circle DCW margin moves) ✅

## Architecture rules (never break)
- Dune MCP is the ONLY market data source — no CEX, no Arc RPC for market data
- Arc RPC used ONLY for account state (wallet balance, vault TVL)
- L1 blocks → L2/L3 skipped, final_score=0.0 (cascade, not average)
- Every metric must carry provenance: `dune:<query_id>` | `n/a` | `error`
- All `DUNE_QUERY_*_ID` values live ONLY in the top `.env` block (lines 72–86).
  Duplicate empty keys lower in the file silently override real ones — the
  detector in `src/utils/config.py::_warn_duplicate_env_keys` logs an
  `ERROR` at startup if a duplicate sneaks back in.

## Dune query IDs — current wiring (9/9 live)

| Level | Metric            | Env var                          | Query ID |
|-------|-------------------|----------------------------------|----------|
| L1    | ohlcv             | `DUNE_QUERY_OHLCV_ID`            | 7546473  |
| L2    | market_sentiment  | `DUNE_QUERY_MARKET_SENTIMENT_ID` | 7547421  |
| L2    | volume            | `DUNE_QUERY_VOLUME_ID`           | 7548324  |
| L2    | funding_rates     | `DUNE_QUERY_FUNDING_RATES_ID`    | 7548438  |
| L2    | open_interest     | `DUNE_QUERY_OPEN_INTEREST_ID`    | 7552510  |
| L2    | vault_flows       | `DUNE_QUERY_VAULT_FLOWS_ID`      | 7552541  |
| L2    | whale_activity    | `DUNE_QUERY_WHALE_ACTIVITY_ID`   | 7552613  |
| L2    | long_short_ratio  | `DUNE_QUERY_LONG_SHORT_RATIO_ID` | 7552641  |
| L2    | cum_funding       | `DUNE_QUERY_CUM_FUNDING_ID`      | 7552648  |

Startup logs print `Loaded <metric> query ID = <id>` for every loaded one,
so any drift between `.env` and what Python actually sees is visible
immediately — no need to grep.

## L2 implementation notes (current)
- Market heat: branches on whether Dune returned a row (NOT on `heat is null`).
  If the row exists, the Dune value wins; `null` collapses to neutral 0.5
  via SQL-side `COALESCE`. Heuristic is only used when the query
  isn't configured at all. Log: `Market heat from Dune = X.XXX (query <id>)`
  vs `Market heat from heuristic = X.XXX (...)`.
- Heat source is exposed in `Level2Intelligence.heat_source` and rendered
  in the Level 2 panel: `0.523 [dune:7547421]` (green) vs
  `0.500 [heuristic (fallback)]` (yellow).
- Whale snapshot now carries `n_whales` (count of whale trades above
  `DUNE_WHALE_MIN_USD`); the L2 panel has a `Count` column for it.
- All 8 L2 SQL templates follow the "gold standard" style:
  `WITH symbols AS (...) UNION ALL`, `INNER JOIN ... ON ... OR ...`,
  `LEFT JOIN agg AS a ON s.symbol = a.symbol`, every numeric column
  wrapped in `COALESCE(..., 0)`, `ORDER BY s.symbol`.

## SQL gotchas baked into `dune/queries/`
- `erc20_<chain>.evt_Transfer` columns `from` / `to` / `contract_address`
  are all **`varbinary`** in Trino. Trino does NOT implicit-cast
  `varbinary <-> varchar`. Every comparison wraps the column in
  `lower(CAST(... AS varchar))`. `from` is also a Trino reserved keyword,
  so it must be double-quoted: `lower(CAST("from" AS varchar))`.
- `dex.trades` columns `token_bought_address` / `token_sold_address` are
  also `varbinary` — same `lower(CAST(... AS varchar))` pattern.
- When `per_symbol` / aggregation CTE is empty, `AVG()` returns NULL and
  the whole expression collapses. Wrap final scalar outputs in
  `COALESCE(..., 0.5 or 0)` so the Python layer never falls back to a
  heuristic just because the trade window happened to be empty.
- If a saved Dune query was last saved with a different parameter set
  than the local SQL template declares, Dune returns HTTP 400
  `unknown parameters (...)`. `DuneMCPClient._submit_execution` does
  one transparent retry without the rejected names and logs a `WARNING`.
  Safe but doubles HTTP traffic per cycle — fix by re-saving the query
  on Dune with the actual local template.

## Key files (current map)
- `src/core/level1.py`, `src/core/level2.py`, `src/core/level3.py`,
  `src/core/decision_engine.py` — the three levels + cascade aggregator
- `src/data/dune_market_data.py` — L1 OHLCV adapter on top of Dune MCP
- `src/data/dune_mcp.py` — REST client + lenient retry for unknown params
- `src/utils/config.py` — pydantic Settings + `.env` duplicate-key detector
- `src/utils/console.py` — Rich panels (Level 1 / Level 2 / Final Decision /
  Execution Plan / On-chain Result / Market Context)
- `dune/queries/*.sql` — 9 templates (1 for L1 OHLCV, 8 for L2 metrics);
  see `dune/README.md` for the save-to-Dune workflow
- `.env` — top block (lines 72–86) is the single source of truth for
  every `DUNE_QUERY_*_ID`

## What's next (Day 4)
1. Wire real Gemini 2.5 Flash in `src/core/level3.py` + `src/llm/gemini_client.py`
   — replace the deterministic synthetic re-weight with a real
   `ArbiterBriefing → Gemini → {score, regime, rationale}` round-trip
   (strict JSON, low temperature).
2. EIP-712 order signing for `open_position` in
   `src/execution/arc_perp_executor.py` so the agent can actually open
   leveraged longs on Arc Perp DEX (currently only margin moves are live).
3. USYC rotation on risk-off signal in `src/allocation/allocation_router.py`
   (rotate idle USDC → USYC when `final_score <= RISK_OFF_THRESHOLD`).
4. JSONL decision log for observability (one line per cycle: inputs,
   per-level scores, final score, directive, tx hashes).
