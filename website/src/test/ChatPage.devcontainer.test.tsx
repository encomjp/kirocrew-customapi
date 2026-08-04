/**
 * The Dev Container trust prompt is gated on the FEATURE, not just the file.
 *
 * `agent.devcontainer` defaults to `off`, and a repo ships `devcontainer.json`
 * regardless — so `has_config` alone made the card ask for a decision that could
 * not change anything, which is the usability defect this pins. `enabled` on the
 * status response is the config mode, and it gates both the prompt and the
 * composer's running-container chip.
 */
import React from 'react'
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { configureStore } from '@reduxjs/toolkit'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import chatReducer from '../store/chatSlice'
import dashboardReducer from '../store/dashboardSlice'
import notificationsReducer from '../store/notificationsSlice'
import { ThemeProvider } from '../hooks/useTheme'

const devcontainerStatus = vi.fn()
const devcontainerTrust = vi.fn().mockResolvedValue({ trusted: true, digest: 'abcdef0123456789' })

vi.mock('react-virtuoso', () => ({ Virtuoso: ({ data, itemContent }: { data?: unknown[]; itemContent: (i: number, d: unknown) => React.ReactNode }) => <div data-testid="virtuoso">{data?.map((d: unknown, i: number) => <div key={i}>{itemContent(i, d)}</div>)}</div> }))
vi.mock('../api/client', () => ({
  api: {
    chatSlots: vi.fn().mockResolvedValue([]),
    chatSlotDetail: vi.fn().mockResolvedValue({ messages: [], running: false, has_more: false, total: 0 }),
    chatHistory: vi.fn().mockResolvedValue({ sessions: [] }),
    models: vi.fn().mockResolvedValue([]),
    agents: vi.fn().mockResolvedValue([]),
    agentDetail: vi.fn().mockResolvedValue({}),
    workspaces: vi.fn().mockResolvedValue({ workspaces: [] }),
    slackChannels: vi.fn().mockResolvedValue([]),
    spawnList: vi.fn().mockResolvedValue({ agents: [] }),
    projectGit: vi.fn().mockResolvedValue({ branch: 'main', detached: false, head: '' }),
    devcontainerStatus: (project: string) => devcontainerStatus(project),
    devcontainerTrust: (project: string, digest: string) => devcontainerTrust(project, digest),
    devcontainerConfig: vi.fn().mockResolvedValue({
      config_path: '/repo/.devcontainer/devcontainer.json',
      digest: 'abcdef0123456789',
      raw: '{"name": "repo"}',
      name: 'repo',
      image: null,
      trusted: false,
    }),
  },
  SEARCH_MIN_CHARS: 2,
}))
vi.mock('../hooks/useVoiceInput', () => ({ useVoiceInput: () => ({ recording: false, transcribing: false, toggle: vi.fn() }), voiceInputSupported: false }))
vi.mock('../hooks/useBranding', () => ({ useBranding: () => ({ botName: 'Test', avatar: '' }) }))
vi.mock('../hooks/useAgents', () => ({ useAgents: () => ({ agents: [], defaultAgent: 'default' }) }))
vi.mock('../components/MarkdownRenderer', () => ({ default: ({ content }: { content: string }) => <span>{content}</span> }))
vi.mock('../components/WelcomeView', () => ({ default: () => null }))
vi.mock('../components/MarkdownPanel', () => ({ default: () => null }))
vi.mock('../pages/chat/ActivityViewer', () => ({ default: () => null }))
vi.mock('../components/DetailPanel', () => ({ default: () => null }))
vi.mock('../hooks/useWebSocket', () => ({ useWebSocket: () => ({ subscribeLogs: () => {} }) }))

Object.defineProperty(window, 'matchMedia', {
  writable: true,
  value: vi.fn().mockReturnValue({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() }),
})

import ChatPage from '../pages/ChatPage'

const PROJECT = '/home/u/work/repo'

