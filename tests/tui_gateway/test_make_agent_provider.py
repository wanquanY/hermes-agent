"""Regression test for #11884: _make_agent must resolve runtime provider.

Without resolve_runtime_provider(), bare-slug models in config
(e.g. ``claude-opus-4-6`` with ``model.provider: anthropic``) leave
provider/base_url/api_key empty in AIAgent, causing HTTP 404.
"""

import os
from unittest.mock import MagicMock, patch


def test_make_agent_passes_resolved_provider():
    """_make_agent forwards provider/base_url/api_key/api_mode from
    resolve_runtime_provider to AIAgent."""

    fake_runtime = {
        "provider": "anthropic",
        "base_url": "https://api.anthropic.com",
        "api_key": "sk-test-key",
        "api_mode": "anthropic_messages",
        "command": None,
        "args": None,
        "credential_pool": None,
    }

    fake_cfg = {
        "model": {"default": "claude-opus-4-6", "provider": "anthropic"},
        "agent": {"system_prompt": "test"},
    }

    with (
        patch("tui_gateway.server._load_cfg", return_value=fake_cfg),
        patch("tui_gateway.server._get_db", return_value=MagicMock()),
        patch("tui_gateway.server._load_tool_progress_mode", return_value="compact"),
        patch("tui_gateway.server._load_reasoning_config", return_value=None),
        patch("tui_gateway.server._load_service_tier", return_value=None),
        patch("tui_gateway.server._load_enabled_toolsets", return_value=None),
        patch(
            "hermes_cli.runtime_provider.resolve_runtime_provider",
            return_value=fake_runtime,
        ) as mock_resolve,
        patch("run_agent.AIAgent") as mock_agent,
    ):

        from tui_gateway.server import _make_agent

        _make_agent("sid-1", "key-1")

        # target_model comes from _resolve_startup_runtime() which reads
        # _load_cfg().  Due to module-level caching in tui_gateway.server,
        # the patched config may not take effect when the module was already
        # imported by an earlier test.  Assert the stable part of the call.
        mock_resolve.assert_called_once()
        assert mock_resolve.call_args.kwargs.get("requested") is None

        call_kwargs = mock_agent.call_args
        assert call_kwargs.kwargs["provider"] == "anthropic"
        assert call_kwargs.kwargs["base_url"] == "https://api.anthropic.com"
        assert call_kwargs.kwargs["api_key"] == "sk-test-key"
        assert call_kwargs.kwargs["api_mode"] == "anthropic_messages"


def test_make_agent_remembers_requested_runtime_provider():
    fake_runtime = {
        "provider": "custom",
        "base_url": "http://127.0.0.1:8011/api/v1/llm-proxy/v1",
        "api_key": "token-a",
        "api_mode": "chat_completions",
        "requested_provider": "dovie-cloud",
        "command": None,
        "args": None,
        "credential_pool": None,
    }
    fake_cfg = {
        "model": {"default": "gpt-5.5", "provider": "dovie-cloud"},
        "agent": {"system_prompt": ""},
    }
    fake_agent = MagicMock()

    with (
        patch("tui_gateway.server._load_cfg", return_value=fake_cfg),
        patch("tui_gateway.server._get_db", return_value=MagicMock()),
        patch("tui_gateway.server._load_tool_progress_mode", return_value="compact"),
        patch("tui_gateway.server._load_reasoning_config", return_value=None),
        patch("tui_gateway.server._load_service_tier", return_value=None),
        patch("tui_gateway.server._load_enabled_toolsets", return_value=None),
        patch("hermes_cli.runtime_provider.resolve_runtime_provider", return_value=fake_runtime),
        patch("run_agent.AIAgent", return_value=fake_agent),
    ):
        from tui_gateway.server import _make_agent

        agent = _make_agent("sid-dovie", "key-dovie")

    assert agent._gateway_runtime_requested_provider == "dovie-cloud"


def test_make_agent_restores_persisted_session_toolsets():
    fake_runtime = {
        "provider": None,
        "base_url": None,
        "api_key": None,
        "api_mode": None,
        "command": None,
        "args": None,
        "credential_pool": None,
    }
    fake_cfg = {"agent": {"system_prompt": ""}}

    with (
        patch("tui_gateway.server._load_cfg", return_value=fake_cfg),
        patch("tui_gateway.server._get_db", return_value=None),
        patch("tui_gateway.server._load_tool_progress_mode", return_value="off"),
        patch("tui_gateway.server._load_reasoning_config", return_value=None),
        patch("tui_gateway.server._load_service_tier", return_value=None),
        patch("tui_gateway.server._load_enabled_toolsets", return_value=["memory"]),
        patch("tui_gateway.server._load_disabled_toolsets", return_value=None),
        patch(
            "tui_gateway.services.toolset_scope.load_session_toolset_overrides",
            return_value={
                "enabled_toolsets": ["memory", "dovie"],
                "disabled_toolsets": ["delegation"],
            },
        ),
        patch("hermes_cli.runtime_provider.resolve_runtime_provider", return_value=fake_runtime),
        patch("run_agent.AIAgent") as mock_agent,
    ):
        from tui_gateway.server import _make_agent

        _make_agent("sid-restored", "stored-design", session_id="stored-design")

    assert mock_agent.call_args.kwargs["enabled_toolsets"] == ["memory", "dovie"]
    assert mock_agent.call_args.kwargs["disabled_toolsets"] == ["delegation"]


