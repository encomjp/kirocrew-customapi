/**
 * Telemetry panel: the sortable-table rewrite.
 *
 * The page used to be six stacked sections, four of which were the same
 * "label + horizontal bar + right-aligned numbers" shape over different
 * groupings. This suite pins the properties that made replacing them with one
 * sortable table safe, each of which regresses SILENTLY:
 *
 *  1. `context.sessions[]` reaches the screen. The backend has always computed,
 *     serialised and shipped a per-session occupancy list, and the page never
 *     rendered it — a payload that costs a scan of every usage row on every
 *     poll and answered nobody. If the Context tab loses its table, the payload
 *     goes back to being dead and nothing else fails.
 *  2. Nulls sort last in BOTH directions. "No growth measured yet" is not a
 *     small growth rate, so flipping a column must not promote unmeasured rows
 *     over measured ones. The shared `useSortableTable` direction model cannot
 *     express this (it negates by swapping arguments), which is exactly why the
 *     comparison is local — and why it needs a test.
 *  3. Peak occupancy is NOT painted in the danger colour. A peak is a
 *     high-water mark over a session's whole life, so on a real window six of
 *     eight rows sat above 90% and the danger colour marked "this row exists"
 *     rather than "act on this row".
 *  4. Turns-to-compaction IS painted, because it is the actionable companion:
 *     it can still be acted on when it is small.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'

import TelemetryPanel from '../pages/TelemetryPanel'

const convo = (over: Record<string, unknown> = {}) => ({
  slot: 'chat-1-1700000000',
  title: 'A named conversation',
  credits: 100,
  turns: 10,
  peak_pct: 50,
  span_days: 1,
  first_ts: 1700000000,
  growth_pct_per_turn: 2,
  turns_to_compaction: 20,
  ...over,
})

const cost = (over: Record<string, unknown> = {}) => ({
  window_days: 7,
  credits: 1000,
  turns: 100,
  per_turn: 10,
  prior_credits: 500,
  prior_turns: 50,
  prior_per_turn: 10,
  delta_pct: 100,
  priciest: { credits: 90, slot: 'chat-1-1700000000', ts: '2026-08-05' },
  by_model: [{ name: 'opus-5', credits: 800, turns: 80, per_turn: 10, share_pct: 80, delta_pct: 12 }],
  by_channel: [{ name: 'dashboard', credits: 900, turns: 90, per_turn: 10, share_pct: 90, delta_pct: 8 }],
  context_bands: [],
  conversations: [convo()],
  conversation_count: 1,
  ...over,
})

/** Only the named surface carries data, so its tab renders without navigation. */
const only = (over: Record<string, unknown>) => ({
  enabled: true,
  window_days: 7,
  shard_count: 1,
  metrics_dir: '/tmp/metrics',
  startup: null,
  turn: null,
  context: null,
  cost: null,
  other: [],
  ...over,
})

vi.mock('../api/client', () => ({ api: { telemetryStartup: vi.fn() } }))

const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
const Wrapper = ({ children }: { children: React.ReactNode }) => (
  <QueryClientProvider client={qc}>
    <MemoryRouter>{children}</MemoryRouter>
  </QueryClientProvider>
)

async function mount(payload: Record<string, unknown>) {
  const { api } = await import('../api/client')
  vi.mocked(api.telemetryStartup).mockResolvedValue(payload as never)
  render(<TelemetryPanel />, { wrapper: Wrapper })
}

/** Row labels in document order for one column, identified by its header. */
function columnOrder(header: string): string[] {
  const ths = Array.from(document.querySelectorAll('thead th'))
  const idx = ths.findIndex(th => th.textContent?.startsWith(header))
  expect(idx).toBeGreaterThanOrEqual(0)
  return Array.from(document.querySelectorAll('tbody tr')).map(
    tr => tr.children[idx]?.textContent?.trim() ?? '',
  )
}

beforeEach(() => {
  vi.clearAllMocks()
  qc.clear()
  localStorage.clear()
})

