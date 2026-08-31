"""Minimal tests for kirocrew-bridge MCP adapter (H7/bounds).

Covers:
- bridge dist exists and is not empty (packaging contract)
- electron-builder extraResources includes the bridge (bundle gate)
- bridge package.json declares correct entry points
"""

from __future__ import annotations

import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
BRIDGE_PKG = REPO_ROOT / "mcp" / "kirocrew-bridge" / "package.json"
BRIDGE_DIST = REPO_ROOT / "mcp" / "kirocrew-bridge" / "dist" / "index.js"
ELECTRON_PKG = REPO_ROOT / "website" / "electron" / "package.json"


def test_bridge_dist_exists():
    assert BRIDGE_DIST.exists(), "mcp/kirocrew-bridge/dist/index.js must exist after build"
    assert BRIDGE_DIST.stat().st_size > 1000, "bridge dist too small — build likely failed"


def test_bridge_package_json():
    data = json.loads(BRIDGE_PKG.read_text())
    assert data["name"] == "kirocrew-bridge"
    assert data["bin"]["kirocrew-bridge"] == "./dist/index.js"
    assert data["type"] == "module"


def test_electron_extra_resources_includes_bridge():
    data = json.loads(ELECTRON_PKG.read_text())
    resources = data["build"]["extraResources"]
    bridge = next((r for r in resources if r["from"] == "../../mcp/kirocrew-bridge/dist"), None)
    assert bridge is not None, "electron extraResources must include kirocrew-bridge"
    assert bridge["to"] == "kirocrew-bridge"
    assert bridge["filter"] == ["**/*"]
