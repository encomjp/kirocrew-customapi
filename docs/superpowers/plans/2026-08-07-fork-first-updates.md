# Fork-First Updates Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make both update surfaces of the kirocrew-customapi fork (Electron desktop updater + Python gateway git auto-update) track the fork's own repo (`encomjp/kirocrew-customapi`) instead of upstream `kirodotdev/KiroCrew`.

**Architecture:** Desktop uses electron-updater with a single stable lane pointed at the fork's GitHub Releases (native GitHub provider; `KIROCREW_UPDATE_FEED` env override keeps the generic provider for the E2E OTA harness). The gateway's boot auto-apply resolves the update remote from the branch's tracked remote (fallback `origin`) and guards on branch `main`; the dashboard check/button already follow the tracked upstream and need no change once `main` tracks `fork/main`.

**Tech Stack:** Node 22 / electron-updater (generic + GitHub providers), electron-builder, Python 3.12 / asyncio / pytest, GitHub Actions.

## Global Constraints

- Single `stable` update lane for the fork: `currentChannel()` returns `"stable"`; `getInfo().channelSwitchable` is `false` (hides the Settings channel switcher — `AboutPanel.tsx:615`).
- `KIROCREW_UPDATE_FEED` env override MUST keep working with the generic provider (E2E harness `scripts/stage3-autoupdate-test.sh` + `scripts/local-feed-server.js` depend on it).
- Fork identity: owner `encomjp`, repo `kirocrew-customapi`. GitHub Releases are non-prerelease, assets named `KiroCrew-<ver>.AppImage` (Linux) / `KiroCrew-<ver>-arm64.dmg` (macOS).
- Gateway auto-apply runs ONLY on branch `main` (never on `testing`/feature branches). Detached HEAD coerces to `main` (behavior preserved from `mainline`).
- Governance: `resolve_remote_url` is called with the SAME remote name that is fetched/reset (pin must validate the actual source).
- Desktop supported platforms stay darwin + linux (`SUPPORTED_PLATFORMS`).
- No git commits unless the user explicitly asks (session rule). Each task's commit step is therefore OPTIONAL — confirm with the user once before the first one.
- Verification commands: `cd website/electron && npm test` (node:test) and `pytest test/test_slack_gateway.py -q` from the repo root.

---

### Task 1: Electron — GitHub provider + single stable lane

**Files:**
- Modify: `website/electron/auto-update.js` (deps destructure ~line 383, `currentChannel` ~395, `getInfo` ~407, `configureFeed` ~521, module header ~36-85, exports ~853)
- Modify: `website/electron/test/auto-update.test.js` (imports ~1-13, channel-helper tests ~15-68, DEFAULT_FEED_BASE test ~90-95, `makeDeps` ~252, channel-wiring tests ~745-796)

**Interfaces:**
- Consumes: `initAutoUpdate(deps)` where `deps` gains optional `githubOwner`/`githubRepo`; `process.env.KIROCREW_UPDATE_FEED` read at init.
- Produces: `configureFeed()` calls `autoUpdater.setFeedURL({ provider: "github", owner, repo })` by default, `{ provider: "generic", url }` under the env override. Exports drop `channelForFlavor`, `channelForVersion`, `resolveChannel`, `DEFAULT_FEED_BASE`; keep `buildFeedBase` (requires explicit `base`), `manualDownloadUrl`, `configureUpdater`, `classifyError`, `initAutoUpdate`, `DOWNLOAD_BASE`, `SUPPORTED_PLATFORMS`.

- [ ] **Step 1: Replace the channel tests with fork-lane tests**

In `website/electron/test/auto-update.test.js`:

a) Change the import block (lines ~1-12) to:

```js
const {
  initAutoUpdate,
  buildFeedBase,
  configureUpdater,
  SUPPORTED_PLATFORMS,
} = require("../auto-update");
```

b) Delete the test blocks for `channelForVersion`/`channelForFlavor`/`resolveChannel` (lines ~15-68) and the `buildFeedBase defaults to the public pointer host (DEFAULT_FEED_BASE)` test (lines ~90-95). Keep the `buildFeedBase` trailing-slash, strip, url-encode, http-throw, and loopback-allow tests.

