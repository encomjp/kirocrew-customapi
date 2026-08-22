"""``kirocrew secret`` — manage the provider API key outside plaintext config.

Sub-actions:

* ``migrate`` — move an existing plaintext ``agent.provider_api_key`` into the
  OS keyring and blank it in config.json (the single high-value action).
* ``set <key>`` / ``clear`` — direct keyring writes.
* ``status`` — where the effective key comes from + keyring health.

All keyring failures degrade to clear messages, never tracebacks: the keyring
is optional by design (see :mod:`kiro_crew.provider_secrets`).
"""

from __future__ import annotations

import argparse
import sys

from kiro_crew.config.loader import KiroCrewConfig, read_config_for_update, update_config_locked
from kiro_crew.provider_secrets import (
    clear_provider_key,
    describe_key_source,
    effective_provider_api_key,
    keyring_available,
    store_provider_key,
)


def _migrate(_args: argparse.Namespace) -> int:
    if not keyring_available():
        print("❌ No usable OS keyring backend (install keyring + a backend "
              "such as gnome-keyring / KWallet / SecuresSystems).")
        print("   Alternative: export KIROCREW_PROVIDER_API_KEY instead.")
        return 1

    cfg_path = KiroCrewConfig.load()
    plain = (cfg_path.agent.provider_api_key or "").strip()
    if not plain:
        print("Nothing to migrate: agent.provider_api_key is empty in config.json.")
        print(f"Effective key source: {describe_key_source('')}")
        return 0

    if not store_provider_key(plain):
        print("❌ Keyring write failed — config left unchanged.")
        return 1

    def _strip(data: dict) -> None:
        agent = data.get("agent")
        if isinstance(agent, dict):
            agent["provider_api_key"] = ""

    update_config_locked(_strip)
    print("✅ Migrated provider API key → OS keyring; plaintext value removed "
          "from config.json.")
    return 0


def _set(args: argparse.Namespace) -> int:
    if not keyring_available():
        print("❌ No usable OS keyring backend.")
        return 1
    ok = store_provider_key(args.key)
    print("✅ Stored." if ok else "❌ Store failed.")
    return 0 if ok else 1


def _clear(_args: argparse.Namespace) -> int:
    ok = clear_provider_key()
    print("✅ Cleared." if ok else "Nothing to clear (or no keyring).")
    return 0


def _status(_args: argparse.Namespace) -> int:
    cfg = KiroCrewConfig.load()
    configured = (cfg.agent.provider_api_key or "").strip()
    print(f"keyring backend available : {keyring_available()}")
    print(f"effective key source      : {describe_key_source(configured)}")
    eff = effective_provider_api_key(configured)
    print(f"effective key set         : {bool(eff)}")
    if configured and describe_key_source(configured).startswith("config.json"):
        print("→ run `kirocrew secret migrate` to move it into the keyring")
    return 0


def secret_cmd(args: argparse.Namespace) -> int:
    action = args.secret_action
    if action == "migrate":
        return _migrate(args)
    if action == "set":
        return _set(args)
    if action == "clear":
        return _clear(args)
    if action == "status":
        return _status(args)
    print("usage: kirocrew secret {migrate,set,clear,status} [key]")
    return 2


def register_secret_parser(sub) -> None:  # noqa: ANN001 - argparse subparsers
    p = sub.add_parser(
        "secret",
        help="Manage the router API key (OS keyring)",
        epilog="""
Examples:
  kirocrew secret status     # Where does the effective key come from?
  kirocrew secret migrate    # Move plaintext config key -> OS keyring
  kirocrew secret set <KEY>  # Write keyring directly
  kirocrew secret clear      # Remove from keyring

Precedence everywhere: KIROCREW_PROVIDER_API_KEY env > OS keyring >
plaintext agent.provider_api_key.
""",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    secret_sub = p.add_subparsers(dest="secret_action")
    secret_sub.add_parser("migrate", help="Move plaintext config key into the OS keyring")
    set_p = secret_sub.add_parser("set", help="Store key in the OS keyring")
    set_p.add_argument("key", help="The API key (read once, never logged)")
    secret_sub.add_parser("clear", help="Remove the key from the OS keyring")
    secret_sub.add_parser("status", help="Show effective key source + keyring health")
