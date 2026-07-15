"""Session lifecycle and message storage contracts."""

import time

import pytest


# =========================================================================
# Session lifecycle
# =========================================================================

class TestSessionLifecycle:
    def test_create_and_get_session(self, db):
        sid = db.sessions.create(
            session_id="s1",
            source="cli",
            model="test-model",
        )
        assert sid == "s1"

        session = db.sessions.get("s1")
        assert session is not None
        assert session["source"] == "cli"
        assert session["model"] == "test-model"
        assert session["ended_at"] is None


    def test_get_nonexistent_session(self, db):
        assert db.sessions.get("nonexistent") is None

    def test_end_session(self, db):
        db.sessions.create(session_id="s1", source="cli")
        db.sessions.end("s1", reason="user_exit")

        session = db.sessions.get("s1")
        assert isinstance(session["ended_at"], float)
        assert session["end_reason"] == "user_exit"

    def test_end_session_preserves_original_end_reason(self, db):
        """The first end_reason wins — compression splits must not be
        overwritten when a later stale ``end_session()`` call lands on the
        same row (e.g. from a CLI session_id that desynced after compression
        and then tried to /resume another session).
        """
        db.sessions.create(session_id="s1", source="cli")
        db.sessions.end("s1", reason="compression")
        first_ended_at = db.sessions.get("s1")["ended_at"]

        # Simulate a stale CLI holding the old session_id and calling
        # end_session() again with a different reason.
        time.sleep(0.01)
        db.sessions.end("s1", reason="resumed_other")

        session = db.sessions.get("s1")
        assert session["end_reason"] == "compression"
        assert session["ended_at"] == first_ended_at

    def test_end_session_after_reopen_allows_re_end(self, db):
        """reopen_session() is the explicit escape hatch for re-ending a
        closed session. After reopen, end_session() takes effect again.
        """
        db.sessions.create(session_id="s1", source="cli")
        db.sessions.end("s1", reason="compression")
        db.sessions.reopen("s1")
        db.sessions.end("s1", reason="user_exit")

        session = db.sessions.get("s1")
        assert session["end_reason"] == "user_exit"

    def test_update_system_prompt(self, db):
        db.sessions.create(session_id="s1", source="cli")
        db.sessions.update_system_prompt("s1", "You are a helpful assistant.")

        session = db.sessions.get("s1")
        assert session["system_prompt"] == "You are a helpful assistant."

    def test_scoped_system_prompt_does_not_overwrite_session_prompt(self, db):
        db.sessions.create(session_id="s1", source="team_mission")
        db.sessions.update_system_prompt("s1", "backend transcript prompt")

        db.sessions.update_scoped_system_prompt(
            "s1",
            "member-chat:s1:frontend",
            "frontend scoped prompt",
        )

        assert (
            db.sessions.get_scoped_system_prompt("s1", "member-chat:s1:frontend")
            == "frontend scoped prompt"
        )
        assert db.sessions.get("s1")["system_prompt"] == "backend transcript prompt"

        db.sessions.update_scoped_system_prompt(
            "s1",
            "member-chat:s1:frontend",
            "frontend scoped prompt v2",
        )
        assert (
            db.sessions.get_scoped_system_prompt("s1", "member-chat:s1:frontend")
            == "frontend scoped prompt v2"
        )

        db.update_scoped_system_prompt(
            "s1",
            "member-chat:s1:product",
            "product scoped prompt",
        )
        assert (
            db.get_scoped_system_prompt("s1", "member-chat:s1:product")
            == "product scoped prompt"
        )

    def test_update_token_counts(self, db):
        db.sessions.create(session_id="s1", source="cli")
        db.sessions.update_token_counts("s1", input_tokens=200, output_tokens=100)
        db.sessions.update_token_counts("s1", input_tokens=100, output_tokens=50)

        session = db.sessions.get("s1")
        assert session["input_tokens"] == 300
        assert session["output_tokens"] == 150

    def test_update_token_counts_tracks_api_call_count(self, db):
        """api_call_count increments with each update_token_counts call."""
        db.sessions.create(session_id="s1", source="cli")
        db.sessions.update_token_counts("s1", input_tokens=100, output_tokens=50, api_call_count=1)
        db.sessions.update_token_counts("s1", input_tokens=100, output_tokens=50, api_call_count=1)
        db.sessions.update_token_counts("s1", input_tokens=100, output_tokens=50, api_call_count=1)

        session = db.sessions.get("s1")
        assert session["api_call_count"] == 3

    def test_update_token_counts_api_call_count_absolute(self, db):
        """absolute mode sets api_call_count directly."""
        db.sessions.create(session_id="s1", source="cli")
        db.sessions.update_token_counts("s1", input_tokens=100, output_tokens=50, api_call_count=1)
        db.sessions.update_token_counts("s1", input_tokens=300, output_tokens=150,
                               api_call_count=5, absolute=True)

        session = db.sessions.get("s1")
        assert session["api_call_count"] == 5
        assert session["input_tokens"] == 300

    def test_update_token_counts_backfills_model_when_null(self, db):
        db.sessions.create(session_id="s1", source="telegram")
        db.sessions.update_token_counts("s1", input_tokens=10, output_tokens=5, model="openai/gpt-5.4")

        session = db.sessions.get("s1")
        assert session["model"] == "openai/gpt-5.4"

    def test_update_token_counts_preserves_existing_model(self, db):
        db.sessions.create(session_id="s1", source="cli", model="anthropic/claude-opus-4.6")
        db.sessions.update_token_counts("s1", input_tokens=10, output_tokens=5, model="openai/gpt-5.4")

        session = db.sessions.get("s1")
        assert session["model"] == "anthropic/claude-opus-4.6"

    def test_parent_session(self, db):
        db.sessions.create(session_id="parent", source="cli")
        db.sessions.create(session_id="child", source="cli", parent_session_id="parent")

        child = db.sessions.get("child")
        assert child["parent_session_id"] == "parent"


