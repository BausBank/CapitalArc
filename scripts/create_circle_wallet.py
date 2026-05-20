"""Create a Circle Developer-Controlled Wallet on Arc Testnet.

This is a one-shot setup script for CapitalArc. It will:

    1. Load `CIRCLE_API_KEY` and `CIRCLE_ENTITY_SECRET` from `.env`
       (the same loader used by `src/execution/circle_wallet.py`).
    2. Reuse `CIRCLE_WALLET_SET_ID` if it's already in `.env`, or create
       a brand-new wallet set when `--new-set` is passed (or none is set).
    3. Create a single EOA wallet on `ARC-TESTNET` inside that set.
    4. Print the Wallet Set ID, Wallet ID and on-chain Address, plus a
       ready-to-paste block for `.env` so the runtime agent picks them up.

Usage
-----
    python scripts/create_circle_wallet.py
    python scripts/create_circle_wallet.py --new-set
    python scripts/create_circle_wallet.py --set-name "CapitalArc Mainnet" \
        --blockchain ARC-TESTNET --account-type EOA

Notes
-----
- The Circle Python SDK (`circle-developer-controlled-wallets`) handles the
  RSA encryption of the entity secret for every API call. You only need to
  generate + register the 32-byte hex entity secret once via Circle Console.
  See: https://developers.circle.com/wallets/dev-controlled/register-entity-secret
- The script defaults to `ARC-TESTNET` and `EOA` per Circle's documented
  quickstart. EOA is the right choice for an agent that holds real
  positions on Arc; SCA would only matter once we need gasless ops with
  Circle Paymaster.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from dotenv import load_dotenv  # noqa: E402

from src.utils.config import Settings, get_settings  # noqa: E402


# --------------------------------------------------------------------------
# Pretty-print helpers (no external deps - this is a one-shot script).
# --------------------------------------------------------------------------

_BAR = "=" * 72
_SUB = "-" * 72


def _section(title: str) -> None:
    print()
    print(_BAR)
    print(f"  {title}")
    print(_BAR)


def _row(label: str, value: object) -> None:
    print(f"  {label:<22} {value}")


def _ok(msg: str) -> None:
    print(f"  [OK]   {msg}")


def _warn(msg: str) -> None:
    print(f"  [WARN] {msg}")


def _fail(msg: str) -> None:
    print(f"  [FAIL] {msg}", file=sys.stderr)


def _abort(msg: str, hint: str | None = None) -> "None":
    _section("ABORTED")
    _fail(msg)
    if hint:
        print()
        print("  How to fix:")
        for line in hint.strip().splitlines():
            print(f"    {line}")
    sys.exit(1)


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------


def _validate_settings(settings: Settings) -> tuple[str, str]:
    """Return `(api_key, entity_secret)` after sanity checks."""
    api_key = settings.CIRCLE_API_KEY or ""
    entity_secret = settings.CIRCLE_ENTITY_SECRET or ""

    if not api_key:
        _abort(
            "CIRCLE_API_KEY is missing from .env",
            "Get one from https://console.circle.com/ and paste it into your\n"
            ".env as CIRCLE_API_KEY=TEST_API_KEY:... (or LIVE_API_KEY for prod).",
        )

    if not entity_secret:
        _abort(
            "CIRCLE_ENTITY_SECRET is missing from .env",
            "Generate a 32-byte hex secret and register it with Circle:\n"
            "  https://developers.circle.com/wallets/dev-controlled/register-entity-secret\n"
            "Then put the hex value into .env as CIRCLE_ENTITY_SECRET=...",
        )

    if len(entity_secret) != 64 or not all(c in "0123456789abcdefABCDEF" for c in entity_secret):
        _abort(
            f"CIRCLE_ENTITY_SECRET looks wrong ({len(entity_secret)} chars).",
            "The entity secret must be a 32-byte value encoded as 64 hex characters.",
        )

    if api_key.startswith("LIVE_API_KEY") and "TESTNET" in (
        # using upper-case helper since blockchain identifier is upper-case
        "ARC-TESTNET"
    ):
        _warn(
            "You are using a LIVE API key against a TESTNET blockchain. "
            "Double-check this is intentional."
        )

    return api_key, entity_secret


# --------------------------------------------------------------------------
# Circle SDK access
# --------------------------------------------------------------------------


def _build_client(api_key: str, entity_secret: str):
    """Instantiate the Circle Developer-Controlled Wallets SDK client."""
    try:
        from circle.web3 import utils
    except ImportError as exc:
        _abort(
            f"Circle SDK is not installed: {exc}",
            "Run:  pip install circle-developer-controlled-wallets\n"
            "Or:   pip install -r requirements.txt",
        )
        raise  # for type-checkers; _abort() already exits

    return utils.init_developer_controlled_wallets_client(
        api_key=api_key,
        entity_secret=entity_secret,
    )


def _create_wallet_set(client, name: str) -> tuple[str, dict]:
    """Create a new wallet set and return `(id, raw_dict)`."""
    from circle.web3 import developer_controlled_wallets as dcw

    api = dcw.WalletSetsApi(client)
    resp = api.create_wallet_set(
        dcw.CreateWalletSetRequest.from_dict({"name": name})
    )
    raw = resp.to_dict()
    wallet_set = resp.data.wallet_set.actual_instance
    return wallet_set.id, raw


def _unwrap_wallet(wallet_entry) -> tuple[str, str]:
    """Pull `(id, address)` out of a `WalletsDataWalletsInner` entry.

    The Circle SDK models the wallets list as a oneOf union, so each item
    is a wrapper whose real fields live on `.actual_instance`. We try
    `actual_instance` first, then fall back to direct attributes (older
    SDK builds), then to `.to_dict()` (last resort).
    """
    inner = getattr(wallet_entry, "actual_instance", None) or wallet_entry

    wallet_id = getattr(inner, "id", None)
    address = getattr(inner, "address", None)

    if not wallet_id or not address:
        try:
            data = wallet_entry.to_dict()
        except Exception:  # noqa: BLE001 - last-resort introspection
            data = {}
        wallet_id = wallet_id or data.get("id")
        address = address or data.get("address")

    if not wallet_id or not address:
        raise RuntimeError(
            "Could not extract wallet id/address from Circle response. "
            f"Got entry: {wallet_entry!r}"
        )
    return wallet_id, address


def _create_wallet(
    client,
    wallet_set_id: str,
    blockchain: str,
    account_type: str,
    wallet_name: str,
) -> tuple[str, str, dict]:
    """Create a single wallet inside the set. Return `(id, address, raw)`."""
    from circle.web3 import developer_controlled_wallets as dcw

    api = dcw.WalletsApi(client)
    resp = api.create_wallet(
        dcw.CreateWalletRequest.from_dict(
            {
                "walletSetId": wallet_set_id,
                "blockchains": [blockchain],
                "count": 1,
                "accountType": account_type,
                "metadata": [{"name": wallet_name}],
            }
        )
    )
    raw = resp.to_dict()
    wallets = resp.data.wallets or []
    if not wallets:
        raise RuntimeError(f"Circle returned no wallets in response: {raw!r}")

    wallet_id, address = _unwrap_wallet(wallets[0])
    return wallet_id, address, raw


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------


def main() -> None:
    load_dotenv()
    settings = get_settings()

    parser = argparse.ArgumentParser(
        description="Create a Circle Developer-Controlled Wallet on Arc Testnet."
    )
    parser.add_argument(
        "--new-set",
        action="store_true",
        help="Always create a new wallet set, even if CIRCLE_WALLET_SET_ID is in .env.",
    )
    parser.add_argument(
        "--set-name",
        default="CapitalArc Agent Wallets",
        help="Name to assign when creating a new wallet set.",
    )
    parser.add_argument(
        "--blockchain",
        default="ARC-TESTNET",
        help="Circle blockchain identifier (default: ARC-TESTNET).",
    )
    parser.add_argument(
        "--account-type",
        choices=["EOA", "SCA"],
        default="EOA",
        help="Wallet account type. EOA is the right default for the agent.",
    )
    parser.add_argument(
        "--wallet-name",
        default="CapitalArc Main Agent",
        help="Human-readable name shown in Circle Console for the new wallet.",
    )
    args = parser.parse_args()

    _section("CapitalArc - Create Circle Developer-Controlled Wallet")
    _row("Blockchain:", args.blockchain)
    _row("Account type:", args.account_type)
    _row("Env file:", ".env (loaded via python-dotenv)")

    api_key, entity_secret = _validate_settings(settings)
    masked_key = f"{api_key[:18]}...{api_key[-6:]}" if len(api_key) > 24 else "***"
    _row("API key:", masked_key)
    _row("Entity secret:", f"{entity_secret[:6]}...{entity_secret[-4:]} (64 hex)")

    client = _build_client(api_key, entity_secret)

    # ----- Wallet set -----
    existing_set = (settings.CIRCLE_WALLET_SET_ID or "").strip()
    if existing_set and not args.new_set:
        wallet_set_id = existing_set
        _section("Wallet set")
        _ok(f"Reusing existing wallet set from .env: {wallet_set_id}")
        _warn(
            "Pass --new-set if you want to create a fresh set instead "
            "of reusing this one."
        )
    else:
        _section("Wallet set")
        print(f"  Creating wallet set: {args.set_name!r} ...")
        try:
            wallet_set_id, _ = _create_wallet_set(client, args.set_name)
        except Exception as exc:  # noqa: BLE001 - surface SDK error verbatim
            _abort(f"Failed to create wallet set: {exc}")
            return
        _ok(f"Created wallet set: {wallet_set_id}")

    # ----- Wallet -----
    _section("Wallet")
    print(
        f"  Creating 1 x {args.account_type} wallet "
        f"named {args.wallet_name!r} on {args.blockchain} "
        f"in set {wallet_set_id} ..."
    )
    try:
        wallet_id, wallet_address, _ = _create_wallet(
            client,
            wallet_set_id=wallet_set_id,
            blockchain=args.blockchain,
            account_type=args.account_type,
            wallet_name=args.wallet_name,
        )
    except Exception as exc:  # noqa: BLE001
        _abort(f"Failed to create wallet: {exc}")
        return

    _ok(f"Wallet created: {wallet_id}")
    _ok(f"Wallet name:    {args.wallet_name}")
    _ok(f"Address:        {wallet_address}")

    # ----- Summary -----
    _section("Summary")
    _row("Wallet Set ID:", wallet_set_id)
    _row("Wallet ID:", wallet_id)
    _row("Wallet Name:", args.wallet_name)
    _row("Wallet Address:", wallet_address)
    _row("Blockchain:", args.blockchain)
    _row("Account Type:", args.account_type)

    _section(".env values - copy these into your local .env")
    print(f"  CIRCLE_WALLET_SET_ID={wallet_set_id}")
    print(f"  CIRCLE_AGENT_WALLET_ID={wallet_id}")
    print(_SUB)
    print(f"  # Agent on-chain address: {wallet_address}")

    _section("Next steps")
    print("  1. Paste the two lines above into .env (keep your real keys there).")
    print("  2. Fund the wallet with testnet ETH (gas) and testnet USDC:")
    print(f"       https://faucet.circle.com/?address={wallet_address}")
    print("  3. Run a dry-run smoke test of the full pipeline:")
    print("       python main.py")
    print()


if __name__ == "__main__":
    main()
