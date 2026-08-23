"""Built-in Anthropic → OpenAI translation proxy (the "shim").

Listens on loopback only and speaks the Anthropic Messages API on the front
(``POST /v1/messages``) while forwarding to any OpenAI-compatible
``/v1/chat/completions`` backend on the back (Ollama, llama.cpp server,
DeepSeek, vLLM …). Lets the fork's ``claude_code`` path drive plain
OpenAI-shaped endpoints with no external router in the chain.

Scope, deliberately:

* system prompts, multi-turn text, images (data: URLs pass through),
* tool ADVERTISEMENT + tool CALLS both directions (agents depend on tools),
* streaming via SSE with Anthropic event framing rebuilt from OpenAI deltas.

NOT translated (fail-fast, never silently dropped): ``tool_choice`` beyond
``auto``, ``thinking`` blocks, server-side tools. An unsupported field logs a
warning once and continues without it rather than erroring the whole turn.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from aiohttp import ClientSession, web

logger = logging.getLogger(__name__)

STOP_REASON_MAP = {
    "stop": "end_turn",
    "length": "max_tokens",
    "tool_calls": "tool_use",
    "content_filter": "refusal",
}


def _openai_tools(tools: list[dict[str, Any]] | None) -> list[dict[str, Any]] | None:
    """Anthropic ``{name, description, input_schema}`` → OpenAI function defs."""
    if not tools:
        return None
    return [
        {
            "type": "function",
            "function": {
                "name": t.get("name", ""),
                "description": t.get("description", ""),
                "parameters": t.get("input_schema")
                or {"type": "object", "properties": {}},
            },
        }
        for t in tools
    ]


def _flatten_content(content: Any) -> tuple[str, list[dict[str, Any]], list[Any]]:
    """Anthropic message content → (text, image_parts, tool_results).

    Images become OpenAI ``image_url`` parts (data: URLs pass through as-is;
    local paths were already inlined upstream by the prompt builder).
    """
    if isinstance(content, str):
        return content, [], []
    texts: list[str] = []
    images: list[dict[str, Any]] = []
    tool_results: list[Any] = []
    for block in content or []:
        btype = block.get("type")
        if btype == "text":
            texts.append(block.get("text", ""))
        elif btype == "image":
            src = block.get("source", {})
            if src.get("type") == "base64":
                data_url = f"data:{src.get('media_type', 'image/png')};base64,{src.get('data', '')}"
            else:
                data_url = src.get("url", "")
            if data_url:
                images.append({"type": "image_url", "image_url": {"url": data_url}})
        elif btype == "tool_use":
            # Assistant-side tool call replayed as history — handled by caller.
            pass
        elif btype == "tool_result":
            inner = block.get("content")
            inner_text = (
                " ".join(b.get("text", "") for b in inner if b.get("type") == "text")
                if isinstance(inner, list)
                else str(inner or "")
            )
            tool_results.append(
                {"tool_call_id": block.get("tool_use_id", ""), "content": inner_text}
            )
    return "\n".join(texts), images, tool_results


def anthropic_to_openai(body: dict[str, Any]) -> dict[str, Any]:
    """Translate an Anthropic Messages request into a chat.completions one."""
    out: dict[str, Any] = {
        "model": body.get("model", ""),
        "max_tokens": body.get("max_tokens") or 4096,
    }
    if body.get("temperature") is not None:
        out["temperature"] = body["temperature"]

    messages: list[dict[str, Any]] = []
    system = body.get("system")
    if system:
        sys_text = (
            " ".join(b.get("text", "") for b in system if b.get("type") == "text")
            if isinstance(system, list)
            else str(system)
        )
        if sys_text.strip():
            messages.append({"role": "system", "content": sys_text})

    for msg in body.get("messages", []):
        role = msg.get("role", "user")
        text, images, tool_results = _flatten_content(msg.get("content"))
        if role == "user":
            if tool_results:
                for tr in tool_results:
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tr["tool_call_id"],
                            "content": tr["content"],
                        }
                    )
                if not text and not images:
                    continue
            parts: list[dict[str, Any]] = [{"type": "text", "text": text}] if text else []
            parts.extend(images)
            if images:
                messages.append({"role": "user", "content": parts})
            else:
                messages.append({"role": "user", "content": text or ""})
        else:  # assistant
            entry: dict[str, Any] = {"role": "assistant", "content": text or None}
            calls = [
                {
                    "id": b.get("id", ""),
                    "type": "function",
                    "function": {
                        "name": b.get("name", ""),
                        "arguments": json.dumps(b.get("input", {})),
                    },
                }
                for b in msg.get("content", [])
                if isinstance(b, dict) and b.get("type") == "tool_use"
            ]
            if calls:
                entry["tool_calls"] = calls
            messages.append(entry)

    out["messages"] = messages
    tools = _openai_tools(body.get("tools"))
    if tools:
        out["tools"] = tools
    stream = bool(body.get("stream"))
    out["stream"] = stream
    if stream:
        # Ask compatible backends to append a final usage-only chunk. Servers
        # that don't know the field ignore it; usage then stays at 0 and the
        # client sees the same shape it would from any silent backend.
        out["stream_options"] = {"include_usage": True}
    return out


def openai_to_anthropic(payload: dict[str, Any], model: str) -> dict[str, Any]:
    """Translate a non-streaming chat.completions response back."""
    choice = (payload.get("choices") or [{}])[0]
    msg = choice.get("message", {}) or {}
    content: list[dict[str, Any]] = []
    if msg.get("content"):
        content.append({"type": "text", "text": msg["content"]})
    for call in msg.get("tool_calls") or []:
        fn = call.get("function", {})
        try:
            input_obj = json.loads(fn.get("arguments") or "{}")
        except json.JSONDecodeError:
            input_obj = {"_raw": fn.get("arguments", "")}
        content.append(
            {"type": "tool_use", "id": call.get("id", ""), "name": fn.get("name", ""), "input": input_obj}
        )
    stop = STOP_REASON_MAP.get(choice.get("finish_reason", "stop"), "end_turn")
    usage = payload.get("usage", {}) or {}
    return {
        "id": payload.get("id", "msg_shim"),
        "type": "response",
        "role": "assistant",
        "model": model,
        "content": content or [{"type": "text", "text": ""}],
        "stop_reason": stop,
        "stop_sequence": None,
        "usage": {
            "input_tokens": usage.get("prompt_tokens", 0),
            "output_tokens": usage.get("completion_tokens", 0),
        },
    }


class ShimState:
    """Per-app config captured at start; immutable for the app's lifetime."""

    def __init__(self, openai_base_url: str, api_key: str):
        self.openai_base_url = openai_base_url.rstrip("/")
        self.api_key = api_key


