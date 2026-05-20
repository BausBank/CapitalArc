# AGENTS.md - CapitalArc Internal Agent Design

This document describes the internal agent and decision-engine design for **CapitalArc**.
It is the source of truth for *how* the agent thinks, not just *what* it does.

---

## 1. High-level model

CapitalArc is a single autonomous agent with a **3-level decision engine** feeding into an **allocation router**.

```
                +--------------------------------------------------+
                |               Decision Engine                    |
                |  +--------+  +--------+  +----------------------+ |
   Market  ---> |  | L1 TA  |  | L2 OnC |  | L3 Gemini 2.5 Flash  | | ---> Risk score in [0,1]
   Data        |  | rules  |  | (Dune) |  |    (final arbiter)   | |
                |  +--------+  +--------+  +----------------------+ |
                +--------------------------------------------------+
                                    |
                                    v
                +--------------------------------------------------+
                |             Allocation Router                    |
                |   score >= RISK_ON_THRESHOLD  -> Arc Perp DEX    |
                |   score <= RISK_OFF_THRESHOLD -> USYC            |
                |   otherwise                    -> Hold / Cash    |
                +--------------------------------------------------+
                                    |
                                    v
                +--------------------------------------------------+
                |   Execution Layer (Circle DCW + Paymaster)       |
                +--------------------------------------------------+
```

---

## 2. Decision levels

The engine evaluates levels **cascadingly**, not in parallel:

1. **L1** runs first. If L1 emits any `severity="block"` reason
   (drawdown, trend mismatch, RSI extreme, ATR outside band, no
   OHLCV), the engine **short-circuits**: `final_score = 0.0`,
   `regime = "risk-off"`, **L2 and L3 are skipped**.
2. **L2** runs only if L1 passes.
3. **L3** runs only if L1 passed and a real Gemini client is wired;
   otherwise the engine emits a synthetic deterministic L3 score so
   the weighted aggregation still has three terms.

### Level 1 - Technical hard rules ("защита от дурака")
- **Inputs:** OHLCV on `15m` and `1h` for `BTC-PERP / ETH-PERP /
  SOL-PERP`, fetched through `DuneMarketData`
  (`src/data/dune_market_data.py`) which executes the `ohlcv` saved
  Dune query (`dune/queries/ohlcv.sql`) against Dune's multichain
  `dex.trades` table. **There is no other market data path:** no CEX
  feed, no Arc-RPC scrape. If Dune returns no rows (or
  `DUNE_QUERY_OHLCV_ID` is unset), the rule returns
  `ohlcv_unavailable` and the agent refuses to trade.
- **Chain:** the Arc Testnet isn't yet indexed by Dune, so the SQL
  template targets a live high-liquidity EVM chain selectable via
  `DUNE_CHAIN` (default `ethereum`; `base` and `arbitrum` are wired
  out of the box). The agent's three symbols map to the canonical
  on-chain wraps of BTC / ETH / SOL on that chain (WBTC or cbBTC,
  WETH, Wormhole-SOL) - the token map lives in
  `src/utils/config.py::_DEFAULT_TOKEN_ADDRESSES` and is overridable
  via `DUNE_TOKEN_*` env vars.
- **Hard rules:**
  - Trend filter: `close > EMA9 > EMA21` (or mirrored down) on *both*
    timeframes; otherwise `trend_mixed` blocks.
  - RSI extreme: blocks when `RSI(14) ≥ L1_RSI_OVERBOUGHT` or
    `≤ L1_RSI_OVERSOLD` on any timeframe.
  - Volatility band: blocks when `ATR%` (ATR / price) is outside
    `[L1_ATR_PCT_MIN, L1_ATR_PCT_MAX]`.
  - Account drawdown: blocks when current drawdown breaches
    `MAX_DRAWDOWN_PCT`.
- **Output:** `Level1Decision { passes, score, rationale, reasons,
  per_symbol }` carrying every triggered rule as a structured
  `Level1Reason` so the UI can render the *why*. Each block reason
  carries Dune provenance (`source`, `query_id`, bars vs required).
  Mapped to `LevelScore` in `[0, 1]` (0.0 when blocked;
  `0.5 + 0.5 * strength` otherwise).
- **Code:** `src/core/level1.py`, OHLCV adapter in
  `src/data/dune_market_data.py`, SQL in `dune/queries/ohlcv.sql`.

### Level 2 - On-chain intelligence (Dune MCP only)
- **Single data source:** `DuneMCPClient` (`src/data/dune_mcp.py`) -
  speaks the same Bearer-authenticated surface the Dune MCP server
  exposes to LLMs (`execute_query`, `latest_results`, `ping`), plus
  a high-level `fetch_metric(name, params)` that resolves each metric
  to a saved Dune query id. **Nothing else feeds Level 2** - no CEX,
  no direct RPC reads. This is intentional: by routing every metric
  through Dune we keep the on-chain analytics path consistent,
  cacheable, and shareable as a Dune dashboard.
