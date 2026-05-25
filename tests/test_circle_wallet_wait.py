"""Tests for the noisy-poll-storm fix on :class:`CircleWallet.wait_for_tx`.

Regression guards for two related bugs that surfaced after the
Day-5 Hyperliquid pivot:

1. ``wait_for_tx`` retried indefinitely on HTTP 400 — Circle was
   rejecting non-Circle tx ids (e.g. Hyperliquid order ids), and
   every retry produced a fresh WARNING line for ~90 seconds.
2. ``main.py``'s post-cycle wait loop sent **every** tx to
   ``wallet.wait_for_tx`` regardless of venue, including already-
   ``CONFIRMED`` Hyperliquid orders that have no Circle record to
   poll.

Both layers now defend against the failure mode independently.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest

from src.execution.circle_wallet import (
    CircleWallet,
    CircleWalletConfig,
    TxResult,
)


# ---------------------------------------------------------------------------
# CircleWallet.wait_for_tx — 4xx fail-fast
# ---------------------------------------------------------------------------


def _make_wallet() -> CircleWallet:
    """Build a CircleWallet without ever hitting the real Circle API."""
    return CircleWallet(
        CircleWalletConfig(
            api_key="dummy", wallet_id="dummy", entity_secret="00" * 32
        )
    )


def _http_status_error(status: int, url: str = "https://api.circle.com/v1/w3s/transactions/x") -> httpx.HTTPStatusError:
    """Build an httpx.HTTPStatusError with a given status code."""
    request = httpx.Request("GET", url)
    response = httpx.Response(status, request=request)
    return httpx.HTTPStatusError(
        f"Client error '{status}'", request=request, response=response
    )


@pytest.mark.asyncio
async def test_wait_for_tx_bails_immediately_on_400() -> None:
    """The pathological case: Circle returns 400 -> we MUST NOT retry."""
    wallet = _make_wallet()
    resp = AsyncMock()
    resp.raise_for_status = lambda: (_ for _ in ()).throw(
        _http_status_error(400)
    )
    wallet._client.get = AsyncMock(return_value=resp)  # type: ignore[method-assign]

    result = await wallet.wait_for_tx(
        "53624986552", poll_seconds=0.01, timeout_seconds=5.0
    )

    assert isinstance(result, TxResult)
    assert result.state == "FAILED"
    # Exactly ONE call to /transactions/{id}; no retry storm.
    assert wallet._client.get.await_count == 1
    await wallet.aclose()


@pytest.mark.asyncio
async def test_wait_for_tx_bails_on_404_too() -> None:
    """Any 4xx is a client error - no retries make sense for any of them."""
    wallet = _make_wallet()
    resp = AsyncMock()
    resp.raise_for_status = lambda: (_ for _ in ()).throw(
        _http_status_error(404)
    )
    wallet._client.get = AsyncMock(return_value=resp)  # type: ignore[method-assign]

    result = await wallet.wait_for_tx(
        "ghost-id", poll_seconds=0.01, timeout_seconds=5.0
    )
    assert result.state == "FAILED"
    assert wallet._client.get.await_count == 1
    await wallet.aclose()


@pytest.mark.asyncio
async def test_wait_for_tx_retries_on_5xx() -> None:
    """5xx is transient - keep polling until either terminal or timeout."""
    wallet = _make_wallet()
    fail = AsyncMock()
    fail.raise_for_status = lambda: (_ for _ in ()).throw(
        _http_status_error(503)
    )
    ok = AsyncMock()
    # raise_for_status is a SYNC method on httpx.Response; use a plain
    # no-op lambda so awaiting it doesn't leak a RuntimeWarning.
    ok.raise_for_status = lambda: None
    ok.json = lambda: {
        "data": {
            "transaction": {"state": "CONFIRMED", "txHash": "0xabc"}
        }
    }
    # Two 5xx failures, then a clean CONFIRMED on the third call.
    wallet._client.get = AsyncMock(side_effect=[fail, fail, ok])  # type: ignore[method-assign]

    result = await wallet.wait_for_tx(
        "real-circle-uuid", poll_seconds=0.01, timeout_seconds=5.0
    )
    assert result.state == "CONFIRMED"
    assert result.tx_hash == "0xabc"
    assert wallet._client.get.await_count == 3  # retried twice, then ok
    await wallet.aclose()


@pytest.mark.asyncio
async def test_wait_for_tx_short_circuits_on_dryrun_prefix() -> None:
    """Synthetic ``dryrun-*`` ids never hit the network."""
    wallet = _make_wallet()
    wallet._client.get = AsyncMock()  # type: ignore[method-assign]
    result = await wallet.wait_for_tx("dryrun-abc123")
    assert result.state == "DRY_RUN"
    wallet._client.get.assert_not_called()
    await wallet.aclose()


# ---------------------------------------------------------------------------
# main._is_circle_tx_id — venue discriminator
# ---------------------------------------------------------------------------


def test_is_circle_tx_id_accepts_uuid() -> None:
    from main import _is_circle_tx_id

    assert _is_circle_tx_id("8b3b3b3b-1234-5678-9abc-def012345678") is True
    # Mixed case is fine - UUIDs are case-insensitive.
    assert _is_circle_tx_id("8B3B3B3B-1234-5678-9ABC-DEF012345678") is True


def test_is_circle_tx_id_rejects_hyperliquid_order_id() -> None:
    from main import _is_circle_tx_id

    # The exact id that triggered the original 400-storm bug.
    assert _is_circle_tx_id("53624986552") is False


def test_is_circle_tx_id_rejects_synthetic_markers() -> None:
    from main import _is_circle_tx_id

    assert _is_circle_tx_id("dryrun-hl-open-dec-1779615111-abc") is False
    assert _is_circle_tx_id("hl-error-dec-1779615111-abc") is False
    assert _is_circle_tx_id("noop-hl-hold-dec-1779615111-abc") is False
    assert _is_circle_tx_id("hl-dust-dec-1779615111-abc") is False


def test_is_circle_tx_id_rejects_empty_and_garbage() -> None:
    from main import _is_circle_tx_id

    assert _is_circle_tx_id("") is False
    assert _is_circle_tx_id("not-a-uuid") is False
    # Almost a UUID but wrong length.
    assert _is_circle_tx_id("8b3b3b3b-1234-5678-9abc-def01234") is False
