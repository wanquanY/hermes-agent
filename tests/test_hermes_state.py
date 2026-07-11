"""Tests for hermes_state.py — CliSessionStore SQLite CRUD, FTS5 search, export."""

import sqlite3
import time
import pytest
from pathlib import Path

from hermes_conversation_message_identity import AssistantMessageIdentity
from hermes_conversation_message_identity import assistant_conversation_message_id_for
from hermes_agent.domain.session_service import SessionService
from hermes_agent.read_models.session_recall import contains_cjk, sanitize_fts5_query
from hermes_agent.storage.cli_session_store import open_cli_session_store
from hermes_agent.storage.migration_operations import parse_schema_columns
from hermes_agent.storage.migrations import CURRENT_SCHEMA_VERSION
from hermes_agent.storage.state_schema import SCHEMA_SQL


@pytest.fixture()
def db(tmp_path):
    """Create a CliSessionStore with a temp database file."""
    db_path = tmp_path / "test_state.db"
    session_db = open_cli_session_store(db_path=db_path)
    yield session_db
    session_db.close()


def _business_payload(
    event: dict,
    *,
    session_id: str = "stored-1",
    execution_session_id: str = "runtime-1",
) -> dict:
    payload = dict(event.get("payload") or {})
    assert payload.pop("session_id") == session_id
    assert payload.pop("conversation_session_id") == session_id
    assert payload.pop("execution_session_id") == execution_session_id
    return payload


def test_create_activity_persists_target_team_id(db):
    row = db.activities.create(
        activity_id="act-team",
        conversation_id="conv-team",
        kind="team_dispatch",
        target_team_id="team-1",
        target_mission_id="mission-1",
        prompt_summary="Release readiness",
    )

    assert row["target_team_id"] == "team-1"
    assert row["prompt_summary"] == "Release readiness"
    assert db.activities.get("act-team")["target_team_id"] == "team-1"
    assert db.activities.list("conv-team")[0]["target_team_id"] == "team-1"


def test_existing_activities_table_migrates_target_team_id_column(tmp_path):
    db_path = tmp_path / "legacy_state.db"
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            CREATE TABLE activities (
                activity_id TEXT PRIMARY KEY,
                conversation_id TEXT NOT NULL,
                parent_activity_id TEXT,
                kind TEXT NOT NULL CHECK (kind IN ('chat', 'agent_dispatch', 'team_dispatch', 'member_chat')),
                target_profile_id TEXT,
                target_mission_id TEXT,
                status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'running', 'completed', 'failed', 'cancelled')),
                prompt_summary TEXT,
                result_summary TEXT,
                result_json TEXT,
                started_at REAL,
                completed_at REAL,
                notify_parent INTEGER NOT NULL DEFAULT 1,
                read_at REAL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            )
            """
        )
        conn.execute(
            """
            INSERT INTO activities (
                activity_id, conversation_id, parent_activity_id, kind,
                target_profile_id, target_mission_id, status,
                prompt_summary, result_summary, result_json,
                started_at, completed_at, notify_parent, read_at,
                created_at, updated_at
            )
            VALUES (
                'legacy-act', 'conv-team', NULL, 'team_dispatch',
                NULL, 'legacy-mission', 'pending',
                'Legacy dispatch', NULL, NULL,
                NULL, NULL, 1, NULL,
                1, 1
            )
            """
        )
        conn.commit()
    finally:
        conn.close()

    migrated = open_cli_session_store(db_path=db_path)
    try:
        legacy_row = migrated.activities.get("legacy-act")
        assert legacy_row["target_team_id"] is None

        new_row = migrated.activities.create(
            activity_id="new-act",
            conversation_id="conv-team",
            kind="team_dispatch",
            target_team_id="team-1",
        )
        assert new_row["target_team_id"] == "team-1"
    finally:
        migrated.close()


def test_existing_session_info_events_backfill_session_runtime_state(tmp_path):
    db_path = tmp_path / "legacy_state.db"
    legacy = open_cli_session_store(db_path=db_path)
    try:
        legacy.runs.append_event(
            "stored-1",
            {
                "type": "session.info",
                "session_id": "runtime-1",
                "conversation_session_id": "stored-1",
                "run_id": "run-1",
                "turn_id": "turn-1",
                "runtime_scope_key": "profile:agent-default",
                "seq": 1,
                "payload": {"status": "starting", "model": "old-model"},
            },
        )
        legacy.runs.append_event(
            "stored-1",
            {
                "type": "session.info",
                "session_id": "runtime-1",
                "conversation_session_id": "stored-1",
                "run_id": "run-1",
                "turn_id": "turn-1",
                "runtime_scope_key": "profile:agent-default",
                "seq": 2,
                "payload": {"status": "running", "model": "new-model"},
            },
        )
        legacy._conn.execute("DELETE FROM session_runtime_state")
        legacy._conn.execute("UPDATE schema_version SET version = 32")
        legacy._conn.commit()
    finally:
        legacy.close()

    migrated = open_cli_session_store(db_path=db_path)
    try:
        state = migrated.runs.runtime_state("stored-1")

        assert state["runtime_scope_key"] == "profile:agent-default"
        assert state["execution_session_id"] == "runtime-1"
        assert state["run_id"] == "run-1"
        assert state["turn_id"] == "turn-1"
        assert state["status"] == "running"
        assert state["model"] == "new-model"
        assert state["source_seq"] == 2
    finally:
        migrated.close()


def test_list_run_events_filtered_filters_subagent_events_at_db_boundary(db):
    db.runs.append_event(
        "stored-1",
        {
            "type": "subagent.start",
            "session_id": "runtime-1",
            "conversation_session_id": "stored-1",
            "run_id": "run-1",
            "runtime_scope_key": "scope-1",
            "seq": 1,
            "payload": {"subagent_id": "sa-1", "goal": "查看目录"},
        },
    )
    db.runs.append_event(
        "stored-1",
        {
            "type": "tool.complete",
            "session_id": "runtime-1",
            "conversation_session_id": "stored-1",
            "run_id": "run-1",
            "runtime_scope_key": "scope-1",
            "seq": 2,
            "payload": {"tool_name": "terminal"},
        },
    )
    db.runs.append_event(
        "stored-1",
        {
            "type": "subagent.output_delta",
            "session_id": "runtime-1",
            "conversation_session_id": "stored-1",
            "run_id": "run-1",
            "runtime_scope_key": "scope-1",
            "seq": 3,
            "payload": {"subagent_id": "sa-2", "text": "输出"},
        },
    )

    snapshots = db.runs.list_filtered_events(
        "stored-1",
        runtime_scope_key="scope-1",
        event_types=["subagent.start", "subagent.complete"],
    )
    selected = db.runs.list_filtered_events(
        "stored-1",
        runtime_scope_key="scope-1",
        event_type_prefix="subagent.",
        payload_contains="sa-2",
    )

    assert [event["type"] for event in snapshots] == ["subagent.start"]
    assert [event["seq"] for event in selected] == [3]


def test_append_run_event_coalesces_adjacent_subagent_output_deltas(db):
    db.runs.append_event(
        "stored-1",
        {
            "type": "subagent.output_delta",
            "session_id": "runtime-1",
            "conversation_session_id": "stored-1",
            "run_id": "run-1",
            "seq": 1,
            "payload": {"subagent_id": "sa-1", "text": "第一"},
        },
    )
    db.runs.append_event(
        "stored-1",
        {
            "type": "subagent.output_delta",
            "session_id": "runtime-1",
            "conversation_session_id": "stored-1",
            "run_id": "run-1",
            "seq": 2,
            "payload": {"subagent_id": "sa-1", "text": "段"},
        },
    )
    db.runs.append_event(
        "stored-1",
        {
            "type": "subagent.tool",
            "session_id": "runtime-1",
            "conversation_session_id": "stored-1",
            "run_id": "run-1",
            "seq": 3,
            "payload": {"subagent_id": "sa-1", "tool_name": "terminal"},
        },
    )
    db.runs.append_event(
        "stored-1",
        {
            "type": "subagent.output_delta",
            "session_id": "runtime-1",
            "conversation_session_id": "stored-1",
            "run_id": "run-1",
            "seq": 4,
            "payload": {"subagent_id": "sa-1", "text": "第二段"},
        },
    )

    events = db.runs.list_filtered_events("stored-1", event_type_prefix="subagent.", limit=10)

    assert [event["type"] for event in events] == [
        "subagent.output_delta",
        "subagent.tool",
        "subagent.output_delta",
    ]
    assert [event["seq"] for event in events] == [2, 3, 4]
    assert events[0]["payload"]["text"] == "第一段"
    assert events[2]["payload"]["text"] == "第二段"


def test_append_run_event_coalesces_adjacent_subagent_reasoning_deltas(db):
    db.runs.append_event(
        "stored-1",
        {
            "type": "subagent.reasoning_delta",
            "session_id": "runtime-1",
            "conversation_session_id": "stored-1",
            "run_id": "run-1",
            "seq": 1,
            "payload": {
                "subagent_id": "sa-1",
                "source": "provider_reasoning",
                "text": "先分析",
            },
        },
    )
    db.runs.append_event(
        "stored-1",
        {
            "type": "subagent.reasoning_delta",
            "session_id": "runtime-1",
            "conversation_session_id": "stored-1",
            "run_id": "run-1",
            "seq": 2,
            "payload": {
                "subagent_id": "sa-1",
                "source": "provider_reasoning",
                "text": "再验证",
            },
        },
    )
    db.runs.append_event(
        "stored-1",
        {
            "type": "subagent.reasoning_delta",
            "session_id": "runtime-1",
            "conversation_session_id": "stored-1",
            "run_id": "run-1",
            "seq": 3,
            "payload": {
                "subagent_id": "sa-2",
                "source": "provider_reasoning",
                "text": "另一个子 agent",
            },
        },
    )

    events = db.runs.list_filtered_events("stored-1", event_types=["subagent.reasoning_delta"], limit=10)

    assert [event["seq"] for event in events] == [2, 3]
    assert events[0]["payload"]["text"] == "先分析再验证"
    assert events[0]["payload"]["subagent_id"] == "sa-1"
    assert events[1]["payload"]["text"] == "另一个子 agent"
    assert events[1]["payload"]["subagent_id"] == "sa-2"


def test_append_run_event_preserves_adjacent_main_message_deltas(db):
    db.runs.append_event(
        "stored-1",
        {
            "type": "message.delta",
            "session_id": "runtime-1",
            "conversation_session_id": "stored-1",
            "run_id": "run-1",
            "turn_id": "turn-1",
            "seq": 1,
            "payload": {"mode": "append", "text": "你", "delta": "你", "offset": 0},
        },
    )
    db.runs.append_event(
        "stored-1",
        {
            "type": "message.delta",
            "session_id": "runtime-1",
            "conversation_session_id": "stored-1",
            "run_id": "run-1",
            "turn_id": "turn-1",
            "seq": 2,
            "payload": {"mode": "append", "text": "好", "delta": "好", "offset": 1},
        },
    )
    db.runs.append_event(
        "stored-1",
        {
            "type": "message.delta",
            "session_id": "runtime-1",
            "conversation_session_id": "stored-1",
            "run_id": "run-1",
            "turn_id": "turn-1",
            "seq": 3,
            "payload": {"mode": "snapshot", "text": "你好啊", "snapshot": "你好啊", "offset": 0},
        },
    )
    db.runs.append_event(
        "stored-1",
        {
            "type": "message.delta",
            "session_id": "runtime-1",
            "conversation_session_id": "stored-1",
            "run_id": "run-1",
            "turn_id": "turn-1",
            "seq": 4,
            "payload": {"mode": "append", "text": "。", "delta": "。", "offset": 3},
        },
    )

    events = db.runs.list_filtered_events("stored-1", event_types=["message.delta"], limit=10)

    assert [event["seq"] for event in events] == [1, 2, 3, 4]
    assert _business_payload(events[0]) == {
        "mode": "append",
        "text": "你",
        "delta": "你",
        "offset": 0,
    }
    assert _business_payload(events[1]) == {
        "mode": "append",
        "text": "好",
        "delta": "好",
        "offset": 1,
    }
    assert events[2]["payload"]["mode"] == "snapshot"
    assert events[2]["payload"]["snapshot"] == "你好啊"
    assert events[3]["payload"]["text"] == "。"


def test_append_run_event_skips_delta_coalesce_when_target_seq_is_occupied(db):
    db.runs.append_event(
        "stored-1",
        {
            "type": "message.delta",
            "session_id": "runtime-1",
            "conversation_session_id": "stored-1",
            "run_id": "run-1",
            "turn_id": "turn-1",
            "runtime_scope_key": "team:mission-1:leader",
            "seq": 1,
            "payload": {"mode": "append", "text": "你", "delta": "你", "offset": 0},
        },
    )
    db.runs.append_event(
        "stored-1",
        {
            "type": "tool.start",
            "session_id": "runtime-2",
            "conversation_session_id": "stored-1",
            "run_id": "run-2",
            "turn_id": "turn-2",
            "runtime_scope_key": "team:mission-1:node:worker",
            "seq": 2,
            "payload": {"name": "team_mission_node_start"},
        },
    )

    db.runs.append_event(
        "stored-1",
        {
            "type": "message.delta",
            "session_id": "runtime-1",
            "conversation_session_id": "stored-1",
            "run_id": "run-1",
            "turn_id": "turn-1",
            "runtime_scope_key": "team:mission-1:leader",
            "seq": 2,
            "payload": {"mode": "append", "text": "好", "delta": "好", "offset": 1},
        },
    )

    events = db.runs.list_events("stored-1")

    assert [event["seq"] for event in events] == [1, 2, 3]
    assert [event["type"] for event in events] == ["message.delta", "tool.start", "message.delta"]
    assert _business_payload(events[2]) == {
        "mode": "append",
        "text": "好",
        "delta": "好",
        "offset": 1,
    }


def test_append_run_event_preserves_message_deltas_across_tool_events(db):
    db.runs.append_event(
        "stored-1",
        {
            "type": "message.delta",
            "session_id": "runtime-1",
            "conversation_session_id": "stored-1",
            "run_id": "run-1",
            "turn_id": "turn-1",
            "seq": 1,
            "payload": {"mode": "append", "text": "A", "delta": "A", "offset": 0},
        },
    )
    db.runs.append_event(
        "stored-1",
        {
            "type": "tool.progress",
            "session_id": "runtime-1",
            "conversation_session_id": "stored-1",
            "run_id": "run-1",
            "turn_id": "turn-1",
            "seq": 2,
            "payload": {"tool_name": "terminal", "text": "running"},
        },
    )
    db.runs.append_event(
        "stored-1",
        {
            "type": "message.delta",
            "session_id": "runtime-1",
            "conversation_session_id": "stored-1",
            "run_id": "run-1",
            "turn_id": "turn-1",
            "seq": 3,
            "payload": {"mode": "append", "text": "B", "delta": "B", "offset": 1},
        },
    )

    events = db.runs.list_filtered_events("stored-1", limit=10)
    message_events = [event for event in events if event["type"] == "message.delta"]

    assert [event["type"] for event in events] == ["message.delta", "tool.progress", "message.delta"]
    assert [event["seq"] for event in message_events] == [1, 3]
    assert _business_payload(message_events[0]) == {
        "mode": "append",
        "text": "A",
        "delta": "A",
        "offset": 0,
    }
    assert _business_payload(message_events[1]) == {
        "mode": "append",
        "text": "B",
        "delta": "B",
        "offset": 1,
    }


def test_append_run_event_treats_session_recalled_as_terminal_boundary(db):
    db.runs.append_event(
        "stored-1",
        {
            "type": "message.delta",
            "session_id": "runtime-1",
            "conversation_session_id": "stored-1",
            "run_id": "run-1",
            "turn_id": "turn-1",
            "seq": 1,
            "payload": {"mode": "append", "text": "A", "delta": "A", "offset": 0},
        },
    )

    recalled = db.runs.append_event(
        "stored-1",
        {
            "type": "session.recalled",
            "session_id": "runtime-1",
            "conversation_session_id": "stored-1",
            "run_id": "run-1",
            "turn_id": "turn-1",
            "seq": 2,
            "payload": {
                "run_id": "run-1",
                "turn_id": "turn-1",
                "removed_messages": 2,
                "messages": [],
            },
        },
    )

    events = db.runs.list_events("stored-1", run_id="run-1")
    run = db.runs.get("run-1")

    assert recalled["seq"] == 2
    assert [event["type"] for event in events] == ["session.recalled"]
    assert events[-1]["payload"]["run_id"] == "run-1"
    assert run is not None
    assert run["status"] == "interrupted"
    assert run["last_seq"] == 2


def test_append_run_event_does_not_coalesce_message_delta_when_offset_restarts(db):
    db.runs.append_event(
        "stored-1",
        {
            "type": "message.delta",
            "session_id": "runtime-1",
            "conversation_session_id": "stored-1",
            "run_id": "run-1",
            "turn_id": "turn-1",
            "seq": 1,
            "payload": {"mode": "append", "text": "你", "delta": "你", "offset": 0},
        },
    )
    db.runs.append_event(
        "stored-1",
        {
            "type": "message.delta",
            "session_id": "runtime-1",
            "conversation_session_id": "stored-1",
            "run_id": "run-1",
            "turn_id": "turn-1",
            "seq": 2,
            "payload": {"mode": "append", "text": "好", "delta": "好", "offset": 1},
        },
    )
    db.runs.append_event(
        "stored-1",
        {
            "type": "message.delta",
            "session_id": "runtime-1",
            "conversation_session_id": "stored-1",
            "run_id": "run-1",
            "turn_id": "turn-1",
            "seq": 3,
            "payload": {"mode": "append", "text": "重新开始", "delta": "重新开始", "offset": 0},
        },
    )

    events = db.runs.list_filtered_events("stored-1", event_types=["message.delta"], limit=10)

    assert [event["seq"] for event in events] == [1, 2, 3]
    assert _business_payload(events[0]) == {
        "mode": "append",
        "text": "你",
        "delta": "你",
        "offset": 0,
    }
    assert _business_payload(events[1]) == {
        "mode": "append",
        "text": "好",
        "delta": "好",
        "offset": 1,
    }
    assert _business_payload(events[2]) == {
        "mode": "append",
        "text": "重新开始",
        "delta": "重新开始",
        "offset": 0,
    }


def test_append_run_event_preserves_cumulative_message_delta_as_source_event(db):
    prefix = "## ✅ 团队任务执行结果整合\n\n本"
    full = (
        "## ✅ 团队任务执行结果整合\n\n"
        "本次团队任务目标：**创建一个测试文件。**\n\n"
        "---\n\n"
        "## 一、执行结果\n\n"
        "```text\n"
        "创建测试文件 → 验证测试文件\n"
        "```"
    )
    db.runs.append_event(
        "stored-1",
        {
            "type": "message.delta",
            "session_id": "runtime-1",
            "conversation_session_id": "stored-1",
            "run_id": "run-1",
            "turn_id": "turn-1",
            "seq": 1,
            "payload": {"mode": "append", "text": prefix, "delta": prefix},
        },
    )
    db.runs.append_event(
        "stored-1",
        {
            "type": "message.delta",
            "session_id": "runtime-1",
            "conversation_session_id": "stored-1",
            "run_id": "run-1",
            "turn_id": "turn-1",
            "seq": 2,
            "payload": {"mode": "append", "text": full, "delta": full},
        },
    )

    events = db.runs.list_filtered_events("stored-1", event_types=["message.delta"], limit=10)

    assert [event["seq"] for event in events] == [1, 2]
    assert events[0]["payload"]["text"] == prefix
    assert events[0]["payload"]["delta"] == prefix
    assert events[1]["payload"]["text"] == full
    assert events[1]["payload"]["delta"] == full


def test_append_run_event_coalesces_adjacent_reasoning_deltas_by_source(db):
    db.runs.append_event(
        "stored-1",
        {
            "type": "reasoning.delta",
            "session_id": "runtime-1",
            "conversation_session_id": "stored-1",
            "run_id": "run-1",
            "turn_id": "turn-1",
            "seq": 1,
            "payload": {"text": "思考", "source": "provider_reasoning"},
        },
    )
    db.runs.append_event(
        "stored-1",
        {
            "type": "reasoning.delta",
            "session_id": "runtime-1",
            "conversation_session_id": "stored-1",
            "run_id": "run-1",
            "turn_id": "turn-1",
            "seq": 2,
            "payload": {"text": "过程", "source": "provider_reasoning"},
        },
    )
    db.runs.append_event(
        "stored-1",
        {
            "type": "reasoning.delta",
            "session_id": "runtime-1",
            "conversation_session_id": "stored-1",
            "run_id": "run-1",
            "turn_id": "turn-1",
            "seq": 3,
            "payload": {"text": "不同来源", "source": "other"},
        },
    )

    events = db.runs.list_filtered_events("stored-1", event_types=["reasoning.delta"], limit=10)

    assert [event["seq"] for event in events] == [2, 3]
    assert _business_payload(events[0]) == {
        "text": "思考过程",
        "source": "provider_reasoning",
    }
    assert _business_payload(events[1]) == {"text": "不同来源", "source": "other"}


def test_append_run_event_prunes_on_terminal_event(db, monkeypatch):
    calls = []
    monkeypatch.setattr(
        db.runs.retention,
        "prune",
        lambda **kwargs: calls.append(kwargs) or {"deleted_events": 0},
    )

    db.runs.append_event(
        "stored-1",
        {
            "type": "message.delta",
            "session_id": "runtime-1",
            "conversation_session_id": "stored-1",
            "run_id": "run-1",
            "seq": 7,
            "payload": {"text": "still running"},
        },
    )
    db.runs.append_event(
        "stored-1",
        {
            "type": "message.complete",
            "session_id": "runtime-1",
            "conversation_session_id": "stored-1",
            "run_id": "run-1",
            "seq": 8,
            "payload": {"status": "completed", "text": "done"},
        },
    )

    assert calls == [{"session_id": "stored-1"}]


def test_append_run_event_compacts_on_terminal_event(db, monkeypatch):
    calls = []
    original_compact = db.runs.retention.prune_terminal_streams

    def compact_spy(**kwargs):
        calls.append(kwargs)
        return original_compact(**kwargs)

    monkeypatch.setattr(db.runs.retention, "prune_terminal_streams", compact_spy)
    db.runs.append_event(
        "stored-1",
        {
            "type": "message.delta",
            "session_id": "runtime-1",
            "conversation_session_id": "stored-1",
            "run_id": "run-1",
            "turn_id": "turn-1",
            "seq": 1,
            "payload": {"mode": "append", "text": "A", "delta": "A", "offset": 0},
        },
    )
    db.runs.append_event(
        "stored-1",
        {
            "type": "message.complete",
            "session_id": "runtime-1",
            "conversation_session_id": "stored-1",
            "run_id": "run-1",
            "turn_id": "turn-1",
            "seq": 2,
            "payload": {"status": "completed", "text": "A"},
        },
    )

    assert calls == [{"session_id": "stored-1", "run_id": "run-1"}]


def test_append_session_info_updates_runtime_state_and_deduplicates_raw_rows(db):
    payload = {
        "status": "starting",
        "model": "test-model",
        "provider": "test-provider",
        "profile": {"id": "agent-default", "name": "Default"},
    }
    first = db.runs.append_event(
        "stored-1",
        {
            "type": "session.info",
            "session_id": "runtime-1",
            "conversation_session_id": "stored-1",
            "run_id": "run-1",
            "turn_id": "turn-1",
            "runtime_scope_key": "profile:agent-default",
            "seq": 1,
            "payload": payload,
        },
    )
    duplicate = db.runs.append_event(
        "stored-1",
        {
            "type": "session.info",
            "session_id": "runtime-1",
            "conversation_session_id": "stored-1",
            "run_id": "run-1",
            "turn_id": "turn-1",
            "runtime_scope_key": "profile:agent-default",
            "seq": 2,
            "payload": dict(payload),
        },
    )

    state = db.runs.runtime_state("stored-1")
    events = db.runs.list_events("stored-1")

    assert first["seq"] == 1
    assert duplicate["_persistence_disposition"] == "duplicate_session_info"
    assert duplicate["seq"] == 1
    assert state["runtime_scope_key"] == "profile:agent-default"
    assert state["execution_session_id"] == "runtime-1"
    assert state["run_id"] == "run-1"
    assert state["turn_id"] == "turn-1"
    assert state["status"] == "starting"
    assert state["model"] == "test-model"
    assert state["provider"] == "test-provider"
    assert state["profile"] == {"id": "agent-default", "name": "Default"}
    assert state["source_seq"] == 1
    assert [event["type"] for event in events] == ["session.info"]


def test_append_session_info_same_payload_new_run_is_not_deduplicated(db):
    payload = {"status": "starting", "model": "test-model"}
    db.runs.append_event(
        "stored-1",
        {
            "type": "session.info",
            "session_id": "runtime-1",
            "conversation_session_id": "stored-1",
            "run_id": "run-1",
            "turn_id": "turn-1",
            "runtime_scope_key": "profile:agent-default",
            "seq": 1,
            "payload": payload,
        },
    )
    second = db.runs.append_event(
        "stored-1",
        {
            "type": "session.info",
            "session_id": "runtime-2",
            "conversation_session_id": "stored-1",
            "run_id": "run-2",
            "turn_id": "turn-2",
            "runtime_scope_key": "profile:agent-default",
            "seq": 2,
            "payload": dict(payload),
        },
    )

    state = db.runs.runtime_state("stored-1")
    events = db.runs.list_events("stored-1")

    assert "_persistence_disposition" not in second
    assert state["execution_session_id"] == "runtime-2"
    assert state["run_id"] == "run-2"
    assert state["turn_id"] == "turn-2"
    assert state["source_seq"] == 2
    assert [event["seq"] for event in events] == [1, 2]


def test_prune_duplicate_session_info_events_keeps_latest_duplicate(db):
    db.sessions.create("stored-1", source="test")
    payload_a = '{"status":"starting","model":"test-model"}'
    payload_b = '{"status":"running","model":"test-model"}'
    for seq, payload in ((1, payload_a), (2, payload_a), (3, payload_b), (4, payload_b)):
        db._conn.execute(
            """
            INSERT INTO run_events (
                session_id, run_id, turn_id, execution_session_id, runtime_scope_key,
                event_type, seq, timestamp, payload_json, event_json, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "stored-1",
                "run-1",
                "turn-1",
                "runtime-1",
                "profile:agent-default",
                "session.info",
                seq,
                float(seq),
                payload,
                (
                    '{"type":"session.info","session_id":"runtime-1",'
                    '"conversation_session_id":"stored-1","run_id":"run-1",'
                    '"turn_id":"turn-1","runtime_scope_key":"profile:agent-default",'
                    f'"seq":{seq},"timestamp":{float(seq)},"payload":{payload}}}'
                ),
                "",
            ),
        )

    result = db.run_event_maintenance.prune_duplicate_session_info(session_id="stored-1")
    events = db.runs.list_events("stored-1")
    archive = db._conn.execute(  # noqa: SLF001 - storage contract assertion.
        "SELECT reason, event_count, first_seq, last_seq FROM run_event_archives"
    ).fetchone()

    assert result["deleted_events"] == 2
    assert [event["seq"] for event in events] == [2, 4]
    assert [event["payload"]["status"] for event in events] == ["starting", "running"]
    assert archive["reason"] == "duplicate_session_info"
    assert archive["event_count"] == 2
    assert archive["first_seq"] == 1
    assert archive["last_seq"] == 3


