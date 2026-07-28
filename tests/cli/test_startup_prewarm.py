from unittest.mock import patch

from hermes_cli.startup_prewarm import (
    _reset_startup_prewarm_for_tests,
    prewarm_agent_runtime_async,
)


def setup_function() -> None:
    _reset_startup_prewarm_for_tests()


def teardown_function() -> None:
    _reset_startup_prewarm_for_tests()


def test_starts_one_daemon_thread(monkeypatch):
    monkeypatch.delenv("HERMES_DEFER_AGENT_STARTUP", raising=False)

    with patch("hermes_cli.startup_prewarm.threading.Thread") as thread:
        assert prewarm_agent_runtime_async() is True
        assert prewarm_agent_runtime_async() is False

    thread.assert_called_once()
    assert thread.call_args.kwargs["name"] == "agent-runtime-prewarm"
    assert thread.call_args.kwargs["daemon"] is True
    thread.return_value.start.assert_called_once_with()


def test_respects_deferred_startup(monkeypatch):
    monkeypatch.setenv("HERMES_DEFER_AGENT_STARTUP", "1")

    with patch("hermes_cli.startup_prewarm.threading.Thread") as thread:
        assert prewarm_agent_runtime_async() is False

    thread.assert_not_called()
