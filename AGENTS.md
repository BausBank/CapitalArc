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

### Level 1 - Technical rules
- **Inputs:** OHLCV from Arc Perp DEX, funding rate, open interest.
- **Signals:** trend (EMA stack), momentum (RSI, MACD), volatility regime (ATR, realized vol), structural breaks.
- **Output:** `l1_score` in `[0, 1]`.
- **Code:** `src/core/level1.py`.

### Level 2 - On-chain intelligence (Dune MCP)
- **Inputs:** Dune Analytics queries served through the Dune **MCP** server.
- **Signals:** stablecoin net flows, DEX volume regime, perp open interest delta, whale wallet rotations, CCTP bridge volume.
- **Output:** `l2_score` in `[0, 1]`.
- **Code:** `src/core/level2.py`.

### Level 3 - Gemini 2.5 Flash (final arbiter)
- **Role:** the *arbiter*, not just another signal. Gemini sees the structured outputs of L1 and L2 plus a compact market briefing, and emits a final regime label + score.
- **Inputs:** `ArbiterBriefing { l1_score, l1_rationale, l2_score, l2_rationale, market_snapshot }`.
- **Contract:** Gemini must return strict JSON: `{"score": float, "regime": str, "rationale": str}`.
- **Output:** `l3_score` in `[0, 1]`, plus a human-readable rationale.
- **Code:** `src/core/level3.py`, client in `src/llm/gemini_client.py`.

### Aggregation

`final_score = w1*l1_score + w2*l2_score + w3*l3_score`

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
| `src/core`       | Decision engine, Level 1-3, score aggregation, scheduler        |
| `src/execution`  | Arc Perp DEX client, order/position management, risk checks     |
| `src/allocation` | Risk-on / risk-off router, USYC rotation, CCTP moves            |
| `src/llm`        | Gemini 2.5 Flash client (Level 3 final arbiter)                 |
| `src/agents`     | Top-level autonomous agent loop                                 |
| `src/utils`      | Config, logging, types, retry, time helpers                     |
| `prompts/`       | LLM prompt templates used by the Level 3 arbiter                |
| `scripts/`       | One-off ops scripts (deploy wallets, simulate, backtest, seed)  |
| `tests/`         | Unit + integration tests                                        |

---

## 6. Operating principles

1. **On-chain by default.** If an action can happen on Arc through Circle primitives, it must.
2. **Deterministic decisions.** Same inputs -> same score -> same action. Gemini is pinned to low temperature and strict JSON output.
3. **Safety over alpha.** Drawdown guard and stale-data guard always win over signals.
4. **Observable.** Every decision logs its inputs, level scores, final score, action and tx hash.
5. **Modular.** Each level is replaceable; the router does not care how a score was computed.
