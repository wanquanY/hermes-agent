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
from tui_gateway.services.run_control import record_event


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


# ── P1: auto-population from team membership ─────────────────────────
def test_team_conversation_populates_participants_from_team_membership(tmp_path: Path):
    """ensure_team_mission_conversation upserts user/leader/members into the
    participant table so the publish path has stable identities to stamp."""
    db = SessionDB(tmp_path / "state.db")
    # Build a team with one leader + one worker member.
    db.upsert_agent_team(team_id="team-1", name="Team", description="")
    db.upsert_agent_team_member(
        member_id="m-lead", team_id="team-1", agent_profile_id="p-lead",
        role="lead", profile_name="Lead",
    )
    db.upsert_agent_team_member(
        member_id="m-alice", team_id="team-1", agent_profile_id="p-alice",
        role="member", profile_name="Alice",
    )

    db.ensure_team_mission_conversation(
        conversation_id="conv-1",
        stable_session_id="team-session-1",
        team_id="team-1",
        title="Team Conversation",
    )

    participants = db.list_conversation_participants("team-session-1")
    by_id = {p["participant_id"]: p for p in participants}
    assert set(by_id.keys()) == {"user", "leader", "m-alice"}
    assert by_id["leader"]["role"] == "leader"
    assert by_id["leader"]["runtime_scope_key"] == "team:conv-1:leader-conversation"
    assert by_id["leader"]["display_name"] == "Lead"
    assert by_id["m-alice"]["role"] == "member"
    assert by_id["m-alice"]["agent_profile_id"] == "p-alice"
    assert by_id["m-alice"]["runtime_scope_key"] == "profile:p-alice"
    assert by_id["m-alice"]["display_name"] == "Alice"

    # Resolver: leader scope key → leader; alice's profile id → m-alice.
    assert db.resolve_participant_id_for_run(
        "team-session-1", runtime_scope_key="team:conv-1:leader-conversation"
    ) == "leader"
    assert db.resolve_participant_id_for_run(
        "team-session-1", agent_profile_id="p-alice"
    ) == "m-alice"


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


# ── P1: publish-time participant_id stamping ─────────────────────────
def test_record_event_stamps_participant_id_for_leader(tmp_path: Path):
    """record_event resolves participant_id from runtime_scope_key/profile and
    stamps it onto both the persisted frame and the payload. The frontend
    speaker resolver will read this directly."""
    db = SessionDB(tmp_path / "state.db")
    db.upsert_agent_team(team_id="team-1", name="Team")
    db.upsert_agent_team_member(
        member_id="m-lead", team_id="team-1", agent_profile_id="p-lead",
        role="lead", profile_name="Lead",
    )
    db.upsert_agent_team_member(
        member_id="m-alice", team_id="team-1", agent_profile_id="p-alice",
        role="member", profile_name="Alice",
    )
    db.ensure_team_mission_conversation(
        conversation_id="conv-1", stable_session_id="team-session-1",
        team_id="team-1", title="T",
    )
    db.upsert_run(run_id="run-leader", session_id="team-session-1", status="running")

    record_event(
        {
            "type": "message.delta",
            "session_id": "team-session-1",
            "stored_session_id": "team-session-1",
            "run_id": "run-leader",
            "turn_id": "t1",
            "seq": 1,
            "payload": {
                "text": "hi",
                "runtime_scope_key": "team:conv-1:leader-conversation",
            },
        },
        db=db,
    )

    events = db.list_run_events("team-session-1", run_id="run-leader")
    assert events, "event was not persisted"
    stored = events[0]
    assert stored.get("participant_id") == "leader"
    assert (stored.get("payload") or {}).get("participant_id") == "leader"


def test_record_event_stamps_participant_id_for_member_by_profile(tmp_path: Path):
    """A member run identifies its speaker through agent_profile_id alone
    (member_chat runs publish under the member's profile-default scope)."""
    db = SessionDB(tmp_path / "state.db")
    db.upsert_agent_team(team_id="team-1", name="Team")
    db.upsert_agent_team_member(
        member_id="m-alice", team_id="team-1", agent_profile_id="p-alice",
        role="member", profile_name="Alice",
    )
    db.ensure_team_mission_conversation(
        conversation_id="conv-1", stable_session_id="team-session-1",
        team_id="team-1", title="T",
    )
    db.upsert_run(run_id="run-alice", session_id="team-session-1", status="running")

    record_event(
        {
            "type": "message.delta",
            "session_id": "team-session-1",
            "stored_session_id": "team-session-1",
            "run_id": "run-alice",
            "turn_id": "t1",
            "seq": 1,
            "payload": {
                "text": "hello from alice",
                "agent_profile_id": "p-alice",
            },
        },
        db=db,
    )

    events = db.list_run_events("team-session-1", run_id="run-alice")
    assert events
    assert events[0].get("participant_id") == "m-alice"


def test_record_event_with_no_hints_does_not_stamp(tmp_path: Path):
    """No identity hints in the frame → no stamping, defensive fallback path
    stays open. Lookup misses must never block event delivery."""
    db = SessionDB(tmp_path / "state.db")
    db.create_session("plain-conv", source="chat", transient=False)
    db.upsert_run(run_id="run-y", session_id="plain-conv", status="running")

    record_event(
        {
            "type": "message.delta",
            "session_id": "plain-conv",
            "stored_session_id": "plain-conv",
            "run_id": "run-y",
            "turn_id": "t1",
            "seq": 1,
            "payload": {"text": "x"},
        },
        db=db,
    )

    events = db.list_run_events("plain-conv", run_id="run-y")
    assert events
    assert not events[0].get("participant_id")
