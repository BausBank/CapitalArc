"""Typed configuration loader for CapitalArc.

Loads values from environment (`.env` via `python-dotenv` in `main.py`)
into a strongly-typed Pydantic model. Every other module imports
`Settings` instead of poking at `os.environ` directly.
"""

from __future__ import annotations

from functools import lru_cache

from typing import Any

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
    ARC_PERP_SYMBOLS: str = "BTC-PERP,ETH-PERP,SOL-PERP"

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
    # (BTC-PERP / ETH-PERP / SOL-PERP) map to the canonical on-chain
    # representations of those assets (WBTC / WETH / Wormhole-SOL on
    # Ethereum; cbBTC / WETH / Wormhole-SOL on Base). Spot DEX trades
    # (`dex.trades`) feed both Level 1 OHLCV and the Level 2 metrics;
    # ERC-20 `Transfer` events (`erc20_<chain>.evt_Transfer`) drive
    # the whale-activity and vault-flow queries.
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

    # ---------- Per-symbol token addresses (resolved against DUNE_CHAIN) ----------
    # If left blank, defaults from `_DEFAULT_TOKEN_ADDRESSES` are used.
    # Override via .env when you point at a chain we haven't pre-mapped
    # or want to track a different wrapping (e.g. cbBTC vs WBTC on Base).
    DUNE_TOKEN_BTC_ADDRESS: str | None = None
    DUNE_TOKEN_ETH_ADDRESS: str | None = None
    DUNE_TOKEN_SOL_ADDRESS: str | None = None
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

    # ---------- Gemini (Level 3) ----------
    GEMINI_API_KEY: str | None = None
    GEMINI_MODEL: str = "gemini-2.5-flash"
    GEMINI_TEMPERATURE: float = 0.2
    GEMINI_MAX_OUTPUT_TOKENS: int = 1024
    GEMINI_TIMEOUT_SECONDS: int = 30

    # ---------- Risk & Allocation ----------
    RISK_ON_THRESHOLD: float = 0.6
    RISK_OFF_THRESHOLD: float = 0.4
    MAX_DRAWDOWN_PCT: float = 10.0

    WEIGHT_LEVEL1: float = Field(default=0.25)
    WEIGHT_LEVEL2: float = Field(default=0.35)
    WEIGHT_LEVEL3: float = Field(default=0.40)

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
            "SOL-PERP": (
                self.DUNE_TOKEN_SOL_ADDRESS or chain_defaults.get("SOL")
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
        # Wormhole-wrapped SOL on Ethereum mainnet.
        "SOL": "0xd31a59c85ae9d8edefec411d448f90841571b89c",
        # Circle USDC on Ethereum mainnet.
        "USDC": "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48",
    },
    "base": {
        # Coinbase-wrapped BTC on Base (cbBTC, the dominant BTC token).
        "BTC": "0xcbb7c0000ab88b473b1f5afd9ef808440eed33bf",
        # Canonical Base WETH (predeploy).
        "ETH": "0x4200000000000000000000000000000000000006",
        # Wormhole-wrapped SOL on Base.
        "SOL": "0x1c61629598e4a901136a81bc138e5828dc150d67",
        # Native USDC on Base.
        "USDC": "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",
    },
    "arbitrum": {
        # WBTC on Arbitrum One.
        "BTC": "0x2f2a2543b76a4166549f7aab2e75bef0aefc5b0f",
        # WETH on Arbitrum One.
        "ETH": "0x82af49447d8a07e3bd95bd0d56f35241523fbab1",
        # Wormhole-wrapped SOL on Arbitrum One.
        "SOL": "0x2bcc6d6cdbbdc0a4071e48bb3b969b06b3330c07",
        # Native USDC on Arbitrum One.
        "USDC": "0xaf88d065e77c8cc2239327c5edb3a432268e5831",
    },
}


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached settings accessor; reads `.env` once per process."""
    return Settings()
