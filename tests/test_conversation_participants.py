"""Conversation-architecture refactor foundation tests.

Two locks:
  1. P1 — the conversation_participants data layer + run→participant resolver.
  2. P0 — the routing invariant: a recorded run event reaches a conversation
     purely by stored_session_id, with NO mission binding required. This is the
     physical basis for "conversation-first"; every later phase depends on it
     staying true, so it gets an explicit regression guard.
"""

from pathlib import Path

from hermes_state import SessionDB


# ── P1: participant data layer ───────────────────────────────────────
def test_participant_upsert_list_and_get(tmp_path: Path):
    db = SessionDB(tmp_path / "state.db")
    db.create_session("conv", source="team_mission", transient=False)

    db.upsert_conversation_participant(
        conversation_session_id="conv",
        participant_id="leader",
        role="leader",
        agent_profile_id="p-lead",
        runtime_scope_key="team:conv:leader-conversation",
        display_name="Leader",
    )
    db.upsert_conversation_participant(
        conversation_session_id="conv",
        participant_id="m1",
        role="member",
        member_id="m1",
        agent_profile_id="p-alice",
        runtime_scope_key="team:conv:member:m1",
        display_name="Alice",
    )

    participants = db.list_conversation_participants("conv")
    assert {p["participant_id"] for p in participants} == {"leader", "m1"}
    # leader sorts ahead of plain members.
    assert participants[0]["participant_id"] == "leader"

    alice = db.get_conversation_participant("conv", "m1")
    assert alice["display_name"] == "Alice"
    assert alice["role"] == "member"
    assert alice["agent_profile_id"] == "p-alice"


def test_participant_upsert_is_idempotent_and_preserves_display(tmp_path: Path):
    db = SessionDB(tmp_path / "state.db")
    db.create_session("conv", source="team_mission", transient=False)
    db.upsert_conversation_participant(
        conversation_session_id="conv",
        participant_id="m1",
        member_id="m1",
        display_name="Alice",
        agent_profile_id="p-alice",
    )
    # Re-upsert with an empty display_name must not wipe the existing one.
    db.upsert_conversation_participant(
        conversation_session_id="conv",
        participant_id="m1",
        member_id="m1",
        agent_profile_id="p-alice-v2",
    )
    assert len(db.list_conversation_participants("conv")) == 1
    row = db.get_conversation_participant("conv", "m1")
    assert row["display_name"] == "Alice"
    assert row["agent_profile_id"] == "p-alice-v2"


def test_resolve_participant_for_run_priority(tmp_path: Path):
    db = SessionDB(tmp_path / "state.db")
    db.create_session("conv", source="team_mission", transient=False)
    db.upsert_conversation_participant(
        conversation_session_id="conv", participant_id="leader", role="leader",
        agent_profile_id="p-lead", runtime_scope_key="team:conv:leader-conversation",
    )
    db.upsert_conversation_participant(
        conversation_session_id="conv", participant_id="m1", role="member",
        member_id="m1", agent_profile_id="p-alice", runtime_scope_key="team:conv:member:m1",
    )

    # member_id is the most specific hint.
    assert db.resolve_participant_id_for_run("conv", member_id="m1") == "m1"
    # scope key resolves the running participant.
    assert db.resolve_participant_id_for_run(
        "conv", runtime_scope_key="team:conv:leader-conversation"
    ) == "leader"
    # profile id is unambiguous within a team.
    assert db.resolve_participant_id_for_run("conv", agent_profile_id="p-alice") == "m1"
    # nothing matches → empty, caller decides the fallback.
    assert db.resolve_participant_id_for_run("conv", agent_profile_id="ghost") == ""
    # unknown conversation → empty.
    assert db.resolve_participant_id_for_run("other", member_id="m1") == ""


# ── P0: routing invariant ────────────────────────────────────────────
def test_run_event_routes_by_stored_session_id_without_mission(tmp_path: Path):
    """A run event reaches a conversation by stored_session_id alone.

    No team_mission, no active_mission_id, no mission binding of any kind — yet
    the event is durably recorded against the conversation session and readable
    back by that session id. If this ever regresses, conversation-first routing
    is broken at the foundation.
    """
    db = SessionDB(tmp_path / "state.db")
    db.create_session("plain-conv", source="chat", transient=False)
    db.upsert_run(run_id="run-x", session_id="plain-conv", status="running")

    db.append_run_event(
        "plain-conv",
        {
            "type": "message.delta",
            "session_id": "plain-conv",
            "stored_session_id": "plain-conv",
            "run_id": "run-x",
            "turn_id": "turn-x",
            "seq": 1,
            "payload": {"text": "hi"},
        },
    )

    events = db.list_run_events("plain-conv")
    assert any(e.get("run_id") == "run-x" for e in events), events
    # The event is addressable by session id with no mission filter applied.
    by_run = db.list_run_events("plain-conv", run_id="run-x")
    assert len(by_run) >= 1
    assert all(e.get("run_id") == "run-x" for e in by_run)
