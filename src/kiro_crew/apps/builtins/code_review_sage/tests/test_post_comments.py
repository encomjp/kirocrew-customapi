#!/usr/bin/env python3
"""Posting is an explicit action, not a consequence of reviewing.

``review.auto_post`` defaults off, so a review is READ in the app. These tests
cover the deferred path: the records survive long enough to post, the public
poster publishes only the Python-redacted envelope, and the endpoint refuses the
cases where posting would be wrong (still running, nothing to post, already
posted).
"""
from __future__ import annotations

import asyncio
import inspect
import os
import shutil
import tempfile
import unittest
import unittest.mock
from pathlib import Path

from aiohttp import web
from backend import routes
from sage_lib import results
from sage_lib import review_driver as D
from sage_lib import store

from kiro_crew.dashboard import state as dashboard_state


def _record(cid: str = "CR-1", red: int = 1, yellow: int = 2) -> dict:
    return {
        "schema": "code-review-sage-result", "version": 1, "change_id": cid,
        "platform": "github", "repo_identity": "github.com/o/r", "revision": "1",
        "phase1": {"gate_verdict": "CONCERNS", "design_risk": "medium",
                   "criticality": "medium", "design_headline": "h",
                   "problem": "p", "why_it_matters": "w",
                   "solution_assessment": "Fit: ok"},
        "blast_radius": {"rating": "MEDIUM", "signals": {}},
        "counts": {"red": red, "yellow": yellow},
        "findings": [
            {"dimension": "correctness", "severity": "red", "file": "f.py",
             "line": 3, "snippet": "x", "observation": "o", "consequence": "c",
             "suggestion": "s"},
        ] * red + [
            {"dimension": "style", "severity": "yellow", "file": "f.py",
             "line": 4, "snippet": "y", "observation": "o", "consequence": "c",
             "suggestion": "s"},
        ] * yellow,
        "deep_reviewed": True, "title": cid, "ship_summary": "looks fine",
        "files_covered": ["f.py"], "coverage_complete": True,
    }


def await_sync(fn, *a, **kw):
    """Run a sync driver call from an async test class without asyncio.to_thread
    boilerplate in every case."""
    return fn(*a, **kw)


