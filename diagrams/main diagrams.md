=== CapitalArc - Autonomous AI Agent on Arc ===
- - - - - - - - - - - - - - - - - - - - - - - -
  Smart capital management on Perp DEX & Arc


1. Full System Architecture 
2. How the Agent Thinks and Can Change Its Mind 
3. One Full Trading Cycle 
4. Execution & Treasury Flow
5. Position Manager 
7–8. Decision Engine Cascade + L3 override  


                                   One Full Trading Cycle
               (From --loop start to execution and feedback to the next cycle)

                                   [ START OF NEW CYCLE ]
                             main.py --live --loop  (every ~120 seconds)
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
                    ├─► RISK_ON (≥ 0.60 + dir ≠ 0)
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
                    ├─► RISK_ON → open_position + TP/SL
                    ├─► RISK_OFF → close_all + withdraw + USYC mint
                    └─► HOLD → no transaction
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











                      How the Agent Thinks and Can Change Its Mind
                       (Self-Correction & Decision Revision Loop)

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










                        How the Agent Thinks and Can Change Its Mind
                         (Self-Correction & Decision Revision Loop)
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
                    │   direction = sign(market_bias)
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









                                  Full Capital Flow Cycle
                     (USDC → Trading → Yield Rotation → Feedback Loop)

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
   Position open + venue TP/SL   (USDC → USYC on Arc Testnet)
          │                          │
          │                          ▼
          └──────────────────────────┘
                                     ▼
                       **ArcOnchainReader ★ ARC**
                       (equity_usd, drawdown_pct, USDC/USYC balance)
                                     │
                                     ▼
                       Next cycle starts (L1 uses fresh drawdown)
                                     │
                                     └─► feedback loop → Decision Engine











                                   Execution & Treasury Flow
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
   **USYC Check** (USYC_ENABLED?)   **HyperliquidExecutor.close_all_positions()**
          │                                (market order, reduce-only, decision_id)
          ├─► Yes + margin needed         │
          │     **USYCExecutor.redeem() ★ ARC**   **Withdraw margin** ─► freed USDC
          │     (USYC → USDC on Arc Testnet)      to Arc wallet address
          │     wait_for_tx (CircleWallet)         │
          └─► No / disabled                        ▼
                   │                       **USYC rotation?** (USYC_ENABLED?)
                   │                               │
                   ▼                               ├─► Yes
          **HyperliquidExecutor.open_position()**  │
          │ • symbol: BTC-PERP / ETH-PERP          ▼
          │ • side = long/short                    **USYCExecutor.mint() ★ ARC**
          │ • size_usd / markPx → units            (USDC → USYC on Arc Testnet)
          │ • leverage ≤ 5×                        to_mint = freed − USYC_USDC_RESERVE
          │ • EIP-712 signing (phantom-agent)      bounded MIN/MAX amount
          │ • POST /exchange (testnet)             stamped with decision_id
          │ • dry_run=True by default              │
          ▼                                        ▼
    **Venue-side TP/SL**                    **ExecutionPlan** (logged)
    (TP: entry ± TP_ATR_MULT×ATR)           decision_id, action, size, tx_hash, extra
    (SL: entry ± SL_ATR_MULT×ATR)           rotation_amount, reserve, skipped
    reduce-only limit + stop-market
                   │
                   ▼
          **ArcOnchainReader ★ ARC** ← feedback (equity, drawdown, USDC/USYC balance)









                               === Full System Architecture ===

                                   
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
               → can **change previous decision**
                  (HOLD-rescue, override Fast Path, etc.)
        │
        ▼
