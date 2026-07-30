// One pull request, as tabs: Sage Review · Description · Comments · Checks.
//
// Sage Review leads because it is what this app is for; the other three are the
// context you need to interpret it, which previously meant leaving for GitHub.
// They are tabs rather than stacked sections because the description alone can
// run for pages — stacked, the review ended up below the fold on exactly the
// PRs where it matters most.
//
// Every tab reads ONE query (see usePrSource), so switching is free and no tab
// triggers its own provider call.
import {
  CircleDot, ExternalLink, FileText, GitPullRequest, Loader2, MessageSquare,
  ScanSearch,
} from 'lucide-react'
import { useMemo, useState, type ReactNode } from 'react'

import { useSage } from '../context'
import { effectiveRunStatus, relativeAge, typicalRunMs } from '../lib/format'
import type { PrRef, RunReport } from '../lib/types'
import EmptyState from './EmptyState'
import {
  ChecksBody, CommentsBody, DescriptionBody, PartialNote, SourceError, usePrSource,
} from './PrSourcePanel'
import ReportView from './ReportView'
import FailureNotice from './FailureNotice'
import PrStatusChips from './PrStatusChips'
import RunProgress from './RunProgress'
import PostCommentsButton from './PostCommentsButton'
import RunStatusPill from './RunStatusPill'
import ShimmerLine from './ShimmerLine'

import { i18nT } from '../../../i18n/t'
type Tab = 'review' | 'description' | 'comments' | 'checks'

/** http(s) only: the URL is provider-derived and lands in an href. */
function safeHref(url: string): string {
  try {
    return ['http:', 'https:'].includes(new URL(url).protocol) ? url : '#'
  } catch {
    return '#'
  }
}

function ReportSkeleton() {
  return (
    <>
      <span className="sr-only" role="status">{i18nT('apps.codeReviewSage.components.prReviewDetail.loading_review')}</span>
      <div aria-hidden="true" className="flex flex-col gap-3">
        {[0, 1].map((i) => (
          <div key={i} className="rounded-lg border border-border bg-card p-3 flex flex-col gap-2">
            <ShimmerLine w="46%" delay={i * 0.08} />
            <ShimmerLine w="80%" delay={i * 0.08 + 0.05} />
          </div>
        ))}
      </div>
    </>
  )
}

function Meta({ label, value }: { label: string; value: string }) {
  return (
    <span className="inline-flex items-baseline gap-1.5">
      <span className="text-[11px] uppercase tracking-wider text-muted">{label}</span>
      <span className="text-[12.5px] text-text">{value}</span>
    </span>
  )
}

function TabButton({
  label, icon: Icon, active, badge, badgeTone = 'muted', onClick,
}: {
  label: string
  icon: typeof FileText
  active: boolean
  badge?: ReactNode
  /** `alert` colours the badge when it represents something wrong (failing CI). */
  badgeTone?: 'muted' | 'alert' | 'accent'
  onClick: () => void
}) {
  const tone = badgeTone === 'alert'
    ? 'bg-bg-elevated text-danger'
    : badgeTone === 'accent'
      ? 'bg-accent-subtle text-accent'
      : 'bg-bg-elevated text-muted'
  return (
    <button
      type="button"
      role="tab"
      aria-selected={active}
      onClick={onClick}
      className={`inline-flex items-center gap-1.5 px-3 py-1.5 text-[12.5px] font-medium rounded-md cursor-pointer transition-colors ${
        active
          ? 'bg-bg-elevated text-text border border-border'
          : 'bg-transparent text-muted border border-transparent hover:text-text'
      }`}
    >
      <Icon size={13} aria-hidden="true" />
      {label}
      {badge !== undefined && badge !== null && (
        <span className={`text-[10.5px] px-1.5 py-0.5 rounded-full ${tone}`}>{badge}</span>
      )}
    </button>
  )
}

