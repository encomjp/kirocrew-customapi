# Dev Containers Module

## Overview

`devcontainer.py` runs a session's `kiro-cli` inside the project's Dev Container,
built by the reference `@devcontainers/cli`. This is VS Code parity: the repo's
`devcontainer.json` is honored in full (image/build, features, lifecycle hooks,
mounts, `runArgs`) after a per-config human trust grant. The gateway does **not**
strip or override security-posture properties — parity, not a sandbox.

The user guide is [docs/devcontainers.md](../../devcontainers.md).

## Architecture

```
gateway (host)                          container (project toolchain)
  KiroCrewClient._spawn
    _maybe_devcontainer_info() ─────┐
      config off?           → None  │  trust-gated
      non-Linux?            → None  │  devcontainer up
      no config in workdir? → None  ├──────────────────────►  kiro-cli acp
      docker missing?       → None  │   docker exec -i        (shell + file
      untrusted?            → None  │   -u remoteUser          tools run here)
      up() failed?          → None  │   -w remoteWorkspace
                                    │   -e KIROCREW_*
    exec_argv(...)  ────────────────┘
    session/new cwd = _acp_cwd() = info.remote_workspace_folder
```

The agent process must move, not just its tool calls: `kiro-cli` 2.14 executes
shell and file tools in-process and ignores the ACP client's `fs`/`terminal`
capabilities, so there is no interception seam. Verified by spike, not assumed.

## Key Invariants

1. **Fail to host, never fail the spawn.** `_maybe_devcontainer_info()` returns
   `None` for every negative case (feature off, non-Linux, no config, docker
   missing, untrusted, `up()` raised). Spawn must not block on a human decision;
   the trust prompt is surfaced out of band. Untrusted and failed cases log
   loudly.
2. **No trust, no build.** `DevcontainerManager.up()` raises
   `DevcontainerNotTrusted` before executing anything when the current config
   bytes have no grant. `is_trusted()` is also checked in `_maybe_devcontainer_info`
   before `up()`; `up()` re-checks to close the edit race between the two.
3. **Trust binds to content, not path.** A grant is valid only while the config's
   SHA-256 matches the recorded digest, so any edit forces a fresh decision.
4. **Container-side cwd over ACP.** `_acp_cwd()` returns
   `info.remote_workspace_folder` for a containerized client and the host work dir
   otherwise. The gateway keeps host paths; the agent gets container paths; the
   bind mount makes them the same bytes.
5. **Mutually exclusive with host isolation wrappers.** The devcontainer branch
   sets `_sandbox_cleanup = None` and skips both `wrap_argv` and
   `cgroup_scope_argv`. Host mechanisms cannot cross the container boundary.
6. **All state is derivable.** The container is re-found after a gateway restart
   by its id-label, so nothing in this module needs persistence except the trust
   store.
7. **In-container kill is explicit.** Killing the host-side `docker exec` client
   only detaches. `kill_exec()` signals the recorded pid through a pidfile.

## Trust store

`config_dir()/devcontainers/trust.json`, written atomically via a `.tmp` file plus
`os.replace`. Shape:

```json
{
  "/realpath/of/project": {
    "digest": "<sha256 of config bytes>",
    "config_path": "/realpath/of/project/.devcontainer/devcontainer.json",
    "granted_at": 1780000000.0
  }
}
```

Config lookup order is `.devcontainer/devcontainer.json`, then
`.devcontainer.json`. `config_digest()` hashes raw bytes; jsonc comments are
stripped only for the preview's `name`/`image` extraction, never for hashing.

## Container identity and lifecycle

| Concern | Mechanism |
|---|---|
| Identity | `--id-label kirocrew.devcontainer=<sha256(realpath)[:24]>`. Path-charset-safe and short. |
| Reuse | One container per project realpath, shared by all sessions on that directory and across gateway restarts (`devcontainer up` is idempotent for an unchanged config). |
| Serialization | Per-project `asyncio.Lock` around `up()`. Image builds are not concurrent-safe on one config. |
| Cache validation | An in-memory `DevcontainerInfo` is reused only when its digest matches and `docker inspect .State.Running` is `true`. |
| Rebuild | `rebuild=True`, or a digest change against a cached info, appends `--remove-existing-container`. |
| Timeout | `_UP_TIMEOUT_SECS` = 900. `_EXEC_PROBE_TIMEOUT_SECS` = 20 for inspect/kill probes. |
| Teardown | `down()` does `docker rm -f` and drops the cache entry. |

`up()` runs the CLI with `--log-format json`, which interleaves log records with
the result on stdout. `_parse_up_output()` therefore scans **from the end** for
the last JSON object carrying `outcome`; it does not assume the last line. A
non-zero exit or an `outcome` other than `success` raises `DevcontainerError`
carrying the CLI message or the stderr tail.

## Exec plumbing

`exec_argv()` builds `docker exec -i [-u remoteUser] -w <workdir> -e K=V ... <cid>
sh -c <preamble> sh <inner argv>`. The preamble records `$$` to
`/tmp/kirocrew-exec/<exec_id>.pid`, then `exec setsid "$@"` when `setsid` exists
so the whole in-container tree is one process group, falling back to `exec "$@"`.
`exec` matters: the recorded pid IS the target, with no wrapper shell left behind.

`docker exec` does not inherit the parent environment, so the client forwards
`KIROCREW_SESSION_KEY`, `KIROCREW_CHANNEL_ID`, and the spawned-process marker
explicitly. `DEVCONTAINER_EXEC_ENV` (`KIROCREW_DEVCONTAINER_EXEC`) carries the
exec id inward for kill-file naming and diagnostics. The inner argv is
`kiro-cli acp --agent <name>` unqualified: the host-resolved binary path is
meaningless inside the image.

