# CapitalArc

> **The On-Chain Adaptive Portfolio Manager.**
> Trade when the market is risk-on. Earn yield when it isn't. Fully autonomous, fully on-chain.

---

## Overview

**CapitalArc** is a fully on-chain AI agent that reads the market regime in real time and adapts capital allocation automatically:

- **Risk-On regime** — the agent actively trades on the **Arc Perp DEX**, opening leveraged positions based on a three-level decision engine (technicals, on-chain intelligence, LLM arbitration).
- **Risk-Off regime** — the agent protects capital by rotating into **USYC** (yield-bearing tokenized USDC), preserving value with native institutional yield while waiting for the next opportunity.

The system is built end-to-end on the **Arc** stablechain and the full **Circle** stack: CCTP for liquidity routing, Developer-Controlled Wallets for non-custodial execution, Circle Paymaster for gasless UX, and USYC for the risk-off yield leg. Market intelligence is sourced through **Dune MCP** for on-chain flows and **Gemini 2.5 Flash** as the final regime arbiter.

---

## Positioning — RFB 04: Adaptive Portfolio Manager

This project is built for the **Agora Agents Hackathon (Canteen × Circle on Arc)**, under request **RFB 04 — Adaptive Portfolio Manager**.

CapitalArc directly addresses the brief by delivering:

1. **An autonomous portfolio agent** that decides *what* to hold, *when* to trade, and *when* to step aside — without human intervention.
2. **Regime-aware allocation** between active perp trading and a safe yield-bearing asset (USYC), instead of a static strategy.
3. **A full Arc + Circle native execution stack** — every action (trade, rebalance, swap, yield rotation) happens on-chain through Circle's primitives.
4. **Multi-signal intelligence with an LLM arbiter** — fast technicals, on-chain capital flows (Dune MCP), and Gemini 2.5 Flash as the final, regime-aware judge.

---

## Architecture

