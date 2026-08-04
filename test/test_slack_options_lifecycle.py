"""OPTIONS lifecycle on the Slack surface.

Two behaviours are locked in here:

* every outbound path renders a trailing ``[OPTIONS: …]`` tag as a control,
  never as literal text — including the link-time backfill, which used to post
  message bodies verbatim;
* a control stops being answerable once the conversation moves past the question
  it asked, whichever surface the next turn arrives on.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_state

from kiro_crew.slack.format import (
    OPTIONS_CHECKBOXES_ACTION,
    OPTIONS_SUBMIT_ACTION,
    replace_options_blocks,
)
from kiro_crew.slack.outbound import (
    PostedOptions,
    expire_options,
    post_assistant_text,
    render_for_slack,
)

AWS_KEY = "AKIAIOSFODNN7EXAMPLE"


def _slack() -> MagicMock:
    slack = MagicMock()
    slack.post_message = AsyncMock(return_value="body_ts")
    slack.post_blocks = AsyncMock(return_value="opt_ts")
    slack.update_message = AsyncMock()
    return slack


def _posted_texts(slack: MagicMock) -> list[str]:
    return [c.args[1] for c in slack.post_message.await_args_list]


def _is_live_control(blocks: list[dict]) -> bool:
    """True when *blocks* contain a clickable OPTIONS control."""
    return any(
        el.get("action_id") in (OPTIONS_CHECKBOXES_ACTION, OPTIONS_SUBMIT_ACTION)
        for b in blocks
        if b.get("type") == "actions"
        for el in b.get("elements", [])
    )


def _context_text(blocks: list[dict]) -> str:
    return " ".join(
        el.get("text", "")
        for b in blocks
        if b.get("type") == "context"
        for el in b.get("elements", [])
    )


class TestPostAssistantText:
    """The shared outbound seam."""

    @pytest.mark.asyncio
    async def test_options_post_as_control_not_literal_text(self):
        """The tag never reaches the channel as text a reader has to parse."""
        slack = _slack()

        posted = await post_assistant_text(
            slack, "C1", "Pick one.\n\n[OPTIONS: Ship it | Hold off]", "T1"
        )

        assert all("[OPTIONS:" not in t for t in _posted_texts(slack))
        assert _posted_texts(slack) == ["Pick one."]
        blocks = slack.post_blocks.await_args.args[1]
        assert _is_live_control(blocks)
        assert posted is not None
        assert posted.choices == ("Ship it", "Hold off")
        assert posted.ts == "opt_ts"
        assert posted.channel == "C1"

    @pytest.mark.asyncio
    async def test_no_options_posts_nothing_extra(self):
        slack = _slack()

        posted = await post_assistant_text(slack, "C1", "Just an answer.", "T1")

        assert posted is None
        slack.post_blocks.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_non_interactive_renders_spent_and_is_not_recorded(self):
        """Replayed history must not invite an answer to a stale question."""
        slack = _slack()

        posted = await post_assistant_text(
            slack, "C1", "Old question.\n\n[OPTIONS: A | B]", "T1", interactive=False
        )

        blocks = slack.post_blocks.await_args.args[1]
        assert not _is_live_control(blocks)
        assert _context_text(blocks) == "~A~  |  ~B~"
        # Nothing to expire later — it was never answerable.
        assert posted is None

    @pytest.mark.asyncio
    async def test_markdown_is_converted_to_mrkdwn(self):
        slack = _slack()

        await post_assistant_text(slack, "C1", "**bold** and [text](http://x)", "T1")

        body = _posted_texts(slack)[0]
        assert "*bold*" in body
        assert "<http://x|text>" in body

    @pytest.mark.asyncio
    async def test_credential_in_a_choice_is_redacted_before_it_becomes_a_value(self):
        """Redaction runs over the whole text BEFORE the tag is split off.

        Extracting first would lift the raw choice out of redaction's reach and
        straight into a Block Kit button value.
        """
        slack = _slack()

        posted = await post_assistant_text(
            slack, "C1", f"Choose.\n\n[OPTIONS: use {AWS_KEY} | cancel]", "T1"
        )

        assert posted is not None
        assert AWS_KEY not in " ".join(posted.choices)
        assert "REDACTED" in posted.choices[0]
        blocks = slack.post_blocks.await_args.args[1]
        assert AWS_KEY not in str(blocks)

    @pytest.mark.asyncio
    async def test_truncation_caps_the_body_but_keeps_the_control(self):
        slack = _slack()

        posted = await post_assistant_text(
            slack,
            "C1",
            "x" * 5000 + "\n\n[OPTIONS: A | B]",
            "T1",
            truncate_to=100,
        )

        assert len(_posted_texts(slack)[0]) <= 100
        assert posted is not None
        assert posted.choices == ("A", "B")

    @pytest.mark.asyncio
    async def test_truncation_redacts_before_slicing(self):
        """A credential straddling the cut must not survive as a fragment."""
        slack = _slack()

        await post_assistant_text(
            slack, "C1", "y" * 40 + AWS_KEY, "T1", truncate_to=50
        )

        assert AWS_KEY[:10] not in _posted_texts(slack)[0]

    @pytest.mark.asyncio
    async def test_a_credential_straddling_the_formatter_cut_is_redacted(self):
        """``to_slack_mrkdwn`` truncates at ``SLACK_MAX_TEXT`` on its own.

        Redacting after it let a credential sitting across that cut survive as a
        prefix — short enough that the credential regex no longer matched it, so
        the fragment posted to the channel. The fragment lands near the 39,000th
        character, so this has to inspect every chunk, not just the first.
        """
        from kiro_crew.slack.format import SLACK_MAX_TEXT

        slack = _slack()

        await post_assistant_text(
            slack, "C1", "y" * (SLACK_MAX_TEXT - 5) + AWS_KEY, "T1"
        )

        posted = "".join(_posted_texts(slack))
        assert AWS_KEY not in posted
        assert AWS_KEY[:5] not in posted

    @pytest.mark.asyncio
    async def test_a_body_past_the_formatter_cut_still_yields_its_control(self):
        """The tag trails the body, so truncation reached it first and ate it.

        A long reply then arrived with its question unanswerable. Splitting the
        tag off before conversion keeps it out of the formatter's reach.
        """
        from kiro_crew.slack.format import SLACK_MAX_TEXT

        slack = _slack()

        posted = await post_assistant_text(
            slack, "C1", "y" * (SLACK_MAX_TEXT + 500) + "\n\n[OPTIONS: A | B]", "T1"
        )

        assert posted is not None
        assert posted.choices == ("A", "B")
        assert _is_live_control(slack.post_blocks.await_args.args[1])

    @pytest.mark.asyncio
    async def test_pipe_separated_choices_survive_intact(self):
        """A choice list is `|`-separated, the same shape as a table row.

        Characterization, not a regression guard: the formatter does not flatten
        the tag today. Extracting before conversion keeps it that way even if the
        table handling later widens.
        """
        slack = _slack()

        posted = await post_assistant_text(
            slack, "C1", "Pick.\n\n[OPTIONS: Ship it | Hold off | Ask again]", "T1"
        )

        assert posted is not None
        assert posted.choices == ("Ship it", "Hold off", "Ask again")

    def test_render_for_slack_returns_body_and_choices(self):
        body, choices = render_for_slack("Answer.\n\n[OPTIONS: A | B]")
        assert body == "Answer."
        assert choices == ["A", "B"]


class TestExpireOptions:
    """Spending a control the conversation has moved past."""

    @pytest.mark.asyncio
    async def test_every_choice_is_struck_through(self):
        slack = _slack()
        posted = await post_assistant_text(
            slack, "C1", "Pick.\n\n[OPTIONS: A | B]", "T1"
        )
        assert posted is not None

        await expire_options(slack, posted)

        blocks = slack.update_message.await_args.kwargs["blocks"]
        assert not _is_live_control(blocks)
        assert _context_text(blocks) == "~A~  |  ~B~"
        assert slack.update_message.await_args.args == ("C1", "opt_ts")

    @pytest.mark.asyncio
    async def test_surrounding_blocks_survive(self):
        """A turn's timing footer shares the message with its control."""
        slack = _slack()
        footer = {"type": "section", "text": {"type": "mrkdwn", "text": "12.3s"}}
        control = {
            "type": "actions",
            "elements": [
                {"type": "checkboxes", "action_id": OPTIONS_CHECKBOXES_ACTION},
                {"type": "button", "action_id": OPTIONS_SUBMIT_ACTION},
            ],
        }
        posted = PostedOptions(
            channel="C1",
            ts="opt_ts",
            choices=("A", "B"),
            blocks=(footer, control),
            text="12.3s",
        )

        await expire_options(slack, posted)

        blocks = slack.update_message.await_args.kwargs["blocks"]
        assert footer in blocks
        assert control not in blocks
        assert not _is_live_control(blocks)

    @pytest.mark.asyncio
    async def test_slack_failure_is_swallowed(self):
        """A thread keeping a live control is the status quo, not a new failure."""
        slack = _slack()
        slack.update_message = AsyncMock(side_effect=Exception("channel_not_found"))
        posted = PostedOptions(
            channel="C1", ts="opt_ts", choices=("A",), blocks=({"type": "actions"},)
        )

        await expire_options(slack, posted)  # must not raise

        slack.update_message.assert_awaited_once()


