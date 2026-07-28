import json
import sqlite3
import zlib

import pytest

from hermes_agent.read_models.session_index import SessionIndexQuery, SessionIndexReadModel
from hermes_agent.composition.cli_session_store import open_cli_session_store


def test_cli_session_store_persists_and_replays_messages(tmp_path):
    store = open_cli_session_store(tmp_path / "state.db")

    store.sessions.create("s1", "cli", model="gpt-test")
    row_id = store.messages.append(
        "s1",
        "assistant",
        "hello",
        tool_calls=[{"function": {"name": "search"}}],
        metadata={"run_id": "run-1"},
    )

    assert row_id > 0
    rows = store.messages.list("s1")
    assert [row["role"] for row in rows] == ["assistant"]
    assert rows[0]["tool_calls"] == [{"function": {"name": "search"}}]
    assert rows[0]["metadata"] == {"run_id": "run-1"}

    replay = store.messages.all_as_conversation("s1", include_storage_metadata=True)
    assert replay[0]["role"] == "assistant"
    assert replay[0]["content"] == "hello"
    assert replay[0]["message_id"] == str(row_id)


def test_cli_session_store_repeated_runtime_initialization_preserves_prior_turns(tmp_path):
    store = open_cli_session_store(tmp_path / "state.db")

    store.sessions.create("s1", "tui", title="First prompt")
    started_at = store.sessions.get("s1")["started_at"]
    store.messages.append("s1", "user", "first question")
    store.messages.append("s1", "assistant", "first answer")

    store.sessions.create("s1", "runtime", title="Second prompt")
    store.messages.append("s1", "user", "second question")
    store.messages.append("s1", "assistant", "second answer")

    assert store.sessions.get("s1")["started_at"] == started_at
    assert [row["content"] for row in store.messages.list("s1")] == [
        "first question",
        "first answer",
        "second question",
        "second answer",
    ]


def test_cli_session_store_allows_multiple_untitled_sessions(tmp_path):
    store = open_cli_session_store(tmp_path / "state.db")

    store.sessions.create("s1", "tui")
    store.sessions.create("s2", "tui")

    assert store.sessions.get("s1")["title"] is None
    assert store.sessions.get("s2")["title"] is None


def test_cli_session_store_exposes_persisted_compression_leases(tmp_path):
    store = open_cli_session_store(tmp_path / "state.db")

    store.sessions.create("s1", "tui")

    assert store.compression_leases.try_acquire("s1", "foreground") is True
    assert store.compression_leases.holder("s1") == "foreground"
    assert store.compression_leases.release("s1", "foreground") is True


def test_cli_session_store_exposes_runtime_stability_aggregate(tmp_path):
    store = open_cli_session_store(tmp_path / "state.db")
    store.sessions.create("s1", "tui")

    store.runtime_stability.write_compression(
        "s1",
        ineffective_count=1,
        fallback_streak=2,
        verdict_pending=True,
    )

    state = store.runtime_stability.get("s1")
    assert state.compression_ineffective_count == 1
    assert state.compression_fallback_streak == 2
    assert state.compression_verdict_pending is True


def test_cli_session_store_title_resume_and_handoff(tmp_path):
    store = open_cli_session_store(tmp_path / "state.db")

    store.sessions.create("root", "cli", title="Planning")
    store.sessions.end("root", "compression")
    store.sessions.create("child", "cli", parent_session_id="root")
    store.messages.append("child", "user", "continued")

    assert store.sessions.resolve_resume_id("root") == "child"
    assert store.sessions.set_title("child", "Planning #2")
    assert store.sessions.get_by_title("Planning #2")["id"] == "child"
    assert store.sessions.next_title_in_lineage("Planning") == "Planning #3"

    assert store.sessions.request_handoff("child", "telegram") is True
    assert store.sessions.handoff_state("child") == {
        "state": "pending",
        "platform": "telegram",
        "error": None,
    }
    store.sessions.fail_handoff("child", "timeout")
    assert store.sessions.handoff_state("child")["state"] == "failed"