c) In `makeDeps` delete the line `feedBase: "https://cdn.example.dev/feed",` (line ~252).

d) Replace the three channel-wiring tests (lines ~751-796) with:

```js
test("default feed uses the fork's GitHub provider (single stable lane)", async () => {
  const { deps, calls } = makeDeps({ appVersion: "0.2.0-customapi.1" });
  const u = initAutoUpdate(deps);
  await u.check();
  assert.ok(calls.setFeedURL.length >= 1);
  for (const o of calls.setFeedURL) {
    assert.strictEqual(o.provider, "github");
    assert.strictEqual(o.owner, "encomjp");
    assert.strictEqual(o.repo, "kirocrew-customapi");
  }
});

test("KIROCREW_UPDATE_FEED override keeps the generic provider (E2E harness)", async () => {
  process.env.KIROCREW_UPDATE_FEED = "http://127.0.0.1:8799/feed";
  try {
    const { deps, calls } = makeDeps({ appVersion: "1.0.0" });
    const u = initAutoUpdate(deps);
    await u.check();
    assert.ok(calls.setFeedURL.length >= 1);
    assert.ok(
      calls.setFeedURL.every((o) => o.provider === "generic" && o.url === "http://127.0.0.1:8799/feed/stable/"),
      `expected generic stable feed urls, got: ${JSON.stringify(calls.setFeedURL)}`,
    );
  } finally {
    delete process.env.KIROCREW_UPDATE_FEED;
  }
});

test("getInfo reports the single stable lane (switcher hidden)", () => {
  const { deps } = makeDeps({ appVersion: "0.2.0-customapi.1" });
  const u = initAutoUpdate(deps);
  const info = u.getInfo();
  assert.strictEqual(info.channel, "stable");
  assert.strictEqual(info.stampedChannel, "stable");
  assert.strictEqual(info.channelSwitchable, false);
  assert.strictEqual(info.packaged, true);
});
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd website/electron && npm test`
Expected: the three new tests FAIL (current code uses the generic provider / reports `channelSwitchable: true` for stable stamps).

- [ ] **Step 3: Implement the fork-lane provider in `auto-update.js`**

a) Module header — replace `DEFAULT_FEED_BASE` and `KNOWN_CHANNELS` (lines ~65, ~82):

```js
// The fork's own repo: the single update source. The GitHub provider resolves
// release metadata (latest-mac.yml / latest-linux.yml) from the repo's releases
// over the GitHub API, and bytes from the release assets — no CDN involved.
const GITHUB_OWNER = "encomjp";
const GITHUB_REPO = "kirocrew-customapi";
const DOWNLOAD_BASE = `https://github.com/${GITHUB_OWNER}/${GITHUB_REPO}/releases/latest/download`;
```

b) Delete `channelForFlavor` (lines ~91-94), `channelForVersion` (lines ~107-112) and `resolveChannel` (lines ~133-138) including their doc comments. Keep `KNOWN_CHANNELS` for now — Task 2 removes it together with the `manualDownloadUrl` rewrite, keeping this task green.

c) `buildFeedBase` — require an explicit base (no upstream default):

```js
function buildFeedBase({ base, channel }) {
  if (!base) throw new Error("feed base required");
  const b = String(base).replace(/\/+$/, "");
  // ... rest unchanged (url construction, https/loopback guard) ...
}
```

d) `manualDownloadUrl` stays untouched in this task — it still reads `KNOWN_CHANNELS`, which is kept until Task 2. Task 2 rewrites it and removes `KNOWN_CHANNELS` together.

e) Deps destructure (line ~383): replace

```js
    feedBase = process.env.KIROCREW_UPDATE_FEED || DEFAULT_FEED_BASE,
```

with

```js
    githubOwner = GITHUB_OWNER,
    githubRepo = GITHUB_REPO,
    feedOverride = process.env.KIROCREW_UPDATE_FEED || "",