def test_make_agent_forwards_descriptor_context_window_before_agent_init():
    """Dovie catalog metadata must bypass remote /models discovery at init."""

    fake_runtime = {
        "provider": "custom",
        "base_url": "https://api.doviemate.com/api/v1/llm-proxy/v1",
        "api_key": "token-a",
        "api_mode": "chat_completions",
        "command": None,
        "args": None,
        "credential_pool": None,
    }
    fake_cfg = {"agent": {"system_prompt": ""}}

    with (
        patch("tui_gateway.server._load_cfg", return_value=fake_cfg),
        patch("tui_gateway.server._get_db", return_value=None),
        patch("tui_gateway.server._load_tool_progress_mode", return_value="off"),
        patch("tui_gateway.server._load_reasoning_config", return_value=None),
        patch("tui_gateway.server._load_service_tier", return_value=None),
        patch("tui_gateway.server._load_enabled_toolsets", return_value=None),
        patch("hermes_cli.runtime_provider.resolve_runtime_provider", return_value=fake_runtime),
        patch("run_agent.AIAgent") as mock_agent,
    ):
        from tui_gateway import server

        server._sessions["sid-context-window"] = {
            "model_descriptor": {
                "id": "deepseek-v4-pro",
                "context_window": 200_000,
            },
        }
        try:
            server._make_agent(
                "sid-context-window",
                "stored-context-window",
                model_override={
                    "model": "deepseek-v4-pro",
                    "model_explicit": True,
                },
            )
        finally:
            server._sessions.pop("sid-context-window", None)

    assert mock_agent.call_args.kwargs["model_context_window"] == 200_000


def test_ensure_agent_runtime_current_rebinds_stale_session_credentials():
    from tui_gateway.services.runtime_credentials import ensure_agent_runtime_current

    agent = MagicMock()
    agent.model = "gpt-5.5"
    agent.provider = "custom"
    agent.base_url = "http://127.0.0.1:8011/api/v1/llm-proxy/v1"
    agent.api_key = "token-a"
    agent.api_mode = "chat_completions"
    agent._gateway_runtime_requested_provider = "dovie-cloud"
    session = {"agent": agent}
    fake_runtime = {
        "provider": "custom",
        "base_url": "http://127.0.0.1:8011/api/v1/llm-proxy/v1",
        "api_key": "token-b",
        "api_mode": "chat_completions",
        "requested_provider": "dovie-cloud",
    }

    with (
        patch("hermes_cli.runtime_provider.resolve_runtime_provider", return_value=fake_runtime) as mock_resolve,
    ):
        changed = ensure_agent_runtime_current(
            sid="sid-dovie",
            session=session,
            resolve_model=lambda: "gpt-5.5",
            emit_session_info=lambda *_args: None,
        )

    assert changed is True
    mock_resolve.assert_called_once_with(
        requested="dovie-cloud",
        target_model="gpt-5.5",
    )
    agent.switch_model.assert_called_once_with(
        new_model="gpt-5.5",
        new_provider="custom",
        api_key="token-b",
        base_url="http://127.0.0.1:8011/api/v1/llm-proxy/v1",
        api_mode="chat_completions",
    )


def test_ensure_agent_runtime_current_keeps_current_session_credentials():
    from tui_gateway.services.runtime_credentials import ensure_agent_runtime_current

    agent = MagicMock()
    agent.model = "gpt-5.5"
    agent.provider = "custom"
    agent.base_url = "http://127.0.0.1:8011/api/v1/llm-proxy/v1"
    agent.api_key = "token-a"
    agent.api_mode = "chat_completions"
    agent._gateway_runtime_requested_provider = "dovie-cloud"
    session = {"agent": agent}
    fake_runtime = {
        "provider": "custom",
        "base_url": "http://127.0.0.1:8011/api/v1/llm-proxy/v1",
        "api_key": "token-a",
        "api_mode": "chat_completions",
        "requested_provider": "dovie-cloud",
    }

    with patch("hermes_cli.runtime_provider.resolve_runtime_provider", return_value=fake_runtime):
        changed = ensure_agent_runtime_current(
            sid="sid-dovie",
            session=session,
            resolve_model=lambda: "gpt-5.5",
            emit_session_info=lambda *_args: None,
        )

    assert changed is False
    agent.switch_model.assert_not_called()


