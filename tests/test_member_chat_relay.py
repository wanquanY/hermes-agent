"""Decoupled group-chat: the run_control.record_event mirror block replays
every worker frame onto the team conversation session — broadcast +
persist + participant_id stamp — so member replies stream the same way
the leader's do."""

import json
from pathlib import Path

from hermes_state import SessionDB
from tui_gateway.services.run_control import record_event


def _setup(tmp_path: Path, *, optimistic_run_id: str = "") -> SessionDB:
    db = SessionDB(tmp_path / "state.db")
    db.create_session("conv", source="team_mission", transient=False)
    db.create_session("member-sess", source="team_mission", transient=False)
    db.register_member_chat_run(
        run_id="run-1",
        conversation_session_id="conv",
        member_id="m1",
        agent_profile_id="p1",
        display_name="Alice",
        optimistic_run_id=optimistic_run_id,
    )
    db.upsert_run(run_id="run-1", session_id="member-sess", status="running")
    return db


def _conv_events(db: SessionDB):
    rows = db._conn.execute(  # noqa: SLF001 - test introspection
        "SELECT seq, event_type, run_id, runtime_scope_key, payload_json, event_json "
        "FROM run_events WHERE session_id = ? ORDER BY seq",
        ("conv",),
    ).fetchall()
    out = []
    for r in rows:
        out.append({
            "seq": r["seq"],
            "type": r["event_type"],
            "run_id": r["run_id"],
            "scope": r["runtime_scope_key"],
            "payload": json.loads(r["payload_json"] or "{}"),
            "frame": json.loads(r["event_json"] or "{}"),
        })
    return out


def _publish(db: SessionDB, *, seq: int, ev_type: str, payload, runtime_scope_key: str = ""):
    frame = {
        "type": ev_type,
        "session_id": "member-sess",
        "stored_session_id": "member-sess",
        "run_id": "run-1",
        "turn_id": "t1",
        "seq": seq,
        "payload": payload,
    }
    if runtime_scope_key:
        frame["runtime_scope_key"] = runtime_scope_key
    record_event(frame, db=db)


# ── architecture: full frame-for-frame mirror (NOT terminal-only) ───
def test_every_worker_frame_mirrors_into_conversation(tmp_path: Path):
    db = _setup(tmp_path)
    _publish(db, seq=1, ev_type="reasoning.delta", payload={"text": "thinking"})
    _publish(db, seq=2, ev_type="message.delta", payload={"delta": "你", "mode": "append"})
    _publish(db, seq=3, ev_type="message.delta", payload={"delta": "好", "mode": "append"})
    _publish(db, seq=4, ev_type="message.complete", payload={"text": "你好", "status": "complete"})

    mirrored = _conv_events(db)
    # Reasoning + every delta + the terminal frame all surface on the conversation.
    assert [e["type"] for e in mirrored] == [
        "reasoning.delta", "message.delta", "message.delta", "message.complete",
    ]
    # Conversation seq is a fresh contiguous domain (not the worker's).
    assert [e["seq"] for e in mirrored] == [1, 2, 3, 4]
    # Namespaced run_id avoids colliding with leader runs in the same conversation.
    assert all(e["run_id"] == "member-chat:run-1" for e in mirrored)
    # P1: participant_id stamped at publish on both frame and payload.
    assert all(e["frame"].get("participant_id") == "m1" for e in mirrored)
    assert all(e["payload"].get("participant_id") == "m1" for e in mirrored)
    # Source pointers carried so debugging / future joins can trace back.
    assert all(e["payload"].get("source_run_id") == "run-1" for e in mirrored)
    assert all(e["payload"].get("source_session_id") == "member-sess" for e in mirrored)