```

and remove `getFlavor` from the destructure (it is now unused).

f) `currentChannel()` (lines ~395-398) becomes:

```js
  function currentChannel() {
    // Single stable lane for the fork: version stamps (-customapi.N,
    // -9router.N, bare semver) all resolve to stable, display-only.
    return "stable";
  }
```

g) `getInfo()` (lines ~407-422) becomes:

```js
  function getInfo() {
    return {
      version: app.getVersion(),
      channel: currentChannel(),
      // Switcher inputs: the fork has exactly one lane, so the About panel
      // hides the channel switcher (channelSwitchable=false).
      stampedChannel: "stable",
      channelSwitchable: false,
      channelPreference: getChannelPreference() || "",
      platform,
      packaged: !!app.isPackaged,
      // Escape hatch for a failed install (see manualDownloadUrl).
      downloadUrl: manualDownloadUrl(pendingVersion(), osPlatform),
    };
  }
```

h) `configureFeed()` (lines ~521-527) becomes:

```js
  function configureFeed() {
    if (feedOverride) {
      // E2E OTA harness (scripts/stage3-autoupdate-test.sh + local-feed-server.js):
      // the generic provider reads latest-*.yml from the local server. Production
      // never sets this env var.
      const url = buildFeedBase({ base: feedOverride, channel: "stable" });
      autoUpdater.setFeedURL({ provider: "generic", url });
      log.info(`[update] feed (override): ${url}`);
      return url;
    }
    autoUpdater.setFeedURL({ provider: "github", owner: githubOwner, repo: githubRepo });
    log.info(`[update] feed: github ${githubOwner}/${githubRepo}`);
    return `${DOWNLOAD_BASE}/`;
  }
```

i) Exports (lines ~853-865): remove `channelForFlavor`, `channelForVersion`, `resolveChannel`, `DEFAULT_FEED_BASE`. Keep everything else.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd website/electron && npm test`
Expected: all tests pass (including the three new ones and the retained buildFeedBase/loopback tests).

- [ ] **Step 5: Commit (ask user first)**

```bash
git add website/electron/auto-update.js website/electron/test/auto-update.test.js
git commit -m "feat(update): point desktop updater at the fork's GitHub releases (single stable lane)"
```

---

### Task 2: Electron — manual download URL → fork GitHub releases

**Files:**
- Modify: `website/electron/auto-update.js` (`manualDownloadUrl` ~191-204, remove `KNOWN_CHANNELS` ~82)
- Modify: `website/electron/test/auto-update.test.js` (add manualDownloadUrl tests)

**Interfaces:**
- Produces: `manualDownloadUrl(version, osPlatform) -> string|null` — `https://github.com/encomjp/kirocrew-customapi/releases/latest/download/KiroCrew-<version>.AppImage` (linux), `.../KiroCrew-<version>-arm64.dmg` (darwin), `null` otherwise.

- [ ] **Step 1: Write the failing tests**

Add `manualDownloadUrl` to the test file's import block (lines ~1-12):

```js
const {
  initAutoUpdate,
  buildFeedBase,
  configureUpdater,
  manualDownloadUrl,
  SUPPORTED_PLATFORMS,
} = require("../auto-update");
```

Append to `website/electron/test/auto-update.test.js`:

```js
test("manualDownloadUrl: linux points at the fork's latest AppImage release", () => {
  assert.strictEqual(
    manualDownloadUrl("0.2.0", "linux"),
    "https://github.com/encomjp/kirocrew-customapi/releases/latest/download/KiroCrew-0.2.0.AppImage",
  );
});

test("manualDownloadUrl: darwin points at the fork's arm64 dmg", () => {
  assert.strictEqual(
    manualDownloadUrl("0.2.0", "darwin"),
    "https://github.com/encomjp/kirocrew-customapi/releases/latest/download/KiroCrew-0.2.0-arm64.dmg",
  );
});

test("manualDownloadUrl: unsupported platform or missing version -> null", () => {
  assert.strictEqual(manualDownloadUrl("0.2.0", "win32"), null);
  assert.strictEqual(manualDownloadUrl("", "linux"), null);
});

test("getInfo.downloadUrl uses the pending version (running version before discovery)", () => {
  const { deps } = makeDeps({ appVersion: "1.0.0", osPlatform: "linux" });
  const u = initAutoUpdate(deps);
  assert.strictEqual(
    u.getInfo().downloadUrl,
    "https://github.com/encomjp/kirocrew-customapi/releases/latest/download/KiroCrew-1.0.0.AppImage",
  );
});
```

