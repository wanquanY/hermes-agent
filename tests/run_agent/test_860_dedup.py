"""Tests for issue #860 — SQLite session transcript deduplication.

Verifies that:
1. _flush_messages_to_session_db uses _last_flushed_db_idx to avoid re-writing
2. Multiple _persist_session calls don't duplicate messages
3. append_to_transcript(skip_db=True) skips SQLite but writes JSONL
4. The gateway doesn't double-write messages the agent already persisted
"""

import json
import os
import sqlite3
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Test: _flush_messages_to_session_db only writes new messages
# ---------------------------------------------------------------------------

class TestFlushDeduplication:
    """Verify _flush_messages_to_session_db tracks what it already wrote."""

    def _make_agent(self, session_db):
        """Create a minimal AIAgent with a real session DB."""
        agent = self._make_uninitialized_agent(session_db)
        # Simulate lazy session creation (normally done by run_conversation)
        agent._ensure_db_session()
        return agent

    def _make_uninitialized_agent(self, session_db, *, session_id="test-session-860"):
        """Create a minimal AIAgent without initializing model transports."""
        from run_agent import AIAgent

        agent = object.__new__(AIAgent)
        agent.session_id = session_id
        agent.platform = "test"
        agent.model = "test/model"
        agent._session_db = session_db
        agent._session_db_created = False
        agent._session_init_model_config = None
        agent._cached_system_prompt = None
        agent._parent_session_id = None
        agent._last_flushed_db_idx = 0
        agent._persist_user_message_idx = None
        agent._persist_user_message_override = None
        agent._hermes_active_run_id = ""
        agent._hermes_active_turn_id = ""
        agent._hermes_active_runtime_scope_key = ""
        agent.run_context = None
        agent._run_context = None
        return agent

    def test_flush_writes_only_new_messages(self):
        """First flush writes all new messages, second flush writes none."""
        from hermes_agent.composition.cli_session_store import open_cli_session_store

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"
            db = open_cli_session_store(db_path=db_path)

            agent = self._make_agent(db)

            conversation_history = [
                {"role": "user", "content": "old message"},
            ]
            messages = list(conversation_history) + [
                {"role": "user", "content": "new question"},
                {"role": "assistant", "content": "new answer"},
            ]

            # First flush — should write 2 new messages
            agent._flush_messages_to_session_db(messages, conversation_history)

            rows = db.messages.list(agent.session_id)
            assert len(rows) == 2, f"Expected 2 messages, got {len(rows)}"

            # Second flush with SAME messages — should write 0 new messages
            agent._flush_messages_to_session_db(messages, conversation_history)

            rows = db.messages.list(agent.session_id)
            assert len(rows) == 2, f"Expected still 2 messages after second flush, got {len(rows)}"

    def test_flush_writes_incrementally(self):
        """Messages added between flushes are written exactly once."""
        from hermes_agent.composition.cli_session_store import open_cli_session_store

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"
            db = open_cli_session_store(db_path=db_path)

            agent = self._make_agent(db)

            conversation_history = []
            messages = [
                {"role": "user", "content": "hello"},
            ]

            # First flush — 1 message
            agent._flush_messages_to_session_db(messages, conversation_history)
            rows = db.messages.list(agent.session_id)
            assert len(rows) == 1

            # Add more messages
            messages.append({"role": "assistant", "content": "hi there"})
            messages.append({"role": "user", "content": "follow up"})

            # Second flush — should write only 2 new messages
            agent._flush_messages_to_session_db(messages, conversation_history)
            rows = db.messages.list(agent.session_id)
            assert len(rows) == 3, f"Expected 3 total messages, got {len(rows)}"

    def test_turn_start_persist_then_final_flush_keeps_one_ordered_turn(self):
        """Early user persistence and final turn flush share one DB transcript."""
        from agent.turn_message_buffer import TurnMessageBuffer
        from hermes_agent.composition.cli_session_store import open_cli_session_store

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"
            db = open_cli_session_store(db_path=db_path)
            agent = self._make_agent(db)
            agent._hermes_active_run_id = "run-first"
            agent._hermes_active_turn_id = "turn-first"

            messages = TurnMessageBuffer.from_history([])
            messages.append(
                {
                    "role": "user",
                    "content": "第一轮问题",
                    "metadata": {
                        "run_id": "run-first",
                        "turn_id": "turn-first",
                        "client_message_id": "client-first",
                    },
                }
            )
            agent._persist_session(messages, [])

            messages.append({"role": "assistant", "content": "第一轮回答"})
            agent._persist_session(messages, [])

            rows = db.messages.list(agent.session_id)
            assert [row["role"] for row in rows] == ["user", "assistant"]
            assert [row["content"] for row in rows] == ["第一轮问题", "第一轮回答"]
            assert [row["metadata"]["persist_message_key"] for row in rows] == [
                "run:run-first|turn:turn-first|idx:0",
                "run:run-first|turn:turn-first|idx:1",
            ]

    def test_prompt_submit_user_prepersist_is_idempotent_with_agent_flush(self):
        """Gateway-owned user persistence must not duplicate agent turn flush."""
        from agent.turn_message_buffer import TurnMessageBuffer
        from hermes_agent.composition.cli_session_store import open_cli_session_store

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"
            db = open_cli_session_store(db_path=db_path)
            agent = self._make_uninitialized_agent(db)
            db.sessions.create(agent.session_id, source="tui", transient=False)
            agent._hermes_active_run_id = "run-first"
            agent._hermes_active_turn_id = "turn-first"

            prepersisted_id = db.messages.append(
                session_id=agent.session_id,
                role="user",
                content="第一轮问题",
                metadata={
                    "run_id": "run-first",
                    "turn_id": "turn-first",
                    "client_message_id": "client-first",
                    "turn_message_index": 0,
                    "persist_message_key": "run:run-first|turn:turn-first|idx:0",
                    "prompt_submit_owned": True,
                },
            )

            messages = TurnMessageBuffer.from_history([])
            messages.append(
                {
                    "role": "user",
                    "content": "第一轮问题",
                    "metadata": {
                        "run_id": "run-first",
                        "turn_id": "turn-first",
                        "client_message_id": "client-first",
                    },
                }
            )
            agent._persist_session(messages, [])

            messages.append({"role": "assistant", "content": "第一轮回答"})
            agent._persist_session(messages, [])

            rows = db.messages.list(agent.session_id)
            assert [row["role"] for row in rows] == ["user", "assistant"]
            assert [row["content"] for row in rows] == ["第一轮问题", "第一轮回答"]
            assert rows[0]["id"] == prepersisted_id
            assert [row["metadata"]["persist_message_key"] for row in rows] == [
                "run:run-first|turn:turn-first|idx:0",
                "run:run-first|turn:turn-first|idx:1",
            ]

    def test_team_conversation_flush_uses_active_run_identity(self):
        """Team worker flush must not inherit run ids from projected history."""
        from hermes_agent.composition.cli_session_store import open_cli_session_store

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"
            db = open_cli_session_store(db_path=db_path)

            visible_session_id = "team-session-team-conversation-test"
            agent = self._make_uninitialized_agent(db, session_id="runtime-member-session")
            db.sessions.create(visible_session_id, source="team_mission", transient=False)
            db.session_index.upsert(
                session_id=visible_session_id,
                source="team_mission",
                conversation_kind="team",
                started_at=1.0,
                updated_at=1.0,
            )

            agent._hermes_active_run_id = "team-member-run-current"
            agent._hermes_active_turn_id = "team-member-turn-current"
            agent._hermes_active_runtime_scope_key = "member-chat:team-conversation-1:member-1"
            agent.run_context = SimpleNamespace(
                conversation_session_id=visible_session_id,
                activity_id="act-member-chat-1",
                activity_kind="member_chat",
                execution_scope_key="member-chat:team-conversation-1:member-1",
                participant_id="member:member-1",
                to_payload=lambda: {
                    "conversation_session_id": visible_session_id,
                    "activity_id": "act-member-chat-1",
                    "activity_kind": "member_chat",
                    "execution_scope_key": "member-chat:team-conversation-1:member-1",
                    "participant_id": "member:member-1",
                },
            )
            history = [
                {
                    "role": "user",
                    "content": "[Leader] prior visible speech",
                    "metadata": {
                        "run_id": "team-leader-run-stale",
                        "turn_id": "team-leader-turn-stale",
                    },
                }
            ]
            messages = history + [
                {"role": "assistant", "content": "member answer", "reasoning": "thinking"},
                {
                    "role": "tool",
                    "content": "{}",
                    "tool_name": "search_files",
                    "tool_call_id": "call-member-1",
                },
            ]

            agent._flush_messages_to_session_db(messages, history)

            rows = db.messages.list(visible_session_id)
            assert [row["role"] for row in rows] == ["assistant", "tool"]
            for row in rows:
                metadata = row["metadata"]
                assert metadata["run_id"] == "team-member-run-current"
                assert metadata["turn_id"] == "team-member-turn-current"
                assert metadata["participant_id"] == "member:member-1"
                assert metadata["execution_scope_key"] == "member-chat:team-conversation-1:member-1"
                assert metadata["run_id"] != "team-leader-run-stale"
            assert rows[0]["participant_id"] == "member:member-1"
            assert rows[0]["reasoning"] == "thinking"
            assert agent._last_flushed_db_idx == len(messages)

    def test_team_conversation_flush_does_not_use_history_len_when_history_is_not_prefix(self):
        """Team worker messages may be current-turn-only, not history + new turn."""
        from hermes_agent.composition.cli_session_store import open_cli_session_store

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"
            db = open_cli_session_store(db_path=db_path)

            visible_session_id = "team-session-team-conversation-test"
            agent = self._make_uninitialized_agent(db, session_id=visible_session_id)
            db.sessions.create(visible_session_id, source="team_mission", transient=False)
            db.session_index.upsert(
                session_id=visible_session_id,
                source="team_mission",
                conversation_kind="team",
                started_at=1.0,
                updated_at=1.0,
            )

            agent._hermes_active_run_id = "team-member-run-current"
            agent._hermes_active_turn_id = "team-member-turn-current"
            agent._hermes_active_runtime_scope_key = "member-chat:team-conversation-1:member-1"
            agent.run_context = SimpleNamespace(
                conversation_session_id=visible_session_id,
                activity_id="act-member-chat-1",
                activity_kind="member_chat",
                execution_scope_key="member-chat:team-conversation-1:member-1",
                participant_id="member:member-1",
                to_payload=lambda: {
                    "conversation_session_id": visible_session_id,
                    "activity_id": "act-member-chat-1",
                    "activity_kind": "member_chat",
                    "execution_scope_key": "member-chat:team-conversation-1:member-1",
                    "participant_id": "member:member-1",
                },
            )
            history = [
                {"role": "user", "content": "prior user"},
                {"role": "assistant", "content": "prior leader"},
                {"role": "user", "content": "current submitted user"},
            ]
            messages = [
                {"role": "user", "content": "current submitted user"},
                {
                    "role": "assistant",
                    "content": "text before search",
                    "tool_calls": [
                        {
                            "id": "call-search",
                            "type": "function",
                            "function": {"name": "search_files", "arguments": "{}"},
                        }
                    ],
                },
                {
                    "role": "tool",
                    "content": "{}",
                    "tool_name": "search_files",
                    "tool_call_id": "call-search",
                },
                {
                    "role": "assistant",
                    "content": "text before terminal",
                    "tool_calls": [
                        {
                            "id": "call-terminal",
                            "type": "function",
                            "function": {"name": "terminal", "arguments": "{}"},
                        }
                    ],
                },
                {
                    "role": "tool",
                    "content": "{}",
                    "tool_name": "terminal",
                    "tool_call_id": "call-terminal",
                },
                {"role": "assistant", "content": "final answer"},
            ]

            agent._flush_messages_to_session_db(messages, history)

            rows = db.messages.list(visible_session_id)
            assert [row["role"] for row in rows] == [
                "assistant",
                "tool",
                "assistant",
                "tool",
                "assistant",
            ]
            assert [row.get("tool_name") for row in rows if row["role"] == "tool"] == [
                "search_files",
                "terminal",
            ]
            assert all(row["participant_id"] == "member:member-1" for row in rows)
            assert all(row["metadata"]["run_id"] == "team-member-run-current" for row in rows)
            assert agent._last_flushed_db_idx == len(messages)

    def test_team_mission_start_flush_persists_main_transcript_tools(self):
        """Leader mission-start messages are main conversation history."""
        from hermes_agent.composition.cli_session_store import open_cli_session_store

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"
            db = open_cli_session_store(db_path=db_path)

            visible_session_id = "team-session-team-conversation-test"
            agent = self._make_uninitialized_agent(db, session_id="runtime-mission-session")
            db.sessions.create(visible_session_id, source="team_mission", transient=False)
            db.session_index.upsert(
                session_id=visible_session_id,
                source="team_mission",
                conversation_kind="team",
                started_at=1.0,
                updated_at=1.0,
            )

            agent._hermes_active_run_id = "team-mission-run-current"
            agent._hermes_active_turn_id = "team-mission-turn-current"
            agent._hermes_active_runtime_scope_key = "team:conversation-1"
            agent.run_context = SimpleNamespace(
                conversation_session_id=visible_session_id,
                activity_id="mission:mission-1",
                activity_kind="mission",
                execution_scope_key="team:conversation-1",
                participant_id="leader:conversation-1",
                to_payload=lambda: {
                    "conversation_session_id": visible_session_id,
                    "activity_id": "mission:mission-1",
                    "activity_kind": "mission",
                    "execution_scope_key": "team:conversation-1",
                    "participant_id": "leader:conversation-1",
                },
            )
            messages = [
                {"role": "user", "content": "创建一个文件"},
                {
                    "role": "assistant",
                    "content": "运行期规划",
                    "tool_calls": [
                        {
                            "id": "call-plan",
                            "type": "function",
                            "function": {"name": "team_mission_node_create", "arguments": "{}"},
                        }
                    ],
                },
                {
                    "role": "tool",
                    "content": "{}",
                    "tool_name": "team_mission_node_create",
                    "tool_call_id": "call-plan",
                },
                {"role": "assistant", "content": "运行期状态更新"},
            ]

            agent._flush_messages_to_session_db(messages, [])

            rows = db.messages.list(visible_session_id)
            assert [row["role"] for row in rows] == ["assistant", "tool", "assistant"]
            assert [row.get("tool_name") for row in rows] == [None, "team_mission_node_create", None]
            assert all(row["metadata"]["transcript_activity_kind"] == "mission_start" for row in rows)
            assert agent._last_flushed_db_idx == len(messages)

    def test_team_mission_node_flush_does_not_persist_node_rows_to_visible_transcript(self):
        """Mission node execution belongs to the task graph, not the main conversation."""
        from hermes_agent.composition.cli_session_store import open_cli_session_store

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"
            db = open_cli_session_store(db_path=db_path)

            visible_session_id = "team-session-team-conversation-test"
            agent = self._make_uninitialized_agent(db, session_id="runtime-mission-session")
            db.sessions.create(visible_session_id, source="team_mission", transient=False)
            db.session_index.upsert(
                session_id=visible_session_id,
                source="team_mission",
                conversation_kind="team",
                started_at=1.0,
                updated_at=1.0,
            )

            agent._hermes_active_run_id = "team-mission-run-current"
            agent._hermes_active_turn_id = "team-mission-turn-current"
            agent._hermes_active_runtime_scope_key = "team:conversation-1"
            agent.run_context = SimpleNamespace(
                conversation_session_id=visible_session_id,
                activity_id="act-node:mission-1:node-1",
                activity_kind="mission",
                execution_scope_key="team:conversation-1",
                participant_id="leader:conversation-1",
                to_payload=lambda: {
                    "conversation_session_id": visible_session_id,
                    "activity_id": "act-node:mission-1:node-1",
                    "activity_kind": "mission",
                    "execution_scope_key": "team:conversation-1",
                    "participant_id": "leader:conversation-1",
                },
            )
            messages = [
                {"role": "user", "content": "执行节点"},
                {
                    "role": "assistant",
                    "content": "节点内部规划",
                    "tool_calls": [
                        {
                            "id": "call-plan",
                            "type": "function",
                            "function": {"name": "team_mission_node_create", "arguments": "{}"},
                        }
                    ],
                },
                {
                    "role": "tool",
                    "content": "{}",
                    "tool_name": "team_mission_node_create",
                    "tool_call_id": "call-plan",
                },
                {"role": "assistant", "content": "节点内部状态更新"},
            ]

            agent._flush_messages_to_session_db(messages, [])

            assert db.messages.list(visible_session_id) == []
            runtime_rows = db.messages.list("runtime-mission-session")
            assert [row["role"] for row in runtime_rows] == ["user", "assistant", "tool", "assistant"]
            assert [row.get("tool_name") for row in runtime_rows] == [None, None, "team_mission_node_create", None]
            assert all(row["metadata"]["transcript_activity_kind"] == "mission_node" for row in runtime_rows)
            assert agent._last_flushed_db_idx == len(messages)
            for message in messages[1:]:
                assert message["metadata"]["transcript_activity_kind"] == "mission_node"

    def test_team_dispatch_flush_persists_main_transcript_tools(self):
        """Team dispatch leader/tool messages are main conversation history."""
        from hermes_agent.composition.cli_session_store import open_cli_session_store

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"
            db = open_cli_session_store(db_path=db_path)

            visible_session_id = "team-session-team-conversation-test"
            agent = self._make_uninitialized_agent(db, session_id="runtime-dispatch-session")
            db.sessions.create(visible_session_id, source="team_mission", transient=False)
            db.session_index.upsert(
                session_id=visible_session_id,
                source="team_mission",
                conversation_kind="team",
                started_at=1.0,
                updated_at=1.0,
            )

            agent._hermes_active_run_id = "team-dispatch-run-current"
            agent._hermes_active_turn_id = "team-dispatch-turn-current"
            agent._hermes_active_runtime_scope_key = "team:conversation-1"
            agent.run_context = SimpleNamespace(
                conversation_session_id=visible_session_id,
                activity_id="act-team_dispatch-1",
                activity_kind="team_dispatch",
                execution_scope_key="team:conversation-1",
                participant_id="leader:conversation-1",
                to_payload=lambda: {
                    "conversation_session_id": visible_session_id,
                    "activity_id": "act-team_dispatch-1",
                    "activity_kind": "team_dispatch",
                    "execution_scope_key": "team:conversation-1",
                    "participant_id": "leader:conversation-1",
                },
            )
            messages = [
                {"role": "user", "content": "启动团队任务"},
                {
                    "role": "assistant",
                    "content": "准备启动团队任务",
                    "tool_calls": [
                        {
                            "id": "call-start",
                            "type": "function",
                            "function": {"name": "team_mission_start_task", "arguments": "{}"},
                        }
                    ],
                },
                {
                    "role": "tool",
                    "content": "{}",
                    "tool_name": "team_mission_start_task",
                    "tool_call_id": "call-start",
                },
                {"role": "assistant", "content": "任务已经进入规划"},
            ]

            agent._flush_messages_to_session_db(messages, [])

            rows = db.messages.list(visible_session_id)
            assert [row["role"] for row in rows] == ["assistant", "tool", "assistant"]
            assert [row.get("tool_name") for row in rows] == [None, "team_mission_start_task", None]
            assert all(row["metadata"]["transcript_activity_kind"] == "team_dispatch" for row in rows)
            assert agent._last_flushed_db_idx == len(messages)

    def test_team_dispatch_second_run_does_not_reuse_stale_memory_flush_cursor(self):
        """A rebuilt leader history must not let the previous run's cursor skip rows."""
        from hermes_agent.composition.cli_session_store import open_cli_session_store
        from tui_gateway.services.run_control import record_event

        def install_dispatch_context(agent, *, run_id: str, turn_id: str) -> None:
            agent._hermes_active_run_id = run_id
            agent._hermes_active_turn_id = turn_id
            agent._hermes_active_runtime_scope_key = "team:conversation-1"
            agent.run_context = SimpleNamespace(
                conversation_session_id=visible_session_id,
                activity_id=f"act-team_dispatch-{run_id}",
                activity_kind="team_dispatch",
                execution_scope_key="team:conversation-1",
                participant_id="leader:conversation-1",
                to_payload=lambda: {
                    "conversation_session_id": visible_session_id,
                    "activity_id": f"act-team_dispatch-{run_id}",
                    "activity_kind": "team_dispatch",
                    "execution_scope_key": "team:conversation-1",
                    "participant_id": "leader:conversation-1",
                },
            )

        def record_leader_event_shape(db, *, run_id: str, turn_id: str) -> None:
            base = {
                "conversation_session_id": visible_session_id,
                "session_id": visible_session_id,
                "run_id": run_id,
                "turn_id": turn_id,
                "runtime_scope_key": "team:conversation-1",
            }
            for seq, event_type, payload in (
                (1, "message.start", {"text": "准备启动团队任务"}),
                (2, "tool.start", {"name": "team_mission_start_task", "tool_call_id": "call-start"}),
                (3, "tool.complete", {"name": "team_mission_start_task", "tool_call_id": "call-start"}),
                (4, "message.start", {"text": "任务已经进入规划"}),
                (5, "tool.start", {"name": "session.info", "tool_call_id": "call-info"}),
                (6, "tool.complete", {"name": "session.info", "tool_call_id": "call-info"}),
                (7, "session.info", {"status": "running"}),
                (8, "session.info", {"status": "completed"}),
                (9, "message.complete", {"text": "任务已经进入规划", "status": "complete"}),
            ):
                record_event({**base, "seq": seq, "type": event_type, "payload": payload}, db=db)

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"
            db = open_cli_session_store(db_path=db_path)

            visible_session_id = "team-session-team-conversation-test"
            agent = self._make_uninitialized_agent(db, session_id="runtime-dispatch-session")
            db.sessions.create(visible_session_id, source="team_mission", transient=False)
            db.session_index.upsert(
                session_id=visible_session_id,
                source="team_mission",
                conversation_kind="team",
                started_at=1.0,
                updated_at=1.0,
            )

            install_dispatch_context(agent, run_id="team-leader-run-first", turn_id="team-leader-turn-first")
            record_leader_event_shape(db, run_id="team-leader-run-first", turn_id="team-leader-turn-first")
            first_history = [{"role": "user", "content": "会话创建"}]
            first_messages = list(first_history) + [
                {"role": "user", "content": "第一次启动团队任务"},
                {
                    "role": "assistant",
                    "content": "第一轮准备启动",
                    "tool_calls": [
                        {
                            "id": "call-first-start",
                            "type": "function",
                            "function": {"name": "team_mission_start_task", "arguments": "{}"},
                        }
                    ],
                },
                {
                    "role": "tool",
                    "content": "{}",
                    "tool_name": "team_mission_start_task",
                    "tool_call_id": "call-first-start",
                },
                {"role": "assistant", "content": "第一轮任务已经进入规划"},
            ]
            agent._flush_messages_to_session_db(first_messages, first_history)
            assert agent._last_flushed_db_idx == 5

            install_dispatch_context(agent, run_id="team-leader-run-second", turn_id="team-leader-turn-second")
            record_leader_event_shape(db, run_id="team-leader-run-second", turn_id="team-leader-turn-second")
            rebuilt_history = [
                {"role": "user", "content": "会话创建"},
                {"role": "assistant", "content": "第一轮任务已经进入规划"},
            ]
            second_messages = list(rebuilt_history) + [
                {"role": "user", "content": "第二次启动团队任务"},
                {
                    "role": "assistant",
                    "content": "第二轮准备启动",
                    "tool_calls": [
                        {
                            "id": "call-second-start",
                            "type": "function",
                            "function": {"name": "team_mission_start_task", "arguments": "{}"},
                        }
                    ],
                },
                {
                    "role": "tool",
                    "content": "{}",
                    "tool_name": "team_mission_start_task",
                    "tool_call_id": "call-second-start",
                },
                {"role": "assistant", "content": "第二轮任务已经进入规划"},
            ]
            agent._flush_messages_to_session_db(second_messages, rebuilt_history)

            rows = db.messages.list(visible_session_id)
            assert [row["content"] for row in rows] == [
                "第一轮准备启动",
                {},
                "第一轮任务已经进入规划",
                "第二轮准备启动",
                {},
                "第二轮任务已经进入规划",
            ]
            second_rows = [
                row for row in rows
                if row["metadata"].get("run_id") == "team-leader-run-second"
            ]
            assert [row["role"] for row in second_rows] == ["assistant", "tool", "assistant"]
            assert [row.get("tool_name") for row in second_rows] == [
                None,
                "team_mission_start_task",
                None,
            ]
            assert [row["metadata"]["turn_message_index"] for row in second_rows] == [1, 2, 3]
            assert [row["metadata"].get("assistant_segment_index") for row in second_rows] == [0, None, 1]
            assert all(row["metadata"].get("persist_message_key") for row in second_rows)
            assert {
                tuple(event["type"] for event in db.runs.list_events(visible_session_id, run_id=run_id))
                for run_id in ("team-leader-run-first", "team-leader-run-second")
            } == {
                (
                    "message.start",
                    "tool.start",
                    "tool.complete",
                    "message.start",
                    "tool.start",
                    "tool.complete",
                    "session.info",
                    "session.info",
                    "message.complete",
                )
            }

            # A fresh rebuilt buffer for the same run must be de-duplicated by
            # persisted run/turn/message-index keys, not by Python list identity.
            second_messages_rebuilt_again = list(rebuilt_history) + [
                {"role": "user", "content": "第二次启动团队任务"},
                {
                    "role": "assistant",
                    "content": "第二轮准备启动",
                    "tool_calls": [
                        {
                            "id": "call-second-start",
                            "type": "function",
                            "function": {"name": "team_mission_start_task", "arguments": "{}"},
                        }
                    ],
                },
                {
                    "role": "tool",
                    "content": "{}",
                    "tool_name": "team_mission_start_task",
                    "tool_call_id": "call-second-start",
                },
                {"role": "assistant", "content": "第二轮任务已经进入规划"},
            ]
            agent._flush_messages_to_session_db(second_messages_rebuilt_again, rebuilt_history)

            assert len(db.messages.list(visible_session_id)) == len(rows)

    def test_turn_message_buffer_boundary_keeps_history_out_when_history_arg_is_lost(self):
        """Loaded history is never reclassified as current output by DB flush."""
        from agent.turn_message_buffer import TurnMessageBuffer
        from hermes_agent.composition.cli_session_store import open_cli_session_store

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"
            db = open_cli_session_store(db_path=db_path)

            visible_session_id = "team-session-team-conversation-test"
            agent = self._make_uninitialized_agent(db, session_id="runtime-dispatch-session")
            db.sessions.create(visible_session_id, source="team_mission", transient=False)
            db.session_index.upsert(
                session_id=visible_session_id,
                source="team_mission",
                conversation_kind="team",
                started_at=1.0,
                updated_at=1.0,
            )

            agent._hermes_active_run_id = "team-dispatch-run-current"
            agent._hermes_active_turn_id = "team-dispatch-turn-current"
            agent._hermes_active_runtime_scope_key = "team:conversation-1"
            agent.run_context = SimpleNamespace(
                conversation_session_id=visible_session_id,
                activity_id="act-team_dispatch-current",
                activity_kind="team_dispatch",
                execution_scope_key="team:conversation-1",
                participant_id="leader:conversation-1",
                to_payload=lambda: {
                    "conversation_session_id": visible_session_id,
                    "activity_id": "act-team_dispatch-current",
                    "activity_kind": "team_dispatch",
                    "execution_scope_key": "team:conversation-1",
                    "participant_id": "leader:conversation-1",
                },
            )
            messages = TurnMessageBuffer.from_history(
                [
                    {"role": "user", "content": "旧用户消息"},
                    {
                        "role": "assistant",
                        "content": "旧工具调用前文本",
                        "tool_calls": [
                            {
                                "id": "call-old-search",
                                "type": "function",
                                "function": {"name": "search_files", "arguments": "{}"},
                            }
                        ],
                    },
                    {
                        "role": "tool",
                        "content": "{}",
                        "tool_name": "search_files",
                        "tool_call_id": "call-old-search",
                    },
                    {"role": "assistant", "content": "旧工具测试总结"},
                ]
            )
            messages.append(
                {
                    "role": "user",
                    "content": "启动团队任务",
                    "metadata": {
                        "run_id": "team-dispatch-run-current",
                        "turn_id": "team-dispatch-turn-current",
                    },
                }
            )
            messages.append(
                {
                    "role": "assistant",
                    "content": "准备启动团队任务",
                    "tool_calls": [
                        {
                            "id": "call-start",
                            "type": "function",
                            "function": {"name": "team_mission_start_task", "arguments": "{}"},
                        }
                    ],
                }
            )
            messages.append(
                {
                    "role": "tool",
                    "content": "{}",
                    "tool_name": "team_mission_start_task",
                    "tool_call_id": "call-start",
                }
            )
            messages.append({"role": "assistant", "content": "任务已经进入规划"})

            agent._flush_messages_to_session_db(messages, None)

            rows = db.messages.list(visible_session_id)
            assert [row["content"] for row in rows] == [
                "准备启动团队任务",
                {},
                "任务已经进入规划",
            ]
            assert [row.get("tool_name") for row in rows] == [None, "team_mission_start_task", None]
            assert all(row["metadata"]["run_id"] == "team-dispatch-run-current" for row in rows)
            assert all(row["metadata"]["activity_id"] == "act-team_dispatch-current" for row in rows)
            assert "search_files" not in {row.get("tool_name") for row in rows}
            assert agent._last_flushed_db_idx == len(messages)

    def test_team_projected_summary_is_not_reflushed_by_generic_writer(self):
        """Stable team projection rows are owned by their upsert writer."""
        from hermes_agent.composition.cli_session_store import open_cli_session_store

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"
            db = open_cli_session_store(db_path=db_path)

            visible_session_id = "team-session-team-conversation-test"
            stable_id = "team-mission-summary:mission-1:completed"
            agent = self._make_uninitialized_agent(db, session_id="runtime-summary-session")
            db.sessions.create(visible_session_id, source="team_mission", transient=False)
            db.session_index.upsert(
                session_id=visible_session_id,
                source="team_mission",
                conversation_kind="team",
                started_at=1.0,
                updated_at=1.0,
            )
            db.messages.append(
                visible_session_id,
                role="assistant",
                content="最终汇总",
                conversation_message_id=stable_id,
                metadata={
                    "conversation_message_id": stable_id,
                    "transcript_activity_kind": "mission_summary",
                    "team_mission": {"kind": "mission_summary", "mission_id": "mission-1"},
                },
            )

            agent._hermes_active_run_id = "team-dispatch-run-current"
            agent._hermes_active_turn_id = "team-dispatch-turn-current"
            agent._hermes_active_runtime_scope_key = "team:conversation-1"
            agent.run_context = SimpleNamespace(
                conversation_session_id=visible_session_id,
                activity_id="act-team_dispatch-1",
                activity_kind="team_dispatch",
                execution_scope_key="team:conversation-1",
                participant_id="leader:conversation-1",
                to_payload=lambda: {
                    "conversation_session_id": visible_session_id,
                    "activity_id": "act-team_dispatch-1",
                    "activity_kind": "team_dispatch",
                    "execution_scope_key": "team:conversation-1",
                    "participant_id": "leader:conversation-1",
                },
            )
            messages = [
                {
                    "role": "assistant",
                    "content": "最终汇总",
                    "conversation_message_id": stable_id,
                    "metadata": {
                        "conversation_message_id": stable_id,
                        "transcript_activity_kind": "mission_summary",
                        "team_mission": {"kind": "mission_summary", "mission_id": "mission-1"},
                    },
                }
            ]

            agent._flush_messages_to_session_db(messages, [])

            rows = db.messages.list(visible_session_id)
            assert len(rows) == 1
            assert rows[0]["conversation_message_id"] == stable_id
            assert agent._last_flushed_db_idx == len(messages)

    def test_persist_session_multiple_calls_no_duplication(self):
        """Multiple _persist_session calls don't duplicate DB entries."""
        from hermes_agent.composition.cli_session_store import open_cli_session_store

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"
            db = open_cli_session_store(db_path=db_path)

            agent = self._make_agent(db)

            conversation_history = [{"role": "user", "content": "old"}]
            messages = list(conversation_history) + [
                {"role": "user", "content": "q1"},
                {"role": "assistant", "content": "a1"},
                {"role": "user", "content": "q2"},
                {"role": "assistant", "content": "a2"},
            ]

            # Simulate multiple persist calls (like the agent's many exit paths)
            for _ in range(5):
                agent._persist_session(messages, conversation_history)

            rows = db.messages.list(agent.session_id)
            assert len(rows) == 4, f"Expected 4 messages, got {len(rows)} (duplication bug!)"

    def test_flush_reset_after_compression(self):
        """After compression creates a new session, flush index resets."""
        from hermes_agent.composition.cli_session_store import open_cli_session_store

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"
            db = open_cli_session_store(db_path=db_path)

            agent = self._make_agent(db)

            # Write some messages
            messages = [
                {"role": "user", "content": "msg1"},
                {"role": "assistant", "content": "reply1"},
            ]
            agent._flush_messages_to_session_db(messages, [])

            old_session = agent.session_id
            assert agent._last_flushed_db_idx == 2

            # Simulate what _compress_context does: new session, reset idx
            agent.session_id = "compressed-session-new"
            db.sessions.create(session_id=agent.session_id, source="test")
            agent._last_flushed_db_idx = 0

            # Now flush compressed messages to new session
            compressed_messages = [
                {"role": "user", "content": "summary of conversation"},
            ]
            agent._flush_messages_to_session_db(compressed_messages, [])

            new_rows = db.messages.list(agent.session_id)
            assert len(new_rows) == 1

            # Old session should still have its 2 messages
            old_rows = db.messages.list(old_session)
            assert len(old_rows) == 2