def test_make_agent_ignores_display_personality_without_system_prompt():
    """The TUI matches the classic CLI: personality only becomes active once
    it has been saved to agent.system_prompt."""

    fake_runtime = {
        "provider": "openrouter",
        "base_url": "https://api.synthetic.new/v1",
        "api_key": "sk-test",
        "api_mode": "chat_completions",
        "command": None,
        "args": None,
        "credential_pool": None,
    }
    fake_cfg = {
        "agent": {
            "system_prompt": "",
            "personalities": {"kawaii": "sparkle system prompt"},
        },
        "display": {"personality": "kawaii"},
        "model": {"default": "glm-5"},
    }

    with (
        patch("tui_gateway.server._load_cfg", return_value=fake_cfg),
        patch("tui_gateway.server._get_db", return_value=MagicMock()),
        patch(
            "hermes_cli.runtime_provider.resolve_runtime_provider",
            return_value=fake_runtime,
        ),
        patch("run_agent.AIAgent") as mock_agent,
    ):
        from tui_gateway.server import _make_agent

        _make_agent("sid-default-personality", "key-default-personality")

        assert mock_agent.call_args.kwargs["ephemeral_system_prompt"] is None


def test_make_agent_honors_tui_launch_env_flags():
    fake_runtime = {
        "provider": "openrouter",
        "base_url": "https://api.synthetic.new/v1",
        "api_key": "sk-test",
        "api_mode": "chat_completions",
        "command": None,
        "args": None,
        "credential_pool": None,
    }
    fake_cfg = {"agent": {"system_prompt": ""}, "model": {"default": "glm-5"}}

    with (
        patch.dict(
            os.environ,
            {
                "HERMES_TUI_MAX_TURNS": "7",
                "HERMES_TUI_CHECKPOINTS": "1",
                "HERMES_TUI_PASS_SESSION_ID": "1",
                "HERMES_IGNORE_RULES": "1",
            },
        ),
        patch("tui_gateway.server._load_cfg", return_value=fake_cfg),
        patch("tui_gateway.server._get_db", return_value=MagicMock()),
        patch(
            "hermes_cli.runtime_provider.resolve_runtime_provider",
            return_value=fake_runtime,
        ),
        patch("run_agent.AIAgent") as mock_agent,
    ):
        from tui_gateway.server import _make_agent

        _make_agent("sid-env", "key-env")

        kwargs = mock_agent.call_args.kwargs
        assert kwargs["max_iterations"] == 7
        assert kwargs["checkpoints_enabled"] is True
        assert kwargs["pass_session_id"] is True
        assert kwargs["skip_context_files"] is True
        assert kwargs["skip_memory"] is True


def test_make_agent_team_leader_context_inherits_profile_identity_loading():
    fake_runtime = {
        "provider": "openrouter",
        "base_url": "https://api.synthetic.new/v1",
        "api_key": "sk-test",
        "api_mode": "chat_completions",
        "command": None,
        "args": None,
        "credential_pool": None,
    }
    fake_cfg = {"agent": {"system_prompt": ""}, "model": {"default": "glm-5"}}

    with (
        patch.dict(os.environ, {"HERMES_IGNORE_RULES": "0"}, clear=False),
        patch("tui_gateway.server._load_cfg", return_value=fake_cfg),
        patch("tui_gateway.server._get_db", return_value=MagicMock()),
        patch(
            "hermes_cli.runtime_provider.resolve_runtime_provider",
            return_value=fake_runtime,
        ),
        patch("run_agent.AIAgent") as mock_agent,
    ):
        from tui_gateway.server import _make_agent

        _make_agent(
            "sid-team-leader",
            "team-session-1",
            agent_context_mode="team_leader",
        )

        kwargs = mock_agent.call_args.kwargs
        assert kwargs["skip_context_files"] is False
        assert kwargs["skip_memory"] is False
        assert kwargs["load_soul_identity"] is True


def test_probe_config_health_flags_null_sections():
    """Bare YAML keys (`agent:` with no value) parse as None and silently
    drop nested settings; probe must surface them so users can fix."""
    from tui_gateway.server import _probe_config_health

    assert _probe_config_health({"agent": {"x": 1}}) == ""
    assert _probe_config_health({}) == ""

    msg = _probe_config_health({"agent": None, "display": None, "model": {}})
    assert "agent" in msg and "display" in msg
    assert "model" not in msg


def test_probe_config_health_flags_null_personalities_with_active_personality():
    from tui_gateway.server import _probe_config_health

    msg = _probe_config_health(
        {
            "agent": {"personalities": None},
            "display": {"personality": "kawaii"},
            "model": {},
        }
    )
    assert "display.personality" in msg
    assert "agent.personalities" in msg


