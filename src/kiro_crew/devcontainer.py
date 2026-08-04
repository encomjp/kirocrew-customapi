"""Dev Container support: run a session's kiro-cli inside the project's
devcontainer (VS Code parity).

When ``agent.devcontainer`` is ``"auto"`` and a session's work dir carries a
``.devcontainer/devcontainer.json`` (or ``.devcontainer.json``), the ACP spawn
path replaces the host kiro-cli argv with a ``docker exec`` into a container
built by the reference ``@devcontainers/cli`` — the same engine VS Code uses.
The repo's devcontainer.json is honored in full (image/build, features,
lifecycle hooks, mounts, runArgs) after a one-time per-config human trust
grant, mirroring VS Code's Workspace Trust model. The gateway does NOT strip
or override the file: parity, not a sandbox.

Architecture (mirrors VS Code's client/server split):
  - gateway stays on the host (UI plane);
  - kiro-cli is executed INSIDE the container (execution plane), like
    vscode-server. Verified necessary: kiro-cli 2.14 executes shell/file
    tools in-process and ignores the ACP client fs/terminal capabilities,
    so the process itself must move.
  - the workspace is bind-mounted by the devcontainer CLI; the ACP
    ``session/new`` cwd uses the container-side workspace folder.

Trust model: the SHA-256 of the effective devcontainer.json must be granted
by a dashboard user before any build or exec. Config edits invalidate trust
(hash mismatch → re-prompt), matching VS Code's re-prompt on change.

Container reuse: one container per project directory, keyed by an id-label,
reused across sessions and gateway restarts (``devcontainer up`` is
idempotent for an unchanged config).

Known v1 limitations (documented in docs/devcontainers.md):
  - Kiro Crew's own managed MCP servers (mcp-core/cron/computer) are not
    reachable from inside the container (their REST callback targets the
    gateway's host loopback). kiro-cli reports mcp_server_init_failure and
    the session continues with the project toolchain fully functional.
  - /proc-based liveness observes the host-side ``docker exec`` client
    proxy: death detection works (pipe close), wedge heuristics degrade.
  - Linux hosts only. On macOS, Docker Desktop is a VM; the existing
    Seatbelt sandbox path is unchanged.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import shutil
import stat
import time
from dataclasses import dataclass
from pathlib import Path

from kiro_crew.config.loader import config_dir

logger = logging.getLogger(__name__)

# jsonc comments are legal in devcontainer.json; strip for hashing/preview
# only — the devcontainer CLI does its own real parse.
_LINE_COMMENT_RE = re.compile(r"^\s*//.*$", re.MULTILINE)

# Marker env for processes exec'd into a container, so in-container helpers
# can identify their exec instance (kill file naming, diagnostics).
DEVCONTAINER_EXEC_ENV = "KIROCREW_DEVCONTAINER_EXEC"

# Where exec pid files live inside the container. tmpfs on most images.
_EXEC_PIDFILE_DIR = "/tmp/kirocrew-exec"

_UP_TIMEOUT_SECS = 15 * 60  # image build + feature install can be slow
_EXEC_PROBE_TIMEOUT_SECS = 20


class DevcontainerError(RuntimeError):
    """A devcontainer operation failed. Message is operator-facing."""


class DevcontainerNotTrusted(DevcontainerError):
    """The project's devcontainer.json has no valid trust grant."""


class DevcontainerConfigChanged(DevcontainerError):
    """The config changed between being shown to a human and being trusted.

    Distinct from DevcontainerNotTrusted so the dashboard can tell "you never
    approved this" from "what you approved is no longer what is on disk" and
    re-prompt with the new bytes rather than reporting a plain refusal.
    """


def find_devcontainer_config(project_dir: str | Path) -> Path | None:
    """Locate the project's devcontainer config, spec lookup order.

    ``.devcontainer/devcontainer.json`` wins over ``.devcontainer.json``.
    Returns None when the project has no devcontainer config.

    Symlink leaves are treated as absent: the config is read back to the
    caller and hashed for trust, so a link pointing outside the project
    (``.devcontainer/devcontainer.json -> ~/.aws/credentials``) would turn
    the preview endpoint into an arbitrary-file read. _read_config_bytes
    enforces the same property at open time (lstat here is advisory).
    """
    root = Path(project_dir)
    for candidate in (
        root / ".devcontainer" / "devcontainer.json",
        root / ".devcontainer.json",
    ):
        try:
            if candidate.is_file() and not candidate.is_symlink():
                return candidate
        except OSError:
            continue
    return None


