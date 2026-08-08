"""Shared vision-model helpers for image prompts on text-only router models.

Three consumers share one implementation so they cannot drift:

* :func:`decide_image_input_mode` — the per-turn routing decision: attach the
  image natively (``native``) when the active model is vision-capable, or run
  the describe pipeline (``text``) when it is not. Mirrors Hermes's
  ``agent.image_input_mode`` (``auto`` | ``native`` | ``text``).
* :meth:`kiro_crew.acp.client.AcpClient._describe_images_with_vision` — the
  legacy user-image path: a text-only model gets a message carrying an image
  path, so the path is replaced by a one-shot vision subagent's description.
* :func:`mcp_core._call_tool_inner` ``vision_analyze`` — the tool surface: the
  AGENT itself asks to describe a screenshot / chart / URL-referenced image,
  so the tool spawns the same one-shot subagent and returns the text.

Both describe paths spawn an :class:`~kiro_crew.acp.client.AcpClient` on the
configured vision-capable fallback model (default ``cmc/mimo-v2.5``) against
the same router proxy, then tear the subagent process down. The main session's
model is untouched, so a text-only main model never sees an image block.

The import of ``acp.client`` is deferred into the function (not module-level)
to keep this module importable from ``mcp_core`` and ``config`` without
re-entering the ``config.loader -> providers.acp -> acp.client`` cycle.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

#: Valid values for ``agent.image_input_mode`` (mirrors Hermes). ``auto`` is the
#: default and is decided per turn by :func:`decide_image_input_mode`.
IMAGE_INPUT_MODES = frozenset({"auto", "native", "text"})

#: Prompt sent to the vision subagent. 1-3 short sentences keeps the injected
#: description token-cheap on the text-only main model and matches the marker
#: the legacy redirect injects (``[image: <name>: <description>]``).
_VISION_DESCRIBE_PROMPT = "Describe this image in 1-3 short sentences: {ref}"

#: Per-image describe timeout. A vision subagent is a full ACP session spawn
#: (initialize + session/new + prompt), so a long image can legitimately take a
#: while; 120s is the same bound the legacy ``_vision_subagent_describe`` used.
VISION_DESCRIBE_TIMEOUT = 120.0


def _coerce_image_input_mode(raw: Any) -> str:
    """Normalize a config value into one of the valid modes (default ``auto``)."""
    if isinstance(raw, str):
        val = raw.strip().lower()
        if val in IMAGE_INPUT_MODES:
            return val
    return "auto"


def decide_image_input_mode(
    model_id: str,
    *,
    image_input_mode: str = "auto",
    text_only_models: Iterable[str] | None = None,
    registry_supports_vision: Any = None,
) -> str:
    """Decide how *model_id*'s turn should carry images: ``"native"`` or ``"text"``.

    Resolution order, first hit wins:

    1. ``image_input_mode`` config override — ``native`` or ``text`` pins the
       mode unconditionally; ``auto`` falls through.
    2. ``registry_supports_vision`` — when supplied (the caller's pre-resolved
       registry/metadata lookup), a definite True/False decides directly. This
       lets tests and callers inject a catalog capability (e.g. a router
       ``/v1/models`` entry that reports ``capabilities``) without reaching
       into the registry.
    3. ``model_registry.model_supports_vision(model_id)`` — True -> native
       (Anthropic Claude models), False -> text.
    4. ``text_only_models`` membership — the router per-model denylist
       (``oc/deepseek-v4-flash``, ``ol/deepseek-v4-flash:0731``). A match
       -> text.
    5. Default -> ``"native"``. Unknown models are treated as vision-capable
       (fail open on capability, fail closed on rejecting the image): a model
       that genuinely rejects images surfaces a 400 the operator can add to
       ``text_only_models``; a model we wrongly text-route would silently
       degrade every image to a lossy summary forever.
    """
    mode = _coerce_image_input_mode(image_input_mode)
    if mode != "auto":
        return mode

    if registry_supports_vision is not None:
        return "native" if registry_supports_vision else "text"

    try:
        from kiro_crew.model_registry import model_supports_vision

        supports = model_supports_vision(model_id)
        if supports is not None:
            return "native" if supports else "text"
    except Exception:  # pragma: no cover - registry load is defensive
        logger.debug("vision: model_supports_vision lookup failed for %s", model_id, exc_info=True)

    if text_only_models and model_id in set(text_only_models):
        return "text"
    return "native"


async def vision_subagent_describe(
    image_ref: str,
    *,
    vision_model: str,
    work_dir: str | Path | None = None,
    extra_env: dict[str, str] | None = None,
    acp_backend: str = "",
    sandbox_mode: str = "auto",
    timeout: float = VISION_DESCRIBE_TIMEOUT,
) -> str:
    """Describe *image_ref* (a local path or http(s) URL) on *vision_model*.

    Spawns a one-shot :class:`~kiro_crew.acp.client.AcpClient` on the given
    vision-capable model against the same proxy env as the caller, sends it the
    image with a describe prompt, collects the text reply, and shuts the
    subagent down. The caller's own ``ANTHROPIC_MODEL`` (which belongs to the
    text-only main model) is dropped so the subagent derives its own model via
    ``_model_via_env`` -> ``ANTHROPIC_MODEL=<stripped vision id>`` while keeping
    the base URL + API key.

    Returns the trimmed description text. Raises on any failure — callers
    decide the fallback (legacy redirect falls back to a session switch; the
    tool returns an ``Error:`` string).
    """
    # Deferred import: acp.client pulls in providers/agent/config which would
    # cycle when mcp_core (already deep in config) imports this module.
    from kiro_crew.acp.client import AcpClient

    sub_env = dict(extra_env or {})
    sub_env.pop("ANTHROPIC_MODEL", None)
    sub = AcpClient(
        work_dir=work_dir,  # None -> AcpClient's default (config_dir()/workspace)
        # Picker spelling — the client strips the prefix to the raw id for the
        # wire and validates config options against the picker form.
        model=vision_model,
        sandbox_mode=sandbox_mode,
        extra_env=sub_env,
        acp_backend=acp_backend,
        audit_source="vision-subagent",
    )
    try:
        chunks: list[str] = []
        async for chunk in sub.send_message_stream(
            _VISION_DESCRIBE_PROMPT.format(ref=image_ref),
            timeout=timeout,
        ):
            chunks.append(chunk)
        return "".join(chunks).strip()
    finally:
        try:
            await sub.shutdown()
        except Exception:
            logger.debug("vision subagent shutdown failed", exc_info=True)


async def describe_image_via_vision(
    image_ref: str,
    *,
    vision_model: str,
    work_dir: str | Path | None = None,
    extra_env: dict[str, str] | None = None,
    acp_backend: str = "",
    sandbox_mode: str = "auto",
) -> str:
    """Best-effort wrapper: describe *image_ref* or return ``"unavailable"``.

    ``"unavailable"`` matches the marker the legacy ``_describe_images_with_vision``
    injects when the subagent returns an empty description, so callers that
    substitute ``[image: <name>: <desc>]`` read one vocabulary.
    """
    try:
        return await vision_subagent_describe(
            image_ref,
            vision_model=vision_model,
            work_dir=work_dir,
            extra_env=extra_env,
            acp_backend=acp_backend,
            sandbox_mode=sandbox_mode,
        )
    except Exception:
        logger.warning("vision describe failed for %s", image_ref, exc_info=True)
        return "unavailable"


def _extract_env_from_client(client: Any) -> dict[str, str]:
    """Return the environment an :class:`AcpClient`-like object carries.

    Kept as a small helper so callers that already hold a configured
    ``AcpClient`` (the legacy path) can forward its proxy env without reaching
    into private fields at every call site.
    """
    return dict(getattr(client, "_extra_env", None) or {})