def test_make_agent_tolerates_null_config_sections():
    """Bare `agent:` / `display:` keys in ~/.hermes/config.yaml parse as
    None. cfg.get("agent", {}) returns None (default only fires on missing
    key), so downstream .get() chains must be guarded. Reported via Twitter
    against the new TUI."""

    fake_runtime = {
        "provider": "openrouter",
        "base_url": "https://api.synthetic.new/v1",
        "api_key": "sk-test",
        "api_mode": "chat_completions",
        "command": None,
        "args": None,
        "credential_pool": None,
    }
    null_cfg = {"agent": None, "display": None, "model": {"default": "glm-5"}}

    with (
        patch("tui_gateway.server._load_cfg", return_value=null_cfg),
        patch("tui_gateway.server._get_db", return_value=MagicMock()),
        patch(
            "hermes_cli.runtime_provider.resolve_runtime_provider",
            return_value=fake_runtime,
        ),
        patch("run_agent.AIAgent") as mock_agent,
    ):

        from tui_gateway.server import _make_agent

        _make_agent("sid-null", "key-null")

        assert mock_agent.called


def test_make_agent_tolerates_null_personalities_with_active_personality():
    fake_runtime = {
        "provider": "openrouter",
        "base_url": "https://api.synthetic.new/v1",
        "api_key": "sk-test",
        "api_mode": "chat_completions",
        "command": None,
        "args": None,
        "credential_pool": None,
    }
    cfg = {
        "agent": {"personalities": None},
        "display": {"personality": "kawaii"},
        "model": {"default": "glm-5"},
    }

    with (
        patch("tui_gateway.server._load_cfg", return_value=cfg),
        patch("tui_gateway.server._get_db", return_value=MagicMock()),
        patch("cli.load_cli_config", return_value={"agent": {"personalities": None}}),
        patch(
            "hermes_cli.runtime_provider.resolve_runtime_provider",
            return_value=fake_runtime,
        ),
        patch("run_agent.AIAgent") as mock_agent,
    ):
        from tui_gateway.server import _make_agent

        _make_agent("sid-null-personality", "key-null-personality")

        assert mock_agent.called
        assert mock_agent.call_args.kwargs["ephemeral_system_prompt"] is None


def test_make_agent_honors_per_session_model_override():
    """Regression for cross-session model contamination: a per-session
    ``model_override`` (set by an in-session /model switch) must drive the
    rebuilt agent's model/provider/base_url, NOT global config — and without
    reading process-global env vars that a sibling session may have changed.
    """

    # resolve_runtime_provider echoes the requested provider so we can prove
    # the override's provider (not the global default) was passed through.
    def echo_runtime(requested=None, target_model=None):
        return {
            "provider": requested or "GLOBAL_DEFAULT",
            "base_url": "global-url",
            "api_key": "global-key",
            "api_mode": "chat_completions",
            "command": None,
            "args": None,
            "credential_pool": None,
        }

    fake_cfg = {
        "agent": {"system_prompt": ""},
        "model": {"default": "global/model", "provider": "globalprov"},
    }

    override = {
        "model": "zai/glm-5.1",
        "provider": "zai",
        "base_url": "https://api.z.ai/v1",
        "api_key": "sk-glm",
        "api_mode": "chat_completions",
    }

    with (
        # Ensure no leaked env biases _resolve_startup_runtime (it must not even
        # be consulted when an override is present).
        patch.dict(os.environ, {}, clear=False),
        patch("tui_gateway.server._load_cfg", return_value=fake_cfg),
        patch("tui_gateway.server._get_db", return_value=MagicMock()),
        patch("tui_gateway.server._load_reasoning_config", return_value=None),
        patch("tui_gateway.server._load_service_tier", return_value=None),
        patch("tui_gateway.server._load_enabled_toolsets", return_value=None),
        patch(
            "hermes_cli.runtime_provider.resolve_runtime_provider",
            side_effect=echo_runtime,
        ),
        patch("run_agent.AIAgent") as mock_agent,
    ):
        for var in (
            "HERMES_MODEL",
            "HERMES_INFERENCE_MODEL",
            "HERMES_TUI_PROVIDER",
            "HERMES_INFERENCE_PROVIDER",
        ):
            os.environ.pop(var, None)

        from tui_gateway.server import _make_agent

        _make_agent(
            "sid-override", "key-override", model_override=override
        )

        kwargs = mock_agent.call_args.kwargs
        assert kwargs["model"] == "zai/glm-5.1"
        assert kwargs["provider"] == "zai"
        # Concrete credentials from the switch survive the rebuild.
        assert kwargs["base_url"] == "https://api.z.ai/v1"
        assert kwargs["api_key"] == "sk-glm"


