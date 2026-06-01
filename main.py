"""CapitalArc - main entry point.

Wires the full pipeline:

    Level 1 (Dune MCP OHLCV) + Level 2 (Dune MCP on-chain) [+ Level 3]
        -> DecisionEngine (cascade L1 -> L2 -> L3)
            -> AllocationRouter (+ PositionManager: TP / SL / flip)
                -> HyperliquidExecutor    [primary trading venue]
                -> USYCExecutor + CircleWallet   [Arc-side yield leg]
                -> ArcPerpExecutor  [legacy, dry-run telemetry only]

Single source of truth: **Dune MCP**. Both Level 1 (OHLCV / TA) and
Level 2 (funding, OI, volume, vault flow, whales, L/S, cum funding,
sentiment) read from saved Dune queries. There is no CEX adapter
and no Arc-RPC market-data scraper.

Arc RPC is still used, but **only** for non-trading account state -
the agent's wallet balance, vault TVL and margin in the Market
Context panel. The decision engine itself never reads market signals
through it.

Usage
-----
    # One-shot dry-run (default - no on-chain transactions):
    python main.py

    # Loop in dry-run mode, decision every DECISION_INTERVAL_SECONDS:
    python main.py --loop

    # Live execution (ONLY when real router address + Circle creds set):
    python main.py --live

`--dry-run` is the default. Use `--live` consciously.
"""

from __future__ import annotations

import argparse
import asyncio
import re
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from dotenv import load_dotenv
from rich.console import Console
from rich.rule import Rule

from rich import box
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from src.allocation.allocation_router import AllocationConfig, AllocationRouter
from src.core.decision_engine import DecisionEngine, LevelScore
from src.core.level1 import Level1, Level1Config
from src.core.level2 import Level2, Level2Config
from src.core.level3 import ArbiterBriefing, Level3, Level3Config
from src.data.arc_onchain import ArcOnchainConfig, ArcOnchainReader
from src.data.dune_market_data import DuneMarketData, DuneMarketDataConfig
from src.data.dune_mcp import DuneMCPClient, DuneMCPClientConfig
from src.data.hyperliquid_intelligence import HyperliquidIntelligenceAdapter
from src.execution.arc_perp_executor import ArcPerpConfig, ArcPerpExecutor
from src.execution.circle_wallet import CircleWallet, CircleWalletConfig
from src.execution.hyperliquid_executor import (
    HyperliquidConfig,
    HyperliquidExecutor,
)
from src.execution.position_manager import (
    PositionManager,
    build_default_position_arbiter,
)
from src.execution.usyc_executor import USYCExecutor, USYCExecutorConfig
from src.llm.openrouter_client import OpenRouterClient, OpenRouterClientConfig
from src.utils.config import Settings, get_settings
from src.utils.console import (
    execution_plan_panel,
    final_decision_panel,
    level1_panel,
    level2_panel,
    level3_panel,
    market_context_panel,
    onchain_result_panel,
    position_review_panel,
)
from src.utils.logging import configure_logging, logger


console = Console()


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _build_circle_wallet(settings: Settings, dry_run: bool) -> CircleWallet:
    cfg = CircleWalletConfig(
        api_key=settings.CIRCLE_API_KEY or "",
        wallet_id=settings.CIRCLE_AGENT_WALLET_ID or "",
        entity_secret=settings.CIRCLE_ENTITY_SECRET,
        base_url=settings.CIRCLE_API_BASE_URL,
        paymaster_url=settings.CIRCLE_PAYMASTER_URL,
        paymaster_policy_id=settings.CIRCLE_PAYMASTER_POLICY_ID,
    )
    return CircleWallet(config=cfg, dry_run=dry_run)


def _build_executor(
    settings: Settings, wallet: CircleWallet, dry_run: bool
) -> ArcPerpExecutor:
    """Build the Arc Perp executor.

    .. deprecated::
        Live trading has migrated to :class:`HyperliquidExecutor`
        (see ``_build_hyperliquid_executor``). This builder remains
        only so that legacy treasury / dry-run telemetry paths keep
        compiling. ``open_position`` / ``close_position`` on the
        returned executor are no-ops in ``--live``.
    """
    cfg = ArcPerpConfig(
        router_address=settings.ARC_PERP_ROUTER_ADDRESS,
        vault_address=settings.ARC_PERP_VAULT_ADDRESS,
        market_registry_address=settings.ARC_PERP_MARKET_REGISTRY_ADDRESS,
        position_ledger_address=settings.ARC_PERP_POSITION_LEDGER_ADDRESS,
        matcher_url=settings.ARC_PERP_MATCHER_URL,
        rpc_url=settings.ARC_RPC_URL,
        max_leverage=settings.ARC_PERP_MAX_LEVERAGE,
    )
    return ArcPerpExecutor(wallet=wallet, config=cfg, dry_run=dry_run)


def _build_hyperliquid_executor(
    settings: Settings, dry_run: bool
) -> HyperliquidExecutor:
    """Build the primary Hyperliquid Testnet trading executor.

    This is the executor the AllocationRouter actually opens / closes
    positions through. It is configured entirely from ``HYPERLIQUID_*``
    settings in ``.env``; the Arc-side Circle wallet is NOT involved
    here (Hyperliquid uses its own EVM key for signing - see the
    ``HYPERLIQUID_PRIVATE_KEY`` doc in ``.env.example``).

    A one-line confirmation log shows whether the key was picked up
    and which API URL we're hitting, so operators can validate the
    wiring on every startup even before the SDK actually contacts
    Hyperliquid.
    """
    cfg = HyperliquidConfig(
        private_key=settings.HYPERLIQUID_PRIVATE_KEY,
        account_address=settings.HYPERLIQUID_ACCOUNT_ADDRESS or None,
        api_url=settings.HYPERLIQUID_API_URL,
        # ALWAYS mainnet by default - keeps the market-data plane
        # honest even when ``api_url`` flips to testnet for safe
        # live-test runs. See HyperliquidConfig.data_api_url docstring.
        data_api_url=settings.HYPERLIQUID_DATA_API_URL,
        vault_address=settings.HYPERLIQUID_VAULT_ADDRESS or None,
        max_leverage=settings.HYPERLIQUID_MAX_LEVERAGE,
        max_position_usd=Decimal(str(settings.HYPERLIQUID_MAX_POSITION_USD)),
        default_slippage_bps=settings.HYPERLIQUID_DEFAULT_SLIPPAGE_BPS,
        default_take_profit_pct=Decimal(
            str(settings.HYPERLIQUID_DEFAULT_TAKE_PROFIT_PCT)
        ),
        default_stop_loss_pct=Decimal(
            str(settings.HYPERLIQUID_DEFAULT_STOP_LOSS_PCT)
        ),
        use_market_orders=settings.HYPERLIQUID_USE_MARKET_ORDERS,
    )
    _log_hyperliquid_config(cfg)
    return HyperliquidExecutor(config=cfg, dry_run=dry_run)


def _log_hyperliquid_config(cfg: HyperliquidConfig) -> None:
    """Print a one-line confirmation that the Hyperliquid config was wired.

    Called from both the real executor builder (``--dry-run`` / ``--live``)
    and ``--test-allocation`` so operators always get visual confirmation
    that ``HYPERLIQUID_PRIVATE_KEY`` and friends came through ``.env``.

    For the key itself we never print the full value - only a redacted
    prefix and the EVM address it derives to via ``eth_account`` (a
    transitive dependency of ``hyperliquid-python-sdk`` and therefore
    always available at runtime).
    """
    # eth_account is a transitive dep of hyperliquid-python-sdk and is
    # guaranteed installed; the local import just keeps the symbol scoped.
    from eth_account import Account

    derived_addr: str | None = None
    key_preview: str | None = None
    if cfg.private_key:
        key_preview = (
            f"{cfg.private_key[:6]}...{cfg.private_key[-4:]}"
            if len(cfg.private_key) >= 12
            else "(short)"
        )
        try:
            derived_addr = Account.from_key(cfg.private_key).address
        except (ValueError, TypeError) as exc:
            # Bad hex / wrong length - log and surface a placeholder so
            # the operator notices that the key is malformed without
            # crashing the whole startup path.
            logger.warning(
                "HYPERLIQUID_PRIVATE_KEY could not be parsed by eth_account: {}",
                exc,
            )
            derived_addr = "(invalid key)"

    # Surface BOTH URLs in the startup banner. ``exec_api`` is where
    # orders + account-state reads land (typically testnet); ``data_api``
    # is where the market-data Info client points (typically mainnet -
    # we never want a thin testnet tape to feed L2 analytics).
    signer_short = f"…{derived_addr[-8:]}" if derived_addr and len(derived_addr) > 8 else (derived_addr or "-")
    logger.info(
        "HL wired | signer={} exec={} lev={}x pos=${}",
        signer_short,
        cfg.api_url,
        cfg.max_leverage,
        int(cfg.max_position_usd),
    )


def _build_usyc_executor(
    settings: Settings, wallet: CircleWallet, dry_run: bool
) -> USYCExecutor:
    """Build the USYC mint / redeem executor (yield-bearing risk-off leg).

    When ``USYC_TOKEN_ADDRESS`` / ``USYC_MINT_CONTRACT_ADDRESS`` /
    ``USDC_TOKEN_ADDRESS`` aren't all set in ``.env``,
    :attr:`USYCExecutor.is_configured` returns False and the
    AllocationRouter skips the rotation leg gracefully (no hard
    failure - the perp close still runs).
    """
    cfg = USYCExecutorConfig(
        usyc_token_address=settings.USYC_TOKEN_ADDRESS,
        usyc_mint_address=settings.USYC_MINT_CONTRACT_ADDRESS,
        usdc_address=settings.USDC_TOKEN_ADDRESS,
        mint_signature=settings.USYC_MINT_SIGNATURE,
        redeem_signature=settings.USYC_REDEEM_SIGNATURE,
        min_rotation_usdc=Decimal(str(settings.USYC_MIN_ROTATION_AMOUNT)),
        max_rotation_usdc=Decimal(str(settings.USYC_MAX_ROTATION_AMOUNT)),
        usdc_reserve_usd=Decimal(str(settings.USYC_USDC_RESERVE_USD)),
        rpc_url=settings.ARC_RPC_URL,
    )
    return USYCExecutor(wallet=wallet, config=cfg, dry_run=dry_run)


def _build_dune_market_data(
    settings: Settings, dune: DuneMCPClient | None
) -> DuneMarketData:
    return DuneMarketData(
        dune=dune,
        config=DuneMarketDataConfig(
            chain=settings.dune_chain,
            symbols=settings.perp_symbols or ["BTC-PERP", "ETH-PERP"],
            intervals=settings.l1_timeframes,
            lookback_hours=settings.OHLCV_LOOKBACK_HOURS,
            cache_ttl_seconds=(
                settings.DEMO_CACHE_TTL_SECONDS
                if settings.DEMO_MODE
                else settings.DUNE_CACHE_TTL_SECONDS
            ),
            token_addresses=settings.dune_token_addresses,
        ),
    )


def _build_hyperliquid_intelligence(
    settings: Settings,
    executor: HyperliquidExecutor,
) -> HyperliquidIntelligenceAdapter | None:
    """Build the Level-2 perp-metric overlay adapter, if enabled.

    Returns ``None`` when ``HYPERLIQUID_INTELLIGENCE_ENABLED=false``
    so Level 2 transparently stays on the pure-Dune path. Otherwise
    constructs the adapter pointed at the executor's market-data
    Info client (always pinned to ``HYPERLIQUID_DATA_API_URL`` =
    mainnet by default) - which is exactly why we built the
    execution-vs-data API split: real market intelligence even when
    the trading venue is testnet.
    """
    if not settings.HYPERLIQUID_INTELLIGENCE_ENABLED:
        logger.info(
            "Hyperliquid intelligence overlay disabled "
            "(HYPERLIQUID_INTELLIGENCE_ENABLED=false) - "
            "Level 2 will use Dune proxies for funding / OI / "
            "volume / cum_funding."
        )
        return None
    return HyperliquidIntelligenceAdapter(
        info_client=executor.info_data,
        api_url=settings.HYPERLIQUID_DATA_API_URL,
    )


