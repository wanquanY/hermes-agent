from __future__ import annotations

from pathlib import Path

from tools.delegate_tool import DELEGATE_TASK_SCHEMA


ROOT = Path(__file__).resolve().parents[2]


def _source(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_subagent_execution_service_is_the_only_async_business_owner() -> None:
    facade = _source("tools/async_delegation.py")
    assert "ThreadPoolExecutor" not in facade
    assert "_records" not in facade
    assert "completion_queue" not in facade
    assert "SubagentExecutionService" in _source(
        "hermes_agent/application/subagent_execution_service.py"
    )
    assert "dispatch_async_delegation(" not in _source("tools/delegate_tool.py")


def test_no_subagent_completion_can_be_injected_as_a_prompt_message() -> None:
    assert "async_delegation_watcher" not in _source(
        "hermes_gateway/process_watcher.py"
    )
    assert "ASYNC DELEGATION" not in _source("tools/process_registry.py")
    assert "async_delegation" not in _source(
        "hermes_gateway/process_notifications.py"
    )
    poller = _source("tui_gateway/services/notification_poller.py")
    assert 'startswith("activity.")' in poller


def test_delegate_schema_exposes_explicit_mode_and_deprecates_background() -> None:
    properties = DELEGATE_TASK_SCHEMA["parameters"]["properties"]
    assert properties["execution_mode"]["enum"] == ["sync", "async"]
    assert "Deprecated compatibility alias" in properties["background"]["description"]


def test_phase_8_delegation_modules_respect_file_size_limit() -> None:
    paths = [
        "tools/delegate_tool.py",
        "tools/delegation_builder.py",
        "tools/delegation_credentials.py",
        "tools/delegation_dovie.py",
        "tools/delegation_result_utils.py",
        "tools/delegation_runner.py",
        "tools/delegation_schema.py",
        "tools/delegation_summary.py",
        "hermes_agent/application/subagent_execution_service.py",
    ]
    oversized = {
        path: len(_source(path).splitlines())
        for path in paths
        if len(_source(path).splitlines()) > 2000
    }
    assert oversized == {}
