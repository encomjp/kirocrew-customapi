"""Tests for the fork's provider_guard (validation + safe_mode) and the
Anthropic↔OpenAI shim translation functions. Pure-function focus: no network,
no config files, no event loop except one streaming smoke test."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
import pytest_asyncio
from aiohttp import web as aweb
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.provider_guard import (
    assert_endpoint_allowed,
    classify_endpoint,
    endpoint_is_local,
    validate_provider_settings,
)
import kiro_crew.shim as shim_mod
from kiro_crew.shim import anthropic_to_openai, openai_to_anthropic


# ── provider_guard: validation ────────────────────────────────────────────


def _agent(**kw):
    base = dict(
        provider="acp",
        provider_base_url="",
        provider_api_key="",
        model="",
        safe_mode=False,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def test_acp_provider_never_produces_router_problems():
    assert validate_provider_settings(_agent()) == []


def test_claude_code_without_url_or_key_flagged():
    problems = validate_provider_settings(_agent(provider="claude_code"))
    assert any("401" in p for p in problems)


def test_claude_code_router_with_auto_model_flagged():
    problems = validate_provider_settings(
        _agent(provider="claude_code", provider_base_url="http://127.0.0.1:8317")
    )
    assert any("model" in p for p in problems)


def test_opencode_without_base_url_flagged():
    problems = validate_provider_settings(_agent(provider="opencode"))
    assert any("opencode" in p.lower() for p in problems)


# ── provider_guard: safe_mode ─────────────────────────────────────────────


def test_safe_mode_allows_loopback():
    assert_endpoint_allowed("http://127.0.0.1:8391", safe_mode=True)
    assert_endpoint_allowed("http://localhost:11434/v1", safe_mode=True)


def test_safe_mode_allows_rfc1918_literal():
    assert endpoint_is_local("http://192.168.8.1:8317") is True
    assert endpoint_is_local("http://10.0.0.5/v1") is True


def test_safe_mode_blocks_public_ip():
    assert endpoint_is_local("http://93.184.216.34/v1") is False
    with pytest.raises(ValueError, match="safe_mode"):
        assert_endpoint_allowed("http://93.184.216.34/v1", safe_mode=True)


def test_safe_mode_off_is_noop_even_for_public():
    assert_endpoint_allowed("http://93.184.216.34/v1", safe_mode=False)


def test_classify_defaults_ports():
    assert classify_endpoint("https://api.example.com")[1] == 443
    assert classify_endpoint("http://127.0.0.1:8391") == ("127.0.0.1", 8391)


# ── shim: request translation ─────────────────────────────────────────────


def test_basic_request_translation():
    body = {
        "model": "m1",
        "max_tokens": 100,
        "system": "be brief",
        "messages": [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": [{"type": "text", "text": "hi"}]},
            {"role": "user", "content": "go on"},
        ],
    }
    out = anthropic_to_openai(body)
    assert out["model"] == "m1"
    assert out["messages"][0] == {"role": "system", "content": "be brief"}
    assert out["messages"][1]["role"] == "user"
    assert out["stream"] is False


def test_tool_definition_translation():
    body = {
        "model": "m",
        "tools": [
            {"name": "ls", "description": "list", "input_schema": {"type": "object"}}
        ],
        "messages": [{"role": "user", "content": "run ls"}],
    }
    out = anthropic_to_openai(body)
    assert out["tools"][0]["function"]["name"] == "ls"


def test_tool_result_history_translation():
    body = {
        "model": "m",
        "messages": [
            {"role": "assistant", "content": [
                {"type": "tool_use", "id": "t1", "name": "ls", "input": {}}
            ]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "t1", "content": "a.txt"}
            ]},
        ],
    }
    out = anthropic_to_openai(body)
    assert out["messages"][0]["tool_calls"][0]["id"] == "t1"
    assert out["messages"][1]["role"] == "tool"
    assert out["messages"][1]["tool_call_id"] == "t1"
    assert out["messages"][1]["content"] == "a.txt"


def test_image_block_becomes_data_url_part():
    body = {
        "model": "m",
        "messages": [
            {"role": "user", "content": [
                {"type": "text", "text": "what"},
                {"type": "image", "source": {"type": "base64",
                                             "media_type": "image/png",
                                             "data": "AAAA"}},
            ]}
        ],
    }
    out = anthropic_to_openai(body)
    parts = out["messages"][0]["content"]
    assert parts[1]["image_url"]["url"].startswith("data:image/png;base64,")


# ── shim: response translation ────────────────────────────────────────────


def test_response_text_translation():
    payload = {
        "id": "x",
        "choices": [{"message": {"role": "assistant", "content": "ok"},
                     "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 3, "completion_tokens": 2},
    }
    out = openai_to_anthropic(payload, "m1")
    assert out["content"] == [{"type": "text", "text": "ok"}]
    assert out["stop_reason"] == "end_turn"
    assert out["usage"] == {"input_tokens": 3, "output_tokens": 2}


def test_response_tool_call_translation():
    payload = {
        "choices": [{
            "message": {
                "role": "assistant",
                "content": None,
                "tool_calls": [{
                    "id": "c1", "type": "function",
                    "function": {"name": "ls", "arguments": '{"path": "/"}'},
                }],
            },
            "finish_reason": "tool_calls",
        }],
    }
    out = openai_to_anthropic(payload, "m")
    block = out["content"][0]
    assert block["type"] == "tool_use"
    assert block["name"] == "ls"
    assert block["input"] == {"path": "/"}
    assert out["stop_reason"] == "tool_use"


def test_streaming_smoke_via_live_shim():
    """Full loop against a fake OpenAI backend: SSE framing comes back in
    Anthropic order with translated deltas."""
    import asyncio

    from aiohttp import ClientSession, web as aweb
    from aiohttp.test_utils import TestClient, TestServer

    from kiro_crew.shim import build_shim_app

    async def fake_backend(request):
        async def gen():
            for piece in ["he", "llo"]:
                chunk = {"choices": [{"delta": {"content": piece}}]}
                yield f"data: {json.dumps(chunk)}\n\n".encode()
            yield b"data: [DONE]\n\n"
        resp = aweb.StreamResponse(headers={"content-type": "text/event-stream"})
        await resp.prepare(request)
        async for piece in gen():
            await resp.write(piece)
        await resp.write_eof()
        return resp

    async def run():
        backend = aweb.Application()
        backend.router.add_post("/v1/chat/completions", fake_backend)
        bclient = TestClient(TestServer(backend))
        await bclient.start_server()
        # Real loopback URL of the fake backend (a literal "http://fake" would
        # never resolve) — the shim forwards to <base>/chat/completions.
        app = build_shim_app(str(bclient.make_url("/v1")), "")
        sclient = TestClient(TestServer(app))
        await sclient.start_server()
        try:
            async with ClientSession() as http:
                async with http.post(
                    sclient.make_url("/v1/messages"),
                    json={"model": "m", "max_tokens": 10, "stream": True,
                          "messages": [{"role": "user", "content": "hi"}]},
                ) as resp:
                    events = []
                    text = ""
                    async for line in resp.content:
                        line = line.decode().strip()
                        if line.startswith("data:"):
                            ev = json.loads(line[5:])
                            events.append(ev.get("type"))
                            if ev.get("type") == "content_block_delta":
                                text += ev["delta"]["text"]
                    assert events[0] == "message_start"
                    assert "content_block_delta" in events
                    assert events[-1] == "message_stop"
                    assert text == "hello"
        finally:
            await sclient.close()
            await bclient.close()

    asyncio.run(run())


# ── shim v2: streaming tool-calls, usage, count_tokens ────────────────────


@pytest_asyncio.fixture
async def shim_client_factory():
    """Start a shim instance pointed at a caller-supplied fake OpenAI backend
    handler; async factory so each test controls its backend's behavior."""

    async def _make(handler) -> TestClient:
        backend_app = aweb.Application()
        backend_app.router.add_post("/v1/chat/completions", handler)
        backend = TestClient(TestServer(backend_app))
        await backend.start_server()
        app = shim_mod.build_shim_app(str(backend.make_url("/v1")), "")
        client = TestClient(TestServer(app))
        await client.start_server()
        client._backend = backend  # type: ignore[attr-defined]  # keep alive
        return client

    yield _make


