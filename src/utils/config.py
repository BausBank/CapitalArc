"""Typed configuration loader for CapitalArc.

Loads values from environment (`.env` via `python-dotenv` in `main.py`)
into a strongly-typed Pydantic model. Every other module imports
`Settings` instead of poking at `os.environ` directly.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from typing import Any, Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Strongly-typed application settings."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
    )

    # ---------- General ----------
    APP_ENV: str = "dev"
    LOG_LEVEL: str = "INFO"
    # Wall-clock interval between consecutive **full** decision cycles
    # in ``--loop`` mode. The full cycle runs the entire L1 + L2 + L3
    # pipeline (Dune + OpenRouter), the AllocationRouter and the
    # PositionManager. Default 600 s (10 minutes) is tuned for live
    # trading: it bounds OpenRouter (Sonnet 4.6) and Dune MCP API
    # costs without missing meaningful market moves on the 15m/1h
    # timeframes the L1 cascade uses. Set to 0 to remove the sleep
    # entirely (back-to-back cycles - useful for offline replay).
    DECISION_INTERVAL_SECONDS: int = 600
    # Wall-clock interval between consecutive **fast** cycles in
    # ``--loop`` mode. The fast cycle runs ONLY the PositionManager
    # (Fast Path stewardship - dynamic SL/TP, trailing stop, time exit,
    # breakeven, daily-DD, vol spike); it does NOT touch L1/L2/L3,
    # Dune or OpenRouter. Native TP/SL trigger orders submitted on
    # the venue at open time are also untouched - the fast cycle only
    # issues ``close_position`` / ``partial_close`` when the off-chain
    # rules fire. Default 120 s (2 minutes) gives a 5x oversampling vs
    # the full cycle, so adverse moves are caught minutes before the
    # next full re-arbitration. Set to 0 to disable the fast cycle
    # entirely (single-speed loop on ``DECISION_INTERVAL_SECONDS``).
    DECISION_FAST_INTERVAL_SECONDS: int = 120
    # When True, Level 2 caches Dune / Binance / on-chain results for
    # DEMO_CACHE_TTL_SECONDS so a demo run is fast and idempotent.
    DEMO_MODE: bool = True
    DEMO_CACHE_TTL_SECONDS: int = 1800  # 30 minutes

    # ---------- Market data (Level 1 OHLCV - sourced via Dune MCP) ----------
    # Day-3+: Level 1 OHLCV comes from the Dune `ohlcv` saved query
    # (see dune/queries/ohlcv.sql + DUNE_QUERY_OHLCV_ID below). No
    # Arc RPC scrape, no CEX feed - the agent's analytical stack is
    # single-sourced from Dune MCP.
    OHLCV_LOOKBACK_HOURS: int = 48

    # ---------- Arc Chain ----------
    ARC_RPC_URL: str = "https://rpc.testnet.arc.network"
    ARC_CHAIN_ID: int | None = None
    ARC_EXPLORER_URL: str | None = None

    # ---------- Hyperliquid Testnet (PRIMARY TRADING VENUE) ----------
    # Real trading happens here. Arc + Circle stay on the treasury /
    # yield leg (USYC, CCTP, Paymaster). The split is:
    #   - Risk-off : capital sits in USYC on Arc (yields interest).
    #   - Risk-on  : CCTP moves USDC Arc -> Arbitrum -> Hyperliquid
    #                bridge, then HyperliquidExecutor opens the perp.
    # Funded via the Hyperliquid testnet faucet at
    # https://app.hyperliquid-testnet.xyz - no real capital at risk.
    #
    # ⚠ Execution-vs-data URL split (Day 5+):
    # We deliberately separate the two endpoints. ``HYPERLIQUID_API_URL``
    # is the **execution** URL - every order signed by the SDK and
    # every account-specific read (margin, positions, fills for OUR
    # wallet) goes here. It MUST point at the venue where our funds
    # live (testnet for safety today; mainnet only after manual
    # roll-out approval). ``HYPERLIQUID_DATA_API_URL`` below is the
    # **market-data** URL and SHOULD stay on mainnet so any Level-2
    # analytics that reads market-wide signals (real perp OI / funding
    # / L-S from Hyperliquid's Info API) sees honest production data,
    # not the thin testnet tape.
    HYPERLIQUID_API_URL: str = "https://api.hyperliquid-testnet.xyz"
    # Market-data Info API root. ALWAYS mainnet by default. Used (or
    # reserved for) any caller that reads market-wide intelligence
    # rather than our own account state - e.g. a future Level-2
    # adapter that calls Hyperliquid's ``meta`` /
    # ``metaAndAssetCtxs`` endpoints for real perp OI / funding /
    # asset-context. Keeping it pinned to mainnet decouples
    # "where we trade" from "where we read the market", so that
    # flipping ``HYPERLIQUID_API_URL`` to testnet for safe live-test
    # runs never silently degrades L2 to the testnet's lower-liquidity
    # tape.
    #
    # As of Day 6, the :class:`HyperliquidIntelligenceAdapter` reads
    # from this URL to overlay real perp ``funding`` /
    # ``open_interest`` / ``volume`` / ``cum_funding`` on top of the
    # Dune-derived proxies in Level 2 (see
    # ``HYPERLIQUID_INTELLIGENCE_ENABLED`` below for the kill switch).
    HYPERLIQUID_DATA_API_URL: str = "https://api.hyperliquid.xyz"
    # Master switch for the HyperliquidIntelligenceAdapter overlay.
    # When True (default), Level 2 replaces its spot-DEX-derived perp
    # proxies (funding / OI / volume / cum_funding) with real
    # Hyperliquid Info-API readings from ``HYPERLIQUID_DATA_API_URL``
    # (mainnet). When False, Level 2 stays on the pure-Dune path -
    # useful for A/B comparison or when debugging the HL integration.
    # The overlay is per-symbol and per-metric: any failure (whole
    # HL outage, single coin missing) transparently falls back to
    # the Dune proxy for the affected slot, so flipping this OFF is
    # only ever a manual operator decision, not an automatic fail.
    HYPERLIQUID_INTELLIGENCE_ENABLED: bool = True
    # EVM private key the agent signs Hyperliquid orders with. For
    # testnet, generate a fresh key and faucet it on the testnet
    # bridge. NEVER commit a real key - keep it in a per-host .env
    # outside source control.
    HYPERLIQUID_PRIVATE_KEY: str | None = None
    # Master account address being traded. Leave empty to default to
    # the signer's own address (typical single-wallet setup). Set
    # only when running the signer as an API-wallet sub-key allowed
    # to trade on behalf of a different master account.
    HYPERLIQUID_ACCOUNT_ADDRESS: str | None = None
    # Optional Hyperliquid vault address (multi-strat or PnL-shared
    # vault). Leave empty for the single-signer case.
    HYPERLIQUID_VAULT_ADDRESS: str | None = None
    # Hard caps applied BEFORE the SDK call regardless of router
    # output. Belt-and-braces vs runaway sizing bugs.
    HYPERLIQUID_MAX_LEVERAGE: int = 5
    HYPERLIQUID_MAX_POSITION_USD: float = 10000.0
    HYPERLIQUID_DEFAULT_SLIPPAGE_BPS: int = 50
    # Take-profit / stop-loss thresholds applied by PositionManager
    # when the cycle directive doesn't carry per-trade overrides.
    HYPERLIQUID_DEFAULT_TAKE_PROFIT_PCT: float = 0.02   # +2%
    HYPERLIQUID_DEFAULT_STOP_LOSS_PCT: float = 0.015    # -1.5%
    # When True, ``open_position`` uses the SDK's ``market_open``
    # helper (immediate fill at best book price). When False, the
    # executor posts a marketable limit at mid +/- slippage.
    HYPERLIQUID_USE_MARKET_ORDERS: bool = True

    # ---------- Arc Perp DEX (DEPRECATED - kept for treasury reads) ----------
    # ClearingHouse - entry contract for batch settlement (settleBatch).
    ARC_PERP_ROUTER_ADDRESS: str | None = None
    # USDCCollateralVault - holds USDC margin (deposit / withdraw).
    ARC_PERP_VAULT_ADDRESS: str | None = None
    # MarketRegistry - market id <-> spec mapping (getMarket).
    ARC_PERP_MARKET_REGISTRY_ADDRESS: str | None = None
    # PositionLedger - per-(accountId, marketId) position state (getPosition).
    ARC_PERP_POSITION_LEDGER_ADDRESS: str | None = None
    # Off-chain matching engine endpoint for EIP-712 OrderTypes.Order POSTs.
    # DEPRECATED: Arc Perp DEX is no longer the trading venue (matcher spec
    # was never published). Trading has migrated to Hyperliquid Testnet -
    # see HYPERLIQUID_* settings below. Kept only so legacy treasury code
    # still compiles.
    ARC_PERP_MATCHER_URL: str | None = None
    ARC_PERP_MAX_LEVERAGE: int = 3
    # CapitalArc trades two perps: BTC-PERP and ETH-PERP. SOL-PERP was
    # part of the original symbol set but was removed because Wormhole-
    # wrapped SOL has near-zero deep-liquidity DEX volume on Ethereum
    # mainnet (the chain Dune indexes most completely), which made the
    # SOL leg honestly unusable rather than fake-zero. Re-add only if a
    # future chain switch (`DUNE_CHAIN=base`/`arbitrum`) brings enough
    # SOL liquidity AND the SQL templates here are re-derived.
    ARC_PERP_SYMBOLS: str = "BTC-PERP,ETH-PERP"

    # ---------- Circle DCW ----------
    CIRCLE_API_KEY: str | None = None
    CIRCLE_ENTITY_SECRET: str | None = None
    CIRCLE_WALLET_SET_ID: str | None = None
    CIRCLE_AGENT_WALLET_ID: str | None = None
    CIRCLE_API_BASE_URL: str = "https://api.circle.com/v1/w3s"

    # ---------- Circle CCTP ----------
    CCTP_SOURCE_DOMAIN: int | None = None
    CCTP_DESTINATION_DOMAIN: int | None = None
    CCTP_TOKEN_MESSENGER_ADDRESS: str | None = None
    CCTP_MESSAGE_TRANSMITTER_ADDRESS: str | None = None

    # ---------- Circle Paymaster ----------
    CIRCLE_PAYMASTER_URL: str | None = None
    CIRCLE_PAYMASTER_POLICY_ID: str | None = None

    # ---------- USYC (yield-bearing risk-off leg) ----------
    # When the engine flips to ``risk_off`` (low conviction / neutral
    # direction) the AllocationRouter closes the perp position, pulls
    # margin out of the Arc Perp vault and rotates the freed USDC into
    # USYC so the capital keeps earning ~T-bill yield while we wait
    # for the next risk-on setup. When the engine flips back to
    # ``risk_on`` we redeem just enough USYC to fund the new perp
    # entry. Leaving any of the three address fields below blank turns
    # the rotation leg into a graceful no-op - the router will still
    # close perps but will skip the USYC mint / redeem with an
    # explanatory note in the ExecutionPlan (no hard failure).
    USYC_TOKEN_ADDRESS: str | None = None
    USYC_MINT_CONTRACT_ADDRESS: str | None = None
    # Smallest single USDC -> USYC rotation we will submit. Below this
    # we skip the rotation to avoid spamming the chain (and Circle's
    # rate limits) with sub-economic dust transactions.
    USYC_MIN_ROTATION_AMOUNT: float = 100.0
    # Safety net: hard cap on any single USYC mint or redeem, in USD.
    # A runaway decision-engine loop is therefore always financially
    # bounded on testnet.
    USYC_MAX_ROTATION_AMOUNT: float = 100000.0
    # Free USDC reserve to keep in the agent wallet at all times.
    # The risk-off path will mint USYC only with `free_usdc - reserve`,
    # so non-sponsored gas fallbacks and the next perp entry's
    # initial-margin call never hit a "wallet is empty" state.
    USYC_USDC_RESERVE_USD: float = 50.0
    # When False, the AllocationRouter skips the entire USYC leg even
    # if the contracts are configured. Use this to demo a perp-only
    # build of the agent without re-blanking USYC addresses in .env.
    USYC_ENABLED: bool = True
    # When True, a risk-off directive (Daily-DD kill-switch, RISK_OFF
    # action, etc.) will close every open perp AND withdraw all
    # remaining USDC margin from Hyperliquid back to the signer's
    # Arbitrum address. When False (default for safety), risk-off
    # only CLOSES positions and leaves the freed USDC sitting in the
    # perp sub-account so the agent can re-enter on the next
    # risk-on cycle without a 1-block bridge round-trip - and, more
    # importantly, so that a buggy PnL/equity read can never
    # auto-drain the testnet vault.  The USYC mint leg is also
    # skipped when withdraws are disabled (there's nothing freed on
    # Arc to mint with). Re-enable explicitly with
    # ``RISK_OFF_WITHDRAW=true`` once the full risk-off-to-treasury
    # path has been validated end-to-end.
    RISK_OFF_WITHDRAW: bool = False
    # Optional ABI overrides if the deployed USYC on Arc uses
    # non-standard mint / redeem function names.
    USYC_MINT_SIGNATURE: str = "mint(uint256)"
    USYC_REDEEM_SIGNATURE: str = "redeem(uint256)"

    # ---------- USDC ----------
    USDC_TOKEN_ADDRESS: str | None = None

    # ---------- Dune MCP (single source of truth for L1 + L2) ----------
    # Day-3 (post-Arc): the Dune Analytics catalogue does not yet
    # index Arc Testnet, so all market signals are sourced from a
    # live, high-liquidity EVM chain (Ethereum mainnet by default,
    # Base / Arbitrum trivially switchable). The agent's symbols
    # (BTC-PERP / ETH-PERP) map to the canonical on-chain
    # representations of those assets (WBTC / WETH on Ethereum;
    # cbBTC / WETH on Base). Spot DEX trades (`dex.trades`) feed
    # both Level 1 OHLCV and the Level 2 metrics; ERC-20 `Transfer`
    # events (`erc20_<chain>.evt_Transfer`) drive the whale-activity
    # and vault-flow queries.
    DUNE_API_KEY: str | None = None
    DUNE_MCP_URL: str = "https://mcp.dune.com/sse"
    # Dune REST endpoint used to actually execute queries (the MCP SSE
    # endpoint is the LLM-facing tool surface; the REST endpoint is the
    # one we hit programmatically with the same DUNE_API_KEY).
    DUNE_API_BASE_URL: str = "https://api.dune.com/api/v1"
    DUNE_CACHE_TTL_SECONDS: int = 300
    # Active chain that Level 1 / Level 2 read from. Must match the
    # value of the `blockchain` column on Dune's `dex.trades` (and the
    # `erc20_<chain>` schema name). Supported out of the box:
    # `ethereum`, `base`, `arbitrum`. Pin one and the agent uses it
    # for *every* signal - SQL templates are chain-agnostic.
    DUNE_CHAIN: str = "ethereum"
    DUNE_LOOKBACK_HOURS: int = 24
    # ---- Dune REST throttling (free-tier safety net) ----
    # L2 fans out 8 metric calls + L1 fans out 1 OHLCV call in
    # parallel; without throttling the burst hits HTTP 429 on free
    # plans and metrics render as `error`. The semaphore caps the
    # concurrent in-flight requests and 429s are retried with
    # exponential backoff. Defaults are tuned for Dune's free tier
    # (very strict ~10 reqs/min cumulative across execute + poll);
    # bump CONCURRENT_REQUESTS to 6-8 if you have a paid plan.
    DUNE_MAX_CONCURRENT_REQUESTS: int = 2
    DUNE_RATE_LIMIT_MAX_RETRIES: int = 6
    DUNE_RATE_LIMIT_BACKOFF_SECONDS: float = 2.5

    # ---------- Per-symbol token addresses (resolved against DUNE_CHAIN) ----------
    # If left blank, defaults from `_DEFAULT_TOKEN_ADDRESSES` are used.
    # Override via .env when you point at a chain we haven't pre-mapped
    # or want to track a different wrapping (e.g. cbBTC vs WBTC on Base).
    DUNE_TOKEN_BTC_ADDRESS: str | None = None
    DUNE_TOKEN_ETH_ADDRESS: str | None = None
    DUNE_TOKEN_USDC_ADDRESS: str | None = None
    # Address watched by `vault_flows.sql` for USDC deposits /
    # withdrawals. Defaults to a placeholder of the Arc Perp DEX
    # collateral vault; users can repoint to any address they want
    # to monitor (e.g. an Aave / GMX vault on the chosen chain).
    DUNE_PERP_VAULT_ADDRESS: str | None = None
    # USD threshold above which a single ERC-20 transfer counts as
    # "whale activity" in `whale_activity.sql`.
    DUNE_WHALE_MIN_USD: float = 250_000.0

    # Per-metric saved Dune query ids. Leave any of these unset and
    # the corresponding metric is reported as `n/a` (no fake zeros).
    # Level 1 OHLCV ships first because the entire technical-rule
    # stack depends on it - without this id Level 1 cannot run.
    DUNE_QUERY_OHLCV_ID: int | None = None
    DUNE_QUERY_FUNDING_RATES_ID: int | None = None
    DUNE_QUERY_OPEN_INTEREST_ID: int | None = None
    DUNE_QUERY_VOLUME_ID: int | None = None
    DUNE_QUERY_VAULT_FLOWS_ID: int | None = None
    DUNE_QUERY_WHALE_ACTIVITY_ID: int | None = None
    DUNE_QUERY_LONG_SHORT_RATIO_ID: int | None = None
    DUNE_QUERY_CUM_FUNDING_ID: int | None = None
    DUNE_QUERY_MARKET_SENTIMENT_ID: int | None = None

    # ---------- Level 1 thresholds (technical hard rules) ----------
    # NOTE (Day 5 testing): the ATR band and TF-agreement defaults
    # below are intentionally **loosened** so the HyperliquidExecutor /
    # PositionManager / AllocationRouter pipeline can be exercised
    # against live signals without L1 short-circuiting every cycle on
    # currently-quiet markets. These are **testing defaults**; tighten
    # back to the conservative values (ATR_PCT_MIN=0.15,
    # ATR_PCT_MAX=6.0, REQUIRE_TF_AGREEMENT=true) before going to
    # mainnet. The same loosened defaults are mirrored in
    # ``.env.example`` and ``.env`` with a clearly labelled comment.
    L1_RSI_OVERBOUGHT: float = 70.0
    L1_RSI_OVERSOLD: float = 30.0
    L1_ATR_PCT_MIN: float = 0.05      # was 0.15 (TESTING ONLY)
    L1_ATR_PCT_MAX: float = 12.0      # was 6.0  (TESTING ONLY)
    L1_EMA_FAST: int = 9
    L1_EMA_SLOW: int = 21
    L1_RSI_PERIOD: int = 14
    L1_ATR_PERIOD: int = 14
    L1_TIMEFRAMES: str = "15m,1h"
    L1_KLINES_LIMIT: int = 150
    L1_REQUIRE_TF_AGREEMENT: bool = False  # was True (TESTING ONLY)
    # ---------- Stage 5: flat-market (chop) detector ----------
    # When True, an all-timeframes-flat tape whose mean EMA9/EMA21
    # separation is <= L1_FLAT_EMA_SEP_ATR_MAX (in ATR units) raises a
    # SOFT ``flat_market`` block instead of the generic ``trend_mixed``.
    # Refuses chop cleanly (L3 may still override on a decisive on-chain
    # thrust). Off by default -> no behaviour change until enabled.
    L1_FLAT_MARKET_DETECT: bool = False
    L1_FLAT_EMA_SEP_ATR_MAX: float = 0.15

    # ---------- Stage 7: Level 2 bull-bias offset ----------
    # Subtracted from the accumulated bull_votes in Level 2's market-
    # bias voter before the bull/bear margin is computed, correcting a
    # structural LONG lean (positive funding proxy + upward drift in
    # trending-up regimes). 0.0 = no-op (default); ~0.5-1.0 trims a
    # mild persistent long bias seen in live logs.
    L2_BULL_BIAS_OFFSET: float = 0.0

    # ---------- OpenRouter (Level 3 final arbiter) ----------
    # OpenRouter is the LLM gateway in front of Level 3. Setting
    # OPENROUTER_API_KEY is the ONLY toggle needed to switch L3 from
    # synthetic-placeholder mode to real Claude arbitration. When
    # unset, the engine emits a synthetic L3 whose weight is
    # redistributed back to L1+L2 (see REDISTRIBUTE_SYNTHETIC_L3_WEIGHT).
    #
    # We migrated from Google's Gemini SDK (geo-locked away from
    # multiple user regions with `400 FAILED_PRECONDITION`) to
    # OpenRouter because the latter routes through their own
    # infrastructure and stays accessible globally. The default
    # upstream model is Anthropic's Claude Sonnet 4.6; swap to any
    # other slug listed on https://openrouter.ai/models with a
    # one-line `.env` change.
    OPENROUTER_API_KEY: str | None = None
    OPENROUTER_MODEL: str = "anthropic/claude-sonnet-4.6"
    OPENROUTER_TEMPERATURE: float = 0.2
    # 2048 leaves the critical-mode 5-section rationale room to breathe
    # (Market Context / Key Signals Analysis / Contradictions & Risks /
    # My Independent View / Final Recommendation typically lands at
    # 600-1100 output tokens; 1024 starts truncating it on long signals
    # tables). Standard mode rarely uses more than ~300 tokens, so the
    # extra budget is free in that mode.
    OPENROUTER_MAX_TOKENS: int = 2048
    OPENROUTER_TIMEOUT_SECONDS: int = 60
    # Bounded retry for transient failures (429 rate-limit, 5xx
    # upstream, malformed JSON). Hard failures (auth, model-not-found,
    # policy denial) raise immediately so the operator sees the
    # actionable diagnosis without waiting through backoff windows.
    OPENROUTER_MAX_RETRIES: int = 2
    OPENROUTER_BACKOFF_SECONDS: float = 2.0
    # Optional analytics headers (visible on
    # https://openrouter.ai/activity).
    OPENROUTER_REFERER: str = "https://github.com/capitalarc/capitalarc"
    OPENROUTER_APP_TITLE: str = "CapitalArc"

    # ---------- Level 3 mode (critical | standard) ----------
    # Critical mode (default) makes Claude behave as an *independent*
    # senior risk manager: it is allowed to disagree with L1+L2 when
    # signals are weak or contradictory, and its rationale follows a
    # strict 5-section template (Market Context / Key Signals Analysis /
    # Contradictions & Risks / My Independent View / Final
    # Recommendation) so the demo always shows *how* the decision was
    # reached, not just *what* it is.
    #
    # Standard mode is the original "trader voice" prompt: concise 1-2
    # sentence rationale with short technical tags. Useful for high-
    # frequency runs where the structured rationale is overkill.
    #
    # Each mode resolves a distinct prompt file under `prompts/`:
    #   critical -> prompts/level3_arbiter_critical.md
    #   standard -> prompts/level3_arbiter_standard.md
    L3_MODE: Literal["critical", "standard"] = "critical"

    # ---------- Risk & Allocation ----------
    RISK_ON_THRESHOLD: float = 0.6
    RISK_OFF_THRESHOLD: float = 0.4
    MAX_DRAWDOWN_PCT: float = 10.0

    WEIGHT_LEVEL1: float = Field(default=0.25)
    WEIGHT_LEVEL2: float = Field(default=0.35)
    WEIGHT_LEVEL3: float = Field(default=0.40)

    # ---------- Conviction / direction controls ----------
    # Minimum aggregated *direction strength* (|weighted_direction|) in
    # [0, 1] required for the engine to treat the side as decided. Used
    # both in the risk-off / risk-on classification (turns the
    # `final_direction` from neutral into long/short) and in the mid-
    # band override below.
    SHORT_BIAS_MIN_STRENGTH: float = 0.35
    # Minimum direction strength required to override a mid-band hold
    # and open a *reduced-size* position. Raise to 0.8 to fire almost
    # never; lower to 0.5 for more frequent opens.
    STRONG_BIAS_OPEN_STRENGTH: float = 0.6
    # ---------- STRONG-DIRECTION L1 corroboration (Day-6+) ----------
    # When the mid-band STRONG-DIRECTION override fires, it normally
    # only checks the conviction-weighted direction vote. In ranging
    # markets L1 outputs ``strength=0.25`` (hard-coded for flat/mixed
    # trend) which contributes near-zero to the direction vote -
    # meaning L2 + L3 alone can swing the engine into a LONG even
    # when the technical picture says "I have no opinion". This gate
    # demands that L1's conviction CLEARS this floor before the
    # mid-band override is allowed. Set to 0.0 to disable; 0.40 is
    # the recommended floor (above the 0.25 flat/mixed plateau but
    # below the typical 0.5-1.0 trend-confirmed band).
    STRONG_DIRECTION_L1_CORROBORATION_MIN: float = 0.40

    # ---------- Day-3 Entry-Quality Gate ----------
    # Final entry-quality filter over a would-be risk_on open (runs
    # after the engine builds the directive, on the normal cascade
    # path only - the L3-overrides-L1 path is exempt). All floors
    # default to a NO-OP so the gate is invisible until tuned.
    #   * ENTRY_MIN_CONVICTION_TO_OPEN - aggregated conviction floor to
    #     open (0.0 = off; raise to e.g. 0.6 to refuse sub-threshold
    #     opens - note this would also kill mid-band STRONG-DIRECTION
    #     probes, so keep 0.0 unless that is intended).
    #   * ENTRY_MIN_DIRECTION_STRENGTH_TO_OPEN - directional-strength
    #     floor to open (0.0 = off; the conviction-direction decoupler).
    #   * ENTRY_MIN_LEVEL_AGREEMENT - minimum fraction of the
    #     directional (weight x conviction) mass that must agree with
    #     the final direction (0.0 = off). Below it -> downgrade.
    #   * ENTRY_BLOCK_BELOW_AGREEMENT - hard-block below this agreement
    #     (0.0 = never hard-block on dissent; must be <= the floor).
    #   * ENTRY_DISSENT_INTENSITY_MULT - intensity multiplier applied
    #     on a dissent downgrade (1.0 = no-op; 0.5 = halve a contested
    #     open).
    #   * L3_SC_MIN_AGREEMENT - minimum L3 multi-sample agreement before
    #     a contested open is downgraded (0.0 = off; pairs with the L3
    #     self-consistency feature below).
    ENTRY_QUALITY_ENABLED: bool = True
    ENTRY_MIN_CONVICTION_TO_OPEN: float = 0.0
    ENTRY_MIN_DIRECTION_STRENGTH_TO_OPEN: float = 0.0
    ENTRY_MIN_LEVEL_AGREEMENT: float = 0.0
    ENTRY_BLOCK_BELOW_AGREEMENT: float = 0.0
    ENTRY_DISSENT_INTENSITY_MULT: float = 1.0
    L3_SC_MIN_AGREEMENT: float = 0.0
    # When True (default), a synthetic L3 score (no real Gemini wiring
    # yet) gets its weight redistributed proportionally to L1 + L2 in
    # the aggregation, so the placeholder doesn't silently dilute the
    # real signal back into itself. Flip to False to keep the legacy
    # behaviour where synthetic L3 votes with its configured weight.
    REDISTRIBUTE_SYNTHETIC_L3_WEIGHT: bool = True

    # ---------- Level 3 override of a Level 1 block ----------
    # By default the cascade short-circuits the moment Level 1 blocks
    # (saves API budget, keeps the agent on the safe side). When
    # ``ALLOW_L3_TO_OVERRIDE_L1`` is True AND a real OpenRouter L3 is
    # wired, the engine instead asks Level 3 to inspect the L1 block
    # reasons + per-symbol indicators + the Level 2 on-chain payload,
    # and only short-circuits if L3 *also* declines the trade.
    #
    # This lets Claude critically audit a borderline L1 veto (e.g.
    # ATR sitting on the edge of the band, single-timeframe trend
    # disagreement) instead of having the rule-based gate decide
    # unilaterally. Hard blocks (``drawdown_breach``,
    # ``ohlcv_unavailable``) are NEVER overrideable - drawdown is
    # sacred and missing data means we can't trade safely.
    #
    # ``L3_OVERRIDE_MIN_CONVICTION`` is the floor L3 must clear to
    # actually open the trade; anything below it is treated as a
    # decline-to-override and the engine short-circuits as normal.
    ALLOW_L3_TO_OVERRIDE_L1: bool = True
    L3_OVERRIDE_MIN_CONVICTION: float = 0.55

    # ---------- L3 aggression / calibration (Day 6+) ----------
    # The critical-mode L3 prompt was tuned to be "skeptical by
    # default" to prevent reckless trading on weak signals. In
    # production this proved over-conservative: L3 was holding even
    # when L1 passed AND L2 conviction >= 0.7, citing single
    # contradictions (cross-asset ATR, n=3 whales, flat OI in a
    # continuation) that would not move a real desk's verdict.
    #
    # ``L3_AGGRESSION`` is the operator's master knob:
    #
    #   * ``conservative`` - legacy Day-4 behaviour, conviction and
    #     intensity scaled to 0.90, no HOLD-rescue rule. Use when
    #     drawdown is elevated or you want to debug a noisy market.
    #   * ``balanced`` (DEFAULT) - no calibration multipliers; L3's
    #     verdict goes through as-is. The prompt itself is rewritten
    #     to use a concrete decision matrix instead of vague
    #     skepticism, so "balanced" with the new prompt is roughly
    #     as aggressive as "aggressive" was under the old prompt -
    #     but with auditable rules.
    #   * ``aggressive`` - conviction ×1.10, intensity ×1.15, and
    #     a HOLD-rescue rule: if Claude returns ``regime="hold"``
    #     but L1.passes AND L2.conviction >= L3_HOLD_RESCUE_L2_MIN,
    #     the engine flips it to a low-intensity OPEN aligned with
    #     L2's direction. Use only when you explicitly want the
    #     agent to lean into convergent signals.
    #
    # The calibration is applied in code AFTER ``ArbiterResponse``
    # validation - never in the prompt - so the audit trail is
    # deterministic and reversible.
    L3_AGGRESSION: Literal["conservative", "balanced", "aggressive"] = (
        "balanced"
    )
    # Minimum L2 conviction required to trigger the HOLD-rescue
    # rule under L3_AGGRESSION=aggressive. 0.65 reflects the
    # smoke-test pattern where L2 was firing 0.70-0.80 conviction
    # bullish but L3 was still holding.
    L3_HOLD_RESCUE_L2_MIN: float = 0.65
    # Intensity to use when the HOLD-rescue rule fires. Deliberately
    # small - we're overriding Claude's judgement, so size
    # conservatively.
    L3_HOLD_RESCUE_INTENSITY: float = 0.30

    # ---------- Day-3 L3 multi-sample self-consistency ----------
    # When enabled, the arbiter draws extra LLM samples ONLY when the
    # primary verdict's conviction lands in the borderline band
    # [L3_SC_BORDERLINE_LOW, L3_SC_BORDERLINE_HIGH], then takes the
    # majority direction + median conviction/intensity across samples
    # and records the sample agreement (consumed by L3_SC_MIN_AGREEMENT
    # in the entry-quality gate). Off by default (samples=1) to protect
    # the LLM budget; samples is hard-capped at 3 in code.
    L3_SELF_CONSISTENCY_ENABLED: bool = False
    L3_SELF_CONSISTENCY_SAMPLES: int = 1
    L3_SC_BORDERLINE_LOW: float = 0.50
    L3_SC_BORDERLINE_HIGH: float = 0.65
    # Temperature for the extra samples (the primary call stays at
    # OPENROUTER_TEMPERATURE) so the resamples actually vary.
    L3_SC_TEMPERATURE: float = 0.50

    # ---------- Short-selling controls ----------
    # When True, shorts use the same sizing pipeline as longs
    # (recommended). Flip to False + tune SHORT_SIZE_MULTIPLIER if
    # you want half-size shorts during the ramp-up phase.
    SYMMETRIC_SHORT_SIZING: bool = True
    SHORT_SIZE_MULTIPLIER: float = 1.0

    # ---------- Position sizing (equity-% risk + vol-targeted + DD haircut) ----------
    # Default notional fallback when equity/ATR aren't available
    # (typical first dry-run cycle before any margin is deposited).
    BASE_POSITION_USD: float = 1000.0
    MAX_POSITION_USD: float = 10000.0
    # PRIMARY operator-friendly knob: percentage of CURRENT equity to
    # risk on a single trade, under a `STOP_ATR_MULT * ATR` adverse
    # move. Operator-friendly format (1.0 = 1%, NOT a fraction).
    # When set, this value WINS over the legacy ``TARGET_RISK_PCT``
    # fraction below. Sensible range: 0.5 .. 2.0 (textbook).
    #
    #   * 0.5  - very conservative; ideal for live capital ramp-up
    #   * 1.0  - balanced (DEFAULT)
    #   * 2.0  - aggressive; only with proven edge + small account
    #
    # Combined with ATR%, the resulting notional is:
    #
    #   notional = equity * (risk_pct/100)
    #              / (stop_atr_mult * atr_pct/100)
    #
    # i.e. equity * risk_pct / (stop_atr_mult * atr_pct). All four
    # inputs are exposed so operators can dial conservatively without
    # touching the L3 prompt or the engine weights.
    RISK_PER_TRADE_PCT: float | None = 1.0
    # LEGACY fraction-form knob; only consulted when
    # ``RISK_PER_TRADE_PCT`` is None. 0.02 = 2% (textbook default).
    # Kept for backward compatibility with existing .env files.
    TARGET_RISK_PCT: float = 0.02
    # ---- Per-asset risk multipliers ----
    # Multiplier applied to the resolved per-trade risk for each
    # symbol bucket. 1.0 = full risk; 0.5 = half-size on this asset.
    # Lets operators express "I trust the BTC setup more than the
    # ETH setup" or "shrink any ALT to half risk" without touching
    # the engine. Sane bands: 0.25 .. 1.5.
    RISK_PER_TRADE_MULT_BTC: float = 1.0
    RISK_PER_TRADE_MULT_ETH: float = 1.0
    RISK_PER_TRADE_MULT_DEFAULT: float = 1.0
    # Stop distance in ATR multiples (used in the sizing denominator
    # AND by the Fast Path's dynamic stop-loss when
    # USE_DYNAMIC_ATR_TPSL is on - one knob for both consumers).
    STOP_ATR_MULT: float = 1.5
    # ATR% floor used in the sizing denominator to avoid divide-by-zero
    # / absurdly large sizes when ATR collapses to near-zero. Should
    # sit close to L1_ATR_PCT_MIN.
    MIN_ATR_PCT_FOR_SIZING: float = 0.25
    # Exponent of the drawdown haircut curve.
    # `intensity *= max(0, 1 - (dd_pct / max_dd_pct) ** exponent)`.
    # 1.0 = linear haircut; 2.0 = soft early, hard near the cap.
    DD_HAIRCUT_EXPONENT: float = 2.0
    # When True, the router stamps the full sizing breakdown (vol
    # target, intensity, haircut, etc.) onto each ExecutionPlan so the
    # console panel can show the "why" behind each size.
    EXPLAIN_SIZING: bool = True

    @property
    def resolved_risk_per_trade_frac(self) -> float:
        """Return the per-trade risk as a fraction in [0, 1].

        Prefers the operator-friendly ``RISK_PER_TRADE_PCT`` (1.0 =
        1%); falls back to the legacy ``TARGET_RISK_PCT`` fraction
        when not set. Clamped to a sane range so a stray ``50.0``
        in .env doesn't blow up the sizing denominator.
        """
        if self.RISK_PER_TRADE_PCT is not None:
            frac = float(self.RISK_PER_TRADE_PCT) / 100.0
        else:
            frac = float(self.TARGET_RISK_PCT)
        # Clamp: 0.05% (sanity floor) .. 5% (hard ceiling; anything
        # above is almost certainly a unit error).
        return max(0.0005, min(0.05, frac))

    def risk_multiplier_for_symbol(self, symbol: str) -> float:
        """Resolve the per-asset risk multiplier for ``symbol``.

        Pattern-matches by leading token (BTC / ETH) and falls back
        to ``RISK_PER_TRADE_MULT_DEFAULT`` for anything else. Symbol
        is matched case-insensitively against the part before the
        first ``-`` / ``/`` separator so ``BTC-PERP`` / ``BTC/USD``
        / ``btc-perp`` all hit the same bucket.
        """
        if not symbol:
            return float(self.RISK_PER_TRADE_MULT_DEFAULT)
        head = symbol.strip().upper().split("-")[0].split("/")[0]
        if head == "BTC":
            return float(self.RISK_PER_TRADE_MULT_BTC)
        if head == "ETH":
            return float(self.RISK_PER_TRADE_MULT_ETH)
        return float(self.RISK_PER_TRADE_MULT_DEFAULT)

    # ============================================================
    # Position management (PositionManager - two-tier stewardship)
    # ============================================================
    # The PositionManager runs on EVERY cycle, BEFORE the regime
    # dispatch, with TWO independent paths:
    #
    #   * Fast Path  (always runs, no LLM, ~ms): dynamic ATR-based
    #     TP/SL, partial TP, breakeven, trailing stop, vol-spike
    #     filter, time-based exit, portfolio-wide daily-DD guard.
    #     Driven entirely by Level 1 indicators + executor state.
    #
    #   * Smart Path (LLM, ~1s): a per-position Claude review
    #     triggered ONLY by meaningful changes (price moved
    #     N x ATR since last check, funding spike, whale spike,
    #     time-since-last-review elapsed). L3 can override or
    #     veto Fast-Path decisions within bounds.
    #
    # All percent fields are operator-friendly *percentages*
    # (3.0 = 3%); converted to Decimal fractions inside
    # :meth:`PositionManager.from_settings`.

    # ---------- Legacy fixed-pct triggers (fallback when no ATR) ----------
    # The Fast Path prefers DYNAMIC, ATR-based triggers (TP_ATR_MULT
    # below); these fixed-pct values are used as a fall-back when the
    # primary symbol's ATR% is unavailable (e.g. Dune outage). Set
    # any of them to 0 to fully disable the corresponding trigger,
    # or use the ENABLE_* flags below to keep telemetry visible
    # while muting the action.
    # Native venue TP/SL trigger orders are also submitted at open
    # time using these percentages (or the dynamic ATR equivalents,
    # see USE_DYNAMIC_ATR_TPSL). +3.5% / -2.0% is a textbook 1.75:1
    # reward-to-risk envelope - tighten in choppy markets, widen in
    # trends. Set to 0 to mute that side of the trigger.
    TAKE_PROFIT_PCT: float = 3.5           # +3.5% (fallback close + native TP)
    STOP_LOSS_PCT: float = 2.0             # -2.0% (fallback close + native SL)
    TRAILING_STOP_PCT: float = 1.0         # fallback trail width
    # Conviction floor below which a *profitable* position is closed
    # to lock in gains. Deep losers are caught by STOP_LOSS_PCT first.
    MIN_CONVICTION_TO_HOLD: float = 0.45
    # Profit threshold required for the re-evaluation trigger to fire.
    # Prevents flattening a slightly-underwater position just because
    # conviction wobbled briefly.
    RE_EVAL_MIN_PROFIT_PCT: float = 0.5    # 0.5%
    # When True (default), an open position is flipped the moment the
    # engine emits an opposite-side directive (close + reopen on the
    # same cycle).
    AUTO_FLIP_ON_SIDE_CHANGE: bool = True

    # ---------- Dynamic ATR-based TP / SL (preferred path) ----------
    # When the primary symbol's ATR% is available from L1, the Fast
    # Path computes TP/SL as multiples of the LIVE ATR rather than
    # fixed percentages, so the agent breathes with volatility.
    #
    #   sl_price = entry +/- (SL_ATR_MULT      * ATR)   (1.0-1.5)
    #   tp_price = entry +/- (TP_ATR_MULT      * ATR)   (2.5-3.5)
    #
    # Recomputed every cycle, so a position that was sized in a
    # quiet market gracefully widens its stop when vol picks up.
    USE_DYNAMIC_ATR_TPSL: bool = True
    SL_ATR_MULT: float = 1.2                # stop distance = 1.2 * ATR
    TP_ATR_MULT: float = 3.0                # full TP distance = 3.0 * ATR
    # Trailing stop distance in ATR multiples; clamped to a sane
    # minimum so a quiet market doesn't produce a trail tighter
    # than the spread.
    TRAIL_ATR_MULT: float = 1.5

    # ---------- Partial take-profit (scaling out) ----------
    # When a position hits the partial-TP target (PARTIAL_TP_ATR_MULT
    # x ATR in our favour), close PARTIAL_TP_FRACTION of the size
    # and let the remainder ride to the full TP. Disables itself
    # when PARTIAL_TP_FRACTION <= 0 or >= 1.
    ENABLE_PARTIAL_TAKE_PROFIT: bool = True
    PARTIAL_TP_ATR_MULT: float = 1.5        # first scale-out at ~+1.5 ATR
    PARTIAL_TP_FRACTION: float = 0.50       # close 50% of the position

    # ---------- Breakeven move ----------
    # Once profit reaches BREAKEVEN_TRIGGER_ATR_MULT * ATR, advance
    # the effective SL to entry + BREAKEVEN_BUFFER_PCT (so we never
    # give back the trade). Stamped onto state - no on-chain order
    # change; the dynamic stop branch reads it on subsequent cycles.
    ENABLE_BREAKEVEN: bool = True
    BREAKEVEN_TRIGGER_ATR_MULT: float = 1.0   # arm BE at +1 ATR
    BREAKEVEN_BUFFER_PCT: float = 0.05        # lock in 0.05% past entry

    # ---------- Volatility filter (live ATR spike) ----------
    # If the current ATR% jumps to VOL_SPIKE_MULT x the ATR% snapshot
    # we took at position open, react: either CLOSE (safest) or
    # TIGHTEN_STOP (advisory). Default = TIGHTEN_STOP. Set spike
    # threshold to 0 to disable.
    ENABLE_VOL_FILTER: bool = True
    VOL_SPIKE_MULT: float = 1.8               # 1.8x = ATR almost doubled
    VOL_SPIKE_ACTION: Literal["close", "tighten_stop"] = "tighten_stop"

    # ---------- Time-based exit ----------
    # Maximum hours we hold a perp open. Stops a trade from drifting
    # forever after its thesis has evaporated. Set 0 to disable.
    MAX_POSITION_HOLD_HOURS: float = 24.0

    # ---------- Daily / global drawdown guard ----------
    # Portfolio-wide PnL kill switch. Tracked in-process across cycles
    # (rebuilt on restart). When the rolling realised+unrealised PnL
    # since session start (or since the last UTC midnight rollover)
    # crosses DAILY_LOSS_LIMIT_PCT of starting equity, the manager
    # forces a full risk-off and refuses to re-open until the operator
    # confirms (today = until the next process restart).
    ENABLE_DAILY_DD_GUARD: bool = True
    DAILY_LOSS_LIMIT_PCT: float = 5.0          # 5% of session starting equity

    # ---------- Per-asset ATR caps (1h) ----------
    # Hard ATR% ceiling per symbol on the 1h timeframe. Above the cap
    # the manager refuses to OPEN a fresh position and trims an
    # existing one to <= 50% size. Mirrors the per-asset bands in the
    # L3 critical-mode prompt so Fast Path + LLM agree on regimes.
    PER_ASSET_ATR_CAP_BTC_1H: float = 3.0      # BTC -> <= 3% ATR
    PER_ASSET_ATR_CAP_ETH_1H: float = 4.0      # ETH -> <= 4% ATR
    PER_ASSET_ATR_CAP_DEFAULT_1H: float = 4.0  # any other coin

    # ---------- Smart Path (L3 review trigger gates) ----------
    # The Smart Path is expensive (one LLM round-trip per fired
    # trigger), so we only invoke it on MATERIAL change. Each gate
    # below is OR-combined: any single trigger fires the review.
    ENABLE_SMART_PATH: bool = True
    # Price moved by N x ATR since the last L3 review on this
    # position -> review. 1.5-2.0 x ATR is the textbook "material
    # move" envelope.
    L3_REVIEW_TRIGGER_PRICE_ATR_MULT: float = 1.5
    # Funding rate magnitude (per-8h fraction) above which the L3 is
    # called even if price hasn't moved. 0.0005 = 0.05% per 8h.
    L3_REVIEW_TRIGGER_FUNDING_RATE: float = 0.0005
    # Number of large-trade whales seen in the L2 window. 5+ matches
    # the L3 prompt's "real signal" threshold.
    L3_REVIEW_TRIGGER_WHALE_COUNT: int = 5
    # OI delta magnitude (over 1h) above which we re-arbitrate.
    L3_REVIEW_TRIGGER_OI_DELTA_PCT: float = 4.0
    # Wall-clock minimum between forced reviews. We trigger at LEAST
    # once every N minutes per position so the LLM stays in the
    # loop even on a quiet market.
    L3_REVIEW_MIN_INTERVAL_MINUTES: float = 30.0
    # Wall-clock MAXIMUM between reviews - regardless of any other
    # gate, we re-arbitrate at least once every N minutes per open
    # position. Belt-and-braces vs forever-stale L3 verdicts.
    L3_REVIEW_MAX_INTERVAL_MINUTES: float = 120.0
    # When True, the Smart Path verdict can override the Fast Path
    # decision (e.g. veto a close, set a custom stop). Set False to
    # treat L3 as advisory only (Fast Path always wins).
    L3_CAN_OVERRIDE_FAST_PATH: bool = True
    # Minimum age (minutes) before a brand-new position is eligible
    # for a Smart-Path review. Stops the LLM from reviewing every
    # new open immediately - let the Fast Path observe the entry
    # first. 10 min is roughly one full --loop cycle at the default
    # DECISION_INTERVAL_SECONDS=600.
    L3_FIRST_REVIEW_DELAY_MINUTES: float = 10.0

    # ---------- HOLD-rescue (per-position) ----------
    # NB: a HARD floor of 25 minutes on the per-position Smart-Path
    # cooldown is enforced inside PositionManager regardless of any
    # .env value (see ``_HARD_COOLDOWN_FLOOR_MINUTES``). Operators
    # can configure values BELOW the floor but the manager will
    # silently uphold the floor for cost-safety.
    #
    # When unset, ENABLE_HOLD_RESCUE auto-resolves to True iff
    # L3_AGGRESSION=aggressive (mirrors the main-engine semantics).
    # ENABLE_HOLD_RESCUE: bool = True
    # Minimum L2 conviction (on this position's symbol, opposite
    # direction by default) required to trigger the rescue. Falls
    # back to L3_HOLD_RESCUE_L2_MIN (the main-engine equivalent)
    # when unset. Sensible bands: 0.55 .. 0.80.
    # HOLD_RESCUE_L2_MIN: float = 0.65
    # When unset, HOLD_RESCUE_DIRECTION_MODE auto-resolves to "any"
    # under L3_AGGRESSION=aggressive (allows the LLM to scale into
    # a strong trend, not just bail) and to "opposite" otherwise
    # (rescue only on a contradiction).
    # HOLD_RESCUE_DIRECTION_MODE: Literal["opposite", "any"] = "opposite"
    HOLD_RESCUE_COOLDOWN_MINUTES: float = 20.0

    # ---------- HOLD-must-be-earned (periodic re-audit) ----------
    # When True (auto-on under aggressive), a position that has been
    # HOLDing for >= HOLD_MUST_BE_EARNED_MINUTES forces a fresh
    # Smart-Path review even if no other gate fired. The intent:
    # HOLD shouldn't be a "we forgot about you" default - we should
    # be regularly auditing whether each open slot is still earned.
    # Suppressed when the previous Smart Path verdict was already
    # an explicit HOLD (avoids hitting the same answer twice in a
    # row).
    # HOLD_MUST_BE_EARNED: bool = True
    HOLD_MUST_BE_EARNED_MINUTES: float = 45.0

    # ---------- Global LLM budget (cost-safety rails, Day-6+) ----------
    # Hard ceilings on the AGGREGATE Smart-Path call volume across
    # all positions and across cycles. The per-position cooldown
    # ladder (hard floor + first-review delay + aggression shift +
    # rescue cooldown) bounds how often a single position can wake
    # the LLM. These ceilings sit ON TOP of that, bounding the TOTAL
    # call count so a multi-position cycle with N simultaneous fired
    # gates can never produce N round-trips.
    #
    # When more candidates fire than fit in ``LLM_MAX_PER_CYCLE``,
    # the manager keeps the highest-priority ones (HOLD-rescue >
    # close-audit > HOLD-must-be-earned > periodic > price/funding/
    # whale/OI > first-review) and skips the rest with telemetry.
    # When ``LLM_MAX_PER_HOUR`` is exhausted, ALL Smart-Path calls
    # are skipped regardless of trigger severity.
    #
    # Defaults are tuned for the default DECISION_INTERVAL_SECONDS
    # = 600 (10-min cycles): up to 2 Smart-Path round-trips per
    # cycle and 8 per rolling hour. At a 6-cycle/hr cadence with 2
    # positions, that's at most 12 fires "wanted" vs 8 allowed.
    # Operators should raise these only after observing actual call
    # rates via the panel's `Smart-Path usage` row.
    LLM_MAX_PER_CYCLE: int = 2
    LLM_MAX_PER_HOUR: int = 8

    # ---------- Conservative-mode hardening (Day-6+) ----------
    # Two flags that make conservative meaningfully more cautious
    # than just "raise the override threshold". Both auto-resolve
    # to True when L3_AGGRESSION=conservative, but can be set
    # explicitly to override that default.
    #
    # L3_CONSERVATIVE_VETO_ONLY:
    # When True under conservative, the LLM is allowed to VETO
    # Fast-Path closes (smart=hold downgrades close -> hold), but
    # NOT allowed to UPGRADE a Fast-Path HOLD into a close (smart=
    # close_full/partial). HOLD-rescue is exempt because rescue is
    # explicitly the operator asking the LLM to act on
    # contradiction. Net effect: conservative mode treats the LLM
    # strictly as a brake, never as an accelerator.
    #
    # L3_REQUIRE_MULTI_TRIGGER:
    # When True under conservative, Smart Path requires >= 2
    # corroborating gates on the same position before invocation.
    # A single price-move or single funding spike isn't enough -
    # we want at least two independent signals before paying for
    # an OpenRouter round-trip. Rescue is exempt (it has its own
    # cooldown bookkeeping). First-review doesn't count as
    # corroboration.
    #
    # L3_CONSERVATIVE_VETO_ONLY: bool = True   (auto under conservative)
    # L3_REQUIRE_MULTI_TRIGGER: bool = True    (auto under conservative)

    # ---------- Per-trigger feature flags (legacy + new) ----------
    # Useful for muting an individual trigger during debugging without
    # zeroing out its threshold (keeps panel telemetry intact).
    ENABLE_TRAILING_STOP: bool = True
    ENABLE_RE_EVALUATION: bool = True

    # ============================================================
    # Day-6+ Anti-overlap / anti-chop router guards
    # ============================================================
    # Hyperliquid is a *netting* venue: a second ``open_position`` on
    # an already-open ``(symbol, side)`` ADDS notional to the existing
    # position rather than opening a separate one. In ranging markets
    # the agent can issue ``risk_on long`` on every full cycle while
    # the previous long is still alive, silently stacking exposure
    # and amplifying every adverse tick. When True (default), the
    # AllocationRouter pre-empts the regime dispatch with a HOLD
    # whenever the surviving review snapshot already contains a
    # same-side position. The PositionManager keeps stewardship; the
    # router never doubles down. Flip to False ONLY to restore the
    # legacy "always add" behaviour (e.g. for averaging-down strategies
    # under a different risk model).
    BLOCK_DUPLICATE_SAME_SIDE_OPENS: bool = True

    # ---------- Post-stop-out cooldown (Stage 4 / Day-1) ----------
    # After a ``stop_loss`` close, the agent refuses a fresh entry on
    # the SAME symbol for ``POST_STOP_COOLDOWN_MINUTES``. This closes
    # the "stop -> instant re-entry -> stop again" chop loop observed
    # in live testing on 2026-05-26 (20+ stop-outs on a chopping BTC
    # long). The cooldown is PER-SYMBOL (a long stop-out also blocks
    # an immediate short re-entry on the same symbol) because the
    # thing we distrust after a stop is the *symbol's* near-term
    # regime, not one side of it. Stewardship of OPEN positions stays
    # with the PositionManager; this gate only suppresses NEW opens.
    #
    # The cooldown is owned by the PositionManager (it is the
    # component that knows a stop_loss close fired) and queried by the
    # AllocationRouter before every risk-on open - a single source of
    # truth, no duplicated bookkeeping. An in-progress ``side_flip``
    # is exempt so a flip's close+reopen chain can still complete.
    #
    # Set ENABLE_POST_STOP_COOLDOWN=false or POST_STOP_COOLDOWN_MINUTES
    # =0 to disable (e.g. for a mean-reversion strategy that *wants*
    # to re-enter quickly).
    ENABLE_POST_STOP_COOLDOWN: bool = True
    POST_STOP_COOLDOWN_MINUTES: float = 30.0

    # ============================================================
    # Day-2 Risk Engine - portfolio-level risk controls
    # ============================================================
    # These knobs feed :class:`src.allocation.risk_engine.RiskEngine`,
    # which layers portfolio-level controls on top of the per-trade
    # sizing pipeline. All defaults are chosen so the engine is a
    # NO-OP on a flat account until the operator tightens them.

    # ---- (Stage 6a) Minimum-equity-to-trade gate ----
    # Below this equity the router refuses NEW risk-on opens and holds
    # cash / USYC instead of churning a sub-scale account through fees.
    # 0 disables the gate. Example: 200 parks a sub-$200 testnet
    # account rather than bleeding it on round-trip costs.
    MIN_EQUITY_TO_TRADE_USD: float = 0.0

    # ---- Post-loss warm-up ramp ----
    # After a realised loss (stop-out, daily-DD flatten, or any close
    # whose loss clears WARMUP_TRIGGER_LOSS_PCT of equity) the engine
    # shrinks EVERY new entry to WARMUP_SIZE_MULT and ramps it back to
    # 1.0 linearly over WARMUP_AFTER_LOSS_MINUTES. Complements the
    # per-symbol post-stop cooldown: the cooldown blocks re-entry on the
    # stopped symbol; the warm-up de-risks the whole book for a while
    # because a fresh loss is evidence the regime is hostile.
    ENABLE_LOSS_WARMUP: bool = True
    WARMUP_AFTER_LOSS_MINUTES: float = 60.0
    WARMUP_SIZE_MULT: float = 0.5
    WARMUP_TRIGGER_LOSS_PCT: float = 1.0

    # ---- Correlated-exposure cap ----
    # Caps aggregate SAME-SIDE notional across a correlation group
    # (BTC + ETH move together) as a % of equity, shrinking or refusing
    # a new open that would breach it. Stops the agent from taking the
    # same directional bet twice under two tickers. 150% with 5x
    # leverage still leaves head-room; lower it to decorrelate harder.
    ENABLE_CORRELATION_CAP: bool = True
    MAX_CORRELATED_EXPOSURE_PCT: float = 150.0

    # ---- (Stage 6b) Pre-trade expected-value (EV) filter ----
    # Refuses opens whose take-profit reward can't beat the round-trip
    # cost (fees + slippage, both legs) by EV_MIN_REWARD_TO_COST. Kills
    # "scalp a 0.05% ATR range" entries that can't pay for themselves.
    # Pairs with the per-asset ATR cap (which rejects vol that's too
    # HOT) to bound the tradeable ATR band from both ends.
    ENABLE_PRETRADE_EV_FILTER: bool = True
    ROUND_TRIP_COST_BPS: float = 12.0
    EV_MIN_REWARD_TO_COST: float = 2.0

    # ---- Per-asset max position caps ----
    # Per-symbol-head hard cap on notional (USD) on top of the global
    # MAX_POSITION_USD. 0 => use the global cap for that asset. Lets BTC
    # run bigger than an ALT under one config.
    MAX_POSITION_USD_BTC: float = 0.0
    MAX_POSITION_USD_ETH: float = 0.0
    MAX_POSITION_USD_DEFAULT: float = 0.0

    @field_validator("*", mode="before")
    @classmethod
    def _empty_string_to_none(cls, value: Any) -> Any:
        """Treat empty strings in `.env` as missing values."""
        if isinstance(value, str) and value.strip() == "":
            return None
        return value

    @property
    def perp_symbols(self) -> list[str]:
        return [s.strip() for s in self.ARC_PERP_SYMBOLS.split(",") if s.strip()]

    @property
    def l1_timeframes(self) -> list[str]:
        return [t.strip() for t in self.L1_TIMEFRAMES.split(",") if t.strip()]

    @property
    def dune_chain(self) -> str:
        """Active chain tag (always lower-case to match Dune's column)."""
        return (self.DUNE_CHAIN or "ethereum").strip().lower()

    @property
    def dune_token_addresses(self) -> dict[str, str]:
        """Per-symbol token addresses resolved against `DUNE_CHAIN`.

        Returns a `{"BTC-PERP": "0x..", "ETH-PERP": "0x..", ...}`
        mapping that the Dune SQL templates use to filter
        `dex.trades` / `erc20_<chain>.evt_Transfer` to the assets the
        agent tracks. `.env` overrides win over the built-in chain map
        so you can repoint to any token (e.g. cbBTC instead of WBTC).
        """
        chain_defaults = _DEFAULT_TOKEN_ADDRESSES.get(self.dune_chain, {})
        resolved = {
            "BTC-PERP": (
                self.DUNE_TOKEN_BTC_ADDRESS or chain_defaults.get("BTC")
            ),
            "ETH-PERP": (
                self.DUNE_TOKEN_ETH_ADDRESS or chain_defaults.get("ETH")
            ),
        }
        return {sym: addr for sym, addr in resolved.items() if addr}

    @property
    def dune_usdc_address(self) -> str | None:
        """Resolved USDC address on `DUNE_CHAIN` (env override > default)."""
        chain_defaults = _DEFAULT_TOKEN_ADDRESSES.get(self.dune_chain, {})
        return self.DUNE_TOKEN_USDC_ADDRESS or chain_defaults.get("USDC")

    @property
    def dune_query_ids(self) -> dict[str, int]:
        """Per-metric saved Dune query ids, keyed by metric name.

        Metrics whose id is `None` are intentionally omitted so the
        Dune client reports them as `n/a` instead of executing query 0.
        """
        candidates = {
            "ohlcv": self.DUNE_QUERY_OHLCV_ID,
            "funding_rates": self.DUNE_QUERY_FUNDING_RATES_ID,
            "open_interest": self.DUNE_QUERY_OPEN_INTEREST_ID,
            "volume": self.DUNE_QUERY_VOLUME_ID,
            "vault_flows": self.DUNE_QUERY_VAULT_FLOWS_ID,
            "whale_activity": self.DUNE_QUERY_WHALE_ACTIVITY_ID,
            "long_short_ratio": self.DUNE_QUERY_LONG_SHORT_RATIO_ID,
            "cum_funding": self.DUNE_QUERY_CUM_FUNDING_ID,
            "market_sentiment": self.DUNE_QUERY_MARKET_SENTIMENT_ID,
        }
        return {k: int(v) for k, v in candidates.items() if v}

    @property
    def level_weights(self) -> dict[str, float]:
        return {
            "level1": self.WEIGHT_LEVEL1,
            "level2": self.WEIGHT_LEVEL2,
            "level3": self.WEIGHT_LEVEL3,
        }


# ---------------------------------------------------------------------------
# Chain -> token-address map
# ---------------------------------------------------------------------------
# Canonical on-chain representations of BTC / ETH / SOL on each EVM chain
# we support. Used to resolve `dune_token_addresses` when the .env doesn't
# override the address explicitly. Always lower-cased.

_DEFAULT_TOKEN_ADDRESSES: dict[str, dict[str, str]] = {
    "ethereum": {
        # WBTC - canonical Bitcoin wrap on Ethereum mainnet.
        "BTC": "0x2260fac5e5542a773aa44fbcfedf7c193bc2c599",
        # WETH - canonical Ether wrap on Ethereum mainnet.
        "ETH": "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2",
        # Circle USDC on Ethereum mainnet.
        "USDC": "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48",
    },
    "base": {
        # Coinbase-wrapped BTC on Base (cbBTC, the dominant BTC token).
        "BTC": "0xcbb7c0000ab88b473b1f5afd9ef808440eed33bf",
        # Canonical Base WETH (predeploy).
        "ETH": "0x4200000000000000000000000000000000000006",
        # Native USDC on Base.
        "USDC": "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",
    },
    "arbitrum": {
        # WBTC on Arbitrum One.
        "BTC": "0x2f2a2543b76a4166549f7aab2e75bef0aefc5b0f",
        # WETH on Arbitrum One.
        "ETH": "0x82af49447d8a07e3bd95bd0d56f35241523fbab1",
        # Native USDC on Arbitrum One.
        "USDC": "0xaf88d065e77c8cc2239327c5edb3a432268e5831",
    },
}


def _warn_duplicate_env_keys(env_path: Path) -> None:
    """Warn loudly when a `.env` key is declared more than once.

    python-dotenv / pydantic-settings silently keep the LAST occurrence
    of a duplicated key. A trailing empty duplicate (e.g.
    `DUNE_QUERY_FUNDING_RATES_ID=` further down the file) therefore
    overrides a real value declared earlier, and the affected metric
    renders as `n/a` with no clear error. We parse the raw file once
    and shout about any duplicates so the bug is caught at startup,
    not in the L2 provenance panel.
    """
    if not env_path.exists():
        return
    seen: dict[str, list[tuple[int, str]]] = {}
    try:
        for lineno, raw in enumerate(
            env_path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            if not key or " " in key:
                continue
            seen.setdefault(key, []).append((lineno, value.strip()))
    except OSError:
        return

    duplicates = {k: v for k, v in seen.items() if len(v) > 1}
    if not duplicates:
        return

    # Import locally to avoid a circular import at module load time.
    from src.utils.logging import logger

    for key, occurrences in duplicates.items():
        last_value = occurrences[-1][1]
        last_line = occurrences[-1][0]
        winning = f"line {last_line} -> {last_value!r}"
        details = ", ".join(
            f"line {ln}={'<empty>' if not val else val!r}"
            for ln, val in occurrences
        )
        # Empty trailing override = the silent-override footgun.
        if not last_value and any(val for _, val in occurrences[:-1]):
            logger.error(
                "Duplicate .env key {!r} - last occurrence is EMPTY and "
                "overrides a real value (winning: {}). Earlier values "
                "will be ignored! Occurrences: {}",
                key,
                winning,
                details,
            )
        else:
            logger.warning(
                "Duplicate .env key {!r} (winning: {}). Occurrences: {}",
                key,
                winning,
                details,
            )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached settings accessor; reads `.env` once per process."""
    _warn_duplicate_env_keys(Path(".env"))
    return Settings()
