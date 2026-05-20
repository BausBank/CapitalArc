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
  SOL-PERP`, fetched through `BinanceClient` (Binance USDT-M Perp is
  the Day-3 proxy for the venue's market data).
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
  `Level1Reason` so the UI can render the *why*. Mapped to
  `LevelScore` in `[0, 1]` (0.0 when blocked; `0.5 + 0.5 * strength`
  otherwise).
- **Code:** `src/core/level1.py`.

### Level 2 - On-chain intelligence
- **Inputs:** three live feeds aggregated under one Level-2 facade:
  - `DuneMCPClient` (`src/data/dune_mcp.py`) — speaks the same
    Bearer-authenticated surface the Dune MCP server exposes to LLMs
    (`https://api.dune.com/api/v1/...` with `DUNE_API_KEY`), with a
    TTL cache and a `ping()` health check.
  - `BinanceClient` — funding rate history, premium / mark price,
    open-interest history, 24h ticker, long/short ratio.
  - `ArcOnchainReader` (`src/data/arc_onchain.py`) — vault USDC TVL,
    agent margin, recent vault deposits / withdrawals via ERC-20
    `Transfer` logs.
- **Per-symbol metrics:** funding (current + 8h / 24h delta +
  weighted average + annualised %), OI (current + 1h / 4h / 24h
  deltas), volume + z-score spike detection, long/short ratio with
  inferred bias, cumulative funding paid / received over a 48h
  window, whale-activity flag derived from 1h OI deltas.
- **Aggregation:** market-wide *heat* score in `[0, 1]` blended with
  an Arc-vault-flow modifier, then bucketed into `risk_on` /
  `risk_off` / `neutral` / `transition`. In `DEMO_MODE` the full
  `Level2Intelligence` payload is cached for
  `DEMO_CACHE_TTL_SECONDS` (default 30 min).
- **Output:** `LevelScore` in `[0, 1]` with the full intelligence
  payload exposed via `raw["l2"]`.
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
| `src/data`       | Market data adapters: Binance perp, Dune MCP, Arc on-chain      |
| `src/execution`  | Arc Perp DEX client, order/position management, risk checks     |
| `src/allocation` | Risk-on / risk-off router, USYC rotation, CCTP moves            |
| `src/llm`        | Gemini 2.5 Flash client (Level 3 final arbiter)                 |
| `src/agents`     | Top-level autonomous agent loop                                 |
| `src/utils`      | Config (pydantic), logging (loguru), rich console panels        |
| `prompts/`       | LLM prompt templates used by the Level 3 arbiter                |
| `scripts/`       | One-off ops scripts (deploy wallets, simulate, backtest, seed)  |
| `tests/`         | Unit + integration tests                                        |

---

## 6. Operating principles

1. **On-chain by default.** If an action can happen on Arc through Circle primitives, it must.
2. **Deterministic decisions.** Same inputs -> same score -> same action. Gemini is pinned to low temperature and strict JSON output.
3. **Safety over alpha.** Drawdown guard and stale-data guard always win over signals. L1 hard rules veto trades; they are never softened by L2 / L3.
4. **Cascade, don't average.** Levels are *gates*, not weighted blobs. If L1 says no, the engine doesn't call L2 / L3 and never produces a false-positive risk-on.
5. **Observable.** Every decision logs its inputs, level scores, final score, action and tx hash. The CLI renders a six-panel rich report on each cycle.
6. **Modular.** Each level is replaceable; the router does not care how a score was computed.

---

## 7. Current status

| Day | Status      | Notes                                                                              |
|-----|-------------|------------------------------------------------------------------------------------|
| 1   | ✅ Done     | Repository scaffolding, three-level stubs, secret hygiene.                         |
| 2   | ✅ Done     | Live Arc Perp DEX margin moves through Circle DCW + Paymaster + RSA encryption.    |
| 3   | ✅ Done     | Level 1 hard rules + Level 2 on-chain intelligence (Dune MCP + Binance + Arc RPC). Cascading engine with short-circuit. Rich-panel CLI. |
| 4   | 🚧 Planned  | Real Gemini 2.5 Flash arbiter (L3), EIP-712 order signing for `open_position`, USYC rotation on risk-off, JSONL decision log. |