class TestReplaceOptionsBlocks:
    """The block surgery shared by the click path and the expiry path."""

    def test_replaces_control_in_place_and_preserves_the_rest(self):
        top = {"type": "section", "text": {"type": "mrkdwn", "text": "top"}}
        control = {
            "type": "actions",
            "elements": [{"type": "checkboxes", "action_id": OPTIONS_CHECKBOXES_ACTION}],
        }
        tail = {"type": "context", "elements": [{"type": "mrkdwn", "text": "tail"}]}
        spent = [{"type": "context", "elements": [{"type": "mrkdwn", "text": "~A~"}]}]

        result = replace_options_blocks([top, control, tail], spent)

        assert result == [top, spent[0], tail]

    def test_unrelated_actions_block_is_left_alone(self):
        other = {
            "type": "actions",
            "elements": [{"type": "button", "action_id": "mc_link_dashboard"}],
        }
        spent = [{"type": "context", "elements": [{"type": "mrkdwn", "text": "~A~"}]}]

        result = replace_options_blocks([other], spent)

        assert other in result

    def test_appends_when_no_control_is_present(self):
        spent = [{"type": "context", "elements": [{"type": "mrkdwn", "text": "~A~"}]}]

        result = replace_options_blocks([], spent)

        assert result == spent

    def test_input_blocks_are_not_mutated(self):
        control = {
            "type": "actions",
            "elements": [{"type": "checkboxes", "action_id": OPTIONS_CHECKBOXES_ACTION}],
        }
        blocks = [control]

        replace_options_blocks(blocks, [{"type": "context", "elements": []}])

        assert blocks == [control]