# ---------------------------------------------------------------------------
# Test: append_to_transcript skip_db parameter
# ---------------------------------------------------------------------------

class TestAppendToTranscriptSkipDb:
    """Verify skip_db=True skips the SQLite write."""

    def test_skip_db_prevents_sqlite_write(self, tmp_path):
        """With skip_db=True and a real DB, message does NOT appear in SQLite."""
        from hermes_gateway.config import GatewayConfig
        from hermes_gateway.session import SessionStore
        from hermes_agent.repositories.session_repo import SessionRepoImpl, SessionSpec
        from hermes_agent.composition.session_repository_db import connect_session_repository_db

        conn = connect_session_repository_db(tmp_path / "test_skip.db")
        session_repo = SessionRepoImpl(conn)

        config = GatewayConfig()
        with patch("hermes_gateway.session.SessionStore._ensure_loaded"):
            store = SessionStore(
                sessions_dir=tmp_path,
                config=config,
                session_repo=session_repo,
                storage_conn=conn,
            )
        store._loaded = True

        session_id = "test-skip-db-real"
        session_repo.create(SessionSpec(session_id=session_id, source="test"))

        msg = {"role": "assistant", "content": "hello world"}
        store.append_to_transcript(session_id, msg, skip_db=True)

        # SQLite should NOT have the message
        rows = conn.execute("SELECT * FROM messages WHERE session_id = ?", (session_id,)).fetchall()
        assert len(rows) == 0, f"Expected 0 DB rows with skip_db=True, got {len(rows)}"

    def test_default_writes_to_sqlite(self, tmp_path):
        """Without skip_db, message appears in SQLite."""
        from hermes_gateway.config import GatewayConfig
        from hermes_gateway.session import SessionStore
        from hermes_agent.repositories.session_repo import SessionRepoImpl, SessionSpec
        from hermes_agent.composition.session_repository_db import connect_session_repository_db

        conn = connect_session_repository_db(tmp_path / "test_both.db")
        session_repo = SessionRepoImpl(conn)

        config = GatewayConfig()
        with patch("hermes_gateway.session.SessionStore._ensure_loaded"):
            store = SessionStore(
                sessions_dir=tmp_path,
                config=config,
                session_repo=session_repo,
                storage_conn=conn,
            )
        store._loaded = True

        session_id = "test-default-write"
        session_repo.create(SessionSpec(session_id=session_id, source="test"))

        msg = {"role": "user", "content": "test message"}
        store.append_to_transcript(session_id, msg)

        # SQLite should have the message
        rows = conn.execute("SELECT * FROM messages WHERE session_id = ?", (session_id,)).fetchall()
        assert len(rows) == 1