Final Action → Allocation Router





    === How the Agent Thinks and Can Change Its Mind (L1 → L2 → L3 → Final Decision)===

                                   [ Market Context ]
                             (OHLCV + account state + L2 payload)
                                           │
                                           ▼
   **Level 1** (level1.py)
                    │
                    ├─► **HARD BLOCK** (drawdown_breach ≥ MAX_DRAWDOWN_PCT=10% or ohlcv_unavailable)
                    │      → SHORT-CIRCUIT → final_conviction=0, regime=risk_off (L3 cannot override)
                    └─► **SOFT BLOCK** (trend_mixed, RSI extremes, ATR bounds)
                           → reasons[] + marginality
                                           │
                                           ▼
   **Level 2** (level2.py)
                    │ • market heat [0..1]
                    │ • bias_strength
                    │ • conviction = max(2·|heat−0.5|, bias_strength)
                    │ • direction = sign(market_bias)
                                           │
                                           ▼
   **Level 3 — Claude Sonnet 4.6** (level3.py)
                    │ Receives full briefing:
                    │ • L1 scores + soft blocks + reasons + marginality
                    │ • Complete L2 payload (funding, OI deltas, whales, heat, etc.)
                    │ • Market context
                    │
                    ├─► If **L3 conviction ≥ L3_OVERRIDE_MIN_CONVICTION (0.55)** and ALLOW_L3_TO_OVERRIDE_L1=true
                    │      → OVERRIDE L1/L2
                    │      → applies stacked-veto haircut:
                    │         1 soft block → ×1.00
                    │         2 soft blocks → ×0.70
                    │         3 soft blocks → ×0.50
                    │         4+ soft blocks → ×0.35
                    └─► Otherwise → weighted aggregation (L1 25% + L2 35% + L3 40%)
                                           │
                                           ▼
   **Decision Engine** (decision_engine.py)
                    │ final_conviction + direction_score
                    │
                    ├─► ≥ 0.60 + direction ≠ 0 → **RISK_ON**
                    ├─► ≤ 0.40                → **RISK_OFF**
                    ├─► 0.40 < conv < 0.60 + |dir_strength| ≥ 0.60 → **STRONG-DIR override**
                    └─► mid-band + |dir_strength| < 0.60 → **HOLD**





                    === Position Manager — Fast Path + Smart Path ===

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
                    └─► Yes → **Fast Path** (always runs, ~milliseconds)
                           │
                           ① **daily_dd_guard** (session loss ≥ DAILY_LOSS_LIMIT_PCT → CLOSE ALL)
                           ② **stop_loss** (dynamic ATR or % fallback, breakeven_arm)
                           ③ **vol_spike_close** (live_ATR / entry_ATR ≥ VOL_SPIKE_MULT)
                           ④ **time_exit** (age ≥ MAX_POSITION_HOLD_HOURS)
                           ⑤ **take_profit** (dynamic ATR or % fallback)
                           ⑥ **partial_take_profit** (PARTIAL_TP_FRACTION, once per lifetime)
                           ⑦ **trailing_stop** (peak PnL give-back > TRAIL_ATR_MULT)
                           ⑧ **side_flip** (AUTO_FLIP_ON_SIDE_CHANGE + L3 flip)
                           ⑨ **re_evaluation** (conviction < MIN_CONVICTION_TO_HOLD + modest profit)
                           │
                           ▼
                    If nothing triggers → HOLD justification taxonomy
                                           │
                                           ▼
                    **Smart Path** (only if at least one gate fires)
                           │
                           **Gates (OR)**:
                           • price ≥ 1.5×ATR (L3_REVIEW_TRIGGER_PRICE_ATR_MULT)
                           • |funding rate| ≥ threshold
                           • n_whales ≥ L3_REVIEW_TRIGGER_WHALE_COUNT
                           • |OI δ1h %| ≥ threshold
                           • periodic refresh (L3_REVIEW_MAX_INTERVAL_MINUTES)
                           • **HOLD-rescue** (Fast Path=HOLD + L2 conv ≥ 0.65)
                           • **HOLD-must-be-earned** (age ≥ 45 min)
                           │
                           ▼
                    **Cost-safety rails** (non-overridable):
                           • Global LLM budget (2/cycle, 8/hour)
                           • Multi-trigger (≥2 gates in conservative)
                           • Hard cooldown floor = 25 min/position
                           • First-review delay = 10 min for new positions
                           • Aggression cooldown shift
                           │
                           ▼
                    **Claude Sonnet 4.6** → SmartPathVerdict
                           │
                           ▼
                    **Veto-only mode** (in conservative) → HOLD / CLOSE_FULL / CLOSE_PARTIAL / TIGHTEN_STOP
                           │
                           ▼
                    Allocation Router → final action (open / close / hold)














CapitalArc — Architecture & Decision Flow Diagrams
8 key diagrams showing the complete system architecture and how the agent thinks, decides and can change its mind.
1. Full System Architecture Overview
text[ LAYER 0 — DATA SOURCES ]
        │
        ├─► DuneMCPClient (src/data/dune_mcp.py)
        ├─► HyperliquidIntelligenceAdapter (src/data/hyperliquid_intelligence.py)
        └─► ArcOnchainReader ★ ARC (src/data/arc_onchain.py)
        │
        ▼
[ LAYER 1 — DECISION ENGINE ] src/core/
        │
        ├─► Level 1 — Technical Hard Rules (level1.py)
        ├─► Level 2 — On-chain Intelligence (level2.py)
        ├─► Level 3 — Claude Sonnet 4.6 (level3.py)
        └─► Decision Engine + Aggregator (decision_engine.py)
        │
        ▼
[ LAYER 2 — POSITION MANAGER ] src/execution/position_manager.py
        │
        ├─► Fast Path (always runs)
        └─► Smart Path (event-triggered)
        │
        ▼
[ LAYER 3 — ALLOCATION ROUTER ] src/allocation/allocation_router.py
        │
        ├─► RISK_ON
        ├─► RISK_OFF
        └─► HOLD
        │
        ▼
[ LAYER 4 — EXECUTION ]
        │
        ├─► HyperliquidExecutor
        ├─► Circle DCW ★ ARC/CIRCLE
        └─► USYCExecutor ★ ARC
        │
        ▼
