"""Decoupled group-chat: member reply relays into the conversation session."""

import json
from pathlib import Path

from hermes_state import SessionDB


def test_member_reply_relays_into_conversation_session(tmp_path: Path):
    db = SessionDB(tmp_path / "state.db")
    db.create_session("conv", source="team_mission", transient=False)
    db.create_session("member-sess", source="team_mission", transient=False)
    db.register_member_chat_run(
        run_id="run-1",
        conversation_session_id="conv",
        member_id="m1",
        agent_profile_id="p1",
        display_name="Alice",
    )
    # Run must be non-terminal so the message.complete is accepted.
    db.upsert_run(run_id="run-1", session_id="member-sess", runtime_scope_key="team:conv:member:m1", status="running")

    # The member's reply arrives on its own session → hook relays it.
    db.append_run_event(
        "member-sess",
        {
            "type": "message.complete",
            "session_id": "member-sess",
            "stored_session_id": "member-sess",
            "run_id": "run-1",
            "turn_id": "t1",
            "runtime_scope_key": "team:conv:member:m1",
            "seq": 1,
            "payload": {"text": "你好，我是 Alice", "status": "complete"},
        },
    )

    # Relayed as an assistant message into the conversation session, tagged member.
    messages = db.get_messages("conv")
    relayed = [m for m in messages if "你好，我是 Alice" in str(m.get("content") or "")]
    assert relayed, messages
    msg = relayed[0]
    assert msg.get("role") == "assistant"
    meta = msg.get("metadata")
    if isinstance(meta, str):
        meta = json.loads(meta)
    assert (meta or {}).get("team_mission", {}).get("member_id") == "m1"
    assert (meta or {}).get("team_mission", {}).get("display_name") == "Alice"

    # A live run event was relayed onto the conversation session.
    events = db.list_run_events("conv")
    assert any((e.get("payload") or {}).get("member_chat_conversation_mirror") for e in events)

    # Registry marked relayed (dedup) → re-appending does not double-relay.
    assert int(db.get_member_chat_run("run-1").get("relayed") or 0) == 1
    db.append_run_event(
        "member-sess",
        {
            "type": "message.complete",
            "session_id": "member-sess",
            "stored_session_id": "member-sess",
            "run_id": "run-1",
            "turn_id": "t1",
            "seq": 2,
            "payload": {"text": "你好，我是 Alice", "status": "complete"},
        },
    )
    again = [m for m in db.get_messages("conv") if "你好，我是 Alice" in str(m.get("content") or "")]
    assert len(again) == 1  # not double-relayed


def test_unregistered_run_is_not_relayed(tmp_path: Path):
    db = SessionDB(tmp_path / "state.db")
    db.create_session("conv", source="team_mission", transient=False)
    db.create_session("worker-sess", source="team_mission", transient=False)
    db.upsert_run(run_id="run-x", session_id="worker-sess", status="running")
    db.append_run_event(
        "worker-sess",
        {
            "type": "message.complete",
            "session_id": "worker-sess",
            "stored_session_id": "worker-sess",
            "run_id": "run-x",
            "seq": 1,
            "payload": {"text": "not a member chat", "status": "complete"},
        },
    )
    assert db.get_messages("conv") == [] or all(
        "not a member chat" not in str(m.get("content") or "") for m in db.get_messages("conv")
    )