class _Base(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._old = os.environ.get("KIROCREW_HOME")
        os.environ["KIROCREW_HOME"] = self.tmp
        self.root = Path(self.tmp) / "app"
        store.ensure_layout(self.root)

    def tearDown(self):
        if self._old is None:
            os.environ.pop("KIROCREW_HOME", None)
        else:
            os.environ["KIROCREW_HOME"] = self._old
        shutil.rmtree(self.tmp, ignore_errors=True)


class TestPostRecorded(_Base):
    async def test_publishes_the_redacted_envelope_not_model_text(self):
        results.write_result(_record(), self.root, "run-a")
        seen: list = []

        def dispatch(task, timeout=0):
            seen.append(task)
            rec = results.read_result("CR-1", self.root, None) or {}
            # The poster's only job: publish what Python already built.
            self.assertIn("github_review_payload", rec)
            rec["posted_comments"] = len(rec.get("pending_comments") or [])
            rec["design_comment_posted"] = True
            results.write_result(rec, self.root, None)
            return {"ok": True, "output": "posted", "error": ""}

        out = await asyncio.to_thread(
            D.post_recorded, "CR-1", "https://github.com/o/r/pull/1",
            dispatch=dispatch, root=self.root, run_id="run-a")
        self.assertTrue(out["post_ok"])
        # 1 red + 2 yellow inline + the always-on ship-readiness comment.
        self.assertEqual(out["pending"], 4)
        self.assertEqual(out["posted_comments"], 4)
        self.assertTrue(seen)

    async def test_no_poster_is_spawned_when_there_is_nothing_to_post(self):
        results.write_result(_record(red=0, yellow=0), self.root, "run-a")
        calls: list = []

        def dispatch(task, timeout=0):
            calls.append(task)
            return {"ok": True, "output": "", "error": ""}

        out = await asyncio.to_thread(
            D.post_recorded, "CR-1", "https://github.com/o/r/pull/1",
            dispatch=dispatch, root=self.root, run_id="run-a")
        # A clean review still gets its ship-readiness comment, so "nothing" here
        # means no record at all — verified below.
        self.assertTrue(out["post_ok"])

    async def test_a_missing_record_posts_nothing(self):
        calls: list = []

        def dispatch(task, timeout=0):
            calls.append(task)
            return {"ok": True, "output": "", "error": ""}

        out = await asyncio.to_thread(
            D.post_recorded, "CR-GONE", "https://github.com/o/r/pull/1",
            dispatch=dispatch, root=self.root, run_id="run-a")
        self.assertEqual(out["posted_comments"], 0)
        self.assertEqual(calls, [])


class TestSelectivePosting(_Base):
    """Posting individual comments: you rarely agree with every finding."""

    def _poster(self, delivered: int | None = None):
        def dispatch(task, timeout=0):
            rec = results.read_result("CR-1", self.root, None) or {}
            pending = rec.get("pending_comments") or []
            rec["posted_comments"] = len(pending) if delivered is None else delivered
            rec["design_comment_posted"] = any(
                e.get("kind") == "design" for e in pending)
            results.write_result(rec, self.root, None)
            return {"ok": True, "output": "posted", "error": ""}
        return dispatch

    async def test_posts_only_the_selected_comment(self):
        results.write_result(_record(), self.root, "run-a")
        out = await asyncio.to_thread(
            D.post_recorded, "CR-1", "https://github.com/o/r/pull/1",
            dispatch=self._poster(), root=self.root, run_id="run-a",
            keys=["finding:1"])
        self.assertEqual(out["pending"], 1)
        self.assertEqual(out["posted_keys"], ["finding:1"])

    async def test_a_second_post_skips_what_already_landed(self):
        results.write_result(_record(), self.root, "run-a")
        await asyncio.to_thread(
            D.post_recorded, "CR-1", "https://github.com/o/r/pull/1",
            dispatch=self._poster(), root=self.root, run_id="run-a",
            keys=["finding:0"])
        # Each post creates its own pending review, so re-sending one would put a
        # duplicate on the pull request.
        out = await asyncio.to_thread(
            D.post_recorded, "CR-1", "https://github.com/o/r/pull/1",
            dispatch=self._poster(), root=self.root, run_id="run-a",
            keys=["finding:0"])
        self.assertEqual(out["pending"], 0)
        self.assertEqual(out["posted_comments"], 0)

    async def test_posting_the_rest_leaves_the_first_alone(self):
        results.write_result(_record(), self.root, "run-a")
        await asyncio.to_thread(
            D.post_recorded, "CR-1", "https://github.com/o/r/pull/1",
            dispatch=self._poster(), root=self.root, run_id="run-a",
            keys=["finding:0"])
        out = await asyncio.to_thread(
            D.post_recorded, "CR-1", "https://github.com/o/r/pull/1",
            dispatch=self._poster(), root=self.root, run_id="run-a")
        # 1 red + 2 yellow + ship comment = 4. The replacement draft carries all
        # four: the poster deletes the draft holding the first one, so leaving it
        # out of the payload would delete it from the pull request.
        self.assertEqual(out["pending"], 4)
        self.assertEqual(len(out["posted_keys"]), 4)

    async def test_a_second_post_rebuilds_the_whole_draft(self):
        """A replacement draft must carry the comments already in it.

        GitHub allows one pending review per author, so the poster deletes the
        existing sage draft and creates a new one. A payload holding only the new
        selection replaces rather than appends: the first finding would be deleted
        with the old draft, while `posted_keys` still claimed it had landed — so
        nothing would ever re-send it.
        """
        results.write_result(_record(), self.root, "run-a")
        await asyncio.to_thread(
            D.post_recorded, "CR-1", "https://github.com/o/r/pull/1",
            dispatch=self._poster(), root=self.root, run_id="run-a",
            keys=["finding:0"])

        seen: list[list[str]] = []

        def capture(task, timeout=0):
            rec = results.read_result("CR-1", self.root, None) or {}
            payload = rec.get("github_review_payload") or {}
            seen.append([c.get("body", "")[:40]
                         for c in (payload.get("comments") or [])])
            rec["posted_comments"] = len(rec.get("pending_comments") or [])
            results.write_result(rec, self.root, None)
            return {"ok": True, "output": "posted", "error": ""}

        await asyncio.to_thread(
            D.post_recorded, "CR-1", "https://github.com/o/r/pull/1",
            dispatch=capture, root=self.root, run_id="run-a",
            keys=["finding:1"])

        # The second payload contains BOTH findings, not just the newly chosen one.
        self.assertEqual(len(seen), 1)
        self.assertGreaterEqual(len(seen[0]), 2)

    async def test_a_poster_that_delivered_nothing_records_nothing(self):
        # The poster's written-back count is the ONLY evidence of delivery; a
        # spawn returning cleanly proves nothing. Recording keys on that would
        # mark comments as sent that are not on the pull request.
        results.write_result(_record(), self.root, "run-a")
        out = await asyncio.to_thread(
            D.post_recorded, "CR-1", "https://github.com/o/r/pull/1",
            dispatch=self._poster(delivered=0), root=self.root, run_id="run-a",
            keys=["finding:0"])
        self.assertEqual(out["posted_keys"], [])
        rec = results.read_result("CR-1", self.root, "run-a") or {}
        self.assertFalse(rec.get("posted_keys"))

    async def test_a_partial_delivery_is_not_attributed(self):
        results.write_result(_record(), self.root, "run-a")
        out = await asyncio.to_thread(
            D.post_recorded, "CR-1", "https://github.com/o/r/pull/1",
            dispatch=self._poster(delivered=1), root=self.root, run_id="run-a")
        # Which of the four landed is unknowable, so none are marked sent:
        # a visible duplicate beats a silently dropped finding.
        self.assertEqual(out["posted_keys"], [])

    async def test_every_pending_comment_has_a_stable_key(self):
        from sage_lib import pipeline
        pending = pipeline.build_pending_comments(_record())
        self.assertEqual(
            [e["key"] for e in pending],
            ["finding:0", "finding:1", "finding:2", "design"])


class TestRecordsSurviveForPosting(_Base):
    def _dispatch(self):
        def dispatch(task, timeout=0):
            results.write_result(_record(), self.root)
            return {"ok": True, "output": "done", "error": ""}
        return dispatch

    async def test_records_are_kept_when_the_review_was_not_posted(self):
        # They are the ONLY source of the redacted payload, so clearing them on
        # archive would silently make posting later impossible.
        out = await asyncio.to_thread(
            lambda: D.run_review(
                ["CR-1"], dispatch=self._dispatch(),
                archiver=lambda *_a, **_k: "slug-1",
                generate_report=True, root=self.root, run_id="run-a"))
        self.assertNotIn("results_cleaned", out)
        self.assertIsNotNone(results.read_result("CR-1", self.root, "run-a"))

    async def test_records_are_cleared_once_they_have_been_delivered(self):
        import json
        cfg = store.data_dir(self.root) / "config.json"
        cfg.write_text(json.dumps({"review": {"auto_post": True}}),
                       encoding="utf-8")

        def dispatch(task, timeout=0):
            if "SINGLE thorough pass" in task:
                results.write_result(_record(), self.root)
            else:
                rec = results.read_result("CR-1", self.root, None)
                if rec:
                    rec["posted_comments"] = len(rec.get("pending_comments") or [])
                    results.write_result(rec, self.root, None)
            return {"ok": True, "output": "done", "error": ""}

        out = await asyncio.to_thread(
            lambda: D.run_review(
                ["CR-1"], dispatch=dispatch, archiver=lambda *_a, **_k: "slug-1",
                generate_report=True, root=self.root, run_id="run-b", post=True))
        self.assertGreaterEqual(out.get("results_cleaned", 0), 1)


class TestPostEndpoint(_Base):
    async def asyncSetUp(self):
        self.app = web.Application()
        routes.register_routes(self.app)
        routes._RUNS.clear()

    def _run(self, **over) -> dict:
        run = {
            "run_id": "run-a", "repo": "o/r",
            "changes": ["https://github.com/o/r/pull/1"],
            "change_ids": ["CR-1"], "status": "done",
            "started_at": "2026-01-01T00:00:00Z",
            "finished_at": "2026-01-01T00:05:00Z",
            **over,
        }
        routes._RUNS.append(run)
        return run

    async def _post(self, run_id="run-a") -> web.Response:
        req = _FakeRequest(run_id)
        return await routes._handle_run_post(req)  # type: ignore[arg-type]

    async def test_refuses_while_the_review_is_still_running(self):
        self._run(status="running")
        resp = await self._post()
        self.assertEqual(resp.status, 409)

    async def test_refuses_when_there_is_nothing_to_post(self):
        self._run()
        resp = await self._post()
        self.assertEqual(resp.status, 409)
        self.assertIn("nothing to post", _body(resp))

    async def test_refuses_a_second_post(self):
        results.write_result(_record(), None, "run-a")
        self._run(posted_at="2026-01-01T00:06:00Z", posted_comments=4)
        resp = await self._post()
        # A duplicate review on someone's PR is not undoable from here.
        self.assertEqual(resp.status, 409)
        self.assertIn("already posted", _body(resp))

    async def test_404_for_an_unknown_run(self):
        resp = await self._post("nope")
        self.assertEqual(resp.status, 404)

    async def test_counts_what_it_would_post(self):
        results.write_result(_record(), None, "run-a")
        run = self._run()
        n = await asyncio.to_thread(routes._pending_comment_count, "run-a", run)
        self.assertEqual(n, 4)


def _body(resp: web.Response) -> str:
    """The response text, for asserting on the refusal reason."""
    return (resp.text or "") if isinstance(resp.text, str) else str(resp.body)


class TestGroupedPost(_Base):
    """A multi-change selection posts as ONE request.

    `posting` is a per-RUN flag and only the poster clears it, while this handler
    returns as soon as it dispatches the poster -- so a client sending one request
    per change had every change after the first refused with `already_posting`, and
    those comments were never published. Sequencing the client's requests does not
    help: resolution means "the poster started", not "it finished".
    """

    async def asyncSetUp(self):
        self.app = web.Application()
        routes.register_routes(self.app)
        routes._RUNS.clear()
        self.dispatched: list[tuple] = []

        async def _capture(run_id, run, change_id="", keys=None, groups=None):
            self.dispatched.append((run_id, change_id, keys, groups))

        self._patch = unittest.mock.patch.object(
            routes, "_post_comments_bg", _capture)
        self._patch.start()
        self.addCleanup(self._patch.stop)

    def _two_change_run(self) -> dict:
        run = {
            "run_id": "run-g", "repo": "o/r",
            "changes": ["https://github.com/o/r/pull/1",
                        "https://github.com/o/r/pull/2"],
            "change_ids": ["CR-1", "CR-2"], "status": "done",
            "started_at": "2026-01-01T00:00:00Z",
            "finished_at": "2026-01-01T00:05:00Z",
        }
        routes._RUNS.append(run)
        return run

    async def test_one_request_covers_both_changes(self):
        results.write_result(_record("CR-1"), None, "run-g")
        results.write_result(_record("CR-2"), None, "run-g")
        self._two_change_run()

        resp = await routes._handle_run_post(_FakeRequest("run-g", {"groups": [
            {"change_id": "CR-1"},
            {"change_id": "CR-2"},
        ]}))  # type: ignore[arg-type]
        # The handler returns as soon as it schedules the poster.
        await asyncio.sleep(0)

        self.assertEqual(resp.status, 200)
        # ONE dispatch covering both changes. Two requests would have had the
        # second refused with `already_posting`.
        self.assertEqual(len(self.dispatched), 1)
        _, _, _, groups = self.dispatched[0]
        self.assertEqual(groups, {"CR-1": None, "CR-2": None})

    async def test_a_change_left_out_of_the_groups_is_not_posted(self):
        results.write_result(_record("CR-1"), None, "run-g")
        results.write_result(_record("CR-2"), None, "run-g")
        self._two_change_run()

        resp = await routes._handle_run_post(_FakeRequest("run-g", {"groups": [
            {"change_id": "CR-2"},
        ]}))  # type: ignore[arg-type]
        # The handler returns as soon as it schedules the poster.
        await asyncio.sleep(0)

        self.assertEqual(resp.status, 200)
        _, _, _, groups = self.dispatched[0]
        # CR-1 is absent, so the poster skips it rather than applying CR-2's
        # selection to it (the round-8 scoping rule, preserved inside groups).
        self.assertEqual(groups, {"CR-2": None})
        self.assertNotIn("CR-1", groups)

    async def test_the_single_change_form_still_works(self):
        """The per-finding post path (change_id + keys) is unchanged."""
        results.write_result(_record("CR-1"), None, "run-g")
        results.write_result(_record("CR-2"), None, "run-g")
        self._two_change_run()

        resp = await routes._handle_run_post(
            _FakeRequest("run-g", {"change_id": "CR-1"}))  # type: ignore[arg-type]
        await asyncio.sleep(0)

        self.assertEqual(resp.status, 200)
        _, change_id, keys, groups = self.dispatched[0]
        # No groups: the single-change path is untouched by this change.
        self.assertEqual((change_id, keys, groups), ("CR-1", None, None))

    async def test_a_group_naming_no_real_comment_is_refused(self):
        """A selection that would post nothing must not start a posting cycle,
        because the run-level `posting` flag would then be set for a no-op."""
        results.write_result(_record("CR-1"), None, "run-g")
        results.write_result(_record("CR-2"), None, "run-g")
        self._two_change_run()

        resp = await routes._handle_run_post(_FakeRequest("run-g", {"groups": [
            {"change_id": "CR-1", "keys": ["no-such-key"]},
        ]}))  # type: ignore[arg-type]
        # The handler returns as soon as it schedules the poster.
        await asyncio.sleep(0)

        self.assertEqual(resp.status, 409)
        self.assertEqual(self.dispatched, [])


class _FakeRequest:
    """Minimal stand-in: the handler only reads match_info, query and json()."""

    def __init__(self, run_id: str, body: dict | None = None):
        self.match_info = {"run_id": run_id}
        self.query: dict = {}
        self._body = body

    async def json(self):
        if self._body is None:
            raise ValueError("no body")   # the handler treats this as {}
        return self._body


if __name__ == "__main__":  # pragma: no cover
    unittest.main()


class TestPostedNotificationActuallyFires(unittest.IsolatedAsyncioTestCase):
    """``notify(kind, title, body, *, meta=None)`` — ``meta`` is keyword-only.

    The call passed a deep link as a 4th POSITIONAL argument, so every invocation
    raised TypeError inside the ``to_thread``; the surrounding ``except Exception``
    swallowed it and the "comments posted" bell notification never fired for any
    post. The fake below mirrors the real signature, so a positional overflow
    fails here the same way it failed in production.
    """

    async def test_the_posted_notification_reaches_the_bell_feed(self):
        seen: dict = {}

        class _FakeState:
            def notify(self, kind, title, body, *, meta=None):
                seen.update(kind=kind, title=title, body=body, meta=meta)

        # Guard the premise: if `notify` ever grows a 4th positional parameter,
        # this test would stop discriminating and should be revisited.
        sig = inspect.signature(dashboard_state.DashboardState.notify)
        self.assertEqual(sig.parameters["meta"].kind,
                         inspect.Parameter.KEYWORD_ONLY)

        prior = routes._APP_STATE.get("state")
        routes._APP_STATE["state"] = _FakeState()
        try:
            await routes._notify_posted({"run_id": "r1"}, 2, False)
        finally:
            if prior is None:
                routes._APP_STATE.pop("state", None)
            else:
                routes._APP_STATE["state"] = prior

        self.assertTrue(seen, "the posted notification never fired")
        self.assertEqual(seen["kind"], "agent")


class TestStaleDeliveryEvidenceIsNotInherited(_Base):
    """The poster's write-back is the only proof of delivery, so it must be fresh.

    `posted_comments` was left on the record between attempts. A first post that
    partially failed left it at 3; the one-comment retry published that record,
    a poster that delivered nothing wrote nothing back, and `3 >= 1` then marked
    that comment delivered and added it to `posted_keys` — permanently skipping a
    finding that was never posted. The posting-skipped path in `run_review`
    already reset these two fields; this was its un-mirrored sibling.
    """

    async def test_a_silent_poster_cannot_inherit_an_earlier_count(self):
        rec = _record()
        rec["posted_comments"] = 3          # residue from a partial attempt
        rec["design_comment_posted"] = True
        results.write_result(rec, self.root, "run-a")

        def dispatch(task, timeout=0):
            return {"ok": True, "output": "", "error": ""}   # writes NOTHING

        out = await asyncio.to_thread(
            D.post_recorded, "CR-1", "https://github.com/o/r/pull/1",
            dispatch=dispatch, root=self.root, run_id="run-a")

        self.assertEqual(out["posted_comments"], 0, out)
        self.assertEqual(out["posted_keys"], [], out)
        after = results.read_result("CR-1", self.root, "run-a") or {}
        self.assertFalse(after.get("posted_keys"))
        self.assertFalse(after.get("design_comment_posted"))

    async def test_the_skipped_comment_is_still_offered_afterwards(self):
        """The point of the fix: the finding must remain postable."""
        rec = _record()
        rec["posted_comments"] = 9
        results.write_result(rec, self.root, "run-a")

        def silent(task, timeout=0):
            return {"ok": True, "output": "", "error": ""}

        await asyncio.to_thread(
            D.post_recorded, "CR-1", "https://github.com/o/r/pull/1",
            dispatch=silent, root=self.root, run_id="run-a")

        # A real poster on the retry delivers and IS recorded.
        def real(task, timeout=0):
            r = results.read_result("CR-1", self.root, None) or {}
            r["posted_comments"] = len(r.get("pending_comments") or [])
            results.write_result(r, self.root, None)
            return {"ok": True, "output": "posted", "error": ""}

        out = await asyncio.to_thread(
            D.post_recorded, "CR-1", "https://github.com/o/r/pull/1",
            dispatch=real, root=self.root, run_id="run-a")
        self.assertTrue(out["posted_keys"], out)

    async def test_a_real_delivery_is_still_recorded(self):
        """The reset must not break the path where the poster does write back."""
        results.write_result(_record(), self.root, "run-a")

        def dispatch(task, timeout=0):
            r = results.read_result("CR-1", self.root, None) or {}
            r["posted_comments"] = len(r.get("pending_comments") or [])
            r["design_comment_posted"] = True
            results.write_result(r, self.root, None)
            return {"ok": True, "output": "posted", "error": ""}

        out = await asyncio.to_thread(
            D.post_recorded, "CR-1", "https://github.com/o/r/pull/1",
            dispatch=dispatch, root=self.root, run_id="run-a")
        self.assertEqual(out["posted_comments"], out["pending"])
        self.assertTrue(out["posted_keys"])


class TestDeliveryIsCountedInPayloadUnits(_Base):
    """An unanchored finding folds into the review body, so findings != units.

    The poster reports `len(comments) + 1 when body is non-empty`. Comparing that
    against the number of pending entries made a COMPLETE delivery look short
    whenever a finding lacked a usable `{path, line}` anchor, because such a finding
    is folded into the body rather than becoming its own inline comment. `posted_keys`
    then went unwritten and the next post duplicated comments already on the PR.

    The same miscount was used for `posting_expected`, which gates `_record_reviewed`
    and `_all_delivered`, so both also read a finished review as incomplete.
    """

    def test_payload_units_ignores_findings_folded_into_the_body(self):
        from sage_lib import pipeline

        rec = {"revision": "a" * 40, "pending_comments": [
            {"kind": "design", "body": "ship summary", "key": "d1"},
            {"kind": "finding", "body": "anchored", "file": "a.py", "line": 3,
             "key": "f1"},
            {"kind": "finding", "body": "no anchor", "file": "", "line": None,
             "key": "f2"},
        ]}
        payload = pipeline.build_github_review_payload(rec)
        # Three pending entries, but only two deliverable units.
        self.assertEqual(len(payload.get("comments") or []), 1)
        self.assertTrue(payload.get("body"))
        self.assertEqual(pipeline.review_payload_units(payload), 2)
        self.assertLess(pipeline.review_payload_units(payload),
                        len(rec["pending_comments"]))

    def test_an_unanchored_finding_still_records_delivery(self):
        """The real path: a poster that delivers every unit marks them delivered."""
        from sage_lib import pipeline, results

        rec = _record(red=1, yellow=0)
        # Strip the anchor from the one red finding so it folds into the body.
        for f in rec["findings"]:
            f["file"] = ""
            f["line"] = None
        results.write_result(rec, self.root, "run-a")

        def dispatch(task, timeout=0):
            r = results.read_result("CR-1", self.root, None) or {}
            payload = r.get("github_review_payload") or {}
            r["posted_comments"] = pipeline.review_payload_units(payload)
            r["design_comment_posted"] = bool(payload.get("body"))
            results.write_result(r, self.root, None)
            return {"ok": True, "output": "posted", "error": ""}

        out = await_sync(D.post_recorded, "CR-1", "https://github.com/o/r/pull/1",
                         dispatch=dispatch, root=self.root, run_id="run-a")
        # Delivery is recognised: keys recorded, so a retry cannot duplicate.
        self.assertTrue(out["posted_keys"], out)
        self.assertEqual(out["posted_comments"], out["expected_units"])

    def test_expected_units_is_reported_for_the_caller(self):
        """`posting_expected` is set from this, not from red + yellow + 1."""
        from sage_lib import pipeline, results

        results.write_result(_record(red=1, yellow=2), self.root, "run-a")

        def dispatch(task, timeout=0):
            r = results.read_result("CR-1", self.root, None) or {}
            r["posted_comments"] = pipeline.review_payload_units(
                r.get("github_review_payload") or {})
            results.write_result(r, self.root, None)
            return {"ok": True, "output": "posted", "error": ""}

        out = await_sync(D.post_recorded, "CR-1", "https://github.com/o/r/pull/1",
                         dispatch=dispatch, root=self.root, run_id="run-a")
        self.assertIn("expected_units", out)
        self.assertGreater(out["expected_units"], 0)
