import { useState, useCallback, useRef, useEffect } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { SettingsSection, SettingsCard, SettingsToggle, SettingsSelect, SettingsInput, SettingsButtonGroup } from '../../components/settings'
import { Btn } from '../../components/ui'
import { loadChatConfig, saveChatConfig, type ChatConfig, type ContentWidth, type DashboardConfig, type SendMode } from '../chat/ChatSettings'
import { api } from '../../api/client'
import { useAvailableModels } from '../../hooks/useAvailableModels'
import { useProvider } from '../../providers'
import { BACKEND_OPTIONS, PROVIDER_PRESETS, type AgentBackend } from './providerPresets'
import { modelListRefetchInterval } from '../../providers/modelListHealth'
import { EFFORT_LEVELS, effortLabel, modelSupportsEffort } from '../../lib/effort'
import { isMac } from '../../utils/platform'
import { capRoleOther, clampRoleOther } from '../../lib/userProfile'
import { ROLE_SLUGS, TECH_SLUGS } from '../../lib/profileOptions'

import { i18nT } from '../../i18n/t'
import ErrorNotice from '../../components/ErrorNotice'
/**
 * Option labels are FUNCTIONS, not module-level arrays.
 *
 * Every `*_LABELS` array below used to be a module-level const, which is evaluated
 * once at import: an `i18nT()` call there would freeze whatever language was active
 * at boot and never re-resolve. Each resolver is called in the render body instead
 * (`optionLabels={roleLabels()}`), so a language switch re-reads the catalog.
 *
 * Each list stays POSITIONALLY paired with its `*_OPTIONS` array — `SettingsSelect`
 * matches a label to a value by index — so entries must be added and reordered in
 * lockstep.
 */
const RESTORE_OPTIONS = ['15', '30', '60', '120', '360', '720', '1440', '0']
/** Duration abbreviations are left verbatim (locale-aware unit formatting is Phase 4
 *  territory); only the `'0'` sentinel's label is prose. It reuses the in-chat
 *  settings popover's key — same setting, same option, one string to translate. */
function restoreLabels(): string[] {
  return ['15m', '30m', '1h', '2h', '6h', '12h', '24h', i18nT('pages.settings.chatPanel.no_limit')]
}
const COMPACT_OPTIONS = ['20', '40', '60', '70', '80', '90']
const COMPACT_LABELS = ['20% (aggressive)', '40%', '60%', '70% (default)', '80%', '90%']

// About You — slugs shared with onboarding step 2 and context.py's prompt maps.
const ROLE_OPTIONS = ['', ...ROLE_SLUGS]
function roleLabels(): string[] {
  return [
    i18nT('pages.settings.chatPanel.not_set'),
    i18nT('pages.settings.chatPanel.developer'),
    i18nT('pages.settings.chatPanel.ux_designer'),
    i18nT('pages.settings.chatPanel.product_manager'),
    i18nT('pages.settings.chatPanel.data_ml'),
    i18nT('pages.settings.chatPanel.it_ops'),
    i18nT('pages.settings.chatPanel.other'),
  ]
}
const TECH_OPTIONS = ['', ...TECH_SLUGS]
function techLabels(): string[] {
  return [
    i18nT('pages.settings.chatPanel.not_set'),
    i18nT('pages.settings.chatPanel.i_write_code'),
    i18nT('pages.settings.chatPanel.somewhat'),
    i18nT('pages.settings.chatPanel.not_technical'),
  ]
}

const SOFT_STOP_MIN = 0.5
const SOFT_STOP_MAX = 60
const SOFT_STOP_DEFAULT = 10.0

type CompletionKeepMode = 'head' | 'tail' | 'both'
const COMPLETION_KEEP_OPTIONS: CompletionKeepMode[] = ['head', 'tail', 'both']

type VerbosityLevel = 'default' | 'concise' | 'ultra'
const VERBOSITY_OPTIONS: VerbosityLevel[] = ['default', 'concise', 'ultra']

/**
 * Narrow a persisted `dashboard.verbosity` to a level this Select can render.
 *
 * The config loader reads the field with a plain `.get()` and does not type-check
 * it, so a hand-edited or migrated `config.json` can put any JSON there — e.g.
 * `{"dashboard": {"verbosity": {}}}` — and the GET response hands that object
 * straight to the UI. `?? 'default'` guards only null/undefined, so an object
 * would flow into SimpleSelect's `triggerFallback`
 * (`optionLabels?.[options.indexOf(value)] ?? (value || '—')`): `indexOf` misses,
 * the object is truthy, and React throws on rendering it as a child — taking the
 * whole Chat settings page down rather than degrading one row.
 */
function asVerbosity(value: unknown): VerbosityLevel {
  return VERBOSITY_OPTIONS.includes(value as VerbosityLevel)
    ? (value as VerbosityLevel)
    : 'default'
}
function completionKeepLabels(): string[] {
  return [
    i18nT('pages.settings.chatPanel.head_preserve_start_of_stream'),
    i18nT('pages.settings.chatPanel.tail_preserve_end_final_summary'),
    i18nT('pages.settings.chatPanel.both_head_tail_with_truncation_marker'),
  ]
}
const COMPLETION_KEEP_CHARS_MIN = 0
// Mirrors RESULT_FILE_MAX_BYTES on the backend (handlers/core.py _EDITABLE_CONFIG).
const COMPLETION_KEEP_CHARS_MAX = 512000
const COMPLETION_KEEP_CHARS_DEFAULT = 3000