describe('TelemetryPanel — context sessions', () => {
  it('renders the per-session occupancy payload the page used to discard', async () => {
    await mount(
      only({
        context: {
          turns: 40,
          p50_pct: 40,
          p90_pct: 80,
          max_pct: 95,
          window_days: 14,
          sessions: [
            {
              slot: 'chat-7-1700000700',
              turns: 12,
              peak_pct: 95,
              used: 152341,
              window: 200000,
              agent: 'kirocrew',
              model: 'opus-5',
              surface: 'dashboard',
              ts: '2026-08-05T00:00:00Z',
            },
          ],
        },
      }),
    )
    await waitFor(() => expect(screen.getByText('chat-7-1700000700')).toBeInTheDocument())
    // The occupancy numbers, not just the slot: a row that renders an id and
    // drops its measurements would still satisfy a slot-only assertion.
    expect(screen.getByText('152,341')).toBeInTheDocument()
    expect(screen.getByText('200,000')).toBeInTheDocument()
  })

  it('states the unit for the two token counts', async () => {
    await mount(
      only({
        context: {
          turns: 1,
          p50_pct: 10,
          p90_pct: 10,
          max_pct: 10,
          window_days: 14,
          sessions: [
            {
              slot: 'chat-8-1700000800',
              turns: 1,
              peak_pct: 10,
              used: 100,
              window: 1000,
              agent: 'a',
              model: 'm',
              surface: 's',
              ts: '2026-08-05T00:00:00Z',
            },
          ],
        },
      }),
    )
    // A bare "Used" beside a bare "Window" was the reported defect: neither
    // number said what it counted.
    await waitFor(() => expect(screen.getByRole('columnheader', { name: /Used/ })).toBeInTheDocument())
    expect(screen.getByRole('columnheader', { name: /Used \(tokens\)/ })).toBeInTheDocument()
    expect(screen.getByRole('columnheader', { name: /Window \(tokens\)/ })).toBeInTheDocument()
  })
})

describe('TelemetryPanel — sorting', () => {
  it('keeps an unmeasured row last when the column is flipped', async () => {
    await mount(
      only({
        cost: cost({
          conversations: [
            convo({ slot: 'a', title: 'slow growth', growth_pct_per_turn: 1 }),
            convo({ slot: 'b', title: 'unmeasured', growth_pct_per_turn: null, turns_to_compaction: null }),
            convo({ slot: 'c', title: 'fast growth', growth_pct_per_turn: 9 }),
          ],
          conversation_count: 3,
        }),
      }),
    )
    await waitFor(() => expect(screen.getByText('fast growth')).toBeInTheDocument())

    const growth = screen.getByRole('button', { name: /Growth/ })
    await userEvent.click(growth)
    const first = columnOrder('Conversation')
    expect(first[first.length - 1]).toBe('unmeasured')

    await userEvent.click(growth)
    const flipped = columnOrder('Conversation')
    // The measured rows must have reversed — otherwise this asserts nothing.
    expect(flipped[0]).not.toBe(first[0])
    expect(flipped[flipped.length - 1]).toBe('unmeasured')
  })

  it('marks the sorted column for assistive technology', async () => {
    await mount(only({ cost: cost() }))
    await waitFor(() => expect(screen.getByText('A named conversation')).toBeInTheDocument())
    const credits = screen.getByRole('columnheader', { name: /Credits/ })
    expect(credits).toHaveAttribute('aria-sort', 'descending')
    await userEvent.click(screen.getByRole('button', { name: /Credits/ }))
    expect(credits).toHaveAttribute('aria-sort', 'ascending')
  })
})

describe('TelemetryPanel — group by', () => {
  it('re-keys the same table instead of stacking a second one', async () => {
    await mount(only({ cost: cost() }))
    await waitFor(() => expect(screen.getByText('A named conversation')).toBeInTheDocument())
    // One table, not one per grouping: the old page drew by-model and
    // by-channel as separate always-visible sections.
    expect(document.querySelectorAll('table')).toHaveLength(1)

    await userEvent.click(screen.getByRole('button', { name: 'Model' }))
    await waitFor(() => expect(screen.getByText('opus-5')).toBeInTheDocument())
    expect(screen.queryByText('A named conversation')).not.toBeInTheDocument()
    expect(document.querySelectorAll('table')).toHaveLength(1)

    await userEvent.click(screen.getByRole('button', { name: 'Channel' }))
    await waitFor(() => expect(screen.getByText('dashboard')).toBeInTheDocument())
    expect(screen.queryByText('opus-5')).not.toBeInTheDocument()
  })
})