def test_message_complete_appends_assistant_message_with_member_identity(tmp_path: Path):
    db = _setup(tmp_path)
    _publish(db, seq=1, ev_type="message.complete", payload={"text": "你好，我是 Alice", "status": "complete"})
    relayed = [m for m in db.get_messages("conv") if "你好，我是 Alice" in str(m.get("content") or "")]
    assert relayed
    meta = relayed[0].get("metadata")
    if isinstance(meta, str):
        meta = json.loads(meta)
    assert (meta or {}).get("team_mission", {}).get("member_id") == "m1"
    assert (meta or {}).get("team_mission", {}).get("display_name") == "Alice"


def test_streaming_deltas_do_not_append_assistant_messages(tmp_path: Path):
    """The messages table is the durable transcript — assistant rows only land
    on the terminal frame. Streamed deltas live in run_events only."""
    db = _setup(tmp_path)
    _publish(db, seq=1, ev_type="message.delta", payload={"delta": "你", "mode": "append"})
    _publish(db, seq=2, ev_type="message.delta", payload={"delta": "好", "mode": "append"})
    assert db.get_messages("conv") == []


def test_unregistered_run_is_not_mirrored(tmp_path: Path):
    db = SessionDB(tmp_path / "state.db")
    db.create_session("conv", source="team_mission", transient=False)
    db.create_session("worker-sess", source="team_mission", transient=False)
    db.upsert_run(run_id="run-x", session_id="worker-sess", status="running")
    record_event(
        {
            "type": "message.complete",
            "session_id": "worker-sess",
            "stored_session_id": "worker-sess",
            "run_id": "run-x",
            "turn_id": "t1",
            "seq": 1,
            "payload": {"text": "not a member chat", "status": "complete"},
        },
        db=db,
    )
    rows = db._conn.execute(  # noqa: SLF001
        "SELECT 1 FROM run_events WHERE session_id = 'conv'"
    ).fetchall()
    assert rows == []


def test_mirror_stamps_optimistic_run_id_so_frontend_settles(tmp_path: Path):
    """When register_member_chat_run records the frontend's pre-reserved
    optimistic run_id, mirrored frames carry that id (not a namespaced
    member-chat:<src>) so the frontend's existing "运行中" indicator on the
    conversation settles the moment terminal arrives."""
    db = _setup(tmp_path, optimistic_run_id="team-leader-run-optimistic")
    _publish(db, seq=1, ev_type="message.complete", payload={"text": "你好", "status": "complete"})
    mirrored = _conv_events(db)
    assert mirrored, "no mirror produced"
    assert all(e["run_id"] == "team-leader-run-optimistic" for e in mirrored), [e["run_id"] for e in mirrored]


def test_mirror_preserves_full_member_chat_runtime_scope_for_subscriptions(tmp_path: Path):
    """The frontend subscribes to the full worker scope
    member-chat:<conversation_id>:<member_id>. Mirrored conversation frames must
    carry the same scope; shortening it to member-chat:<member_id> makes scoped
    subscriptions drop every live frame."""
    db = _setup(tmp_path, optimistic_run_id="team-member-run-optimistic")
    full_scope = "member-chat:team-conversation-1:m1"

    _publish(
        db,
        seq=1,
        ev_type="message.delta",
        payload={"delta": "你", "mode": "append", "runtime_scope_key": full_scope},
        runtime_scope_key=full_scope,
    )

    mirrored = _conv_events(db)
    assert mirrored
    assert all(e["scope"] == full_scope for e in mirrored), [e["scope"] for e in mirrored]
    assert all(e["payload"].get("runtime_scope_key") == full_scope for e in mirrored)
    assert all(e["payload"].get("source_runtime_scope_key") == full_scope for e in mirrored)


def test_mirror_falls_back_to_namespaced_id_when_no_optimistic(tmp_path: Path):
    """No optimistic id (e.g. legacy backend call) → namespaced fallback."""
    db = _setup(tmp_path)  # default: optimistic_run_id=""
    _publish(db, seq=1, ev_type="message.complete", payload={"text": "你好", "status": "complete"})
    mirrored = _conv_events(db)
    assert mirrored
    assert all(e["run_id"] == "member-chat:run-1" for e in mirrored)


