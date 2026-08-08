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

    def test_raw_model_matches_prefixed_denylist(self):
        """The config model is often written raw (no prefix) while
        text_only_models uses the prefixed picker form; they must match. A miss
        means the redirect never fires and the text-only model 400s."""
        assert (
            decide_image_input_mode(
                "deepseek-v4-flash:0731",
                text_only_models={"ol/deepseek-v4-flash:0731"},
            )
            == "text"
        )
        assert (
            decide_image_input_mode(
                "deepseek-v4-flash",
                text_only_models={"oc/deepseek-v4-flash"},
            )
            == "text"
        )

    def test_prefixed_model_matches_raw_denylist(self):
        assert (
            decide_image_input_mode(
                "ol/deepseek-v4-flash:0731",
                text_only_models={"deepseek-v4-flash:0731"},
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


class TestResolveVisionProviders:
    def test_empty_config_appends_fallback(self):
        from kiro_crew.acp.vision import resolve_vision_providers

        providers = resolve_vision_providers(main_env={"ANTHROPIC_BASE_URL": "http://r:1"})
        assert len(providers) == 1
        assert providers[0].model == "cmc/mimo-v2.5"
        assert providers[0].provider == "router"
        assert providers[0].extra_env["ANTHROPIC_BASE_URL"] == "http://r:1"

    def test_explicit_fallback_model(self):
        from kiro_crew.acp.vision import resolve_vision_providers

        providers = resolve_vision_providers(
            vision_fallback_model="ag/gemini-3.6-flash-high",
            main_env={"ANTHROPIC_BASE_URL": "http://r:1"},
        )
        assert [p.model for p in providers] == ["ag/gemini-3.6-flash-high"]

    def test_configured_entries_then_fallback(self):
        from kiro_crew.acp.vision import resolve_vision_providers

        providers = resolve_vision_providers(
            vision_providers=[
                {"provider": "custom", "model": "qwen-vl-7b", "base_url": "http://127.0.0.1:8000/v1"},
                {"provider": "router", "model": "cmc/mimo-v2.5"},
            ],
            vision_fallback_model="ag/gemini-3.6-flash-high",
            main_env={"ANTHROPIC_BASE_URL": "http://r:1"},
        )
        # 3 providers: custom, router (deduped against fallback), then fallback
        assert len(providers) == 3
        assert providers[0].provider == "custom"
        assert providers[0].extra_env["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:8000/v1"
        assert providers[1].model == "cmc/mimo-v2.5"
        assert providers[1].extra_env["ANTHROPIC_BASE_URL"] == "http://r:1"
        assert providers[2].model == "ag/gemini-3.6-flash-high"

    def test_router_entry_uses_main_env(self):
        from kiro_crew.acp.vision import resolve_vision_providers

        providers = resolve_vision_providers(
            vision_providers=[{"provider": "router", "model": "cmc/mimo-v2.5"}],
            main_env={"ANTHROPIC_BASE_URL": "http://r:1", "ANTHROPIC_API_KEY": "k"},
        )
        assert len(providers) == 1
        assert providers[0].extra_env["ANTHROPIC_BASE_URL"] == "http://r:1"
        assert providers[0].extra_env["ANTHROPIC_API_KEY"] == "k"

    def test_dedupes_duplicate_models(self):
        from kiro_crew.acp.vision import resolve_vision_providers

        providers = resolve_vision_providers(
            vision_providers=[
                {"provider": "router", "model": "cmc/mimo-v2.5"},
                {"provider": "router", "model": "cmc/mimo-v2.5"},
            ],
            main_env={"ANTHROPIC_BASE_URL": "http://r:1"},
        )
        assert len(providers) == 1

    def test_skips_entry_without_model_or_endpoint(self):
        from kiro_crew.acp.vision import resolve_vision_providers

        providers = resolve_vision_providers(
            vision_providers=[
                {},
                {"provider": "custom", "model": "qwen-vl-7b"},  # no base_url
            ],
            main_env={"ANTHROPIC_BASE_URL": "http://r:1"},
        )
        # custom without base_url is unusable -> only the fallback remains
        assert len(providers) == 1
        assert providers[0].provider == "router"
        assert providers[0].model == "cmc/mimo-v2.5"


class TestDescribeImageViaChain:
    @pytest.mark.asyncio
    async def test_first_success_wins(self, monkeypatch):
        from kiro_crew.acp.vision import VisionProvider, describe_image_via_chain

        calls: list[str] = []

        async def fake_describe(image_ref, **kw):
            calls.append(kw.get("vision_model", ""))
            if kw["vision_model"] == "qwen-vl-7b":
                return "first description"
            return "unavailable"

        monkeypatch.setattr(
            "kiro_crew.acp.vision.describe_image_via_vision", fake_describe
        )
        out = await describe_image_via_chain(
            "/tmp/a.png",
            [
                VisionProvider("custom", "qwen-vl-7b", {"ANTHROPIC_BASE_URL": "http://x"}),
                VisionProvider("router", "cmc/mimo-v2.5", {}),
            ],
        )
        assert out == "first description"
        assert calls == ["qwen-vl-7b"]

    @pytest.mark.asyncio
    async def test_all_fail_returns_unavailable(self, monkeypatch):
        from kiro_crew.acp.vision import VisionProvider, describe_image_via_chain

        calls: list[str] = []

        async def fake_describe(image_ref, **kw):
            calls.append(kw.get("vision_model", ""))
            return "unavailable"

        monkeypatch.setattr(
            "kiro_crew.acp.vision.describe_image_via_vision", fake_describe
        )
        out = await describe_image_via_chain(
            "/tmp/a.png",
            [
                VisionProvider("custom", "a", {"ANTHROPIC_BASE_URL": "http://x"}),
                VisionProvider("router", "b", {}),
            ],
        )
        assert out == "unavailable"
        assert calls == ["a", "b"]

    @pytest.mark.asyncio
    async def test_falls_through_to_second_on_first_failure(self, monkeypatch):
        from kiro_crew.acp.vision import VisionProvider, describe_image_via_chain

        calls: list[str] = []

        async def fake_describe(image_ref, **kw):
            calls.append(kw.get("vision_model", ""))
            if kw["vision_model"] == "b":
                return "second works"
            return "unavailable"

        monkeypatch.setattr(
            "kiro_crew.acp.vision.describe_image_via_vision", fake_describe
        )
        out = await describe_image_via_chain(
            "/tmp/a.png",
            [
                VisionProvider("custom", "a", {"ANTHROPIC_BASE_URL": "http://x"}),
                VisionProvider("router", "b", {}),
            ],
        )
        assert out == "second works"
        assert calls == ["a", "b"]

    @pytest.mark.asyncio
    async def test_first_provider_raises_then_second_succeeds(self, monkeypatch):
        """A provider that RAISES (not returns 'unavailable') must still fail
        over to the next one — describe_image_via_vision catches it."""
        import kiro_crew.acp.vision as vision_mod
        from kiro_crew.acp.vision import VisionProvider, describe_image_via_chain

        calls: list[str] = []

        async def fake_subagent(image_ref, **kw):
            calls.append(kw.get("vision_model", ""))
            if kw["vision_model"] == "bad":
                raise RuntimeError("vision http describe: 401")
            return "the good description"

        monkeypatch.setattr(vision_mod, "vision_subagent_describe", fake_subagent)
        out = await describe_image_via_chain(
            "/tmp/a.png",
            [
                VisionProvider("custom", "bad", {"ANTHROPIC_BASE_URL": "http://x"}, "opencode"),
                VisionProvider("custom", "good", {"ANTHROPIC_BASE_URL": "http://y"}, "opencode"),
            ],
        )
        assert out == "the good description"
        assert calls == ["bad", "good"]

    @pytest.mark.asyncio
    async def test_empty_chain_returns_unavailable(self):
        from kiro_crew.acp.vision import describe_image_via_chain

        out = await describe_image_via_chain("/tmp/a.png", [])
        assert out == "unavailable"

    @pytest.mark.asyncio
    async def test_all_fail_with_exceptions_returns_unavailable(self, monkeypatch):
        import kiro_crew.acp.vision as vision_mod
        from kiro_crew.acp.vision import VisionProvider, describe_image_via_chain

        async def fake_subagent(image_ref, **kw):
            raise RuntimeError("boom")

        monkeypatch.setattr(vision_mod, "vision_subagent_describe", fake_subagent)
        out = await describe_image_via_chain(
            "/tmp/a.png",
            [
                VisionProvider("custom", "a", {"ANTHROPIC_BASE_URL": "http://x"}, "opencode"),
                VisionProvider("custom", "b", {"ANTHROPIC_BASE_URL": "http://y"}, "opencode"),
            ],
        )
        assert out == "unavailable"


class TestHttpDescribe:
    @pytest.mark.asyncio
    async def test_opencode_backend_uses_http(self, monkeypatch, tmp_path):
        """An opencode-backend provider must describe via direct HTTP, because
        opencode's ACP adapter drops image parts (verified on the wire)."""
        from kiro_crew.acp.vision import vision_subagent_describe

        img = tmp_path / "shot.png"
        img.write_bytes(b"fakepngbytes")

        captured: dict = {}

        class _Resp:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self):
                return b'{"choices":[{"message":{"content":" a red square "}}]}'

        def fake_urlopen(req, timeout=None):
            captured["url"] = req.full_url
            captured["data"] = req.data
            return _Resp()

        import json
        import urllib.request

        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
        out = await vision_subagent_describe(
            str(img),
            vision_model="kimi-k2.6",
            extra_env={
                "ANTHROPIC_BASE_URL": "https://ollama.com/v1",
                "ANTHROPIC_API_KEY": "k",
            },
            acp_backend="opencode",
        )
        assert out == "a red square"
        assert captured["url"] == "https://ollama.com/v1/chat/completions"
        body = json.loads(captured["data"])
        assert body["model"] == "kimi-k2.6"
        content = body["messages"][0]["content"]
        assert content[0]["type"] == "text"
        assert content[1]["type"] == "image_url"
        assert content[1]["image_url"]["url"].startswith("data:image/png;base64,")

    @pytest.mark.asyncio
    async def test_url_ref_passed_through(self, monkeypatch):
        from kiro_crew.acp.vision import vision_subagent_describe

        captured: dict = {}

        class _Resp:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self):
                return b'{"choices":[{"message":{"content":"ok"}}]}'

        def fake_urlopen(req, timeout=None):
            captured["url"] = req.full_url
            captured["data"] = req.data
            return _Resp()

        import json
        import urllib.request

        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
        out = await vision_subagent_describe(
            "https://example.com/a.png",
            vision_model="kimi-k2.6",
            extra_env={
                "ANTHROPIC_BASE_URL": "https://ollama.com/v1",
                "ANTHROPIC_API_KEY": "k",
            },
            acp_backend="opencode",
        )
        assert out == "ok"
        content = json.loads(captured["data"])["messages"][0]["content"]
        assert content[1]["image_url"]["url"] == "https://example.com/a.png"

    @pytest.mark.asyncio
    async def test_non_opencode_backend_uses_acp_subagent(self, monkeypatch):
        """kiro-cli (acp_backend='') keeps the ACP subagent path."""
        from kiro_crew.acp.vision import vision_subagent_describe

        async def fake_stream(*args, **kwargs):
            yield "described via acp"

        class FakeClient:
            def __init__(self, **kwargs):
                pass

            def send_message_stream(self, *a, **kw):
                return fake_stream(*a, **kw)

            async def shutdown(self):
                return None

        monkeypatch.setattr("kiro_crew.acp.client.AcpClient", FakeClient, raising=False)
        # kiro-cli path sends the path as text; the HTTP branch must NOT fire
        # even though ANTHROPIC_BASE_URL is present.
        out = await vision_subagent_describe(
            "/tmp/a.png",
            vision_model="cmc/mimo-v2.5",
            extra_env={"ANTHROPIC_BASE_URL": "http://router:8317"},
            acp_backend="",
        )
        assert out == "described via acp"

    @pytest.mark.asyncio
    async def test_http_error_surfaces_clean_error(self, monkeypatch, tmp_path):
        """A 401/404 from the vision endpoint becomes a clean RuntimeError,
        not a raw HTTPError."""
        from kiro_crew.acp.vision import _http_describe_image

        img = tmp_path / "shot.png"
        img.write_bytes(b"pngbytes")

        import urllib.error
        import urllib.request

        def fake_urlopen(req, timeout=None):
            raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {}, None)

        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
        with pytest.raises(RuntimeError, match="401"):
            await _http_describe_image(
                str(img),
                vision_model="kimi-k2.6",
                env={"ANTHROPIC_BASE_URL": "https://ollama.com/v1", "ANTHROPIC_API_KEY": "bad"},
                timeout=15,
            )

    @pytest.mark.asyncio
    async def test_network_error_surfaces_clean_error(self, monkeypatch, tmp_path):
        from kiro_crew.acp.vision import _http_describe_image

        img = tmp_path / "shot.png"
        img.write_bytes(b"pngbytes")

        import urllib.error
        import urllib.request

        def fake_urlopen(req, timeout=None):
            raise urllib.error.URLError("connection refused")

        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
        with pytest.raises(RuntimeError, match="network error"):
            await _http_describe_image(
                str(img),
                vision_model="kimi-k2.6",
                env={"ANTHROPIC_BASE_URL": "https://ollama.com/v1", "ANTHROPIC_API_KEY": "k"},
                timeout=15,
            )

    @pytest.mark.asyncio
    async def test_non_json_reply_raises(self, monkeypatch, tmp_path):
        from kiro_crew.acp.vision import _http_describe_image

        img = tmp_path / "shot.png"
        img.write_bytes(b"pngbytes")

        import urllib.request

        class _Resp:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self):
                return b"<html>not json</html>"

        monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=None: _Resp())
        with pytest.raises(RuntimeError, match="non-JSON"):
            await _http_describe_image(
                str(img),
                vision_model="kimi-k2.6",
                env={"ANTHROPIC_BASE_URL": "https://ollama.com/v1", "ANTHROPIC_API_KEY": "k"},
                timeout=15,
            )

    @pytest.mark.asyncio
    async def test_empty_content_with_reasoning_surfaces_reasoning(self, monkeypatch, tmp_path):
        """A reasoning model that runs out of tokens returns empty content but
        a non-empty reasoning field; surface that instead of a silent ''."""
        from kiro_crew.acp.vision import _http_describe_image

        img = tmp_path / "shot.png"
        img.write_bytes(b"pngbytes")

        import json
        import urllib.request

        payload = json.dumps(
            {
                "choices": [
                    {
                        "message": {
                            "content": "",
                            "reasoning": "Let me think about the color of this image carefully...",
                        }
                    }
                ]
            }
        ).encode()

        class _Resp:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self):
                return payload

        monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=None: _Resp())
        out = await _http_describe_image(
            str(img),
            vision_model="kimi-k2.6",
            env={"ANTHROPIC_BASE_URL": "https://ollama.com/v1", "ANTHROPIC_API_KEY": "k"},
            timeout=15,
        )
        assert "reasoning" in out
        assert "color" in out

    @pytest.mark.asyncio
    async def test_empty_content_no_reasoning_raises(self, monkeypatch, tmp_path):
        from kiro_crew.acp.vision import _http_describe_image

        img = tmp_path / "shot.png"
        img.write_bytes(b"pngbytes")

        import json
        import urllib.request

        payload = json.dumps({"choices": [{"message": {"content": ""}}]}).encode()

        class _Resp:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self):
                return payload

        monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=None: _Resp())
        with pytest.raises(RuntimeError, match="empty reply"):
            await _http_describe_image(
                str(img),
                vision_model="kimi-k2.6",
                env={"ANTHROPIC_BASE_URL": "https://ollama.com/v1", "ANTHROPIC_API_KEY": "k"},
                timeout=15,
            )

    @pytest.mark.asyncio
    async def test_missing_choices_raises(self, monkeypatch, tmp_path):
        from kiro_crew.acp.vision import _http_describe_image

        img = tmp_path / "shot.png"
        img.write_bytes(b"pngbytes")

        import json
        import urllib.request

        class _Resp:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self):
                return json.dumps({"error": "model not found"}).encode()

        monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=None: _Resp())
        with pytest.raises(RuntimeError, match="unexpected reply shape"):
            await _http_describe_image(
                str(img),
                vision_model="kimi-k2.6",
                env={"ANTHROPIC_BASE_URL": "https://ollama.com/v1", "ANTHROPIC_API_KEY": "k"},
                timeout=15,
            )


