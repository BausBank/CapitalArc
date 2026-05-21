"""CapitalArc - main entry point.

Wires the full pipeline:

    Level 1 (Dune MCP OHLCV) + Level 2 (Dune MCP on-chain) [+ Level 3]
        -> DecisionEngine (cascade L1 -> L2 -> L3)
            -> AllocationRouter
                -> ArcPerpExecutor + CircleWallet (Arc Perp DEX, Paymaster)

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
import os
import sys
from decimal import Decimal
from typing import Any

from dotenv import load_dotenv
from rich.console import Console
from rich.rule import Rule


def _force_utf8_console() -> None:
    """Force the Windows console into UTF-8 + VT100 mode.

    The clean-mode CLI uses emojis (\U0001f4c8 \U0001f4c9 \u26a0\ufe0f \u2713 \u2717)
    and box-drawing glyphs that crash on the legacy code page (cp1251 here).
    `os.system("")` is the canonical no-op that enables ANSI escape
    parsing on Windows 10+, after which `sys.stdout.reconfigure` swaps
    the encoding to UTF-8 so the emojis render natively. Wrapped in
    a best-effort try/except so non-Windows runtimes are untouched.
    """
    if sys.platform == "win32":
        try:
            os.system("")
        except Exception:  # noqa: BLE001
            pass
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except (AttributeError, OSError, ValueError):
            pass


_force_utf8_console()

from src.allocation.allocation_router import AllocationConfig, AllocationRouter
from src.core.decision_engine import DecisionEngine, DecisionResult, LevelScore
from src.core.level1 import Level1, Level1Config
from src.core.level2 import Level2, Level2Config
from src.data.arc_onchain import ArcOnchainConfig, ArcOnchainReader
from src.data.dune_market_data import DuneMarketData, DuneMarketDataConfig
from src.data.dune_mcp import DuneMCPClient, DuneMCPClientConfig
from src.execution.arc_perp_executor import ArcPerpConfig, ArcPerpExecutor
from src.execution.circle_wallet import CircleWallet, CircleWalletConfig
from src.utils.config import Settings, get_settings
from src.utils.console import (
    execution_plan_panel,
    final_decision_panel,
    level1_panel,
    level2_panel,
    market_context_panel,
    metric_progress,
    onchain_result_panel,
    print_cycle_summary,
    print_market_bias_summary,
    print_note,
    print_retro_header,
    print_section,
)
from src.utils.logging import configure_logging, logger


console = Console(force_terminal=True, legacy_windows=False)

# Metric labels used to size the per-metric progress bars in clean mode.
# Order matters - it's the order rendered in the terminal.
_L1_METRICS = ("ohlcv",)
_L2_METRICS = (
    "funding_rates",
    "open_interest",
    "volume",
    "long_short_ratio",
    "cum_funding",
    "whale_activity",
    "vault_flows",
    "market_sentiment",
)


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


def _build_engine(
    settings: Settings,
    market_data: DuneMarketData,
    dune: DuneMCPClient | None,
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
        ),
        dune=dune,
    )
    return DecisionEngine(
        level1=level1,
        level2=level2,
        level3=None,  # Level 3 (Gemini final arbiter) lands on Day 4
        weights=settings.level_weights,
        risk_on_threshold=settings.RISK_ON_THRESHOLD,
        risk_off_threshold=settings.RISK_OFF_THRESHOLD,
        short_bias_min_strength=settings.SHORT_BIAS_MIN_STRENGTH,
    )


def _build_router(
    settings: Settings, executor: ArcPerpExecutor, dry_run: bool
) -> AllocationRouter:
    symbols = settings.perp_symbols or ["BTC-PERP"]
    cfg = AllocationConfig(
        perp_symbol=symbols[0],
        base_position_usd=Decimal("1000"),
        max_position_usd=Decimal("10000"),
        max_drawdown_pct=settings.MAX_DRAWDOWN_PCT,
        risk_on_threshold=settings.RISK_ON_THRESHOLD,
        risk_off_threshold=settings.RISK_OFF_THRESHOLD,
        symmetric_short_sizing=settings.SYMMETRIC_SHORT_SIZING,
        short_size_multiplier=Decimal(str(settings.SHORT_SIZE_MULTIPLIER)),
    )
    return AllocationRouter(executor=executor, config=cfg, dry_run=dry_run)


# ---------------------------------------------------------------------------
# Pre-flight & helpers
# ---------------------------------------------------------------------------


def _preflight_live(settings: Settings) -> list[str]:
    required = {
        "CIRCLE_API_KEY": settings.CIRCLE_API_KEY,
        "CIRCLE_ENTITY_SECRET": settings.CIRCLE_ENTITY_SECRET,
        "CIRCLE_AGENT_WALLET_ID": settings.CIRCLE_AGENT_WALLET_ID,
        "ARC_PERP_ROUTER_ADDRESS": settings.ARC_PERP_ROUTER_ADDRESS,
        "ARC_PERP_VAULT_ADDRESS": settings.ARC_PERP_VAULT_ADDRESS,
    }
    return [name for name, value in required.items() if not value]


def _explorer_link(settings: Settings, tx_hash: str | None) -> str | None:
    if not (tx_hash and settings.ARC_EXPLORER_URL):
        return None
    base = settings.ARC_EXPLORER_URL.rstrip("/")
    return f"{base}/tx/{tx_hash}"


async def _build_market_context(
    settings: Settings,
    executor: ArcPerpExecutor,
    wallet: CircleWallet,
) -> dict[str, Any]:
    """Assemble the decision-engine context (symbols, RPC, account, drawdown)."""
    addr = await wallet.get_address()
    if addr:
        executor.set_account_address(addr)
    account_id: str | None = None
    try:
        account_id = executor._account_id() if addr else None
    except Exception:  # noqa: BLE001 - address may be empty in dry-run
        account_id = None
    margin = await executor.get_margin()
    pnl = await executor.get_pnl()
    drawdown_pct = 0.0
    if margin > 0 and pnl < 0:
        drawdown_pct = float(-pnl / margin * 100)
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
    }


def _replace_l2_score(
    decision: DecisionResult, l2_score: LevelScore
) -> DecisionResult:
    """Swap the L2 placeholder in a short-circuited decision for a real L2 read.

    The decision math (final_score, regime, directive) is preserved -
    we only patch the L2 entry so the panel can render the real
    on-chain intelligence even on cycles where Level 1 short-circuited.
    """
    new_scores: list[LevelScore] = []
    for s in decision.level_scores:
        if s.level == 2:
            new_scores.append(
                LevelScore(
                    level=2,
                    score=l2_score.score,
                    rationale=l2_score.rationale,
                    raw=l2_score.raw,
                )
            )
        else:
            new_scores.append(s)
    decision.level_scores = new_scores
    return decision


# ---------------------------------------------------------------------------
# Cycle
# ---------------------------------------------------------------------------


async def _run_cycle_clean(
    *,
    settings: Settings,
    dry_run: bool,
    mode: str,
    wallet: CircleWallet,
    executor: ArcPerpExecutor,
    dune: DuneMCPClient | None,
    onchain: ArcOnchainReader,
    engine: DecisionEngine,
    router: AllocationRouter,
) -> DecisionResult:
    """Clean retro cycle (default). Renders progress bars + six panels."""
    # Always re-attach the hook before a cycle (Dune client persists
    # across cycles in --loop, the reporter is per-cycle).
    if dune is not None:
        dune.on_metric_event = None

    onchain_snapshot = await onchain.snapshot(account_id=None)
    context = await _build_market_context(settings, executor, wallet)
    context["onchain"] = onchain_snapshot

    # ---- Level 1 ------------------------------------------------------
    print_section(console, "Loading Level 1 data (OHLCV via Dune MCP)...")
    with metric_progress(console, list(_L1_METRICS)) as l1_progress:
        if dune is not None:
            dune.on_metric_event = l1_progress.callback()
        l1_score = await engine.level1.score(context)
    if dune is not None:
        dune.on_metric_event = None

    l1_blocked = bool(l1_score.raw.get("l1", {}).get("passes") is False)
    if l1_blocked:
        print_note(
            console,
            f"Level 1 BLOCKED: {l1_score.rationale}",
            style="bold red",
        )
    else:
        print_note(console, "Level 1 PASS", style="bold green")

    # ---- Level 2 (always - even on short-circuit, for display) -------
    print_section(
        console,
        "Loading Level 2 data (on-chain intelligence via Dune MCP)...",
    )
    with metric_progress(console, list(_L2_METRICS)) as l2_progress:
        if dune is not None:
            dune.on_metric_event = l2_progress.callback()
        l2_score = await engine.level2.score(context)
    if dune is not None:
        dune.on_metric_event = None

    l2_raw = l2_score.raw.get("l2", {})
    market_bias = str(l2_raw.get("market_bias", "neutral"))
    bias_strength = float(l2_raw.get("bias_strength", 0.0) or 0.0)
    print_market_bias_summary(
        console, bias=market_bias, strength=bias_strength
    )

    # ---- Build the final DecisionResult ------------------------------
    if l1_blocked:
        # Re-run the engine's short-circuit path so the directive +
        # final_score stay consistent with AGENTS.md, then patch the
        # L2 placeholder with the real reading we just fetched for the
        # panel.
        decision = engine._short_circuited(l1_score)  # type: ignore[attr-defined]
        decision = _replace_l2_score(decision, l2_score)
    else:
        l3_score = await engine._maybe_level3(  # type: ignore[attr-defined]
            l1_score, l2_score, context
        )
        scores = [l1_score, l2_score, l3_score]
        final_score = engine._aggregate(scores)  # type: ignore[attr-defined]
        directive = engine._build_directive(final_score, scores)  # type: ignore[attr-defined]
        regime = (
            l2_score.raw.get("l2", {}).get("regime")
            or directive.action.replace("_", "-")
        )
        decision = DecisionResult(
            final_score=final_score,
            regime=regime,
            directive=directive,
            level_scores=scores,
            weights=engine.weights,
        )

    plan = await router.route(decision)

    # ---- Headline summary --------------------------------------------
    print_cycle_summary(
        console,
        action=plan.action,
        bias=decision.directive.market_bias,
        bias_strength=decision.directive.bias_strength,
        score=decision.final_score,
        symbol=plan.symbol or context.get("symbol"),
    )

    # ---- Panels (six-panel rich report) ------------------------------
    console.print(
        market_context_panel(context, mode=mode, app_env=settings.APP_ENV)
    )
    l1_raw = decision.level_score(1).raw.get("l1", {}) if decision.level_score(1) else {}
    l2_raw = decision.level_score(2).raw.get("l2", {}) if decision.level_score(2) else {}
    console.print(level1_panel(l1_raw, decision.level_score(1).score))
    short_circuit_note = (
        "Level 2 evaluated (short-circuited for final decision) - "
        "L1 hard rules vetoed the trade, but on-chain intelligence is "
        "rendered in full so the demo always shows what Dune MCP saw."
        if decision.short_circuited
        else None
    )
    console.print(
        level2_panel(
            l2_raw, decision.level_score(2).score,
            short_circuit_note=short_circuit_note,
        )
    )
    console.print(final_decision_panel(decision))
    console.print(execution_plan_panel(plan))
    console.print(
        onchain_result_panel(
            plan.tx_results,
            explorer=lambda h: _explorer_link(settings, h),
        )
    )

    if not dry_run:
        for tx in plan.tx_results:
            if tx.state in {"DRY_RUN", "FAILED", "DENIED"}:
                continue
            final = await wallet.wait_for_tx(tx.tx_id, poll_seconds=3.0)
            link = _explorer_link(settings, final.tx_hash)
            logger.info(
                "  tx settled | id={} state={} hash={} {}",
                final.tx_id, final.state, final.tx_hash,
                f"explorer={link}" if link else "",
            )

    console.print(
        f"[bold cyan]>[/] [dim]cycle done[/]  score=[bold]"
        f"{decision.final_score:.3f}[/]  action=[bold]"
        f"{decision.directive.action}[/]"
    )
    return decision


async def _run_cycle_verbose(
    *,
    settings: Settings,
    dry_run: bool,
    mode: str,
    wallet: CircleWallet,
    executor: ArcPerpExecutor,
    dune: DuneMCPClient | None,
    onchain: ArcOnchainReader,
    engine: DecisionEngine,
    router: AllocationRouter,
) -> DecisionResult:
    """Verbose cycle - the original Day-3 behaviour with full loguru chatter."""
    console.print(
        Rule(f"[bold cyan]CapitalArc[/]  mode={mode}  env={settings.APP_ENV}")
    )

    onchain_snapshot = await onchain.snapshot(account_id=None)
    context = await _build_market_context(settings, executor, wallet)
    context["onchain"] = onchain_snapshot
    console.print(
        market_context_panel(context, mode=mode, app_env=settings.APP_ENV)
    )

    decision = await engine.decide(context)

    l1_raw = decision.level_score(1).raw.get("l1", {}) if decision.level_score(1) else {}
    l2_raw = decision.level_score(2).raw.get("l2", {}) if decision.level_score(2) else {}
    console.print(level1_panel(l1_raw, decision.level_score(1).score))
    console.print(level2_panel(l2_raw, decision.level_score(2).score))
    console.print(final_decision_panel(decision))

    plan = await router.route(decision)
    console.print(execution_plan_panel(plan))
    console.print(
        onchain_result_panel(
            plan.tx_results,
            explorer=lambda h: _explorer_link(settings, h),
        )
    )

    if not dry_run:
        for tx in plan.tx_results:
            if tx.state in {"DRY_RUN", "FAILED", "DENIED"}:
                continue
            final = await wallet.wait_for_tx(tx.tx_id, poll_seconds=3.0)
            link = _explorer_link(settings, final.tx_hash)
            logger.info(
                "  tx settled | id={} state={} hash={} {}",
                final.tx_id, final.state, final.tx_hash,
                f"explorer={link}" if link else "",
            )

    console.print(
        Rule(
            f"[green]cycle done[/]  score={decision.final_score:.3f}  "
            f"action={decision.directive.action}"
        )
    )
    return decision


async def run_once(dry_run: bool, *, first_cycle: bool = True) -> None:
    settings = get_settings()
    configure_logging(settings.LOG_LEVEL, verbose=settings.VERBOSE)
    mode = "DRY-RUN" if dry_run else "LIVE"

    if first_cycle:
        if settings.VERBOSE:
            logger.info("CapitalArc starting | mode={} env={}", mode, settings.APP_ENV)
        else:
            print_retro_header(console)
            console.print(
                f"[bold cyan]>[/] mode=[bold]{mode}[/]  "
                f"env=[bold]{settings.APP_ENV}[/]  "
                f"verbose=[bold]{settings.VERBOSE}[/]"
            )

    if not dry_run:
        missing = _preflight_live(settings)
        if missing:
            logger.error(
                "Cannot start --live: missing env values: {}", ", ".join(missing)
            )
            raise SystemExit(2)
        logger.warning(
            "LIVE mode: real on-chain transactions will be submitted via Circle DCW."
        )

    wallet = _build_circle_wallet(settings, dry_run=dry_run)
    executor = _build_executor(settings, wallet=wallet, dry_run=dry_run)
    dune = _build_dune(settings)
    market_data = _build_dune_market_data(settings, dune=dune)
    onchain = _build_arc_reader(settings)
    engine = _build_engine(settings, market_data=market_data, dune=dune)
    router = _build_router(settings, executor=executor, dry_run=dry_run)

    try:
        # Probe Dune MCP once so the panel shows accurate health.
        await engine.level2.connect()

        cycle = (
            _run_cycle_verbose if settings.VERBOSE else _run_cycle_clean
        )
        await cycle(
            settings=settings,
            dry_run=dry_run,
            mode=mode,
            wallet=wallet,
            executor=executor,
            dune=dune,
            onchain=onchain,
            engine=engine,
            router=router,
        )
    finally:
        await wallet.aclose()
        if dune is not None:
            await dune.aclose()


async def run_loop(dry_run: bool) -> None:
    settings = get_settings()
    interval = max(1, settings.DECISION_INTERVAL_SECONDS)
    if settings.VERBOSE:
        logger.info("Looping every {} seconds. Ctrl-C to stop.", interval)
    else:
        # Header prints inside `run_once` on the first cycle; print the
        # loop hint as a clean retro line so the demo viewer knows the
        # cadence without needing to read the verbose loguru sink.
        print_note(
            console,
            f"Looping every {interval}s. Press Ctrl-C to stop.",
            style="dim",
        )

    first_cycle = True
    while True:
        try:
            await run_once(dry_run=dry_run, first_cycle=first_cycle)
        except Exception as exc:  # noqa: BLE001 - top-level guard
            logger.exception("Decision cycle failed: {}", exc)
        first_cycle = False
        await asyncio.sleep(interval)


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
    args = parser.parse_args()

    dry_run = not args.live

    if args.loop:
        asyncio.run(run_loop(dry_run=dry_run))
    else:
        asyncio.run(run_once(dry_run=dry_run))


if __name__ == "__main__":
    main()
