# AGENTS.md — CapitalArc Internal Agent Design

This document describes the internal agent and decision-engine design for
**CapitalArc**. It is the source of truth for *how the agent thinks*, not
just *what it does*. Pair it with the project [`README.md`](./README.md)
(user-facing overview) and [`dune/README.md`](./dune/README.md) (per-query
SQL column contracts).

**Detailed references** for specific subsections live in [`docs/`](./docs/):
- [§2.2–2.4 Level details](./docs/agents-detailed-levels.md)
- [§3.4.2 Smart Path](./docs/agents-detailed-smart-path.md)
- [§5 Module map](./docs/agents-detailed-module-map.md)
- [§7 Development status](./docs/agents-detailed-status.md)

---

## Table of Contents

1. [High-level model](#1-high-level-model)
2. [Decision engine](#2-decision-engine)
   - [2.1 Cascade order](#21-cascade-order--always-invite-l3)
   - [2.2 Level 1 — Technical hard rules](#22-level-1--technical-hard-rules)
   - [2.3 Level 2 — On-chain intelligence](#23-level-2--on-chain-intelligence)
   - [2.4 Level 3 — Claude Sonnet 4.6](#24-level-3--claude-sonnet-46-final-arbiter)
   - [2.5 Conviction vs Direction split](#25-conviction-vs-direction-split)
   - [2.6 Effective weights & L3 redistribution](#26-effective-weights--l3-redistribution)
   - [2.7 Aggregation](#27-aggregation)
   - [2.8 Decision rules](#28-decision-rules)
   - [2.9 L1 override by L3](#29-l1-override-by-l3-hard-vs-soft-blocks)
3. [Allocation Router](#3-allocation-router)
   - [3.1 Decision-to-action mapping](#31-decision-to-action-mapping)
   - [3.2 Sizing pipeline](#32-sizing-pipeline-4-steps-equity--risk)
   - [3.3 Hard overrides](#33-hard-overrides)
   - [3.4 Position Manager](#34-position-manager--two-tier-fast-path--smart-path)
   - [3.5 Auto-allocation pipeline](#35-auto-allocation-pipeline)
4. [Execution](#4-execution)
5. [Module map](#5-module-map)
6. [Operating principles](#6-operating-principles)
7. [Current status](#7-current-status)

---

## 1. High-level model

CapitalArc is a single autonomous agent built from four building blocks:

1. A **3-level cascading decision engine** that converts raw market data
   into a `(conviction, direction)` pair.
2. An **allocation router** that turns `(conviction, direction)` into a
   concrete trade size on the right side, vol-scaled and drawdown-aware,
   with USYC rotation on risk-off.
3. A **two-tier position manager** that supervises every open position
   each cycle. The **Fast Path** (always-on, deterministic, ATR-aware)
   handles dynamic ATR-based TP/SL, breakeven arming, partial take-profit,
   trailing stops, a volatility filter, time-based exit and a
   portfolio-wide daily-DD kill switch. The **Smart Path** (Claude Sonnet
   4.6, event-triggered) fires ONLY on material change (price moved
   ≥ 1.5×ATR, funding/whale/OI spike, periodic refresh) and can override
   the Fast Path. Allocation and stewardship stay separate concerns;
   "HOLD must be earned" — a hold verdict is a *conscious* outcome, not
   a default.
4. An **execution layer** split across two venues:
   - **Hyperliquid Testnet** — primary perp trading venue (the agent's
     long/short positions). Signed via the official
     `hyperliquid-python-sdk` (EIP-712 L1 actions over the
     phantom-agent domain), submitted to `POST /exchange`.
   - **Arc + Circle** — treasury & yield leg: USYC mint/redeem, CCTP
     v2 USDC bridging, Circle DCW + Paymaster for sponsored on-Arc tx.

```
                +-------------------------------------------------------+
                |                  Decision Engine                      |
                |  +--------+   +--------+   +----------------------+   |
   Market  ---> |  | L1 TA  |   | L2 OnC |   | L3 Claude Sonnet 4.6 |   | ---> (conviction, direction)
   Data         |  | rules  |   | (Dune) |   |   (final arbiter)    |   |        +  per-level vote map
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
                |  mid-band + |direction_strength| >= θ   -> open small |
                |  mid-band + direction neutral           -> hold       |
                +-------------------------------------------------------+
                                       |
                                       v
                +-------------------------------------------------------+
                |  Vol-targeted sizing  +  Gradient drawdown haircut    |
                |  TRADING:  Hyperliquid Testnet (hyperliquid-python-sdk)|
                |  TREASURY: Arc + Circle DCW + Paymaster + USYC + CCTP |
                +-------------------------------------------------------+
```

The defining design choice is that **conviction ("do we act?") and
direction ("which side?") are tracked independently** at every level and
through aggregation. See [§2.5](#25-conviction-vs-direction-split).

> 🧭 **Venue split (Day 5 pivot).** The original target was the Arc
> Perp DEX, but its public matcher / EIP-712 `OrderTypes.Order` spec
> was never published. CapitalArc therefore trades on **Hyperliquid
> Testnet** while keeping **Arc + Circle as the treasury and yield leg**.
> The `ArcPerpExecutor` is retained for treasury moves and dry-run
> telemetry; production routing goes through `HyperliquidExecutor`.

---

## 2. Decision engine

### 2.1 Cascade order — "always invite L3"

Levels are evaluated **cascadingly**, not in parallel — but the cascade
no longer short-circuits at L1:

1. **L1** runs first. The full `Level1Decision` is computed regardless
   of pass/fail, including per-symbol indicators and marginality metadata.
2. **L2** runs next, **always** — even when L1 blocked.
3. **L3** runs next, **always**, with an enriched `ArbiterBriefing` that
   includes `l1_blocked`, `l1_blocked_reasons` (each carrying an `is_hard`
   flag and marginality bucket) and `l1_indicators`. This lets Claude
   *audit* an L1 veto — see [§2.9](#29-l1-override-by-l3-hard-vs-soft-blocks).
4. **Engine-level guardrails** apply AFTER L3:
   - **Hard L1 block** (`drawdown_breach`, `ohlcv_unavailable`) →
     short-circuit to risk-off regardless of L3's verdict.
   - **Soft L1 block + L3 declined / synthetic L3** → short-circuit.
   - **Soft L1 block + real L3 with `conviction ≥ L3_OVERRIDE_MIN_CONVICTION`**
     → engine bypasses the weighted aggregator and hands control to L3's
     verdict directly, haircut by the stacked-veto cap.
   - **L1 passes** → normal weighted aggregation ([§2.7](#27-aggregation)).

When `OPENROUTER_API_KEY` is unset, L3 emits a deterministic synthetic
score (re-blend of L1 + L2) and the engine redistributes its weight to
L1 + L2 in aggregation. Synthetic L3 is never used to override an L1 block.

---

### 2.2 Level 1 — Technical hard rules

> Full detail: [docs/agents-detailed-levels.md §2.2](./docs/agents-detailed-levels.md#22-level-1--technical-hard-rules)

OHLCV on `15m` and `1h` for `BTC-PERP` / `ETH-PERP` via `DuneMarketData`.
No other market-data path.

**Hard rules (always veto; L3 cannot override):**
- `drawdown_breach` — current drawdown ≥ `MAX_DRAWDOWN_PCT`.
- `ohlcv_unavailable` — Dune returned no rows.

**Soft rules (open to L3 override; each carries `severity_label ∈ {marginal, moderate, decisive}`):**
- `trend_mixed` — EMA9/EMA21 alignment broken across timeframes.
- `rsi_overbought` / `rsi_oversold` — RSI(14) at extremes.
- `atr_too_low` / `atr_too_high` — ATR% outside `[L1_ATR_PCT_MIN, L1_ATR_PCT_MAX]`.

Conviction = `mean(per-symbol strength)` — **no `0.5` floor**.
Code: `src/core/level1.py`.

---

### 2.3 Level 2 — On-chain intelligence

> Full detail: [docs/agents-detailed-levels.md §2.3](./docs/agents-detailed-levels.md#23-level-2--on-chain-intelligence-dune-mcp--hyperliquid-info-overlay)

Primary backbone: `DuneMCPClient`. Per-symbol funding / OI / volume /
cum-funding are **overlaid with real Hyperliquid perp data** via
`HyperliquidIntelligenceAdapter` when `HYPERLIQUID_INTELLIGENCE_ENABLED=true`
(default). The data API URL is pinned to **mainnet** by default
(`HYPERLIQUID_DATA_API_URL`) so the data plane stays honest when execution
is on testnet. Every metric reports its provenance (`dune:<id>`,
`hyperliquid:…`, `n/a`, or `error`).

Conviction: `min(1.0, max(2·|heat − 0.5|, bias_strength))`.
Direction: sign of `market_bias` (`bullish=+1`, `bearish=−1`, `neutral=0`).
Code: `src/core/level2.py`.

---

### 2.4 Level 3 — Claude Sonnet 4.6 — final arbiter

> Full detail (personas, calibration, telemetry, critical-mode template): [docs/agents-detailed-levels.md §2.4](./docs/agents-detailed-levels.md#24-level-3--claude-sonnet-46-via-openrouter--final-arbiter)

- **Role.** Arbiter, not just a signal. Emits
  `(conviction, direction, regime, recommended_intensity)` + English
  `rationale` + `key_factors`.
- **Model.** Default `anthropic/claude-sonnet-4.6` via OpenRouter.
  Swappable via `OPENROUTER_MODEL=…`.
- **Two personas (`L3_MODE`):** `critical` (default — senior risk manager,
  mandatory 5-section rationale, veto authority) and `standard` (concise
  trader-voice, 1-2 sentences).
- **Contract.** Strict `ArbiterResponse` JSON, Pydantic-validated.
  `rationale` ceiling: 8000 chars. Any deviation → safe HOLD fallback.
- **Aggression (`L3_AGGRESSION`):**
  `conservative` (×0.90), `balanced` / default (×1.00),
  `aggressive` (×1.10 + HOLD-rescue).
- Code: `src/core/level3.py`.

---

### 2.5 Conviction vs Direction split

The single most important design decision in the engine.

Every level emits **two orthogonal values**:

| Field | Range | Meaning |
|-------|-------|---------|
| `score` (alias `conviction`) | `[0, 1]` | "How strongly do we want to act?" |
| `direction_sign` | `{-1, 0, +1}` | "If we act, which side?" (−1 short, 0 none, +1 long) |

**Why this split matters.** Previously a bearish setup with low heat
(`market_heat = 0.15`) emitted a low aggregated score, which the router
interpreted as "low conviction → close everything" even though on-chain
it was actually a *high-conviction SHORT*. Folding heat into a symmetric
conviction (`2·|heat-0.5|`) and tracking direction on a separate axis lets
that exact case open a SHORT at full conviction — the engine is now
symmetric for longs and shorts.

**Per-level vote summary:**

| Level | Conviction formula | Direction sign |
|-------|--------------------|----------------|
| L1 | `mean(per-symbol strength)` — no `0.5` floor | Sign of primary symbol's trend |
| L2 | `max(2·|heat − 0.5|, bias_strength)` capped at 1.0 | Sign of `market_bias` |
| L3 (real) | Claude-arbited `conviction` ∈ `[0, 1]` (Pydantic-validated) | Claude `direction` ∈ {long, short, neutral} → {-1, 0, +1} |
| L3 (synthetic) | `0.5·L1 + 0.5·L2` (no `OPENROUTER_API_KEY`) | conviction-weighted sign of L1 + L2 |
| L3 (fallback HOLD) | `0.0` (OpenRouter errored or returned malformed JSON) | `0` (neutral) — safe HOLD |

---

### 2.6 Effective weights & L3 redistribution

Default weights: L1 `0.25`, L2 `0.35`, L3 `0.40`.

When L3 is the synthetic placeholder and `REDISTRIBUTE_SYNTHETIC_L3_WEIGHT=True`
(default), L3's weight is redistributed proportionally to L1 + L2:

```
w1_eff = w1 + w3 · (w1 / (w1 + w2))
w2_eff = w2 + w3 · (w2 / (w1 + w2))
w3_eff = 0
```

With defaults: `0.25 / 0.35 / 0.40` → effective `0.4167 / 0.5833 / 0.0`.
Both configured and effective weights surface in `DecisionResult.weights` /
`effective_weights` and the Final Decision panel renders them side-by-side.

Once `OPENROUTER_API_KEY` is set and Claude returns a clean verdict, L3
votes with its full configured weight (`0.40`). An OpenRouter error falls
back to a safe HOLD at zero conviction — that HOLD is **not** flagged as
synthetic (it's a real failed call, correctly counted as L3's vote).

---

### 2.7 Aggregation

**Final conviction** (weighted average over effective weights):
```
final_conviction = Σᵢ (w_eff_i · convictionᵢ)
```

**Final direction** (conviction-weighted vote — wishy-washy levels can't
drag the side):
```
direction_score = Σᵢ(w_eff_i · convictionᵢ · direction_signᵢ) / Σᵢ(w_eff_i · convictionᵢ)

final_direction = +1  if direction_score >  SHORT_BIAS_MIN_STRENGTH   (default 0.35)
final_direction = -1  if direction_score < -SHORT_BIAS_MIN_STRENGTH
final_direction =  0  otherwise
```

**Short-circuit.** When L1 blocks hard: `final_conviction = 0`,
`final_direction = 0`, `regime = "risk-off"`, `short_circuited = True`.

---

### 2.8 Decision rules

```
conviction >= RISK_ON_THRESHOLD (0.6) AND direction != 0
    → action = risk_on, side = direction, intensity = full

conviction <= RISK_OFF_THRESHOLD (0.4)
    → action = risk_off, side = none, intensity = full (close)

mid-band AND direction != 0 AND direction_strength >= STRONG_BIAS_OPEN_STRENGTH (0.6)
    → action = risk_on (STRONG-DIRECTION override)
      intensity = 0.5 · conviction · direction_strength

otherwise → action = hold
```

The **STRONG-DIRECTION override** opens at half normal intensity rather
than sitting idle through a clean setup when conviction is ambiguous but
the directional vote is decisive.

---

### 2.9 L1 override by L3 — hard vs soft blocks

| Setting | Default | Effect |
|---------|---------|--------|
| `ALLOW_L3_TO_OVERRIDE_L1` | `True` | Master switch. `False` = pre-Day-5 behaviour. |
| `L3_OVERRIDE_MIN_CONVICTION` | `0.55` | Minimum L3 conviction to bypass an L1 soft block. |

**Hard blocks (`drawdown_breach`, `ohlcv_unavailable`) are NEVER
overrideable.** If L3 tries, a WARNING is logged and the trade is silently
rejected.

**Stacked-veto intensity haircut** when L3 overrides multiple soft blocks:

| Soft blocks overridden | Intensity cap |
|------------------------|---------------|
| 1 | × 1.00 (L3 fully trusted) |
| 2 | × 0.70 (moderate haircut) |
| 3 | × 0.50 (sharp haircut) |
| 4+ | × 0.35 (defensive floor) |

Caps are tuneable via `_DEFAULT_STACKED_VETO_CAPS` in
`src/core/decision_engine.py`.

**Audit trail** (`DecisionResult.l1_override_meta`): `status ∈
{executed, declined, hard_block_uphold}`, `n_soft_blocks`, `n_hard_blocks`,
`soft_block_codes`, `stacked_veto_cap`, `l3_conviction`, `l3_direction`,
`l3_rationale_snippet`. The CLI renders an `L1 OVERRIDDEN BY L3` banner
above the Final Decision panel.

---

## 3. Allocation Router

The router consumes a `DecisionResult` and turns it into a concrete
on-chain action via a duck-typed `PerpExecutorProtocol` (production:
`HyperliquidExecutor`; tests: lightweight fake). SHORT directives are
first-class citizens — every sizing step, leverage cap and gas path is
the same for both sides.

Each cycle order: ① Position review → ② Hard overrides → ③ Apply position
review → ④ Regime dispatch.

### 3.1 Decision-to-action mapping

| Engine output | Router action |
|---------------|---------------|
| `action=risk_on, side=long` | `HyperliquidExecutor.open_position(side="long", …)` (+ USYC redeem if cash-short) |
| `action=risk_on, side=short` | `HyperliquidExecutor.open_position(side="short", …)` |
| `action=risk_off` | `close_all_positions(...)` → withdraw margin → `USYCExecutor.mint(...)` |
| `action=hold` | no on-chain change, just logged |
| `short_circuited=True` | router runs risk-off path with `reason="L1 short-circuit"` |
| stale data (all zero scores, no short-circuit) | `action="deny"`, no tx |
| `PositionReview` close | close position before regime dispatch; chains into new open on `side_flip` |

### 3.2 Sizing pipeline (4 steps, equity-% risk)

```
0. Resolve per-trade risk fraction:
   risk_pct   = RISK_PER_TRADE_PCT / 100   (e.g. 1.0% → 0.01)
                else TARGET_RISK_PCT        (legacy fraction fallback)
   risk_mult  = risk_multiplier_for_symbol(primary)   # 0.25 .. 1.5
   effective  = risk_pct × risk_mult

1. Vol-target:
   vol_target = (equity_usd × effective) / (STOP_ATR_MULT × ATR%/100)

2. × Intensity (from the engine directive):
   after_intensity = vol_target × intensity

3. × Drawdown haircut (gradient, not cliff):
   haircut       = max(0, 1 − (drawdown_pct / MAX_DRAWDOWN_PCT) ** DD_HAIRCUT_EXPONENT)
   after_haircut = after_intensity × haircut

4. Cap at MAX_POSITION_USD:
   size_usd = min(after_haircut, MAX_POSITION_USD)
```

Defaults: `RISK_PER_TRADE_PCT = 1.0`, `STOP_ATR_MULT = 1.5`,
`DD_HAIRCUT_EXPONENT = 2.0`, `MAX_POSITION_USD = $10,000`.
Per-asset multipliers: `RISK_PER_TRADE_MULT_BTC` / `MULT_ETH` / `MULT_DEFAULT`.
`EXPLAIN_SIZING=True` (default) stamps the full breakdown on `ExecutionPlan.extra["sizing"]`.

### 3.3 Hard overrides

| Override | Trigger | Result |
|----------|---------|--------|
| Drawdown breach | `-pnl / equity × 100 ≥ MAX_DRAWDOWN_PCT` | Forced `close_all_positions` |
| Stale data | All level scores `== 0` AND not short-circuit | `action="deny"`, no tx |
| Leverage cap | `directive.target_leverage > HYPERLIQUID_MAX_LEVERAGE` | Clipped to `HYPERLIQUID_MAX_LEVERAGE` (5x) |
| Max position cap | `size_usd > HYPERLIQUID_MAX_POSITION_USD` | Clipped to $10k |
| Live without key | `--live` AND `HYPERLIQUID_PRIVATE_KEY` unset | Pre-flight refuses to start |

### 3.4 Position Manager — Two-Tier (Fast Path + Smart Path)

`PositionManager` (`src/execution/position_manager.py`) runs in two
independent tiers on every cycle — cheap deterministic rules carry every
cycle; the expensive LLM fires only on material change.

#### 3.4.1 Tier 1 — Fast Path (~ ms, no LLM, always runs)

Priority ladder (first match wins):

| Priority | Trigger | Fires when |
|----------|---------|------------|
| 1 | `daily_dd_guard` | Session loss ≥ `DAILY_LOSS_LIMIT_PCT` since UTC midnight |
| 2 | `stop_loss` | Dynamic: mark crosses `entry ± SL_ATR_MULT·ATR`. Fallback: PnL ≤ −`STOP_LOSS_PCT`. |
| 3 | `vol_spike_close` | Live ATR / entry-ATR ≥ `VOL_SPIKE_MULT` + `VOL_SPIKE_ACTION="close"` |
| 4 | `time_exit` | Position older than `MAX_POSITION_HOLD_HOURS` |
| 5 | `take_profit` | Dynamic: mark crosses `entry ± TP_ATR_MULT·ATR`. Fallback: PnL ≥ +`TAKE_PROFIT_PCT`. |
| 6 | `partial_take_profit` | mark crosses `entry ± PARTIAL_TP_ATR_MULT·ATR`; fires once per position lifetime |
| 7 | `trailing_stop` | Peak PnL gave back > `TRAIL_ATR_MULT·ATR` |
| 8 | `side_flip` | `AUTO_FLIP_ON_SIDE_CHANGE=true` + engine flipped to opposite side |
| 9 | `re_evaluation` | Conviction collapsed below `MIN_CONVICTION_TO_HOLD` while in modest profit |

State-only verbs: `breakeven_arm` (lifts effective SL to `entry ± buffer`
when profit ≥ `BREAKEVEN_TRIGGER_ATR_MULT·ATR`; one-way: only tightens),
`vol_spike_warn` (advisory tightening when `VOL_SPIKE_ACTION="tighten_stop"`).

#### 3.4.2 Tier 2 — Smart Path (Claude, event-triggered)

> Full detail (gates, cost-safety rails, veto-only, telemetry, HOLD taxonomy, HOLD-rescue):
> [docs/agents-detailed-smart-path.md](./docs/agents-detailed-smart-path.md)

Fires ONLY on material change (price ≥ 1.5×ATR, funding spike, whale
activity, OI delta, periodic refresh, HOLD-rescue, HOLD-must-be-earned).

**Cost-safety rails (non-configurable below floors):**
- `LLM_MAX_PER_CYCLE=2` / `LLM_MAX_PER_HOUR=8` — hard aggregate caps.
- Hard cooldown floor: 25 min/position (silent, cannot be misconfigured below).
- First-review delay: 10 min for brand-new positions.
- Conservative multi-trigger AND: ≥ 2 corroborating gates required (auto-on under `conservative`).

Returns `SmartPathVerdict ∈ {hold, close_full, close_partial, tighten_stop, raise_target}`.
When `L3_CAN_OVERRIDE_FAST_PATH=true` (default) the verdict overrides the
Fast Path. Under `conservative`, the LLM can only veto (be a brake), never
initiate new exposure.

#### 3.4.3–3.4.6 Additional PM design

- **Dynamic ATR TP/SL** — recomputed every cycle; `TP_ATR_MULT`,
  `SL_ATR_MULT`, `TRAIL_ATR_MULT`.
- **Per-asset ATR cap** — `PER_ASSET_ATR_CAP_BTC_1H=3.0`,
  `PER_ASSET_ATR_CAP_ETH_1H=4.0`. Exceeding the cap refuses new
  risk-on opens; open positions are unaffected.
- **Daily-DD kill switch** (`_DailyDDTracker`) — closes everything and
  refuses new opens until process restart; resets at UTC midnight.
- **Per-position state** (`_PositionState`): `opened_at`, `entry_atr_pct`,
  `peak_pnl_pct`, `breakeven_armed`, `partial_tp_done`,
  `last_l3_check_at/price`, `last_smart_verdict`.
- **Design invariants:** `PositionManager` is pure logic (never calls
  `close_position`; the router owns execution). Every cycle begins with a
  fresh `get_open_positions()` pull to defeat desync (resync counters:
  `resync_fresh_pulls`, `resync_recovered_missing`,
  `resync_phantom_in_snapshot`). `side_flip` chains into a new open.
  Single source of truth for TP/SL thresholds: both local trigger and
  venue-side resting orders read from `position_manager.config`.
- **Tests:** 83+ scenarios in `tests/test_position_manager.py`;
  190+ total in the gating suite.

### 3.5 Auto-allocation pipeline (Risk-On / Risk-Off + USYC rotation)

**Risk-on:** size via §3.2 pipeline → redeem USYC if Arc cash is short →
`open_position` → stamp `decision_id` + sizing breakdown into `ExecutionPlan`.

**Risk-off:** `close_all_positions` → withdraw margin → compute freed USDC
→ preserve `USYC_USDC_RESERVE_USD` → mint USYC with leftover (bounded by
`USYC_MIN/MAX_ROTATION_AMOUNT`). USYC leg is optional (`USYC_ENABLED=false`
skips the mint and stamps `rotation_skipped` on the plan).

**Hold:** no on-chain change; `PositionReview` still ran, so any
TP/SL/trailing trigger is honoured.

Every action keyed by `decision_id = "dec-<unix_ts>-<8hex>"` for
idempotency.

---

## 4. Execution

### 4.1 Hyperliquid Testnet (`HyperliquidExecutor`)

- Signing via `hyperliquid-python-sdk` (EIP-712 phantom-agent L1 domain);
  submits to `POST /exchange` on `https://api.hyperliquid-testnet.xyz`.
- Key: `HYPERLIQUID_PRIVATE_KEY`. Agent-wallet mode: `HYPERLIQUID_ACCOUNT_ADDRESS`
  + `HYPERLIQUID_VAULT_ADDRESS`.
- Hard caps in executor: `HYPERLIQUID_MAX_LEVERAGE` (5x),
  `HYPERLIQUID_MAX_POSITION_USD` ($10k), `HYPERLIQUID_DEFAULT_SLIPPAGE_BPS` (50bp).
- **Netting venue** — a second `open_position` on the same `(symbol, side)`
  accumulates size, not a new position.
- **Execution-vs-data plane separation:** `HYPERLIQUID_API_URL` (testnet,
  signed orders + own-account reads) vs `HYPERLIQUID_DATA_API_URL` (mainnet
  by default, market-wide intelligence for L2 overlay). Two independent
  `Info` SDK clients — `_info` (execution) and `_info_data` (data).
- `dry_run=True` (default): plan logged, no order leaves the process.
  Read-only methods still hit the live `Info` endpoint.

### 4.2 Treasury & yield leg — Arc + Circle

- **Circle DCW**: non-custodial, RSA-OAEP-SHA256 per-request encryption.
- **Gas**: sponsored via Circle Paymaster on Arc Testnet.
- **Liquidity routing**: CCTP v2 for USDC bridging to Arc / Arbitrum / Base.
- **Yield**: `USYCExecutor` mints/redeems USYC on risk-off/on.
  Optional via `USYC_ENABLED`.
- **`wait_for_tx` defence-in-depth**: `CircleWallet.wait_for_tx` fails fast
  on HTTP 4xx (one ERROR line). `main.py` venue filter `_is_circle_tx_id`
  (UUID regex) skips Hyperliquid order ids before polling Circle.
  See `tests/test_circle_wallet_wait.py` (8 tests).

---

## 5. Module map

> Full table with all env-var knobs: [docs/agents-detailed-module-map.md](./docs/agents-detailed-module-map.md)

| Folder | Key classes / responsibility |
|--------|------------------------------|
| `src/core` | `DecisionEngine`, `Level1`, `Level2`, `Level3` |
| `src/data` | `DuneMCPClient`, `DuneMarketData`, `HyperliquidIntelligenceAdapter`, `ArcOnchainReader` |
| `src/execution` | `HyperliquidExecutor` (primary venue), `PositionManager` (two-tier), `USYCExecutor`, `CircleWallet`, `ArcPerpExecutor` (legacy) |
| `src/allocation` | `AllocationRouter` — vol-targeted sizing, drawdown haircut, USYC rotation |
| `src/llm` | `OpenRouterClient` (L3 + Smart Path, one key two consumers) |
| `src/utils` | `Settings` (pydantic + `.env`), `console` (8 rich panels), `logging` |
| `prompts/` | `level3_arbiter_critical.md`, `level3_arbiter_standard.md` |
| `dune/` | SQL templates + per-query column contracts in `dune/README.md` |
| `tests/` | 190+ tests (`pytest`): L3 arbiter, HL executor, position manager (83+), circle wallet |

### 5.1 Recommended risk profiles (quick-start)

| Setting | Conservative | Balanced (default) | Aggressive |
|---------|--------------|--------------------|------------|
| `RISK_PER_TRADE_PCT` | `0.5` (0.5%) | `1.0` (1%) | `1.5` (1.5%) |
| `L3_AGGRESSION` | `conservative` | `balanced` | `aggressive` |
| `MAX_POSITION_USD` | `3000` | `10000` | `25000` |
| `MAX_DRAWDOWN_PCT` | `5.0` | `10.0` | `12.0` |

`L3_AGGRESSION` implications for Smart Path:
- **conservative** — LLM floor 45 min; override threshold 0.75; HOLD-rescue off;
  partial closes ×0.85; **veto-only ON**; **multi-trigger AND ON**.
  Use for first live runs and ramp-up from `--dry-run`.
- **balanced** — floor 30 min; override 0.55; HOLD-rescue off; defaults.
  Use after watching 50+ cycles in conservative.
- **aggressive** — floor 25 min; override 0.45; HOLD-rescue on (any direction);
  HOLD-must-be-earned on; partial closes ×1.15.
  Use only with a proven edge + small account.

Five rails operate independently of the above profiles:
global LLM budget (`LLM_MAX_PER_CYCLE=2`, `LLM_MAX_PER_HOUR=8`);
hard cooldown floor (25 min/position);
first-review delay (10 min);
HOLD-justification classification (no silent fall-through);
drawdown haircut (`1 − (dd/max_dd)^2`).

---

## 6. Operating principles

1. **On-chain by default; right venue for the job.** Treasury/yield/gas through
   Arc + Circle. Directional perp trading through Hyperliquid Testnet. The two
   halves talk only through the `AllocationRouter`.
2. **Dune MCP is the single source of truth.** Every market signal — OHLCV (L1)
   and on-chain intelligence (L2) — reads through a saved Dune query. No CEX
   feed, no direct RPC market-data scrape.
3. **Chain-portable, not chain-coupled.** Switching chain is a one-line
   `DUNE_CHAIN=…` change; every SQL template is parameterised by chain +
   token addresses.
4. **Cascade, don't average.** Levels are *gates*, not weighted blobs. L1 hard
   rules veto trades absolutely.
5. **Decouple conviction from direction.** Every level votes on two axes.
   Direction is aggregated as a conviction-weighted vote so wishy-washy levels
   can't drag the side. The engine is symmetric for longs and shorts: low
   conviction means "stand down", not "be bearish".
6. **Placeholders don't dilute signal.** A synthetic L3 is a function of L1 +
   L2; its weight is redistributed back to L1 + L2 so it never silently
   re-blends the real signal into itself.
7. **Risk-scale, don't risk-flat.** Position size is solved from account equity,
   target risk-per-trade and the primary symbol's ATR%. The agent risks a fixed
   *fraction of equity* per trade.
8. **Gradient guards, not cliffs.** Drawdown trims intensity smoothly via
   `1 − (dd/max_dd)^exponent` long before the hard close fires.
9. **Safety over alpha.** Drawdown guard and stale-data guard always win over
   signals. L1 hard rules are never softened by L2 / L3.
10. **Deterministic decisions.** Same inputs → same conviction → same direction
    → same action. L3 is pinned to low temperature and a strict JSON contract;
    ambiguous replies fall back to a safe HOLD.
11. **Honest provenance.** Every metric reports its Dune query id (or `n/a`)
    so users always know whether a number is on-chain truth or a placeholder.
    Spot-derived proxies are labelled as such.
12. **Observable.** Every decision logs inputs, per-level conviction + direction,
    effective weights, final conviction + direction, action, sizing breakdown and
    tx hash. Eight-panel rich CLI on every cycle.
13. **Idempotent execution.** Every action is keyed by a `decision_id` so
    retries never double-trade.
14. **Modular.** Each level is replaceable; the router doesn't care *how* a
    conviction or direction was computed.

---

## 7. Current status

> Full notes per day: [docs/agents-detailed-status.md](./docs/agents-detailed-status.md)

| Day | Status | Summary |
|-----|--------|---------|
| 1 | ✅ Done | Scaffolding, three-level stubs, secret hygiene, cascade spec. |
| 2 | ✅ Done | Arc DCW + Paymaster + RSA-OAEP end-to-end on Arc Testnet. `--dry-run` / `--live` / `--loop`. |
| 3 | ✅ Done | L1 + L2 via Dune MCP. Conviction/direction split. Vol-targeted sizing. Gradient drawdown haircut. 6-panel CLI. |
| 4 | ✅ Done | Real Claude Sonnet 4.6 via OpenRouter. Two L3 personas. Strict JSON contract + safe-HOLD fallback. 7-panel CLI. |
| 5 | ✅ Done | Hyperliquid Testnet as primary trading venue. `PositionManager`. Auto-allocation pipeline. 8-panel CLI. |
| 5.x | 🟡 In progress | Live-test follow-ups: L1 override architecture, L3 calibration (Day-6 prompt rewrite + `L3_AGGRESSION`), `HyperliquidIntelligenceAdapter`, Circle DCW noisy-poll fix. |
| 6+ | 🚧 Planned | Mainnet roll-out, per-symbol routing, durable trailing-stop state, JSONL decision log. |
