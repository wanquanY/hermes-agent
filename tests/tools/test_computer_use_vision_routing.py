"""Unit tests for tools.computer_use.vision_routing.

Cover the small ``should_route_capture_to_aux_vision`` policy helper that
decides whether a captured screenshot from ``computer_use(action='capture')``
should be returned as a multimodal envelope (main model handles vision
natively) or pre-analysed via the ``auxiliary.vision`` pipeline so the
main model only sees text.

The companion end-to-end regression for #24015 lives in
``tests/tools/test_computer_use_capture_routing.py``; this file pins the
unit contract of the helper in isolation so behaviour does not regress
silently if the surrounding ``computer_use`` plumbing is refactored.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from tools.computer_use.backend import CaptureResult
from tools.computer_use.tool import _capture_response


def _doxie_managed_vision_config() -> dict:
    return {
        "auxiliary": {
            "vision": {
                "provider": "doxie-cloud",
                "model": "gpt-5.5",
                "base_url": "http://127.0.0.1:8011/api/v1/llm-proxy/v1",
                "key_env": "DOXIE_LLM_RUNTIME_TOKEN",
                "api_mode": "chat_completions",
            },
        },
    }


# ---------------------------------------------------------------------------
# _explicit_aux_vision_override
# ---------------------------------------------------------------------------

class TestExplicitAuxVisionOverride:
    """Mirror agent.image_routing — config detection must agree across paths."""

    def test_returns_false_for_none_cfg(self):
        from tools.computer_use.vision_routing import _explicit_aux_vision_override
        assert _explicit_aux_vision_override(None) is False

    def test_returns_false_for_non_dict_cfg(self):
        from tools.computer_use.vision_routing import _explicit_aux_vision_override
        assert _explicit_aux_vision_override("not-a-dict") is False
        assert _explicit_aux_vision_override([]) is False

    def test_returns_false_when_auxiliary_block_missing(self):
        from tools.computer_use.vision_routing import _explicit_aux_vision_override
        assert _explicit_aux_vision_override({}) is False
        assert _explicit_aux_vision_override({"model": {"default": "x"}}) is False

    def test_returns_false_when_vision_block_missing(self):
        from tools.computer_use.vision_routing import _explicit_aux_vision_override
        cfg = {"auxiliary": {"compression": {"provider": "openai"}}}
        assert _explicit_aux_vision_override(cfg) is False

    def test_returns_false_for_blank_provider_no_model_no_base_url(self):
        from tools.computer_use.vision_routing import _explicit_aux_vision_override
        cfg = {"auxiliary": {"vision": {"provider": "", "model": "", "base_url": ""}}}
        assert _explicit_aux_vision_override(cfg) is False

    def test_returns_false_for_provider_auto(self):
        from tools.computer_use.vision_routing import _explicit_aux_vision_override
        cfg = {"auxiliary": {"vision": {"provider": "auto"}}}
        assert _explicit_aux_vision_override(cfg) is False

    def test_returns_false_for_provider_AUTO_uppercase(self):
        from tools.computer_use.vision_routing import _explicit_aux_vision_override
        cfg = {"auxiliary": {"vision": {"provider": "  AUTO  "}}}
        assert _explicit_aux_vision_override(cfg) is False

    def test_returns_true_for_explicit_provider(self):
        from tools.computer_use.vision_routing import _explicit_aux_vision_override
        cfg = {"auxiliary": {"vision": {"provider": "openrouter"}}}
        assert _explicit_aux_vision_override(cfg) is True

    def test_returns_true_for_explicit_model_only(self):
        from tools.computer_use.vision_routing import _explicit_aux_vision_override
        cfg = {"auxiliary": {"vision": {"model": "google/gemini-2.5-flash"}}}
        assert _explicit_aux_vision_override(cfg) is True

    def test_returns_true_for_explicit_base_url_only(self):
        from tools.computer_use.vision_routing import _explicit_aux_vision_override
        cfg = {"auxiliary": {"vision": {"base_url": "http://localhost:1234/v1"}}}
        assert _explicit_aux_vision_override(cfg) is True

    def test_returns_true_for_provider_auto_plus_explicit_model(self):
        """``provider: auto`` + an explicit model still counts as override."""
        from tools.computer_use.vision_routing import _explicit_aux_vision_override
        cfg = {
            "auxiliary": {
                "vision": {"provider": "auto", "model": "claude-3-haiku"},
            }
        }
        assert _explicit_aux_vision_override(cfg) is True

    def test_handles_non_dict_vision_block(self):
        from tools.computer_use.vision_routing import _explicit_aux_vision_override
        cfg = {"auxiliary": {"vision": "not-a-dict"}}
        assert _explicit_aux_vision_override(cfg) is False


# ---------------------------------------------------------------------------
# should_route_capture_to_aux_vision
# ---------------------------------------------------------------------------

class TestRouteDecision:
    """End-to-end policy: explicit override > tool-result support > vision caps."""

    def test_explicit_override_routes_to_aux_even_for_vision_main(self):
        """Issue #24015 core repro: explicit aux config must win.

        Even if the main model fully supports vision (Anthropic / Claude),
        an explicit ``auxiliary.vision`` block means the user wants their
        configured backend used. Don't silently bypass it.
        """
        from tools.computer_use import vision_routing

        cfg = {
            "auxiliary": {
                "vision": {
                    "provider": "openrouter",
                    "model": "google/gemini-2.5-flash",
                }
            }
        }
        with patch.object(vision_routing, "_lookup_supports_vision", return_value=True), \
             patch.object(vision_routing,
                          "_provider_accepts_multimodal_tool_result",
                          return_value=True):
            assert vision_routing.should_route_capture_to_aux_vision(
                "anthropic", "claude-opus-4-5", cfg
            ) is True

    def test_runtime_vision_descriptor_overrides_doxie_managed_auxiliary_config(self):
        """Doxie-managed aux config is not a user preference for screenshots."""
        from tools.computer_use import vision_routing

        with patch.object(vision_routing,
                          "_provider_accepts_multimodal_tool_result",
                          return_value=True):
            assert vision_routing.should_route_capture_to_aux_vision(
                "doxie-cloud",
                "gpt-5.5",
                _doxie_managed_vision_config(),
                supports_vision_override=True,
            ) is False

    def test_runtime_non_vision_descriptor_forces_auxiliary_route(self):
        from tools.computer_use import vision_routing

        with patch.object(vision_routing,
                          "_provider_accepts_multimodal_tool_result",
                          return_value=True):
            assert vision_routing.should_route_capture_to_aux_vision(
                "doxie-cloud",
                "text-only",
                {},
                supports_vision_override=False,
            ) is True

    def test_non_vision_main_model_routes_to_aux(self):
        """The reported #24015 scenario: tencent/hy3-preview has no vision."""
        from tools.computer_use import vision_routing

        cfg = {"model": {"default": "tencent/hy3-preview", "provider": "openrouter"}}
        with patch.object(vision_routing, "_lookup_supports_vision", return_value=False), \
             patch.object(vision_routing,
                          "_provider_accepts_multimodal_tool_result",
                          return_value=True):
            assert vision_routing.should_route_capture_to_aux_vision(
                "openrouter", "tencent/hy3-preview", cfg
            ) is True

    def test_vision_main_model_no_override_keeps_multimodal(self):
        """Default path: vision-capable main model + no aux override → native."""
        from tools.computer_use import vision_routing

        with patch.object(vision_routing, "_lookup_supports_vision", return_value=True), \
             patch.object(vision_routing,
                          "_provider_accepts_multimodal_tool_result",
                          return_value=True):
            assert vision_routing.should_route_capture_to_aux_vision(
                "anthropic", "claude-opus-4-5", None
            ) is False

    def test_provider_rejects_multimodal_tool_results_routes_to_aux(self):
        """Some providers' tool-result messages won't carry images at all."""
        from tools.computer_use import vision_routing

        with patch.object(vision_routing, "_lookup_supports_vision", return_value=True), \
             patch.object(vision_routing,
                          "_provider_accepts_multimodal_tool_result",
                          return_value=False):
            assert vision_routing.should_route_capture_to_aux_vision(
                "some-aggregator", "some-vision-model", {}
            ) is True

    def test_unknown_provider_capabilities_fail_closed(self):
        """When tool-result lookup returns None, route to aux (safe default)."""
        from tools.computer_use import vision_routing

        with patch.object(vision_routing, "_lookup_supports_vision", return_value=True), \
             patch.object(vision_routing,
                          "_provider_accepts_multimodal_tool_result",
                          return_value=None):
            assert vision_routing.should_route_capture_to_aux_vision(
                "exotic-provider", "exotic-model", {}
            ) is True

    def test_unknown_vision_capability_fails_closed(self):
        """When models.dev has no entry, prefer aux over a likely 404."""
        from tools.computer_use import vision_routing

        with patch.object(vision_routing, "_lookup_supports_vision", return_value=None), \
             patch.object(vision_routing,
                          "_provider_accepts_multimodal_tool_result",
                          return_value=True):
            assert vision_routing.should_route_capture_to_aux_vision(
                "openrouter", "novel/never-seen-model", {}
            ) is True

    def test_explicit_override_wins_over_unknown_caps(self):
        """Explicit aux config wins regardless of unknown caps elsewhere."""
        from tools.computer_use import vision_routing

        cfg = {"auxiliary": {"vision": {"provider": "openrouter"}}}
        with patch.object(vision_routing, "_lookup_supports_vision", return_value=None), \
             patch.object(vision_routing,
                          "_provider_accepts_multimodal_tool_result",
                          return_value=None):
            assert vision_routing.should_route_capture_to_aux_vision(
                "openrouter", "tencent/hy3-preview", cfg
            ) is True


