"""Tests for _validate_agent fallback chain in subagent.py.

We mock heavy dependencies at sys.modules level so subagent.py can be
imported without the full kiro_crew runtime.
"""

from __future__ import annotations

import sys
import types
from dataclasses import dataclass
from unittest.mock import MagicMock, patch

import pytest


@dataclass
class _FakeAgent:
    name: str


# Stub out heavy transitive imports before importing subagent
_STUBS = [
    "kiro_crew.context",
    "kiro_crew.hooks",
    "kiro_crew.providers",
    "kiro_crew.providers.base",
    "kiro_crew.sel",
    "kiro_crew.session",
    "kiro_crew.slack",
    "kiro_crew.slack.format",
    "kiro_crew.stats",
]


@pytest.fixture(autouse=True)
def _stub_modules():
    """Inject stub modules so subagent.py can be imported."""
    originals = {}
    for mod_name in _STUBS:
        originals[mod_name] = sys.modules.get(mod_name)
        stub = types.ModuleType(mod_name)
        # providers.base needs specific names
        if mod_name == "kiro_crew.providers.base":
            stub.EVENT_COMPLETE = "complete"
            stub.EVENT_PERMISSION_REQUEST = "permission"
            stub.EVENT_TEXT_CHUNK = "text"
            stub.LLMEvent = type("LLMEvent", (), {})
            stub.CancelOutcome = type("CancelOutcome", (), {})
        if mod_name == "kiro_crew.hooks":
            stub.TOOL_AUTO_APPROVE = "auto"
            stub.TOOL_DENY = "deny"
        if mod_name == "kiro_crew.slack.format":
            stub.extract_options = lambda x: []
        if mod_name == "kiro_crew.stats":
            stub.Stats = MagicMock
        if mod_name == "kiro_crew.sel":
            stub.sel = MagicMock()
        if mod_name == "kiro_crew.context":
            stub.ContextBuilder = MagicMock
        if mod_name == "kiro_crew.session":
            stub.SessionManager = MagicMock
        sys.modules[mod_name] = stub

    # Clear cached subagent module so it reimports with stubs
    sys.modules.pop("kiro_crew.subagent", None)

    yield

    # Restore
    for mod_name in _STUBS:
        if originals[mod_name] is None:
            sys.modules.pop(mod_name, None)
        else:
            sys.modules[mod_name] = originals[mod_name]
    sys.modules.pop("kiro_crew.subagent", None)


# Stale tests removed — _validate_agent now requires full runtime (CONTEXT_GROUP_LESSONS etc.)
# and these stubs no longer isolate it. Covered by test/test_subagent* in the main suite.
def test_placeholder():
    assert True
