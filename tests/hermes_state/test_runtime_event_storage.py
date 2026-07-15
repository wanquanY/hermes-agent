"""Run-event persistence and projection contracts."""

import sqlite3
import time
from pathlib import Path

from hermes_conversation_message_identity import AssistantMessageIdentity
from hermes_conversation_message_identity import assistant_conversation_message_id_for
from hermes_agent.storage.cli_session_store import open_cli_session_store
from tests.hermes_state.support import business_payload


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
    assert business_payload(events[0]) == {
        "mode": "append",
        "text": "你",
        "delta": "你",
        "offset": 0,
    }
    assert business_payload(events[1]) == {
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
    assert business_payload(events[2]) == {
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
    assert business_payload(message_events[0]) == {
        "mode": "append",
        "text": "A",
        "delta": "A",
        "offset": 0,
    }
    assert business_payload(message_events[1]) == {
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
    assert business_payload(events[0]) == {
        "mode": "append",
        "text": "你",
        "delta": "你",
        "offset": 0,
    }
    assert business_payload(events[1]) == {
        "mode": "append",
        "text": "好",
        "delta": "好",
        "offset": 1,
    }
    assert business_payload(events[2]) == {
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
    assert business_payload(events[0]) == {
        "text": "思考过程",
        "source": "provider_reasoning",
    }
    assert business_payload(events[1]) == {"text": "不同来源", "source": "other"}


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