def test_make_agent_honors_codex_runtime_override():
    fake_runtime = {
        "provider": "openai-codex",
        "base_url": "",
        "api_key": "",
        "api_mode": "codex_app_server",
        "command": None,
        "args": None,
        "credential_pool": None,
        "codex_home": "/tmp/dovie/codex-home",
    }
    fake_cfg = {
        "agent": {"system_prompt": ""},
        "model": {"default": "gpt-5.5", "provider": "anthropic"},
    }
    override = {
        "model": "gpt-5.5-codex",
        "provider": "openai-codex",
        "runtime_executor": "codex_app_server",
        "codex_home": "/tmp/dovie/codex-home",
    }

    with (
        patch("tui_gateway.server._load_cfg", return_value=fake_cfg),
        patch("tui_gateway.server._get_db", return_value=MagicMock()),
        patch("tui_gateway.server._load_tool_progress_mode", return_value="compact"),
        patch("tui_gateway.server._load_reasoning_config", return_value=None),
        patch("tui_gateway.server._load_service_tier", return_value=None),
        patch("tui_gateway.server._load_enabled_toolsets", return_value=None),
        patch("hermes_cli.runtime_provider.resolve_runtime_provider", return_value=fake_runtime) as mock_resolve,
        patch("run_agent.AIAgent") as mock_agent,
    ):
        from tui_gateway.server import _make_agent

        agent = _make_agent("sid-codex", "key-codex", model_override=override)

    mock_resolve.assert_called_once_with(
        requested="openai-codex",
        target_model="gpt-5.5-codex",
        runtime_executor="codex_app_server",
        codex_home="/tmp/dovie/codex-home",
    )
    kwargs = mock_agent.call_args.kwargs
    assert kwargs["provider"] == "openai-codex"
    assert kwargs["api_mode"] == "codex_app_server"
    assert agent.codex_home == "/tmp/dovie/codex-home"


def test_make_agent_carries_codex_extra_env_from_override():
    """Dovie ships the platform runtime token via `codex_extra_env`; the
    agent must remember it so `run_codex_app_server_turn` merges it into
    the codex subprocess spawn env.
    """
    fake_runtime = {
        "provider": "openai-codex",
        "base_url": "",
        "api_key": "",
        "api_mode": "codex_app_server",
        "command": None,
        "args": None,
        "credential_pool": None,
        "codex_home": "/tmp/dovie/codex-home",
    }
    fake_cfg = {
        "agent": {"system_prompt": ""},
        "model": {"default": "gpt-5.5", "provider": "anthropic"},
    }
    override = {
        "model": "gpt-5.5-codex",
        "provider": "openai-codex",
        "runtime_executor": "codex_app_server",
        "codex_home": "/tmp/dovie/codex-home",
        "codex_extra_env": {"DOXIE_PLATFORM_API_KEY": "rt-token-abc"},
    }

    with (
        patch("tui_gateway.server._load_cfg", return_value=fake_cfg),
        patch("tui_gateway.server._get_db", return_value=MagicMock()),
        patch("tui_gateway.server._load_tool_progress_mode", return_value="compact"),
        patch("tui_gateway.server._load_reasoning_config", return_value=None),
        patch("tui_gateway.server._load_service_tier", return_value=None),
        patch("tui_gateway.server._load_enabled_toolsets", return_value=None),
        patch("hermes_cli.runtime_provider.resolve_runtime_provider", return_value=fake_runtime),
        patch("run_agent.AIAgent") as mock_agent,
    ):
        from tui_gateway.server import _make_agent

        agent = _make_agent("sid-extra-env", "key-extra-env", model_override=override)

    assert agent.codex_extra_env == {"DOXIE_PLATFORM_API_KEY": "rt-token-abc"}
    # Sanity: still forwards codex_home + api_mode
    assert agent.codex_home == "/tmp/dovie/codex-home"
    assert mock_agent.call_args.kwargs["api_mode"] == "codex_app_server"


def test_make_agent_codex_extra_env_absent_when_byo():
    """BYO mode: dovie sends nothing here. Agent should not carry an env
    bag, so the codex subprocess falls back to its own auth.json.
    """
    fake_runtime = {
        "provider": "openai-codex",
        "base_url": "",
        "api_key": "",
        "api_mode": "codex_app_server",
        "command": None,
        "args": None,
        "credential_pool": None,
        "codex_home": "/tmp/dovie/byo-home",
    }
    fake_cfg = {
        "agent": {"system_prompt": ""},
        "model": {"default": "gpt-5.5", "provider": "anthropic"},
    }
    override = {
        "model": "gpt-5.5-codex",
        "provider": "openai-codex",
        "runtime_executor": "codex_app_server",
        "codex_home": "/tmp/dovie/byo-home",
        # no codex_extra_env
    }

    from types import SimpleNamespace

    with (
        patch("tui_gateway.server._load_cfg", return_value=fake_cfg),
        patch("tui_gateway.server._get_db", return_value=MagicMock()),
        patch("tui_gateway.server._load_tool_progress_mode", return_value="compact"),
        patch("tui_gateway.server._load_reasoning_config", return_value=None),
        patch("tui_gateway.server._load_service_tier", return_value=None),
        patch("tui_gateway.server._load_enabled_toolsets", return_value=None),
        patch("hermes_cli.runtime_provider.resolve_runtime_provider", return_value=fake_runtime),
        # SimpleNamespace makes hasattr honest (MagicMock always returns True)
        patch("run_agent.AIAgent", return_value=SimpleNamespace()),
    ):
        from tui_gateway.server import _make_agent

        agent = _make_agent("sid-byo", "key-byo", model_override=override)

    assert not hasattr(agent, "codex_extra_env")


