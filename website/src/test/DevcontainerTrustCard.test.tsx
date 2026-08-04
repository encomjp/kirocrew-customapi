import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'

import DevcontainerTrustCard from '../components/DevcontainerTrustCard'
import type { DevcontainerConfig } from '../api/client'

const RAW = '{\n  "name": "kirocrew",\n  "image": "example/base:1"\n}\n'

const config = (over: Partial<DevcontainerConfig> = {}): DevcontainerConfig => ({
  config_path: '/repo/.devcontainer/devcontainer.json',
  digest: 'abcdef0123456789abcdef',
  raw: RAW,
  name: 'kirocrew',
  image: 'example/base:1',
  trusted: false,
  ...over,
})

function setup(props: Partial<React.ComponentProps<typeof DevcontainerTrustCard>> = {}) {
  const onTrust = vi.fn().mockResolvedValue(undefined)
  const onDismiss = vi.fn()
  const loadConfig = vi.fn().mockResolvedValue(config())
  const utils = render(
    <DevcontainerTrustCard
      projectDir="/repo"
      configPath="/repo/.devcontainer/devcontainer.json"
      onTrust={onTrust}
      onDismiss={onDismiss}
      loadConfig={loadConfig}
      {...props}
    />,
  )
  return { onTrust, onDismiss, loadConfig, ...utils }
}