def _project_root_of(config_path: Path) -> Path:
    """The project directory a config path belongs to (both spec layouts)."""
    parent = config_path.parent
    return parent.parent if parent.name == ".devcontainer" else parent


def _read_config_bytes(config_path: Path) -> bytes:
    """Read the config refusing symlinks, escapes, and sensitive targets.

    Defense in depth for the trust-preview read path (the bytes go back to
    the dashboard caller verbatim):
      1. O_NOFOLLOW on the final component — a symlink leaf fails with ELOOP
         even if it appeared between lookup and open (TOCTOU);
      2. fstat must report a regular file;
      3. the realpath must stay inside the project root — covers a symlinked
         PARENT directory (.devcontainer -> elsewhere), which O_NOFOLLOW on
         the leaf cannot see;
      4. is_sensitive_path screen on the resolved target.
    """
    from kiro_crew.security import is_sensitive_path  # circular import

    resolved = os.path.realpath(config_path)
    root = os.path.realpath(_project_root_of(config_path))
    if not resolved.startswith(root.rstrip(os.sep) + os.sep):
        raise DevcontainerError(
            f"devcontainer config resolves outside the project: {config_path}"
        )
    if is_sensitive_path(resolved):
        raise DevcontainerError(
            f"devcontainer config resolves to a sensitive path: {config_path}"
        )
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(str(config_path), os.O_RDONLY | nofollow)
    except OSError as exc:
        raise DevcontainerError(f"cannot open devcontainer config: {exc}") from exc
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise DevcontainerError(
                f"devcontainer config is not a regular file: {config_path}"
            )
        with os.fdopen(fd, "rb") as fh:
            fd = -1
            return fh.read()
    finally:
        if fd >= 0:
            os.close(fd)


def config_digest(config_path: Path) -> str:
    """Trust digest for a devcontainer config. Trust grants bind to this.

    Covers the WHOLE ``.devcontainer/`` directory tree (sorted relpath +
    content per file), not just devcontainer.json: a referenced Dockerfile,
    compose file, or postCreateCommand script can change what a build executes
    while the json stays byte-identical.

    A symlink ANYWHERE in the tree is refused rather than skipped. Skipping one
    would leave it outside the digest, so its target could be retargeted (or
    its content swapped) after the grant without changing the hash — and a
    lifecycle hook like ``bash setup.sh`` would then run unreviewed code under
    a still-valid trust. Refusing fails closed instead.

    For the root-layout ``.devcontainer.json`` there is no directory; the
    digest covers that one file, read through the same hardened opener.

    Blocking I/O (directory walk + reads). Callers on the event loop must
    offload it — see the ``asyncio.to_thread`` sites in DevcontainerManager
    and AcpClient._maybe_devcontainer_info.
    """
    h = hashlib.sha256()
    parent = config_path.parent
    if parent.name == ".devcontainer":
        entries = sorted(parent.rglob("*"))
        for p in entries:
            if p.is_symlink():
                raise DevcontainerError(
                    f"devcontainer tree contains a symlink, which cannot be "
                    f"content-bound to a trust grant: {p}"
                )
            if not p.is_file():
                continue
            rel = str(p.relative_to(parent))
            h.update(rel.encode())
            h.update(b"\0")
            try:
                h.update(p.read_bytes())
            except OSError:
                h.update(b"<unreadable>")
            h.update(b"\0")
        # The config itself is inside the walk; the marker below just makes
        # the layout unambiguous vs the single-file branch.
        h.update(b"tree")
    else:
        h.update(_read_config_bytes(config_path))
        h.update(b"file")
    return h.hexdigest()


# ── Trust store ──────────────────────────────────────────────────────────
#
# JSON file mapping realpath(project_dir) -> {"digest": ..., "granted_at": ...,
# "config_path": ...}. A grant is valid only while the current config bytes
# hash to the recorded digest, so any edit (including by an agent) forces a
# fresh human decision — the devcontainer analogue of Workspace Trust.


def _trust_path() -> Path:
    return config_dir() / "devcontainers" / "trust.json"


def _read_trust() -> dict:
    try:
        data = json.loads(_trust_path().read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_trust(data: dict) -> None:
    path = _trust_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True))
    os.replace(tmp, path)


