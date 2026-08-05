#!/usr/bin/env python3
"""push_guard.py - pre-push stale-base guard for the prepare-pr skill.

Verifies that the current HEAD is safe to force-push by checking:
1. The fetch of origin/<base> succeeds (fail closed on network error).
2. origin/<base> is an ancestor of HEAD (i.e. HEAD sits on the freshly
   fetched base tip, not on a stale fork point that would cause the squash
   to bake in reversions of newer base changes).
3. The number of commits HEAD is ahead of origin/<base> is plausibly small
   (default threshold: 5 commits for a single-commit PR workflow; configurable
   via --max-ahead).
4. None of the ahead-commits are patch-equivalent to commits in a bounded
   window (REPLAY_HISTORY_WINDOW) of origin/<base> history.  Comparison uses
   `git patch-id --stable` so renames, whitespace, and commit metadata are
   ignored — only the semantic diff matters.  The window is bounded because
   full history is unbounded cost, and replayed commits from a recent stale
   fork are by construction recent.

This prevents the catastrophic failure mode where a worktree branched from a
local integration trunk (kiki-trunk) carries 100+ unshipped commits that get
force-pushed to the remote feature branch, clobbering upstream work.

Portable: stdlib only; shells out to git via argument lists.

Usage:  python3 push_guard.py [--base <branch>] [--max-ahead <N>]
Exit:   0 SAFE | 40 REFUSED (stale base detected) | 2 environment error
"""

import argparse
import re
import subprocess
import sys

# Single source of truth for the default max-ahead threshold.  Shared by
# preflight.py (which imports this constant) so the two scripts cannot drift.
DEFAULT_MAX_AHEAD = 5

# Maximum number of base-history commits to scan for patch-id equivalence in
# the replay-detection check.  Bounded because full history is unbounded cost,
# and replayed commits from a recent stale fork are by construction recent
# (the operator branched from a stale tip that was N commits behind; the
# replayed patches are in that window).  500 is generous — a typical PR
# workflow replays at most a few dozen commits from a stale integration trunk.
REPLAY_HISTORY_WINDOW = 500

# Pattern that matches URL userinfo (user, user:pass, or token before @host).
# Covers https://user:token@host, ssh://user@host, and bare token@host forms.
_URL_USERINFO_RE = re.compile(r"://[^/@\s]+@")

# Pattern that matches URL query strings on scheme-bearing URLs.
# Credential-bearing query parameters (private_token=, x-access-token=,
# access_token=, token=, etc.) are common in self-hosted Git forges and CI
# systems.  Rather than enumerating parameter names (fragile — new names
# appear), redact the ENTIRE query string: the diagnostic value is in the
# host/path, not the query.  Matches scheme://...?<non-whitespace>.
_URL_QUERY_RE = re.compile(r"((?:https?|ssh|git)://[^\s?\"']*)\?[^\s\"']+")


def redact_credentials(text: str) -> str:
    """Strip URL credentials from diagnostic text to prevent leaks.

    Two passes:
    1. Userinfo: replaces `://user:token@` or `://user@` with
       `://<redacted>@`.
    2. Query string: replaces `?<params>` on any scheme-bearing URL with
       `?<redacted>` (covers private_token, access_token, x-access-token,
       and any future credential-bearing query parameter).

    The host and path are preserved for diagnostic value.
    Shared by preflight.py (which imports this helper).
    """
    text = _URL_USERINFO_RE.sub("://<redacted>@", text)
    text = _URL_QUERY_RE.sub(r"\1?<redacted>", text)
    return text


def run(args):
    """Run a command; return (returncode, stdout, stderr) as stripped text."""
    try:
        p = subprocess.run(args, capture_output=True, text=True, errors="replace")
        return p.returncode, p.stdout.strip(), p.stderr.strip()
    except OSError as exc:
        return 127, "", "{}: {}".format(args[0], exc)


def err(msg):
    sys.stderr.write(msg + "\n")


def _resolve_base(base_arg):
    """Resolve the base branch name from arg, symbolic ref, or default."""
    base = base_arg
    if not base:
        sym = run(["git", "symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD"])[1]
        if sym.startswith("origin/"):
            base = sym[len("origin/") :]
        if not base:
            base = "main"
    return base


def _fetch_base(base):
    """Fetch origin/<base>; return 0 on success or 40 on failure.

    Uses an explicit refspec (+refs/heads/<base>:refs/remotes/origin/<base>)
    so the remote-tracking ref is always updated regardless of the clone's
    configured remote.origin.fetch (e.g. single-branch clones, narrow CI
    checkouts).  The leading '+' ensures non-fast-forward updates are accepted
    (required after an upstream force-push of the base branch).
    """
    print("Fetching origin/{} ...".format(base))
    refspec = "+refs/heads/{}:refs/remotes/origin/{}".format(base, base)
    fetch_rc, _, fetch_err = run(["git", "fetch", "--quiet", "origin", refspec])
    if fetch_rc != 0:
        err(
            "REFUSED: git fetch origin {} failed. Cannot verify merge-base "
            "freshness — refusing to push on a potentially stale ref.\n"
            "  error: {}".format(base, redact_credentials(fetch_err[:300]))
        )
        return 40
    return 0