def test_make_agent_codex_runtime_profile_defaults_provider():
    fake_runtime = {
        "provider": "openai-codex",
        "base_url": "",
        "api_key": "",
        "api_mode": "codex_app_server",
        "command": None,
        "args": None,
        "credential_pool": None,
        "codex_home": "/tmp/dovie/profile-codex",
    }
    fake_cfg = {
        "agent": {"system_prompt": ""},
        "model": {"default": "claude-opus-4-6", "provider": "anthropic"},
    }
    profile_context = {
        "runtime_executor": "codex_app_server",
        "codex_home": "/tmp/dovie/profile-codex",
    }

    with (
        patch("tui_gateway.server._load_cfg", return_value=fake_cfg),
        patch("tui_gateway.server._get_db", return_value=MagicMock()),
        patch("tui_gateway.server._load_tool_progress_mode", return_value="compact"),
        patch("tui_gateway.server._load_reasoning_config", return_value=None),
        patch("tui_gateway.server._load_service_tier", return_value=None),
        patch("tui_gateway.server._load_enabled_toolsets", return_value=None),
        patch("hermes_cli.runtime_provider.resolve_runtime_provider", return_value=fake_runtime) as mock_resolve,
        patch("run_agent.AIAgent") as mock_agent,
    ):
        from tui_gateway.server import _make_agent

        _make_agent("sid-codex-profile", "key-codex-profile", profile_context=profile_context)

    mock_resolve.assert_called_once()
    assert mock_resolve.call_args.kwargs["requested"] == "openai-codex"
    assert mock_resolve.call_args.kwargs["runtime_executor"] == "codex_app_server"
    assert mock_agent.call_args.kwargs["provider"] == "openai-codex"


def test_make_agent_hermes_executor_keeps_configured_cloud_provider():
    """The ordinary Dovie executor must not be mistaken for Codex.

    Dovie profile context always carries ``runtime_executor=hermes`` for the
    native Hermes runtime.  Treating every non-empty executor as Codex routes a
    managed cloud model into the Codex OAuth adapter and fails before the first
    provider request when the user has no local Codex credentials.
    """
    fake_runtime = {
        "provider": "custom",
        "base_url": "https://api.doviemate.com/api/v1/llm-proxy/v1",
        "api_key": "runtime-token",
        "api_mode": "chat_completions",
        "command": None,
        "args": None,
        "credential_pool": None,
    }
    fake_cfg = {
        "agent": {"system_prompt": ""},
        "model": {"default": "gpt-5.5", "provider": "dovie-cloud"},
    }
    override = {
        "model": "deepseek-v4-pro",
        "runtime_executor": "hermes",
        "model_explicit": True,
    }

    with (
        patch("tui_gateway.server._load_cfg", return_value=fake_cfg),
        patch("tui_gateway.server._get_db", return_value=MagicMock()),
        patch("tui_gateway.server._load_tool_progress_mode", return_value="compact"),
        patch("tui_gateway.server._load_reasoning_config", return_value=None),
        patch("tui_gateway.server._load_service_tier", return_value=None),
        patch("tui_gateway.server._load_enabled_toolsets", return_value=None),
        patch(
            "hermes_cli.runtime_provider.resolve_runtime_provider",
            return_value=fake_runtime,
        ) as mock_resolve,
        patch("run_agent.AIAgent") as mock_agent,
    ):
        from tui_gateway.server import _make_agent

        _make_agent("sid-hermes", "key-hermes", model_override=override)

    mock_resolve.assert_called_once_with(
        requested=None,
        target_model="deepseek-v4-pro",
        runtime_executor="hermes",
    )
    kwargs = mock_agent.call_args.kwargs
    assert kwargs["model"] == "deepseek-v4-pro"
    assert kwargs["provider"] == "custom"
    assert kwargs["api_mode"] == "chat_completions"


