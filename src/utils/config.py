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

    # ---------- Dune MCP (Level 2 - the only L2 data source) ----------
    DUNE_API_KEY: str | None = None
    DUNE_MCP_URL: str = "https://mcp.dune.com/sse"
    # Dune REST endpoint used to actually execute queries (the MCP SSE
    # endpoint is the LLM-facing tool surface; the REST endpoint is the
    # one we hit programmatically with the same DUNE_API_KEY).
    DUNE_API_BASE_URL: str = "https://api.dune.com/api/v1"
    DUNE_CACHE_TTL_SECONDS: int = 300
    # Chain tag passed to the Dune SQL templates (see dune/queries/*.sql).
    DUNE_CHAIN_TAG: str = "arc"
    DUNE_LOOKBACK_HOURS: int = 24

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


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached settings accessor; reads `.env` once per process."""
    return Settings()
