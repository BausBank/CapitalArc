```text
=== CapitalArc — Autonomous AI Agent on Arc ===
- - - - - - - - - - - - - - - - - - - - - - - -
  Smart capital management on Arc & Perp DEX 
```

[![Agora Agents Hackathon](https://img.shields.io/badge/Agora-Agents%20Hackathon-blueviolet)](https://www.canteen.xyz/)
[![Built on Arc × Circle](https://img.shields.io/badge/Built%20on-Arc%20%C3%97%20Circle-0052FF)](https://www.circle.com/)

---

> **🎯 RFB 01 — Perpetual Futures Trading Agent.**
> A two-layer build: leveraged positions executed on **Hyperliquid**;
> collateral, settlement, accountability and yield rotated through **Arc + Circle**.
> The AI decides *when to act, which side, how hard* — 24/7, fully on-chain, fully observable.

---

## `[ OVERVIEW ]`

**CapitalArc** is a fully autonomous AI agent that trades **perpetual futures on Hyperliquid Testnet** while using **Arc + Circle** as its settlement, collateral and accountability layer. A **cascading three-level decision engine** — deterministic technicals (L1) → on-chain intelligence (L2) → **Claude Sonnet 4.6** arbiter (L3) — separates **conviction** ("do we act?") from **direction** ("which side?") on every cycle, then ships the verdict through a **two-tier Position Manager** (fast deterministic rails + LLM-driven self-correction) and a **vol-targeted Allocation Router** that opens longs and shorts symmetrically, with dynamic ATR-based TP/SL, breakeven arming, partial take-profit, trailing stops and a portfolio-wide daily-drawdown kill switch. On risk-off the perp book is flattened, margin is withdrawn back to Arc, and **the architecture is designed to support** rotation of the freed USDC into **USYC** (yield-bearing tokenized USDC) through **Circle DCW + Paymaster + CCTP v2** — non-custodial signing is live today; gasless settlement and cross-chain collateral routing are **wired and ready for activation** as the next phase on the Arc + Circle leg.

Every decision is **idempotent** (keyed by a `decision_id`), **observable** (eight-panel rich CLI with per-level scores, effective weights, sizing pipeline and position telemetry), and **honest** (every market metric carries a `dune:<id>` / `hyperliquid:<endpoint>` provenance tag — the agent never invents data).

---

## `[ KEY FEATURES ]`

### 🧠 Three-level cascading decision engine with L3 override
Deterministic technicals (L1, OHLCV via Dune) feed on-chain intelligence (L2, real Hyperliquid perp data + Dune flow analytics) which feed **Claude Sonnet 4.6** as final arbiter (L3, via OpenRouter, strict JSON contract, Pydantic-validated). L3 receives the full L1+L2 briefing including L1 block reasons and indicator snapshots, so it can **audit and override soft L1 vetoes** when conviction ≥ `L3_OVERRIDE_MIN_CONVICTION` (0.55). Hard blocks (`drawdown_breach`, `ohlcv_unavailable`) are sacred and never overrideable. Overriding multiple stacked soft blocks triggers an automatic **stacked-veto haircut** (1→×1.0, 2→×0.7, 3→×0.5, 4+→×0.35) so position size shrinks proportionally with the number of vetoes ignored.

### ⚖️ Decoupled conviction & direction (longs and shorts are symmetric)
Every level votes on **two orthogonal axes**: conviction ∈ `[0,1]` ("how strongly?") and direction ∈ `{-1, 0, +1}` ("which side?"). Direction is aggregated as a **conviction-weighted vote**, so a wishy-washy level can't drag the side. Result: a high-conviction bearish setup correctly opens a SHORT instead of being mistaken for "low conviction → close everything". The engine has no bullish bias baked in.

### 🎯 Hyperliquid as the execution venue, Arc as the accountability layer
Real long/short perp positions signed via the official `hyperliquid-python-sdk` (EIP-712 phantom-agent L1 actions over `POST /exchange`). Hard caps enforced **before** the SDK call: `HYPERLIQUID_MAX_LEVERAGE=5×`, `HYPERLIQUID_MAX_POSITION_USD=$10k`, `HYPERLIQUID_DEFAULT_SLIPPAGE_BPS=50bp`. Two independent Info clients keep the data plane honest: testnet execution + mainnet market intelligence. **Arc + Circle anchor the agent's decisions, fills and PnL on-chain**: Circle Developer-Controlled Wallets (DCW) with **RSA-OAEP-SHA256 entity-secret encryption** are live today for non-custodial signing; **the architecture is designed to support** Circle Paymaster for gasless settlement and CCTP v2 for cross-chain USDC collateral movement — both are **wired and ready for activation** as the next phase, beyond the current perp-only live test.

### 🛠️ Two-tier Position Manager — Fast Path (deterministic, ms) + Smart Path (Claude, event-triggered)
Runs **before** the regime dispatch on every cycle. The **Fast Path** is a 9-trigger priority ladder (first match wins): `daily_dd_guard > stop_loss > vol_spike_close > time_exit > take_profit > partial_take_profit > trailing_stop > side_flip > re_evaluation`, all ATR-aware (TP/SL recomputed every cycle from the live ATR snapshot), with state-only verbs (`breakeven_arm`, `vol_spike_warn`). The **Smart Path** invokes Claude Sonnet 4.6 **only on material change** (price moved ≥ 1.5×ATR, funding spike, whale activity, OI delta, scheduled refresh) and can override the Fast Path verdict — gated by cost-safety rails (`LLM_MAX_PER_CYCLE=2`, `LLM_MAX_PER_HOUR=8`, hard 25-minute per-position cooldown floor) and a veto-only mode under `conservative` aggression. *"HOLD must be earned"* — no silent fall-through.

### 📊 Volatility-targeted sizing + gradient drawdown haircut (not cliffs)
`size = (equity × risk_per_trade) / (stop_atr_mult × ATR%/100)` — the Kelly-fraction-style recipe used by systematic CTAs. Then **× directive intensity**, then **× drawdown haircut** `1 − (dd/max_dd)²` (5% dd → 75% size; 9% dd → 19% size; 10% dd → hard close), then capped at `MAX_POSITION_USD`. Every plan stamps the full sizing breakdown onto the Execution Plan so the demo visibly answers *"why this size?"* line by line.

### 🔁 Auto-allocation pipeline — perps ↔ USYC rotation
**Risk-on:** redeem just enough USYC if Arc cash is short → `Hyperliquid.open_position(...)` → stamp directive + sizing + reduce-only TP/SL intent.
**Risk-off:** `close_all_positions(...)` → withdraw margin → mint USYC with the leftover USDC (bounded by `USYC_USDC_RESERVE_USD` + min/max rotation amounts).
**Hold:** no on-chain change, but the Position Manager already ran any TP/SL/trailing trigger that was due.
The Hyperliquid trading leg runs live on Testnet today; the **USYC rotation leg is wired end-to-end** (`USYCExecutor.mint/redeem`, `decision_id` stamping, `rotation_skipped` telemetry) and gated by `USYC_ENABLED` — **wired and ready for activation** in the next phase, currently kept `false` for a clean perp-only live test.

### 🌐 Honest data: Hyperliquid perp intelligence + Dune analytical backbone
L2's funding / OI / volume / cum-funding read live from Hyperliquid's `metaAndAssetCtxs` + `fundingHistory` endpoints (in-process ring buffer fills in 1h/4h/24h OI deltas the API doesn't natively expose). The Dune MCP backbone provides OHLCV, whale activity, vault flows, L/S ratio and market sentiment — chain-portable via `DUNE_CHAIN=ethereum|base|arbitrum` (one-line switch; SQL templates parameterised by chain + token addresses). **Every metric** carries a `dune:<id>` / `hyperliquid:<endpoint>` / `n/a` / `error` tag — the agent never silently invents numbers.

### 🧑‍⚖️ L3 personas + aggression calibration
`L3_MODE=critical` (default — senior risk-manager persona with veto authority, Day-6 decision matrix, per-asset ATR caps, mandatory 5-section rationale `Market Context → Key Signals Analysis → Contradictions & Risks → My Independent View → Final Recommendation`) or `L3_MODE=standard` (concise trader voice). `L3_AGGRESSION ∈ {conservative, balanced, aggressive}` is a post-validation multiplier on conviction/intensity; `aggressive` adds a **HOLD-rescue** rule (flips HOLD → low-intensity OPEN when L1 passes AND L2 conviction ≥ `L3_HOLD_RESCUE_L2_MIN=0.65`). In-process `_L3Telemetry` tracks held / opened / rescued so operators can spot over-conservatism in live runs.

### 🛟 Liquidation protection by construction
Five independent rails: (1) hard caps (5× leverage, $10k max position) enforced in the executor before any SDK call; (2) gradient drawdown haircut on size; (3) per-asset ATR ceiling (BTC ≤ 3%, ETH ≤ 4% on 1h) refuses new opens in violent regimes; (4) dynamic ATR-based stop-loss with breakeven arming; (5) portfolio-wide **daily-DD kill switch** flattens everything and refuses new opens on session loss ≥ `DAILY_LOSS_LIMIT_PCT=5%` until process restart. No silent failures: every guard is logged and surfaced in the CLI.

---

## `[ ARCHITECTURE ]`

CapitalArc is documented as **eight architectural diagrams** that together describe (1) the system topology, (2) the cycle, (3) the agent's cognition, (4) the decision cascade, (5) the L3 override, (6) the Position Manager tiers, (7) the execution & treasury split, and (8) the full capital flow.

---

### `=== DIAGRAM 1 — FULL SYSTEM ARCHITECTURE ===`

The complete top-down view: data sources → decision engine → position manager → allocation router → execution → on-chain feedback.

```text
[ Market Context ]
(OHLCV 15m+1h + Account State + L2 Payload)
        │
        ▼
[ LEVEL 1 — Technical Hard Rules ]  (level1.py)
        │
        ├─► HARD BLOCK triggered?
        │      (drawdown ≥ MAX_DRAWDOWN_PCT OR ohlcv_unavailable)
        │      → SHORT-CIRCUIT → conviction=0, regime=risk_off
        │      → L3 CANNOT override
        │
        └─► SOFT BLOCK detected?
               (trend_mixed, RSI extreme, ATR out of band, etc.)
               → reasons[] + marginality + score + direction_sign
        │
        ▼
[ LEVEL 2 — On-chain Intelligence ]  (level2.py)
        │
        ├─► Calculates market heat [0..1] + bias_strength
        └─► conviction = max(2·|heat−0.5|, bias_strength)
        │   direction = sign(market_bias)
        │
        ▼
[ LEVEL 3 — Claude Sonnet 4.6 ]  (level3.py + OpenRouter)
        │
        Receives full briefing:
        • L1 scores + soft blocks + reasons + marginality
        • Complete L2 payload (funding, OI deltas, whales, heat)
        • Current market context
        │
        ├─► Can L3 OVERRIDE L1/L2?
        │      Condition: L3 conviction ≥ L3_OVERRIDE_MIN_CONVICTION (0.55)
        │      AND ALLOW_L3_TO_OVERRIDE_L1 = true
        │      → YES: Agent "changes its mind"
        │          → Applies stacked-veto haircut:
        │             1 soft block → ×1.00
        │             2 soft blocks → ×0.70
        │             3 soft blocks → ×0.50
        │             4+ soft blocks → ×0.35
        │
        └─► NO override → weighted aggregation (L1 25% + L2 35% + L3 40%)
        │
        ▼
[ Decision Engine + Aggregator ]  (decision_engine.py)
        │
        ├─► final_conviction + direction_score
        │
        ├─► ≥ 0.60 + dir ≠ 0          → RISK_ON
        ├─► ≤ 0.40                     → RISK_OFF
        ├─► mid-band + |dir_strength| ≥ 0.60 → STRONG-DIR override
        └─► mid-band + |dir_strength| < 0.60 → HOLD
        │
        ▼
[ POSITION MANAGER ]  (position_manager.py)
        │
        ├─► Fast Path (always runs, ~ms)
        │      ①–⑨ deterministic rules + breakeven_arm
        │
        └─► Smart Path (if gate triggered)
               → Claude review + cost-safety rails + veto-only mode
               → can change previous decision
                  (HOLD-rescue, override Fast Path, etc.)
        │
        ▼
Final Action → Allocation Router
```

---

### `=== DIAGRAM 2 — ONE FULL TRADING CYCLE ===`

End-to-end walkthrough of a single `--live --loop` iteration: data collection → 3-level cascade → position review → allocation → execution → on-chain feedback → next cycle.

```text
                                   [ START OF NEW CYCLE ]
                             main.py --live --loop  (every ~600 seconds)
                                           │
                                           ▼
   ┌─────────────────────────────────────────────────────────────────────────────┐
   │  1. DATA COLLECTION LAYER                                                   │
   └─────────────────────────────────────────────────────────────────────────────┘
                    │
                    ├─► DuneMCPClient → OHLCV 15m+1h, funding, OI, L/S, whales, heat
                    ├─► HyperliquidIntelligenceAdapter → real perp data + OI ring buffer
                    └─► ArcOnchainReader ★ ARC → equity_usd, drawdown_pct, USDC balance
                                           │
                                           ▼
   ┌─────────────────────────────────────────────────────────────────────────────┐
   │  2. LEVEL 1 — Technical Hard Rules  (level1.py)                             │
   └─────────────────────────────────────────────────────────────────────────────┘
                    │
                    ├─► HARD BLOCK? → SHORT-CIRCUIT (conviction=0, risk_off)
                    └─► SOFT BLOCK? → reasons[] + marginality + score + direction_sign
                                           │
                                           ▼
   ┌─────────────────────────────────────────────────────────────────────────────┐
   │  3. LEVEL 2 — On-chain Intelligence  (level2.py)                            │
   └─────────────────────────────────────────────────────────────────────────────┘
                    │
                    ├─► market heat [0..1] + bias_strength
                    └─► conviction + direction calculated
                                           │
                                           ▼
   ┌─────────────────────────────────────────────────────────────────────────────┐
   │  4. LEVEL 3 — Claude Sonnet 4.6  (level3.py + OpenRouter)                   │
   └─────────────────────────────────────────────────────────────────────────────┘
                    │
                    ├─► Receives full briefing (L1 + L2 + Market Context)
                    │
                    ├─► Can OVERRIDE L1 soft blocks? (conv ≥ 0.55)
                    │      → YES → stacked-veto haircut applied
                    └─► NO → weighted aggregation (L1 25% + L2 35% + L3 40%)
                                           │
                                           ▼
   ┌─────────────────────────────────────────────────────────────────────────────┐
   │  5. DECISION ENGINE + AGGREGATOR  (decision_engine.py)                      │
   └─────────────────────────────────────────────────────────────────────────────┘
                    │
                    ├─► final_conviction + direction_score
                    │
                    ├─► RISK_ON  (≥ 0.60 + dir ≠ 0)
                    ├─► RISK_OFF (≤ 0.40)
                    ├─► STRONG-DIR override
                    └─► HOLD
                                           │
                                           ▼
   ┌─────────────────────────────────────────────────────────────────────────────┐
   │  6. POSITION MANAGER  (position_manager.py)                                 │
   └─────────────────────────────────────────────────────────────────────────────┘
                    │
                    ├─► Fast Path (always runs)
                    │      ①–⑨ deterministic rules + breakeven_arm
                    │
                    └─► Smart Path (if gate triggered)
                           → Claude review + cost rails + veto-only mode
                           → can change mind (HOLD-rescue, override Fast Path)
                                           │
                                           ▼
   ┌─────────────────────────────────────────────────────────────────────────────┐
   │  7. ALLOCATION ROUTER  (allocation_router.py)                               │
   └─────────────────────────────────────────────────────────────────────────────┘
                    │
                    ├─► Vol-targeted sizing + regime dispatch
                    │
                    ├─► RISK_ON  → open_position + TP/SL
                    ├─► RISK_OFF → close_all + withdraw + USYC mint
                    └─► HOLD     → no transaction
                                           │
                                           ▼
   ┌─────────────────────────────────────────────────────────────────────────────┐
   │  8. EXECUTION LAYER                                                         │
   └─────────────────────────────────────────────────────────────────────────────┘
                    │
                    ├─► HyperliquidExecutor (EIP-712 signing + POST /exchange)
                    ├─► Circle DCW ★ ARC/CIRCLE + Paymaster + CCTP
                    └─► USYCExecutor ★ ARC (mint/redeem if enabled)
                                           │
                                           ▼
   ┌─────────────────────────────────────────────────────────────────────────────┐
   │  9. LOGGING + FEEDBACK                                                      │
   └─────────────────────────────────────────────────────────────────────────────┘
                    │
                    ├─► ExecutionPlan logged (decision_id, action, extra)
                    ├─► ArcOnchainReader updated (equity, drawdown, balances)
                    └─► Stats updated (opens, holds, rescues, overrides)
                                           │
                                           ▼
         └─────────────────────── LOOP BACK TO NEXT CYCLE ───────────────────────┘
```

---

### `=== DIAGRAM 3 — HOW THE AGENT THINKS & CAN CHANGE ITS MIND ===`

The self-correction loop. L3 doesn't just rubber-stamp L1+L2 — given a marginality-labelled veto list and the full indicator snapshot, it can **revise** the verdict. The cascade *always invites L3*.

```text
                                   [ Market Context ]
                             (OHLCV 15m+1h + Account State + L2 Payload)
                                           │
                                           ▼
   ┌─────────────────────────────────────────────────────────────────────────────┐
   │  LEVEL 1 — Technical Hard Rules  (level1.py)                                │
   └─────────────────────────────────────────────────────────────────────────────┘
                    │
                    ├─► HARD BLOCK triggered?
                    │      (drawdown ≥ MAX_DRAWDOWN_PCT OR ohlcv_unavailable)
                    │      → SHORT-CIRCUIT → conviction=0, regime=risk_off
                    │      → L3 CANNOT override
                    │
                    └─► SOFT BLOCK detected?
                           (trend_mixed, RSI extreme, ATR out of band, etc.)
                           → reasons[] + marginality + score + direction_sign
                                           │
                                           ▼
   ┌─────────────────────────────────────────────────────────────────────────────┐
   │  LEVEL 2 — On-chain Intelligence  (level2.py)                               │
   └─────────────────────────────────────────────────────────────────────────────┘
                    │
                    ├─► Calculates market heat [0..1] + bias_strength
                    └─► conviction = max(2·|heat−0.5|, bias_strength)
                        direction = sign(market_bias)
                                           │
                                           ▼
   ┌─────────────────────────────────────────────────────────────────────────────┐
   │  LEVEL 3 — Claude Sonnet 4.6  (level3.py + OpenRouter)                      │
   └─────────────────────────────────────────────────────────────────────────────┘
                    │
                    Receives full briefing:
                    • L1 scores + soft blocks + reasons + marginality
                    • Complete L2 payload (funding, OI deltas, whales, heat)
                    • Current market context
                    │
                    ├─► Can L3 OVERRIDE L1/L2?
                    │      Condition: L3 conviction ≥ L3_OVERRIDE_MIN_CONVICTION (0.55)
                    │      AND ALLOW_L3_TO_OVERRIDE_L1 = true
                    │      → YES: Agent "changes its mind"
                    │          → Applies stacked-veto haircut:
                    │             1 soft block → ×1.00
                    │             2 soft blocks → ×0.70
                    │             3 soft blocks → ×0.50
                    │             4+ soft blocks → ×0.35
                    │
                    └─► NO override → weighted aggregation
                                           │
                                           ▼
   ┌─────────────────────────────────────────────────────────────────────────────┐
   │  Decision Engine + Aggregator  (decision_engine.py)                         │
   └─────────────────────────────────────────────────────────────────────────────┘
                    │
                    ├─► final_conviction + direction_score
                    │
                    ├─► ≥ 0.60 + dir ≠ 0          → RISK_ON
                    ├─► ≤ 0.40                     → RISK_OFF
                    ├─► mid-band + |dir_strength| ≥ 0.60 → STRONG-DIR override
                    └─► mid-band + |dir_strength| < 0.60 → HOLD
                                           │
                                           ▼
   ┌─────────────────────────────────────────────────────────────────────────────┐
   │  POSITION MANAGER  (position_manager.py)                                    │
   └─────────────────────────────────────────────────────────────────────────────┘
                    │
                    ├─► Fast Path (always runs, ~ms)
                    │      ①–⑨ deterministic rules + breakeven_arm
                    │
                    └─► Smart Path (if gate triggered)
                           → Claude review + cost-safety rails + veto-only mode
                           → can **change previous decision**
                              (HOLD-rescue, override Fast Path, etc.)
                                           │
                                           ▼
                             Final Action → Allocation Router
```

---

### `=== DIAGRAM 4 — DECISION ENGINE CASCADE (L1 → L2 → L3 → FINAL DECISION) ===`

The compact view of the cascade and the final routing rules. Every level emits `(conviction, direction)`; the engine aggregates direction as a conviction-weighted vote.

```text
                                   [ Market Context ]
                             (OHLCV + account state + L2 payload)
                                           │
                                           ▼
   Level 1 (level1.py)
                    │
                    ├─► HARD BLOCK (drawdown_breach ≥ MAX_DRAWDOWN_PCT=10% or ohlcv_unavailable)
                    │      → SHORT-CIRCUIT → final_conviction=0, regime=risk_off (L3 cannot override)
                    └─► SOFT BLOCK (trend_mixed, RSI extremes, ATR bounds)
                           → reasons[] + marginality
                                           │
                                           ▼
   Level 2 (level2.py)
                    │ • market heat [0..1]
                    │ • bias_strength
                    │ • conviction = max(2·|heat−0.5|, bias_strength)
                    │ • direction = sign(market_bias)
                                           │
                                           ▼
   Level 3 — Claude Sonnet 4.6 (level3.py)
                    │ Receives full briefing:
                    │ • L1 scores + soft blocks + reasons + marginality
                    │ • Complete L2 payload (funding, OI deltas, whales, heat, etc.)
                    │ • Market context
                    │
                    ├─► If L3 conviction ≥ L3_OVERRIDE_MIN_CONVICTION (0.55) and ALLOW_L3_TO_OVERRIDE_L1=true
                    │      → OVERRIDE L1/L2
                    │      → applies stacked-veto haircut:
                    │         1 soft block → ×1.00
                    │         2 soft blocks → ×0.70
                    │         3 soft blocks → ×0.50
                    │         4+ soft blocks → ×0.35
                    └─► Otherwise → weighted aggregation (L1 25% + L2 35% + L3 40%)
                                           │
                                           ▼
   Decision Engine (decision_engine.py)
                    │ final_conviction + direction_score
                    │
                    ├─► ≥ 0.60 + direction ≠ 0 → RISK_ON
                    ├─► ≤ 0.40                → RISK_OFF
                    ├─► 0.40 < conv < 0.60 + |dir_strength| ≥ 0.60 → STRONG-DIR override
                    └─► mid-band + |dir_strength| < 0.60 → HOLD
```

---

### `=== DIAGRAM 5 — L3 OVERRIDE & SELF-CORRECTION MECHANISM ===`

The compact view of *where the agent gets its judgement*. L3 is not a tiebreaker — it's a final arbiter with veto authority over soft L1 blocks, governed by a strict conviction threshold and a stacked-veto haircut.

```text
   L1 + L2 give their verdict
           │
           ▼
   L3 receives full briefing
   (L1 scores + soft blocks + reasons + marginality
    + complete L2 payload + market context)
           │
           ├─► L3 conviction ≥ 0.55  ?  → OVERRIDE L1/L2
           │      → stacked-veto haircut applied
           │            1 block → ×1.00
           │            2 blocks → ×0.70
           │            3 blocks → ×0.50
           │            4+ blocks → ×0.35
           │      → BUT: hard L1 blocks (drawdown / ohlcv) NEVER overrideable
           │
           └─► Otherwise → weighted aggregation (L1 0.25 / L2 0.35 / L3 0.40)
           │
           ▼
   Agent "changes its mind" here
   (every override logged into DecisionResult.l1_override_meta,
    rendered as an `L1 OVERRIDDEN BY L3` banner in the CLI)
```

---

### `=== DIAGRAM 6 — POSITION MANAGER (FAST PATH + SMART PATH) ===`

Stewardship is a separate concern from allocation. Each cycle, every open position is reviewed first by the deterministic Fast Path (~ms), and only on a material change does the Smart Path call Claude — gated by hard cost-safety rails.

```text
                                     [ New Cycle ]
                          (router calls review_open_positions())
                                           │
                                           ▼
                    get_open_positions() → fresh pull
                                           │
                                           ▼
                    Any open positions?
                                           │
                    ├─► No → go directly to Allocation Router
                    └─► Yes → Fast Path (always runs, ~milliseconds)
                           │
                           ① daily_dd_guard  (session loss ≥ DAILY_LOSS_LIMIT_PCT → CLOSE ALL)
                           ② stop_loss       (dynamic ATR or % fallback, breakeven_arm)
                           ③ vol_spike_close (live_ATR / entry_ATR ≥ VOL_SPIKE_MULT)
                           ④ time_exit       (age ≥ MAX_POSITION_HOLD_HOURS)
                           ⑤ take_profit     (dynamic ATR or % fallback)
                           ⑥ partial_take_profit (PARTIAL_TP_FRACTION, once per lifetime)
                           ⑦ trailing_stop   (peak PnL give-back > TRAIL_ATR_MULT)
                           ⑧ side_flip       (AUTO_FLIP_ON_SIDE_CHANGE + L3 flip)
                           ⑨ re_evaluation   (conviction < MIN_CONVICTION_TO_HOLD + modest profit)
                           │
                           ▼
                    If nothing triggers → HOLD justification taxonomy
                                           │
                                           ▼
                    Smart Path (only if at least one gate fires)
                           │
                           Gates (OR):
                           • price ≥ 1.5×ATR  (L3_REVIEW_TRIGGER_PRICE_ATR_MULT)
                           • |funding rate| ≥ threshold
                           • n_whales ≥ L3_REVIEW_TRIGGER_WHALE_COUNT
                           • |OI δ1h %| ≥ threshold
                           • periodic refresh (L3_REVIEW_MAX_INTERVAL_MINUTES)
                           • HOLD-rescue (Fast Path=HOLD + L2 conv ≥ 0.65)
                           • HOLD-must-be-earned (age ≥ 45 min)
                           │
                           ▼
                    Cost-safety rails (non-overridable):
                           • Global LLM budget (2/cycle, 8/hour)
                           • Multi-trigger AND (≥2 gates in conservative)
                           • Hard cooldown floor = 25 min/position
                           • First-review delay = 10 min for new positions
                           • Aggression cooldown shift
                           │
                           ▼
                    Claude Sonnet 4.6 → SmartPathVerdict
                           │
                           ▼
                    Veto-only mode (in conservative) → HOLD / CLOSE_FULL / CLOSE_PARTIAL / TIGHTEN_STOP
                           │
                           ▼
                    Allocation Router → final action (open / close / hold)
```

---

### `=== DIAGRAM 7 — EXECUTION & TREASURY FLOW ===`

The two-layer build: execution off-Arc (Hyperliquid), settlement and yield on-Arc (Circle DCW + Paymaster + USYC + CCTP). Every leg stamped with the same `decision_id` for idempotency.

```text
                          [ AllocationRouter receives DecisionResult ]
                                             │
                                             ▼
                           Action? (risk_on / risk_off / hold / deny)
                                             │
                  ┌──────────────────────────┼──────────────────────────────┐
                  │                          │                              │
             **RISK_ON**                 **RISK_OFF**                   **HOLD / DENY**
                  │                          │                              │
                  ▼                          ▼                              ▼
   USYC Check (USYC_ENABLED?)   HyperliquidExecutor.close_all_positions()
          │                                (market order, reduce-only, decision_id)
          ├─► Yes + margin needed         │
          │     USYCExecutor.redeem() ★ ARC   Withdraw margin ─► freed USDC
          │     (USYC → USDC on Arc Testnet)  to Arc wallet address
          │     wait_for_tx (CircleWallet)    │
          └─► No / disabled                   ▼
                   │                    USYC rotation? (USYC_ENABLED?)
                   │                          │
                   ▼                          ├─► Yes
          HyperliquidExecutor.open_position() │
          │ • symbol: BTC-PERP / ETH-PERP     ▼
          │ • side = long/short              USYCExecutor.mint() ★ ARC
          │ • size_usd / markPx → units      (USDC → USYC on Arc Testnet)
          │ • leverage ≤ 5×                  to_mint = freed − USYC_USDC_RESERVE
          │ • EIP-712 signing (phantom-agent) bounded MIN/MAX amount
          │ • POST /exchange (testnet)       stamped with decision_id
          │ • dry_run=True by default        │
          ▼                                  ▼
    Venue-side TP/SL                    ExecutionPlan (logged)
    (TP: entry ± TP_ATR_MULT×ATR)       decision_id, action, size, tx_hash, extra
    (SL: entry ± SL_ATR_MULT×ATR)       rotation_amount, reserve, skipped
    reduce-only limit + stop-market
                   │
                   ▼
          ArcOnchainReader ★ ARC ← feedback (equity, drawdown, USDC/USYC balance)
```

---

### `=== DIAGRAM 8 — FULL CAPITAL FLOW CYCLE (USDC → TRADING → YIELD → FEEDBACK) ===`

Where the money goes. The same Circle DCW on Arc funds both the trading and the yield leg; capital rotates between Hyperliquid margin and USYC depending on the engine's regime, with the on-chain account state feeding back into L1 on the next cycle.

```text
                              [ START — USDC on Arc Testnet ]
                                             │
                                             ▼
     ┌─────────────────────────────────────────────────────────────────────────────┐
     │  Circle Developer-Controlled Wallet ★ ARC/CIRCLE                            │
     └─────────────────────────────────────────────────────────────────────────────┘
                                             │
                  ┌──────────────────────────┼──────────────────────────────┐
                  │                          │                              │
             **RISK_ON**                **RISK_OFF**                   **HOLD**
                  │                          │                              │
                  ▼                          ▼                              ▼
    USYC redeem (if needed)     Hyperliquid close_all()          No transaction
    (USYC → USDC on Arc)        → freed USDC                     (PositionManager already reviewed)
          │                           │
          ▼                           ▼
    Hyperliquid open_position()   Withdraw → Arc wallet address
    (BTC/ETH PERP, max 5×)            │
          │                           ▼
          ▼                   USYC mint (if enabled)
   Position open + venue TP/SL  (USDC → USYC on Arc Testnet)
          │                          │
          │                          ▼
          └──────────────────────────┘
                                     ▼
                       ArcOnchainReader ★ ARC
                       (equity_usd, drawdown_pct, USDC/USYC balance)
                                     │
                                     ▼
                       Next cycle starts (L1 uses fresh drawdown)
                                     │
                                     └─► feedback loop → Decision Engine
```

---

## `[ QUICK START ]`

### 1. Install

```bash
git clone <repo-url>
cd CapitalArc

python -m venv .venv
.venv\Scripts\activate          # Windows
# source .venv/bin/activate     # macOS / Linux

pip install -r requirements.txt
cp .env.example .env
# Edit .env: Hyperliquid key + Arc RPC + Circle DCW + Dune key + OpenRouter key
```

### 2. Wire up Dune (one-time)

Save the 9 SQL templates from [`dune/queries/`](./dune/queries) into your Dune workspace and paste each numeric query id into the matching `DUNE_QUERY_*_ID` slot in `.env`. The startup banner prints `Loaded <metric> query ID = <id>` for every one so any drift is visible immediately.

### 3. Run

```bash
# 🟢 Dry-run (default — no on-chain tx, just logs the would-be calls)
python main.py

# 🔁 Loop every DECISION_INTERVAL_SECONDS (default 600 s / 10 min)
python main.py --loop

# 🔴 Live execution. Requires HYPERLIQUID_PRIVATE_KEY (testnet,
#    fauceted from https://app.hyperliquid-testnet.xyz) + Circle DCW.
#    Set USYC_ENABLED=false for clean perp-only live testing.
python main.py --live

# 🔴🔁 The standard live setup — continuous trading on a 10-min cadence
python main.py --live --loop
```

### 4. Offline scenario testers (no Dune / no chain / no SDK calls)

```bash
# Probe the decision engine with synthetic inputs
python main.py --test-bias bearish  --test-bias-strength 0.86 --test-conviction 0.52
python main.py --test-bias bullish  --test-bias-strength 0.80 --test-conviction 0.75
python main.py --test-bias neutral  --test-bias-strength 0.10 --test-conviction 0.50

# Same, but also call the real Claude Sonnet 4.6 arbiter end-to-end
python main.py --test-bias bearish --test-bias-strength 0.86 --test-conviction 0.52 --real-sonnet

# Probe each PositionManager trigger
python main.py --test-allocation take_profit
python main.py --test-allocation stop_loss
python main.py --test-allocation side_flip
python main.py --test-allocation re_evaluation
```

### 5. Important env knobs

| Variable | Default | Purpose |
|----------|---------|---------|
| `DECISION_INTERVAL_SECONDS` | `600` | Sleep between cycles in `--loop` mode. `0` removes the sleep. |
| `USYC_ENABLED` | `false` (testing) | When `false`, the USYC mint/redeem leg is skipped — clean perp-only mode. |
| `HYPERLIQUID_API_URL` | `https://api.hyperliquid-testnet.xyz` | Execution venue + own-account state. |
| `HYPERLIQUID_DATA_API_URL` | `https://api.hyperliquid.xyz` (mainnet) | Market intelligence for L2 overlay. Kept independent so the data plane stays honest. |
| `L3_MODE` | `critical` | `critical` (5-section structured rationale) or `standard` (concise trader voice). |
| `L3_AGGRESSION` | `balanced` | `conservative` / `balanced` / `aggressive`. `aggressive` enables HOLD-rescue. |
| `ALLOW_L3_TO_OVERRIDE_L1` | `true` | Master switch for letting L3 audit & override soft L1 blocks. |
| `L3_OVERRIDE_MIN_CONVICTION` | `0.55` | Minimum Claude conviction to override an L1 soft block. |
| `RISK_PER_TRADE_PCT` | `1.0` | Per-trade equity fraction risked (1.0 = 1%). |
| `MAX_POSITION_USD` | `10000` | Hard cap clipped by `HyperliquidExecutor` before the SDK call. |
| `MAX_DRAWDOWN_PCT` | `10.0` | Hard L1 veto + denominator for the gradient drawdown haircut. |
| `DAILY_LOSS_LIMIT_PCT` | `5.0` | Trigger for the portfolio-wide daily-DD kill switch. |

---

## `[ TECH STACK ]`

### Trading venue (RFB 01 execution layer)
- **🎯 Hyperliquid Testnet** — primary perp venue for real long/short positions, signed via the official [`hyperliquid-python-sdk`](https://github.com/hyperliquid-dex/hyperliquid-python-sdk) (EIP-712 L1 actions over the phantom-agent domain, `POST /exchange` + `POST /info`).

### Settlement, treasury & yield (Arc + Circle)
- **🏗️ Arc** stablechain (Testnet today, mainnet on launch) — settlement, accountability and yield layer.
- **🔐 Circle Developer-Controlled Wallets** — **live today** for non-custodial programmable signing, with RSA-OAEP-SHA256 entity-secret encryption (fresh ciphertext per request).
- **⛽ Circle Paymaster** — `gasPolicyId` plumbing and `TxResult.sponsored` flag are in place; **the architecture is designed to support** gasless / sponsored transactions on Arc, **wired and ready for activation** in the next phase.
- **🌉 CCTP v2** — **the architecture is designed to support** cross-chain USDC routing (Arc ⇄ Arbitrum / Base when funding / defunding Hyperliquid margin); **planned for the next phase**, alongside USYC mainnet roll-out.
- **🪙 USYC** — yield-bearing tokenized USDC (risk-off leg via `USYCExecutor`); mint/redeem surface implemented and **wired and ready for activation**, gated by `USYC_ENABLED` during the current perp-only live test.

### Intelligence & data
- **🔭 Dune MCP** — single source of truth for L1 OHLCV + L2 on-chain flows (Ethereum / Base / Arbitrum out of the box, chain-portable via `DUNE_CHAIN`).
- **📡 Hyperliquid Info API** — real perp data (`metaAndAssetCtxs` + `fundingHistory`) overlaying the L2 spot-derived proxies; mainnet by default so the data plane stays honest.
- **🧱 Arc RPC** — account state only (agent wallet, vault TVL, agent margin on Arc).
- **🤖 Claude Sonnet 4.6** via **OpenRouter** — L3 final arbiter, strict-JSON Pydantic-validated, two personas (`critical` / `standard`), aggression calibration (`conservative` / `balanced` / `aggressive`).

### Backend
- **🐍 Python 3.10+**
- `hyperliquid-python-sdk` — Hyperliquid EIP-712 signing + REST client
- `web3.py`, `eth-account`, `eth-abi` — Arc chain interaction
- `httpx`, `aiohttp`, `tenacity` — async HTTP with rate-limit retries
- `pydantic`, `pydantic-settings` — strongly-typed config + validated L3 responses
- `pandas`, `numpy`, `ta` — L1 indicator math
- `mcp`, `dune-client` — L2 on-chain data
- `loguru` — structured logging
- `rich` — eight-panel terminal UI (incl. Position Review + L1-override banner)
- `apscheduler` — loop scheduling

### Tests
**190+ pytest scenarios** across the gating suite — L3 arbiter (modes, calibration, telemetry, prompt rendering), Hyperliquid executor (open / close / get_position / mid-price across all paths with fake SDK clients), Position Manager (83+ scenarios: full priority ladder, dynamic-ATR triggers, daily-DD persistence, per-asset ATR caps, Smart-Path gating + override, side-symmetric long/short), Circle DCW `wait_for_tx` 4xx fail-fast (8 regression tests), and the full L1 → L2 → L3 cascade aggregation with both real and synthetic L3.

---

## `[ OPERATING PRINCIPLES ]`

1. **On-chain by default; right venue for the job.** Treasury, yield and gas through Arc + Circle. Directional perp trading through Hyperliquid. The two halves talk only through the `AllocationRouter`.
2. **Dune MCP is the single source of truth.** Every market signal — OHLCV (L1) and on-chain intelligence (L2) — reads through a saved Dune query. No CEX feed, no direct RPC market-data scrape.
3. **Cascade, don't average.** Levels are *gates*, not weighted blobs. L1 hard rules veto trades absolutely.
4. **Decouple conviction from direction.** Every level votes on two axes. Direction is aggregated as a conviction-weighted vote so wishy-washy levels can't drag the side. The engine is symmetric for longs and shorts.
5. **Placeholders don't dilute signal.** When L3 is synthetic (no `OPENROUTER_API_KEY`), its weight is redistributed back to L1+L2 proportionally so it never silently re-blends the real signal into itself.
6. **Risk-scale, don't risk-flat.** Position size is solved from account equity, target risk-per-trade and the primary symbol's ATR%. A fixed *fraction of equity* per trade — not a fixed USD.
7. **Gradient guards, not cliffs.** Drawdown trims intensity smoothly via `1 − (dd/max_dd)²` long before the hard close fires.
8. **Safety over alpha.** Drawdown guard and stale-data guard always win over signals. L1 hard rules are never softened by L2 / L3.
9. **Deterministic decisions.** Same inputs → same conviction → same direction → same action. L3 is pinned to low temperature with a strict JSON contract; ambiguous replies fall back to a safe HOLD.
10. **Honest provenance.** Every metric reports its `dune:<id>` / `hyperliquid:<endpoint>` / `n/a` / `error` tag so users always know whether a number is on-chain truth or a placeholder.
11. **Observable.** Every decision logs inputs, per-level conviction + direction, effective weights, final verdict, action, sizing breakdown and tx hash. Eight-panel rich CLI on every cycle.
12. **Idempotent execution.** Every action is keyed by a `decision_id` so retries never double-trade.
13. **HOLD must be earned.** A hold verdict is a *conscious* outcome — the manager iterates every priority, considers Smart Path overrides, and only lands on hold when no trigger fired AND no override was warranted.
14. **Modular.** Each level is replaceable; the router doesn't care *how* a conviction or direction was computed.

---

## `[ ROADMAP ]`

Nine intense build days, each closing one well-defined slice of the agent. When a slice took more than a day, the follow-up day is named explicitly rather than hidden under a vague "polish" tag.

| Phase | Status | Highlights |
|-------|--------|------------|
| **Day 1** | ✅ | Scaffolding, secret hygiene (`.env.example` template + git-ignored `.env`), three-level public interfaces, repo layout, `pydantic-settings` as the sole `.env` reader, internal `AGENTS.md` spec. |
| **Day 2** | ✅ | Circle **Developer-Controlled Wallets** live on Arc Testnet with **RSA-OAEP-SHA256** entity-secret encryption (fresh ciphertext per request); first margin moves via `contractExecution`; Paymaster plumbing in place (`gasPolicyId` + `TxResult.sponsored`); strict `--dry-run` / `--live` / `--loop` pre-flight; Arcscan explorer links per tx. |
| **Day 3** | ✅ | L1 (OHLCV via Dune `dex.trades` on 15m + 1h) and L2 (**9/9 Dune queries** live, per-metric `dune:<id>` provenance map) wired end-to-end. **Conviction-vs-direction split** implemented (every level emits both axes; aggregation is a conviction-weighted vote). Vol-targeted sizing pipeline (`size = equity × risk / (stop_atr × ATR%/100)`) and gradient drawdown haircut `1 − (dd/max_dd)²`. Six-panel rich CLI and an offline `--test-bias` scenario tester. |
| **Day 4** | ✅ | Real **Claude Sonnet 4.6** L3 arbiter via OpenRouter. Production-grade async `OpenRouterClient` (bounded retry, exponential backoff, hard-failure short-circuit on 401/402/403/404, defensive JSON parsing). Strict `ArbiterResponse` Pydantic contract (`conviction`, `direction`, `regime`, `recommended_intensity`, `rationale`, `key_factors`). Two `L3_MODE` personas — `critical` (5-section rationale with veto authority) and `standard` (concise trader voice). Safe-HOLD fallback on any schema violation or LLM error. Seventh CLI panel `Level 3 — FINAL ARBITER (SONNET 4.6)` with `Mode` badge. |
| **Day 5** | ✅ | **Trading-venue pivot to Hyperliquid Testnet.** Full `HyperliquidExecutor` built on the official `hyperliquid-python-sdk` (EIP-712 phantom-agent L1 actions over `POST /exchange`, dual Info clients for execution + data planes). Hard caps enforced before the SDK call (`MAX_LEVERAGE=5×`, `MAX_POSITION_USD=$10k`, `DEFAULT_SLIPPAGE_BPS=50`). First two-tier `PositionManager` (Fast Path: TP / SL / trailing / side-flip / re-evaluation). First **auto-allocation pipeline** (risk-on opens on Hyperliquid; risk-off closes perps + withdraws margin + rotates into USYC). Eighth `Position Review` CLI panel. `tests/test_hyperliquid_executor.py` with fake SDK clients across all paths. |
| **Day 6** | ✅ | **L1 → L3 override architecture.** `Level1Reason.is_hard` flag at source + per-reason marginality buckets (`marginal / moderate / decisive`) so L3 can tell "RSI at 70.2" from "RSI at 84". `DecisionEngine` rewired to **always invite L2 and L3** even on an L1 block; enriched `ArbiterBriefing` carries `l1_blocked`, `l1_blocked_reasons`, `l1_indicators`. Short-circuit moved **after** L3. Soft blocks overrideable via `ALLOW_L3_TO_OVERRIDE_L1=true` + `L3_OVERRIDE_MIN_CONVICTION=0.55`; hard blocks (`drawdown_breach`, `ohlcv_unavailable`) sacrosanct. **Stacked-veto intensity haircut** (1→×1.0, 2→×0.7, 3→×0.5, 4+→×0.35). Full audit trail in `DecisionResult.l1_override_meta` + an `L1 OVERRIDDEN BY L3` CLI banner. |
| **Day 7** | ✅ | **L3 calibration sprint.** Critical-mode prompt rewritten with **STRICT RESPONSE LENGTH RULES** (≤ 3500 chars target); rationale ceiling raised `4k → 8k` chars after observing 6.8k-char rationales tripping safe-HOLD for length alone. Concrete **default-action decision matrix** replacing the old "skeptical by default" baseline. Per-asset ATR caps baked into the prompt (BTC ≤ 3%, ETH ≤ 4% on 1h); cross-asset ATR demoted to context; `n_whales < 5` classified as noise; flat OI = neutral in continuation; `history_unavailable` no longer treated as bearish. Post-validation `L3_AGGRESSION` multiplier (`conservative ×0.90 / balanced ×1.00 / aggressive ×1.10`) and a **HOLD-rescue rule** under `aggressive` (flips HOLD → low-intensity OPEN when L1 passes AND L2 conviction ≥ `L3_HOLD_RESCUE_L2_MIN=0.65`). In-process `_L3Telemetry` counter (total / held / opened / rescued_holds / raw_holds). |
| **Day 8** | ✅ | **PositionManager v2 — Fast Path hardened + Smart Path landed.** Fast Path extended to a **9-trigger priority ladder** (`daily_dd_guard > stop_loss > vol_spike_close > time_exit > take_profit > partial_take_profit > trailing_stop > side_flip > re_evaluation`) plus state-only verbs (`breakeven_arm`, `vol_spike_warn`). **Dynamic ATR TP/SL** recomputed every cycle (`SL_ATR_MULT=1.2`, `TP_ATR_MULT=3.0`, `TRAIL_ATR_MULT=1.5`); **partial take-profit** at `1.5×ATR` (50% scale-out, once per lifetime); **breakeven arming** (one-way SL tighten at `1.0×ATR`); **volatility filter** (live/entry ATR ≥ `1.8×` → close or tighten); **time-based exit** (`MAX_POSITION_HOLD_HOURS=24`); **portfolio-wide daily-DD kill switch** at `5%` session loss. Per-asset ATR cap refuses new opens in violent regimes. Per-position state object (`_PositionState`: `opened_at`, `entry_atr_pct`, `peak_pnl_pct`, `breakeven_armed`, `partial_tp_done`, `last_l3_check_at/price`, `last_smart_verdict`). **Smart Path** landed: Claude-driven `SmartPathVerdict ∈ {hold, close_full, close_partial, tighten_stop, raise_target}`, event-triggered (price ≥ 1.5×ATR / funding / whales / OI / scheduled refresh / HOLD-rescue / HOLD-must-be-earned), with cost-safety rails (`LLM_MAX_PER_CYCLE=2`, `LLM_MAX_PER_HOUR=8`, 25-min per-position cooldown floor, 10-min first-review delay) and a **veto-only mode** under `conservative`. Position Review panel extended with `BE` / `pTP` / `L3:…` flags, source-tagged badges (`FAST` / `SMART`) and a daily-DD banner with `BREACHED` indicator. |
| **Day 9** | 🟡 | **Live-test hardening + intelligence overlay.** New `HyperliquidIntelligenceAdapter` reads real perp data (funding / OI / volume / cum-funding) from `metaAndAssetCtxs` + `fundingHistory`; in-process ring buffer per coin synthesises 1h / 4h / 24h OI deltas (warming horizons surface as `history_unavailable`, never zero); graceful per-metric degradation back to the Dune proxy; provenance flips between `dune:<id>` and `hyperliquid:<endpoint>` per metric. Data plane pinned to **mainnet** (`HYPERLIQUID_DATA_API_URL`) so it stays honest while execution runs on testnet. Circle DCW `wait_for_tx` **4xx fail-fast** (one ERROR line instead of 90 s of WARN spam) + `_is_circle_tx_id` UUID discriminator that skips Hyperliquid order ids. Operational knobs landed (`DECISION_INTERVAL_SECONDS=600`, `USYC_ENABLED=false` perp-only mode, testing-loosened L1 defaults clearly labelled `TESTING ONLY`). Risk-profile quick-start table defined in `AGENTS.md`. First `--live --loop` runs on Hyperliquid Testnet with real Claude Sonnet 4.6 in critical mode, end-to-end. |
| **Day 10+** | 🚧 | **In progress / planned.** Calibration of `L3_AGGRESSION` defaults from accumulated live telemetry; **the architecture is designed to support** full Paymaster sponsorship and CCTP v2 cross-chain USDC routing — both are **wired and ready for activation** in the next phase. USYC mint/redeem leg **planned for the next phase**, once the rotation flow moves past dry-run. Beyond that: per-symbol routing (Hyperliquid is a netting venue today); durable trailing-stop state across restarts; JSONL decision log for replay + backtest; mainnet roll-out plan; arc-native Dune dataset once Arc Testnet is indexed; tightening back the testing-loosened L1 defaults (`L1_ATR_PCT_MIN/MAX`, `L1_REQUIRE_TF_AGREEMENT`). |

**Build telemetry across Day 1 → Day 9:**

| Slice | Count |
|-------|-------|
| Test scenarios in the gating suite | **190+** |
| Position Manager scenarios alone | **83+** |
| Dune queries live with full provenance | **9 / 9** |
| Decision-engine levels with conviction + direction split | **3 / 3** |
| Position Manager triggers (Fast Path priority ladder) | **9** |
| Position Manager state-only verbs | **2** (`breakeven_arm`, `vol_spike_warn`) |
| Smart Path verdict types | **5** (`hold`, `close_full`, `close_partial`, `tighten_stop`, `raise_target`) |
| Liquidation-protection rails | **5** (hard caps · gradient DD · per-asset ATR · dyn SL+breakeven · daily-DD kill switch) |
| Rich CLI panels per cycle | **8** |
| Independent Info clients (data plane vs execution plane) | **2** (mainnet data + testnet execution) |

---

## `[ FOR THE JUDGES — WHY RFB 01 ]`

| RFB 01 requirement | CapitalArc answer |
|--------------------|-------------------|
| **24/7 autonomous monitoring** | `python main.py --live --loop` runs a fresh 9-step cycle every `DECISION_INTERVAL_SECONDS` (10 min default). |
| **Split-second leverage decisions** | Fast Path runs in milliseconds, 9-trigger priority ladder, ATR-aware TP/SL recomputed every cycle. |
| **Optimal leverage based on volatility & conviction** | Vol-targeted sizing solves `size = (equity × risk) / (stop_atr × ATR%/100)`; leverage hard-capped at 5×; intensity scales with conviction. |
| **Dynamic stop-loss & take-profit** | Dynamic ATR multipliers (`SL_ATR_MULT`, `TP_ATR_MULT`, `TRAIL_ATR_MULT`) recomputed every cycle from live ATR; partial TP, breakeven arming, trailing-stop, time-exit, vol-spike close. |
| **Automated liquidation protection** | 5 independent rails: hard caps, gradient DD haircut, per-asset ATR ceiling, dynamic SL + breakeven, daily-DD kill switch. |
| **Two-layer build (execution off-Arc, settlement on Arc)** | Execution on Hyperliquid (EIP-712 + `POST /exchange`); collateral & accountability on Arc via Circle DCW (live today). **The architecture is designed to support** Paymaster (gasless), CCTP v2 (cross-chain USDC) and USYC (yield) — **wired and ready for activation** in the next phase. |
| **Verifiable on-chain track record** | Every action keyed by `decision_id`; ExecutionPlan + ArcOnchainReader feedback loop; tx hashes + explorer links emitted in the CLI. |
| **Cross-chain collateral movement** | **The architecture is designed to support** CCTP v2 USDC routing (Arc ⇄ Arbitrum / Base) when funding / defunding Hyperliquid margin; **planned for the next phase** (tracked on the roadmap, Day 10+). |
| **AI that can change its mind** | L3 overrides soft L1 vetoes with a stacked-veto haircut; Smart Path overrides Fast Path verdicts on material change; HOLD-rescue flips HOLD to OPEN under aggressive mode when L2 conviction is high. |

---

## `[ LICENSE ]`

To be defined before the hackathon submission.

---

```text
> end of file. capital is on chain. signal is honest. risk is bounded.
> [ OK ]
```
