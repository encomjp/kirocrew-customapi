// The Comments tab: review threads you can read, reply to, and resolve.
//
// The provider returns a FLAT comment list, but inline comments already carry a
// `threadId` (core maps it from a GraphQL reviewThreads query), so the
// conversation structure is recoverable without another request. Grouping is
// what makes the tab usable: a reply is meaningless separated from the line it
// answers, and 40 loose comments in timestamp order is not a review.
//
// Writes go through the gateway's owner-only mutations, which hold the provider
// credential and re-verify that the thread belongs to this pull request.
import { useMemo, useState } from 'react'
import {
  Check, CornerDownRight, Loader2, MessageSquare, MessagesSquare, RotateCcw,
} from 'lucide-react'
import { useMutation, useQueryClient } from '@tanstack/react-query'

import { api } from '../../../api/client'
import MarkdownRenderer from '../../../components/MarkdownRenderer'
import type { PullRequestComment, PullRequestSource } from '../../../types'
import { relativeAge } from '../lib/format'

import { i18nT } from '../../../i18n/t'
/** One review conversation: the comment that opened it plus its replies. */
export interface CommentThread {
  /** Provider thread id — absent for standalone comments and review summaries. */
  threadId: string
  key: string
  root: PullRequestComment
  replies: PullRequestComment[]
  resolved: boolean
  /** Only threads carry an anchor; a top-level comment has no file or line. */
  path: string
  line?: number | null
}

/** Group a flat comment list into threads, preserving provider order.
 *
 * Comments with a `threadId` collapse into one thread (first seen is the root).
 * Everything else — top-level conversation comments and review summaries — stays
 * its own single-comment entry, because that is what it is: GitHub does not
 * thread them either. Exported for testing, since the grouping is the part with
 * real logic in it. */
export function groupThreads(comments: PullRequestComment[]): CommentThread[] {
  const byThread = new Map<string, CommentThread>()
  const out: CommentThread[] = []
  comments.forEach((c, i) => {
    // A review with no body is a state change, not a message — scanners submit
    // one alongside their inline findings. Rendering it as an "(empty)" card
    // pushes the real threads down the page for nothing. Inline comments are
    // never dropped, even empty ones: they belong to a thread.
    if (c.kind === 'review' && !(c.body || '').trim() && !(c.threadId || '').trim()) {
      return
    }
    const tid = (c.threadId || '').trim()
    if (!tid) {
      out.push({
        threadId: '', key: c.id || `c${i}`, root: c, replies: [],
        resolved: Boolean(c.resolved), path: c.path || '', line: c.line,
      })
      return
    }
    const existing = byThread.get(tid)
    if (existing) {
      existing.replies.push(c)
      // Resolution is a property of the thread, so any comment reporting it
      // resolved settles it — the flat payload repeats the flag per comment.
      existing.resolved = existing.resolved || Boolean(c.resolved)
      return
    }
    const thread: CommentThread = {
      threadId: tid, key: tid, root: c, replies: [],
      resolved: Boolean(c.resolved), path: c.path || '', line: c.line,
    }
    byThread.set(tid, thread)
    out.push(thread)
  })
  return out
}

function ThreadComment({ c, reply = false }: { c: PullRequestComment; reply?: boolean }) {
  return (
    <div className={reply ? 'pl-3 border-l-2 border-border' : ''}>
      <div className="flex items-center gap-2 text-[12px] text-muted flex-wrap">
        {reply && (
          <CornerDownRight size={11} className="flex-shrink-0" aria-hidden="true" />
        )}
        <span className="font-medium text-text">{c.author || 'someone'}</span>
        {c.kind === 'review' && c.state && (
          <span className="text-[10.5px] px-1.5 py-0.5 rounded-full bg-card border border-border">
            {c.state.toLowerCase().replace(/_/g, ' ')}
          </span>
        )}
        {c.createdAt && (
          <span className="ml-auto flex-shrink-0">{relativeAge(c.createdAt)}</span>
        )}
      </div>
      <div className="mt-1 text-[13px] text-text">
        <MarkdownRenderer content={c.body || i18nT('apps.codeReviewSage.components.commentThreads.empty_comment')} />
      </div>
    </div>
  )
}

/** Reply box. Collapsed to a link until used, so a long thread list stays
 *  scannable instead of being half composer.
 *
 *  The text is cleared only once the write has actually succeeded: closing on
 *  submit would throw away what the user typed the moment the provider refused,
 *  and leave the error with nowhere to appear. */
