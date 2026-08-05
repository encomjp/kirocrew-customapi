"""Tests for the model picker's entitlement narrowing (/api/models, kiro path).

``kiro chat --list-models`` is a CATALOG, not an entitlement: it returns the same
rows whatever the account's plan. So after a downgrade the picker kept offering —
and the composer kept displaying as selected — a premium model no turn could run,
while the session itself quietly ran on the backend default.

The tier-aware signal is the live session's ``session/new`` ``availableModels``
list, the same one ``model_is_unusable`` pre-flights against. These tests pin that
it narrows the catalog when known, and that every unknowable case FAILS OPEN
(returns the full catalog) rather than emptying the picker.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

from kiro_crew.dashboard.handlers import agents
from kiro_crew.kiro_prerequisite import KiroPrerequisiteService

CATALOG = [
    {"model_name": "auto", "description": "Models chosen by task"},
    {"model_name": "claude-opus-5", "description": "Opus 5"},
    {"model_name": "claude-sonnet-5", "description": "Sonnet 5"},
    {"model_name": "claude-opus-4.8", "description": "Opus 4.8"},
]


def _provider(models: object, *, getter: bool = True, raises: bool = False) -> MagicMock:
    provider = MagicMock()
    if not getter:
        # A provider type with no available_models attribute at all (the
        # claude-code placeholder before session init).
        del provider.available_models
        return provider
    if raises:
        provider.available_models = MagicMock(side_effect=RuntimeError("boom"))
    else:
        provider.available_models = MagicMock(return_value=models)
    return provider


def _request(*providers: MagicMock) -> MagicMock:
    state = MagicMock()
    state.sessions.active_providers = MagicMock(return_value=list(providers))
    request = MagicMock()
    request.app = {"state": state}
    return request


def _names(rows: list[dict]) -> list[str]:
    return [r["model_name"] for r in rows]


def test_advertised_narrows_the_catalog():
    # The free tier advertises auto + sonnet only: opus rows must not survive.
    request = _request(_provider([{"modelId": "auto"}, {"modelId": "claude-sonnet-5"}]))
    assert _names(agents._entitled_kiro_models(request, CATALOG)) == ["auto", "claude-sonnet-5"]


def test_dotted_and_dashed_spellings_match():
    # The adapter advertises dashed ids while the catalog row is dotted — the
    # same model, so it must be KEPT (a spelling mismatch dropping a legitimate
    # row is the failure mode this normalization exists to prevent).
    request = _request(_provider([{"modelId": "claude-opus-4-8"}]))
    assert _names(agents._entitled_kiro_models(request, CATALOG)) == ["claude-opus-4.8"]


def test_no_live_session_leaves_the_catalog_alone():
    # Nothing has initialized yet: entitlement is unknown, not "nothing".
    request = _request()
    assert agents._entitled_kiro_models(request, CATALOG) == CATALOG


def test_missing_state_leaves_the_catalog_alone():
    request = MagicMock()
    request.app = {}
    assert agents._entitled_kiro_models(request, CATALOG) == CATALOG


def test_backend_that_advertises_nothing_leaves_the_catalog_alone():
    request = _request(_provider([]))
    assert agents._entitled_kiro_models(request, CATALOG) == CATALOG


def test_provider_without_getter_is_skipped_not_fatal():
    # First provider has no getter; the second one's list still applies.
    request = _request(
        _provider(None, getter=False),
        _provider([{"modelId": "claude-sonnet-5"}]),
    )
    assert _names(agents._entitled_kiro_models(request, CATALOG)) == ["claude-sonnet-5"]


def test_getter_raising_is_skipped_not_fatal():
    request = _request(_provider(None, raises=True))
    assert agents._entitled_kiro_models(request, CATALOG) == CATALOG


def test_disjoint_advertised_set_fails_open():
    # A backend whose ids live in a different namespace (the claude backend's
    # bare/prefixed split) intersects nothing. Filtering there would empty the
    # picker, so the catalog is returned untouched.
    request = _request(_provider([{"modelId": "global.anthropic.claude-opus-4-8[1m]"}]))
    assert agents._entitled_kiro_models(request, CATALOG) == CATALOG


def test_malformed_advertised_entries_are_ignored():
    # advertised_model_ids tolerates junk; a list that yields no usable id is
    # the same as "advertised nothing".
    request = _request(_provider(["not-a-dict", {"no_model_id": 1}, {"modelId": ""}]))
    assert agents._entitled_kiro_models(request, CATALOG) == CATALOG


# ── End-to-end through the handler ──


async def _no_audit(**kwargs: Any) -> None:
    del kwargs


class _FakeProc:
    def __init__(self, stdout: bytes = b"", returncode: int = 0):
        self._stdout = stdout
        self.returncode = returncode

    def kill(self):  # noqa: D401 - matches Process API
        pass

    async def communicate(self):
        return self._stdout, b""


def _kiro_request(tmp_path: Path, *providers: MagicMock) -> MagicMock:
    service = KiroPrerequisiteService(
        platform_name="linux",
        environ={"HOME": str(tmp_path), "PATH": "/usr/bin:/bin"},
        home=tmp_path,
        audit_writer=_no_audit,
        assume_ready=True,
    )
    state = MagicMock()
    state.sessions.active_providers = MagicMock(return_value=list(providers))
    request = MagicMock()
    request.app = {"kiro_prerequisite_service": service, "state": state}
    return request


def test_api_models_returns_only_entitled_rows(tmp_path):
    payload = json.dumps({"models": CATALOG}).encode()
    request = _kiro_request(
        tmp_path, _provider([{"modelId": "auto"}, {"modelId": "claude-sonnet-5"}])
    )
    with patch.object(
        agents.KiroCrewConfig, "load", return_value=SimpleNamespace(agent=SimpleNamespace(provider="kiro"))
    ), patch(
        "kiro_crew.acp.client._resolve_kiro_bin_for_spawn", return_value="/usr/bin/kiro-cli"
    ), patch(
        "kiro_crew.acp.client._resolve_ssh_auth_sock", lambda env: None
    ), patch(
        "kiro_crew.env.augmented_path", lambda p: p
    ), patch(
        "kiro_crew.dashboard.handlers.agents.wrap_argv", lambda argv: (argv, None)
    ), patch(
        "kiro_crew.dashboard.handlers.agents.cgroup_scope_argv", lambda argv: argv
    ), patch(
        "kiro_crew.sandbox.resource_limit_preexec", lambda: None
    ), patch.object(
        agents.asyncio, "create_subprocess_exec", return_value=_FakeProc(stdout=payload)
    ):
        resp = asyncio.get_event_loop().run_until_complete(agents.api_models(request))
    assert resp.status == 200
    assert _names(json.loads(resp.body)) == ["auto", "claude-sonnet-5"]
