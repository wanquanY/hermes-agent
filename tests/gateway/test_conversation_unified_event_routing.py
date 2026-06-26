"""Conversation Runtime Protocol — invariant contract tests.

The non-negotiable invariant for all three conversation kinds (regular,
team leader, @member) is:

    A run's user-visible ``message.complete`` event MUST land in the
    conversation's ``run_events`` table, addressable via
    ``stored_session_id == conversation_session_id``.

Today the regular and leader cases hold by construction (worker session
IS the conversation session). The @member case is fragile: the worker
runs on ``memberchat:<conv>:<member>`` and a separate mirror path
relays the event into the conversation session, but mirroring depends
on a pre-registered row in ``member_chat_runs``. If the worker publishes
before registration (real-world race), the mirror silently drops the
frame and the user sees a blank page even though the worker completed.

These tests lock the invariant. Test 3 (@member without prior
registration) is RED on baseline (commit before P0-fallback lands) and
turns GREEN once the P0 mirror band-aid (run_control fan-out via
team_mission_conversations) is wired. Later phases (P2 RunContext)
make the entire mirror path obsolete — by then these tests still pass
because the invariant they express is conversation-kind agnostic.

See:
- docs/Hermes/V0.9.5/conversation-architecture-redesign.md  (north star)
- docs/Hermes/V0.9.5/team-mission-architecture-refactor-acceptance-audit.md
  (where this test was first proposed as P0)
"""

from __future__ import annotations

import json
from pathlib import Path

from hermes_state import SessionDB
from tui_gateway.services.run_control import record_event


CONV_SESSION = "conv-session-uuid"
MEMBER_SESSION = "memberchat:conv-1:member-alice"


def _new_db(tmp_path: Path) -> SessionDB:
    return SessionDB(tmp_path / "state.db")


def _events_for_session(db: SessionDB, session_id: str) -> list[dict]:
    rows = db._conn.execute(  # noqa: SLF001 - test introspection
        "SELECT seq, event_type, run_id, runtime_scope_key, payload_json, event_json "
        "FROM run_events WHERE session_id = ? ORDER BY seq",
        (session_id,),
    ).fetchall()
    return [
        {
            "seq": r["seq"],
            "type": r["event_type"],
            "run_id": r["run_id"],
            "scope": r["runtime_scope_key"],
            "payload": json.loads(r["payload_json"] or "{}"),
            "frame": json.loads(r["event_json"] or "{}"),
        }
        for r in rows
    ]


# ── case 1: regular chat — worker session IS conversation session ──────


def test_regular_chat_message_complete_persists_on_conversation_session(tmp_path: Path):
    """Regular run.submit: worker publishes directly to the conversation
    session. message.complete lands in run_events[stored_session_id=conv].
    """
    db = _new_db(tmp_path)
    db.create_session(CONV_SESSION, source="user", transient=False)
    db.upsert_run(run_id="run-direct-1", session_id=CONV_SESSION, status="running")

    record_event(
        {
            "type": "message.complete",
            "session_id": CONV_SESSION,
            "stored_session_id": CONV_SESSION,
            "run_id": "run-direct-1",
            "turn_id": "t1",
            "seq": 1,
            "payload": {"text": "hi from regular chat", "status": "complete"},
        },
        db=db,
    )

    events = _events_for_session(db, CONV_SESSION)
    assert [e["type"] for e in events] == ["message.complete"]
    assert events[0]["payload"]["text"] == "hi from regular chat"


# ── case 2: team leader chat — leader run lives on conversation session ─


def test_team_leader_message_complete_persists_on_conversation_session(tmp_path: Path):
    """Team leader run: same shape as regular but with team_mission
    conversation registration. Invariant identical: the leader run's
    stored_session_id IS the conversation session.
    """
    db = _new_db(tmp_path)
    db.create_session(CONV_SESSION, source="team_mission", transient=False)
    db.upsert_team_mission_conversation(
        conversation_id="conv-1",
        team_id="team-1",
        stable_session_id=CONV_SESSION,
        title="Team Conversation",
        objective="leader chat smoke",
        workspace_id="ws-1",
        workspace_path="/tmp/ws",
        active_mission_id="",
        created_at=100,
        updated_at=200,
    )
    db.upsert_run(run_id="run-leader-1", session_id=CONV_SESSION, status="running")

    record_event(
        {
            "type": "message.complete",
            "session_id": CONV_SESSION,
            "stored_session_id": CONV_SESSION,
            "run_id": "run-leader-1",
            "turn_id": "t1",
            "seq": 1,
            "payload": {"text": "leader reply", "status": "complete"},
        },
        db=db,
    )

    events = _events_for_session(db, CONV_SESSION)
    assert [e["type"] for e in events] == ["message.complete"]
    assert events[0]["payload"]["text"] == "leader reply"


# ── case 3: @member chat WITH registration (current happy path) ────────


