"""Conversation Runtime Protocol — invariant contract tests.

The non-negotiable invariant for all three conversation kinds (regular,
team leader, @member) is:

    A run's user-visible ``message.complete`` event MUST land in the
    conversation's ``run_events`` table, addressable via
    ``conversation_session_id == conversation_session_id``.

Regular, leader, and @member workers now publish user-visible events
to the conversation session through RunContext + conversation-session
storage instead of ``memberchat:*`` worker sessions or
legacy run registries.

See:
- docs/Hermes/V0.9.5/conversation-architecture-redesign.md  (north star)
- docs/Hermes/V0.9.5/team-mission-architecture-refactor-acceptance-audit.md
  (where this test was first proposed as P0)
"""

from __future__ import annotations

import json
from pathlib import Path

from hermes_state import SessionDB
from hermes_team_mission.domain.run_context import RunContext
from hermes_team_mission.gateway import runtime_methods
from hermes_state_participants import member_participant_id
from tui_gateway.services.run_control import record_event


CONV_SESSION = "conv-session-uuid"
MEMBER_SESSION = "memberchat:conv-1:member-alice"


def _member_run_context() -> RunContext:
    return RunContext(
        conversation_session_id=CONV_SESSION,
        participant_id=member_participant_id("member-alice"),
        activity_id="act-member_chat:conv-1:member-alice",
        activity_kind="member_chat",
        execution_scope_key="member-chat:conv-1:member-alice",
        control_home="/tmp/hermes-member-alice",
        execution_home="/tmp/hermes-member-alice",
    )


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


def _memberchat_run_events(db: SessionDB) -> list[dict]:
    rows = db._conn.execute(  # noqa: SLF001 - test introspection
        "SELECT event_json FROM run_events WHERE session_id LIKE 'memberchat:%' ORDER BY seq"
    ).fetchall()
    return [json.loads(r["event_json"] or "{}") for r in rows]


def _submit_member_once(monkeypatch, tmp_path: Path, db: SessionDB) -> dict:
    captured: dict = {}

    def fake_proxy_run_submit(params: dict) -> dict:
        captured.update(params)
        return {"ok": True}

    monkeypatch.setattr(runtime_methods, "_proxy_run_submit_via_worker", fake_proxy_run_submit)
    mission = {
        "mission_id": "mission-1",
        "team_id": "team-1",
        "workspace_path": str(tmp_path),
        "metadata": {
            "members": [
                {
                    "member_id": "member-alice",
                    "agent_profile_id": "profile-alice",
                    "role": "member",
                    "profile_name": "Alice",
                    "dovie_profile": {
                        "id": "profile-alice",
                        "hermesHomePath": str(tmp_path / "alice-home"),
                    },
                }
            ]
        },
    }
    params = {
        "team_id": "team-1",
        "conversation_id": "conv-1",
        "conversation_session_id": CONV_SESSION,
        "client_run_id": "optimistic-member-run",
        "turn_id": "turn-member-1",
        "cwd": str(tmp_path),
        "workspace": {"id": "ws-1", "path": str(tmp_path), "kind": "local"},
    }
    response = runtime_methods._submit_message_to_member(
        "rid-member",
        params,
        db=db,
        target_member_id="member-alice",
        conversation_id="conv-1",
        conversation_session_id=CONV_SESSION,
        mission=mission,
        text="@Alice please check this",
    )

    assert "error" not in response, response
    assert captured
    return captured


# ── case 1: regular chat — worker session IS conversation session ──────


