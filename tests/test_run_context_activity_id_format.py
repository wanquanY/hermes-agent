from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any

import pytest

from hermes_agent.composition.cli_session_store import CliSessionStore, open_cli_session_store
from hermes_team_mission.domain.run_context import RunContext
from hermes_team_mission.gateway import runtime_methods
from tests.team_mission_gateway_test_support import team_mission_gateway
from tui_gateway.services import run_control


CONVERSATION_ID = "conversation-1"
CONVERSATION_SESSION_ID = "team-session-1"


def _run_context_payload(**overrides: str) -> dict[str, str]:
    payload = {
        "conversation_session_id": CONVERSATION_SESSION_ID,
        "participant_id": "leader:conversation-1",
        "activity_id": f"chat:{CONVERSATION_SESSION_ID}",
        "activity_kind": "chat",
        "execution_scope_key": "team:conversation-1:leader-conversation",
        "control_home": "/tmp/hermes-control",
        "execution_home": "/tmp/hermes-execution",
    }
    payload.update(overrides)
    return payload


def _workspace_payload(tmp_path: Path) -> dict[str, str]:
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    return {"workspace_id": "workspace-1", "workspace_path": str(workspace)}


def _leader_member(tmp_path: Path) -> dict[str, Any]:
    return {
        "member_id": "leader",
        "profile_id": "profile-leader",
        "profile_version_id": "version-leader",
        "role": "leader",
        "runtime_scope_key": "profile:profile-leader:version:version-leader",
        "dovie_profile": {
            "id": "profile-leader",
            "agentProfileVersionId": "version-leader",
            "runtimeScopeKey": "profile:profile-leader:version:version-leader",
            "hermesHomePath": str(tmp_path / "leader-home"),
        },
    }


def _worker_member(tmp_path: Path) -> dict[str, Any]:
    return {
        "member_id": "member-builder",
        "profile_id": "profile-builder",
        "profile_version_id": "version-builder",
        "role": "builder",
        "runtime_scope_key": "profile:profile-builder:version:version-builder",
        "dovie_profile": {
            "id": "profile-builder",
            "agentProfileVersionId": "version-builder",
            "runtimeScopeKey": "profile:profile-builder:version:version-builder",
            "hermesHomePath": str(tmp_path / "builder-home"),
        },
    }


def _submit_message(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    mission_id: str = "",
    target_member_id: str = "",
    request_activity_id: str = "",
) -> tuple[CliSessionStore, dict[str, Any], dict[str, Any]]:
    team_mission = team_mission_gateway()
    db = open_cli_session_store(tmp_path / "state.db")
    captured: dict[str, Any] = {}
    workspace = _workspace_payload(tmp_path)

    monkeypatch.setattr(runtime_methods, "_get_db", lambda: db)

    def fake_proxy_run_submit(params: dict[str, Any]) -> dict[str, bool]:
        captured.update(params)
        return {"ok": True}

    monkeypatch.setattr(runtime_methods, "_proxy_run_submit_via_worker", fake_proxy_run_submit)

    params: dict[str, Any] = {
        "conversation_id": CONVERSATION_ID,
        "conversation_session_id": CONVERSATION_SESSION_ID,
        "team_id": "team-1",
        "text": "Leader message",
        "workspace": workspace,
    }

    if mission_id:
        members = [_worker_member(tmp_path)] if target_member_id else [_leader_member(tmp_path)]
        db.initialize_team_mission_from_strategy(
            mission_id=mission_id,
            conversation_id=CONVERSATION_ID,
            team_id="team-1",
            title="Test mission",
            objective="Validate activity id formatting.",
            workspace_id=workspace["workspace_id"],
            workspace_path=workspace["workspace_path"],
            mode="supervised_mission",
            leader_session_id=CONVERSATION_SESSION_ID,
            metadata={"conversation_session_id": CONVERSATION_SESSION_ID},
            members=members,
        )
        params.update({
            "mission_id": mission_id,
            "missionId": mission_id,
            "members": members,
        })
        if target_member_id:
            params["target_member_id"] = target_member_id
            params["text"] = "@Builder please check"
        else:
            params["leader_runtime_scope_key"] = f"team:{mission_id}:leader-conversation"
            monkeypatch.setenv(
                "DOVIE_HERMES_RUNTIME_SCOPE_KEY",
                "profile:profile-leader:version:version-leader",
            )
    else:
        params["runtime_scope_key"] = f"team:{CONVERSATION_ID}:leader-conversation"
        monkeypatch.setenv(
            "DOVIE_HERMES_RUNTIME_SCOPE_KEY",
            f"team:{CONVERSATION_ID}:leader-conversation",
        )
    if request_activity_id:
        params["activity_id"] = request_activity_id

    response = team_mission._methods["team_mission.message.submit"](  # noqa: SLF001
        "rid-submit",
        params,
    )
    assert "error" not in response, response
    assert captured
    return db, captured, response