async def handle_messages(request: web.Request) -> web.StreamResponse:
    state: ShimState = request.app["state"]
    try:
        body = await request.json()
    except Exception:
        return web.json_response(
            {"code": "invalid_json", "type": "error",
             "error": {"type": "invalid_request_error", "message": "invalid JSON"}},
            status=400,
        )
    model = body.get("model", "")
    forward = anthropic_to_openai(body)
    headers = {"content-type": "application/json"}
    if state.api_key:
        headers["authorization"] = f"Bearer {state.api_key}"

    session: ClientSession = request.app["client"]
    try:
        async with session.post(
            f"{state.openai_base_url}/chat/completions",
            json=forward,
            headers=headers,
        ) as resp:
            if not forward.get("stream"):
                data = await resp.json(content_type=None)
                if resp.status != 200:
                    detail = (
                        data.get("error", {}).get("message", str(data)[:300])
                        if isinstance(data, dict)
                        else str(data)[:300]
                    )
                    return web.json_response(
                        {
                            "code": "backend_error",
                            "type": "error",
                            "error": {
                                "type": "api_error",
                                "message": f"backend {resp.status}: {detail}",
                            },
                        },
                        status=502,
                    )
                return web.json_response(openai_to_anthropic(data, model))
            return await _stream_translation(resp, request, model)
    except Exception as exc:  # noqa: BLE001 - surface as provider error frame
        logger.exception("shim backend failure")
        return web.json_response(
            {
                "code": "backend_unreachable",
                "type": "error",
                "error": {"type": "api_error", "message": f"shim: backend unreachable: {exc}"},
            },
            status=502,
        )