def test_capture_response_uses_parent_agent_model_descriptor_for_native_screenshot():
    parent_agent = SimpleNamespace(model_descriptor={"vision_enabled": True})
    cap = CaptureResult(
        mode="som",
        width=100,
        height=80,
        png_b64="iVBORw0KGgo=",
        elements=[],
        app="WeChat",
        window_title="chat",
    )

    with (
        patch("agent.auxiliary_client._read_main_provider", return_value="doxie-cloud"),
        patch("agent.auxiliary_client._read_main_model", return_value="gpt-5.5"),
        patch("hermes_cli.config.load_config", return_value=_doxie_managed_vision_config()),
        patch(
            "tools.computer_use.vision_routing._provider_accepts_multimodal_tool_result",
            return_value=True,
        ),
        patch("tools.computer_use.tool._route_capture_through_aux_vision") as route_aux,
    ):
        result = _capture_response(cap, parent_agent=parent_agent)

    assert result["_multimodal"] is True
    assert result["content"][1]["type"] == "image_url"
    route_aux.assert_not_called()


# ---------------------------------------------------------------------------
# Internal lookups — defensive paths
# ---------------------------------------------------------------------------

class TestLookupHelpers:
    def test_lookup_supports_vision_returns_none_for_blank_provider(self):
        from tools.computer_use.vision_routing import _lookup_supports_vision
        assert _lookup_supports_vision("", "claude") is None

    def test_lookup_supports_vision_returns_none_for_blank_model(self):
        from tools.computer_use.vision_routing import _lookup_supports_vision
        assert _lookup_supports_vision("anthropic", "") is None

    def test_lookup_supports_vision_handles_lookup_exception(self):
        """Underlying caps lookup may raise; helper must swallow + return None."""
        from tools.computer_use import vision_routing

        def _boom(_provider, _model):
            raise RuntimeError("models.dev unreachable")

        with patch("agent.models_dev.get_model_capabilities", side_effect=_boom):
            assert vision_routing._lookup_supports_vision("anthropic", "claude") is None

    def test_lookup_supports_vision_returns_none_when_caps_missing(self):
        from tools.computer_use import vision_routing

        with patch("agent.models_dev.get_model_capabilities", return_value=None):
            assert vision_routing._lookup_supports_vision("anthropic", "claude") is None

    def test_provider_accepts_multimodal_tool_result_returns_none_for_blank_provider(self):
        from tools.computer_use.vision_routing import (
            _provider_accepts_multimodal_tool_result,
        )
        assert _provider_accepts_multimodal_tool_result("", "claude") is None


# ---------------------------------------------------------------------------
# Module surface
# ---------------------------------------------------------------------------

class TestModuleSurface:
    """Pin the public surface so dependents stay in lockstep."""

    def test_should_route_capture_to_aux_vision_is_exported(self):
        from tools.computer_use import vision_routing

        assert "should_route_capture_to_aux_vision" in vision_routing.__all__
        assert callable(vision_routing.should_route_capture_to_aux_vision)

    @pytest.mark.parametrize("name", [
        "_explicit_aux_vision_override",
        "_lookup_supports_vision",
        "_provider_accepts_multimodal_tool_result",
    ])
    def test_internal_helpers_are_addressable(self, name):
        """Internal helpers stay importable so tests can monkeypatch them."""
        from tools.computer_use import vision_routing

        assert hasattr(vision_routing, name)
        assert callable(getattr(vision_routing, name))