def _check_single_on_base(base):
    """Post-squash structural guard: assert HEAD~1 == origin/<base>.

    After a squash, the single commit should sit directly on the freshly
    fetched origin/<base>.  If HEAD~1 != origin/<base>, the squash landed
    on a stale ref or the branch carries unexpected history.

    Returns: 0 safe, 40 refused.
    """
    rc, head_parent, _ = run(["git", "rev-parse", "HEAD~1"])
    if rc != 0:
        err(
            "REFUSED: cannot resolve HEAD~1. The branch may have no parent "
            "commit (single root commit with no base)."
        )
        return 40

    rc, origin_base_sha, _ = run(["git", "rev-parse", "origin/{}".format(base)])
    if rc != 0:
        err("REFUSED: cannot resolve origin/{}.".format(base))
        return 40

    head_sha = run(["git", "rev-parse", "HEAD"])[1][:12]

    print("HEAD~1:          " + head_parent[:12])
    print("origin/{}:     {}".format(base, origin_base_sha[:12]))
    print("HEAD:            " + head_sha)

    if head_parent != origin_base_sha:
        err(
            "REFUSED: HEAD~1 ({}) != origin/{} ({}). "
            "The squashed commit does not sit directly on the freshly fetched "
            "remote base — either the squash landed on a stale ref or the "
            "branch carries unexpected history.\n"
            "  To fix: rebase onto the fresh origin/{} first "
            "(git rebase origin/{}), then re-squash "
            "(git reset --soft origin/{} && git commit).".format(
                head_parent[:12], base, origin_base_sha[:12], base, base, base
            )
        )
        return 40

    print("STATUS: SAFE TO PUSH (single commit on base)")
    return 0