def test_cli_session_store_resolves_titles_to_latest_numbered_session(tmp_path):
    store = open_cli_session_store(tmp_path / "state.db")

    store.sessions.create("root", "cli", title="Planning")
    store.sessions.create("child-2", "cli", title="Planning #2")
    store.sessions.create("child-3", "cli", title="Planning #3")
    with store._lock:
        store._conn.execute("UPDATE sessions SET started_at = ? WHERE id = ?", (100.0, "child-2"))
        store._conn.execute("UPDATE sessions SET started_at = ? WHERE id = ?", (200.0, "child-3"))
        store._conn.commit()

    assert store.sessions.resolve_by_title("Planning") == "child-3"
    assert store.sessions.resolve_by_title("Missing") is None


def test_cli_session_store_updates_usage_and_system_prompt(tmp_path):
    store = open_cli_session_store(tmp_path / "state.db")

    store.sessions.create("s1", "cli", cwd="/workspace/initial")
    store.sessions.update_system_prompt("s1", "system")
    store.sessions.update_cwd("s1", "/workspace/current")
    store.sessions.update_token_counts(
        "s1",
        input_tokens=10,
        output_tokens=5,
        billing_provider="openai",
        pricing_version="v1",
    )

    row = store.sessions.get("s1")
    assert row["cwd"] == "/workspace/current"
    assert row["system_prompt"] == "system"
    assert row["input_tokens"] == 10
    assert row["output_tokens"] == 5
    assert row["billing_provider"] == "openai"
    assert row["pricing_version"] == "v1"


def test_cli_session_store_meta_and_delete(tmp_path):
    store = open_cli_session_store(tmp_path / "state.db")

    store.metadata.set("k", "v")
    assert store.metadata.get("k") == "v"
    store.sessions.create("s1", "cli")
    store.messages.append("s1", "user", {"kind": "json"})
    assert json.loads(store.sessions.get("s1")["model_config"] or "{}") == {}
    assert store.maintenance.delete_session("s1") is True
    assert store.sessions.get("s1") is None


def test_cli_session_store_db_path_remains_available_after_close(tmp_path):
    db_path = tmp_path / "state.db"
    store = open_cli_session_store(db_path)

    store.close()

    assert store.db_path == db_path


def test_cli_session_store_exports_counts_and_prunes_cli_sessions(tmp_path):
    db_path = tmp_path / "state.db"
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    store = open_cli_session_store(db_path)

    store.sessions.create("old", "cli")
    store.sessions.create("new", "cli")
    store.messages.append("old", "user", "stale")
    store.messages.append("new", "user", "keep")
    (sessions_dir / "old.jsonl").write_text("{}", encoding="utf-8")
    with store._lock:
        store._conn.execute(
            "UPDATE sessions SET started_at = ?, ended_at = ? WHERE id = ?",
            (100.0, 110.0, "old"),
        )
        store._conn.execute(
            "UPDATE sessions SET started_at = ?, ended_at = NULL WHERE id = ?",
            (9999999999.0, "new"),
        )
        store._conn.commit()

    assert store.db_path == db_path
    assert store.messages.count() == 2
    assert store.messages.count("old") == 1
    assert store.sessions.export("old")["messages"][0]["content"] == "stale"
    assert {session["id"] for session in store.sessions.export_all(source="cli")} == {"old", "new"}

    assert store.maintenance.prune_sessions(
        older_than_days=1,
        source="cli",
        sessions_dir=sessions_dir,
    ) == 1
    assert store.sessions.get("old") is None
    assert store.sessions.get("new") is not None
    assert not (sessions_dir / "old.jsonl").exists()


def test_cli_session_store_replaces_and_searches_messages(tmp_path):
    store = open_cli_session_store(tmp_path / "state.db")

    store.sessions.create("acp-1", "acp", model_config={"cwd": "/work"})
    store.messages.replace(
        "acp-1",
        [
            {"role": "user", "content": "configure nginx for ACP"},
            {
                "role": "assistant",
                "content": "I'll inspect the config",
                "reasoning": "needs file lookup",
                "tool_calls": [{"function": {"name": "read_file"}}],
            },
        ],
    )

    replay = store.messages.all_as_conversation("acp-1")
    assert [message["role"] for message in replay] == ["user", "assistant"]
    assert replay[1]["reasoning"] == "needs file lookup"
    assert replay[1]["tool_calls"] == [{"function": {"name": "read_file"}}]
    assert store.sessions.search(source="acp")[0]["id"] == "acp-1"
    assert store.messages.search("nginx", source_filter=["acp"])[0]["session_id"] == "acp-1"

    store.messages.replace("acp-1", [{"role": "user", "content": "replacement only"}])
    assert [message["content"] for message in store.messages.all_as_conversation("acp-1")] == [
        "replacement only"
    ]


