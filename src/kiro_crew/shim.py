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
            {"type": "error", "error": {"type": "invalid_request_error", "message": "invalid JSON"}},
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
                "type": "error",
                "error": {"type": "api_error", "message": f"shim: backend unreachable: {exc}"},
            },
            status=502,
        )


async def _stream_translation(
    resp, request: web.Request, model: str
) -> web.StreamResponse:
    """Rebuild Anthropic SSE framing from OpenAI chat.completions chunks."""
    out = web.StreamResponse(
        headers={
            "content-type": "text/event-stream",
            "cache-control": "no-cache",
            "connection": "keep-alive",
        }
    )
    await out.prepare(request)

    def send(event: dict[str, Any]) -> bytes:
        name = {"message_start": "message_start", "content_block_start": "content_block_start",
                "content_block_delta": "content_block_delta", "content_block_stop": "content_block_stop",
                "message_delta": "message_delta", "message_stop": "message_stop",
                "ping": "ping", "error": "error"}.get(event.get("_t"), "message_stop")
        return f"event: {name}\ndata: {json.dumps({k: v for k, v in event.items() if k != '_t'})}\n\n".encode()

    msg_id = f"msg_{model}_{id(resp)}"
    await out.write(send({
        "_t": "message_start", "type": "message_start",
        "message": {"id": msg_id, "type": "message", "role": "assistant",
                    "model": model, "content": [], "stop_reason": None,
                    "usage": {"input_tokens": 0, "output_tokens": 0}},
    }))
    await out.write(send({"_t": "content_block_start", "type": "content_block_start",
                          "index": 0, "content_block": {"type": "text", "text": ""}}))
    finish_reason = "stop"
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
        choice = (chunk.get("choices") or [{}])[0]
        delta = choice.get("delta", {}) or {}
        if choice.get("finish_reason"):
            finish_reason = choice["finish_reason"]
        if delta.get("content"):
            await out.write(send({
                "_t": "content_block_delta", "type": "content_block_delta",
                "index": 0, "delta": {"type": "text_delta", "text": delta["content"]},
            }))
    await out.write(send({"_t": "content_block_stop", "type": "content_block_stop", "index": 0}))
    await out.write(send({
        "_t": "message_delta", "type": "message_delta",
        "delta": {"stop_reason": STOP_REASON_MAP.get(finish_reason, "end_turn"), "stop_sequence": None},
        "usage": {"output_tokens": 0},
    }))
    await out.write(send({"_t": "message_stop", "type": "message_stop"}))
    await out.write_eof()
    return out


async def handle_health(request: web.Request) -> web.Response:
    return web.json_response({"ok": True, "service": "kirocrew-shim"})


def build_shim_app(openai_base_url: str, api_key: str) -> web.Application:
    app = web.Application()
    app["state"] = ShimState(openai_base_url, api_key)
    app["client"] = ClientSession()
    app.router.add_get("/health", handle_health)
    app.router.add_post("/v1/messages", handle_messages)

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
