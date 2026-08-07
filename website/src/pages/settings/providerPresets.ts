// Provider presets for Settings > Chat > Provider.
//
// Each preset only PREFILLS the base-URL field (and flags whether an API key
// is expected) — it never auto-saves. The lists are split by agent backend:
// claude_code presets are Anthropic-compatible (`{base}/v1/messages`,
// live-verified 401/400), opencode presets are OpenAI-compatible
// (`{base}/v1/chat/completions`, live-verified 401/400). kiro-native (acp)
// has no presets — its router is managed by kiro-cli.

export type AgentBackend = 'acp' | 'claude_code' | 'opencode'

export interface BackendOption {
  value: AgentBackend
  label: string
  /** Short format subheader shown under the option label. */
  sub: string
}

export interface ProviderPreset {
  value: string
  label: string
  url: string
  /** OpenCode provider adapter format (ignored by claude_code). */
  format?: 'anthropic' | 'openai'
  keyRequired?: boolean
}

export const BACKEND_OPTIONS: BackendOption[] = [
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
    // OpenCode's own providers (Anthropic-format gateways).
    { value: 'opencode-zen', label: 'OpenCode Zen', url: 'https://opencode.ai/zen', format: 'anthropic', keyRequired: true },
    { value: 'opencode-go', label: 'OpenCode Go', url: 'https://opencode.ai/zen/go', format: 'anthropic', keyRequired: true },
    // Anthropic-compatible gateways.
    { value: 'commandcode', label: 'commandcode.ai', url: 'https://commandcode.ai', format: 'anthropic', keyRequired: true },
    // Ollama Cloud's Anthropic endpoint rejects cloud API keys; use OpenAI wire.
    // URL is bare host (https://ollama.com) — the backend normalizes to /v1
    // for the OpenCode AI-SDK adapter, and the test endpoint appends /v1/models.
    { value: 'ollama-cloud', label: 'Ollama Cloud', url: 'https://ollama.com', format: 'openai', keyRequired: true },
    { value: 'anthropic', label: 'Anthropic', url: 'https://api.anthropic.com', format: 'anthropic', keyRequired: true },
    { value: 'openrouter', label: 'OpenRouter', url: 'https://openrouter.ai/api', format: 'anthropic', keyRequired: true },
    { value: 'xai-a', label: 'xAI (Anthropic)', url: 'https://api.x.ai', format: 'anthropic', keyRequired: true },
    { value: 'mistral-a', label: 'Mistral (Anthropic)', url: 'https://api.mistral.ai', format: 'anthropic', keyRequired: true },
    { value: 'deepseek-a', label: 'DeepSeek (Anthropic)', url: 'https://api.deepseek.com', format: 'anthropic', keyRequired: true },
    { value: 'together-a', label: 'Together (Anthropic)', url: 'https://api.together.xyz', format: 'anthropic', keyRequired: true },
    // OpenAI-compatible endpoints.
    { value: 'openai', label: 'OpenAI', url: 'https://api.openai.com', format: 'openai', keyRequired: true },
    { value: 'groq', label: 'Groq', url: 'https://api.groq.com/openai', format: 'openai', keyRequired: true },
    { value: 'deepseek', label: 'DeepSeek (OpenAI)', url: 'https://api.deepseek.com', format: 'openai', keyRequired: true },
    { value: 'xai', label: 'xAI (OpenAI)', url: 'https://api.x.ai', format: 'openai', keyRequired: true },
    { value: 'ollama', label: 'Ollama (OpenAI)', url: 'https://ollama.com', format: 'openai', keyRequired: true },
    { value: 'together', label: 'Together (OpenAI)', url: 'https://api.together.xyz', format: 'openai', keyRequired: true },
    { value: 'mistral', label: 'Mistral (OpenAI)', url: 'https://api.mistral.ai', format: 'openai', keyRequired: true },
  ],
}

/** True when the backend has a URL/key to configure (kiro-native does not). */
export function backendNeedsProviderConfig(backend: AgentBackend): boolean {
  return backend === 'claude_code' || backend === 'opencode'
}