def test_append_run_event_deduplicates_repeated_terminal_for_run(db):
    db.runs.append_event(
        "stored-1",
        {
            "type": "message.delta",
            "session_id": "runtime-1",
            "conversation_session_id": "stored-1",
            "run_id": "run-1",
            "turn_id": "turn-1",
            "seq": 1,
            "payload": {"text": "running"},
        },
    )
    first = db.runs.append_event(
        "stored-1",
        {
            "type": "message.complete",
            "session_id": "runtime-1",
            "conversation_session_id": "stored-1",
            "run_id": "run-1",
            "turn_id": "turn-1",
            "seq": 2,
            "payload": {"status": "complete", "text": "done"},
        },
    )
    duplicate = db.runs.append_event(
        "stored-1",
        {
            "type": "message.complete",
            "session_id": "runtime-1",
            "conversation_session_id": "stored-1",
            "run_id": "run-1",
            "turn_id": "turn-1",
            "seq": 3,
            "payload": {"status": "complete", "text": "done again"},
        },
    )

    events = db.runs.list_events("stored-1")
    run = db.runs.get("run-1")

    assert first["seq"] == 2
    assert duplicate["_persistence_disposition"] == "duplicate_terminal"
    assert duplicate["seq"] == 2
    assert [event["type"] for event in events] == ["message.complete"]
    assert events[-1]["payload"]["text"] == "done"
    assert run["status"] == "completed"
    assert run["last_seq"] == 2


def _create_team_conversation_session(db, session_id="team-session-team-conversation-test") -> str:
    db.sessions.create(session_id, "dovie")
    db.session_index.upsert(
        session_id=session_id,
        source="team_mission",
        session_kind="team_mission",
        conversation_kind="team",
    )
    return session_id


def test_append_run_event_projects_team_member_message_complete_to_read_model(db):
    session_id = _create_team_conversation_session(db)

    event = db.runs.append_event(
        session_id,
        {
            "type": "message.complete",
            "session_id": "runtime-member-1",
            "conversation_session_id": session_id,
            "run_id": "team-member-run-1",
            "turn_id": "team-member-turn-1",
            "message_seq_in_run": 1,
            "runtime_scope_key": "member-chat:team-conversation-test:member-1",
            "participant_id": "member:member-1",
            "seq": 10,
            "timestamp": 1000,
            "payload": {
                "status": "complete",
                "text": "member response",
                "activity_id": f"act-member_chat:{session_id}:member-1",
            },
        },
    )
    conversation_message_id = assistant_conversation_message_id_for(
        AssistantMessageIdentity(
            session_id=session_id,
            run_id="team-member-run-1",
            message_seq_in_run="1",
        )
    )

    messages = db.messages.all_as_conversation(
        session_id,
        include_storage_metadata=True,
    )

    assert event["_projected_message_id"] == conversation_message_id
    assert len(messages) == 1
    assert messages[0]["role"] == "assistant"
    assert messages[0]["content"] == "member response"
    assert messages[0]["participant_id"] == "member:member-1"
    assert messages[0]["conversation_message_id"] == conversation_message_id
    assert messages[0]["metadata"]["transcript_activity_kind"] == "member_direct_chat"


def test_team_message_complete_event_does_not_duplicate_late_worker_flush(db):
    session_id = _create_team_conversation_session(db)
    db.runs.append_event(
        session_id,
        {
            "type": "message.complete",
            "session_id": "runtime-member-1",
            "conversation_session_id": session_id,
            "run_id": "team-member-run-1",
            "turn_id": "team-member-turn-1",
            "message_seq_in_run": 1,
            "runtime_scope_key": "member-chat:team-conversation-test:member-1",
            "participant_id": "member:member-1",
            "seq": 10,
            "timestamp": 1000,
            "payload": {
                "status": "complete",
                "text": "member response",
                "activity_id": f"act-member_chat:{session_id}:member-1",
            },
        },
    )
    db.messages.append(
        session_id,
        "assistant",
        "member response",
        participant_id="member:member-1",
        metadata={
            "run_id": "team-member-run-1",
            "turn_id": "team-member-turn-1",
        },
    )

    messages = db.messages.all_as_conversation(
        session_id,
        include_storage_metadata=True,
    )

    assert [message["role"] for message in messages] == ["assistant"]
    assert [message["content"] for message in messages] == ["member response"]
    assert messages[0].get("conversation_message_id")


def test_team_message_complete_event_does_not_claim_early_worker_flush(db):
    session_id = _create_team_conversation_session(db)
    legacy_message_id = db.messages.append(
        session_id,
        "assistant",
        "member response",
        participant_id="member:member-1",
        reasoning="native reasoning",
        metadata={
            "run_id": "team-member-run-1",
            "turn_id": "team-member-turn-1",
        },
    )

    event = db.runs.append_event(
        session_id,
        {
            "type": "message.complete",
            "session_id": "runtime-member-1",
            "conversation_session_id": session_id,
            "run_id": "team-member-run-1",
            "turn_id": "team-member-turn-1",
            "message_seq_in_run": 1,
            "runtime_scope_key": "member-chat:team-conversation-test:member-1",
            "participant_id": "member:member-1",
            "seq": 10,
            "timestamp": 1000,
            "payload": {
                "status": "complete",
                "text": "member response",
                "activity_id": f"act-member_chat:{session_id}:member-1",
            },
        },
    )

    messages = db.messages.all_as_conversation(
        session_id,
        include_storage_metadata=True,
    )

    assert len(messages) == 1
    assert messages[0]["message_id"] == str(legacy_message_id)
    assert event.get("_projected_message_id")
    assert messages[0].get("conversation_message_id")
    assert messages[0]["content"] == "member response"
    assert messages[0]["reasoning"] == "native reasoning"


def test_append_run_event_does_not_project_team_mission_node_message_complete(db):
    session_id = "team:mission-test:node:root"
    db.sessions.create(session_id, "dovie")

    event = db.runs.append_event(
        session_id,
        {
            "type": "message.complete",
            "session_id": "runtime-node-1",
            "conversation_session_id": session_id,
            "run_id": "node-run-1",
            "turn_id": "node-turn-1",
            "message_seq_in_run": 1,
            "runtime_scope_key": "profile:agent-default",
            "seq": 3,
            "timestamp": 1000,
            "payload": {"status": "complete", "text": "node result"},
        },
    )

    messages = db.messages.all_as_conversation(
        session_id,
        include_storage_metadata=True,
    )

    assert "_projected_message_id" not in event
    assert messages == []


def test_append_run_event_ignores_stream_events_after_terminal_for_run(db):
    db.runs.append_event(
        "stored-1",
        {
            "type": "message.delta",
            "session_id": "runtime-1",
            "conversation_session_id": "stored-1",
            "run_id": "run-1",
            "turn_id": "turn-1",
            "seq": 1,
            "payload": {"text": "running"},
        },
    )
    terminal = db.runs.append_event(
        "stored-1",
        {
            "type": "message.complete",
            "session_id": "runtime-1",
            "conversation_session_id": "stored-1",
            "run_id": "run-1",
            "turn_id": "turn-1",
            "seq": 2,
            "payload": {"status": "complete", "text": "done"},
        },
    )
    late_delta = db.runs.append_event(
        "stored-1",
        {
            "type": "message.delta",
            "session_id": "runtime-1",
            "conversation_session_id": "stored-1",
            "run_id": "run-1",
            "turn_id": "turn-1",
            "seq": 3,
            "payload": {"text": "late"},
        },
    )

    events = db.runs.list_events("stored-1")
    run = db.runs.get("run-1")

    assert terminal["seq"] == 2
    assert late_delta["_persistence_disposition"] == "ignored_after_terminal"
    assert late_delta["seq"] == 3
    assert [event["type"] for event in events] == ["message.complete"]
    assert events[-1]["payload"]["text"] == "done"
    assert run["status"] == "completed"
    assert run["last_seq"] == 2


def test_append_run_event_allows_higher_priority_terminal_upgrade(db):
    db.runs.append_event(
        "stored-1",
        {
            "type": "message.complete",
            "session_id": "runtime-1",
            "conversation_session_id": "stored-1",
            "run_id": "run-1",
            "turn_id": "turn-1",
            "seq": 1,
            "payload": {"status": "failed", "message": "worker closed"},
        },
    )
    db.runs.append_event(
        "stored-1",
        {
            "type": "message.complete",
            "session_id": "runtime-1",
            "conversation_session_id": "stored-1",
            "run_id": "run-1",
            "turn_id": "turn-1",
            "seq": 2,
            "payload": {"status": "complete", "text": "final answer"},
        },
    )

    events = db.runs.list_events("stored-1")
    run = db.runs.get("run-1")

    assert [event["payload"]["status"] for event in events] == ["failed", "complete"]
    assert run["status"] == "completed"
    assert run["last_seq"] == 2


def test_compact_run_events_prunes_terminal_stream_rows_from_old_database(db):
    db.runs.upsert(
        run_id="run-1",
        session_id="stored-1",
        runtime_scope_key="stored-1",
        turn_id="turn-1",
        execution_session_id="runtime-1",
        status="completed",
    )

    prunable_rows = (
        ("message.delta", '{"mode":"append","text":"A","delta":"A","offset":0}'),
        ("reasoning.delta", '{"mode":"append","delta":"thinking"}'),
        ("thinking.delta", '{"mode":"append","delta":"thinking"}'),
        ("subagent.output_delta", '{"mode":"append","delta":"worker"}'),
        ("subagent.reasoning_delta", '{"mode":"append","delta":"worker-thinking"}'),
        ("subagent.thinking", '{"mode":"append","delta":"worker-thought"}'),
        ("agent_profile_test.output_delta", '{"mode":"append","delta":"profile"}'),
        ("agent_profile_test.thinking", '{"mode":"append","delta":"profile-thinking"}'),
        ("tool.progress", '{"message":"working"}'),
        ("tool.generating", '{"message":"generating"}'),
    )

    def insert_run_event(seq: int, event_type: str, payload: str, status: str = "") -> None:
        db._conn.execute(
            """
            INSERT INTO run_events (
                session_id, run_id, turn_id, execution_session_id, runtime_scope_key,
                event_type, seq, timestamp, payload_json, event_json, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "stored-1",
                "run-1",
                "turn-1",
                "runtime-1",
                "stored-1",
                event_type,
                seq,
                float(seq),
                payload,
                (
                    f'{{"type":"{event_type}","session_id":"runtime-1",'
                    '"conversation_session_id":"stored-1","run_id":"run-1",'
                    '"turn_id":"turn-1","runtime_scope_key":"stored-1",'
                    f'"seq":{seq},"timestamp":{float(seq)},"payload":{payload}}}'
                ),
                status,
            ),
        )
    for seq, (event_type, payload) in enumerate(prunable_rows, start=1):
        insert_run_event(seq, event_type, payload)
    insert_run_event(
        len(prunable_rows) + 1,
        "message.complete",
        '{"status":"complete","text":"final"}',
        "completed",
    )
    result = db.run_event_maintenance.compact(session_id="stored-1")
    events = db.runs.list_events("stored-1")
    run = db.runs.get("run-1")
    archive = db._conn.execute(  # noqa: SLF001 - storage contract assertion.
        "SELECT reason, event_count, first_seq, last_seq FROM run_event_archives"
    ).fetchone()

    assert result["pruned_terminal_stream_events"] == len(prunable_rows)
    assert result["deleted_events"] == len(prunable_rows)
    assert [event["type"] for event in events] == ["message.complete"]
    assert events[0]["payload"]["status"] == "complete"
    assert events[0]["payload"]["text"] == "final"
    assert run["last_seq"] == len(prunable_rows) + 1
    assert archive["reason"] == "terminal_run_stream_events"
    assert archive["event_count"] == len(prunable_rows)
    assert archive["first_seq"] == 1
    assert archive["last_seq"] == len(prunable_rows)


def test_compact_run_events_preserves_tool_complete_for_terminal_run(db):
    db.runs.upsert(
        run_id="run-1",
        session_id="stored-1",
        runtime_scope_key="stored-1",
        turn_id="turn-1",
        execution_session_id="runtime-1",
        status="completed",
    )

    rows = (
        (
            1,
            "tool.complete",
            '{"tool_name":"terminal","result":"important output"}',
            "",
        ),
        (
            2,
            "message.delta",
            '{"mode":"append","text":"A","delta":"A","offset":0}',
            "",
        ),
        (
            3,
            "message.complete",
            '{"status":"complete","text":"final"}',
            "completed",
        ),
    )
    for seq, event_type, payload, status in rows:
        db._conn.execute(
            """
            INSERT INTO run_events (
                session_id, run_id, turn_id, execution_session_id, runtime_scope_key,
                event_type, seq, timestamp, payload_json, event_json, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "stored-1",
                "run-1",
                "turn-1",
                "runtime-1",
                "stored-1",
                event_type,
                seq,
                float(seq),
                payload,
                (
                    f'{{"type":"{event_type}","session_id":"runtime-1",'
                    '"conversation_session_id":"stored-1","run_id":"run-1",'
                    '"turn_id":"turn-1","runtime_scope_key":"stored-1",'
                    f'"seq":{seq},"timestamp":{float(seq)},"payload":{payload}}}'
                ),
                status,
            ),
        )
    result = db.run_event_maintenance.compact(session_id="stored-1")
    events = db.runs.list_events("stored-1")

    assert result["pruned_terminal_stream_events"] == 1
    assert result["deleted_events"] == 1
    assert [event["type"] for event in events] == ["tool.complete", "message.complete"]
    assert events[0]["payload"]["tool_name"] == "terminal"
    assert events[0]["payload"]["result"] == "important output"
    assert events[1]["payload"]["status"] == "complete"
    assert events[1]["payload"]["text"] == "final"