def _build_dune(settings: Settings) -> DuneMCPClient | None:
    if not settings.DUNE_API_KEY:
        return None
    return DuneMCPClient(
        DuneMCPClientConfig(
            api_key=settings.DUNE_API_KEY,
            api_base_url=settings.DUNE_API_BASE_URL,
            mcp_url=settings.DUNE_MCP_URL,
            cache_ttl_seconds=(
                settings.DEMO_CACHE_TTL_SECONDS
                if settings.DEMO_MODE
                else settings.DUNE_CACHE_TTL_SECONDS
            ),
            max_concurrent_requests=settings.DUNE_MAX_CONCURRENT_REQUESTS,
            rate_limit_max_retries=settings.DUNE_RATE_LIMIT_MAX_RETRIES,
            rate_limit_backoff_seconds=settings.DUNE_RATE_LIMIT_BACKOFF_SECONDS,
            query_ids=settings.dune_query_ids,
        )
    )


def _build_arc_reader(settings: Settings) -> ArcOnchainReader:
    return ArcOnchainReader(
        ArcOnchainConfig(
            rpc_url=settings.ARC_RPC_URL,
            vault_address=settings.ARC_PERP_VAULT_ADDRESS,
            usdc_address=settings.USDC_TOKEN_ADDRESS,
            chain_id=settings.ARC_CHAIN_ID,
        )
    )


def _build_level3(settings: Settings) -> tuple[Level3 | None, OpenRouterClient | None]:
    """Build the real OpenRouter-backed Level 3 if ``OPENROUTER_API_KEY`` is set.

    Returns a ``(level3, client)`` pair so the caller can ``aclose()``
    the underlying httpx pool on shutdown. When no API key is present,
    returns ``(None, None)`` and the engine falls back to the synthetic
    L3 placeholder (whose weight is then redistributed back to L1+L2).
    This keeps demos running without an OpenRouter key while making the
    upgrade to real arbitration a single ``.env`` change.

    The underlying upstream model is whatever ``OPENROUTER_MODEL``
    resolves to (default: ``anthropic/claude-sonnet-4.6``); switch
    providers by changing that env var alone.
    """
    if not settings.OPENROUTER_API_KEY:
        logger.info(
            "OPENROUTER_API_KEY not set - Level 3 will run as synthetic "
            "placeholder (weight redistributed to L1+L2)."
        )
        return None, None
    client = OpenRouterClient(
        OpenRouterClientConfig(
            api_key=settings.OPENROUTER_API_KEY,
            model=settings.OPENROUTER_MODEL,
            temperature=settings.OPENROUTER_TEMPERATURE,
            max_tokens=settings.OPENROUTER_MAX_TOKENS,
            timeout_seconds=settings.OPENROUTER_TIMEOUT_SECONDS,
            max_retries=settings.OPENROUTER_MAX_RETRIES,
            backoff_seconds=settings.OPENROUTER_BACKOFF_SECONDS,
            referer=settings.OPENROUTER_REFERER,
            app_title=settings.OPENROUTER_APP_TITLE,
        )
    )
    logger.info(
        "L3 wired | model={} mode={} aggression={}",
        settings.OPENROUTER_MODEL.split("/")[-1],
        settings.L3_MODE,
        settings.L3_AGGRESSION,
    )
    level3 = Level3(
        client=client,
        config=Level3Config(
            model=settings.OPENROUTER_MODEL,
            temperature=settings.OPENROUTER_TEMPERATURE,
            max_output_tokens=settings.OPENROUTER_MAX_TOKENS,
            timeout_seconds=settings.OPENROUTER_TIMEOUT_SECONDS,
            mode=settings.L3_MODE,
            # Day-6 aggression calibration knob.
            aggression=settings.L3_AGGRESSION,
            hold_rescue_l2_min=settings.L3_HOLD_RESCUE_L2_MIN,
            hold_rescue_intensity=settings.L3_HOLD_RESCUE_INTENSITY,
        ),
    )
    return level3, client


def _build_engine(
    settings: Settings,
    market_data: DuneMarketData,
    dune: DuneMCPClient | None,
    level3: Level3 | None,
    hyperliquid_intel: HyperliquidIntelligenceAdapter | None = None,
) -> DecisionEngine:
    level1 = Level1(
        Level1Config(
            timeframes=settings.l1_timeframes,
            klines_limit=settings.L1_KLINES_LIMIT,
            ema_fast=settings.L1_EMA_FAST,
            ema_slow=settings.L1_EMA_SLOW,
            rsi_period=settings.L1_RSI_PERIOD,
            atr_period=settings.L1_ATR_PERIOD,
            rsi_overbought=settings.L1_RSI_OVERBOUGHT,
            rsi_oversold=settings.L1_RSI_OVERSOLD,
            atr_pct_min=settings.L1_ATR_PCT_MIN,
            atr_pct_max=settings.L1_ATR_PCT_MAX,
            max_drawdown_pct=settings.MAX_DRAWDOWN_PCT,
            require_tf_agreement=settings.L1_REQUIRE_TF_AGREEMENT,
        ),
        market_data=market_data,
    )
    level2 = Level2(
        Level2Config(
            symbols=settings.perp_symbols or ["BTC-PERP", "ETH-PERP"],
            chain=settings.dune_chain,
            lookback_hours=settings.DUNE_LOOKBACK_HOURS,
            cache_ttl_seconds=(
                settings.DEMO_CACHE_TTL_SECONDS
                if settings.DEMO_MODE
                else settings.DUNE_CACHE_TTL_SECONDS
            ),
            demo_mode=settings.DEMO_MODE,
            token_addresses=settings.dune_token_addresses,
            usdc_address=settings.dune_usdc_address,
            vault_address=(
                settings.DUNE_PERP_VAULT_ADDRESS
                or settings.ARC_PERP_VAULT_ADDRESS
            ),
            whale_min_usd=settings.DUNE_WHALE_MIN_USD,
            # Master switch is at the engine wiring level (we only
            # pass the adapter in when the operator left
            # HYPERLIQUID_INTELLIGENCE_ENABLED=true). The Level2Config
            # toggle is kept as a tactical kill switch we can flip
            # in tests / future bias scenarios.
            prefer_hyperliquid_for_perp_metrics=(
                hyperliquid_intel is not None
            ),
        ),
        dune=dune,
        hyperliquid_intel=hyperliquid_intel,
    )
    return DecisionEngine(
        level1=level1,
        level2=level2,
        level3=level3,
        weights=settings.level_weights,
        risk_on_threshold=settings.RISK_ON_THRESHOLD,
        risk_off_threshold=settings.RISK_OFF_THRESHOLD,
        short_bias_min_strength=settings.SHORT_BIAS_MIN_STRENGTH,
        strong_bias_open_strength=settings.STRONG_BIAS_OPEN_STRENGTH,
        # Day-6+ anti-chop: require L1 to corroborate STRONG-DIRECTION
        # mid-band overrides (L1 must clear the 0.25 flat/mixed plateau).
        strong_direction_l1_corroboration_min=float(
            getattr(settings, "STRONG_DIRECTION_L1_CORROBORATION_MIN", 0.40)
        ),
        redistribute_synthetic_l3_weight=settings.REDISTRIBUTE_SYNTHETIC_L3_WEIGHT,
        allow_l3_to_override_l1=settings.ALLOW_L3_TO_OVERRIDE_L1,
        l3_override_min_conviction=settings.L3_OVERRIDE_MIN_CONVICTION,
    )


def _build_router(
    settings: Settings,
    executor: Any,
    dry_run: bool,
    usyc_executor: USYCExecutor | None = None,
    openrouter_client: OpenRouterClient | None = None,
) -> AllocationRouter:
    """Build the AllocationRouter.

    ``executor`` is duck-typed via :class:`PerpExecutorProtocol` so
    either a :class:`HyperliquidExecutor` (production) or a
    :class:`ArcPerpExecutor` (legacy / dry-run telemetry) - or a fake
    used by ``--test-allocation`` - works without changes here.

    ``openrouter_client`` is the same OpenRouter client built for L3
    main arbitration. When provided AND ``ENABLE_SMART_PATH=true``,
    the PositionManager's Smart Path wires the same backend so we
    pay for one API key, not two.
    """
    symbols = settings.perp_symbols or ["BTC-PERP"]
    # Per-asset risk multipliers - operators can dial BTC at 1.0x and
    # ETH at 0.75x without touching the engine. Built from the
    # Settings helpers so the resolution is the same source of truth
    # used by Settings.risk_multiplier_for_symbol.
    risk_multipliers = {
        "BTC": float(settings.RISK_PER_TRADE_MULT_BTC),
        "ETH": float(settings.RISK_PER_TRADE_MULT_ETH),
    }
    # Resolve the per-trade risk fraction. RISK_PER_TRADE_PCT (1.0 =
    # 1%) wins over the legacy TARGET_RISK_PCT (0.02 = 2%) when set.
    resolved_risk_frac = settings.resolved_risk_per_trade_frac
    cfg = AllocationConfig(
        perp_symbol=symbols[0],
        base_position_usd=Decimal(str(settings.BASE_POSITION_USD)),
        max_position_usd=Decimal(str(settings.MAX_POSITION_USD)),
        max_drawdown_pct=settings.MAX_DRAWDOWN_PCT,
        risk_on_threshold=settings.RISK_ON_THRESHOLD,
        risk_off_threshold=settings.RISK_OFF_THRESHOLD,
        symmetric_short_sizing=settings.SYMMETRIC_SHORT_SIZING,
        short_size_multiplier=Decimal(str(settings.SHORT_SIZE_MULTIPLIER)),
        target_risk_pct=resolved_risk_frac,
        risk_multiplier_per_symbol=risk_multipliers,
        risk_multiplier_default=float(settings.RISK_PER_TRADE_MULT_DEFAULT),
        stop_atr_mult=settings.STOP_ATR_MULT,
        min_atr_pct_for_sizing=settings.MIN_ATR_PCT_FOR_SIZING,
        dd_haircut_exponent=settings.DD_HAIRCUT_EXPONENT,
        explain_sizing=settings.EXPLAIN_SIZING,
        usyc_enabled=settings.USYC_ENABLED,
        usdc_reserve_usd=Decimal(str(settings.USYC_USDC_RESERVE_USD)),
        risk_off_withdraw=settings.RISK_OFF_WITHDRAW,
        # Day-6+ anti-overlap guard: refuse same-side opens when an
        # existing position survives the cycle's PositionReview.
        # See src/utils/config.py::BLOCK_DUPLICATE_SAME_SIDE_OPENS.
        block_duplicate_same_side_opens=bool(
            getattr(settings, "BLOCK_DUPLICATE_SAME_SIDE_OPENS", True)
        ),
    )
    logger.info(
        "Sizing | risk={:.2f}% stop={}xATR mults BTC={} ETH={} default={}",
        resolved_risk_frac * 100,
        settings.STOP_ATR_MULT,
        settings.RISK_PER_TRADE_MULT_BTC,
        settings.RISK_PER_TRADE_MULT_ETH,
        settings.RISK_PER_TRADE_MULT_DEFAULT,
    )
    # Smart Path arbiter: when ENABLE_SMART_PATH=true AND an
    # OpenRouter client is wired, the per-position L3 review uses
    # the same client as the main L3. Otherwise we pass None, the
    # PositionManager falls back to the inert synthetic arbiter and
    # the Fast Path carries every cycle on its own.
    position_arbiter = None
    if settings.ENABLE_SMART_PATH and openrouter_client is not None:
        position_arbiter = build_default_position_arbiter(
            openrouter_client,
            model=settings.OPENROUTER_MODEL,
            timeout_seconds=settings.OPENROUTER_TIMEOUT_SECONDS,
        )
        logger.info(
            "SmartPath | model={} price≥{}xATR fund≥{:.4f}% interval≤{}min",
            settings.OPENROUTER_MODEL.split("/")[-1],
            settings.L3_REVIEW_TRIGGER_PRICE_ATR_MULT,
            settings.L3_REVIEW_TRIGGER_FUNDING_RATE * 100,
            settings.L3_REVIEW_MAX_INTERVAL_MINUTES,
        )
    elif settings.ENABLE_SMART_PATH:
        logger.info(
            "PositionManager Smart Path enabled but no OPENROUTER_API_KEY - "
            "running Fast Path only (synthetic Smart Path will defer to Fast)."
        )
    # Build the PositionManager from the operator-friendly percentage
    # form (.env: TAKE_PROFIT_PCT=3.0 etc.) - see config.py / .env.example
    # for the full documented surface.
    position_manager = PositionManager.from_settings(
        settings, executor=executor, position_arbiter=position_arbiter,
    )
    return AllocationRouter(
        executor=executor,
        config=cfg,
        dry_run=dry_run,
        usyc_executor=usyc_executor,
        position_manager=position_manager,
    )


