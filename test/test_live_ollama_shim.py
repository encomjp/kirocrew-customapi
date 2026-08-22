"""LIVE integration tests: fork shim against Ollama Cloud (real network).

Skipped unless OLLAMA_CLOUD_KEY is set:

    OLLAMA_CLOUD_KEY=... OLLAMA_CLOUD_MODEL=gemma4:31b \
        pytest test/test_live_ollama_shim.py -v

The shim app is served through aiohttp's TestServer INSIDE each test's own
running loop (a runner started in one asyncio.run() and queried from another
accepts TCP but never answers — the exact failure mode this file guards).
"""

from __future__ import annotations

import json
import os

import pytest
import pytest_asyncio

aiohttp = pytest.importorskip("aiohttp")
pytest.importorskip("pytest_asyncio")

from aiohttp import ClientSession  # noqa: E402
from aiohttp.test_utils import TestClient, TestServer  # noqa: E402

import kiro_crew.shim as shim_mod  # noqa: E402

OLLAMA_BASE = os.environ.get("OLLAMA_CLOUD_BASE", "https://ollama.com/v1")
KEY = os.environ.get("OLLAMA_CLOUD_KEY", "")
MODEL = os.environ.get("OLLAMA_CLOUD_MODEL", "gemma4:31b")

pytestmark = [
    pytest.mark.skipif(not KEY, reason="OLLAMA_CLOUD_KEY not set"),
    pytest.mark.asyncio,
]


@pytest_asyncio.fixture
async def shim_client():
    app = shim_mod.build_shim_app(OLLAMA_BASE, KEY)
    client = TestClient(TestServer(app))
    await client.start_server()
    yield client
    await client.close()


async def _messages(client, payload):
    return await client.post("/v1/messages", json=payload, timeout=aiohttp.ClientTimeout(total=120))


def _payload(**kw):
    body = {
        "model": MODEL,
        "max_tokens": kw.pop("max_tokens", 60),
        "messages": [{"role": "user", "content": kw.pop("prompt")}],
    }
    body.update(kw)
    return body


async def test_nonstreaming_text_roundtrip(shim_client):
    resp = await _messages(shim_client, _payload(prompt="Say exactly: ROUNDTRIP-OK"))
    assert resp.status == 200
    out = await resp.json()
    assert out["role"] == "assistant"
    texts = [b.get("text", "") for b in out["content"] if b.get("type") == "text"]
    assert any("ROUNDTRIP-OK" in t for t in texts), out["content"]
    assert out["usage"]["input_tokens"] > 0


async def test_streaming_framing(shim_client):
    resp = await _messages(
        shim_client, _payload(stream=True, prompt="Count words: one two three")
    )
    assert resp.status == 200
    events: list[str] = []
    text_parts: list[str] = []
    stop_reason = None
    async for raw in resp.content:
        line = raw.decode().strip()
        if not line.startswith("data:"):
            continue
        ev = json.loads(line[5:])
        etype = ev.get("type")
        events.append(etype)
        if etype == "content_block_delta":
            text_parts.append(ev["delta"].get("text", ""))
        elif etype == "message_delta":
            stop_reason = ev["delta"].get("stop_reason")
    assert events[0] == "message_start"
    assert "content_block_delta" in events
    assert events[-1] == "message_stop"
    assert "".join(text_parts).strip(), "no streamed text received"
    assert stop_reason in ("end_turn", "max_tokens")


async def test_tool_advertisement_and_call(shim_client):
    """Unambiguous arithmetic trigger; expect a well-formed tool_use block."""
    payload = _payload(
        max_tokens=300,
        prompt="What is 6 times 7? You MUST answer by calling the calculator tool.",
        tools=[
            {
                "name": "calculator",
                "description": "Multiply two integers. Always use this for arithmetic.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "a": {"type": "integer"},
                        "b": {"type": "integer"},
                    },
                    "required": ["a", "b"],
                },
            }
        ],
    )
    resp = await _messages(shim_client, payload)
    assert resp.status == 200
    out = await resp.json()
    tool_uses = [b for b in out["content"] if b.get("type") == "tool_use"]
    assert tool_uses, f"expected tool_use, got: {out['content']}"
    call = tool_uses[0]
    assert call["name"] == "calculator"
    assert set(call["input"]) >= {"a", "b"}