def test_cli_session_store_exposes_agent_profile_registry(tmp_path):
    store = open_cli_session_store(tmp_path / "state.db")

    profile = store.profiles.upsert_agent_profile(
        profile_id="agent-1",
        slug="research-agent",
        name="Research Agent",
        hermes_profile_name="research-agent",
        hermes_home_path=str(tmp_path / "profiles" / "research-agent"),
        default_model="gpt-5",
        recommended_skills=["search"],
        current_version_id="snapshot-1",
        current_version_number=1,
    )
    draft = store.profiles.upsert_agent_profile_draft(
        draft_id="draft-1",
        draft_kind="revision",
        base_agent_profile_id=profile["id"],
        target_agent_profile_id=profile["id"],
        source_session_id="session-1",
        name="Research Agent draft",
        recommended_toolsets=["file"],
    )

    assert profile["runtimeScopeKey"] == "profile:agent-1"
    assert store.profiles.get_agent_profile_by_slug("research-agent")["id"] == "agent-1"
    assert store.profiles.list_agent_profiles()[0]["agentProfileVersionId"] == "snapshot-1"
    assert store.profiles.get_agent_profile_draft(draft["id"])["draftKind"] == "revision"
    assert store.profiles.list_agent_profile_drafts(source_session_id="session-1")[0]["id"] == "draft-1"
    assert store.profiles.discard_agent_profile_draft("draft-1")["status"] == "discarded"


def test_cli_session_store_exposes_team_registry(tmp_path):
    store = open_cli_session_store(tmp_path / "state.db")

    store.profiles.upsert_agent_profile(
        profile_id="agent-leader",
        slug="leader",
        name="Leader",
        avatar="dovie-avatar://leader",
        hermes_profile_name="leader",
        hermes_home_path=str(tmp_path / "profiles" / "leader"),
        current_version_id="snapshot-leader",
        current_version_number=1,
    )
    team = store.teams.upsert_agent_team(
        team_id="team-1",
        name="Research Team",
        description="Research and verify.",
        default_mode="supervised_mission",
        policy={"planApproval": "always"},
    )
    member = store.teams.upsert_agent_team_member(
        member_id="member-leader",
        team_id=team["id"],
        agent_profile_id="agent-leader",
        role="lead",
        capability_tags=["planning"],
    )

    teams = store.teams.list_agent_teams()
    members = store.teams.list_agent_team_members(team["id"])
    detail = store.teams.get_agent_team_with_members(team["id"])
    summaries = store.teams.list_agent_team_summaries()

    assert [item["id"] for item in teams] == ["team-1"]
    assert members[0]["id"] == member["id"]
    assert members[0]["profileName"] == "Leader"
    assert detail["members"][0]["capability_tags"] == ["planning"]
    assert summaries[0]["member_count"] == 1
    assert summaries[0]["leaderMember"]["profileAvatar"] == "dovie-avatar://leader"

    archived = store.teams.archive_agent_team(team["id"])
    assert archived["status"] == "archived"
    assert store.teams.list_agent_teams() == []
    assert [item["id"] for item in store.teams.list_agent_teams(include_archived=True)] == ["team-1"]


def test_cli_session_store_session_index_bootstraps_cross_read_tables(tmp_path):
    store = open_cli_session_store(tmp_path / "state.db")

    result = SessionIndexReadModel(store._conn).list(SessionIndexQuery(limit=10))

    assert result["sessions"] == []


