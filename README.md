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
│   ├── execution/    # ArcPerpExecutor + CircleWallet (DCW + Paymaster)
│   ├── allocation/   # AllocationRouter: directive -> on-chain action
│   ├── llm/          # Gemini 2.5 Flash client (final arbiter)
│   ├── agents/       # Reserved for top-level orchestration helpers
│   └── utils/        # Settings (pydantic), logging (loguru)
├── prompts/          # LLM prompt templates for the Level 3 arbiter
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
- **Dune MCP** — on-chain analytics, flows, dashboards (Level 2)
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

### Day 2 — In Progress

Implementing the on-chain execution layer so the agent can actually trade:

- **`ArcPerpExecutor`** (`src/execution/arc_perp_executor.py`) — `open_position`, `close_position`, `get_position`, `get_pnl`, `get_margin`, `get_account_info`.
- **`CircleWallet`** (`src/execution/circle_wallet.py`) — async client over Circle Developer-Controlled Wallets REST API, with **Circle Paymaster** hook for gasless transactions.
- **`AllocationRouter`** (`src/allocation/allocation_router.py`) — turns a `DecisionResult` into perp open/close (USYC rotation lands on Day 3).
- **`DecisionEngine.decide()`** now returns a concrete `ExecutionDirective` (action, side, intensity, target size/leverage), not just a score.
- **`main.py`** — full pipeline entry point with `--dry-run` (default) / `--live` and `--loop` modes; every level + plan + tx is logged through `loguru`.
- Typed configuration via `src/utils/config.py` (`pydantic-settings`).

**Default mode is `--dry-run`:** the pipeline runs end-to-end and logs the exact Circle DCW `contractExecution` payload it would have submitted (including Paymaster hints), without touching the chain. Flip to `--live` only once the Arc Perp router address, Circle `entitySecret` encryption and entity wallet are filled in.

**Next (Day 3):** real Level 1 indicators on live OHLCV, Dune MCP client for Level 2, first real Gemini arbitration call returning strict JSON, and USYC rotation in the allocation router.

---

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