class TestLifecycleOnTheSlot:
    """remember / expire / forget against a real slot registry."""

    def _state(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.sessions.get_slack_link = MagicMock(return_value=(None, None))
        state.push_slots_update = MagicMock()
        state.slack_client = _slack()
        return state

    def _posted(self) -> PostedOptions:
        return PostedOptions(
            channel="C1",
            ts="opt_ts",
            choices=("A", "B"),
            blocks=(
                {
                    "type": "actions",
                    "elements": [
                        {"type": "checkboxes", "action_id": OPTIONS_CHECKBOXES_ACTION}
                    ],
                },
            ),
        )

    @pytest.mark.asyncio
    async def test_a_recorded_control_is_expired_on_the_next_turn(
        self, tmp_path, monkeypatch
    ):
        from kiro_crew.dashboard.chat_utils import (
            effective_session_key,
            expire_slack_options,
            remember_slack_options,
        )

        state = self._state(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        key = effective_session_key(slot)

        remember_slack_options(state, key, self._posted())
        assert slot._slack_options_posted is not None

        await expire_slack_options(state, key)

        state.slack_client.update_message.assert_awaited_once()
        assert slot._slack_options_posted is None

    @pytest.mark.asyncio
    async def test_expiry_runs_once_even_if_more_turns_follow(
        self, tmp_path, monkeypatch
    ):
        """The record is cleared before the edit, so a failure is not retried."""
        from kiro_crew.dashboard.chat_utils import (
            effective_session_key,
            expire_slack_options,
            remember_slack_options,
        )

        state = self._state(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        key = effective_session_key(slot)
        remember_slack_options(state, key, self._posted())

        await expire_slack_options(state, key)
        await expire_slack_options(state, key)

        assert state.slack_client.update_message.await_count == 1

    @pytest.mark.asyncio
    async def test_forget_stops_expiry_erasing_the_users_selection(
        self, tmp_path, monkeypatch
    ):
        """A Send click already re-rendered the message with the choice made.

        Striking every choice through afterwards would erase it, so the click
        drops the record instead.
        """
        from kiro_crew.dashboard.chat_utils import (
            effective_session_key,
            expire_slack_options,
            forget_slack_options,
            remember_slack_options,
        )

        state = self._state(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        key = effective_session_key(slot)
        remember_slack_options(state, key, self._posted())

        forget_slack_options(state, key)
        await expire_slack_options(state, key)

        state.slack_client.update_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_state_without_slots_cannot_break_the_turn(
        self, tmp_path, monkeypatch
    ):
        """Bookkeeping is cleanup, so it must never abort the turn it runs in.

        ``_run_chat`` takes whatever state object its caller passes; several
        callers pass a stand-in that has no slot registry at all.
        """
        from kiro_crew.dashboard.chat_utils import (
            expire_slack_options,
            forget_slack_options,
            remember_slack_options,
        )

        class _NoSlots:
            slack_client = None

        bare = _NoSlots()

        remember_slack_options(bare, "dashboard:s1", self._posted())
        await expire_slack_options(bare, "dashboard:s1")
        forget_slack_options(bare, "dashboard:s1")

    @pytest.mark.asyncio
    async def test_a_raising_slot_registry_cannot_break_the_turn(
        self, tmp_path, monkeypatch
    ):
        from kiro_crew.dashboard.chat_utils import expire_slack_options

        state = self._state(tmp_path, monkeypatch)
        state.get_slot = MagicMock(side_effect=RuntimeError("registry down"))

        await expire_slack_options(state, "dashboard:s1")

        state.slack_client.update_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_click_clears_the_record_a_mirroring_session_holds(
        self, tmp_path, monkeypatch
    ):
        """A thread can be owned by a dashboard session mirroring into it.

        The control is then recorded under the dashboard key, so clearing only
        the ``slack:<ts>`` key leaves it live — and the next dashboard turn
        strikes it through, erasing the selection the user just made.

        The link is established through the real endpoint, not by seeding the
        thread index by hand: an earlier version of this test set
        ``state._slack_to_slot`` itself and so passed while production never
        registered that mapping at all.
        """
        from kiro_crew.dashboard.chat_utils import (
            effective_session_key,
            expire_slack_options,
            remember_slack_options,
        )
        from kiro_crew.slack import interactions

        state = self._state(tmp_path, monkeypatch)
        state.slack_client.open_dm = AsyncMock(return_value="C1")
        state.slack_client.post_message = AsyncMock(return_value="1785370133.085469")
        state.owner_id = "U1"
        state.sessions.set_slack_link = MagicMock()
        slot = state.get_or_create_slot("s1")

        async with TestClient(TestServer(_slack_app(state))) as client:
            resp = await client.post("/api/chat/slots/s1/slack-link", json={})
            assert resp.status == 200
            thread_ts = (await resp.json())["thread_ts"]

        key = effective_session_key(slot)
        remember_slack_options(state, key, self._posted())

        orch = MagicMock()
        orch.dashboard_state = state
        monkeypatch.setattr(interactions, "_orch", orch)
        interactions._forget_options_control(thread_ts)

        await expire_slack_options(state, key)

        state.slack_client.update_message.assert_not_awaited()
        assert slot._slack_options_posted is None

    def test_linking_registers_the_thread_so_a_click_can_route_back(
        self, tmp_path, monkeypatch
    ):
        """The reverse index is what resolves a click back to this conversation.

        The link handler used to assign the slot's fields directly and skip the
        state helper that writes it, so a click on the replayed control could
        not find the slot and answered into a separate Slack session.
        """
        from kiro_crew.dashboard.chat_utils import slack_options_owner_key

        state = self._state(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        thread_ts = "1785370133.085469"

        state.link_slack("s1", thread_ts, "C1")

        assert state.get_linked_slot(thread_ts) is slot
        assert slack_options_owner_key(state, thread_ts) == "dashboard:s1"

    def test_the_owner_key_survives_a_missing_thread_index(
        self, tmp_path, monkeypatch
    ):
        """The index is written by a helper a caller can forget.

        Resolving through it alone is what made this silently return the wrong
        session, so the resolver also matches on the slot's own link fields.
        """
        from kiro_crew.dashboard.chat_utils import slack_options_owner_key

        state = self._state(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        thread_ts = "1785370133.085469"
        slot._slack_linked = True
        slot._slack_channel = "C1"
        slot._slack_thread_ts = thread_ts
        state._slack_to_slot.clear()

        assert slack_options_owner_key(state, thread_ts) == "dashboard:s1"

    def test_an_unowned_thread_resolves_to_its_own_slack_key(
        self, tmp_path, monkeypatch
    ):
        from kiro_crew.dashboard.chat_utils import slack_options_owner_key

        state = self._state(tmp_path, monkeypatch)

        assert (
            slack_options_owner_key(state, "1785370133.085469")
            == "slack:1785370133.085469"
        )

    @pytest.mark.asyncio
    async def test_missing_slot_and_missing_state_are_no_ops(
        self, tmp_path, monkeypatch
    ):
        from kiro_crew.dashboard.chat_utils import (
            expire_slack_options,
            forget_slack_options,
            remember_slack_options,
        )

        state = self._state(tmp_path, monkeypatch)

        # A Slack thread can be mid-turn before its slot exists.
        remember_slack_options(state, "slack:1.0", self._posted())
        await expire_slack_options(state, "slack:1.0")
        forget_slack_options(state, "slack:1.0")
        remember_slack_options(None, "slack:1.0", self._posted())
        await expire_slack_options(None, "slack:1.0")

        state.slack_client.update_message.assert_not_awaited()


class TestTurnEntryWiring:
    """The expiry has to actually fire on a new turn, from either surface.

    The lifecycle helpers above are exercised directly, which would stay green
    if the calls into them were deleted. These tests cover the call sites.
    """

    @pytest.mark.asyncio
    async def test_dashboard_turn_expires_before_doing_anything_else(
        self, tmp_path, monkeypatch
    ):
        """Covers dashboard sends, queue drains, regenerate, rewind, cron
        injection and the Slack-linked-thread route — every turn that runs
        through the dashboard engine."""
        from kiro_crew.dashboard import chat_runner

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.sessions.get_slack_link = MagicMock(return_value=(None, None))
        state.push_slots_update = MagicMock()
        slot = state.get_or_create_slot("s1")

        calls: list[str] = []

        async def _record(_state, session_key):
            calls.append(session_key)

        monkeypatch.setattr(chat_runner, "expire_slack_options", _record)
        # The turn itself is irrelevant — the expiry runs before any provider
        # work, so let the turn fail however it likes.
        try:
            await chat_runner._run_chat(state, slot, "hello")
        except Exception:
            pass

        assert calls == ["dashboard:s1"]

    @pytest.mark.asyncio
    async def test_dashboard_prompt_expansion_is_not_a_new_turn(
        self, tmp_path, monkeypatch
    ):
        """A /prompts reference re-enters the same turn; re-expiring there would
        spend a control the user has not been shown an answer to yet."""
        from kiro_crew.dashboard import chat_runner

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.sessions.get_slack_link = MagicMock(return_value=(None, None))
        state.push_slots_update = MagicMock()
        slot = state.get_or_create_slot("s1")

        calls: list[str] = []

        async def _record(_state, session_key):
            calls.append(session_key)

        monkeypatch.setattr(chat_runner, "expire_slack_options", _record)
        try:
            await chat_runner._run_chat(state, slot, "hello", _prompt_depth=1)
        except Exception:
            pass

        assert calls == []

    @pytest.mark.asyncio
    async def test_slack_inbound_turn_expires(self, monkeypatch):
        """Covers the live Slack path, which never reaches the dashboard engine."""
        from kiro_crew.slack import transport_dispatch

        calls: list[str] = []

        async def _record(_state, session_key):
            calls.append(session_key)

        async def _no_linked_thread(*_a, **_k):
            return False

        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_utils.expire_slack_options", _record
        )
        monkeypatch.setattr(
            transport_dispatch, "maybe_route_linked_thread", _no_linked_thread
        )
        monkeypatch.setattr(transport_dispatch, "_hydrate_thread_overrides", MagicMock())
        monkeypatch.setattr(transport_dispatch, "_hydrate_conv_flags", MagicMock())

        slack = _slack()
        sessions = MagicMock()
        # Slack-born: the thread index has no owner for it, so the turn runs
        # under the syntactic slack:<ts> key. Must be set explicitly — a bare
        # MagicMock returns a truthy stub and the dispatcher would reroute to it.
        sessions.get_session_for_thread.return_value = None
        # "ping" short-circuits immediately AFTER the expiry, so the turn never
        # needs a provider.
        await transport_dispatch.handle_message_transport(
            slack,
            sessions,
            "C1",
            "ping",
            "1785370133.085469",
            "1785370133.085469",
            "U1",
        )

        assert calls == ["slack:1785370133.085469"]
        slack.post_message.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_slack_inbound_expires_the_threads_owning_session(self, monkeypatch):
        """A Slack reply in a dashboard-owned thread must spend THAT session's control.

        The dispatcher resolves the thread to its owning session before the turn
        begins, so a thread the dashboard created answers under its own key. The
        expiry has to run after that resolution — expiring ``slack:<ts>`` would
        leave the dashboard session's control live and strike through nothing.
        """
        from kiro_crew.slack import transport_dispatch

        calls: list[str] = []

        async def _record(_state, session_key):
            calls.append(session_key)

        async def _no_linked_thread(*_a, **_k):
            return False

        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_utils.expire_slack_options", _record
        )
        monkeypatch.setattr(
            transport_dispatch, "maybe_route_linked_thread", _no_linked_thread
        )
        monkeypatch.setattr(transport_dispatch, "_hydrate_thread_overrides", MagicMock())
        monkeypatch.setattr(transport_dispatch, "_hydrate_conv_flags", MagicMock())

        slack = _slack()
        sessions = MagicMock()
        sessions.get_session_for_thread.return_value = "dashboard:chat-7-1785370000"
        await transport_dispatch.handle_message_transport(
            slack,
            sessions,
            "C1",
            "ping",
            "1785370133.085469",
            "1785370133.085469",
            "U1",
        )

        assert calls == ["dashboard:chat-7-1785370000"]


def _slack_app(state):
    from kiro_crew.dashboard.chat_slack import api_chat_slot_slack_link

    app = web.Application()
    app["state"] = state
    app.router.add_post("/api/chat/slots/{slot}/slack-link", api_chat_slot_slack_link)
    return app


class TestLinkTimeBackfill:
    """Replaying context into a freshly-linked thread."""

    def _state(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.slack_client = MagicMock()
        state.slack_client.open_dm = AsyncMock(return_value="C1")
        state.slack_client.post_message = AsyncMock(return_value="ts1")
        state.slack_client.post_blocks = AsyncMock(return_value="opt_ts")
        state.owner_id = "U1"
        state.sessions.get_slack_link = MagicMock(return_value=(None, None))
        state.sessions.set_slack_link = MagicMock()
        state.push_slots_update = MagicMock()
        return state

    @pytest.mark.asyncio
    async def test_replayed_options_are_a_control_not_literal_text(
        self, tmp_path, monkeypatch
    ):
        """The backfill posted bodies verbatim, so the tag arrived as text.

        This is the path that carries the reply when the Slack link is created
        after the turn already finished — the mirror has nothing to send by
        then, so the backfill is the only thing that puts the answer in Slack.
        """
        state = self._state(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        slot.append("user", "which one?")
        slot.append("assistant", "Your call.\n\n[OPTIONS: Ship it | Hold off]")
        slot.drain()

        async with TestClient(TestServer(_slack_app(state))) as client:
            resp = await client.post("/api/chat/slots/s1/slack-link", json={})
            assert resp.status == 200

        texts = [c.args[1] for c in state.slack_client.post_message.await_args_list]
        assert all("[OPTIONS:" not in t for t in texts)
        blocks = state.slack_client.post_blocks.await_args.args[1]
        assert _is_live_control(blocks)

    @pytest.mark.asyncio
    async def test_the_newest_reply_stays_answerable_and_is_recorded(
        self, tmp_path, monkeypatch
    ):
        state = self._state(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        slot.append("assistant", "Latest.\n\n[OPTIONS: A | B]")
        slot.drain()

        async with TestClient(TestServer(_slack_app(state))) as client:
            await client.post("/api/chat/slots/s1/slack-link", json={})

        assert _is_live_control(state.slack_client.post_blocks.await_args.args[1])
        assert slot._slack_options_posted is not None
        assert slot._slack_options_posted.choices == ("A", "B")

    @pytest.mark.asyncio
    async def test_a_superseded_question_is_replayed_spent(
        self, tmp_path, monkeypatch
    ):
        """The user already answered it — replaying it live would re-ask it."""
        state = self._state(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        slot.append("assistant", "Older.\n\n[OPTIONS: A | B]")
        slot.append("user", "A")
        slot.drain()

        async with TestClient(TestServer(_slack_app(state))) as client:
            await client.post("/api/chat/slots/s1/slack-link", json={})

        blocks = state.slack_client.post_blocks.await_args.args[1]
        assert not _is_live_control(blocks)
        assert _context_text(blocks) == "~A~  |  ~B~"
        assert slot._slack_options_posted is None

    @pytest.mark.asyncio
    async def test_a_trailing_system_row_does_not_spend_the_newest_reply(
        self, tmp_path, monkeypatch
    ):
        """The transcript holds rows that are never replayed.

        A completed turn appends one, so the last reply is not the last row.
        Judging "newest" by raw position spent the very control the replay
        exists to deliver — the user saw a struck-through question again.
        """
        state = self._state(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        slot.append("assistant", "Your call.\n\n[OPTIONS: Ship it | Hold off]")
        slot.append("done", "turn complete")
        slot.drain()

        async with TestClient(TestServer(_slack_app(state))) as client:
            await client.post("/api/chat/slots/s1/slack-link", json={})

        blocks = state.slack_client.post_blocks.await_args.args[1]
        assert _is_live_control(blocks)
        assert slot._slack_options_posted is not None

    @pytest.mark.asyncio
    async def test_a_users_own_options_syntax_survives_the_replay(
        self, tmp_path, monkeypatch
    ):
        """A person can type the OPTIONS syntax — quoting it, or discussing it.

        Routing user rows through the agent-authored path lifted the tag out of
        their words and rendered it as struck-through choices they never offered.
        Their text has to come back verbatim.
        """
        state = self._state(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        # The tag must END the message: the parser is line-anchored, so trailing
        # words after it would make this pass whatever the code did.
        slot.append("user", "should I paste this?\n\n[OPTIONS: A | B]")
        slot.drain()

        async with TestClient(TestServer(_slack_app(state))) as client:
            await client.post("/api/chat/slots/s1/slack-link", json={})

        texts = [c.args[1] for c in state.slack_client.post_message.await_args_list]
        assert any("[OPTIONS: A | B]" in t for t in texts)
        state.slack_client.post_blocks.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_only_the_newest_reply_is_answerable(self, tmp_path, monkeypatch):
        state = self._state(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        slot.append("assistant", "First.\n\n[OPTIONS: A | B]")
        slot.append("user", "A")
        slot.append("assistant", "Second.\n\n[OPTIONS: C | D]")
        slot.drain()

        async with TestClient(TestServer(_slack_app(state))) as client:
            await client.post("/api/chat/slots/s1/slack-link", json={})

        posts = state.slack_client.post_blocks.await_args_list
        assert len(posts) == 2
        assert not _is_live_control(posts[0].args[1])
        assert _is_live_control(posts[1].args[1])
