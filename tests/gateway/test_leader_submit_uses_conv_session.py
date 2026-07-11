from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from hermes_agent.storage.cli_session_store import CliSessionStore, open_cli_session_store
from hermes_team_mission.domain.run_context import RunContext
from hermes_team_mission.gateway import runtime_methods
from tests.team_mission_gateway_test_support import team_mission_gateway
from tui_gateway.run_worker import EventFrame
from tui_gateway.services.run_control import record_event
from hermes_agent.orchestration.worker_frame_router import WorkerFrameRouter


CONVERSATION_ID = "conversation-1"
CONVERSATION_SESSION_ID = "team-session-1"


class _FakeSupervisor:
    async def send(self, scope_key: str, frame: Any) -> bool:
        return True


def _workspace_payload(tmp_path: Path) -> dict[str, str]:
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    return {"workspace_id": "workspace-1", "workspace_path": str(workspace)}


def _workspace_kwargs(tmp_path: Path) -> dict[str, str]:
    workspace = _workspace_payload(tmp_path)
    return {"workspace_id": workspace["workspace_id"], "workspace_path": workspace["workspace_path"]}


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


def _submit_leader(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    mission_id: str = "",
    conversation_ensure_index_only: bool = False,
    request_activity_id: str = "",
    extra_params: dict[str, Any] | None = None,
) -> tuple[CliSessionStore, dict[str, Any], dict[str, Any]]:
    team_mission = team_mission_gateway()
    db = open_cli_session_store(tmp_path / "state.db")
    captured: dict[str, Any] = {}

    monkeypatch.setattr(runtime_methods, "_get_db", lambda: db)

    def fake_proxy_run_submit(params: dict[str, Any]) -> dict[str, bool]:
        captured.update(params)
        return {"ok": True}

    monkeypatch.setattr(runtime_methods, "_proxy_run_submit_via_worker", fake_proxy_run_submit)
    if conversation_ensure_index_only:
        def fake_ensure_team_mission_conversation(**kwargs: Any) -> dict[str, Any]:
            db.session_index.upsert(
                session_id=kwargs["conversation_session_id"],
                source="team_mission",
                session_kind="team_mission",
                conversation_kind="team",
                title=kwargs.get("title") or "",
                team_id=kwargs.get("team_id") or "",
                mission_id=kwargs.get("mission_id") or "",
                conversation_id=kwargs.get("conversation_id") or "",
            )
            return {
                "conversation_id": kwargs["conversation_id"],
                "conversation_session_id": kwargs["conversation_session_id"],
                "title": kwargs.get("title") or "",
                "team_id": kwargs.get("team_id") or "",
            }

        monkeypatch.setattr(db, "ensure_team_mission_conversation", fake_ensure_team_mission_conversation)

    params: dict[str, Any]
    if mission_id:
        leader = _leader_member(tmp_path)
        db.initialize_team_mission_from_strategy(
            mission_id=mission_id,
            conversation_id=CONVERSATION_ID,
            team_id="team-1",
            title="监督执行",
            objective="初始任务",
            **_workspace_kwargs(tmp_path),
            mode="supervised_mission",
            leader_session_id=CONVERSATION_SESSION_ID,
            metadata={"conversation_session_id": CONVERSATION_SESSION_ID},
            members=[leader],
        )
        monkeypatch.setenv("DOVIE_HERMES_RUNTIME_SCOPE_KEY", leader["runtime_scope_key"])
        params = {
            "mission_id": mission_id,
            "conversation_id": CONVERSATION_ID,
            "conversation_session_id": CONVERSATION_SESSION_ID,
            "team_id": "team-1",
            "text": "Leader 查看当前任务进度",
            "workspace": _workspace_payload(tmp_path),
            "members": [leader],
            "leader_runtime_scope_key": f"team:{mission_id}:leader-conversation",
        }
    else:
        monkeypatch.setenv(
            "DOVIE_HERMES_RUNTIME_SCOPE_KEY",
            f"team:{CONVERSATION_ID}:leader-conversation",
        )
        params = {
            "conversation_id": CONVERSATION_ID,
            "conversation_session_id": CONVERSATION_SESSION_ID,
            "team_id": "team-1",
            "text": "Leader 直接回复一次",
            "runtime_scope_key": f"team:{CONVERSATION_ID}:leader-conversation",
            "workspace": _workspace_payload(tmp_path),
        }
    if request_activity_id:
        params["activity_id"] = request_activity_id
    if extra_params:
        params.update(extra_params)

    response = team_mission._methods["team_mission.message.submit"](  # noqa: SLF001
        "rid-leader",
        params,
    )

    assert "error" not in response, response
    assert captured
    return db, captured, response