describe('TelemetryPanel — where the alarm colour goes', () => {
  it('does not paint a high peak occupancy as a fault', async () => {
    await mount(
      only({ cost: cost({ conversations: [convo({ peak_pct: 95, turns_to_compaction: 40 })] }) }),
    )
    await waitFor(() => expect(screen.getByText('95%')).toBeInTheDocument())
    // A peak is history. Painting it red put six of eight measured rows in the
    // danger colour, at which point the colour stopped meaning anything.
    const cell = screen.getByText('95%').closest('td') as HTMLElement
    expect(cell.style.color).not.toBe('var(--danger)')
    expect(cell.style.color).not.toBe('var(--warn)')
  })

  it('paints the turns remaining before compaction, which can still be acted on', async () => {
    await mount(
      only({ cost: cost({ conversations: [convo({ peak_pct: 95, turns_to_compaction: 1 })] }) }),
    )
    await waitFor(() => expect(screen.getByText('A named conversation')).toBeInTheDocument())
    const ths = Array.from(document.querySelectorAll('thead th'))
    const idx = ths.findIndex(th => th.textContent?.startsWith('To 90%'))
    const cell = document.querySelectorAll('tbody tr')[0].children[idx] as HTMLElement
    expect(cell.textContent).toBe('1')
    expect(cell.style.color).toBe('var(--danger)')
  })
})

describe('TelemetryPanel — share of spend', () => {
  it('does not report a real share as a flat zero', async () => {
    // Measured shape: the backend had already rounded share_pct, so a 7-credit
    // model arrived as 0 and rendered "0%" while its 9.5-credit neighbour
    // arrived as 0.05 and rendered "<1%". Two claims for one magnitude, and the
    // smaller one was false — the same rounding erasure the fault-rate tile was
    // fixed for.
    await mount(
      only({
        cost: cost({
          credits: 17279,
          by_model: [
            { name: 'big', credits: 17262.5, turns: 900, per_turn: 19, share_pct: 99, delta_pct: 5 },
            { name: 'nine-point-five', credits: 9.5, turns: 1, per_turn: 9.5, share_pct: 0.05, delta_pct: null },
            { name: 'seven', credits: 7, turns: 1, per_turn: 7, share_pct: 0, delta_pct: null },
          ],
        }),
      }),
    )
    await waitFor(() => expect(screen.getByText('A named conversation')).toBeInTheDocument())
    await userEvent.click(screen.getByRole('button', { name: 'Model' }))
    await waitFor(() => expect(screen.getByText('seven')).toBeInTheDocument())

    const shareOf = (name: string) => {
      const ths = Array.from(document.querySelectorAll('thead th'))
      const idx = ths.findIndex(th => th.textContent?.startsWith('Share'))
      const row = Array.from(document.querySelectorAll('tbody tr')).find(
        tr => tr.children[0]?.textContent?.trim() === name,
      )
      return row?.children[idx]?.textContent?.trim()
    }
    // Both are real spend below half a percent, so both must say so the same way.
    expect(shareOf('seven')).toBe(shareOf('nine-point-five'))
    expect(shareOf('seven')).not.toBe('0%')
  })
})