function ReplyBox({
  onSubmit, pending, error, label = 'Reply',
}: {
  onSubmit: (body: string) => Promise<unknown>
  pending: boolean
  error: string | null
  label?: string
}) {
  const [open, setOpen] = useState(false)
  const [text, setText] = useState('')
  const trimmed = text.trim()

  if (!open) {
    return (
      <button
        type="button"
        onClick={() => setOpen(true)}
        className="inline-flex items-center gap-1.5 self-start bg-transparent p-0 text-[12px] text-muted hover:text-accent cursor-pointer"
      >
        <MessageSquare size={12} aria-hidden="true" />
        {label}
      </button>
    )
  }

  const send = async () => {
    if (!trimmed || pending) return
    try {
      await onSubmit(trimmed)
    } catch {
      // The mutation surfaces the reason; keep the draft so it can be retried.
      return
    }
    setText('')
    setOpen(false)
  }

  return (
    <div className="flex flex-col gap-1.5">
      <textarea
        value={text}
        onChange={(e) => setText(e.target.value)}
        // Enter inserts a newline (comments are markdown and often multi-line);
        // Cmd/Ctrl+Enter sends, matching the rest of the dashboard's composers.
        onKeyDown={(e) => {
          if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) {
            e.preventDefault()
            void send()
          }
          if (e.key === 'Escape') setOpen(false)
        }}
        rows={3}
        autoFocus
        aria-label={label}
        placeholder={i18nT('apps.codeReviewSage.components.commentThreads.write_a_comment_to_send')}
        className="w-full resize-y rounded-lg border border-border bg-card px-2.5 py-2 text-[13px] text-text placeholder:text-muted outline-none focus:border-accent"
      />
      <div className="flex items-center gap-2">
        <button
          type="button"
          onClick={() => { void send() }}
          disabled={!trimmed || pending}
          className="inline-flex items-center gap-1.5 rounded-md border border-accent bg-accent-subtle px-2.5 py-1 text-[12.5px] font-medium text-accent disabled:opacity-50 cursor-pointer disabled:cursor-default"
        >
          {pending && (
            <Loader2 size={12} className="animate-spin motion-reduce:animate-none" aria-hidden="true" />
          )}
          {label}
        </button>
        <button
          type="button"
          onClick={() => { setText(''); setOpen(false) }}
          className="rounded-md bg-transparent px-1.5 py-1 text-[12.5px] text-muted hover:text-text cursor-pointer"
        >
          {i18nT('apps.codeReviewSage.components.commentThreads.cancel')}
        </button>
        {error && <span className="text-[12px] text-danger">{error}</span>}
      </div>
    </div>
  )
}

