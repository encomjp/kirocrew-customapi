"""The vision_analyze tool: what it advertises and what it does.

Fork-only tool. ``vision_analyze`` lets a text-only model's agent describe an
image (local absolute path or http(s) URL) through the configured vision
provider chain, so screenshots never have to reach an upstream that rejects
image content. The schema half lives in :mod:`kiro_crew.validation`
(``VISION_ANALYZE_SCHEMA``); this module carries the descriptor shown to the
model plus the handler that runs the chain.

Handlers reach shared plumbing lazily and at call time, mirroring the other
domain modules: config is loaded inside the handler (the config -> dashboard
-> acp import cycle forbids module-scope loads), and the vision chain is
imported where used so a broken optional dependency cannot take down the MCP
server at boot.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

from kiro_crew.validation import VISION_ANALYZE_SCHEMA


def schemas() -> list[dict[str, Any]]:
    """Descriptor for vision_analyze."""
    return [
        {
            "name": "vision_analyze",
            "description": (
                "Describe an image for a text-only model: pass a local absolute "
                "path (e.g. a screenshot you just captured at /tmp/shot.png) or an "
                "http(s) image URL, and get back a 1-3 sentence text description "
                "from a vision-capable model. Use this INSTEAD of trying to inline "
                "an image on a model that rejects image input (deepseek-v4-flash "
                "family) — the image never reaches the text-only upstream. "
                "Exactly one of path or url is required."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Absolute path to a local image file",
                    },
                    "url": {
                        "type": "string",
                        "description": "http(s) URL of an image",
                    },
                },
                "anyOf": [{"required": ["path"]}, {"required": ["url"]}],
            },
        },
    ]


def _run_vision_analyze(name: str, args: dict[str, Any]) -> str:
    """Describe an image (path or url) via the configured vision provider chain.

    Runs synchronously in the MCP stdio worker thread: the vision subagent is a
    one-shot AcpClient on the configured vision fallback chain (default
    ``cmc/mimo-v2.5``) against the same router proxy the main session uses (or
    each ``vision_providers`` entry in turn).
    """
    from kiro_crew import mcp_core
    from kiro_crew.acp.vision import describe_image_via_chain, resolve_vision_providers

    ref = args.get("path") or args.get("url") or ""

    # Config through the mcp_core seam (attribute lookup resolves at CALL time,
    # so tests that rebind it still intercept the handler) — the same pattern
    # as every other domain module. The lazy attribute access also dodges the
    # config.loader -> dashboard -> session -> acp import cycle.
    cfg = mcp_core.KiroCrewConfig.load()

    vision_fallback = (cfg.agent.vision_fallback_model or "").strip() or "cmc/mimo-v2.5"

    # Mirror the provider factory's env wiring for the vision subagent: the
    # router base URL + API key (config key > ANTHROPIC_API_KEY env >
    # CLIPROXY_API_KEY env, exactly the loader's precedence).
    backend = (
        "claude"
        if cfg.agent.provider == "claude_code"
        else ("opencode" if cfg.agent.provider == "opencode" else "")
    )
    env: dict[str, str] = {}
    base_url = (cfg.agent.provider_base_url or "").strip()
    api_key = (
        (cfg.agent.provider_api_key or "").strip()
        or os.environ.get("ANTHROPIC_API_KEY")
        or os.environ.get("CLIPROXY_API_KEY")
    )
    if base_url:
        env["ANTHROPIC_BASE_URL"] = base_url
    if api_key:
        env["ANTHROPIC_API_KEY"] = api_key
    if backend == "opencode":
        env.setdefault("OPENCODE_API_FORMAT", cfg.agent.provider_api_format or "openai")

    providers = resolve_vision_providers(
        vision_providers=list(cfg.agent.vision_providers or []),
        vision_fallback_model=vision_fallback,
        main_env=env,
        main_backend=backend,
    )
    if not providers:
        return "Error: no vision provider configured"

    # For a local path, verify readability + sensitive-path gate up front so a
    # bad ref returns a clean error instead of a subagent spawn failure.
    if args.get("path"):
        p = Path(ref)
        if not p.is_file():
            return f"Error: no such file: {ref}"
        try:
            from kiro_crew.hooks import safe_read_file_bytes

            if safe_read_file_bytes(str(p)) is None:
                return f"Error: image read refused (sensitive path): {ref}"
        except Exception:
            # Fall through to the subagent which surfaces its own error.
            pass

    try:
        description = asyncio.run(
            describe_image_via_chain(
                ref,
                providers,
                sandbox_mode=cfg.agent.sandbox,
            )
        )
    except Exception as exc:  # noqa: BLE001 - surface a clean tool error
        return f"Error: vision describe failed: {exc}"
    if not description or description == "unavailable":
        return "Error: vision describe failed (no description returned)"
    return description


HANDLERS: dict[str, Any] = {
    VISION_ANALYZE_SCHEMA.tool_name: _run_vision_analyze,
}