# ---------------------------------------------------------------------------
# Pre-flight & helpers
# ---------------------------------------------------------------------------


def _preflight_live(settings: Settings) -> list[str]:
    """Return the list of env vars that ``--live`` requires but are missing.

    Covers three groups:

    1. **Hyperliquid Testnet** (must be present - this is where every
       perp open / close lands).
    2. **Circle DCW** (must be present for USYC mint / redeem and
       any future CCTP bridge step).
    3. **USYC rotation leg** - required ONLY when
       ``USYC_ENABLED=true``. When the operator opts out of the yield
       leg (``USYC_ENABLED=false``), the agent runs in **perp-only
       mode**: the AllocationRouter still closes perps on risk-off
       but skips the USDC -> USYC mint, so the three USYC_/USDC_
       address values are not needed to start ``--live``. This makes
       it possible to live-test the Hyperliquid trading leg in
       isolation, before the USYC integration is wired.

       When ``USYC_ENABLED=true`` we *do* enforce strictness in
       ``--live``: the router calls into the USYC mint contract
       during every risk-off cycle, and a missing address means real
       capital would silently sit idle in USDC instead of earning
       yield (the dry-run path degrades gracefully; live should be
       loud about this).

    Arc Perp DEX (legacy) settings are NOT required - the venue is
    deprecated and the corresponding executor is no-op in ``--live``.
    """
    required = {
        "HYPERLIQUID_PRIVATE_KEY": settings.HYPERLIQUID_PRIVATE_KEY,
        "HYPERLIQUID_API_URL": settings.HYPERLIQUID_API_URL,
        "CIRCLE_API_KEY": settings.CIRCLE_API_KEY,
        "CIRCLE_ENTITY_SECRET": settings.CIRCLE_ENTITY_SECRET,
        "CIRCLE_AGENT_WALLET_ID": settings.CIRCLE_AGENT_WALLET_ID,
    }
    if settings.USYC_ENABLED:
        # USYC leg is opted-in - the three address values become hard
        # requirements. With USYC_ENABLED=false this whole block is
        # skipped and --live can start on Hyperliquid + Circle alone.
        required.update(
            {
                "USYC_TOKEN_ADDRESS": settings.USYC_TOKEN_ADDRESS,
                "USYC_MINT_CONTRACT_ADDRESS": settings.USYC_MINT_CONTRACT_ADDRESS,
                "USDC_TOKEN_ADDRESS": settings.USDC_TOKEN_ADDRESS,
            }
        )
    return [name for name, value in required.items() if not value]


def _explorer_link(settings: Settings, tx_hash: str | None) -> str | None:
    if not (tx_hash and settings.ARC_EXPLORER_URL):
        return None
    base = settings.ARC_EXPLORER_URL.rstrip("/")
    return f"{base}/tx/{tx_hash}"


# Circle DCW transaction IDs are 36-character UUIDs
# ("8b3b3b3b-1234-5678-9abc-def012345678"). Anything else - numeric
# Hyperliquid order ids, ``hl-*`` / ``noop-*`` / ``dryrun-*``
# synthetic markers - is NOT a Circle tx and must not be polled
# against ``/v1/w3s/transactions/{id}`` (Circle returns 400).
_CIRCLE_TX_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


def _is_circle_tx_id(tx_id: str) -> bool:
    """True iff ``tx_id`` looks like a Circle DCW transaction UUID."""
    return bool(tx_id) and bool(_CIRCLE_TX_UUID_RE.match(tx_id))


async def _build_market_context(
    settings: Settings,
    executor: Any,
    wallet: CircleWallet,
    usyc_executor: USYCExecutor | None = None,
) -> dict[str, Any]:
    """Assemble the decision-engine context (symbols, RPC, account, drawdown).

    ``executor`` is duck-typed - the Market Context panel only needs
    ``get_margin`` / ``get_pnl`` and an optional ``set_account_address``
    hook, both of which are exposed by HyperliquidExecutor,
    ArcPerpExecutor and the test fakes alike.
    """
    addr = await wallet.get_address()
    if addr:
        # Late-bind the trading account address ONLY when the executor
        # doesn't already know it. The legacy ArcPerpExecutor uses the
        # Circle DCW address as its trading account (the smart-wallet
        # IS the on-chain trader on Arc) and starts with
        # ``account_address=None`` until set here. HyperliquidExecutor
        # is different: it manages its own ``_account_address``
        # internally during SDK construction (signer's own EVM address
        # by default, or HYPERLIQUID_ACCOUNT_ADDRESS in agent-wallet
        # mode) and the Circle DCW address is meaningless to it - that
        # wallet lives on Arc, not on Hyperliquid. Overwriting
        # Hyperliquid's address with the Circle one here used to
        # silently break BOTH ``get_open_positions()`` (queries the
        # wrong account -> always empty) AND the post-open native
        # TP/SL submission (the executor's ``get_position`` saw "flat"
        # -> TP/SL silently skipped). The "skip when already set"
        # rule keeps the legacy Arc path working without trampling
        # any executor that wired its own address upstream.
        existing_addr = getattr(executor, "account_address", None) or getattr(
            executor, "_account_address", None
        )
        if not existing_addr:
            set_addr = getattr(executor, "set_account_address", None)
            if callable(set_addr):
                set_addr(addr)
        if usyc_executor is not None:
            usyc_executor.set_account_address(addr)
    # Arc-style account_id (bytes32 padded address). Only present on
    # the legacy ArcPerpExecutor; HyperliquidExecutor has no analogue
    # because positions are keyed by EVM address directly.
    account_id: str | None = None
    account_id_fn = getattr(executor, "_account_id", None)
    if callable(account_id_fn):
        try:
            account_id = account_id_fn() if addr else None
        except Exception:  # noqa: BLE001 - address may be empty in dry-run
            account_id = None
    margin = await executor.get_margin()
    pnl = await executor.get_pnl()
    drawdown_pct = 0.0
    if margin > 0 and pnl < 0:
        drawdown_pct = float(-pnl / margin * 100)

    usyc_snapshot = None
    if usyc_executor is not None:
        usyc_snapshot = await usyc_executor.get_snapshot()

    return {
        "symbols": settings.perp_symbols,
        "symbol": (settings.perp_symbols or ["BTC-PERP"])[0],
        "rpc_url": settings.ARC_RPC_URL,
        "account_address": addr,
        "account_id": account_id,
        "account_margin_usdc": float(margin),
        "account_unrealized_pnl_usdc": float(pnl),
        "account_drawdown_pct": drawdown_pct,
        "l1_timeframes": settings.l1_timeframes,
        "dune_chain": settings.dune_chain,
        "dune_tokens": settings.dune_token_addresses,
        "usyc": usyc_snapshot,
    }


# ---------------------------------------------------------------------------
# Runtime context (shared across fast + full cycles in --loop mode)
# ---------------------------------------------------------------------------


@dataclass
class _RuntimeContext:
    """Long-lived components reused across decision cycles.

    Built once at the top of :func:`run_loop` (or :func:`run_once`) and
    reused for the entire process lifetime. Two reasons we keep it
    long-lived:

    1. **PositionManager state.** ``_PositionState`` carries
       ``breakeven_armed``, ``partial_tp_done``, ``peak_pnl_pct``,
       ``last_l3_check_at`` and ``opened_at`` - all in-process. If we
       rebuilt the manager every cycle the trailing stop would lose
       its peak, breakeven would never arm, and partial TP would fire
       on every cycle the price stays above the target. Keeping one
       :class:`PositionManager` instance for the lifetime of the loop
       is the only way the stewardship rules behave correctly.
    2. **Cost.** Building Dune / OpenRouter / SDK clients is cheap but
       not free; reusing them across cycles avoids redundant TCP
       handshakes and SDK ``meta()`` warm-ups every 2 minutes.
    """

    settings: Settings
    wallet: CircleWallet
    executor: HyperliquidExecutor
    arc_legacy: Any
    usyc_executor: USYCExecutor
    dune: DuneMCPClient | None
    market_data: DuneMarketData
    onchain: ArcOnchainReader
    level3: Level3 | None
    openrouter_client: OpenRouterClient | None
    hyperliquid_intel: Any
    engine: DecisionEngine
    router: AllocationRouter
    dry_run: bool
    mode: str
    level2_connected: bool = False

    async def aclose(self) -> None:
        """Tear down every long-lived client. Idempotent."""
        await self.wallet.aclose()
        if self.dune is not None:
            await self.dune.aclose()
        if self.openrouter_client is not None:
            await self.openrouter_client.aclose()


def _print_live_banner(settings: Settings) -> None:
    """Loud one-time announcement of the live venue + yield-leg mode."""
    logger.warning(
        "LIVE mode: orders WILL be placed on Hyperliquid Testnet "
        "(api={}).",
        settings.HYPERLIQUID_API_URL,
    )
    if settings.USYC_ENABLED:
        logger.info(
            "USYC leg ACTIVE - risk-off rotations will mint USYC "
            "from the freed USDC margin (yield earned while flat)."
        )
    else:
        logger.info(
            "USYC disabled | perp-only mode; freed margin stays USDC on risk-off"
        )


def _enforce_live_preflight(settings: Settings) -> None:
    """Hard-stop ``--live`` startup when required env vars are missing."""
    missing = _preflight_live(settings)
    if not missing:
        return
    usyc_missing = any(
        name.startswith(("USYC_", "USDC_TOKEN_ADDRESS"))
        for name in missing
    )
    hl_missing = any(name.startswith("HYPERLIQUID_") for name in missing)
    extra_hint = ""
    if hl_missing:
        extra_hint += (
            "\n  Hint: HYPERLIQUID_PRIVATE_KEY must be a fresh "
            "testnet key fauceted at https://app.hyperliquid-testnet.xyz. "
            "Default API URL is https://api.hyperliquid-testnet.xyz."
        )
    if usyc_missing:
        extra_hint += (
            "\n  Hint: USYC_ENABLED=true requires USYC_TOKEN_ADDRESS, "
            "USYC_MINT_CONTRACT_ADDRESS and USDC_TOKEN_ADDRESS. "
            "Set USYC_ENABLED=false in .env to demo a perp-only build "
            "without wiring USYC."
        )
    logger.error(
        "Cannot start --live: missing env values: {}{}",
        ", ".join(missing),
        extra_hint,
    )
    raise SystemExit(2)