def test_cli_session_store_indexes_team_conversation_session_column(tmp_path):
    db_path = tmp_path / "state.db"
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(
            """
            CREATE TABLE team_mission_conversations (
                conversation_id TEXT PRIMARY KEY,
                team_id TEXT,
                conversation_session_id TEXT NOT NULL UNIQUE,
                title TEXT NOT NULL,
                objective TEXT,
                workspace_id TEXT,
                workspace_path TEXT,
                status TEXT NOT NULL,
                active_mission_id TEXT,
                created_by_user_id TEXT,
                metadata_json TEXT,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );
            INSERT INTO team_mission_conversations (
                conversation_id, team_id, conversation_session_id, title, objective,
                workspace_id, workspace_path, status, active_mission_id,
                created_by_user_id, metadata_json, created_at, updated_at
            )
            VALUES (
                'conversation-1', 'team-1', 'team-session-1', 'Legacy Team',
                'objective', 'workspace-1', '/tmp/workspace', 'active',
                '', '', '{}', 1, 1
            );
            """
        )
        conn.commit()
    finally:
        conn.close()
    store = open_cli_session_store(db_path)
    store.sessions.create("team-session-1", "team_mission", title="Legacy Team")
    store._conn.execute(
        """
        UPDATE session_index
           SET conversation_kind = 'team',
               conversation_id = 'conversation-1',
               team_id = 'team-1',
               started_at = 1,
               updated_at = 1
         WHERE session_id = 'team-session-1'
        """
    )
    store._conn.commit()

    columns = {row["name"] for row in store._conn.execute("PRAGMA table_info(team_mission_conversations)").fetchall()}
    row = store._conn.execute(
        "SELECT conversation_session_id FROM team_mission_conversations WHERE conversation_id = ?",
        ("conversation-1",),
    ).fetchone()
    result = SessionIndexReadModel(store._conn).list(SessionIndexQuery(limit=10))

    assert "conversation_session_id" in columns
    assert "stable_session_id" not in columns
    assert row["conversation_session_id"] == "team-session-1"
    assert result["sessions"][0]["session_id"] == "team-session-1"

    store._conn.execute(
        """
        INSERT INTO team_mission_conversations (
            conversation_id, team_id, conversation_session_id, title, objective,
            workspace_id, workspace_path, status, created_by_user_id,
            metadata_json, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "conversation-2",
            "team-1",
            "team-session-2",
            "New Team",
            "new objective",
            "workspace-1",
            "/tmp/workspace",
            "active",
            "",
            "{}",
            2,
            2,
        ),
    )
    created = store._conn.execute(
        "SELECT conversation_id, conversation_session_id FROM team_mission_conversations WHERE conversation_id = ?",
        ("conversation-2",),
    ).fetchone()
    assert created["conversation_id"] == "conversation-2"
    assert created["conversation_session_id"] == "team-session-2"


def test_cli_session_store_keeps_internal_execution_out_of_conversation_index(tmp_path):
    store = open_cli_session_store(tmp_path / "state.db")
    store.sessions.create("conversation-1", "tui")
    store.sessions.create("execution-1", "runtime")
    store.sessions.create(
        "execution-1",
        "tui",
        parent_session_id="conversation-1",
        session_kind="execution",
        conversation_kind="internal",
    )

    execution = store.sessions.get("execution-1")
    indexed = store._conn.execute(
        "SELECT 1 FROM session_index WHERE session_id = ?",
        ("execution-1",),
    ).fetchone()
    sidebar = SessionIndexReadModel(store._conn).list(SessionIndexQuery(limit=10))

    assert execution["session_kind"] == "execution"
    assert execution["conversation_kind"] == "internal"
    assert indexed is None
    assert [item["session_id"] for item in sidebar["sessions"]] == ["conversation-1"]


def test_cli_session_store_persists_canonical_runtime_and_tool_events(tmp_path):
    db_path = tmp_path / "state.db"
    store = open_cli_session_store(db_path)
    store.sessions.create("conversation-1", "tui")
    store.runs.upsert(
        run_id="run-1",
        session_id="conversation-1",
        turn_id="turn-1",
        execution_session_id="execution-1",
    )

    first = store.runs.append_event(
        "conversation-1",
        {
            "type": "tool.start",
            "execution_session_id": "execution-1",
            "run_id": "run-1",
            "turn_id": "turn-1",
            "seq": 41,
            "payload": {
                "tool_call_id": "call-1",
                "name": "terminal",
                "arguments": {"cmd": "date"},
            },
        },
    )
    second = store.runs.append_event(
        "conversation-1",
        {
            "type": "tool.complete",
            "execution_session_id": "execution-1",
            "run_id": "run-1",
            "turn_id": "turn-1",
            "seq": 42,
            "payload": {
                "tool_call_id": "call-1",
                "name": "terminal",
                "result": "ok",
            },
        },
    )
    store.close()

    reopened = open_cli_session_store(db_path)
    replay = reopened.runs.list_events("conversation-1")
    tool = reopened._conn.execute(
        "SELECT status, seq_start, seq_last FROM tool_events WHERE tool_call_id = ?",
        ("call-1",),
    ).fetchone()

    assert [first["seq"], second["seq"]] == [1, 2]
    assert [event["type"] for event in replay] == ["tool.start", "tool.complete"]
    assert [event["runtime_source_seq"] for event in replay] == [41, 42]
    assert [event["execution_session_id"] for event in replay] == ["execution-1", "execution-1"]
    assert dict(tool) == {"status": "completed", "seq_start": 1, "seq_last": 2}


def test_repository_bootstrap_rejects_noncanonical_runtime_identity_schema(tmp_path):
    db_path = tmp_path / "state.db"
    store = open_cli_session_store(db_path)
    store.sessions.create("conversation-1", "tui")
    store.runs.upsert(
        run_id="run-1",
        session_id="conversation-1",
        execution_session_id="execution-1",
    )
    store.runs.append_event(
        "conversation-1",
        {
            "type": "message.start",
            "execution_session_id": "execution-1",
            "run_id": "run-1",
            "payload": {},
        },
    )
    store._conn.execute(
        """
        INSERT INTO session_runtime_state (
            session_id, execution_session_id, updated_at
        ) VALUES (?, ?, ?)
        """,
        ("conversation-1", "execution-1", 1),
    )
    store.close()

    legacy = sqlite3.connect(db_path)
    for table in ("runs", "run_events", "session_runtime_state"):
        legacy.execute(
            f"ALTER TABLE {table} "
            "RENAME COLUMN execution_session_id TO runtime_session_id"
        )
    legacy.commit()
    legacy.close()

    with pytest.raises(RuntimeError, match="runtime repository schema is not canonical"):
        open_cli_session_store(db_path)


def test_cli_session_store_recovers_orphaned_active_run_into_canonical_terminal_event(tmp_path):
    store = open_cli_session_store(tmp_path / "state.db")
    store.sessions.create("conversation-1", "tui")
    store.runs.upsert(
        run_id="run-orphan",
        session_id="conversation-1",
        execution_session_id="execution-orphan",
        metadata={"gateway_pid": 999_999_999, "gateway_instance_id": "old"},
    )

    failed = store.runs.fail_orphaned(
        current_pid=1,
        current_gateway_instance_id="new",
        owner_dead_grace_seconds=0,
    )
    run = store.runs.get("run-orphan")
    events = store.runs.list_events("conversation-1")
    status = store.runs.session_status("conversation-1")

    assert failed == 1
    assert run["status"] == "failed"
    assert events[-1]["type"] == "error"
    assert events[-1]["payload"]["status"] == "failed"
    assert status["running"] is False
    assert status["last_event_seq"] == events[-1]["seq"]


def test_cli_session_store_projects_session_info_from_canonical_event(tmp_path):
    store = open_cli_session_store(tmp_path / "state.db")
    store.sessions.create("conversation-1", "tui")

    saved = store.runs.append_event(
        "conversation-1",
        {
            "type": "session.info",
            "execution_session_id": "execution-1",
            "runtime_scope_key": "profile:agent-default",
            "seq": 7,
            "payload": {
                "status": "ready",
                "model": "gpt-test",
                "provider": "test-provider",
            },
        },
    )
    state = store._conn.execute(
        "SELECT * FROM session_runtime_state WHERE session_id = ?",
        ("conversation-1",),
    ).fetchone()

    assert saved["seq"] == 1
    assert state["execution_session_id"] == "execution-1"
    assert state["runtime_scope_key"] == "profile:agent-default"
    assert state["status"] == "ready"
    assert state["source_seq"] == 1


def test_run_event_read_model_pages_the_immediately_preceding_canonical_window(tmp_path):
    store = open_cli_session_store(tmp_path / "state.db")
    store.sessions.create("conversation-1", "tui")
    store.runs.upsert(
        run_id="run-1",
        session_id="conversation-1",
        execution_session_id="execution-1",
    )
    for source_seq in range(1, 7):
        store.runs.append_event(
            "conversation-1",
            {
                "type": "message.delta",
                "execution_session_id": "execution-1",
                "run_id": "run-1",
                "seq": source_seq,
                "payload": {"delta": str(source_seq)},
            },
        )

    page = store.runs.list_events(
        "conversation-1",
        before_seq=5,
        limit=2,
    )

    assert [event["seq"] for event in page] == [3, 4]


def test_run_event_read_model_overwrites_stale_frame_identity_from_ledger_row(tmp_path):
    store = open_cli_session_store(tmp_path / "state.db")
    store.sessions.create("conversation-1", "tui")
    saved = store.runs.append_event(
        "conversation-1",
        {
            "type": "message.delta",
            "execution_session_id": "execution-1",
            "run_id": "run-1",
            "payload": {"delta": "hello"},
        },
    )
    stale_frame = {
        **saved,
        "session_id": "legacy-runtime-id",
        "conversation_session_id": "legacy-conversation-id",
        "execution_session_id": "legacy-execution-id",
        "payload": {
            **saved["payload"],
            "session_id": "legacy-runtime-id",
            "conversation_session_id": "legacy-conversation-id",
            "execution_session_id": "legacy-execution-id",
        },
    }
    store._conn.execute(
        "UPDATE run_events SET frame_blob = ? WHERE session_id = ?",
        (
            zlib.compress(json.dumps(stale_frame).encode("utf-8")),
            "conversation-1",
        ),
    )
    store._conn.commit()

    [event] = store.runs.list_events("conversation-1")

    assert event["session_id"] == "conversation-1"
    assert event["conversation_session_id"] == "conversation-1"
    assert event["execution_session_id"] == "execution-1"
    assert event["payload"]["session_id"] == "conversation-1"
    assert event["payload"]["conversation_session_id"] == "conversation-1"
    assert event["payload"]["execution_session_id"] == "execution-1"


def test_repository_bootstrap_reclassifies_legacy_delegate_session_from_persisted_evidence(tmp_path):
    db_path = tmp_path / "state.db"
    goal = "Inspect the repository and report architecture violations."
    store = open_cli_session_store(db_path)
    store.sessions.create("parent", "tui")
    store.messages.append(
        "parent",
        "assistant",
        "Delegating the audit.",
        tool_calls=[
            {
                "id": "call-delegate",
                "type": "function",
                "function": {
                    "name": "delegate_task",
                    "arguments": json.dumps({"goal": goal}),
                },
            }
        ],
    )
    store.sessions.create("child", "tui", parent_session_id="parent")
    store.messages.append("child", "user", goal)
    store.close()

    migrated = open_cli_session_store(db_path)
    child = migrated.sessions.get("child")
    indexed = migrated._conn.execute(
        "SELECT 1 FROM session_index WHERE session_id = 'child'"
    ).fetchone()

    assert child["session_kind"] == "execution"
    assert child["conversation_kind"] == "internal"
    assert indexed is None


def test_repository_bootstrap_does_not_reclassify_user_branch(tmp_path):
    db_path = tmp_path / "state.db"
    goal = "Same text can appear in an explicit user branch."
    store = open_cli_session_store(db_path)
    store.sessions.create("parent", "tui")
    store.messages.append(
        "parent",
        "assistant",
        "Delegating.",
        tool_calls=[{
            "function": {
                "name": "delegate_task",
                "arguments": json.dumps({"goal": goal}),
            }
        }],
    )
    store.branches.branch_session(
        source_session_id="parent",
        new_session_id="branch",
        scope="full_conversation",
        branch_origin="user_message_action",
    )
    store.messages.append("branch", "user", goal)
    store.close()

    reopened = open_cli_session_store(db_path)
    branch = reopened.sessions.get("branch")
    indexed = reopened._conn.execute(
        "SELECT 1 FROM session_index WHERE session_id = 'branch'"
    ).fetchone()

    assert branch["session_kind"] == "hermes_session"
    assert branch["conversation_kind"] == "direct"
    assert indexed is not None
