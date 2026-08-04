import { memo, useCallback, useEffect, useId, useRef, useState } from 'react'
import { Box, ChevronDown, ChevronRight, X } from 'lucide-react'

import { api, type DevcontainerConfig } from '../api/client'
import { i18nT } from '../i18n/t'

/** Characters of a sha256 shown to the user. Long enough to compare by eye
 *  against a second prompt, short enough not to wrap the card. */
const DIGEST_CHARS = 12

export interface DevcontainerTrustCardProps {
  /** Absolute project directory of the active slot — the trust key. */
  projectDir: string
  /** Config file the prompt is about, from the status response. */
  configPath?: string | null
  /**
   * Grant trust and refetch status. Rejects with a user-facing message, which
   * the card renders inline rather than swallowing: a failed grant leaves the
   * container unbuilt, so the user has to know it did not take.
   */
  onTrust: (reviewedDigest: string) => Promise<void>
  /** Dismiss for now. Trust is unchanged, so the prompt returns next session. */
  onDismiss: () => void
  /**
   * Config loader, injected for tests. Defaults to the api client, called
   * optionally because many test suites mock `../api/client` partially and
   * would otherwise see `undefined` here on mount.
   */
  loadConfig?: (project: string) => Promise<DevcontainerConfig>
}

/**
 * Workspace Trust prompt for a project that ships a Dev Container config.
 *
 * Rendered above the composer, in FollowUpCard's slot and styling, because it
 * gates the same thing the composer starts: nothing is built or run until the
 * user answers. Trust is granted against the config's CURRENT fingerprint, so
 * the fingerprint is shown — an edit to `devcontainer.json` produces a new one
 * and brings this card back rather than inheriting the old decision.
 *
 * The copy carries four things a first-time reader needs and cannot infer from
 * the word "trust": what a Dev Container IS, what trusting actually executes,
 * what stops working while a session runs inside one, and how to undo it. They
 * are separate lines rather than one paragraph so the consequence line can be
 * weighted differently from the explanation.
 *
 * Both answers state their outcome in VISIBLE text rather than a `title`
 * tooltip: a tooltip is unreachable by touch and by keyboard, and "Not now"
 * having no consequence is precisely the fact a hesitant user is looking for.
 *
 * The raw config is UNTRUSTED file content and is rendered as text children only
 * (never dangerouslySetInnerHTML), collapsed by default: the point is that the
 * user CAN read what they are about to authorize, not that they must scroll past
 * it every time.
 */