def test_member_chat_profile_context_builds_byo_codex_agent_without_turn_model():
    from types import SimpleNamespace

    from agent.codex_runtime import codex_app_server_turn_model
    from tui_gateway import server

    fake_runtime = {
        "provider": "openai-codex",
        "base_url": "",
        "api_key": "",
        "api_mode": "codex_app_server",
        "command": None,
        "args": None,
        "credential_pool": None,
        "codex_home": "/tmp/dovie/member-codex-home",
    }
    fake_cfg = {
        "agent": {"system_prompt": ""},
        "model": {"default": "gpt-5.5", "provider": "dovie-cloud"},
    }
    params = {
        "conversation_session_id": "team-session-1",
        "session_id": "team-session-1",
        "agent_context_mode": "member_chat",
        "runtime_scope_key": "member-chat:conv-1:codex-member",
        "run_context_json": (
            '{"conversation_session_id":"team-session-1",'
            '"participant_id":"member:codex-member",'
            '"activity_id":"act-member_chat:team-session-1:codex-member",'
            '"activity_kind":"member_chat",'
            '"execution_scope_key":"member-chat:conv-1:codex-member",'
            '"control_home":"/tmp/dovie/control",'
            '"execution_home":"/tmp/dovie/member"}'
        ),
        "dovie_profile": {
            "id": "agent-codex-member",
            "runtimeExecutor": "codex",
            "runtime_executor": "codex",
            "codexHome": "/tmp/dovie/member-codex-home",
            "codex_home": "/tmp/dovie/member-codex-home",
            "codexAccountMode": "byo",
            "codex_account_mode": "byo",
            "codexExtraEnv": {
                "CODEX_TRACE": "1",
                "DROP_ME": None,
            },
            "codex_extra_env": {
                "CODEX_TRACE": "1",
                "DROP_ME": None,
            },
        },
    }
    profile_context = server._profile_context_for_params(params)

    with (
        patch("tui_gateway.server._load_cfg", return_value=fake_cfg),
        patch("tui_gateway.server._get_db", return_value=MagicMock()),
        patch("tui_gateway.server._persisted_session_runtime", return_value=("", None)),
        patch("tui_gateway.server._resolve_startup_runtime", return_value=("gpt-5.5", "dovie-cloud")),
        patch("tui_gateway.server._load_tool_progress_mode", return_value="compact"),
        patch("tui_gateway.server._load_reasoning_config", return_value=None),
        patch("tui_gateway.server._load_service_tier", return_value=None),
        patch("tui_gateway.server._load_enabled_toolsets", return_value=None),
        patch("hermes_cli.runtime_provider.resolve_runtime_provider", return_value=fake_runtime) as mock_resolve,
        patch("run_agent.AIAgent", side_effect=lambda **kwargs: SimpleNamespace(**kwargs)),
    ):
        agent = server._make_agent(
            "sid-member-codex",
            "team-session-1",
            session_id="team-session-1",
            agent_context_mode="member_chat",
            profile_context=profile_context,
        )

    mock_resolve.assert_called_once_with(
        requested="openai-codex",
        target_model="gpt-5.5",
        runtime_executor="codex",
        codex_home="/tmp/dovie/member-codex-home",
    )
    assert agent.api_mode == "codex_app_server"
    assert agent.codex_home == "/tmp/dovie/member-codex-home"
    assert agent.codex_account_mode == "byo"
    assert agent.codex_extra_env == {"CODEX_TRACE": "1"}
    assert codex_app_server_turn_model(agent) == ""


def test_make_agent_rejects_forced_codex_runtime_without_codex_home():
    """Guard: refusing to fall back to ~/.codex is a load-bearing invariant.

    If the desktop drives runtime_executor=codex_app_server but forgets to
    pass codex_home, the codex CLI would happily spawn against the user's
    personal ~/.codex — bleeding platform employee state into the user's
    ChatGPT account. _make_agent must refuse loudly.
    """
    import pytest

    fake_cfg = {
        "agent": {"system_prompt": ""},
        "model": {"default": "gpt-5.5", "provider": "openai"},
    }
    profile_context = {"runtime_executor": "codex_app_server"}  # no codex_home

    with (
        patch("tui_gateway.server._load_cfg", return_value=fake_cfg),
        patch("tui_gateway.server._get_db", return_value=MagicMock()),
        patch("tui_gateway.server._load_tool_progress_mode", return_value="compact"),
        patch("tui_gateway.server._load_reasoning_config", return_value=None),
        patch("tui_gateway.server._load_service_tier", return_value=None),
        patch("tui_gateway.server._load_enabled_toolsets", return_value=None),
        patch("hermes_cli.runtime_provider.resolve_runtime_provider") as mock_resolve,
        patch("run_agent.AIAgent"),
    ):
        from tui_gateway.server import _make_agent

        with pytest.raises(ValueError, match="codex_home"):
            _make_agent("sid-forced-no-home", "key-forced-no-home", profile_context=profile_context)

    mock_resolve.assert_not_called()


