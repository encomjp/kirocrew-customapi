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
from dataclasses import dataclass
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

#: Default vision-capable router fallback. cmc/mimo-v2.5 (commandcode) accepts
#: images (verified 200) and is fast. Mirrors client._DEFAULT_VISION_FALLBACK_MODEL.
DEFAULT_VISION_FALLBACK_MODEL = "cmc/mimo-v2.5"


@dataclass(frozen=True)
class VisionProvider:
    """One vision-capable backend in the fallback chain.

    ``model`` is the picker spelling the AcpClient accepts (the client strips a
    known router prefix to the raw wire id). ``extra_env`` carries the endpoint
    wiring: for a ``router`` entry it is the main session's router env; for a
    ``custom`` entry it is ``ANTHROPIC_BASE_URL`` / ``ANTHROPIC_API_KEY`` for
    the explicit endpoint.
    """

    provider: str
    model: str
    extra_env: dict[str, str]
    acp_backend: str = ""


def _coerce_vision_provider_entry(
    entry: Any,
    *,
    main_env: dict[str, str],
    main_backend: str,
) -> VisionProvider | None:
    """Normalize one ``agent.vision_providers`` dict entry into a provider.

    Returns None when the entry is unusable (missing model, or a non-router
    entry with no base_url) so the chain builder can skip it rather than fail.
    """
    if not isinstance(entry, dict):
        return None
    model = str(entry.get("model") or "").strip()
    if not model:
        return None
    provider = str(entry.get("provider") or "").strip().lower()
    base_url = str(entry.get("base_url") or "").strip()
    api_key = str(entry.get("api_key") or "").strip()

    if provider == "router" or (not base_url and provider in ("", "auto")):
        # Reuse the main session's router endpoint; the model is a picker id.
        return VisionProvider(
            provider=provider or "router",
            model=model,
            extra_env=dict(main_env),
            acp_backend=main_backend,
        )
    if base_url:
        env: dict[str, str] = {}
        if base_url:
            env["ANTHROPIC_BASE_URL"] = base_url
        if api_key:
            env["ANTHROPIC_API_KEY"] = api_key
        return VisionProvider(
            provider=provider or "custom",
            model=model,
            extra_env=env,
            # A custom endpoint speaks the OpenAI-compatible HTTP contract; the
            # ``opencode`` marker routes it through the direct-HTTP describe
            # path (opencode's own ACP adapter drops image parts).
            acp_backend="opencode",
        )
    return None


def resolve_vision_providers(
    *,
    vision_providers: Iterable[dict[str, Any]] | None = None,
    vision_fallback_model: str = "",
    main_env: dict[str, str] | None = None,
    main_backend: str = "",
) -> list[VisionProvider]:
    """Build the ordered vision fallback chain from config.

    Order: configured ``agent.vision_providers`` entries first (in list order),
    then the legacy ``agent.vision_fallback_model`` as the final entry (kept so
    a single-model setup — the pre-chain behavior — works unchanged). When no
    custom entry pins its own endpoint, every entry rides the main session's
    router env, so a bare ``[{"model": "cmc/mimo-v2.5"}]`` behaves exactly like
    the old ``vision_fallback_model``.

    Duplicate models are kept (each entry is a distinct attempt), but an entry
    with no model or no usable endpoint is skipped. Returns an empty list when
    nothing is configured.
    """
    out: list[VisionProvider] = []
    seen: set[tuple[str, str]] = set()
    main_env = dict(main_env or {})

    for entry in vision_providers or []:
        prov = _coerce_vision_provider_entry(entry, main_env=main_env, main_backend=main_backend)
        if prov is None:
            continue
        key = (prov.model, prov.extra_env.get("ANTHROPIC_BASE_URL", ""))
        if key in seen:
            continue
        seen.add(key)
        out.append(prov)

    fallback = (vision_fallback_model or DEFAULT_VISION_FALLBACK_MODEL).strip()
    if fallback:
        key = (fallback, main_env.get("ANTHROPIC_BASE_URL", ""))
        if key not in seen:
            out.append(
                VisionProvider(
                    provider="router",
                    model=fallback,
                    extra_env=dict(main_env),
                    acp_backend=main_backend,
                )
            )
    return out


