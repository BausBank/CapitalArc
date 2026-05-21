# CapitalArc

> 🧭 **The on-chain adaptive portfolio manager.**
> Trades when the market is risk-on. Earns yield when it isn't.
> Fully autonomous, fully on-chain, fully observable.

[![Hackathon](https://img.shields.io/badge/Agora-Agents%20Hackathon-blueviolet)](https://www.canteen.xyz/)
[![Built on](https://img.shields.io/badge/Built%20on-Arc%20%C3%97%20Circle-0052FF)](https://www.circle.com/)
[![Data](https://img.shields.io/badge/Data-Dune%20MCP-FF6E40)](https://dune.com/)
[![LLM](https://img.shields.io/badge/LLM-Gemini%202.5%20Flash-4285F4)](https://ai.google.dev/)
[![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB)](https://www.python.org/)

---

## 🌍 Overview

**CapitalArc** is a fully on-chain AI agent that reads the market regime in real time and reallocates capital autonomously:

- 📈 **Risk-on regime** — the agent opens **long or short** leveraged perp positions on the **Arc Perp DEX**, sized by a vol-targeted risk budget.
- 🛡️ **Risk-off regime** — capital rotates into **USYC** (yield-bearing tokenized USDC), preserving value with native institutional yield while waiting for the next setup.

Decisions come from a **cascading three-level engine** (technicals → on-chain intelligence → LLM arbiter) that separates **conviction** ("do we act?") from **direction** ("which side?"). Execution rides the full **Circle** stack — CCTP for liquidity, Developer-Controlled Wallets for non-custodial signing, Circle Paymaster for gasless UX, USYC for the yield leg — on **Arc**.

> Built for the **Agora Agents Hackathon (Canteen × Circle on Arc)**, request **RFB 04 — Adaptive Portfolio Manager**.

---

## ✨ Key Features

- 🧠 **Three-level cascading decision engine** — fast deterministic technicals (L1) → on-chain intelligence (L2) → LLM final arbiter (L3, Day 4).
- ⚖️ **Decoupled conviction & direction** — the engine separates "how strongly do we want to act?" from "which way?", so a high-conviction bearish setup correctly opens a **SHORT**, not a confused close. _(See [Architecture](#-architecture) below.)_
- 📊 **Volatility-targeted sizing** — `size = equity × target_risk_pct / (stop_atr_mult × ATR%/100)`. The Kelly-fraction-style recipe used by every systematic CTA shop.
- 🛟 **Gradient drawdown haircut** — intensity smoothly decays as drawdown grows (`× max(0, 1 − (dd/max_dd)^exponent)`), no cliff-edge stops.
- 🔄 **L1 short-circuit** — any blocking L1 rule (RSI extreme, ATR out-of-band, trend mixed, drawdown breach) bypasses L2/L3 immediately. Saves API budget, stays honest.
- 🔗 **Dune MCP as the single source of truth** — every signal (L1 OHLCV + 8 L2 metrics) reads through saved Dune queries with per-metric provenance. No CEX feeds. No RPC market-data scraping.
- 🪞 **Chain-portable** — `DUNE_CHAIN=ethereum|base|arbitrum` is a one-line switch; SQL templates are parameterised by chain + token addresses.
- ⛽ **Gasless on-chain execution** — Circle DCW + Paymaster, RSA-OAEP-SHA256-encrypted entity secret, sponsored tx on Arc Testnet.
- 🎛️ **Six-panel rich CLI** — every cycle prints market context, L1, L2, final decision, execution plan and on-chain results with conviction/direction/sizing breakdown.
- 🧪 **Offline scenario tester** — `python main.py --test-bias bearish --test-conviction 0.52` exercises the live decision branch with synthetic inputs, no Dune/Circle calls required.

---

## 🏛️ Architecture

### Three-level decision engine

```
                +-----------------------------------------------------+
                |                  Decision Engine                    |
                |  +--------+   +--------+   +----------------------+ |
   Market  ---> |  | L1 TA  |  | L2 OnC |  | L3 Gemini 2.5 Flash  | | ---> (conviction, direction)
   Data        |  | rules  |  | (Dune) |  |    (final arbiter)   | |
                |  +--------+   +--------+   +----------------------+ |
                +-----------------------------------------------------+
                                       |
                                       v
                +-----------------------------------------------------+
                |                Allocation Router                    |
                |  conviction >= RISK_ON + direction != 0 -> Arc Perp |
                |  conviction <= RISK_OFF                  -> USYC    |
                |  mid-band + strong direction             -> open    |
                |  mid-band + neutral                      -> hold    |
                +-----------------------------------------------------+
                                       |
                                       v
                +-----------------------------------------------------+
                |  Execution: Circle DCW + Paymaster + Vol-targeted   |
                |  sizing + Gradient drawdown haircut                 |
                +-----------------------------------------------------+
```

| Level | Signal source                          | Role                                              | Default weight |
|-------|----------------------------------------|---------------------------------------------------|----------------|
| L1    | OHLCV / TA via **Dune `dex.trades`**   | Hard "защита от дурака" rules + trend direction   | 0.25           |
| L2    | On-chain flows via **Dune MCP**        | Funding, OI, volume, L/S, whales, vault flows     | 0.35           |
| L3    | **Gemini 2.5 Flash** final arbiter     | Regime + conviction + direction (Day 4)           | 0.40           |

> 🛈 **Synthetic L3 redistribution.** Until Gemini wiring lands on Day 4, the L3 placeholder has its weight **redistributed proportionally to L1+L2** during aggregation — so the placeholder doesn't silently dilute the real signal back into itself. Effective weights are surfaced in the Final Decision panel as `0.25 → 0.42` etc.

### Conviction vs Direction (the core idea)

Every level emits **two** independent values:

- 🎯 **Conviction** ∈ `[0, 1]` — "how strongly do we want to act at all?"
- ➡️ **Direction** ∈ `{-1, 0, +1}` — "if we act, which side?"

The aggregation:

```
final_conviction   = Σ (effective_weightᵢ × convictionᵢ)
direction_strength = | Σ (effective_weightᵢ × convictionᵢ × directionᵢ) / Σ (effective_weightᵢ × convictionᵢ) |
final_direction    = sign(direction_strength) if strength ≥ θ else 0
```

Direction votes are weighted by their **own conviction**, so a wishy-washy level can't drag the side.

### Per-level mechanics

| Level | Conviction formula                                              | Direction sign                      |
|-------|-----------------------------------------------------------------|-------------------------------------|
| L1    | `avg(per-symbol trend strength)` — no `0.5` floor              | Sign of primary symbol's trend      |
| L2    | `max(2 × |heat − 0.5|, bias_strength)`                          | Sign of `market_bias` (bullish/bearish/neutral) |
| L3    | Real Gemini call (Day 4) or synthetic blend of L1+L2 (today)    | Conviction-weighted blend           |

> 💡 **Why `max(2·|heat-0.5|, bias_strength)` for L2?** Heat alone is directional (0.85 = bullish, 0.15 = bearish), so it makes a bad *conviction* signal — both extremes are equally decisive on-chain. The `2·|heat-0.5|` term folds heat into a symmetric conviction; the `max(..., bias_strength)` term catches the case where heat sits near neutral but on-chain signals (funding, OI, whales) point decisively one way.

### Decision rules

```
conviction ≥ RISK_ON_THRESHOLD AND direction ≠ 0  →  open in direction (full intensity)
conviction ≤ RISK_OFF_THRESHOLD                   →  close everything (side-agnostic)
mid-band AND direction_strength ≥ STRONG_BIAS_OPEN →  open at reduced intensity
                                                       (½ × conviction × direction_strength)
mid-band AND direction_strength < STRONG_BIAS_OPEN →  hold
```

The L1 short-circuit always wins: any blocking L1 rule sets `final_conviction = 0`, skips L2/L3, and forces risk-off.

### Position sizing pipeline

When a `risk_on` directive opens a position, the router walks four steps:

1. **Vol-target** — `vol_target_size = (equity × TARGET_RISK_PCT) / (STOP_ATR_MULT × ATR%/100)`. Uses the primary symbol's average ATR% from L1.
2. **× Intensity** — scaled by `directive.intensity ∈ [0, 1]` (mid-band overrides use `0.5 × conviction × direction_strength`).
3. **× Drawdown haircut** — `× max(0, 1 − (dd_pct / max_dd_pct) ** DD_HAIRCUT_EXPONENT)`. Exponent 2.0 means 5% dd → 75% size, 9% dd → 19% size, 10% dd → hard close.
4. **Cap** at `MAX_POSITION_USD`.

Every plan stamps a full `sizing` breakdown onto the Execution Plan panel so the demo answers *"why this size?"* visibly, line by line.

### Repository layout

```
CapitalArc/
├── main.py                       # Entry point: --dry-run / --live / --loop / --test-bias
├── src/
│   ├── core/
│   │   ├── decision_engine.py    # Cascading L1→L2→L3, conviction+direction aggregation
│   │   ├── level1.py             # Technical hard rules + per-symbol direction
│   │   ├── level2.py             # On-chain intelligence (Dune MCP only)
│   │   └── level3.py             # Gemini final arbiter (Day 4)
│   ├── data/
│   │   ├── dune_mcp.py           # DuneMCPClient — single source of truth
│   │   ├── dune_market_data.py   # OHLCV adapter on dex.trades (L1 feed)
│   │   └── arc_onchain.py        # Arc RPC reader (account state only)
│   ├── execution/
│   │   ├── arc_perp_executor.py  # Arc Perp DEX: margin, positions, ledger reads
│   │   └── circle_wallet.py      # Circle DCW + Paymaster + RSA-OAEP signing
│   ├── allocation/
│   │   └── allocation_router.py  # Vol-targeted sizing + DD haircut + side routing
│   ├── llm/
│   │   └── gemini_client.py      # Google AI Studio client (Level 3)
│   └── utils/
│       ├── config.py             # Pydantic settings (sole .env reader)
│       ├── console.py            # Six-panel rich renderer
│       └── logging.py            # Loguru sinks
├── dune/
│   ├── README.md                 # SQL templates + column contracts (deep dive)
│   └── queries/                  # ohlcv.sql, funding_rates.sql, ... (9 templates)
├── prompts/                      # Gemini arbiter prompt templates
├── scripts/                      # One-off ops (Circle wallet creation, etc.)
├── tests/                        # pytest + pytest-asyncio
├── .env.example                  # Documented template — never commit real keys
├── requirements.txt
├── AGENTS.md                     # Internal agent spec (source of truth)
└── README.md
```

---

## 📅 Current Status

### ✅ Day 1 — Scaffolding

- Three-level architecture in place with stable public interfaces.
- Secret hygiene: `.env.example` template + git-ignored `.env`.
- `AGENTS.md` describes the cascade + risk-on/off split.

### ✅ Day 2 — Live Circle / Arc execution

- **`ArcPerpExecutor`** wired against the real Arc Perp DEX stack on Arc Testnet (`ClearingHouse 0x70a06946…`, `USDCCollateralVault 0x75E4FBFB…`, `MarketRegistry 0x9cED23e4…`, `PositionLedger 0xd6D77291…`).
- **Live margin moves** via Circle DCW `contractExecution` with **RSA-OAEP-SHA256** entity-secret encryption (fresh ciphertext per request).
- **Circle Paymaster** sponsorship wired via `gasPolicyId`; `TxResult.sponsored` flips to `True` when policy is configured.
- **`--dry-run` / `--live` / `--loop`** with strict pre-flight checks and Arcscan explorer links per tx.

### ✅ Day 3 — Real signals & decision engine (this commit)

The agent now produces **honest, financially-grounded decisions** end-to-end on live on-chain data.

- **L1 — Technical hard rules** (`src/core/level1.py`)
  - OHLCV for `BTC-PERP` / `ETH-PERP` on `15m` + `1h`, reconstructed from Dune's multichain `dex.trades` via `DuneMarketData`.
  - Four hard veto rules (`trend_mixed`, `rsi_overbought/oversold`, `atr_too_low/high`, `drawdown_breach`).
  - Emits per-symbol `direction_sign` (+1/0/-1) and `atr_pct_avg` for downstream vol-targeting.
  - Conviction = average per-symbol trend strength (no `+0.5` floor — a weak trend produces weak conviction, as it should).

- **L2 — On-chain intelligence** (`src/core/level2.py`)
  - **9/9 Dune queries live** (provenance map renders `dune:<id>` for every metric in the L2 panel).
  - Conviction = `max(2·|heat-0.5|, bias_strength)` — symmetric for longs and shorts.
  - Direction inferred from funding / OI / 24h price / L/S / whales / heat extremes.
  - Vault TVL + net deposit/withdrawal flow over the lookback window.
  - `DEMO_MODE` caches the full payload for `DEMO_CACHE_TTL_SECONDS` (30 min default).

- **DecisionEngine — conviction + direction aggregation** (`src/core/decision_engine.py`)
  - L1 short-circuit (any blocking reason → `final_conviction = 0`, L2/L3 skipped).
  - Synthetic L3 weight redistributed to L1+L2 proportionally (configurable).
  - Direction is a conviction-weighted vote across levels.
  - Mid-band strong-direction override opens at reduced intensity.

- **AllocationRouter — vol-targeted sizing + gradient drawdown haircut** (`src/allocation/allocation_router.py`)
  - Solves `size = (equity × risk) / (stop × ATR/100)` using L1's primary-symbol ATR%.
  - Drawdown haircut is gradient, not cliff (`exponent = 2.0` default).
  - Full sizing attribution stamped onto every `ExecutionPlan.extra["sizing"]`.

- **Six-panel rich CLI** — Market Context, L1, L2, Final Decision (with effective-weights and per-level direction), Execution Plan (with sizing pipeline), On-chain Result.

- **Offline scenario tester** — `--test-bias bearish --test-bias-strength 0.86 --test-conviction 0.52` exercises the real decision branch with synthetic inputs; no Dune/Circle/router calls.

### 🚧 Day 4 — Planned

- **Real Gemini 2.5 Flash arbitration** (`src/llm/gemini_client.py`): pinned low temperature, strict JSON `{score, direction, regime, rationale}`, briefing rendered from L1+L2.
- **EIP-712 `OrderTypes.Order` signing** + matcher POST (`ARC_PERP_MATCHER_URL`) for full `open_position` on `--live`.
- **USYC rotation** on risk-off: withdraw vault margin → USYC mint.
- **JSONL decision log** for replay / backtest.

---

## ⚡ Quick Start

### 1. Install

```bash
git clone <repo-url>
cd CapitalArc

python -m venv .venv
.venv\Scripts\activate          # Windows
# source .venv/bin/activate     # macOS / Linux

pip install -r requirements.txt
cp .env.example .env
# Edit .env: Arc RPC + Circle DCW + Dune API key + saved query ids
```

### 2. Wire up Dune (one-time)

Save the SQL templates from [`dune/queries/`](./dune/queries) to your Dune workspace and paste each query id into `.env`. See [Dune Queries Setup](#-dune-queries-setup) below.

### 3. Run

```bash
# 🟢 Dry-run (default — no on-chain tx, just logs the would-be calls):
python main.py

# 🔁 Loop every DECISION_INTERVAL_SECONDS:
python main.py --loop

# 🔴 Live execution (requires CIRCLE_API_KEY + CIRCLE_ENTITY_SECRET +
#                    CIRCLE_AGENT_WALLET_ID + ARC_PERP_ROUTER_ADDRESS):
python main.py --live
```

### 4. Offline scenario tester (`--test-bias`)

Probe the decision engine with synthetic inputs — no Dune, no Circle, no chain. Useful for sanity-checking thresholds and the strong-direction override before deploying.

```bash
# Bearish setup with mid-band conviction → STRONG-DIRECTION override → SHORT
python main.py --test-bias bearish --test-bias-strength 0.86 --test-conviction 0.52

# High-conviction bullish → RISK_ON LONG
python main.py --test-bias bullish --test-bias-strength 0.80 --test-conviction 0.75

# Neutral mid-band → HOLD
python main.py --test-bias neutral --test-bias-strength 0.10 --test-conviction 0.50
```

Each invocation prints the **INPUTS** panel (per-level conviction, thresholds, configured-vs-effective weights) and the **DECISION** panel (action, side, aggregate direction, final conviction, intensity, plain-English `why`).

### 5. What a live cycle looks like

```
─── CapitalArc  mode=DRY-RUN  env=dev ───

┌── Market Context ──────────────────────┐
│  Time, mode, symbols, RPC, agent       │
│  wallet, account ID, L1 timeframes,    │
│  Arc latest block, vault TVL, margin   │
└────────────────────────────────────────┘
┌── Level 1 — Technical Hard Rules ──────┐
│  PASS / BLOCKED verdict, conviction,   │
│  indicators table (EMA9/21, RSI, ATR%, │
│  trend) and reasons table.             │
└────────────────────────────────────────┘
┌── Level 2 — On-chain Intel (Dune MCP) ─┐
│  regime, conviction = max(heat_conv,   │
│  bias_strength), per-symbol funding /  │
│  OI / volume / L-S / whales / cum fund │
│  + per-metric provenance map.          │
└────────────────────────────────────────┘
┌── Final Decision ──────────────────────┐
│  conviction, aggregate direction       │
│  (with strength), action, side, L2     │
│  bias, intensity, per-level vote table │
│  (configured w → effective w).         │
└────────────────────────────────────────┘
┌── Execution Plan ──────────────────────┐
│  decision_id, action, symbol, size,    │
│  leverage, conviction, sizing pipeline │
│  (vol-target → intensity → DD haircut  │
│  → final).                             │
└────────────────────────────────────────┘
┌── On-chain Result ─────────────────────┐
│  tx_id, state, hash, sponsored,        │
│  Arcscan explorer link.                │
└────────────────────────────────────────┘
```

---

## 🗺️ Roadmap

| Phase | Status | Highlights |
|-------|--------|------------|
| **Day 1** | ✅ | Scaffolding, secret hygiene, 3-level interfaces |
| **Day 2** | ✅ | Circle DCW + Paymaster live; Arc Perp DEX margin moves on Testnet |
| **Day 3** | ✅ | L1 + L2 wired to Dune MCP (9/9 queries live); conviction/direction split; vol-targeted sizing; gradient drawdown haircut |
| **Day 4** | 🚧 | Real Gemini 2.5 Flash L3 arbiter; EIP-712 perp orders; USYC rotation; JSONL decision log |
| **Post-hack** | 💡 | Per-symbol routing (open BTC long while ETH is flat); on-chain DSL for declaring strategies; arc-native Dune dataset when indexed |

---

## 🧰 Tech Stack

**Chain & infrastructure**
- 🏗️ **Arc** stablechain (Testnet today, mainnet on launch)
- 🎯 **Arc Perp DEX** — `ClearingHouse` + `USDCCollateralVault` + `MarketRegistry` + `PositionLedger`

**Circle stack**
- 🌉 **CCTP v2** — cross-chain USDC routing
- 🔐 **Developer-Controlled Wallets** — non-custodial programmable signing
- ⛽ **Circle Paymaster** — gasless / sponsored transactions
- 🪙 **USYC** — yield-bearing tokenized USDC (risk-off leg)

**Intelligence & data**
- 🔭 **Dune MCP** — single source of truth for L1 OHLCV + L2 on-chain intelligence (Ethereum / Base / Arbitrum supported out of the box)
- 🧱 **Arc RPC** — account state only (wallet, vault TVL, agent margin)
- 🤖 **Gemini 2.5 Flash** — Level 3 final arbiter (Day 4)

**Backend**
- 🐍 Python 3.10+
- `web3.py`, `eth-account`, `eth-abi` — chain interaction
- `httpx`, `aiohttp`, `tenacity` — async HTTP with rate-limit retries
- `pydantic`, `pydantic-settings` — strongly-typed config
- `pandas`, `numpy`, `ta` — L1 indicator math
- `mcp`, `dune-client` — L2 on-chain data
- `google-generativeai` — L3 LLM client
- `loguru` — structured logging
- `rich` — six-panel terminal UI
- `apscheduler` — loop scheduling

---

## 🔬 Dune Queries Setup

CapitalArc sources **all** market analysis (L1 OHLCV + L2 on-chain intelligence) **exclusively** from Dune MCP. The agent queries a fixed set of saved Dune queries by id; SQL templates ship under [`dune/queries/`](./dune/queries) for you to save into your own Dune workspace.

### Why Ethereum / Base / Arbitrum and not Arc Testnet?

Arc Testnet is not yet indexed in the public Dune catalog (no `arc.dex.trades`, no `erc20_arc.evt_Transfer`). Until it lands, CapitalArc points its decision engine at a live, high-liquidity EVM chain via Dune's multichain `dex.trades` table. Every SQL template is parameterised by `{{chain}}` and per-symbol token addresses, so switching chains is a one-line `.env` change.

| Symbol     | Ethereum mainnet | Base                       | Arbitrum One |
|------------|------------------|----------------------------|--------------|
| `BTC-PERP` | WBTC             | cbBTC                      | WBTC         |
| `ETH-PERP` | WETH             | WETH (`0x4200…0006`)       | WETH         |
| USDC       | Circle USDC      | native USDC                | native USDC  |

Defaults live in `src/utils/config.py::_DEFAULT_TOKEN_ADDRESSES`. Override any of them via `DUNE_TOKEN_{BTC,ETH,USDC}_ADDRESS` in `.env`.

### Saved queries (9/9 live)

Save each SQL file in your Dune workspace and paste the resulting numeric id into `.env`:

| Level | Metric             | Env var                          | SQL template                                              |
|-------|--------------------|----------------------------------|-----------------------------------------------------------|
| L1    | `ohlcv`            | `DUNE_QUERY_OHLCV_ID`            | [`dune/queries/ohlcv.sql`](./dune/queries/ohlcv.sql)                       |
| L2    | `market_sentiment` | `DUNE_QUERY_MARKET_SENTIMENT_ID` | [`dune/queries/market_sentiment.sql`](./dune/queries/market_sentiment.sql) |
| L2    | `volume`           | `DUNE_QUERY_VOLUME_ID`           | [`dune/queries/volume.sql`](./dune/queries/volume.sql)                     |
| L2    | `funding_rates`    | `DUNE_QUERY_FUNDING_RATES_ID`    | [`dune/queries/funding_rates.sql`](./dune/queries/funding_rates.sql)       |
| L2    | `open_interest`    | `DUNE_QUERY_OPEN_INTEREST_ID`    | [`dune/queries/open_interest.sql`](./dune/queries/open_interest.sql)       |
| L2    | `vault_flows`      | `DUNE_QUERY_VAULT_FLOWS_ID`      | [`dune/queries/vault_flows.sql`](./dune/queries/vault_flows.sql)           |
| L2    | `whale_activity`   | `DUNE_QUERY_WHALE_ACTIVITY_ID`   | [`dune/queries/whale_activity.sql`](./dune/queries/whale_activity.sql)     |
| L2    | `long_short_ratio` | `DUNE_QUERY_LONG_SHORT_RATIO_ID` | [`dune/queries/long_short_ratio.sql`](./dune/queries/long_short_ratio.sql) |
| L2    | `cum_funding`      | `DUNE_QUERY_CUM_FUNDING_ID`      | [`dune/queries/cum_funding.sql`](./dune/queries/cum_funding.sql)           |

Startup logs print `Loaded <metric> query ID = <id>` for every loaded query, so any drift between `.env` and what Python actually sees is visible immediately — no need to grep.

### Honest provenance everywhere

Every metric records its source: `dune:<query_id>` when the query ran, `n/a` (with an instructive note pointing at the missing env var) when the id isn't set, or `error` if Dune was unreachable. The L2 panel renders this provenance map so demos and live runs are honest about what's actually on-chain. **The agent never invents data** — Level 1 honestly blocks on `ohlcv_unavailable` if `DUNE_QUERY_OHLCV_ID` isn't configured.

### Spot-derived perp proxies (clearly labelled)

L2 measures funding / OI / L/S / cumulative funding even though `dex.trades` is a spot tape. Each is computed as a **proxy** that captures the same structural signal:

| Metric              | Proxy                                                                                                |
|---------------------|------------------------------------------------------------------------------------------------------|
| Funding rate        | Buy-vs-sell USD imbalance over 8h, scaled to a per-8h funding-rate equivalent (0.05% / 1.0 imbalance)|
| Open interest       | Rolling-USD volume + 1h / 4h / 24h deltas                                                            |
| Long/short ratio    | `sum(buy_usd) / sum(sell_usd)`, with unique-wallet counts for the account columns                    |
| Cumulative funding  | Net signed aggressor flow scaled across the lookback window                                          |
| Whale activity      | Spot DEX trades above `DUNE_WHALE_MIN_USD`, signed accumulating / distributing                       |
| Vault flows         | ERC-20 `Transfer` events into / out of `DUNE_PERP_VAULT_ADDRESS`                                     |
| Market sentiment    | `0.5 + 0.25 × mean(imbalance) + 0.5 × clip(mean(24h price change), ±0.25)`                          |

When a real perp-DEX schema lands on Dune (Arc, Synthetix V3 perps, etc.), swap the `dex.trades` source for the perp's `fills` / `funding_events` table — the rest of the pipeline is unchanged.

### Free-tier safety nets

L2 fans out 8 metric calls + L1 fans out 1 OHLCV call in parallel each cycle. On Dune's free tier the burst hits HTTP 429 quickly; the client caps in-flight requests with a semaphore (`DUNE_MAX_CONCURRENT_REQUESTS=2` by default) and retries 429s with exponential backoff (`DUNE_RATE_LIMIT_*` knobs). Bump concurrency to 6–8 on a paid plan.

### Full column contracts & SQL gotchas

The full per-query column contract (what every column means, types, gotchas around `varbinary` casts and Trino reserved keywords) lives in **[`dune/README.md`](./dune/README.md)** — saved separately so the SQL files and their docs stay co-located.

---

## 📜 License

To be defined before the hackathon submission.