def test_apply_model_switch_does_not_leak_process_env():
    """Core fix for cross-session contamination: an in-session /model switch
    must mutate only the target session (record a per-session override + switch
    that session's agent in place) and must NOT write process-global env vars,
    which the single-process desktop backend shares across every live session.
    """
    from tui_gateway import server

    class _FakeResult:
        success = True
        error_message = ""
        warning_message = ""
        new_model = "zai/glm-5.1"
        target_provider = "zai"
        base_url = "https://api.z.ai/v1"
        api_key = "sk-glm"
        api_mode = "chat_completions"

    class _FakeAgent:
        def __init__(self):
            self.model = "minimax/m3"
            self.provider = "minimax"
            self.base_url = ""
            self.api_key = ""

        def switch_model(self, **kw):
            self.model = kw["new_model"]
            self.provider = kw["new_provider"]

    env_keys = (
        "HERMES_MODEL",
        "HERMES_INFERENCE_MODEL",
        "HERMES_TUI_PROVIDER",
        "HERMES_INFERENCE_PROVIDER",
    )

    sess_b = {"agent": _FakeAgent(), "session_key": "k-B", "model_override": None}
    sess_a = {"agent": _FakeAgent(), "session_key": "k-A", "model_override": None}

    with (
        patch("hermes_cli.model_switch.parse_model_flags_detailed",
              return_value=("glm-5.1", None, False, False, True)),
        patch("hermes_cli.model_switch.resolve_persist_behavior",
              return_value=False),
        patch("hermes_cli.model_switch.switch_model", return_value=_FakeResult()),
        patch("tui_gateway.server._emit"),
        patch("tui_gateway.server._restart_slash_worker"),
        patch("tui_gateway.server._session_info", return_value={}),
        patch("tui_gateway.server._persist_model_switch") as mock_persist,
    ):
        before = {k: os.environ.get(k) for k in env_keys}
        result = server._apply_model_switch("sidB", sess_b, "glm-5.1")
        after = {k: os.environ.get(k) for k in env_keys}

    assert result["value"] == "zai/glm-5.1"
    # No process-global env mutation (the contamination vector).
    assert before == after
    # persist_global was False → config untouched.
    mock_persist.assert_not_called()
    # Target session recorded a per-session override.
    assert sess_b["model_override"]["model"] == "zai/glm-5.1"
    assert sess_b["model_override"]["provider"] == "zai"
    # The switched agent mutated in place.
    assert sess_b["agent"].model == "zai/glm-5.1"
    # Sibling session is completely untouched.
    assert sess_a["model_override"] is None
    assert sess_a["agent"].model == "minimax/m3"


def test_apply_model_switch_records_platform_codex_explicit_model():
    from tui_gateway import server

    class _FakeAgent:
        api_mode = "codex_app_server"
        model = "glm-5.2-polluted"
        provider = "openai-codex"
        base_url = "https://should-not-change.invalid"
        api_key = "should-not-change"
        codex_home = "/tmp/dovie/platform-codex"
        codex_account_mode = "platform"
        codex_extra_env = {"DOXIE_PLATFORM_API_KEY": "rt-token"}

    agent = _FakeAgent()
    session = {
        "agent": agent,
        "session_key": "k-platform",
        "model_override": {
            "runtime_executor": "codex_app_server",
            "codex_home": "/tmp/dovie/platform-codex",
            "codex_account_mode": "platform",
            "codex_extra_env": {"DOXIE_PLATFORM_API_KEY": "rt-token"},
        },
    }

    with (
        patch("hermes_cli.model_switch.parse_model_flags_detailed",
              return_value=("glm-5.2", None, False, False, True)),
        patch("hermes_cli.model_switch.resolve_persist_behavior",
              return_value=False),
        patch("hermes_cli.model_switch.switch_model") as mock_switch,
    ):
        result = server._apply_model_switch("sid-platform", session, "glm-5.2")

    assert result["value"] == "glm-5.2"
    assert session["model_override"]["model"] == "glm-5.2"
    assert session["model_override"]["model_explicit"] is True
    assert session["model_override"]["codex_account_mode"] == "platform"
    assert agent.codex_explicit_model == "glm-5.2"
    assert agent.provider == "openai-codex"
    assert agent.base_url == "https://should-not-change.invalid"
    assert agent.api_key == "should-not-change"
    mock_switch.assert_not_called()


def test_apply_model_switch_byo_codex_remains_noop():
    from tui_gateway import server

    class _FakeAgent:
        api_mode = "codex_app_server"
        model = "glm-5.2-polluted"
        provider = "openai-codex"
        base_url = ""
        api_key = ""
        codex_account_mode = "byo"

    session = {
        "agent": _FakeAgent(),
        "session_key": "k-byo",
        "model_override": {
            "runtime_executor": "codex_app_server",
            "codex_home": "/tmp/dovie/byo-codex",
            "codex_account_mode": "byo",
        },
    }

    with (
        patch("hermes_cli.model_switch.parse_model_flags_detailed",
              return_value=("glm-5.2", None, False, False, True)),
        patch("hermes_cli.model_switch.resolve_persist_behavior",
              return_value=False),
        patch("hermes_cli.model_switch.switch_model") as mock_switch,
    ):
        result = server._apply_model_switch("sid-byo", session, "glm-5.2")

    assert result["no_op"] is True
    assert "model" not in session["model_override"]
    assert not hasattr(session["agent"], "codex_explicit_model")
    mock_switch.assert_not_called()
