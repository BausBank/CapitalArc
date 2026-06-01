"""Async OpenRouter client used by Level 3 (Claude Sonnet 4.6 arbiter).

OpenRouter is a single-API meta-gateway in front of dozens of LLM
providers; we use it because the user's outbound region is hard-blocked
from Google AI Studio (Gemini) but OpenRouter routes through their own
infrastructure. The wire format is OpenAI's `/v1/chat/completions`, so
the client is essentially an OpenAI-style chat-completions caller with
a few OpenRouter-specific niceties baked in:

* Mandatory ``Authorization: Bearer <key>``.
* Optional but recommended ``HTTP-Referer`` and ``X-Title`` headers
  that surface the calling app in OpenRouter analytics.
* ``transforms: []`` to disable middle-out summarisation - we want
  Claude to see the briefing exactly as we sent it.
* A high default timeout (60s) because Anthropic's longer reasoning
  budget can take 5-10s end-to-end.

The client exposes a single high-level entry point
``generate_json(system_prompt, user_prompt)`` that returns a parsed
JSON object. Validation against :class:`ArbiterResponse` happens in
the caller (``src/core/level3.py``) so this layer stays
provider-agnostic.

Anthropic models on OpenRouter do **not** support
``response_format={"type": "json_object"}`` (only OpenAI / Mistral
models do today). We therefore rely on the system prompt + an explicit
"JSON only, no markdown" footer in the user prompt to keep Claude
disciplined, plus a permissive parser that strips ```` ```json ```` /
```` ``` ```` fences before json.loads. Together this gives us a clean
JSON object on every successful call.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from dataclasses import dataclass
from typing import Any

import httpx

from src.utils.logging import logger

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
CHAT_COMPLETIONS_PATH = "/chat/completions"


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class OpenRouterAPIError(RuntimeError):
    """Clean, actionable wrapper for OpenRouter API failures.

    Funnels every 4xx, network and parsing error into a single class
    with a human-readable diagnosis so the arbiter's fallback panel
    can tell the operator exactly what to fix (bad key, model not
    available on the account, etc.).
    """


def _map_status_error(
    status: int, body_text: str, model: str
) -> OpenRouterAPIError:
    """Translate a non-2xx HTTP status into an :class:`OpenRouterAPIError`.

    Tailors each message to the operator's most likely next step.
    """
    snippet = body_text.strip()[:240] or f"HTTP {status} (empty body)"
    if status == 401:
        return OpenRouterAPIError(
            "OpenRouter authentication failed (HTTP 401). OPENROUTER_API_KEY "
            "is missing, malformed or revoked. Generate a new key on "
            f"https://openrouter.ai/keys and update .env. Original: {snippet}"
        )
    if status == 402:
        return OpenRouterAPIError(
            "OpenRouter payment required (HTTP 402). Account is out of "
            "credits for paid models. Top up balance on "
            f"https://openrouter.ai/credits. Original: {snippet}"
        )
    if status == 403:
        return OpenRouterAPIError(
            f"OpenRouter forbidden (HTTP 403) for model {model!r}. The "
            f"API key may lack access to this model (e.g. Anthropic "
            f"requires enabling provider routing). Check key permissions "
            f"on https://openrouter.ai/keys. Original: {snippet}"
        )
    if status == 404:
        return OpenRouterAPIError(
            f"OpenRouter model not found (HTTP 404): {model!r}. Verify the "
            f"model slug on https://openrouter.ai/models. Recent Claude "
            f"slugs: anthropic/claude-sonnet-4.6, anthropic/claude-3.5-"
            f"sonnet. Original: {snippet}"
        )
    if status == 429:
        return OpenRouterAPIError(
            f"OpenRouter rate-limited (HTTP 429) for model {model!r}. "
            f"Either you hit the account-wide RPM, or the upstream "
            f"provider (Anthropic) throttled OpenRouter. Wait or upgrade "
            f"plan. Original: {snippet}"
        )
    if 500 <= status < 600:
        return OpenRouterAPIError(
            f"OpenRouter upstream error (HTTP {status}) for model "
            f"{model!r}. This is transient on their side; the client's "
            f"retry loop will try again. Original: {snippet}"
        )
    return OpenRouterAPIError(
        f"OpenRouter unexpected status (HTTP {status}) for model "
        f"{model!r}. Original: {snippet}"
    )


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass
class OpenRouterClientConfig:
    """Configuration for :class:`OpenRouterClient`."""

    api_key: str
    model: str = "anthropic/claude-sonnet-4.6"
    temperature: float = 0.2
    max_tokens: int = 1024
    timeout_seconds: int = 60
    max_retries: int = 2
    backoff_seconds: float = 2.0
    # Optional analytics headers - shown on https://openrouter.ai/activity
    # so it's easy to attribute traffic when sharing a single key across
    # projects. Both are safe to leave at defaults for hackathon use.
    referer: str = "https://github.com/capitalarc/capitalarc"
    app_title: str = "CapitalArc"
    base_url: str = OPENROUTER_BASE_URL

    @classmethod
    def from_env(cls) -> "OpenRouterClientConfig":
        """Build a config from `OPENROUTER_*` environment variables.

        Raises
        ------
        RuntimeError
            When `OPENROUTER_API_KEY` is missing. Callers that want a
            graceful degradation should check the env var before
            invoking `from_env`.
        """
        api_key = os.getenv("OPENROUTER_API_KEY", "")
        if not api_key:
            raise RuntimeError(
                "OPENROUTER_API_KEY is not set; cannot create "
                "OpenRouterClientConfig"
            )
        return cls(
            api_key=api_key,
            model=os.getenv("OPENROUTER_MODEL", "anthropic/claude-sonnet-4.6"),
            temperature=float(os.getenv("OPENROUTER_TEMPERATURE", "0.2")),
            max_tokens=int(os.getenv("OPENROUTER_MAX_TOKENS", "1024")),
            timeout_seconds=int(os.getenv("OPENROUTER_TIMEOUT_SECONDS", "60")),
            max_retries=int(os.getenv("OPENROUTER_MAX_RETRIES", "2")),
            backoff_seconds=float(os.getenv("OPENROUTER_BACKOFF_SECONDS", "2.0")),
            referer=os.getenv(
                "OPENROUTER_REFERER",
                "https://github.com/capitalarc/capitalarc",
            ),
            app_title=os.getenv("OPENROUTER_APP_TITLE", "CapitalArc"),
            base_url=os.getenv("OPENROUTER_BASE_URL", OPENROUTER_BASE_URL),
        )


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class OpenRouterClient:
    """Async OpenRouter client tuned for the Level 3 arbiter.

    The client owns a single :class:`httpx.AsyncClient` for connection
    pooling across the agent's lifetime. Call :meth:`aclose` when
    shutting the agent down (the engine does this in ``run_once`` /
    ``run_loop``); otherwise the underlying socket pool is cleaned up
    automatically by httpx when the process exits.

    Public surface is intentionally minimal: :meth:`generate_json` is
    the one call Level 3 needs.
    """

    def __init__(self, config: OpenRouterClientConfig) -> None:
        self.config = config
        # Build a fresh httpx client. We use a single client across all
        # calls so HTTP keep-alive and connection pooling work as
        # intended (Claude turns are ~5-10s; reusing the TCP connection
        # shaves ~200ms per call).
        self._http: httpx.AsyncClient = httpx.AsyncClient(
            base_url=config.base_url,
            timeout=httpx.Timeout(config.timeout_seconds),
            headers={
                "Authorization": f"Bearer {config.api_key}",
                "Content-Type": "application/json",
                "HTTP-Referer": config.referer,
                "X-Title": config.app_title,
            },
        )

    async def aclose(self) -> None:
        """Release the underlying HTTP connection pool."""
        await self._http.aclose()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def generate_json(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        temperature: float | None = None,
    ) -> dict[str, Any]:
        """Send a structured chat prompt to OpenRouter and return parsed JSON.

        Anthropic models do not accept OpenAI's ``response_format``, so
        we rely on:

        1. The system prompt enforcing "JSON only, no markdown".
        2. An additional "JSON only" trailer appended to the user
           prompt for redundancy.
        3. A permissive :meth:`_safe_json_loads` that strips markdown
           fences if Claude leaks them anyway.

        Combined, these three give us a clean JSON object on every
        observed Claude Sonnet 4.6 call.

        Parameters
        ----------
        system_prompt
            High-level role / behaviour instructions sent as the
            ``system`` message.
        user_prompt
            Per-call payload (the structured briefing) sent as the
            ``user`` message.
        temperature
            Optional per-call override of the configured sampling
            temperature. Used by the Level-3 self-consistency feature
            to draw additional samples at a higher temperature than the
            deterministic primary call. ``None`` keeps the config value.

        Returns
        -------
        dict[str, Any]
            The parsed JSON object.

        Raises
        ------
        OpenRouterAPIError
            On any non-retriable failure (bad key, missing model,
            geo / policy denial, ...). Surfaces with an actionable
            error message.
        ValueError
            When the response body is not valid JSON despite the
            prompt-level instructions and all retries.
        RuntimeError
            On transient SDK / network errors after exhausting the
            retry budget.
        """
        body = {
            "model": self.config.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {
                    "role": "user",
                    "content": (
                        f"{user_prompt}\n\n"
                        "Respond with valid JSON ONLY. No markdown fences, "
                        "no prose. The JSON must match the schema defined "
                        "in the system prompt."
                    ),
                },
            ],
            "temperature": float(
                self.config.temperature if temperature is None else temperature
            ),
            "max_tokens": int(self.config.max_tokens),
            # Disable OpenRouter's middle-out compression - we want the
            # arbiter to see the full briefing without summarisation.
            "transforms": [],
        }

        last_error: Exception | None = None
        attempts = max(1, self.config.max_retries + 1)
        for attempt in range(1, attempts + 1):
            try:
                text = await self._call_once(body)
                return self._safe_json_loads(text)
            except OpenRouterAPIError as exc:
                # Hard failures (auth, model-not-found, geo / policy
                # denial) won't fix themselves on retry; surface
                # immediately. Transient ones (5xx, 429) loop back via
                # the generic except below.
                if self._is_transient(exc):
                    last_error = exc
                    logger.warning(
                        "OpenRouter transient failure attempt {}/{}: {}",
                        attempt, attempts, exc,
                    )
                    if attempt >= attempts:
                        break
                    await asyncio.sleep(
                        self.config.backoff_seconds * (2 ** (attempt - 1))
                    )
                    continue
                logger.error("OpenRouter hard failure: {}", exc)
                raise
            except (ValueError, httpx.HTTPError, RuntimeError) as exc:
                last_error = exc
                logger.warning(
                    "OpenRouter call attempt {}/{} failed: {}",
                    attempt, attempts, exc,
                )
                if attempt >= attempts:
                    break
                await asyncio.sleep(
                    self.config.backoff_seconds * (2 ** (attempt - 1))
                )
        assert last_error is not None
        raise RuntimeError(
            f"OpenRouter failed after {attempts} attempt(s): {last_error}"
        ) from last_error

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    async def _call_once(self, body: dict[str, Any]) -> str:
        """Single POST to ``/chat/completions``; returns the message text."""
        try:
            response = await self._http.post(
                CHAT_COMPLETIONS_PATH, json=body
            )
        except httpx.TimeoutException as exc:
            raise RuntimeError(
                f"OpenRouter request timed out after "
                f"{self.config.timeout_seconds}s"
            ) from exc
        except httpx.HTTPError as exc:
            # Network-level failure (DNS, TLS, connection reset).
            raise RuntimeError(
                f"OpenRouter network error: {exc!r}"
            ) from exc

        if response.status_code >= 400:
            raise _map_status_error(
                response.status_code,
                response.text or "",
                self.config.model,
            )

        try:
            payload = response.json()
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"OpenRouter returned non-JSON envelope: "
                f"{response.text[:240]!r}"
            ) from exc

        return self._extract_message_text(payload)

    @staticmethod
    def _extract_message_text(payload: dict[str, Any]) -> str:
        """Pull the assistant's text out of an OpenRouter chat response.

        Schema:
        ``{"choices": [{"message": {"content": "..."}}], ...}``.
        OpenRouter mirrors OpenAI's structure, but some upstream
        providers occasionally return ``content`` as a list of
        content-parts (``[{"type": "text", "text": "..."}]``); we
        handle both shapes.
        """
        choices = payload.get("choices") or []
        if not choices:
            err = payload.get("error") or {}
            err_msg = (err.get("message") if isinstance(err, dict) else None) or ""
            raise ValueError(
                f"OpenRouter response has no choices. Error: {err_msg or payload!r}"
            )
        message = choices[0].get("message") or {}
        content = message.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            # Concatenate textual parts.
            chunks: list[str] = []
            for part in content:
                if isinstance(part, dict) and part.get("type") == "text":
                    chunks.append(str(part.get("text", "")))
            return "".join(chunks)
        raise ValueError(
            f"OpenRouter returned an unsupported content shape: "
            f"{type(content).__name__}"
        )

    @staticmethod
    def _safe_json_loads(text: str) -> dict[str, Any]:
        """Parse JSON, stripping markdown fences if Claude leaks them.

        Claude occasionally wraps even ``Respond with JSON only`` in
        ```` ```json ... ``` ```` fences. This helper strips those
        defensively before json.loads so the validator never sees
        extraneous wrapping.
        """
        stripped = text.strip()
        if stripped.startswith("```"):
            stripped = re.sub(
                r"^```(?:json)?\s*|\s*```$", "", stripped, flags=re.DOTALL
            ).strip()
        try:
            data = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"OpenRouter returned non-JSON content: {stripped[:300]!r}"
            ) from exc
        if not isinstance(data, dict):
            raise ValueError(
                f"OpenRouter returned a non-object JSON value: "
                f"{type(data).__name__}"
            )
        return data

    @staticmethod
    def _is_transient(exc: OpenRouterAPIError) -> bool:
        """Return True if the error should be retried.

        Transient: 429 (rate limit) and 5xx (upstream blip). Everything
        else is a hard failure that retrying can't fix.
        """
        msg = str(exc)
        return "HTTP 429" in msg or "HTTP 5" in msg


__all__ = [
    "OpenRouterAPIError",
    "OpenRouterClient",
    "OpenRouterClientConfig",
]