- [ ] **Step 2: Run to verify they fail**

Run: `cd website/electron && npm test`
Expected: the four new tests FAIL (old signature is `manualDownloadUrl(channel, osPlatform)`).

- [ ] **Step 3: Implement**

Replace `manualDownloadUrl` (lines ~191-204) with:

```js
/**
 * Human download permalink for a fork version + platform, or null when there is
 * no publish lane (Windows until a signed lane lands).
 *
 * Why the UI needs this: an update that downloads but fails to APPLY leaves the
 * user with no next step — the card simply re-offers the same update after
 * relaunch. Reinstalling over the top is the supported recovery and is
 * non-destructive: user data lives in the KiroCrew home directory, never inside
 * the app bundle.
 *
 * The fork's GitHub release assets embed the version in the filename
 * (KiroCrew-<ver>.AppImage / KiroCrew-<ver>-arm64.dmg), so the caller passes the
 * pending version (found/staged/running — see getInfo).
 *
 * @param {string} version    pending version to download
 * @param {string} osPlatform process.platform value
 * @returns {string|null}
 */
function manualDownloadUrl(version, osPlatform) {
  if (!version) return null;
  const file = osPlatform === "darwin"
    ? `KiroCrew-${version}-arm64.dmg`
    : osPlatform === "linux"
      ? `KiroCrew-${version}.AppImage`
      : null;
  if (!file) return null;
  return `${DOWNLOAD_BASE}/${file}`;
}
```

Then delete the now-unused `KNOWN_CHANNELS` set (line ~82).

- [ ] **Step 4: Run to verify they pass**

Run: `cd website/electron && npm test`
Expected: all tests pass.

- [ ] **Step 5: Commit (ask user first)**

```bash
git add website/electron/auto-update.js website/electron/test/auto-update.test.js
git commit -m "feat(update): manual download link points at the fork's GitHub release assets"
```

---

### Task 3: Electron — publish config to GitHub provider

**Files:**
- Modify: `website/electron/package.json` (`build.publish`)
- Modify: `website/electron/test/auto-update.test.js` (BLOCKING-fix contract test ~883-894)

**Interfaces:**
- Produces: `build.publish` = `[{ "provider": "github", "owner": "encomjp", "repo": "kirocrew-customapi" }]`. electron-builder emits `latest-mac.yml`/`latest-linux.yml` whose artifact URLs resolve against the fork's GitHub releases.

- [ ] **Step 1: Write the failing test**

Replace the `BLOCKING-fix contract` test (lines ~883-894) with:

```js
test("BLOCKING-fix contract: package.json declares the fork's GitHub publish entry so app-update.yml is emitted", () => {
  // electron-updater's downloadUpdate() -> getOrCreateDownloadHelper() awaits
  // configOnDisk -> readFile(app-update.yml). electron-builder only writes that
  // file when a publish config exists. Without it, DISCOVERY works and every
  // consented download throws ENOENT.
  const pkg = require("../package.json");
  const publish = pkg.build && pkg.build.publish;
  assert.ok(Array.isArray(publish) && publish.length > 0, "build.publish must be a non-empty array");
  assert.strictEqual(publish[0].provider, "github");
  assert.strictEqual(publish[0].owner, "encomjp");
  assert.strictEqual(publish[0].repo, "kirocrew-customapi");
});
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd website/electron && npm test`
Expected: the contract test FAILS (`publish[0].provider` is still `"generic"`).

- [ ] **Step 3: Implement**