def test_run_context_accepts_mission_prefix_activity_id() -> None:
    context = RunContext(**_run_context_payload(
        activity_id="mission:mission-1",
        activity_kind="mission",
    ))

    assert context.activity_id == "mission:mission-1"


def test_run_context_accepts_chat_prefix_activity_id() -> None:
    context = RunContext(**_run_context_payload(activity_id="chat:session-1"))

    assert context.activity_id == "chat:session-1"


def test_run_context_accepts_team_conversation_prefix_activity_id() -> None:
    context = RunContext(**_run_context_payload(activity_id="team-conversation:conv-1"))

    assert context.activity_id == "team-conversation:conv-1"


def test_run_context_accepts_act_prefix_activity_id() -> None:
    context = RunContext(**_run_context_payload(
        activity_id="act-member_chat:session-1:member-1",
        activity_kind="member_chat",
    ))

    assert context.activity_id == "act-member_chat:session-1:member-1"


def test_run_context_accepts_team_dispatch_activity_kind() -> None:
    context = RunContext(**_run_context_payload(
        activity_id="act-team_dispatch-create",
        activity_kind="team_dispatch",
    ))

    assert context.activity_id == "act-team_dispatch-create"
    assert context.activity_kind == "team_dispatch"


def test_run_context_rejects_bare_mission_id() -> None:
    with pytest.raises(ValueError, match="RunContext.activity_id"):
        RunContext(**_run_context_payload(activity_id="mission-1", activity_kind="mission"))


def test_run_context_rejects_bare_chat() -> None:
    with pytest.raises(ValueError, match="RunContext.activity_id"):
        RunContext(**_run_context_payload(activity_id="chat"))


def test_run_context_rejects_bare_member_chat() -> None:
    with pytest.raises(ValueError, match="RunContext.activity_id"):
        RunContext(**_run_context_payload(
            activity_id="member_chat",
            activity_kind="member_chat",
        ))


def test_run_context_accepts_empty_activity_id() -> None:
    context = RunContext(**_run_context_payload(activity_id=""))

    assert context.activity_id == ""