describe('TelemetryPanel — one mounted table per persisted sort', () => {
  it('does not carry a filter across a group-by switch', async () => {
    // Both groupings render a DataTable at the SAME position in one ternary, so
    // React reconciled them as one instance and the filter useState survived the
    // switch. The model table has no filter box, so the stale text was invisible
    // there — and silently re-applied on the way back, hiding rows the user had
    // no cue were filtered out.
    await mount(only({ cost: cost() }))
    await waitFor(() => expect(screen.getByText('A named conversation')).toBeInTheDocument())

    await userEvent.type(screen.getByPlaceholderText(/Filter conversations/), 'nothing-matches-this')
    await waitFor(() => expect(screen.queryByText('A named conversation')).not.toBeInTheDocument())

    await userEvent.click(screen.getByRole('button', { name: 'Model' }))
    await waitFor(() => expect(screen.getByText('opus-5')).toBeInTheDocument())

    await userEvent.click(screen.getByRole('button', { name: 'Conversation' }))
    await waitFor(() => expect(screen.getByText('A named conversation')).toBeInTheDocument())
    expect((screen.getByPlaceholderText(/Filter conversations/) as HTMLInputElement).value).toBe('')
  })

  it('marks the header that actually orders the rows, even from a stale saved sort', async () => {
    // A sort persisted by an older column layout names a column this set no
    // longer has. The lookup falls back to the first column, so the rows ARE
    // ordered — by a header carrying no caret, with every aria-sort reading
    // "none". A table sorted by nothing visible is worse than an unsorted one.
    localStorage.setItem('sort:telemetry-spend-conversation', JSON.stringify({ key: 'retired_column', dir: 'desc' }))
    await mount(only({ cost: cost() }))
    await waitFor(() => expect(screen.getByText('A named conversation')).toBeInTheDocument())

    const marked = Array.from(document.querySelectorAll('thead th')).filter(
      th => th.getAttribute('aria-sort') !== 'none',
    )
    expect(marked).toHaveLength(1)
    expect(marked[0].textContent).toMatch(/Conversation/)
  })
})

describe('TelemetryPanel — latency distribution order', () => {
  const stat = (over: Record<string, number> = {}) => ({
    count: 10, mean_ms: 100, p50_ms: 90, p90_ms: 200, min_ms: 10, max_ms: 300,
    other_generations: 0, total_count: 10, ...over,
  })

  it('opens in bucket-bound order, not by sample count', async () => {
    // The one thing a distribution exists to show is its shape. Ordering the
    // rows by count destroys it, and sorting the label column as TEXT is no
    // better — "≤ 1.0s" collates before "≤ 500ms". The counts below are
    // deliberately not monotonic with the bounds, so a count-ordered table
    // cannot accidentally pass.
    await mount(
      only({
        startup: {
          overall: stat({ count: 128 }), cold: stat(), warm: stat(),
          outcome: { ready: 128 },
          daily: [],
          distribution: { buckets: [5, 100, 20, 3], bounds: [500, 1000, 3000] },
          phases: [],
          by_channel: [],
        },
      }),
    )
    await waitFor(() => expect(screen.getByRole('button', { name: /Distribution/ })).toBeInTheDocument())
    await userEvent.click(screen.getByRole('button', { name: /Distribution/ }))

    await waitFor(() => expect(screen.getByText('≤ 500ms')).toBeInTheDocument())
    expect(columnOrder('Latency bucket')).toEqual(['≤ 500ms', '≤ 1.0s', '≤ 3.0s', '> 3.0s'])
  })
})

describe('TelemetryPanel — a filter that matches nothing', () => {
  it('does not claim the data is missing', async () => {
    // The table rendered its single no-data title whenever zero rows showed,
    // so filtering 252 conversations down to none had the page assert that no
    // spend was recorded — while holding all of it. Same class of false claim
    // as a real 0.4% share rendering as "0%".
    await mount(only({ cost: cost() }))
    await waitFor(() => expect(screen.getByText('A named conversation')).toBeInTheDocument())

    await userEvent.type(screen.getByPlaceholderText(/Filter conversations/), 'zzz-no-such-row')
    await waitFor(() => expect(screen.queryByText('A named conversation')).not.toBeInTheDocument())

    expect(screen.getByText('No rows match that filter')).toBeInTheDocument()
    expect(screen.queryByText('No spend recorded in this window')).not.toBeInTheDocument()
  })

  it('still says so when the window really recorded nothing', async () => {
    // The other half of the distinction: with no rows AND no filter, the
    // no-data title is the correct and only honest message.
    await mount(only({ cost: cost({ conversations: [], conversation_count: 0 }) }))
    await waitFor(() =>
      expect(screen.getByText('No spend recorded in this window')).toBeInTheDocument(),
    )
    expect(screen.queryByText('No rows match that filter')).not.toBeInTheDocument()
  })
})