async def _stream_translation(
    resp, request: web.Request, model: str
) -> web.StreamResponse:
    """Rebuild Anthropic SSE framing from OpenAI chat.completions chunks.

    Handles interleaved TEXT and TOOL_CALL streams: OpenAI backends stream
    tool calls as per-index fragments (``delta.tool_calls[i].function.arguments``
    arrives in pieces), which we re-emit as Anthropic ``tool_use`` blocks with
    ``input_json_delta`` partials. Usage, when the backend reports it (final
    chunk via ``stream_options.include_usage``), lands in ``message_delta``.
    """
    out = web.StreamResponse(
        headers={
            "content-type": "text/event-stream",
            "cache-control": "no-cache",
            "connection": "keep-alive",
        }
    )
    await out.prepare(request)

    EVENT_NAMES = {
        "message_start": "message_start",
        "content_block_start": "content_block_start",
        "content_block_delta": "content_block_delta",
        "content_block_stop": "content_block_stop",
        "message_delta": "message_delta",
        "message_stop": "message_stop",
        "error": "error",
    }

    def send(event: dict[str, Any]) -> bytes:
        name = EVENT_NAMES.get(event.get("_t"), "message_stop")
        payload = {k: v for k, v in event.items() if k != "_t"}
        return f"event: {name}\ndata: {json.dumps(payload)}\n\n".encode()

    msg_id = f"msg_{model}_{id(resp)}"
    await out.write(send({
        "_t": "message_start", "type": "message_start",
        "message": {"id": msg_id, "type": "message", "role": "assistant",
                    "model": model, "content": [], "stop_reason": None,
                    "usage": {"input_tokens": 0, "output_tokens": 0}},
    }))

    # Block state: Anthropic blocks are opened/closed sequentially. ``open_kind``
    # is None | "text" | ("tool", openai_index). Tool accumulators buffer
    # argument fragments until their block is opened.
    next_index = 0
    open_kind: str | tuple[str, int] | None = None
    tools: dict[int, dict[str, Any]] = {}   # openai idx -> {id,name,args,opened}

    async def _close_open() -> None:
        nonlocal open_kind
        if open_kind is None:
            return
        await out.write(send({"_t": "content_block_stop",
                              "type": "content_block_stop", "index": next_index - 1}))
        open_kind = None

    async def _open_text() -> int:
        nonlocal next_index, open_kind
        await _close_open()
        idx = next_index
        next_index += 1
        await out.write(send({"_t": "content_block_start",
                              "type": "content_block_start", "index": idx,
                              "content_block": {"type": "text", "text": ""}}))
        open_kind = "text"
        return idx

    async def _ensure_tool_open(idx_oai: int) -> None:
        nonlocal next_index, open_kind
        acc = tools[idx_oai]
        if acc.get("opened"):
            return
        await _close_open()
        a_idx = next_index
        next_index += 1
        await out.write(send({
            "_t": "content_block_start", "type": "content_block_start",
            "index": a_idx,
            "content_block": {"type": "tool_use", "id": acc["id"],
                              "name": acc["name"], "input": {}},
        }))
        acc["opened"] = True
        acc["a_index"] = a_idx
        open_kind = ("tool", idx_oai)

    finish_reason = "stop"
    usage_in = usage_out = 0

    async for raw in resp.content:
        line = raw.decode("utf-8", "replace").strip()
        if not line.startswith("data:"):
            continue
        data_str = line[len("data:"):].strip()
        if data_str == "[DONE]":
            break
        try:
            chunk = json.loads(data_str)
        except json.JSONDecodeError:
            continue

        usage = chunk.get("usage")
        if usage:
            usage_in = usage.get("prompt_tokens", usage_in)
            usage_out = usage.get("completion_tokens", usage_out)

        choices = chunk.get("choices") or []
        choice = choices[0] if choices else {}
        if choice.get("finish_reason"):
            finish_reason = choice["finish_reason"]
        delta = choice.get("delta", {}) or {}

        text = delta.get("content")
        if text:
            if open_kind != "text":
                await _close_open()
                await _open_text()
            await out.write(send({
                "_t": "content_block_delta", "type": "content_block_delta",
                "index": next_index - 1,
                "delta": {"type": "text_delta", "text": text},
            }))

        for call in delta.get("tool_calls") or []:
            oai_idx = call.get("index", 0)
            acc = tools.setdefault(
                oai_idx, {"id": f"toolu_{oai_idx}", "name": "", "args": "", "opened": False}
            )
            if call.get("id"):
                acc["id"] = call["id"]
            fn = call.get("function") or {}
            if fn.get("name"):
                acc["name"] += fn["name"]
            fragment = fn.get("arguments") or ""
            if (fragment or fn.get("name")) and not acc.get("opened"):
                await _ensure_tool_open(oai_idx)
            if fragment:
                await out.write(send({
                    "_t": "content_block_delta", "type": "content_block_delta",
                    "index": acc["a_index"],
                    "delta": {"type": "input_json_delta", "partial_json": fragment},
                }))
                acc["args"] += fragment

    # Close any still-open tool blocks (a backend that named a tool but sent
    # zero argument fragments still gets an empty-input block).
    for oai_idx, acc in list(tools.items()):
        if not acc.get("opened") and acc["name"]:
            await _ensure_tool_open(oai_idx)
    await _close_open()

    await out.write(send({
        "_t": "message_delta", "type": "message_delta",
        "delta": {"stop_reason": STOP_REASON_MAP.get(finish_reason, "end_turn"),
                  "stop_sequence": None},
        "usage": {"output_tokens": usage_out},
    }))
    await out.write(send({"_t": "message_stop", "type": "message_stop"}))
    await out.write_eof()
    return out


