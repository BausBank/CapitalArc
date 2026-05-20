"""CapitalArc - main entry point.

Wires the full pipeline:

    Level 1 (TA) + Level 2 (Dune MCP) + Level 3 (Gemini)
        -> DecisionEngine
            -> AllocationRouter
                -> ArcPerpExecutor + CircleWallet (Arc Perp DEX, Paymaster)

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

from src.allocation.allocation_router import AllocationConfig, AllocationRouter
from src.core.decision_engine import DecisionEngine
from src.core.level1 import Level1, Level1Config
from src.core.level2 import Level2, Level2Config
from src.core.level3 import Level3, Level3Config
from src.execution.arc_perp_executor import ArcPerpConfig, ArcPerpExecutor
from src.execution.circle_wallet import CircleWallet, CircleWalletConfig
from src.llm.gemini_client import GeminiClient, GeminiClientConfig
from src.utils.config import Settings, get_settings
from src.utils.logging import configure_logging, logger


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


def _build_engine(settings: Settings) -> DecisionEngine:
    level1 = Level1(Level1Config())
    level2 = Level2(
        Level2Config(
            dune_mcp_url=settings.DUNE_MCP_URL,
            dune_api_key=settings.DUNE_API_KEY or "",
            cache_ttl_seconds=settings.DUNE_CACHE_TTL_SECONDS,
        )
    )

    gemini_client: GeminiClient | None = None
    if settings.GEMINI_API_KEY:
        try:
            gemini_client = GeminiClient(
                GeminiClientConfig(
                    api_key=settings.GEMINI_API_KEY,
                    model=settings.GEMINI_MODEL,
                    temperature=settings.GEMINI_TEMPERATURE,
                    max_output_tokens=settings.GEMINI_MAX_OUTPUT_TOKENS,
                    timeout_seconds=settings.GEMINI_TIMEOUT_SECONDS,
                )
            )
        except Exception as exc:  # noqa: BLE001 - we want a soft fallback
            logger.warning("Gemini client init failed, using placeholder: {}", exc)
            gemini_client = None

    level3 = Level3(
        client=gemini_client,
        config=Level3Config(
            model=settings.GEMINI_MODEL,
            temperature=settings.GEMINI_TEMPERATURE,
            max_output_tokens=settings.GEMINI_MAX_OUTPUT_TOKENS,
            timeout_seconds=settings.GEMINI_TIMEOUT_SECONDS,
        ),
    )
    return DecisionEngine(
        level1=level1,
        level2=level2,
        level3=level3,
        weights=settings.level_weights,
        risk_on_threshold=settings.RISK_ON_THRESHOLD,
        risk_off_threshold=settings.RISK_OFF_THRESHOLD,
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
    )
    return AllocationRouter(executor=executor, config=cfg, dry_run=dry_run)


def _build_market_context(settings: Settings) -> dict[str, Any]:
    """Minimal market context for Day 2. Day 3 hydrates this from real feeds."""
    return {
        "symbol": (settings.perp_symbols or ["BTC-PERP"])[0],
        "rpc_url": settings.ARC_RPC_URL,
        "ohlcv": None,  # placeholder, wired in Day 3
        "funding_rate": None,
        "open_interest": None,
    }


def _preflight_live(settings: Settings) -> list[str]:
    """Return a list of missing fields that block --live. Empty list = OK."""
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


async def run_once(dry_run: bool) -> None:
    settings = get_settings()
    configure_logging(settings.LOG_LEVEL)
    mode = "DRY-RUN" if dry_run else "LIVE"
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
    engine = _build_engine(settings)
    router = _build_router(settings, executor=executor, dry_run=dry_run)

    try:
        # Best-effort: discover the agent's EVM address so accountId derivation
        # works (live reads & writes). In dry-run we tolerate missing creds.
        addr = await wallet.get_address()
        if addr:
            executor.set_account_address(addr)
            logger.info("Agent wallet address: {}", addr)
        else:
            logger.debug("Wallet address not resolved (read-only fallback).")

        context = _build_market_context(settings)
        logger.info("Market context: {}", context)

        decision = await engine.decide(context)
        logger.info(
            "Decision | score={:.3f} regime={} action={} side={} intensity={:.2f}",
            decision.final_score,
            decision.regime,
            decision.directive.action,
            decision.directive.side,
            decision.directive.intensity,
        )
        for s in decision.level_scores:
            logger.info("  L{} score={:.3f} :: {}", s.level, s.score, s.rationale)

        plan = await router.route(decision)
        logger.info(
            "ExecutionPlan | id={} action={} symbol={} size_usd={} leverage={}",
            plan.decision_id,
            plan.action,
            plan.symbol,
            plan.size_usd,
            plan.leverage,
        )

        for tx in plan.tx_results:
            link = _explorer_link(settings, tx.tx_hash)
            logger.info(
                "  tx | id={} state={} hash={} sponsored={} {}",
                tx.tx_id, tx.state, tx.tx_hash, tx.sponsored,
                f"explorer={link}" if link else "",
            )
            if not dry_run and tx.state not in {"DRY_RUN", "FAILED", "DENIED"}:
                final = await wallet.wait_for_tx(tx.tx_id, poll_seconds=3.0)
                link = _explorer_link(settings, final.tx_hash)
                logger.info(
                    "  tx settled | id={} state={} hash={} {}",
                    final.tx_id, final.state, final.tx_hash,
                    f"explorer={link}" if link else "",
                )
    finally:
        await wallet.aclose()


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
    args = parser.parse_args()

    dry_run = not args.live  # default to dry-run unless --live is explicit

    if args.loop:
        asyncio.run(run_loop(dry_run=dry_run))
    else:
        asyncio.run(run_once(dry_run=dry_run))


if __name__ == "__main__":
    main()
