# AGENTS.md — CapitalArc Internal Agent Design

This document describes the internal agent and decision-engine design for
**CapitalArc**. It is the source of truth for *how the agent thinks*, not
just *what it does*. Pair it with the project [`README.md`](./README.md)
(user-facing overview) and [`dune/README.md`](./dune/README.md) (per-query
SQL column contracts).

---

## 1. High-level model

CapitalArc is a single autonomous agent built from three building blocks:

1. A **3-level cascading decision engine** that converts raw market data
   into a `(conviction, direction)` pair.
2. An **allocation router** that turns `(conviction, direction)` into a
   concrete trade size on the right side, vol-scaled and drawdown-aware.
3. An **execution layer** (Circle DCW + Paymaster + Arc Perp DEX + USYC)
   that puts the directive on-chain idempotently.

```
                +-------------------------------------------------------+
                |                  Decision Engine                      |
                |  +--------+   +--------+   +----------------------+   |
   Market  ---> |  | L1 TA  |   | L2 OnC |   | L3 Claude Sonnet 4.6 |   | ---> (conviction, direction)
   Data        |  | rules  |   | (Dune) |   |   (final arbiter)    |   |        +  per-level vote map
                |  +--------+   +--------+   +----------------------+   |
                +-------------------------------------------------------+
                                       |
                                       v
                +-------------------------------------------------------+
                |                Allocation Router                      |
                |  conviction >= RISK_ON + direction != 0 -> Arc Perp   |
                |  conviction <= RISK_OFF                  -> USYC      |
                |  mid-band + |direction_strength| >= θ   -> open small |
                |  mid-band + direction neutral           -> hold       |
                +-------------------------------------------------------+
                                       |
                                       v
                +-------------------------------------------------------+
                |  Vol-targeted sizing  +  Gradient drawdown haircut    |
                |  Circle DCW + Paymaster -> Arc Perp DEX  /  USYC      |
                +-------------------------------------------------------+
```