def test_runtime_methods_submit_writes_mission_prefix_when_mission_active(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    db, captured, _response = _submit_message(
        monkeypatch,
        tmp_path,
        mission_id="mission-1",
    )
    try:
        run_context = json.loads(captured["run_context_json"])
        assert run_context["activity_kind"] == "mission"
        assert run_context["activity_id"] == "mission:mission-1"
    finally:
        db.close()


def test_runtime_methods_submit_writes_chat_prefix_when_chat_only(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    db, captured, _response = _submit_message(monkeypatch, tmp_path)
    try:
        run_context = json.loads(captured["run_context_json"])
        assert run_context["activity_kind"] == "chat"
        assert run_context["activity_id"] == f"chat:{CONVERSATION_SESSION_ID}"
    finally:
        db.close()


def test_runtime_methods_submit_writes_team_dispatch_kind_for_request_activity(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    db, captured, _response = _submit_message(
        monkeypatch,
        tmp_path,
        request_activity_id="act-team_dispatch-create",
    )
    try:
        run_context = json.loads(captured["run_context_json"])
        assert run_context["activity_kind"] == "team_dispatch"
        assert run_context["activity_id"] == "act-team_dispatch-create"
        messages = db.messages.all_as_conversation(
            CONVERSATION_SESSION_ID,
            include_storage_metadata=True,
        )
        assert [message["role"] for message in messages] == ["user"]
        assert messages[0]["metadata"]["transcript_activity_kind"] == "mission_start"
    finally:
        db.close()


def test_member_chat_runtime_emits_act_member_chat_prefix(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    db, captured, _response = _submit_message(
        monkeypatch,
        tmp_path,
        mission_id="mission-1",
        target_member_id="member-builder",
    )
    try:
        run_context = json.loads(captured["run_context_json"])
        assert run_context["activity_kind"] == "member_chat"
        assert (
            run_context["activity_id"]
            == f"act-member_chat:{CONVERSATION_SESSION_ID}:member-builder"
        )
    finally:
        db.close()


def test_record_event_for_leader_session_persists_mission_prefix_activity_id(
    tmp_path: Path,
) -> None:
    db = open_cli_session_store(tmp_path / "state.db")
    try:
        db.sessions.create(CONVERSATION_SESSION_ID, source="team_mission", transient=False)
        run_context = RunContext(**_run_context_payload(
            activity_id="mission:mission-1",
            activity_kind="mission",
        ))
        run_control.record_event(
            {"type": "message.start", "run_id": "run-1", "payload": {"text": "hi"}},
            db=db,
            run_context=run_context,
        )

        row = db._conn.execute(  # noqa: SLF001
            "SELECT activity_id FROM run_events WHERE session_id = ? ORDER BY id DESC LIMIT 1",
            (CONVERSATION_SESSION_ID,),
        ).fetchone()
        assert row is not None
        assert row["activity_id"] == "mission:mission-1"
    finally:
        db.close()


def test_record_event_for_chat_session_persists_chat_prefix_activity_id(
    tmp_path: Path,
) -> None:
    db = open_cli_session_store(tmp_path / "state.db")
    try:
        db.sessions.create(CONVERSATION_SESSION_ID, source="team_mission", transient=False)
        run_context = RunContext(**_run_context_payload(
            activity_id=f"chat:{CONVERSATION_SESSION_ID}",
            activity_kind="chat",
        ))
        run_control.record_event(
            {"type": "message.start", "run_id": "run-1", "payload": {"text": "hi"}},
            db=db,
            run_context=run_context,
        )

        row = db._conn.execute(  # noqa: SLF001
            "SELECT activity_id FROM run_events WHERE session_id = ? ORDER BY id DESC LIMIT 1",
            (CONVERSATION_SESSION_ID,),
        ).fetchone()
        assert row is not None
        assert row["activity_id"] == f"chat:{CONVERSATION_SESSION_ID}"
    finally:
        db.close()


def test_activity_id_format_pattern_declared_once_in_domain() -> None:
    pattern_start = "^(?:(?:mission|chat|" + "team-conversation):"
    domain_activity = Path("hermes_team_mission/domain/activity.py").read_text()
    run_context_src = Path("hermes_team_mission/domain/run_context.py").read_text()
    rpc_activity_src = Path("tui_gateway/methods/activity.py").read_text()

    assert domain_activity.count(pattern_start) == 1
    assert pattern_start not in run_context_src
    assert pattern_start not in rpc_activity_src

    for path in (
        Path("hermes_team_mission/domain/run_context.py"),
        Path("tui_gateway/methods/activity.py"),
    ):
        tree = ast.parse(path.read_text())
        imports = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
            and node.module == "hermes_team_mission.domain.activity"
        ]
        assert any(
            alias.name == "ACTIVITY_ID_FORMAT_PATTERN"
            for node in imports
            for alias in node.names
        ), f"{path} must import ACTIVITY_ID_FORMAT_PATTERN from domain/activity.py"
