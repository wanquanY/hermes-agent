from __future__ import annotations

from hermes_team_mission.runtime.conversation_transcript import _append_message_once
from hermes_team_mission.runtime.leader_runs import ensure_team_leader_message_run_state


class _Sessions:
    def __init__(self) -> None:
        self.created: list[tuple[str, str, bool]] = []

    def get(self, _session_id: str):
        return None

    def create(self, session_id: str, *, source: str, transient: bool) -> None:
        self.created.append((session_id, source, transient))


class _Messages:
    def __init__(self) -> None:
        self.rows: list[dict] = []

    def list(self, _session_id: str):
        return list(self.rows)

    def append(self, session_id: str, role: str, content: str, **kwargs) -> None:
        self.rows.append(
            {
                "session_id": session_id,
                "role": role,
                "content": content,
                **kwargs,
            }
        )


class _ConversationStore:
    def __init__(self) -> None:
        self.sessions = _Sessions()
        self.messages = _Messages()


class _Runs:
    def __init__(self) -> None:
        self.rows: dict[str, dict] = {}

    def get(self, run_id: str):
        return self.rows.get(run_id)

    def upsert(self, **kwargs) -> None:
        self.rows[kwargs["run_id"]] = kwargs


class _RunStore:
    def __init__(self) -> None:
        self.runs = _Runs()


def test_conversation_transcript_uses_composed_session_and_message_services() -> None:
    db = _ConversationStore()
    metadata = {
        "team_mission": {
            "kind": "leader_report",
            "mission_id": "mission-1",
            "source_run_id": "run-1",
        }
    }

    assert _append_message_once(
        db,
        session_id="conversation-session-1",
        role="assistant",
        content="done",
        metadata=metadata,
        participant_id="leader:team-1",
    )
    assert db.sessions.created == [
        ("conversation-session-1", "team_mission", False)
    ]
    assert db.messages.rows == [
        {
            "session_id": "conversation-session-1",
            "role": "assistant",
            "content": "done",
            "participant_id": "leader:team-1",
            "metadata": metadata,
        }
    ]

    assert not _append_message_once(
        db,
        session_id="conversation-session-1",
        role="assistant",
        content="done again",
        metadata=metadata,
        participant_id="leader:team-1",
    )
    assert len(db.messages.rows) == 1


def test_leader_run_state_uses_composed_run_service_idempotently() -> None:
    db = _RunStore()

    ensure_team_leader_message_run_state(
        db,
        run_id="run-1",
        session_id="conversation-session-1",
        runtime_scope_key="profile:leader",
        result={"execution_session_id": "runtime-1", "status": "running"},
    )
    ensure_team_leader_message_run_state(
        db,
        run_id="run-1",
        session_id="conversation-session-1",
        runtime_scope_key="profile:leader",
        result={"execution_session_id": "runtime-2", "status": "failed"},
    )

    assert db.runs.rows == {
        "run-1": {
            "run_id": "run-1",
            "session_id": "conversation-session-1",
            "runtime_scope_key": "profile:leader",
            "execution_session_id": "runtime-1",
            "status": "running",
        }
    }
