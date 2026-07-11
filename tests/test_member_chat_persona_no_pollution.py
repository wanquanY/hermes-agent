from __future__ import annotations

import threading
from pathlib import Path

from hermes_agent.storage.cli_session_store import CliSessionStore, open_cli_session_store
from hermes_team_mission.domain.run_context import RunContext
from tui_gateway.run_worker import RunStartFrame
from tui_gateway.services import agent_runner
from tui_gateway.services.agent_runner import _NoopTransport, _ensure_worker_session


CONVERSATION_SESSION_ID = "team-session-1"
LEADER_PARTICIPANT_ID = "leader:conv-1"
ALICE_PARTICIPANT_ID = "member:alice"


def _run_context(
    tmp_path: Path,
    *,
    activity_kind: str = "member_chat",
    participant_id: str = ALICE_PARTICIPANT_ID,
) -> RunContext:
    activity_id = (
        f"act-member_chat:{CONVERSATION_SESSION_ID}:alice"
        if activity_kind == "member_chat"
        else "mission:mission-1"
    )
    return RunContext(
        conversation_session_id=CONVERSATION_SESSION_ID,
        participant_id=participant_id,
        activity_id=activity_id,
        activity_kind=activity_kind,
        execution_scope_key="profile:alice",
        control_home=str(tmp_path),
        execution_home=str(tmp_path),
    )


def _db_with_team_history(tmp_path: Path) -> CliSessionStore:
    db = open_cli_session_store(tmp_path / "state.db")
    db.sessions.create(CONVERSATION_SESSION_ID, source="team_mission")
    db.participants.upsert_conversation_participant(
        conversation_session_id=CONVERSATION_SESSION_ID,
        participant_id=LEADER_PARTICIPANT_ID,
        role="leader",
        display_name="Leader Name",
    )
    db.participants.upsert_conversation_participant(
        conversation_session_id=CONVERSATION_SESSION_ID,
        participant_id=ALICE_PARTICIPANT_ID,
        role="member",
        display_name="Alice",
    )
    db.messages.append(
        CONVERSATION_SESSION_ID,
        role="assistant",
        content="I am Hermes Agent and will lead this task.",
        metadata={"participant_id": LEADER_PARTICIPANT_ID},
    )
    db.messages.append(
        CONVERSATION_SESSION_ID,
        role="assistant",
        content="Alice previous reply in her own persona.",
        metadata={"participant_id": ALICE_PARTICIPANT_ID},
    )
    return db


def _hydrate_worker_history(
    monkeypatch,
    tmp_path: Path,
    db: CliSessionStore,
    *,
    run_context: RunContext | None = None,
) -> list[dict]:
    from tui_gateway import server as _server

    sessions: dict[str, dict] = {}
    monkeypatch.setattr(_server, "_sessions", sessions)
    monkeypatch.setattr(_server, "_sessions_lock", threading.Lock())
    monkeypatch.setattr(_server, "_stdio_transport", _NoopTransport())
    monkeypatch.setattr(_server, "_db_for_stable_session", lambda _sid: db)
    monkeypatch.setattr(
        agent_runner,
        "session_workspace_run_context",
        lambda _session_id, _params: {
            "cwd": str(tmp_path),
            "workspace": {
                "id": "workspace-1",
                "path": str(tmp_path),
                "kind": "local",
            },
            "source": "test",
        },
    )

    _sid, session = _ensure_worker_session(
        RunStartFrame(
            run_id="run-alice-1",
            turn_id="turn-alice-1",
            conversation_session_id=CONVERSATION_SESSION_ID,
            prompt="@Alice please respond",
            params={
                "runtime_scope_key": "profile:alice",
                "run_context_json": (run_context or _run_context(tmp_path)).to_payload(),
            },
        )
    )
    return list(session["history"])


def test_member_worker_hydrates_with_other_speakers_as_user_role(monkeypatch, tmp_path: Path) -> None:
    db = _db_with_team_history(tmp_path)

    history = _hydrate_worker_history(monkeypatch, tmp_path, db)

    assert history[0]["role"] == "system"
    assert history[0]["metadata"]["team_member_identity_contract"] is True
    assert history[1]["role"] == "user"
    assert history[1]["content"] == "[Leader Name] I am Hermes Agent and will lead this task."
    assert history[1]["metadata"]["transformed_from_role"] == "assistant"
    assert history[1]["metadata"]["transformed_speaker_pid"] == LEADER_PARTICIPANT_ID


def test_member_own_replies_kept_as_assistant(monkeypatch, tmp_path: Path) -> None:
    db = _db_with_team_history(tmp_path)

    history = _hydrate_worker_history(monkeypatch, tmp_path, db)

    assert history[2]["role"] == "assistant"
    assert history[2]["content"] == "Alice previous reply in her own persona."
    assert history[2]["metadata"]["participant_id"] == ALICE_PARTICIPANT_ID


def test_leader_mission_hydration_uses_same_participant_perspective(
    monkeypatch,
    tmp_path: Path,
) -> None:
    db = _db_with_team_history(tmp_path)

    history = _hydrate_worker_history(
        monkeypatch,
        tmp_path,
        db,
        run_context=_run_context(
            tmp_path,
            activity_kind="mission",
            participant_id=LEADER_PARTICIPANT_ID,
        ),
    )

    assert [message["role"] for message in history] == ["system", "assistant", "user"]
    assert history[0]["metadata"]["participant_id"] == LEADER_PARTICIPANT_ID
    assert history[1]["content"] == "I am Hermes Agent and will lead this task."
    assert "transformed_from_role" not in history[1]["metadata"]
    assert history[2]["content"] == "[Alice] Alice previous reply in her own persona."
    assert history[2]["metadata"]["transformed_speaker_pid"] == ALICE_PARTICIPANT_ID