def is_trusted(project_dir: str | Path) -> bool:
    """True when the project's CURRENT devcontainer tree carries a grant.

    Fails closed: a tree whose digest cannot be computed — including one that
    grew a symlink after the grant (config_digest refuses those) — is NOT
    trusted. Blocking I/O; callers on the event loop must offload it.
    """
    cfg = find_devcontainer_config(project_dir)
    if cfg is None:
        return False
    key = os.path.realpath(str(project_dir))
    entry = _read_trust().get(key)
    if not isinstance(entry, dict):
        return False
    try:
        return entry.get("digest") == config_digest(cfg)
    except (OSError, DevcontainerError):
        return False


def grant_trust(project_dir: str | Path, expected_digest: str | None = None) -> str:
    """Record a trust grant for the project's current config. Returns digest.

    ``expected_digest`` binds the grant to the bytes a human actually
    reviewed: the dashboard passes back the digest it showed in the trust
    prompt, and a mismatch raises instead of granting. Without it there is a
    window between the preview read and the grant in which the agent can
    rewrite ``.devcontainer/`` and have its OWN configuration authorized —
    the digest recorded here is computed from whatever is on disk now, not
    from what was displayed. Optional only so a deliberate caller with no
    prior preview (tests, CLI) can still grant.

    Caller (the dashboard trust endpoint) is responsible for having shown
    the config to a human first; this function only records the decision.
    """
    cfg = find_devcontainer_config(project_dir)
    if cfg is None:
        raise DevcontainerError(f"no devcontainer config under {project_dir}")
    digest = config_digest(cfg)
    if expected_digest is not None and expected_digest != digest:
        raise DevcontainerConfigChanged(
            f"devcontainer config for {project_dir} changed since it was shown: "
            f"reviewed {expected_digest[:12]}, on disk {digest[:12]} — re-read "
            f"the configuration before trusting it"
        )
    key = os.path.realpath(str(project_dir))
    data = _read_trust()
    data[key] = {
        "digest": digest,
        "config_path": str(cfg),
        "granted_at": time.time(),
    }
    _write_trust(data)
    logger.info("devcontainer trust granted for %s (digest %s)", key, digest[:12])
    return digest


def revoke_trust(project_dir: str | Path) -> bool:
    """Remove a project's grant. Returns True when one existed."""
    key = os.path.realpath(str(project_dir))
    data = _read_trust()
    if key in data:
        del data[key]
        _write_trust(data)
        logger.info("devcontainer trust revoked for %s", key)
        return True
    return False


def config_preview(project_dir: str | Path) -> dict:
    """Digest + raw text of the config, for the dashboard trust prompt.

    Reads through _read_config_bytes so the same symlink/containment/
    sensitive-path screens gate the preview as gate the digest — this raw
    text is returned verbatim to the dashboard caller.
    """
    cfg = find_devcontainer_config(project_dir)
    if cfg is None:
        raise DevcontainerError(f"no devcontainer config under {project_dir}")
    raw_bytes = _read_config_bytes(cfg)
    raw = raw_bytes.decode(errors="replace")
    parsed: dict | None = None
    try:
        parsed = json.loads(_LINE_COMMENT_RE.sub("", raw))
    except ValueError:
        parsed = None  # preview stays raw-text; CLI owns real jsonc parsing
    return {
        "config_path": str(cfg),
        "digest": config_digest(cfg),
        "raw": raw[:65536],
        "name": (parsed or {}).get("name"),
        "image": (parsed or {}).get("image"),
        "trusted": is_trusted(project_dir),
    }


# ── Container lifecycle ──────────────────────────────────────────────────


@dataclass
class DevcontainerInfo:
    """Result of a successful ``devcontainer up`` for one project."""

    container_id: str
    remote_workspace_folder: str
    remote_user: str
    project_dir: str  # realpath key
    config_digest: str
    created_at: float


def _cli_argv() -> list[str]:
    """Resolve the devcontainer CLI. Prefer a real binary; fall back to npx.

    ``npx --yes`` downloads on first use; the docs tell operators to install
    ``@devcontainers/cli`` globally for deterministic startup.
    """
    binary = shutil.which("devcontainer")
    if binary:
        return [binary]
    npx = shutil.which("npx")
    if npx:
        return [npx, "--yes", "@devcontainers/cli"]
    raise DevcontainerError(
        "devcontainer CLI not found: install with 'npm i -g @devcontainers/cli' "
        "(or ensure npx is on PATH)"
    )


def docker_available() -> bool:
    return shutil.which("docker") is not None


