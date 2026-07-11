"""Member-chat transcript projection and materialized view behavior.

The old relay/mirror event path was removed in P2-PR-E. These tests keep the
remaining non-mirror coverage for viewer-specific transcript projection and
member-chat view synchronization.
"""

from pathlib import Path

from hermes_agent.storage.cli_session_store import CliSessionStore, open_cli_session_store


def test_project_messages_for_viewer_leader_perspective():
    """Leader hydrating a multi-participant conversation: its own past
    assistant turns stay assistant; members' replies become observed
    user-side speech with a speaker prefix."""
    from hermes_team_mission.domain.member_chat_projection import project_messages_for_viewer

    msgs = [
        {"id": "1", "role": "user", "content": "用户的问题"},
        # leader's own past turn - no member_id, no member_chat kind
        {"id": "2", "role": "assistant", "content": "我以 leader 身份处理",
         "metadata": {"team_mission": {"kind": "leader_conversation"}}},
        # Bob (a member) reply in the shared conversation
        {"id": "3", "role": "assistant", "content": "我是 Hermes Agent",
         "metadata": {"team_mission": {"kind": "member_chat", "member_id": "m-bob",
                                        "display_name": "Bob"}}},
        # in-flight @-request targeting Bob - should be visible to LEADER
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
    from hermes_team_mission.domain.member_chat_projection import project_messages_for_viewer

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
    # leader -> user with prefix; bob's own reply -> suppressed (worker
    # has it); in-flight @-request to bob -> suppressed (worker handles).
    assert [(m["role"], m["content"]) for m in view] == [
        ("user", "用户的问题"),
        ("user", "[小多 在群聊里说] 我以 leader 身份处理"),
    ]


def test_sync_member_chat_conversation_view_role_remap(tmp_path: Path):
    """The conv->member-chat view: other speakers become observed user-side
    speech (so the LLM doesn't mimic them), the member's own past replies
    stay as real assistant turns, and the in-flight user request + own
    member-chat reply are skipped (the worker handles those locally)."""
    db = open_cli_session_store(tmp_path / "state.db")
    db.sessions.create("conv", source="team_mission", transient=False)
    db.sessions.create("memberchat:conv:m-alice", source="team_mission_member_chat", transient=False)

    # Source conv transcript:
    #  - regular user msg                                      -> mirror as user
    #  - leader assistant msg (no member_id)                   -> user "[Leader] ..."
    #  - Bob (a different member)'s reply                      -> user "[Bob] ..."
    #  - Alice's own reply (kind=member_chat,m=alice)          -> skip (worker has it)
    #  - in-flight user @-request targeting Alice              -> skip (run.submit text)
    #  - earlier Alice direct assistant turn (member_id=alice) -> assistant
    db.messages.append("conv", role="user", content="嗨大家好")
    db.messages.append("conv", role="assistant", content="我是 leader 小多,需要什么帮助?",
                      metadata={"team_mission": {"display_name": "小多", "kind": "leader"}})
    db.messages.append("conv", role="assistant", content="我是 Hermes Agent - Bob's wrong reply",
                      metadata={"team_mission": {"kind": "member_chat", "member_id": "m-bob",
                                                  "display_name": "Bob"}})
    db.messages.append("conv", role="assistant", content="(alice's own reply - already on worker)",
                      metadata={"team_mission": {"kind": "member_chat", "member_id": "m-alice",
                                                  "display_name": "Alice"}})
    db.messages.append("conv", role="user", content="@Alice 现在的问题是什么",
                      metadata={"team_mission": {"kind": "member_chat_user",
                                                  "target_member_id": "m-alice"}})
    db.messages.append("conv", role="assistant", content="(alice's older direct turn)",
                      metadata={"team_mission": {"member_id": "m-alice",
                                                  "display_name": "Alice"}})

    appended = db.member_chat_views.sync_member_chat_conversation_view(
        conversation_session_id="conv",
        member_chat_session_id="memberchat:conv:m-alice",
        member_id="m-alice",
    )
    msgs = db.messages.list("memberchat:conv:m-alice")
    rendered = [(m.get("role"), str(m.get("content") or "")) for m in msgs]
    assert appended == 4, rendered  # user + leader + bob + alice-direct
    assert ("user", "嗨大家好") in rendered
    assert ("user", "[小多 在群聊里说] 我是 leader 小多,需要什么帮助?") in rendered
    assert ("user", "[Bob 在群聊里说] 我是 Hermes Agent - Bob's wrong reply") in rendered
    assert ("assistant", "(alice's older direct turn)") in rendered
    # In-flight request + Alice's own reply are NOT mirrored into the worker view.
    assert not any("@Alice 现在的问题是什么" in c for _, c in rendered)
    assert not any("alice's own reply" in c for _, c in rendered)


def test_sync_member_chat_conversation_view_is_idempotent(tmp_path: Path):
    """Re-syncing only appends NEW source messages - no duplication."""
    db = open_cli_session_store(tmp_path / "state.db")
    db.sessions.create("conv", source="team_mission", transient=False)
    db.sessions.create("memberchat:conv:m-alice", source="team_mission_member_chat", transient=False)
    db.messages.append("conv", role="user", content="第一条")

    first = db.member_chat_views.sync_member_chat_conversation_view(
        conversation_session_id="conv",
        member_chat_session_id="memberchat:conv:m-alice",
        member_id="m-alice",
    )
    assert first == 1

    # Re-run with no new source messages -> 0 appended
    second = db.member_chat_views.sync_member_chat_conversation_view(
        conversation_session_id="conv",
        member_chat_session_id="memberchat:conv:m-alice",
        member_id="m-alice",
    )
    assert second == 0
    assert len(db.messages.list("memberchat:conv:m-alice")) == 1

    # Add a new source message -> only it gets mirrored
    db.messages.append("conv", role="user", content="第二条")
    third = db.member_chat_views.sync_member_chat_conversation_view(
        conversation_session_id="conv",
        member_chat_session_id="memberchat:conv:m-alice",
        member_id="m-alice",
    )
    assert third == 1
    assert len(db.messages.list("memberchat:conv:m-alice")) == 2


def test_viewer_projection_preserves_own_tool_calls_and_tool_responses():
    """REGRESSION (bug found 2026-06-25): the earlier viewer projection
    dropped every ``tool`` row AND every assistant row whose content was
    empty, which silently severed tool_call -> tool_response pairing in
    the leader's hydrated history. The model then saw unmatched
    tool_calls and fell back to emitting the call as raw text (user
    saw ``clarify({...})`` printed instead of an actual tool invocation).
    Pin the correct behavior: the viewer's own assistant turn (empty
    content + tool_calls) and the paired tool response BOTH survive."""
    from hermes_team_mission.domain.member_chat_projection import project_messages_for_viewer, LEADER_PARTICIPANT_ID

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
    """Symmetric guarantee: a member's tool-call turn that landed in the
    conv must NOT bring its tool responses into the leader's view - the
    leader never invoked those calls; surfacing the responses would
    confuse the model. The member's user-facing reply IS reflected as
    ``[<speaker> 在群聊里说] ...``."""
    from hermes_team_mission.domain.member_chat_projection import project_messages_for_viewer, LEADER_PARTICIPANT_ID

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
    read time - re-projection would prefix-wrap already-prefixed user
    speech (``[X 说] [X 说] foo``) or drop the worker's own assistant
    turns. Pin the read-time no-op."""
    from hermes_team_mission.domain.member_chat_projection import project_messages_for_viewer

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
