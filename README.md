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

### Day 3 — Planned

Concrete checklist for the next session (so a fresh chat can pick up
without re-discovering anything):

- **Level 1 — real technical signals** (`src/core/level1.py`)
  - Wire OHLCV ingestion (1m / 5m / 1h candles). Source TBD: public Arc
    perp API if exposed, else fall back to a CEX proxy (Binance perp
    BTC/ETH) for the demo.
  - Implement EMA stack (e.g. 21 / 55 / 200), RSI(14), MACD, ATR(14),
    realised vol. Return a `LevelScore` in `[0, 1]` with a short rationale.

- **Level 2 — Dune MCP client** (`src/core/level2.py`)
  - Talk to the Dune MCP server using `DUNE_API_KEY`.
  - Queries: stablecoin net flows on Arc, perp open-interest delta, DEX
    volume regime, whale-wallet rotations, CCTP bridge volume.
  - Aggregate into a `LevelScore` with structured rationale lines.

- **Level 3 — real Gemini 2.5 Flash arbitration** (`src/core/level3.py`
  + `src/llm/gemini_client.py`)
  - Pin temperature low, force strict JSON: `{"score": float, "regime":
    str, "rationale": str}`.
  - Feed `ArbiterBriefing { l1_score, l1_rationale, l2_score, l2_rationale,
    market_snapshot }`.

- **Trading wire-up** (`src/execution/arc_perp_executor.py`)
  - Set `ARC_PERP_MATCHER_URL` in `.env` once published in
    `#agora-hackers`.
  - Sign EIP-712 `OrderTypes.Order` with `eth-account`, POST signed
    orders to the matcher. `open_position` / `close_position` then go
    fully live.
  - Decode `PositionLedger.getPosition` return tuple into the `Position`
    dataclass (we already read it; only the decoder is missing).

- **Risk-off leg — USYC rotation** (`src/allocation/allocation_router.py`)
  - On `risk_off`, withdraw USDC margin from the perp vault and route
    into USYC (mint via Circle's USYC token contracts, addresses already
    in `.env`).
  - Reverse path on the next `risk_on`.

- **Observability**
  - Persist every `DecisionResult` + `ExecutionPlan` to a local JSONL
    log so we can replay the agent's day.
  - Optional: small CLI summary (`scripts/show_last_decisions.py`).

**Definition of done for Day 3:** the agent runs end-to-end on real
signals, opens / closes a real perp position on Arc Testnet on `--live`,
and can rotate into USYC on a risk-off flip.

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