async def _build_runtime(dry_run: bool) -> _RuntimeContext:
    """Build every long-lived component for a CapitalArc loop.

    Used by both ``--loop`` (one ctx for the whole process) and the
    one-shot ``run_once`` path (ctx torn down after a single cycle).
    Wires the live banner + ``--live`` preflight in the same place so
    every entry point gets the same safety gates.
    """
    settings = get_settings()
    configure_logging(settings.LOG_LEVEL, rich_console=console)
    mode = "DRY-RUN" if dry_run else "LIVE"
    console.print(Rule(f"[bold cyan]CapitalArc[/]  mode={mode}  env={settings.APP_ENV}"))
    logger.info("CapitalArc starting | mode={} env={}", mode, settings.APP_ENV)

    if not dry_run:
        _enforce_live_preflight(settings)
        _print_live_banner(settings)

    wallet = _build_circle_wallet(settings, dry_run=dry_run)
    executor = _build_hyperliquid_executor(settings, dry_run=dry_run)
    arc_legacy = _build_executor(settings, wallet=wallet, dry_run=dry_run)
    usyc_executor = _build_usyc_executor(
        settings, wallet=wallet, dry_run=dry_run
    )
    dune = _build_dune(settings)
    market_data = _build_dune_market_data(settings, dune=dune)
    onchain = _build_arc_reader(settings)
    level3, openrouter_client = _build_level3(settings)
    hyperliquid_intel = _build_hyperliquid_intelligence(settings, executor)
    engine = _build_engine(
        settings,
        market_data=market_data,
        dune=dune,
        level3=level3,
        hyperliquid_intel=hyperliquid_intel,
    )
    router = _build_router(
        settings,
        executor=executor,
        dry_run=dry_run,
        usyc_executor=usyc_executor,
        openrouter_client=openrouter_client,
    )

    return _RuntimeContext(
        settings=settings,
        wallet=wallet,
        executor=executor,
        arc_legacy=arc_legacy,
        usyc_executor=usyc_executor,
        dune=dune,
        market_data=market_data,
        onchain=onchain,
        level3=level3,
        openrouter_client=openrouter_client,
        hyperliquid_intel=hyperliquid_intel,
        engine=engine,
        router=router,
        dry_run=dry_run,
        mode=mode,
    )


# ---------------------------------------------------------------------------
# Cycle
# ---------------------------------------------------------------------------


# Terminal-or-synthetic Circle tx states - nothing left to settle.
_SETTLED_TX_STATES = frozenset(
    {"DRY_RUN", "SKIPPED", "FAILED", "DENIED", "CANCELLED",
     "CONFIRMED", "COMPLETE"}
)


async def _settle_circle_tx_results(
    ctx: _RuntimeContext, tx_results: list[Any]
) -> None:
    """Wait for in-flight Circle DCW txs to reach a terminal state.

    Two filters apply:

      1. Terminal-or-synthetic states have nothing to wait for - skip.
      2. Hyperliquid order ids and synthetic ``hl-*`` / ``noop-*`` /
         ``dryrun-*`` markers are NOT Circle txs and must not be
         polled against ``/v1/w3s/transactions/{id}`` (Circle returns
         400). Filtered out by the UUID regex.
    """
    for tx in tx_results:
        if tx.state in _SETTLED_TX_STATES:
            continue
        if not _is_circle_tx_id(tx.tx_id):
            logger.debug(
                "Skipping wait_for_tx on non-Circle id {} "
                "(state={}) - venue settles synchronously.",
                tx.tx_id, tx.state,
            )
            continue
        final = await ctx.wallet.wait_for_tx(tx.tx_id, poll_seconds=3.0)
        link = _explorer_link(ctx.settings, final.tx_hash)
        logger.info(
            "  tx settled | id={} state={} hash={} {}",
            final.tx_id, final.state, final.tx_hash,
            f"explorer={link}" if link else "",
        )
        tx.state = final.state or tx.state
        tx.tx_hash = final.tx_hash or tx.tx_hash


async def run_full_cycle(ctx: _RuntimeContext) -> None:
    """One full L1+L2+L3+Router+PM cycle (the canonical decision tick).

    Mirrors the legacy ``run_once`` body but operates on a pre-built
    :class:`_RuntimeContext` so the long-lived clients survive across
    cycles. The Level 2 connection probe runs at most once per process
    (``ctx.level2_connected``) - subsequent cycles skip the warm-up.
    """
    settings = ctx.settings

    # ── Spinner: covers all network I/O + engine decision ─────────────────
    # console.status() is transient — vanishes before panels appear.
    # loguru is routed through the same console object so no interleaving.
    with console.status("[>>>] Initializing…", spinner="dots", spinner_style="white") as status:
        if not ctx.level2_connected:
            status.update("[>>>] Probing Dune MCP…")
            await ctx.engine.level2.connect()
            ctx.level2_connected = True

        status.update("[>>>] Fetching on-chain snapshot…")
        onchain_snapshot = await ctx.onchain.snapshot(account_id=None)

        status.update("[>>>] Building market context…")
        context = await _build_market_context(
            settings, ctx.executor, ctx.wallet, usyc_executor=ctx.usyc_executor,
        )
        context["onchain"] = onchain_snapshot

        status.update("[>>>] L1 · L2 · L3 deciding…")
        decision = await ctx.engine.decide(context)

    # ── Spinner gone — print all panels ───────────────────────────────────
    console.print(
        market_context_panel(
            context, mode=ctx.mode, app_env=settings.APP_ENV
        )
    )

    l1_raw = decision.level_score(1).raw.get("l1", {}) if decision.level_score(1) else {}
    l2_raw = decision.level_score(2).raw.get("l2", {}) if decision.level_score(2) else {}
    l3_raw = decision.level_score(3).raw.get("l3", {}) if decision.level_score(3) else {}
    console.print(level1_panel(l1_raw, decision.level_score(1).score))
    console.print(level2_panel(l2_raw, decision.level_score(2).score))
    console.print(level3_panel(l3_raw, decision.level_score(3).score if decision.level_score(3) else 0.0))
    console.print(final_decision_panel(decision))

    plan = await ctx.router.route(decision)
    console.print(execution_plan_panel(plan))
    if ctx.router.last_position_review is not None:
        console.print(
            position_review_panel(
                ctx.router.last_position_review,
                config=ctx.router.position_manager.config,
                stats=getattr(ctx.router.position_manager, "stats", None),
            )
        )
    console.print(
        onchain_result_panel(
            plan.tx_results,
            explorer=lambda h: _explorer_link(settings, h),
        )
    )

    if not ctx.dry_run:
        await _settle_circle_tx_results(ctx, plan.tx_results)

    console.print(
        Rule(
            f"[bright_green]CYCLE DONE[/]  "
            f"score={decision.final_score:.3f}  "
            f"action={decision.directive.action.upper()}"
        )
    )


async def run_fast_cycle(ctx: _RuntimeContext) -> None:
    """Lightweight cycle: PositionManager only, no L1/L2/L3, no Dune.

    Runs every ``DECISION_FAST_INTERVAL_SECONDS`` between full cycles
    so adverse moves (SL hit, trailing stop, vol spike, time exit,
    daily-DD) are caught in seconds instead of minutes. The fast cycle
    NEVER touches the venue's native TP/SL trigger orders - those
    were submitted at open time and stay live until the position
    closes (or the operator resizes them via a full cycle).

    Pipeline:
      1. Fresh ``get_account_info`` from Hyperliquid (defeats stale
         AccountInfo).
      2. ``PositionManager.review_open_positions(decision=None,
         directive=None)`` - the PM gracefully degrades when L1/L2
         data is absent: dynamic ATR triggers go silent, fixed-pct
         fall-backs (TAKE_PROFIT_PCT, STOP_LOSS_PCT) carry every
         cycle. Daily-DD, time exit and trailing stop work fine
         without market data.
      3. Execute close / partial_close actions directly via the
         executor (we bypass the AllocationRouter regime dispatch -
         the fast cycle is not allowed to OPEN new positions or
         flip sides; only the full cycle's directive can do that).
    """
    settings = ctx.settings
    pm = ctx.router.position_manager

    console.print(
        Rule(
            f"[bold cyan]CapitalArc fast cycle[/]  mode={ctx.mode}  "
            "PositionManager only (no L1/L2/L3/Dune)"
        )
    )

    account = await ctx.executor.get_account_info()
    review = await pm.review_open_positions(
        account=account,
        decision=None,
        directive=None,
    )
    ctx.router.last_position_review = review

    if review.snapshots:
        console.print(
            position_review_panel(
                review,
                config=pm.config,
                stats=getattr(pm, "stats", None),
            )
        )
    elif review.notes:
        logger.info("Fast cycle: {}", "; ".join(review.notes))

    decision_id = (
        f"fast-{int(datetime.now(timezone.utc).timestamp())}-"
        f"{uuid.uuid4().hex[:6]}"
    )
    fast_tx_results: list[Any] = []
    for action in review.actions:
        if action.action == "close":
            res = await ctx.executor.close_position(
                symbol=action.symbol, decision_id=decision_id,
            )
            fast_tx_results.append(res)
            logger.info(
                "Fast cycle TRIGGER {} on {}/{} | pnl={:.2f}% "
                "size={} -> close (state={} tx_id={})",
                action.trigger.upper(), action.symbol,
                action.side.upper(), action.pnl_pct * 100,
                action.size_usd, res.state, res.tx_id,
            )
        elif action.action == "partial_close":
            # Reuse the router's degradation-aware partial-close
            # helper so legacy / test executors that don't support
            # ``size_usd_to_close`` fall back to a full close with a
            # logged warning instead of failing silently.
            res = await ctx.router._safe_partial_close(
                symbol=action.symbol,
                size_usd_to_close=action.size_usd_to_close,
                decision_id=decision_id,
            )
            fast_tx_results.append(res)
            logger.info(
                "Fast cycle TRIGGER {} on {}/{} | pnl={:.2f}% "
                "partial={} -> partial_close (state={} tx_id={})",
                action.trigger.upper(), action.symbol,
                action.side.upper(), action.pnl_pct * 100,
                action.size_usd_to_close, res.state, res.tx_id,
            )
        # arm_breakeven / tighten_stop / hold are state-only - no
        # on-chain action. Daily-DD breach in the fast cycle would
        # produce a 'close' for every position via the priority ladder,
        # so we don't need a separate flatten path here.

    if fast_tx_results:
        console.print(
            onchain_result_panel(
                fast_tx_results,
                explorer=lambda h: _explorer_link(settings, h),
            )
        )
        if not ctx.dry_run:
            await _settle_circle_tx_results(ctx, fast_tx_results)

    console.print(
        Rule(
            f"[green]fast cycle done[/]  "
            f"actions={sum(1 for a in review.actions if a.action in {'close', 'partial_close'})}  "
            f"open_positions={len(review.snapshots)}"
        )
    )


async def run_once(dry_run: bool) -> None:
    """One full cycle, then tear down all clients. Used by the no-loop path."""
    ctx = await _build_runtime(dry_run=dry_run)
    try:
        await run_full_cycle(ctx)
    finally:
        await ctx.aclose()


# ---------------------------------------------------------------------------
# --test-bias: offline scenario tester for the strong-bias override
# ---------------------------------------------------------------------------


def _explain_decision(
    bias: str,
    strength: float,
    conviction: float,
    final_direction: int,
    direction_strength: float,
    risk_on: float,
    risk_off: float,
    strong_open: float,
    action: str,
    side: str | None,
) -> str:
    """One-line plain-English why for the rendered directive."""
    side_str = (
        "LONG" if final_direction > 0
        else "SHORT" if final_direction < 0
        else "NEUTRAL"
    )
    if conviction >= risk_on and final_direction != 0:
        return (
            f"conv {conviction:.2f} >= risk_on {risk_on:.2f} AND "
            f"direction={side_str}(strength={direction_strength:.2f}) "
            f"-> open {side_str}"
        )
    if conviction <= risk_off:
        return (
            f"conv {conviction:.2f} <= risk_off {risk_off:.2f} -> "
            f"risk-off close (bias {bias} {strength:.2f} ignored)"
        )
    if action == "risk_on":
        return (
            f"MID-BAND ({risk_off:.2f} < {conviction:.2f} < {risk_on:.2f}) "
            f"AND direction_strength {direction_strength:.2f} >= "
            f"strong_bias_open ({strong_open:.2f}) -> STRONG-DIRECTION "
            f"override {side_str}"
        )
    return (
        f"MID-BAND ({risk_off:.2f} < {conviction:.2f} < {risk_on:.2f}) "
        f"AND direction_strength {direction_strength:.2f} < "
        f"strong_bias_open ({strong_open:.2f}) -> HOLD"
    )


