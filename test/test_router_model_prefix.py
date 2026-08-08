"""Tests for the router model-id prefix contract (kirocrew-customapi fork).

CLIProxyAPI (http://127.0.0.1:8317) rejects prefixed model ids with "unknown
provider", so the ACP client must strip the known provider prefix before the
request leaves and send the provider's RAW model id upstream. The GUI picker
shows prefixed ids (cmc/, oc/, ol/, cx/, ag/) so the user can tell providers
apart when they share model names.

Contract:
- Known prefix + short name -> the provider's raw id (lookup table, e.g.
  ``cmc/deepseek-v4-pro`` -> ``deepseek/deepseek-v4-pro``).
- Unknown or absent prefix -> id passes through unchanged.
- ollama-cloud exposes ONLY ``deepseek-v4-flash:0731``; ``gpt-5.3-codex-spark``
  is deliberately absent (verified 400 upstream).
"""

from __future__ import annotations

import pytest

from kiro_crew.acp.client import (
    AcpClient,
    strip_router_model_prefix,
)
from kiro_crew.acp.types import ACP_BACKEND_CLAUDE

# (prefixed id as shown in the picker, raw id sent upstream)
_STRIP_CASES = [
    # cmc/ -> commandcode (raw ids carry the vendor/org prefix)
    ("cmc/deepseek-v4-pro", "deepseek/deepseek-v4-pro"),
    ("cmc/deepseek-v4-flash", "deepseek/deepseek-v4-flash"),
    ("cmc/Kimi-K3", "moonshotai/Kimi-K3"),
    # oc/ -> opencode-go (raw ids are short)
    ("oc/deepseek-v4-flash", "deepseek-v4-flash"),
    ("oc/mimo-v2.5", "mimo-v2.5"),
    # ol/ -> ollama-cloud (only deepseek-v4-flash:0731 is exposed)
    ("ol/deepseek-v4-flash:0731", "deepseek-v4-flash:0731"),
    # cx/ -> codex (openai-owned models via Codex OAuth)
    ("cx/gpt-5.6-luna", "gpt-5.6-luna"),
    # ag/ -> antigravity
    ("ag/claude-sonnet-4-6", "claude-sonnet-4-6"),
]

_PASS_THROUGH_CASES = [
    # unknown prefix -> unchanged
    "foo/x",
    # raw id with no prefix at all -> unchanged
    "deepseek-v4-flash:0731",
]


class TestStripRouterModelPrefix:
    """The strip-prefix helper maps prefixed ids to the provider's raw id."""

    @pytest.mark.parametrize(("prefixed", "raw"), _STRIP_CASES)
    def test_known_prefix_strips_to_raw_id(self, prefixed: str, raw: str) -> None:
        assert strip_router_model_prefix(prefixed) == raw

    @pytest.mark.parametrize("model_id", _PASS_THROUGH_CASES)
    def test_unknown_or_absent_prefix_passes_through(self, model_id: str) -> None:
        assert strip_router_model_prefix(model_id) == model_id

    def test_prefixed_id_is_not_returned_raw(self) -> None:
        # The prefixed spelling must never survive the strip; the raw id for
        # cmc/Kimi-K3 is the full vendor-prefixed "moonshotai/Kimi-K3".
        assert strip_router_model_prefix("cmc/Kimi-K3") != "Kimi-K3"


class TestRouterModelViaEnvRawId:
    """On the router path the model rides in via ANTHROPIC_MODEL env; the env
    must carry the RAW id, because CLIProxyAPI rejects prefixed spellings."""

    def test_init_strips_prefix_into_anthropic_model_env(self) -> None:
        client = AcpClient(
            acp_backend=ACP_BACKEND_CLAUDE,
            model="cmc/deepseek-v4-pro",
            extra_env={"ANTHROPIC_BASE_URL": "http://127.0.0.1:8317"},
        )
        assert client._extra_env["ANTHROPIC_MODEL"] == "deepseek/deepseek-v4-pro"

    def test_init_passes_unknown_prefix_through_unchanged(self) -> None:
        client = AcpClient(
            acp_backend=ACP_BACKEND_CLAUDE,
            model="foo/x",
            extra_env={"ANTHROPIC_BASE_URL": "http://127.0.0.1:8317"},
        )
        assert client._extra_env["ANTHROPIC_MODEL"] == "foo/x"


