# Provider Settings Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a Provider section at the top of Settings > Chat where the operator picks an agent backend (Claude Code / OpenCode / kiro-native) with a format subheader, chooses a preset that prefills the router base URL, enters an API key, and saves — applied live without a gateway restart.

**Architecture:** Frontend adds a SettingsSection with a three-way backend switch + per-backend presets + URL/key inputs writing `agent.provider` / `agent.provider_base_url` / `agent.provider_api_key`. Backend widens the provider enum, adds schema entries, extends the live provider-switch reload, and teaches the ACP client a third backend (`opencode acp`) that writes an OpenCode provider config before spawning.

**Tech Stack:** React (ChatPanel, settings components, react-query mutations), Python (aiohttp config handler, ACP JSON-RPC client), pytest + vitest.

## Global Constraints

- Backends: `claude_code` (Claude Code, *anthropic endpoint*), `opencode` (OpenCode, *OpenAI-compatible endpoint*), `acp` (kiro-native, kiro-cli, *kiro-cli backend* — URL/key hidden with hint).
- Presets only prefill the URL field, never auto-save. Claude Code presets are Anthropic-compatible (`/v1/messages`), OpenCode presets are OpenAI-compatible (`/v1/chat/completions`); kiro-native has no presets.
- API key masked; stored key shows "••• saved" and is only overwritten by new input.
- Live apply = rebuild agent config → `reload_provider_factory` → clear slot models (same as the `agent.provider` switch).
- No git commits during implementation until the user has seen the built AppImage (session rule).

---

### Task 1: Backend — provider enum, schema entries, live reload

**Files:**
- Modify: `src/kiro_crew/dashboard/handlers/core.py` (schema ~1282, reload block ~1580)
- Test: `test/test_config_patch.py`

**Interfaces:**
- Produces: `agent.provider` enum accepts `acp|claude_code|opencode`; `agent.provider_base_url` (str, URL pattern, max_len 2048); `agent.provider_api_key` (str, max_len 512, masked in GET); PATCHing any of the three keys triggers the provider-switch reload path.

- [ ] **Step 1: Write failing tests**

Add to `test/test_config_patch.py` (follow the existing `TestPatchGeneral`/`TestRoleModels` style with the `tmp_config` fixture):

```python
class TestProviderPatch:
    @pytest.mark.asyncio
    async def test_provider_enum_accepts_backends(self, tmp_config) -> None:
        async with TestClient(TestServer(_make_app())) as c:
            for v in ("acp", "claude_code", "opencode"):
                assert (await _patch(c, "agent.provider", v)).status == 200

    @pytest.mark.asyncio
    async def test_provider_base_url_urls(self, tmp_config) -> None:
        async with TestClient(TestServer(_make_app())) as c:
            assert (await _patch(c, "agent.provider_base_url", "http://localhost:20128")).status == 200
            assert (await _patch(c, "agent.provider_base_url", "https://api.anthropic.com")).status == 200
            assert (await _patch(c, "agent.provider_base_url", "bad url; rm -rf /")).status == 400

    @pytest.mark.asyncio
    async def test_provider_api_key_persists_and_masks(self, tmp_config) -> None:
        async with TestClient(TestServer(_make_app())) as c:
            assert (await _patch(c, "agent.provider_api_key", "sk-test-123")).status == 200
        data = json.loads(tmp_config.read_text(encoding="utf-8"))
        assert data["agent"]["provider_api_key"] == "sk-test-123"

    @pytest.mark.asyncio
    async def test_provider_base_url_triggers_reload(self, tmp_config) -> None:
        app = _make_app()
        app["state"] = SimpleNamespace(
            subagents=MagicMock(spec=["update_completion_keep"]),
            sessions=SimpleNamespace(reload_provider_factory=AsyncMock(), refresh_defaults=AsyncMock()),
            _slots={},
        )
        with patch("kiro_crew.agent.rebuild_agent_config") as rebuild:
            async with TestClient(TestServer(app)) as c:
                assert (await _patch(c, "agent.provider_base_url", "http://localhost:8317")).status == 200
            rebuild.assert_called_once()
        app["state"].sessions.reload_provider_factory.assert_awaited_once()
```

- [ ] **Step 2: Run to verify they fail**