ArcOnchainReader ★ ARC (feedback loop)
2. How the Agent Thinks and Can Change Its Mind (Self-Correction Loop)
text[ Market Context ]
(OHLCV 15m+1h + Account State + L2 Payload)
        │
        ▼
[ LEVEL 1 — Technical Hard Rules ] (level1.py)
        │
        ├─► HARD BLOCK triggered? → SHORT-CIRCUIT (L3 cannot override)
        └─► SOFT BLOCK detected? → reasons[] + marginality
        │
        ▼
[ LEVEL 2 — On-chain Intelligence ] (level2.py)
        │
        ├─► market heat + bias_strength
        └─► conviction + direction
        │
        ▼
[ LEVEL 3 — Claude Sonnet 4.6 ] (level3.py)
        │
        ├─► Can OVERRIDE L1/L2? (conv ≥ 0.55)
        │      → YES: Agent changes its mind + stacked-veto haircut
        └─► NO → weighted aggregation
        │
        ▼
[ Decision Engine + Aggregator ] (decision_engine.py)
        │
        ├─► RISK_ON / RISK_OFF / STRONG-DIR / HOLD
        │
        ▼
[ POSITION MANAGER ] (position_manager.py)
        │
        ├─► Fast Path (deterministic)
        └─► Smart Path (Claude + veto-only mode)
        │
        ▼
Final Action → Allocation Router
3. One Full Trading Cycle (from --loop to next cycle)
text[ START OF NEW CYCLE ]
main.py --live --loop
        │
        ▼
1. DATA COLLECTION LAYER
        │
        ▼
2. LEVEL 1 — Technical Hard Rules
        │
        ▼
3. LEVEL 2 — On-chain Intelligence
        │
        ▼
4. LEVEL 3 — Claude Sonnet 4.6
        │
        ▼
5. DECISION ENGINE + AGGREGATOR
        │
        ▼
6. POSITION MANAGER (Fast Path + Smart Path)
        │
        ▼
7. ALLOCATION ROUTER
        │
        ▼
8. EXECUTION LAYER (Hyperliquid + Circle + USYC)
        │
        ▼
9. LOGGING + FEEDBACK (ExecutionPlan + ArcOnchainReader)
        │
        ▼
LOOP BACK → Next cycle with fresh data
4. Execution & Treasury Flow
text[ AllocationRouter receives DecisionResult ]
        │
        ▼
action? (risk_on / risk_off / hold / deny)
        │
        ├─► RISK_ON
        │      ├─► USYC redeem (if needed) ★ ARC
        │      └─► Hyperliquid open_position() + TP/SL
        │
        ├─► RISK_OFF
        │      ├─► Hyperliquid close_all()
        │      ├─► Withdraw margin
        │      └─► USYC mint (if enabled) ★ ARC
        │
        └─► HOLD → No transaction
        │
        ▼
ArcOnchainReader ★ ARC (feedback loop)
5. Position Manager — Fast Path + Smart Path
text[ New Cycle ]
        │
        ▼
Any open positions?
        │
        ├─► No → Allocation Router
        └─► Yes → Fast Path (always runs)
               │
               ① daily_dd_guard   ② stop_loss   ③ vol_spike
               ④ time_exit        ⑤ take_profit  ⑥ partial_tp
               ⑦ trailing_stop    ⑧ side_flip    ⑨ re_evaluation
               │
               ▼
        Smart Path (if gate triggered)
               │
               → Claude Sonnet 4.6 + cost rails + veto-only mode
               → can change mind (HOLD-rescue, override Fast Path)
               │
               ▼
Final Action → Allocation Router
6. Full Capital Flow Cycle
text[ USDC on Arc Testnet ]
        │
        ▼
Circle Developer-Controlled Wallet ★ ARC/CIRCLE
        │
        ├─► RISK_ON → USYC redeem → Hyperliquid open
        ├─► RISK_OFF → Hyperliquid close → Withdraw → USYC mint
        └─► HOLD → No transaction
        │
        ▼
ArcOnchainReader ★ ARC (equity, drawdown, balances)
        │
        ▼
Next cycle (feedback to L1)
7. Decision Engine Cascade (L1 → L2 → L3 → Final Decision)
textMarket Context
        │
        ▼
Level 1 → HARD / SOFT blocks
        │
        ▼
Level 2 → conviction + direction
        │
        ▼
Level 3 (Claude) → can OVERRIDE (conv ≥ 0.55)
        │
        ▼
Decision Engine → final_conviction + direction_score
        │
        ▼
RISK_ON / RISK_OFF / STRONG-DIR / HOLD
8. L3 Override & Self-Correction Mechanism
textL1 + L2 give their verdict
        │
        ▼
L3 receives full briefing
        │
        ├─► L3 conviction ≥ 0.55 ? → OVERRIDE L1/L2
        │      → stacked-veto haircut applied
        └─► Otherwise → weighted average
        │
        ▼
Agent can change its mind here