### Three-level decision engine

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
```

| Level | Signal source                       | Role                                              | Weight |
|-------|-------------------------------------|---------------------------------------------------|--------|
| 1     | Price action / technicals           | Fast deterministic rules on OHLCV + funding + OI  | 0.25   |
| 2     | On-chain flows via **Dune MCP**     | Stablecoin flows, OI delta, whale and bridge data | 0.35   |
| 3     | **Gemini 2.5 Flash** final arbiter  | Regime classification + final risk score          | 0.40   |

The aggregated score drives the **Risk-On / Risk-Off** switch, which then decides whether capital sits in **Arc Perp positions** or in **USYC**.

### Repository layout

```
CapitalArc/
├── main.py           # Entry point: --dry-run / --live, optional --loop
├── src/
│   ├── core/         # DecisionEngine + Level 1/2/3 + ExecutionDirective
│   ├── data/         # DuneMCPClient + DuneMarketData (OHLCV), ArcOnchainReader
│   ├── execution/    # ArcPerpExecutor + CircleWallet (DCW + Paymaster)
│   ├── allocation/   # AllocationRouter: directive -> on-chain action
│   ├── llm/          # Gemini 2.5 Flash client (final arbiter)
│   ├── agents/       # Reserved for top-level orchestration helpers
│   └── utils/        # Settings (pydantic), logging (loguru), rich panels
├── prompts/          # LLM prompt templates for the Level 3 arbiter
├── dune/             # Dune MCP SQL templates (Level 1 + 2) + setup README
│   └── queries/      #   ohlcv.sql, funding_rates.sql, open_interest.sql, ...
├── scripts/          # One-off scripts: deploy, seed, simulate, backtest
├── tests/            # Unit & integration tests
├── .env.example      # Template for environment variables
├── requirements.txt  # Python dependencies
├── README.md
└── AGENTS.md         # Internal agent design and decision-level spec
```

---

## Tech Stack

**Chain & Infrastructure**
- **Arc** — stablechain, primary execution layer
- **Arc Perp DEX** — active trading venue (risk-on leg)

**Circle Stack**
- **CCTP v2** — cross-chain USDC transfers and liquidity routing
- **Developer-Controlled Wallets (DCW)** — non-custodial programmable wallets
- **Circle Paymaster** — gasless transactions, sponsored UX
- **USYC** — yield-bearing tokenized USDC (risk-off leg)

**Intelligence & Data**
- **Dune MCP** — the **single source of truth** for both Level 1 (OHLCV / TA) and Level 2 (on-chain intelligence). Queries currently target a live, high-liquidity EVM chain (`DUNE_CHAIN=ethereum` by default; `base` / `arbitrum` selectable) because Arc Testnet isn't indexed by Dune yet.
- **Arc RPC** — used **only** for non-trading account state (wallet, vault TVL, agent margin); never feeds trading signals
- **Gemini 2.5 Flash** (Google AI Studio) — LLM final arbiter (Level 3)

**Backend**
- **Python 3.11+**
- `web3.py`, `eth-account` — chain interaction
- `httpx`, `aiohttp` — async HTTP
- `pydantic`, `pydantic-settings` — typed configuration and schemas
- `pandas`, `numpy`, `ta` — Level 1 signal processing
- `mcp`, `dune-client` — Level 2 on-chain data
- `google-generativeai` — Level 3 Gemini client
- `apscheduler` — strategy scheduling loop
- `loguru` — structured logging

---

## Current Status

### Day 1 — Completed

- Repository initialised, `.gitignore` and `.env.example` in place
- Three-level architecture (L1 technical rules, L2 Dune MCP, L3 Gemini 2.5 Flash) reflected in code structure
- Stub classes with stable public interfaces:
  - `DecisionEngine`, `Level1`, `Level2`, `Level3` in `src/core/`
  - `GeminiClient` in `src/llm/`
- Secrets cleanly separated: real keys in local `.env`, only placeholders in `.env.example`
- Twitter sentiment and xAI/Grok layers fully removed
- `AGENTS.md` updated to the 3-level model

### Day 2 — Completed

The agent can now move real capital on Arc Testnet through Circle DCW.

- **`ArcPerpExecutor`** (`src/execution/arc_perp_executor.py`) — wired
  against the real Arc Perp DEX stack on Arc Testnet (verified contracts
  deployed by `0x880bd26A...`): `ClearingHouse 0x70a06946…`,
  `USDCCollateralVault 0x75E4FBFB…`, `MarketRegistry 0x9cED23e4…`,
  `PositionLedger 0xd6D77291…`. The venue is order-book + on-chain batch
  settlement (dYdX v3 style); margin moves are pure on-chain and live
  today, EIP-712 order signing lands on Day 3 once the matcher URL is
  available.
- **Live margin moves**: `deposit_margin` / `withdraw_margin` execute real
  `USDCCollateralVault.deposit / withdraw` transactions through Circle
  DCW. `get_margin` / `get_position` read on-chain state via `web3.py`
  against the public Arc Testnet RPC.
- **`CircleWallet`** (`src/execution/circle_wallet.py`) — real
  **RSA-OAEP-SHA256** encryption of the entity secret with Circle's
  cached public key (fresh ciphertext per request, as Circle requires).
  Circle **Paymaster** / Gas Station policy is wired via `gasPolicyId`
  on the contractExecution body; `TxResult.sponsored` flips to `True`
  when a policy is configured.
- **`AllocationRouter`** — turns the `DecisionResult` into `open_position`
  or `close_position`; in live mode, if the off-chain matcher URL isn't
  set, it gracefully degrades to a margin-deposit so capital still lands
  on the perp venue.
- **`main.py`** — `--dry-run` (default), `--live` (with strict pre-flight
  checks: refuses to start unless `CIRCLE_API_KEY`, `CIRCLE_ENTITY_SECRET`,
  `CIRCLE_AGENT_WALLET_ID`, `ARC_PERP_ROUTER_ADDRESS` and
  `ARC_PERP_VAULT_ADDRESS` are present), `--loop` for repeated cycles,
  Arcscan explorer links printed for every tx, automatic polling of
  Circle tx state to a terminal status.
- **Day-1 setup script** (`scripts/create_circle_wallet.py`) — already
  creates a named Circle DCW wallet on `ARC-TESTNET`.

**Default mode remains `--dry-run`**, which logs the exact Circle DCW
`contractExecution` payload (including Paymaster hints) without touching
the chain. Flip to `--live` once the Arc Testnet wallet is funded with
USDC.

### Day 3 — Completed

Level 1 (technical hard rules) and Level 2 (on-chain intelligence)
are both fully wired and feed a cascading `DecisionEngine`. By design
the agent has exactly **one market-data source**: **Dune MCP**. Both
Level 1 (OHLCV / TA) and Level 2 (funding, OI, volume, vault flows,
whales, L/S, cum funding, market sentiment) read through saved Dune
queries. Arc RPC is still used, but **only** for non-trading account
state (wallet, vault TVL, agent margin) - it never feeds the
decision engine.

> **Data path:** Arc Testnet is not yet indexed by Dune Analytics, so
> the saved queries point at a live, high-liquidity EVM chain
> (`DUNE_CHAIN=ethereum` by default; `base` and `arbitrum` are wired
> out of the box). The agent's symbols (`BTC-PERP / ETH-PERP /
> SOL-PERP`) map to the canonical on-chain wraps of BTC / ETH / SOL
> on that chain (WBTC or cbBTC, WETH, Wormhole-SOL). Switching chains
> is a one-line `.env` change because every SQL template is
> parameterised by chain + token addresses. When Arc lands on Dune,
> flip `DUNE_CHAIN=arc` and the rest of the pipeline is unchanged.

- **Level 1 — Technical hard rules** (`src/core/level1.py`)
  - OHLCV for **BTC-PERP / ETH-PERP / SOL-PERP** on `15m` and `1h`
    is reconstructed from Dune's multichain `dex.trades` table
    through the **`DuneMarketData`** adapter
    (`src/data/dune_market_data.py`), which wraps `DuneMCPClient`
    and runs the `ohlcv` saved query (`dune/queries/ohlcv.sql`).
    The adapter executes one Dune call per cycle for *every*
    `(symbol, interval)` combination, then fan-outs the rows to
    per-symbol pandas DataFrames so the indicator stack stays
    unchanged.
  - When `DUNE_QUERY_OHLCV_ID` isn't configured (or the query
    returns no rows), Level 1 emits an `ohlcv_unavailable` block
    that explicitly cites the Dune source (`source=n/a / dune:<id>`,
    bars returned vs required) - never invents data.
  - Four hard rules ("защита от дурака") that can each veto a trade:
    1. **Trend filter** — `close > EMA9 > EMA21` (or mirror down) must
       hold on *both* 15m and 1h; otherwise `trend_mixed` blocks.
    2. **RSI extreme** — `RSI(14) ≥ L1_RSI_OVERBOUGHT` (default 70) or
       `≤ L1_RSI_OVERSOLD` (default 30) blocks the trade.
    3. **Volatility band** — `ATR%` (ATR / price) must lie inside
       `[L1_ATR_PCT_MIN, L1_ATR_PCT_MAX]` (defaults 0.15% / 6.00%).
    4. **Account drawdown** — current `unrealized_pnl / margin`
       breaching `MAX_DRAWDOWN_PCT` forces a flat outcome.
  - `Level1Decision` carries a structured list of `Level1Reason`s
    (`code`, `severity`, `message`, `symbol`, `timeframe`) plus a
    per-symbol `SymbolReadout` with the latest indicator snapshot.

- **Level 2 — On-chain intelligence (Dune MCP only)**
  (`src/core/level2.py`)
  - Sourced **exclusively** from Dune MCP via the new
    `DuneMCPClient` (`src/data/dune_mcp.py`). Speaks the same
    Bearer-authenticated surface the Dune MCP server exposes to
    LLMs (`execute_query`, `latest_results`, `ping`), plus a
    high-level `fetch_metric(name, params)` that resolves each
    metric to a saved Dune query id (configurable per metric via
    `DUNE_QUERY_*_ID` env vars).
  - Per-symbol metrics: funding rate (current + 8h / 24h delta +
    weighted average + annualised %), open interest (current +
    1h / 4h / 24h deltas), volume + 1h-vs-24h spike detection,
    long/short ratio with inferred bias, cumulative funding paid /
    received over the window, whale-activity flag with rationale.
    `volume`, `whale_activity`, `vault_flows`, `market_sentiment`
    are computed straight from `dex.trades` and
    `erc20_<chain>.evt_Transfer`. `funding_rates`, `open_interest`,
    `long_short_ratio` and `cum_funding` are clearly labelled
    **spot-derived proxies** that capture the same structural signal
    (buy-vs-sell imbalance, rolling-USD volume, unique-wallet ratio,
    signed aggressor flow). When a real perp-DEX schema lands on
    Dune for Arc / Base, the underlying source swaps without
    touching the rest of the pipeline.
  - Vault-level metrics: TVL + net deposits / withdrawals over the
    recent window for the configurable `DUNE_PERP_VAULT_ADDRESS`
    (defaults to `ARC_PERP_VAULT_ADDRESS`).
  - A market-wide **heat score** in `[0, 1]` labels the regime as
    `risk_on` / `risk_off` / `neutral` / `transition`. When the
    `market_sentiment` Dune query is configured, its `heat` is used
    directly; otherwise the engine falls back to a heuristic blend
    of funding, OI 1h delta, 24h price change and L/S.
  - **Per-metric provenance.** Every metric records its source
    (`dune:<query_id>` when it ran, `n/a` with an instructive note
    when the corresponding `DUNE_QUERY_*_ID` isn't set, `error` if
    Dune was unreachable). The L2 panel renders this provenance map
    so demos and live runs are honest about what's actually on-chain.
  - **SQL templates ship in `dune/queries/`** — `funding_rates.sql`,
    `open_interest.sql`, `volume.sql`, `vault_flows.sql`,
    `whale_activity.sql`, `long_short_ratio.sql`, `cum_funding.sql`,
    `market_sentiment.sql`, each documented with its expected
    parameters and column contract. Save them in your Dune
    workspace, set the query ids in `.env`, and Level 2 starts
    returning live numbers.
  - `DEMO_MODE` caches the full `Level2Intelligence` payload for
    `DEMO_CACHE_TTL_SECONDS` (default 30 min) so demo loops are fast
    and idempotent.

- **DecisionEngine — cascading L1 → L2 with short-circuit**
  (`src/core/decision_engine.py`)
  - If Level 1 blocks (`Level1Decision.passes == False`), Level 2 is
    **not** called and the engine emits `final_score = 0.0`,
    `regime = "risk-off"`, `short_circuited = True` with the explicit
    L1 block reason. The `AllocationRouter` distinguishes a
    short-circuit risk-off (legitimate close) from stale data
    (denied).
  - When L1 passes, L2 runs. Level 3 is left as a deterministic
    placeholder (synthetic re-weight of L1 + L2) until Day 4 wires
    Gemini 2.5 Flash as the final arbiter — the engine already
    accepts a `Level3` instance and the briefing path is in place.
  - Side inference: positive 24h price change on a majority of
    symbols → `long`, negative → `short`.

- **Rich visualisation** (`src/utils/console.py`, `main.py`)
  - Every decision cycle prints six panels: **Market Context**
    (with Arc latest block + vault TVL + agent margin / PnL /
    drawdown), **Level 1 — Technical Hard Rules**, **Level 2 — On-chain
    Intelligence (Dune MCP only)** with a **Metric provenance** table,
    **Final Decision**, **Execution Plan** and **On-chain Result**.
    Panels colour-code regime, trend, severity, source-availability
    and tx state.

- **Config & env**
  - New settings: `OHLCV_LOOKBACK_HOURS`, `DUNE_CHAIN`
    (`ethereum` / `base` / `arbitrum`), `DUNE_LOOKBACK_HOURS`,
    `DUNE_TOKEN_{BTC,ETH,SOL,USDC}_ADDRESS` (overrides for the
    built-in chain map), `DUNE_PERP_VAULT_ADDRESS` (vault watched
    by `vault_flows`), `DUNE_WHALE_MIN_USD`, and one
    `DUNE_QUERY_*_ID` per metric (now including `DUNE_QUERY_OHLCV_ID`
    for Level 1). Removed: all `BINANCE_*` settings, the
    `ARC_PERP_TRADE_EVENT_SIG / ARC_PERP_*_DECIMALS /
    ARC_PERP_OHLCV_LOOKBACK_BLOCKS` family (no more direct Arc-RPC
    OHLCV scraping), and the old `DUNE_CHAIN_TAG=arc` default
    (replaced by `DUNE_CHAIN=ethereum`).

### Day 4 — Planned

- **Level 3 — real Gemini 2.5 Flash arbitration** (`src/core/level3.py`
  + `src/llm/gemini_client.py`)
  - Pin temperature low, force strict JSON: `{"score": float, "regime":
    str, "rationale": str}`.
  - Feed `ArbiterBriefing { l1_score, l1_rationale, l2_score,
    l2_rationale, market_snapshot }`.

- **Trading wire-up** (`src/execution/arc_perp_executor.py`)
  - Set `ARC_PERP_MATCHER_URL` in `.env` once published in
    `#agora-hackers`.
  - Sign EIP-712 `OrderTypes.Order` with `eth-account`, POST signed
    orders to the matcher.
  - Decode `PositionLedger.getPosition` into the `Position` dataclass.