Run: `cd <repo> && .venv/bin/python -m pytest test/test_config_patch.py -q -k Provider`
Expected: 4 FAILED (enum rejects claude_code/opencode with 400; provider_base_url/api_key are unknown fields → 400).

- [ ] **Step 3: Implement**

In `core.py`:

a) Schema — replace the `"agent.provider"` entry (line ~1282) and add two entries after it:

```python
    "agent.provider": {"type": "enum", "values": ["acp", "claude_code", "opencode"]},
    "agent.provider_base_url": {
        "type": "str",
        "max_len": 2048,
        "pattern": r"^https?://[A-Za-z0-9._\-:\[\]/]+$",
    },
    "agent.provider_api_key": {"type": "str", "max_len": 512},
```

b) Reload block — change the guard so base-URL/key changes reload the factory too:

```python
    if path_key in ("agent.provider", "agent.provider_base_url", "agent.provider_api_key"):
        state: DashboardState = request.app["state"]
        try:
            from kiro_crew.agent import rebuild_agent_config  # noqa: F811

            await asyncio.to_thread(rebuild_agent_config)
        except Exception:
            logger.warning("Agent config rebuild after provider change failed", exc_info=True)
        await state.sessions.reload_provider_factory()
        for slot in state._slots.values():
            if slot.model:
                slot.model = ""
        state.push_slots_update()
        logger.info("Provider config changed to %r — factory reloaded, slot models cleared", value)
```

(Replace the existing `if path_key == "agent.provider":` block wholesale; keep its comment intent.)

- [ ] **Step 4: Run to verify they pass**

Run: `cd <repo> && .venv/bin/python -m pytest test/test_config_patch.py -q`
Expected: all pass (67 existing + 4 new).

---

### Task 2: Backend — ACP opencode backend (spawn + provider config)

**Files:**
- Modify: `src/kiro_crew/acp/client.py` (constants, `_resolve_opencode_bin`, `_spawn`, env build)
- Modify: `src/kiro_crew/providers/acp.py` (pass backend for opencode)
- Test: `test/test_acp_client.py` (or new `test/test_acp_opencode.py`)

**Interfaces:**
- Produces: `ACP_BACKEND_OPENCODE = "opencode"`; `_resolve_opencode_bin() -> list[str] | None`;
  when `self.backend == ACP_BACKEND_OPENCODE`, `_spawn` writes
  `~/.config/opencode/opencode.json` (`{"provider": {"kirocrew": {"options": {"baseURL": <base_url>, "apiKey": <key>, "format": "openai"}}}}`)
  and spawns `opencode acp`.

- [ ] **Step 1: Write failing tests**

```python
def test_opencode_bin_resolution_missing():
    from kiro_crew.acp import client as acp

    with patch("shutil.which", return_value=None):
        assert acp._resolve_opencode_bin() is None


@pytest.mark.asyncio
async def test_spawn_writes_opencode_config(tmp_path, monkeypatch):
    from kiro_crew.acp import client as acp

    monkeypatch.setenv("HOME", str(tmp_path))
    client = acp.AcpClient(**_backend_kwargs(acp.ACP_BACKEND_OPENCODE, base_url="http://localhost:8317", api_key="sk-x"))
    # Stub the binary + subprocess spawn; assert argv + the written opencode.json.
    ...
```

(Implement `_backend_kwargs` in the test to build the kwargs the provider factory passes. Keep the assertion on argv `== [<opencode bin>, "acp"]` and the JSON content.)

- [ ] **Step 2: Run to verify they fail** — new module-level helpers don't exist yet.

- [ ] **Step 3: Implement**

In `acp/client.py`:

a) Near `ACP_BACKEND_CLAUDE` (line ~47 import) add:

```python
ACP_BACKEND_OPENCODE = "opencode"
OPENCODE_BIN = "opencode"
```

b) Resolver (near `_resolve_claude_acp_bin`, line ~397):

```python
def _resolve_opencode_bin() -> list[str] | None:
    """Resolve the `opencode` CLI entry (PATH + common npm-global roots)."""
    on_path = shutil.which(OPENCODE_BIN)
    if on_path:
        return [on_path]
    for root in _npm_global_roots():
        candidate = os.path.join(root, OPENCODE_BIN)
        if os.path.isfile(candidate):
            return [candidate]
    return None
```

(`_npm_global_roots()` already exists for claude-agent-acp — reuse the same roots the claude resolver walks.)