function DevcontainerTrustCard({
  projectDir,
  configPath,
  onTrust,
  onDismiss,
  loadConfig,
}: DevcontainerTrustCardProps) {
  const [config, setConfig] = useState<DevcontainerConfig | null>(null)
  const [expanded, setExpanded] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  // The explanation is the accessible description of the answer buttons, so a
  // screen reader reaching "Trust" reads what trusting does before activating it.
  const bodyId = useId()

  // Guards a resolve landing after the project changed under us, which would
  // show one project's config beside another project's Trust button.
  const projectGen = useRef(0)

  useEffect(() => {
    projectGen.current += 1
    const gen = projectGen.current
    setConfig(null)
    setError('')
    setExpanded(false)
    if (!projectDir) return
    const load = loadConfig || ((p: string) => api.devcontainerConfig?.(p))
    Promise.resolve(load(projectDir))
      .then((data) => {
        if (projectGen.current === gen && data) setConfig(data)
      })
      .catch(() => {
        // A preview that cannot be read is not a blocker: the card still offers
        // the decision, minus the fingerprint and the raw text.
        if (projectGen.current === gen) setConfig(null)
      })
  }, [projectDir, loadConfig])

  const trust = useCallback(async () => {
    if (busy) return
    // Trust is granted against the digest THIS card displayed, so a config the
    // user never saw cannot be authorized. With no loaded config there is
    // nothing to bind to, and the button is disabled for that reason.
    const reviewed = config?.digest
    if (!reviewed) {
      setError(i18nT('components.devcontainerTrustCard.could_not_grant_trust'))
      return
    }
    const gen = projectGen.current
    setBusy(true)
    setError('')
    try {
      await onTrust(reviewed)
    } catch (err) {
      if (projectGen.current === gen) {
        setError(err instanceof Error ? err.message : i18nT('components.devcontainerTrustCard.could_not_grant_trust'))
      }
    } finally {
      setBusy(false)
    }
  }, [busy, config, onTrust])

  const path = config?.config_path || configPath || ''
  const digest = config?.digest ? config.digest.slice(0, DIGEST_CHARS) : ''

  return (
    <div
      className="border border-accent/30 rounded-xl bg-card shadow-md overflow-hidden animate-scale-in"
      role="group"
      aria-label={i18nT('components.devcontainerTrustCard.dev_container_trust_prompt')}
    >
      <div className="flex items-center gap-2 px-4 pt-3 pb-1">
        <Box size={13} className="text-accent" aria-hidden="true" />
        <span className="text-[11px] font-semibold uppercase tracking-wider text-accent">
          {i18nT('components.devcontainerTrustCard.dev_container')}
        </span>
      </div>
      <div className="px-4 py-2">
        <div className="text-[13px] font-medium text-text">
          {i18nT('components.devcontainerTrustCard.run_this_project_s_agent_inside_its_dev_container')}
        </div>
        <div id={bodyId} className="text-[12px] text-muted mt-1 leading-relaxed">
          {i18nT('components.devcontainerTrustCard.this_project_ships_a_dev_container')}
        </div>
        {/* Weighted apart from the explanation above: this is the line that says
            code from the repo runs, which is the actual risk being consented to. */}
        <div className="text-[12px] text-text mt-1.5 leading-relaxed">
          {i18nT('components.devcontainerTrustCard.trusting_runs_the_setup_commands_in_this_file')}
        </div>
        <div className="text-[12px] text-muted mt-1.5 leading-relaxed">
          {i18nT('components.devcontainerTrustCard.while_a_session_runs_in_the_container')}
        </div>
        <div className="text-[12px] text-muted mt-1.5 leading-relaxed">
          {i18nT('components.devcontainerTrustCard.trust_applies_to_this_exact_configuration')}
        </div>

        <dl className="mt-2 grid grid-cols-[auto_minmax(0,1fr)] gap-x-3 gap-y-1 text-[11px]">
          {path && (
            <>
              <dt className="text-muted">{i18nT('components.devcontainerTrustCard.configuration')}</dt>
              <dd className="font-mono text-text truncate" title={path}>{path}</dd>
            </>
          )}
          {config?.name && (
            <>
              <dt className="text-muted">{i18nT('components.devcontainerTrustCard.name')}</dt>
              <dd className="text-text truncate" title={config.name}>{config.name}</dd>
            </>
          )}
          {config?.image && (
            <>
              <dt className="text-muted">{i18nT('components.devcontainerTrustCard.image')}</dt>
              <dd className="font-mono text-text truncate" title={config.image}>{config.image}</dd>
            </>
          )}
          {digest && (
            <>
              <dt className="text-muted">{i18nT('components.devcontainerTrustCard.fingerprint')}</dt>
              <dd className="font-mono text-text truncate" title={config?.digest}>{digest}</dd>
            </>
          )}
        </dl>

        {config?.raw ? (
          <div className="mt-2.5">
            <button
              onClick={() => setExpanded(e => !e)}
              aria-expanded={expanded}
              className="inline-flex items-center gap-1 px-0 py-1 text-[12px] text-muted hover:text-text bg-transparent border-none cursor-pointer transition-colors"
            >
              {expanded
                ? <ChevronDown size={13} aria-hidden="true" />
                : <ChevronRight size={13} aria-hidden="true" />}
              {expanded
                ? i18nT('components.devcontainerTrustCard.hide_details')
                : i18nT('components.devcontainerTrustCard.show_what_this_will_run')}
            </button>
            {expanded && (
              <pre className="mt-1 max-h-56 overflow-auto rounded-md border border-border bg-bg p-2.5 text-[11px] font-mono text-text whitespace-pre-wrap break-words">
                {config.raw}
              </pre>
            )}
          </div>
        ) : null}

        <div className="flex flex-wrap items-start gap-2 mt-2.5" aria-describedby={bodyId}>
          <button
            onClick={trust}
            // No loaded config means no digest to bind the grant to, so there is
            // nothing the user can be said to have reviewed.
            disabled={busy || !config?.digest}
            className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-md text-[12px] font-medium cursor-pointer transition-all disabled:opacity-40 disabled:cursor-not-allowed bg-accent text-accent-fg hover:bg-accent-hover border-none"
          >
            {busy
              ? i18nT('components.devcontainerTrustCard.trusting')
              : i18nT('components.devcontainerTrustCard.trust')}
          </button>
          <div className="flex flex-col items-start gap-0.5 min-w-0">
            <button
              onClick={onDismiss}
              disabled={busy}
              className="inline-flex items-center gap-1.5 px-2.5 py-1.5 rounded-md text-[12px] cursor-pointer transition-all disabled:opacity-40 disabled:cursor-not-allowed border border-transparent text-muted hover:text-text bg-transparent"
            >
              <X size={13} aria-hidden="true" /> {i18nT('components.devcontainerTrustCard.not_now')}
            </button>
            {/* Visible, not a tooltip: "declining costs nothing" is the reassurance
                a hesitant user needs, and a tooltip hides it from touch and keyboard. */}
            <span className="text-[11px] text-muted leading-snug pl-2.5">
              {i18nT('components.devcontainerTrustCard.nothing_is_built_or_changed')}
            </span>
          </div>
        </div>

        {error && (
          <div role="alert" className="text-[12px] text-danger mt-2">
            {error}
          </div>
        )}
      </div>
    </div>
  )
}

export default memo(DevcontainerTrustCard)