def test_project_messages_for_viewer_leader_perspective():
    """Leader hydrating a multi-participant conversation: its own past
    assistant turns stay assistant; members' mirrored replies become
    observed user-side speech with a speaker prefix."""
    from hermes_state_member_chat import project_messages_for_viewer

    msgs = [
        {"id": "1", "role": "user", "content": "用户的问题"},
        # leader's own past turn — no member_id, no member_chat kind
        {"id": "2", "role": "assistant", "content": "我以 leader 身份处理",
         "metadata": {"team_mission": {"kind": "leader_conversation"}}},
        # Bob (a member) mirrored reply
        {"id": "3", "role": "assistant", "content": "我是 Hermes Agent",
         "metadata": {"team_mission": {"kind": "member_chat", "member_id": "m-bob",
                                        "display_name": "Bob"}}},
        # in-flight @-request targeting Bob — should be visible to LEADER
        # (it's not directed at the leader)
        {"id": "4", "role": "user", "content": "@Bob 帮我",
         "metadata": {"team_mission": {"kind": "member_chat_user",
                                        "target_member_id": "m-bob"}}},
    ]
    view = project_messages_for_viewer(msgs, "leader")
    assert [(m["role"], m["content"]) for m in view] == [
        ("user", "用户的问题"),
        ("assistant", "我以 leader 身份处理"),
        ("user", "[Bob 在群聊里说] 我是 Hermes Agent"),
        ("user", "@Bob 帮我"),
    ]


def test_project_messages_for_viewer_member_perspective():
    """Same conversation, from Bob's perspective: HIS own past replies are
    assistant, leader and others are observed user-side speech, his own
    in-flight request is suppressed (worker will handle via run.submit)."""
    from hermes_state_member_chat import project_messages_for_viewer

    msgs = [
        {"id": "1", "role": "user", "content": "用户的问题"},
        {"id": "2", "role": "assistant", "content": "我以 leader 身份处理",
         "metadata": {"team_mission": {"kind": "leader_conversation",
                                        "display_name": "小多"}}},
        {"id": "3", "role": "assistant", "content": "Bob 的回答",
         "metadata": {"team_mission": {"kind": "member_chat", "member_id": "m-bob",
                                        "display_name": "Bob"}}},
        {"id": "4", "role": "user", "content": "@Bob 帮我",
         "metadata": {"team_mission": {"kind": "member_chat_user",
                                        "target_member_id": "m-bob"}}},
    ]
    view = project_messages_for_viewer(msgs, "m-bob")
    # leader → user with prefix; bob's own mirror → suppressed (worker
    # has it); in-flight @-request to bob → suppressed (worker handles).
    assert [(m["role"], m["content"]) for m in view] == [
        ("user", "用户的问题"),
        ("user", "[小多 在群聊里说] 我以 leader 身份处理"),
    ]