`kill_exec()` reads the pidfile and issues `kill -TERM -$P` (group) with a
single-pid fallback, sleeps 2s, escalates to `KILL`, and removes the pidfile. It
runs before the normal host-side teardown, which still reaps the `docker exec`
client itself.

## Config

| Key | Values | Default |
|---|---|---|
| `agent.devcontainer` | `auto`, `off` | `off` |

Read per spawn via `KiroCrewConfig.load()`, so the live-reload fingerprint cache
applies and no restart is needed.

## Dashboard API

Registered in `dashboard/server.py`. `project` is accepted only when its realpath
matches an existing chat slot's project directory (the same barrier idea as
`worktree.py`'s `_allowed_repo_roots`), so a caller cannot probe or trust paths no
session is scoped to. Unknown project ⇒ 400.

| Route | Guard | Notes |
|---|---|---|
| `GET /api/devcontainer/status` | project barrier | Config presence, trust, container id, running, remote workspace folder. |
| `GET /api/devcontainer/config` | project barrier | Raw text capped at 64 KiB, digest, `name`, `image`, `trusted`. 404 when no config. |
| `POST /api/devcontainer/trust` | `deny_non_dashboard_caller` + SEL | Grants for current bytes. |
| `DELETE /api/devcontainer/trust` | `deny_non_dashboard_caller` + SEL | Returns `removed`. |
| `POST /api/devcontainer/rebuild` | `deny_non_dashboard_caller` + SEL | 409 on any `DevcontainerError`, including `DevcontainerNotTrusted` — a rebuild must not silently re-grant. |

Trust mutations are dashboard-caller-only because a grant authorizes arbitrary
image pulls and lifecycle-hook execution for that project. That is precisely the
decision VS Code gates behind Workspace Trust, so it may not be made by an agent,
a subagent, or an app.

## Frontend surface

`DevcontainerTrustCard` renders above the composer in `FollowUpCard`'s slot and
styling, because it gates the same thing the composer starts: nothing is built or
run until the user answers. It shows the config path and the first 12 digest
characters, and the raw config is rendered as **text children only** (never
`dangerouslySetInnerHTML`) and collapsed by default — untrusted file content that
the user can read, not must scroll past.

`ChatPage` polls `GET /api/devcontainer/status` for the active slot's project and
shows the card while `has_config && !trusted && !dismissed`. Dismissal is keyed
on `project_dir \0 config_path`, so it does not carry across projects, and it does
not persist trust. `api.devcontainerStatus?.()` and `api.devcontainerTrust?.()`
are called optionally because many test suites mock `../api/client` partially.
`ChatInput` renders a static Dev Container chip (a `<span>`, deliberately outside
the shelf's tab order) while a container is running.

## Known v1 limitations

- Kiro Crew's managed MCP servers (`mcp-core`, `mcp-cron`, `mcp-computer`) are not
  reachable from inside the container: their REST callbacks target the gateway's
  host loopback. `kiro-cli` reports `mcp_server_init_failure` and the session
  continues with the project toolchain fully functional.
- `/proc`-based liveness observes the host-side `docker exec` client proxy. Death
  detection works (pipe close); wedge heuristics degrade.
- Linux hosts only. On macOS, Docker Desktop is a VM; the existing Seatbelt
  sandbox path is unchanged.
- **One runtime, one container.** `AcpRuntime` multiplexes many ACP sessions over
  one kiro-cli process, so it resolves its container from its own `work_dir`.
  `_session_cwd()` maps a session's cwd to `remoteWorkspaceFolder` when it
  realpath-equals that `work_dir` and raises `AcpRuntimeError` otherwise —
  mapping a foreign cwd would hand the agent a path that either does not exist in
  the image or belongs to another project. `cwd_blocks_pool` (session.py) already
  keeps project-scoped sessions off shared runtimes, so the guard enforces that
  invariant rather than assuming it.
- Warm-pool runtimes are spawned with `default_project_dir()` as `work_dir`
  before any project is known, so they containerize only if that directory has a
  trusted config; a session for another project cannot claim one.

## Source Files

| File | Purpose |
|---|---|
| `devcontainer.py` | Trust store, `DevcontainerManager` (up/down/status/alive), `exec_argv`, `kill_exec`, module singleton. |
| `acp/runtime.py` | **The active path.** `_maybe_devcontainer_info()` (eligibility), the spawn branch that replaces argv with `docker exec`, `_session_cwd()` (container path + one-container invariant), in-container kill on teardown. Every non-claude session reaches here via `AcpProvider.start()` -> `_start_kiro_runtime()`. |
| `acp/client.py` | The same branch on the legacy client path, which serves the dormant claude backend (`AcpProvider.__init__`'s client is "never spawned — just used for config storage"). Kept so both spawn paths behave alike. |
| `dashboard/handlers/devcontainer.py` | The five endpoints, the slot-project barrier, SEL audit. |
| `dashboard/server.py` | Route registration. |
| `config/loader.py` | The `agent.devcontainer` field. |
| `website/src/components/DevcontainerTrustCard.tsx` | The Workspace Trust prompt. |
| `website/src/pages/ChatPage.tsx` | Status query, card gating, dismissal keying. |
| `website/src/components/ChatInput.tsx` | The Dev Container status chip. |
| `test/test_devcontainer.py` | Config lookup order, digest binding and invalidation, trust-store atomicity, preview capping, `exec_argv` shape, `up` output parsing, the `up` trust gate, and the project-resolution barrier. |
