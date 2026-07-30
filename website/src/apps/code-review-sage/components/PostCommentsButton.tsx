// "Post comments" — publish a reviewed run's findings to its pull request.
//
// Reviews are never posted automatically (``review.auto_post`` defaults off):
// writing to someone else's pull request is a side effect you opt into, not a
// consequence of running a review. This is that opt-in, taken after you have read
// the findings.
//
// Two clicks, deliberately. The post is a visible action on a shared artifact
// that this app cannot undo — the comments land as a GitHub review from your
// account. So the first click states the count and asks; only the second sends.
import { useEffect, useState } from 'react'
import { AlertTriangle, Check, Loader2, MessageSquarePlus } from 'lucide-react'

import { relativeAge } from '../lib/format'
import type { Run, RunReport } from '../lib/types'

import { i18nT } from '../../../i18n/t'
/** How many comments a post would publish.
 *
 * Mirrors the backend's ``posting_expected``: every must-fix and should-fix
 * finding becomes an
 * inline comment, plus one always-on ship-readiness comment per reviewed change.
 * Derived from the report the pane already has, so labelling the button honestly
 * costs no extra request — the endpoint recounts from the records before sending
 * and refuses a no-op, so this is a label, never the authority. */
export function pendingCommentCount(report: RunReport | null): number {
  if (!report?.rows?.length) return 0
  return report.rows.reduce(
    (n, row) => n + (row.red ?? 0) + (row.yellow ?? 0) + 1, 0)
}

export default function PostCommentsButton({
  run, report, onPost, posting = false, error = null,
}: {
  run: Run
  report: RunReport | null
  onPost: () => void
  posting?: boolean
  error?: string | null
}) {
  const [confirming, setConfirming] = useState(false)
  const pending = pendingCommentCount(report)

  // Drop the confirm prompt if the run changes under it, so a click cannot land
  // on a different pull request than the one you were looking at.
  useEffect(() => setConfirming(false), [run.run_id])

  if (run.status === 'running') return null

  if (run.posted_at) {
    return (
      <span
        className="inline-flex items-center gap-1.5 text-[12.5px] text-ok"
        title={i18nT('apps.codeReviewSage.components.postCommentsButton.posted_age',
          { age: relativeAge(run.posted_at) })}
      >
        <Check size={13} aria-hidden="true" />
        {/* One key, not "Posted" + number + "comment(s)": a translator cannot
            reorder three sibling fragments, and several languages need the count
            in a different position. */}
        {i18nT('apps.codeReviewSage.components.postCommentsButton.posted_comments',
          { count: run.posted_comments ?? 0 })}
      </span>
    )
  }

  if (posting) {
    return (
      <span className="inline-flex items-center gap-1.5 text-[12.5px] text-muted">
        <Loader2 size={13} className="animate-spin motion-reduce:animate-none" aria-hidden="true" />
        {i18nT('apps.codeReviewSage.components.postCommentsButton.posting')}
      </span>
    )
  }

  // Nothing was flagged: there is no comment to write, so offering the action
  // would be a dead end.
  if (pending === 0) return null

  if (confirming) {
    return (
      <span className="inline-flex items-center gap-2 text-[12.5px]">
        <span className="text-muted">
          {i18nT('apps.codeReviewSage.components.postCommentsButton.confirm_post',
            { count: pending })}
        </span>
        <button
          type="button"
          onClick={() => { setConfirming(false); onPost() }}
          className="inline-flex items-center gap-1.5 rounded-md border border-accent bg-accent-subtle px-2.5 py-1 text-[12.5px] font-medium text-accent hover:bg-accent/20 cursor-pointer"
        >
          {i18nT('apps.codeReviewSage.components.postCommentsButton.post_action')}
        </button>
        <button
          type="button"
          onClick={() => setConfirming(false)}
          className="rounded-md bg-transparent px-1.5 py-1 text-[12.5px] text-muted hover:text-text cursor-pointer"
        >
          {i18nT('apps.codeReviewSage.components.postCommentsButton.cancel')}
        </button>
      </span>
    )
  }

  return (
    <span className="inline-flex items-center gap-2">
      {error && (
        <span className="inline-flex items-center gap-1 text-[12px] text-danger" title={error}>
          <AlertTriangle size={12} aria-hidden="true" />
          {i18nT('apps.codeReviewSage.components.postCommentsButton.post_failed')}
        </span>
      )}
      <button
        type="button"
        onClick={() => setConfirming(true)}
        title={i18nT('apps.codeReviewSage.components.postCommentsButton.publish_these_findings_as_a_review_on_the_pull_r')}
        className="inline-flex items-center gap-1.5 rounded-md border border-border bg-card px-2.5 py-1 text-[12.5px] text-text hover:text-accent hover:border-accent cursor-pointer"
      >
        <MessageSquarePlus size={13} aria-hidden="true" />
        {i18nT('apps.codeReviewSage.components.postCommentsButton.post_count', { count: pending })}
      </button>
    </span>
  )
}
