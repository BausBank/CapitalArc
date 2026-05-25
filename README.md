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

- 📈 **Risk-on regime** — the agent opens **long or short** leveraged perp positions on **Hyperliquid Testnet**, sized by a vol-targeted risk budget and supervised by a per-position **TP / SL / trailing-stop / side-flip** manager.
- 🛡️ **Risk-off regime** — perps are flattened on Hyperliquid, margin is withdrawn back, and the freed USDC rotates into **USYC** on Arc (yield-bearing tokenized USDC), preserving value with native institutional yield while waiting for the next setup.

Decisions come from a **cascading three-level engine** (technicals → on-chain intelligence → LLM arbiter) that separates **conviction** ("do we act?") from **direction** ("which side?"). Trading rides **Hyperliquid Testnet** via the official `hyperliquid-python-sdk` (EIP-712 L1 actions over the phantom-agent domain). The treasury & yield leg keeps using the full **Circle** stack on **Arc** — CCTP for liquidity, Developer-Controlled Wallets for non-custodial signing, Circle Paymaster for gasless UX, USYC for yield.

> Built for the **Agora Agents Hackathon (Canteen × Circle on Arc)**, request **RFB 04 — Adaptive Portfolio Manager**.

---

## ✨ Key Features

- 🧠 **Three-level cascading decision engine** — fast deterministic technicals (L1) → on-chain intelligence (L2) → Claude Sonnet 4.6 final arbiter (L3, via OpenRouter) with strict JSON verdict + Pydantic-validated safe-HOLD fallback.
- ⚖️ **Decoupled conviction & direction** — the engine separates "how strongly do we want to act?" from "which way?", so a high-conviction bearish setup correctly opens a **SHORT**, not a confused close. _(See [Architecture](#-architecture) below.)_
- 🎯 **Hyperliquid Testnet as the trading venue** — real long/short perp positions signed via the official `hyperliquid-python-sdk` (EIP-712 L1 actions). Hard caps `HYPERLIQUID_MAX_LEVERAGE` / `HYPERLIQUID_MAX_POSITION_USD` enforced in the executor before any SDK call. Two independent API URLs (`HYPERLIQUID_API_URL` = testnet execution, `HYPERLIQUID_DATA_API_URL` = mainnet market data) so the data plane stays honest even when execution runs on testnet.
- 🛠️ **`PositionManager` — Two-Tier (Fast Path + Smart Path)** — runs every cycle BEFORE the regime dispatch. The **Fast Path** (always-on, deterministic, ATR-aware) handles a 9-trigger priority ladder: `daily_dd_guard > stop_loss > vol_spike_close > time_exit > take_profit > partial_take_profit > trailing_stop > side_flip > re_evaluation`, with state-only advisory verbs (`breakeven_arm`, `vol_spike_warn`). The **Smart Path** (Claude Sonnet 4.6, event-triggered) fires ONLY on material change (price ≥ 1.5×ATR, funding spike, whale activity, OI delta, or scheduled refresh) and can override the Fast Path verdict. Per-asset ATR caps (BTC ≤ 3%, ETH ≤ 4% on 1h) gate new opens; a portfolio-wide daily-DD kill switch flattens everything and refuses new opens on session loss. "HOLD must be earned" — pure logic, the router owns the actual close call.
- 🔁 **Auto-allocation pipeline** — risk-on opens on Hyperliquid (redeeming USYC first if Arc cash is short); risk-off flattens perps, withdraws margin, and mints USYC with the leftover USDC (bounded by configurable reserve + min/max rotation amounts). The USYC leg is optional via `USYC_ENABLED=false` for clean perp-only live tests.
- 📊 **Volatility-targeted sizing** — `size = equity × target_risk_pct / (stop_atr_mult × ATR%/100)`. The Kelly-fraction-style recipe used by every systematic CTA shop.
- 🛟 **Gradient drawdown haircut** — intensity smoothly decays as drawdown grows (`× max(0, 1 − (dd/max_dd)^exponent)`), no cliff-edge stops.
- 🛂 **L1 hard/soft block taxonomy with L3 override** — L1 reasons carry an explicit `is_hard` flag plus marginality metadata (`marginal / moderate / decisive`). Hard blocks (`drawdown_breach`, `ohlcv_unavailable`) always short-circuit. Soft blocks (RSI / ATR / trend-mixed) are eligible for L3 audit & override, with a **stacked-veto intensity haircut** (1 block → ×1.0, 2 → ×0.7, 3 → ×0.5, 4+ → ×0.35) so overriding compound vetoes shrinks position size proportionally. Every override is auditable via `DecisionResult.l1_override_meta` and rendered as an `L1 OVERRIDDEN BY L3` console banner.
- 🌐 **Real perp data via `HyperliquidIntelligenceAdapter`** — Level 2's funding / OI / volume / cum-funding metrics are pulled live from Hyperliquid's `metaAndAssetCtxs` + `fundingHistory` Info endpoints (an in-process ring buffer fills in the 1h/4h/24h OI deltas the API doesn't natively expose), overlaying the Dune spot-derived proxies. The provenance map keeps `dune:<id>` vs `hyperliquid:<endpoint>` visible per metric.
- 🔗 **Dune MCP as the analytical backbone** — L1 OHLCV + the on-chain L2 metrics (whales, vault flows, L/S, market sentiment) still read through saved Dune queries with per-metric provenance. No CEX feeds. No RPC market-data scraping.
- 🪞 **Chain-portable analytics** — `DUNE_CHAIN=ethereum|base|arbitrum` is a one-line switch; SQL templates are parameterised by chain + token addresses.
- ⛽ **Gasless treasury on Arc** — Circle DCW + Paymaster, RSA-OAEP-SHA256-encrypted entity secret, sponsored tx on Arc Testnet for the USYC / CCTP legs.
- 🎛️ **Eight-panel rich CLI** — every cycle prints market context, L1, L2, **L3 Claude verdict** (provider, model, latency, `Mode` badge, `Aggression` badge, section-coloured rationale, bulleted key factors, raw → calibrated breakdown when calibration changed the verdict), final decision (incl. `L1 OVERRIDDEN BY L3` banner when applicable), execution plan with sizing pipeline, **Position Review** (TP/SL/trailing prices vs live mid + per-position trigger badges) and on-chain results.
- 🧑‍⚖️ **Two L3 personas via `L3_MODE`** — `critical` (default; independent senior-risk-manager persona with veto authority, a Day-6 decision matrix, per-asset ATR caps, STRICT RESPONSE LENGTH RULES, and a mandatory 5-section rationale `Market Context → Key Signals Analysis → Contradictions & Risks → My Independent View → Final Recommendation`) or `standard` (concise trader voice, 1-2 sentence rationale). One-line `.env` swap; nothing else changes.
- 🎚️ **L3 aggression calibration (`L3_AGGRESSION`)** — `conservative` / `balanced` (default) / `aggressive` post-validation knob with a `HOLD-rescue` rule under `aggressive`: when Claude returns HOLD but L1 passes AND L2 conviction ≥ `L3_HOLD_RESCUE_L2_MIN`, the engine flips to a low-intensity OPEN. In-process `_L3Telemetry` counter tracks the held / opened / rescued mix so operators can spot over-conservatism in live runs.
- ⏰ **Configurable loop cadence** — `DECISION_INTERVAL_SECONDS=600` (10-minute default) bounds OpenRouter and Dune API costs in `--loop` mode without missing meaningful moves on the L1 15m/1h timeframes. `0` removes the sleep for back-to-back replay runs.
- 🧪 **Offline scenario testers** — `python main.py --test-bias bearish --test-conviction 0.52` exercises the live decision branch with synthetic inputs; `python main.py --test-allocation take_profit` (and `stop_loss` / `side_flip` / `re_evaluation`) exercises each `PositionManager` trigger without any Dune / Circle / SDK calls.

---

## 🏛️ Architecture

### Three-level decision engine

```
                +-------------------------------------------------------+
                |                  Decision Engine                      |
                |  +--------+   +--------+   +----------------------+   |
   Market  ---> |  | L1 TA  |  | L2 OnC |  | L3 Claude Sonnet 4.6 |     | ---> (conviction, direction)
   Data         |  | rules  |  | (Dune) |  |    (final arbiter)   |     |
                |  +--------+   +--------+   +----------------------+   |
                +-------------------------------------------------------+
                                       |
                                       v
                +-------------------------------------------------------+
                |  Position Manager  (two-tier, runs first per cycle)   |
                |  Fast Path : daily-DD / dyn-SL / vol-spike / time     |
                |              dyn-TP / partial-TP / trail / flip       |
                |              re-eval / breakeven-arm (state)          |
                |  Smart Path: Claude on material change ONLY           |
                |              (price>=1.5xATR | funding | whales | OI) |
                |              can override Fast Path verdict           |
                +-------------------------------------------------------+
                                       |
                                       v
                +-------------------------------------------------------+
                |                Allocation Router                      |
                |  conviction >= RISK_ON + direction != 0 -> Hyperliquid|
                |  conviction <= RISK_OFF                  -> USYC      |
                |  mid-band + strong direction             -> open      |
                |  mid-band + neutral                      -> hold      |
                +-------------------------------------------------------+
                                       |
                                       v
                +-------------------------------------------------------+
                |  Vol-targeted sizing  +  Gradient drawdown haircut    |
                | TRADING:  Hyperliquid Testnet (hyperliquid-python-sdk)|
                | TREASURY: Arc + Circle DCW + Paymaster + USYC + CCTP  |
                +-------------------------------------------------------+
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

| Level | Conviction formula                                              | Direction sign                                  |
|-------|-----------------------------------------------------------------|-------------------------------------            |
| L1    | `avg(per-symbol trend strength)` — no `0.5` floor               | Sign of primary symbol's trend                  |
| L2    | `max(2 × |heat − 0.5|, bias_strength)`                          | Sign of `market_bias` (bullish/bearish/neutral) |
| L3    | Claude Sonnet 4.6 `conviction ∈ [0, 1]` (strict JSON)           | Claude `direction ∈ {long, short, neutral}`     |

> 💡 **Why `max(2·|heat-0.5|, bias_strength)` for L2?** Heat alone is directional (0.85 = bullish, 0.15 = bearish), so it makes a bad *conviction* signal — both extremes are equally decisive on-chain. The `2·|heat-0.5|` term folds heat into a symmetric conviction; the `max(..., bias_strength)` term catches the case where heat sits near neutral but on-chain signals (funding, OI, whales) point decisively one way.

### Decision rules

```
conviction ≥ RISK_ON_THRESHOLD AND direction ≠ 0  →  open in direction (full intensity)
conviction ≤ RISK_OFF_THRESHOLD                   →  close everything (side-agnostic)
mid-band AND direction_strength ≥ STRONG_BIAS_OPEN →  open at reduced intensity
                                                       (½ × conviction × direction_strength)
mid-band AND direction_strength < STRONG_BIAS_OPEN →  hold
```

### Cascade — "always invite L3" (Day 5+)

The cascade no longer short-circuits at L1. **L2 and L3 are always invoked**, so Claude is given full situational awareness on every cycle — including the per-`(symbol, timeframe)` indicator snapshot L1 actually saw and a marginality-labelled list of the blocking reasons. The short-circuit logic moved to AFTER L3:

- **Hard L1 block** (`drawdown_breach`, `ohlcv_unavailable`) → forced risk-off. If L3 tried to risk_on anyway, a WARNING logs that the override was IGNORED. Drawdown and missing data are sacred.
- **Soft L1 block + real L3 with `conviction ≥ L3_OVERRIDE_MIN_CONVICTION` (0.55 default)** → engine bypasses the weighted aggregator and hands control to L3's verdict. Intensity is haircut by the **stacked-veto cap** (1 block → ×1.0, 2 → ×0.7, 3 → ×0.5, 4+ → ×0.35) so overriding multiple stacked vetoes shrinks position size proportionally.
- **Soft L1 block + L3 declined / synthetic L3** → short-circuit as if L1 had been honoured. Synthetic L3 (no `OPENROUTER_API_KEY`) is a function of L1+L2 and cannot honestly audit L1.
- **L1 passes** → normal weighted aggregation across L1/L2/L3.

Every override (executed, declined, or hard-block upheld) is logged into `DecisionResult.l1_override_meta` and rendered as an `L1 OVERRIDDEN BY L3` banner above the Final Decision panel so operators never see a mysterious risk_on after an L1 block.

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
├── main.py                       # Entry point: --dry-run / --live / --loop / --test-bias / --test-allocation
├── src/
│   ├── core/
│   │   ├── decision_engine.py    # Always-invite-L3 cascade + L1 override + stacked-veto haircut
│   │   ├── level1.py             # Technical rules + hard/soft taxonomy + marginality metadata
│   │   ├── level2.py             # On-chain intelligence (Dune MCP + optional Hyperliquid overlay)
│   │   └── level3.py             # Claude Sonnet 4.6 final arbiter (L3_MODE + L3_AGGRESSION + telemetry)
│   ├── data/
│   │   ├── dune_mcp.py                  # DuneMCPClient — Dune analytics backbone
│   │   ├── dune_market_data.py          # OHLCV adapter on dex.trades (L1 feed)
│   │   ├── hyperliquid_intelligence.py  # Real perp metrics from HL Info API (L2 overlay)
│   │   └── arc_onchain.py               # Arc RPC reader (account state only)
│   ├── execution/
│   │   ├── hyperliquid_executor.py  # PRIMARY trading venue — EIP-712 via hyperliquid-python-sdk
│   │   │                            #   dual Info clients: exec (testnet) + data (mainnet)
│   │   ├── position_manager.py      # TP / SL / trailing / side-flip / re-evaluation
│   │   ├── usyc_executor.py         # USYC mint / redeem (yield leg, optional via USYC_ENABLED)
│   │   ├── circle_wallet.py         # Circle DCW + Paymaster + RSA-OAEP; wait_for_tx 4xx fail-fast
│   │   └── arc_perp_executor.py     # Legacy — treasury moves + dry-run telemetry only
│   ├── allocation/
│   │   └── allocation_router.py  # Vol-targeted sizing + DD haircut + USYC rotation + position-review hook
│   ├── llm/
│   │   └── openrouter_client.py  # OpenRouter HTTP client (Level 3)
│   └── utils/
│       ├── config.py             # Pydantic settings (sole .env reader)
│       ├── console.py            # Eight-panel rich renderer (incl. L1-override banner, Position Review)
│       └── logging.py            # Loguru sinks
├── dune/
│   ├── README.md                 # SQL templates + column contracts (deep dive)
│   └── queries/                  # ohlcv.sql, funding_rates.sql, ... (9 templates)
├── prompts/                      # Level 3 arbiter system prompts
│   ├── level3_arbiter_critical.md   # Default — independent risk-manager voice + Day-6 decision matrix
│   └── level3_arbiter_standard.md   # Concise trader voice
├── scripts/                      # One-off ops (Circle wallet creation, etc.)
├── tests/                        # pytest + pytest-asyncio (110+ tests; HL exec test currently excluded)
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

### ✅ Day 5 — Trading-venue pivot + position manager + auto-allocation

The headline change: the agent now opens real long/short positions on **Hyperliquid Testnet**, while Arc + Circle keep the treasury & yield leg. The Arc Perp DEX matcher / EIP-712 `OrderTypes.Order` spec was never published, so building live trading against it was blocked indefinitely — Hyperliquid gives us a public testnet + faucet + maintained Python SDK + clean REST API and lets the rest of the architecture stay untouched.

- **`HyperliquidExecutor`** (`src/execution/hyperliquid_executor.py`)
  - EIP-712 L1 actions signed by the official [`hyperliquid-python-sdk`](https://github.com/hyperliquid-dex/hyperliquid-python-sdk) (msgpack-encoded action + phantom-agent domain). The agent never rolls its own crypto.
  - Single signer key (`HYPERLIQUID_PRIVATE_KEY`); agent-wallet mode via `HYPERLIQUID_ACCOUNT_ADDRESS`, vault mode via `HYPERLIQUID_VAULT_ADDRESS`.
  - Hard caps applied in the executor BEFORE the SDK call: `HYPERLIQUID_MAX_LEVERAGE` (5x), `HYPERLIQUID_MAX_POSITION_USD` ($10k), `HYPERLIQUID_DEFAULT_SLIPPAGE_BPS` (50bp). `use_market_orders=True` (default) routes through `market_open / market_close`; flip to `False` for limit orders at mid ± slippage.
  - Surface matches the legacy `ArcPerpExecutor` (`open_position / close_position / close_all_positions / get_position / get_account_info / get_pnl / get_margin / set_leverage / update_take_profit_stop_loss / get_mid_price`) so the router swaps venues via duck-typing (`PerpExecutorProtocol`).
  - Startup banner `Hyperliquid executor configured | api=... sdk=ready key_set=True signer_addr=0x... max_lev=5x max_pos=10000` printed in every mode (`--dry-run` / `--live` / `--test-allocation`).

- **`PositionManager` — Two-Tier (Fast Path + Smart Path)** (`src/execution/position_manager.py`)
  - **Fast Path** (always-on, deterministic, ATR-aware, runs every cycle). Priority ladder (first match wins): `daily_dd_guard > stop_loss > vol_spike_close > time_exit > take_profit > partial_take_profit > trailing_stop > side_flip > re_evaluation`. Plus state-only verbs (`breakeven_arm`, `vol_spike_warn`) that mutate per-position state without closing.
  - **Smart Path** (Claude Sonnet 4.6 via OpenRouter, event-triggered). Fires ONLY on material change: price moved ≥ `L3_REVIEW_TRIGGER_PRICE_ATR_MULT × ATR` since last check, `|funding| ≥ L3_REVIEW_TRIGGER_FUNDING_RATE`, `n_whales ≥ L3_REVIEW_TRIGGER_WHALE_COUNT`, `|oi_delta_1h_pct| ≥ L3_REVIEW_TRIGGER_OI_DELTA_PCT`, or scheduled refresh every `L3_REVIEW_MAX_INTERVAL_MINUTES`. Throttled by `L3_REVIEW_MIN_INTERVAL_MINUTES`. Returns a `SmartPathVerdict` (`hold` / `close_full` / `close_partial` / `tighten_stop` / `raise_target`) that **overrides** the Fast Path when `L3_CAN_OVERRIDE_FAST_PATH=true`.
  - **Dynamic ATR-based TP/SL** (`USE_DYNAMIC_ATR_TPSL=true`, default): TP / SL / trailing distances are multiples of the **live** ATR snapshot recomputed every cycle. Defaults: `SL_ATR_MULT=1.2`, `TP_ATR_MULT=3.0` (≈ 2.5:1 R:R), `TRAIL_ATR_MULT=1.5`. Falls back to fixed `STOP_LOSS_PCT` / `TAKE_PROFIT_PCT` when L1 ATR% is unavailable for that cycle — no silent failure.
  - **Partial take-profit** (`ENABLE_PARTIAL_TAKE_PROFIT=true`): scales out `PARTIAL_TP_FRACTION=0.50` (half) of the position at `PARTIAL_TP_ATR_MULT=1.5` × ATR, once per position lifetime. Combines well with breakeven + trailing on the residual.
  - **Breakeven arming** (`ENABLE_BREAKEVEN=true`): once profit ≥ `BREAKEVEN_TRIGGER_ATR_MULT=1.0` × ATR, lifts the effective SL to `entry ± BREAKEVEN_BUFFER_PCT=0.05%`. One-way: SL only tightens, never widens.
  - **Volatility filter** (`ENABLE_VOL_FILTER=true`): compares live ATR% vs the entry-time ATR snapshot; on `≥ VOL_SPIKE_MULT=1.8x` either closes (`VOL_SPIKE_ACTION=close`) or just tightens the next SL pass (`VOL_SPIKE_ACTION=tighten_stop`, default).
  - **Time-based exit** (`ENABLE_TIME_EXIT=true`): forces close after `MAX_POSITION_HOLD_HOURS=24` so stale ideas don't pile up.
  - **Daily-DD kill switch** (`ENABLE_DAILY_DD_GUARD=true`): tracks PnL since UTC midnight; on session loss ≥ `DAILY_LOSS_LIMIT_PCT=5%`, flattens everything, runs USYC rotation, and refuses ALL new opens until process restart. Portfolio-level state — persists across positions becoming flat.
  - **Per-asset ATR caps** (`PER_ASSET_ATR_CAP_BTC_1H=3.0`, `PER_ASSET_ATR_CAP_ETH_1H=4.0`, default `3.0`): router refuses new opens when live ATR% exceeds the per-asset 1h ceiling; the L3 critical-mode prompt enforces the same gate independently.
  - **Per-position state** (`_PositionState`): keeps `opened_at`, `entry_atr_pct/abs`, `peak_pnl_pct`, `breakeven_armed`, `partial_tp_done`, `last_l3_check_at/price`, `last_smart_verdict` per `(symbol, side)`. Garbage-collected every cycle when the key is no longer in the open-positions list.
  - **"HOLD must be earned"**: a `hold` verdict is a *conscious* outcome — the manager iterates every priority, considers Smart Path overrides, and only lands on `hold` when no trigger fired AND no override was warranted. Panel labels source (`FAST` / `SMART`) so operators can audit which tier authored the call.
  - **Tests.** `tests/test_position_manager.py` covers 40+ scenarios across the legacy ladder, the new dynamic-ATR ladder, daily-DD persistence, per-asset ATR cap surfacing, and Smart-Path gating + override.

- **`AllocationRouter` — full auto-allocation pipeline.**
  - Risk-on: redeem just enough USYC if Arc cash is short → `HyperliquidExecutor.open_position(...)` → stamp directive + sizing + (optional) reduce-only TP/SL orders.
  - Risk-off: `close_all_positions(...)` → withdraw margin → mint USYC with the leftover USDC (bounded by `[USYC_MIN_ROTATION_AMOUNT, USYC_MAX_ROTATION_AMOUNT]` and gated by `USYC_USDC_RESERVE_USD`).
  - USYC leg is **optional** — when the contracts aren't configured the router stamps `rotation_skipped` on the plan and still closes perps.
  - All hard overrides (drawdown breach, stale data, leverage cap, max-position cap) apply identically to longs and shorts.

- **Eighth rich CLI panel — `Position Review` (extended in Day-5+).** TP / SL / trailing-stop prices vs live mid, current PnL %, peak PnL %, **live ATR% vs per-asset cap**, **position age**, **per-position flags** (`BE` once breakeven armed, `pTP` once partial fired, `L3:hold|close|partial|tighten` once Smart Path ran), source-tagged trigger badges (`FAST` / `SMART`), and a **daily-DD banner** showing the session loss vs the configured limit with a `BREACHED` indicator when the kill switch is active.

- **Tests.** `tests/test_hyperliquid_executor.py` (fake SDK clients exercise open/close/get_position/get_mid_price/get_account_info on all paths) + `tests/test_position_manager.py` (all five triggers, peak-PnL reset, symmetric long/short, `from_settings` rounding).

### 🟡 Day 5.x — Live-test follow-ups (in progress)

Day 5 closed the architecture; the follow-up tickets cover what we learned running the agent **`--live --loop` on Hyperliquid Testnet** end-to-end with real Claude Sonnet 4.6 in critical mode. The day is **not** marked fully done — there are still calibration items pending.

- **Live test snapshot.** Agent opened several long BTC-PERP positions which Hyperliquid (a netting venue) collapsed into one per-account position with accumulated size. The operator manually flattened from the Hyperliquid UI to bound balance burn while L3's behaviour was still being calibrated. `PositionManager` ran each cycle as designed; `HyperliquidIntelligenceAdapter` produced real OI / funding / whale-activity reads; USYC was off for the duration.

- **L1 → L3 override architecture.** `Level1Reason.is_hard` flag declared at the source; per-reason marginality buckets (`marginal / moderate / decisive`) so L3 can tell "RSI at 70.2" from "RSI at 84". `DecisionEngine` now always invokes L2 and L3 even on an L1 block, enriches `ArbiterBriefing` with `l1_blocked_reasons` + `l1_indicators`, and only short-circuits AFTER L3 has spoken. Soft blocks are overrideable via `ALLOW_L3_TO_OVERRIDE_L1=true` + `L3_OVERRIDE_MIN_CONVICTION=0.55`; hard blocks (`drawdown_breach`, `ohlcv_unavailable`) are never overrideable. **Stacked-veto intensity haircut** caps L3-driven opens at `×1.0 / ×0.7 / ×0.5 / ×0.35` for 1 / 2 / 3 / 4+ stacked soft blocks. `DecisionResult.l1_override_meta` carries the full audit trail; the Final Decision panel renders an `L1 OVERRIDDEN BY L3` banner.

- **L3 calibration (Day-6 prompt rewrite + post-validation layer).** `ArbiterResponse.rationale` ceiling raised `4000` → `8000` chars after observing 6.8k-char rationales tripping safe-HOLD for a length-only reason. **STRICT RESPONSE LENGTH RULES** added at the top of the critical-mode prompt (≤ 3500 chars target). The "skeptical by default" principle was replaced with a concrete **default-action matrix** + **per-asset ATR caps** (cross-asset ATR no longer a veto) + **whale activity below `n<5` = noise** + **flat OI = neutral in continuation** + **`history_unavailable` is ignored**, not bearish. Post-validation `L3_AGGRESSION ∈ {conservative, balanced (default), aggressive}` multiplier scales `conviction` / `intensity` AFTER Pydantic validation; the `aggressive` mode adds a **HOLD-rescue rule** (flips a HOLD to a low-intensity OPEN when L1 passes AND L2 conviction ≥ `L3_HOLD_RESCUE_L2_MIN=0.65`). In-process `_L3Telemetry` counter tracks `total / held / opened / rescued_holds / raw_holds` per session.

- **`HyperliquidIntelligenceAdapter`** (`src/data/hyperliquid_intelligence.py`). Replaces the spot-derived L2 proxies for funding / OI / volume / cum-funding with real perp data from Hyperliquid's `meta_and_asset_ctxs` + `funding_history` Info endpoints. OI history isn't natively exposed, so the adapter maintains an in-process ring buffer per coin for `delta_1h / 4h / 24h` (warming-up horizons surface as `history_unavailable` rather than zero). Failures degrade gracefully per metric — the Dune proxy fills in any field the HL call couldn't produce — and provenance flips between `dune:<id>` and `hyperliquid:<endpoint>` per metric in the L2 panel. The adapter reads from **`HYPERLIQUID_DATA_API_URL` (mainnet by default)** — kept deliberately separate from the testnet execution venue so the data plane stays honest.

- **Operational knobs.** `DECISION_INTERVAL_SECONDS=600` (10-minute `--loop` cadence — tuned to bound OpenRouter and Dune API costs without missing meaningful moves on the 15m/1h L1 timeframes; set to `0` for back-to-back replay). `USYC_ENABLED=false` activates **perp-only mode** — `--live` pre-flight stops requiring the USYC addresses; risk-off still closes perps but skips the USDC → USYC mint; startup emits a clear `USYC leg disabled - running perp-only mode on Hyperliquid Testnet` INFO line. **Testing-loosened L1 defaults** clearly labelled "TESTING ONLY" in `.env`: `L1_ATR_PCT_MIN=0.05` (was 0.15), `L1_ATR_PCT_MAX=12.0` (was 6.0), `L1_REQUIRE_TF_AGREEMENT=false` (was true) — tighten back before mainnet.

- **Circle DCW noisy-poll fix.** After every Hyperliquid open, `main.py`'s post-cycle wait loop was calling `CircleWallet.wait_for_tx(<hyperliquid_order_id>)` and Circle returned 400 — the old code then retried for ~90 s spamming WARNINGs. Fixed at two layers: (1) `main.py` `_is_circle_tx_id` UUID discriminator + extended skip set covering already-terminal states like `CONFIRMED` / `COMPLETE`; (2) `CircleWallet.wait_for_tx` now fails fast on HTTP 4xx with a single ERROR line. Covered by `tests/test_circle_wallet_wait.py` (8 regression tests).

- **What's still pending in 5.x.** Calibration of `L3_AGGRESSION` defaults from live-test telemetry; durable trailing-stop state across restarts; per-symbol routing so multiple symbols don't net into one position; mainnet roll-out plan.

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

# 🔁 Loop every DECISION_INTERVAL_SECONDS (default 600 s / 10 min):
python main.py --loop

# 🔴 Live execution. Requires HYPERLIQUID_PRIVATE_KEY (testnet, fauceted from
#    https://app.hyperliquid-testnet.xyz) + Circle DCW (CIRCLE_API_KEY,
#    CIRCLE_ENTITY_SECRET, CIRCLE_AGENT_WALLET_ID). USYC addresses are only
#    required when USYC_ENABLED=true (set USYC_ENABLED=false for clean
#    perp-only live testing):
python main.py --live

# 🔴🔁 Most common live setup: continuous trading with the 10-min cadence.
python main.py --live --loop
```

> ⚠️ **Operator note from the Day-5 live test.** Hyperliquid is a **netting
> venue** — consecutive same-side opens collapse into one accumulated
> position per `(symbol, side)`, they do NOT stack as separate positions.
> Always keep an eye on the position in the [Hyperliquid Testnet
> dashboard](https://app.hyperliquid-testnet.xyz) when running `--live --loop`;
> the operator can manually flatten any position from the UI at any
> moment without confusing the agent (next cycle will simply see "no open
> positions" and either hold or re-enter based on fresh signals).

### 3a. Important env knobs

| Variable | Default | Purpose |
|----------|---------|---------|
| `DECISION_INTERVAL_SECONDS` | `600` | Sleep between cycles in `--loop` mode. `0` removes the sleep (back-to-back replay). |
| `USYC_ENABLED` | `true` in code; `false` in checked-in `.env` for current testing | When `false`, the USYC mint/redeem leg is skipped and `--live` pre-flight no longer requires the USYC addresses. |
| `HYPERLIQUID_API_URL` | `https://api.hyperliquid-testnet.xyz` | Execution venue + own-account state. |
| `HYPERLIQUID_DATA_API_URL` | `https://api.hyperliquid.xyz` (**mainnet**) | Market-wide intelligence (asset universe, OI, funding) for the L2 overlay. Kept independent so the data plane stays honest when execution is testnet. |
| `HYPERLIQUID_INTELLIGENCE_ENABLED` | `true` | Master switch for the `HyperliquidIntelligenceAdapter` overlay on L2. Flip to `false` for a pure-Dune A/B comparison. |
| `L3_MODE` | `critical` | `critical` (5-section structured rationale) or `standard` (concise trader voice). |
| `L3_AGGRESSION` | `balanced` | `conservative` / `balanced` / `aggressive`. `aggressive` enables the HOLD-rescue rule. |
| `L3_HOLD_RESCUE_L2_MIN` | `0.65` | Minimum L2 conviction needed for HOLD-rescue to fire (only under `aggressive`). |
| `L3_HOLD_RESCUE_INTENSITY` | `0.30` | Position intensity used when HOLD-rescue flips a HOLD to OPEN. |
| `ALLOW_L3_TO_OVERRIDE_L1` | `true` | Master switch for letting L3 audit & override soft L1 blocks. |
| `L3_OVERRIDE_MIN_CONVICTION` | `0.55` | Minimum Claude conviction needed to override an L1 soft block. |
| `L1_ATR_PCT_MIN` / `L1_ATR_PCT_MAX` | `0.05` / `12.0` (**TESTING ONLY**; production = `0.15` / `6.0`) | Soft block band on ATR%. Loosened for the Day-5 live test; tighten back before mainnet. |
| `L1_REQUIRE_TF_AGREEMENT` | `false` (**TESTING ONLY**; production = `true`) | Require 15m + 1h trend agreement. Loosened to allow trading on currently-quiet markets during testing. |

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

### 4b. Offline `PositionManager` tester (`--test-allocation`)

Probe the position manager's trigger ladder against synthetic open positions — no Dune, no Circle, no Hyperliquid SDK calls. Pick a scenario:

```bash
# Position with +5% PnL — TAKE_PROFIT fires
python main.py --test-allocation take_profit

# Position with -3% PnL — STOP_LOSS fires
python main.py --test-allocation stop_loss

# Open long while engine emits a SHORT directive — SIDE_FLIP fires
python main.py --test-allocation side_flip

# Mildly profitable position, conviction collapses below MIN_CONVICTION_TO_HOLD
python main.py --test-allocation re_evaluation
```

Every scenario prints the new Position Review panel with the live trigger badge so you can verify TP / SL / trailing prices are computed correctly before sending real orders to Hyperliquid.

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
│  → final) + USYC rotation legs.        │
└────────────────────────────────────────┘
┌── Position Review (PositionManager) ───┐
│  per open position: side, size, PnL%,  │
│  peak PnL, TP/SL/trailing prices vs    │
│  live mid, trigger badge (HOLD / SL /  │
│  TP / TRAILING / FLIP / RE-EVAL).      │
└────────────────────────────────────────┘
┌── On-chain Result ─────────────────────┐
│  tx_id, state, hash, sponsored,        │
│  Hyperliquid + Arc explorer links.     │
└────────────────────────────────────────┘
```

---

## 🗺️ Roadmap

| Phase | Status | Highlights |
|-------|--------|------------|
| **Day 1** | ✅ | Scaffolding, secret hygiene, 3-level interfaces |
| **Day 2** | ✅ | Circle DCW + Paymaster live; Arc Perp DEX margin moves on Testnet |
| **Day 3** | ✅ | L1 + L2 wired to Dune MCP (9/9 queries live); conviction/direction split; vol-targeted sizing; gradient drawdown haircut |
| **Day 4** | ✅ | Real Claude Sonnet 4.6 L3 arbiter via OpenRouter; two `L3_MODE` personas (`critical` default with 5-section rationale + veto authority, `standard` for trader-voice); strict JSON verdict + Pydantic; safe HOLD fallback; seventh rich panel with mode badge |
| **Day 5** | ✅ | **Trading-venue pivot to Hyperliquid Testnet** (`HyperliquidExecutor` via official `hyperliquid-python-sdk`); **`PositionManager`** (TP / SL / trailing / side-flip / re-evaluation); **auto-allocation pipeline** (risk-on opens on Hyperliquid + USYC redeem if cash-short; risk-off closes perps + withdraws margin + mints USYC); **eighth Position Review CLI panel**; Arc + Circle retained as treasury & yield leg |
| **Day 5.x** | 🟡 | **Live-test follow-ups (in progress).** L1 → L3 override architecture with hard/soft taxonomy + marginality + stacked-veto haircut + `L1 OVERRIDDEN BY L3` banner; L3 calibration (rationale ceiling `4000 → 8000`, STRICT LENGTH RULES, decision matrix, per-asset ATR caps, `L3_AGGRESSION` + HOLD-rescue + telemetry); `HyperliquidIntelligenceAdapter` (real perp data from `metaAndAssetCtxs` + `fundingHistory`); operational knobs (`DECISION_INTERVAL_SECONDS=600`, `USYC_ENABLED=false` perp-only mode, testing-loosened L1 defaults); Circle DCW `wait_for_tx` 4xx fail-fast + UUID discriminator |
| **Day 6+** | 🚧 | Calibration of `L3_AGGRESSION` defaults from live telemetry; mainnet roll-out plan; per-symbol routing (open BTC long while ETH stays flat); durable trailing-stop state across restarts; JSONL decision log for replay / backtest; arc-native Dune dataset once Arc Testnet is indexed |

---

## 🧰 Tech Stack

**Trading venue**
- 🎯 **Hyperliquid Testnet** — primary perp venue for real long/short positions, signed via the official [`hyperliquid-python-sdk`](https://github.com/hyperliquid-dex/hyperliquid-python-sdk) (EIP-712 L1 actions over the phantom-agent domain, `POST /exchange` + `POST /info`)

**Treasury & yield (Arc + Circle)**
- 🏗️ **Arc** stablechain (Testnet today, mainnet on launch) — settlement layer for the yield leg
- 🌉 **CCTP v2** — cross-chain USDC routing (Arc ⇄ Arbitrum / Base when funding / defunding Hyperliquid margin)
- 🔐 **Developer-Controlled Wallets** — non-custodial programmable signing on Arc
- ⛽ **Circle Paymaster** — gasless / sponsored transactions on Arc
- 🪙 **USYC** — yield-bearing tokenized USDC (risk-off leg via `USYCExecutor`)
- 🏛️ **Arc Perp DEX contracts** (`ClearingHouse` / `USDCCollateralVault` / `MarketRegistry` / `PositionLedger`) — retained for treasury moves and dry-run telemetry only; production routing is on Hyperliquid

**Intelligence & data**
- 🔭 **Dune MCP** — single source of truth for L1 OHLCV + L2 on-chain intelligence (Ethereum / Base / Arbitrum supported out of the box)
- 🧱 **Arc RPC** — account state only (wallet, vault TVL, agent margin on Arc)
- 🤖 **Claude Sonnet 4.6** (via **OpenRouter**) — Level 3 final arbiter with strict-JSON verdict (Pydantic-validated)

**Backend**
- 🐍 Python 3.10+
- `hyperliquid-python-sdk` — Hyperliquid EIP-712 signing + REST client (hard runtime dependency)
- `web3.py`, `eth-account`, `eth-abi` — chain interaction
- `httpx`, `aiohttp`, `tenacity` — async HTTP with rate-limit retries
- `pydantic`, `pydantic-settings` — strongly-typed config
- `pandas`, `numpy`, `ta` — L1 indicator math
- `mcp`, `dune-client` — L2 on-chain data
- `httpx` (above) — L3 OpenRouter HTTP client
- `loguru` — structured logging
- `rich` — eight-panel terminal UI (incl. Position Review)
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
