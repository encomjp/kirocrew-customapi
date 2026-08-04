"""Dashboard endpoints for Dev Container support (VS Code parity).

Routes (registered in server.py):
  GET    /api/devcontainer/status?project=...  — config presence, trust, container state
  GET    /api/devcontainer/config?project=...  — raw config + digest for the trust prompt
  POST   /api/devcontainer/trust               — {project}: grant trust for the CURRENT config
  DELETE /api/devcontainer/trust               — {project}: revoke
  POST   /api/devcontainer/rebuild             — {project}: rebuild the container

Input trust model: `project` is only accepted when it realpath-matches an
existing chat slot's project (the same barrier idea as worktree.py's
_allowed_repo_roots) — the trust decision is only meaningful for a directory
a session is actually scoped to, and this prevents an arbitrary caller from
probing or trusting paths sessions never touch.

Trust mutations are dashboard-caller-only and SEL-audited: granting trust
authorizes arbitrary container builds (image pulls, lifecycle hooks) for that
project, which is exactly the decision VS Code gates behind Workspace Trust.
"""

from __future__ import annotations

import asyncio
import os

from aiohttp import web

from kiro_crew.dashboard.chat_handlers import deny_non_dashboard_caller
from kiro_crew.devcontainer import (
    DevcontainerConfigChanged,
    DevcontainerError,
    config_preview,
    get_manager,
    grant_trust,
    revoke_trust,
)
from kiro_crew.sel import sel


def _slot_project_roots(state: object) -> set[str]:
    """Realpaths of every chat slot's project directory."""
    roots: set[str] = set()
    slots = getattr(state, "chat_slots", None) or {}
    values = slots.values() if hasattr(slots, "values") else []
    for slot in values:
        project = getattr(slot, "project", None)
        if isinstance(project, str) and project:
            try:
                roots.add(os.path.realpath(project))
            except OSError:
                continue
    return roots


async def _resolve_project(request: web.Request, raw: object) -> str | None:
    """Validate a caller-supplied project path against live slot projects."""
    if not isinstance(raw, str) or not raw.strip():
        return None
    probe = await asyncio.to_thread(os.path.realpath, raw.strip())
    roots = await asyncio.to_thread(_slot_project_roots, request.app.get("state"))
    return probe if probe in roots else None


async def api_devcontainer_status(request: web.Request) -> web.Response:
    """GET /api/devcontainer/status?project=<path>"""
    project = await _resolve_project(request, request.query.get("project"))
    if project is None:
        return web.json_response({"error": "unknown project", "code": "unknown_project"}, status=400)
    status = await get_manager().status(project)
    return web.json_response(status)


async def api_devcontainer_config(request: web.Request) -> web.Response:
    """GET /api/devcontainer/config?project=<path> — for the trust prompt.

    Dashboard-caller-only: the response carries raw file bytes from the
    project tree, which no app/internal caller has business reading through
    this surface (B1 review finding — pairs with the O_NOFOLLOW +
    containment + sensitive-path screens in _read_config_bytes).
    """
    denied = deny_non_dashboard_caller(request, "devcontainer_config")
    if denied is not None:
        return denied
    project = await _resolve_project(request, request.query.get("project"))
    if project is None:
        return web.json_response({"error": "unknown project", "code": "unknown_project"}, status=400)
    try:
        preview = await asyncio.to_thread(config_preview, project)
    except DevcontainerError as exc:
        return web.json_response({"error": str(exc), "code": "no_devcontainer_config"}, status=404)
    return web.json_response(preview)


async def api_devcontainer_trust(request: web.Request) -> web.Response:
    """POST /api/devcontainer/trust {project, digest} — grant for current config.

    ``digest`` is the fingerprint the dashboard showed in the trust prompt, and
    it is REQUIRED. Granting against whatever happens to be on disk would let
    the agent rewrite ``.devcontainer/`` between the preview and the click and
    get its own configuration authorized. A mismatch returns 409 so the UI can
    re-read and re-prompt with the new bytes.
    """
    caller = str(request.get("user") or "dashboard")
    denied = deny_non_dashboard_caller(request, "devcontainer_trust")
    if denied is not None:
        return denied
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON", "code": "invalid_json"}, status=400)
    project = await _resolve_project(request, (body or {}).get("project"))
    if project is None:
        return web.json_response({"error": "unknown project", "code": "unknown_project"}, status=400)
    reviewed = (body or {}).get("digest")
    if not isinstance(reviewed, str) or not reviewed.strip():
        return web.json_response(
            {
                "error": "digest of the reviewed configuration is required",
                "code": "digest_required",
            },
            status=400,
        )
    try:
        digest = await asyncio.to_thread(grant_trust, project, reviewed.strip())
    except DevcontainerConfigChanged as exc:
        sel().log_api_access(
            caller=caller,
            operation="devcontainer_trust.grant",
            outcome="denied",
            resources=f"project={project}",
            error="config changed between preview and grant",
        )
        return web.json_response(
            {"error": str(exc), "code": "devcontainer_config_changed"}, status=409
        )
    except DevcontainerError as exc:
        return web.json_response({"error": str(exc), "code": "no_devcontainer_config"}, status=404)
    sel().log_api_access(
        caller=caller,
        operation="devcontainer_trust.grant",
        outcome="success",
        resources=f"project={project} digest={digest[:12]}",
    )
    return web.json_response({"trusted": True, "digest": digest})


async def api_devcontainer_untrust(request: web.Request) -> web.Response:
    """DELETE /api/devcontainer/trust {project}"""
    caller = str(request.get("user") or "dashboard")
    denied = deny_non_dashboard_caller(request, "devcontainer_trust")
    if denied is not None:
        return denied
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON", "code": "invalid_json"}, status=400)
    project = await _resolve_project(request, (body or {}).get("project"))
    if project is None:
        return web.json_response({"error": "unknown project", "code": "unknown_project"}, status=400)
    removed = await asyncio.to_thread(revoke_trust, project)
    sel().log_api_access(
        caller=caller,
        operation="devcontainer_trust.revoke",
        outcome="success" if removed else "noop",
        resources=f"project={project}",
    )
    return web.json_response({"trusted": False, "removed": removed})


async def api_devcontainer_rebuild(request: web.Request) -> web.Response:
    """POST /api/devcontainer/rebuild {project} — trust-gated full rebuild."""
    caller = str(request.get("user") or "dashboard")
    denied = deny_non_dashboard_caller(request, "devcontainer_rebuild")
    if denied is not None:
        return denied
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON", "code": "invalid_json"}, status=400)
    project = await _resolve_project(request, (body or {}).get("project"))
    if project is None:
        return web.json_response({"error": "unknown project", "code": "unknown_project"}, status=400)
    try:
        info = await get_manager().up(project, rebuild=True)
    except DevcontainerError as exc:
        # Covers DevcontainerNotTrusted too: rebuild of an untrusted config
        # must fail, not silently re-grant.
        return web.json_response({"error": str(exc), "code": "devcontainer_up_failed"}, status=409)
    sel().log_api_access(
        caller=caller,
        operation="devcontainer_rebuild",
        outcome="success",
        resources=f"project={project} container={info.container_id[:12]}",
    )
    return web.json_response(
        {
            "container_id": info.container_id,
            "remote_workspace_folder": info.remote_workspace_folder,
        }
    )