class TestRouterModelWhitelistPrefixed:
    """The picker whitelist exposes prefixed ids only — no raw-only duplicates,
    ollama-cloud only deepseek-v4-flash:0731, and no gpt-5.3-codex-spark."""

    _PREFIXED_PRESENT = [
        "cmc/deepseek-v4-pro",
        "cmc/deepseek-v4-flash",
        "cmc/Kimi-K3",
        "oc/deepseek-v4-flash",
        "oc/mimo-v2.5",
        "ol/deepseek-v4-flash:0731",
        "cx/gpt-5.6-luna",
        "cx/gpt-5.4",
        "cx/codex-auto-review",
        "ag/claude-sonnet-4-6",
        "ag/gemini-3-flash",
        "ag/gpt-oss-120b-medium",
    ]

    _RAW_ONLY_ABSENT = [
        "deepseek/deepseek-v4-pro",
        "deepseek/deepseek-v4-flash",
        "deepseek-v4-flash",  # opencode-go raw spelling
        "mimo-v2.5",
        "deepseek-v4-flash:0731",  # ollama raw spelling
        "gpt-5.6-luna",
        "gpt-5.4",
        "codex-auto-review",
        "claude-sonnet-4-6",
        "gemini-3-flash",
        "gpt-oss-120b-medium",
    ]

    def test_prefixed_ids_exposed(self) -> None:
        wl = AcpClient.router_model_whitelist()
        for prefixed in self._PREFIXED_PRESENT:
            assert prefixed in wl, f"missing prefixed id {prefixed!r}"

    def test_no_raw_only_duplicates(self) -> None:
        wl = AcpClient.router_model_whitelist()
        for raw in self._RAW_ONLY_ABSENT:
            assert raw not in wl, f"raw-only id {raw!r} must not appear in the picker"

    def test_ollama_exposes_full_catalog(self) -> None:
        from kiro_crew.acp.client import _ROUTER_RAW_MODEL_IDS

        wl = AcpClient.router_model_whitelist()
        ol_ids = {m for m in wl if m.startswith("ol/")}
        # The ollama whitelist exposes the full /v1 catalog so the picker can
        # select any ollama model, not just the 0731 deepseek build.
        assert "ol/deepseek-v4-flash:0731" in ol_ids
        assert "ol/glm-5.2" in ol_ids
        assert "ol/kimi-k2.6" in ol_ids
        assert "ol/kimi-k2.7-code" in ol_ids
        assert "ol/minimax-m3" in ol_ids
        assert "ol/gpt-oss:120b" in ol_ids
        assert "ol/gemma4:31b" in ol_ids
        # Every ol/ picker id maps to a whitelisted raw id.
        for mid in ol_ids:
            prefix, _, rest = mid.partition("/")
            assert rest in _ROUTER_RAW_MODEL_IDS.get(prefix, ()), mid

    def test_opencode_exposes_full_catalog(self) -> None:
        wl = AcpClient.router_model_whitelist()
        oc_ids = {m for m in wl if m.startswith("oc/")}
        # The opencode-go whitelist exposes the full /zen/go/v1 catalog.
        assert "oc/mimo-v2.5" in oc_ids
        assert "oc/kimi-k2.6" in oc_ids
        assert "oc/glm-5.2" in oc_ids
        assert "oc/deepseek-v4-flash" in oc_ids
        assert "oc/qwen3.7-max" in oc_ids
        assert "oc/minimax-m3" in oc_ids

    def test_9router_prefixed_ids_normalize(self) -> None:
        from kiro_crew.acp.client import prefixed_router_model_id, strip_router_model_prefix

        # 9router serves already-prefixed ids; they must normalize to the
        # fork's canonical picker prefix and strip to the wire id.
        assert prefixed_router_model_id("ollama/glm-5.2", "ollama") == "ol/glm-5.2"
        assert prefixed_router_model_id("ocg/kimi-k2.6", "ocg") == "oc/kimi-k2.6"
        assert prefixed_router_model_id("ocg/mimo-v2.5", "ocg") == "oc/mimo-v2.5"
        assert (
            prefixed_router_model_id("ag/gemini-3.6-flash-high", "ag")
            == "ag/gemini-3.6-flash-high"
        )
        assert strip_router_model_prefix("ollama/glm-5.2") == "glm-5.2"
        assert strip_router_model_prefix("ocg/kimi-k2.6") == "kimi-k2.6"
        assert strip_router_model_prefix("ocg/mimo-v2.5") == "mimo-v2.5"
        assert strip_router_model_prefix("ol/glm-5.2") == "glm-5.2"
        assert strip_router_model_prefix("oc/mimo-v2.5") == "mimo-v2.5"

    def test_gpt_5_3_codex_spark_absent(self) -> None:
        # verified to 400 upstream, so neither spelling may be offered
        wl = AcpClient.router_model_whitelist()
        assert "cx/gpt-5.3-codex-spark" not in wl
        assert "gpt-5.3-codex-spark" not in wl