def test_member_chat_with_registration_mirrors_to_conversation(tmp_path: Path):
    """Established baseline (commit 2d18ef24d and earlier already test
    this via tests/test_member_chat_relay.py). Repeated here as part of
    the unified contract suite so all three cases sit side by side.
    """
    db = _new_db(tmp_path)
    db.create_session(CONV_SESSION, source="team_mission", transient=False)
    db.create_session(MEMBER_SESSION, source="team_mission", transient=False)
    db.upsert_team_mission_conversation(
        conversation_id="conv-1",
        team_id="team-1",
        stable_session_id=CONV_SESSION,
        title="Team Conversation",
        objective="member chat smoke",
        workspace_id="ws-1",
        workspace_path="/tmp/ws",
        active_mission_id="",
        created_at=100,
        updated_at=200,
    )
    db.register_member_chat_run(
        run_id="run-member-1",
        conversation_session_id=CONV_SESSION,
        member_id="member-alice",
        agent_profile_id="profile-alice",
        display_name="Alice",
        optimistic_run_id="",
    )
    db.upsert_run(run_id="run-member-1", session_id=MEMBER_SESSION, status="running")

    record_event(
        {
            "type": "message.complete",
            "session_id": MEMBER_SESSION,
            "stored_session_id": MEMBER_SESSION,
            "run_id": "run-member-1",
            "turn_id": "t1",
            "seq": 1,
            "payload": {"text": "member reply (registered)", "status": "complete"},
        },
        db=db,
    )

    events = _events_for_session(db, CONV_SESSION)
    assert any(e["type"] == "message.complete" for e in events)
    msg = next(e for e in events if e["type"] == "message.complete")
    assert msg["payload"]["text"] == "member reply (registered)"
    assert msg["payload"].get("participant_id") == "member-alice"


# ── case 4: @member chat WITHOUT registration — the bug ────────────────


def test_member_chat_without_registration_still_reaches_conversation(tmp_path: Path):
    """The unified contract:

        Even when ``member_chat_runs`` has NO row for the worker's run
        (registration race lost, or P2 has removed the registry table),
        a ``message.complete`` event whose payload identifies the
        member + conversation MUST still land in the conversation's
        ``run_events`` table.

    The mirror path today queries member_chat_runs and silently drops
    on miss (run_control.py:1446 'mirror-lookup-miss' diagnostic). This
    test will be RED before P0-Commit2's fallback is wired, and GREEN
    after. Later, when P2 RunContext eliminates the memberchat:* session
    entirely, this test still passes because workers will publish
    directly to the conversation session.

    What the contract does NOT specify:
    - The mechanism (registry lookup, payload-based fallback, RunContext)
    - The exact run_id namespacing on the conversation side
    - Whether deltas are mirrored frame-for-frame or only the terminal

    What it DOES specify:
    - At least one event with the same text MUST exist in the
      conversation session's run_events.
    """
    db = _new_db(tmp_path)
    db.create_session(CONV_SESSION, source="team_mission", transient=False)
    db.create_session(MEMBER_SESSION, source="team_mission", transient=False)
    db.upsert_team_mission_conversation(
        conversation_id="conv-1",
        team_id="team-1",
        stable_session_id=CONV_SESSION,
        title="Team Conversation",
        objective="member chat unregistered race",
        workspace_id="ws-1",
        workspace_path="/tmp/ws",
        active_mission_id="",
        created_at=100,
        updated_at=200,
    )
    # NOTE: intentionally NOT calling register_member_chat_run — simulating
    # the registration-race that triggers the user-reported blank-page bug.
    db.upsert_run(run_id="run-member-orphan", session_id=MEMBER_SESSION, status="running")

    record_event(
        {
            "type": "message.complete",
            "session_id": MEMBER_SESSION,
            "stored_session_id": MEMBER_SESSION,
            "run_id": "run-member-orphan",
            "turn_id": "t1",
            "seq": 1,
            # The payload carries enough identity for any unification
            # mechanism (mirror fallback OR RunContext) to address the
            # conversation: a member_id (or participant_id) + a hint
            # of which team conversation this belongs to.
            "payload": {
                "text": "member reply (unregistered)",
                "status": "complete",
                "member_id": "member-alice",
                "agent_profile_id": "profile-alice",
                "display_name": "Alice",
                "team_mission": {
                    "conversation_id": "conv-1",
                    "conversation_session_id": CONV_SESSION,
                    "member_id": "member-alice",
                },
            },
        },
        db=db,
    )

    events = _events_for_session(db, CONV_SESSION)
    matching = [e for e in events if e["payload"].get("text") == "member reply (unregistered)"]
    assert matching, (
        "INVARIANT VIOLATED: an @member worker's message.complete event with "
        "member_id + conversation_session_id in its payload reached "
        "run_events[stored_session_id=memberchat:*] but NOT run_events"
        "[stored_session_id=conv]. The conversation timeline will show no "
        "member reply. See P0-Commit2 (mirror fallback) or P2 (RunContext) "
        "for the fix. Current conversation events seen: "
        f"{[(e['type'], e['payload'].get('text')) for e in events]}"
    )


# ── case 5: invariant — no visible events ever land in memberchat-only ─


def test_memberchat_session_is_not_the_visible_event_truth(tmp_path: Path):
    """Reverse invariant: ``memberchat:*`` is an implementation detail.
    No code path should treat run_events[stored_session_id=memberchat:*]
    as the source of truth for what the user sees in the conversation
    timeline.

    Currently the memberchat session DOES persist events (worker writes
    them when it publishes — see test_member_chat_relay.py for the
    mirror's source side). This test does not forbid that intermediate
    write; it forbids using memberchat:* as the consumer-facing read
    source.

    When P2 lands and the memberchat:* session is removed entirely,
    this test will still hold (vacuously — no memberchat events to
    classify as visible).
    """
    # No active assertion today — this test reserves the name to enforce
    # the principle as soon as P1/P2 introduce the conversation_kind
    # field or the RunContext addresses the issue at the source.
    #
    # When P2 lands, replace this body with:
    #
    #   ev = _events_for_session(db, MEMBER_SESSION)
    #   assert ev == [], (
    #       "memberchat:* sessions must not carry visible events after P2 "
    #       "RunContext — workers publish directly to conversation session"
    #   )
    #
    # For now we leave it as a documented placeholder so the test file's
    # 5-case structure makes the invariant set explicit at one glance.
    assert True