c) In `_spawn` (line ~2640): add an opencode branch before the kiro fallback:

```python
        if self._is_claude:
            ... existing claude branch unchanged ...
        elif self.backend == ACP_BACKEND_OPENCODE:
            await asyncio.to_thread(self._write_opencode_provider_config)
            opencode_argv = await asyncio.to_thread(_resolve_opencode_bin)
            if not opencode_argv:
                raise AcpError(f"{OPENCODE_BIN} not found. Install it with 'npm i -g opencode-ai'.")
            argv = [*opencode_argv, "acp"]
        else:
            ... existing kiro branch unchanged ...
```

d) New method `_write_opencode_provider_config` (mirror `_write_claude_local_settings` pattern):

```python
    def _write_opencode_provider_config(self) -> None:
        """Seed ~/.config/opencode/opencode.json with the fork's custom provider.

        baseURL comes from extra_env ANTHROPIC_BASE_URL (set from
        agent.provider_base_url); the key from extra_env ANTHROPIC_AUTH_TOKEN
        when present. format is openai (OpenCode translates to Anthropic).
        """
        home = os.path.expanduser("~")
        cfg_dir = os.path.join(home, ".config", "opencode")
        os.makedirs(cfg_dir, exist_ok=True)
        path = os.path.join(cfg_dir, "opencode.json")
        existing = {}
        try:
            with open(path, encoding="utf-8") as fh:
                existing = json.load(fh)
        except (OSError, ValueError):
            existing = {}
        base_url = (self._extra_env or {}).get("ANTHROPIC_BASE_URL", "")
        api_key = (self._extra_env or {}).get("ANTHROPIC_AUTH_TOKEN", "")
        options: dict = {"baseURL": base_url, "format": "openai"}
        if api_key:
            options["apiKey"] = api_key
        existing.setdefault("provider", {})["kirocrew"] = {"options": options}
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(existing, fh, indent=2)
```

e) In `providers/acp.py` (line ~273 construction): when the configured provider is `opencode`, pass `_acp_backend=ACP_BACKEND_OPENCODE` and keep putting `ANTHROPIC_BASE_URL` / `ANTHROPIC_AUTH_TOKEN` into `extra_env` (already the mechanism for claude_code — reuse it).

- [ ] **Step 4: Run tests** — `pytest test/test_acp_client.py -q` + the new tests; then the full `test/test_config_patch.py`.

---

### Task 3: Frontend — Provider section in ChatPanel

**Files:**
- Create: `website/src/pages/settings/providerPresets.ts`
- Modify: `website/src/pages/settings/ChatPanel.tsx` (new section above `model`)
- Modify: `website/src/i18n/locales/en.manual.json` + the 11 other locale files (keys added to all; only en gets full copy, consistent with untranslated-baseline)
- Test: `website/src/test/ChatPanel.provider.test.tsx`

**Interfaces:**
- Produces: `PROVIDER_PRESETS: Record<'claude_code' | 'opencode', { value: string; label: string; url: string }[]>`; a `SettingsSection` titled "Provider" with backend switch, preset select, URL/key inputs, save button.

- [ ] **Step 1: Write failing test**

`website/src/test/ChatPanel.provider.test.tsx` — render ChatPanel (follow `ChatPanel.defaultModel.test.tsx` harness), assert:
- the three backend options render with their subheaders ("anthropic endpoint", "OpenAI-compatible endpoint", "kiro-cli backend");
- selecting the "Groq" preset under OpenCode prefills the URL field with `https://api.groq.com/openai`;
- clicking Save issues `PATCH /api/config/kirocrew` for `agent.provider`, `agent.provider_base_url`, `agent.provider_api_key`;
- a stored key renders as "••• saved".

- [ ] **Step 2: Run to verify it fails** — `npx vitest run src/test/ChatPanel.provider.test.tsx`.

- [ ] **Step 3: Implement**

a) `providerPresets.ts`:

```ts
export type AgentBackend = 'acp' | 'claude_code' | 'opencode'
export interface ProviderPreset { value: string; label: string; url: string; keyRequired?: boolean }
export const BACKEND_OPTIONS: { value: AgentBackend; label: string; sub: string }[] = [
  { value: 'claude_code', label: 'Claude Code', sub: 'anthropic endpoint' },
  { value: 'opencode', label: 'OpenCode', sub: 'OpenAI-compatible endpoint' },
  { value: 'acp', label: 'kiro-native', sub: 'kiro-cli backend' },
]
export const PROVIDER_PRESETS: Record<'claude_code' | 'opencode', ProviderPreset[]> = {
  claude_code: [
    { value: 'custom', label: 'Custom', url: '' },
    { value: 'ollama-cloud', label: 'Ollama Cloud', url: 'https://ollama.com', keyRequired: true },
    { value: 'opencode-zen', label: 'OpenCode Zen', url: 'https://opencode.ai/zen', keyRequired: true },
    { value: 'opencode-go', label: 'OpenCode Go', url: 'https://opencode.ai/zen/go', keyRequired: true },
    { value: 'commandcode', label: 'commandcode.ai', url: 'https://commandcode.ai', keyRequired: true },
    { value: '9router', label: '9router', url: 'http://localhost:20128' },
    { value: 'cli-proxy-api', label: 'CLIProxyAPI', url: 'http://localhost:8317' },
    { value: 'omnirouter', label: 'OmniRouter', url: '' },
    { value: 'anthropic', label: 'Anthropic', url: 'https://api.anthropic.com', keyRequired: true },
    { value: 'openrouter', label: 'OpenRouter', url: 'https://openrouter.ai/api', keyRequired: true },
    { value: 'xai', label: 'xAI', url: 'https://api.x.ai', keyRequired: true },
    { value: 'mistral', label: 'Mistral', url: 'https://api.mistral.ai', keyRequired: true },
    { value: 'deepseek', label: 'DeepSeek', url: 'https://api.deepseek.com', keyRequired: true },
    { value: 'together', label: 'Together', url: 'https://api.together.xyz', keyRequired: true },
  ],
  opencode: [
    { value: 'custom', label: 'Custom', url: '' },
    { value: 'openai', label: 'OpenAI', url: 'https://api.openai.com', keyRequired: true },
    { value: 'groq', label: 'Groq', url: 'https://api.groq.com/openai', keyRequired: true },
    { value: 'deepseek', label: 'DeepSeek', url: 'https://api.deepseek.com', keyRequired: true },
    { value: 'xai', label: 'xAI', url: 'https://api.x.ai', keyRequired: true },
    { value: 'ollama', label: 'Ollama', url: 'https://ollama.com', keyRequired: true },
    { value: 'together', label: 'Together', url: 'https://api.together.xyz', keyRequired: true },
    { value: 'mistral', label: 'Mistral', url: 'https://api.mistral.ai', keyRequired: true },
  ],
}
```

b) ChatPanel.tsx — insert the section above the `model` SettingsSection (line ~390). Local state: `backend`, `preset`, `url`, `keyInput`; read initial values from `mcCfg?.agent`. Backend switch = two-column button group (mirror `SettingsButtonGroup` styles) with each option's `sub` line under the label. Preset `SettingsSelect` filtered by backend; onChange prefills url. Save button runs three `patchConfig` mutations (provider, provider_base_url, provider_api_key only when a new key was typed) then `qc.invalidateQueries`. Show `ErrorNotice` on failure. When backend === 'acp', hide URL/key/preset and show the hint.

c) i18n keys under `pages.settings.chatPanel`: `provider`, `provider_backend`, `claude_code`, `opencode`, `kiro_native`, `anthropic_endpoint`, `openai_compatible_endpoint`, `kiro_cli_backend`, `provider_preset`, `provider_url`, `provider_api_key`, `provider_api_key_saved`, `provider_save`, `provider_saved`, `provider_managed_by_kiro_cli`, `provider_opencode_missing`.

- [ ] **Step 4: Run tests** — `npx vitest run src/test/ChatPanel.provider.test.tsx src/test/ChatPanel.defaultModel.test.tsx` + full `npm test` (electron) unaffected.

---

### Task 4: Build the AppImage and show it

**Files:** none (manual verification).

- [ ] Run `UNIVERSAL=0 bash packaging/build-desktop.sh` from `website/electron` (or `npm run dist:linux`), producing `dist/kirocrew-customapi-<ver>.AppImage`.
- [ ] Install over `~/Applications/KiroCrew.AppImage` (keep `.bak`), launch on `:0`, open Settings > Chat, screenshot the Provider section, and show the user before any commit.