- **Risk-off leg — USYC rotation** (`src/allocation/allocation_router.py`)
  - On `risk_off`, withdraw USDC margin from the perp vault and route
    into USYC.

- **Observability**
  - Persist every `DecisionResult` + `ExecutionPlan` to a local JSONL
    log so we can replay the agent's day.

**Definition of done for Day 4:** the agent runs end-to-end on real
signals + Gemini arbitration, opens / closes a real perp position on
Arc Testnet on `--live`, and rotates into USYC on a risk-off flip.

---

## What a cycle looks like

Every cycle prints six panels to the terminal:

```
─── CapitalArc  mode=DRY-RUN  env=dev ───

┌── Market Context ──────────────────────┐
│  Time, mode, symbols, RPC, agent       │
│  wallet, account ID, L1 timeframes     │
└────────────────────────────────────────┘
┌── Level 1 - Technical Hard Rules ──────┐
│  PASS / BLOCKED verdict, score,        │
│  indicators table (EMA9/21, RSI, ATR%, │
│  trend) and reasons table.             │
└────────────────────────────────────────┘
┌── Level 2 - On-chain Intel (Dune MCP) ─┐
│  regime, heat, Dune MCP health,        │
│  per-symbol funding / OI / volume /    │
│  L-S / whales / cum funding,           │
│  vault TVL + net flow, plus a          │
│  per-metric provenance map (dune:id /  │
│  n/a / error) so the data path is      │
│  honest at a glance.                   │
└────────────────────────────────────────┘
┌── Final Decision ──────────────────────┐
│  final score, regime, action, side,    │
│  intensity, per-level breakdown        │
└────────────────────────────────────────┘
┌── Execution Plan ──────────────────────┐
│  decision_id, action, symbol, size,    │
│  leverage, rationale                   │
└────────────────────────────────────────┘
┌── On-chain Result ─────────────────────┐
│  tx_id, state, hash, sponsored,        │
│  Arcscan explorer link                 │
└────────────────────────────────────────┘
```

## Quick Start

```bash
git clone <repo-url>
cd CapitalArc

python -m venv .venv
.venv\Scripts\activate         # Windows
# source .venv/bin/activate    # macOS / Linux

pip install -r requirements.txt
cp .env.example .env
# Fill the keys in .env (Arc, Circle, Dune, Gemini).

# Dry-run (no on-chain transactions, just logs the would-be calls):
python main.py

# Loop in dry-run mode at DECISION_INTERVAL_SECONDS:
python main.py --loop

# Live execution (only after the Arc Perp router + Circle creds are real):
python main.py --live
```

---

## License

To be defined before the hackathon submission.
