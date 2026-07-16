from __future__ import annotations

import json
from types import SimpleNamespace

from agent.verification_runtime import (
    completion_requirement_for_agent,
    hold_verification_stream,
    record_codex_item_verification,
    record_tool_verification,
    release_verification_stream,
    verification_completion_guard_enabled,
)
from hermes_agent.composition.cli_session_store import open_cli_session_store
from run_agent import AIAgent


def _agent(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    (root / "package.json").write_text(
        '{"scripts":{"test":"vitest run"}}', encoding="utf-8"
    )
    (root / "pnpm-lock.yaml").write_text("lockfileVersion: '9.0'", encoding="utf-8")
    store = open_cli_session_store(tmp_path / "state.db")
    agent = SimpleNamespace(
        _session_db=store,
        session_id="execution-1",
        memory_session_id="conversation-1",
        session_cwd=str(root),
        verification_completion_guard=True,
        verification_max_attempts=1,
        _visible_transcript_session_id=lambda: "conversation-1",
    )
    return agent, store, root


def test_native_tools_share_persisted_verification_aggregate(tmp_path) -> None:
    agent, store, root = _agent(tmp_path)
    record_tool_verification(
        agent,
        "write_file",
        {"path": str(root / "src/app.ts")},
        json.dumps({"bytes_written": 10}),
        is_error=False,
    )
    requirement = completion_requirement_for_agent(agent, attempt=0)
    assert requirement is not None
    assert requirement.scope_id == "conversation-1"

    # A trailing guardrail observation must not hide the terminal JSON.
    record_tool_verification(
        agent,
        "terminal",
        {"command": "corepack pnpm test", "workdir": str(root)},
        json.dumps({"output": "all passed", "exit_code": 0}) + "\n[advisory]",
        is_error=False,
    )
    assert completion_requirement_for_agent(agent, attempt=0) is None
    state = store.verification.status("conversation-1", root)
    assert state is not None
    assert state.status == "passed"
    store.close()


def test_codex_items_feed_the_same_aggregate(tmp_path) -> None:
    agent, store, root = _agent(tmp_path)
    record_codex_item_verification(
        agent,
        {
            "type": "fileChange",
            "status": "applied",
            "cwd": str(root),
            "changes": [{"path": str(root / "src/app.ts")}],
        },
    )
    assert completion_requirement_for_agent(agent, attempt=0) is not None

    record_codex_item_verification(
        agent,
        {
            "type": "commandExecution",
            "command": "pnpm test -- src/app.test.ts",
            "cwd": str(root),
            "exitCode": 1,
            "aggregatedOutput": "failed",
        },
    )
    failed = store.verification.status("conversation-1", root)
    assert failed is not None
    assert failed.status == "failed"

    record_codex_item_verification(
        agent,
        {
            "type": "commandExecution",
            "command": "pnpm test -- src/app.test.ts",
            "cwd": str(root),
            "exitCode": 0,
            "aggregatedOutput": "passed",
        },
    )
    passed = store.verification.status("conversation-1", root)
    assert passed is not None
    assert passed.status == "passed"
    assert passed.evidence is not None
    assert passed.evidence.scope == "targeted"
    store.close()


def test_requirement_is_disabled_or_bounded(tmp_path) -> None:
    agent, store, root = _agent(tmp_path)
    record_tool_verification(
        agent,
        "patch",
        {"path": str(root / "src/app.ts")},
        json.dumps({"success": True}),
        is_error=False,
    )
    assert completion_requirement_for_agent(agent, attempt=1) is None
    agent.verification_completion_guard = False
    assert completion_requirement_for_agent(agent, attempt=0) is None
    store.close()


def test_auto_completion_guard_is_surface_aware(monkeypatch) -> None:
    agent = SimpleNamespace(verification_completion_guard="auto", platform="desktop")
    monkeypatch.delenv("HERMES_PLATFORM", raising=False)
    monkeypatch.delenv("HERMES_SESSION_PLATFORM", raising=False)
    monkeypatch.delenv("HERMES_SESSION_SOURCE", raising=False)
    assert verification_completion_guard_enabled(agent) is True

    agent.platform = "telegram"
    assert verification_completion_guard_enabled(agent) is False

    agent.verification_completion_guard = True
    assert verification_completion_guard_enabled(agent) is True


def test_stream_hold_discards_premature_text_and_releases_verified_text() -> None:
    delivered: list[str] = []
    agent = object.__new__(AIAgent)
    agent.stream_delta_callback = delivered.append
    agent._stream_callback = None
    agent._stream_needs_break = False
    agent._stream_inject_tool_breaks = True
    agent._stream_think_scrubber = None
    agent._stream_context_scrubber = None
    agent._current_streamed_assistant_text = ""

    hold_verification_stream(agent)
    agent._fire_stream_delta("premature")
    assert delivered == []
    assert release_verification_stream(agent, deliver=False) == "premature"
    assert delivered == []

    hold_verification_stream(agent)
    agent._fire_stream_delta("verified")
    assert release_verification_stream(agent, deliver=True) == "verified"
    assert delivered == ["verified"]
    assert agent._current_streamed_assistant_text == "verified"
