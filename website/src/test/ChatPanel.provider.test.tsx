// SettingsSelect wraps Radix Select, which needs pointer APIs jsdom lacks —
// use the same lightweight mock the SettingsSelect unit tests use.
vi.mock('@radix-ui/react-select', async () => await import('./__mocks__/@radix-ui/react-select'))

import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import React from 'react'

const { patchConfigMock, kirocrewConfigMock, modelsMock } = vi.hoisted(() => ({
  patchConfigMock: vi.fn(() => Promise.resolve({})),
  kirocrewConfigMock: vi.fn(() =>
    Promise.resolve({ agent: { provider: 'claude_code', provider_base_url: '', provider_api_key: '', model: 'auto' } })
  ),
  modelsMock: vi.fn(() => Promise.resolve([{ model_name: 'auto', description: 'Default' }])),
}))

vi.mock('../api/client', () => ({
  api: {
    dashboardConfig: () => Promise.resolve({ restore_sessions: false, restore_window_minutes: 30, merge_queued_messages: false, widget_density: 'more' }),
    kirocrewConfig: kirocrewConfigMock,
    models: modelsMock,
    patchConfig: patchConfigMock,
    updateDashboardConfig: () => Promise.resolve({}),
    tipsStatus: () => Promise.resolve({ enabled_config: true, opted_out: false }),
    tipsFeedback: () => Promise.resolve({ ok: true }),
  },
}))

import { ChatPanel } from '../pages/settings/ChatPanel'

function wrap(ui: React.ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>)
}

const seed = (agent: Record<string, unknown>) =>
  kirocrewConfigMock.mockImplementation(() => Promise.resolve({ agent }) as never)

async function openSelect(label: string) {
  const trigger = await screen.findByRole('combobox', { name: label })
  await waitFor(() => expect(trigger).not.toHaveAttribute('data-disabled'))
  fireEvent.click(trigger)
  return screen.getAllByRole('option')
}

describe('ChatPanel Provider section', () => {
  it('renders the three backends with their format subheaders', async () => {
    wrap(<ChatPanel />)
    const claude = await screen.findByRole('button', { name: /Claude Code/i })
    expect(claude.textContent).toContain('anthropic endpoint')
    expect(screen.getByRole('button', { name: /OpenCode/i }).textContent).toContain('OpenAI-compatible endpoint')
    expect(screen.getByRole('button', { name: /kiro-native/i }).textContent).toContain('kiro-cli backend')
  })

  it('prefills the URL from a preset and saves provider fields', async () => {
    seed({ provider: 'claude_code', provider_base_url: '', provider_api_key: '', model: 'auto' })
    patchConfigMock.mockClear()
    wrap(<ChatPanel />)

    // Switch to OpenCode backend.
    fireEvent.click(await screen.findByRole('button', { name: /OpenCode/i }))

    // Pick the Groq preset — URL field must prefill.
    const options = await openSelect('Preset')
    fireEvent.click(options.find(o => o.textContent === 'Groq')!)

    const urlInput = await screen.findByLabelText('Base URL') as HTMLInputElement
    expect(urlInput.value).toBe('https://api.groq.com/openai')

    // Type a key and save.
    const keyInput = screen.getByLabelText('API key') as HTMLInputElement
    fireEvent.change(keyInput, { target: { value: 'sk-groq-1' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save provider' }))

    await waitFor(() => {
      expect(patchConfigMock).toHaveBeenCalledWith('agent.provider', 'opencode')
      expect(patchConfigMock).toHaveBeenCalledWith('agent.provider_base_url', 'https://api.groq.com/openai')
      expect(patchConfigMock).toHaveBeenCalledWith('agent.provider_api_key', 'sk-groq-1')
    })
  })

  it('shows a saved-key placeholder instead of the real key', async () => {
    seed({ provider: 'claude_code', provider_base_url: 'http://localhost:8317', provider_api_key: 'sk-stored', model: 'auto' })
    wrap(<ChatPanel />)
    const keyInput = await screen.findByLabelText('API key') as HTMLInputElement
    expect(keyInput.value).toBe('')
    expect(keyInput.placeholder).toContain('saved')
  })
})
