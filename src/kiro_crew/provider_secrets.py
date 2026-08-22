"""Provider API-key storage with OS-keyring support (optional dependency).

Precedence everywhere a router API key is read:

1. ``KIROCREW_PROVIDER_API_KEY`` environment variable (CI / service deploys),
2. the OS keyring (service ``kirocrew-customapi``, entry ``provider_api_key``)
   when the :mod:`keyring` package is installed,
3. plaintext ``agent.provider_api_key`` in config.json (legacy, still honoured
   so nothing breaks — but ``kirocrew doctor`` flags it).

Nothing here is load-bearing for boot: every failure mode degrades to "the
plaintext path still works" with a log line, never an exception. The desktop
app must not grow a hard keyring dependency just because one is available.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

SERVICE_NAME = "kirocrew-customapi"
ENTRY_NAME = "provider_api_key"
ENV_VAR = "KIROCREW_PROVIDER_API_KEY"


def keyring_available() -> bool:
    """True when the optional :mod:`keyring` package imports AND has a usable
    backend. A headless Linux box without gnome-keyring/KWallet/D-Bus yields a
    fail backend that raises on every call — treated as unavailable."""
    try:
        import keyring  # noqa: PLC0415 - optional dependency

        backend = keyring.get_keyring()
        name = type(backend).__module__ + "." + type(backend).__name__
        if "fail" in name.lower():
            return False
        return True
    except Exception:  # noqa: BLE001 - any import/backend failure means "no"
        return False


def store_provider_key(key: str) -> bool:
    """Persist *key* into the OS keyring. Returns False when unsupported."""
    if not key:
        return False
    try:
        import keyring  # noqa: PLC0415

        keyring.set_password(SERVICE_NAME, ENTRY_NAME, key)
        return True
    except Exception as exc:  # noqa: BLE001 - degrade, never raise
        logger.warning("keyring store failed (%s); key stays wherever it was", exc)
        return False


def load_provider_key() -> str:
    """Read the key from the keyring, or '' when absent/unsupported."""
    try:
        import keyring  # noqa: PLC0415

        return (keyring.get_password(SERVICE_NAME, ENTRY_NAME) or "").strip()
    except Exception:  # noqa: BLE001
        return ""


def clear_provider_key() -> bool:
    """Best-effort removal from the keyring."""
    try:
        import keyring  # noqa: PLC0415

        keyring.delete_password(SERVICE_NAME, ENTRY_NAME)
        return True
    except Exception:  # noqa: BLE001
        return False


def effective_provider_api_key(configured: str | None) -> str:
    """Resolve the API key by precedence: env > keyring > plaintext config.

    Every reader of ``agent.provider_api_key`` goes through here so the
    precedence rule exists in exactly ONE place.
    """
    env_val = (os.environ.get(ENV_VAR) or "").strip()
    if env_val:
        return env_val
    ring_val = load_provider_key()
    if ring_val:
        return ring_val
    return (configured or "").strip()


def describe_key_source(configured: str | None) -> str:
    """Where the effective key comes from — for ``doctor`` output only."""
    if (os.environ.get(ENV_VAR) or "").strip():
        return f"environment ({ENV_VAR})"
    if load_provider_key():
        return "OS keyring"
    if (configured or "").strip():
        return "config.json plaintext (consider migrating: kirocrew doctor)"
    return "not set"
