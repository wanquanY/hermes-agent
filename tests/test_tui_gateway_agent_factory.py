from unittest.mock import patch

from tui_gateway import server


def _setup_make_agent_mocks(monkeypatch, cfg):
    monkeypatch.setattr(server, "_load_cfg", lambda: cfg)
    monkeypatch.setattr(
        server,
        "_resolve_startup_runtime",
        lambda: ("test-model", None),
    )
    monkeypatch.setattr(
        "hermes_cli.runtime_provider.resolve_runtime_provider",
        lambda requested=None, target_model=None: {
            "provider": None,
            "base_url": None,
            "api_key": None,
            "api_mode": None,
            "command": None,
            "args": None,
            "credential_pool": None,
        },
    )
    monkeypatch.setattr(server, "_load_tool_progress_mode", lambda: "off")
    monkeypatch.setattr(server, "_load_reasoning_config", lambda: None)
    monkeypatch.setattr(server, "_load_service_tier", lambda: None)
    monkeypatch.setattr(server, "_load_enabled_toolsets", lambda: None)
    monkeypatch.setattr(server, "_get_db", lambda: None)
    monkeypatch.setattr(server, "_agent_cbs", lambda _sid: {})


def test_make_agent_reads_nested_max_turns(monkeypatch):
    _setup_make_agent_mocks(monkeypatch, {"agent": {"max_turns": 200}})

    with patch("run_agent.AIAgent") as mock_agent:
        server._make_agent("sid1", "key1")

    assert mock_agent.call_args.kwargs["max_iterations"] == 200


def test_make_agent_nested_max_turns_takes_priority(monkeypatch):
    _setup_make_agent_mocks(
        monkeypatch,
        {"agent": {"max_turns": 500}, "max_turns": 100},
    )

    with patch("run_agent.AIAgent") as mock_agent:
        server._make_agent("sid1", "key1")

    assert mock_agent.call_args.kwargs["max_iterations"] == 500


def test_make_agent_defaults_to_90(monkeypatch):
    _setup_make_agent_mocks(monkeypatch, {})

    with patch("run_agent.AIAgent") as mock_agent:
        server._make_agent("sid1", "key1")

    assert mock_agent.call_args.kwargs["max_iterations"] == 90


def test_make_agent_prefers_session_reasoning_and_tier_over_global(monkeypatch):
    _setup_make_agent_mocks(monkeypatch, {})
    monkeypatch.setattr(
        server,
        "_load_reasoning_config",
        lambda: {"enabled": True, "effort": "low"},
    )
    monkeypatch.setattr(server, "_load_service_tier", lambda: "default")

    with patch("run_agent.AIAgent") as mock_agent:
        server._make_agent(
            "sid1",
            "key1",
            reasoning_config_override={"enabled": True, "effort": "xhigh"},
            service_tier_override="priority",
        )

    assert mock_agent.call_args.kwargs["reasoning_config"] == {
        "enabled": True,
        "effort": "xhigh",
    }
    assert mock_agent.call_args.kwargs["service_tier"] == "priority"


def test_make_agent_handles_null_agent_config(monkeypatch):
    _setup_make_agent_mocks(monkeypatch, {"agent": None, "max_turns": 80})

    with patch("run_agent.AIAgent") as mock_agent:
        server._make_agent("sid1", "key1")

    assert mock_agent.call_args.kwargs["max_iterations"] == 80


def test_make_agent_uses_persisted_session_model(monkeypatch):
    """Runtime workers build from the persisted model and provider."""
    _setup_make_agent_mocks(monkeypatch, {})

    class FakeDB:
        def __init__(self):
            self.sessions = self

        def get(self, key):
            return {
                "id": key,
                "model": "deepseek-v4-pro",
                "model_config": '{"provider": "dovie-cloud"}',
            }

    monkeypatch.setattr(server, "_db_for_stable_session", lambda _key: FakeDB())
    captured = {}

    def _fake_resolve(requested=None, target_model=None):
        captured["requested"] = requested
        captured["target_model"] = target_model
        return {
            "provider": None,
            "base_url": None,
            "api_key": None,
            "api_mode": None,
            "command": None,
            "args": None,
            "credential_pool": None,
        }

    monkeypatch.setattr(
        "hermes_cli.runtime_provider.resolve_runtime_provider",
        _fake_resolve,
    )

    with patch("run_agent.AIAgent") as mock_agent:
        server._make_agent("sid1", "key1", session_id="key1")

    assert mock_agent.call_args.kwargs["model"] == "deepseek-v4-pro"
    assert captured["requested"] == "dovie-cloud"
    assert captured["target_model"] == "deepseek-v4-pro"


def test_make_agent_falls_back_to_global_without_persisted_model(monkeypatch):
    _setup_make_agent_mocks(monkeypatch, {})
    monkeypatch.setattr(server, "_db_for_stable_session", lambda _key: None)

    with patch("run_agent.AIAgent") as mock_agent:
        server._make_agent("sid1", "key1", session_id="key1")

    assert mock_agent.call_args.kwargs["model"] == "test-model"