- **Chain:** same `DUNE_CHAIN` switch as Level 1. Every SQL template
  is chain-agnostic and parameterised by the BTC / ETH / SOL token
  addresses on that chain.
- **Per-symbol metrics:** funding (current + 8h / 24h delta +
  weighted average + annualised %), open interest (current +
  1h / 4h / 24h deltas), volume + 1h-vs-24h spike detection,
  long/short ratio with inferred bias, cumulative funding paid /
  received over the window, whale-activity flag with rationale.
  Because the underlying tape is spot (`dex.trades`), every "perp"
  metric is a clearly labelled spot-derived proxy that captures the
  same structural signal:
  - Funding rate = buy-vs-sell USD imbalance over 8h, scaled to a
    per-8h funding-equivalent (0.05% / 1.0 of imbalance).
  - Open interest = rolling-USD volume + 1h/4h/24h deltas.
  - Long/short ratio = `sum(buy_usd) / sum(sell_usd)` with unique-
    wallet counts for the account-ratio columns.
  - Cumulative funding = net signed aggressor flow scaled across the
    lookback window.
  When a real perp-DEX schema lands on Dune (Arc, Synthetix V3 perps,
  etc.) we swap the `dex.trades` source for the perp's `fills` /
  `funding_events` table and the rest of the pipeline is unchanged.
- **Vault-level metrics:** TVL + net deposits / withdrawals over the
  recent window for the address configured in `DUNE_PERP_VAULT_ADDRESS`
  (defaults to `ARC_PERP_VAULT_ADDRESS`; repoint at any vault on the
  active chain - e.g. a Synthetix V3 collateral vault on Base).
- **Aggregation:** Dune `market_sentiment` returns a `heat` in
  `[0, 1]` that Level 2 uses verbatim. When the query isn't
  configured, the engine falls back to a heuristic blend of funding,
  OI 1h delta, 24h price change and L/S. The score is bucketed into
  `risk_on` / `risk_off` / `neutral` / `transition`.
- **Provenance is first-class.** Every metric reports its source as
  `dune:<query_id>`, `n/a` (with the exact `DUNE_QUERY_*_ID` env var
  to set), or `error`. The L2 panel renders this map so demo and
  live runs are honest about what's actually on-chain.
- **SQL templates** for every metric ship in `dune/queries/` (see
  `dune/README.md` for the workflow: save them in your Dune
  workspace, paste each query id into `.env`).
- **Caching:** in `DEMO_MODE` the full `Level2Intelligence` payload
  is cached for `DEMO_CACHE_TTL_SECONDS` (default 30 min).
- **Output:** `LevelScore` in `[0, 1]` with the full intelligence
  payload exposed via `raw["l2"]` (per-symbol metrics, vault flow,
  metric_status / provenance, notes).
- **Code:** `src/core/level2.py`.

### Level 3 - Gemini 2.5 Flash (final arbiter, Day 4)
- **Role:** the *arbiter*, not just another signal. Gemini sees the
  structured outputs of L1 and L2 plus a compact market briefing,
  and emits a final regime label + score.
- **Inputs:** `ArbiterBriefing { l1_score, l1_rationale, l2_score,
  l2_rationale, market_snapshot }`.
- **Contract:** Gemini must return strict JSON: `{"score": float,
  "regime": str, "rationale": str}`.
- **Output:** `l3_score` in `[0, 1]`, plus a human-readable rationale.
- **Day-3 status:** the engine accepts a `Level3` instance; until the
  Gemini round-trip is wired on Day 4 it returns a deterministic
  synthetic re-weight of L1 + L2 so the aggregation math stays
  consistent.
- **Code:** `src/core/level3.py`, client in `src/llm/gemini_client.py`.

### Aggregation

`final_score = w1*l1_score + w2*l2_score + w3*l3_score` when L1 passes.

Short-circuit: when L1 blocks, `final_score = 0.0` and L2 / L3 carry
zero scores with `raw.skipped = True` (so the panels render the
"skipped" badge instead of a misleading 0.0).

Default weights (configurable via `.env`):

| Level | Weight |
|-------|--------|
| L1    | 0.25   |
| L2    | 0.35   |
| L3    | 0.40   |

The score is mapped to an action by the **Allocation Router**.

---

## 3. Allocation Router

| Final score          | Action                                                       |
|----------------------|--------------------------------------------------------------|
| `>= RISK_ON_THRESH`  | Open / maintain leveraged longs on Arc Perp DEX              |
| `<= RISK_OFF_THRESH` | Close perp exposure, rotate USDC into USYC for yield         |
| in between           | Hold current allocation, no rebalance, log "transition"      |