export default function PrReviewDetail({ pr }: { pr: PrRef }) {
  const {
    prRun, report, reportLoading, reportError,
    startReview, cancelRun, cancelling, pool,
    archiveRun, archiving, archiveError, runs, postComments, postCommentGroups,
    posting, postError, postingSelection,
  } = useSage()
  const [tab, setTab] = useState<Tab>('review')
  const source = usePrSource(pr.url)

  const running = prRun?.status === 'running'
  // The per-change error the driver wrote for THIS pull request, if it failed.
  const changeEntry = prRun?.progress?.[pr.change_id]
  const changeError = changeEntry?.phase === 'failed'
    ? (changeEntry.error || '').trim() || null
    : null
  const busy = startReview.isPending
  // Which cards may claim they are posting. A post started elsewhere (another
  // tab, or a reload mid-post) arrives only as the run's `posting` flag and
  // cannot be attributed to comments, so it covers the whole review; a local
  // per-finding post covers exactly the keys it sent.
  const localKeys = postingSelection === undefined
    ? []
    : (postingSelection?.keys ?? null)
  const isPosting = (key: string): boolean => {
    if (localKeys === null) return true
    if (localKeys.length > 0) return localKeys.includes(key)
    return Boolean(prRun?.posting)
  }
  // A run can cover many PRs; this pane shows only this one's findings.
  const scoped: RunReport | null = useMemo(() => {
    if (!report?.ready) return null
    const rows = report.rows.filter((r) => r.change_id === pr.change_id)
    const bands = { red: 0, yellow: 0, green: 0 }
    for (const r of rows) bands[r.band] += 1
    return { ...report, rows, bands, total: rows.length }
  }, [report, pr.change_id])

  const src = source.data
  // A PR opened from the thread list carries only a URL + change id, so every
  // display field falls back to the provider fetch. Same render either way.
  const title = pr.title || src?.title || `#${pr.number}`
  const author = pr.author || src?.author || ''
  const updatedAt = pr.updated_at || src?.updatedAt || ''
  const headSha = pr.head_sha || src?.headSha || ''
  // A failed run's action is a retry, not a first review: "Review" on a run that
  // just failed reads as though nothing had been attempted.
  const failed = !!prRun && effectiveRunStatus(prRun) === 'error'
  const isDraft = pr.draft ?? src?.draft ?? false
  const failing = (src?.checks ?? []).filter((c) => c.bucket === 'failed').length
  const findings = (scoped?.bands.red ?? 0) + (scoped?.bands.yellow ?? 0)

  const reviewBody = (
    <>
      {/* Above the progress block: a failed run's reason and its retry are the
          first things you need, not a footnote under an empty report. */}
      {prRun && (
        <FailureNotice
          run={prRun}
          changeId={pr.change_id}
          onRetry={() => startReview.mutate([pr.url])}
          retrying={startReview.isPending}
        />
      )}
      {prRun && (
        <RunProgress
          run={prRun}
          pool={pool}
          typicalMs={typicalRunMs(runs, prRun.changes.length)}
          onCancel={() => cancelRun(prRun.run_id)}
          cancelling={cancelling || Boolean(prRun.cancel_requested_at)}
        />
      )}

      {!prRun ? (
        <EmptyState
          icon={ScanSearch}
          title={i18nT('apps.codeReviewSage.components.prReviewDetail.this_pull_request_has_not_been_reviewed_yet')}
          hint={i18nT('apps.codeReviewSage.components.prReviewDetail.start_a_review_and_its_findings_appear_here_you')}
        />
      ) : reportError ? (
        <div className="text-[12.5px] text-danger">{reportError.message}</div>
      ) : reportLoading ? (
        <ReportSkeleton />
      ) : scoped && scoped.rows.length > 0 ? (
        <ReportView
          report={scoped}
          // The report is where you decide the findings are worth sending, so the
          // post control lives with it — and it is scoped to THIS pull request.
          actions={(
            <PostCommentsButton
              run={prRun}
              report={scoped}
              // Scoped to THIS pull request. Without the change id the
              // backend loops over every change in the run, so posting from
              // one PR detail published comments to every PR a repo run
              // covered.
              onPost={() => postComments(prRun.run_id,
                { changeId: pr.change_id })}
              posting={posting || Boolean(prRun.posting)}
              error={prRun.post_error ?? postError?.message ?? null}
            />
          )}
          postedKeys={prRun.posted_keys}
          isPosting={isPosting}
          onPostFinding={(changeId, key) => postComments(prRun.run_id, {
            changeId, keys: [key],
          })}
          // One request per change: a request posts one pending review against
          // one pull request, so a selection spanning changes is grouped.
          onPostSelection={(groups) => postCommentGroups(prRun.run_id, groups)}
          onArchive={() => archiveRun(prRun.run_id)}
          archiving={archiving}
          archiveError={archiveError?.message ?? null}
        />
      ) : running ? (
        // Deliberately NOT another "running" heading: the progress block above is
        // the status. This says only what the user cannot already see.
        <div className="text-[12.5px] text-muted leading-[1.6]">
          {i18nT('apps.codeReviewSage.components.prReviewDetail.findings_appear_here_as_soon_as_the_review_finis')}
        </div>
      ) : report?.ready ? (
        <EmptyState
          icon={GitPullRequest}
          title={i18nT('apps.codeReviewSage.components.prReviewDetail.nothing_flagged_on_this_pull_request')}
          hint={i18nT('apps.codeReviewSage.components.prReviewDetail.review_completed_with_no_findings')}
        />
      ) : (
        <EmptyState
          icon={GitPullRequest}
          title={i18nT('apps.codeReviewSage.components.prReviewDetail.that_review_produced_no_report_for_this_pull_req')}
          // The driver records WHY a change failed, so say it rather than making
          // the user guess whether this is a bug or a clean review.
          // The reason and the retry are in the notice above, so this only says
          // what that notice does not.
          hint={prRun.status === 'cancelled'
            ? i18nT('apps.codeReviewSage.components.prReviewDetail.cancelled_before_finishing')
            : changeError
              ? i18nT('apps.codeReviewSage.components.prReviewDetail.see_the_reason_above')
              : i18nT('apps.codeReviewSage.components.prReviewDetail.did_not_complete_for_this_change')}
        />
      )}
    </>
  )

  /** Provider-backed tabs share one loading/error treatment. */
  const sourceBody = (body: (s: NonNullable<typeof src>) => ReactNode) => {
    if (source.isLoading) {
      return <div className="text-[12.5px] text-muted">{i18nT('apps.codeReviewSage.components.prReviewDetail.loading_pull_request_details')}</div>
    }
    if (source.error) return <SourceError error={source.error as Error} />
    if (!src) return null
    return <>{body(src)}<PartialNote src={src} /></>
  }

  return (
    <article className="h-full flex flex-col min-h-0">
      <header className="px-6 pt-5 pb-3 border-b border-border flex-shrink-0">
        <div className="flex items-start gap-3">
          <div className="flex-1 min-w-0">
            <h1 className="text-[21px] font-bold leading-tight text-text-strong break-words">
              {title}
            </h1>
            <div className="flex items-center gap-2.5 mt-2 flex-wrap text-[12.5px] text-muted">
              <a
                href={safeHref(pr.url)}
                target="_blank"
                rel="noreferrer"
                title={i18nT('apps.codeReviewSage.components.prReviewDetail.open_on_github')}
                className="inline-flex items-center gap-1 font-mono text-muted hover:text-accent hover:underline"
              >
                #{pr.number} <ExternalLink size={11} aria-hidden="true" />
              </a>
              {author && <Meta label={i18nT('apps.codeReviewSage.components.prReviewDetail.author')} value={author} />}
              {updatedAt && <Meta label={i18nT('apps.codeReviewSage.components.prReviewDetail.updated')} value={relativeAge(updatedAt)} />}
              {headSha && <Meta label={i18nT('apps.codeReviewSage.components.prReviewDetail.head')} value={headSha.slice(0, 7)} />}
              {isDraft && (
                <span className="text-[10.5px] px-1.5 py-0.5 rounded-full bg-bg-elevated border border-border">
                  {i18nT('apps.codeReviewSage.components.prReviewDetail.draft')}
                </span>
              )}
              {/* The PULL REQUEST's own status. Previously the header described
                  the review but not the thing being reviewed, so a merged or
                  conflicting PR looked like a healthy open one. */}
              <PrStatusChips src={src} />
              {/* Terminal states only: while running, the action button already
                  reads "Reviewing…" and the progress block carries the detail —
                  a pill here made three signals say one thing. */}
              {prRun && prRun.status !== 'running' && (
                <RunStatusPill
                  status={effectiveRunStatus(prRun)}
                  cancelRequested={Boolean(prRun.cancel_requested_at)}
                />
              )}
            </div>
          </div>
          <button
            type="button"
            onClick={() => startReview.mutate([pr.url])}
            disabled={busy || running}
            title={running
              ? i18nT('apps.codeReviewSage.components.prReviewDetail.review_already_running')
              : pr.reviewed
                ? i18nT('apps.codeReviewSage.components.prReviewDetail.review_again_at_current_head')
                : i18nT('apps.codeReviewSage.components.prReviewDetail.review_this_pull_request')}
            className="flex-shrink-0 inline-flex items-center gap-1.5 rounded-md bg-accent text-accent-fg px-3 py-1.5 text-[12.5px] font-medium border-none cursor-pointer hover:bg-accent-hover disabled:opacity-40 disabled:cursor-default transition-colors"
          >
            {busy
              ? <Loader2 size={13} className="animate-spin motion-reduce:animate-none" />
              : <ScanSearch size={13} />}
            {running
              ? i18nT('apps.codeReviewSage.components.prReviewDetail.reviewing')
              : failed
                ? i18nT('apps.codeReviewSage.components.prReviewDetail.retry_review')
                : pr.reviewed || pr.reviewed_stale
                  ? i18nT('apps.codeReviewSage.components.prReviewDetail.review_again')
                  : i18nT('apps.codeReviewSage.components.prReviewDetail.review')}
          </button>
        </div>
        {startReview.error && (
          <div className="mt-2 text-[12.5px] text-danger">
            {(startReview.error as Error).message}
          </div>
        )}

        <div
          role="tablist"
          aria-label={i18nT('apps.codeReviewSage.components.prReviewDetail.pull_request_detail')}
          className="flex items-center gap-1 mt-3 flex-wrap"
        >
          <TabButton
            label={i18nT('apps.codeReviewSage.components.prReviewDetail.sage_review')}
            icon={ScanSearch}
            active={tab === 'review'}
            // A dot, not the word: "running" is already on the action button.
            // The dot exists so an in-flight review is visible from another tab.
            badge={running
              ? (
                <span
                  aria-label={i18nT('apps.codeReviewSage.components.prReviewDetail.running')}
                  className="inline-block w-1.5 h-1.5 rounded-full bg-accent animate-pulse motion-reduce:animate-none"
                />
              )
              : findings || undefined}
            badgeTone={running ? 'accent' : findings ? 'alert' : 'muted'}
            onClick={() => setTab('review')}
          />
          <TabButton
            label={i18nT('apps.codeReviewSage.components.prReviewDetail.description')}
            icon={FileText}
            active={tab === 'description'}
            onClick={() => setTab('description')}
          />
          <TabButton
            label={i18nT('apps.codeReviewSage.components.prReviewDetail.comments')}
            icon={MessageSquare}
            active={tab === 'comments'}
            badge={src ? src.comments.length : undefined}
            onClick={() => setTab('comments')}
          />
          <TabButton
            label={i18nT('apps.codeReviewSage.components.prReviewDetail.checks')}
            icon={CircleDot}
            active={tab === 'checks'}
            // The count that matters is what is broken, not how many ran.
            badge={src ? (failing || src.checks.length) : undefined}
            badgeTone={failing ? 'alert' : 'muted'}
            onClick={() => setTab('checks')}
          />
        </div>
      </header>

      <div className="flex-1 min-h-0 overflow-y-auto scrollbar-none px-6 py-5 flex flex-col gap-5">
        {tab === 'review' && reviewBody}
        {tab === 'description' && sourceBody((s) => <DescriptionBody src={s} />)}
        {tab === 'comments' && sourceBody((s) => <CommentsBody src={s} />)}
        {tab === 'checks' && sourceBody((s) => <ChecksBody src={s} />)}
      </div>
    </article>
  )
}
