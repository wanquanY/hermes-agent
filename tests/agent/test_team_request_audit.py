from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent.team_request_audit import dump_team_inference_request_audit
from hermes_team_mission.domain.run_context import RunContext


def _agent(tmp_path: Path, participant_id: str) -> SimpleNamespace:
    context = RunContext(
        conversation_session_id="team-conversation-1",
        participant_id=participant_id,
        activity_id="chat:team-conversation-1",
        activity_kind="chat" if participant_id.startswith("leader:") else "member_chat",
        execution_scope_key=f"scope:{participant_id}",
        control_home=str(tmp_path / "control"),
        execution_home=str(tmp_path / "execution"),
        profile_id="profile-1",
        memory_namespace=f"memory:{participant_id}",
    )
    return SimpleNamespace(
        session_id="team-conversation-1",
        provider="openrouter",
        api_mode="chat_completions",
        base_url="https://example.invalid/v1",
        logs_dir=tmp_path / "execution" / "sessions",
        _hermes_active_run_id="run-1",
        _hermes_active_turn_id="turn-1",
        _active_run_context=lambda: context,
    )


@pytest.mark.parametrize("participant_id", ["leader:team-1", "member:member-1"])
def test_team_request_audit_captures_final_body_without_transport_secrets(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    participant_id: str,
) -> None:
    monkeypatch.setenv("DOVIE_STREAM_TRACE", "1")
    agent = _agent(tmp_path, participant_id)
    request_body = {
        "model": "deepseek-v4-pro",
        "messages": [
            {"role": "system", "content": "actor identity"},
            {"role": "user", "content": "你是谁？"},
        ],
        "tools": [{"type": "function", "function": {"name": "status"}}],
        "temperature": 0.2,
        "timeout": 60,
        "extra_headers": {"Authorization": "Bearer secret"},
    }

    audit_file = dump_team_inference_request_audit(
        agent,
        request_body,
        api_call_count=2,
        retry_count=1,
    )

    assert audit_file is not None
    assert audit_file.parent == tmp_path / "control" / "logs" / "team-request-audit" / audit_file.parent.name
    payload = json.loads(audit_file.read_text(encoding="utf-8"))
    assert payload["participant_id"] == participant_id
    assert payload["run_context"]["execution_scope_key"] == f"scope:{participant_id}"
    assert payload["request_body"] == {
        "model": "deepseek-v4-pro",
        "messages": request_body["messages"],
        "tools": request_body["tools"],
        "temperature": 0.2,
    }
    assert payload["omitted_transport_keys"] == ["extra_headers", "timeout"]
    assert "secret" not in audit_file.read_text(encoding="utf-8")


def test_team_request_audit_is_disabled_without_trace_flag(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.delenv("DOVIE_STREAM_TRACE", raising=False)
    monkeypatch.delenv("DOVIE_TEAM_REQUEST_AUDIT", raising=False)

    result = dump_team_inference_request_audit(
        _agent(tmp_path, "member:member-1"),
        {"model": "test", "messages": []},
        api_call_count=1,
        retry_count=0,
    )

    assert result is None
    assert not (tmp_path / "control" / "logs" / "team-request-audit").exists()
