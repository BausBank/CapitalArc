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

    # ---------- Arc Chain ----------
    ARC_RPC_URL: str = "https://rpc.testnet.arc.network"
    ARC_CHAIN_ID: int | None = None
    ARC_EXPLORER_URL: str | None = None

    # ---------- Arc Perp DEX ----------
    ARC_PERP_ROUTER_ADDRESS: str | None = None
    ARC_PERP_MAX_LEVERAGE: int = 3
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

    # ---------- Dune MCP (Level 2) ----------
    DUNE_API_KEY: str | None = None
    DUNE_MCP_URL: str = "https://mcp.dune.com/sse"
    DUNE_CACHE_TTL_SECONDS: int = 300

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