def test_regular_chat_message_complete_persists_on_conversation_session(tmp_path: Path):
    """Regular run.submit: worker publishes directly to the conversation
    session. message.complete lands in run_events[conversation_session_id=conv].
    """
    db = _new_db(tmp_path)
    db.create_session(CONV_SESSION, source="user", transient=False)
    db.upsert_run(run_id="run-direct-1", session_id=CONV_SESSION, status="running")

    record_event(
        {
            "type": "message.complete",
            "session_id": CONV_SESSION,
            "conversation_session_id": CONV_SESSION,
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
    conversation_session_id IS the conversation session.
    """
    db = _new_db(tmp_path)
    db.create_session(CONV_SESSION, source="team_mission", transient=False)
    db.upsert_team_mission_conversation(
        conversation_id="conv-1",
        team_id="team-1",
        conversation_session_id=CONV_SESSION,
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
            "conversation_session_id": CONV_SESSION,
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


# ── case 3: @member chat with legacy session hints ──────────────────────


def test_member_chat_with_legacy_session_hints_routes_by_run_context(tmp_path: Path):
    """Legacy memberchat session hints must not be required for delivery.

    P5 removes the deprecated registry table. RunContext remains the routing
    source of truth and stores the event directly in the conversation session
    even if an old worker frame still carries memberchat session ids.
    """
    db = _new_db(tmp_path)
    db.create_session(CONV_SESSION, source="team_mission", transient=False)
    db.create_session(MEMBER_SESSION, source="team_mission", transient=False)
    db.upsert_team_mission_conversation(
        conversation_id="conv-1",
        team_id="team-1",
        conversation_session_id=CONV_SESSION,
        title="Team Conversation",
        objective="member chat smoke",
        workspace_id="ws-1",
        workspace_path="/tmp/ws",
        active_mission_id="",
        created_at=100,
        updated_at=200,
    )
    db.upsert_run(run_id="run-member-1", session_id=MEMBER_SESSION, status="running")

    record_event(
        {
            "type": "message.complete",
            "session_id": MEMBER_SESSION,
            "conversation_session_id": MEMBER_SESSION,
            "run_id": "run-member-1",
            "turn_id": "t1",
            "seq": 1,
            "payload": {"text": "member reply (legacy hints)", "status": "complete"},
        },
        db=db,
        run_context=_member_run_context(),
    )

    events = _events_for_session(db, CONV_SESSION)
    assert any(e["type"] == "message.complete" for e in events)
    msg = next(e for e in events if e["type"] == "message.complete")
    assert msg["payload"]["text"] == "member reply (legacy hints)"
    assert msg["frame"].get("participant_id") == member_participant_id("member-alice")
    assert msg["payload"]["run_context"]["participant_id"] == member_participant_id("member-alice")


# ── case 4: @member chat WITHOUT registration — the bug ────────────────


def test_member_chat_without_registration_still_reaches_conversation(tmp_path: Path):
    """The unified contract:

        Even when ``member_chat_runs`` has NO row for the worker's run
        (the new submit path no longer writes one), a member worker's
        ``message.complete`` event MUST still land in the conversation's
        ``run_events`` table.

    PR-C makes this naturally true: the worker runs on the conversation
    session and record_event applies RunContext before persistence. This
    test depends on direct RunContext routing rather than legacy synthesis.

    What the contract does NOT specify:
    - The mechanism (registry lookup, payload inspection, RunContext)
    - The exact run_id namespacing on the conversation side
    - Whether deltas or only terminal frames are published

    What it DOES specify:
    - At least one event with the same text MUST exist in the
      conversation session's run_events.
    """
    db = _new_db(tmp_path)
    db.create_session(CONV_SESSION, source="team_mission", transient=False)
    db.upsert_team_mission_conversation(
        conversation_id="conv-1",
        team_id="team-1",
        conversation_session_id=CONV_SESSION,
        title="Team Conversation",
        objective="member chat unregistered race",
        workspace_id="ws-1",
        workspace_path="/tmp/ws",
        active_mission_id="",
        created_at=100,
        updated_at=200,
    )
    # NOTE: intentionally NOT calling register_member_chat_run. PR-C routes by
    # RunContext + conversation-session storage, so there is no registry row.
    db.upsert_run(run_id="run-member-orphan", session_id=CONV_SESSION, status="running")

    record_event(
        {
            "type": "message.complete",
            "session_id": CONV_SESSION,
            "conversation_session_id": CONV_SESSION,
            "run_id": "run-member-orphan",
            "turn_id": "t1",
            "seq": 1,
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
        run_context=_member_run_context(),
    )

    events = _events_for_session(db, CONV_SESSION)
    matching = [e for e in events if e["payload"].get("text") == "member reply (unregistered)"]
    assert matching, (
        "INVARIANT VIOLATED: an @member worker's message.complete event with "
        "member_id + conversation_session_id in its payload reached "
        "run_events[conversation_session_id=conv]. The conversation timeline will "
        "show no member reply. PR-C expects RunContext direct routing, not "
        "member_chat_runs or memberchat:* mirror state. Current conversation "
        "events seen: "
        f"{[(e['type'], e['payload'].get('text')) for e in events]}"
    )


def test_member_reply_leading_known_speaker_prefix_is_stripped_before_canonical_write(
    tmp_path: Path,
):
    db = _new_db(tmp_path)
    db.create_session(CONV_SESSION, source="team_mission", transient=False)
    db.upsert_team_mission_conversation(
        conversation_id="conv-1",
        team_id="team-1",
        conversation_session_id=CONV_SESSION,
        title="Team Conversation",
        objective="member chat prefix stripping",
        workspace_id="ws-1",
        workspace_path="/tmp/ws",
        active_mission_id="",
        created_at=100,
        updated_at=200,
    )
    db.upsert_conversation_participant(
        conversation_session_id=CONV_SESSION,
        participant_id="leader:conv-1",
        role="leader",
        display_name="小多",
    )
    db.upsert_conversation_participant(
        conversation_session_id=CONV_SESSION,
        participant_id=member_participant_id("member-alice"),
        role="member",
        display_name="Alice",
    )
    db.upsert_run(run_id="run-member-prefix", session_id=CONV_SESSION, status="running")

    record_event(
        {
            "type": "message.complete",
            "session_id": CONV_SESSION,
            "conversation_session_id": CONV_SESSION,
            "run_id": "run-member-prefix",
            "turn_id": "t1",
            "seq": 1,
            "payload": {"text": "[小多] 你好", "status": "complete"},
        },
        db=db,
        run_context=_member_run_context(),
    )

    messages = db.get_messages(CONV_SESSION)
    assert len(messages) == 1
    assert messages[0]["content"] == "你好"
    assert messages[0]["metadata"]["stripped_speaker_prefix"] == "小多"
    events = _events_for_session(db, CONV_SESSION)
    assert events[-1]["payload"]["text"] == "你好"
    assert events[-1]["payload"]["stripped_speaker_prefix"] == "小多"


def test_member_reply_non_leading_known_speaker_prefix_is_not_stripped(tmp_path: Path):
    db = _new_db(tmp_path)
    db.create_session(CONV_SESSION, source="team_mission", transient=False)
    db.upsert_team_mission_conversation(
        conversation_id="conv-1",
        team_id="team-1",
        conversation_session_id=CONV_SESSION,
        title="Team Conversation",
        objective="member chat prefix non-leading",
        workspace_id="ws-1",
        workspace_path="/tmp/ws",
        active_mission_id="",
        created_at=100,
        updated_at=200,
    )
    db.upsert_conversation_participant(
        conversation_session_id=CONV_SESSION,
        participant_id="leader:conv-1",
        role="leader",
        display_name="小多",
    )
    db.upsert_conversation_participant(
        conversation_session_id=CONV_SESSION,
        participant_id=member_participant_id("member-alice"),
        role="member",
        display_name="Alice",
    )
    db.upsert_run(run_id="run-member-prefix-mid", session_id=CONV_SESSION, status="running")

    record_event(
        {
            "type": "message.complete",
            "session_id": CONV_SESSION,
            "conversation_session_id": CONV_SESSION,
            "run_id": "run-member-prefix-mid",
            "turn_id": "t1",
            "seq": 1,
            "payload": {"text": "他说 [小多] 你好", "status": "complete"},
        },
        db=db,
        run_context=_member_run_context(),
    )

    messages = db.get_messages(CONV_SESSION)
    assert len(messages) == 1
    assert messages[0]["content"] == "他说 [小多] 你好"
    assert "stripped_speaker_prefix" not in messages[0]["metadata"]


# ── case 5: invariant — no visible events ever land in memberchat-only ─


def test_memberchat_session_is_not_the_visible_event_truth(monkeypatch, tmp_path: Path):
    """Reverse invariant: ``memberchat:*`` is an implementation detail.
    No code path should treat run_events[conversation_session_id=memberchat:*]
    as the source of truth for what the user sees in the conversation
    timeline.

    PR-C removes memberchat:* from the new submit path entirely: the worker
    spawn payload stores the conversation session id, and RunContext keeps
    worker events addressed to that same conversation.
    """
    db = _new_db(tmp_path)
    captured = _submit_member_once(monkeypatch, tmp_path, db)
    run_context = RunContext.from_payload(captured["run_context_json"])

    for seq, frame in enumerate(
        [
            {
                "type": "message.delta",
                "payload": {"delta": "hello", "mode": "append"},
            },
            {
                "type": "message.complete",
                "payload": {"text": "member reply from PR-C path", "status": "complete"},
            },
        ],
        start=1,
    ):
        record_event(
            {
                **frame,
                "session_id": captured["session_id"],
                "conversation_session_id": captured["conversation_session_id"],
                "run_id": captured["run_id"],
                "turn_id": captured["turn_id"],
                "seq": seq,
            },
            db=db,
            run_context=run_context,
        )

    assert _memberchat_run_events(db) == []
    conv_events = _events_for_session(db, CONV_SESSION)
    assert [event["type"] for event in conv_events] == ["message.delta", "message.complete"]
    assert {event["frame"]["conversation_session_id"] for event in conv_events} == {CONV_SESSION}
    assert conv_events[-1]["payload"]["text"] == "member reply from PR-C path"