class TestSessionHandleRedirect:
    """The shared-runtime prompt() path (dashboard/cron) must apply the same
    image redirect as AcpClient._send_prompt."""

    def _make_runtime(self):
        class _Rt:
            supports_image_prompt = True

            def __init__(self):
                self.sent: list[dict] = []

            async def send_request(self, method, params):
                self.sent.append({"method": method, "params": params})
                return 1

            def is_alive(self):
                return True

            def send_notification(self, *a, **kw):
                return None

            def send_response(self, *a, **kw):
                return None

        return _Rt()

    @pytest.mark.asyncio
    async def test_text_only_model_message_is_rewritten(self, monkeypatch, tmp_path):
        import asyncio

        from kiro_crew.acp.session_handle import AcpSessionHandle

        img = tmp_path / "shot.png"
        img.write_bytes(b"pngbytes")

        # Fake the whole vision chain so no network/ACP is touched.
        import kiro_crew.acp.vision as vision_mod

        async def fake_redirect(message, **kw):
            assert kw["model_id"] == "deepseek-v4-flash:0731"
            return "[image: shot.png: a blue square]", "text"

        monkeypatch.setattr(vision_mod, "redirect_image_message", fake_redirect)

        # Override the config the handle reads.
        class _Agent:
            image_redirect = "subagent"
            image_input_mode = "auto"
            text_only_models = ["ol/deepseek-v4-flash:0731"]
            vision_providers = []
            vision_fallback_model = "kimi-k2.6"
            provider = "opencode"
            provider_base_url = "https://ollama.com/v1"
            provider_api_key = "k"
            sandbox = "off"

        class _Cfg:
            agent = _Agent()

        class _FakeKiroCrewConfig:
            @staticmethod
            def load():
                return _Cfg()

        monkeypatch.setattr(
            "kiro_crew.config.loader.KiroCrewConfig", _FakeKiroCrewConfig, raising=False
        )

        rt = self._make_runtime()
        handle = AcpSessionHandle("sA", asyncio.Queue(), rt)
        handle._model = "deepseek-v4-flash:0731"

        async def drain():
            async for _ in handle.prompt(
                f"what is {img}?", timeout=5.0
            ):
                pass

        task = asyncio.ensure_future(drain())
        await asyncio.sleep(0.05)
        await asyncio.wait_for(task, timeout=5.0)

        assert rt.sent, "prompt() never sent a request"
        prompt = rt.sent[0]["params"]["prompt"]
        text = prompt[0]["text"] if prompt else ""
        assert "[image: shot.png: a blue square]" in text
        # No image block (allow_image gated to text-mode=False -> runtime
        # supports_image_prompt True but image_mode text -> False)
        assert not any(p.get("type") == "image" for p in prompt)
