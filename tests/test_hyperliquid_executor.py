"""Unit tests for the Hyperliquid Testnet executor.

We don't touch the real Hyperliquid API in tests - every SDK call is
mocked at the ``Info`` / ``Exchange`` boundary. This gives us:

* Deterministic assertions on the order payloads we send.
* Coverage of every state-machine branch (filled / resting / rejected
  / SDK exception).
* CI compatibility - no faucet, no testnet RPC, no network.

The fakes mirror the real SDK's surface so that swapping in the real
``hyperliquid.exchange.Exchange`` later is a one-import-change.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest

from src.execution.arc_perp_executor import Position
from src.execution.hyperliquid_executor import (
    HyperliquidConfig,
    HyperliquidExecutor,
)


# --------------------------------------------------------------------------
# Test fakes (Info / Exchange SDK doubles)
# --------------------------------------------------------------------------


class _FakeInfo:
    """Mocks the ``hyperliquid.info.Info`` surface the executor reads."""

    def __init__(
        self,
        user_state_payload: dict[str, Any] | None = None,
        mids: dict[str, str] | None = None,
        raise_on_user_state: bool = False,
        meta_payload: dict[str, Any] | None = None,
    ) -> None:
        self._user_state = user_state_payload or {
            "crossMarginSummary": {
                "accountValue": "1000",
                "totalMarginUsed": "0",
                "totalNtlPos": "0",
                "totalRawUsd": "0",
            },
            "assetPositions": [],
        }
        self._mids = mids or {"BTC": "60000", "ETH": "3000"}
        self._raise_on_user_state = raise_on_user_state
        # ``meta_payload`` mirrors the shape returned by the real SDK
        # ``Info.meta()``: ``{"universe": [{"name": "BTC",
        # "szDecimals": 5}, ...]}``. The executor caches the answer
        # per-coin so this is read at most once per (executor, coin).
        # ``None`` simulates the legacy-fake case where ``meta`` is
        # absent on the Info client and the executor falls back to
        # the conservative ``_HL_SZ_DECIMALS_FALLBACK`` table.
        self._meta_payload = meta_payload
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    def user_state(self, address: str) -> dict[str, Any]:
        self.calls.append(("user_state", (address,)))
        if self._raise_on_user_state:
            raise RuntimeError("network unreachable")
        return self._user_state

    def all_mids(self) -> dict[str, str]:
        self.calls.append(("all_mids", ()))
        return self._mids

    def meta(self) -> dict[str, Any]:
        """SDK ``Info.meta()`` shim - returns the asset universe.

        Only invoked when the executor needs ``szDecimals`` to round
        a trigger price (TP / SL submission). Tests that don't
        construct ``_FakeInfo`` with ``meta_payload`` are exercising
        the executor's "no meta available" fallback path, which is
        why ``meta`` must be present on the fake (otherwise
        ``getattr(info, "meta", None)`` would return None and we'd
        never see the cache populated even when the test wants it to).
        """
        self.calls.append(("meta", ()))
        if self._meta_payload is None:
            # Mimic the real SDK shape but with a single common asset
            # so tests that don't care about exact szDecimals still
            # get sensible (BTC=5) rounding behaviour. BTC + ETH is
            # enough for every code path in the executor today.
            return {
                "universe": [
                    {"name": "BTC", "szDecimals": 5},
                    {"name": "ETH", "szDecimals": 4},
                ]
            }
        return self._meta_payload


class _FakeExchange:
    """Mocks the ``hyperliquid.exchange.Exchange`` surface.

    Captures every call so the test can assert on the side / coin /
    size / leverage / slippage that the executor passed in, and lets
    the test script the SDK ack via ``next_resp``.
    """

    def __init__(self, next_resp: dict[str, Any] | None = None) -> None:
        self.next_resp = next_resp or {
            "status": "ok",
            "response": {
                "type": "order",
                "data": {
                    "statuses": [
                        {"filled": {"oid": 42, "avgPx": "60000", "totalSz": "0.01"}}
                    ]
                },
            },
        }
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.raise_on_call: type[BaseException] | None = None

    def update_leverage(self, leverage: int, coin: str, is_cross: bool) -> Any:
        self.calls.append(
            ("update_leverage", {"leverage": leverage, "coin": coin, "is_cross": is_cross})
        )
        return {"status": "ok"}

    def market_open(
        self,
        coin: str,
        is_buy: bool,
        sz: float,
        px: float | None,
        slippage: float,
    ) -> dict[str, Any]:
        self.calls.append(
            (
                "market_open",
                {
                    "coin": coin,
                    "is_buy": is_buy,
                    "sz": sz,
                    "px": px,
                    "slippage": slippage,
                },
            )
        )
        if self.raise_on_call:
            raise self.raise_on_call("simulated SDK failure")
        return self.next_resp

    def market_close(
        self,
        coin: str,
        sz: float | None,
        px: float | None,
        slippage: float,
    ) -> dict[str, Any]:
        self.calls.append(
            (
                "market_close",
                {"coin": coin, "sz": sz, "px": px, "slippage": slippage},
            )
        )
        if self.raise_on_call:
            raise self.raise_on_call("simulated SDK failure")
        return self.next_resp

    def order(
        self,
        coin: str,
        is_buy: bool,
        sz: float,
        limit_px: float,
        order_type: dict[str, Any],
        reduce_only: bool = False,
    ) -> dict[str, Any]:
        self.calls.append(
            (
                "order",
                {
                    "coin": coin,
                    "is_buy": is_buy,
                    "sz": sz,
                    "limit_px": limit_px,
                    "order_type": order_type,
                    "reduce_only": reduce_only,
                },
            )
        )
        return self.next_resp

    def withdraw_from_bridge(self, amount: float, address: str) -> dict[str, Any]:
        self.calls.append(
            ("withdraw_from_bridge", {"amount": amount, "address": address})
        )
        return {"status": "ok", "nonce": 99}


def _make_executor(
    *,
    info: _FakeInfo | None = None,
    exchange: _FakeExchange | None = None,
    dry_run: bool = False,
    account: str = "0x" + "ab" * 20,
    **cfg_overrides: Any,
) -> HyperliquidExecutor:
    base_kwargs: dict[str, Any] = {
        "private_key": None,
        "account_address": account,
        "api_url": "https://api.hyperliquid-testnet.xyz",
        "max_leverage": 5,
        "max_position_usd": Decimal("10000"),
        "default_slippage_bps": 50,
    }
    base_kwargs.update(cfg_overrides)
    cfg = HyperliquidConfig(**base_kwargs)
    ex = HyperliquidExecutor(
        config=cfg,
        dry_run=dry_run,
        info_client=info or _FakeInfo(),
        exchange_client=exchange or _FakeExchange(),
    )
    return ex


# --------------------------------------------------------------------------
# Read-only methods
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_mid_price_maps_symbol_to_coin() -> None:
    info = _FakeInfo(mids={"BTC": "63000.5"})
    ex = _make_executor(info=info)
    px = await ex.get_mid_price("BTC-PERP")
    assert px == Decimal("63000.5")
    # all_mids was called once
    assert any(call[0] == "all_mids" for call in info.calls)


@pytest.mark.asyncio
async def test_get_mid_price_handles_missing_coin() -> None:
    info = _FakeInfo(mids={"ETH": "3000"})
    ex = _make_executor(info=info)
    assert await ex.get_mid_price("BTC-PERP") is None


@pytest.mark.asyncio
async def test_get_account_info_renders_positions_and_margin() -> None:
    user_state = {
        "crossMarginSummary": {
            "accountValue": "1500.50",
            "totalMarginUsed": "300",
            "totalNtlPos": "0",
            "totalRawUsd": "0",
        },
        "assetPositions": [
            {
                "position": {
                    "coin": "BTC",
                    "szi": "0.05",         # long 0.05 BTC
                    "entryPx": "60000",
                    "unrealizedPnl": "25",
                    "leverage": {"value": "3"},
                }
            },
            {
                "position": {
                    "coin": "ETH",
                    "szi": "-0.5",         # short 0.5 ETH
                    "entryPx": "3000",
                    "unrealizedPnl": "-10",
                    "leverage": {"value": "5"},
                }
            },
        ],
    }
    info = _FakeInfo(user_state_payload=user_state, mids={"BTC": "61000", "ETH": "2900"})
    ex = _make_executor(info=info)
    account = await ex.get_account_info()
    assert account.equity_usd == Decimal("1500.50")
    assert account.used_margin_usd == Decimal("300")
    assert account.free_margin_usd == Decimal("1200.50")
    assert len(account.positions) == 2
    btc = next(p for p in account.positions if p.symbol == "BTC-PERP")
    eth = next(p for p in account.positions if p.symbol == "ETH-PERP")
    assert btc.side == "long"
    assert btc.size_usd == Decimal("0.05") * Decimal("61000")
    assert btc.leverage == Decimal("3")
    assert btc.unrealized_pnl_usd == Decimal("25")
    assert eth.side == "short"
    assert eth.size_usd == Decimal("0.5") * Decimal("2900")
    assert eth.unrealized_pnl_usd == Decimal("-10")


@pytest.mark.asyncio
async def test_get_position_returns_flat_when_missing() -> None:
    ex = _make_executor()
    pos = await ex.get_position("BTC-PERP")
    assert pos.side == "flat"
    assert pos.size_usd == Decimal("0")


@pytest.mark.asyncio
async def test_get_account_info_survives_network_failure() -> None:
    info = _FakeInfo(raise_on_user_state=True)
    ex = _make_executor(info=info)
    account = await ex.get_account_info()
    assert account.equity_usd == Decimal("0")
    assert account.positions == []


@pytest.mark.asyncio
async def test_get_account_info_zero_pnl_for_usdc_only_wallet() -> None:
    """USDC-only wallet (no positions) must report PnL=0, not -deposit.

    Regression for the "Drawdown 100%" bug: before the fix the fallback
    formula computed ``totalNtlPos - totalRawUsd`` which returned the
    full negative balance for a freshly-funded wallet (totalNtlPos=0
    because no positions are open, totalRawUsd=deposit). The CLI then
    rendered ``Unrealized PnL = -$900`` and ``Drawdown = 100%`` for a
    healthy account. The corrected formula uses
    ``accountValue - totalRawUsd`` (= 0 with no positions) and matches
    the assetPositions walk when positions exist.
    """
    user_state = {
        "marginSummary": {
            "accountValue": "900",
            "totalMarginUsed": "0",
            "totalNtlPos": "0",
            "totalRawUsd": "900",
        },
        "assetPositions": [],
    }
    info = _FakeInfo(user_state_payload=user_state, mids={})
    ex = _make_executor(info=info)
    account = await ex.get_account_info()
    assert account.equity_usd == Decimal("900")
    assert account.free_margin_usd == Decimal("900")
    assert account.total_unrealized_pnl_usd == Decimal("0")
    assert account.positions == []


@pytest.mark.asyncio
async def test_get_account_info_prefers_margin_summary_over_cross() -> None:
    """``marginSummary`` (cross + isolated) must win over ``crossMarginSummary``.

    Real Hyperliquid testnet accounts in isolated-margin / Manual mode
    return ``crossMarginSummary.accountValue=0`` while
    ``marginSummary.accountValue`` carries the real equity. The
    executor must read the latter, otherwise sizing pipelines see $0
    equity and refuse to open any new position.
    """
    user_state = {
        "marginSummary": {
            "accountValue": "984.96",
            "totalMarginUsed": "0",
            "totalNtlPos": "0",
            "totalRawUsd": "984.96",
        },
        "crossMarginSummary": {
            "accountValue": "0",
            "totalMarginUsed": "0",
            "totalNtlPos": "0",
            "totalRawUsd": "0",
        },
        "assetPositions": [],
    }
    info = _FakeInfo(user_state_payload=user_state, mids={})
    ex = _make_executor(info=info)
    account = await ex.get_account_info()
    assert account.equity_usd == Decimal("984.96")
    assert account.total_unrealized_pnl_usd == Decimal("0")


# --------------------------------------------------------------------------
# get_open_positions - canonical fresh pull used by PositionManager
# resync to defeat the "snapshot says no positions" desync bug.
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_open_positions_returns_only_live_positions() -> None:
    """A live short + a closed-flat coin: only the short is returned."""
    user_state = {
        "crossMarginSummary": {
            "accountValue": "1000",
            "totalMarginUsed": "0",
            "totalNtlPos": "0",
            "totalRawUsd": "0",
        },
        "assetPositions": [
            {
                "position": {
                    "coin": "BTC",
                    "szi": "-0.05",            # active short
                    "entryPx": "60000",
                    "unrealizedPnl": "0",
                    "leverage": {"value": "3"},
                }
            },
            {
                "position": {
                    "coin": "ETH",
                    "szi": "0",                # closed (Hyperliquid leaves
                    "entryPx": "0",            # the asset entry, side=flat)
                    "unrealizedPnl": "0",
                    "leverage": {"value": "0"},
                }
            },
        ],
    }
    info = _FakeInfo(
        user_state_payload=user_state,
        mids={"BTC": "60500", "ETH": "3000"},
    )
    ex = _make_executor(info=info)
    positions = await ex.get_open_positions()
    assert len(positions) == 1
    assert positions[0].symbol == "BTC-PERP"
    assert positions[0].side == "short"
    assert positions[0].size_usd > 0


@pytest.mark.asyncio
async def test_get_open_positions_returns_empty_on_network_failure() -> None:
    """user_state raising must not break the cycle - empty list, no raise."""
    info = _FakeInfo(raise_on_user_state=True)
    ex = _make_executor(info=info)
    positions = await ex.get_open_positions()
    assert positions == []


@pytest.mark.asyncio
async def test_get_open_positions_returns_empty_when_no_assets() -> None:
    """No assetPositions at all -> empty list, no exception."""
    user_state = {
        "crossMarginSummary": {
            "accountValue": "1000",
            "totalMarginUsed": "0",
            "totalNtlPos": "0",
            "totalRawUsd": "0",
        },
        "assetPositions": [],
    }
    info = _FakeInfo(user_state_payload=user_state)
    ex = _make_executor(info=info)
    positions = await ex.get_open_positions()
    assert positions == []


@pytest.mark.asyncio
async def test_get_open_positions_independently_pulls_user_state() -> None:
    """The method must issue its OWN user_state call - not reuse a cache.

    This is the architectural contract that makes PositionManager
    resync work: every call goes back to the SDK so eventual-
    consistency right after a market_open can be re-checked.
    """
    user_state = {
        "crossMarginSummary": {
            "accountValue": "1000",
            "totalMarginUsed": "0",
            "totalNtlPos": "0",
            "totalRawUsd": "0",
        },
        "assetPositions": [
            {
                "position": {
                    "coin": "BTC",
                    "szi": "0.05",
                    "entryPx": "60000",
                    "unrealizedPnl": "0",
                    "leverage": {"value": "3"},
                }
            },
        ],
    }
    info = _FakeInfo(
        user_state_payload=user_state,
        mids={"BTC": "60500"},
    )
    ex = _make_executor(info=info)
    await ex.get_open_positions()
    await ex.get_open_positions()
    # Two calls to the executor method = two calls to user_state.
    user_state_calls = [c for c in info.calls if c[0] == "user_state"]
    assert len(user_state_calls) == 2


# --------------------------------------------------------------------------
# open_position
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_open_position_dry_run_logs_planned_order() -> None:
    ex = _make_executor(dry_run=True)
    res = await ex.open_position(
        symbol="BTC-PERP",
        side="long",
        size_usd=Decimal("1000"),
        leverage=Decimal("3"),
        decision_id="dec-test-1",
    )
    assert res.state == "DRY_RUN"
    assert res.raw["planned_order"]["coin"] == "BTC"
    assert res.raw["planned_order"]["is_buy"] is True
    assert res.raw["planned_order"]["leverage"] == "3"


@pytest.mark.asyncio
async def test_open_position_clamps_leverage_to_max() -> None:
    exchange = _FakeExchange()
    ex = _make_executor(exchange=exchange, max_leverage=3)
    await ex.open_position(
        symbol="BTC-PERP",
        side="long",
        size_usd=Decimal("100"),
        leverage=Decimal("20"),  # asks for 20x
        decision_id="dec-clamp",
    )
    update_call = next(c for c in exchange.calls if c[0] == "update_leverage")
    assert update_call[1]["leverage"] == 3  # clamped


@pytest.mark.asyncio
async def test_open_position_clamps_size_to_max_position() -> None:
    exchange = _FakeExchange()
    ex = _make_executor(exchange=exchange, max_position_usd=Decimal("500"))
    await ex.open_position(
        symbol="BTC-PERP",
        side="long",
        size_usd=Decimal("100000"),  # absurd
        leverage=Decimal("3"),
        decision_id="dec-cap",
    )
    market_call = next(c for c in exchange.calls if c[0] == "market_open")
    # $500 notional / $60000 mid ~ 0.0083 BTC
    assert market_call[1]["sz"] < 0.01


@pytest.mark.asyncio
async def test_open_position_submits_market_order_with_right_side() -> None:
    exchange = _FakeExchange()
    ex = _make_executor(exchange=exchange)
    await ex.open_position(
        symbol="ETH-PERP",
        side="short",
        size_usd=Decimal("600"),
        leverage=Decimal("2"),
        slippage_bps=100,
        decision_id="dec-eth-short",
    )
    market_call = next(c for c in exchange.calls if c[0] == "market_open")
    assert market_call[1]["coin"] == "ETH"
    assert market_call[1]["is_buy"] is False
    # 100 bps slippage -> 0.01 fraction
    assert market_call[1]["slippage"] == pytest.approx(0.01)


@pytest.mark.asyncio
async def test_open_position_handles_sdk_exception() -> None:
    exchange = _FakeExchange()
    exchange.raise_on_call = RuntimeError
    ex = _make_executor(exchange=exchange)
    res = await ex.open_position(
        symbol="BTC-PERP",
        side="long",
        size_usd=Decimal("100"),
        decision_id="dec-fail",
    )
    assert res.state == "FAILED"
    assert "simulated SDK failure" in res.raw["error"]


@pytest.mark.asyncio
async def test_open_position_parses_filled_ack() -> None:
    exchange = _FakeExchange(
        next_resp={
            "status": "ok",
            "response": {
                "type": "order",
                "data": {
                    "statuses": [
                        {"filled": {"oid": 999, "avgPx": "60123.45", "totalSz": "0.5"}}
                    ]
                },
            },
        }
    )
    ex = _make_executor(exchange=exchange)
    res = await ex.open_position(
        symbol="BTC-PERP",
        side="long",
        size_usd=Decimal("100"),
        decision_id="dec-fill",
    )
    assert res.state == "CONFIRMED"
    assert res.tx_id == "999"
    assert res.raw["avg_price"] == "60123.45"


@pytest.mark.asyncio
async def test_open_position_parses_rejection() -> None:
    exchange = _FakeExchange(
        next_resp={"status": "err", "response": "insufficient margin"}
    )
    ex = _make_executor(exchange=exchange)
    res = await ex.open_position(
        symbol="BTC-PERP",
        side="long",
        size_usd=Decimal("100"),
        decision_id="dec-reject",
    )
    assert res.state == "DENIED"
    assert res.raw["rejection_reason"] == "insufficient margin"


# --------------------------------------------------------------------------
# close_position
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_close_position_skips_when_flat() -> None:
    ex = _make_executor()
    res = await ex.close_position(symbol="BTC-PERP", decision_id="dec-flat")
    assert res.state in ("DRY_RUN", "SKIPPED")


@pytest.mark.asyncio
async def test_close_position_submits_market_close_when_open() -> None:
    user_state = {
        "crossMarginSummary": {
            "accountValue": "1000",
            "totalMarginUsed": "100",
            "totalNtlPos": "0",
            "totalRawUsd": "0",
        },
        "assetPositions": [
            {
                "position": {
                    "coin": "BTC",
                    "szi": "0.02",
                    "entryPx": "60000",
                    "unrealizedPnl": "5",
                    "leverage": {"value": "3"},
                }
            }
        ],
    }
    info = _FakeInfo(user_state_payload=user_state)
    exchange = _FakeExchange()
    ex = _make_executor(info=info, exchange=exchange)
    res = await ex.close_position(symbol="BTC-PERP", decision_id="dec-close")
    assert res.state == "CONFIRMED"
    close_call = next(c for c in exchange.calls if c[0] == "market_close")
    assert close_call[1]["coin"] == "BTC"


# --------------------------------------------------------------------------
# withdraw_all_margin
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_withdraw_all_margin_dry_run_returns_synthetic() -> None:
    user_state = {
        "crossMarginSummary": {
            "accountValue": "500",
            "totalMarginUsed": "0",
            "totalNtlPos": "0",
            "totalRawUsd": "0",
        },
        "assetPositions": [],
    }
    info = _FakeInfo(user_state_payload=user_state)
    ex = _make_executor(info=info, dry_run=True)
    res = await ex.withdraw_all_margin(decision_id="dec-wd")
    assert res.state == "DRY_RUN"


@pytest.mark.asyncio
async def test_withdraw_all_margin_live_calls_bridge() -> None:
    user_state = {
        "crossMarginSummary": {
            "accountValue": "750",
            "totalMarginUsed": "0",
            "totalNtlPos": "0",
            "totalRawUsd": "0",
        },
        "assetPositions": [],
    }
    info = _FakeInfo(user_state_payload=user_state)
    exchange = _FakeExchange()
    ex = _make_executor(info=info, exchange=exchange)
    res = await ex.withdraw_all_margin(decision_id="dec-wd-live")
    assert res.state == "CONFIRMED"
    bridge_call = next(c for c in exchange.calls if c[0] == "withdraw_from_bridge")
    assert bridge_call[1]["amount"] == pytest.approx(750.0)


# --------------------------------------------------------------------------
# Symbol mapping
# --------------------------------------------------------------------------


def test_to_hl_coin_strips_perp_suffix() -> None:
    assert HyperliquidExecutor._to_hl_coin("BTC-PERP") == "BTC"
    assert HyperliquidExecutor._to_hl_coin("eth-perp") == "ETH"
    assert HyperliquidExecutor._to_hl_coin("SOL") == "SOL"


def test_from_hl_coin_appends_perp_suffix() -> None:
    assert HyperliquidExecutor._from_hl_coin("BTC") == "BTC-PERP"
    assert HyperliquidExecutor._from_hl_coin("eth") == "ETH-PERP"


# --------------------------------------------------------------------------
# Side validation
# --------------------------------------------------------------------------


def test_side_is_long_validates_inputs() -> None:
    assert HyperliquidExecutor._side_is_long("long") is True
    assert HyperliquidExecutor._side_is_long("LONG") is True
    assert HyperliquidExecutor._side_is_long("short") is False
    with pytest.raises(ValueError):
        HyperliquidExecutor._side_is_long("sideways")


# --------------------------------------------------------------------------
# update_take_profit_stop_loss
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tp_sl_skipped_when_flat_and_no_hint() -> None:
    ex = _make_executor()
    results = await ex.update_take_profit_stop_loss(
        symbol="BTC-PERP",
        tp_price=Decimal("65000"),
        sl_price=Decimal("58000"),
        decision_id="dec-tp",
    )
    assert results == []


@pytest.mark.asyncio
async def test_tp_sl_submits_one_trigger_each_when_position_open() -> None:
    user_state = {
        "crossMarginSummary": {
            "accountValue": "1000",
            "totalMarginUsed": "100",
            "totalNtlPos": "0",
            "totalRawUsd": "0",
        },
        "assetPositions": [
            {
                "position": {
                    "coin": "BTC",
                    "szi": "0.01",
                    "entryPx": "60000",
                    "unrealizedPnl": "0",
                    "leverage": {"value": "3"},
                }
            }
        ],
    }
    info = _FakeInfo(user_state_payload=user_state, mids={"BTC": "60000"})
    exchange = _FakeExchange()
    ex = _make_executor(info=info, exchange=exchange)
    results = await ex.update_take_profit_stop_loss(
        symbol="BTC-PERP",
        tp_price=Decimal("65000"),
        sl_price=Decimal("58000"),
        decision_id="dec-tp-sl",
    )
    # One TP + one SL = 2 results, 2 SDK ``order`` calls.
    assert len(results) == 2
    order_calls = [c for c in exchange.calls if c[0] == "order"]
    assert len(order_calls) == 2
    for _name, kwargs in order_calls:
        assert kwargs["coin"] == "BTC"
        # Close side for a LONG position is SELL (is_buy=False).
        assert kwargs["is_buy"] is False
        assert kwargs["reduce_only"] is True
        assert "trigger" in kwargs["order_type"]


@pytest.mark.asyncio
async def test_tp_sl_uses_caller_hint_when_venue_says_flat() -> None:
    """Regression: post-open propagation race used to silently skip TP/SL.

    Right after ``open_position`` returns, the Hyperliquid Info
    endpoint can briefly read back ``assetPositions: []`` even though
    the fill is real. Without the caller hint, the executor used to
    look up ``get_position`` -> "flat" -> skip TP/SL, leaving the
    venue without a safety net for the entire position lifetime.

    This test exercises the bypass path: the caller passes
    ``position_side`` + ``position_size_usd`` from the open it just
    submitted, and the executor honours it without consulting
    ``user_state``.
    """
    # Empty assetPositions -> simulates the propagation lag.
    info = _FakeInfo(
        user_state_payload={
            "crossMarginSummary": {
                "accountValue": "1000",
                "totalMarginUsed": "0",
                "totalNtlPos": "0",
                "totalRawUsd": "0",
            },
            "assetPositions": [],
        },
        mids={"BTC": "60000"},
    )
    exchange = _FakeExchange()
    ex = _make_executor(info=info, exchange=exchange)

    results = await ex.update_take_profit_stop_loss(
        symbol="BTC-PERP",
        tp_price=Decimal("65000"),
        sl_price=Decimal("58000"),
        decision_id="dec-tp-race",
        position_side="long",
        position_size_usd=Decimal("600"),
    )
    assert len(results) == 2
    order_calls = [c for c in exchange.calls if c[0] == "order"]
    assert len(order_calls) == 2
    # 600 USD / 60000 mid = 0.01 BTC, rounded to 4dp.
    for _name, kwargs in order_calls:
        assert kwargs["coin"] == "BTC"
        assert kwargs["is_buy"] is False  # LONG -> close is SELL
        assert kwargs["reduce_only"] is True
        assert kwargs["sz"] == 0.01


@pytest.mark.asyncio
async def test_tp_sl_caller_hint_short_inverts_close_side() -> None:
    info = _FakeInfo(
        user_state_payload={
            "crossMarginSummary": {
                "accountValue": "1000",
                "totalMarginUsed": "0",
                "totalNtlPos": "0",
                "totalRawUsd": "0",
            },
            "assetPositions": [],
        },
        mids={"BTC": "60000"},
    )
    exchange = _FakeExchange()
    ex = _make_executor(info=info, exchange=exchange)

    results = await ex.update_take_profit_stop_loss(
        symbol="BTC-PERP",
        tp_price=Decimal("55000"),  # SHORT TP below entry
        sl_price=Decimal("63000"),  # SHORT SL above entry
        decision_id="dec-short-tpsl",
        position_side="short",
        position_size_usd=Decimal("600"),
    )
    assert len(results) == 2
    order_calls = [c for c in exchange.calls if c[0] == "order"]
    for _name, kwargs in order_calls:
        # Close side for a SHORT position is BUY (is_buy=True).
        assert kwargs["is_buy"] is True
        assert kwargs["reduce_only"] is True


@pytest.mark.asyncio
async def test_tp_sl_caller_hint_rejected_on_bad_input() -> None:
    """Bad caller hint short-circuits without falling back to flat read."""
    info = _FakeInfo(mids={"BTC": "60000"})
    exchange = _FakeExchange()
    ex = _make_executor(info=info, exchange=exchange)

    # Garbage side -> abort, no orders submitted.
    results = await ex.update_take_profit_stop_loss(
        symbol="BTC-PERP",
        tp_price=Decimal("65000"),
        sl_price=Decimal("58000"),
        decision_id="dec-bad-hint",
        position_side="sideways",
        position_size_usd=Decimal("600"),
    )
    assert results == []
    assert not [c for c in exchange.calls if c[0] == "order"]

    # Zero size -> abort, no orders submitted.
    results = await ex.update_take_profit_stop_loss(
        symbol="BTC-PERP",
        tp_price=Decimal("65000"),
        sl_price=Decimal("58000"),
        decision_id="dec-zero-size",
        position_side="long",
        position_size_usd=Decimal("0"),
    )
    assert results == []


# --------------------------------------------------------------------------
# Tick-size rounding (Hyperliquid pricing rule)
# --------------------------------------------------------------------------


def test_round_hl_price_btc_typical_collapses_to_integer() -> None:
    """BTC perp (szDecimals=5, max 1 decimal) at ~$77k -> integer tick.

    Regression for the silent FAILED-trigger bug: AllocationRouter
    used to hand the executor a raw Decimal like
    ``77629.420523686994833332`` (18 decimal places). The SDK then
    raised on the order RPC call because the price violates the
    "5 sig figs AND <= (6 - szDecimals) decimals" rule. With BTC
    szDecimals=5 the only valid representation in the $77k range is
    the integer dollar (5 sig figs already maxed out).
    """
    rounded = HyperliquidExecutor._round_hl_price(
        Decimal("77629.420523686994833332"), sz_decimals=5
    )
    assert rounded == Decimal("77629")


def test_round_hl_price_btc_six_figs_rounds_to_5_sig() -> None:
    """BTC at $112,345.67 -> $112,350 (5 sig figs, integer).

    The sig-fig rule is stricter than the decimal-place rule here:
    BTC szDecimals=5 nominally allows 1 decimal, but $112,345.7
    would be 6 sig figs. The integer-allowed clause lets us round
    up to multiples of 10 to land back at 5 sig figs.
    """
    rounded = HyperliquidExecutor._round_hl_price(
        Decimal("112345.67"), sz_decimals=5
    )
    assert rounded == Decimal("112350")


def test_round_hl_price_eth_keeps_one_decimal() -> None:
    """ETH (szDecimals=4, max 2 decimals) at $2700.55.

    5 sig figs caps the precision before the decimal rule does:
    2700.55 -> 6 sig figs (too many) -> round to 2700.6 (5 sig
    figs, 1 decimal — still within the 2-decimal cap).
    """
    rounded = HyperliquidExecutor._round_hl_price(
        Decimal("2700.55"), sz_decimals=4
    )
    assert rounded == Decimal("2700.6")


def test_round_hl_price_eth_already_valid_passthrough() -> None:
    """Already-valid price round-trips unchanged (idempotency)."""
    valid = Decimal("2700.6")
    assert HyperliquidExecutor._round_hl_price(valid, sz_decimals=4) == valid
    # Integer prices are always allowed -> unchanged.
    assert HyperliquidExecutor._round_hl_price(
        Decimal("60000"), sz_decimals=5
    ) == Decimal("60000")


def test_round_hl_price_rejects_non_positive() -> None:
    """Zero / negative prices pass through (caller already handles them)."""
    assert HyperliquidExecutor._round_hl_price(
        Decimal("0"), sz_decimals=5
    ) == Decimal("0")
    assert HyperliquidExecutor._round_hl_price(
        Decimal("-1"), sz_decimals=5
    ) == Decimal("-1")


@pytest.mark.asyncio
async def test_sz_decimals_reads_and_caches_from_meta() -> None:
    """``Info.meta()`` is read once per coin and cached."""
    info = _FakeInfo(
        meta_payload={
            "universe": [
                {"name": "BTC", "szDecimals": 5},
                {"name": "ETH", "szDecimals": 4},
                {"name": "SOL", "szDecimals": 2},
            ]
        }
    )
    ex = _make_executor(info=info)
    assert await ex._sz_decimals("BTC-PERP") == 5
    assert await ex._sz_decimals("ETH-PERP") == 4
    assert await ex._sz_decimals("SOL-PERP") == 2
    # Cache hits on subsequent calls — no second meta() request.
    assert await ex._sz_decimals("BTC-PERP") == 5
    assert await ex._sz_decimals("ETH-PERP") == 4
    meta_calls = [c for c in info.calls if c[0] == "meta"]
    # The implementation warms the full universe on first hit so a
    # single ``meta()`` call serves every subsequent lookup.
    assert len(meta_calls) == 1


@pytest.mark.asyncio
async def test_sz_decimals_falls_back_when_meta_unavailable() -> None:
    """Info client without ``meta`` -> fall back to the static table.

    Older / legacy test fakes don't expose ``meta``; the executor
    must still produce *some* szDecimals so rounding can apply
    (otherwise the bug we're fixing here regresses for those code
    paths).
    """
    class _LegacyInfo(_FakeInfo):
        # Surgically remove ``meta`` so ``getattr(info, "meta", None)``
        # returns None — exercises the fallback branch.
        meta = None  # type: ignore[assignment]

    info = _LegacyInfo()
    ex = _make_executor(info=info)
    # BTC + ETH have explicit fallback entries.
    assert await ex._sz_decimals("BTC-PERP") == 5
    assert await ex._sz_decimals("ETH-PERP") == 4
    # Unknown coin falls back to the ETH default (4).
    assert await ex._sz_decimals("DOESNOTEXIST-PERP") == 4


@pytest.mark.asyncio
async def test_tp_sl_rounds_trigger_price_before_sdk_call() -> None:
    """Regression: TP / SL trigger price is venue-rounded before SDK call.

    Before this fix, AllocationRouter handed
    ``update_take_profit_stop_loss`` a raw Decimal like
    ``77629.420523686994833332`` (18 decimal places). Hyperliquid's
    SDK raised on the wire because the price violates the "5 sig
    figs AND <= (6 - szDecimals) decimals" rule, and the executor
    rendered the failure as a synthetic ``hl-tp-error-...`` /
    ``hl-sl-error-...`` FAILED ``TxResult`` while the entry order
    survived without a venue-side safety net.

    The fix: ``update_take_profit_stop_loss`` now quantizes
    ``trigger_px`` via :meth:`_round_hl_price` (using cached
    ``szDecimals``) BEFORE the SDK call. This test pins both halves
    of that contract — the rounded ``trigger_px`` reaches the SDK,
    AND the original raw value is preserved in ``TxResult.raw``
    for forensics.
    """
    info = _FakeInfo(
        mids={"BTC": "77385.0"},
        meta_payload={"universe": [{"name": "BTC", "szDecimals": 5}]},
    )
    exchange = _FakeExchange()
    ex = _make_executor(info=info, exchange=exchange)

    raw_tp = Decimal("76773.948690782512916670")  # exact reproducer
    raw_sl = Decimal("77629.420523686994833332")
    results = await ex.update_take_profit_stop_loss(
        symbol="BTC-PERP",
        tp_price=raw_tp,
        sl_price=raw_sl,
        decision_id="dec-rounding",
        position_side="short",
        position_size_usd=Decimal("46.76"),
    )
    assert len(results) == 2

    order_calls = [c for c in exchange.calls if c[0] == "order"]
    assert len(order_calls) == 2

    # Both legs hit the wire with the rounded integer price.
    # BTC at 5 szDecimals -> max 1 decimal AND 5 sig figs -> integer.
    tp_call = order_calls[0][1]
    sl_call = order_calls[1][1]
    assert tp_call["limit_px"] == 76774.0
    assert sl_call["limit_px"] == 77629.0
    # The triggerPx inside the order_type dict must match the rounded
    # limit price (same source) — otherwise the venue would see a
    # mismatched limit vs trigger and reject.
    assert tp_call["order_type"]["trigger"]["triggerPx"] == 76774.0
    assert sl_call["order_type"]["trigger"]["triggerPx"] == 77629.0

    # The raw (unrounded) price is preserved in the TxResult for
    # forensics — useful to compare against the venue's view in a
    # post-mortem and to spot drift between ATR-derived signal and
    # tick-rounded reality. Non-dry-run path flattens ``planned``
    # into ``TxResult.raw`` via ``_ack_to_tx_result``.
    assert results[0].raw["trigger_px"] == "76774"
    assert results[0].raw["trigger_px_raw"] == str(raw_tp)
    assert results[1].raw["trigger_px"] == "77629"
    assert results[1].raw["trigger_px_raw"] == str(raw_sl)


@pytest.mark.asyncio
async def test_round_price_helper_is_idempotent_async() -> None:
    """Public ``round_price`` matches the static rule and is idempotent."""
    info = _FakeInfo(meta_payload={"universe": [{"name": "BTC", "szDecimals": 5}]})
    ex = _make_executor(info=info)
    once = await ex.round_price("BTC-PERP", Decimal("77629.42"))
    twice = await ex.round_price("BTC-PERP", once)
    assert once == twice == Decimal("77629")