function makeStore() {
  return configureStore({
    reducer: { dashboard: dashboardReducer, chat: chatReducer, notifications: notificationsReducer },
    preloadedState: {
      dashboard: {
        status: null,
        slots: [{ key: 'slot-a', messages: 0, running: false, stop_state: 'idle', mode: '', project: PROJECT, pending_approval: false, waiting_for_input: false, last_activity_ts: undefined }],
        unreadSlots: [], refreshTrigger: 0, approvalMode: 'normal',
        subagentRunning: {}, subagentDetails: {}, subagentText: {},
      } as unknown as ReturnType<typeof dashboardReducer>,
      chat: {
        activeSlot: 'slot-a', messages: [],
        slotRunning: false, slotStopping: false, slotState: 'idle',
        history: [], historyHasMore: false, pendingInput: null,
        subagents: {}, toolLog: [], activityOpen: false, activityTab: 'tools',
        slotHasMore: false, slotOldestIndex: 0, loadingOlder: false,
        slotStatusDetail: {}, slotContextPct: {}, slotActivity: {}, slotHistory: [],
        historyOffset: 0, _wsChunkedDuringFetch: false,
        slotMessages: {}, slotLoading: false,
      } as unknown as ReturnType<typeof chatReducer>,
      notifications: { items: [] } as unknown as ReturnType<typeof notificationsReducer>,
    },
  })
}

function renderChat() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <Provider store={makeStore()}>
      <QueryClientProvider client={queryClient}>
        <ThemeProvider>
          <MemoryRouter initialEntries={['/chat']}>
            <ChatPage />
          </MemoryRouter>
        </ThemeProvider>
      </QueryClientProvider>
    </Provider>,
  )
}

const status = (over: Record<string, unknown> = {}) => ({
  project_dir: PROJECT,
  enabled: true,
  has_config: true,
  config_path: `${PROJECT}/.devcontainer/devcontainer.json`,
  trusted: false,
  container_id: null,
  running: false,
  remote_workspace_folder: null,
  ...over,
})

beforeEach(() => {
  vi.clearAllMocks()
  localStorage.clear()
})

describe('ChatPage Dev Container trust prompt', () => {
  it('prompts when the feature is on and the config is untrusted', async () => {
    devcontainerStatus.mockResolvedValue(status())
    renderChat()
    await waitFor(() => expect(devcontainerStatus).toHaveBeenCalledWith(PROJECT))
    await waitFor(() =>
      expect(screen.getByText(/run this project's agent inside its dev container\?/i)).toBeInTheDocument(),
    )
  })

  it('sends the reviewed digest with the grant, not just the project', async () => {
    // The security property end to end: the digest the card displayed is what
    // reaches the API, so a config rewritten between the preview and the click
    // cannot be authorized (the backend refuses a mismatch with 409).
    devcontainerStatus.mockResolvedValue(status())
    renderChat()
    const button = await screen.findByRole('button', { name: /^trust$/i })
    await waitFor(() => expect(button).toBeEnabled())
    fireEvent.click(button)
    await waitFor(() => expect(devcontainerTrust).toHaveBeenCalledTimes(1))
    expect(devcontainerTrust).toHaveBeenCalledWith(PROJECT, 'abcdef0123456789')
  })

  it('never prompts while the feature is off, config or not', async () => {
    devcontainerStatus.mockResolvedValue(status({ enabled: false }))
    renderChat()
    await waitFor(() => expect(devcontainerStatus).toHaveBeenCalledWith(PROJECT))
    // Settle the render the status resolution triggers before asserting absence,
    // so this cannot pass merely because the query had not landed yet.
    await waitFor(() => expect(screen.queryByText(/dev container/i)).not.toBeInTheDocument())
  })

  it('hides the running-container chip while the feature is off', async () => {
    devcontainerStatus.mockResolvedValue(
      status({ enabled: false, trusted: true, running: true, container_id: 'aabbccddeeff0011' }),
    )
    renderChat()
    await waitFor(() => expect(devcontainerStatus).toHaveBeenCalledWith(PROJECT))
    await waitFor(() =>
      expect(screen.queryByRole('button', { name: /dev container/i })).not.toBeInTheDocument(),
    )
  })

  it('shows the chip when the feature is on and a container runs', async () => {
    devcontainerStatus.mockResolvedValue(
      status({ trusted: true, running: true, container_id: 'aabbccddeeff0011' }),
    )
    renderChat()
    await waitFor(() =>
      expect(screen.getByRole('button', { name: /dev container/i })).toBeInTheDocument(),
    )
  })
})