class DevcontainerManager:
    """One container per project directory, built by the devcontainer CLI.

    All state is derivable: the container is found again after a gateway
    restart via its id-label, so nothing here needs persistence. up() calls
    for the same project are serialized (image builds are not concurrent-safe
    on one config).
    """

    def __init__(self) -> None:
        self._infos: dict[str, DevcontainerInfo] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def _lock_for(self, key: str) -> asyncio.Lock:
        # Safe without a guard ONLY because there is no await between the
        # get and the set — both run within one event-loop step (N4: this
        # invariant is load-bearing; do not insert awaits here).
        lock = self._locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[key] = lock
        return lock

    @staticmethod
    def _id_label(key: str) -> str:
        # Stable per-project container identity; realpath-keyed digest keeps
        # the label short and free of path-charset issues.
        return f"kirocrew.devcontainer={hashlib.sha256(key.encode()).hexdigest()[:24]}"

    async def up(self, project_dir: str | Path, *, rebuild: bool = False) -> DevcontainerInfo:
        """Create or reuse the project's devcontainer. Trust-gated.

        Raises DevcontainerNotTrusted before running anything when the
        current config has no valid grant.
        """
        key = os.path.realpath(str(project_dir))
        cfg = await asyncio.to_thread(find_devcontainer_config, key)
        if cfg is None:
            raise DevcontainerError(f"no devcontainer config under {key}")
        # Trust check and digest both walk + read the .devcontainer tree, so
        # they run off the event loop (a large tree would otherwise stall every
        # gateway task while status polling recomputes the hash).
        if not await asyncio.to_thread(is_trusted, key):
            raise DevcontainerNotTrusted(
                f"devcontainer.json for {key} is not trusted; grant trust in "
                f"the dashboard before the container can be used"
            )
        digest = await asyncio.to_thread(config_digest, cfg)

        async with self._lock_for(key):
            cached = self._infos.get(key)
            if cached and cached.config_digest == digest and not rebuild:
                if await self._alive(cached.container_id):
                    return cached
                self._infos.pop(key, None)

            argv = [
                *_cli_argv(),
                "up",
                "--workspace-folder", key,
                "--id-label", self._id_label(key),
                "--log-format", "json",
            ]
            if rebuild or (cached and cached.config_digest != digest):
                argv.append("--remove-existing-container")

            logger.info("devcontainer up starting for %s", key)
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=key,
            )
            try:
                out_b, err_b = await asyncio.wait_for(
                    proc.communicate(), timeout=_UP_TIMEOUT_SECS
                )
            except asyncio.TimeoutError:
                proc.kill()
                raise DevcontainerError(
                    f"devcontainer up timed out after {_UP_TIMEOUT_SECS}s for {key}"
                )
            result = self._parse_up_output(out_b.decode(errors="replace"))
            if proc.returncode != 0 or result.get("outcome") != "success":
                tail = err_b.decode(errors="replace")[-2000:]
                desc = result.get("message") or result.get("description") or tail
                raise DevcontainerError(f"devcontainer up failed for {key}: {desc}")

            # Post-build digest re-verification: the devcontainer CLI re-read
            # the config tree from disk during the build, so a swap timed
            # between the pre-check above and the CLI's read would have built
            # UNTRUSTED content (M3 TOCTOU). A mismatch tears the container
            # down rather than handing it to a session.
            post_digest = await asyncio.to_thread(config_digest, cfg)
            if post_digest != digest:
                container_id = result.get("containerId", "")
                if container_id:
                    rm = await asyncio.create_subprocess_exec(
                        "docker", "rm", "-f", container_id,
                        stdout=asyncio.subprocess.DEVNULL,
                        stderr=asyncio.subprocess.DEVNULL,
                    )
                    try:
                        await asyncio.wait_for(rm.wait(), timeout=60)
                    except asyncio.TimeoutError:
                        rm.kill()
                raise DevcontainerNotTrusted(
                    f"devcontainer config for {key} changed during the build; "
                    f"container discarded — re-grant trust for the new config"
                )

            info = DevcontainerInfo(
                container_id=result["containerId"],
                remote_workspace_folder=result.get("remoteWorkspaceFolder", key),
                remote_user=result.get("remoteUser", ""),
                project_dir=key,
                config_digest=digest,
                created_at=time.time(),
            )
            # Preflight: without kiro-cli in the image, the session's later
            # `docker exec ... kiro-cli` exits 127 and surfaces as a generic
            # ACP init failure with no hint of the cause (N1). Fail here with
            # the fix in the message instead.
            probe = await asyncio.create_subprocess_exec(
                "docker", "exec", info.container_id,
                "sh", "-c", "command -v kiro-cli",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            try:
                await asyncio.wait_for(probe.wait(), timeout=_EXEC_PROBE_TIMEOUT_SECS)
            except asyncio.TimeoutError:
                probe.kill()
                raise DevcontainerError(
                    f"devcontainer for {key} is unresponsive to exec probes"
                )
            if probe.returncode != 0:
                raise DevcontainerError(
                    f"kiro-cli is not installed in the devcontainer for {key}. "
                    f"Install it in the image or via postCreateCommand — see "
                    f"docs/devcontainers.md for the install snippet."
                )
            self._infos[key] = info
            logger.info(
                "devcontainer ready for %s: container=%s workspace=%s user=%s",
                key, info.container_id[:12], info.remote_workspace_folder,
                info.remote_user or "<image default>",
            )
            return info

    @staticmethod
    def _parse_up_output(stdout: str) -> dict:
        """The up result is the last JSON object on stdout carrying `outcome`.

        --log-format json interleaves log records on the same stream, so scan
        from the end for the result record instead of assuming the last line.
        """
        for line in reversed(stdout.strip().splitlines()):
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if isinstance(obj, dict) and "outcome" in obj:
                return obj
        return {}

    async def _alive(self, container_id: str) -> bool:
        proc = await asyncio.create_subprocess_exec(
            "docker", "inspect", "--format", "{{.State.Running}}", container_id,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            out_b, _ = await asyncio.wait_for(
                proc.communicate(), timeout=_EXEC_PROBE_TIMEOUT_SECS
            )
        except asyncio.TimeoutError:
            proc.kill()
            return False
        return proc.returncode == 0 and out_b.decode().strip() == "true"

    # ── exec plumbing ────────────────────────────────────────────────────

    def exec_argv(
        self,
        info: DevcontainerInfo,
        inner_argv: list[str],
        *,
        env: dict[str, str],
        exec_id: str,
        workdir: str | None = None,
    ) -> list[str]:
        """Wrap ``inner_argv`` in a ``docker exec`` into the container.

        The inner command runs under ``setsid`` when available so the whole
        in-container tree is one process group that kill_exec() can signal;
        its pid is recorded in a pidfile named by ``exec_id``. Env vars are
        forwarded explicitly with -e (docker exec does not inherit).
        """
        argv = ["docker", "exec", "-i"]
        if info.remote_user:
            argv += ["-u", info.remote_user]
        argv += ["-w", workdir or info.remote_workspace_folder]
        fwd = dict(env)
        fwd[DEVCONTAINER_EXEC_ENV] = exec_id
        for k, v in fwd.items():
            argv += ["-e", f"{k}={v}"]
        argv.append(info.container_id)
        pidfile = f"{_EXEC_PIDFILE_DIR}/{exec_id}.pid"
        # sh -c preamble: record the pid, prefer setsid for group kill, exec
        # so the recorded pid IS the target (no wrapper shell left behind).
        script = (
            f'mkdir -p {_EXEC_PIDFILE_DIR} && echo $$ > {pidfile}; '
            f'if command -v setsid >/dev/null 2>&1; then exec setsid "$@"; '
            f'else exec "$@"; fi'
        )
        argv += ["sh", "-c", script, "sh", *inner_argv]
        return argv

    async def kill_exec(self, info: DevcontainerInfo, exec_id: str) -> None:
        """Terminate an exec'd process tree inside the container.

        Killing the host-side ``docker exec`` client only detaches; the
        in-container process keeps running. Target discovery order:

        1. AUTHORITATIVE: scan /proc/<pid>/environ for the exec marker.
           The environ block is fixed at exec time — the agent process
           cannot rewrite its own marker — so this cannot be spoofed or
           suppressed from inside (M1 review finding: the pidfile CAN).
        2. Fallback: the pidfile written by exec_argv's preamble, accepted
           only when strictly numeric, not PID 1, and no leading zero —
           a tampered value like ``1`` would otherwise turn the group kill
           into ``kill -1`` (signal-everything).

        exec_id is a uuid4 hex generated by the gateway (never
        caller-supplied), so embedding it in the script is injection-safe.
        """
        pidfile = f"{_EXEC_PIDFILE_DIR}/{exec_id}.pid"
        script = (
            f'PIDS=""; '
            f'for E in /proc/[0-9]*/environ; do '
            f'  if tr "\\0" "\\n" < "$E" 2>/dev/null | '
            f'     grep -qx "{DEVCONTAINER_EXEC_ENV}={exec_id}"; then '
            f'    PIDS="$PIDS ${{E#/proc/}}"; '
            f'  fi; '
            f'done; '
            f'PIDS=$(echo "$PIDS" | sed "s|/environ||g"); '
            f'if [ -z "$PIDS" ]; then '
            f'  P=$(cat {pidfile} 2>/dev/null); '
            f'  case "$P" in ""|*[!0-9]*|0*|1) exit 0;; esac; '
            f'  PIDS=$P; '
            f'fi; '
            f'for P in $PIDS; do '
            f'  kill -TERM -"$P" 2>/dev/null || kill -TERM "$P" 2>/dev/null; '
            f'done; '
            f'sleep 2; '
            f'for P in $PIDS; do '
            f'  kill -KILL -"$P" 2>/dev/null || kill -KILL "$P" 2>/dev/null; '
            f'done; '
            f'rm -f {pidfile}'
        )
        proc = await asyncio.create_subprocess_exec(
            "docker", "exec", info.container_id, "sh", "-c", script,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            await asyncio.wait_for(proc.wait(), timeout=_EXEC_PROBE_TIMEOUT_SECS)
        except asyncio.TimeoutError:
            proc.kill()

    async def _find_by_label(self, key: str) -> str | None:
        """Locate the project's container by id-label (survives restarts)."""
        proc = await asyncio.create_subprocess_exec(
            "docker", "ps", "-q", "--filter", f"label={self._id_label(key)}",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            out_b, _ = await asyncio.wait_for(
                proc.communicate(), timeout=_EXEC_PROBE_TIMEOUT_SECS
            )
        except asyncio.TimeoutError:
            proc.kill()
            return None
        cid = out_b.decode().strip().splitlines()
        return cid[0] if cid else None

    async def status(self, project_dir: str | Path) -> dict:
        """Dashboard-facing status for one project directory.

        ``enabled`` reflects the agent.devcontainer config mode: the frontend
        must not show the trust prompt for a feature that will not run (M4
        review finding — a no-effect security prompt trains bad clicks).
        Container lookup falls back to the id-label so a live container is
        still reported after a gateway restart (M5).
        """
        from kiro_crew.config.loader import KiroCrewConfig

        key = os.path.realpath(str(project_dir))
        cfg = await asyncio.to_thread(find_devcontainer_config, key)
        enabled = False
        try:
            enabled = getattr(KiroCrewConfig.load().agent, "devcontainer", "off") == "auto"
        except Exception:
            pass
        # is_trusted() walks + hashes the tree — off-loop (this endpoint is
        # polled by the dashboard).
        trusted = bool(cfg) and await asyncio.to_thread(is_trusted, key)
        out: dict = {
            "project_dir": key,
            "enabled": enabled,
            "has_config": cfg is not None,
            "config_path": str(cfg) if cfg else None,
            "trusted": trusted,
            "container_id": None,
            "running": False,
            "remote_workspace_folder": None,
        }
        info = self._infos.get(key)
        if info:
            out["container_id"] = info.container_id
            out["running"] = await self._alive(info.container_id)
            out["remote_workspace_folder"] = info.remote_workspace_folder
        elif cfg is not None:
            cid = await self._find_by_label(key)
            if cid:
                out["container_id"] = cid
                out["running"] = True
        return out

    async def down(self, project_dir: str | Path) -> bool:
        """Stop and remove the project's container. Returns True if removed.

        Resolves by id-label when the in-memory cache is cold (gateway
        restarted since up()), so a container never becomes unreapable (M5).
        """
        key = os.path.realpath(str(project_dir))
        info = self._infos.pop(key, None)
        container_id = info.container_id if info else await self._find_by_label(key)
        if not container_id:
            return False
        proc = await asyncio.create_subprocess_exec(
            "docker", "rm", "-f", container_id,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            await asyncio.wait_for(proc.wait(), timeout=60)
        except asyncio.TimeoutError:
            proc.kill()
            return False
        return proc.returncode == 0


# Module singleton, mirroring other gateway-wide managers.
_manager: DevcontainerManager | None = None


def get_manager() -> DevcontainerManager:
    global _manager
    if _manager is None:
        _manager = DevcontainerManager()
    return _manager
