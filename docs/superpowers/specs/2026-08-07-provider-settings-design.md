# Provider Settings (Agent Backend + Presets) Design

Date: 2026-08-07 · Branch: `testing` · Status: approved (direction)

## Goal

Give Settings > Preferences > Chat a **Provider** section at the top where the
operator picks an **agent backend** (Claude Code vs OpenCode) with a small
format subheader, chooses a **preset** that prefills the router **base URL**,
enters an optional **API key**, and saves — all in the app's existing settings
style. Saving applies the change live (provider factory reload), no gateway
restart needed.

## Decisions

- **Agent backend switch** — three options, each with a small format subheader:
  - `Claude Code` — *anthropic endpoint* → `agent.provider = claude_code`
  - `OpenCode` — *OpenAI-compatible endpoint* → `agent.provider = opencode`
  - `kiro-native` — *kiro-cli backend* → `agent.provider = acp` (the default;
    URL/key are managed by kiro-cli, so the URL/key fields are hidden/disabled
    with a hint when selected)
- **No bundled HTTP converter.** OpenCode translates OpenAI↔Anthropic
  internally (AI-SDK adapters); the fork points its custom provider's
  `baseURL` at the router and OpenCode handles the format. Claude Code keeps
  using its Anthropic-compatible base URL directly.
- **Presets are per-backend** and only prefill the URL field (never
  auto-save). Claude Code presets are verified Anthropic-compatible
  (`/v1/messages` live-checked 401/400); OpenCode presets are verified
  OpenAI-compatible (`/v1/chat/completions` live-checked). kiro-native has no
  presets.
- API key field is masked; a stored key renders as a "••• saved" placeholder
  and is only overwritten by new input (the config GET masks secrets).
- Live apply mirrors the existing `agent.provider` switch: rebuild agent
  config, `reload_provider_factory`, clear slot models.

## Verified preset URLs

### Claude Code backend (Anthropic-compatible, `{base}/v1/messages`)
| Preset | Base URL | Key |
|---|---|---|
| Custom | *(empty, editable)* | optional |
| Ollama Cloud | `https://ollama.com` | yes |
| OpenCode Zen | `https://opencode.ai/zen` | yes |
| OpenCode Go | `https://opencode.ai/zen/go` | yes |
| commandcode.ai | `https://commandcode.ai` | yes (path 405 — verify) |
| 9router | `http://localhost:20128` | optional |
| CLIProxyAPI | `http://localhost:8317` | optional |
| OmniRouter | *(empty, editable)* | optional |
| Anthropic | `https://api.anthropic.com` | yes |
| OpenRouter | `https://openrouter.ai/api` | yes |
| xAI | `https://api.x.ai` | yes |
| Mistral | `https://api.mistral.ai` | yes |
| DeepSeek | `https://api.deepseek.com` | yes |
| Together | `https://api.together.xyz` | yes |

### OpenCode backend (OpenAI-compatible, `{base}/v1/chat/completions`)
| Preset | Base URL | Key |
|---|---|---|
| Custom | *(empty, editable)* | optional |
| OpenAI | `https://api.openai.com` | yes |
| Groq | `https://api.groq.com/openai` | yes |
| DeepSeek | `https://api.deepseek.com` | yes |
| xAI | `https://api.x.ai` | yes |
| Ollama | `https://ollama.com` | yes |
| Together | `https://api.together.xyz` | yes |
| Mistral | `https://api.mistral.ai` | yes |

## Frontend (website/src/pages/settings/ChatPanel.tsx)

- New `SettingsSection` titled `Provider`, placed **above** the existing
  `Model` section.
- **Backend switch**: two-option button group; each option renders its label
  plus a small subheader line (`anthropic endpoint` / `OpenAI-compatible
  endpoint`). Uses the existing `SettingsButtonGroup`/`SettingsSelect` idioms.
