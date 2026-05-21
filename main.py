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
from decimal import Decimal
from typing import Any

from dotenv import load_dotenv
from rich.console import Console
from rich.rule import Rule

from rich import box
from rich.panel import Panel
from rich.table import Table

from src.allocation.allocation_router import AllocationConfig, AllocationRouter
from src.core.decision_engine import DecisionEngine, LevelScore
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
    onchain_result_panel,
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
        strong_bias_open_strength=settings.STRONG_BIAS_OPEN_STRENGTH,
        redistribute_synthetic_l3_weight=settings.REDISTRIBUTE_SYNTHETIC_L3_WEIGHT,
    )


def _build_router(
    settings: Settings, executor: ArcPerpExecutor, dry_run: bool
) -> AllocationRouter:
    symbols = settings.perp_symbols or ["BTC-PERP"]
    cfg = AllocationConfig(
        perp_symbol=symbols[0],
        base_position_usd=Decimal(str(settings.BASE_POSITION_USD)),
        max_position_usd=Decimal(str(settings.MAX_POSITION_USD)),
        max_drawdown_pct=settings.MAX_DRAWDOWN_PCT,
        risk_on_threshold=settings.RISK_ON_THRESHOLD,
        risk_off_threshold=settings.RISK_OFF_THRESHOLD,
        symmetric_short_sizing=settings.SYMMETRIC_SHORT_SIZING,
        short_size_multiplier=Decimal(str(settings.SHORT_SIZE_MULTIPLIER)),
        target_risk_pct=settings.TARGET_RISK_PCT,
        stop_atr_mult=settings.STOP_ATR_MULT,
        min_atr_pct_for_sizing=settings.MIN_ATR_PCT_FOR_SIZING,
        dd_haircut_exponent=settings.DD_HAIRCUT_EXPONENT,
        explain_sizing=settings.EXPLAIN_SIZING,
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


# ---------------------------------------------------------------------------
# Cycle
# ---------------------------------------------------------------------------


async def run_once(dry_run: bool) -> None:
    settings = get_settings()
    configure_logging(settings.LOG_LEVEL)
    mode = "DRY-RUN" if dry_run else "LIVE"

    console.print(Rule(f"[bold cyan]CapitalArc[/]  mode={mode}  env={settings.APP_ENV}"))
    logger.info("CapitalArc starting | mode={} env={}", mode, settings.APP_ENV)

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

        onchain_snapshot = await onchain.snapshot(
            account_id=None  # filled in by the executor below if known
        )

        context = await _build_market_context(settings, executor, wallet)
        context["onchain"] = onchain_snapshot
        console.print(
            market_context_panel(
                context, mode=mode, app_env=settings.APP_ENV
            )
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

        console.print(Rule(f"[green]cycle done[/]  score={decision.final_score:.3f}  action={decision.directive.action}"))
    finally:
        await wallet.aclose()
        if dune is not None:
            await dune.aclose()


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


async def run_test_bias(
    bias: str, strength: float, conviction: float
) -> None:
    """Offline scenario test for the conviction/direction engine.

    Synthesises L1, L2 and L3 `LevelScore`s with the requested
    `conviction` + `direction` (derived from `bias`) and exercises the
    real `_aggregate / _aggregate_direction / _build_directive`
    pipeline. Skips Dune, Circle and the AllocationRouter so the test
    stays fast and offline.

    Note: with the new conviction/direction split, the input `bias`
    sets every level's `direction_sign`, `strength` is the per-level
    "magnitude of direction" used only as a tag, and `conviction` is
    the per-level conviction score that the engine will then aggregate
    into `final_score`. With three equal-conviction levels and L3
    redistribution, the aggregate conviction equals the input
    conviction - which is what the test typically wants to probe.
    """
    settings = get_settings()
    configure_logging(settings.LOG_LEVEL)

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
        redistribute_synthetic_l3_weight=settings.REDISTRIBUTE_SYNTHETIC_L3_WEIGHT,
    )

    # Synthetic LevelScores: all three levels carry the same conviction
    # + direction so the test probes the score-classification branch
    # cleanly. L3 is marked synthetic so its weight is redistributed,
    # making the aggregate conviction == input conviction.
    scores = [
        LevelScore(
            level=1,
            score=conviction,
            rationale="synthetic L1 (test)",
            direction_sign=direction_sign,
        ),
        LevelScore(
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
        ),
        LevelScore(
            level=3,
            score=conviction,
            rationale="synthetic L3 (test)",
            raw={"l3": {"synthetic": True}},
            direction_sign=direction_sign,
        ),
    ]

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


async def run_loop(dry_run: bool) -> None:
    settings = get_settings()
    interval = max(1, settings.DECISION_INTERVAL_SECONDS)
    logger.info("Looping every {} seconds. Ctrl-C to stop.", interval)
    while True:
        try:
            await run_once(dry_run=dry_run)
        except Exception as exc:  # noqa: BLE001 - top-level guard
            logger.exception("Decision cycle failed: {}", exc)
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
    args = parser.parse_args()

    if args.test_bias is not None:
        asyncio.run(
            run_test_bias(
                bias=args.test_bias,
                strength=args.test_bias_strength,
                conviction=args.test_conviction,
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
