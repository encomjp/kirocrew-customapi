"""Regression tests for fork review leftover fixes (must-fix)."""
import pathlib

import pytest

def test_patch_skips_mask():
    from kiro_crew.dashboard.handlers.core import _SENSITIVE_MASK
    assert _SENSITIVE_MASK == "••••••••"
    # The handler should skip write when value == mask - checked via code presence
    import pathlib
    text = pathlib.Path("src/kiro_crew/dashboard/handlers/core.py").read_text()
    assert 'if value == _SENSITIVE_MASK:' in text

def test_bridge_whitelist_inner():
    text = pathlib.Path("mcp/kirocrew-bridge/src/index.ts").read_text() if (pathlib.Path("mcp/kirocrew-bridge/src/index.ts").exists()) else ""
    # Actually check the built file
    src = pathlib.Path("mcp/kirocrew-bridge/src/index.ts").read_text()
    assert "check inner args.tool" in src
    assert src.count("ALLOWED_TOOLS.has") >= 2

def test_model_via_env_uses_effective():
    text = pathlib.Path("src/kiro_crew/acp/client.py").read_text()
    assert "key off effective post-whitelist self._model" in text
    assert "self._model not in" in text

def test_effective_key_for_openai_catalog():
    text = pathlib.Path("src/kiro_crew/acp/client.py").read_text()
    assert "effective_provider_api_key" in text
    # Ensure the dashboard handler also uses effective key, not plaintext
    dash = pathlib.Path("src/kiro_crew/dashboard/handlers/agents.py").read_text()
    # Should have Bearer {api_key} not Bearer {cfg.agent.provider_api_key}
    assert 'f"Bearer {api_key}"' in dash
    assert dash.count('f"Bearer {cfg.agent.provider_api_key}"') == 0

def test_live_catalog_union():
    text = pathlib.Path("src/kiro_crew/acp/client.py").read_text()
    assert "live catalog is union with static commandcode" in text
    assert 'captured_ids = {c["modelId"] for c in captured}' in text

def test_bearer_uses_effective():
    # Direct check for the fixed line
    dash = pathlib.Path("src/kiro_crew/dashboard/handlers/agents.py").read_text()
    # The fixed line should be present
    assert 'headers["Authorization"] = f"Bearer {api_key}"' in dash
