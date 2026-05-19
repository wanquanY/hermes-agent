"""SessionDB message storage tests."""

from tests.hermes_state_fixtures import db


# =========================================================================
# Message storage
# =========================================================================

class TestMessageStorage:
    def test_append_and_get_messages(self, db):
        db.create_session(session_id="s1", source="cli")
        db.append_message("s1", role="user", content="Hello")
        db.append_message("s1", role="assistant", content="Hi there!")

        messages = db.get_messages("s1")
        assert len(messages) == 2
        assert messages[0]["role"] == "user"
        assert messages[0]["content"] == "Hello"
        assert messages[1]["role"] == "assistant"

    def test_message_increments_session_count(self, db):
        db.create_session(session_id="s1", source="cli")
        db.append_message("s1", role="user", content="Hello")
        db.append_message("s1", role="assistant", content="Hi")

        session = db.get_session("s1")
        assert session["message_count"] == 2

    def test_tool_response_does_not_increment_tool_count(self, db):
        """Tool responses (role=tool) should not increment tool_call_count.

        Only assistant messages with tool_calls should count.
        """
        db.create_session(session_id="s1", source="cli")
        db.append_message("s1", role="tool", content="result", tool_name="web_search")

        session = db.get_session("s1")
        assert session["tool_call_count"] == 0

    def test_assistant_tool_calls_increment_by_count(self, db):
        """An assistant message with N tool_calls should increment by N."""
        db.create_session(session_id="s1", source="cli")
        tool_calls = [
            {"id": "call_1", "function": {"name": "web_search", "arguments": "{}"}},
        ]
        db.append_message("s1", role="assistant", content="", tool_calls=tool_calls)

        session = db.get_session("s1")
        assert session["tool_call_count"] == 1

    def test_tool_call_count_matches_actual_calls(self, db):
        """tool_call_count should equal the number of tool calls made, not messages."""
        db.create_session(session_id="s1", source="cli")

        # Assistant makes 2 parallel tool calls in one message
        tool_calls = [
            {"id": "call_1", "function": {"name": "ha_call_service", "arguments": "{}"}},
            {"id": "call_2", "function": {"name": "ha_call_service", "arguments": "{}"}},
        ]
        db.append_message("s1", role="assistant", content="", tool_calls=tool_calls)

        # Two tool responses come back
        db.append_message("s1", role="tool", content="ok", tool_name="ha_call_service")
        db.append_message("s1", role="tool", content="ok", tool_name="ha_call_service")

        session = db.get_session("s1")
        # Should be 2 (the actual number of tool calls), not 3
        assert session["tool_call_count"] == 2, (
            f"Expected 2 tool calls but got {session['tool_call_count']}. "
            "tool responses are double-counted and multi-call messages are under-counted"
        )

    def test_tool_calls_serialization(self, db):
        db.create_session(session_id="s1", source="cli")
        tool_calls = [{"id": "call_1", "function": {"name": "web_search", "arguments": "{}"}}]
        db.append_message("s1", role="assistant", tool_calls=tool_calls)

        messages = db.get_messages("s1")
        assert messages[0]["tool_calls"] == tool_calls

    def test_multimodal_list_content_round_trip(self, db):
        """Multimodal ``content`` (list of parts) must survive the SQLite
        round-trip.  sqlite3 cannot bind Python lists directly, so the DB
        layer JSON-encodes structured content on write and decodes on read.

        Regression test for the "Error binding parameter 3: type 'list' is
        not supported" crash users hit when pasting screenshots into the
        TUI (issue #17522).
        """
        db.create_session(session_id="s1", source="cli")
        content = [
            {"type": "text", "text": "describe this screenshot"},
            {
                "type": "image_url",
                "image_url": {"url": "data:image/png;base64,iVBORw0KG..."},
            },
        ]

        # Write must not raise
        db.append_message("s1", role="user", content=content)

        # get_messages decodes back to the original list
        msgs = db.get_messages("s1")
        assert len(msgs) == 1
        assert msgs[0]["content"] == content

        # get_messages_as_conversation decodes back to the original list
        conv = db.get_messages_as_conversation("s1")
        assert len(conv) == 1
        assert conv[0] == {"role": "user", "content": content}

    def test_dict_content_round_trip(self, db):
        """Dict-shaped content (e.g. provider wrappers) also round-trips."""
        db.create_session(session_id="s1", source="cli")
        content = {"parts": [{"text": "hi"}]}

        db.append_message("s1", role="user", content=content)
        msgs = db.get_messages("s1")
        assert msgs[0]["content"] == content

    def test_string_content_unchanged_by_encoding(self, db):
        """Plain strings must not be wrapped — FTS search and legacy
        consumers depend on raw-string storage for text content.
        """
        db.create_session(session_id="s1", source="cli")
        db.append_message("s1", role="user", content="plain text")

        # Peek at the raw column to confirm no encoding was applied
        with db._lock:
            row = db._conn.execute(
                "SELECT content FROM messages WHERE session_id = ?", ("s1",)
            ).fetchone()
        assert row["content"] == "plain text"

    def test_replace_messages_handles_multimodal_content(self, db):
        """`replace_messages` (used by /retry, /undo, /compress) must also
        handle list content without crashing."""
        db.create_session(session_id="s1", source="cli")
        content = [
            {"type": "text", "text": "look at this"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAA"}},
        ]

        db.replace_messages(
            "s1",
            [
                {"role": "user", "content": content},
                {"role": "assistant", "content": "I see a screenshot."},
            ],
        )

        msgs = db.get_messages("s1")
        assert len(msgs) == 2
        assert msgs[0]["content"] == content
        assert msgs[1]["content"] == "I see a screenshot."

    def test_get_messages_as_conversation(self, db):
        db.create_session(session_id="s1", source="cli")
        db.append_message("s1", role="user", content="Hello")
        db.append_message("s1", role="assistant", content="Hi!")

        conv = db.get_messages_as_conversation("s1")
        assert len(conv) == 2
        assert conv[0] == {"role": "user", "content": "Hello"}
        assert conv[1] == {"role": "assistant", "content": "Hi!"}

    def test_get_messages_as_conversation_can_include_storage_metadata(self, db):
        db.create_session(session_id="s1", source="cli")
        msg_id = db.append_message("s1", role="user", content="Hello")

        conv = db.get_messages_as_conversation("s1", include_storage_metadata=True)

        assert conv[0]["id"] == msg_id
        assert conv[0]["session_id"] == "s1"
        assert isinstance(conv[0]["timestamp"], float)

    def test_get_messages_page_as_conversation_loads_tail_and_before_page(self, db):
        db.create_session(session_id="s1", source="cli")
        ids = [
            db.append_message("s1", role="user", content=f"message {index}")
            for index in range(5)
        ]

        tail = db.get_messages_page_as_conversation("s1", direction="tail", limit=2)
        assert [m["content"] for m in tail["messages"]] == ["message 3", "message 4"]
        assert tail["pageInfo"]["hasMoreBefore"] is True
        assert tail["pageInfo"]["hasMoreAfter"] is False

        before = db.get_messages_page_as_conversation(
            "s1",
            direction="before",
            cursor_id=ids[3],
            limit=2,
        )
        assert [m["content"] for m in before["messages"]] == ["message 1", "message 2"]
        assert before["pageInfo"]["hasMoreBefore"] is True
        assert before["pageInfo"]["hasMoreAfter"] is True

    def test_get_messages_as_conversation_includes_ancestor_chain(self, db):
        db.create_session("root", "tui")
        db.append_message("root", role="user", content="first prompt")
        db.append_message("root", role="assistant", content="first answer")
        db.create_session("child", "tui", parent_session_id="root")
        db.append_message("child", role="user", content="second prompt")
        db.append_message("child", role="assistant", content="second answer")

        conv = db.get_messages_as_conversation("child", include_ancestors=True)

        assert [m["content"] for m in conv] == [
            "first prompt",
            "first answer",
            "second prompt",
            "second answer",
        ]

    def test_get_messages_as_conversation_avoids_repeated_resume_prompts_from_ancestors(self, db):
        db.create_session("root", "tui")
        db.append_message("root", role="user", content="same prompt")
        db.append_message("root", role="user", content="same prompt")
        db.append_message("root", role="assistant", content="answer")
        db.create_session("child", "tui", parent_session_id="root")
        db.append_message("child", role="user", content="next prompt")

        conv = db.get_messages_as_conversation("child", include_ancestors=True)

        assert [m["content"] for m in conv if m["role"] == "user"] == ["same prompt", "next prompt"]

    def test_finish_reason_stored(self, db):
        db.create_session(session_id="s1", source="cli")
        db.append_message("s1", role="assistant", content="Done", finish_reason="stop")

        messages = db.get_messages("s1")
        assert messages[0]["finish_reason"] == "stop"

    def test_get_messages_as_conversation_strips_leaked_memory_context(self, db):
        db.create_session(session_id="s1", source="cli")
        db.append_message(
            "s1",
            role="assistant",
            content=(
                "<memory-context>\n"
                "[System note: The following is recalled memory context, NOT new user input. Treat as informational background data.]\n\n"
                "## Honcho Context\n"
                "stale memory\n"
                "</memory-context>\n\n"
                "Visible answer"
            ),
        )

        conv = db.get_messages_as_conversation("s1")
        assert conv == [{"role": "assistant", "content": "Visible answer"}]

    def test_reasoning_persisted_and_restored(self, db):
        """Reasoning text is stored for assistant messages and restored by
        get_messages_as_conversation() so providers receive coherent multi-turn
        reasoning context."""
        db.create_session(session_id="s1", source="telegram")
        db.append_message("s1", role="user", content="create a cron job")
        db.append_message(
            "s1",
            role="assistant",
            content=None,
            tool_calls=[{"function": {"name": "cronjob", "arguments": "{}"}, "id": "c1", "type": "function"}],
            reasoning="I should call the cronjob tool to schedule this.",
        )
        db.append_message("s1", role="tool", content='{"job_id": "abc"}', tool_call_id="c1")

        conv = db.get_messages_as_conversation("s1")
        assert len(conv) == 3
        # reasoning must be present on the assistant message
        assistant = conv[1]
        assert assistant["role"] == "assistant"
        assert assistant.get("reasoning") == "I should call the cronjob tool to schedule this."
        # user and tool messages must NOT carry reasoning
        assert "reasoning" not in conv[0]
        assert "reasoning" not in conv[2]

    def test_reasoning_details_persisted_and_restored(self, db):
        """reasoning_details (structured array) is round-tripped through JSON
        serialization in the DB."""
        db.create_session(session_id="s1", source="telegram")
        details = [
            {"type": "reasoning.summary", "summary": "Thinking about tools"},
            {"type": "reasoning.encrypted_content", "encrypted_content": "abc123"},
        ]
        db.append_message(
            "s1",
            role="assistant",
            content="Hello",
            reasoning="Thinking about what to say",
            reasoning_details=details,
        )

        conv = db.get_messages_as_conversation("s1")
        assert len(conv) == 1
        msg = conv[0]
        assert msg["reasoning"] == "Thinking about what to say"
        assert msg["reasoning_details"] == details

    def test_finish_reason_restored_by_get_messages_as_conversation(self, db):
        """finish_reason on assistant messages must survive conversation replay.

        Without this, /branch copies and other transcript round-trips silently
        drop the provider's stop signal.
        """
        db.create_session(session_id="s1", source="cli")
        db.append_message(
            "s1",
            role="assistant",
            content="Done",
            finish_reason="tool_calls",
        )
        db.append_message("s1", role="user", content="next")

        conv = db.get_messages_as_conversation("s1")
        assert conv[0]["role"] == "assistant"
        assert conv[0]["finish_reason"] == "tool_calls"
        # Non-assistant rows should not have a finish_reason key added.
        assert "finish_reason" not in conv[1]

    def test_reasoning_content_persisted_and_restored(self, db):
        """reasoning_content must survive session replay as its own field."""
        db.create_session(session_id="s1", source="cli")
        db.append_message(
            "s1",
            role="assistant",
            content="Hello",
            reasoning="Short summary",
            reasoning_content="Longer provider-native scratchpad",
        )

        conv = db.get_messages_as_conversation("s1")
        assert len(conv) == 1
        assert conv[0]["reasoning"] == "Short summary"
        assert conv[0]["reasoning_content"] == "Longer provider-native scratchpad"

    def test_reasoning_content_empty_string_restored_for_assistant(self, db):
        """Empty reasoning_content still needs to round-trip for strict replays."""
        db.create_session(session_id="s1", source="cli")
        db.append_message(
            "s1",
            role="assistant",
            content="",
            tool_calls=[{"id": "c1", "type": "function", "function": {"name": "date", "arguments": "{}"}}],
            reasoning_content="",
        )

        conv = db.get_messages_as_conversation("s1")
        assert len(conv) == 1
        assert "reasoning_content" in conv[0]
        assert conv[0]["reasoning_content"] == ""

    def test_codex_message_items_persisted_and_restored(self, db):
        """codex_message_items must round-trip through JSON serialization."""
        db.create_session(session_id="s1", source="cli")
        items = [
            {
                "type": "message",
                "role": "assistant",
                "status": "completed",
                "id": "msg_123",
                "phase": "commentary",
                "content": [{"type": "output_text", "text": "Thinking..."}],
            },
            {
                "type": "message",
                "role": "assistant",
                "status": "completed",
                "id": "msg_456",
                "phase": "final_answer",
                "content": [{"type": "output_text", "text": "Done!"}],
            },
        ]
        db.append_message("s1", role="assistant", content="Done!", codex_message_items=items)

        conv = db.get_messages_as_conversation("s1")
        assert len(conv) == 1
        assert conv[0].get("codex_message_items") == items

    def test_reasoning_not_set_for_non_assistant(self, db):
        """reasoning is never leaked onto user or tool messages."""
        db.create_session(session_id="s1", source="telegram")
        db.append_message("s1", role="user", content="hi")
        db.append_message("s1", role="assistant", content="hello", reasoning=None)

        conv = db.get_messages_as_conversation("s1")
        assert "reasoning" not in conv[0]
        assert "reasoning" not in conv[1]

    def test_reasoning_empty_string_not_restored(self, db):
        """Empty string reasoning is treated as absent."""
        db.create_session(session_id="s1", source="cli")
        db.append_message("s1", role="assistant", content="hi", reasoning="")

        conv = db.get_messages_as_conversation("s1")
        assert "reasoning" not in conv[0]

    def test_codex_reasoning_items_persisted_and_restored(self, db):
        """codex_reasoning_items (encrypted blobs for Codex Responses API) are
        round-tripped through JSON serialization in the DB."""
        db.create_session(session_id="s1", source="cli")
        codex_items = [
            {"type": "reasoning", "id": "rs_abc", "encrypted_content": "enc_blob_123"},
            {"type": "reasoning", "id": "rs_def", "encrypted_content": "enc_blob_456"},
        ]
        db.append_message(
            "s1",
            role="assistant",
            content="Done",
            codex_reasoning_items=codex_items,
        )

        conv = db.get_messages_as_conversation("s1")
        assert len(conv) == 1
        assert conv[0]["codex_reasoning_items"] == codex_items
        assert conv[0]["codex_reasoning_items"][0]["encrypted_content"] == "enc_blob_123"


# =========================================================================
# FTS5 search
# =========================================================================