The defining design choice is that **conviction ("do we act?") and
direction ("which side?") are tracked independently** at every level and
through aggregation. Older versions collapsed both into one Long-biased
scalar; a low score meant "stand down" which made high-conviction shorts
indistinguishable from low-confidence neutrality. See
[§2.5 Conviction vs Direction split](#25-conviction-vs-direction-split).

---

## 2. Decision engine

### 2.1 Cascade order & short-circuit

Levels are evaluated **cascadingly**, not in parallel:

1. **L1** runs first. If L1 emits any `severity="block"` reason
   (drawdown, trend mismatch, RSI extreme, ATR outside band, missing
   OHLCV), the engine **short-circuits**: `final_score = 0.0`,
   `final_direction = 0`, `regime = "risk-off"`, **L2 and L3 are
   skipped**. L2 and L3 are emitted as zero `LevelScore`s flagged
   `raw.skipped = True` so the panel renders the "skipped" badge
   instead of a misleading 0.0.
2. **L2** runs only if L1 passes.
3. **L3** runs only if L1 passed. If a real `OpenRouterClient` is
   wired (`OPENROUTER_API_KEY` set), it arbitrates over an
   `ArbiterBriefing` built from L1 + L2 using the persona selected
   by `L3_MODE` (`critical` by default, `standard` optional —
   see [§2.4](#24-level-3--claude-sonnet-46-via-openrouter--final-arbiter)).
   Otherwise the engine emits a deterministic synthetic L3 score
   (re-blend of L1 + L2) **whose weight is redistributed to L1 + L2**
   in aggregation — see [§2.7 Aggregation](#27-aggregation).

### 2.2 Level 1 — Technical hard rules ("защита от дурака")

- **Inputs.** OHLCV on `15m` and `1h` for `BTC-PERP` and `ETH-PERP`,
  fetched through `DuneMarketData` (`src/data/dune_market_data.py`)
  which runs the `ohlcv` saved Dune query against the multichain
  `dex.trades` table. There is **no other market-data path** — no
  CEX feed, no Arc-RPC scrape. If Dune returns no rows (or
  `DUNE_QUERY_OHLCV_ID` is unset), the rule returns
  `ohlcv_unavailable` and the agent refuses to trade.
- **Chain.** Arc Testnet isn't yet indexed by Dune, so the SQL
  template targets a live high-liquidity EVM chain selected via
  `DUNE_CHAIN` (default `ethereum`; `base` and `arbitrum` are wired
  out of the box). Symbols map to the canonical on-chain wraps of
  BTC / ETH on the chain (WBTC or cbBTC, WETH) via
  `src/utils/config.py::_DEFAULT_TOKEN_ADDRESSES`, overridable via
  `DUNE_TOKEN_*` env vars.
- **Hard rules** (each can veto the trade):
  - **Trend filter.** `close > EMA9 > EMA21` (or mirrored down) must
    hold on *both* timeframes; otherwise `trend_mixed` blocks.
  - **RSI extreme.** Blocks when `RSI(14) ≥ L1_RSI_OVERBOUGHT` or
    `≤ L1_RSI_OVERSOLD` on any timeframe.
  - **Volatility band.** Blocks when `ATR%` (ATR / close) is outside
    `[L1_ATR_PCT_MIN, L1_ATR_PCT_MAX]`.
  - **Account drawdown.** Blocks when current drawdown breaches
    `MAX_DRAWDOWN_PCT`.
- **Conviction.** `Level1Decision.score = mean(per-symbol strength)`
  of the symbols that individually passed — **no `0.5` floor**. A
  passing-but-weak trend now produces weak conviction (e.g. `0.22`),
  not a free `0.65` that previously dragged the aggregate toward
  risk-on regardless of direction. `strength` is the average
  EMA-fast vs EMA-slow separation normalised by ATR (1.0 ≈ one ATR
  of stretch, a clear sustainable trend).
- **Direction.** `LevelScore.direction_sign` is the sign of the
  *primary symbol's* trend (`up = +1`, `down = -1`, `flat/mixed = 0`).
  Per-symbol directions also live in `decision.per_symbol[sym].direction`
  for future per-symbol routing. Average ATR% per symbol surfaces on
  `per_symbol[sym].atr_pct_avg` so the router can vol-target.
- **Output.** `Level1Decision { passes, score, rationale, reasons,
  per_symbol }` plus a `LevelScore { score, direction_sign, raw }`
  for the engine. Every block reason carries Dune provenance
  (`source`, `query_id`, bars vs required).
- **Code.** `src/core/level1.py`, OHLCV adapter in
  `src/data/dune_market_data.py`, SQL in `dune/queries/ohlcv.sql`.

### 2.3 Level 2 — On-chain intelligence (Dune MCP only)

- **Single data source.** `DuneMCPClient` (`src/data/dune_mcp.py`)
  speaks the same Bearer-authenticated surface the Dune MCP server
  exposes to LLMs (`execute_query`, `latest_results`, `ping`), plus
  a high-level `fetch_metric(name, params)` that resolves each
  metric to a saved Dune query id. **Nothing else feeds Level 2** —
  no CEX, no direct RPC reads. This keeps the analytics path
  consistent, cacheable and shareable as a Dune dashboard.
- **Chain.** Same `DUNE_CHAIN` switch as L1. Every SQL template is
  chain-agnostic and parameterised by BTC / ETH token addresses.
- **Per-symbol metrics.** Funding (current + 8h / 24h delta +
  weighted average + annualised %), open interest (current +
  1h / 4h / 24h deltas), volume + 1h-vs-24h spike detection,
  long/short ratio with inferred bias, cumulative funding paid /
  received over the window, whale-activity flag with rationale.
  Because the underlying tape is spot (`dex.trades`), every "perp"
  metric is a clearly labelled spot-derived proxy:
  - Funding rate = buy-vs-sell USD imbalance over 8h, scaled to a
    per-8h funding-equivalent (`0.05% / 1.0` of imbalance).
  - Open interest = rolling-USD volume + 1h / 4h / 24h deltas.
  - Long/short ratio = `sum(buy_usd) / sum(sell_usd)`, with
    unique-wallet counts for the account-ratio columns.
  - Cumulative funding = net signed aggressor flow scaled across
    the lookback window.

  When a real perp-DEX schema lands on Dune (Arc, Synthetix V3
  perps, etc.) the `dex.trades` source swaps for the perp's `fills`
  / `funding_events` table and the rest of the pipeline is
  unchanged.
- **Vault-level metrics.** TVL + net deposits / withdrawals over the
  window for `DUNE_PERP_VAULT_ADDRESS` (defaults to
  `ARC_PERP_VAULT_ADDRESS`).
- **Market heat.** The Dune `market_sentiment` query returns a
  `heat` in `[0, 1]` (1.0 = max-bullish, 0.0 = max-bearish, 0.5 =
  neutral). When the query isn't configured, the engine falls back
  to a heuristic blend of funding, OI 1h delta, 24h price change
  and L/S. `heat` bucketed into `risk_on` / `risk_off` / `neutral`
  / `transition` is what feeds the L2 panel.
- **Conviction** (`LevelScore.score`):
  ```
  l2_conviction = min(1.0, max(2·|heat − 0.5|, bias_strength))
  ```
  `heat` alone is *directional* (0.85 = bullish, 0.15 = bearish),
  so it is a poor *conviction* metric — both extremes are equally
  decisive on-chain. The `2·|heat-0.5|` term symmetrises heat into a
  conviction in `[0, 1]`; the `max(..., bias_strength)` term catches
  the case where heat sits near neutral but the on-chain bias
  signals (funding / OI / whales / L/S / price change / heat
  extremes) point decisively one way.
- **Direction** (`LevelScore.direction_sign`). Sign of `market_bias`
  produced by `_compute_market_bias`: `bullish = +1`, `bearish = -1`,
  `neutral = 0`. `bias_strength ∈ [0, 1]` is the magnitude margin
  of the directional vote across funding / OI / 24h / L/S / whales
  / heat extremes; it is also exposed on the directive and in the
  panel as `(strength=0.72)`.
- **Provenance is first-class.** Every metric reports its source as
  `dune:<query_id>`, `n/a` (with the exact `DUNE_QUERY_*_ID` env
  var to set), or `error`. The L2 panel renders this map so demo
  and live runs are honest about what's actually on-chain.
- **SQL templates** ship in `dune/queries/`; see
  [`dune/README.md`](./dune/README.md) for the per-query column
  contracts and the wiring workflow.
- **Caching.** In `DEMO_MODE`, the full `Level2Intelligence` payload
  is cached for `DEMO_CACHE_TTL_SECONDS` (default 30 min). The
  cache is **skipped on any erroring metric** so a transient Dune
  failure isn't frozen into the next 30 minutes of runs.
- **Output.** `LevelScore { score = l2_conviction, direction_sign,
  raw }` plus a full `Level2Intelligence` payload exposed via
  `raw["l2"]` (per-symbol metrics, vault flow, `metric_status`
  provenance, conviction breakdown, notes).
- **Code.** `src/core/level2.py`.

### 2.4 Level 3 — Claude Sonnet 4.6 (via OpenRouter) — final arbiter

- **Role.** The *arbiter*, not just another signal. Claude sees the
  full structured outputs of L1 and L2 plus a compact market
  briefing, and emits a final `(conviction, direction, regime,
  recommended_intensity)` verdict plus an English rationale and a
  list of `key_factors`.
- **Upstream model.** Default is `anthropic/claude-sonnet-4.6`
  served through OpenRouter's OpenAI-compatible
  `/v1/chat/completions` gateway. Swapping to any other slug on
  [openrouter.ai/models](https://openrouter.ai/models) (e.g.
  `openai/gpt-4o`, `mistralai/mistral-large-2411`) is a one-line
  `OPENROUTER_MODEL=...` change in `.env`; nothing else in the
  engine, prompt or downstream wiring needs to change.
- **Two personas (`L3_MODE`).** The arbiter ships in two distinct
  modes that share the same JSON contract but differ in *how Claude
  thinks and writes*:

  - **`L3_MODE=critical` (default, demo-quality).** Claude is
    instructed to behave as an *independent senior risk manager*
    with explicit veto authority over the upstream cascade. The
    persona is `skeptical by default`: agreement with L1+L2 must be
    *earned* by the data, not assumed. See
    [§2.4.1 Critical mode in depth](#241-critical-mode-in-depth)
    below for the decision rules, rationale template and override
    rights. System prompt:
    [`prompts/level3_arbiter_critical.md`](./prompts/level3_arbiter_critical.md).

  - **`L3_MODE=standard`.** Concise trader-voice prompt: 1-2
    sentence English rationale plus 2-4 short technical tags.
    Useful for high-frequency loops where the structured 5-section
    rationale is overkill. System prompt:
    [`prompts/level3_arbiter_standard.md`](./prompts/level3_arbiter_standard.md).

  The mode swap is a single `.env` change. It only affects the
  system prompt and a mode-specific reminder appended to the user
  prompt; nothing else in the engine, weights, fallback contract or
  downstream wiring changes. Both modes return the same
  `ArbiterResponse` JSON shape, so the rest of the pipeline cannot
  tell which persona produced the verdict — only the panel renders
  the active mode badge for the operator.

- **Inputs.** Rich `ArbiterBriefing`:
  - `primary_symbol`
  - `level1_conviction`, `level1_direction_sign`, `level1_rationale`,
    `level1_passes`, `level1_raw` (per-symbol trend / ATR%).
  - `level2_conviction`, `level2_direction_sign`,
    `level2_market_bias`, `level2_bias_strength`,
    `level2_market_heat`, `level2_regime`, `level2_rationale`,
    `level2_raw` (per-symbol funding / OI / volume / L-S /
    whales / vault flows).
  - `market_snapshot` (symbols, account, drawdown, RPC, chain).

  The briefing is rendered into compact Markdown — section titles
  act as natural attention anchors and every numeric value lives
  inline so Claude never has to re-derive anything.

- **Contract.** Claude **must** return strict JSON matching
  `ArbiterResponse`:

  ```json
  {
    "conviction": 0.85,
    "direction": "long" | "short" | "neutral",
    "regime":    "risk_on" | "risk_off" | "hold",
    "recommended_intensity": 0.65,
    "rationale":   "<English; critical-mode = 5-section template, standard-mode = 1-2 sentences>",
    "key_factors": ["descriptive phrase", "descriptive phrase", "..."]
  }
  ```

  Pydantic validates the shape, enums and float ranges (rationale up
  to 4000 chars so the critical-mode template fits comfortably).
  Any deviation (missing fields, out-of-range numbers, wrong enums,
  malformed JSON, HTTP error, timeout) **falls back to a neutral
  HOLD** (`conviction=0, direction=neutral, regime=hold,
  recommended_intensity=0.0`) — the agent never trades on a
  hallucinated or partial verdict. Synthetic and fallback rationales
  still follow the active mode's format so the panel stays visually
  consistent whether Claude is wired or not.

- **System prompts.** Two files under `prompts/`, both English:
  [`level3_arbiter_critical.md`](./prompts/level3_arbiter_critical.md)
  (independent risk-manager voice with the 5-section rationale
  template) and
  [`level3_arbiter_standard.md`](./prompts/level3_arbiter_standard.md)
  (trader voice, 1-2 sentence rationale). The path is resolved by
  `Level3Config.resolved_prompt_path()` from `Level3Config.mode`
  (which `main.py` populates from `settings.L3_MODE`). Both
  files are tunable without code changes; both repeat the
  conviction/direction independence rule and the strict no-prose
  JSON requirement.

- **Generation config.** Temperature pinned to
  `OPENROUTER_TEMPERATURE` (default `0.2`). `OPENROUTER_MAX_TOKENS`
  defaults to `2048` so the critical-mode 5-section rationale never
  truncates. Anthropic models don't accept OpenAI's
  `response_format={"type":"json_object"}`, so we enforce JSON via
  the system prompt + an explicit "JSON only, no markdown" trailer
  in the user prompt + a permissive parser that strips
  ```` ```json ```` fences defensively. Retries are bounded
  (`OPENROUTER_MAX_RETRIES`, exponential backoff) only on transient
  failures (HTTP 429 / 5xx, network blips); hard failures (401
  auth, 404 model, 402 payment, 403 policy) bypass retry and
  surface immediately via `OpenRouterAPIError` with an actionable
  diagnosis.

- **Weight redistribution.** When the arbiter returns a real Claude
  verdict, the engine does **not** redistribute L3's weight — L3
  votes with its full configured weight (default `0.40`). The
  synthetic-L3 path (no `OPENROUTER_API_KEY`) still redistributes
  the placeholder's weight to L1+L2.

- **Code.** `src/core/level3.py` (`Level3` + `ArbiterResponse` +
  `Level3Arbiter` + `Level3Config.mode` resolution), client in
  `src/llm/openrouter_client.py`, system prompts in
  `prompts/level3_arbiter_{critical,standard}.md`.

#### 2.4.1 Critical mode in depth

Critical mode is the **default L3 persona** and the one the demo
runs on. Where the standard prompt asks Claude to summarise
upstream signals, the critical prompt asks Claude to *audit* them.
The behavioural contract is:

- **Independent authority.** Claude is told it is *not* a data
  synthesizer — it is the final line of defence against bad trades.
  It has explicit permission to:
  - **Disagree** with Level 1's technical signal when the evidence
    is fragile or single-timeframe.
  - **Override** Level 2's market bias when the underlying metric
    mix is thin (e.g. one signal supporting bias_strength=0.86 is
    weaker than three signals supporting bias_strength=0.60).
  - **Refuse to trade** (`regime="hold"`, `direction="neutral"`,
    low `conviction`) when conditions look fragile, even if both
    upstream levels recommend action.
  - **Reduce intensity** (≤ 0.5) when conviction is real but
    conditions are noisy (high ATR%, drawdown, conflicting
    signals).

- **Decision principles (baked into the prompt).**
  1. **Skepticism is the baseline.** When in doubt, lower conviction
     or return `regime="hold"`. "Insufficient quality" is a valid
     reason to stand down.
  2. **Quality over direction.** Three weak signals pointing the
     same way are weaker than two strong, independent signals from
     different metric categories.
  3. **Drawdown is sacred.** Above 5% drawdown clamp intensity to
     ≤ 0.5 regardless of conviction; above 8% clamp to ≤ 0.3.
  4. **Volatility caps size.** ATR% > 4% → clamp intensity ≤ 0.5;
     ATR% < 0.2% (dead market) → `regime="hold"`.
  5. **Symmetry.** A 0.9 short and a 0.9 long are equally decisive.
     Shorts are not down-weighted.
  6. **Independence.** L1+L2 are *inputs*, not orders. Disagreement
     is acceptable — required, even — when justified by the data.

- **Mandatory 5-section rationale template.** Critical mode enforces
  this exact layout (separated by blank lines so the console panel
  can colour each header):

  ```
  Market Context:
  [1-2 sentences on what the market is doing right now per the
   briefing — primary symbol, price direction, drawdown, ATR regime.]

  Key Signals Analysis:
  • Signal 1 — why it matters + strength + concrete number cited
    from the briefing.
  • Signal 2 — same.
  • Signal 3 — same.
  [3-5 bullets; every bullet references a real number from the
   briefing — funding rate, OI delta, L/S ratio, whale direction,
   ATR%, etc. Inventing numbers is forbidden.]

  Contradictions & Risks:
  [Explicit. Call out where L1 and L2 disagree, where a metric
   looks fragile, or where volatility/drawdown caps the trade.
   If everything aligns cleanly, write: "No material
   contradictions; signals converge." This section is mandatory.]

  My Independent View:
  [First-person opinion: "I agree with L2 here because...", "I push
   back on L1 because...". State whether you fully buy the upstream
   signal, partially buy it, or reject it. Justify any rejection.]

  Final Recommendation:
  [Clear verdict + why intensity is set where it is + what would
   make you change your mind. End with one decisive sentence,
   e.g. "Open SHORT at intensity 0.55 — strong on-chain bearish
   convergence, trimmed by elevated ATR."]
  ```

  The console renderer (`_format_rationale_text` in
  `src/utils/console.py`) recognises these five headers and paints
  each one in bold bright-cyan so the verdict is scannable at a
  glance. Bullet lines get a coloured `•` marker.

- **Mandatory `key_factors`.** 2 to 4 descriptive English phrases
  (never single words, never empty, never `"-"`). Format
  recommendation: `"<observation> (<evidence>)"`, e.g.
  `"converging bearish signals (funding -16.4%, OI -4%, whales distributing)"`.
  The console renderer applies a defensive
  `_normalise_key_factors` helper so malformed shapes (empty
  strings, list-of-dicts, comma-separated single string) still
  produce a clean bullet list.

- **Anti-hallucination guards.** The prompt explicitly forbids
  citing numbers that don't appear in the briefing, deciding on
  anything other than BTC-PERP / ETH-PERP, and refusing to answer
  ("insufficient data" is not allowed — under uncertainty Claude
  must still return `direction="neutral"`, `regime="hold"`, low
  conviction, *and* a fully populated 5-section rationale + 2-4
  key_factors).

- **Synthetic + fallback consistency.** When no `OPENROUTER_API_KEY`
  is set, the synthetic L3 emits a placeholder rationale that
  *also* follows the 5-section layout, so the panel renders the
  same visual structure regardless of whether real Claude is wired.
  Same for the safe-HOLD fallback after a Claude error — the
  rationale explains the failure mode within the 5-section
  template.

### 2.5 Conviction vs Direction split

The single most important design decision in the engine.

Every level emits **two orthogonal values**:

| Field            | Range            | Meaning                                       |
|------------------|------------------|-----------------------------------------------|
| `score` (alias `conviction`) | `[0, 1]` | "How strongly do we want to act?"            |
| `direction_sign` | `{-1, 0, +1}`    | "If we act, which side?" (-1 short, 0 none, +1 long) |

**Why this split matters.** Previously a bearish setup with low heat
(`market_heat = 0.15`) emitted a low aggregated score, which the
router interpreted as "low conviction → close everything" even though
on-chain it was actually a *high-conviction SHORT*. Folding heat into
a symmetric conviction (`2·|heat-0.5|`) and tracking direction on a
separate axis lets that exact case open a SHORT at full conviction —
the engine is now symmetric for longs and shorts.

**Per-level vote summary:**

| Level | Conviction formula                                              | Direction sign                          |
|-------|-----------------------------------------------------------------|-----------------------------------------|
| L1    | `mean(per-symbol strength)` — no `0.5` floor                    | Sign of primary symbol's trend          |
| L2    | `max(2·|heat − 0.5|, bias_strength)` capped at 1.0              | Sign of `market_bias` ({bull, bear, neutral}) |
| L3 (real)     | Claude-arbited `conviction` ∈ `[0, 1]` (strict JSON, Pydantic-validated) | Claude `direction` ∈ {long, short, neutral} → {-1, 0, +1} |
| L3 (synthetic) | `0.5·L1 + 0.5·L2` (no `OPENROUTER_API_KEY`)            | conviction-weighted sign of L1 + L2     |
| L3 (fallback HOLD) | `0.0` (OpenRouter errored or returned malformed JSON) | `0` (neutral) — safe HOLD              |

### 2.6 Effective weights & L3 redistribution

Configured default weights:

| Level | Weight |
|-------|--------|
| L1    | 0.25   |
| L2    | 0.35   |
| L3    | 0.40   |

When L3 is the synthetic placeholder (`raw["l3"]["synthetic"] == True`)
and `REDISTRIBUTE_SYNTHETIC_L3_WEIGHT == True` (the default), L3's
weight is **redistributed proportionally to L1 + L2** in aggregation:

```
w1_eff = w1 + w3 · (w1 / (w1 + w2))
w2_eff = w2 + w3 · (w2 / (w1 + w2))
w3_eff = 0
```

With the defaults, `0.25 / 0.35 / 0.40` becomes effective
`0.4167 / 0.5833 / 0.0`. Without this, a placeholder L3 (which is
mathematically just a function of L1 + L2) would silently dilute the
real signal back into itself — wasting 40% of the budget on
re-blending information we already have. Both the configured and the
effective weights are surfaced in `DecisionResult.weights` and
`DecisionResult.effective_weights`, and the Final Decision panel
renders them side-by-side (`0.25 → 0.42` in yellow when they differ).

Once `OPENROUTER_API_KEY` is set and Claude returns a clean verdict,
`synthetic` is no longer flagged and L3 votes with its full
configured weight (default `0.40`). If OpenRouter errors or returns
malformed JSON, the arbiter falls back to a safe HOLD
(`conviction=0, direction=neutral`); that HOLD is **not** flagged as
synthetic — it represents a real Claude call that was rejected by
validation, so it correctly counts as L3's vote at zero conviction.

### 2.7 Aggregation

**Final conviction** (simple weighted average over effective weights):

```
final_conviction = Σᵢ (w_eff_i · convictionᵢ)
```

**Final direction** is a *conviction-weighted* vote, so a level that
is unsure can't drag the side:

```
weighted_vote        = Σᵢ (w_eff_i · convictionᵢ · direction_signᵢ)
weighted_conviction  = Σᵢ (w_eff_i · convictionᵢ)
direction_score      = weighted_vote / weighted_conviction      ∈ [-1, +1]
direction_strength   = |direction_score|                          ∈ [0, 1]

final_direction = +1   if direction_score >  SHORT_BIAS_MIN_STRENGTH
final_direction = -1   if direction_score < -SHORT_BIAS_MIN_STRENGTH
final_direction =  0   otherwise
```

`SHORT_BIAS_MIN_STRENGTH` defaults to `0.35` — the minimum
directional consensus required to call the side at all. Below that
threshold the engine treats direction as neutral, regardless of
conviction.

**Short-circuit.** When L1 blocks, the aggregator never runs:
`final_conviction = 0`, `final_direction = 0`, `regime = "risk-off"`,
`short_circuited = True`, with L2 / L3 carrying zero scores marked
`raw.skipped = True`.

### 2.8 Decision rules — turning `(conviction, direction)` into action

```
conviction >= RISK_ON_THRESHOLD AND direction != 0
    -> action = risk_on, side = direction, intensity = full

conviction <= RISK_OFF_THRESHOLD
    -> action = risk_off, side = none, intensity = full (close)

risk_off < conviction < risk_on
   AND direction != 0
   AND direction_strength >= STRONG_BIAS_OPEN_STRENGTH
    -> action = risk_on (STRONG-DIRECTION override),
       side = direction,
       intensity = 0.5 · conviction · direction_strength

otherwise
    -> action = hold
```

- `RISK_ON_THRESHOLD` defaults to `0.6`, `RISK_OFF_THRESHOLD` to `0.4`,
  `STRONG_BIAS_OPEN_STRENGTH` to `0.6`.
- The **STRONG-DIRECTION override** is the engine's "fall-back into
  the trade at reduced size" path: when aggregated conviction is
  ambiguous but the directional vote is decisive and unanimous, the
  agent opens a position at half the normal intensity rather than
  sitting idle through a clean setup.
- `intensity` is the size knob the router consumes; it caps at `1.0`
  for full risk-on opens and uses the reduced formula for mid-band
  overrides.
- All downstream guards (drawdown haircut, max leverage, max position
  size, L1 short-circuit) apply identically to longs and shorts.

---

## 3. Allocation Router

The router consumes a `DecisionResult` and turns it into a concrete
on-chain action via the `ArcPerpExecutor`. SHORT directives are
first-class citizens, not "sell to close" syntactic sugar — every
sizing step, leverage cap and gas path is the same for both sides.

### 3.1 Decision-to-action mapping

| Engine output                                       | Router action                                                 |
|-----------------------------------------------------|---------------------------------------------------------------|
| `action=risk_on, side=long`                         | `ArcPerpExecutor.open_position(side="long", …)`               |
| `action=risk_on, side=short`                        | `ArcPerpExecutor.open_position(side="short", …)`              |
| `action=risk_off`                                   | `ArcPerpExecutor.close_position(…)` — side-agnostic flatten   |
| `action=hold`                                       | no on-chain change, just logged                               |
| `short_circuited=True`                              | router runs the close path with `reason="L1 short-circuit"`   |
| stale data (all zero scores, no short-circuit flag) | router denies the cycle (`action="deny"`, no tx)              |

### 3.2 Sizing pipeline (4 steps)

Sizing is no longer "base × (1 + intensity)" — that ignored how
volatile the market is and how close to the drawdown limit we are.
The new pipeline:

```
1. Vol-target:
   vol_target = (equity_usd × TARGET_RISK_PCT)
                / (STOP_ATR_MULT × ATR%/100)

2. × Intensity (from the engine directive):
   after_intensity = vol_target × intensity

3. × Drawdown haircut (gradient, not cliff):
   haircut = max(0, 1 − (drawdown_pct / MAX_DRAWDOWN_PCT) ** DD_HAIRCUT_EXPONENT)
   after_haircut = after_intensity × haircut

4. Cap at MAX_POSITION_USD:
   size_usd = min(after_haircut, MAX_POSITION_USD)
```

Defaults: `TARGET_RISK_PCT = 0.02` (2% of equity at risk per trade),
`STOP_ATR_MULT = 1.5`, `MIN_ATR_PCT_FOR_SIZING = 0.25%` (floor on
ATR% in the denominator), `DD_HAIRCUT_EXPONENT = 2.0`,
`MAX_POSITION_USD = $10,000`.

- **Where ATR% comes from.** The router reads
  `decision.level_score(1).raw["l1"]["per_symbol"][primary_symbol]["atr_pct_avg"]`.
  If L1 never ran (account-only context) or ATR% is missing, the
  router falls back to `BASE_POSITION_USD × (1 + intensity)`.
- **Where equity comes from.** `ArcPerpExecutor.get_account_info()`
  reads `USDCCollateralVault.getBalance(accountId)` on Arc Testnet.
  If equity is `0` (typical first dry-run cycle before any margin
  is deposited), the router falls back to the legacy intensity-only
  path so the demo still produces meaningful sizes.
- **Asymmetric shorts (optional).** When `SYMMETRIC_SHORT_SIZING ==
  False`, short opens multiply by `SHORT_SIZE_MULTIPLIER` (≤ 1.0)
  after step 4. Default is `True` (full long/short parity).
- **Drawdown haircut shape.** Exponent `2.0` makes the early
  haircut soft and the last few percent hard: `5% dd → ×0.75`,
  `9% dd → ×0.19`, `10% dd = MAX_DRAWDOWN_PCT → ×0` (and the
  hard-close guard below has already fired).
- **Sizing telemetry.** When `EXPLAIN_SIZING == True` (default),
  every plan stamps the full breakdown (`method`, `atr_pct`,
  `vol_target_size_usd`, `after_intensity_size_usd`,
  `drawdown_pct`, `dd_haircut`, `after_haircut_size_usd`,
  `size_usd`, `cap_hit`) onto `ExecutionPlan.extra["sizing"]`, and
  the Execution Plan panel renders a "Sizing pipeline (why this
  size?)" table so the demo answers *why*, not just *what*.

### 3.3 Hard overrides

The router enforces these *before* delegating to the engine's
recommendation:

| Override                              | Trigger                                                | Result                                       |
|---------------------------------------|--------------------------------------------------------|----------------------------------------------|
| **Drawdown breach**                   | `-pnl / equity × 100 ≥ MAX_DRAWDOWN_PCT`               | Forced `close_position` (regardless of action) |
| **Stale data**                        | All level scores `== 0` AND not a short-circuit        | `action="deny"`, no tx                       |
| **Leverage cap**                      | `directive.target_leverage > ARC_PERP_MAX_LEVERAGE`    | Clipped to `ARC_PERP_MAX_LEVERAGE` (default 3x) |
| **Max position cap**                  | `size_usd > MAX_POSITION_USD`                          | Clipped to `MAX_POSITION_USD`                |
| **Live mode without matcher URL**     | `--live` AND `ARC_PERP_MATCHER_URL` unset              | Graceful fallback to `deposit_margin` so capital still lands on the venue |

---

## 4. Execution

- **Wallet.** Circle Developer-Controlled Wallet (non-custodial,
  programmatic). Entity secret encrypted per-request via
  **RSA-OAEP-SHA256** against Circle's cached public key — fresh
  ciphertext on every call, as Circle requires.
- **Gas.** Sponsored via Circle Paymaster on Arc Testnet. The
  `gasPolicyId` is attached to the `contractExecution` body;
  `TxResult.sponsored` flips to `True` when a policy is configured.
- **Liquidity routing.** CCTP v2 for bringing USDC onto Arc and back
  out (Day 4+).
- **Trading venue.** Arc Perp DEX — order-book + on-chain batch
  settlement (dYdX v3 style):
  - `ClearingHouse 0x70a06946…` — `settleBatch` entrypoint
  - `USDCCollateralVault 0x75E4FBFB…` — `deposit / withdraw / getBalance`
  - `MarketRegistry 0x9cED23e4…` — `getMarket`
  - `PositionLedger 0xd6D77291…` — `getPosition / positionExists`
  - Off-chain matcher (`ARC_PERP_MATCHER_URL`) for EIP-712
    `OrderTypes.Order` POSTs (Day 4 wire-up).
- **Yield leg.** USYC mint / redeem against USDC (Day 4 wire-up).
- **Idempotency.** Every action is keyed by an internal
  `decision_id = "dec-<unix_ts>-<8hex>"` so retries never
  double-trade.

---

## 5. Module map

| Folder           | Responsibility                                                                 |
|------------------|--------------------------------------------------------------------------------|
| `src/core`       | `DecisionEngine`, `Level1`, `Level2`, `Level3`, conviction/direction aggregation |
| `src/data`       | `DuneMCPClient` (single source of truth for L1 + L2), `DuneMarketData` (OHLCV adapter on `dex.trades`), `ArcOnchainReader` (account state only — wallet / vault) |
| `src/execution`  | `ArcPerpExecutor` (margin + position), `CircleWallet` (DCW + Paymaster + RSA-OAEP signing) |
| `src/allocation` | `AllocationRouter`: directive → on-chain action, vol-targeted sizing, gradient drawdown haircut |
| `src/llm`        | `OpenRouterClient` for Level 3 final arbitration (Claude Sonnet 4.6 default)   |
| `src/utils`      | `Settings` (pydantic + `.env` loader), `console` (seven rich panels incl. L3 with section-coloured rationale), `logging` (loguru sinks) |
| `prompts/`       | Level 3 arbiter system prompts — one per `L3_MODE`: `level3_arbiter_critical.md` (default, 5-section rationale) + `level3_arbiter_standard.md` (concise trader voice) |
| `dune/`          | Dune MCP SQL templates (`dune/queries/*.sql`) + per-query column contracts in `dune/README.md` |
| `scripts/`       | One-off ops scripts (Circle wallet creation, future seed / simulate / backtest) |
| `tests/`         | pytest + pytest-asyncio                                                        |

---

## 6. Operating principles

1. **On-chain by default.** If an action can happen on Arc through
   Circle primitives, it must.
2. **Dune MCP is the single source of truth.** Every market signal —
   OHLCV (L1) and on-chain intelligence (L2) — reads through a saved
   Dune query. No CEX feed, no direct RPC market-data scrape. Arc
   RPC is used **only** for account state (wallet balance, vault
   TVL, agent margin).
3. **Chain-portable, not chain-coupled.** The decision engine reads
   from whatever chain `DUNE_CHAIN` points at (`ethereum` / `base` /
   `arbitrum` shipped today). Switching is a one-line `.env` change
   because every SQL template is parameterised by chain + token
   addresses.
4. **Cascade, don't average.** Levels are *gates*, not weighted
   blobs. If L1 says no, the engine doesn't call L2 / L3 and never
   produces a false-positive risk-on.
5. **Decouple conviction from direction.** Every level votes on two
   axes. Direction is aggregated as a *conviction-weighted* vote so
   wishy-washy levels can't drag the side. The engine is symmetric
   for longs and shorts: a low conviction means "stand down", not
   "be bearish".
6. **Placeholders don't dilute signal.** A synthetic L3 (no
   OpenRouter API key wired) is a function of L1 + L2; its weight is
   redistributed back to L1 + L2 in aggregation so the placeholder
   never silently re-blends the real signal into itself.
7. **Risk-scale, don't risk-flat.** Position size is solved from
   account equity, target risk-per-trade and the primary symbol's
   ATR%. The agent risks a fixed *fraction of equity* per trade, not
   a fixed dollar amount.
8. **Gradient guards, not cliffs.** Drawdown trims intensity
   smoothly via `1 − (dd/max_dd)^exponent` long before the hard
   `MAX_DRAWDOWN_PCT` close is triggered.
9. **Safety over alpha.** Drawdown guard and stale-data guard
   always win over signals. L1 hard rules veto trades; they are
   never softened by L2 / L3.
10. **Deterministic decisions.** Same inputs → same conviction → same
    direction → same action. The L3 LLM (Claude Sonnet 4.6 by
    default) is pinned to low temperature and a strict JSON output
    contract; ambiguous replies fall back to a safe HOLD.
11. **Honest provenance.** Every metric (L1 OHLCV included) reports
    its Dune query id (or `n/a` with a clear note) so users always
    know whether a number is on-chain truth or a placeholder.
    Spot-derived "perp" proxies (funding / OI / L/S / cum funding)
    are labelled as such in the SQL header comments.
12. **Observable.** Every decision logs its inputs, per-level
    conviction + direction, effective weights, final conviction +
    direction, action, sizing breakdown and tx hash. The CLI
    renders a seven-panel rich report on each cycle (the L3 panel
    paints the active mode badge — `CRITICAL` or `STANDARD` — and
    colours each section header of the structured rationale so the
    "why" is scannable at a glance).
13. **Idempotent execution.** Every action is keyed by a
    `decision_id` so retries never double-trade.
14. **Modular.** Each level is replaceable; the router doesn't care
    *how* a conviction or direction was computed.

---

## 7. Current status

| Day | Status      | Notes                                                                              |
|-----|-------------|------------------------------------------------------------------------------------|
| 1   | ✅ Done     | Repository scaffolding, three-level stubs, secret hygiene, `AGENTS.md` cascade spec. |
| 2   | ✅ Done     | Live Arc Perp DEX margin moves through Circle DCW + Paymaster + RSA-OAEP encryption. `deposit_margin / withdraw_margin / get_margin / get_position` work end-to-end on Arc Testnet. `--dry-run` / `--live` / `--loop` with strict pre-flight checks. |
| 3   | ✅ Done     | L1 + L2 fully wired through **Dune MCP as the single source of truth** on live Ethereum / Base / Arbitrum data (Arc Testnet isn't indexed by Dune yet). OHLCV via `DuneMarketData` on `dex.trades` + 8 on-chain metrics (9/9 saved Dune queries live, every metric carries `dune:<id>` provenance). **Conviction / direction split** at every level (L1 emits per-symbol trend sign + raw strength, L2 emits `max(2·|heat-0.5|, bias_strength)` + market_bias sign). **Synthetic-L3 weight redistribution** to L1 + L2 in aggregation. **Conviction-weighted direction vote** across levels. **STRONG-DIRECTION override** opens at reduced intensity in the mid-band. **Vol-targeted sizing** (`equity × target_risk / (stop × ATR%)`) using L1's primary-symbol ATR%. **Gradient drawdown haircut** (`1 − (dd/max_dd)^2`) replaces the legacy cliff stop. Six-panel rich CLI shows configured-vs-effective weights, per-level direction votes, and a full sizing-pipeline breakdown. `--test-bias` offline scenario tester. |
| 4   | ✅ Done     | **Real Claude Sonnet 4.6 final arbiter wired end-to-end via OpenRouter, with a two-mode persona system.** Async `OpenRouterClient.generate_json` round-trip via `httpx.AsyncClient` POST `/v1/chat/completions`, pinned `temperature=0.2`, `OPENROUTER_MAX_TOKENS=2048` so the structured rationale never truncates. JSON enforced via system-prompt + user-prompt trailer + permissive parser (Anthropic models don't accept `response_format`); bounded retry with exponential backoff on HTTP 429 / 5xx, hard failures (401 / 402 / 403 / 404) bypass retry via `OpenRouterAPIError` with actionable diagnoses. Rich `ArbiterBriefing` built from full L1 + L2 raw payloads + market context, rendered as compact Markdown. Strict `ArbiterResponse` Pydantic model (`conviction`, `direction`, `regime`, `recommended_intensity`, English `rationale` up to 4000 chars, `key_factors`). **Two L3 personas via `L3_MODE`:** `critical` (default — independent senior risk manager with veto authority, mandatory 5-section structured rationale `Market Context → Key Signals Analysis → Contradictions & Risks → My Independent View → Final Recommendation`, baked-in skepticism / drawdown / volatility / quality-over-direction principles) and `standard` (concise trader-voice, 1-2 sentence rationale). System prompts live in [`prompts/level3_arbiter_critical.md`](./prompts/level3_arbiter_critical.md) and [`prompts/level3_arbiter_standard.md`](./prompts/level3_arbiter_standard.md); `Level3Config.resolved_prompt_path()` picks the right file from `settings.L3_MODE`. **Safe HOLD fallback** on schema violation / HTTP error / timeout (synthetic + fallback rationales also follow the active mode's format so the panel stays visually consistent). Synthetic placeholder retained when `OPENROUTER_API_KEY` is unset (weight still redistributed to L1+L2). **Seventh rich CLI panel** renders provider / model / latency / `Mode CRITICAL` (or STANDARD) badge / verdict / section-coloured rationale / bulleted key_factors / fallback details. Smoke tests: `tests/test_openrouter_client.py` (JSON parsing, message-content extraction, HTTP status mapping, transient-vs-hard classification) + `tests/test_level3_arbiter.py` (Pydantic validation, synthetic, fallback, prompt rendering, mode-based prompt resolution for both critical and standard, long 5-section rationale acceptance, full L1→L2→L3 cascade aggregation with real + synthetic L3 — 21 tests total). `--test-bias ... --real-sonnet` exercises the live OpenRouter round-trip end-to-end with a coherent synthetic briefing. |
| 5   | 🚧 Planned  | EIP-712 order signing for `open_position` once `ARC_PERP_MATCHER_URL` is published, USYC rotation on risk-off, JSONL decision log for replay / backtest. |
