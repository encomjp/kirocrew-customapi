// The pull request's own content, as tab bodies: description, CI checks, comments.
//
// Data comes from the dashboard's OWN PR endpoint (`/api/source/pull-request`,
// the one the GitHub side panel uses), deliberately rather than a new Sage route:
//   * it already normalises GitHub and GitLab into one shape,
//   * it caches server-side on a short TTL, so opening the same PR twice — or
//     having it open in the side panel at the same time — costs one provider
//     call, not two,
//   * it keeps credentials in the provider CLI on the gateway.
// One request returns all three sections, so the tabs share a single query and
// switching between them costs nothing.
import {
  CheckCircle2, Clock, ExternalLink, MinusCircle, XCircle,
} from 'lucide-react'
import { useEffect, useMemo } from 'react'
import { useQuery, type UseQueryResult } from '@tanstack/react-query'

import { api } from '../../../api/client'
import MarkdownRenderer from '../../../components/MarkdownRenderer'
import { safeHttpUrl } from '../../../lib/safeUrl'
import CommentThreads from './CommentThreads'
import type { PullRequestCheck, PullRequestSource } from '../../../types'
import { readSnapshot, writeSnapshot } from '../lib/persist'

import { i18nT } from '../../../i18n/t'
/** The server's own cache TTL is short; match it rather than hammering. */
const SOURCE_STALE_MS = 30_000

/** One query serves every tab, so switching tabs never refetches.
 *
 * Seeded from the last successful payload with its ORIGINAL fetch timestamp, so
 * reopening a pull request paints its description, checks and threads at once and
 * revalidates behind them. The timestamp is what makes that work: without it the
 * replay would look freshly fetched and suppress the refetch for the whole
 * staleTime window, leaving you reading a payload that never refreshed. */
export function usePrSource(url: string): UseQueryResult<PullRequestSource, Error> {
  const snapshot = useMemo(
    () => (url ? readSnapshot<PullRequestSource>(`pr-source:${url}`) : undefined),
    [url],
  )
  const query = useQuery({
    queryKey: ['code-review-sage', 'pr-source', url],
    queryFn: () => api.pullRequestSource(url),
    staleTime: SOURCE_STALE_MS,
    initialData: snapshot?.data,
    initialDataUpdatedAt: snapshot?.at,
  })
  useEffect(() => {
    if (url && query.data && query.isSuccess) {
      writeSnapshot(`pr-source:${url}`, query.data)
    }
  }, [url, query.data, query.isSuccess])
  return query
}

/** Label keys as full literals in one indexable map, so the key-resolution
 *  gate can verify each one exists. Indexed at the call site, not read off a
 *  local — that indirection is what made these sites unverifiable. */
const BUCKET_LABEL_KEY: Record<PullRequestCheck['bucket'], string> = {
  failed: 'apps.codeReviewSage.components.prSourcePanel.bucket.failing',
  pending: 'apps.codeReviewSage.components.prSourcePanel.bucket.running',
  passed: 'apps.codeReviewSage.components.prSourcePanel.bucket.passed',
  skipped: 'apps.codeReviewSage.components.prSourcePanel.bucket.skipped',
}

const BUCKET_META: Record<PullRequestCheck['bucket'], {
  icon: typeof CheckCircle2
  color: string
}> = {
  // Keys, not strings: this table is module-level, so a resolved string would
  // freeze whichever locale was active at import. Resolved per render below.
  failed: { icon: XCircle, color: 'text-danger' },
  pending: { icon: Clock, color: 'text-accent' },
  passed: { icon: CheckCircle2, color: 'text-ok' },
  skipped: { icon: MinusCircle, color: 'text-muted' },
}

export function DescriptionBody({ src }: { src: PullRequestSource }) {
  if (!src.description?.trim()) {
    return <div className="text-[12.5px] text-muted italic">{i18nT('apps.codeReviewSage.components.prSourcePanel.no_description_provided')}</div>
  }
  // Author-written markdown — the shared renderer sanitises it.
  return <MarkdownRenderer content={src.description} />
}

export function ChecksBody({ src }: { src: PullRequestSource }) {
  const checks = src.checks
  if (!checks.length) {
    return <div className="text-[12.5px] text-muted italic">{i18nT('apps.codeReviewSage.components.prSourcePanel.no_checks_reported')}</div>
  }
  // Worst first: a failing check is the thing you need to see.
  const order: PullRequestCheck['bucket'][] = ['failed', 'pending', 'passed', 'skipped']
  const sorted = [...checks].sort(
    (a, b) => order.indexOf(a.bucket) - order.indexOf(b.bucket),
  )
  return (
    <ul className="flex flex-col gap-1 list-none p-0 m-0">
      {sorted.map((c) => {
        const meta = BUCKET_META[c.bucket] ?? BUCKET_META.skipped
        const Icon = meta.icon
        return (
          <li
            key={`${c.workflow}/${c.name}`}
            className="flex items-center gap-2 text-[12.5px] rounded-md px-2 py-1.5 border border-border bg-card"
          >
            <Icon size={12} className={`flex-shrink-0 ${meta.color}`} aria-hidden="true" />
            <span className="flex-1 min-w-0 truncate text-text">{c.name}</span>
            {c.workflow && c.workflow !== c.name && (
              <span className="flex-shrink-0 text-[11px] text-muted truncate max-w-[140px]">
                {c.workflow}
              </span>
            )}
            <span className={`flex-shrink-0 text-[11px] ${meta.color}`}>
              {c.conclusion || c.status || i18nT(BUCKET_LABEL_KEY[c.bucket])}
            </span>
            {safeHttpUrl(c.url) && (
              <a
                href={safeHttpUrl(c.url) as string}
                target="_blank"
                rel="noreferrer"
                aria-label={`Open ${c.name} run`}
                className="flex-shrink-0 text-muted hover:text-accent"
              >
                <ExternalLink size={11} />
              </a>
            )}
          </li>
        )
      })}
    </ul>
  )
}

/** The comments tab. Threading, replying and resolving live in CommentThreads —
 *  this stays a one-line adapter so the tab wiring does not care. */
export function CommentsBody({ src }: { src: PullRequestSource }) {
  return <CommentThreads src={src} />
}

/** Shown in every provider-backed tab when the source fetch fails. Degrades to a
 *  note rather than an error screen: the review is the point of this pane, and a
 *  flaky provider call must not hide it. */
export function SourceError({ error }: { error: Error }) {
  return (
    <div className="text-[12.5px] text-muted">
      {i18nT('apps.codeReviewSage.components.prSourcePanel.load_failed',
        { reason: error.message })}
    </div>
  )
}

export function PartialNote({ src }: { src: PullRequestSource }) {
  if (!(src.partialSections?.length ?? 0)) return null
  return (
    <div className="text-[11.5px] text-muted opacity-80 leading-[1.5] mt-3">
      {i18nT('apps.codeReviewSage.components.prSourcePanel.partial_sections',
        { sections: src.partialSections?.join(', ') ?? '' })}
    </div>
  )
}