def _briefing_from_test_inputs(
    *,
    settings: Settings,
    bias: str,
    strength: float,
    conviction: float,
    direction_sign: int,
    l1_score: LevelScore,
    l2_score: LevelScore,
) -> ArbiterBriefing:
    """Build an ``ArbiterBriefing`` from synthetic ``--test-bias`` inputs.

    The briefing is what the real :class:`Level3` (Gemini) consumes.
    We populate it with values that are *consistent* with the test
    inputs (bias, strength, conviction, direction) so Gemini sees a
    coherent narrative rather than zeros. The per-symbol metrics are
    plausible synthetic numbers shaped by ``direction_sign`` - bearish
    setups carry negative funding / falling OI / distributing whales
    and so on, so Gemini's verdict reflects the requested scenario.
    """
    primary_symbol = (settings.perp_symbols or ["BTC-PERP"])[0]
    # Sign-aware synthetic metrics: positive when bullish, negative when
    # bearish, near-zero when neutral. Keeps the briefing internally
    # consistent so Gemini doesn't see contradictory signals.
    sgn = float(direction_sign)
    funding = 0.00015 * sgn        # +/-0.015% spot-derived funding rate
    oi_1h = 1.5 * sgn              # +/-1.5% OI 1h delta
    oi_24h = 4.0 * sgn             # +/-4% OI 24h delta
    price_24h = 2.5 * sgn          # +/-2.5% 24h price change
    ls_ratio = 1.0 + 0.3 * sgn     # 1.3 bullish, 0.7 bearish
    whale_dir = (
        "accumulating" if direction_sign > 0
        else "distributing" if direction_sign < 0
        else "neutral"
    )
    market_heat = 0.5 + 0.2 * sgn  # 0.7 bull / 0.5 neutral / 0.3 bear

    return ArbiterBriefing(
        primary_symbol=primary_symbol,
        level1_conviction=float(conviction),
        level1_direction_sign=int(direction_sign),
        level1_rationale=l1_score.rationale,
        level1_passes=True,
        level1_raw={
            "passes": True,
            "score": conviction,
            "per_symbol": {
                primary_symbol: {
                    "trend": (
                        "up" if direction_sign > 0
                        else "down" if direction_sign < 0
                        else "flat"
                    ),
                    "passes": True,
                    "atr_pct_avg": 1.2,
                }
            },
        },
        level2_conviction=float(conviction),
        level2_direction_sign=int(direction_sign),
        level2_rationale=l2_score.rationale,
        level2_market_bias=bias,
        level2_bias_strength=float(strength),
        level2_market_heat=float(market_heat),
        level2_regime=(
            "risk_on" if direction_sign > 0
            else "risk_off" if direction_sign < 0
            else "neutral"
        ),
        level2_raw={
            "market_bias": bias,
            "bias_strength": strength,
            "market_heat": market_heat,
            "regime": "risk_on" if direction_sign > 0 else "risk_off" if direction_sign < 0 else "neutral",
            "per_symbol": {
                primary_symbol: {
                    "funding": {
                        "current_rate": funding,
                        "annualised_pct": funding * 3 * 365 * 100,
                        "rate_8h_change": funding * 0.2,
                    },
                    "open_interest": {
                        "current_value_usd": 1_000_000.0,
                        "delta_1h_pct": oi_1h,
                        "delta_4h_pct": oi_1h * 2,
                        "delta_24h_pct": oi_24h,
                    },
                    "volume": {
                        "last_price": 60000.0 if primary_symbol == "BTC-PERP" else 3000.0,
                        "price_change_pct_24h": price_24h,
                        "spike_detected": False,
                    },
                    "long_short": {
                        "long_short_ratio": ls_ratio,
                        "long_account_pct": 0.5 + 0.1 * sgn,
                        "inferred_bias": (
                            "long" if direction_sign > 0
                            else "short" if direction_sign < 0
                            else "balanced"
                        ),
                    },
                    "whales": {
                        "flagged": direction_sign != 0,
                        "direction": whale_dir,
                        "n_whales": 3 if direction_sign != 0 else 0,
                    },
                }
            },
            "vault_flow": {
                "tvl_usdc": 2_500_000.0,
                "net_flow_usdc": 50_000.0 * sgn,
                "window_hours": 24,
            },
        },
        market_snapshot={
            "symbol": primary_symbol,
            "symbols": settings.perp_symbols or ["BTC-PERP", "ETH-PERP"],
            "account_margin_usdc": 1000.0,
            "account_unrealized_pnl_usdc": 0.0,
            "account_drawdown_pct": 0.0,
            "dune_chain": settings.dune_chain,
        },
    )


# ---------------------------------------------------------------------------
# --test-allocation: offline end-to-end demo of the full pipeline
# ---------------------------------------------------------------------------


from src.allocation.allocation_router import ExecutionPlan
from src.execution.arc_perp_executor import AccountInfo, Position
from src.execution.circle_wallet import TxResult
from src.execution.usyc_executor import USYCSnapshot


class _FakeWallet:
    """Minimal stand-in for :class:`CircleWallet` used by --test-allocation.

    Implements just enough of the surface that :class:`ArcPerpExecutor`
    and :class:`USYCExecutor` need to walk the dry-run code path:
    ``send_contract_execution`` always returns a ``DRY_RUN`` TxResult
    and ``_gas_is_sponsored`` reports True so panels can show the
    Paymaster badge.
    """

    async def send_contract_execution(self, req: Any) -> TxResult:  # noqa: ANN401
        action = req.metadata.get("action", "fake") if req.metadata else "fake"
        return TxResult(
            tx_id=f"sim-{action}-{req.decision_id or 'na'}",
            state="DRY_RUN",
            sponsored=True,
            raw={
                "simulated": True,
                "contract": req.contract_address,
                "signature": req.abi_function_signature,
                "params": req.abi_parameters,
            },
        )

    def _gas_is_sponsored(self) -> bool:
        return True

    async def aclose(self) -> None:
        return None

    async def get_address(self) -> str:
        return "0x" + "ab" * 20


class _FakePerpExecutor:
    """Dry-run Hyperliquid-shaped executor for ``--test-allocation``.

    Implements just enough of :class:`PerpExecutorProtocol` for the
    AllocationRouter / PositionManager to run end-to-end against
    deterministic, scriptable account state. ``open_position`` /
    ``close_position`` always return synthetic ``DRY_RUN`` TxResults
    so the demo can finish without any HTTP / SDK calls.
    """

    def __init__(
        self,
        *,
        margin_usd: Decimal,
        pnl_usd: Decimal = Decimal("0"),
        positions: list[Position] | None = None,
    ) -> None:
        # The router only reads ``config.max_leverage`` - we mimic the
        # Hyperliquid shape so the demo reflects the new venue.
        self.config = HyperliquidConfig(
            private_key=None,
            account_address="0x" + "ab" * 20,
            api_url="https://api.hyperliquid-testnet.xyz",
            max_leverage=5,
            max_position_usd=Decimal("10000"),
        )
        self.dry_run = True
        self.account_address = "0x" + "ab" * 20
        self._margin = margin_usd
        self._pnl = pnl_usd
        self._positions = positions or []

    def set_account_address(self, address: str) -> None:
        self.account_address = address

    async def get_account_info(self) -> AccountInfo:
        return AccountInfo(
            equity_usd=self._margin,
            free_margin_usd=self._margin,
            used_margin_usd=Decimal("0"),
            total_unrealized_pnl_usd=self._pnl,
            positions=list(self._positions),
        )

    async def get_margin(self) -> Decimal:
        return self._margin

    async def get_pnl(self, symbol: str | None = None) -> Decimal:
        if symbol is None:
            return self._pnl
        for pos in self._positions:
            if pos.symbol == symbol:
                return pos.unrealized_pnl_usd
        return Decimal("0")

    async def get_position(self, symbol: str) -> Position:
        for pos in self._positions:
            if pos.symbol == symbol:
                return pos
        return Position(
            symbol=symbol,
            side="flat",
            size_usd=Decimal("0"),
            entry_price=Decimal("0"),
            mark_price=Decimal("0"),
            leverage=Decimal("0"),
            unrealized_pnl_usd=Decimal("0"),
        )

    async def get_mid_price(self, symbol: str) -> Decimal | None:
        return Decimal("60000") if symbol.startswith("BTC") else Decimal("3000")

    async def open_position(
        self,
        symbol: str,
        side: str,
        size_usd: Decimal,
        leverage: Decimal | None = None,
        slippage_bps: int | None = None,
        decision_id: str | None = None,
    ) -> TxResult:
        return TxResult(
            tx_id=f"dryrun-hl-open-{decision_id or 'na'}",
            state="DRY_RUN",
            raw={
                "action": "open_position",
                "venue": "hyperliquid_mock",
                "symbol": symbol,
                "side": side,
                "size_usd": str(size_usd),
                "leverage": str(leverage),
            },
        )

    async def close_position(
        self,
        symbol: str,
        slippage_bps: int | None = None,
        decision_id: str | None = None,
    ) -> TxResult:
        return TxResult(
            tx_id=f"dryrun-hl-close-{decision_id or 'na'}",
            state="DRY_RUN",
            raw={
                "action": "close_position",
                "venue": "hyperliquid_mock",
                "symbol": symbol,
            },
        )

    async def close_all_positions(
        self,
        decision_id: str | None = None,
    ) -> list[TxResult]:
        return [
            await self.close_position(symbol=p.symbol, decision_id=decision_id)
            for p in self._positions
            if p.side != "flat"
        ]

    async def withdraw_all_margin(
        self,
        decision_id: str | None = None,
    ) -> TxResult:
        return TxResult(
            tx_id=f"dryrun-hl-withdraw-{decision_id or 'na'}",
            state="DRY_RUN",
            raw={
                "action": "withdraw_all_margin",
                "amount_usd": str(self._margin),
            },
        )

    async def deposit_margin(
        self,
        amount_usd: Decimal,
        decision_id: str | None = None,
    ) -> TxResult:
        return TxResult(
            tx_id=f"dryrun-hl-deposit-{decision_id or 'na'}",
            state="DRY_RUN",
            raw={"action": "deposit_margin", "amount_usd": str(amount_usd)},
        )

    async def update_take_profit_stop_loss(
        self,
        symbol: str,
        tp_price: Decimal | None = None,
        sl_price: Decimal | None = None,
        decision_id: str | None = None,
        *,
        position_side: str | None = None,
        position_size_usd: Decimal | None = None,
    ) -> list[TxResult]:
        out: list[TxResult] = []
        if tp_price is not None:
            out.append(TxResult(
                tx_id=f"dryrun-hl-tp-{decision_id or 'na'}",
                state="DRY_RUN",
                raw={
                    "action": "trigger_tp",
                    "trigger_px": str(tp_price),
                    "position_side": position_side,
                    "position_size_usd": (
                        str(position_size_usd) if position_size_usd else None
                    ),
                },
            ))
        if sl_price is not None:
            out.append(TxResult(
                tx_id=f"dryrun-hl-sl-{decision_id or 'na'}",
                state="DRY_RUN",
                raw={
                    "action": "trigger_sl",
                    "trigger_px": str(sl_price),
                    "position_side": position_side,
                    "position_size_usd": (
                        str(position_size_usd) if position_size_usd else None
                    ),
                },
            ))
        return out


class _FakeUSYCExecutor(USYCExecutor):
    """Dry-run USYC executor for the offline demo."""

    def __init__(
        self,
        *,
        configured: bool,
        usdc_balance: Decimal,
        usyc_balance: Decimal,
        reserve_usd: Decimal,
    ) -> None:
        self.wallet = _FakeWallet()  # type: ignore[assignment]
        self.config = USYCExecutorConfig(
            usyc_token_address=("0x" + "55" * 20) if configured else None,
            usyc_mint_address=("0x" + "66" * 20) if configured else None,
            usdc_address=("0x" + "77" * 20) if configured else None,
            usdc_reserve_usd=reserve_usd,
        )
        self.dry_run = True
        self.account_address = "0x" + "ab" * 20
        self._w3 = None
        self._usdc = usdc_balance
        self._usyc = usyc_balance

    async def get_snapshot(self) -> USYCSnapshot:
        return USYCSnapshot(
            usyc_balance=self._usyc,
            usyc_value_usd=self._usyc,
            usdc_balance=self._usdc,
            configured=self.is_configured,
        )

    async def get_usdc_balance(self) -> Decimal:
        return self._usdc

    async def get_usyc_balance(self) -> Decimal:
        return self._usyc