def test_compact_run_events_preserves_active_message_delta_rows(db):
    db.runs.upsert(
        run_id="run-1",
        session_id="stored-1",
        runtime_scope_key="stored-1",
        turn_id="turn-1",
        execution_session_id="runtime-1",
        status="running",
    )
    for seq, payload in (
        (1, '{"mode":"append","text":"A","delta":"A","offset":0}'),
        (2, '{"mode":"append","text":"B","delta":"B","offset":1}'),
        (3, '{"mode":"append","text":"RESET","delta":"RESET","offset":0}'),
    ):
        db._conn.execute(
            """
            INSERT INTO run_events (
                session_id, run_id, turn_id, execution_session_id, runtime_scope_key,
                event_type, seq, timestamp, payload_json, event_json, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "stored-1",
                "run-1",
                "turn-1",
                "runtime-1",
                "stored-1",
                "message.delta",
                seq,
                float(seq),
                payload,
                (
                    '{"type":"message.delta","session_id":"runtime-1",'
                    '"conversation_session_id":"stored-1","run_id":"run-1",'
                    '"turn_id":"turn-1","runtime_scope_key":"stored-1",'
                    f'"seq":{seq},"timestamp":{float(seq)},"payload":{payload}}}'
                ),
                "",
            ),
        )

    result = db.run_event_maintenance.compact(session_id="stored-1")
    events = db.runs.list_filtered_events("stored-1", event_types=["message.delta"], limit=10)

    assert result["deleted_events"] == 0
    assert result["pruned_terminal_stream_events"] == 0
    assert [event["seq"] for event in events] == [1, 2, 3]
    assert {
        key: events[0]["payload"][key] for key in ("mode", "text", "delta", "offset")
    } == {"mode": "append", "text": "A", "delta": "A", "offset": 0}
    assert {
        key: events[1]["payload"][key] for key in ("mode", "text", "delta", "offset")
    } == {"mode": "append", "text": "B", "delta": "B", "offset": 1}
    assert {
        key: events[2]["payload"][key] for key in ("mode", "text", "delta", "offset")
    } == {"mode": "append", "text": "RESET", "delta": "RESET", "offset": 0}


def test_compact_run_events_prunes_terminal_stream_rows_without_run_row(db):
    db.sessions.create("stored-1", source="test")
    for seq, event_type, payload, status in (
        (1, "message.delta", '{"mode":"append","text":"A","delta":"A","offset":0}', ""),
        (2, "message.complete", '{"status":"complete","text":"A"}', "completed"),
    ):
        db._conn.execute(
            """
            INSERT INTO run_events (
                session_id, run_id, turn_id, execution_session_id, runtime_scope_key,
                event_type, seq, timestamp, payload_json, event_json, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "stored-1",
                "run-missing",
                "turn-1",
                "runtime-1",
                "stored-1",
                event_type,
                seq,
                float(seq),
                payload,
                (
                    f'{{"type":"{event_type}","session_id":"runtime-1",'
                    '"conversation_session_id":"stored-1","run_id":"run-missing",'
                    '"turn_id":"turn-1","runtime_scope_key":"stored-1",'
                    f'"seq":{seq},"timestamp":{float(seq)},"payload":{payload}}}'
                ),
                status,
            ),
        )

    result = db.run_event_maintenance.compact(session_id="stored-1")
    events = db.runs.list_events("stored-1")

    assert result["pruned_terminal_stream_events"] == 1
    assert result["deleted_events"] == 1
    assert [event["type"] for event in events] == ["message.complete"]
    assert db.runs.get("run-missing") is None