Hard overrides (router-side):
- Portfolio drawdown >= `MAX_DRAWDOWN_PCT` -> forced risk-off.
- Stale data feed (no fresh OHLCV or Dune result) -> forced cash, no new trades.

---

## 4. Execution

- **Wallet:** Circle Developer-Controlled Wallet (non-custodial, programmatic).
- **Gas:** sponsored via Circle Paymaster (gasless UX where supported).
- **Liquidity routing:** CCTP v2 for bringing USDC onto Arc and back out.
- **Trading venue:** Arc Perp DEX (router + clearing-house contracts).
- **Yield leg:** USYC mint / redeem against USDC.

All execution is idempotent: every action is keyed by an internal
`decision_id` so retries never double-trade.

---

## 5. Module map

| Folder           | Responsibility                                                  |
|------------------|-----------------------------------------------------------------|
| `src/core`       | Decision engine, Level 1-3, score aggregation                   |
| `src/data`       | `DuneMCPClient` (single source of truth for L1 + L2), `DuneMarketData` (OHLCV adapter on top of Dune), `ArcOnchainReader` (account state only - wallet / vault) |
| `src/execution`  | Arc Perp DEX client, order/position management, risk checks     |
| `src/allocation` | Risk-on / risk-off router, USYC rotation, CCTP moves            |
| `src/llm`        | Gemini 2.5 Flash client (Level 3 final arbiter)                 |
| `src/agents`     | Top-level autonomous agent loop                                 |
| `src/utils`      | Config (pydantic), logging (loguru), rich console panels        |
| `prompts/`       | LLM prompt templates used by the Level 3 arbiter                |
| `dune/`          | Dune MCP SQL templates (`dune/queries/*.sql`) + setup README    |
| `scripts/`       | One-off ops scripts (deploy wallets, simulate, backtest, seed)  |
| `tests/`         | Unit + integration tests                                        |

---

## 6. Operating principles

1. **On-chain by default.** If an action can happen on Arc through Circle primitives, it must.
2. **Dune MCP is the single source of truth.** Every market signal - OHLCV (L1) and on-chain intelligence (L2) - reads through a saved Dune query. No CEX feed, no direct RPC market-data scrape. Arc RPC is used **only** for account state (wallet balance, vault TVL, agent margin).
3. **Chain-portable, not chain-coupled.** The decision engine reads from whatever chain `DUNE_CHAIN` points at (`ethereum` / `base` / `arbitrum` shipped today). Switching is a one-line `.env` change because every SQL template is parameterised by chain + token addresses.
4. **Deterministic decisions.** Same inputs -> same score -> same action. Gemini is pinned to low temperature and strict JSON output.
5. **Safety over alpha.** Drawdown guard and stale-data guard always win over signals. L1 hard rules veto trades; they are never softened by L2 / L3.
6. **Cascade, don't average.** Levels are *gates*, not weighted blobs. If L1 says no, the engine doesn't call L2 / L3 and never produces a false-positive risk-on.
7. **Honest provenance.** Every metric (L1 OHLCV included) reports its Dune query id (or `n/a` with a clear note) so users always know whether a number is on-chain truth or a placeholder. Spot-derived "perp" proxies (funding / OI / L-S / cum funding) are labelled as such in the SQL header comments.
8. **Observable.** Every decision logs its inputs, level scores, final score, action and tx hash. The CLI renders a six-panel rich report on each cycle.
9. **Modular.** Each level is replaceable; the router does not care how a score was computed.

---

## 7. Current status

| Day | Status      | Notes                                                                              |
|-----|-------------|------------------------------------------------------------------------------------|
| 1   | ✅ Done     | Repository scaffolding, three-level stubs, secret hygiene.                         |
| 2   | ✅ Done     | Live Arc Perp DEX margin moves through Circle DCW + Paymaster + RSA encryption.    |
| 3   | ✅ Done     | Level 1 + Level 2 fully implemented with **Dune MCP as the single source of truth** on live Ethereum / Base / Arbitrum data (Arc Testnet isn't indexed by Dune yet). OHLCV via `DuneMarketData` on `dex.trades` + 8 on-chain metrics (volume / whale flows are real; funding / OI / L-S / cum funding are spot-derived proxies clearly labelled in the SQL). All metrics carry per-query provenance; SQL templates in `dune/queries/`. Chain is one-line switchable via `DUNE_CHAIN`. Cascading engine with short-circuit. Rich-panel CLI. Arc RPC kept only for account state. |
| 4   | 🚧 Planned  | Real Gemini 2.5 Flash arbiter (L3), EIP-712 order signing for `open_position`, USYC rotation on risk-off, JSONL decision log. |