def test_sync_member_chat_conversation_view_role_remap(tmp_path: Path):
    """The conv→member-chat view: other speakers become observed user-side
    speech (so the LLM doesn't mimic them), the member's own past replies
    stay as real assistant turns, and the in-flight user request + own
    member-chat mirror are skipped (the worker handles those locally)."""
    db = SessionDB(tmp_path / "state.db")
    db.create_session("conv", source="team_mission", transient=False)
    db.create_session("memberchat:conv:m-alice", source="team_mission_member_chat", transient=False)

    # Source conv transcript:
    #  - regular user msg                                     → mirror as user
    #  - leader assistant msg (no member_id)                  → user "[Leader] ..."
    #  - Bob (a different member)'s mirrored reply            → user "[Bob] ..."
    #  - Alice's own mirrored reply (kind=member_chat,m=alice)→ skip (worker has it)
    #  - in-flight user @-request targeting Alice             → skip (run.submit text)
    #  - earlier Alice direct assistant turn (member_id=alice)→ assistant
    db.append_message("conv", role="user", content="嗨大家好")
    db.append_message("conv", role="assistant", content="我是 leader 小多,需要什么帮助?",
                      metadata={"team_mission": {"display_name": "小多", "kind": "leader"}})
    db.append_message("conv", role="assistant", content="我是 Hermes Agent — Bob's wrong reply",
                      metadata={"team_mission": {"kind": "member_chat", "member_id": "m-bob",
                                                  "display_name": "Bob"}})
    db.append_message("conv", role="assistant", content="(alice's own mirror — already on worker)",
                      metadata={"team_mission": {"kind": "member_chat", "member_id": "m-alice",
                                                  "display_name": "Alice"}})
    db.append_message("conv", role="user", content="@Alice 现在的问题是什么",
                      metadata={"team_mission": {"kind": "member_chat_user",
                                                  "target_member_id": "m-alice"}})
    db.append_message("conv", role="assistant", content="(alice's older direct turn)",
                      metadata={"team_mission": {"member_id": "m-alice",
                                                  "display_name": "Alice"}})

    appended = db.sync_member_chat_conversation_view(
        conversation_session_id="conv",
        member_chat_session_id="memberchat:conv:m-alice",
        member_id="m-alice",
    )
    msgs = db.get_messages("memberchat:conv:m-alice")
    rendered = [(m.get("role"), str(m.get("content") or "")) for m in msgs]
    assert appended == 4, rendered  # user + leader + bob + alice-direct
    assert ("user", "嗨大家好") in rendered
    assert ("user", "[小多 在群聊里说] 我是 leader 小多,需要什么帮助?") in rendered
    assert ("user", "[Bob 在群聊里说] 我是 Hermes Agent — Bob's wrong reply") in rendered
    assert ("assistant", "(alice's older direct turn)") in rendered
    # In-flight request + Alice's own mirror are NOT mirrored
    assert not any("@Alice 现在的问题是什么" in c for _, c in rendered)
    assert not any("alice's own mirror" in c for _, c in rendered)


def test_sync_member_chat_conversation_view_is_idempotent(tmp_path: Path):
    """Re-syncing only appends NEW source messages — no duplication."""
    db = SessionDB(tmp_path / "state.db")
    db.create_session("conv", source="team_mission", transient=False)
    db.create_session("memberchat:conv:m-alice", source="team_mission_member_chat", transient=False)
    db.append_message("conv", role="user", content="第一条")

    first = db.sync_member_chat_conversation_view(
        conversation_session_id="conv",
        member_chat_session_id="memberchat:conv:m-alice",
        member_id="m-alice",
    )
    assert first == 1

    # Re-run with no new source messages → 0 appended
    second = db.sync_member_chat_conversation_view(
        conversation_session_id="conv",
        member_chat_session_id="memberchat:conv:m-alice",
        member_id="m-alice",
    )
    assert second == 0
    assert len(db.get_messages("memberchat:conv:m-alice")) == 1

    # Add a new source message → only it gets mirrored
    db.append_message("conv", role="user", content="第二条")
    third = db.sync_member_chat_conversation_view(
        conversation_session_id="conv",
        member_chat_session_id="memberchat:conv:m-alice",
        member_id="m-alice",
    )
    assert third == 1
    assert len(db.get_messages("memberchat:conv:m-alice")) == 2


def test_mirror_does_not_recurse(tmp_path: Path):
    """A mirrored frame must NOT re-enter the mirror block — only ONE mirror
    per source frame, no infinite loop."""
    db = _setup(tmp_path)
    _publish(db, seq=1, ev_type="message.complete", payload={"text": "hi", "status": "complete"})
    mirrored = _conv_events(db)
    # Exactly one mirror per source frame; no exponential blow-up.
    assert len(mirrored) == 1


