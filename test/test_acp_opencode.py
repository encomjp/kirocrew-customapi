"""Tests for the OpenCode ACP backend (config seeding + binary resolution)."""

from __future__ import annotations

import json
from unittest.mock import patch

from kiro_crew.acp import client as acp
from unittest.mock import MagicMock, patch
from kiro_crew.acp.types import ACP_BACKEND_OPENCODE


def test_opencode_bin_resolution_missing() -> None:
    with patch("shutil.which", return_value=None):
        assert acp._resolve_opencode_bin() is None


def test_opencode_bin_resolution_path() -> None:
    with patch("shutil.which", return_value="/usr/bin/opencode"):
        assert acp._resolve_opencode_bin() == ["/usr/bin/opencode"]


def test_write_opencode_provider_config(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    client = object.__new__(acp.AcpClient)
    client._extra_env = {
        "ANTHROPIC_BASE_URL": "http://localhost:8317",
        "ANTHROPIC_API_KEY": "sk-opencode-test",
        "OPENCODE_API_FORMAT": "openai",
    }
    client._write_opencode_provider_config()

    cfg_path = tmp_path / ".config" / "opencode" / "opencode.json"
    data = json.loads(cfg_path.read_text(encoding="utf-8"))
    prov = data["provider"]["kirocrew"]
    assert prov["npm"] == "@ai-sdk/openai-compatible"
    assert prov["options"] == {
        "baseURL": "http://localhost:8317",
        "apiKey": "sk-opencode-test",
    }


def test_write_opencode_provider_config_preserves_existing(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    cfg_dir = tmp_path / ".config" / "opencode"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "opencode.json").write_text(
        json.dumps({"provider": {"anthropic": {"options": {"baseURL": "https://api.anthropic.com"}}}}),
        encoding="utf-8",
    )
    client = object.__new__(acp.AcpClient)
    client._extra_env = {"ANTHROPIC_BASE_URL": "http://localhost:8317", "OPENCODE_API_FORMAT": "anthropic"}
    client._write_opencode_provider_config()

    data = json.loads((cfg_dir / "opencode.json").read_text(encoding="utf-8"))
    assert data["provider"]["anthropic"]["options"]["baseURL"] == "https://api.anthropic.com"
    assert data["provider"]["kirocrew"]["npm"] == "@ai-sdk/anthropic"
    assert data["provider"]["kirocrew"]["options"]["baseURL"] == "http://localhost:8317"


def test_backend_identifier() -> None:
    assert ACP_BACKEND_OPENCODE == "opencode"


def test_set_model_falls_back_to_default_when_unadvertised():
    """A stale/unadvertised model must never reach the wire: set_model falls
    back to the configured default, then 'auto', instead of raising."""
    import asyncio

    from kiro_crew.acp.client import AcpClient

    client = object.__new__(AcpClient)
    client._acp_backend = "opencode"
    client._session_id = "s1"
    client._extra_env = {"KIROCREW_DEFAULT_MODEL": "deepseek-v4-flash:0731"}
    client._available_models = [
        {"modelId": "deepseek-v4-flash:0731"},
        {"modelId": "glm-5.2"},
    ]
    client._model = "cmc/deepseek-v4-pro"
    client._resolved_model_id = ""
    client.last_prompt_stats = MagicMock()
    async def _fake_send(*a, **k):
        return None

    client._send_request = _fake_send

    asyncio.get_event_loop().run_until_complete(client.set_model("cmc/deepseek-v4-pro"))
    assert client._model == "deepseek-v4-flash:0731", client._model


def test_set_model_raises_only_when_default_also_unusable():
    """Genuinely broken entitlement (neither the request nor the default is
    advertised) still raises AcpModelUnavailable."""
    import asyncio

    import pytest

    from kiro_crew.acp.client import AcpClient, AcpModelUnavailable

    client = object.__new__(AcpClient)
    client._acp_backend = "opencode"
    client._session_id = "s1"
    client._extra_env = {"KIROCREW_DEFAULT_MODEL": "not-advertised-either"}
    client._available_models = [{"modelId": "glm-5.2"}]
    client._model = "cmc/deepseek-v4-pro"
    client._resolved_model_id = ""
    client.last_prompt_stats = MagicMock()

    with pytest.raises(AcpModelUnavailable):
        asyncio.get_event_loop().run_until_complete(client.set_model("cmc/deepseek-v4-pro"))
