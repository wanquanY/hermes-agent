"""Visible browser lifecycle, configuration, and argv contracts."""

from __future__ import annotations

import os
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest


def _reset_headed_cache() -> None:
    import tools.browser_tool as browser_tool

    browser_tool._cached_headed_mode = None
    browser_tool._headed_mode_resolved = False


@pytest.fixture(autouse=True)
def clean_headed_cache():
    _reset_headed_cache()
    yield
    _reset_headed_cache()


@pytest.mark.parametrize(
    ("config", "environment", "expected"),
    [
        ({}, {}, False),
        ({"browser": {"headed": True}}, {}, True),
        ({"browser": {"headed": "yes"}}, {}, True),
        ({"browser": {"headed": False}}, {}, False),
        ({}, {"AGENT_BROWSER_HEADED": "1"}, True),
        ({}, {"AGENT_BROWSER_HEADED": "invalid"}, False),
    ],
)
def test_headed_mode_resolution(config, environment, expected):
    from tools.browser_tool import _is_headed_mode

    with (
        patch.dict(os.environ, environment, clear=False),
        patch("hermes_cli.config.read_raw_config", return_value=config),
    ):
        if "AGENT_BROWSER_HEADED" not in environment:
            os.environ.pop("AGENT_BROWSER_HEADED", None)
        assert _is_headed_mode() is expected


def test_headed_mode_resolution_is_cached():
    from tools.browser_tool import _is_headed_mode

    with patch(
        "hermes_cli.config.read_raw_config",
        return_value={"browser": {"headed": True}},
    ) as read_config:
        assert _is_headed_mode() is True
        assert _is_headed_mode() is True
    read_config.assert_called_once_with()


@pytest.mark.parametrize("headed", [False, True])
def test_per_turn_cleanup_preserves_only_headed_browser(headed):
    from agent.chat_completion_helpers import cleanup_task_resources

    agent = SimpleNamespace(verbose_logging=False)
    with (
        patch("tools.browser_tool._is_headed_mode", return_value=headed),
        patch("run_agent.cleanup_vm") as cleanup_vm,
        patch("run_agent.cleanup_browser") as cleanup_browser,
        patch(
            "agent.chat_completion_helpers.is_persistent_env",
            return_value=False,
        ),
    ):
        cleanup_task_resources(agent, "task-visible-browser")

    cleanup_vm.assert_called_once_with("task-visible-browser")
    if headed:
        cleanup_browser.assert_not_called()
    else:
        cleanup_browser.assert_called_once_with("task-visible-browser")


def test_env_fallback_preserves_browser_when_headed_resolver_fails():
    from agent.chat_completion_helpers import cleanup_task_resources

    with (
        patch(
            "tools.browser_tool._is_headed_mode",
            side_effect=RuntimeError("config unavailable"),
        ),
        patch.dict(os.environ, {"AGENT_BROWSER_HEADED": "true"}),
        patch("run_agent.cleanup_vm"),
        patch("run_agent.cleanup_browser") as cleanup_browser,
        patch(
            "agent.chat_completion_helpers.is_persistent_env",
            return_value=False,
        ),
    ):
        cleanup_task_resources(SimpleNamespace(verbose_logging=False), "task")
    cleanup_browser.assert_not_called()


def _capture_browser_argv(browser_tool, session_info: dict) -> list[str]:
    captured: list[list[str]] = []
    process = MagicMock(returncode=0)
    process.wait.return_value = None

    def popen(argv, **_kwargs):
        captured.append(argv)
        return process

    output = '{"success": true, "data": {"snapshot": "page", "refs": {}}}'
    file_handle = MagicMock()
    file_handle.__enter__.return_value.read.return_value = output
    file_handle.__exit__.return_value = False
    with (
        patch("tools.browser_tool._get_session_info", return_value=session_info),
        patch(
            "tools.browser_tool._find_agent_browser",
            return_value="/usr/bin/agent-browser",
        ),
        patch("tools.browser_tool._is_local_mode", return_value=True),
        patch("tools.browser_tool._chromium_installed", return_value=True),
        patch("tools.browser_tool._get_cloud_provider", return_value=None),
        patch("tools.browser_tool._get_cdp_override", return_value=""),
        patch("tools.browser_tool._is_camofox_mode", return_value=False),
        patch("subprocess.Popen", side_effect=popen),
        patch("os.open", return_value=99),
        patch("os.close"),
        patch("os.unlink"),
        patch("os.makedirs"),
        patch("builtins.open", return_value=file_handle),
        patch("tools.interrupt.is_interrupted", return_value=False),
        patch("tools.browser_tool._write_owner_pid"),
    ):
        browser_tool._run_browser_command(
            "task",
            "snapshot",
            [],
            _engine_override="auto",
        )
    assert len(captured) == 1
    return captured[0]


def test_headed_flag_is_local_only():
    import tools.browser_tool as browser_tool

    browser_tool._cached_headed_mode = True
    browser_tool._headed_mode_resolved = True

    local_argv = _capture_browser_argv(
        browser_tool,
        {"session_name": "local-session"},
    )
    cloud_argv = _capture_browser_argv(
        browser_tool,
        {
            "session_name": "cloud-session",
            "cdp_url": "wss://example.invalid/cdp",
        },
    )

    assert "--headed" in local_argv
    assert "--headed" not in cloud_argv
    assert "--cdp" in cloud_argv