@pytest.mark.asyncio
async def test_streaming_tool_call_fragments(shim_client_factory):
    """OpenAI-style fragmented tool_call deltas → one tool_use block with
    assembled input_json_delta partials."""
    async def fake_backend(request):
        async def gen():
            chunks = [
                {"choices": [{"delta": {"tool_calls": [
                    {"index": 0, "id": "c9", "type": "function",
                     "function": {"name": "calculator", "arguments": ""}}]}}]},
                {"choices": [{"delta": {"tool_calls": [
                    {"index": 0, "function": {"arguments": '{"a": 6,'}}]}}]},
                {"choices": [{"delta": {"tool_calls": [
                    {"index": 0, "function": {"arguments": ' "b": 7}'}}]}}]},
                {"choices": [{"delta": {}, "finish_reason": "tool_calls"}],
                 "usage": {"prompt_tokens": 11, "completion_tokens": 5}},
            ]
            for c in chunks:
                yield f"data: {json.dumps(c)}\n\n".encode()
            yield b"data: [DONE]\n\n"
        resp = aweb.StreamResponse(headers={"content-type": "text/event-stream"})
        await resp.prepare(request)
        async for piece in gen():
            await resp.write(piece)
        await resp.write_eof()
        return resp

    client = await shim_client_factory(fake_backend)
    try:
        resp = await client.post("/v1/messages", json={
            "model": "m", "max_tokens": 10, "stream": True,
            "messages": [{"role": "user", "content": "6*7"}],
            "tools": [{"name": "calculator", "description": "d",
                       "input_schema": {"type": "object"}}],
        })
        events = []
        json_buf = ""
        stop = None
        async for raw in resp.content:
            line = raw.decode().strip()
            if not line.startswith("data:"):
                continue
            ev = json.loads(line[5:])
            events.append(ev.get("type"))
            if ev.get("type") == "content_block_delta" and ev["delta"]["type"] == "input_json_delta":
                json_buf += ev["delta"]["partial_json"]
            elif ev.get("type") == "message_delta":
                stop = ev["delta"]["stop_reason"]
        starts = [e for e in events if e == "content_block_start"]
        assert len(starts) == 1
        assert json_buf == '{"a": 6, "b": 7}'
        assert json.loads(json_buf) == {"a": 6, "b": 7}
        assert stop == "tool_use"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_streaming_text_then_tool_two_blocks(shim_client_factory):
    async def fake_backend(request):
        async def gen():
            chunks = [
                {"choices": [{"delta": {"content": "Let me compute."}}]},
                {"choices": [{"delta": {"tool_calls": [
                    {"index": 0, "id": "t1", "type": "function",
                     "function": {"name": "calc", "arguments": '{"a":1}'}}]}}]},
            ]
            for c in chunks:
                yield f"data: {json.dumps(c)}\n\n".encode()
            yield b"data: [DONE]\n\n"
        resp = aweb.StreamResponse(headers={"content-type": "text/event-stream"})
        await resp.prepare(request)
        async for piece in gen():
            await resp.write(piece)
        await resp.write_eof()
        return resp

    client = await shim_client_factory(fake_backend)
    try:
        resp = await client.post("/v1/messages", json={
            "model": "m", "max_tokens": 10, "stream": True,
            "messages": [{"role": "user", "content": "go"}],
        })
        blocks = []
        async for raw in resp.content:
            line = raw.decode().strip()
            if line.startswith("data:"):
                ev = json.loads(line[5:])
                if ev.get("type") == "content_block_start":
                    blocks.append(ev["content_block"])
        assert [b["type"] for b in blocks] == ["text", "tool_use"]
        assert blocks[1]["name"] == "calc"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_count_tokens_endpoint(shim_client_factory):
    async def fake_backend(request):  # never called
        raise AssertionError

    client = await shim_client_factory(fake_backend)
    try:
        resp = await client.post("/v1/messages/count_tokens", json={
            "model": "m",
            "system": "x" * 400,
            "messages": [{"role": "user", "content": "y" * 400}],
        })
        out = await resp.json()
        assert resp.status == 200
        assert out["input_tokens"] >= 150  # ~800 chars / 4
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_streaming_usage_forwarded(shim_client_factory):
    async def fake_backend(request):
        async def gen():
            yield b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n'
            yield b'data: {"choices":[],"usage":{"prompt_tokens":7,"completion_tokens":2}}\n\n'
            yield b"data: [DONE]\n\n"
        resp = aweb.StreamResponse(headers={"content-type": "text/event-stream"})
        await resp.prepare(request)
        async for piece in gen():
            await resp.write(piece)
        await resp.write_eof()
        return resp

    client = await shim_client_factory(fake_backend)
    try:
        resp = await client.post("/v1/messages", json={
            "model": "m", "max_tokens": 5, "stream": True,
            "messages": [{"role": "user", "content": "hi"}],
        })
        usage = None
        async for raw in resp.content:
            line = raw.decode().strip()
            if line.startswith("data:"):
                ev = json.loads(line[5:])
                if ev.get("type") == "message_delta":
                    usage = ev["usage"]
        assert usage is not None and usage["output_tokens"] == 2
    finally:
        await client.close()
