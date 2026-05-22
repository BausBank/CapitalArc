# CapitalArc

> 🧭 **The on-chain adaptive portfolio manager.**
> Trades when the market is risk-on. Earns yield when it isn't.
> Fully autonomous, fully on-chain, fully observable.

[![Hackathon](https://img.shields.io/badge/Agora-Agents%20Hackathon-blueviolet)](https://www.canteen.xyz/)
[![Built on](https://img.shields.io/badge/Built%20on-Arc%20%C3%97%20Circle-0052FF)](https://www.circle.com/)
[![Data](https://img.shields.io/badge/Data-Dune%20MCP-FF6E40)](https://dune.com/)
[![LLM](https://img.shields.io/badge/LLM-Claude%20Sonnet%204.6%20via%20OpenRouter-d97757)](https://openrouter.ai/)
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

- 🧠 **Three-level cascading decision engine** — fast deterministic technicals (L1) → on-chain intelligence (L2) → Claude Sonnet 4.6 final arbiter (L3, via OpenRouter) with strict JSON verdict + Pydantic-validated safe-HOLD fallback.
- ⚖️ **Decoupled conviction & direction** — the engine separates "how strongly do we want to act?" from "which way?", so a high-conviction bearish setup correctly opens a **SHORT**, not a confused close. _(See [Architecture](#-architecture) below.)_
- 📊 **Volatility-targeted sizing** — `size = equity × target_risk_pct / (stop_atr_mult × ATR%/100)`. The Kelly-fraction-style recipe used by every systematic CTA shop.
- 🛟 **Gradient drawdown haircut** — intensity smoothly decays as drawdown grows (`× max(0, 1 − (dd/max_dd)^exponent)`), no cliff-edge stops.
- 🔄 **L1 short-circuit** — any blocking L1 rule (RSI extreme, ATR out-of-band, trend mixed, drawdown breach) bypasses L2/L3 immediately. Saves API budget, stays honest.
- 🔗 **Dune MCP as the single source of truth** — every signal (L1 OHLCV + 8 L2 metrics) reads through saved Dune queries with per-metric provenance. No CEX feeds. No RPC market-data scraping.
- 🪞 **Chain-portable** — `DUNE_CHAIN=ethereum|base|arbitrum` is a one-line switch; SQL templates are parameterised by chain + token addresses.
- ⛽ **Gasless on-chain execution** — Circle DCW + Paymaster, RSA-OAEP-SHA256-encrypted entity secret, sponsored tx on Arc Testnet.
- 🎛️ **Seven-panel rich CLI** — every cycle prints market context, L1, L2, **L3 Claude verdict** (provider, model, latency, active `Mode` badge, section-coloured structured rationale, bulleted key factors), final decision, execution plan and on-chain results with conviction/direction/sizing breakdown.
- 🧑‍⚖️ **Two L3 personas via `L3_MODE`** — `critical` (default; independent senior-risk-manager persona with veto authority and a mandatory 5-section rationale `Market Context → Key Signals Analysis → Contradictions & Risks → My Independent View → Final Recommendation`) or `standard` (concise trader voice, 1-2 sentence rationale). One-line `.env` swap; nothing else changes.
- 🧪 **Offline scenario tester** — `python main.py --test-bias bearish --test-conviction 0.52` exercises the live decision branch with synthetic inputs, no Dune/Circle calls required.

---

## 🏛️ Architecture

### Three-level decision engine

```
                +-----------------------------------------------------+
                |                  Decision Engine                    |
                |  +--------+   +--------+   +----------------------+ |
   Market  ---> |  | L1 TA  |  | L2 OnC |  | L3 Claude Sonnet 4.6 | | ---> (conviction, direction)
   Data         |  | rules  |  | (Dune) |  |    (final arbiter)   | |
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
| L3    | **Claude Sonnet 4.6** (via OpenRouter) | Conviction + direction + regime + intensity       | 0.40           |

> 🛈 **Synthetic L3 redistribution.** When `OPENROUTER_API_KEY` is unset (or the arbiter errors and falls back to a safe HOLD), the L3 placeholder has its weight **redistributed proportionally to L1+L2** during aggregation — so the placeholder doesn't silently dilute the real signal back into itself. Once `OPENROUTER_API_KEY` is configured and Claude returns a clean verdict, L3 votes with its full configured weight (`0.40` by default). Effective weights are surfaced in the Final Decision panel as `0.25 → 0.42` etc.

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
| L3    | Claude Sonnet 4.6 `conviction ∈ [0, 1]` (strict JSON)            | Claude `direction ∈ {long, short, neutral}` |

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
│   │   └── level3.py             # Claude Sonnet 4.6 final arbiter (L3_MODE-aware)
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
│   │   └── openrouter_client.py  # OpenRouter HTTP client (Level 3)
│   └── utils/
│       ├── config.py             # Pydantic settings (sole .env reader)
│       ├── console.py            # Six-panel rich renderer
│       └── logging.py            # Loguru sinks
├── dune/
│   ├── README.md                 # SQL templates + column contracts (deep dive)
│   └── queries/                  # ohlcv.sql, funding_rates.sql, ... (9 templates)
├── prompts/                      # Level 3 arbiter system prompts
│   ├── level3_arbiter_critical.md   # Default — independent risk-manager voice
│   └── level3_arbiter_standard.md   # Concise trader voice
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

### ✅ Day 4 — Real Claude Sonnet 4.6 final arbiter (via OpenRouter)

CapitalArc now talks to **Anthropic Claude Sonnet 4.6** as the cascade's final arbiter, routed through **OpenRouter**'s OpenAI-compatible HTTP gateway. L1 and L2 hand it a structured Markdown briefing (primary symbol, account drawdown, per-symbol funding / OI / volume / L/S / whales / vault flows, market bias and strength); Claude replies with a **strict JSON verdict** validated by Pydantic.

- **`src/llm/openrouter_client.py` — production-grade async client.**
  - Async `generate_json(...)` round-trip via `httpx.AsyncClient` POST `/v1/chat/completions`, pinned `temperature=0.2`, `max_tokens=2048` to fit the structured rationale; "JSON only" enforced via system prompt + user-prompt trailer + permissive parser.
  - Bounded retry with exponential backoff on 429 / 5xx / network blips (`OPENROUTER_MAX_RETRIES`, `OPENROUTER_BACKOFF_SECONDS`); hard failures (401 auth, 404 model, 402 payment, 403 policy) bypass retry and surface immediately with an actionable diagnosis via `OpenRouterAPIError`.
  - Defensive parsing: handles both plain-string `content` and OpenAI-style content-parts lists, strips stray ```` ```json ```` fences, rejects non-object payloads, surfaces timeouts as `RuntimeError` so the arbiter can always fall back safely.

- **`src/core/level3.py` — final arbiter with hard contract + two personas.**
  - `ArbiterResponse` (Pydantic): `{conviction, direction, regime, recommended_intensity, rationale, key_factors}` — enums + range-validated, rationale up to 4000 chars so the structured layout fits comfortably.
  - **Two modes selected by `L3_MODE` in `.env`** (one-line swap, nothing else changes):
    - **`critical` (default).** Claude wears the persona of an **independent senior risk manager** with explicit veto authority over L1 + L2 — it is *allowed and encouraged* to disagree when signals are weak, contradictory or fragile. The rationale follows a **mandatory 5-section template**:
      ```
      Market Context:        → what the market is doing right now
      Key Signals Analysis:  → 3-5 bullets, each citing a concrete number
      Contradictions & Risks:→ explicit L1/L2 disagreements + fragility
      My Independent View:   → first-person opinion ("I agree…", "I push back…")
      Final Recommendation:  → verdict + intensity rationale
      ```
      Baked-in decision principles: skepticism is the baseline, quality over direction, drawdown ≥ 5% clamps intensity to ≤ 0.5, ATR% > 4% clamps intensity, longs and shorts are symmetric. System prompt: [`prompts/level3_arbiter_critical.md`](./prompts/level3_arbiter_critical.md).
    - **`standard`.** Concise trader voice, 1-2 sentence rationale plus 2-4 short technical tags. Useful for high-frequency loops where the structured rationale is overkill. System prompt: [`prompts/level3_arbiter_standard.md`](./prompts/level3_arbiter_standard.md).
  - On schema violation or any LLM error → **safe neutral HOLD** (`conviction=0`, `direction=neutral`, `regime=hold`); never trades on malformed JSON. Synthetic + fallback rationales follow the active mode's format so the panel stays visually consistent whether Claude is wired or errored.
  - On `OPENROUTER_API_KEY` unset → synthetic placeholder; `DecisionEngine` redistributes its weight to L1+L2 (unchanged from Day 3).
  - **English** rationale + English descriptive `key_factors` everywhere (`"converging bearish signals (funding -16.4%, OI -4%, whales distributing)"` rather than terse one-word tags).

- **`DecisionEngine` cascade upgrade.** Builds a rich `ArbiterBriefing` (full L1 + L2 raw payloads + market context) and asks Level 3 under the active persona. When a real Claude verdict lands, L3's weight stops being redistributed — Claude votes with its **full configured weight** (`0.40` by default). The Final Decision panel contrasts configured weights vs effective weights so the demo never lies about which level decided what.

- **Six-panel rich CLI gets a seventh — `Level 3 — FINAL ARBITER (SONNET 4.6)`.** Renders provider (`Agent Sonnet-4.6, <latency>ms` for the happy path, `SYNTHETIC` or `FALLBACK` for the safety paths), the active **`Mode` badge** (red-bold `CRITICAL` or cyan `STANDARD`), validated verdict, the structured rationale with each section header painted in bold bright-cyan, and a clean bulleted `key_factors` list. Fallbacks render in red with the underlying error reason.

- **Tests** (`tests/test_openrouter_client.py` + `tests/test_level3_arbiter.py`, 34 total). JSON parsing, message-text extraction (string + content-parts list), HTTP status-error mapping, Pydantic validation, synthetic placeholder, malformed-payload fallback, raised-exception fallback, prompt rendering, mode-based prompt-path resolution for both `critical` and `standard`, on-disk template integrity for both modes, mode-specific user-prompt instructions, long 5-section rationale acceptance, full L1→L2→L3 cascade aggregation with both real and synthetic L3.

- **End-to-end smoke.** `python main.py --test-bias bearish --test-bias-strength 0.86 --test-final-score 0.52 --real-sonnet` runs the live OpenRouter round-trip with a coherent synthetic briefing — Claude (in critical mode) produces a full 5-section rationale, can independently nudge conviction up or down vs L1/L2's suggestion, flags contradictions explicitly, and returns descriptive `key_factors`.

### 🚧 Day 5 — Planned

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

# 🧑‍⚖️ Same as above, but also call the real Claude Sonnet 4.6 arbiter
# end-to-end (requires OPENROUTER_API_KEY). Critical mode by default —
# Claude returns a full 5-section structured rationale.
python main.py --test-bias bearish --test-bias-strength 0.86 --test-conviction 0.52 --real-sonnet
```

Each invocation prints the **INPUTS** panel (per-level conviction, thresholds, configured-vs-effective weights), the **Level 3** panel (with the active `Mode` badge — `CRITICAL` or `STANDARD` — and section-coloured rationale when real Claude is wired) and the **DECISION** panel (action, side, aggregate direction, final conviction, intensity, plain-English `why`).

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
┌── Level 3 — FINAL ARBITER (SONNET 4.6) ┐
│  provider (Agent Sonnet-4.6, latency / │
│  SYNTHETIC / FALLBACK), Mode badge     │
│  (CRITICAL / STANDARD), strict JSON    │
│  verdict (conviction, direction,       │
│  regime, recommended_intensity),       │
│  structured 5-section rationale,       │
│  bulleted key_factors.                 │
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
| **Day 4** | ✅ | Real Claude Sonnet 4.6 L3 arbiter via OpenRouter; two `L3_MODE` personas (`critical` default with 5-section rationale + veto authority, `standard` for trader-voice); strict JSON verdict + Pydantic; safe HOLD fallback; seventh rich panel with mode badge; 34 smoke tests |
| **Day 5** | 🚧 | EIP-712 perp orders; USYC rotation; JSONL decision log |
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
- 🤖 **Claude Sonnet 4.6** (via **OpenRouter**) — Level 3 final arbiter with strict-JSON verdict (Pydantic-validated)

**Backend**
- 🐍 Python 3.10+
- `web3.py`, `eth-account`, `eth-abi` — chain interaction
- `httpx`, `aiohttp`, `tenacity` — async HTTP with rate-limit retries
- `pydantic`, `pydantic-settings` — strongly-typed config
- `pandas`, `numpy`, `ta` — L1 indicator math
- `mcp`, `dune-client` — L2 on-chain data
- `httpx` (above) — L3 OpenRouter HTTP client
- `loguru` — structured logging
- `rich` — seven-panel terminal UI
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
