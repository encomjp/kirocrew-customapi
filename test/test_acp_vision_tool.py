"""Tests for the ``vision_analyze`` kirocrew-core MCP tool + vision routing.

Covers the schema gate (exactly one of path/url, http(s) only), the handler's
dispatch through the shared vision subagent helper (subagent mocked so no real
ACP session or network is touched), and the :func:`decide_image_input_mode`
routing decision (registry metadata, config override, router text-only list).
"""

from __future__ import annotations

import pytest

from kiro_crew.acp.vision import (
    decide_image_input_mode,
    describe_image_via_vision,
    vision_subagent_describe,
)
from kiro_crew.mcp_core import _call_tool, _list_tools
from kiro_crew.validation import (
    MCP_CORE_SCHEMAS,
    VISION_ANALYZE_SCHEMA,
    ValidationError,
    validate_tool_args,
)


def _tool_spec() -> dict:
    for spec in _list_tools():
        if spec.get("name") == "vision_analyze":
            return spec
    raise AssertionError("vision_analyze not advertised in _list_tools()")


class TestDecideImageInputMode:
    def test_registry_vision_model_native(self):
        # Claude models are declared vision-capable in model_registry.json.
        assert decide_image_input_mode("claude-opus-4.8") == "native"
        assert decide_image_input_mode("claude-haiku-4.5") == "native"

    def test_router_text_only_model_text(self):
        assert (
            decide_image_input_mode(
                "oc/deepseek-v4-flash",
                text_only_models={"oc/deepseek-v4-flash", "ol/deepseek-v4-flash:0731"},
            )
            == "text"
        )
        assert (
            decide_image_input_mode(
                "ol/deepseek-v4-flash:0731",
                text_only_models={"oc/deepseek-v4-flash", "ol/deepseek-v4-flash:0731"},
            )
            == "text"
        )

    def test_unknown_router_model_native(self):
        # Fail open on capability: an unlisted router model is treated as
        # vision-capable (a genuine 400 is actionable; silent text-routing of
        # every image forever is not).
        assert decide_image_input_mode("cmc/mimo-v2.5") == "native"

    def test_pinned_mode_wins(self):
        assert decide_image_input_mode("claude-opus-4.8", image_input_mode="text") == "text"
        assert (
            decide_image_input_mode(
                "oc/deepseek-v4-flash",
                image_input_mode="native",
                text_only_models={"oc/deepseek-v4-flash"},
            )
            == "native"
        )

    def test_registry_flag_override_wins(self):
        # Caller-supplied capability (e.g. router /v1/models capabilities)
        # beats the registry lookup.
        assert decide_image_input_mode("claude-opus-4.8", registry_supports_vision=False) == "text"
        assert (
            decide_image_input_mode("oc/deepseek-v4-flash", registry_supports_vision=True)
            == "native"
        )

    def test_invalid_mode_coerces_to_auto(self):
        assert decide_image_input_mode("cmc/mimo-v2.5", image_input_mode="bogus") == "native"


class TestVisionAnalyzeToolRegistration:
    def test_advertised_with_path_and_url(self):
        spec = _tool_spec()
        schema = spec["inputSchema"]
        assert set(schema["properties"]) == {"path", "url"}
        # anyOf enforces exactly-one-of at the tool-list level (the schema
        # validator enforces it at call time via the custom validator).
        assert schema.get("anyOf") == [
            {"required": ["path"]},
            {"required": ["url"]},
        ]

    def test_registered_in_mcp_core_schemas(self):
        # Without this, _validate_args passes args through raw and a bad call
        # would propagate a ValidationError out of the stdio loop, killing the
        # kirocrew-core server (see test_mcp_core_arg_crash.py).
        assert MCP_CORE_SCHEMAS["vision_analyze"] is VISION_ANALYZE_SCHEMA


class TestVisionAnalyzeSchema:
    def test_path_valid(self):
        result = validate_tool_args({"path": "/tmp/a.png"}, VISION_ANALYZE_SCHEMA)
        assert result["path"] == "/tmp/a.png"

    def test_url_valid(self):
        result = validate_tool_args({"url": "https://example.com/a.png"}, VISION_ANALYZE_SCHEMA)
        assert result["url"] == "https://example.com/a.png"

    def test_neither_rejected(self):
        with pytest.raises(ValidationError, match="exactly one"):
            validate_tool_args({}, VISION_ANALYZE_SCHEMA)

    def test_both_rejected(self):
        with pytest.raises(ValidationError, match="exactly one"):
            validate_tool_args(
                {"path": "/tmp/a.png", "url": "https://example.com/a.png"},
                VISION_ANALYZE_SCHEMA,
            )

    def test_non_http_url_rejected(self):
        with pytest.raises(ValidationError, match="invalid format"):
            validate_tool_args({"url": "file:///tmp/a.png"}, VISION_ANALYZE_SCHEMA)

    def test_relative_path_rejected(self):
        with pytest.raises(ValidationError, match="invalid format"):
            validate_tool_args({"path": "a.png"}, VISION_ANALYZE_SCHEMA)

    def test_bad_call_returns_clean_error(self):
        # The MCP outer guard converts a schema rejection into an "Error:" string.
        result = _call_tool("vision_analyze", {})
        assert isinstance(result, str)
        assert result.lower().startswith("error")


class TestVisionSubagentDescribe:
    @pytest.mark.asyncio
    async def test_returns_description(self, monkeypatch):
        async def fake_stream(*args, **kwargs):
            yield "A cat sits on a mat."

        async def fake_shutdown():
            return None

        class FakeClient:
            def __init__(self, **kwargs):
                self._kwargs = kwargs

            def send_message_stream(self, *a, **kw):
                return fake_stream(*a, **kw)

            async def shutdown(self):
                await fake_shutdown()

        monkeypatch.setattr("kiro_crew.acp.client.AcpClient", FakeClient, raising=False)
        out = await vision_subagent_describe(
            "/tmp/a.png",
            vision_model="cmc/mimo-v2.5",
        )
        assert out == "A cat sits on a mat."

    @pytest.mark.asyncio
    async def test_returns_unavailable_on_failure(self, monkeypatch):
        async def boom(*args, **kwargs):
            raise RuntimeError("boom")
            yield  # pragma: no cover - make this an async generator

        class FakeClient:
            def __init__(self, **kwargs):
                pass

            def send_message_stream(self, *a, **kw):
                return boom(*a, **kw)

            async def shutdown(self):
                return None

        monkeypatch.setattr("kiro_crew.acp.client.AcpClient", FakeClient, raising=False)
        out = await describe_image_via_vision(
            "/tmp/a.png",
            vision_model="cmc/mimo-v2.5",
        )
        assert out == "unavailable"