async def describe_image_via_chain(
    image_ref: str,
    providers: Iterable[VisionProvider],
    *,
    work_dir: str | Path | None = None,
    sandbox_mode: str = "auto",
    timeout: float = VISION_DESCRIBE_TIMEOUT,
) -> str:
    """Describe *image_ref* trying each provider in *providers* until one succeeds.

    Returns the first non-empty description. Returns ``"unavailable"`` when every
    provider fails or returns nothing, so callers (the ``[image: …]`` marker and
    the ``vision_analyze`` tool) read one vocabulary.
    """
    for prov in providers:
        description = await describe_image_via_vision(
            image_ref,
            vision_model=prov.model,
            work_dir=work_dir,
            extra_env=prov.extra_env,
            acp_backend=prov.acp_backend,
            sandbox_mode=sandbox_mode,
            timeout=timeout,
        )
        if description and description != "unavailable":
            return description
    return "unavailable"


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

    Two transports, chosen by the provider:

    * **Direct HTTP** — when ``extra_env`` carries ``ANTHROPIC_BASE_URL`` and the
      backend is a custom/OpenAI-compatible endpoint (``opencode`` backend or a
      ``custom`` ``vision_providers`` entry). POSTs an OpenAI ``image_url``
      content part to ``{base_url}/chat/completions``. This is the ONLY reliable
      path for these providers: opencode's ACP adapter drops ``image`` parts
      before they reach the model (it rewrites them to a text error), so an ACP
      subagent on opencode cannot see the image at all.
    * **ACP subagent** — otherwise (kiro-cli ``acp`` backend, or a ``claude``
      backend whose adapter inlines image paths itself). Spawns a one-shot
      :class:`~kiro_crew.acp.client.AcpClient` on the vision model against the
      same proxy env as the caller and shuts it down after the turn.

    The caller's own ``ANTHROPIC_MODEL`` (which belongs to the text-only main
    model) is dropped on the ACP path so the subagent derives its own model.

    Returns the trimmed description text. Raises on any failure — callers
    decide the fallback (legacy redirect falls back to a session switch; the
    tool returns an ``Error:`` string).
    """
    env = dict(extra_env or {})
    base_url = (env.get("ANTHROPIC_BASE_URL") or "").strip()
    if base_url and acp_backend == "opencode":
        return await _http_describe_image(
            image_ref, vision_model=vision_model, env=env, timeout=timeout
        )

    # Deferred import: acp.client pulls in providers/agent/config which would
    # cycle when mcp_core (already deep in config) imports this module.
    from kiro_crew.acp.client import AcpClient

    sub_env = dict(env)
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


async def _http_describe_image(
    image_ref: str,
    *,
    vision_model: str,
    env: dict[str, str],
    timeout: float = VISION_DESCRIBE_TIMEOUT,
) -> str:
    """Describe *image_ref* by POSTing an OpenAI ``image_url`` part directly.

    Targets ``{ANTHROPIC_BASE_URL}/chat/completions`` (the OpenAI-compatible
    shape every such endpoint speaks). A local path is read and inlined as a
    ``data:`` URL; an http(s) URL is passed through. Uses stdlib
    ``urllib.request`` in a thread so the caller's event loop is never blocked,
    and offloads the read through ``hooks.safe_read_file_bytes`` so the
    sensitive-path gate still applies.

    Returns the trimmed description text, or raises on any failure (network,
    non-2xx, empty reply).
    """
    import asyncio
    import base64
    import json
    import urllib.error
    import urllib.request

    base_url = (env.get("ANTHROPIC_BASE_URL") or "").strip().rstrip("/")
    api_key = (env.get("ANTHROPIC_API_KEY") or "").strip()
    if not base_url:
        raise RuntimeError("vision http describe: no ANTHROPIC_BASE_URL")

    content: list[dict[str, Any]] = [
        {"type": "text", "text": _VISION_DESCRIBE_PROMPT.format(ref=image_ref)}
    ]
    if image_ref.startswith("http://") or image_ref.startswith("https://"):
        content.append({"type": "image_url", "image_url": {"url": image_ref}})
    else:
        from kiro_crew.hooks import safe_read_file_bytes

        raw = await asyncio.to_thread(safe_read_file_bytes, image_ref)
        if not raw:
            raise RuntimeError(f"vision http describe: cannot read image {image_ref}")
        b64 = base64.b64encode(raw).decode("ascii")
        content.append(
            {
                "type": "image_url",
                "image_url": {"url": f"data:image/png;base64,{b64}"},
            }
        )

    payload = json.dumps(
        {
            "model": vision_model,
            "messages": [{"role": "user", "content": content}],
            "max_tokens": 1024,
        }
    ).encode()
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    def _post() -> bytes:
        req = urllib.request.Request(
            f"{base_url}/chat/completions",
            data=payload,
            headers=headers,
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()

    body = await asyncio.to_thread(_post)
    try:
        parsed = json.loads(body)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"vision http describe: non-JSON reply: {body[:200]!r}") from exc
    try:
        text = parsed["choices"][0]["message"].get("content") or ""
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError(f"vision http describe: unexpected reply shape: {body[:200]!r}") from exc
    return text.strip()


async def describe_image_via_vision(
    image_ref: str,
    *,
    vision_model: str,
    work_dir: str | Path | None = None,
    extra_env: dict[str, str] | None = None,
    acp_backend: str = "",
    sandbox_mode: str = "auto",
    timeout: float = VISION_DESCRIBE_TIMEOUT,
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
            timeout=timeout,
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