In `website/electron/package.json`, replace the `publish` array (currently `[{ "provider": "generic", "url": "https://updates.crew.kiro.dev/feed/stable/" }]`) with:

```json
  "publish": [
    {
      "provider": "github",
      "owner": "encomjp",
      "repo": "kirocrew-customapi"
    }
  ],
```

(Keep the surrounding key formatting as it is — the file is pretty-printed with 2-space indent.)

- [ ] **Step 4: Run to verify it passes**

Run: `cd website/electron && npm test`
Expected: all tests pass.

- [ ] **Step 5: Commit (ask user first)**

```bash
git add website/electron/package.json website/electron/test/auto-update.test.js
git commit -m "feat(update): electron-builder publishes update metadata to the fork's GitHub releases"
```

---

### Task 4: Workflow — attach update metadata to fork releases

**Files:**
- Modify: `.github/workflows/build-desktop-fork.yml` (upload-artifact paths ~113-120, attach-release copy step ~139-169)

**Interfaces:**
- Produces: every fork release carries `latest-mac.yml` (from the macOS build job) and `latest-linux.yml` (from the Linux build job) alongside the artifacts. Manual `workflow_dispatch` builds still land them in Actions artifacts.

- [ ] **Step 1: Add the metadata files to the upload path**

In the `build-desktop` job's `Upload desktop artifact` step, extend `path:` (lines ~113-120) with:

```yaml
            website/electron/dist/latest-linux.yml
            website/electron/dist/latest-mac.yml
```

(electron-builder writes these into `dist/` during packaging; the mac runner emits `latest-mac.yml`, the linux runner `latest-linux.yml` — each job uploads only the one its platform produced, and upload-artifact ignores missing globs.)

- [ ] **Step 2: Copy them into the release bundle in `attach-release`**

In the `Rename artifacts with release tag` step, after the `.deb` copy block (around line ~168), add:

```yaml
          if ls artifacts/build-linux-desktop/latest-linux.yml >/dev/null 2>&1; then
            cp artifacts/build-linux-desktop/latest-linux.yml "release/latest-linux.yml"
          fi
          if ls artifacts/build-macos-desktop/latest-mac.yml >/dev/null 2>&1; then
            cp artifacts/build-macos-desktop/latest-mac.yml "release/latest-mac.yml"
          fi
```

(`files: release/*` in the `softprops/action-gh-release` step already attaches everything in `release/`.)

- [ ] **Step 3: Validate the workflow YAML parses**

Run: `cd <repo-root> && python3 -c "import yaml,sys; yaml.safe_load(open('.github/workflows/build-desktop-fork.yml')); print('yaml ok')"`
Expected: prints `yaml ok`.

- [ ] **Step 4: Commit (ask user first)**

```bash
git add .github/workflows/build-desktop-fork.yml
git commit -m "ci(desktop): attach latest-*.yml update metadata to fork releases"
```

---

### Task 5: Gateway — auto-apply follows the tracked remote on branch `main`

**Files:**
- Modify: `src/kiro_crew/slack/gateway.py` (`_auto_apply_update`, lines ~5357-5485)
- Modify: `test/test_slack_gateway.py` (`TestAutoApplyUpdate.test_non_mainline_branch_skips` ~908, `TestAutoApplyUpdateGitPath` ~1983-2044, `TestAutoApplyUpdateVenvPath` ~2685-2740, `TestAutoApplyUpdateResetPath` ~3111-3167)

**Interfaces:**
- Consumes: `update_governance.resolve_remote_url(proj, remote=<name>)`, `update_blocked_reason(url)`.
- Produces: `_auto_apply_update()` — branch guard `main` (detached HEAD coerces to `main`); update remote = `git config branch.<branch>.remote` (fallback `origin`); `git fetch <remote> <branch>`; `git diff HEAD <remote>/<branch> --quiet`; `git reset --hard <remote>/<branch>`.

- [ ] **Step 1: Write the failing test for tracked-remote resolution**

Add to `test/test_slack_gateway.py` in `TestAutoApplyUpdateResetPath` (after `test_reset_then_frontend_then_pip`, ~line 3167):

