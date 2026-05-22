"""Smoke tests for the OpenRouter client wrapper.

Network calls are not exercised here - we lock down only the pure
parsing / extraction / error-mapping helpers that protect the agent
from malformed API responses.
"""

from __future__ import annotations

import pytest

from src.llm.openrouter_client import (
    OpenRouterAPIError,
    OpenRouterClient,
    _map_status_error,
)


# ---------------------------------------------------------------------------
# JSON parsing
# ---------------------------------------------------------------------------


def test_safe_json_loads_parses_plain_json() -> None:
    out = OpenRouterClient._safe_json_loads(
        '{"conviction": 0.5, "direction": "long"}'
    )
    assert out == {"conviction": 0.5, "direction": "long"}


def test_safe_json_loads_strips_markdown_fences() -> None:
    fenced = '```json\n{"conviction": 0.5}\n```'
    assert OpenRouterClient._safe_json_loads(fenced) == {"conviction": 0.5}


def test_safe_json_loads_strips_bare_fences() -> None:
    fenced = '```\n{"conviction": 0.5}\n```'
    assert OpenRouterClient._safe_json_loads(fenced) == {"conviction": 0.5}


def test_safe_json_loads_raises_on_garbage() -> None:
    with pytest.raises(ValueError, match="non-JSON"):
        OpenRouterClient._safe_json_loads("not a json")


def test_safe_json_loads_rejects_list_payload() -> None:
    """The arbiter must return a JSON object, not an array."""
    with pytest.raises(ValueError, match="non-object"):
        OpenRouterClient._safe_json_loads("[1, 2, 3]")


# ---------------------------------------------------------------------------
# Response text extraction
# ---------------------------------------------------------------------------


def test_extract_message_text_handles_plain_string_content() -> None:
    payload = {
        "choices": [
            {"message": {"role": "assistant", "content": '{"k": 1}'}}
        ]
    }
    assert OpenRouterClient._extract_message_text(payload) == '{"k": 1}'


def test_extract_message_text_handles_content_parts_list() -> None:
    """Some upstream providers return content as a list of typed parts."""
    payload = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": [
                        {"type": "text", "text": '{"a":'},
                        {"type": "text", "text": " 1}"},
                    ],
                }
            }
        ]
    }
    assert OpenRouterClient._extract_message_text(payload) == '{"a": 1}'


def test_extract_message_text_raises_when_no_choices() -> None:
    payload = {"choices": [], "error": {"message": "rate_limited"}}
    with pytest.raises(ValueError, match="rate_limited"):
        OpenRouterClient._extract_message_text(payload)


# ---------------------------------------------------------------------------
# Error mapping
# ---------------------------------------------------------------------------


def test_map_status_error_401_mentions_api_key() -> None:
    err = _map_status_error(401, "unauthorized", "anthropic/claude-sonnet-4.6")
    assert isinstance(err, OpenRouterAPIError)
    assert "401" in str(err)
    assert "OPENROUTER_API_KEY" in str(err)


def test_map_status_error_404_mentions_model_slug() -> None:
    err = _map_status_error(404, "unknown model", "anthropic/claude-x")
    assert "anthropic/claude-x" in str(err)
    assert "404" in str(err)


def test_map_status_error_429_is_recognised_as_transient() -> None:
    err = _map_status_error(429, "too many requests", "anthropic/claude-sonnet-4.6")
    assert OpenRouterClient._is_transient(err) is True


def test_map_status_error_5xx_is_recognised_as_transient() -> None:
    err = _map_status_error(503, "upstream busy", "anthropic/claude-sonnet-4.6")
    assert OpenRouterClient._is_transient(err) is True


def test_map_status_error_401_is_not_transient() -> None:
    err = _map_status_error(401, "no key", "anthropic/claude-sonnet-4.6")
    assert OpenRouterClient._is_transient(err) is False