export function ChatPanel() {
  const qc = useQueryClient()
  const [chatCfg, setChatCfg] = useState<ChatConfig>(loadChatConfig)
  const [saveError, setSaveError] = useState('')

  // ── Dashboard config (server-side) ──
  const dashQ = useQuery<DashboardConfig>({
    queryKey: ['dashboardConfig'],
    queryFn: () => api.dashboardConfig(),
  })
  const dashCfg = dashQ.data ?? { restore_sessions: false, restore_window_minutes: 30, merge_queued_messages: false, widget_density: 'more' as const, verbosity: 'default' as const, quick_send: false, session_grid: false, tail_fork_enabled: false, link_previews: false, mcp_app_panel: false, auto_open_git_panel: false, folder_suggestions_enabled: true, use_builtin_browser: true }

  // ── Feature Tips opt-out (server-side per-user state) ──
  const tipsQ = useQuery<{ enabled_config: boolean; opted_out: boolean }>({
    queryKey: ['tipsStatus'],
    queryFn: () => api.tipsStatus(),
  })
  const tipsMut = useMutation({
    mutationFn: (enable: boolean) => api.tipsFeedback('', enable ? 'optin' : 'optout'),
    onMutate: async (enable) => {
      await qc.cancelQueries({ queryKey: ['tipsStatus'] })
      const prev = qc.getQueryData<{ enabled_config: boolean; opted_out: boolean }>(['tipsStatus'])
      if (prev) qc.setQueryData(['tipsStatus'], { ...prev, opted_out: !enable })
      return { prev }
    },
    onError: (_err, _vars, ctx) => {
      if (ctx?.prev) qc.setQueryData(['tipsStatus'], ctx.prev)
      setSaveError(i18nT('pages.settings.chatPanel.failed_to_save_tips_preference'))
    },
    onSettled: () => {
      qc.invalidateQueries({ queryKey: ['tipsStatus'] })
      // Drop any cached/in-flight tip so a running Chat view can't display a
      // tip fetched before the preference changed.
      qc.removeQueries({ queryKey: ['tips-next'] })
    },
  })
  const tipsConfigOff = tipsQ.data ? !tipsQ.data.enabled_config : false

  const dashMut = useMutation({
    mutationFn: (next: DashboardConfig) => api.updateDashboardConfig(next),
    onMutate: async (next) => {
      await qc.cancelQueries({ queryKey: ['dashboardConfig'] })
      const prev = qc.getQueryData<DashboardConfig>(['dashboardConfig'])
      qc.setQueryData(['dashboardConfig'], next)
      return { prev }
    },
    onError: (_err, _vars, ctx) => {
      if (ctx?.prev) qc.setQueryData(['dashboardConfig'], ctx.prev)
      setSaveError(i18nT('pages.settings.chatPanel.failed_to_save_dashboard_config'))
    },
    onSettled: () => qc.invalidateQueries({ queryKey: ['dashboardConfig'] }),
  })

  // ── KiroCrew config (server-side) ──
  const mcQ = useQuery<{
    session?: { autocompact_pct?: number }
    session_summary?: { enabled?: boolean }
    agent?: {
      provider?: string
      provider_base_url?: string
      provider_api_key?: string
      provider_api_format?: string
      model_whitelist?: string[]
      model?: string
      role_models?: { background?: string; subagent?: string }
      role_efforts?: { background?: string; subagent?: string }
      reasoning_effort?: string
      soft_stop_budget_secs?: number
      completion_keep?: CompletionKeepMode
      completion_keep_chars?: number
      fallback_model?: string
    }
    dashboard?: { user_role?: string; user_role_other?: string; user_technical_level?: string; prevent_sleep?: boolean }
  }>({
    queryKey: ['kirocrewConfig'],
    queryFn: () => api.kirocrewConfig(),
  })
  const mcCfg = mcQ.data

  // ── User profile (About You) ──
  // Same slugs as onboarding step 2 (OnboardingFlow.tsx), validated by the
  // config PATCH allowlist (handlers/core.py) and mapped to the prompt's
  // [USER PROFILE] block in context.py.
  const userRole = mcCfg?.dashboard?.user_role ?? ''
  const userRoleOther = mcCfg?.dashboard?.user_role_other ?? ''
  const userTechLevel = mcCfg?.dashboard?.user_technical_level ?? ''
  const profileMut = useMutation({
    mutationFn: ({ path, value }: { path: string; value: string }) =>
      api.patchConfig(path, value),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['kirocrewConfig'] }),
    onError: () => setSaveError(i18nT('pages.settings.chatPanel.failed_to_save_profile')),
  })

  // ── Prevent sleep while running (server-side; gateway-host behavior) ──
  const preventSleep = mcCfg?.dashboard?.prevent_sleep ?? false
  const preventSleepMut = useMutation({
    mutationFn: (v: boolean) => api.patchConfig('dashboard.prevent_sleep', v),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['kirocrewConfig'] }),
    onError: () => setSaveError(i18nT('pages.settings.chatPanel.failed_to_save_dashboard_config')),
  })

  // ── Session summaries (server-side; spends tokens per changed turn) ──
  const summaryEnabled = mcCfg?.session_summary?.enabled ?? false
  const summaryMut = useMutation({
    mutationFn: (v: boolean) => api.patchConfig('session_summary.enabled', v),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['kirocrewConfig'] }),
    onError: () => setSaveError(i18nT('pages.settings.chatPanel.failed_to_save_session_summaries')),
  })

  // "Other" reveals a free-text role. Typed locally and committed on blur /
  // Enter so a PATCH does not fire per keystroke; seeded from the server once
  // the config query resolves, and re-seeded whenever the server value changes
  // (another tab, or the onboarding replay writing it).
  const [localRoleOther, setLocalRoleOther] = useState(userRoleOther)
  const roleOtherSeedRef = useRef(userRoleOther)
  useEffect(() => {
    if (roleOtherSeedRef.current !== userRoleOther) {
      roleOtherSeedRef.current = userRoleOther
      setLocalRoleOther(userRoleOther)
    }
  }, [userRoleOther])
  const commitRoleOther = () => {
    const next = clampRoleOther(localRoleOther)
    if (next === userRoleOther) return
    roleOtherSeedRef.current = next
    setLocalRoleOther(next)
    profileMut.mutate({ path: 'dashboard.user_role_other', value: next })
  }

  const [localBudget, setLocalBudget] = useState('')
  const budgetInitRef = useRef(false)
  useEffect(() => {
    if (mcQ.data && !budgetInitRef.current) {
      budgetInitRef.current = true
      setLocalBudget(String(mcQ.data.agent?.soft_stop_budget_secs ?? SOFT_STOP_DEFAULT))
    }
  }, [mcQ.data])

  const budgetMut = useMutation({
    mutationFn: (n: number) => api.patchConfig('agent.soft_stop_budget_secs', n),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['kirocrewConfig'] }),
    onError: () => {
      setSaveError(i18nT('pages.settings.chatPanel.failed_to_save_soft_stop_budget'))
      // Revert the input to the last-known server value so the user isn't
      // left looking at an unpersisted number. budgetInitRef stays true,
      // so the init effect will not clobber this on future query updates.
      setLocalBudget(String(mcCfg?.agent?.soft_stop_budget_secs ?? SOFT_STOP_DEFAULT))
    },
  })

  // ── Throttle-fallback model (agent.fallback_model) ──
  // Single-select dropdown fed by the same advertised-model list as the
  // role-model rows (no free text — a typo'd id can't exist). "" = disabled,
  // "auto" (default) = backend availability-aware routing, concrete id =
  // tried first with "auto" as the final fallthrough.
  const fallbackModel = mcCfg?.agent?.fallback_model ?? 'auto'
  const fallbackMut = useMutation({
    mutationFn: (v: string) => api.patchConfig('agent.fallback_model', v),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['kirocrewConfig'] }),
    onError: (err: unknown) => {
      // Surface the backend's actual deny reason (e.g. an unentitled id)
      // next to the generic failure line.
      const reason = err instanceof Error && err.message ? `: ${err.message}` : ''
      setSaveError(i18nT('pages.settings.chatPanel.failed_to_save_fallback_model') + reason)
    },
  })
  const fallbackModelOptions = (current: string): string[] => {
    const opts = ['', 'auto', ...availableModels.map(m => m.name).filter(m => m !== 'auto')]
    if (current && !opts.includes(current)) opts.splice(2, 0, current)
    return opts
  }
  const fallbackModelLabels = (opts: string[]): string[] =>
    opts.map(m =>
      m === ''
        ? i18nT('pages.settings.chatPanel.fallback_disabled')
        : m === 'auto'
          ? i18nT('pages.settings.chatPanel.fallback_auto')
          : m,
    )

  const [localKeepChars, setLocalKeepChars] = useState('')
  const keepCharsInitRef = useRef(false)
  useEffect(() => {
    if (mcQ.data && !keepCharsInitRef.current) {
      keepCharsInitRef.current = true
      setLocalKeepChars(String(mcQ.data.agent?.completion_keep_chars ?? COMPLETION_KEEP_CHARS_DEFAULT))
    }
  }, [mcQ.data])

  const keepCharsMut = useMutation({
    mutationFn: (n: number) => api.patchConfig('agent.completion_keep_chars', n),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['kirocrewConfig'] }),
    onError: () => {
      setSaveError(i18nT('pages.settings.chatPanel.failed_to_save_completion_keep_characters'))
      setLocalKeepChars(
        String(mcCfg?.agent?.completion_keep_chars ?? COMPLETION_KEEP_CHARS_DEFAULT)
      )
    },
  })

  // ── Vision (agent.image_input_mode / image_redirect / vision_fallback_model) ──
  // Same card that owns the model picker, so Vision reads as a first-class
  // section there rather than a hidden config.json toggle. The picker already
  // groups by supports_vision; these controls govern HOW image prompts are
  // handled on text-only models (the runtime's decide_image_input_mode + ACP
  // redirect). Wrong-mode images veto session resume (SessionConfig), so the
  // values here have UI-visible consequences — they deserve UI.
  const IMAGE_MODE_OPTIONS: ('auto' | 'native' | 'text')[] = ['auto', 'native', 'text']
  const IMAGE_MODE_LABELS = ['Auto (vision-aware)', 'Native (always pixels)', 'Text (always describe)']
  const REDIRECT_OPTIONS = ['subagent', 'switch', 'off'] as const
  const REDIRECT_LABELS = ['Describe via vision subagent', 'Switch session to vision model', 'Off (send through — may 400)']
  const visionMode = (mcCfg?.agent as Record<string, unknown> | undefined)?.image_input_mode as string | undefined ?? 'auto'
  const visionRedirect = (mcCfg?.agent as Record<string, unknown> | undefined)?.image_redirect as string | undefined ?? 'subagent'
  const visionFallback = (mcCfg?.agent as Record<string, unknown> | undefined)?.vision_fallback_model as string | undefined ?? 'cmc/mimo-v2.5'
  const fallbackOptions = ['cmc/mimo-v2.5', 'cmc/Kimi-K2.6', 'cmc/GLM-5.2', 'cmc/deepseek-v4-pro', 'ol/kimi-k2.6', 'ol/glm-5.2', 'oc/kimi-k2.6', 'oc/glm-5.2', 'oc/mimo-v2.5', 'ag/gemini-3-flash', 'ag/gemini-3.6-flash-high', 'cx/gpt-5.6-luna']
  const visionModeMut = useMutation({
    mutationFn: (v: string) => api.patchConfig('agent.image_input_mode', v),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['kirocrewConfig'] }),
    onError: () => setSaveError(i18nT('pages.settings.chatPanel.failed_to_save_vision_mode')),
  })
  const visionRedirectMut = useMutation({
    mutationFn: (v: string) => api.patchConfig('agent.image_redirect', v),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['kirocrewConfig'] }),
    onError: () => setSaveError(i18nT('pages.settings.chatPanel.failed_to_save_vision_redirect')),
  })
  const visionFallbackMut = useMutation({
    mutationFn: (v: string) => api.patchConfig('agent.vision_fallback_model', v),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['kirocrewConfig'] }),
    onError: () => setSaveError(i18nT('pages.settings.chatPanel.failed_to_save_vision_fallback')),
  })

  // ── Security & Shim (agent.safe_mode / use_shim / shim_openai_base_url) ──
  // Fork-only guardrails + the built-in Anthropic→OpenAI translator. safe_mode
  // refuses public router endpoints at session construction; the shim lets
  // provider_base_url point at loopback and drive any OpenAI-shaped backend.
  const safeMode = (mcCfg?.agent as Record<string, unknown> | undefined)?.safe_mode as boolean | undefined ?? false
  const useShim = (mcCfg?.agent as Record<string, unknown> | undefined)?.use_shim as boolean | undefined ?? false
  const shimUrlCfg = (mcCfg?.agent as Record<string, unknown> | undefined)?.shim_openai_base_url as string | undefined ?? 'http://127.0.0.1:11434/v1'
  const [localShimUrl, setLocalShimUrl] = useState('')
  const shimUrlInitRef = useRef(false)
  useEffect(() => {
    if (mcQ.data && !shimUrlInitRef.current) {
      shimUrlInitRef.current = true
      setLocalShimUrl(shimUrlCfg)
    }
  }, [mcQ.data])
  const safeModeMut = useMutation({
    mutationFn: (v: boolean) => api.patchConfig('agent.safe_mode', v),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['kirocrewConfig'] }),
    onError: () => setSaveError('Failed to save safe mode'),
  })
  const useShimMut = useMutation({
    mutationFn: (v: boolean) => api.patchConfig('agent.use_shim', v),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['kirocrewConfig'] }),
    onError: () => setSaveError('Failed to save shim toggle'),
  })
  const shimUrlMut = useMutation({
    mutationFn: (v: string) => api.patchConfig('agent.shim_openai_base_url', v),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['kirocrewConfig'] }),
    onError: () => setSaveError('Failed to save shim URL'),
  })

  // Backend default for knowledge.auto_ingest_chunk_budget (config/loader.py).
  // Definition was lost in the v0.5.0 rebase splice — restored; keep in sync.
  const CHUNK_BUDGET_DEFAULT = 150
  const [localChunkBudget, setLocalChunkBudget] = useState('')
  const chunkBudgetInitRef = useRef(false)
  useEffect(() => {
    if (mcQ.data && !chunkBudgetInitRef.current) {
      chunkBudgetInitRef.current = true
      setLocalChunkBudget(
        String(mcQ.data.knowledge?.auto_ingest_chunk_budget ?? CHUNK_BUDGET_DEFAULT)
      )
    }
  }, [mcQ.data])

  const knowledgeMut = useMutation({
    mutationFn: ({ path, value }: { path: string; value: boolean | number }) =>
      api.patchConfig(path, value),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['kirocrewConfig'] }),
    onError: () => {
      setSaveError(i18nT('pages.settings.chatPanel.failed_to_save_knowledge_setting'))
      setLocalChunkBudget(
        String(mcCfg?.knowledge?.auto_ingest_chunk_budget ?? CHUNK_BUDGET_DEFAULT)
      )
    },
  })
  const knowledgeDisabled = !mcQ.isSuccess || knowledgeMut.isPending

  const keepModeMut = useMutation({
    mutationFn: (v: CompletionKeepMode) => api.patchConfig('agent.completion_keep', v),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['kirocrewConfig'] }),
    onError: () => setSaveError(i18nT('pages.settings.chatPanel.failed_to_save_completion_keep_mode')),
  })

  // Check which ACP backends have their binary installed so we can warn the
  // user before they select a backend that will fail at chat time.
  const { data: providerStatus } = useQuery({
    queryKey: ['provider-status'],
    queryFn: () => api.providerStatus(),
    staleTime: 30_000,
  })

  // ── Default model + default reasoning effort ──
  // These are the DEFAULTS for new sessions. A session's own model/effort
  // picker still overrides them per-slot; nothing here touches live sessions.
  // Same query key as every other model picker so the list is fetched once.
  const availableModels = useAvailableModels()
  // '' in config means "unset" and resolves the same way 'auto' does, so both
  // render as the 'auto' option rather than as a missing selection.
  const defaultModel = mcCfg?.agent?.model || 'auto'
  const modelOptions = availableModels.map(m => m.name)
  // A model the live backend no longer advertises must still be selectable,
  // otherwise the select would silently jump to another entry and a stray
  // change event would overwrite the user's stored choice.
  if (!modelOptions.includes(defaultModel)) modelOptions.unshift(defaultModel)

  const defaultModelMut = useMutation({
    mutationFn: (v: string) => api.patchConfig('agent.model', v),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['kirocrewConfig'] }),
    onError: () => setSaveError(i18nT('pages.settings.chatPanel.failed_to_save_default_model')),
  })

  // ── Provider (agent backend + router URL/key) ──
  // The section edits a DRAFT that only becomes config on Save: the backend
  // switch, preset prefill and URL/key inputs never write anything on their
  // own. A draft overrides the loaded config until saved or the user edits
  // something else; kiro-native (acp) manages its router itself.
  const [draft, setDraft] = useState<{ backend: AgentBackend; preset: string; url: string; key: string; format?: 'anthropic' | 'openai' } | null>(null)
  const [providerSaving, setProviderSaving] = useState(false)
  const [providerSaveError, setProviderSaveError] = useState('')
  const [providerTesting, setProviderTesting] = useState(false)
  const [providerTestResult, setProviderTestResult] = useState<{ ok: boolean; message: string; models?: string[] } | null>(null)
  const [modelSel, setModelSel] = useState<string[]>([])
  useEffect(() => {
    if (mcCfg?.agent?.model_whitelist) setModelSel(mcCfg.agent.model_whitelist)
  }, [mcCfg])
  const savedWhitelist = mcCfg?.agent?.model_whitelist ?? []
  const whitelistChanged = modelSel.length !== savedWhitelist.length || modelSel.some((m, i) => m !== savedWhitelist[i])

  const effBackend: AgentBackend = draft?.backend ??
    (mcCfg?.agent?.provider === 'claude_code' || mcCfg?.agent?.provider === 'opencode'
      ? mcCfg.agent.provider
      : 'acp')
  const effUrl = draft?.url ?? mcCfg?.agent?.provider_base_url ?? ''
  // Bug 1 fix: derive preset from the saved URL so it doesn't reset to
  // "custom" after save + reload.
  const savedPreset = (() => {
    const savedUrl = (mcCfg?.agent?.provider_base_url ?? '').replace(/\/+$/, '')
    const presets = (mcCfg?.agent?.provider === 'claude_code' || mcCfg?.agent?.provider === 'opencode')
      ? PROVIDER_PRESETS[mcCfg.agent.provider] : []
    return presets.find(p => p.url === savedUrl)?.value ?? 'custom'
  })()
  const effPreset = draft?.preset ?? savedPreset
  const effFormat: 'anthropic' | 'openai' = (draft?.format ??
    (effBackend === 'opencode'
      ? (mcCfg?.agent?.provider_api_format ?? 'openai')
      : 'anthropic')) as 'anthropic' | 'openai'
  const hasStoredKey = !!mcCfg?.agent?.provider_api_key
  const providerPresets = effBackend === 'acp' ? [] : PROVIDER_PRESETS[effBackend]

  const applyPreset = (value: string) => {
    const preset = providerPresets.find(p => p.value === value)
    setDraft(prev => ({
      backend: effBackend,
      preset: value,
      url: preset ? preset.url : '',
      key: prev?.key ?? '',
      format: preset?.format ?? (effBackend === 'opencode' ? 'openai' : 'anthropic'),
    }))
  }

  const switchBackend = (value: AgentBackend) => {
    // Switching backend resets the draft to a clean custom entry — preset
    // URLs are backend-specific.
    setDraft({ backend: value, preset: 'custom', url: '', key: '', format: value === 'opencode' ? 'openai' : 'anthropic' })
    setProviderTestResult(null)
  }

  const providerTest = async () => {
    setProviderTesting(true)
    setProviderTestResult(null)
    try {
      // Bug 2 fix: use the stored key when no draft key is entered (e.g.
      // after a restart with no unsaved changes).
      const key = draft?.key || (hasStoredKey ? undefined : undefined)
      // When no draft key is present but a key is stored, the backend
      // should use the saved key. Pass a flag so the endpoint knows.
      const res = await api.providerTest({ url: effUrl, api_key: key, format: effFormat, use_stored: !draft?.key && hasStoredKey })
      setProviderTestResult(res)
    } catch {
      setProviderTestResult({ ok: false, message: 'request failed' })
    } finally {
      setProviderTesting(false)
    }
  }

  const toggleWhitelistModel = (id: string) => {
    setModelSel(cur => (cur.includes(id) ? cur.filter(x => x !== id) : [...cur, id]))
  }

  const providerSave = async () => {
    setProviderSaving(true)
    setProviderSaveError('')
    try {
      if (draft?.backend && draft.backend !== (mcCfg?.agent?.provider ?? 'acp')) {
        await api.patchConfig('agent.provider', draft.backend)
      }
      if (draft?.url !== undefined && draft.url !== (mcCfg?.agent?.provider_base_url ?? '')) {
        await api.patchConfig('agent.provider_base_url', draft.url)
      }
      if (draft?.key) {
        await api.patchConfig('agent.provider_api_key', draft.key)
      }
      if (draft?.format && draft.format !== (mcCfg?.agent?.provider_api_format ?? (draft.backend === 'opencode' ? 'openai' : 'anthropic'))) {
        await api.patchConfig('agent.provider_api_format', draft.format)
      }
      if (modelSel.length !== savedWhitelist.length || modelSel.some((m, i) => m !== savedWhitelist[i])) {
        await api.patchConfig('agent.model_whitelist', modelSel)
      }
      qc.invalidateQueries({ queryKey: ['kirocrewConfig'] })
      setDraft(null)
    } catch {
      setProviderSaveError(i18nT('pages.settings.chatPanel.failed_to_save_provider'))
    } finally {
      setProviderSaving(false)
    }
  }

  const defaultEffort = mcCfg?.agent?.reasoning_effort ?? ''
  // Effort is only meaningful on reasoning-capable models. Rather than hide the
  // row (which would make the setting look absent), keep it visible and
  // disabled with an explanatory hint.
  const effortSupported = modelSupportsEffort(defaultModel)
  const defaultEffortMut = useMutation({
    mutationFn: (v: string) => api.patchConfig('agent.reasoning_effort', v),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['kirocrewConfig'] }),
    onError: () => setSaveError(i18nT('pages.settings.chatPanel.failed_to_save_default_reasoning_effort')),
  })

  // ── Per-role model defaults (agent.role_models) ──
  // Same picker as the chat default above, but NOT the same precedence:
  // `RoleModels.resolve_model` returns the role's own pin or "auto" and
  // deliberately never falls back to `agent.model`, so unattended work cannot
  // silently ride the interactive flagship on every cycle. "auto" therefore
  // means "the provider picks", not "inherit the chat default" — which is why
  // these rows label it differently from the chat row's Default (auto).
  const backgroundModel = mcCfg?.agent?.role_models?.background || 'auto'
  const subagentModel = mcCfg?.agent?.role_models?.subagent || 'auto'
  // A pinned model the live backend no longer advertises must stay selectable
  // (same reasoning as the chat-default picker), so prepend it when missing.
  const roleModelOptions = (current: string): string[] => {
    const opts = availableModels.map(m => m.name)
    if (!opts.includes(current)) opts.unshift(current)
    return opts
  }
  const roleModelLabels = (opts: string[]): string[] =>
    opts.map(m => (m === 'auto' ? i18nT('pages.settings.chatPanel.role_model_auto') : m))
  const backgroundModelMut = useMutation({
    mutationFn: (v: string) => api.patchConfig('agent.role_models.background', v),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['kirocrewConfig'] }),
    onError: () => setSaveError(i18nT('pages.settings.chatPanel.failed_to_save_role_model')),
  })
  const subagentModelMut = useMutation({
    mutationFn: (v: string) => api.patchConfig('agent.role_models.subagent', v),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['kirocrewConfig'] }),
    onError: () => setSaveError(i18nT('pages.settings.chatPanel.failed_to_save_role_model')),
  })

  // Per-role reasoning effort, paired with each role's model. Empty inherits the
  // the MODEL's own default: `RoleModels.resolve_effort` does not fall back to
  // `agent.reasoning_effort` either. The effort row is only meaningful on a
  // reasoning-capable model, so it disables against a resolved model.
  //
  // KNOWN GAP: the two gates below resolve `auto` to the CHAT default, which
  // `resolve_model` never does — so a role on auto can offer an effort control
  // for a model that role will not run on. Changing it is a behaviour change
  // with a test asserting the current answer, so it is tracked separately
  // rather than folded into this copy fix.
  const backgroundEffort = mcCfg?.agent?.role_efforts?.background ?? ''
  const subagentEffort = mcCfg?.agent?.role_efforts?.subagent ?? ''
  const bgEffortSupported = modelSupportsEffort(backgroundModel !== 'auto' ? backgroundModel : defaultModel)
  const subEffortSupported = modelSupportsEffort(subagentModel !== 'auto' ? subagentModel : defaultModel)
  const effortLabels = EFFORT_LEVELS.map(l => (l === '' ? i18nT('pages.settings.chatPanel.model_default') : effortLabel(l)))
  const backgroundEffortMut = useMutation({
    mutationFn: (v: string) => api.patchConfig('agent.role_efforts.background', v),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['kirocrewConfig'] }),
    onError: () => setSaveError(i18nT('pages.settings.chatPanel.failed_to_save_role_effort')),
  })
  const subagentEffortMut = useMutation({
    mutationFn: (v: string) => api.patchConfig('agent.role_efforts.subagent', v),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['kirocrewConfig'] }),
    onError: () => setSaveError(i18nT('pages.settings.chatPanel.failed_to_save_role_effort')),
  })

  // ── Local chat config (localStorage) ──
  const setChat = useCallback(<K extends keyof ChatConfig>(k: K, v: ChatConfig[K]) => {
    setChatCfg(prev => {
      const next = { ...prev, [k]: v }
      saveChatConfig(next)
      return next
    })
  }, [])

  const setDash = (patch: Partial<DashboardConfig>) => {
    dashMut.mutate({ ...dashCfg, ...patch })
  }

  const dashDisabled = !dashQ.isSuccess

  return (
    <>
      <ErrorNotice message={saveError} onDismiss={() => setSaveError('')} className="mb-4 animate-rise" />
      {dashQ.isError && (
        <div className="mb-4 text-[13px] text-danger">
          {i18nT('pages.settings.chatPanel.failed_to_load_dashboard_config')}{' '}
          <button className="underline cursor-pointer bg-transparent border-none text-danger" onClick={() => dashQ.refetch()}>{i18nT('pages.settings.chatPanel.retry')}</button>
        </div>
      )}
      {mcQ.isError && (
        <div className="mb-4 text-[13px] text-danger">
          {i18nT('pages.settings.chatPanel.failed_to_load_config')}{' '}
          <button className="underline cursor-pointer bg-transparent border-none text-danger" onClick={() => mcQ.refetch()}>{i18nT('pages.settings.chatPanel.retry')}</button>
        </div>
      )}

      <SettingsSection title={i18nT('pages.settings.chatPanel.provider')}>
        <SettingsCard>
          {/* Agent backend switch — each option carries its format subheader. */}
          <div className="flex flex-wrap gap-1.5">
            {BACKEND_OPTIONS.map(o => (
              <button
                key={o.value}
                type="button"
                aria-pressed={effBackend === o.value}
                className={`flex flex-col items-center gap-0.5 px-3 py-2 rounded-lg border text-[13px] cursor-pointer transition-colors ${
                  effBackend === o.value
                    ? 'bg-bg-elevated text-text-strong border-border-strong shadow-sm font-semibold'
                    : 'bg-transparent text-muted border-border font-medium hover:text-text-strong'
                }`}
                onClick={() => switchBackend(o.value)}
              >
                <span>{o.label}</span>
                <span className={`text-[11px] font-normal ${effBackend === o.value ? 'text-muted' : 'text-muted/70'}`}>{o.sub}</span>
              </button>
            ))}
          </div>

          {effBackend === 'opencode' && providerStatus && !providerStatus.opencode && (
            <p className="mt-2 text-[13px] text-warning">
              OpenCode CLI not found. Install it with <code className="text-text-strong">npm i -g opencode-ai</code> or set <code className="text-text-strong">OPENCODE_BIN</code> to its path.
            </p>
          )}
          {effBackend === 'claude_code' && providerStatus && !providerStatus.claude_code && (
            <p className="mt-2 text-[13px] text-warning">
              claude-agent-acp not found. Install it with <code className="text-text-strong">npm i -g @agentclientprotocol/claude-agent-acp</code>.
            </p>
          )}

          {effBackend === 'acp' ? (
            <p className="mt-3 text-[13px] text-muted">{i18nT('pages.settings.chatPanel.provider_managed_by_kiro_cli')}</p>
          ) : (
            <>
              <SettingsSelect
                label={i18nT('pages.settings.chatPanel.provider_preset')}
                value={effPreset}
                options={providerPresets.map(p => p.value)}
                optionLabels={providerPresets.map(p => p.label)}
                onChange={applyPreset}
              />
              <SettingsInput
                label={i18nT('pages.settings.chatPanel.provider_url')}
                aria-label={i18nT('pages.settings.chatPanel.provider_url')}
                value={effUrl}
                onChange={v => setDraft(prev => ({ backend: effBackend, preset: effPreset, url: v, key: prev?.key ?? '', format: prev?.format ?? effFormat }))}
                placeholder="https://…"
              />
              <SettingsInput
                label={i18nT('pages.settings.chatPanel.provider_api_key')}
                aria-label={i18nT('pages.settings.chatPanel.provider_api_key')}
                type="password"
                value={draft?.key ?? ''}
                placeholder={hasStoredKey ? i18nT('pages.settings.chatPanel.provider_api_key_saved') : ''}
                onChange={v => setDraft(prev => ({ backend: effBackend, preset: effPreset, url: prev?.url ?? effUrl, key: v, format: prev?.format ?? effFormat }))}
              />
              <div className="mt-3 flex items-center gap-2">
                <Btn onClick={providerTest} disabled={providerTesting || !effUrl}>
                  {providerTesting ? '…' : i18nT('pages.settings.chatPanel.provider_test')}
                </Btn>
                <Btn primary onClick={providerSave} disabled={providerSaving || (!draft && !whitelistChanged)}>
                  {i18nT('pages.settings.chatPanel.provider_save')}
                </Btn>
                {providerSaveError && <span className="text-[13px] text-danger">{providerSaveError}</span>}
              </div>
              {providerTestResult && (
                <div className={`mt-2 text-[13px] ${providerTestResult.ok ? 'text-accent' : 'text-danger'}`}>
                  {providerTestResult.ok
                    ? `${i18nT('pages.settings.chatPanel.provider_test_ok')} (${providerTestResult.models?.length ?? 0})`
                    : `${i18nT('pages.settings.chatPanel.provider_test_failed')}: ${providerTestResult.message}`}
                </div>
              )}
              {(providerTestResult?.models?.length || savedWhitelist.length > 0) && (
                <div className="mt-3">
                  <div className="text-[13px] font-semibold text-text-strong mb-1">{i18nT('pages.settings.chatPanel.provider_models')}</div>
                  <div className="max-h-40 overflow-y-auto rounded border border-border p-2 grid grid-cols-1 gap-1">
                    {(providerTestResult?.models ?? savedWhitelist).map(id => (
                      <label key={id} className="flex items-center gap-2 text-[13px] cursor-pointer">
                        <input type="checkbox" checked={modelSel.includes(id)} onChange={() => toggleWhitelistModel(id)} className="accent-accent" />
                        <span className="font-mono text-text truncate">{id}</span>
                      </label>
                    ))}
                  </div>
                </div>
              )}
            </>
          )}
        </SettingsCard>
      </SettingsSection>

      <SettingsSection title={i18nT('pages.settings.chatPanel.model')}>
        {/* Grouped by role so each block reads as "which model + how hard it
            thinks" for one kind of work, rather than six stacked selects.
            Chat is the interactive default; Background and Sub-agents inherit it
            when left on Auto. */}
        <SettingsCard>
          <div className="text-[13px] font-semibold text-text-strong">{i18nT('pages.settings.chatPanel.role_chat')}</div>
          <SettingsSelect
            label={i18nT('pages.settings.chatPanel.default_model')}
            description={i18nT('pages.settings.chatPanel.which_model_new_sessions_start_with_pick_a_model')}
            hint={i18nT('pages.settings.chatPanel.default_defers_to_your_agent_config_and_then_to')}
            value={defaultModel}
            options={modelOptions}
            optionLabels={modelOptions.map(m => (m === 'auto' ? i18nT('pages.settings.chatPanel.default_auto') : m))}
            onChange={v => defaultModelMut.mutate(v)}
            disabled={!mcQ.isSuccess}
          />
          <SettingsSelect
            label={i18nT('pages.settings.chatPanel.default_reasoning_effort')}
            description={i18nT('pages.settings.chatPanel.how_long_models_think_before_answering_by_defaul')}
            hint={
              effortSupported
                ? i18nT('pages.settings.chatPanel.model_default_applies_no_override_the_model_pick')
                : i18nT('pages.settings.chatPanel.effort_needs_reasoning_model')
            }
            value={defaultEffort}
            options={[...EFFORT_LEVELS]}
            optionLabels={effortLabels}
            onChange={v => defaultEffortMut.mutate(v)}
            disabled={!mcQ.isSuccess || !effortSupported}
          />
        </SettingsCard>

        <SettingsCard index={1}>
          <div className="text-[13px] font-semibold text-text-strong">{i18nT('pages.settings.chatPanel.role_background')}</div>
          <div className="text-[12px] text-muted -mt-0.5">{i18nT('pages.settings.chatPanel.model_for_background_lite_heartbeat_work')}</div>
          <SettingsSelect
            label={i18nT('pages.settings.chatPanel.background_model')}
            hint={i18nT('pages.settings.chatPanel.role_model_auto_hint')}
            value={backgroundModel}
            options={roleModelOptions(backgroundModel)}
            optionLabels={roleModelLabels(roleModelOptions(backgroundModel))}
            onChange={v => backgroundModelMut.mutate(v)}
            disabled={!mcQ.isSuccess}
          />
          <SettingsSelect
            label={i18nT('pages.settings.chatPanel.background_effort')}
            hint={i18nT('pages.settings.chatPanel.role_effort_hint')}
            value={backgroundEffort}
            options={[...EFFORT_LEVELS]}
            optionLabels={effortLabels}
            onChange={v => backgroundEffortMut.mutate(v)}
            disabled={!mcQ.isSuccess || !bgEffortSupported}
          />
        </SettingsCard>

        <SettingsCard index={2}>
          <div className="text-[13px] font-semibold text-text-strong">{i18nT('pages.settings.chatPanel.role_subagents')}</div>
          <div className="text-[12px] text-muted -mt-0.5">{i18nT('pages.settings.chatPanel.model_for_spawned_sub_agents')}</div>
          <SettingsSelect
            label={i18nT('pages.settings.chatPanel.subagent_model')}
            hint={i18nT('pages.settings.chatPanel.role_model_auto_hint')}
            value={subagentModel}
            options={roleModelOptions(subagentModel)}
            optionLabels={roleModelLabels(roleModelOptions(subagentModel))}
            onChange={v => subagentModelMut.mutate(v)}
            disabled={!mcQ.isSuccess}
          />
          <SettingsSelect
            label={i18nT('pages.settings.chatPanel.subagent_effort')}
            hint={i18nT('pages.settings.chatPanel.role_effort_hint')}
            value={subagentEffort}
            options={[...EFFORT_LEVELS]}
            optionLabels={effortLabels}
            onChange={v => subagentEffortMut.mutate(v)}
            disabled={!mcQ.isSuccess || !subEffortSupported}
          />
        </SettingsCard>

        <SettingsCard>
          <div className="text-[13px] font-semibold text-text-strong">{i18nT('pages.settings.chatPanel.throttle_fallback')}</div>
          <div className="text-[12px] text-muted -mt-0.5">{i18nT('pages.settings.chatPanel.model_tried_when_your_current_model_stays_rate_li')}</div>
          <SettingsSelect
            label={i18nT('pages.settings.chatPanel.fallback_model')}
            hint={i18nT('pages.settings.chatPanel.fallback_auto_hint')}
            value={fallbackModel}
            options={fallbackModelOptions(fallbackModel)}
            optionLabels={fallbackModelLabels(fallbackModelOptions(fallbackModel))}
            onChange={v => fallbackMut.mutate(v)}
            disabled={!mcQ.isSuccess}
            configKey="agent.fallback_model"
          />
        </SettingsCard>
      </SettingsSection>

      <SettingsSection title={i18nT('pages.settings.chatPanel.about_you')}>
        <SettingsCard index={3}>
          <SettingsSelect
            label={i18nT('pages.settings.chatPanel.your_role')}
            description={i18nT('pages.settings.chatPanel.kiro_matches_vocabulary_and_examples_to_your_pro')}
            value={userRole}
            options={ROLE_OPTIONS}
            optionLabels={roleLabels()}
            onChange={v => profileMut.mutate({ path: 'dashboard.user_role', value: v })}
          />
          {userRole === 'other' && (
            <SettingsInput
              label={i18nT('pages.settings.chatPanel.describe_your_role')}
              aria-label={i18nT('pages.settings.chatPanel.describe_your_role')}
              description={i18nT('pages.settings.chatPanel.kiro_quotes_this_back_to_itself_when_calibrating')}
              placeholder={i18nT('pages.settings.chatPanel.e_g_solutions_architect_sre_founder')}
              value={localRoleOther}
              onChange={v => setLocalRoleOther(capRoleOther(v))}
              onBlur={commitRoleOther}
            />
          )}
          <SettingsSelect
            label={i18nT('pages.settings.chatPanel.technical_comfort')}
            description={i18nT('pages.settings.chatPanel.sets_how_deep_explanations_go_plain_language_vs')}
            value={userTechLevel}
            options={TECH_OPTIONS}
            optionLabels={techLabels()}
            onChange={v => profileMut.mutate({ path: 'dashboard.user_technical_level', value: v })}
          />
        </SettingsCard>
      </SettingsSection>

      <SettingsSection title={i18nT('pages.settings.chatPanel.power')}>
        <SettingsCard index={4}>
          <SettingsToggle
            label={i18nT('pages.settings.chatPanel.prevent_sleep_while_running')}
            description={i18nT('pages.settings.chatPanel.keep_your_computer_awake_while_a_task_is_running')}
            checked={preventSleep}
            onChange={v => preventSleepMut.mutate(v)}
            disabled={!mcQ.isSuccess}
            configKey="dashboard.prevent_sleep"
          />
        </SettingsCard>
      </SettingsSection>

      <SettingsSection title={i18nT('pages.settings.chatPanel.composer')}>
        <SettingsCard index={5}>
          <SettingsSelect
            label={i18nT('pages.settings.chatPanel.send_shortcut')}
            description={chatCfg.sendOnEnter === 'enter' ? i18nT('pages.settings.chatPanel.shift_enter_for_newline') : chatCfg.sendOnEnter === 'ctrl-enter' ? i18nT('pages.settings.chatPanel.enter_for_newline') : i18nT('pages.settings.chatPanel.mod_enter_for_newline', { mod: isMac ? '⌘' : 'Ctrl' })}
            value={chatCfg.sendOnEnter}
            options={['enter', 'ctrl-enter', 'enter-ctrl-newline']}
            optionLabels={[i18nT('pages.settings.chatPanel.enter_sends'), i18nT('pages.settings.chatPanel.mod_enter_sends', { mod: isMac ? '⌘' : 'Ctrl' }), i18nT('pages.settings.chatPanel.enter_sends_mod_enter_newline', { mod: isMac ? '⌘' : 'Ctrl' })]}
            onChange={v => setChat('sendOnEnter', v as SendMode)}
          />
          <SettingsToggle label={i18nT('pages.settings.chatPanel.quick_send')} description={i18nT('pages.settings.chatPanel.click_a_suggested_reply_to_send_it_instantly', { mod: isMac ? '⇧' : 'Shift' })} checked={dashCfg.quick_send} onChange={v => setDash({ quick_send: v })} disabled={dashDisabled} />
          <SettingsToggle label={i18nT('pages.settings.chatPanel.merge_queued_messages')} description={i18nT('pages.settings.chatPanel.combine_follow_up_messages_into_a_single_labeled')} checked={dashCfg.merge_queued_messages} onChange={v => setDash({ merge_queued_messages: v })} disabled={dashDisabled} />
          <SettingsButtonGroup label={i18nT('pages.settings.chatPanel.follow_up_bar_layout')} description={i18nT('pages.settings.chatPanel.multiline_wraps_suggestions_onto_multiple_rows_s')} value={chatCfg.followUpLayout} options={[{ value: "multiline", label: i18nT('pages.settings.chatPanel.multiline') }, { value: "scroll", label: i18nT('pages.settings.chatPanel.single_line') }]} onChange={v => setChat('followUpLayout', v as ChatConfig['followUpLayout'])} />
          <SettingsInput
            label={i18nT('pages.settings.chatPanel.soft_stop_budget_seconds')}
            aria-label={i18nT('pages.settings.chatPanel.soft_stop_budget_seconds')}
            hint={i18nT('pages.settings.chatPanel.how_long_to_wait_for_the_agent_to_honor_a_stop_p')}
            type="number"
            value={localBudget}
            min={SOFT_STOP_MIN}
            max={SOFT_STOP_MAX}
            step={0.5}
            onChange={setLocalBudget}
            onBlur={() => {
              const n = parseFloat(localBudget)
              if (isNaN(n) || n < SOFT_STOP_MIN || n > SOFT_STOP_MAX) {
                setLocalBudget(String(mcCfg?.agent?.soft_stop_budget_secs ?? SOFT_STOP_DEFAULT))
                return
              }
              budgetMut.mutate(n)
            }}
            disabled={!mcQ.isSuccess}
          />
        </SettingsCard>
      </SettingsSection>

      <SettingsSection title={i18nT('pages.settings.chatPanel.messages')}>
        <SettingsCard index={6}>
          <SettingsButtonGroup
            label={i18nT('pages.settings.chatPanel.text_streaming_style')}
            description={i18nT('pages.settings.chatPanel.immediate_mode_shows_raw_chunks_as_they_arrive_s')}
            value={chatCfg.streamMode}
            options={[{ value: 'immediate', label: i18nT('pages.settings.chatPanel.immediate') }, { value: 'smooth', label: i18nT('pages.settings.chatPanel.smooth') }]}
            onChange={v => setChat('streamMode', v as ChatConfig['streamMode'])}
          />
          <SettingsToggle label={i18nT('pages.settings.chatPanel.show_timestamps')} description={i18nT('pages.settings.chatPanel.display_time_on_each_message')} checked={chatCfg.showTimestamps} onChange={v => setChat('showTimestamps', v)} />
          <SettingsButtonGroup label={i18nT('pages.settings.chatPanel.content_width')} description={i18nT('pages.settings.chatPanel.compact_is_the_original_view_comfortable_and_ful')} value={chatCfg.contentWidth} options={[{ value: "compact", label: i18nT('pages.settings.chatPanel.compact') }, { value: "comfortable", label: i18nT('pages.settings.chatPanel.comfortable') }, { value: "full", label: i18nT('pages.settings.chatPanel.full') }]} onChange={v => setChat('contentWidth', v as ContentWidth)} />
          <SettingsToggle label={i18nT('pages.settings.chatPanel.show_thinking_inline')} description={i18nT('pages.settings.chatPanel.show_intermediate_reasoning_text_between_tool_ca')} checked={!chatCfg.collapseAllSteps} onChange={v => setChat('collapseAllSteps', !v)} />
          <SettingsToggle label={i18nT('pages.settings.chatPanel.pin_last_prompt')} description={i18nT('pages.settings.chatPanel.pin_last_prompt_desc')} checked={chatCfg.pinLastPrompt} onChange={v => setChat('pinLastPrompt', v)} />
          <SettingsToggle label={i18nT('pages.settings.chatPanel.simplified_tool_call_names')} description={i18nT('pages.settings.chatPanel.when_enabled_inline_tool_pills_show_simplified_t')} checked={chatCfg.simplifiedToolNames} onChange={v => setChat('simplifiedToolNames', v)} />
          <SettingsSelect label={i18nT('pages.settings.chatPanel.file_change_chips')} description={i18nT('pages.settings.chatPanel.how_file_diff_chips_appear_below_assistant_messa')} value={chatCfg.fileChipStyle} options={['expanded', 'minimal']} optionLabels={[i18nT('pages.settings.chatPanel.expanded_icon_name_stats'), i18nT('pages.settings.chatPanel.minimal_stats_only_name_on_hover')]} onChange={v => setChat('fileChipStyle', v as ChatConfig['fileChipStyle'])} />
          <SettingsToggle label={i18nT('pages.settings.chatPanel.link_previews')} description={i18nT('pages.settings.chatPanel.show_a_favicon_and_page_title_instead_of_the_raw')} checked={dashCfg.link_previews} onChange={v => setDash({ link_previews: v })} disabled={dashDisabled} />
          <SettingsSelect label={i18nT('pages.settings.chatPanel.widget_density')} description={i18nT('pages.settings.chatPanel.how_aggressively_the_agent_uses_inline_widgets_f')} value={dashCfg.widget_density ?? 'more'} options={['more', 'less']} optionLabels={[i18nT('pages.settings.chatPanel.more_encourage_widgets'), i18nT('pages.settings.chatPanel.less_only_when_needed')]} onChange={v => setDash({ widget_density: v as 'more' | 'less' })} disabled={dashDisabled} />
          <SettingsToggle label={i18nT('pages.settings.chatPanel.mcp_apps_in_side_panel')} description={i18nT('pages.settings.chatPanel.render_interactive_mcp_apps_in_the_right_side_pa')} checked={dashCfg.mcp_app_panel} onChange={v => setDash({ mcp_app_panel: v })} disabled={dashDisabled} />
          <SettingsToggle label={i18nT('pages.settings.chatPanel.auto_open_git_panel')} description={i18nT('pages.settings.chatPanel.expand_the_side_panel_to_the_git_tab_each_time_yo')} checked={dashCfg.auto_open_git_panel} onChange={v => setDash({ auto_open_git_panel: v })} disabled={dashDisabled} />
          <SettingsSelect label={i18nT('pages.settings.chatPanel.response_verbosity')} description={i18nT('pages.settings.chatPanel.how_terse_the_agent_s_prose_is_ultra_concise_cap')} value={asVerbosity(dashCfg.verbosity)} options={VERBOSITY_OPTIONS} optionLabels={[i18nT('pages.settings.chatPanel.default_normal_length'), i18nT('pages.settings.chatPanel.concise_trim_filler'), i18nT('pages.settings.chatPanel.ultra_concise_3_sentences')]} onChange={v => setDash({ verbosity: v as VerbosityLevel })} disabled={dashDisabled} />
          <SettingsToggle label={i18nT('pages.settings.chatPanel.show_context_percentage')} description={i18nT('pages.settings.chatPanel.display_usage_percentage_next_to_the_context_pro')} checked={chatCfg.showContextPct} onChange={v => setChat('showContextPct', v)} />
          <SettingsToggle label={i18nT('pages.settings.chatPanel.show_token_usage')} description={i18nT('pages.settings.chatPanel.display_used_and_total_tokens_next_to_the_contex')} checked={chatCfg.showContextTokens} onChange={v => setChat('showContextTokens', v)} />
          <SettingsToggle label={i18nT('pages.settings.chatPanel.feature_tips')} description={tipsConfigOff ? i18nT('pages.settings.chatPanel.disabled_by_instance_config_tips_enabled_false') : i18nT('pages.settings.chatPanel.show_occasional_feature_discovery_tips_above_the')} checked={!!tipsQ.data && tipsQ.data.enabled_config && !tipsQ.data.opted_out} onChange={v => tipsMut.mutate(v)} disabled={tipsConfigOff || tipsQ.isLoading || tipsQ.isError} />
          <SettingsToggle label={i18nT('pages.settings.chatPanel.folder_suggestions')} description={i18nT('pages.settings.chatPanel.offer_to_file_a_new_session_into_a_matching_fold')} checked={dashCfg.folder_suggestions_enabled} onChange={v => setDash({ folder_suggestions_enabled: v })} disabled={dashDisabled} />
        </SettingsCard>
      </SettingsSection>

      <SettingsSection title={i18nT('pages.settings.chatPanel.sessions')}>
        <SettingsCard index={7}>
          <SettingsToggle label={i18nT('pages.settings.chatPanel.split_view_session_grid')} description={i18nT('pages.settings.chatPanel.opt_in_split_the_chat_into_resizable_session_pan', { mod: isMac ? '⌘' : 'Ctrl' })} checked={dashCfg.session_grid} onChange={v => setDash({ session_grid: v })} disabled={dashDisabled} />
          <SettingsToggle label={i18nT('pages.settings.chatPanel.history_expanded')} description={i18nT('pages.settings.chatPanel.expand_history_sidebar_by_default')} checked={chatCfg.historyExpanded} onChange={v => setChat('historyExpanded', v)} />
          <SettingsToggle label={i18nT('pages.settings.chatPanel.confirm_before_closing_session')} description={i18nT('pages.settings.chatPanel.show_a_confirmation_dialog_when_closing_a_sessio')} checked={chatCfg.confirmCloseSession} onChange={v => setChat('confirmCloseSession', v)} />
          <SettingsToggle label={i18nT('pages.settings.chatPanel.default_to_autopilot_mode')} description={i18nT('pages.settings.chatPanel.new_sessions_start_in_autopilot_mode_plan_approv')} checked={chatCfg.defaultAutopilot} onChange={v => setChat('defaultAutopilot', v)} />
          <SettingsToggle label={i18nT('pages.settings.chatPanel.tail_only_fork')} description={i18nT('pages.settings.chatPanel.fork_keeps_only_the_messages_after_the_chosen_po')} checked={dashCfg.tail_fork_enabled} onChange={v => setDash({ tail_fork_enabled: v })} disabled={dashDisabled} />
          <SettingsToggle label={i18nT('pages.settings.chatPanel.restore_sessions')} description={i18nT('pages.settings.chatPanel.re_open_recently_active_sessions_on_startup')} checked={dashCfg.restore_sessions} onChange={v => setDash({ restore_sessions: v })} disabled={dashDisabled} />
          {dashCfg.restore_sessions && (
            <SettingsSelect label={i18nT('pages.settings.chatPanel.restore_window')} description={i18nT('pages.settings.chatPanel.time_window_for_session_restoration')} value={String(dashCfg.restore_window_minutes)} options={RESTORE_OPTIONS} optionLabels={restoreLabels()} onChange={v => setDash({ restore_window_minutes: Number(v) })} disabled={dashDisabled} />
          )}
          <SettingsToggle label={i18nT('pages.settings.chatPanel.session_summaries')} description={i18nT('pages.settings.chatPanel.summarize_each_session_by_intent_in_the_right_pa')} checked={summaryEnabled} onChange={v => summaryMut.mutate(v)} disabled={!mcQ.isSuccess || summaryMut.isPending} />
        </SettingsCard>
      </SettingsSection>

      <SettingsSection title="Vision">
        <SettingsCard>
          <SettingsSelect
            label="Image input mode"
            description="How user-attached images are presented to the model."
            value={visionMode}
            options={[...IMAGE_MODE_OPTIONS]}
            optionLabels={IMAGE_MODE_LABELS}
            onChange={v => visionModeMut.mutate(v)}
            disabled={!mcQ.isSuccess}
          />
          <SettingsSelect
            label="On text-only models"
            description="What to do when the active model cannot take images."
            value={visionRedirect}
            options={[...REDIRECT_OPTIONS]}
            optionLabels={[...REDIRECT_LABELS]}
            onChange={v => visionRedirectMut.mutate(v)}
            disabled={!mcQ.isSuccess}
          />
          <SettingsSelect
            label="Vision fallback model"
            description="Picker-spelling id the describe/switch path uses (must be vision-capable)."
            value={visionFallback}
            options={fallbackOptions.includes(visionFallback) ? fallbackOptions : [visionFallback, ...fallbackOptions]}
            optionLabels={fallbackOptions.includes(visionFallback) ? fallbackOptions : [visionFallback, ...fallbackOptions]}
            onChange={v => visionFallbackMut.mutate(v)}
            disabled={!mcQ.isSuccess}
          />
          <p className="text-[12px] text-muted">Vision section mirrors the picker grouping (Vision — image input with an Image badge, then Text). On text-only models the active policy's fallback model is used; the text input stays the same — images ride the same composer attachment.</p>
        </SettingsCard>
      </SettingsSection>

      <SettingsSection title="Security & Shim">
        <SettingsCard>
          <SettingsToggle
            label="Safe mode (local routers only)"
            description="Refuse sessions whose provider_base_url resolves to a public address. Loopback, LAN and Tailscale ranges stay allowed."
            checked={safeMode}
            onChange={v => safeModeMut.mutate(v)}
            disabled={!mcQ.isSuccess || safeModeMut.isPending}
          />
          <SettingsToggle
            label="Built-in OpenAI shim"
            description="Translate Anthropic requests to OpenAI chat completions so provider_base_url can target Ollama, llama.cpp or DeepSeek directly. Gateway restart applies."
            checked={useShim}
            onChange={v => useShimMut.mutate(v)}
            disabled={!mcQ.isSuccess || useShimMut.isPending}
          />
          <SettingsInput
            label="Shim: OpenAI base URL"
            aria-label="Shim: OpenAI base URL"
            description="Forward target for the built-in shim (OpenAI-compatible /v1)."
            value={localShimUrl}
            placeholder="http://127.0.0.1:11434/v1"
            onChange={v => setLocalShimUrl(v)}
            onBlur={() => { if (localShimUrl && localShimUrl !== shimUrlCfg) shimUrlMut.mutate(localShimUrl) }}
            onKeyDown={e => { if (e.key === 'Enter' && localShimUrl && localShimUrl !== shimUrlCfg) { e.preventDefault(); shimUrlMut.mutate(localShimUrl) } }}
            disabled={!mcQ.isSuccess}
          />
          <p className="text-[12px] text-muted">With the shim on, set Provider URL to http://127.0.0.1:8391 — requests are translated and forwarded to the base URL above. `kirocrew doctor` probes reachability and auth.</p>
        </SettingsCard>
      </SettingsSection>

      <SettingsSection title={i18nT('pages.settings.chatPanel.context')}>
        <SettingsCard index={8}>
          <SettingsSelect
            label={i18nT('pages.settings.chatPanel.auto_compact_threshold')}
            description={i18nT('pages.settings.chatPanel.context_usage_at_which_auto_compaction_triggers')}
            value={String(mcCfg?.session?.autocompact_pct ?? 70)}
            options={COMPACT_OPTIONS}
            optionLabels={COMPACT_LABELS}
            onChange={v =>
              api.patchConfig('session.autocompact_pct', Number(v))
                .then(() => qc.invalidateQueries({ queryKey: ['kirocrewConfig'] }))
                .catch(() => setSaveError(i18nT('pages.settings.chatPanel.failed_to_save_auto_compact_threshold')))
            }
            disabled={!mcQ.isSuccess}
            configKey="session.autocompact_pct"
          />
        </SettingsCard>
      </SettingsSection>

      <SettingsSection title={i18nT('pages.settings.chatPanel.subagents')}>
        <SettingsCard index={10}>
          <SettingsSelect
            label={i18nT('pages.settings.chatPanel.completion_event_truncation')}
            description={i18nT('pages.settings.chatPanel.which_part_of_a_subagent_s_stream_to_keep_when_i')}
            value={mcCfg?.agent?.completion_keep ?? 'head'}
            options={COMPLETION_KEEP_OPTIONS}
            optionLabels={completionKeepLabels()}
            onChange={v => keepModeMut.mutate(v as CompletionKeepMode)}
            disabled={!mcQ.isSuccess}
          />
          <SettingsInput
            label={i18nT('pages.settings.chatPanel.completion_event_characters')}
            aria-label={i18nT('pages.settings.chatPanel.completion_event_characters_2')}
            hint={i18nT('pages.settings.chatPanel.maximum_characters_retained_in_the_completion_ev', { n: COMPLETION_KEEP_CHARS_DEFAULT })}
            type="number"
            value={localKeepChars}
            min={COMPLETION_KEEP_CHARS_MIN}
            max={COMPLETION_KEEP_CHARS_MAX}
            step={500}
            onChange={setLocalKeepChars}
            onBlur={() => {
              const n = parseInt(localKeepChars, 10)
              if (
                isNaN(n) ||
                n < COMPLETION_KEEP_CHARS_MIN ||
                n > COMPLETION_KEEP_CHARS_MAX
              ) {
                setLocalKeepChars(
                  String(mcCfg?.agent?.completion_keep_chars ?? COMPLETION_KEEP_CHARS_DEFAULT)
                )
                return
              }
              keepCharsMut.mutate(n)
            }}
            disabled={!mcQ.isSuccess}
          />
        </SettingsCard>
      </SettingsSection>

    </>
  )
}