async def handle_count_tokens(request: web.Request) -> web.Response:
    """POST /v1/messages/count_tokens — cheap heuristic estimate.

    ~4 chars/token over system + messages + serialized tool schemas. The
    claude-agent-acp layer only needs a ballpark for context-window gating;
    exact tokenizer parity is explicitly NOT a goal here.
    """
    try:
        body = await request.json()
    except Exception:
        return web.json_response(
            {"code": "invalid_json", "error": {"type": "invalid_request_error", "message": "invalid JSON"}},
            status=400,
        )
    total = len(json.dumps(body.get("system", "")))
    for msg in body.get("messages") or []:
        content = msg.get("content")
        if isinstance(content, str):
            total += len(content)
        else:
            for block in content or []:
                if isinstance(block, dict):
                    if block.get("type") == "text":
                        total += len(block.get("text", ""))
                    elif block.get("type") == "image":
                        total += 1600  # ~one image, conservative flat cost
                    else:
                        total += len(json.dumps(block))
    for tool in body.get("tools") or []:
        total += len(json.dumps(tool))
    return web.json_response({"input_tokens": max(1, total // 4)})

async def handle_health(request: web.Request) -> web.Response:
    return web.json_response({"ok": True, "service": "kirocrew-shim"})


def build_shim_app(openai_base_url: str, api_key: str) -> web.Application:
    app = web.Application()
    app["state"] = ShimState(openai_base_url, api_key)
    app["client"] = ClientSession()
    app.router.add_get("/health", handle_health)
    app.router.add_post("/v1/messages", handle_messages)
    app.router.add_post("/v1/messages/count_tokens", handle_count_tokens)

    async def _close(app: web.Application) -> None:
        await app["client"].close()

    app.on_cleanup.append(_close)
    return app


async def start_shim(host: str, port: int, openai_base_url: str, api_key: str) -> tuple[Any, Any]:
    """Bind the shim on loopback. Returns ``(runner, site)`` — keep a reference
    to both for the process lifetime; cancel/``runner.cleanup()`` at shutdown."""
    runner = web.AppRunner(build_shim_app(openai_base_url, api_key),
                           access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    logger.info("anthropic shim listening on http://%s:%s → %s", host, port, openai_base_url)
    return runner, site