export default function CommentThreads({ src }: { src: PullRequestSource }) {
  const qc = useQueryClient()
  const threads = useMemo(() => groupThreads(src.comments), [src.comments])
  const [showResolved, setShowResolved] = useState(false)

  // Every write invalidates the source query, and the gateway already dropped its
  // own cache before dispatching — so the refetch reflects the provider, not a
  // locally-patched guess about what the write did.
  const refresh = () => {
    void qc.invalidateQueries({ queryKey: ['code-review-sage', 'pr-source', src.url] })
  }

  const reply = useMutation({
    mutationFn: ({ threadId, body }: { threadId: string; body: string }) =>
      api.replyToPullRequestThread(src.url, threadId, body),
    onSuccess: refresh,
  })
  const comment = useMutation({
    mutationFn: (body: string) => api.commentOnPullRequest(src.url, body),
    onSuccess: refresh,
  })
  const setResolved = useMutation({
    mutationFn: ({ threadId, resolved }: { threadId: string; resolved: boolean }) =>
      resolved
        ? api.resolvePullRequestThread(src.url, threadId)
        : api.unresolvePullRequestThread(src.url, threadId),
    onSuccess: refresh,
  })

  const resolvedCount = threads.filter((t) => t.resolved).length
  const visible = showResolved ? threads : threads.filter((t) => !t.resolved)
  const writable = src.provider === 'github'

  return (
    <div className="flex flex-col gap-3">
      {/* Resolved threads are settled business: collapsed by default so the open
          ones are what you see, with an honest count rather than silent hiding. */}
      {resolvedCount > 0 && (
        <div className="flex items-center gap-2 text-[12px] text-muted">
          <MessagesSquare size={12} aria-hidden="true" />
          <span>
            {threads.length - resolvedCount} {i18nT('apps.codeReviewSage.components.commentThreads.open')} {resolvedCount} {i18nT('apps.codeReviewSage.components.commentThreads.resolved')}
          </span>
          <button
            type="button"
            onClick={() => setShowResolved((v) => !v)}
            className="ml-auto bg-transparent p-0 text-[12px] text-accent hover:underline cursor-pointer"
          >
            {showResolved
          ? i18nT('apps.codeReviewSage.components.commentThreads.hide_resolved')
          : i18nT('apps.codeReviewSage.components.commentThreads.show_resolved')}
          </button>
        </div>
      )}

      {visible.length === 0 && (
        <div className="text-[12.5px] text-muted italic">
          {threads.length === 0
          ? i18nT('apps.codeReviewSage.components.commentThreads.no_comments_yet')
          : i18nT('apps.codeReviewSage.components.commentThreads.no_open_threads')}
        </div>
      )}

      <ul className="flex flex-col gap-2.5 list-none p-0 m-0">
        {visible.map((t) => (
          <li
            key={t.key}
            className="rounded-lg border border-border bg-card overflow-hidden"
          >
            {/* Anchor line: which file and line this conversation is about. */}
            <div className="flex items-center gap-2 px-2.5 py-1.5 text-[12px] text-muted border-b border-border bg-bg-elevated flex-wrap">
              {t.path ? (
                <span className="font-mono text-[11px] truncate max-w-[260px]" title={t.path}>
                  {t.path}{t.line ? `:${t.line}` : ''}
                </span>
              ) : (
                <span className="text-[11px]">
                  {t.root.kind === 'review' ? 'review' : 'conversation'}
                </span>
              )}
              {t.replies.length > 0 && (
                <span className="text-[10.5px] px-1.5 py-0.5 rounded-full bg-card border border-border tabular-nums">
                  {t.replies.length + 1} {i18nT('apps.codeReviewSage.components.commentThreads.messages')}
                </span>
              )}
              {t.resolved && (
                <span className="inline-flex items-center gap-1 text-[10.5px] px-1.5 py-0.5 rounded-full bg-ok-subtle text-ok">
                  <Check size={10} aria-hidden="true" /> {i18nT('apps.codeReviewSage.components.commentThreads.resolved')}
                </span>
              )}
              {/* Resolve is only offered on real threads: standalone comments and
                  review summaries have nothing to resolve. */}
              {writable && t.threadId && (
                <button
                  type="button"
                  onClick={() => setResolved.mutate({
                    threadId: t.threadId, resolved: !t.resolved,
                  })}
                  disabled={setResolved.isPending}
                  className="ml-auto inline-flex items-center gap-1 rounded-md bg-transparent px-1.5 py-0.5 text-[11.5px] text-muted hover:text-accent disabled:opacity-50 cursor-pointer disabled:cursor-default"
                >
                  {t.resolved
                    ? <><RotateCcw size={11} aria-hidden="true" /> {i18nT('apps.codeReviewSage.components.commentThreads.reopen')}</>
                    : <><Check size={11} aria-hidden="true" /> {i18nT('apps.codeReviewSage.components.commentThreads.resolve')}</>}
                </button>
              )}
            </div>

            <div className="flex flex-col gap-2.5 px-2.5 py-2">
              <ThreadComment c={t.root} />
              {t.replies.map((r) => (
                <ThreadComment key={r.id} c={r} reply />
              ))}
              {writable && t.threadId && (
                <ReplyBox
                  onSubmit={(body) => reply.mutateAsync({ threadId: t.threadId, body })}
                  pending={reply.isPending}
                  error={(reply.error as Error | null)?.message ?? null}
                />
              )}
            </div>
          </li>
        ))}
      </ul>

      {/* A comment that answers nobody's line still needs somewhere to go. */}
      {writable && (
        <div className="border-t border-border pt-2.5">
          <ReplyBox
            label={i18nT('apps.codeReviewSage.components.commentThreads.comment_on_this_pull_request')}
            onSubmit={(body) => comment.mutateAsync(body)}
            pending={comment.isPending}
            error={(comment.error as Error | null)?.message ?? null}
          />
        </div>
      )}
      {!writable && (
        <div className="text-[12px] text-muted italic">
          {i18nT('apps.codeReviewSage.components.commentThreads.replying_is_github_only_for_now_open_the_merge_r')}
        </div>
      )}
    </div>
  )
}
