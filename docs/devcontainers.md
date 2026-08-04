# Dev Containers

Run a session's agent inside the project's own Dev Container, so it builds and
tests against the project's toolchain instead of whatever the gateway host
happens to have installed.

This is **VS Code parity**, not a sandbox. The repo's `devcontainer.json` is
honored in full — image or build, features, lifecycle hooks, mounts, `runArgs` —
after a one-time human trust grant, exactly as VS Code's Workspace Trust works.
The gateway does not strip or override security-relevant properties. A container
whose config asks for `privileged` or a host mount gets them once a human has
approved that config.

## What it does

When the feature is on and a session's project directory carries a Dev Container
config that has been trusted, the ACP spawn path replaces the host `kiro-cli`
argv with a `docker exec` into a container built by the reference
[`@devcontainers/cli`](https://github.com/devcontainers/cli) — the same engine
VS Code uses.

The split mirrors VS Code's client/server model:

| Plane | Where it runs |
|---|---|
| Gateway, dashboard, memory, sessions, cron | Host |
| `kiro-cli` and every tool it executes (shell, file edits, builds, tests) | Inside the container |

The agent process itself has to move: `kiro-cli` executes shell and file tools
in-process and ignores the ACP client's `fs`/`terminal` capabilities, so there is
no way to keep the process on the host and route only its tool calls inward.

The workspace is bind-mounted by the devcontainer CLI. The gateway keeps using
the host path, while the `session/new` cwd sent over ACP is the **container-side**
workspace folder (usually `/workspaces/<name>`) so the agent's file tools resolve
against the same bytes through the bind mount.

## Requirements

| Requirement | Detail |
|---|---|
| Linux host | On macOS, Docker Desktop is a VM and the parity path is not used; the existing Seatbelt sandbox path stays in effect. |
| Docker | `docker` must be on the gateway's `PATH`. If it is missing, the session runs on the host and a warning is logged. |
| devcontainer CLI | A real `devcontainer` binary is preferred: `npm i -g @devcontainers/cli`. Without one, `npx --yes @devcontainers/cli` is used, which downloads on first use — install it globally for deterministic session startup. |
| `kiro-cli` inside the container | The inner command is resolved against the **container's** `PATH`, not the host's. `kiro-cli` must be in the image, installed by a devcontainer feature, or installed by a lifecycle hook such as `postCreateCommand`. |
| glibc >= 2.34 in the image | `kiro-cli` is dynamically linked against glibc 2.34 or newer. Debian bookworm (2.36) and Ubuntu 22.04 (2.35) satisfy this; Debian bullseye (2.31) and Alpine (musl) do not. |
| A signed-in `kiro-cli` inside the container | Host credentials are not forwarded. Only `KIROCREW_SESSION_KEY`, `KIROCREW_CHANNEL_ID`, and the spawned-process marker are passed with `-e`. Either run `kiro-cli login` inside the container, or mount the host credential directory from your `devcontainer.json` — the latter is your decision to make, and it removes the host/container separation for those credentials. |

## Enabling it

The feature is off by default. Set `agent.devcontainer` to `auto`:

```bash
kirocrew config set agent.devcontainer auto
```

| Value | Behavior |
|---|---|
| `off` (default) | The agent always runs on the host, as before. |
| `auto` | Per session: containerize when the project qualifies, otherwise fall back to the host. |

Config is read live, so no gateway restart is needed.

Under `auto`, a session containerizes only when **all** of these hold. Any miss
means the session runs on the host instead of failing:

1. The host is Linux.
2. The session's work directory contains `.devcontainer/devcontainer.json`, or
   `.devcontainer.json` as a fallback. The first wins when both exist.
3. `docker` is on `PATH`.
4. The current config bytes carry a valid trust grant.
5. `devcontainer up` succeeds.

Cases 3, 4, and 5 log loudly. Falling back on an untrusted config is also what
VS Code does: no trust, no container.

## Trust

A trust grant binds to the **SHA-256 of the config file's bytes**, not to the
path. Any edit — by you, by a `git pull`, or by an agent — changes the digest,
invalidates the grant, and forces a fresh human decision before the next build
or exec. Granting trust authorizes arbitrary image pulls and lifecycle-hook
execution for that project, which is exactly the decision VS Code gates behind
Workspace Trust.

Grants are stored in `~/.kiro/crew/devcontainers/trust.json`, keyed by the
project directory's realpath, recording the digest, the config path, and the
grant time.

### Granting it in the dashboard

When the active chat slot's project carries a Dev Container config that is not
yet trusted, a **Workspace Trust card** appears above the composer. It names the
config file, shows the first 12 characters of its digest, and can expand to show
the raw config text so you can read what you are about to authorize. Trust it,
and the next session spawn for that project builds and uses the container.
Dismiss it, and nothing is granted — the card returns next session.

Because the grant is bound to the digest, an edit to `devcontainer.json` brings
the card back with a new digest rather than inheriting the earlier decision.

While a container is up for the active project, a **Dev Container** chip appears
in the composer shelf; its tooltip carries the short container id. The chip is a
status readout, not a control.

### Endpoints

Three properties keep an agent from trusting its own config:

- Trust mutations are **dashboard-caller-only**. A session, a subagent, or an
  app calling the endpoint is denied.
- The `project` path is accepted only when it realpath-matches an existing chat
  slot's project directory, so an arbitrary caller cannot probe or trust paths
  no session is scoped to.
- Grant and revoke are recorded in the security event log.

| Endpoint | Purpose |
|---|---|
| `GET /api/devcontainer/status?project=<path>` | Config presence, trust state, container id, running state, container workspace folder. |
| `GET /api/devcontainer/config?project=<path>` | Raw config text (capped at 64 KiB), its digest, and the parsed `name`/`image`, for review before granting. |
| `POST /api/devcontainer/trust` | Body `{"project": "<path>"}`. Grants trust for the config's **current** bytes. |
| `DELETE /api/devcontainer/trust` | Body `{"project": "<path>"}`. Revokes. |
| `POST /api/devcontainer/rebuild` | Body `{"project": "<path>"}`. Trust-gated rebuild; a rebuild of an untrusted config fails rather than silently re-granting. |

`devcontainer.json` may contain `//` comments. The preview strips them only to
extract `name` and `image`; the devcontainer CLI does the real jsonc parse, and
the digest always covers the raw bytes.

## Container lifecycle

One container per project directory, reused by every session scoped to that
directory and across gateway restarts. Identity is an id-label derived from the
project realpath, so `devcontainer up` finds the existing container again instead
of building a second one; nothing about the container needs to be persisted by
the gateway.

- `up` calls for the same project are serialized. Two sessions starting at once
  on one config do not race the image build.
- A cached container is reused only while its recorded config digest still
  matches and the container is actually running. A stale entry is dropped and
  rebuilt.
- A digest change, or an explicit rebuild, removes the existing container first.
- `devcontainer up` is allowed 15 minutes. Image builds and feature installs are
  slow the first time; later starts hit the cache.

Inside the container each agent is launched under `docker exec -i`, as the
config's `remoteUser` when one is set, with the container workspace folder as
cwd. The inner process is started under `setsid` when available and records its
pid to `/tmp/kirocrew-exec/<exec-id>.pid`, because killing the host-side
`docker exec` client only detaches — teardown signals the in-container process
group through that pidfile, escalating `TERM` to `KILL`.

Host-side sandbox and cgroup wrappers are **not** applied to a containerized
session. Those mechanisms cannot cross the container boundary; the container's
own namespaces and any limits in `runArgs` take their place.

## Known v1 limitations

- Kiro Crew's own managed MCP servers (`mcp-core`, `mcp-cron`, `mcp-computer`)
  are not reachable from inside the container, because their REST callback
  targets the gateway's host loopback. `kiro-cli` reports
  `mcp_server_init_failure` and the session continues with the project toolchain
  fully functional. Cron, subagent spawning, learning, and the other MCP-backed
  capabilities are unavailable to a containerized session; foreign MCP servers
  declared inside the container still work.
- `/proc`-based liveness observes the host-side `docker exec` client proxy.
  Death detection still works, because the pipe closes; the wedge heuristics
  degrade.
- Linux hosts only. On macOS, Docker Desktop is a VM; the existing Seatbelt
  sandbox path is unchanged.
- **One runtime hosts one container.** A kiro-cli runtime can host several ACP
  sessions (session sharing) but is containerized for exactly one project, so a
  session whose cwd is not that runtime's working directory is refused and must
  cold-start its own runtime. In normal operation this does not fire: a
  project-scoped session cannot claim a pooled runtime, so it already gets a
  runtime whose working directory is its own project.
- Warm-pool runtimes follow the same rule as any other. They are pre-spawned
  with the default workspace directory as their working directory, before any
  project is known — so they are containerized only if *that* directory carries
  a trusted config, and a session for a different project cannot claim one. Set
  `session.pool_size` to `0` if you want every session to resolve its container
  at start.

## Example `devcontainer.json`

A Python + Node image on bookworm (glibc 2.36) that installs `kiro-cli` in a
lifecycle hook:

```jsonc
{
  "name": "my-project",
  "image": "mcr.microsoft.com/devcontainers/python:3.12-bookworm",
  "features": {
    "ghcr.io/devcontainers/features/node:1": { "version": "20" }
  },
  // Runs once, after the container is created. Put the install here rather than
  // postStartCommand so it is not repeated on every reuse.
  "postCreateCommand": "bash .devcontainer/install-kiro-cli.sh",
  "remoteUser": "vscode",
  "containerEnv": {
    "PATH": "${containerEnv:HOME}/.local/bin:${containerEnv:PATH}"
  }
}
```

`.devcontainer/install-kiro-cli.sh`, using the installer command from the
[Kiro CLI docs](https://kiro.dev/docs/cli/):

```bash
#!/usr/bin/env bash
set -euo pipefail

# Install kiro-cli into a PATH directory the remoteUser owns. Substitute the
# installer invocation published in the Kiro CLI docs for your platform.
mkdir -p "$HOME/.local/bin"
# <installer command from https://kiro.dev/docs/cli/>

kiro-cli --version   # fail the build now rather than at first session spawn
```

Baking `kiro-cli` into a prebuilt image, or adding it as a devcontainer feature,
is preferable for a team: `postCreateCommand` runs on every fresh container and
adds that time to the first session start after a rebuild.

After adding or editing the config, review and trust it before the next session
spawns — an edit invalidates any earlier grant.

## Troubleshooting

| Symptom | Cause |
|---|---|
| Session runs on the host with no error | One of the five `auto` preconditions failed. Check the gateway log for the untrusted-config, docker-missing, or `devcontainer up failed` warning. |
| `devcontainer CLI not found` | Neither `devcontainer` nor `npx` is on the gateway's `PATH`. Install with `npm i -g @devcontainers/cli`. |
| `kiro-cli not found` inside the container | The image or its lifecycle hooks do not provide `kiro-cli` on the container's `PATH`, or it is installed somewhere `remoteUser`'s `PATH` does not cover. |
| `kiro-cli` starts but is not logged in | Host credentials are not forwarded. Sign in inside the container, or mount the credential directory from your config. |
| Trust prompt returns after a `git pull` | Expected. The pull changed the config bytes and therefore the digest. |
| MCP tool calls fail in a containerized session | Expected in v1. See the limitations above. |
| `devcontainer up timed out` | The build exceeded 15 minutes. Prebuild the image, or move heavy work out of `postCreateCommand`. |

## Related

- [Config schema](system-specs/modules/config.md) — where `agent.devcontainer` lives.
- [Module spec](system-specs/modules/devcontainers.md) — the technical contract.
- [ACP client](system-specs/modules/acp-client.md) — the spawn path this hooks into.
- [Security](system-specs/modules/security.md) — the host sandbox that a containerized session replaces.