@dataclass(frozen=True)
class _AllocationScenario:
    """Inputs to a single offline allocation-pipeline test cycle."""

    name: str
    title: str
    bias: str                            # "bullish" | "bearish" | "neutral"
    direction_sign: int                  # +1 long, -1 short, 0 neutral
    conviction: float                    # 0..1
    bias_strength: float                 # 0..1
    margin_usd: Decimal                  # vault margin at decision time
    pnl_usd: Decimal = Decimal("0")
    positions: tuple[tuple[str, str, str], ...] = ()  # (symbol, side, size_usd)
    usyc_configured: bool = True
    usyc_balance: Decimal = Decimal("0")
    usdc_balance: Decimal = Decimal("0")
    stale_data: bool = False             # force all level_scores to 0
    description: str = ""


# Canonical scenarios shipped with the demo. Picked to exercise every
# branch the AllocationRouter can land in.
_ALLOCATION_SCENARIOS: dict[str, _AllocationScenario] = {
    "risk_on_long": _AllocationScenario(
        name="risk_on_long",
        title="RISK-ON LONG (high conviction bull)",
        bias="bullish",
        direction_sign=1,
        conviction=0.78,
        bias_strength=0.82,
        margin_usd=Decimal("1000"),
        usyc_balance=Decimal("500"),
        usdc_balance=Decimal("200"),
        description=(
            "Both L1 (technicals up) and L2 (positive funding, "
            "accumulating whales) confirm bullish setup."
        ),
    ),
    "risk_on_short": _AllocationScenario(
        name="risk_on_short",
        title="RISK-ON SHORT (high conviction bear)",
        bias="bearish",
        direction_sign=-1,
        conviction=0.74,
        bias_strength=0.78,
        margin_usd=Decimal("1000"),
        usyc_balance=Decimal("500"),
        usdc_balance=Decimal("200"),
        description=(
            "Bearish convergence: negative funding, OI falling, "
            "whales distributing. Symmetric short open."
        ),
    ),
    "risk_off": _AllocationScenario(
        name="risk_off",
        title="RISK-OFF (low conviction -> close + USYC mint)",
        bias="neutral",
        direction_sign=0,
        conviction=0.22,
        bias_strength=0.15,
        margin_usd=Decimal("800"),
        positions=(("BTC-PERP", "long", "600"),),
        usyc_balance=Decimal("100"),
        usdc_balance=Decimal("50"),
        description=(
            "Signals dispersed, no clean edge. Close perp -> "
            "withdraw margin -> rotate freed USDC into USYC."
        ),
    ),
    "mid_band_short_override": _AllocationScenario(
        name="mid_band_short_override",
        title="MID-BAND STRONG-DIRECTION (reduced-size SHORT)",
        bias="bearish",
        direction_sign=-1,
        conviction=0.52,
        bias_strength=0.86,
        margin_usd=Decimal("1000"),
        usyc_balance=Decimal("0"),
        usdc_balance=Decimal("300"),
        description=(
            "Conviction sits in the mid-band but the directional "
            "vote is decisive (strength=0.86) -> reduced-size short."
        ),
    ),
    "hold": _AllocationScenario(
        name="hold",
        title="HOLD (mid-band, no directional edge)",
        bias="neutral",
        direction_sign=0,
        conviction=0.50,
        bias_strength=0.10,
        margin_usd=Decimal("1000"),
        description=(
            "Engine sees no edge in either direction; stays in "
            "current allocation without touching the venue."
        ),
    ),
    "drawdown": _AllocationScenario(
        name="drawdown",
        title="DRAWDOWN BREACH (forced risk-off)",
        bias="bullish",
        direction_sign=1,
        conviction=0.85,
        bias_strength=0.90,
        margin_usd=Decimal("1000"),
        # -12% drawdown breaches MAX_DRAWDOWN_PCT=10 even though
        # the L3 verdict screams "open long".
        pnl_usd=Decimal("-120"),
        positions=(("BTC-PERP", "long", "500"),),
        usyc_balance=Decimal("0"),
        usdc_balance=Decimal("50"),
        description=(
            "Drawdown > MAX_DRAWDOWN_PCT. Hard override fires "
            "regardless of what L1/L2/L3 recommend."
        ),
    ),
    "stale": _AllocationScenario(
        name="stale",
        title="STALE DATA (router denies the cycle)",
        bias="neutral",
        direction_sign=0,
        conviction=0.0,
        bias_strength=0.0,
        margin_usd=Decimal("1000"),
        stale_data=True,
        description=(
            "All level scores == 0 and no short-circuit was set - "
            "the upstream data feed is broken. Router refuses to trade."
        ),
    ),
    "take_profit": _AllocationScenario(
        name="take_profit",
        title="TAKE-PROFIT (PositionManager pre-empts regime)",
        bias="bullish",
        direction_sign=1,
        conviction=0.78,
        bias_strength=0.82,
        margin_usd=Decimal("1000"),
        pnl_usd=Decimal("25"),
        positions=(("BTC-PERP", "long", "600"),),
        usyc_balance=Decimal("0"),
        usdc_balance=Decimal("200"),
        description=(
            "Open LONG sitting at +4.17% unrealised - above the "
            "+3% TAKE_PROFIT_PCT. PositionManager closes the "
            "position before the regime routing runs, even though "
            "the engine still says risk-on."
        ),
    ),
    "stop_loss": _AllocationScenario(
        name="stop_loss",
        title="STOP-LOSS (manager closes losing position)",
        bias="bullish",
        direction_sign=1,
        conviction=0.72,
        bias_strength=0.65,
        margin_usd=Decimal("1000"),
        pnl_usd=Decimal("-18"),
        positions=(("BTC-PERP", "long", "600"),),
        usyc_balance=Decimal("0"),
        usdc_balance=Decimal("200"),
        description=(
            "Open LONG at -3.0% unrealised - through the -2% "
            "STOP_LOSS_PCT but well under MAX_DRAWDOWN_PCT. The "
            "PositionManager flattens the position; the engine's "
            "risk-on directive is overridden by the per-position "
            "stewardship rule."
        ),
    ),
    "side_flip": _AllocationScenario(
        name="side_flip",
        title="SIDE FLIP (close LONG + open SHORT same cycle)",
        bias="bearish",
        direction_sign=-1,
        conviction=0.80,
        bias_strength=0.85,
        margin_usd=Decimal("1000"),
        pnl_usd=Decimal("0"),
        positions=(("BTC-PERP", "long", "500"),),
        usyc_balance=Decimal("0"),
        usdc_balance=Decimal("300"),
        description=(
            "Existing LONG meets a new SHORT directive. With "
            "AUTO_FLIP_ON_SIDE_CHANGE=true the manager closes the "
            "long and the router then opens a fresh short on the "
            "same cycle (full sizing + USYC-redeem + native TP/SL)."
        ),
    ),
    "re_evaluation": _AllocationScenario(
        name="re_evaluation",
        title="RE-EVALUATION (lock-in modest profit)",
        bias="neutral",
        direction_sign=0,
        conviction=0.42,
        bias_strength=0.20,
        margin_usd=Decimal("1000"),
        pnl_usd=Decimal("8"),
        positions=(("BTC-PERP", "long", "600"),),
        usyc_balance=Decimal("100"),
        usdc_balance=Decimal("150"),
        description=(
            "Position is +1.33% (below TP, above the 0.5% "
            "re-eval profit floor) and conviction has fallen to "
            "0.42 - sitting in the mid-band HOLD but below "
            "MIN_CONVICTION_TO_HOLD=0.45. The manager locks in "
            "the modest gain instead of letting it drift back "
            "to break-even."
        ),
    ),
}


def _build_decision_from_scenario(
    settings: Settings, sc: _AllocationScenario
) -> DecisionResult:
    """Construct a synthetic :class:`DecisionResult` for a scenario.

    Mirrors what the live engine would emit: L1 carries ATR% data for
    the sizing pipeline, L2 carries market_bias / heat, and L3 carries
    a synthetic Claude-shaped raw payload so the panels look identical
    whether the LLM is live or not.
    """
    from src.core.decision_engine import DecisionResult, ExecutionDirective, LevelScore

    primary_symbol = (settings.perp_symbols or ["BTC-PERP"])[0]

    # ---- Build per-level scores --------------------------------------
    if sc.stale_data:
        scores = [
            LevelScore(level=1, score=0.0, raw={"l1": {}}, direction_sign=0),
            LevelScore(level=2, score=0.0, raw={"l2": {}}, direction_sign=0),
            LevelScore(level=3, score=0.0, raw={"l3": {}}, direction_sign=0),
        ]
    else:
        # L1 raw carries the per-symbol ATR% so the sizing pipeline can
        # solve for a realistic vol-target size in the demo.
        l1_raw = {
            "passes": True,
            "score": sc.conviction,
            "per_symbol": {
                primary_symbol: {
                    "trend": "up" if sc.direction_sign > 0 else "down" if sc.direction_sign < 0 else "flat",
                    "passes": True,
                    "atr_pct_avg": 1.2,
                }
            },
        }
        l2_raw = {
            "market_bias": sc.bias,
            "bias_strength": sc.bias_strength,
            "market_heat": 0.5 + 0.2 * sc.direction_sign,
            "regime": (
                "risk_on" if sc.direction_sign > 0
                else "risk_off" if sc.direction_sign < 0
                else "neutral"
            ),
            "conviction": sc.conviction,
        }
        l3_raw = {
            "provider": "synthetic",
            "model": None,
            "mode": settings.L3_MODE,
            "synthetic": True,
            "fallback": False,
            "latency_ms": 0.0,
            "response": {
                "conviction": sc.conviction,
                "direction": (
                    "long" if sc.direction_sign > 0
                    else "short" if sc.direction_sign < 0
                    else "neutral"
                ),
                "regime": (
                    "risk_on" if sc.direction_sign != 0 and sc.conviction >= 0.6
                    else "risk_off" if sc.conviction <= 0.4
                    else "hold"
                ),
                "recommended_intensity": sc.conviction,
                "rationale": (
                    f"Offline --test-allocation synthetic verdict for "
                    f"'{sc.name}'. {sc.description}"
                ),
                "key_factors": [
                    f"synthetic scenario: {sc.name}",
                    f"bias={sc.bias} (strength={sc.bias_strength:.2f})",
                    f"conviction={sc.conviction:.2f}",
                ],
            },
        }
        scores = [
            LevelScore(
                level=1,
                score=sc.conviction,
                rationale="synthetic L1 (--test-allocation)",
                raw={"l1": l1_raw},
                direction_sign=sc.direction_sign,
            ),
            LevelScore(
                level=2,
                score=sc.conviction,
                rationale=f"synthetic L2 bias={sc.bias} strength={sc.bias_strength:.2f}",
                raw={"l2": l2_raw},
                direction_sign=sc.direction_sign,
            ),
            LevelScore(
                level=3,
                score=sc.conviction,
                rationale=l3_raw["response"]["rationale"],
                raw={"l3": l3_raw},
                direction_sign=sc.direction_sign,
            ),
        ]

    # ---- Engine-equivalent aggregation -------------------------------
    engine = DecisionEngine(
        level1=None,  # type: ignore[arg-type]
        level2=None,  # type: ignore[arg-type]
        level3=None,
        weights=settings.level_weights,
        risk_on_threshold=settings.RISK_ON_THRESHOLD,
        risk_off_threshold=settings.RISK_OFF_THRESHOLD,
        short_bias_min_strength=settings.SHORT_BIAS_MIN_STRENGTH,
        strong_bias_open_strength=settings.STRONG_BIAS_OPEN_STRENGTH,
        strong_direction_l1_corroboration_min=float(
            getattr(settings, "STRONG_DIRECTION_L1_CORROBORATION_MIN", 0.40)
        ),
        redistribute_synthetic_l3_weight=settings.REDISTRIBUTE_SYNTHETIC_L3_WEIGHT,
    )
    effective = engine._effective_weights(scores)
    final_score = engine._aggregate(scores, effective)
    final_direction, dir_strength = engine._aggregate_direction(scores, effective)
    directive = engine._build_directive(
        final_score, final_direction, dir_strength, scores
    )

    return DecisionResult(
        final_score=final_score,
        regime=directive.action.replace("_", "-"),
        directive=directive,
        level_scores=scores,
        weights=engine.weights,
        final_direction=final_direction,
        direction_strength=dir_strength,
        effective_weights=effective,
        short_circuited=False,
        short_circuit_reason=None,
    )