def _events_for_session(db: CliSessionStore, session_id: str) -> list[dict[str, Any]]:
    rows = db._conn.execute(  # noqa: SLF001 - test introspection
        "SELECT seq, event_type, run_id, runtime_scope_key, payload_json, event_json "
        "FROM run_events WHERE session_id = ? ORDER BY seq",
        (session_id,),
    ).fetchall()
    return [
        {
            "seq": row["seq"],
            "type": row["event_type"],
            "run_id": row["run_id"],
            "scope": row["runtime_scope_key"],
            "payload": json.loads(row["payload_json"] or "{}"),
            "frame": json.loads(row["event_json"] or "{}"),
        }
        for row in rows
    ]


def test_leader_spawn_payload_carries_run_context_json(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    _db, captured, _response = _submit_leader(monkeypatch, tmp_path)

    run_context = RunContext.from_payload(captured["run_context_json"])

    assert run_context.conversation_session_id == CONVERSATION_SESSION_ID
    assert run_context.activity_kind == "chat"
    assert run_context.execution_scope_key == captured["runtime_scope_key"]


def test_leader_team_dispatch_request_uses_team_dispatch_run_context(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    db, captured, _response = _submit_leader(
        monkeypatch,
        tmp_path,
        request_activity_id="act-team_dispatch-create",
    )

    run_context = RunContext.from_payload(captured["run_context_json"])

    assert run_context.conversation_session_id == CONVERSATION_SESSION_ID
    assert run_context.activity_id == "act-team_dispatch-create"
    assert run_context.activity_kind == "team_dispatch"
    messages = db.messages.all_as_conversation(
        CONVERSATION_SESSION_ID,
        include_storage_metadata=True,
    )
    assert [message["role"] for message in messages] == ["user"]
    assert messages[0]["metadata"]["transcript_activity_kind"] == "mission_start"


def test_leader_spawn_conversation_session_id_is_conv_session(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    _db, captured, response = _submit_leader(monkeypatch, tmp_path)

    assert captured["conversation_session_id"] == CONVERSATION_SESSION_ID
    assert captured["session_id"] == CONVERSATION_SESSION_ID
    assert not captured["conversation_session_id"].startswith("memberchat:")
    assert response["result"]["leader_turn"]["conversation_session_id"] == CONVERSATION_SESSION_ID


def test_leader_submit_persists_visible_user_message_before_worker(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    db, captured, _response = _submit_leader(monkeypatch, tmp_path)

    messages = db.messages.all_as_conversation(
        CONVERSATION_SESSION_ID,
        include_storage_metadata=True,
    )

    assert [message["role"] for message in messages] == ["user"]
    assert messages[0]["content"] == "Leader 直接回复一次"
    assert messages[0]["conversation_message_id"].startswith("msg_")
    assert messages[0]["metadata"]["message_kind"] == "user_submission"
    assert messages[0]["metadata"]["run_id"] == captured["run_id"]
    assert messages[0]["metadata"]["turn_id"] == captured["turn_id"]


def test_leader_submit_persists_attachment_metadata_for_history(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    attachments = [
        {
            "id": "image-1",
            "name": "architecture.png",
            "fileName": "architecture.png",
            "mimeType": "image/png",
            "size": 2048,
            "path": "/tmp/architecture.png",
            "kind": "image",
        },
        {
            "id": "pdf-1",
            "name": "report.pdf",
            "fileName": "report.pdf",
            "mimeType": "application/pdf",
            "size": 4096,
            "path": "/tmp/report.pdf",
            "kind": "file",
        },
    ]
    db, captured, _response = _submit_leader(
        monkeypatch,
        tmp_path,
        extra_params={
            "text": "请总结这两个文件\n\n[Attachment Context]\n- report.pdf",
            "draft_text": "请总结这两个文件",
            "attachments": attachments,
        },
    )

    messages = db.messages.all_as_conversation(
        CONVERSATION_SESSION_ID,
        include_storage_metadata=True,
    )

    assert messages[0]["content"] == "请总结这两个文件"
    assert messages[0]["metadata"]["draft_text"] == "请总结这两个文件"
    assert messages[0]["metadata"]["attachments"] == attachments
    assert messages[0]["metadata"]["attachment_count"] == 2
    assert captured["draft_text"] == "请总结这两个文件"
    assert captured["attachments"] == attachments


def test_leader_submit_materializes_canonical_session_when_index_exists_without_session(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    db, captured, _response = _submit_leader(
        monkeypatch,
        tmp_path,
        conversation_ensure_index_only=True,
    )

    assert db.session_index.get(CONVERSATION_SESSION_ID)
    assert db.sessions.get(CONVERSATION_SESSION_ID)
    messages = db.messages.all_as_conversation(
        CONVERSATION_SESSION_ID,
        include_storage_metadata=True,
    )
    assert [message["role"] for message in messages] == ["user"]
    assert messages[0]["content"] == "Leader 直接回复一次"
    assert messages[0]["metadata"]["run_id"] == captured["run_id"]
    assert messages[0]["metadata"]["turn_id"] == captured["turn_id"]


@pytest.mark.asyncio
async def test_leader_events_route_to_conv_via_run_context(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    db, captured, _response = _submit_leader(monkeypatch, tmp_path)

    router = WorkerFrameRouter(
        sender=_FakeSupervisor(),
        publish_event=lambda params, run_context=None: record_event(
            params,
            db=db,
            run_context=run_context,
        ),
        publish_run_terminal=lambda **_kwargs: {"published": True},
    )
    router.record_run_start(
        scope_key=captured["runtime_scope_key"],
        run_id=captured["run_id"],
        conversation_session_id=captured["conversation_session_id"],
        turn_id=captured["turn_id"],
        run_context_json=captured["run_context_json"],
    )

    await router.on_event(
        captured["runtime_scope_key"],
        EventFrame(
            params={
                "type": "message.complete",
                "session_id": "runtime-leader-conversation",
                "conversation_session_id": "runtime-leader-conversation",
                "run_id": captured["run_id"],
                "turn_id": captured["turn_id"],
                "seq": 1,
                "payload": {"text": "leader reply via worker router", "status": "complete"},
            }
        ),
    )

    conv_events = _events_for_session(db, CONVERSATION_SESSION_ID)
    assert [event["type"] for event in conv_events] == ["message.complete"]
    assert conv_events[0]["payload"]["text"] == "leader reply via worker router"
    assert conv_events[0]["frame"]["conversation_session_id"] == CONVERSATION_SESSION_ID
    assert conv_events[0]["payload"]["run_context"]["conversation_session_id"] == CONVERSATION_SESSION_ID
    assert _events_for_session(db, "runtime-leader-conversation") == []


def test_leader_with_active_mission_sets_activity_kind_mission(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    _chat_db, chat_captured, _chat_response = _submit_leader(monkeypatch, tmp_path / "chat")
    chat_context = RunContext.from_payload(chat_captured["run_context_json"])
    assert chat_context.activity_kind == "chat"
    assert chat_context.activity_id == f"chat:{CONVERSATION_SESSION_ID}"

    _mission_db, mission_captured, _mission_response = _submit_leader(
        monkeypatch,
        tmp_path / "mission",
        mission_id="mission-1",
    )
    mission_context = RunContext.from_payload(mission_captured["run_context_json"])
    assert mission_context.conversation_session_id == CONVERSATION_SESSION_ID
    assert mission_context.activity_kind == "mission"
    assert mission_context.activity_id == "mission:mission-1"
