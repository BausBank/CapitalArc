"""Hyperliquid Testnet executor - primary trading venue for CapitalArc.

Why Hyperliquid (replacing Arc Perp DEX)
----------------------------------------
Arc Perp DEX never published a public matcher / EIP-712 OrderTypes
spec, so building the live trading leg against it was blocked
indefinitely. Hyperliquid is a battle-tested perp DEX with:

* A public testnet endpoint (``https://api.hyperliquid-testnet.xyz``)
  funded via a faucet, so the agent can place real long / short
  positions without risking capital.
* A maintained Python SDK (``hyperliquid-python-sdk``) that handles
  the non-trivial EIP-712 signing scheme (msgpack-encoded "action"
  payload over a phantom-agent L1 EIP-712 domain).
* A clean REST API (``POST /exchange`` for trading, ``POST /info``
  for state) so we can reason about what the agent sends on the
  wire.

The CapitalArc treasury / yield leg (USYC on Arc, Circle Paymaster,
CCTP bridges) is **unchanged** - those still live on Arc and feed
USDC into Hyperliquid when the agent rotates risk-on.

Surface (mirrors the legacy :class:`ArcPerpExecutor`)
-----------------------------------------------------
::

    open_position(symbol, side, size_usd, leverage, slippage_bps, decision_id)
    close_position(symbol, slippage_bps, decision_id)
    close_all_positions(decision_id)
    get_position(symbol)         -> Position
    get_account_info()           -> AccountInfo
    get_pnl(symbol=None)         -> Decimal
    get_margin()                 -> Decimal
    set_leverage(symbol, leverage)
    update_take_profit_stop_loss(symbol, tp_price, sl_price, decision_id)
    get_mid_price(symbol)        -> Decimal | None

The same :class:`Position` / :class:`AccountInfo` dataclasses are
re-exported from ``arc_perp_executor`` so :class:`AllocationRouter`
can swap executors without any signature changes.

Safety
------
* ``dry_run=True`` is the default - no order is sent to the matcher.
  The plan is still logged and returned as a synthetic
  :class:`TxResult` with ``state="DRY_RUN"``.
* ``size_usd`` is **always** clamped to ``max_position_usd`` and
  ``leverage`` to ``max_leverage`` BEFORE the SDK call - even if a
  bug elsewhere asks for 100x on a million-dollar notional, the
  executor will trim it.
* Every state-changing call logs symbol / side / size / leverage /
  decision_id so a post-mortem reconstruction is possible from the
  loguru sink alone.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Any

from src.execution.arc_perp_executor import AccountInfo, Position
from src.execution.circle_wallet import TxResult
from src.utils.logging import logger

# The Hyperliquid Python SDK is a hard runtime dependency (see
# ``requirements.txt``); it pulls in eth-account + msgpack and handles
# the non-trivial L1 EIP-712 action signing against Hyperliquid's
# phantom-agent domain. We import it unconditionally - if the import
# fails the agent simply cannot start, which is the right behaviour:
# every code path in this module assumes the SDK is available.
from eth_account import Account
from hyperliquid.exchange import Exchange
from hyperliquid.info import Info
from hyperliquid.utils import constants as hl_constants  # noqa: F401


#: Default base URLs. The SDK also exposes these via
#: ``hyperliquid.utils.constants`` but we keep local aliases for the
#: dataclass default and so the value is greppable from one place.
TESTNET_API_URL = "https://api.hyperliquid-testnet.xyz"
MAINNET_API_URL = "https://api.hyperliquid.xyz"

#: ``MAX_DECIMALS`` from the Hyperliquid pricing rule. Perps cap at 6,
#: spot at 8. Used together with ``szDecimals`` (per-coin) in
#: :meth:`HyperliquidExecutor._round_hl_price` to compute the maximum
#: number of decimal places a price may have.
_HL_PERP_MAX_DECIMALS = 6

#: Conservative ``szDecimals`` defaults for the perps CapitalArc trades.
#: Used only when ``Info.meta()`` is unreachable (test fakes, network
#: hiccup). Wrong values for other coins are still strictly better than
#: "no rounding at all" — the latter is the bug we are fixing.
_HL_SZ_DECIMALS_FALLBACK: dict[str, int] = {
    "BTC": 5,
    "ETH": 4,
    "SOL": 2,
    "DOGE": 0,
}


# --------------------------------------------------------------------------
# Config + dataclasses
# --------------------------------------------------------------------------


@dataclass
class HyperliquidConfig:
    """Configuration for the Hyperliquid Testnet executor.

    Attributes
    ----------
    api_url :
        Hyperliquid REST API root used for **execution + account
        state**. Defaults to **testnet**. Switching to mainnet is a
        one-line ``.env`` change but should be done consciously -
        mainnet uses real USDC. Every signed order and every
        account-specific read (margin, positions, fills for OUR
        wallet) hits this URL.
    data_api_url :
        Hyperliquid REST API root used for **market-wide data reads**
        (asset universe / meta, perp OI / funding from the Info
        endpoints). Defaults to **mainnet** so analytics paths see
        production market intelligence even while ``api_url`` points
        at testnet for safe live-test runs. Kept deliberately
        separate from ``api_url`` so the operator can flip the
        execution venue without silently degrading the data plane.
        See also :attr:`info_data` for the resulting SDK client.
    private_key :
        EVM private key the agent signs orders with. For testnet the
        recommended setup is a fresh key fauceted on the Hyperliquid
        testnet bridge; it does NOT have to match the Circle DCW
        wallet on Arc. Hyperliquid stores positions per signer, so
        all of the agent's positions are bound to this single key.
        SECURITY: never commit a real key; keep it in a per-host
        ``.env`` outside source control.
    account_address :
        Address of the master account being traded. When ``None`` the
        executor falls back to the signer's own address. When set
        (and different from the signer) the SDK runs in "agent" mode:
        ``private_key`` is treated as an API-wallet sub-key allowed
        to act on behalf of ``account_address`` via the
        ``approveAgent`` action signed off-chain by the master.
    vault_address :
        Optional Hyperliquid vault address to trade on behalf of
        (e.g. a multi-strat vault). ``None`` for trading the signer's
        own account.
    max_leverage :
        Hard cap applied to every ``open_position`` call regardless
        of what the AllocationRouter asks for.
    max_position_usd :
        Hard cap on a single position's notional size.
    default_slippage_bps :
        Slippage used when the router doesn't pass one explicitly.
        50bps = 0.5% - generous enough for any liquid major.
    default_take_profit_pct :
        Default take-profit threshold as a fraction of entry price
        (0.02 = +2%). Applied by :class:`PositionManager` unless the
        cycle directive overrides it.
    default_stop_loss_pct :
        Default stop-loss threshold (-0.015 = -1.5%). Same override
        rules as TP.
    use_market_orders :
        When True, ``open_position`` / ``close_position`` use the
        SDK's ``market_open`` / ``market_close`` helpers (immediate
        execution at the best book price). When False, the executor
        submits a limit order at mid +/- slippage.
    """

    private_key: str | None = None
    account_address: str | None = None
    api_url: str = TESTNET_API_URL
    # See class docstring - mainnet by default so future Level-2
    # analytics see real market data even when execution is on testnet.
    data_api_url: str = MAINNET_API_URL
    vault_address: str | None = None
    max_leverage: int = 5
    max_position_usd: Decimal = Decimal("10000")
    default_slippage_bps: int = 50
    default_take_profit_pct: Decimal = Decimal("0.02")
    default_stop_loss_pct: Decimal = Decimal("0.015")
    use_market_orders: bool = True

    @property
    def is_configured(self) -> bool:
        """True when a private key is set - the only hard requirement.

        ``account_address`` defaults to the key's own EVM address, so
        leaving it unset is fine for the common single-wallet setup.
        """
        return bool(self.private_key)


# --------------------------------------------------------------------------
# Executor
# --------------------------------------------------------------------------


class HyperliquidExecutor:
    """High-level perp executor backed by the Hyperliquid Python SDK.

    Parameters
    ----------
    config :
        :class:`HyperliquidConfig` with API URL, private key and risk
        caps.
    dry_run :
        When True (default), no order is sent to Hyperliquid. The
        plan is logged and returned as a synthetic
        :class:`TxResult` with ``state="DRY_RUN"`` - exactly mirrors
        :class:`ArcPerpExecutor`'s contract so the AllocationRouter
        can't tell the executors apart in offline testing.
    info_client / exchange_client / info_data_client :
        Optional pre-built SDK clients. Only used by tests so we can
        inject mock SDK objects without touching the real network.
        ``info_data_client`` corresponds to the **market-data** Info
        client (mainnet by default); see :attr:`info_data`.
    """

    def __init__(
        self,
        config: HyperliquidConfig,
        dry_run: bool = True,
        *,
        info_client: Any | None = None,
        exchange_client: Any | None = None,
        info_data_client: Any | None = None,
    ) -> None:
        self.config = config
        self.dry_run = dry_run
        # ``_info`` is the EXECUTION Info client - pointed at
        # ``config.api_url`` (testnet by default). It serves all
        # account-specific reads (margin, positions, fills for OUR
        # wallet) and execution-side mid-price reads used for limit
        # order pricing. Must hit the same venue where our capital
        # actually lives, otherwise account state would be a phantom.
        self._info: Any | None = info_client
        # ``_info_data`` is the MARKET-DATA Info client - pointed at
        # ``config.data_api_url`` (mainnet by default). It is reserved
        # for callers that need market-wide intelligence (asset
        # universe, real perp OI / funding from Info endpoints) and
        # MUST stay on mainnet so a testnet execution venue never
        # degrades the data plane. Today the only built-in caller is
        # ``info_data`` (the public accessor); upcoming Level-2 work
        # will plug into it directly.
        self._info_data: Any | None = info_data_client
        self._exchange: Any | None = exchange_client
        self._account_address: str | None = config.account_address
        # Lazy cache of ``szDecimals`` per coin, populated from
        # ``Info.meta()`` on first use. Drives :meth:`round_price` /
        # :meth:`_round_hl_price` so reduce-only trigger orders never
        # leave the process with a price that violates Hyperliquid's
        # "5 sig figs AND (6 - szDecimals) decimal places" rule (the
        # silent FAILED-trigger bug behind the post-open propagation
        # race-fix work). Tests inject a ``_FakeInfo`` without
        # ``meta()`` and fall through to the conservative defaults in
        # :func:`_szdecimals_fallback`.
        self._sz_decimals_cache: dict[str, int] = {}
        # Build the SDK clients eagerly when a private key is configured.
        # This applies to BOTH dry-run and live: dry-run still benefits
        # from the live ``Info`` client for mid-price / state reads, and
        # building once at startup makes failures (bad key, unreachable
        # API) loud at the right moment rather than on the first order.
        # Tests bypass this by injecting the three optional client kwargs.
        if (
            info_client is None
            or exchange_client is None
            or info_data_client is None
        ) and config.is_configured:
            self._build_sdk_clients()

    # ------------------------------------------------------------------
    # SDK wiring
    # ------------------------------------------------------------------

    def _build_sdk_clients(self) -> None:
        """Construct the SDK ``Info`` + ``Exchange`` clients.

        Called once at ``__init__`` time when ``config.is_configured``
        is True (i.e. ``HYPERLIQUID_PRIVATE_KEY`` is set). Raises
        :class:`RuntimeError` if the private key can't be decoded into
        an EVM account - that's a hard configuration error and we want
        the agent to refuse to start, not limp on a misconfigured key.
        """
        try:
            wallet = Account.from_key(self.config.private_key)
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(
                f"HYPERLIQUID_PRIVATE_KEY is invalid: {exc}"
            ) from exc

        # When account_address is unset we trade the signer's own
        # account; this is the simplest setup. For agent-wallet /
        # vault setups, account_address points to the master account
        # while ``wallet`` signs on its behalf.
        master_address = self._account_address or wallet.address
        self._account_address = master_address

        if self._info is None:
            # EXECUTION Info: same venue as orders + account state.
            self._info = Info(self.config.api_url, skip_ws=True)
        if self._info_data is None:
            # MARKET-DATA Info: independent of where we execute. We
            # build it even when the two URLs match (the no-op case)
            # so downstream callers can rely on ``info_data`` always
            # being available, never None. Skipping the WS subscription
            # keeps the second client lightweight (REST-only reads).
            if self.config.data_api_url == self.config.api_url:
                # Optimisation: identical URL -> just alias to the
                # same client to save a TCP pool and a meta() warm-up.
                self._info_data = self._info
            else:
                self._info_data = Info(
                    self.config.data_api_url, skip_ws=True
                )
        if self._exchange is None:
            self._exchange = Exchange(
                wallet,
                self.config.api_url,
                account_address=master_address,
                vault_address=self.config.vault_address,
            )
        def _short(addr: str) -> str:
            return f"…{addr[-8:]}" if addr and len(addr) > 8 else addr or "-"
        logger.info(
            "HL SDK ready | signer={} account={} vault={}",
            _short(wallet.address),
            _short(master_address),
            _short(self.config.vault_address) if self.config.vault_address else "-",
        )

    # ------------------------------------------------------------------
    # Identity / configuration
    # ------------------------------------------------------------------

    @property
    def account_address(self) -> str | None:
        """EVM address of the account being traded.

        Note this is NOT necessarily the signer's address - in
        agent-wallet mode the signer is a sub-key allowed to act on
        behalf of the master account.
        """
        return self._account_address

    @property
    def info_data(self) -> Any | None:
        """Read-only accessor for the **market-data** Info client.

        Pointed at :attr:`HyperliquidConfig.data_api_url` (mainnet by
        default) and intentionally distinct from the executor's
        execution-side Info client. Use this from any caller that
        needs market-wide intelligence (asset universe, real perp OI
        / funding via Info endpoints) so that flipping the execution
        venue between testnet and mainnet never silently changes the
        data plane.

        Returns ``None`` only when the executor was constructed
        without a private key AND no ``info_data_client`` was
        injected (i.e. nothing to read from yet).
        """
        return self._info_data

    def set_account_address(self, address: str) -> None:
        """Late-bind the account address.

        Provided so the AllocationRouter can call the same hook on
        either executor (legacy :class:`ArcPerpExecutor` or
        :class:`HyperliquidExecutor`) without branching.
        """
        self._account_address = address

    # ------------------------------------------------------------------
    # Read-only methods (Info endpoint)
    # ------------------------------------------------------------------

    async def get_mid_price(self, symbol: str) -> Decimal | None:
        """Return the current mid-price for ``symbol`` (e.g. ``BTC``).

        Hyperliquid uses raw coin tickers (``BTC``, ``ETH``,
        ``SOL``) rather than the ``BTC-PERP`` convention used in our
        decision engine. :meth:`_to_hl_coin` does the mapping.
        """
        info = self._info
        if info is None:
            return None
        coin = self._to_hl_coin(symbol)
        try:
            mids = await asyncio.to_thread(info.all_mids)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Hyperliquid all_mids failed: {}", exc)
            return None
        raw = mids.get(coin)
        if raw is None:
            return None
        try:
            return Decimal(str(raw))
        except Exception:  # noqa: BLE001
            return None

    async def _user_state(self) -> dict[str, Any] | None:
        """Fetch the raw ``user_state`` payload (positions + margin).

        Centralised here so ``get_position`` / ``get_account_info`` /
        ``get_margin`` share the same network roundtrip.
        """
        info = self._info
        addr = self._account_address
        if info is None or not addr:
            return None
        try:
            return await asyncio.to_thread(info.user_state, addr)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Hyperliquid user_state failed: {}", exc)
            return None

    async def get_position(self, symbol: str) -> Position:
        """Return the agent's current position for ``symbol``.

        Returns a flat :class:`Position` when:
            * the account has no position in that coin,
            * the user_state call failed (logged as warning),
            * the executor is mid-construction without an account
              address.
        """
        coin = self._to_hl_coin(symbol)
        state = await self._user_state()
        if not state:
            return self._flat_position(symbol)
        positions = (state.get("assetPositions") or [])
        mark_price = await self.get_mid_price(symbol)
        for entry in positions:
            pos = entry.get("position") or {}
            if pos.get("coin") != coin:
                continue
            return self._position_from_state(symbol, pos, mark_price)
        return self._flat_position(symbol)

    async def get_open_positions(self) -> list[Position]:
        """Fresh pull of every currently-open position on Hyperliquid.

        This is the **canonical** "what positions does this account
        actually hold right now?" call - it performs its own
        :meth:`_user_state` roundtrip (NEVER reuses a cached
        :class:`AccountInfo`) and returns only positions that are
        non-flat with positive notional size.

        It exists primarily to defeat the position-state desync that
        was observed on Hyperliquid Testnet, where an
        :class:`AccountInfo` snapshot taken at the top of a cycle
        could be silently empty when:

        * ``_user_state`` failed transiently (network blip / SDK
          timeout) and ``get_account_info`` returned ``positions=[]``
          with no warning;
        * the snapshot was taken *just* after an order fill and the
          Hyperliquid Info endpoint hadn't yet propagated the open;
        * any other path between the open and the next cycle's
          snapshot was lossy.

        The PositionManager calls this at the start of every
        ``review_open_positions`` cycle and reconciles the result
        against the snapshot - so even if the snapshot is wrong, the
        stewardship layer is operating on real on-chain state.

        Returns an empty list on network failure (matching the
        defensive behaviour of :meth:`get_account_info`) so the
        caller can fall back to its snapshot without dying.
        """
        state = await self._user_state()
        if not state:
            return []
        positions: list[Position] = []
        mids = await self._all_mids()
        for entry in state.get("assetPositions") or []:
            pos = entry.get("position") or {}
            coin = pos.get("coin")
            if not coin:
                continue
            symbol = self._from_hl_coin(coin)
            mark = mids.get(coin) if mids else None
            rendered = self._position_from_state(symbol, pos, mark)
            # Filter out flats here so the caller can trust the list
            # length as "number of live positions".
            if rendered.side == "flat" or rendered.size_usd <= 0:
                continue
            positions.append(rendered)
        return positions

    async def get_account_info(self) -> AccountInfo:
        """Return a snapshot of the agent's Hyperliquid account.

        Cross-walks the SDK's ``user_state`` payload into the
        :class:`AccountInfo` shape the rest of CapitalArc expects.

        ``marginSummary`` covers both cross and isolated margin and is
        therefore preferred over ``crossMarginSummary`` (which only
        reflects the cross-margin sub-account and reads $0 when the
        account uses isolated-margin mode).  Fall back to
        ``crossMarginSummary`` if ``marginSummary`` is absent so the
        code stays compatible with any future API changes.
        """
        state = await self._user_state()
        if not state:
            return AccountInfo(
                equity_usd=Decimal("0"),
                free_margin_usd=Decimal("0"),
                used_margin_usd=Decimal("0"),
                total_unrealized_pnl_usd=Decimal("0"),
                positions=[],
            )
        # Prefer marginSummary (cross + isolated) over crossMarginSummary
        summary = state.get("marginSummary") or state.get("crossMarginSummary") or {}
        equity = self._dec(summary.get("accountValue"))
        used = self._dec(summary.get("totalMarginUsed"))
        free = max(equity - used, Decimal("0"))
        # Account-level unrealised PnL fallback (used only when the
        # ``assetPositions`` walk below produces no positions).
        # ``accountValue`` already reflects current MTM, while
        # ``totalRawUsd`` is the cash-basis equity (sum of deposits net
        # of fees/funding, before MTM). Their delta is the aggregate
        # unrealised PnL on open positions.  Using
        # ``totalNtlPos - totalRawUsd`` was wrong for accounts that
        # carry only USDC (no positions): ``totalNtlPos`` is the gross
        # notional of OPEN positions and is $0 with nothing on the
        # book, so the formula reported ``-deposit`` as PnL and the
        # CLI showed a 100% drawdown for a perfectly healthy wallet.
        upnl = equity - self._dec(summary.get("totalRawUsd"))
        # Walk every open position so the router has full visibility
        # of cross-asset exposure (BTC-PERP + ETH-PERP today, more
        # later).
        positions: list[Position] = []
        mids = await self._all_mids()
        for entry in state.get("assetPositions") or []:
            pos = entry.get("position") or {}
            coin = pos.get("coin")
            if not coin:
                continue
            symbol = self._from_hl_coin(coin)
            mark = mids.get(coin) if mids else None
            positions.append(
                self._position_from_state(symbol, pos, mark)
            )
        return AccountInfo(
            equity_usd=equity,
            free_margin_usd=free,
            used_margin_usd=used,
            total_unrealized_pnl_usd=sum(
                (p.unrealized_pnl_usd for p in positions), start=Decimal("0")
            )
            if positions
            else upnl,
            positions=positions,
        )

    async def get_pnl(self, symbol: str | None = None) -> Decimal:
        """Return unrealised PnL for ``symbol`` (or total when None)."""
        if symbol is not None:
            pos = await self.get_position(symbol)
            return pos.unrealized_pnl_usd
        info = await self.get_account_info()
        return info.total_unrealized_pnl_usd

    async def get_margin(self) -> Decimal:
        """Return USDC equity held on Hyperliquid.

        On Hyperliquid the "vault balance" is the account's total
        equity (cash + open-position MTM). The AllocationRouter uses
        this for sizing in the same way it used the Arc vault balance.
        """
        info = await self.get_account_info()
        return info.equity_usd

    async def withdraw_all_margin(
        self,
        decision_id: str | None = None,
    ) -> TxResult:
        """Withdraw the entire Hyperliquid balance back to Arbitrum.

        Hyperliquid runs a 1-block bridge to/from Arbitrum, so a
        withdrawal is a single signed action and the USDC arrives on
        Arbitrum a few seconds later. From there CCTP carries it
        back to Arc for USYC minting (handled by
        :class:`USYCExecutor` upstream).

        Returns a ``SKIPPED`` :class:`TxResult` in dry-run so the
        risk-off pipeline still completes end-to-end in offline
        scenarios.
        """
        balance = await self.get_margin()
        if balance <= 0:
            logger.info(
                "Hyperliquid.withdraw_all_margin | nothing to withdraw "
                "(balance=0 USD) decision={}",
                decision_id,
            )
            return self._noop_tx(
                "withdraw_all_skipped", decision_id, Decimal("0")
            )
        if self.dry_run:
            return self._noop_tx(
                "withdraw_all_skipped", decision_id, balance, force_dry=True
            )
        exchange = self._require_exchange()
        try:
            resp = await asyncio.to_thread(
                exchange.withdraw_from_bridge,
                float(balance),
                self._account_address,
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("Hyperliquid.withdraw_from_bridge failed: {}", exc)
            return TxResult(
                tx_id=f"hl-withdraw-error-{decision_id or 'na'}",
                state="FAILED",
                raw={"action": "withdraw_all_margin", "error": str(exc)},
            )
        logger.info(
            "Hyperliquid.withdraw_all_margin | amount={} USD decision={} resp={}",
            balance, decision_id, resp,
        )
        return TxResult(
            tx_id=str(resp.get("nonce") or f"hl-withdraw-{decision_id or 'na'}"),
            state="CONFIRMED" if resp.get("status") == "ok" else "PENDING",
            tx_hash=None,
            raw={
                "action": "withdraw_all_margin",
                "amount_usd": str(balance),
                "hl_response": resp,
            },
        )

    # ------------------------------------------------------------------
    # Trading - open / close
    # ------------------------------------------------------------------

    async def open_position(
        self,
        symbol: str,
        side: str,
        size_usd: Decimal,
        leverage: Decimal | None = None,
        slippage_bps: int | None = None,
        decision_id: str | None = None,
    ) -> TxResult:
        """Open (or grow) a perp position on Hyperliquid Testnet.

        Pipeline:
            1. Clamp leverage / size against the executor's hard caps.
            2. Update on-venue leverage via ``Exchange.update_leverage``
               (Hyperliquid stores per-coin leverage, separate from
               the order itself).
            3. Resolve the mid price -> convert USD notional to coin
               size (e.g. $5,000 / $63,000 BTC -> 0.0794 BTC).
            4. Submit either a market order (``market_open``) or a
               limit order (``order``) at mid +/- slippage.

        Dry-run logs the planned order and returns a synthetic
        :class:`TxResult` with ``state="DRY_RUN"`` so the
        AllocationRouter / panels can render the intended action
        without touching the network.
        """
        lev = self._cap_leverage(leverage)
        size_usd_clamped = min(size_usd, self.config.max_position_usd)
        slip = slippage_bps if slippage_bps is not None else self.config.default_slippage_bps
        side_long = self._side_is_long(side)
        coin = self._to_hl_coin(symbol)

        planned = {
            "action": "open_position",
            "venue": "hyperliquid",
            "symbol": symbol,
            "coin": coin,
            "side": side,
            "is_buy": side_long,
            "size_usd": str(size_usd_clamped),
            "leverage": str(lev),
            "slippage_bps": slip,
            "decision_id": decision_id,
            "use_market_order": self.config.use_market_orders,
        }
        logger.info(
            "HL.open | {} {} ${} lev={}x{}",
            side.upper(), symbol, size_usd_clamped, lev,
            " [dry]" if self.dry_run else "",
        )

        if self.dry_run:
            return TxResult(
                tx_id=f"dryrun-hl-open-{decision_id or 'na'}",
                state="DRY_RUN",
                raw={"planned_order": planned},
            )

        exchange = self._require_exchange()

        # Step 1: align on-venue leverage with what we want for this
        # trade. Hyperliquid keeps leverage per-(account, coin), so
        # we MUST set it before the order or we'd accidentally trade
        # at whatever was left from a previous cycle.
        try:
            await asyncio.to_thread(
                exchange.update_leverage, int(lev), coin, False  # is_cross=False -> isolated
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("update_leverage failed (continuing): {}", exc)

        # Step 2: USD notional -> coin size at current mid.
        mid = await self.get_mid_price(symbol)
        if mid is None or mid <= 0:
            logger.error(
                "Hyperliquid.open_position aborting - no mid price for {}",
                symbol,
            )
            return TxResult(
                tx_id=f"hl-no-mid-{decision_id or 'na'}",
                state="FAILED",
                raw={"error": "no_mid_price", "symbol": symbol},
            )
        size_coin = (size_usd_clamped / mid).quantize(Decimal("0.0001"))
        if size_coin <= 0:
            return TxResult(
                tx_id=f"hl-dust-{decision_id or 'na'}",
                state="SKIPPED",
                raw={"error": "size_below_minimum", "size_coin": str(size_coin)},
            )

        # Step 3: submit. The SDK's market_open returns a JSON ack
        # like ``{"status":"ok","response":{"type":"order","data":{"statuses":[...]}}}``.
        # Non-ok responses surface ``status:"err","response":<msg>``.
        try:
            if self.config.use_market_orders:
                resp = await asyncio.to_thread(
                    exchange.market_open,
                    coin,
                    side_long,
                    float(size_coin),
                    None,                    # px=None -> use mid +/- slippage
                    float(slip) / 10000.0,   # slippage as a fraction (0.005 = 0.5%)
                )
            else:
                limit_px = float(
                    mid * (Decimal("1") + Decimal(slip) / Decimal(10000))
                    if side_long
                    else mid * (Decimal("1") - Decimal(slip) / Decimal(10000))
                )
                resp = await asyncio.to_thread(
                    exchange.order,
                    coin,
                    side_long,
                    float(size_coin),
                    limit_px,
                    {"limit": {"tif": "Gtc"}},
                )
        except Exception as exc:  # noqa: BLE001
            logger.error("Hyperliquid.open_position SDK call failed: {}", exc)
            return TxResult(
                tx_id=f"hl-error-{decision_id or 'na'}",
                state="FAILED",
                raw={"action": "open_position", "error": str(exc), **planned},
            )

        return self._ack_to_tx_result(
            resp,
            action="open_position",
            planned=planned,
            size_coin=size_coin,
            mid=mid,
            decision_id=decision_id,
        )

    async def close_position(
        self,
        symbol: str,
        slippage_bps: int | None = None,
        decision_id: str | None = None,
    ) -> TxResult:
        """Close any open position for ``symbol``.

        Reads the current ``user_state`` to discover side / size,
        then submits a reduce-only market order on the opposite
        side. Dry-run logs the plan; SKIPPED is returned when the
        account is already flat in that coin.
        """
        slip = slippage_bps if slippage_bps is not None else self.config.default_slippage_bps
        coin = self._to_hl_coin(symbol)

        position = await self.get_position(symbol)
        planned = {
            "action": "close_position",
            "venue": "hyperliquid",
            "symbol": symbol,
            "coin": coin,
            "current_side": position.side,
            "current_size_usd": str(position.size_usd),
            "slippage_bps": slip,
            "decision_id": decision_id,
        }
        logger.info(
            "Hyperliquid.close_position | {} current={} size={} USD slip={}bps "
            "decision={} dry_run={}",
            symbol, position.side, position.size_usd, slip, decision_id,
            self.dry_run,
        )

        if position.side == "flat" or position.size_usd <= 0:
            return self._noop_tx("close_already_flat", decision_id, Decimal("0"))

        if self.dry_run:
            return TxResult(
                tx_id=f"dryrun-hl-close-{decision_id or 'na'}",
                state="DRY_RUN",
                raw={"planned_order": planned},
            )

        exchange = self._require_exchange()
        try:
            resp = await asyncio.to_thread(
                exchange.market_close,
                coin,
                None,                    # full close
                None,                    # px=None -> use mid +/- slippage
                float(slip) / 10000.0,
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("Hyperliquid.close_position SDK call failed: {}", exc)
            return TxResult(
                tx_id=f"hl-close-error-{decision_id or 'na'}",
                state="FAILED",
                raw={"action": "close_position", "error": str(exc), **planned},
            )

        return self._ack_to_tx_result(
            resp,
            action="close_position",
            planned=planned,
            size_coin=None,
            mid=None,
            decision_id=decision_id,
        )

    async def close_all_positions(
        self,
        decision_id: str | None = None,
    ) -> list[TxResult]:
        """Close every open position the agent currently holds.

        Returns one :class:`TxResult` per non-flat position. The
        AllocationRouter's risk-off path appends these to the
        ExecutionPlan so the console panels can render the full
        unwind sequence.
        """
        account = await self.get_account_info()
        open_positions = [p for p in account.positions if p.side != "flat"]
        results: list[TxResult] = []
        if not open_positions:
            logger.info(
                "Hyperliquid.close_all_positions | nothing to close "
                "(equity={} USD)", account.equity_usd,
            )
            return results
        logger.info(
            "Hyperliquid.close_all_positions | closing {} position(s) decision={}",
            len(open_positions), decision_id,
        )
        for pos in open_positions:
            res = await self.close_position(
                symbol=pos.symbol, decision_id=decision_id
            )
            results.append(res)
        return results

    # ------------------------------------------------------------------
    # Take-profit / stop-loss (Hyperliquid native trigger orders)
    # ------------------------------------------------------------------

    async def update_take_profit_stop_loss(
        self,
        symbol: str,
        tp_price: Decimal | None = None,
        sl_price: Decimal | None = None,
        decision_id: str | None = None,
        *,
        position_side: str | None = None,
        position_size_usd: Decimal | None = None,
    ) -> list[TxResult]:
        """Place reduce-only TP / SL trigger orders for ``symbol``.

        Hyperliquid supports native trigger orders (``tp`` and ``sl``
        types) - much cheaper than monitoring price in Python and
        submitting a close when threshold is hit. We submit one
        trigger per (TP, SL) the caller asks for. Either argument can
        be ``None`` to skip that side.

        Parameters
        ----------
        position_side
            ``"long"`` or ``"short"``. When provided, bypasses the
            ``get_position`` lookup. **This is critical immediately
            after** ``open_position`` because Hyperliquid Info
            endpoint may not have propagated the new fill yet -
            without an explicit hint, ``get_position`` returns
            ``"flat"`` and the TP/SL is silently skipped (the bug
            this kwarg was added to defeat). The AllocationRouter
            ALWAYS passes this hint right after a successful open.
        position_size_usd
            USD notional of the position to protect. Used to size the
            reduce-only trigger order in coin units. When omitted we
            fall back to ``get_position(symbol).size_usd``. Same
            propagation-race rationale as above: pass it explicitly
            after ``open_position`` to avoid a flat read.

        Returns one :class:`TxResult` per submitted trigger so the
        AllocationRouter / panels can render the full set.
        """
        coin = self._to_hl_coin(symbol)
        results: list[TxResult] = []

        # Resolve the (side, size_usd) pair we need to size + direction
        # the reduce-only trigger orders. Caller-provided values win
        # (post-open path), with ``get_position`` as the fallback for
        # callers that don't already know what they just opened.
        if position_side is not None and position_size_usd is not None:
            side = position_side.lower().strip()
            size_usd = position_size_usd
            if side not in {"long", "short"} or size_usd <= 0:
                logger.warning(
                    "Hyperliquid TP/SL aborted - bad caller hint "
                    "(side={!r}, size_usd={}) for {}",
                    position_side, position_size_usd, symbol,
                )
                return results
        else:
            position = await self.get_position(symbol)
            if position.side == "flat" or position.size_usd <= 0:
                # Loud at WARNING - this used to be an INFO and
                # silently swallowed the post-open propagation race.
                # If the caller didn't pass an explicit hint AND the
                # venue says "flat", we genuinely have nothing to
                # protect; surface it so the operator notices.
                logger.warning(
                    "Hyperliquid TP/SL skipped - {} is flat (no "
                    "caller hint and no exposure visible at "
                    "account={}). If you just called open_position, "
                    "pass position_side / position_size_usd to "
                    "bypass the Info-endpoint propagation lag.",
                    symbol, self._account_address,
                )
                return results
            side = position.side
            size_usd = position.size_usd

        mid = await self.get_mid_price(symbol)
        if mid is None or mid <= 0:
            logger.warning(
                "Hyperliquid TP/SL skipped - no mid price for {} "
                "(can't size coin units)",
                symbol,
            )
            return results

        # Same precision rounding as ``open_position`` so the trigger
        # size matches the position to one tick.
        size_coin = (size_usd / mid).quantize(Decimal("0.0001"))
        if size_coin <= 0:
            logger.warning(
                "Hyperliquid TP/SL skipped - coin size rounded to 0 "
                "(size_usd={}, mid={}) for {}",
                size_usd, mid, symbol,
            )
            return results

        # Reduce-only orders go on the OPPOSITE side of the position.
        close_is_buy = side == "short"
        # Tick-size rule applies once per symbol -> resolve up front so
        # we only hit ``Info.meta()`` once per cycle even when both TP
        # and SL are submitted.
        sz_decimals = await self._sz_decimals(symbol)
        for label, raw_trigger_px, trigger_type in (
            ("tp", tp_price, "tp"),
            ("sl", sl_price, "sl"),
        ):
            if raw_trigger_px is None:
                continue
            # Hyperliquid rejects orders whose price violates the
            # "5 sig figs AND (6 - szDecimals) decimal places" rule
            # with an exception out of the SDK (caught below and
            # rendered as a synthetic FAILED TxResult). Quantize here
            # so the price that hits the wire is always venue-valid
            # AND matches what we log / surface via TxResult.raw.
            trigger_px = self._round_hl_price(
                Decimal(str(raw_trigger_px)), sz_decimals
            )
            planned = {
                "action": f"trigger_{label}",
                "symbol": symbol,
                "coin": coin,
                "trigger_px": str(trigger_px),
                "trigger_px_raw": str(raw_trigger_px),
                "trigger_type": trigger_type,
                "reduce_only": True,
                "size_coin": str(size_coin),
                "decision_id": decision_id,
            }
            logger.info(
                "HL.{} | {} @ {} coin={}{}",
                label.upper(), symbol, trigger_px, size_coin,
                " [dry]" if self.dry_run else "",
            )
            if self.dry_run:
                results.append(TxResult(
                    tx_id=f"dryrun-hl-{label}-{decision_id or 'na'}",
                    state="DRY_RUN",
                    raw={"planned_order": planned},
                ))
                continue
            exchange = self._require_exchange()
            try:
                resp = await asyncio.to_thread(
                    exchange.order,
                    coin,
                    close_is_buy,
                    float(size_coin),
                    float(trigger_px),
                    {
                        "trigger": {
                            "isMarket": True,
                            "triggerPx": float(trigger_px),
                            "tpsl": trigger_type,
                        }
                    },
                    True,  # reduce_only
                )
            except Exception as exc:  # noqa: BLE001
                logger.error("Hyperliquid.{} SDK call failed: {}", label, exc)
                results.append(TxResult(
                    tx_id=f"hl-{label}-error-{decision_id or 'na'}",
                    state="FAILED",
                    raw={**planned, "error": str(exc)},
                ))
                continue
            results.append(
                self._ack_to_tx_result(
                    resp,
                    action=f"trigger_{label}",
                    planned=planned,
                    size_coin=size_coin,
                    mid=mid,
                    decision_id=decision_id,
                )
            )
        return results

    # ------------------------------------------------------------------
    # AllocationRouter compatibility shims
    # ------------------------------------------------------------------

    async def deposit_margin(
        self,
        amount_usd: Decimal,
        decision_id: str | None = None,
    ) -> TxResult:
        """No-op shim - Hyperliquid does not have an Arc-style vault.

        On Hyperliquid, capital arrives on-chain via the
        Arbitrum<->Hyperliquid bridge and immediately becomes
        tradable margin. We keep this method on the executor only so
        the AllocationRouter can call ``deposit_margin`` uniformly
        across venues; on Hyperliquid it returns a SKIPPED TxResult.
        """
        logger.info(
            "Hyperliquid.deposit_margin no-op | amount={} USD decision={} "
            "(margin lands automatically via bridge)",
            amount_usd, decision_id,
        )
        return self._noop_tx("deposit_noop", decision_id, amount_usd)

    async def withdraw_margin(
        self,
        amount_usd: Decimal,
        decision_id: str | None = None,
    ) -> TxResult:
        """Partial withdrawal back to Arbitrum.

        Used by the AllocationRouter when free margin needs to be
        freed up for the USYC leg. Dry-run returns SKIPPED.
        """
        if amount_usd <= 0:
            return self._noop_tx("withdraw_skipped", decision_id, amount_usd)
        if self.dry_run:
            return TxResult(
                tx_id=f"dryrun-hl-withdraw-{decision_id or 'na'}",
                state="DRY_RUN",
                raw={
                    "action": "withdraw_margin",
                    "amount_usd": str(amount_usd),
                },
            )
        exchange = self._require_exchange()
        try:
            resp = await asyncio.to_thread(
                exchange.withdraw_from_bridge,
                float(amount_usd),
                self._account_address,
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("Hyperliquid.withdraw_margin failed: {}", exc)
            return TxResult(
                tx_id=f"hl-withdraw-error-{decision_id or 'na'}",
                state="FAILED",
                raw={
                    "action": "withdraw_margin",
                    "amount_usd": str(amount_usd),
                    "error": str(exc),
                },
            )
        return TxResult(
            tx_id=str(resp.get("nonce") or f"hl-withdraw-{decision_id or 'na'}"),
            state="CONFIRMED" if resp.get("status") == "ok" else "PENDING",
            raw={
                "action": "withdraw_margin",
                "amount_usd": str(amount_usd),
                "hl_response": resp,
            },
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _require_exchange(self) -> Any:
        """Return the SDK ``Exchange`` client or raise.

        The Exchange client is built in :meth:`__init__` whenever
        ``HYPERLIQUID_PRIVATE_KEY`` is configured, so a missing client
        at this point means the key was never set. We've already gone
        past dry-run and the AllocationRouter's safety gates, so this
        is a hard configuration error: refuse to trade.
        """
        if self._exchange is None:
            raise RuntimeError(
                "HyperliquidExecutor live call without private key. "
                "Set HYPERLIQUID_PRIVATE_KEY in .env or run with --dry-run."
            )
        return self._exchange

    def _ack_to_tx_result(
        self,
        resp: dict[str, Any],
        *,
        action: str,
        planned: dict[str, Any],
        size_coin: Decimal | None,
        mid: Decimal | None,
        decision_id: str | None,
    ) -> TxResult:
        """Translate Hyperliquid's JSON ack into a :class:`TxResult`.

        Hyperliquid acks look like::

            {"status":"ok","response":{"type":"order","data":{
                "statuses":[{"filled":{"oid":..,"avgPx":"..","totalSz":".."}}]
            }}}

        or, on rejection::

            {"status":"err","response":"<reason>"}

        We map both into our internal state machine so the settlement
        loop in ``main.py`` treats Hyperliquid acks identically to
        Circle DCW responses.
        """
        status = resp.get("status") if isinstance(resp, dict) else None
        if status == "err":
            reason = resp.get("response")
            logger.warning("Hyperliquid rejected {}: {}", action, reason)
            return TxResult(
                tx_id=f"hl-rejected-{decision_id or 'na'}",
                state="DENIED",
                raw={
                    "action": action,
                    "rejection_reason": reason,
                    "hl_response": resp,
                    **planned,
                },
            )
        data = (resp.get("response") or {}).get("data") or {}
        statuses = data.get("statuses") or []
        first = statuses[0] if statuses else {}
        filled = first.get("filled") or {}
        resting = first.get("resting") or {}
        order_id = (
            filled.get("oid")
            or resting.get("oid")
            or f"hl-{action}-{decision_id or 'na'}"
        )
        if filled:
            return TxResult(
                tx_id=str(order_id),
                state="CONFIRMED",
                raw={
                    "action": action,
                    "filled": filled,
                    "avg_price": filled.get("avgPx"),
                    "size_filled": filled.get("totalSz"),
                    "size_coin": str(size_coin) if size_coin else None,
                    "mid_price": str(mid) if mid else None,
                    "hl_response": resp,
                    **planned,
                },
            )
        if resting:
            return TxResult(
                tx_id=str(order_id),
                state="PENDING",
                raw={
                    "action": action,
                    "resting": resting,
                    "size_coin": str(size_coin) if size_coin else None,
                    "mid_price": str(mid) if mid else None,
                    "hl_response": resp,
                    **planned,
                },
            )
        # Unrecognised ack shape - log loudly and surface as PENDING
        # so the caller doesn't accidentally treat it as failure.
        logger.warning(
            "Hyperliquid unexpected ack shape for {}: {}", action, resp,
        )
        return TxResult(
            tx_id=str(order_id),
            state="PENDING",
            raw={
                "action": action,
                "hl_response": resp,
                "warning": "unrecognised_ack_shape",
                **planned,
            },
        )

    async def _all_mids(self) -> dict[str, Decimal] | None:
        info = self._info
        if info is None:
            return None
        try:
            raw = await asyncio.to_thread(info.all_mids)
        except Exception as exc:  # noqa: BLE001
            logger.debug("all_mids failed: {}", exc)
            return None
        out: dict[str, Decimal] = {}
        for coin, px in raw.items():
            try:
                out[coin] = Decimal(str(px))
            except Exception:  # noqa: BLE001
                continue
        return out

    def _position_from_state(
        self,
        symbol: str,
        pos: dict[str, Any],
        mark_price: Decimal | None,
    ) -> Position:
        """Render a Hyperliquid position dict into our :class:`Position`."""
        size = self._dec(pos.get("szi"))
        if size == 0:
            return self._flat_position(symbol)
        side = "long" if size > 0 else "short"
        entry = self._dec(pos.get("entryPx"))
        notional = abs(size) * (mark_price or entry)
        leverage_field = pos.get("leverage") or {}
        leverage = self._dec(leverage_field.get("value"))
        upnl = self._dec(pos.get("unrealizedPnl"))
        return Position(
            symbol=symbol,
            side=side,
            size_usd=notional,
            entry_price=entry,
            mark_price=mark_price or entry,
            leverage=leverage if leverage > 0 else Decimal("1"),
            unrealized_pnl_usd=upnl,
            raw=pos,
        )

    def _flat_position(self, symbol: str) -> Position:
        return Position(
            symbol=symbol,
            side="flat",
            size_usd=Decimal("0"),
            entry_price=Decimal("0"),
            mark_price=Decimal("0"),
            leverage=Decimal("0"),
            unrealized_pnl_usd=Decimal("0"),
        )

    def _cap_leverage(self, leverage: Decimal | None) -> Decimal:
        lev = leverage if leverage is not None else Decimal(self.config.max_leverage)
        # Floor to 1 (no margin = no trade) and cap at the venue max.
        return max(Decimal("1"), min(lev, Decimal(self.config.max_leverage)))

    @staticmethod
    def _side_is_long(side: str) -> bool:
        s = side.lower()
        if s == "long":
            return True
        if s == "short":
            return False
        raise ValueError(f"Unknown perp side: {side!r}")

    @staticmethod
    def _to_hl_coin(symbol: str) -> str:
        """Map ``BTC-PERP`` -> ``BTC``, ``ETH-PERP`` -> ``ETH``, etc.

        Hyperliquid trades against bare coin tickers. CapitalArc's
        decision engine speaks the ``COIN-PERP`` convention so the
        rest of the agent (Dune queries, console panels) reads as
        "BTC perpetual" rather than "BTC".
        """
        return symbol.split("-")[0].upper()

    # ------------------------------------------------------------------
    # Tick-size rounding (Hyperliquid pricing rules)
    # ------------------------------------------------------------------

    @staticmethod
    def _round_hl_price(
        px: Decimal,
        sz_decimals: int,
        *,
        max_decimals: int = _HL_PERP_MAX_DECIMALS,
    ) -> Decimal:
        """Round ``px`` to Hyperliquid's tick rules.

        Hyperliquid perp pricing rule (from the API docs):

        * At most **5 significant figures**, AND
        * At most ``max_decimals - sz_decimals`` **decimal places**.
        * Integer prices are always allowed regardless of sig-figs.

        Implementation: pick the *coarser* (larger exponent) of the
        two quantization steps and round half-even. Integer-only
        outputs fall out automatically when the sig-fig rule is
        stricter than the decimal-place rule.

        Examples (``sz_decimals=5`` -> max 1 decimal):
            ``77629.420523...`` -> ``77629``
            ``112345.67``       -> ``112350``

        Example (``sz_decimals=4`` -> max 2 decimals):
            ``2700.55``         -> ``2700.6`` (5 sig figs caps at .1)
        """
        if px <= 0:
            return px
        dec_quantum_exp = -max(0, max_decimals - sz_decimals)
        # ``adjusted()`` is the exponent of the most-significant digit
        # (e.g. ``Decimal("77629.42").adjusted() == 4``). Five sig
        # figs -> we want a quantum 4 orders of magnitude smaller.
        sig_quantum_exp = px.adjusted() - 4
        quantum_exp = max(sig_quantum_exp, dec_quantum_exp)
        # ``Decimal("1E<n>")`` carries the exponent ``n`` so
        # ``quantize`` rounds to multiples of ``10**n``. We can't use
        # ``Decimal(10) ** quantum_exp`` because for ``quantum_exp >=
        # 1`` it returns ``Decimal("10")`` / ``Decimal("100")`` with
        # exponent 0, which would silently quantize to multiples of 1.
        template = Decimal(f"1E{quantum_exp}")
        return px.quantize(template, rounding=ROUND_HALF_EVEN)

    async def _sz_decimals(self, symbol: str) -> int:
        """Look up ``szDecimals`` for ``symbol``, caching the answer.

        Reads :attr:`_info_data` (mainnet by default) when available
        — ``szDecimals`` is part of the asset spec and identical on
        mainnet / testnet for the same coin — and falls back to
        :attr:`_info` (testnet, execution plane). When both are
        unreachable, falls back to :data:`_HL_SZ_DECIMALS_FALLBACK`
        so the rounding rule still applies for the common majors.

        Cached forever per executor instance: ``szDecimals`` does not
        change for a listed perp.
        """
        coin = self._to_hl_coin(symbol)
        cached = self._sz_decimals_cache.get(coin)
        if cached is not None:
            return cached
        info = self._info_data or self._info
        meta_fn = getattr(info, "meta", None) if info is not None else None
        if meta_fn is None:
            sd = _HL_SZ_DECIMALS_FALLBACK.get(
                coin, _HL_SZ_DECIMALS_FALLBACK.get("ETH", 4)
            )
            self._sz_decimals_cache[coin] = sd
            return sd
        try:
            meta = await asyncio.to_thread(meta_fn)
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "Hyperliquid Info.meta() failed; using szDecimals "
                "fallback for {}: {}", coin, exc,
            )
            sd = _HL_SZ_DECIMALS_FALLBACK.get(
                coin, _HL_SZ_DECIMALS_FALLBACK.get("ETH", 4)
            )
            self._sz_decimals_cache[coin] = sd
            return sd
        # Warm the cache with the FULL universe on first hit — the SDK
        # already paid the RTT, so caching just BTC and re-fetching
        # for ETH would be wasteful. Subsequent ``_sz_decimals`` calls
        # for any other coin will be O(1) dict reads.
        universe = (meta or {}).get("universe") or []
        for asset in universe:
            try:
                name = (asset.get("name") or "").upper()
            except AttributeError:
                continue
            if not name:
                continue
            try:
                self._sz_decimals_cache[name] = int(asset.get("szDecimals"))
            except (TypeError, ValueError):
                continue
        if coin in self._sz_decimals_cache:
            return self._sz_decimals_cache[coin]
        sd = _HL_SZ_DECIMALS_FALLBACK.get(coin, 4)
        self._sz_decimals_cache[coin] = sd
        return sd

    async def round_price(self, symbol: str, px: Decimal) -> Decimal:
        """Round ``px`` to Hyperliquid's tick rules for ``symbol``.

        Convenience wrapper around :meth:`_sz_decimals` +
        :meth:`_round_hl_price` so callers outside this module (notably
        :class:`AllocationRouter`) can mirror the executor's rounding
        without duplicating the meta lookup. Idempotent: rounding an
        already-rounded value returns the same value.
        """
        sd = await self._sz_decimals(symbol)
        return self._round_hl_price(px, sd)

    @staticmethod
    def _from_hl_coin(coin: str) -> str:
        """Inverse of :meth:`_to_hl_coin`. ``BTC`` -> ``BTC-PERP``."""
        return f"{coin.upper()}-PERP"

    @staticmethod
    def _dec(value: Any) -> Decimal:
        """Decimal-safe parser used for SDK numeric fields.

        Hyperliquid returns numeric values as strings (decimal-precise)
        or floats. Both are converted via ``Decimal(str(x))``.
        """
        if value is None or value == "":
            return Decimal("0")
        try:
            return Decimal(str(value))
        except Exception:  # noqa: BLE001
            return Decimal("0")

    def _noop_tx(
        self,
        action: str,
        decision_id: str | None,
        amount: Decimal,
        *,
        force_dry: bool = False,
    ) -> TxResult:
        """Synthetic ``TxResult`` for skipped / zero-amount operations.

        Mirrors the helper on :class:`ArcPerpExecutor` so the
        AllocationRouter / console panels treat a noop identically
        regardless of which executor produced it.
        """
        state = "DRY_RUN" if (self.dry_run or force_dry) else "SKIPPED"
        return TxResult(
            tx_id=f"noop-hl-{action}-{decision_id or 'na'}",
            state=state,
            raw={"action": action, "amount_usd": str(amount), "venue": "hyperliquid"},
        )