def test_viewer_projection_preserves_own_tool_calls_and_tool_responses():
    """REGRESSION (bug found 2026-06-25): the earlier viewer projection
    dropped every ``tool`` row AND every assistant row whose content was
    empty, which silently severed tool_call → tool_response pairing in
    the leader's hydrated history. The model then saw unmatched
    tool_calls and fell back to emitting the call as raw text (user
    saw ``clarify({...})`` printed instead of an actual tool invocation).
    Pin the correct behavior: the viewer's own assistant turn (empty
    content + tool_calls) and the paired tool response BOTH survive."""
    from hermes_state_member_chat import project_messages_for_viewer, LEADER_PARTICIPANT_ID

    messages = [
        {"role": "user", "content": "请调用 clarify 工具"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "call_1", "function": {"name": "clarify", "arguments": "{}"}}],
            "metadata": {},
        },
        {"role": "tool", "tool_call_id": "call_1", "content": "{\"ok\": true}", "metadata": {}},
        {"role": "assistant", "content": "好的,我已经调用了 clarify 工具。", "metadata": {}},
    ]
    out = project_messages_for_viewer(messages, LEADER_PARTICIPANT_ID)
    assert [m["role"] for m in out] == ["user", "assistant", "tool", "assistant"]
    assert out[1]["tool_calls"][0]["id"] == "call_1"
    assert out[1]["content"] == ""
    assert out[2]["tool_call_id"] == "call_1"
    assert out[3]["content"].startswith("好的")


def test_viewer_projection_drops_other_speakers_tool_messages():
    """Symmetric guarantee: a member's tool-call turn that mirrored into the
    conv must NOT bring its tool responses into the leader's view — the
    leader never invoked those calls; surfacing the responses would
    confuse the model. The member's user-facing reply IS reflected as
    ``[<speaker> 在群聊里说] ...``."""
    from hermes_state_member_chat import project_messages_for_viewer, LEADER_PARTICIPANT_ID

    messages = [
        {"role": "user", "content": "@Bob 帮我跑工具"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "call_b", "function": {"name": "search", "arguments": "{}"}}],
            "metadata": {"team_mission": {"kind": "member_chat", "member_id": "bob", "display_name": "Bob"}},
        },
        {"role": "tool", "tool_call_id": "call_b", "content": "{}",
         "metadata": {"team_mission": {"kind": "member_chat", "member_id": "bob"}}},
        {"role": "assistant", "content": "搞定了!",
         "metadata": {"team_mission": {"kind": "member_chat", "member_id": "bob", "display_name": "Bob"}}},
        {"role": "assistant", "content": "感谢 Bob。", "metadata": {}},
    ]
    out = project_messages_for_viewer(messages, LEADER_PARTICIPANT_ID)
    assert [m["role"] for m in out] == ["user", "user", "assistant"]
    assert out[1]["content"] == "[Bob 在群聊里说] 搞定了!"
    assert "tool_calls" not in out[1]
    assert out[2]["content"] == "感谢 Bob。"


def test_viewer_projection_is_noop_on_already_materialized_view_rows():
    """Worker view sessions are materialized at WRITE time (the worker
    reads its own ``memberchat:`` session, not the conv). Those rows
    carry ``metadata.member_chat_view`` and must NEVER be re-projected at
    read time — re-projection would prefix-wrap already-prefixed user
    speech (``[X 说] [X 说] foo``) or drop the worker's own assistant
    turns. Pin the read-time no-op."""
    from hermes_state_member_chat import project_messages_for_viewer

    materialized = [
        {"role": "user", "content": "@Bob 跑一下", "metadata": {"member_chat_view": {"source_message_id": "1"}}},
        {"role": "user", "content": "[Leader 在群聊里说] 加油",
         "metadata": {"member_chat_view": {"source_message_id": "2"}}},
        {"role": "assistant", "content": "好的", "metadata": {"member_chat_view": {"source_message_id": "3"}}},
    ]
    out = project_messages_for_viewer(materialized, "bob")
    # Every row is dropped because member_chat_view rows should NEVER be
    # re-projected. Worker hydration code is expected to feed view rows
    # straight through, not via this helper; if it accidentally does,
    # the safe behavior is "produce nothing extra" (the worker's own
    # session.list already gives it the same rows).
    assert out == []