def test_compact_run_events_deduplicates_existing_terminal_rows(db):
    db.runs.upsert(
        run_id="run-1",
        session_id="stored-1",
        runtime_scope_key="profile:agent-default",
        turn_id="turn-1",
        execution_session_id="runtime-1",
        status="completed",
    )
    for seq, text in ((11, "first complete"), (12, "latest complete")):
        db._conn.execute(
            """
            INSERT INTO run_events (
                session_id, run_id, turn_id, execution_session_id, runtime_scope_key,
                event_type, seq, timestamp, payload_json, event_json, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "stored-1",
                "run-1",
                "turn-1",
                "runtime-1",
                "profile:agent-default",
                "message.complete",
                seq,
                float(seq),
                f'{{"status":"complete","text":"{text}"}}',
                f'{{"type":"message.complete","session_id":"runtime-1","conversation_session_id":"stored-1","run_id":"run-1","turn_id":"turn-1","runtime_scope_key":"profile:agent-default","seq":{seq},"payload":{{"status":"complete","text":"{text}"}}}}',
                "completed",
            ),
        )

    result = db.run_event_maintenance.compact(session_id="stored-1")
    events = db.runs.list_events("stored-1")
    run = db.runs.get("run-1")

    assert result["deduplicated_terminal_groups"] == 1
    assert result["deleted_events"] == 1
    assert [event["seq"] for event in events] == [11]
    assert events[0]["payload"]["text"] == "latest complete"
    assert run["last_seq"] == 11


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


# =========================================================================
# FTS5 search
# =========================================================================

class TestFTS5Search:
    def test_search_finds_content(self, db):
        db.sessions.create(session_id="s1", source="cli")
        db.messages.append("s1", role="user", content="How do I deploy with Docker?")
        db.messages.append("s1", role="assistant", content="Use docker compose up.")

        results = db.messages.search("docker")
        assert len(results) == 2
        # At least one result should mention docker
        snippets = [r.get("snippet", "") for r in results]
        assert any("docker" in s.lower() or "Docker" in s for s in snippets)

    def test_search_empty_query(self, db):
        assert db.messages.search("") == []
        assert db.messages.search("   ") == []

    def test_search_with_source_filter(self, db):
        db.sessions.create(session_id="s1", source="cli")
        db.messages.append("s1", role="user", content="CLI question about Python")

        db.sessions.create(session_id="s2", source="telegram")
        db.messages.append("s2", role="user", content="Telegram question about Python")

        results = db.messages.search("Python", source_filter=["telegram"])
        # Should only find the telegram message
        sources = [r["source"] for r in results]
        assert all(s == "telegram" for s in sources)

    def test_search_default_sources_include_acp(self, db):
        db.sessions.create(session_id="s1", source="acp")
        db.messages.append("s1", role="user", content="ACP question about Python")

        results = db.messages.search("Python")
        sources = [r["source"] for r in results]
        assert "acp" in sources

    def test_search_default_includes_all_platforms(self, db):
        """Default search (no source_filter) should find sessions from any platform."""
        for src in ("cli", "telegram", "signal", "homeassistant", "acp", "matrix"):
            sid = f"s-{src}"
            db.sessions.create(session_id=sid, source=src)
            db.messages.append(sid, role="user", content=f"universal search test from {src}")

        results = db.messages.search("universal search test")
        found_sources = {r["source"] for r in results}
        assert found_sources == {"cli", "telegram", "signal", "homeassistant", "acp", "matrix"}

    def test_search_with_role_filter(self, db):
        db.sessions.create(session_id="s1", source="cli")
        db.messages.append("s1", role="user", content="What is FastAPI?")
        db.messages.append("s1", role="assistant", content="FastAPI is a web framework.")

        results = db.messages.search("FastAPI", role_filter=["assistant"])
        roles = [r["role"] for r in results]
        assert all(r == "assistant" for r in roles)

    def test_search_returns_context(self, db):
        db.sessions.create(session_id="s1", source="cli")
        db.messages.append("s1", role="user", content="Tell me about Kubernetes")
        db.messages.append("s1", role="assistant", content="Kubernetes is an orchestrator.")

        results = db.messages.search("Kubernetes")
        assert len(results) == 2
        assert "context" in results[0]
        assert isinstance(results[0]["context"], list)
        assert len(results[0]["context"]) > 0

    def test_search_context_uses_session_neighbors_when_ids_are_interleaved(self, db):
        db.sessions.create(session_id="s1", source="cli")
        db.sessions.create(session_id="s2", source="cli")

        db.messages.append("s1", role="user", content="before needle")
        db.messages.append("s2", role="user", content="other session message")
        db.messages.append("s1", role="assistant", content="needle match")
        db.messages.append("s2", role="assistant", content="another other session message")
        db.messages.append("s1", role="user", content="after needle")

        results = db.messages.search('"needle match"')
        needle_result = next(r for r in results if r["session_id"] == "s1" and "needle match" in r["snippet"])

        assert [msg["content"] for msg in needle_result["context"]] == [
            "before needle",
            "needle match",
            "after needle",
        ]

    def test_search_special_chars_do_not_crash(self, db):
        """FTS5 special characters in queries must not raise OperationalError."""
        db.sessions.create(session_id="s1", source="cli")
        db.messages.append("s1", role="user", content="How do I use C++ templates?")

        # Each of these previously caused sqlite3.OperationalError
        dangerous_queries = [
            'C++',              # + is FTS5 column filter
            '"unterminated',    # unbalanced double-quote
            '(problem',         # unbalanced parenthesis
            'hello AND',        # dangling boolean operator
            '***',              # repeated wildcard
            '{test}',           # curly braces (column reference)
            'OR hello',         # leading boolean operator
            'a AND OR b',       # adjacent operators
        ]
        for query in dangerous_queries:
            # Must not raise — should return list (possibly empty)
            results = db.messages.search(query)
            assert isinstance(results, list), f"Query {query!r} did not return a list"

    def test_search_sanitized_query_still_finds_content(self, db):
        """Sanitization must not break normal keyword search."""
        db.sessions.create(session_id="s1", source="cli")
        db.messages.append("s1", role="user", content="Learning C++ templates today")

        # "C++" sanitized to "C" should still match "C++"
        results = db.messages.search("C++")
        # The word "C" appears in the content, so FTS5 should find it
        assert isinstance(results, list)

    def test_search_hyphenated_term_does_not_crash(self, db):
        """Hyphenated terms like 'chat-send' must not crash FTS5."""
        db.sessions.create(session_id="s1", source="cli")
        db.messages.append("s1", role="user", content="Run the chat-send command")

        results = db.messages.search("chat-send")
        assert isinstance(results, list)
        assert len(results) >= 1
        assert any("chat-send" in (r.get("snippet") or r.get("content", "")).lower()
                    for r in results)

    def test_search_dotted_term_does_not_crash(self, db):
        """Dotted terms like 'P2.2' or 'simulate.p2.test.ts' should not crash FTS5."""
        db.sessions.create(session_id="s1", source="cli")
        db.messages.append("s1", role="user", content="Working on P2.2 session_search edge cases")
        db.messages.append("s1", role="assistant", content="See simulate.p2.test.ts for details")

        results = db.messages.search("P2.2")
        assert isinstance(results, list)
        assert len(results) >= 1

        results2 = db.messages.search("simulate.p2.test.ts")
        assert isinstance(results2, list)
        assert len(results2) >= 1

    def test_search_quoted_phrase_preserved(self, db):
        """User-provided quoted phrases should be preserved for exact matching."""
        db.sessions.create(session_id="s1", source="cli")
        db.messages.append("s1", role="user", content="docker networking is complex")
        db.messages.append("s1", role="assistant", content="networking docker tips")

        # Quoted phrase should match only the exact order
        results = db.messages.search('"docker networking"')
        assert isinstance(results, list)
        # Should find the user message (exact phrase) but may or may not find
        # the assistant message depending on FTS5 phrase matching
        assert len(results) >= 1

    def test_sanitize_fts5_query_strips_dangerous_chars(self):
        """Unit test for _sanitize_fts5_query static method."""
        s = sanitize_fts5_query
        assert s('hello world') == 'hello world'
        assert '+' not in s('C++')
        assert '"' not in s('"unterminated')
        assert '(' not in s('(problem')
        assert '{' not in s('{test}')
        # Dangling operators removed
        assert s('hello AND') == 'hello'
        assert s('OR world') == 'world'
        # Leading bare * removed
        assert s('***') == ''
        # Valid prefix kept
        assert s('deploy*') == 'deploy*'

    def test_sanitize_fts5_preserves_quoted_phrases(self):
        """Properly paired double-quoted phrases should be preserved."""
        s = sanitize_fts5_query
        # Simple quoted phrase
        assert s('"exact phrase"') == '"exact phrase"'
        # Quoted phrase alongside unquoted terms
        assert '"docker networking"' in s('"docker networking" setup')
        # Multiple quoted phrases
        result = s('"hello world" OR "foo bar"')
        assert '"hello world"' in result
        assert '"foo bar"' in result
        # Unmatched quote still stripped
        assert '"' not in s('"unterminated')

    def test_sanitize_fts5_quotes_hyphenated_terms(self):
        """Hyphenated terms should be wrapped in quotes for exact matching."""
        s = sanitize_fts5_query
        # Simple hyphenated term
        assert s('chat-send') == '"chat-send"'
        # Multiple hyphens
        assert s('docker-compose-up') == '"docker-compose-up"'
        # Hyphenated term with other words
        result = s('fix chat-send bug')
        assert '"chat-send"' in result
        assert 'fix' in result
        assert 'bug' in result
        # Multiple hyphenated terms with OR
        result = s('chat-send OR deploy-prod')
        assert '"chat-send"' in result
        assert '"deploy-prod"' in result
        # Already-quoted hyphenated term — no double quoting
        assert s('"chat-send"') == '"chat-send"'
        # Hyphenated inside a quoted phrase stays as-is
        assert s('"my chat-send thing"') == '"my chat-send thing"'

    def test_sanitize_fts5_quotes_dotted_terms(self):
        """Dotted terms should be wrapped in quotes to avoid FTS5 query parse edge cases."""
        s = sanitize_fts5_query

        assert s('P2.2') == '"P2.2"'
        assert s('simulate.p2') == '"simulate.p2"'
        assert s('simulate.p2.test.ts') == '"simulate.p2.test.ts"'

        # Already quoted — no double quoting
        assert s('"P2.2"') == '"P2.2"'

        # Works with boolean syntax
        result = s('P2.2 OR simulate.p2')
        assert '"P2.2"' in result
        assert '"simulate.p2"' in result

        # Mixed dots and hyphens — single pass avoids double-quoting
        assert s('my-app.config') == '"my-app.config"'
        assert s('my-app.config.ts') == '"my-app.config.ts"'

    def test_sanitize_fts5_quotes_underscored_terms(self):
        """Underscored terms should be wrapped in quotes for exact matching.

        FTS5 default tokenizer splits 'sp_new1' into tokens 'sp' and 'new1'.
        Without quoting, a search for 'sp_new' becomes an AND query
        ('sp AND new') that fails to match rows indexed as 'sp_new1'.
        """
        s = sanitize_fts5_query
        # Simple underscored term
        assert s('sp_new') == '"sp_new"'
        # Multiple underscores
        assert s('a_b_c') == '"a_b_c"'
        # Mixed underscores and hyphens/dots — single pass avoids double-quoting
        assert s('sp_new1') == '"sp_new1"'
        assert s('docker-compose_up') == '"docker-compose_up"'
        assert s('my.app_config.ts') == '"my.app_config.ts"'
        # Already-quoted — no double quoting
        assert s('"sp_new"') == '"sp_new"'
        # Mixed with other words
        result = s('sp_new and 血管瘤')
        assert '"sp_new"' in result
        assert '血管瘤' in result


# =========================================================================
# CJK (Chinese/Japanese/Korean) LIKE fallback
# =========================================================================

class TestCJKSearchFallback:
    """Regression tests for CJK search (see #11511).

    SQLite FTS5's default tokenizer treats contiguous CJK runs as a single
    token ("和其他agent的聊天记录" → one token), so substring queries like
    "记忆断裂" return 0 rows despite the data being present. CliSessionStore falls
    back to LIKE substring matching whenever FTS5 returns no results and
    the query contains CJK characters.
    """

    def test_cjk_detection_covers_all_ranges(self):
        f = contains_cjk
        # Chinese (CJK Unified Ideographs)
        assert f("记忆断裂") is True
        # Japanese Hiragana + Katakana
        assert f("こんにちは") is True
        assert f("カタカナ") is True
        # Korean Hangul syllables (both early and late — guards against
        # the \ud7a0-\ud7af typo seen in one of the duplicate PRs)
        assert f("안녕하세요") is True
        assert f("기억") is True
        # Non-CJK
        assert f("hello world") is False
        assert f("日本語mixedwithenglish") is True
        assert f("") is False

    def test_chinese_multichar_query_returns_results(self, db):
        """The headline bug: multi-char Chinese query must not return []."""
        db.sessions.create(session_id="s1", source="cli")
        db.messages.append(
            "s1", role="user",
            content="昨天和其他Agent的聊天记录，记忆断裂问题复现了",
        )
        results = db.messages.search("记忆断裂")
        assert len(results) == 1
        assert results[0]["session_id"] == "s1"

    def test_chinese_bigram_query(self, db):
        db.sessions.create(session_id="s1", source="telegram")
        db.messages.append("s1", role="user", content="今天讨论A2A通信协议的实现")
        results = db.messages.search("通信")
        assert len(results) == 1

    def test_korean_query_returns_results(self, db):
        """Guards against Hangul range typos (\\uac00-\\ud7af, not \\ud7a0-)."""
        db.sessions.create(session_id="s1", source="cli")
        db.messages.append("s1", role="user", content="안녕하세요 반갑습니다")
        results = db.messages.search("안녕")
        assert len(results) == 1

    def test_japanese_query_returns_results(self, db):
        db.sessions.create(session_id="s1", source="cli")
        db.messages.append("s1", role="user", content="こんにちは世界")
        assert len(db.messages.search("こんにちは")) == 1
        assert len(db.messages.search("世界")) == 1

    def test_cjk_fallback_preserves_source_filter(self, db):
        """Guards against the SQL-builder bug where filter clauses land
        after LIMIT/OFFSET (seen in one of the duplicate PRs)."""
        db.sessions.create(session_id="s1", source="cli")
        db.sessions.create(session_id="s2", source="telegram")
        db.messages.append("s1", role="user", content="记忆断裂在CLI")
        db.messages.append("s2", role="user", content="记忆断裂在Telegram")

        results = db.messages.search("记忆断裂", source_filter=["telegram"])
        assert len(results) == 1
        assert results[0]["source"] == "telegram"

    def test_cjk_fallback_preserves_exclude_sources(self, db):
        db.sessions.create(session_id="s1", source="cli")
        db.sessions.create(session_id="s2", source="tool")
        db.messages.append("s1", role="user", content="记忆断裂在CLI")
        db.messages.append("s2", role="assistant", content="记忆断裂在tool")

        results = db.messages.search("记忆断裂", exclude_sources=["tool"])
        sources = {r["source"] for r in results}
        assert "tool" not in sources
        assert "cli" in sources

    def test_cjk_fallback_preserves_role_filter(self, db):
        db.sessions.create(session_id="s1", source="cli")
        db.messages.append("s1", role="user", content="用户说的记忆断裂")
        db.messages.append("s1", role="assistant", content="助手说的记忆断裂")

        results = db.messages.search("记忆断裂", role_filter=["assistant"])
        assert len(results) == 1
        assert results[0]["role"] == "assistant"

    def test_cjk_snippet_is_centered_on_match(self, db):
        """Snippet should contain the search term, not just the first N chars."""
        db.sessions.create(session_id="s1", source="cli")
        long_prefix = "这是一段很长的前缀用来把匹配位置推到文档中间" * 3
        long_suffix = "这是一段很长的后缀内容填充剩余空间" * 3
        db.messages.append(
            "s1", role="user",
            content=f"{long_prefix}记忆断裂{long_suffix}",
        )
        results = db.messages.search("记忆断裂")
        assert len(results) == 1
        # The centered substr() snippet must include the matched term.
        assert "记忆断裂" in results[0]["snippet"]

    def test_english_query_still_uses_fts5_fast_path(self, db):
        """English queries must not trigger the LIKE fallback (fast path regression)."""
        db.sessions.create(session_id="s1", source="cli")
        db.messages.append("s1", role="user", content="Deploy docker containers")
        results = db.messages.search("docker")
        assert len(results) == 1
        # No CJK in query → LIKE fallback must not run. We don't assert this
        # directly (no instrumentation), but the FTS5 path produces an
        # FTS5-style snippet with highlight markers when the term is short.
        # At minimum: english queries must still match.

    def test_cjk_query_with_no_matches_returns_empty(self, db):
        db.sessions.create(session_id="s1", source="cli")
        db.messages.append("s1", role="user", content="unrelated English content")
        results = db.messages.search("记忆断裂")
        assert results == []

    def test_mixed_cjk_english_query(self, db):
        """Mixed queries should still fall back to LIKE when FTS5 misses."""
        db.sessions.create(session_id="s1", source="cli")
        db.messages.append("s1", role="user", content="讨论Agent通信协议")
        # "Agent通信" is CJK+English — FTS5 default tokenizer indexes the
        # whole CJK run with embedded "agent" as separate tokens; the LIKE
        # fallback handles the substring correctly.
        results = db.messages.search("Agent通信")
        assert len(results) == 1

    def test_cjk_partial_fts5_results_supplemented_by_like(self, db):
        """When FTS5 returns *some* CJK results, LIKE must still find all matches.

        Regression test for #15500 / #14829: FTS5 unicode61 tokenizer drops
        certain CJK characters, so multi-character queries may return partial
        results.  The LIKE path must always run for CJK queries.
        """
        db.sessions.create(session_id="s1", source="cli")
        db.sessions.create(session_id="s2", source="telegram")
        db.messages.append("s1", role="user", content="昨晚讨论了记忆系统")
        db.messages.append("s2", role="user", content="昨晚的会议纪要已发送")
        results = db.messages.search("昨晚")
        assert len(results) == 2
        session_ids = {r["session_id"] for r in results}
        assert session_ids == {"s1", "s2"}

    def test_cjk_like_dedup_no_duplicates(self, db):
        """When FTS5 and LIKE both find the same message, no duplicates."""
        db.sessions.create(session_id="s1", source="cli")
        db.messages.append("s1", role="user", content="测试去重逻辑")
        results = db.messages.search("测试")
        assert len(results) == 1

    def test_cjk_like_escapes_wildcards(self, db):
        """Special characters (%, _) in CJK queries are treated as literals."""
        db.sessions.create(session_id="s1", source="cli")
        db.sessions.create(session_id="s2", source="cli")
        db.messages.append("s1", role="user", content="达成100%完成率")
        db.messages.append("s2", role="user", content="达成100完成率是目标")
        # The % in the query must be literal — should only match s1
        results = db.messages.search("100%完成")
        assert len(results) == 1
        assert results[0]["session_id"] == "s1"

    def test_cjk_trigram_preserves_boolean_operators(self, db):
        """Boolean operators (OR, AND, NOT) work in CJK trigram queries."""
        db.sessions.create(session_id="s1", source="cli")
        db.sessions.create(session_id="s2", source="cli")
        db.messages.append("s1", role="user", content="记忆系统很好用")
        db.messages.append("s2", role="user", content="断裂连接需要修复")
        results = db.messages.search("记忆系统 OR 断裂连接")
        assert len(results) == 2
        session_ids = {r["session_id"] for r in results}
        assert session_ids == {"s1", "s2"}

    def test_cjk_or_combined_short_tokens_returns_results(self, db):
        """Regression test for #20494.

        OR-combined 2-char CJK tokens (e.g. "广西 OR 桂林 OR 漓江 OR 旅游")
        previously returned 0 results because _count_cjk of the whole query
        was >=3 (8 chars here), selecting the trigram path, but each individual
        token is only 2 CJK chars and trigram requires >=3 chars per token.
        The per-token check must route such queries to the LIKE fallback.
        """
        db.sessions.create(session_id="s1", source="cli")
        db.sessions.create(session_id="s2", source="telegram")
        db.sessions.create(session_id="s3", source="cli")
        db.messages.append("s1", role="user", content="广西是个好地方，去过桂林")
        db.messages.append("s2", role="user", content="漓江风景很美，值得旅游")
        db.messages.append("s3", role="user", content="unrelated English content")

        results = db.messages.search("广西 OR 桂林 OR 漓江 OR 旅游")
        session_ids = {r["session_id"] for r in results}
        assert "s1" in session_ids, "广西/桂林 terms not matched"
        assert "s2" in session_ids, "漓江/旅游 terms not matched"
        assert "s3" not in session_ids, "unrelated message must not match"

    def test_cjk_short_token_or_query_preserves_filters(self, db):
        """Source filter applies correctly in the short-token LIKE path (#20494)."""
        db.sessions.create(session_id="s1", source="cli")
        db.sessions.create(session_id="s2", source="telegram")
        db.messages.append("s1", role="user", content="广西旅游攻略cli")
        db.messages.append("s2", role="user", content="广西旅游攻略telegram")

        results = db.messages.search("广西 OR 旅游", source_filter=["telegram"])
        assert len(results) == 1
        assert results[0]["source"] == "telegram"


# =========================================================================
# Session search and listing
# =========================================================================

class TestSearchSessions:
    def test_list_all_sessions(self, db):
        db.sessions.create(session_id="s1", source="cli")
        db.sessions.create(session_id="s2", source="telegram")

        sessions = db.sessions.search()
        assert len(sessions) == 2

    def test_filter_by_source(self, db):
        db.sessions.create(session_id="s1", source="cli")
        db.sessions.create(session_id="s2", source="telegram")

        sessions = db.sessions.search(source="cli")
        assert len(sessions) == 1
        assert sessions[0]["source"] == "cli"

    def test_pagination(self, db):
        for i in range(5):
            db.sessions.create(session_id=f"s{i}", source="cli")

        page1 = db.sessions.search(limit=2)
        page2 = db.sessions.search(limit=2, offset=2)
        assert len(page1) == 2
        assert len(page2) == 2
        assert page1[0]["id"] != page2[0]["id"]


# =========================================================================
# Counts
# =========================================================================

class TestCounts:
    def test_session_count(self, db):
        assert db.sessions.count() == 0
        db.sessions.create(session_id="s1", source="cli")
        db.sessions.create(session_id="s2", source="telegram")
        assert db.sessions.count() == 2

    def test_session_count_by_source(self, db):
        db.sessions.create(session_id="s1", source="cli")
        db.sessions.create(session_id="s2", source="telegram")
        db.sessions.create(session_id="s3", source="cli")
        assert db.sessions.count(source="cli") == 2
        assert db.sessions.count(source="telegram") == 1

    def test_message_count_total(self, db):
        assert db.messages.count() == 0
        db.sessions.create(session_id="s1", source="cli")
        db.messages.append("s1", role="user", content="Hello")
        db.messages.append("s1", role="assistant", content="Hi")
        assert db.messages.count() == 2

    def test_message_count_per_session(self, db):
        db.sessions.create(session_id="s1", source="cli")
        db.sessions.create(session_id="s2", source="cli")
        db.messages.append("s1", role="user", content="A")
        db.messages.append("s2", role="user", content="B")
        db.messages.append("s2", role="user", content="C")
        assert db.messages.count(session_id="s1") == 1
        assert db.messages.count(session_id="s2") == 2


# =========================================================================
# Delete and export
# =========================================================================

class TestDeleteAndExport:
    def test_delete_session(self, db):
        db.sessions.create(session_id="s1", source="cli")
        db.messages.append("s1", role="user", content="Hello")

        assert db.maintenance.delete_session("s1") is True
        assert db.sessions.get("s1") is None
        assert db.messages.count(session_id="s1") == 0

    def test_delete_branch_source_session_cleans_lineage_references(self, db):
        db.sessions.create("source", "tui")
        db.messages.append("source", role="user", content="branch from this")
        assistant_id = db.messages.append("source", role="assistant", content="answer")
        db.branches.branch_session(
            source_session_id="source",
            new_session_id="branch-1",
            branch_point={"message_id": str(assistant_id)},
            idempotency_key="branch-key-1",
        )

        assert db.maintenance.delete_session("source") is True

        branch = db.sessions.get("branch-1")
        branch_info = db.branches.get_session_branch_info("branch-1")
        branch_requests = db._conn.execute(
            "SELECT COUNT(*) FROM session_branch_requests"
        ).fetchone()[0]
        fk_errors = db._conn.execute("PRAGMA foreign_key_check").fetchall()

        assert db.sessions.get("source") is None
        assert branch is not None
        assert branch_info is not None
        assert branch_info["parent_session_id"] == ""
        assert branch_requests == 0
        assert fk_errors == []

    def test_delete_branch_result_session_cleans_lineage_and_idempotency(self, db):
        db.sessions.create("source", "tui")
        db.messages.append("source", role="user", content="branch from this")
        assistant_id = db.messages.append("source", role="assistant", content="answer")
        db.branches.branch_session(
            source_session_id="source",
            new_session_id="branch-1",
            branch_point={"message_id": str(assistant_id)},
            idempotency_key="branch-key-1",
        )

        assert db.maintenance.delete_session("branch-1") is True

        lineage_rows = db._conn.execute(
            "SELECT COUNT(*) FROM session_lineage WHERE session_id = ?",
            ("branch-1",),
        ).fetchone()[0]
        branch_requests = db._conn.execute(
            "SELECT COUNT(*) FROM session_branch_requests"
        ).fetchone()[0]
        fk_errors = db._conn.execute("PRAGMA foreign_key_check").fetchall()

        assert db.sessions.get("source") is not None
        assert db.sessions.get("branch-1") is None
        assert lineage_rows == 0
        assert branch_requests == 0
        assert fk_errors == []

    def test_delete_nonexistent(self, db):
        assert db.maintenance.delete_session("nope") is False

    def test_resolve_session_id_exact(self, db):
        db.sessions.create(session_id="20260315_092437_c9a6ff", source="cli")
        assert db.sessions.resolve_id("20260315_092437_c9a6ff") == "20260315_092437_c9a6ff"

    def test_resolve_session_id_unique_prefix(self, db):
        db.sessions.create(session_id="20260315_092437_c9a6ff", source="cli")
        assert db.sessions.resolve_id("20260315_092437_c9a6") == "20260315_092437_c9a6ff"

    def test_resolve_session_id_ambiguous_prefix_returns_none(self, db):
        db.sessions.create(session_id="20260315_092437_c9a6aa", source="cli")
        db.sessions.create(session_id="20260315_092437_c9a6bb", source="cli")
        assert db.sessions.resolve_id("20260315_092437_c9a6") is None

    def test_resolve_session_id_escapes_like_wildcards(self, db):
        db.sessions.create(session_id="20260315_092437_c9a6ff", source="cli")
        db.sessions.create(session_id="20260315X092437_c9a6ff", source="cli")
        assert db.sessions.resolve_id("20260315_092437") == "20260315_092437_c9a6ff"

    def test_export_session(self, db):
        db.sessions.create(session_id="s1", source="cli", model="test")
        db.messages.append("s1", role="user", content="Hello")
        db.messages.append("s1", role="assistant", content="Hi")

        export = db.sessions.export("s1")
        assert isinstance(export, dict)
        assert export["source"] == "cli"
        assert len(export["messages"]) == 2

    def test_export_nonexistent(self, db):
        assert db.sessions.export("nope") is None

    def test_export_all(self, db):
        db.sessions.create(session_id="s1", source="cli")
        db.sessions.create(session_id="s2", source="telegram")
        db.messages.append("s1", role="user", content="A")

        exports = db.sessions.export_all()
        assert len(exports) == 2

    def test_export_all_with_source(self, db):
        db.sessions.create(session_id="s1", source="cli")
        db.sessions.create(session_id="s2", source="telegram")

        exports = db.sessions.export_all(source="cli")
        assert len(exports) == 1
        assert exports[0]["source"] == "cli"


# =========================================================================
# Prune
# =========================================================================

class TestPruneSessions:
    def test_prune_old_ended_sessions(self, db):
        # Create and end an "old" session
        db.sessions.create(session_id="old", source="cli")
        db.sessions.end("old", reason="done")
        # Manually backdate started_at
        db._conn.execute(
            "UPDATE sessions SET started_at = ? WHERE id = ?",
            (time.time() - 100 * 86400, "old"),
        )
        db._conn.commit()

        # Create a recent session
        db.sessions.create(session_id="new", source="cli")

        pruned = db.maintenance.prune_sessions(older_than_days=90)
        assert pruned == 1
        assert db.sessions.get("old") is None
        session = db.sessions.get("new")
        assert session is not None
        assert session["id"] == "new"

    def test_prune_skips_active_sessions(self, db):
        db.sessions.create(session_id="active", source="cli")
        # Backdate but don't end
        db._conn.execute(
            "UPDATE sessions SET started_at = ? WHERE id = ?",
            (time.time() - 200 * 86400, "active"),
        )
        db._conn.commit()

        pruned = db.maintenance.prune_sessions(older_than_days=90)
        assert pruned == 0
        assert db.sessions.get("active") is not None

    def test_prune_with_source_filter(self, db):
        for sid, src in [("old_cli", "cli"), ("old_tg", "telegram")]:
            db.sessions.create(session_id=sid, source=src)
            db.sessions.end(sid, reason="done")
            db._conn.execute(
                "UPDATE sessions SET started_at = ? WHERE id = ?",
                (time.time() - 200 * 86400, sid),
            )
        db._conn.commit()

        pruned = db.maintenance.prune_sessions(older_than_days=90, source="cli")
        assert pruned == 1
        assert db.sessions.get("old_cli") is None
        assert db.sessions.get("old_tg") is not None

    def test_prune_with_multilevel_chain(self, db):
        """Pruning old sessions orphans newer children instead of crashing on FK."""
        old_ts = time.time() - 200 * 86400
        recent_ts = time.time() - 10 * 86400

        # Chain: A (old) -> B (old) -> C (recent) -> D (recent)
        db.sessions.create(session_id="A", source="cli")
        db.sessions.end("A", reason="compressed")
        db.sessions.create(session_id="B", source="cli", parent_session_id="A")
        db.sessions.end("B", reason="compressed")
        db.sessions.create(session_id="C", source="cli", parent_session_id="B")
        db.sessions.end("C", reason="compressed")
        db.sessions.create(session_id="D", source="cli", parent_session_id="C")
        db.sessions.end("D", reason="done")

        # Backdate A and B to be old; C and D stay recent
        for sid, ts in [("A", old_ts), ("B", old_ts), ("C", recent_ts), ("D", recent_ts)]:
            db._conn.execute(
                "UPDATE sessions SET started_at = ? WHERE id = ?", (ts, sid)
            )
        db._conn.commit()

        # Should not raise IntegrityError
        pruned = db.maintenance.prune_sessions(older_than_days=90)
        assert pruned == 2  # only A and B
        assert db.sessions.get("A") is None
        assert db.sessions.get("B") is None
        # C and D survive, C is orphaned (parent_session_id NULL)
        c = db.sessions.get("C")
        assert c is not None
        assert c["parent_session_id"] is None
        d = db.sessions.get("D")
        assert d is not None
        assert d["parent_session_id"] == "C"

    def test_prune_entire_old_chain(self, db):
        """All sessions in a chain are old — entire chain is pruned."""
        old_ts = time.time() - 200 * 86400

        db.sessions.create(session_id="X", source="cli")
        db.sessions.end("X", reason="compressed")
        db.sessions.create(session_id="Y", source="cli", parent_session_id="X")
        db.sessions.end("Y", reason="compressed")
        db.sessions.create(session_id="Z", source="cli", parent_session_id="Y")
        db.sessions.end("Z", reason="done")

        for sid in ("X", "Y", "Z"):
            db._conn.execute(
                "UPDATE sessions SET started_at = ? WHERE id = ?", (old_ts, sid)
            )
        db._conn.commit()

        pruned = db.maintenance.prune_sessions(older_than_days=90)
        assert pruned == 3
        for sid in ("X", "Y", "Z"):
            assert db.sessions.get(sid) is None


class TestDeleteSessionOrphansChildren:
    def test_delete_orphans_children(self, db):
        """Deleting a parent session orphans its children."""
        db.sessions.create(session_id="parent", source="cli")
        db.sessions.create(session_id="child", source="cli", parent_session_id="parent")
        db.sessions.create(session_id="grandchild", source="cli", parent_session_id="child")

        # Should not raise IntegrityError
        result = db.maintenance.delete_session("parent")
        assert result is True
        assert db.sessions.get("parent") is None
        # Child is orphaned, not deleted
        child = db.sessions.get("child")
        assert child is not None
        assert child["parent_session_id"] is None
        # Grandchild is untouched
        grandchild = db.sessions.get("grandchild")
        assert grandchild is not None
        assert grandchild["parent_session_id"] == "child"


def test_repair_orphaned_foreign_key_rows_removes_non_authoritative_dangling_rows(db):
    db._conn.execute("PRAGMA foreign_keys=OFF")
    db._conn.execute(
        """
        INSERT INTO session_lineage (
            session_id, parent_session_id, root_session_id,
            branch_origin, branch_mode, branch_depth, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "missing-branch",
            "missing-parent",
            "missing-root",
            "user_message_action",
            "materialized_prefix",
            1,
            time.time(),
        ),
    )
    db._conn.execute(
        """
        INSERT INTO session_branch_requests (
            idempotency_key, source_session_id, branch_fingerprint,
            result_session_id, created_at
        ) VALUES (?, ?, ?, ?, ?)
        """,
        ("branch-key-orphan", "missing-source", "fingerprint", "missing-result", time.time()),
    )
    db._conn.execute(
        """
        INSERT INTO team_capability_snapshot_bindings (
            binding_id, mission_id, conversation_id, snapshot_id,
            snapshot_version, source_digest, pinned_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "team-capability-binding:missing",
            "missing-mission",
            "missing-conversation",
            "missing-snapshot",
            1,
            "digest",
            time.time(),
        ),
    )
    db._conn.execute("PRAGMA foreign_keys=ON")

    assert db._conn.execute("PRAGMA foreign_key_check").fetchall()

    repaired = db.maintenance.repair_orphaned_foreign_key_rows()

    assert repaired == 3
    assert db._conn.execute("PRAGMA foreign_key_check").fetchall() == []


# =========================================================================
# Schema and WAL mode
# =========================================================================

# =========================================================================
# Session title
# =========================================================================

class TestSessionTitle:
    def test_set_and_get_title(self, db):
        db.sessions.create(session_id="s1", source="cli")
        assert db.sessions.set_title("s1", "My Session") is True

        session = db.sessions.get("s1")
        assert session["title"] == "My Session"

    def test_set_title_nonexistent_session(self, db):
        assert db.sessions.set_title("nonexistent", "Title") is False

    def test_title_initially_none(self, db):
        db.sessions.create(session_id="s1", source="cli")
        session = db.sessions.get("s1")
        assert session["title"] is None

    def test_update_title(self, db):
        db.sessions.create(session_id="s1", source="cli")
        db.sessions.set_title("s1", "First Title")
        db.sessions.set_title("s1", "Updated Title")

        session = db.sessions.get("s1")
        assert session["title"] == "Updated Title"

    def test_title_in_search_sessions(self, db):
        db.sessions.create(session_id="s1", source="cli")
        db.sessions.set_title("s1", "Debugging Auth")
        db.sessions.create(session_id="s2", source="cli")

        sessions = db.sessions.search()
        titled = [s for s in sessions if s.get("title") == "Debugging Auth"]
        assert len(titled) == 1
        assert titled[0]["id"] == "s1"

    def test_title_in_export(self, db):
        db.sessions.create(session_id="s1", source="cli")
        db.sessions.set_title("s1", "Export Test")
        db.messages.append("s1", role="user", content="Hello")

        export = db.sessions.export("s1")
        assert export["title"] == "Export Test"

    def test_title_with_special_characters(self, db):
        db.sessions.create(session_id="s1", source="cli")
        title = "PR #438 — fixing the 'auth' middleware"
        db.sessions.set_title("s1", title)

        session = db.sessions.get("s1")
        assert session["title"] == title

    def test_title_empty_string_normalized_to_none(self, db):
        """Empty strings are normalized to None (clearing the title)."""
        db.sessions.create(session_id="s1", source="cli")
        db.sessions.set_title("s1", "My Title")
        # Setting to empty string should clear the title (normalize to None)
        db.sessions.set_title("s1", "")

        session = db.sessions.get("s1")
        assert session["title"] is None

    def test_multiple_empty_titles_no_conflict(self, db):
        """Multiple sessions can have empty-string (normalized to NULL) titles."""
        db.sessions.create(session_id="s1", source="cli")
        db.sessions.create(session_id="s2", source="cli")
        db.sessions.set_title("s1", "")
        db.sessions.set_title("s2", "")
        # Both should be None, no uniqueness conflict
        assert db.sessions.get("s1")["title"] is None
        assert db.sessions.get("s2")["title"] is None

    def test_title_survives_end_session(self, db):
        db.sessions.create(session_id="s1", source="cli")
        db.sessions.set_title("s1", "Before End")
        db.sessions.end("s1", reason="user_exit")

        session = db.sessions.get("s1")
        assert session["title"] == "Before End"
        assert session["ended_at"] is not None


class TestSanitizeTitle:
    """Tests for SessionService.sanitize_title() validation and cleaning."""

    def test_normal_title_unchanged(self):
        assert SessionService.sanitize_title("My Project") == "My Project"

    def test_strips_whitespace(self):
        assert SessionService.sanitize_title("  hello world  ") == "hello world"

    def test_collapses_internal_whitespace(self):
        assert SessionService.sanitize_title("hello   world") == "hello world"

    def test_tabs_and_newlines_collapsed(self):
        assert SessionService.sanitize_title("hello\t\nworld") == "hello world"

    def test_none_returns_none(self):
        assert SessionService.sanitize_title(None) is None

    def test_empty_string_returns_none(self):
        assert SessionService.sanitize_title("") is None

    def test_whitespace_only_returns_none(self):
        assert SessionService.sanitize_title("   \t\n  ") is None

    def test_control_chars_stripped(self):
        # Null byte, bell, backspace, etc.
        assert SessionService.sanitize_title("hello\x00world") == "helloworld"
        assert SessionService.sanitize_title("\x07\x08test\x1b") == "test"

    def test_del_char_stripped(self):
        assert SessionService.sanitize_title("hello\x7fworld") == "helloworld"

    def test_zero_width_chars_stripped(self):
        # Zero-width space (U+200B), zero-width joiner (U+200D)
        assert SessionService.sanitize_title("hello\u200bworld") == "helloworld"
        assert SessionService.sanitize_title("hello\u200dworld") == "helloworld"

    def test_rtl_override_stripped(self):
        # Right-to-left override (U+202E) — used in filename spoofing attacks
        assert SessionService.sanitize_title("hello\u202eworld") == "helloworld"

    def test_bom_stripped(self):
        # Byte order mark (U+FEFF)
        assert SessionService.sanitize_title("\ufeffhello") == "hello"

    def test_only_control_chars_returns_none(self):
        assert SessionService.sanitize_title("\x00\x01\x02\u200b\ufeff") is None

    def test_max_length_allowed(self):
        title = "A" * 100
        assert SessionService.sanitize_title(title) == title

    def test_exceeds_max_length_raises(self):
        title = "A" * 101
        with pytest.raises(ValueError, match="too long"):
            SessionService.sanitize_title(title)

    def test_unicode_emoji_allowed(self):
        assert SessionService.sanitize_title("🚀 My Project 🎉") == "🚀 My Project 🎉"

    def test_cjk_characters_allowed(self):
        assert SessionService.sanitize_title("我的项目") == "我的项目"

    def test_accented_characters_allowed(self):
        assert SessionService.sanitize_title("Résumé éditing") == "Résumé éditing"

    def test_special_punctuation_allowed(self):
        title = "PR #438 — fixing the 'auth' middleware"
        assert SessionService.sanitize_title(title) == title

    def test_sanitize_applied_in_set_session_title(self, db):
        """set_session_title applies sanitize_title internally."""
        db.sessions.create("s1", "cli")
        db.sessions.set_title("s1", "  hello\x00  world  ")
        assert db.sessions.get("s1")["title"] == "hello world"

    def test_too_long_title_rejected_by_set(self, db):
        """set_session_title raises ValueError for overly long titles."""
        db.sessions.create("s1", "cli")
        with pytest.raises(ValueError, match="too long"):
            db.sessions.set_title("s1", "X" * 150)


class TestSchemaInit:
    def test_wal_mode(self, db):
        cursor = db._conn.execute("PRAGMA journal_mode")
        mode = cursor.fetchone()[0]
        assert mode == "wal"

    def test_foreign_keys_enabled(self, db):
        cursor = db._conn.execute("PRAGMA foreign_keys")
        assert cursor.fetchone()[0] == 1

    def test_tables_exist(self, db):
        cursor = db._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        )
        tables = {row[0] for row in cursor.fetchall()}
        assert "sessions" in tables
        assert "messages" in tables
        assert "schema_version" in tables

    def test_schema_version(self, db):
        cursor = db._conn.execute("SELECT version FROM schema_version")
        version = cursor.fetchone()[0]
        assert version == CURRENT_SCHEMA_VERSION

    def test_title_column_exists(self, db):
        """Verify the title column was created in the sessions table."""
        cursor = db._conn.execute("PRAGMA table_info(sessions)")
        columns = {row[1] for row in cursor.fetchall()}
        assert "title" in columns

    def test_topic_mode_schema_is_not_auto_migrated_on_open(self, tmp_path):
        """Opening an old DB should not add topic-mode columns until /topic opts in.

        The gateway must remain rollback-safe: simply upgrading Hermes and starting
        the old bot should not eagerly mutate the state DB for this feature.
        """
        old_db = tmp_path / "old.db"
        import sqlite3

        conn = sqlite3.connect(old_db)
        conn.executescript(
            """
            CREATE TABLE schema_version (version INTEGER NOT NULL);
            INSERT INTO schema_version VALUES (11);
            CREATE TABLE sessions (
                id TEXT PRIMARY KEY,
                source TEXT NOT NULL,
                user_id TEXT,
                model TEXT,
                model_config TEXT,
                system_prompt TEXT,
                parent_session_id TEXT,
                started_at REAL NOT NULL,
                ended_at REAL,
                end_reason TEXT,
                message_count INTEGER DEFAULT 0,
                tool_call_count INTEGER DEFAULT 0,
                input_tokens INTEGER DEFAULT 0,
                output_tokens INTEGER DEFAULT 0,
                cache_read_tokens INTEGER DEFAULT 0,
                cache_write_tokens INTEGER DEFAULT 0,
                reasoning_tokens INTEGER DEFAULT 0,
                billing_provider TEXT,
                billing_base_url TEXT,
                billing_mode TEXT,
                estimated_cost_usd REAL,
                actual_cost_usd REAL,
                cost_status TEXT,
                cost_source TEXT,
                pricing_version TEXT,
                title TEXT,
                api_call_count INTEGER DEFAULT 0,
                FOREIGN KEY (parent_session_id) REFERENCES sessions(id)
            );
            CREATE TABLE messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL REFERENCES sessions(id),
                role TEXT NOT NULL,
                content TEXT,
                tool_call_id TEXT,
                tool_calls TEXT,
                tool_name TEXT,
                timestamp REAL NOT NULL,
                token_count INTEGER,
                finish_reason TEXT,
                reasoning TEXT,
                reasoning_content TEXT,
                reasoning_details TEXT,
                codex_reasoning_items TEXT,
                codex_message_items TEXT
            );
            """
        )
        conn.close()

        db = open_cli_session_store(db_path=old_db)
        cursor = db._conn.execute("PRAGMA table_info(sessions)")
        columns = {row[1] for row in cursor.fetchall()}
        assert {"chat_id", "chat_type", "thread_id", "session_key"}.isdisjoint(columns)
        db.close()

    def test_apply_telegram_topic_migration_creates_topic_tables_explicitly(self, tmp_path):
        """The /topic opt-in path owns the DB migration for Telegram topic mode."""
        old_db = tmp_path / "old.db"
        import sqlite3

        conn = sqlite3.connect(old_db)
        conn.executescript(
            """
            CREATE TABLE schema_version (version INTEGER NOT NULL);
            INSERT INTO schema_version VALUES (11);
            CREATE TABLE sessions (
                id TEXT PRIMARY KEY,
                source TEXT NOT NULL,
                user_id TEXT,
                model TEXT,
                model_config TEXT,
                system_prompt TEXT,
                parent_session_id TEXT,
                started_at REAL NOT NULL,
                ended_at REAL,
                end_reason TEXT,
                message_count INTEGER DEFAULT 0,
                tool_call_count INTEGER DEFAULT 0,
                input_tokens INTEGER DEFAULT 0,
                output_tokens INTEGER DEFAULT 0,
                cache_read_tokens INTEGER DEFAULT 0,
                cache_write_tokens INTEGER DEFAULT 0,
                reasoning_tokens INTEGER DEFAULT 0,
                billing_provider TEXT,
                billing_base_url TEXT,
                billing_mode TEXT,
                estimated_cost_usd REAL,
                actual_cost_usd REAL,
                cost_status TEXT,
                cost_source TEXT,
                pricing_version TEXT,
                title TEXT,
                api_call_count INTEGER DEFAULT 0,
                FOREIGN KEY (parent_session_id) REFERENCES sessions(id)
            );
            CREATE TABLE messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL REFERENCES sessions(id),
                role TEXT NOT NULL,
                content TEXT,
                tool_call_id TEXT,
                tool_calls TEXT,
                tool_name TEXT,
                timestamp REAL NOT NULL,
                token_count INTEGER,
                finish_reason TEXT,
                reasoning TEXT,
                reasoning_content TEXT,
                reasoning_details TEXT,
                codex_reasoning_items TEXT,
                codex_message_items TEXT
            );
            """
        )
        conn.close()

        db = open_cli_session_store(db_path=old_db)
        db.telegram_topics.apply_telegram_topic_migration()

        tables = {
            row[0]
            for row in db._conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        assert "telegram_dm_topic_mode" in tables
        assert "telegram_dm_topic_bindings" in tables
        assert db.metadata.get("telegram_dm_topic_schema_version") == "2"
        db.close()

    def test_telegram_topic_binding_roundtrip_requires_explicit_schema(self, tmp_path):
        db = open_cli_session_store(db_path=tmp_path / "state.db")
        db.sessions.create(
            session_id="topic-session",
            source="telegram",
            user_id="208214988",
        )

        assert db.telegram_topics.get_telegram_topic_binding(chat_id="208214988", thread_id="17585") is None

        db.telegram_topics.bind_telegram_topic(
            chat_id="208214988",
            thread_id="17585",
            user_id="208214988",
            session_key="telegram:dm:208214988:thread:17585",
            session_id="topic-session",
        )

        binding = db.telegram_topics.get_telegram_topic_binding(chat_id="208214988", thread_id="17585")
        assert binding is not None
        assert binding["chat_id"] == "208214988"
        assert binding["thread_id"] == "17585"
        assert binding["user_id"] == "208214988"
        assert binding["session_key"] == "telegram:dm:208214988:thread:17585"
        assert binding["session_id"] == "topic-session"
        assert db.metadata.get("telegram_dm_topic_schema_version") == "2"
        db.close()

    def test_telegram_topic_binding_refuses_to_relink_session_to_another_topic(self, tmp_path):
        db = open_cli_session_store(db_path=tmp_path / "state.db")
        db.sessions.create(
            session_id="topic-session",
            source="telegram",
            user_id="208214988",
        )
        db.telegram_topics.bind_telegram_topic(
            chat_id="208214988",
            thread_id="17585",
            user_id="208214988",
            session_key="key-17585",
            session_id="topic-session",
        )

        with pytest.raises(ValueError, match="already linked"):
            db.telegram_topics.bind_telegram_topic(
                chat_id="208214988",
                thread_id="99999",
                user_id="208214988",
                session_key="key-99999",
                session_id="topic-session",
            )
        db.close()

    def test_list_unlinked_telegram_sessions_for_user_excludes_bound_and_other_users(self, tmp_path):
        db = open_cli_session_store(db_path=tmp_path / "state.db")
        db.sessions.create(
            session_id="old-unlinked",
            source="telegram",
            user_id="208214988",
        )
        db.sessions.set_title("old-unlinked", "Old research")
        db.messages.append("old-unlinked", "user", "first prompt")
        db.sessions.create(
            session_id="already-linked",
            source="telegram",
            user_id="208214988",
        )
        db.telegram_topics.bind_telegram_topic(
            chat_id="208214988",
            thread_id="17585",
            user_id="208214988",
            session_key="key-17585",
            session_id="already-linked",
        )
        db.sessions.create(
            session_id="other-user",
            source="telegram",
            user_id="someone-else",
        )

        sessions = db.telegram_topics.list_unlinked_telegram_sessions_for_user(
            chat_id="208214988",
            user_id="208214988",
        )

        assert [s["id"] for s in sessions] == ["old-unlinked"]
        assert sessions[0]["title"] == "Old research"
        assert sessions[0]["preview"] == "first prompt"
        db.close()

    def test_migration_from_v2(self, tmp_path):
        """Simulate a v2 database and verify migration adds title column."""
        import sqlite3

        db_path = tmp_path / "migrate_test.db"
        conn = sqlite3.connect(str(db_path))
        # Create v2 schema (without title column)
        conn.executescript("""
            CREATE TABLE schema_version (version INTEGER NOT NULL);
            INSERT INTO schema_version (version) VALUES (2);

            CREATE TABLE sessions (
                id TEXT PRIMARY KEY,
                source TEXT NOT NULL,
                user_id TEXT,
                model TEXT,
                model_config TEXT,
                system_prompt TEXT,
                parent_session_id TEXT,
                started_at REAL NOT NULL,
                ended_at REAL,
                end_reason TEXT,
                message_count INTEGER DEFAULT 0,
                tool_call_count INTEGER DEFAULT 0,
                input_tokens INTEGER DEFAULT 0,
                output_tokens INTEGER DEFAULT 0
            );

            CREATE TABLE messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT,
                tool_call_id TEXT,
                tool_calls TEXT,
                tool_name TEXT,
                timestamp REAL NOT NULL,
                token_count INTEGER,
                finish_reason TEXT
            );
        """)
        conn.execute(
            "INSERT INTO sessions (id, source, started_at) VALUES (?, ?, ?)",
            ("existing", "cli", 1000.0),
        )
        conn.commit()
        conn.close()

        # Open with CliSessionStore — should migrate to v9
        migrated_db = open_cli_session_store(db_path=db_path)

        # Verify migration
        cursor = migrated_db._conn.execute("SELECT version FROM schema_version")
        assert cursor.fetchone()[0] == CURRENT_SCHEMA_VERSION

        # Verify title column exists and is NULL for existing sessions
        session = migrated_db.sessions.get("existing")
        assert session is not None
        assert session["title"] is None

        # Verify api_call_count column was added with default 0
        cursor = migrated_db._conn.execute(
            "SELECT api_call_count FROM sessions WHERE id = 'existing'"
        )
        assert cursor.fetchone()[0] == 0

        # Verify we can set title on migrated session
        assert migrated_db.sessions.set_title("existing", "Migrated Title") is True
        session = migrated_db.sessions.get("existing")
        assert session["title"] == "Migrated Title"

        migrated_db.close()

    def test_reconciliation_adds_missing_columns(self, tmp_path):
        """Columns present in SCHEMA_SQL but missing from the live table
        are added by _reconcile_columns regardless of schema_version.

        Regression test: commit a7d78d3b inserted a new v7 migration
        (reasoning_content) and renumbered the old v7 (api_call_count)
        to v8.  Users already at the old v7 had schema_version >= 7,
        so the new v7 block was skipped and reasoning_content was never
        created — causing 'no such column' on /continue.
        """
        import sqlite3

        db_path = tmp_path / "gap_test.db"
        conn = sqlite3.connect(str(db_path))
        # Simulate the old v7 state: api_call_count exists, reasoning_content does NOT
        conn.executescript("""
            CREATE TABLE schema_version (version INTEGER NOT NULL);
            INSERT INTO schema_version (version) VALUES (7);

            CREATE TABLE sessions (
                id TEXT PRIMARY KEY,
                source TEXT NOT NULL,
                user_id TEXT,
                model TEXT,
                model_config TEXT,
                system_prompt TEXT,
                parent_session_id TEXT,
                started_at REAL NOT NULL,
                ended_at REAL,
                end_reason TEXT,
                message_count INTEGER DEFAULT 0,
                tool_call_count INTEGER DEFAULT 0,
                input_tokens INTEGER DEFAULT 0,
                output_tokens INTEGER DEFAULT 0,
                cache_read_tokens INTEGER DEFAULT 0,
                cache_write_tokens INTEGER DEFAULT 0,
                reasoning_tokens INTEGER DEFAULT 0,
                billing_provider TEXT,
                billing_base_url TEXT,
                billing_mode TEXT,
                estimated_cost_usd REAL,
                actual_cost_usd REAL,
                cost_status TEXT,
                cost_source TEXT,
                pricing_version TEXT,
                title TEXT,
                api_call_count INTEGER DEFAULT 0
            );

            CREATE TABLE messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT,
                tool_call_id TEXT,
                tool_calls TEXT,
                tool_name TEXT,
                timestamp REAL NOT NULL,
                token_count INTEGER,
                finish_reason TEXT,
                reasoning TEXT,
                reasoning_details TEXT,
                codex_reasoning_items TEXT
            );
        """)
        conn.execute(
            "INSERT INTO sessions (id, source, started_at) VALUES (?, ?, ?)",
            ("s1", "cli", 1000.0),
        )
        conn.execute(
            "INSERT INTO messages (session_id, role, content, timestamp) "
            "VALUES (?, ?, ?, ?)",
            ("s1", "assistant", "hello", 1001.0),
        )
        conn.commit()
        # Verify reasoning_content is absent
        cols = {r[1] for r in conn.execute("PRAGMA table_info(messages)").fetchall()}
        assert "reasoning_content" not in cols
        conn.close()

        # Open with CliSessionStore — reconciliation should add the missing column
        migrated_db = open_cli_session_store(db_path=db_path)

        msg_cols = {
            r[1]
            for r in migrated_db._conn.execute("PRAGMA table_info(messages)").fetchall()
        }
        assert "reasoning_content" in msg_cols

        # The query that used to crash must now work
        cursor = migrated_db._conn.execute(
            "SELECT role, content, reasoning, reasoning_content, "
            "reasoning_details, codex_reasoning_items "
            "FROM messages WHERE session_id = ?",
            ("s1",),
        )
        row = cursor.fetchone()
        assert row is not None
        assert row[0] == "assistant"
        assert row[3] is None  # reasoning_content NULL for old rows

        migrated_db.close()

    def test_reconciliation_defers_indexes_that_reference_new_columns(self, tmp_path):
        """Indexes that reference newly declared columns must run after reconcile.

        Team mission conversations added team_missions.conversation_id after
        the original team_missions table shipped. Keeping the index in
        SCHEMA_SQL made existing databases fail during executescript() before
        _reconcile_columns() could add the missing column.
        """
        import sqlite3

        db_path = tmp_path / "team_mission_old_schema.db"
        conn = sqlite3.connect(str(db_path))
        conn.executescript("""
            CREATE TABLE schema_version (version INTEGER NOT NULL);
            INSERT INTO schema_version (version) VALUES (16);

            CREATE TABLE sessions (
                id TEXT PRIMARY KEY,
                source TEXT NOT NULL,
                started_at REAL NOT NULL
            );

            CREATE TABLE messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT,
                timestamp REAL NOT NULL
            );

            CREATE TABLE team_missions (
                mission_id TEXT PRIMARY KEY,
                team_id TEXT,
                title TEXT NOT NULL,
                objective TEXT,
                workspace_id TEXT,
                workspace_path TEXT,
                mode TEXT NOT NULL,
                status TEXT NOT NULL,
                leader_session_id TEXT,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                completed_at REAL,
                metadata_json TEXT
            );
        """)
        conn.execute(
            """
            INSERT INTO team_missions (
                mission_id, team_id, title, mode, status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            ("mission-1", "team-1", "Legacy mission", "supervised", "completed", 1.0, 1.0),
        )
        conn.commit()
        conn.close()

        migrated_db = open_cli_session_store(db_path=db_path)

        mission_cols = {
            r[1]
            for r in migrated_db._conn.execute(
                "PRAGMA table_info(team_missions)"
            ).fetchall()
        }
        assert "conversation_id" in mission_cols

        indexes = {
            row[1]
            for row in migrated_db._conn.execute(
                "PRAGMA index_list(team_missions)"
            ).fetchall()
        }
        assert "idx_team_missions_conversation" in indexes

        cursor = migrated_db._conn.execute(
            """
            SELECT mission_id
            FROM team_missions
            WHERE conversation_id IS NULL
            ORDER BY updated_at DESC
            """
        )
        assert cursor.fetchone()[0] == "mission-1"

        migrated_db.close()

    def test_reconciliation_is_idempotent(self, tmp_path):
        """Opening the same database twice doesn't error or duplicate columns."""
        db_path = tmp_path / "idempotent.db"
        db1 = open_cli_session_store(db_path=db_path)
        cols1 = {r[1] for r in db1._conn.execute("PRAGMA table_info(messages)").fetchall()}
        db1.close()

        db2 = open_cli_session_store(db_path=db_path)
        cols2 = {r[1] for r in db2._conn.execute("PRAGMA table_info(messages)").fetchall()}
        db2.close()

        assert cols1 == cols2

    def test_schema_sql_is_source_of_truth(self, db):
        """Every column in SCHEMA_SQL exists in the live database.

        This is the architectural invariant: SCHEMA_SQL declares the
        desired schema, _reconcile_columns ensures it matches reality.
        """

        expected = parse_schema_columns(SCHEMA_SQL)
        for table_name, declared_cols in expected.items():
            live_cols = {
                r[1]
                for r in db._conn.execute(
                    f'PRAGMA table_info("{table_name}")'
                ).fetchall()
            }
            for col_name in declared_cols:
                assert col_name in live_cols, (
                    f"Column {col_name} declared in SCHEMA_SQL for {table_name} "
                    f"but missing from live DB. Live columns: {live_cols}"
                )


class TestTitleUniqueness:
    """Tests for unique title enforcement and title-based lookups."""

    def test_duplicate_title_raises(self, db):
        """Setting a title already used by another session raises ValueError."""
        db.sessions.create("s1", "cli")
        db.sessions.create("s2", "cli")
        db.sessions.set_title("s1", "my project")
        with pytest.raises(ValueError, match="already in use"):
            db.sessions.set_title("s2", "my project")

    def test_same_session_can_keep_title(self, db):
        """A session can re-set its own title without error."""
        db.sessions.create("s1", "cli")
        db.sessions.set_title("s1", "my project")
        # Should not raise — it's the same session
        assert db.sessions.set_title("s1", "my project") is True

    def test_null_titles_not_unique(self, db):
        """Multiple sessions can have NULL titles (no constraint violation)."""
        db.sessions.create("s1", "cli")
        db.sessions.create("s2", "cli")
        # Both have NULL titles — no error
        assert db.sessions.get("s1")["title"] is None
        assert db.sessions.get("s2")["title"] is None

    def test_get_session_by_title(self, db):
        db.sessions.create("s1", "cli")
        db.sessions.set_title("s1", "refactoring auth")
        result = db.sessions.get_by_title("refactoring auth")
        assert result is not None
        assert result["id"] == "s1"

    def test_get_session_by_title_not_found(self, db):
        assert db.sessions.get_by_title("nonexistent") is None

    def test_get_session_title(self, db):
        db.sessions.create("s1", "cli")
        assert db.sessions.get_title("s1") is None
        db.sessions.set_title("s1", "my title")
        assert db.sessions.get_title("s1") == "my title"

    def test_get_session_title_nonexistent(self, db):
        assert db.sessions.get_title("nonexistent") is None


class TestTitleLineage:
    """Tests for title lineage resolution and auto-numbering."""

    def test_resolve_exact_title(self, db):
        db.sessions.create("s1", "cli")
        db.sessions.set_title("s1", "my project")
        assert db.sessions.resolve_by_title("my project") == "s1"

    def test_resolve_returns_latest_numbered(self, db):
        """When numbered variants exist, return the most recent one."""
        import time
        db.sessions.create("s1", "cli")
        db.sessions.set_title("s1", "my project")
        time.sleep(0.01)
        db.sessions.create("s2", "cli")
        db.sessions.set_title("s2", "my project #2")
        time.sleep(0.01)
        db.sessions.create("s3", "cli")
        db.sessions.set_title("s3", "my project #3")
        # Resolving "my project" should return s3 (latest numbered variant)
        assert db.sessions.resolve_by_title("my project") == "s3"

    def test_resolve_exact_numbered(self, db):
        """Resolving an exact numbered title returns that specific session."""
        db.sessions.create("s1", "cli")
        db.sessions.set_title("s1", "my project")
        db.sessions.create("s2", "cli")
        db.sessions.set_title("s2", "my project #2")
        # Resolving "my project #2" exactly should return s2
        assert db.sessions.resolve_by_title("my project #2") == "s2"

    def test_resolve_nonexistent_title(self, db):
        assert db.sessions.resolve_by_title("nonexistent") is None

    def test_next_title_no_existing(self, db):
        """With no existing sessions, base title is returned as-is."""
        assert db.sessions.next_title_in_lineage("my project") == "my project"

    def test_next_title_first_continuation(self, db):
        """First continuation after the original gets #2."""
        db.sessions.create("s1", "cli")
        db.sessions.set_title("s1", "my project")
        assert db.sessions.next_title_in_lineage("my project") == "my project #2"

    def test_next_title_increments(self, db):
        """Each continuation increments the number."""
        db.sessions.create("s1", "cli")
        db.sessions.set_title("s1", "my project")
        db.sessions.create("s2", "cli")
        db.sessions.set_title("s2", "my project #2")
        db.sessions.create("s3", "cli")
        db.sessions.set_title("s3", "my project #3")
        assert db.sessions.next_title_in_lineage("my project") == "my project #4"

    def test_next_title_strips_existing_number(self, db):
        """Passing a numbered title strips the number and finds the base."""
        db.sessions.create("s1", "cli")
        db.sessions.set_title("s1", "my project")
        db.sessions.create("s2", "cli")
        db.sessions.set_title("s2", "my project #2")
        # Even when called with "my project #2", it should return #3
        assert db.sessions.next_title_in_lineage("my project #2") == "my project #3"


class TestTitleSqlWildcards:
    """Titles containing SQL LIKE wildcards (%, _) must not cause false matches."""

    def test_resolve_title_with_underscore(self, db):
        """A title like 'test_project' should not match 'testXproject #2'."""
        db.sessions.create("s1", "cli")
        db.sessions.set_title("s1", "test_project")
        db.sessions.create("s2", "cli")
        db.sessions.set_title("s2", "testXproject #2")
        # Resolving "test_project" should return s1 (exact), not s2
        assert db.sessions.resolve_by_title("test_project") == "s1"

    def test_resolve_title_with_percent(self, db):
        """A title with '%' should not wildcard-match unrelated sessions."""
        db.sessions.create("s1", "cli")
        db.sessions.set_title("s1", "100% done")
        db.sessions.create("s2", "cli")
        db.sessions.set_title("s2", "100X done #2")
        # Should resolve to s1 (exact), not s2
        assert db.sessions.resolve_by_title("100% done") == "s1"

    def test_next_lineage_with_underscore(self, db):
        """get_next_title_in_lineage with underscores doesn't match wrong sessions."""
        db.sessions.create("s1", "cli")
        db.sessions.set_title("s1", "test_project")
        db.sessions.create("s2", "cli")
        db.sessions.set_title("s2", "testXproject #2")
        # Only "test_project" exists, so next should be "test_project #2"
        assert db.sessions.next_title_in_lineage("test_project") == "test_project #2"


class TestListSessionsRich:
    """Tests for enhanced session listing with preview and last_active."""

    def test_preview_from_first_user_message(self, db):
        db.sessions.create("s1", "cli")
        db.messages.append("s1", "system", "You are a helpful assistant.")
        db.messages.append("s1", "user", "Help me refactor the auth module please")
        db.messages.append("s1", "assistant", "Sure, let me look at it.")
        sessions = db.sessions.list_rich()
        assert len(sessions) == 1
        assert "Help me refactor the auth module" in sessions[0]["preview"]

    def test_preview_truncated_at_60(self, db):
        db.sessions.create("s1", "cli")
        long_msg = "A" * 100
        db.messages.append("s1", "user", long_msg)
        sessions = db.sessions.list_rich()
        assert len(sessions[0]["preview"]) == 63  # 60 chars + "..."
        assert sessions[0]["preview"].endswith("...")

    def test_preview_empty_when_no_user_messages(self, db):
        db.sessions.create("s1", "cli")
        db.messages.append("s1", "system", "System prompt")
        sessions = db.sessions.list_rich()
        assert sessions[0]["preview"] == ""

    def test_last_active_from_latest_message(self, db):
        import time
        db.sessions.create("s1", "cli")
        db.messages.append("s1", "user", "Hello")
        time.sleep(0.01)
        db.messages.append("s1", "assistant", "Hi there!")
        sessions = db.sessions.list_rich()
        # last_active should be close to now (the assistant message)
        assert sessions[0]["last_active"] > sessions[0]["started_at"]

    def test_last_active_fallback_to_started_at(self, db):
        db.sessions.create("s1", "cli")
        sessions = db.sessions.list_rich()
        # No messages, so last_active falls back to started_at
        assert sessions[0]["last_active"] == sessions[0]["started_at"]

    def test_list_sessions_rich_reads_summary_not_transcript_rows(self, db):
        db.sessions.create("s1", "cli")
        db.messages.append("s1", "user", "first summary")
        db.sessions.create("s2", "cli")
        db.messages.append("s2", "user", "second summary")

        statements = []
        with db._lock:
            db._conn.set_trace_callback(statements.append)
        try:
            sessions = db.sessions.list_rich(limit=2, order_by_last_active=True)
        finally:
            with db._lock:
                db._conn.set_trace_callback(None)

        assert [session["id"] for session in sessions] == ["s2", "s1"]
        traced_sql = "\n".join(statements).lower()
        assert "from messages" not in traced_sql
        assert "join messages" not in traced_sql

    def test_order_by_last_active_surfaces_recently_touched_older_session_first(self, db):
        t0 = 1709500000.0
        db.sessions.create("old", "cli")
        db.sessions.create("new", "cli")

        with db._lock:
            db._conn.execute("UPDATE sessions SET started_at=? WHERE id=?", (t0, "old"))
            db._conn.execute("UPDATE sessions SET started_at=? WHERE id=?", (t0 + 10, "new"))

        db.messages.append("old", "user", "old first")
        db.messages.append("new", "user", "new first")
        db.messages.append("old", "assistant", "old touched later")

        with db._lock:
            db._conn.execute(
                "UPDATE messages SET timestamp=? WHERE session_id=? AND role=? AND content=?",
                (t0 + 1, "old", "user", "old first"),
            )
            db._conn.execute(
                "UPDATE messages SET timestamp=? WHERE session_id=? AND role=? AND content=?",
                (t0 + 11, "new", "user", "new first"),
            )
            db._conn.execute(
                "UPDATE messages SET timestamp=? WHERE session_id=? AND role=? AND content=?",
                (t0 + 20, "old", "assistant", "old touched later"),
            )
            db._conn.execute(
                "UPDATE sessions SET last_active=? WHERE id=?",
                (t0 + 20, "old"),
            )
            db._conn.execute(
                "UPDATE sessions SET last_active=? WHERE id=?",
                (t0 + 11, "new"),
            )
            db._conn.commit()

        assert [s["id"] for s in db.sessions.list_rich(limit=5)] == ["new", "old"]
        assert [
            s["id"] for s in db.sessions.list_rich(limit=5, order_by_last_active=True)
        ] == ["old", "new"]

    def test_page_cursor_paginates_order_by_last_active(self, db):
        t0 = 1709500000.0
        rows = [
            ("old-active", t0, t0 + 30),
            ("mid-active", t0 + 10, t0 + 20),
            ("new-start", t0 + 20, t0 + 10),
        ]
        for session_id, started_at, message_ts in rows:
            db.sessions.create(session_id, "cli")
            with db._lock:
                db._conn.execute(
                    "UPDATE sessions SET started_at=? WHERE id=?",
                    (started_at, session_id),
                )
            db.messages.append(session_id, "user", session_id)
            with db._lock:
                db._conn.execute(
                    "UPDATE messages SET timestamp=? WHERE session_id=? AND content=?",
                    (message_ts, session_id, session_id),
                )
                db._conn.execute(
                    "UPDATE sessions SET last_active=? WHERE id=?",
                    (message_ts, session_id),
                )
                db._conn.commit()

        first_page = db.sessions.list_rich(limit=2, order_by_last_active=True)
        assert [s["id"] for s in first_page] == ["old-active", "mid-active"]
        assert first_page[-1]["_page_cursor"] == {
            "effective_last_active": t0 + 20,
            "started_at": t0 + 10,
            "id": "mid-active",
        }

        second_page = db.sessions.list_rich(
            limit=2,
            order_by_last_active=True,
            page_cursor=first_page[-1]["_page_cursor"],
        )
        assert [s["id"] for s in second_page] == ["new-start"]

    def test_page_cursor_paginates_started_at_order(self, db):
        t0 = 1709500000.0
        for index, session_id in enumerate(("s1", "s2", "s3")):
            db.sessions.create(session_id, "cli")
            with db._lock:
                db._conn.execute(
                    "UPDATE sessions SET started_at=? WHERE id=?",
                    (t0 + index, session_id),
                )
                db._conn.commit()

        first_page = db.sessions.list_rich(limit=2)
        assert [s["id"] for s in first_page] == ["s3", "s2"]

        second_page = db.sessions.list_rich(
            limit=2,
            page_cursor=first_page[-1]["_page_cursor"],
        )
        assert [s["id"] for s in second_page] == ["s1"]

    def test_order_by_last_active_uses_compression_tip_activity(self, db):
        """A compression root whose tip was touched recently must rank above
        a newer uncompressed session, even when that tip activity lives in a
        different row and the outer LIMIT could otherwise cut it.

        This is the case that forced SQL-level chain walking: a naive "cap
        the SQL fetch at limit*K" optimization would drop the old root off
        the SQL page before post-projection could promote it.
        """
        t0 = 1709500000.0
        db.sessions.create("root1", "cli")
        with db._lock:
            db._conn.execute("UPDATE sessions SET started_at=? WHERE id=?", (t0, "root1"))
            db._conn.execute(
                "UPDATE sessions SET ended_at=?, end_reason=? WHERE id=?",
                (t0 + 100, "compression", "root1"),
            )
        db.messages.append("root1", "user", "old ask")
        with db._lock:
            db._conn.execute(
                "UPDATE sessions SET last_active=? WHERE id=?",
                (t0, "root1"),
            )
            db._conn.commit()

        # Continuation tip created after root ended; last activity much later.
        db.sessions.create("tip1", "cli", parent_session_id="root1")
        with db._lock:
            db._conn.execute("UPDATE sessions SET started_at=? WHERE id=?", (t0 + 101, "tip1"))
        db.messages.append("tip1", "user", "latest message")

        # Bunch of newer, uncompressed sessions — fresher start_at but older
        # last activity than the tip. Explicitly pin message timestamps so
        # they don't pick up wall-clock from append_message.
        for i in range(5):
            sid = f"newer{i}"
            db.sessions.create(sid, "cli")
            with db._lock:
                db._conn.execute(
                    "UPDATE sessions SET started_at=? WHERE id=?",
                    (t0 + 500 + i, sid),
                )
            db.messages.append(sid, "user", f"msg {i}")
            with db._lock:
                db._conn.execute(
                    "UPDATE messages SET timestamp=? WHERE session_id=? AND content=?",
                    (t0 + 500 + i, sid, f"msg {i}"),
                )
                db._conn.execute(
                    "UPDATE sessions SET last_active=? WHERE id=?",
                    (t0 + 500 + i, sid),
                )

        # Tip activity timestamp is the latest thing in the DB.
        with db._lock:
            db._conn.execute(
                "UPDATE messages SET timestamp=? WHERE session_id=? AND content=?",
                (t0 + 10_000, "tip1", "latest message"),
            )
            db._conn.execute(
                "UPDATE sessions SET last_active=? WHERE id=?",
                (t0 + 10_000, "tip1"),
            )
            db._conn.commit()

        # limit=1 is the stress test: the old root must win the single slot.
        top = db.sessions.list_rich(limit=1, order_by_last_active=True)
        assert len(top) == 1
        # Projection surfaces the tip's id in the root's slot.
        assert top[0]["id"] == "tip1"
        assert top[0]["_lineage_root_id"] == "root1"

    def test_rich_list_includes_title(self, db):
        db.sessions.create("s1", "cli")
        db.sessions.set_title("s1", "refactoring auth")
        sessions = db.sessions.list_rich()
        assert sessions[0]["title"] == "refactoring auth"

    def test_rich_list_source_filter(self, db):
        db.sessions.create("s1", "cli")
        db.sessions.create("s2", "telegram")
        sessions = db.sessions.list_rich(source="cli")
        assert len(sessions) == 1
        assert sessions[0]["id"] == "s1"

    def test_preview_newlines_collapsed(self, db):
        db.sessions.create("s1", "cli")
        db.messages.append("s1", "user", "Line one\nLine two\nLine three")
        sessions = db.sessions.list_rich()
        assert "\n" not in sessions[0]["preview"]
        assert "Line one Line two" in sessions[0]["preview"]

    def test_branch_session_visible_in_list(self, db):
        """Branch sessions (parent ended with 'branched') must appear in list_sessions_rich."""
        db.sessions.create("parent", "cli")
        db.sessions.end("parent", "branched")
        db.sessions.create("branch", "cli", parent_session_id="parent")
        db.messages.append("branch", "user", "Exploring the alternative approach")

        sessions = db.sessions.list_rich()
        ids = [s["id"] for s in sessions]
        assert "branch" in ids, "Branch session should be visible in default list"

    def test_subagent_session_still_hidden(self, db):
        """Sub-agent children (parent NOT ended with 'branched') remain hidden."""
        db.sessions.create("root", "cli")
        db.sessions.create("delegate", "cli", parent_session_id="root")

        sessions = db.sessions.list_rich()
        ids = [s["id"] for s in sessions]
        assert "delegate" not in ids, "Delegate sub-agent should not appear in default list"
        assert "root" in ids

    def test_compression_child_still_hidden(self, db):
        """Compression continuation sessions remain hidden (parent ended with 'compression')."""
        import time as _time
        t0 = _time.time()
        db.sessions.create("root", "cli")
        db._conn.execute("UPDATE sessions SET started_at=? WHERE id=?", (t0, "root"))
        db._conn.execute(
            "UPDATE sessions SET ended_at=?, end_reason='compression' WHERE id=?",
            (t0 + 1800, "root"),
        )
        db._conn.commit()
        db.sessions.create("continuation", "cli", parent_session_id="root")
        db._conn.execute(
            "UPDATE sessions SET started_at=? WHERE id=?", (t0 + 1801, "continuation")
        )
        db._conn.commit()

        sessions = db.sessions.list_rich(project_compression_tips=False)
        ids = [s["id"] for s in sessions]
        assert "continuation" not in ids, "Compression continuation should stay hidden"


class TestCompressionChainProjection:
    """Tests for lineage-aware list_sessions_rich — compressed conversations
    surface as their live continuation tip, not the dead parent root.
    """

    def _build_compression_chain(self, db, t0: float):
        """Helper: builds root -> delegate -> compression-child -> tip chain.

        Returns (root_id, delegate_id, mid_id, tip_id).
        """
        import time as _time
        # Root that gets compressed
        db.sessions.create("root1", "cli")
        db._conn.execute("UPDATE sessions SET started_at=? WHERE id=?", (t0, "root1"))
        db.messages.append("root1", "user", "help me refactor auth")

        # Delegate subagent spawned while root1 was live (before it ended)
        db.sessions.create("delegate1", "cli", parent_session_id="root1")
        db._conn.execute(
            "UPDATE sessions SET started_at=?, ended_at=? WHERE id=?",
            (t0 + 600, t0 + 650, "delegate1"),
        )
        db.messages.append("delegate1", "user", "delegate task")

        # root1 compressed at t0+1800
        t_compress_root = t0 + 1800
        db._conn.execute(
            "UPDATE sessions SET ended_at=?, end_reason=? WHERE id=?",
            (t_compress_root, "compression", "root1"),
        )

        # Continuation mid created 1s after parent ended
        db.sessions.create("mid1", "cli", parent_session_id="root1")
        db._conn.execute(
            "UPDATE sessions SET started_at=? WHERE id=?",
            (t_compress_root + 1, "mid1"),
        )
        db.messages.append("mid1", "user", "continuing")

        # mid1 also compressed
        t_compress_mid = t_compress_root + 1800
        db._conn.execute(
            "UPDATE sessions SET ended_at=?, end_reason=? WHERE id=?",
            (t_compress_mid, "compression", "mid1"),
        )

        # Tip — latest continuation
        db.sessions.create("tip1", "cli", parent_session_id="mid1")
        db._conn.execute(
            "UPDATE sessions SET started_at=? WHERE id=?",
            (t_compress_mid + 1, "tip1"),
        )
        db.messages.append("tip1", "user", "latest message")

        db._conn.commit()
        return ("root1", "delegate1", "mid1", "tip1")

    def test_get_compression_tip_walks_full_chain(self, db):
        import time as _time
        self._build_compression_chain(db, _time.time() - 3600)
        assert db.sessions.compression_tip("root1") == "tip1"
        assert db.sessions.compression_tip("mid1") == "tip1"
        assert db.sessions.compression_tip("tip1") == "tip1"

    def test_get_compression_tip_returns_self_for_uncompressed(self, db):
        db.sessions.create("solo", "cli")
        assert db.sessions.compression_tip("solo") == "solo"

    def test_get_compression_tip_skips_delegate_children(self, db):
        """Delegate subagents have parent_session_id set but were created
        BEFORE the parent ended. They must not be followed as compression
        continuations — the started_at >= ended_at guard handles this.
        """
        import time as _time
        self._build_compression_chain(db, _time.time() - 3600)
        # delegate1 is a child of root1 but NOT a compression continuation.
        # root1's tip must be tip1 (via mid1), not delegate1.
        assert db.sessions.compression_tip("root1") == "tip1"

    def test_list_surfaces_tip_for_compressed_root(self, db):
        """The list must show the tip's id/message_count/preview in place of
        the root row, so users can see and resume the live conversation.
        """
        import time as _time
        self._build_compression_chain(db, _time.time() - 3600)
        # Add an uncompressed root for comparison.
        db.sessions.create("solo", "cli")
        db.messages.append("solo", "user", "standalone")
        db._conn.commit()

        sessions = db.sessions.list_rich(source="cli", limit=20)
        ids = [s["id"] for s in sessions]
        # Only top-level conversations appear: tip1 (projected from root1) + solo.
        # Delegate children, mid1, and the dead root1 must NOT be in the list.
        assert "tip1" in ids
        assert "solo" in ids
        assert "root1" not in ids
        assert "mid1" not in ids
        assert "delegate1" not in ids

        tip_row = next(s for s in sessions if s["id"] == "tip1")
        # The row surfaces the tip's identity but preserves the root's start
        # timestamp for stable ordering and lineage tracking.
        assert tip_row["_lineage_root_id"] == "root1"
        assert tip_row["preview"].startswith("latest message")
        assert tip_row["ended_at"] is None  # tip is still live
        assert tip_row["end_reason"] is None

    def test_list_without_projection_returns_raw_root(self, db):
        """project_compression_tips=False returns the raw parent-NULL root
        rows — useful for admin/debug UIs.
        """
        import time as _time
        self._build_compression_chain(db, _time.time() - 3600)
        sessions = db.sessions.list_rich(
            source="cli", limit=20, project_compression_tips=False
        )
        ids = [s["id"] for s in sessions]
        assert "root1" in ids
        assert "tip1" not in ids

        root_row = next(s for s in sessions if s["id"] == "root1")
        assert root_row["end_reason"] == "compression"
        assert "_lineage_root_id" not in root_row

    def test_list_preserves_sort_by_started_at(self, db):
        """Chronological ordering uses the ROOT's started_at (conversation
        start), not the tip's. This keeps lineage entries stable in the list
        even as new compressions push the tip forward in time.
        """
        import time as _time
        t0 = _time.time() - 3600
        self._build_compression_chain(db, t0)

        # Create a newer standalone session that should sort above the lineage
        # if we used tip.started_at, but below if we correctly use root.started_at.
        t_between = t0 + 120  # between root1 and its compression
        db.sessions.create("newer", "cli")
        db._conn.execute("UPDATE sessions SET started_at=? WHERE id=?", (t_between, "newer"))
        db.messages.append("newer", "user", "newer session started after root1")
        db._conn.commit()

        sessions = db.sessions.list_rich(source="cli", limit=20)
        ids_in_order = [s["id"] for s in sessions]
        # 'newer' started AFTER root1 but BEFORE tip1's actual started_at.
        # Correct ordering (by root started_at): newer > tip1's lineage entry.
        assert ids_in_order.index("newer") < ids_in_order.index("tip1")

    def test_list_handles_broken_chain_gracefully(self, db):
        """A compression root with no child (e.g. DB corruption or a partial
        end_session call that didn't finish creating the child) must not
        crash the list — it should fall back to surfacing the root as-is.
        """
        import time as _time
        t0 = _time.time() - 100
        db.sessions.create("orphan", "cli")
        db._conn.execute("UPDATE sessions SET started_at=? WHERE id=?", (t0, "orphan"))
        db._conn.execute(
            "UPDATE sessions SET ended_at=?, end_reason=? WHERE id=?",
            (t0 + 10, "compression", "orphan"),
        )
        db._conn.commit()

        sessions = db.sessions.list_rich(source="cli", limit=10)
        ids = [s["id"] for s in sessions]
        assert "orphan" in ids
        row = next(s for s in sessions if s["id"] == "orphan")
        # No tip means no projection — row stays raw.
        assert "_lineage_root_id" not in row
        assert row["end_reason"] == "compression"


# =========================================================================
# Session source exclusion (--source flag for third-party isolation)
# =========================================================================

class TestExcludeSources:
    """Tests for exclude_sources on list_sessions_rich and search_messages."""

    def test_list_sessions_rich_excludes_tool_source(self, db):
        db.sessions.create("s1", "cli")
        db.sessions.create("s2", "tool")
        db.sessions.create("s3", "telegram")
        sessions = db.sessions.list_rich(exclude_sources=["tool"])
        ids = [s["id"] for s in sessions]
        assert "s1" in ids
        assert "s3" in ids
        assert "s2" not in ids

    def test_list_sessions_rich_no_exclusion_returns_all(self, db):
        db.sessions.create("s1", "cli")
        db.sessions.create("s2", "tool")
        sessions = db.sessions.list_rich()
        ids = [s["id"] for s in sessions]
        assert "s1" in ids
        assert "s2" in ids

    def test_list_sessions_rich_source_and_exclude_combined(self, db):
        """When source= is explicit, exclude_sources should not conflict."""
        db.sessions.create("s1", "cli")
        db.sessions.create("s2", "tool")
        db.sessions.create("s3", "telegram")
        # Explicit source filter: only tool sessions, no exclusion
        sessions = db.sessions.list_rich(source="tool")
        ids = [s["id"] for s in sessions]
        assert ids == ["s2"]

    def test_list_sessions_rich_exclude_multiple_sources(self, db):
        db.sessions.create("s1", "cli")
        db.sessions.create("s2", "tool")
        db.sessions.create("s3", "cron")
        db.sessions.create("s4", "telegram")
        sessions = db.sessions.list_rich(exclude_sources=["tool", "cron"])
        ids = [s["id"] for s in sessions]
        assert "s1" in ids
        assert "s4" in ids
        assert "s2" not in ids
        assert "s3" not in ids

    def test_search_messages_excludes_tool_source(self, db):
        db.sessions.create("s1", "cli")
        db.messages.append("s1", "user", "Python deployment question")
        db.sessions.create("s2", "tool")
        db.messages.append("s2", "user", "Python automated question")
        results = db.messages.search("Python", exclude_sources=["tool"])
        sources = [r["source"] for r in results]
        assert "cli" in sources
        assert "tool" not in sources

    def test_search_messages_no_exclusion_returns_all_sources(self, db):
        db.sessions.create("s1", "cli")
        db.messages.append("s1", "user", "Rust deployment question")
        db.sessions.create("s2", "tool")
        db.messages.append("s2", "user", "Rust automated question")
        results = db.messages.search("Rust")
        sources = [r["source"] for r in results]
        assert "cli" in sources
        assert "tool" in sources

    def test_search_messages_source_include_and_exclude(self, db):
        """source_filter (include) and exclude_sources can coexist."""
        db.sessions.create("s1", "cli")
        db.messages.append("s1", "user", "Golang test")
        db.sessions.create("s2", "telegram")
        db.messages.append("s2", "user", "Golang test")
        db.sessions.create("s3", "tool")
        db.messages.append("s3", "user", "Golang test")
        # Include cli+tool, but exclude tool → should only return cli
        results = db.messages.search(
            "Golang", source_filter=["cli", "tool"], exclude_sources=["tool"]
        )
        sources = [r["source"] for r in results]
        assert sources == ["cli"]


class TestResolveSessionByNameOrId:
    """Tests for the main.py helper that resolves names or IDs."""

    def test_resolve_by_id(self, db):
        db.sessions.create("test-id-123", "cli")
        session = db.sessions.get("test-id-123")
        assert session is not None
        assert session["id"] == "test-id-123"

    def test_resolve_by_title_falls_back(self, db):
        db.sessions.create("s1", "cli")
        db.sessions.set_title("s1", "my project")
        result = db.sessions.resolve_by_title("my project")
        assert result == "s1"


# =========================================================================
# Concurrent write safety / lock contention fixes (#3139)
# =========================================================================

class TestConcurrentWriteSafety:
    def test_create_session_insert_or_ignore_is_idempotent(self, db):
        """create_session with the same ID twice must not raise (INSERT OR IGNORE)."""
        db.sessions.create(session_id="dup-1", source="cli", model="m")
        # Second call should be silent — no IntegrityError
        db.sessions.create(session_id="dup-1", source="gateway", model="m2")
        session = db.sessions.get("dup-1")
        # Row should exist (first write wins with OR IGNORE)
        assert session is not None
        assert session["source"] == "cli"

    def test_ensure_session_creates_missing_row(self, db):
        """ensure_session must create a minimal row when the session doesn't exist."""
        assert db.sessions.get("orphan-session") is None
        db.sessions.ensure("orphan-session", source="gateway", model="test-model")
        row = db.sessions.get("orphan-session")
        assert row is not None
        assert row["source"] == "gateway"
        assert row["model"] == "test-model"

    def test_ensure_session_is_idempotent(self, db):
        """ensure_session on an existing row must be a no-op (no overwrite)."""
        db.sessions.create(session_id="existing", source="cli", model="original-model")
        db.sessions.ensure("existing", source="gateway", model="overwrite-model")
        row = db.sessions.get("existing")
        # First write wins — ensure_session must not overwrite
        assert row["source"] == "cli"
        assert row["model"] == "original-model"

    def test_ensure_session_allows_append_message_after_failed_create(self, db):
        """Messages can be flushed even when create_session failed at startup.

        Simulates the #3139 scenario: create_session raises (lock), then
        ensure_session is called during flush, then append_message succeeds.
        """
        # Simulate failed create_session — row absent
        db.sessions.ensure("late-session", source="gateway", model="gpt-4")
        db.messages.append(
            session_id="late-session",
            role="user",
            content="hello after lock",
        )
        msgs = db.messages.list("late-session")
        assert len(msgs) == 1
        assert msgs[0]["content"] == "hello after lock"

    def test_sqlite_busy_timeout_is_configured(self, db):
        """Repository connections wait for transient writer contention."""
        timeout_ms = int(db._conn.execute("PRAGMA busy_timeout").fetchone()[0])
        assert timeout_ms >= 5000


# =========================================================================
# Auto-maintenance: state_meta + vacuum + maybe_auto_prune_and_vacuum
# =========================================================================

class TestStateMeta:
    def test_get_meta_missing_returns_none(self, db):
        assert db.metadata.get("nonexistent") is None

    def test_set_then_get_meta(self, db):
        db.metadata.set("foo", "bar")
        assert db.metadata.get("foo") == "bar"

    def test_set_meta_upsert(self, db):
        """set_meta overwrites existing value (ON CONFLICT DO UPDATE)."""
        db.metadata.set("key", "v1")
        db.metadata.set("key", "v2")
        assert db.metadata.get("key") == "v2"


class TestVacuum:
    def test_vacuum_runs_without_error(self, db):
        """VACUUM must succeed on a fresh DB (no rows to reclaim)."""
        db.sessions.create(session_id="s1", source="cli")
        db.messages.append(session_id="s1", role="user", content="hi")
        # Should not raise, even though there's nothing significant to reclaim.
        db.maintenance.vacuum()


class TestAutoMaintenance:
    def _make_old_ended(self, db, sid: str, days_old: int = 100):
        """Create a session that is ended and was started `days_old` days ago."""
        db.sessions.create(session_id=sid, source="cli")
        db.sessions.end(sid, reason="done")
        db._conn.execute(
            "UPDATE sessions SET started_at = ? WHERE id = ?",
            (time.time() - days_old * 86400, sid),
        )
        db._conn.commit()

    def test_first_run_prunes_and_vacuums(self, db):
        self._make_old_ended(db, "old1", days_old=100)
        self._make_old_ended(db, "old2", days_old=100)
        db.sessions.create(session_id="new", source="cli")  # active, must survive

        result = db.maintenance.maybe_auto_prune_and_vacuum(retention_days=90)
        assert result["skipped"] is False
        assert result["pruned"] == 2
        assert result["vacuumed"] is True
        assert result.get("error") is None
        assert db.sessions.get("old1") is None
        assert db.sessions.get("old2") is None
        assert db.sessions.get("new") is not None

    def test_second_call_within_interval_skips(self, db):
        self._make_old_ended(db, "old", days_old=100)
        first = db.maintenance.maybe_auto_prune_and_vacuum(
            retention_days=90, min_interval_hours=24
        )
        assert first["skipped"] is False
        assert first["pruned"] == 1

        # Create another prunable session; a second call within
        # min_interval_hours should still skip without touching it.
        self._make_old_ended(db, "old2", days_old=100)
        second = db.maintenance.maybe_auto_prune_and_vacuum(
            retention_days=90, min_interval_hours=24
        )
        assert second["skipped"] is True
        assert second["pruned"] == 0
        assert db.sessions.get("old2") is not None  # untouched

    def test_second_call_after_interval_runs_again(self, db):
        self._make_old_ended(db, "old", days_old=100)
        db.maintenance.maybe_auto_prune_and_vacuum(retention_days=90, min_interval_hours=24)

        # Backdate the last-run marker to force another run.
        db.metadata.set("last_auto_prune", str(time.time() - 48 * 3600))

        self._make_old_ended(db, "old2", days_old=100)
        result = db.maintenance.maybe_auto_prune_and_vacuum(
            retention_days=90, min_interval_hours=24
        )
        assert result["skipped"] is False
        assert result["pruned"] == 1
        assert db.sessions.get("old2") is None

    def test_no_prunable_sessions_no_vacuum(self, db):
        """When prune deletes 0 rows, VACUUM is skipped (wasted I/O)."""
        db.sessions.create(session_id="fresh", source="cli")  # too recent
        result = db.maintenance.maybe_auto_prune_and_vacuum(retention_days=90)
        assert result["skipped"] is False
        assert result["pruned"] == 0
        assert result["vacuumed"] is False
        # But last-run is still recorded so we don't retry immediately.
        assert db.metadata.get("last_auto_prune") is not None

    def test_auto_run_event_compaction_prunes_terminal_message_delta_chunks(self, db):
        db.runs.upsert(
            run_id="run-1",
            session_id="stored-1",
            runtime_scope_key="stored-1",
            turn_id="turn-1",
            execution_session_id="runtime-1",
            status="completed",
        )
        for seq, payload in (
            (1, '{"mode":"append","text":"A","delta":"A","offset":0}'),
            (2, '{"mode":"append","text":"B","delta":"B","offset":1}'),
            (3, '{"status":"complete","text":"AB"}'),
        ):
            db._conn.execute(
                """
                INSERT INTO run_events (
                    session_id, run_id, turn_id, execution_session_id, runtime_scope_key,
                    event_type, seq, timestamp, payload_json, event_json, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "stored-1",
                    "run-1",
                    "turn-1",
                    "runtime-1",
                    "stored-1",
                    "message.complete" if seq == 3 else "message.delta",
                    seq,
                    float(seq),
                    payload,
                    (
                        f'{{"type":"{"message.complete" if seq == 3 else "message.delta"}","session_id":"runtime-1",'
                        '"conversation_session_id":"stored-1","run_id":"run-1",'
                        '"turn_id":"turn-1","runtime_scope_key":"stored-1",'
                        f'"seq":{seq},"timestamp":{float(seq)},"payload":{payload}}}'
                    ),
                    "completed" if seq == 3 else "",
                ),
            )
        db._conn.commit()

        result = db.run_event_maintenance.maybe_auto_compact(vacuum=False)
        second = db.run_event_maintenance.maybe_auto_compact(vacuum=False)
        events = db.runs.list_events("stored-1")

        assert result["skipped"] is False
        assert result["deleted_events"] == 2
        assert result["pruned_terminal_stream_events"] == 2
        assert result["compacted_segments"] == 0
        assert result["vacuumed"] is False
        assert db.metadata.get("last_auto_run_event_compaction_v1") is not None
        assert second["skipped"] is True
        assert [event["type"] for event in events] == ["message.complete"]
        assert _business_payload(events[0]) == {"status": "complete", "text": "AB"}

    def test_vacuum_disabled_via_flag(self, db):
        self._make_old_ended(db, "old", days_old=100)
        result = db.maintenance.maybe_auto_prune_and_vacuum(retention_days=90, vacuum=False)
        assert result["pruned"] == 1
        assert result["vacuumed"] is False

    def test_corrupt_last_run_marker_treated_as_no_prior_run(self, db):
        """A non-numeric marker must not break maintenance."""
        db.metadata.set("last_auto_prune", "not-a-timestamp")
        self._make_old_ended(db, "old", days_old=100)
        result = db.maintenance.maybe_auto_prune_and_vacuum(retention_days=90)
        assert result["skipped"] is False
        assert result["pruned"] == 1

    def test_state_meta_survives_vacuum(self, db):
        """Marker written just before VACUUM must still be readable after."""
        self._make_old_ended(db, "old", days_old=100)
        db.maintenance.maybe_auto_prune_and_vacuum(retention_days=90)
        marker = db.metadata.get("last_auto_prune")
        assert marker is not None
        # Should parse as a float timestamp close to now.
        assert abs(float(marker) - time.time()) < 60

    def test_auto_prune_deletes_transcript_files(self, db, tmp_path):
        """Issue #3015: auto-prune must also delete on-disk transcript files."""
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()

        self._make_old_ended(db, "old1", days_old=100)
        self._make_old_ended(db, "old2", days_old=100)
        db.sessions.create(session_id="new", source="cli")  # active

        # Transcript files mimicking real gateway/CLI layout
        (sessions_dir / "old1.json").write_text("{}")
        (sessions_dir / "old1.jsonl").write_text("{}\n")
        (sessions_dir / "old2.jsonl").write_text("{}\n")
        (sessions_dir / "request_dump_old1_001.json").write_text("{}")
        (sessions_dir / "new.jsonl").write_text("{}\n")  # active, must survive

        result = db.maintenance.maybe_auto_prune_and_vacuum(
            retention_days=90, sessions_dir=sessions_dir
        )
        assert result["pruned"] == 2

        # Pruned transcript files are gone
        assert not (sessions_dir / "old1.json").exists()
        assert not (sessions_dir / "old1.jsonl").exists()
        assert not (sessions_dir / "old2.jsonl").exists()
        assert not (sessions_dir / "request_dump_old1_001.json").exists()
        # Active session's transcript is untouched
        assert (sessions_dir / "new.jsonl").exists()

    def test_auto_prune_without_sessions_dir_preserves_files(self, db, tmp_path):
        """Backward-compat: no sessions_dir = DB-only cleanup (legacy behavior)."""
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()
        self._make_old_ended(db, "old", days_old=100)
        (sessions_dir / "old.jsonl").write_text("{}\n")

        result = db.maintenance.maybe_auto_prune_and_vacuum(retention_days=90)
        assert result["pruned"] == 1
        # File stays — caller didn't opt in
        assert (sessions_dir / "old.jsonl").exists()

    def test_prune_sessions_deletes_files_for_pruned_only(self, db, tmp_path):
        """Active-session transcripts must never be deleted by prune."""
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()
        self._make_old_ended(db, "old", days_old=100)
        db.sessions.create(session_id="active", source="cli")  # not ended
        (sessions_dir / "old.jsonl").write_text("{}\n")
        (sessions_dir / "active.jsonl").write_text("{}\n")

        count = db.maintenance.prune_sessions(older_than_days=90, sessions_dir=sessions_dir)
        assert count == 1
        assert not (sessions_dir / "old.jsonl").exists()
        assert (sessions_dir / "active.jsonl").exists()


# =========================================================================
# FTS5 indexing of tool_calls / tool_name (#16751)
# =========================================================================

class TestFTS5ToolCallIndexing:
    """Regression tests: search_messages must see tool_name and tool_calls.

    Before #16751's fix, `messages_fts` only indexed `messages.content`, so
    tokens that only appeared in `tool_name` or the serialized `tool_calls`
    JSON were invisible to session_search even though the row was in the DB.
    """

    def test_tool_name_is_searchable(self, db):
        db.sessions.create(session_id="s1", source="cli")
        db.messages.append(
            "s1", role="assistant", content="",
            tool_name="UNIQUETOOLNAME",
        )
        results = db.messages.search("UNIQUETOOLNAME")
        assert len(results) == 1

    def test_tool_calls_args_are_searchable(self, db):
        db.sessions.create(session_id="s1", source="cli")
        db.messages.append(
            "s1", role="assistant", content="",
            tool_calls=[{
                "id": "c1",
                "type": "function",
                "function": {
                    "name": "web_search",
                    "arguments": '{"query": "UNIQUESEARCHTOKEN"}',
                },
            }],
        )
        results = db.messages.search("UNIQUESEARCHTOKEN")
        assert len(results) == 1

    def test_tool_function_name_in_tool_calls_is_searchable(self, db):
        db.sessions.create(session_id="s1", source="cli")
        db.messages.append(
            "s1", role="assistant", content="",
            tool_calls=[{
                "id": "c1",
                "type": "function",
                "function": {"name": "UNIQUEFUNCNAME", "arguments": "{}"},
            }],
        )
        results = db.messages.search("UNIQUEFUNCNAME")
        assert len(results) == 1

    def test_delete_message_row_does_not_crash(self, db):
        """DELETE on messages must not raise when FTS rows reference tool fields.

        Previously the messages_fts_delete trigger passed old.content to the
        FTS5 delete-command but the inserted row was the concatenation of
        content || tool_name || tool_calls, so FTS5 rejected the delete with
        'SQL logic error' and every session delete path broke.
        """
        db.sessions.create(session_id="s1", source="cli")
        db.messages.append(
            "s1", role="assistant", content="hello",
            tool_name="web_search",
            tool_calls=[{
                "id": "c1",
                "type": "function",
                "function": {"name": "web_search", "arguments": '{"q": "x"}'},
            }],
        )
        # end_session + end-time prune path would exercise DELETE; hit the
        # row directly through the write helper to keep the regression focused.
        def _delete(conn):
            conn.execute("DELETE FROM messages WHERE session_id = ?", ("s1",))
        db._execute_write(_delete)  # must not raise

        assert db.messages.search("hello") == []
        assert db.messages.search("web_search") == []

    def test_update_message_reindexes_tool_fields(self, db):
        """UPDATE must refresh the FTS row so old tokens drop out and new tokens appear."""
        db.sessions.create(session_id="s1", source="cli")
        db.messages.append(
            "s1", role="assistant", content="",
            tool_name="ORIGINALTOOL",
        )
        assert len(db.messages.search("ORIGINALTOOL")) == 1

        def _update(conn):
            conn.execute(
                "UPDATE messages SET tool_name = ? WHERE session_id = ?",
                ("RENAMEDTOOL", "s1"),
            )
        db._execute_write(_update)

        assert db.messages.search("ORIGINALTOOL") == []
        assert len(db.messages.search("RENAMEDTOOL")) == 1


class TestFTS5ToolCallMigration:
    """v11 migration: pre-existing state.db with old external-content FTS tables
    must be re-indexed so tool_name / tool_calls become searchable after upgrade."""

    def test_v10_to_v11_upgrade_backfills_tool_fields(self, tmp_path):
        """Simulate an existing user: build a v10-shaped DB by hand, insert a
        row with tool_calls, then open via CliSessionStore (which runs migrations).
        After upgrade, the tool_calls token must be searchable."""
        import sqlite3

        db_path = tmp_path / "legacy.db"

        # Build the pre-v11 schema by hand: external-content FTS tables +
        # old triggers that only reference new.content.
        conn = sqlite3.connect(str(db_path))
        conn.executescript("""
            CREATE TABLE schema_version (version INTEGER NOT NULL);
            INSERT INTO schema_version (version) VALUES (10);

            CREATE TABLE sessions (
                id TEXT PRIMARY KEY,
                source TEXT,
                started_at REAL,
                ended_at REAL,
                title TEXT,
                parent_session_id TEXT,
                message_count INTEGER DEFAULT 0,
                tool_call_count INTEGER DEFAULT 0,
                api_call_count INTEGER DEFAULT 0
            );
            CREATE TABLE messages (
                id INTEGER PRIMARY KEY,
                session_id TEXT NOT NULL,
                timestamp REAL NOT NULL,
                role TEXT NOT NULL,
                content TEXT,
                tool_name TEXT,
                tool_calls TEXT,
                tool_call_id TEXT,
                token_count INTEGER,
                finish_reason TEXT,
                reasoning TEXT,
                reasoning_content TEXT,
                reasoning_details TEXT,
                codex_reasoning_items TEXT,
                codex_message_items TEXT
            );

            CREATE VIRTUAL TABLE messages_fts USING fts5(
                content, content=messages, content_rowid=id
            );
            CREATE TRIGGER messages_fts_insert AFTER INSERT ON messages BEGIN
                INSERT INTO messages_fts(rowid, content) VALUES (new.id, new.content);
            END;

            CREATE VIRTUAL TABLE messages_fts_trigram USING fts5(
                content, content=messages, content_rowid=id, tokenize='trigram'
            );
            CREATE TRIGGER messages_fts_trigram_insert AFTER INSERT ON messages BEGIN
                INSERT INTO messages_fts_trigram(rowid, content) VALUES (new.id, new.content);
            END;
        """)
        conn.execute(
            "INSERT INTO sessions (id, source, started_at) VALUES (?, ?, ?)",
            ("s1", "cli", time.time()),
        )
        conn.execute(
            "INSERT INTO messages (session_id, timestamp, role, content, tool_name, tool_calls) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("s1", time.time(), "assistant", "", "LEGACYTOOL",
             '{"function":{"name":"web_search","arguments":"{\\"q\\":\\"LEGACYARG\\"}"}}'),
        )
        conn.commit()

        # Verify the legacy FTS rows don't contain the tool tokens yet.
        legacy_hits = conn.execute(
            "SELECT rowid FROM messages_fts WHERE messages_fts MATCH 'LEGACYTOOL'"
        ).fetchall()
        assert legacy_hits == [], "sanity: legacy FTS must NOT contain tool_name"
        conn.close()

        # Now open via CliSessionStore — migration runs.
        session_db = open_cli_session_store(db_path=db_path)
        try:
            assert len(session_db.messages.search("LEGACYTOOL")) == 1, \
                "v11 migration must backfill tool_name into FTS"
            assert len(session_db.messages.search("LEGACYARG")) == 1, \
                "v11 migration must backfill tool_calls JSON into FTS"
            # schema_version bumped
            row = session_db._conn.execute(
                "SELECT version FROM schema_version LIMIT 1"
            ).fetchone()
            version = row["version"] if hasattr(row, "keys") else row[0]
            assert version == CURRENT_SCHEMA_VERSION
        finally:
            session_db.close()


class TestSessionRewindSoftDelete:
    """Rewind keeps an audit trail while active transcript reads move back."""

    def test_rewind_soft_deletes_target_and_tail(self, db):
        db.sessions.create(session_id="rewind-s1", source="tui")
        db.messages.append("rewind-s1", role="user", content="question 1")
        db.messages.append("rewind-s1", role="assistant", content="answer 1")
        target_id = db.messages.append("rewind-s1", role="user", content="question 2")
        db.messages.append("rewind-s1", role="assistant", content="answer 2")

        result = db.messages.rewind("rewind-s1", target_id)

        assert result["rewound_count"] == 2
        assert result["target_message"]["content"] == "question 2"
        assert result["new_head_id"] == target_id - 1
        assert [m["content"] for m in db.messages.list("rewind-s1")] == [
            "question 1",
            "answer 1",
        ]
        all_rows = db.messages.list("rewind-s1", include_inactive=True)
        assert [row["active"] for row in all_rows] == [1, 1, 0, 0]
        assert db.sessions.get("rewind-s1")["rewind_count"] == 1

    def test_rewind_requires_user_target(self, db):
        db.sessions.create(session_id="rewind-s2", source="tui")
        target_id = db.messages.append("rewind-s2", role="assistant", content="answer")

        with pytest.raises(ValueError, match="user"):
            db.messages.rewind("rewind-s2", target_id)

    def test_rewound_rows_are_hidden_from_search_by_default(self, db):
        db.sessions.create(session_id="rewind-s3", source="tui")
        db.messages.append("rewind-s3", role="user", content="keep this")
        target_id = db.messages.append("rewind-s3", role="user", content="UNIQUE_REWIND_TOKEN")

        db.messages.rewind("rewind-s3", target_id)

        assert db.messages.search("UNIQUE_REWIND_TOKEN") == []
        assert len(db.messages.search("UNIQUE_REWIND_TOKEN", include_inactive=True)) == 1

    def test_list_recent_user_messages_defaults_to_active_rows(self, db):
        db.sessions.create(session_id="rewind-s4", source="tui")
        db.messages.append("rewind-s4", role="user", content="old prompt")
        target_id = db.messages.append("rewind-s4", role="user", content="new prompt")
        db.messages.rewind("rewind-s4", target_id)

        assert [row["preview"] for row in db.messages.recent_user_messages("rewind-s4")] == [
            "old prompt"
        ]
        assert [
            row["preview"]
            for row in db.messages.recent_user_messages("rewind-s4", include_inactive=True)
        ] == ["new prompt", "old prompt"]


class TestSessionIdSearch:
    """Session id search backs desktop/web session search without O(n) scans."""

    def _seed(self, db, sid, *, content="ordinary message"):
        db.sessions.create(session_id=sid, source="cli", model="test-model")
        db.messages.append(session_id=sid, role="user", content=content)

    def test_search_sessions_by_id_matches_exact_prefix_and_substring(self, db):
        self._seed(db, "20260603_090200_abcd12")
        self._seed(db, "20260602_111111_other99")

        assert [s["id"] for s in db.sessions.search_by_id("20260603_090200_abcd12")] == [
            "20260603_090200_abcd12"
        ]
        assert [s["id"] for s in db.sessions.search_by_id("20260603")] == [
            "20260603_090200_abcd12"
        ]
        assert [s["id"] for s in db.sessions.search_by_id("ABCD12")] == [
            "20260603_090200_abcd12"
        ]

    def test_search_sessions_by_id_prioritizes_exact_then_prefix(self, db):
        self._seed(db, "20260603_090200_abcd12")
        self._seed(db, "20260603_090200_abcd12_child")
        self._seed(db, "x_20260603_090200_abcd12")

        ids = [s["id"] for s in db.sessions.search_by_id("20260603_090200_abcd12", limit=2)]

        assert ids == ["20260603_090200_abcd12", "20260603_090200_abcd12_child"]

    def test_search_sessions_by_id_matches_projected_compression_root(self, db):
        root = "20260602_235959_root99"
        tip = "20260603_010000_tip01"
        db.sessions.create(session_id=root, source="cli")
        db.messages.append(root, role="user", content="root conversation")
        db.sessions.end(root, "compression")
        db.sessions.create(session_id=tip, source="cli", parent_session_id=root)
        db.messages.append(tip, role="user", content="continued conversation")

        matches = db.sessions.search_by_id("root99")

        assert [s["id"] for s in matches] == [tip]
        assert matches[0]["_lineage_root_id"] == root


class TestDovieLineageBranchListing:
    def test_session_lineage_branch_stays_visible_after_parent_reopen(self, db):
        db.sessions.create("source-reopen", "tui")
        db.sessions.set_title("source-reopen", "Source")
        target_id = db.messages.append("source-reopen", role="user", content="branch point")
        db.messages.append("source-reopen", role="assistant", content="answer")

        db.branches.branch_session(
            source_session_id="source-reopen",
            new_session_id="branch-reopen",
            branch_point={"message_id": str(target_id)},
            idempotency_key="branch-reopen-key",
        )
        db.sessions.reopen("source-reopen")
        db.sessions.end("source-reopen", "user_exit")

        ids = {row["id"] for row in db.sessions.list_rich(limit=10)}

        assert "source-reopen" in ids
        assert "branch-reopen" in ids
