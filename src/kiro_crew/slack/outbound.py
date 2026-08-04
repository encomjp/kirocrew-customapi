"""One rendering seam for assistant text posted into a Slack thread.

Several paths carry assistant text to Slack — the dashboard mirror, the
link-time backfill, cron and subagent delivery, the MCP ``send_message`` tool.
They all owe the text the same treatment: redaction, splitting a trailing
``[OPTIONS: a | b]`` tag off the body so it renders as a control instead of
literal text, then mrkdwn conversion. Doing that in one place is what keeps a
path from quietly missing a step.

Ordering is load-bearing, and ``render_for_slack`` documents why each step sits
where it does. In short: redaction runs over the whole text at full length,
before anything converts or truncates it, so a credential inside a choice label
is redacted before it can become a button value and no truncation can leave half
a secret behind as a fragment the credential regex no longer matches.

A posted control stays answerable until something spends it. ``PostedOptions``
carries enough to find it again, and ``expire_options`` renders it spent once
the conversation has moved past the question it asked.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from kiro_crew.security import redact_credentials, redact_exfiltration_urls
from kiro_crew.slack.format import (
    SLACK_MSG_LIMIT,
    build_options_blocks,
    build_options_selected_blocks,
    extract_options,
    replace_options_blocks,
    split_message,
    to_slack_mrkdwn,
)

if TYPE_CHECKING:
    from kiro_crew.slack.client import SlackClientOps

logger = logging.getLogger(__name__)

#: Notification/fallback text for the message carrying an OPTIONS control.
OPTIONS_FALLBACK_TEXT = "Options"


@dataclass(frozen=True)
class PostedOptions:
    """A posted OPTIONS control, addressed well enough to expire it later.

    ``blocks`` is the block list exactly as posted. Keeping it means expiry can
    run the same block surgery the Send button uses, editing only the OPTIONS
    block and leaving any surrounding blocks (a timing footer, a
    Link-to-Dashboard button) intact — without re-fetching the message.
    """

    channel: str
    ts: str
    choices: tuple[str, ...]
    blocks: tuple[dict, ...]
    text: str = OPTIONS_FALLBACK_TEXT


def _redacted(text: str) -> str:
    """Both redaction passes, in the order the security helpers expect."""
    body, _ = redact_exfiltration_urls(text)
    body, _ = redact_credentials(body)
    return body


def render_for_slack(text: str) -> tuple[str, list[str]]:
    """Return AGENT-AUTHORED *text* as redacted mrkdwn, with OPTIONS split off.

    Only call this on text the agent wrote. A person's own message can
    legitimately contain the OPTIONS syntax — quoting it, or discussing it — and
    parsing that would lift the tag out of their words and render choices they
    never offered. Use :func:`render_plain_for_slack` for anything else.

    The order of the three steps is load-bearing:

    1. **Redact the raw text, at full length.** ``to_slack_mrkdwn`` truncates at
       ``SLACK_MAX_TEXT``, so redacting after it would let a credential
       straddling that cut survive as a prefix the credential regex no longer
       matches.
    2. **Split the OPTIONS tag off next.** The choices are then already redacted
       before they can become button values, and the tag is out of reach of the
       truncation above. It also keeps the tag away from the table flattening
       below — not a live bug today, but the tag is a line of ``|``-separated
       values, which is the shape that handling looks for.
    3. **Convert what remains to mrkdwn, then redact again.** Conversion rewrites
       ``[label](url)`` to ``<url|label>``, so a URL that did not match in
       markdown form can match in mrkdwn form. Redaction is idempotent.
    """
    body = _redacted(text)
    body, choices = extract_options(body)
    return _redacted(to_slack_mrkdwn(body)), choices


def render_plain_for_slack(text: str) -> str:
    """Return *text* as redacted mrkdwn, leaving any OPTIONS syntax untouched.

    For text the agent did not author, where a trailing tag is just characters
    somebody typed and has to survive the round trip verbatim.
    """
    return _redacted(to_slack_mrkdwn(_redacted(text)))


async def post_assistant_text(
    slack: SlackClientOps,
    channel: str,
    text: str,
    thread_ts: str | None = None,
    *,
    interactive: bool = True,
    limit: int = SLACK_MSG_LIMIT,
    truncate_to: int | None = None,
) -> PostedOptions | None:
    """Post assistant *text* into a Slack thread, rendering any OPTIONS tag.

    The body posts as one or more plain messages. A trailing OPTIONS tag posts
    as a further message: an interactive checkbox control when *interactive*,
    otherwise a spent rendering with every choice struck through — which is what
    replayed history wants, so a reader cannot answer a question the
    conversation has already moved past.

    *truncate_to* caps the body length (used when replaying history, where the
    point is context rather than the full text). The cap applies after
    redaction, never before.

    Returns the control when one was posted and is still answerable, so the
    caller can remember it for later expiry; returns None when there was no
    OPTIONS tag or the control was posted already spent.
    """
    body, choices = render_for_slack(text)
    if truncate_to is not None:
        body = body[:truncate_to].rstrip()
    if body:
        for part in split_message(body, limit=limit):
            await slack.post_message(channel, part, thread_ts)
    if not choices:
        return None
    blocks = (
        build_options_blocks(choices)
        if interactive
        else build_options_selected_blocks(choices, [])
    )
    ts = await slack.post_blocks(channel, blocks, OPTIONS_FALLBACK_TEXT, thread_ts)
    if not interactive or not ts:
        return None
    return PostedOptions(
        channel=channel,
        ts=ts,
        choices=tuple(choices),
        blocks=tuple(blocks),
    )


async def post_plain_text(
    slack: SlackClientOps,
    channel: str,
    text: str,
    thread_ts: str | None = None,
    *,
    limit: int = SLACK_MSG_LIMIT,
    truncate_to: int | None = None,
) -> None:
    """Post text the agent did not author, with no OPTIONS parsing.

    Same redaction and mrkdwn treatment as :func:`post_assistant_text`, but a
    trailing OPTIONS tag stays in the body as literal characters — because for a
    person's own words that is what it is.
    """
    body = render_plain_for_slack(text)
    if truncate_to is not None:
        body = body[:truncate_to].rstrip()
    if not body:
        return
    for part in split_message(body, limit=limit):
        await slack.post_message(channel, part, thread_ts)


async def expire_options(slack: SlackClientOps, posted: PostedOptions) -> None:
    """Render a previously-posted OPTIONS control as spent. Best-effort.

    Strikes every choice through, so a control the conversation has moved past
    reads as unanswerable rather than inviting a click that would answer a
    superseded question. Only the OPTIONS block is replaced; surrounding blocks
    survive.

    Every failure is swallowed: a thread that keeps a stale control is the
    status quo, not a reason to disrupt the turn that triggered the cleanup.
    """
    try:
        spent = build_options_selected_blocks(list(posted.choices), [])
        blocks = replace_options_blocks(list(posted.blocks), spent)
        await slack.update_message(
            posted.channel, posted.ts, text=posted.text, blocks=blocks
        )
    except Exception:
        logger.debug("Failed to expire Slack OPTIONS control", exc_info=True)