# =========================================================================
# Message storage
# =========================================================================

class TestMessageStorage:
    def test_append_and_get_messages(self, db):
        db.sessions.create(session_id="s1", source="cli")
        db.messages.append("s1", role="user", content="Hello")
        db.messages.append("s1", role="assistant", content="Hi there!")

        messages = db.messages.list("s1")
        assert len(messages) == 2
        assert messages[0]["role"] == "user"
        assert messages[0]["content"] == "Hello"
        assert messages[1]["role"] == "assistant"

    def test_message_metadata_round_trips(self, db):
        db.sessions.create(session_id="s1", source="cli")
        metadata = {
            "turn_id": "turn-1",
            "run_id": "run-1",
            "client_message_id": "msg-1",
        }

        db.messages.append("s1", role="user", content="Hello", metadata=metadata)

        messages = db.messages.list("s1")
        assert messages[0]["metadata"] == metadata

        conversation = db.messages.all_as_conversation("s1", include_storage_metadata=True)
        assert conversation[0]["metadata"] == metadata

    def test_replace_messages_preserves_metadata(self, db):
        db.sessions.create(session_id="s1", source="cli")
        metadata = {
            "turn_id": "turn-1",
            "run_id": "run-1",
            "client_message_id": "msg-1",
        }

        db.messages.replace("s1", [{"role": "user", "content": "Hello", "metadata": metadata}])

        conversation = db.messages.all_as_conversation("s1", include_storage_metadata=True)
        assert conversation[0]["metadata"] == metadata

    def test_merge_message_metadata_by_message_id(self, db):
        db.sessions.create(session_id="s1", source="cli")
        message_id = db.messages.append(
            "s1",
            role="assistant",
            content="Done",
            metadata={"run_id": "run-1", "nested": {"a": 1}},
        )

        updated = db.messages.merge_metadata(
            "s1",
            {
                "agentProfileDrafts": [{"draftId": "draft-1", "name": "产品经理分身"}],
                "nested": {"b": 2},
            },
            message_id=message_id,
            role="assistant",
        )

        assert updated["message_id"] == str(message_id)
        assert updated["metadata"] == {
            "run_id": "run-1",
            "nested": {"a": 1, "b": 2},
            "agentProfileDrafts": [{"draftId": "draft-1", "name": "产品经理分身"}],
        }

    def test_merge_message_metadata_by_latest_turn_identity(self, db):
        db.sessions.create(session_id="s1", source="cli")
        db.messages.append("s1", role="assistant", content="older", metadata={"run_id": "run-1"})
        target_id = db.messages.append("s1", role="assistant", content="newer", metadata={"run_id": "run-1"})

        updated = db.messages.merge_metadata(
            "s1",
            {"agentProfileDrafts": [{"draftId": "draft-1", "name": "产品经理分身"}]},
            role="assistant",
            run_id="run-1",
        )

        assert updated["message_id"] == str(target_id)
        conversation = db.messages.all_as_conversation("s1", include_storage_metadata=True)
        assert conversation[-1]["metadata"]["agentProfileDrafts"] == [
            {"draftId": "draft-1", "name": "产品经理分身"}
        ]

    def test_message_increments_session_count(self, db):
        db.sessions.create(session_id="s1", source="cli")
        db.messages.append("s1", role="user", content="Hello")
        db.messages.append("s1", role="assistant", content="Hi")

        session = db.sessions.get("s1")
        assert session["message_count"] == 2

    def test_tool_response_does_not_increment_tool_count(self, db):
        """Tool responses (role=tool) should not increment tool_call_count.

        Only assistant messages with tool_calls should count.
        """
        db.sessions.create(session_id="s1", source="cli")
        db.messages.append("s1", role="tool", content="result", tool_name="web_search")

        session = db.sessions.get("s1")
        assert session["tool_call_count"] == 0

    def test_assistant_tool_calls_increment_by_count(self, db):
        """An assistant message with N tool_calls should increment by N."""
        db.sessions.create(session_id="s1", source="cli")
        tool_calls = [
            {"id": "call_1", "function": {"name": "web_search", "arguments": "{}"}},
        ]
        db.messages.append("s1", role="assistant", content="", tool_calls=tool_calls)

        session = db.sessions.get("s1")
        assert session["tool_call_count"] == 1

    def test_tool_call_count_matches_actual_calls(self, db):
        """tool_call_count should equal the number of tool calls made, not messages."""
        db.sessions.create(session_id="s1", source="cli")

        # Assistant makes 2 parallel tool calls in one message
        tool_calls = [
            {"id": "call_1", "function": {"name": "ha_call_service", "arguments": "{}"}},
            {"id": "call_2", "function": {"name": "ha_call_service", "arguments": "{}"}},
        ]
        db.messages.append("s1", role="assistant", content="", tool_calls=tool_calls)

        # Two tool responses come back
        db.messages.append("s1", role="tool", content="ok", tool_name="ha_call_service")
        db.messages.append("s1", role="tool", content="ok", tool_name="ha_call_service")

        session = db.sessions.get("s1")
        # Should be 2 (the actual number of tool calls), not 3
        assert session["tool_call_count"] == 2, (
            f"Expected 2 tool calls but got {session['tool_call_count']}. "
            "tool responses are double-counted and multi-call messages are under-counted"
        )

    def test_tool_calls_serialization(self, db):
        db.sessions.create(session_id="s1", source="cli")
        tool_calls = [{"id": "call_1", "function": {"name": "web_search", "arguments": "{}"}}]
        db.messages.append("s1", role="assistant", tool_calls=tool_calls)

        messages = db.messages.list("s1")
        assert messages[0]["tool_calls"] == tool_calls

    def test_multimodal_list_content_round_trip(self, db):
        """Multimodal ``content`` (list of parts) must survive the SQLite
        round-trip.  sqlite3 cannot bind Python lists directly, so the DB
        layer JSON-encodes structured content on write and decodes on read.

        Regression test for the "Error binding parameter 3: type 'list' is
        not supported" crash users hit when pasting screenshots into the
        TUI (issue #17522).
        """
        db.sessions.create(session_id="s1", source="cli")
        content = [
            {"type": "text", "text": "describe this screenshot"},
            {
                "type": "image_url",
                "image_url": {"url": "data:image/png;base64,iVBORw0KG..."},
            },
        ]

        # Write must not raise
        db.messages.append("s1", role="user", content=content)

        # get_messages decodes back to the original list
        msgs = db.messages.list("s1")
        assert len(msgs) == 1
        assert msgs[0]["content"] == content

        # get_messages_as_conversation decodes back to the original list
        conv = db.messages.all_as_conversation("s1")
        assert len(conv) == 1
        assert conv[0] == {"role": "user", "content": content}

    def test_get_messages_page_tail_returns_storage_cursors(self, db):
        db.sessions.create(session_id="s1", source="cli")
        ids = [
            db.messages.append("s1", role="user", content=f"msg {idx}")
            for idx in range(5)
        ]

        page = db.messages.page_as_conversation("s1", direction="tail", limit=2)

        assert [m["content"] for m in page["messages"]] == ["msg 3", "msg 4"]
        assert [m["message_id"] for m in page["messages"]] == [str(ids[3]), str(ids[4])]
        assert page["messages"][0]["timestamp"]
        assert page["pageInfo"] == {
            "prev_cursor_id": ids[3],
            "next_cursor_id": None,
            "hasMoreBefore": True,
            "hasMoreAfter": False,
            "totalCount": 5,
        }

    def test_get_messages_page_tail_expands_to_complete_turn_boundary(self, db):
        db.sessions.create(session_id="s1", source="cli")
        db.messages.append("s1", role="user", content="previous request")
        metadata = {
            "turn_id": "turn-long",
            "run_id": "run-long",
            "client_message_id": "client-message-long",
        }
        user_id = db.messages.append(
            "s1",
            role="user",
            content="optimize the site",
            metadata=metadata,
        )
        tool_ids = [
            db.messages.append(
                "s1",
                role="tool",
                content=f"tool result {idx}",
                tool_name="read_file",
                tool_call_id=f"call-{idx}",
                metadata=metadata,
            )
            for idx in range(4)
        ]
        assistant_id = db.messages.append(
            "s1",
            role="assistant",
            content="done",
            metadata=metadata,
        )

        page = db.messages.page_as_conversation("s1", direction="tail", limit=2)

        assert [m["content"] for m in page["messages"]] == [
            "optimize the site",
            "tool result 0",
            "tool result 1",
            "tool result 2",
            "tool result 3",
            "done",
        ]
        assert [m["message_id"] for m in page["messages"]] == [
            str(user_id),
            *(str(tool_id) for tool_id in tool_ids),
            str(assistant_id),
        ]
        assert page["pageInfo"] == {
            "prev_cursor_id": user_id,
            "next_cursor_id": None,
            "hasMoreBefore": True,
            "hasMoreAfter": False,
            "totalCount": 7,
        }

    def test_get_messages_page_turn_expansion_recovers_unkeyed_user_boundary(self, db):
        db.sessions.create(session_id="s1", source="cli")
        db.messages.append("s1", role="user", content="previous request")
        user_id = db.messages.append("s1", role="user", content="legacy keyed tool request")
        metadata = {
            "turn_id": "turn-legacy",
            "run_id": "run-legacy",
            "client_message_id": "client-message-legacy",
        }
        tool_id = db.messages.append(
            "s1",
            role="tool",
            content="tool result",
            tool_name="read_file",
            tool_call_id="call-1",
            metadata=metadata,
        )
        assistant_id = db.messages.append(
            "s1",
            role="assistant",
            content="done",
            metadata=metadata,
        )

        page = db.messages.page_as_conversation("s1", direction="tail", limit=1)

        assert [m["content"] for m in page["messages"]] == [
            "legacy keyed tool request",
            "tool result",
            "done",
        ]
        assert [m["message_id"] for m in page["messages"]] == [
            str(user_id),
            str(tool_id),
            str(assistant_id),
        ]
        assert page["pageInfo"]["prev_cursor_id"] == user_id
        assert page["pageInfo"]["hasMoreBefore"] is True
        assert page["pageInfo"]["next_cursor_id"] is None
        assert page["pageInfo"]["totalCount"] == 4

    def test_get_messages_page_before_cursor_returns_older_page(self, db):
        db.sessions.create(session_id="s1", source="cli")
        ids = [
            db.messages.append("s1", role="user", content=f"msg {idx}")
            for idx in range(5)
        ]

        page = db.messages.page_as_conversation(
            "s1",
            direction="before",
            cursor_id=ids[3],
            limit=2,
        )

        assert [m["content"] for m in page["messages"]] == ["msg 1", "msg 2"]
        assert page["pageInfo"]["prev_cursor_id"] == ids[1]
        assert page["pageInfo"]["next_cursor_id"] == ids[2]
        assert page["pageInfo"]["hasMoreBefore"] is True
        assert page["pageInfo"]["hasMoreAfter"] is True

    def test_get_messages_page_includes_ancestor_messages(self, db):
        db.sessions.create(session_id="root", source="cli")
        db.messages.append("root", role="user", content="root question")
        db.sessions.create(session_id="child", source="cli", parent_session_id="root")
        db.messages.append("child", role="assistant", content="child answer")

        page = db.messages.page_as_conversation(
            "child",
            direction="tail",
            limit=10,
            include_ancestors=True,
        )

        assert [m["content"] for m in page["messages"]] == ["root question", "child answer"]
        assert page["pageInfo"]["totalCount"] == 2

    def test_dict_content_round_trip(self, db):
        """Dict-shaped content (e.g. provider wrappers) also round-trips."""
        db.sessions.create(session_id="s1", source="cli")
        content = {"parts": [{"text": "hi"}]}

        db.messages.append("s1", role="user", content=content)
        msgs = db.messages.list("s1")
        assert msgs[0]["content"] == content

    def test_string_content_unchanged_by_encoding(self, db):
        """Plain strings must not be wrapped — FTS search and legacy
        consumers depend on raw-string storage for text content.
        """
        db.sessions.create(session_id="s1", source="cli")
        db.messages.append("s1", role="user", content="plain text")

        # Peek at the raw column to confirm no encoding was applied
        with db._lock:
            row = db._conn.execute(
                "SELECT content FROM messages WHERE session_id = ?", ("s1",)
            ).fetchone()
        assert row["content"] == "plain text"

    def test_replace_messages_persists_tool_name(self, db):
        """`replace_messages` (used by /retry, /undo, /compress) must write
        tool_name to the DB for messages built by make_tool_result_message."""
        from agent.tool_dispatch_helpers import make_tool_result_message
        db.sessions.create(session_id="s1", source="cli")
        db.messages.replace(
            "s1",
            [
                {"role": "user", "content": "do something"},
                make_tool_result_message("web_search", "some results", "c1"),
            ],
        )

        msgs = db.messages.list("s1")
        tool_msg = next(m for m in msgs if m["role"] == "tool")
        assert tool_msg["tool_name"] == "web_search"

    def test_replace_messages_handles_multimodal_content(self, db):
        """`replace_messages` (used by /retry, /undo, /compress) must also
        handle list content without crashing."""
        db.sessions.create(session_id="s1", source="cli")
        content = [
            {"type": "text", "text": "look at this"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAA"}},
        ]

        db.messages.replace(
            "s1",
            [
                {"role": "user", "content": content},
                {"role": "assistant", "content": "I see a screenshot."},
            ],
        )

        msgs = db.messages.list("s1")
        assert len(msgs) == 2
        assert msgs[0]["content"] == content
        assert msgs[1]["content"] == "I see a screenshot."

    def test_display_title_uses_first_user_message_without_auto_title_overwrite(self, db):
        db.sessions.create(session_id="s1", source="cli")
        db.messages.append("s1", role="user", content="同样的首条消息")
        db.messages.append("s1", role="assistant", content="ok")
        db.sessions.create(session_id="s2", source="cli")
        db.messages.append("s2", role="user", content="同样的首条消息")

        first = db.sessions.get("s1")
        second = db.sessions.get("s2")
        assert first["display_title"] == "同样的首条消息"
        assert second["display_title"] == "同样的首条消息"
        assert first["display_title_source"] == "first_user_message"
        assert second["display_title_source"] == "first_user_message"

        assert db.sessions.set_title("s1", "LLM 自动摘要标题", title_source="auto") is False
        auto_titled = db.sessions.get("s1")
        assert auto_titled["title"] is None
        assert auto_titled["display_title"] == "同样的首条消息"
        assert auto_titled["display_title_source"] == "first_user_message"

        assert db.sessions.set_title("s1", "用户手动重命名")
        renamed = db.sessions.get("s1")
        assert renamed["title"] == "用户手动重命名"
        assert renamed["display_title"] == "用户手动重命名"
        assert renamed["display_title_source"] == "user"

        db.messages.replace("s1", [{"role": "user", "content": "新的首条消息"}])
        rewritten = db.sessions.get("s1")
        assert rewritten["display_title"] == "用户手动重命名"
        assert rewritten["display_title_source"] == "user"

        db.sessions.create(session_id="s3", source="cli")
        db.messages.append(
            "s3",
            role="user",
            content=[
                {"type": "text", "text": "多模态首条标题"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAA"}},
            ],
        )
        multimodal = db.sessions.get("s3")
        assert multimodal["display_title"] == "多模态首条标题"
        assert multimodal["display_title_source"] == "first_user_message"

        rows = {row["id"]: row for row in db.sessions.list_rich(limit=10, include_children=True)}
        assert rows["s1"]["display_title"] == "用户手动重命名"
        assert rows["s2"]["display_title"] == "同样的首条消息"
        assert rows["s3"]["display_title"] == "多模态首条标题"

    def test_get_messages_as_conversation(self, db):
        db.sessions.create(session_id="s1", source="cli")
        db.messages.append("s1", role="user", content="Hello")
        db.messages.append("s1", role="assistant", content="Hi!")

        conv = db.messages.all_as_conversation("s1")
        assert len(conv) == 2
        assert conv[0] == {"role": "user", "content": "Hello"}
        assert conv[1] == {"role": "assistant", "content": "Hi!"}

    def test_platform_message_id_round_trips(self, db):
        """Platform-side message ids (yuanbao msg_id, telegram update_id, …)
        survive append → get_messages_as_conversation under the
        ``message_id`` key so platform recall flows can match by exact id."""
        db.sessions.create(session_id="s_pmi", source="yuanbao")
        db.messages.append(
            "s_pmi",
            role="user",
            content="hi",
            platform_message_id="abc-123",
        )
        db.messages.append("s_pmi", role="assistant", content="hello")

        conv = db.messages.all_as_conversation("s_pmi")
        user_msg = next(m for m in conv if m["role"] == "user")
        assistant_msg = next(m for m in conv if m["role"] == "assistant")
        assert user_msg.get("message_id") == "abc-123"
        # Assistant row had no platform id — must not gain one spuriously.
        assert "message_id" not in assistant_msg

    def test_replace_messages_preserves_platform_message_id(self, db):
        """``rewrite_transcript`` (which goes through replace_messages) must
        keep the platform_message_id round-trip working for /retry, /undo,
        /compress and yuanbao's recall rewrite path."""
        db.sessions.create(session_id="s_rep", source="yuanbao")
        db.messages.replace(
            "s_rep",
            [
                {"role": "user", "content": "x", "message_id": "ext-1"},
                {"role": "assistant", "content": "y"},
            ],
        )
        conv = db.messages.all_as_conversation("s_rep")
        assert next(m for m in conv if m["role"] == "user").get("message_id") == "ext-1"
        assert "message_id" not in next(m for m in conv if m["role"] == "assistant")

    def test_get_messages_as_conversation_includes_ancestor_chain(self, db):
        db.sessions.create("root", "tui")
        db.messages.append("root", role="user", content="first prompt")
        db.messages.append("root", role="assistant", content="first answer")
        db.sessions.create("child", "tui", parent_session_id="root")
        db.messages.append("child", role="user", content="second prompt")
        db.messages.append("child", role="assistant", content="second answer")

        conv = db.messages.all_as_conversation("child", include_ancestors=True)

        assert [m["content"] for m in conv] == [
            "first prompt",
            "first answer",
            "second prompt",
            "second answer",
        ]

    def test_get_messages_as_conversation_avoids_repeated_resume_prompts_from_ancestors(self, db):
        db.sessions.create("root", "tui")
        db.messages.append("root", role="user", content="same prompt")
        db.messages.append("root", role="user", content="same prompt")
        db.messages.append("root", role="assistant", content="answer")
        db.sessions.create("child", "tui", parent_session_id="root")
        db.messages.append("child", role="user", content="next prompt")

        conv = db.messages.all_as_conversation("child", include_ancestors=True)

        assert [m["content"] for m in conv if m["role"] == "user"] == ["same prompt", "next prompt"]

    def test_user_branch_materializes_prefix_without_parent_replay(self, db):
        db.sessions.create(
            "source",
            "tui",
            model="gpt-test",
            system_prompt="system",
        )
        db.sessions.set_title("source", "需求评审")
        db.messages.append("source", role="user", content="prelude")
        metadata = {
            "turn_id": "turn-1",
            "run_id": "run-1",
            "client_message_id": "client-1",
        }
        db.messages.append("source", role="user", content="target prompt", metadata=metadata)
        tool_calls = [
            {"id": "call-1", "function": {"name": "read_file", "arguments": "{}"}},
        ]
        assistant_id = db.messages.append(
            "source",
            role="assistant",
            content="target answer",
            tool_calls=tool_calls,
            finish_reason="stop",
            reasoning="checked files",
            reasoning_details={"summary": "details"},
            codex_reasoning_items=[{"id": "reasoning-1"}],
            codex_message_items=[{"id": "message-1"}],
            platform_message_id="platform-assistant-1",
            metadata=metadata,
        )
        db.messages.append("source", role="user", content="future prompt")

        result = db.branches.branch_session(
            source_session_id="source",
            new_session_id="branch-1",
            branch_point={"turn_id": "turn-1"},
            idempotency_key="branch-key-1",
        )

        assert result["conversation_session_id"] == "branch-1"
        assert result["parent_session_id"] == "source"
        assert result["root_session_id"] == "source"
        assert result["branch_mode"] == "materialized_prefix"
        assert result["branch_point"]["included_message_row_id"] == assistant_id

        source = db.sessions.get("source")
        branch = db.sessions.get("branch-1")
        assert source["end_reason"] is None
        assert branch["parent_session_id"] is None
        assert branch["title"] == "需求评审 #2"
        assert branch["message_count"] == 3
        assert branch["tool_call_count"] == 1

        listed_ids = [session["id"] for session in db.sessions.list_rich(limit=10)]
        assert "source" in listed_ids
        assert "branch-1" in listed_ids

        conv = db.messages.all_as_conversation(
            "branch-1",
            include_ancestors=True,
            include_storage_metadata=True,
        )
        assert [message["content"] for message in conv] == [
            "prelude",
            "target prompt",
            "target answer",
        ]
        assert len({message["message_id"] for message in conv}) == 3
        assert conv[-1]["tool_calls"] == tool_calls
        assert conv[-1]["finish_reason"] == "stop"
        assert conv[-1]["reasoning"] == "checked files"
        assert conv[-1]["reasoning_details"] == {"summary": "details"}
        assert conv[-1]["codex_reasoning_items"] == [{"id": "reasoning-1"}]
        assert conv[-1]["codex_message_items"] == [{"id": "message-1"}]
        assert conv[-1]["metadata"] == metadata

        with db._lock:
            lineage = db._conn.execute(
                "SELECT * FROM session_lineage WHERE session_id = ?",
                ("branch-1",),
            ).fetchone()
            copied_assistant = db._conn.execute(
                "SELECT platform_message_id FROM messages "
                "WHERE session_id = ? AND role = 'assistant'",
                ("branch-1",),
            ).fetchone()

        assert lineage["parent_session_id"] == "source"
        assert lineage["root_session_id"] == "source"
        assert lineage["branch_origin"] == "user_message_action"
        assert lineage["branch_depth"] == 1
        assert copied_assistant["platform_message_id"] == "platform-assistant-1"

        branch_info = db.branches.get_session_branch_info("branch-1")
        assert branch_info["parent_session_id"] == "source"
        assert branch_info["branch_origin"] == "user_message_action"
        assert branch_info["branch_from_message_row_id"] == assistant_id

    def test_branch_session_idempotency_returns_existing_result_and_rejects_conflicts(self, db):
        db.sessions.create("source", "tui")
        db.sessions.set_title("source", "Branch Source")
        first_id = db.messages.append("source", role="user", content="first")
        second_id = db.messages.append("source", role="assistant", content="second")

        created = db.branches.branch_session(
            source_session_id="source",
            new_session_id="branch-1",
            branch_point={"message_id": str(second_id)},
            idempotency_key="branch-key-1",
        )
        replayed = db.branches.branch_session(
            source_session_id="source",
            new_session_id="branch-duplicate",
            branch_point={"message_id": str(second_id)},
            idempotency_key="branch-key-1",
        )

        assert created["conversation_session_id"] == "branch-1"
        assert replayed["conversation_session_id"] == "branch-1"
        assert replayed["replayed"] is True
        assert db.sessions.get("branch-duplicate") is None

        with pytest.raises(ValueError, match="idempotency key conflicts"):
            db.branches.branch_session(
                source_session_id="source",
                new_session_id="branch-conflict",
                branch_point={"message_id": str(first_id)},
                idempotency_key="branch-key-1",
            )

    def test_finish_reason_stored(self, db):
        db.sessions.create(session_id="s1", source="cli")
        db.messages.append("s1", role="assistant", content="Done", finish_reason="stop")

        messages = db.messages.list("s1")
        assert messages[0]["finish_reason"] == "stop"

    def test_get_messages_as_conversation_strips_leaked_memory_context(self, db):
        db.sessions.create(session_id="s1", source="cli")
        db.messages.append(
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

        conv = db.messages.all_as_conversation("s1")
        assert conv == [{"role": "assistant", "content": "Visible answer"}]

    def test_reasoning_persisted_and_restored(self, db):
        """Reasoning text is stored for assistant messages and restored by
        get_messages_as_conversation() so providers receive coherent multi-turn
        reasoning context."""
        db.sessions.create(session_id="s1", source="telegram")
        db.messages.append("s1", role="user", content="create a cron job")
        db.messages.append(
            "s1",
            role="assistant",
            content=None,
            tool_calls=[{"function": {"name": "cronjob", "arguments": "{}"}, "id": "c1", "type": "function"}],
            reasoning="I should call the cronjob tool to schedule this.",
        )
        db.messages.append("s1", role="tool", content='{"job_id": "abc"}', tool_call_id="c1")

        conv = db.messages.all_as_conversation("s1")
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
        db.sessions.create(session_id="s1", source="telegram")
        details = [
            {"type": "reasoning.summary", "summary": "Thinking about tools"},
            {"type": "reasoning.encrypted_content", "encrypted_content": "abc123"},
        ]
        db.messages.append(
            "s1",
            role="assistant",
            content="Hello",
            reasoning="Thinking about what to say",
            reasoning_details=details,
        )

        conv = db.messages.all_as_conversation("s1")
        assert len(conv) == 1
        msg = conv[0]
        assert msg["reasoning"] == "Thinking about what to say"
        assert msg["reasoning_details"] == details

    def test_finish_reason_restored_by_get_messages_as_conversation(self, db):
        """finish_reason on assistant messages must survive conversation replay.

        Without this, /branch copies and other transcript round-trips silently
        drop the provider's stop signal.
        """
        db.sessions.create(session_id="s1", source="cli")
        db.messages.append(
            "s1",
            role="assistant",
            content="Done",
            finish_reason="tool_calls",
        )
        db.messages.append("s1", role="user", content="next")

        conv = db.messages.all_as_conversation("s1")
        assert conv[0]["role"] == "assistant"
        assert conv[0]["finish_reason"] == "tool_calls"
        # Non-assistant rows should not have a finish_reason key added.
        assert "finish_reason" not in conv[1]

    def test_reasoning_content_persisted_and_restored(self, db):
        """reasoning_content must survive session replay as its own field."""
        db.sessions.create(session_id="s1", source="cli")
        db.messages.append(
            "s1",
            role="assistant",
            content="Hello",
            reasoning="Short summary",
            reasoning_content="Longer provider-native scratchpad",
        )

        conv = db.messages.all_as_conversation("s1")
        assert len(conv) == 1
        assert conv[0]["reasoning"] == "Short summary"
        assert conv[0]["reasoning_content"] == "Longer provider-native scratchpad"

    def test_reasoning_content_empty_string_restored_for_assistant(self, db):
        """Empty reasoning_content still needs to round-trip for strict replays."""
        db.sessions.create(session_id="s1", source="cli")
        db.messages.append(
            "s1",
            role="assistant",
            content="",
            tool_calls=[{"id": "c1", "type": "function", "function": {"name": "date", "arguments": "{}"}}],
            reasoning_content="",
        )

        conv = db.messages.all_as_conversation("s1")
        assert len(conv) == 1
        assert "reasoning_content" in conv[0]
        assert conv[0]["reasoning_content"] == ""

    def test_codex_message_items_persisted_and_restored(self, db):
        """codex_message_items must round-trip through JSON serialization."""
        db.sessions.create(session_id="s1", source="cli")
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
        db.messages.append("s1", role="assistant", content="Done!", codex_message_items=items)

        conv = db.messages.all_as_conversation("s1")
        assert len(conv) == 1
        assert conv[0].get("codex_message_items") == items

    def test_reasoning_not_set_for_non_assistant(self, db):
        """reasoning is never leaked onto user or tool messages."""
        db.sessions.create(session_id="s1", source="telegram")
        db.messages.append("s1", role="user", content="hi")
        db.messages.append("s1", role="assistant", content="hello", reasoning=None)

        conv = db.messages.all_as_conversation("s1")
        assert "reasoning" not in conv[0]
        assert "reasoning" not in conv[1]

    def test_reasoning_empty_string_not_restored(self, db):
        """Empty string reasoning is treated as absent."""
        db.sessions.create(session_id="s1", source="cli")
        db.messages.append("s1", role="assistant", content="hi", reasoning="")

        conv = db.messages.all_as_conversation("s1")
        assert "reasoning" not in conv[0]

    def test_codex_reasoning_items_persisted_and_restored(self, db):
        """codex_reasoning_items (encrypted blobs for Codex Responses API) are
        round-tripped through JSON serialization in the DB."""
        db.sessions.create(session_id="s1", source="cli")
        codex_items = [
            {"type": "reasoning", "id": "rs_abc", "encrypted_content": "enc_blob_123"},
            {"type": "reasoning", "id": "rs_def", "encrypted_content": "enc_blob_456"},
        ]
        db.messages.append(
            "s1",
            role="assistant",
            content="Done",
            codex_reasoning_items=codex_items,
        )

        conv = db.messages.all_as_conversation("s1")
        assert len(conv) == 1
        assert conv[0]["codex_reasoning_items"] == codex_items
        assert conv[0]["codex_reasoning_items"][0]["encrypted_content"] == "enc_blob_123"