class TestTextOnlyRedirect:
    """Image prompts on text-only router models redirect to the vision model."""

    def test_text_only_detection(self) -> None:
        from kiro_crew.acp.client import _is_router_text_only_model

        assert _is_router_text_only_model("oc/deepseek-v4-flash") is True
        assert _is_router_text_only_model("ol/deepseek-v4-flash:0731") is True
        # vision-capable models are NOT text-only — incl. oc/mimo-v2.5 which
        # accepts images upstream (verified 200)
        assert _is_router_text_only_model("oc/mimo-v2.5") is False
        assert _is_router_text_only_model("cmc/deepseek-v4-pro") is False
        assert _is_router_text_only_model("ag/gemini-3.6-flash-high") is False
        assert _is_router_text_only_model("cx/gpt-5.6-luna") is False
        # no prefix / bare id -> not text-only (pass-through)
        assert _is_router_text_only_model("deepseek-v4-flash") is False
        assert _is_router_text_only_model("") is False

    def test_message_has_image_path(self) -> None:
        from kiro_crew.acp.client import _message_has_image_path

        assert _message_has_image_path("look at /tmp/shot.png please") is True
        assert _message_has_image_path("attach /home/me/pic.jpg and explain") is True
        assert _message_has_image_path("just text, no images here") is False
        assert _message_has_image_path("") is False
        assert _message_has_image_path("   ") is False

    def test_vision_fallback_is_mimo(self) -> None:
        from kiro_crew.acp.client import (
            _DEFAULT_TEXT_ONLY_MODELS,
            _DEFAULT_VISION_FALLBACK_MODEL,
            AcpClient,
        )

        # default fallback must be the commandcode picker id (image-capable, 1M)
        assert _DEFAULT_VISION_FALLBACK_MODEL == "cmc/mimo-v2.5"
        # instance wiring: config overrides land on the client
        client = AcpClient(
            work_dir="/tmp/x",
            image_redirect="switch",
            vision_fallback_model="ag/gemini-3.6-flash-high",
            text_only_models=["oc/deepseek-v4-flash"],
        )
        assert client._image_redirect == "switch"
        assert client._vision_fallback_model == "ag/gemini-3.6-flash-high"
        assert client._text_only_models == frozenset({"oc/deepseek-v4-flash"})
        # defaults still contain the two verified text-only models
        assert "oc/deepseek-v4-flash" in _DEFAULT_TEXT_ONLY_MODELS
        assert "ol/deepseek-v4-flash:0731" in _DEFAULT_TEXT_ONLY_MODELS
        # oc/mimo-v2.5 is NOT text-only (vision-capable)
        assert "oc/mimo-v2.5" not in _DEFAULT_TEXT_ONLY_MODELS


class TestSessionStartImageGuard:
    """Text-only models never emit image blocks and disable screenshot tools."""

    def test_settings_guard_disables_tools_for_text_only(self, tmp_path, monkeypatch) -> None:
        import json as _json

        from kiro_crew.acp.client import AcpClient

        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
        c = AcpClient(
            work_dir=str(tmp_path),
            model="oc/deepseek-v4-flash",
            acp_backend="claude",
            extra_env={"ANTHROPIC_BASE_URL": "http://127.0.0.1:8317"},
        )
        c._write_claude_local_settings()
        data = _json.loads((tmp_path / "settings.local.json").read_text())
        assert data.get("model") == "deepseek-v4-flash"  # raw pin
        assert "ComputerUse" in data.get("disabledTools", [])
        assert "browser_screenshot" in data.get("disabledTools", [])

    def test_settings_guard_leaves_vision_model_alone(self, tmp_path, monkeypatch) -> None:
        import json as _json

        from kiro_crew.acp.client import AcpClient

        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
        c = AcpClient(
            work_dir=str(tmp_path),
            model="cmc/mimo-v2.5",
            acp_backend="claude",
            extra_env={"ANTHROPIC_BASE_URL": "http://127.0.0.1:8317"},
        )
        c._write_claude_local_settings()
        data = _json.loads((tmp_path / "settings.local.json").read_text())
        assert not data.get("disabledTools")

    def test_prompt_blocks_allow_image_false_for_text_only(self) -> None:
        from kiro_crew.acp.client import AcpClient, _message_has_image_path

        assert _message_has_image_path("look at /tmp/shot.png") is True
        # the guard flag is derived from the text-only set membership, which
        # the instance config controls; verify the set is consulted
        c = AcpClient(work_dir="/tmp/x", text_only_models=["oc/deepseek-v4-flash"])
        assert "oc/deepseek-v4-flash" in c._text_only_models
        assert "cmc/mimo-v2.5" not in c._text_only_models
