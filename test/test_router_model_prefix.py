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

    def test_ollama_exposes_only_deepseek_v4_flash_0731(self) -> None:
        wl = AcpClient.router_model_whitelist()
        ol_ids = {m for m in wl if m.startswith("ol/")}
        assert ol_ids == {"ol/deepseek-v4-flash:0731"}
        # the old 9router-spelling ollama entries are gone too
        for old in (
            "ollama/deepseek-v4-flash",
            "ollama/glm-5.2",
            "ollama/kimi-k2.6",
            "ollama/kimi-k2.7-code",
        ):
            assert old not in wl
        for raw in ("glm-5.2", "kimi-k2.6", "kimi-k2.7-code"):
            assert raw not in wl

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
        # vision-capable providers are NOT text-only
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
        from kiro_crew.acp.client import _VISION_FALLBACK_RAW

        # must be the raw commandcode id the proxy serves (image-capable, 1M)
        assert _VISION_FALLBACK_RAW == "xiaomi/mimo-v2.5"