async def run_test_allocation(scenario_name: str) -> None:
    """End-to-end offline demo of the full Risk-On / Risk-Off pipeline.

    Synthesises Level 1, 2 and 3 scores, builds a real ``DecisionResult``
    via the actual engine helpers (no shortcuts on the conviction /
    direction math), then runs the **real** :class:`AllocationRouter`
    against dry-run mock executors. Renders the same console panels
    a live cycle would produce so the demo answers "what does the
    agent actually do here?" without touching Dune, Circle or
    OpenRouter.

    Parameters
    ----------
    scenario_name :
        Key into :data:`_ALLOCATION_SCENARIOS`. Run
        ``python main.py --test-allocation list`` to see every
        scenario shipped.
    """
    settings = get_settings()
    configure_logging(settings.LOG_LEVEL, rich_console=console)

    if scenario_name == "list":
        _print_allocation_scenarios()
        return
    if scenario_name not in _ALLOCATION_SCENARIOS:
        raise SystemExit(
            f"Unknown --test-allocation scenario {scenario_name!r}. "
            f"Available: {', '.join(_ALLOCATION_SCENARIOS)}. "
            "Pass 'list' to see descriptions."
        )

    # Even though --test-allocation runs against a fake executor, log
    # the REAL HyperliquidConfig that --dry-run / --live would use so
    # operators can visually confirm their .env is wired correctly
    # without having to touch Dune / OpenRouter.
    _log_hyperliquid_config(
        HyperliquidConfig(
            private_key=settings.HYPERLIQUID_PRIVATE_KEY,
            account_address=settings.HYPERLIQUID_ACCOUNT_ADDRESS or None,
            api_url=settings.HYPERLIQUID_API_URL,
            vault_address=settings.HYPERLIQUID_VAULT_ADDRESS or None,
            max_leverage=settings.HYPERLIQUID_MAX_LEVERAGE,
            max_position_usd=Decimal(str(settings.HYPERLIQUID_MAX_POSITION_USD)),
        )
    )

    sc = _ALLOCATION_SCENARIOS[scenario_name]
    console.print(
        Rule(
            f"[bold cyan]CapitalArc --test-allocation[/]  scenario=[bold]{sc.name}[/]",
            style="cyan",
        )
    )
    console.print(
        Panel(
            Text(sc.description, style="white"),
            title=f"[bold]{sc.title}[/]",
            border_style="cyan",
            box=box.ROUNDED,
        )
    )

    # ---- Synthesise the decision -------------------------------------
    decision = _build_decision_from_scenario(settings, sc)

    # ---- Render the three engine-level panels ------------------------
    l1_raw = decision.level_score(1).raw.get("l1", {}) if decision.level_score(1) else {}
    l2_raw = decision.level_score(2).raw.get("l2", {}) if decision.level_score(2) else {}
    l3_raw = decision.level_score(3).raw.get("l3", {}) if decision.level_score(3) else {}

    # ---- Mock executors and the real router --------------------------
    positions = [
        Position(
            symbol=sym,
            side=side,
            size_usd=Decimal(size),
            entry_price=Decimal("60000") if sym.startswith("BTC") else Decimal("3000"),
            mark_price=Decimal("60000") if sym.startswith("BTC") else Decimal("3000"),
            leverage=Decimal("3"),
            unrealized_pnl_usd=sc.pnl_usd,
        )
        for sym, side, size in sc.positions
    ]
    perp = _FakePerpExecutor(
        margin_usd=sc.margin_usd, pnl_usd=sc.pnl_usd, positions=positions
    )
    usyc = _FakeUSYCExecutor(
        configured=sc.usyc_configured,
        usdc_balance=sc.usdc_balance,
        usyc_balance=sc.usyc_balance,
        reserve_usd=Decimal(str(settings.USYC_USDC_RESERVE_USD)),
    )
    router = _build_router(
        settings, executor=perp, dry_run=True, usyc_executor=usyc
    )

    # ---- Build a synthetic Market Context for the panel --------------
    usyc_snap = await usyc.get_snapshot()
    context = {
        "symbols": settings.perp_symbols,
        "symbol": (settings.perp_symbols or ["BTC-PERP"])[0],
        "rpc_url": settings.ARC_RPC_URL,
        "account_address": "0x" + "ab" * 20,
        "account_id": "0x" + "0" * 24 + "ab" * 20,
        "account_margin_usdc": float(sc.margin_usd),
        "account_unrealized_pnl_usdc": float(sc.pnl_usd),
        "account_drawdown_pct": (
            float(-sc.pnl_usd / sc.margin_usd * 100)
            if sc.margin_usd > 0 and sc.pnl_usd < 0
            else 0.0
        ),
        "l1_timeframes": settings.l1_timeframes,
        "dune_chain": settings.dune_chain,
        "dune_tokens": settings.dune_token_addresses,
        "usyc": usyc_snap,
    }
    console.print(
        market_context_panel(context, mode="OFFLINE", app_env=settings.APP_ENV)
    )
    console.print(level1_panel(l1_raw, decision.level_score(1).score))
    console.print(level2_panel(l2_raw, decision.level_score(2).score))
    console.print(
        level3_panel(
            l3_raw,
            decision.level_score(3).score if decision.level_score(3) else 0.0,
        )
    )
    console.print(final_decision_panel(decision))

    # ---- Run the REAL router on the synthetic decision ---------------
    plan: ExecutionPlan = await router.route(decision)
    console.print(execution_plan_panel(plan))
    if router.last_position_review is not None:
        console.print(
            position_review_panel(
                router.last_position_review,
                config=router.position_manager.config,
                stats=getattr(router.position_manager, "stats", None),
            )
        )
    console.print(
        onchain_result_panel(
            plan.tx_results,
            explorer=lambda h: _explorer_link(settings, h),
        )
    )

    console.print(
        Rule(
            f"[green]--test-allocation done[/]  "
            f"action=[bold]{plan.action}[/]  "
            f"tx={len(plan.tx_results)}  "
            f"rotation_legs={len(plan.rotation_legs)}",
            style="green",
        )
    )


def _print_allocation_scenarios() -> None:
    """Pretty-print the catalogue of canned --test-allocation scenarios."""
    table = Table(
        title="--test-allocation scenarios",
        box=box.ROUNDED,
        expand=True,
    )
    table.add_column("Name", style="bold cyan")
    table.add_column("Title", style="white")
    table.add_column("Conv / Bias", style="dim")
    table.add_column("Description")
    for name, sc in _ALLOCATION_SCENARIOS.items():
        table.add_row(
            name,
            sc.title,
            f"conv={sc.conviction:.2f}  bias={sc.bias}({sc.bias_strength:.2f})",
            sc.description,
        )
    console.print(table)


async def run_test_bias(
    bias: str,
    strength: float,
    conviction: float,
    *,
    real_sonnet: bool = False,
) -> None:
    """Offline scenario test for the conviction/direction engine.

    Synthesises L1, L2 and L3 ``LevelScore``s with the requested
    ``conviction`` + ``direction`` (derived from ``bias``) and exercises
    the real ``_aggregate / _aggregate_direction / _build_directive``
    pipeline. Skips Dune, Circle and the AllocationRouter so the test
    stays fast and offline.

    Parameters
    ----------
    bias
        ``"bearish" | "bullish" | "neutral"`` - the L2 directional vote.
    strength
        L2 ``bias_strength`` in ``[0, 1]``.
    conviction
        Per-level synthetic conviction in ``[0, 1]``. With L3
        redistribution, the aggregate conviction equals this value.
    real_sonnet
        When ``True``, replace the synthetic L3 placeholder with a
        real round-trip via :class:`Level3` (Claude Sonnet 4.6 via
        OpenRouter by default; swap upstream model with
        ``OPENROUTER_MODEL``). Requires ``OPENROUTER_API_KEY`` in
        ``.env``; raises ``SystemExit`` otherwise. When ``False``
        (default) the test runs offline - useful for fast iteration
        on thresholds.
    """
    settings = get_settings()
    configure_logging(settings.LOG_LEVEL, rich_console=console)

    bias = bias.lower().strip()
    if bias not in {"bearish", "bullish", "neutral"}:
        raise SystemExit(
            f"--test-bias must be bearish|bullish|neutral, got {bias!r}"
        )
    strength = max(0.0, min(1.0, float(strength)))
    conviction = max(0.0, min(1.0, float(conviction)))
    direction_sign = (
        1 if bias == "bullish" else -1 if bias == "bearish" else 0
    )

    engine = DecisionEngine(
        level1=None,  # type: ignore[arg-type]
        level2=None,  # type: ignore[arg-type]
        level3=None,
        weights=settings.level_weights,
        risk_on_threshold=settings.RISK_ON_THRESHOLD,
        risk_off_threshold=settings.RISK_OFF_THRESHOLD,
        short_bias_min_strength=settings.SHORT_BIAS_MIN_STRENGTH,
        strong_bias_open_strength=settings.STRONG_BIAS_OPEN_STRENGTH,
        strong_direction_l1_corroboration_min=float(
            getattr(settings, "STRONG_DIRECTION_L1_CORROBORATION_MIN", 0.40)
        ),
        redistribute_synthetic_l3_weight=settings.REDISTRIBUTE_SYNTHETIC_L3_WEIGHT,
    )

    # Synthetic LevelScores: L1 + L2 carry the same conviction +
    # direction so the test probes the score-classification branch
    # cleanly. L3 starts as the synthetic placeholder (so its weight
    # is redistributed and the aggregate conviction == input
    # conviction); when `--real-sonnet` is passed we overwrite the
    # third element below with the actual Claude verdict.
    l1_score = LevelScore(
        level=1,
        score=conviction,
        rationale="synthetic L1 (test)",
        direction_sign=direction_sign,
    )
    l2_score = LevelScore(
        level=2,
        score=conviction,
        rationale=f"synthetic L2 bias={bias} strength={strength:.2f}",
        raw={
            "l2": {
                "market_bias": bias,
                "bias_strength": strength,
                "conviction": conviction,
            }
        },
        direction_sign=direction_sign,
    )
    l3_score: LevelScore = LevelScore(
        level=3,
        score=conviction,
        rationale="synthetic L3 (test)",
        raw={
            "l3": {
                "synthetic": True,
                "mode": settings.L3_MODE,
                "provider": "synthetic",
                "model": None,
            }
        },
        direction_sign=direction_sign,
    )

    # ---- --real-sonnet: replace synthetic L3 with a real LLM call --
    # Routes through OpenRouter to the model named by OPENROUTER_MODEL
    # (default: anthropic/claude-sonnet-4.6).
    openrouter_client_for_cleanup: OpenRouterClient | None = None
    if real_sonnet:
        level3, openrouter_client_for_cleanup = _build_level3(settings)
        if level3 is None:
            raise SystemExit(
                "--real-sonnet requires OPENROUTER_API_KEY to be set in "
                ".env (no key found; the synthetic placeholder would "
                "have been used). Set OPENROUTER_API_KEY and retry. "
                "Get a key at https://openrouter.ai/keys"
            )
        briefing = _briefing_from_test_inputs(
            settings=settings,
            bias=bias,
            strength=strength,
            conviction=conviction,
            direction_sign=direction_sign,
            l1_score=l1_score,
            l2_score=l2_score,
        )
        logger.info(
            "--real-sonnet: calling OpenRouter ({}) with synthetic "
            "briefing (bias={}, strength={:.2f}, conviction={:.2f})...",
            settings.OPENROUTER_MODEL, bias, strength, conviction,
        )
        try:
            l3_score = await level3.score(briefing)
        finally:
            # Close the OpenRouter client's underlying httpx pool to
            # avoid "unclosed connector" warnings on process exit.
            if openrouter_client_for_cleanup is not None:
                await openrouter_client_for_cleanup.aclose()

    scores = [l1_score, l2_score, l3_score]

    effective = engine._effective_weights(scores)
    final_score = engine._aggregate(scores, effective)
    final_direction, direction_strength = engine._aggregate_direction(
        scores, effective
    )
    directive = engine._build_directive(
        final_score, final_direction, direction_strength, scores
    )

    # ---- Inputs table -------------------------------------------------
    inputs = Table.grid(padding=(0, 2))
    inputs.add_column(style="dim")
    inputs.add_column(style="bold")
    inputs.add_row("market_bias", f"{bias} (strength={strength:.2f})")
    inputs.add_row("per-level conviction", f"{conviction:.3f}")
    inputs.add_row(
        "thresholds",
        f"risk_off={settings.RISK_OFF_THRESHOLD:.2f}  "
        f"risk_on={settings.RISK_ON_THRESHOLD:.2f}  "
        f"strong_bias_open={settings.STRONG_BIAS_OPEN_STRENGTH:.2f}  "
        f"short_bias_min={settings.SHORT_BIAS_MIN_STRENGTH:.2f}",
    )
    inputs.add_row(
        "weights (configured)",
        " ".join(f"{k}={v:.2f}" for k, v in settings.level_weights.items()),
    )
    inputs.add_row(
        "weights (effective)",
        " ".join(f"{k}={v:.2f}" for k, v in effective.items()),
    )

    # ---- Decision table ----------------------------------------------
    side_str = (directive.side or "-").upper()
    action_colour = {
        "risk_on": "green",
        "risk_off": "red",
        "hold": "yellow",
    }.get(directive.action, "white")
    decision = Table.grid(padding=(0, 2))
    decision.add_column(style="dim")
    decision.add_column()
    decision.add_row(
        "action",
        f"[bold {action_colour}]{directive.action.upper()}[/]",
    )
    decision.add_row("side", f"[bold]{side_str}[/]")
    dir_label = (
        "LONG" if final_direction > 0
        else "SHORT" if final_direction < 0
        else "NEUTRAL"
    )
    decision.add_row(
        "aggregate direction",
        f"{dir_label} (strength={direction_strength:.3f})",
    )
    decision.add_row("final_score (conviction)", f"{final_score:.3f}")
    decision.add_row("intensity", f"{directive.intensity:.3f}")
    decision.add_row("rationale", directive.rationale)

    why = _explain_decision(
        bias=bias,
        strength=strength,
        conviction=final_score,
        final_direction=final_direction,
        direction_strength=direction_strength,
        risk_on=settings.RISK_ON_THRESHOLD,
        risk_off=settings.RISK_OFF_THRESHOLD,
        strong_open=settings.STRONG_BIAS_OPEN_STRENGTH,
        action=directive.action,
        side=directive.side,
    )
    decision.add_row("why", f"[italic]{why}[/]")

    console.print(
        Panel(
            inputs,
            title="[bold]--test-bias INPUTS[/]",
            border_style="cyan",
            box=box.ROUNDED,
        )
    )
    # ---- Level 3 (Claude via OpenRouter) panel: real verdict / synthetic
    l3_raw = l3_score.raw.get("l3", {}) or {}
    l3_kind = "real" if real_sonnet else "synthetic"
    l3_persona = str(l3_raw.get("mode") or settings.L3_MODE)
    console.print(
        Rule(
            f"[bold]Level 3 - Claude Final Arbiter "
            f"({l3_kind}, mode={l3_persona.upper()})[/]",
            style="magenta" if real_sonnet else "yellow",
        )
    )
    console.print(level3_panel(l3_raw, l3_score.score))
    console.print(
        Panel(
            decision,
            title="[bold]--test-bias DECISION[/]",
            border_style=action_colour,
            box=box.ROUNDED,
        )
    )