describe('DevcontainerTrustCard', () => {
  it('asks the decision as a question and offers both answers', () => {
    setup()
    expect(screen.getByText(/run this project's agent inside its dev container\?/i)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /^trust$/i })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /not now/i })).toBeInTheDocument()
  })

  it('explains what a Dev Container is before asking for trust', () => {
    // The usability failure this copy fixes: "Trust" meant nothing to a reader
    // who did not already know what a Dev Container was.
    setup()
    expect(screen.getByText(/a ready-made environment described in devcontainer\.json/i)).toBeInTheDocument()
  })

  it('names the consequence: setup commands run and access is granted', () => {
    setup()
    expect(screen.getByText(/runs the setup commands in this file/i)).toBeInTheDocument()
    expect(screen.getByText(/only trust projects you recognize/i)).toBeInTheDocument()
  })

  it('states what stops working while the session runs in the container', () => {
    setup()
    expect(
      screen.getByText(/scheduled jobs, subagents, and saved lessons are unavailable/i),
    ).toBeInTheDocument()
  })

  it('says the decision is scoped to this configuration and can be withdrawn', () => {
    setup()
    expect(screen.getByText(/if it changes, you will be asked again/i)).toBeInTheDocument()
    expect(screen.getByText(/withdraw trust from the dev container chip/i)).toBeInTheDocument()
  })

  it('spells out that declining costs nothing, in visible text not a tooltip', () => {
    // A `title` tooltip is unreachable by touch and by keyboard, and this is the
    // reassurance a hesitant user is looking for — so it must be in the DOM text.
    setup()
    const subtext = screen.getByText(/your chat keeps running directly on this machine/i)
    expect(subtext).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /not now/i })).not.toHaveAttribute('title')
    expect(screen.getByRole('button', { name: /^trust$/i })).not.toHaveAttribute('title')
  })

  it('describes the answer group with the explanation copy', async () => {
    setup()
    const group = screen.getByRole('button', { name: /^trust$/i }).parentElement!
    const describedBy = group.getAttribute('aria-describedby')
    expect(describedBy).toBeTruthy()
    expect(document.getElementById(describedBy!)).toHaveTextContent(/ready-made environment/i)
  })

  it('shows the config path before the preview has loaded', () => {
    setup({ loadConfig: vi.fn().mockReturnValue(new Promise(() => {})) })
    expect(screen.getByText('/repo/.devcontainer/devcontainer.json')).toBeInTheDocument()
  })

  it('labels the truncated sha as a fingerprint', async () => {
    setup()
    // Truncated, not the full sha: the point is an at-a-glance comparison, and
    // the full value stays in the title attribute.
    await waitFor(() => expect(screen.getByText('abcdef012345')).toBeInTheDocument())
    expect(screen.queryByText('abcdef0123456789abcdef')).not.toBeInTheDocument()
    expect(screen.getByText('Fingerprint')).toBeInTheDocument()
    expect(screen.queryByText('Digest')).not.toBeInTheDocument()
  })

  it('keeps the raw config collapsed behind a toggle that says what it reveals', async () => {
    setup()
    const toggle = await screen.findByRole('button', { name: /show what this will run/i })
    expect(toggle).toHaveAttribute('aria-expanded', 'false')
    expect(screen.queryByText(/"image": "example\/base:1"/)).not.toBeInTheDocument()

    fireEvent.click(toggle)
    const expanded = screen.getByRole('button', { name: /hide details/i })
    expect(expanded).toHaveAttribute('aria-expanded', 'true')
    expect(screen.getByText(/"image": "example\/base:1"/)).toBeInTheDocument()
  })

  it('grants trust bound to the digest it displayed', async () => {
    // The grant carries the digest THIS card showed, so the backend can refuse a
    // config that changed between the preview and the click. Trust is disabled
    // until the preview resolves, because before that there is nothing the user
    // can be said to have reviewed.
    const { onTrust } = setup()
    const button = await screen.findByRole('button', { name: /^trust$/i })
    await waitFor(() => expect(button).toBeEnabled())
    fireEvent.click(button)
    await waitFor(() => expect(onTrust).toHaveBeenCalledTimes(1))
    expect(onTrust).toHaveBeenCalledWith(config().digest)
  })

  it('does not fire a second grant while the first is in flight', async () => {
    let release: () => void = () => {}
    const onTrust = vi.fn(() => new Promise<void>((res) => { release = () => res() }))
    setup({ onTrust })
    const button = await screen.findByRole('button', { name: /^trust$/i })
    await waitFor(() => expect(button).toBeEnabled())
    fireEvent.click(button)
    await waitFor(() => expect(screen.getByRole('button', { name: /trusting/i })).toBeDisabled())
    fireEvent.click(screen.getByRole('button', { name: /trusting/i }))
    release()
    await waitFor(() => expect(onTrust).toHaveBeenCalledTimes(1))
  })

  it('renders a failed grant inline instead of swallowing it', async () => {
    const onTrust = vi.fn().mockRejectedValue(new Error('docker daemon unreachable'))
    setup({ onTrust })
    const button = await screen.findByRole('button', { name: /^trust$/i })
    await waitFor(() => expect(button).toBeEnabled())
    fireEvent.click(button)
    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('docker daemon unreachable')
  })

  it('calls onDismiss for "Not now" and never onTrust', () => {
    const { onDismiss, onTrust } = setup()
    fireEvent.click(screen.getByRole('button', { name: /not now/i }))
    expect(onDismiss).toHaveBeenCalledTimes(1)
    expect(onTrust).not.toHaveBeenCalled()
  })

  it('cannot grant trust when the preview could not be read', async () => {
    // Trust is bound to the digest the user reviewed. With no preview there is
    // no digest, so there is nothing to bind to and the grant is refused at the
    // button rather than sending an unbound request. The card still renders the
    // decision (and the path) so the user understands why it is unavailable.
    const loadConfig = vi.fn().mockRejectedValue(new Error('gone'))
    const { onTrust } = setup({ loadConfig })
    await waitFor(() => expect(loadConfig).toHaveBeenCalled())
    const button = screen.getByRole('button', { name: /^trust$/i })
    expect(button).toBeDisabled()
    fireEvent.click(button)
    expect(onTrust).not.toHaveBeenCalled()
    expect(screen.queryByRole('button', { name: /show what this will run/i })).not.toBeInTheDocument()
  })

  it('drops a preview that resolves after the project changed', async () => {
    // Guards the mismatch that matters: one project's config text rendered
    // beside another project's Trust button.
    let resolveFirst: (c: DevcontainerConfig) => void = () => {}
    const loadConfig = vi.fn((project: string) =>
      project === '/repo'
        ? new Promise<DevcontainerConfig>((res) => { resolveFirst = res })
        : Promise.resolve(config({ image: 'second/image:2', digest: 'ffffffffffffffff' })),
    )
    const { rerender } = render(
      <DevcontainerTrustCard
        projectDir="/repo"
        onTrust={vi.fn().mockResolvedValue(undefined)}
        onDismiss={vi.fn()}
        loadConfig={loadConfig}
      />,
    )
    rerender(
      <DevcontainerTrustCard
        projectDir="/other"
        onTrust={vi.fn().mockResolvedValue(undefined)}
        onDismiss={vi.fn()}
        loadConfig={loadConfig}
      />,
    )
    await waitFor(() => expect(screen.getByText('ffffffffffff')).toBeInTheDocument())
    resolveFirst(config({ image: 'first/image:1', digest: 'aaaaaaaaaaaaaaaa' }))
    await waitFor(() => expect(screen.getByText('ffffffffffff')).toBeInTheDocument())
    expect(screen.queryByText('aaaaaaaaaaaa')).not.toBeInTheDocument()
  })

  it('renders the raw config as text, never as markup', async () => {
    setup({ loadConfig: vi.fn().mockResolvedValue(config({ raw: '{"name": "<img>"}' })) })
    fireEvent.click(await screen.findByRole('button', { name: /show what this will run/i }))
    const pre = screen.getByText(/"name": "<img>"/)
    expect(pre.querySelector('img')).toBeNull()
  })
})
