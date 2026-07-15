from __future__ import annotations

from pathlib import Path

from hermes_agent.composition.cli_session_store import CliSessionStore, open_cli_session_store
from tui_gateway.services import run_control


CONV_SESSION = "team-session-1"


def _new_db(tmp_path: Path) -> CliSessionStore:
    db = open_cli_session_store(tmp_path / "state.db")
    db.sessions.create(CONV_SESSION, source="team_mission", transient=False)
    db.participants.upsert_conversation_participant(
        conversation_session_id=CONV_SESSION,
        participant_id="leader:conv-1",
        role="leader",
        member_id="m-lead",
        agent_profile_id="p-lead",
        runtime_scope_key="team:conv-1:leader-conversation",
        display_name="Lead",
    )
    db.participants.upsert_conversation_participant(
        conversation_session_id=CONV_SESSION,
        participant_id="member:m-alice",
        role="member",
        member_id="m-alice",
        agent_profile_id="p-alice",
        runtime_scope_key="member-chat:conv-1:m-alice",
        display_name="Alice",
    )
    return db


def _record_message(db: CliSessionStore, *, run_id: str, payload: dict, frame_participant_id: str = "") -> dict:
    db.runs.upsert(run_id=run_id, session_id=CONV_SESSION, status="running")
    frame = {
        "type": "message.complete",
        "session_id": CONV_SESSION,
        "conversation_session_id": CONV_SESSION,
        "run_id": run_id,
        "turn_id": f"turn-{run_id}",
        "seq": 1,
        "payload": {"text": f"text from {run_id}", "status": "complete", **payload},
    }
    if frame_participant_id:
        frame["participant_id"] = frame_participant_id
    run_control.record_event(frame, db=db)
    events = db.runs.list_events(CONV_SESSION, run_id=run_id)
    assert events, "event was not persisted"
    return events[0]


def test_record_event_stamps_leader_participant_from_table(tmp_path: Path):
    db = _new_db(tmp_path)

    event = _record_message(
        db,
        run_id="run-leader",
        payload={"runtime_scope_key": "team:conv-1:leader-conversation"},
    )

    assert event.get("participant_id") == "leader:conv-1"
    assert (event.get("payload") or {}).get("participant_id") == "leader:conv-1"


def test_record_event_stamps_member_participant_from_table(tmp_path: Path):
    db = _new_db(tmp_path)

    event = _record_message(
        db,
        run_id="run-member",
        payload={"member_id": "m-alice", "agent_profile_id": "p-alice"},
    )

    assert event.get("participant_id") == "member:m-alice"
    assert (event.get("payload") or {}).get("participant_id") == "member:m-alice"


def test_record_event_lookup_miss_falls_back_to_member_hint(
    tmp_path: Path,
    monkeypatch,
):
    db = _new_db(tmp_path)
    diagnostics: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        run_control,
        "_diagnostic_warning",
        lambda label, **fields: diagnostics.append((label, fields)),
    )

    event = _record_message(
        db,
        run_id="run-unregistered",
        payload={"member_id": "m-ghost", "agent_profile_id": "p-ghost"},
    )

    assert event.get("participant_id") == "member:m-ghost"
    assert (event.get("payload") or {}).get("participant_id") == "member:m-ghost"
    assert [label for label, _fields in diagnostics if label == "participant-resolve-miss"] == []


def test_record_event_preserves_existing_frame_participant_id_and_skips_lookup(
    tmp_path: Path,
    monkeypatch,
):
    db = _new_db(tmp_path)

    def _fail_lookup(**_kwargs):
        raise AssertionError("resolver must not run for pre-stamped frames")

    monkeypatch.setattr(db.participants, "resolve_participant_id", _fail_lookup)

    event = _record_message(
        db,
        run_id="run-prestamped",
        frame_participant_id="member:pre-stamped",
        payload={"member_id": "m-alice", "participant_id": "member:pre-stamped"},
    )

    assert event.get("participant_id") == "member:pre-stamped"
    assert (event.get("payload") or {}).get("participant_id") == "member:pre-stamped"
