"""Truncation auto-continue / large-tool-call recovery prompts must NEVER
hit the session DB.

The LLM has to see them in the in-memory ``messages`` list — that's the
whole point: they tell the model "your previous response was cut off,
continue where you left off" or "your tool call was too big, split it."
But they are PRIVATE retry artifacts, not real human turns. If they get
flushed to the session DB they leak into the transcript UI as phantom
``[System: ...]`` user messages — bug observed 2026-06-25.

Pin the rule: ``_synthetic_continuation: True`` rows are visible to the
model (we don't strip them from ``messages``) but the DB-flush layer
skips them.
"""

from run_agent import AIAgent


def _agent_with_recording_db():
    agent = AIAgent.__new__(AIAgent)
    agent._persist_user_message_idx = None
    agent._persist_user_message_override = None
    agent.session_id = "session-1"
    agent._last_flushed_db_idx = 0
    agent._session_db_created = True
    agent._session_messages = []

    class _RecordingSessionDB:
        def __init__(self):
            self.appended = []

        def append_message(self, **kwargs):
            self.appended.append(kwargs)

    agent._session_db = _RecordingSessionDB()
    return agent


def test_length_continuation_user_message_is_not_persisted():
    """The injected ``[System: Your previous response was truncated by the
    output length limit. ...]`` prompt has ``_synthetic_continuation=True``.
    DB-flush must skip it — and must NOT touch the surrounding real rows."""
    agent = _agent_with_recording_db()
    messages = [
        {"role": "user", "content": "summarize the long doc"},
        {"role": "assistant", "content": "first half ..."},
        {
            "role": "user",
            "content": (
                "[System: Your previous response was truncated by the output "
                "length limit. Continue exactly where you left off. ...]"
            ),
            "_synthetic_continuation": True,
            "metadata": {"synthetic_kind": "length_continuation"},
        },
        {"role": "assistant", "content": "second half ..."},
    ]

    AIAgent._flush_messages_to_session_db(agent, messages, conversation_history=[])

    persisted_contents = [row["content"] for row in agent._session_db.appended]
    persisted_roles = [row["role"] for row in agent._session_db.appended]
    assert persisted_roles == ["user", "assistant", "assistant"]
    assert persisted_contents == ["summarize the long doc", "first half ...", "second half ..."]
    # The synthetic row stayed in messages for the LLM to see.
    assert any(m.get("_synthetic_continuation") for m in messages)


def test_truncated_tool_call_recovery_pair_is_not_persisted():
    """Same rule for the truncated-tool-call recovery pair: the bracketed
    assistant critique + the bracketed user recovery prompt both bear the
    flag and both must be skipped."""
    agent = _agent_with_recording_db()
    messages = [
        {"role": "user", "content": "patch the file"},
        {
            "role": "assistant",
            "content": "[Tool call omitted: arguments were truncated before execution.]",
            "_synthetic_continuation": True,
            "metadata": {"synthetic_kind": "truncated_tool_call_critique"},
        },
        {
            "role": "user",
            "content": "[System: ... split into smaller tool calls.]",
            "_synthetic_continuation": True,
            "metadata": {"synthetic_kind": "large_tool_call_recovery"},
        },
        {"role": "assistant", "content": "OK, splitting into 3 patches."},
    ]

    AIAgent._flush_messages_to_session_db(agent, messages, conversation_history=[])

    persisted_roles = [row["role"] for row in agent._session_db.appended]
    persisted_contents = [row["content"] for row in agent._session_db.appended]
    assert persisted_roles == ["user", "assistant"]
    assert persisted_contents == ["patch the file", "OK, splitting into 3 patches."]


def test_real_user_turn_metadata_still_propagates_to_next_assistant():
    """Subtle invariant: when the synthetic row sits BETWEEN a real user
    message and the assistant reply, the assistant must still get stamped
    with the real user's turn metadata (turn_id / run_id / client_message_id).
    Otherwise the timeline anchor breaks. Verifies the ``continue``
    statement in the flush loop didn't accidentally bypass the
    ``current_turn_metadata`` cursor for real rows."""
    agent = _agent_with_recording_db()
    messages = [
        {
            "role": "user",
            "content": "what's 2+2",
            "metadata": {"turn_id": "T1", "run_id": "R1", "client_message_id": "C1"},
        },
        {
            "role": "user",
            "content": "[System: ...truncated...]",
            "_synthetic_continuation": True,
            "metadata": {"synthetic_kind": "length_continuation"},
        },
        {"role": "assistant", "content": "4"},
    ]

    AIAgent._flush_messages_to_session_db(agent, messages, conversation_history=[])

    persisted_roles = [row["role"] for row in agent._session_db.appended]
    persisted_contents = [row["content"] for row in agent._session_db.appended]
    assert persisted_roles == ["user", "assistant"]
    assert persisted_contents == ["what's 2+2", "4"]
    assistant_row = agent._session_db.appended[-1]
    meta = assistant_row.get("metadata") or {}
    assert meta.get("turn_id") == "T1"
    assert meta.get("run_id") == "R1"
    assert meta.get("client_message_id") == "C1"