```python
    @pytest.mark.asyncio
    async def test_fetch_and_reset_use_the_tracked_remote(self):
        """On main, fetch/reset target branch.<name>.remote, not a hardcoded origin."""
        orch = _make_orchestrator()
        ds = _mock_dashboard_state()
        orch.dashboard_state = ds
        orch.sessions = _mock_sessions()
        exec_calls = []

        async def _fake_exec(*args, **kwargs):
            cmd = args[0]
            proc = AsyncMock()
            proc.kill = MagicMock()
            proc.wait = AsyncMock(return_value=0)
            if cmd == "rev-parse":
                proc.communicate = AsyncMock(return_value=(b"main\n", b""))
                proc.returncode = 0
            elif cmd == "config":
                proc.communicate = AsyncMock(return_value=(b"fork\n", b""))
                proc.returncode = 0
            elif cmd == "fetch":
                proc.communicate = AsyncMock(return_value=(b"", b""))
                proc.returncode = 0
            elif cmd == "diff":
                proc.returncode = 0  # --quiet: no changes -> early return
            else:
                proc.communicate = AsyncMock(return_value=(b"", b""))
                proc.returncode = 0
            exec_calls.append(args)
            return proc

        with patch.dict("os.environ", {"KIROCREW_PROJECT_DIR": "/tmp/proj"}):
            with patch("asyncio.create_subprocess_exec", side_effect=_fake_exec):
                await orch._auto_apply_update()

        assert ("git", "fetch", "fork", "main") in exec_calls, exec_calls
        assert ("git", "diff", "HEAD", "fork/main", "--quiet") in exec_calls, exec_calls
        assert not any(c[0] == "reset" for c in exec_calls), "no diff -> no reset"
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd <repo-root> && pytest test/test_slack_gateway.py::TestAutoApplyUpdateResetPath::test_fetch_and_reset_use_the_tracked_remote -q`
Expected: FAIL — current code fetches `origin main` and resets `origin/main`.

- [ ] **Step 3: Implement**

In `src/kiro_crew/slack/gateway.py`, `_auto_apply_update()`:

a) Branch guard (lines ~5388-5394): replace `mainline` with `main`:

```python
            branch = branch_out.strip().decode() if branch_out else ""
            if not branch or branch == "HEAD":
                branch = "main"

            # Only auto-update on main — feature/testing branches need manual update
            if branch != "main":
                logger.debug("Auto-update: skipping — on branch %s, not main", branch)
                return
```

b) After the branch guard, resolve the tracked remote and derive the ref (insert before the governance check ~5396):

```python
            # Resolve the remote this branch tracks (fallback origin) so a fork
            # checkout updates from ITS upstream, not a hardcoded one.
            remote_proc = await asyncio.create_subprocess_exec(
                "git",
                "config",
                "--get",
                f"branch.{branch}.remote",
                cwd=proj,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            remote_out, _ = await asyncio.wait_for(remote_proc.communicate(), timeout=10)
            remote = (remote_out.strip().decode() if remote_out else "") or "origin"
            remote_ref = f"{remote}/{branch}"
```

c) Governance call (line ~5405): pass the resolved remote so the pin validates the SAME source that is fetched:

```python
            blocked = await asyncio.get_running_loop().run_in_executor(
                None, lambda: update_blocked_reason(resolve_remote_url(proj, remote=remote))
            )
```

d) Fetch (lines ~5416-5424): `"git", "fetch", remote, branch`.

e) Diff (lines ~5433-5442): `"git", "diff", "HEAD", remote_ref, "--quiet"`.

f) Reset (lines ~5471-5479): `"git", "reset", "--hard", remote_ref`.

g) Update the log line (~5486) `logger.info("Auto-update: reset to %s, rebuilding", remote_ref)`.

- [ ] **Step 4: Update the existing gateway tests**

In `test/test_slack_gateway.py`:

a) `TestAutoApplyUpdate.test_non_mainline_branch_skips` (~908): rename to `test_non_main_branch_skips` and change the mocked branch to `b"feat/test\n"` (already) — no other change needed.