- **Preset** `SettingsSelect`: options filtered by the selected backend;
  selecting one prefills the URL field (and sets the API-key-required hint).
- **Base URL** `SettingsInput` (editable) + **API key** `SettingsInput`
  (password, masked) + **Save** button.
- Save writes, in order: `agent.provider`, `agent.provider_base_url`,
  `agent.provider_api_key` via `api.patchConfig`, then invalidates the config
  query. Errors surface through the existing `ErrorNotice`.
- Read current values from `mcCfg.agent.{provider,provider_base_url,
  provider_api_key}`; a masked key shows the "saved" placeholder.
- i18n: new `pages.settings.chatPanel.provider*` keys (provider, backend,
  claude_code, opencode, anthropic_endpoint, openai_compatible_endpoint,
  preset, base_url, api_key, api_key_saved, save, opencode_missing_hint).
  All 12 locale files get the keys (en.manual + generated translations in the
  other files, consistent with the repo's untranslated-baseline approach).

## Backend

### `src/kiro_crew/dashboard/handlers/core.py`
- `agent.provider` enum: `["acp"]` → `["acp", "claude_code", "opencode"]`.
- New schema entries:
  - `agent.provider_base_url`: `{"type": "str", "max_len": 2048, "pattern":
    URL-ish}` — must accept `http://localhost:PORT` and `https://`; reuse the
    repo's existing URL pattern if one exists.
  - `agent.provider_api_key`: `{"type": "str", "max_len": 512}` (masked in the
    config GET response).
- Extend the provider-switch reload block so `agent.provider_base_url` and
  `agent.provider_api_key` changes trigger the same live path as
  `agent.provider` (rebuild agent config → `reload_provider_factory` → clear
  slot models).

### `src/kiro_crew/acp/client.py`
- Backend selection: `claude_code` → `claude-agent-acp` (existing), `opencode`
  → `opencode acp`. Resolve the `opencode` binary like
  `_resolve_claude_acp_bin` (PATH + common roots), with a clear error when
  missing.
- OpenCode spawn env/config: before spawning, write
  `~/.config/opencode/opencode.json` with a fork-managed provider
  (`provider.kirocrew.options.baseURL = <provider_base_url>`,
  `format: "openai"`, `apiKey`), and pass the model through OpenCode's model
  selection (env/args). Keep Claude Code's existing
  `ANTHROPIC_BASE_URL`/`ANTHROPIC_MODEL` env path untouched.
- Model-prefix mapping: strip the fork's router prefixes (`cmc/`, `oc/`,
  `ol/`, `cx/`, `ag/`) for the OpenCode model id, mirroring
  `strip_router_model_prefix`.

### Config loader (`src/kiro_crew/config/loader.py`)
- Accept `opencode` in the provider field; surface `provider_base_url` /
  `provider_api_key` unchanged (already parsed at 4576-4577).

## Out of scope
- The bundled Anthropic↔OpenAI HTTP converter (dropped — OpenCode handles it).
- A Windows-native gateway (unchanged).
- Model-catalog curation for OpenCode (OpenCode lists its own models).

## Tests
- Backend (`test/test_config_patch.py` + new acp tests):
  - `agent.provider` accepts `claude_code`/`opencode`; rejects junk.
  - `agent.provider_base_url` accepts localhost/https URLs, rejects shell
    metacharacters; `provider_api_key` accepted and masked in GET.
  - Reload fires on `provider_base_url`/`provider_api_key` change.
  - ACP backend selection returns `opencode acp` for `opencode` and
    `claude-agent-acp` for `claude_code`; opencode.json written with
    baseURL/format/key.
- Frontend (`website/src/test/ChatPanel.provider.test.tsx`):
  - Backend switch shows both options with their subheaders.
  - Presets filter by backend and prefill the URL field.
  - Save calls patchConfig for provider/base_url/api_key.
  - Stored key renders as "••• saved" and is not overwritten by an empty save.
