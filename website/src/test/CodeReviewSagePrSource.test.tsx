// The PR content tab bodies: description, CI checks, comments.
//
// These are pure given a PullRequestSource, so they are tested directly; the tab
// bar that hosts them (and the single shared query behind it) is covered in
// CodeReviewSageShell.test.tsx.
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import {
  ChecksBody, CommentsBody, DescriptionBody, PartialNote, SourceError,
} from '../apps/code-review-sage/components/PrSourcePanel'
import PrStatusChips from '../apps/code-review-sage/components/PrStatusChips'
import type { PullRequestSource } from '../types'

const URL_ = 'https://github.com/acme/widgets/pull/7'

/** The comments tab posts replies, so it needs a query client for its mutations. */
function renderComments(src: PullRequestSource) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}><CommentsBody src={src} /></QueryClientProvider>,
  )
}

function source(over: Partial<PullRequestSource> = {}): PullRequestSource {
  return {
    provider: 'github',
    url: URL_,
    number: 7,
    title: 'Tighten the cookie jar',
    description: 'Caps the jar so a big header cannot wedge the gateway.',
    state: 'open',
    draft: false,
    mergedAt: '',
    updatedAt: new Date().toISOString(),
    headBranch: 'fix/jar',
    baseBranch: 'main',
    headSha: 'abc1234',
    author: 'ann',
    additions: 12,
    deletions: 3,
    changedFiles: 2,
    commits: [],
    checks: [
      {
        name: 'Lint', workflow: 'CI', status: 'completed',
        conclusion: 'success', bucket: 'passed', url: '',
        startedAt: '', completedAt: '',
      },
      {
        name: 'Backend Tests', workflow: 'CI', status: 'completed',
        conclusion: 'failure', bucket: 'failed', url: 'https://ci/1',
        startedAt: '', completedAt: '',
      },
    ],
    comments: [{
      id: 'c1', kind: 'comment', author: 'bob', body: 'Nice **fix**.',
      state: '', createdAt: new Date().toISOString(), url: '', path: '',
      line: null,
    }],
    files: [],
    ...over,
  }
}

describe('PR content tab bodies', () => {
  it('renders the description as markdown', () => {
    render(<DescriptionBody src={source()} />)
    expect(screen.getByText(/Caps the jar/)).toBeTruthy()
  })

  it('says so when the PR has no description', () => {
    render(<DescriptionBody src={source({ description: '   ' })} />)
    expect(screen.getByText(/No description provided/i)).toBeTruthy()
  })

  it('lists checks worst-first so a failure is not buried', () => {
    render(<ChecksBody src={source()} />)
    const rows = screen.getAllByRole('listitem').map((li) => li.textContent ?? '')
    expect(rows[0]).toContain('Backend Tests')
    expect(rows[1]).toContain('Lint')
  })

  it('links a check out to its run when the provider gave a URL', () => {
    render(<ChecksBody src={source()} />)
    const link = screen.getByRole('link', { name: /Open Backend Tests run/i })
    expect(link.getAttribute('href')).toBe('https://ci/1')
  })

  it('handles a PR with no checks', () => {
    render(<ChecksBody src={source({ checks: [] })} />)
    expect(screen.getByText(/No checks reported/i)).toBeTruthy()
  })

  it('renders comment bodies as markdown with their author and kind', () => {
    renderComments(source())
    expect(screen.getByText('bob')).toBeTruthy()
    expect(screen.getByText(/Nice/)).toBeTruthy()
  })

  it('handles a PR with no comments', () => {
    renderComments(source({ comments: [] }))
    expect(screen.getByText(/No comments yet/i)).toBeTruthy()
  })

  it('degrades to a note when the provider call fails', () => {
    // The review is the point of the pane; a failed context fetch must not
    // replace it with an error screen.
    render(<SourceError error={new Error('gh exploded')} />)
    expect(screen.getByText(/Could not load pull-request details/i)).toBeTruthy()
    expect(screen.getByText(/gh exploded/)).toBeTruthy()
  })

  it('flags incomplete sections instead of implying completeness', () => {
    render(<PartialNote src={source({ partialSections: ['comments'] })} />)
    expect(screen.getByText(/Some sections are incomplete/i)).toBeTruthy()
  })

  it('says nothing when every section came back whole', () => {
    const { container } = render(<PartialNote src={source()} />)
    expect(container.textContent).toBe('')
  })
})

describe('the pull request\'s own status', () => {
  const check = (bucket: string, name: string) => ({
    name, workflow: 'ci', bucket, status: 'completed', conclusion: bucket, url: '',
  })

  it('says a pull request is open', () => {
    render(<PrStatusChips src={source()} />)
    expect(screen.getByText('open')).toBeTruthy()
  })

  it('says merged, whatever the provider calls the state', () => {
    // GitHub reports a merged PR's state as CLOSED, so mergedAt is the signal.
    render(<PrStatusChips src={source({ state: 'closed', mergedAt: '2026-07-01T00:00:00Z' })} />)
    expect(screen.getByText('merged')).toBeTruthy()
    expect(screen.queryByText('closed')).toBeNull()
  })

  it('distinguishes closed-without-merging', () => {
    render(<PrStatusChips src={source({ state: 'closed', mergedAt: '' })} />)
    expect(screen.getByText('closed')).toBeTruthy()
  })

  it('flags conflicts', () => {
    render(<PrStatusChips src={source({ mergeable: 'conflicting' })} />)
    expect(screen.getByText('conflicts')).toBeTruthy()
  })

  it('stays quiet when a PR merges cleanly', () => {
    // "Mergeable" is the expected case; saying so on every PR is noise.
    render(<PrStatusChips src={source({ mergeable: 'mergeable' })} />)
    expect(screen.queryByText(/conflicts|merge blocked/)).toBeNull()
  })

  it('leads with failing checks', () => {
    render(<PrStatusChips src={source({
      checks: [check('failed', 'a'), check('pending', 'b'), check('passed', 'c')],
    })} />)
    // A failure is what stops the PR, so it beats the pending count.
    expect(screen.getByText('1 failing')).toBeTruthy()
    expect(screen.queryByText(/running/)).toBeNull()
  })

  it('reports checks still running when none failed', () => {
    render(<PrStatusChips src={source({
      checks: [check('pending', 'a'), check('passed', 'b')],
    })} />)
    expect(screen.getByText('1 running')).toBeTruthy()
  })

  it('reports a green run', () => {
    render(<PrStatusChips src={source({
      checks: [check('passed', 'a'), check('passed', 'b')],
    })} />)
    expect(screen.getByText('2 passed')).toBeTruthy()
  })

  it('shows no check chip when the provider reported none', () => {
    render(<PrStatusChips src={source({ checks: [] })} />)
    expect(screen.queryByText(/passed|failing|running/)).toBeNull()
  })

  it('renders nothing before the provider call lands', () => {
    // Placeholders would imply a status we do not know yet.
    const { container } = render(<PrStatusChips src={undefined} />)
    expect(container).toBeEmptyDOMElement()
  })
})
