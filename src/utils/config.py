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
    DECISION_INTERVAL_SECONDS: int = 60
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

    # ---------- Arc Perp DEX ----------
    # ClearingHouse - entry contract for batch settlement (settleBatch).
    ARC_PERP_ROUTER_ADDRESS: str | None = None
    # USDCCollateralVault - holds USDC margin (deposit / withdraw).
    ARC_PERP_VAULT_ADDRESS: str | None = None
    # MarketRegistry - market id <-> spec mapping (getMarket).
    ARC_PERP_MARKET_REGISTRY_ADDRESS: str | None = None
    # PositionLedger - per-(accountId, marketId) position state (getPosition).
    ARC_PERP_POSITION_LEDGER_ADDRESS: str | None = None
    # Off-chain matching engine endpoint for EIP-712 OrderTypes.Order POSTs.
    # Wiring lands on Day 3 once the matcher URL is published in #agora-hackers.
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

    # ---------- USYC ----------
    USYC_TOKEN_ADDRESS: str | None = None
    USYC_MINT_CONTRACT_ADDRESS: str | None = None
    USYC_MIN_ROTATION_AMOUNT: float = 100.0

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
    L1_RSI_OVERBOUGHT: float = 70.0
    L1_RSI_OVERSOLD: float = 30.0
    L1_ATR_PCT_MIN: float = 0.15      # too quiet -> skip
    L1_ATR_PCT_MAX: float = 6.0       # too wild -> skip
    L1_EMA_FAST: int = 9
    L1_EMA_SLOW: int = 21
    L1_RSI_PERIOD: int = 14
    L1_ATR_PERIOD: int = 14
    L1_TIMEFRAMES: str = "15m,1h"
    L1_KLINES_LIMIT: int = 150
    L1_REQUIRE_TF_AGREEMENT: bool = True

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
    # When True (default), a synthetic L3 score (no real Gemini wiring
    # yet) gets its weight redistributed proportionally to L1 + L2 in
    # the aggregation, so the placeholder doesn't silently dilute the
    # real signal back into itself. Flip to False to keep the legacy
    # behaviour where synthetic L3 votes with its configured weight.
    REDISTRIBUTE_SYNTHETIC_L3_WEIGHT: bool = True

    # ---------- Short-selling controls ----------
    # When True, shorts use the same sizing pipeline as longs
    # (recommended). Flip to False + tune SHORT_SIZE_MULTIPLIER if
    # you want half-size shorts during the ramp-up phase.
    SYMMETRIC_SHORT_SIZING: bool = True
    SHORT_SIZE_MULTIPLIER: float = 1.0

    # ---------- Position sizing (vol-targeted + DD haircut) ----------
    # Default notional fallback when equity/ATR aren't available
    # (typical first dry-run cycle before any margin is deposited).
    BASE_POSITION_USD: float = 1000.0
    MAX_POSITION_USD: float = 10000.0
    # Fraction of equity to risk per trade under a `STOP_ATR_MULT * ATR`
    # adverse move. 0.02 = 2% (textbook default).
    TARGET_RISK_PCT: float = 0.02
    # Stop distance in ATR multiples.
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