# ---------------------------------------------------------------------------
# Loop
# ---------------------------------------------------------------------------


def _format_interval(seconds: int) -> str:
    """Render a sleep interval as a human-friendly phrase.

    Examples: 600 -> "10 minutes", 60 -> "1 minute", 3600 -> "1 hour",
    30 -> "30 seconds", 0 -> "0 seconds (back-to-back)".

    Used to colour the loop-mode startup banner so the operator can
    immediately see how often the cycle will fire without doing the
    arithmetic from seconds in their head.
    """
    if seconds <= 0:
        return "0 seconds (back-to-back)"
    if seconds % 3600 == 0:
        hours = seconds // 3600
        return f"{hours} hour" if hours == 1 else f"{hours} hours"
    if seconds % 60 == 0:
        minutes = seconds // 60
        return f"{minutes} minute" if minutes == 1 else f"{minutes} minutes"
    return f"{seconds} second" if seconds == 1 else f"{seconds} seconds"


async def run_loop(dry_run: bool) -> None:
    """Run the decision cycle continuously, sleeping between iterations.

    Two-speed scheduler:

    * **Full cycle** every ``DECISION_INTERVAL_SECONDS`` (default 600 s
      / 10 min) - L1 + L2 + L3 + Router + PositionManager. Bounds
      OpenRouter (Sonnet 4.6) and Dune MCP costs while still tracking
      the 15m/1h timeframes the L1 cascade uses.
    * **Fast cycle** every ``DECISION_FAST_INTERVAL_SECONDS`` (default
      120 s / 2 min) - PositionManager Fast Path only. Catches
      adverse moves (SL hit, trailing stop, vol spike, time exit,
      daily-DD) in seconds instead of minutes. NO Dune / OpenRouter
      calls. Native TP/SL trigger orders submitted on the venue at
      open time stay live the entire time and provide the on-venue
      safety net.

    Both intervals share a single :class:`_RuntimeContext` so the
    PositionManager's per-position state (``breakeven_armed``,
    ``partial_tp_done``, ``peak_pnl_pct``, ``last_l3_check_at``,
    ``opened_at``) survives across cycles. Setting
    ``DECISION_FAST_INTERVAL_SECONDS=0`` disables the fast cycle
    entirely and the loop reverts to single-speed full-cycle ticks.

    Errors inside an individual cycle are caught and logged - the loop
    continues so a transient Dune / OpenRouter / RPC blip never kills
    the whole agent. Ctrl-C remains the only clean exit path.
    """
    settings = get_settings()
    full_interval = max(0, settings.DECISION_INTERVAL_SECONDS)
    fast_interval = max(0, settings.DECISION_FAST_INTERVAL_SECONDS)
    mode_label = "LIVE" if not dry_run else "DRY-RUN"
    if fast_interval > 0 and (full_interval == 0 or fast_interval < full_interval):
        logger.info(
            "Running in loop mode ({}) - FULL cycle every {}, "
            "FAST cycle every {}. Ctrl-C to stop.",
            mode_label,
            _format_interval(full_interval),
            _format_interval(fast_interval),
        )
    else:
        # Fast cycle disabled (=0) or misconfigured (>= full). Fall
        # back to single-speed loop on the full interval.
        if fast_interval > 0:
            logger.warning(
                "DECISION_FAST_INTERVAL_SECONDS ({}) >= "
                "DECISION_INTERVAL_SECONDS ({}); fast cycle disabled.",
                fast_interval, full_interval,
            )
        fast_interval = 0
        logger.info(
            "Running in loop mode ({}) - decision cycle every {}. "
            "Ctrl-C to stop.",
            mode_label,
            _format_interval(full_interval),
        )

    ctx = await _build_runtime(dry_run=dry_run)
    last_full_at: float = 0.0  # epoch seconds; 0 forces a full cycle first
    try:
        while True:
            now = time.monotonic()
            should_run_full = (
                full_interval == 0
                or last_full_at == 0.0
                or (now - last_full_at) >= full_interval
            )
            try:
                if should_run_full:
                    await run_full_cycle(ctx)
                    last_full_at = time.monotonic()
                else:
                    await run_fast_cycle(ctx)
            except Exception as exc:  # noqa: BLE001 - top-level guard
                logger.exception("Decision cycle failed: {}", exc)

            # Sleep for the smaller cadence (fast when enabled, else
            # full). DECISION_INTERVAL_SECONDS=0 is the "back-to-back"
            # replay mode and skips the sleep entirely.
            sleep_for = (
                fast_interval if fast_interval > 0 else full_interval
            )
            if sleep_for > 0:
                with console.status(
                    f"[>>>] Next cycle in {_format_interval(sleep_for)}…",
                    spinner="dots",
                    spinner_style="dim white",
                ):
                    await asyncio.sleep(sleep_for)
    finally:
        await ctx.aclose()


def main() -> None:
    load_dotenv()
    parser = argparse.ArgumentParser(description="CapitalArc agent runner")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="Do not submit any on-chain transactions (default).",
    )
    mode.add_argument(
        "--live",
        action="store_true",
        help="Submit real transactions via Circle DCW. Requires full env.",
    )
    parser.add_argument(
        "--loop",
        action="store_true",
        help="Run continuously every DECISION_INTERVAL_SECONDS.",
    )
    parser.add_argument(
        "--test-bias",
        choices=["bearish", "bullish", "neutral"],
        default=None,
        help=(
            "Offline scenario test for the strong-bias override. "
            "Forces market_bias + final_score and prints the directive the "
            "engine would emit. Skips Dune, Circle and the router."
        ),
    )
    parser.add_argument(
        "--test-allocation",
        choices=sorted(list(_ALLOCATION_SCENARIOS) + ["list"]),
        default=None,
        help=(
            "End-to-end offline demo of the full Risk-On / Risk-Off + USYC "
            "pipeline. Synthesises L1/L2/L3 scores for a canned scenario, "
            "runs the REAL AllocationRouter against dry-run mock executors, "
            "and renders every console panel (Market Context, L1, L2, L3, "
            "Final Decision, Execution Plan with rotation legs, On-chain "
            "Result). Pass 'list' to see the scenario catalogue."
        ),
    )
    parser.add_argument(
        "--test-bias-strength",
        type=float,
        default=0.86,
        help="bias_strength for --test-bias (0..1, default 0.86).",
    )
    parser.add_argument(
        "--test-conviction",
        "--test-final-score",
        dest="test_conviction",
        type=float,
        default=0.52,
        help=(
            "Synthetic per-level conviction for --test-bias (0..1, "
            "default 0.52 = mid-band so the strong-direction override "
            "is the branch under test). With L3 redistribution, the "
            "aggregate conviction equals this value."
        ),
    )
    parser.add_argument(
        "--real-sonnet",
        "--real-gemini",  # legacy alias; kept silently for muscle memory
        dest="real_sonnet",
        action="store_true",
        default=False,
        help=(
            "Use the real Level 3 arbiter (Claude Sonnet 4.6 via "
            "OpenRouter) for --test-bias instead of the synthetic "
            "placeholder. Requires OPENROUTER_API_KEY in .env. Default "
            "off - the synthetic L3 runs offline and instantly. Turn on "
            "to exercise the actual LLM round-trip end-to-end with a "
            "coherent synthetic briefing. (`--real-gemini` is accepted "
            "as a deprecated alias from the pre-OpenRouter days.)"
        ),
    )
    args = parser.parse_args()

    if args.test_allocation is not None:
        asyncio.run(run_test_allocation(args.test_allocation))
        return

    if args.test_bias is not None:
        asyncio.run(
            run_test_bias(
                bias=args.test_bias,
                strength=args.test_bias_strength,
                conviction=args.test_conviction,
                real_sonnet=args.real_sonnet,
            )
        )
        return

    dry_run = not args.live

    if args.loop:
        asyncio.run(run_loop(dry_run=dry_run))
    else:
        asyncio.run(run_once(dry_run=dry_run))


if __name__ == "__main__":
    main()
