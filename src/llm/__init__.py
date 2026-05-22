"""LLM clients used by CapitalArc.

Level 3 currently runs through OpenRouter + Claude Sonnet 4.6. The
OpenRouter HTTP gateway lets us swap upstream models (Anthropic,
OpenAI, Mistral, ...) by changing a single env var (`OPENROUTER_MODEL`),
which is why we migrated off the geo-locked Google AI Studio API.

Additional providers, if ever needed, should sit alongside the
OpenRouter client with the same `generate_json(...)` style interface.
"""

from src.llm.openrouter_client import (
    OpenRouterAPIError,
    OpenRouterClient,
    OpenRouterClientConfig,
)

__all__ = [
    "OpenRouterAPIError",
    "OpenRouterClient",
    "OpenRouterClientConfig",
]