b) The four `_fake_exec` tests that drive past branch detection — `test_fetch_fails_returns_early` (~1987), `test_no_diff_returns_early` (~2016), `test_venv_update_full_path` (~2685), `test_reset_then_frontend_then_pip` (~3111):
   - Replace every `b"mainline\n"` mock with `b"main\n"`.
   - Insert a new call for the remote-config subprocess. Call-count positions shift: `1` branch detection, `2` remote config (`proc.communicate = AsyncMock(return_value=(b"origin\n", b""))`, `proc.returncode = 0`), then `3` fetch, `4` diff, `5` status, `6` reset, … Renumber the existing `elif call_count[0] == N` branches accordingly (the new `b"origin\n"` default keeps the fetch args matching the old `origin`-based expectations where asserted).

- [ ] **Step 5: Run the gateway tests**

Run: `cd <repo-root> && pytest test/test_slack_gateway.py -q`
Expected: all pass (including the new tracked-remote test).

- [ ] **Step 6: Commit (ask user first)**

```bash
git add src/kiro_crew/slack/gateway.py test/test_slack_gateway.py
git commit -m "feat(update): gateway auto-apply follows the tracked remote on branch main"
```

---

### Task 6: README — updating from the fork

**Files:**
- Modify: `README.md` (fork root README)

**Interfaces:**
- Produces: operator instructions to point `main` at `fork/main` and the version-bump rule.

- [ ] **Step 1: Add the section**

Append a section to `README.md` (after the existing configuration sections):

```markdown
## Updating from the fork

This fork updates from **its own repo** (`encomjp/kirocrew-customapi`), never
from upstream `kirodotdev/KiroCrew`.

- **Desktop app:** updates come from this repo's GitHub Releases (single
  `stable` lane). The release workflow attaches `latest-mac.yml` /
  `latest-linux.yml` automatically.
- **Gateway (git install):** the update check, the dashboard Update button,
  and the boot auto-update all follow whatever remote the current branch
  tracks. Point `main` at the fork once:

  ```bash
  git branch --set-upstream-to=fork/main main
  ```

  Boot auto-update runs **only on branch `main`** (`git reset --hard
  <remote>/main`), so keep feature/`testing` branches off `main` if you don't
  want them reset.

- Every release must bump `__version__` in `src/kiro_crew/__init__.py`, or the
  version comparison won't detect the update.
```

- [ ] **Step 2: Verify**

Run: `cd <repo-root> && git diff --stat README.md`
Expected: README.md shows one added section.

- [ ] **Step 3: Commit (ask user first)**

```bash
git add README.md
git commit -m "docs(update): document how the fork updates from its own repo"
```

---

### Task 7: Full verification

**Files:** none.

- [ ] **Step 1: Electron test suite**

Run: `cd website/electron && npm test`
Expected: all tests pass.

- [ ] **Step 2: Gateway test suite**

Run: `cd <repo-root> && pytest test/test_slack_gateway.py -q`
Expected: all pass.

- [ ] **Step 3: Broader sanity (no regressions in update-adjacent code)**

Run: `cd <repo-root> && pytest test/test_updates.py test/test_update_governance.py -q 2>/dev/null || echo "no such test files — run: pytest tests/ -q -k update"`
Expected: update-related tests pass or the fallback message prints.

- [ ] **Step 4: Manual smoke (documented for the operator)**

1. Desktop: build once (`npm run dist:linux`), confirm `dist/latest-linux.yml` now lists GitHub release URLs; run `node --test` green.
2. Gateway: on a checkout where `main` tracks `fork/main`, boot the gateway — it must report "Already on latest" (no reset) when `main` is in sync with `fork/main`.
3. E2E harness still works: `scripts/stage3-autoupdate-test.sh` runs green (uses `KIROCREW_UPDATE_FEED`).

- [ ] **Step 5: Report**

Summarize: files changed, tests run, the `git branch --set-upstream-to=fork/main main` operator step, and the version-bump rule.
