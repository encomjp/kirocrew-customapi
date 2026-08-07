# Fork-First Updates (kirocrew-customapi)

Date: 2026-08-07 · Branch: `testing` · Status: approved (design B)

## Goal

The kirocrew-customapi fork must update from **its own repo** (`encomjp/kirocrew-customapi`),
never from the upstream `kirodotdev/KiroCrew` — for both update surfaces:

1. the desktop app (Electron / electron-updater), and
2. the Python gateway running from a git checkout.

## Decisions

- **Both surfaces** are hooked to the fork (user request).
- **Desktop: native GitHub provider (option B)** — single `stable` lane. No
  nightly/insider channels for the fork; the Settings channel switcher is
  disabled.
- **Gateway: tracked upstream + `main` guard** — auto-apply follows whatever
  remote the current branch tracks (user sets `main` → `fork/main`), and only
  runs on branch `main` (was `mainline`).
- **Out of scope:** the wheel/CLI CDN feed (`_check_release_feed`,
  `_wheel_update_command`). The fork has no CDN; the installer owns that path.
  Revisit if the fork ever publishes wheels.

## Desktop changes

### `website/electron/package.json`
- `build.publish` → `{ "provider": "github", "owner": "encomjp", "repo": "kirocrew-customapi" }`
  (replaces the generic provider pointed at `https://updates.crew.kiro.dev/feed/stable/`).

### `website/electron/auto-update.js`
- `configureFeed()`: default is the GitHub provider (owner/repo injected as
  deps, like `feedBase` was, so tests stay runtime-free). The
  `KIROCREW_UPDATE_FEED` env override is KEPT and switches to the generic
  provider — the E2E OTA harness (`stage3-autoupdate-test.sh` +
  `local-feed-server.js`) drives the updater through exactly that env var, and
  must keep working unchanged.
- Delete `DEFAULT_FEED_BASE`, `KNOWN_CHANNELS`, `channelForFlavor`,
  `channelForVersion`, `resolveChannel`. Keep `buildFeedBase` for the
  env-override path (explicit `base` required; drop the DEFAULT_FEED_BASE
  default).
- Channel resolution collapses to a single `stable` lane: `currentChannel()`
  returns `"stable"`; the resolved channel is display-only.
- `getInfo()` returns `channelSwitchable: false` → the About panel already hides
  the channel switcher (`AboutPanel.tsx:615` renders it only when
  `channelSwitchable && setChannel`).
- `manualDownloadUrl` → `https://github.com/encomjp/kirocrew-customapi/releases/latest/download/`
  + `KiroCrew-<ver>.AppImage` (Linux) / `KiroCrew-<ver>-arm64.dmg` (macOS).
- `SUPPORTED_PLATFORMS` (darwin/linux) unchanged.

### `.github/workflows/build-desktop-fork.yml`
- Add `website/electron/dist/latest-*.yml` to the upload-artifact paths and to
  the `attach-release` step, so the update metadata lands on every release
  alongside the artifacts. Keep the existing rename/attach structure (smaller
  diff than reworking to `electron-builder --publish`; the GitHub provider
  resolves files via release assets anyway).

### Tests (electron)
- Rewrite `website/electron/test/auto-update.test.js` to pin the GitHub
  provider (default), the env-override generic path, the stable-only channel,
  and the GitHub manual-download URLs. `update-ipc-registration.test.js`
  asserts source order only — no changes needed.

## Gateway changes

### `src/kiro_crew/slack/gateway.py` — `_auto_apply_update()`
- Resolve the update remote from the branch's tracked remote
  (`branch.<name>.remote`, fallback `origin`) — same resolution
  `resolve_remote_url` uses — instead of hardcoded `origin`.
- Branch guard: `main` (was `mainline`). Detached HEAD still coerces to `main`
  (same behavior as today, renamed).
- `git fetch <remote> <branch>` + `git reset --hard <remote>/<branch>`.
- The dashboard check (`_check_git_checkout`) and the dashboard button (bare
  `git pull`) already follow the tracked upstream — **no code change needed**
  once `main` tracks `fork/main`.

### Tests (gateway)
- Update `test/test_slack_gateway.py`: `test_non_mainline_branch_skips` and the
  mocked branch/remote fixtures that pin `mainline`/`origin`.

### Docs
- README section: "Updating from the fork" —
  `git branch --set-upstream-to=fork/main main`, and the note that auto-update
  runs only on `main`.

## Version bump rule

Every fork release must bump `__version__` in `src/kiro_crew/__init__.py`
(the release workflow already stamps it). `-customapi.N` stamps compare
correctly under the existing PEP 440 logic.

## Error handling (unchanged behavior)

- Desktop: GitHub API unreachable → existing `offline` code; no release →
  `no-release`. `autoDownload=false` consent flow unchanged.
- Gateway: existing honest `checked` contract — a check that cannot run reports
  an error code, never a false "up to date". Unset upstream → `git_read_failed`.

## Out of scope (explicitly)

- Wheel/CLI CDN feed (`_check_release_feed`, `_wheel_update_command`).
- Removing the dormant channel machinery upstream may keep for non-fork builds —
  not our concern here.
- Windows desktop lane (already unsupported by `SUPPORTED_PLATFORMS`).