# ---------------------------------------------------------------------------
# Test: _last_flushed_db_idx initialization
# ---------------------------------------------------------------------------

class TestFlushIdxInit:
    """Verify _last_flushed_db_idx is properly initialized."""

    def _make_agent(self, *, session_db=None):
        from run_agent import AIAgent

        agent = object.__new__(AIAgent)
        agent.session_id = "test-session-860"
        agent.platform = "test"
        agent.model = "test/model"
        agent._session_db = session_db
        agent._session_db_created = False
        agent._session_init_model_config = None
        agent._cached_system_prompt = None
        agent._parent_session_id = None
        agent._last_flushed_db_idx = 0
        agent._persist_user_message_idx = None
        agent._persist_user_message_override = None
        agent._hermes_active_run_id = ""
        agent._hermes_active_turn_id = ""
        agent._hermes_active_runtime_scope_key = ""
        agent.run_context = None
        agent._run_context = None
        return agent

    def test_init_zero(self):
        """Agent starts with _last_flushed_db_idx = 0."""
        agent = self._make_agent()
        assert agent._last_flushed_db_idx == 0

    def test_no_session_db_noop(self):
        """Without session_db, flush is a no-op and doesn't crash."""
        agent = self._make_agent()
        messages = [{"role": "user", "content": "test"}]
        agent._flush_messages_to_session_db(messages, [])
        # Should not crash, idx should remain 0
        assert agent._last_flushed_db_idx == 0
