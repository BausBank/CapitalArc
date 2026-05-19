"""Thin async wrapper around `google.generativeai` for Gemini 2.5 Flash.

The wrapper centralises:
    - API key / model configuration
    - Deterministic generation defaults (low temperature)
    - JSON-only response handling for the Level 3 arbiter
    - Timeouts and retries (wired in on Day 2)

Day 1 ships the configuration surface and a `generate_json` skeleton.
Heavy I/O work is deferred so the rest of the codebase can already
import and type-check against this client.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any

try:
    import google.generativeai as genai
except ImportError:  # pragma: no cover - dependency installed via requirements.txt
    genai = None  # type: ignore[assignment]


@dataclass
class GeminiClientConfig:
    """Configuration for the Gemini client."""

    api_key: str
    model: str = "gemini-2.5-flash"
    temperature: float = 0.2
    max_output_tokens: int = 1024
    timeout_seconds: int = 30

    @classmethod
    def from_env(cls) -> "GeminiClientConfig":
        """Build a config from `GEMINI_*` environment variables."""
        api_key = os.getenv("GEMINI_API_KEY", "")
        if not api_key:
            raise RuntimeError(
                "GEMINI_API_KEY is not set; cannot create GeminiClientConfig"
            )
        return cls(
            api_key=api_key,
            model=os.getenv("GEMINI_MODEL", "gemini-2.5-flash"),
            temperature=float(os.getenv("GEMINI_TEMPERATURE", "0.2")),
            max_output_tokens=int(os.getenv("GEMINI_MAX_OUTPUT_TOKENS", "1024")),
            timeout_seconds=int(os.getenv("GEMINI_TIMEOUT_SECONDS", "30")),
        )


class GeminiClient:
    """Async-friendly wrapper around `google.generativeai`.

    Day 1 implementation: configure the SDK and expose typed methods.
    The actual `generate_json` call uses the SDK's blocking `generate_content`
    and will be made non-blocking via `asyncio.to_thread` on Day 2.
    """

    def __init__(self, config: GeminiClientConfig) -> None:
        if genai is None:
            raise RuntimeError(
                "google-generativeai is not installed. Run "
                "`pip install -r requirements.txt` first."
            )
        self.config = config
        genai.configure(api_key=config.api_key)
        self._model = genai.GenerativeModel(model_name=config.model)

    async def generate_json(
        self,
        system_prompt: str,
        user_prompt: str,
    ) -> dict[str, Any]:
        """Send a structured prompt to Gemini and parse the JSON response.

        Day 1 stub - on Day 2 this will:
            1. Combine `system_prompt` + `user_prompt` into a Gemini chat call.
            2. Force `response_mime_type="application/json"`.
            3. Apply timeout + retries.
            4. Return the parsed dict (or raise on malformed output).
        """
        raise NotImplementedError(
            "GeminiClient.generate_json will be implemented on Day 2"
        )

    def _safe_json_loads(self, text: str) -> dict[str, Any]:
        """Defensive JSON parser used by `generate_json` on Day 2."""
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"Gemini returned non-JSON response: {text[:200]!r}"
            ) from exc