def _check_pre_squash(base, max_ahead):
    """Pre-squash guard: merge-base ancestry, commit count, replayed commits.

    Returns: 0 safe, 40 refused.
    """
    # Compute merge-base of HEAD and freshly-fetched origin/<base>.
    rc, merge_base, _ = run(["git", "merge-base", "HEAD", "origin/{}".format(base)])
    if rc != 0 or not merge_base:
        err(
            "REFUSED: cannot compute merge-base between HEAD and origin/{}. "
            "The branch may have no common history with the remote base.".format(base)
        )
        return 40

    # Verify origin/<base> is an ancestor of HEAD — i.e. HEAD sits on the
    # freshly fetched base tip.  After a correct rebase (Phase 1 step 2),
    # this is always true.  If it fails, the branch forks from a stale base
    # and the squash would bake in reversions of newer base changes.
    rc, _, _ = run(["git", "merge-base", "--is-ancestor", "origin/{}".format(base), "HEAD"])
    if rc != 0:
        err(
            "REFUSED: HEAD is not based on the fresh origin/{} tip — the "
            "branch forks from a stale base and squashing would bake in "
            "reversions of newer base changes. Rebase onto origin/{} "
            "first.".format(base, base)
        )
        return 40

    # Count commits HEAD is ahead of origin/<base>.
    rc, count_str, _ = run(["git", "rev-list", "--count", "origin/{}..HEAD".format(base)])
    if rc != 0:
        err("REFUSED: cannot count commits ahead of origin/{}.".format(base))
        return 40

    try:
        ahead = int(count_str)
    except ValueError:
        err("REFUSED: unexpected rev-list output: {}".format(count_str))
        return 40

    origin_base_sha = run(["git", "rev-parse", "origin/{}".format(base)])[1][:12]
    head_sha = run(["git", "rev-parse", "HEAD"])[1][:12]

    print("merge-base:      " + merge_base[:12])
    print("origin/{}:     {}".format(base, origin_base_sha))
    print("HEAD:            " + head_sha)
    print("commits ahead:   {}".format(ahead))
    print("max allowed:     {}".format(max_ahead))

    if ahead > max_ahead:
        err(
            "REFUSED: HEAD is {} commits ahead of origin/{} (max allowed: {}). "
            "This is far too many for a squashed single-commit PR — the branch "
            "likely carries unshipped local integration commits that would "
            "clobber upstream work if force-pushed.\n"
            "  To fix (if you authored all {} commits): squash them down "
            "(git reset --soft origin/{} && git commit) so the branch carries "
            "a single deliverable commit.\n"
            "  To fix (if any ahead-commit is unfamiliar): STOP and diagnose — "
            "do not squash foreign history. The branch may have picked up "
            "commits from a local integration trunk.\n"
            "  To fix (stale fork): rebase onto origin/{} "
            "(git rebase origin/{}) so HEAD sits on the fresh remote "
            "base tip.".format(ahead, base, max_ahead, ahead, base, base, base)
        )
        return 40

    # Detect replayed commits via patch-id comparison.
    # Compare each ahead-commit's patch-id against a BOUNDED window of
    # origin/<base> history.  If any ahead-commit is patch-equivalent to a
    # base-history commit, the branch replays upstream patches (e.g. from a
    # stale fork that cherry-picked base commits back).  The window is bounded
    # (REPLAY_HISTORY_WINDOW) because full history is unbounded cost, and
    # replayed commits from a recent stale fork are by construction recent.
    #
    # Step 1: get patch-ids for ahead-commits (origin/<base>..HEAD).
    rc, ahead_revs, _ = run(["git", "rev-list", "origin/{}..HEAD".format(base)])
    if rc != 0 or not ahead_revs:
        # No ahead commits or error — nothing to check.
        print("STATUS: SAFE TO PUSH")
        return 0

    ahead_patch_ids: dict[str, str] = {}  # patch-id → commit-sha
    for commit_sha in ahead_revs.splitlines():
        # Get the patch content and pipe to patch-id.
        diff_rc, diff_out, _ = run(["git", "diff-tree", "-p", commit_sha])
        if diff_rc != 0 or not diff_out:
            continue
        # Feed the diff to git patch-id --stable via stdin.
        try:
            pid_proc = subprocess.run(
                ["git", "patch-id", "--stable"],
                input=diff_out,
                capture_output=True,
                text=True,
                errors="replace",
            )
            if pid_proc.returncode == 0 and pid_proc.stdout.strip():
                patch_id = pid_proc.stdout.strip().split()[0]
                ahead_patch_ids[patch_id] = commit_sha
        except OSError:
            continue

    if not ahead_patch_ids:
        print("STATUS: SAFE TO PUSH")
        return 0

    # Step 2: get patch-ids for bounded base history.
    rc, base_revs, _ = run(
        [
            "git",
            "rev-list",
            "--max-count={}".format(REPLAY_HISTORY_WINDOW),
            "origin/{}".format(base),
        ]
    )
    if rc != 0 or not base_revs:
        print("STATUS: SAFE TO PUSH")
        return 0

    base_patch_ids: dict[str, str] = {}  # patch-id → commit-sha
    for commit_sha in base_revs.splitlines():
        diff_rc, diff_out, _ = run(["git", "diff-tree", "-p", commit_sha])
        if diff_rc != 0 or not diff_out:
            continue
        try:
            pid_proc = subprocess.run(
                ["git", "patch-id", "--stable"],
                input=diff_out,
                capture_output=True,
                text=True,
                errors="replace",
            )
            if pid_proc.returncode == 0 and pid_proc.stdout.strip():
                patch_id = pid_proc.stdout.strip().split()[0]
                base_patch_ids[patch_id] = commit_sha
        except OSError:
            continue

    # Step 3: find matches.
    replayed_pairs: list[tuple[str, str]] = []  # (ahead-sha, base-sha)
    for patch_id, ahead_sha in ahead_patch_ids.items():
        if patch_id in base_patch_ids:
            replayed_pairs.append((ahead_sha, base_patch_ids[patch_id]))

    if replayed_pairs:
        pair_strs = ["{} ↔ {}".format(a[:12], b[:12]) for a, b in replayed_pairs]
        err(
            "REFUSED: {} ahead-commit(s) are patch-equivalent to commits "
            "already on origin/{} — the branch replays upstream history.\n"
            "  replayed (ahead ↔ base): {}\n"
            "  To fix: STOP and diagnose. Do not squash — one or more "
            "ahead-commits duplicate patches already on the base branch. "
            "Rebase onto a fresh origin/{} so only novel changes remain, "
            "or cherry-pick your original commits onto "
            "origin/{}.".format(len(replayed_pairs), base, ", ".join(pair_strs), base, base)
        )
        return 40

    print("STATUS: SAFE TO PUSH")
    return 0


def main():
    parser = argparse.ArgumentParser(description="Pre-push stale-base guard")
    parser.add_argument(
        "--base",
        default="",
        help="Base branch name (without origin/ prefix). "
        "Auto-detected from PR or origin/HEAD if omitted.",
    )
    parser.add_argument(
        "--max-ahead",
        type=int,
        default=DEFAULT_MAX_AHEAD,
        help="Maximum commits HEAD may be ahead of origin/<base> (default: {}).".format(
            DEFAULT_MAX_AHEAD
        ),
    )
    parser.add_argument(
        "--require-single-on-base",
        action="store_true",
        default=False,
        help="Post-squash mode: assert HEAD~1 == origin/<base> after a fresh "
        "fetch (the single squashed commit sits directly on the remote base).",
    )
    args = parser.parse_args()

    # Must be in a git repo.
    if run(["git", "rev-parse", "--is-inside-work-tree"])[0] != 0:
        err("ERROR: not inside a git repository.")
        return 2

    base = _resolve_base(args.base)

    # Fetch origin/<base> — MUST succeed (fail closed) for both modes.
    fetch_result = _fetch_base(base)
    if fetch_result != 0:
        return fetch_result

    if args.require_single_on_base:
        return _check_single_on_base(base)
    else:
        return _check_pre_squash(base, args.max_ahead)


if __name__ == "__main__":
    sys.exit(main())
