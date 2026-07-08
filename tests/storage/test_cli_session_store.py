import json

from hermes_agent.storage.cli_session_store import open_cli_session_store


def test_cli_session_store_persists_and_replays_messages(tmp_path):
    store = open_cli_session_store(tmp_path / "state.db")

    store.create_session("s1", "cli", model="gpt-test")
    row_id = store.append_message(
        "s1",
        "assistant",
        "hello",
        tool_calls=[{"function": {"name": "search"}}],
        metadata={"run_id": "run-1"},
    )

    assert row_id > 0
    rows = store.get_messages("s1")
    assert [row["role"] for row in rows] == ["assistant"]
    assert rows[0]["tool_calls"] == [{"function": {"name": "search"}}]
    assert rows[0]["metadata"] == {"run_id": "run-1"}

    replay = store.get_messages_as_conversation("s1", include_storage_metadata=True)
    assert replay[0]["role"] == "assistant"
    assert replay[0]["content"] == "hello"
    assert replay[0]["message_id"] == str(row_id)


def test_cli_session_store_title_resume_and_handoff(tmp_path):
    store = open_cli_session_store(tmp_path / "state.db")

    store.create_session("root", "cli", title="Planning")
    store.end_session("root", "compression")
    store.create_session("child", "cli", parent_session_id="root")
    store.append_message("child", "user", "continued")

    assert store.resolve_resume_session_id("root") == "child"
    assert store.set_session_title("child", "Planning #2")
    assert store.get_session_by_title("Planning #2")["id"] == "child"
    assert store.get_next_title_in_lineage("Planning") == "Planning #3"

    assert store.request_handoff("child", "telegram") is True
    assert store.get_handoff_state("child") == {
        "state": "pending",
        "platform": "telegram",
        "error": None,
    }
    store.fail_handoff("child", "timeout")
    assert store.get_handoff_state("child")["state"] == "failed"


def test_cli_session_store_resolves_titles_to_latest_numbered_session(tmp_path):
    store = open_cli_session_store(tmp_path / "state.db")

    store.create_session("root", "cli", title="Planning")
    store.create_session("child-2", "cli", title="Planning #2")
    store.create_session("child-3", "cli", title="Planning #3")
    with store._lock:
        store._conn.execute("UPDATE sessions SET started_at = ? WHERE id = ?", (100.0, "child-2"))
        store._conn.execute("UPDATE sessions SET started_at = ? WHERE id = ?", (200.0, "child-3"))
        store._conn.commit()

    assert store.resolve_session_by_title("Planning") == "child-3"
    assert store.resolve_session_by_title("Missing") is None


def test_cli_session_store_updates_usage_and_system_prompt(tmp_path):
    store = open_cli_session_store(tmp_path / "state.db")

    store.create_session("s1", "cli", cwd="/workspace/initial")
    store.update_system_prompt("s1", "system")
    store.update_session_cwd("s1", "/workspace/current")
    store.update_token_counts(
        "s1",
        input_tokens=10,
        output_tokens=5,
        billing_provider="openai",
        pricing_version="v1",
    )

    row = store.get_session("s1")
    assert row["cwd"] == "/workspace/current"
    assert row["system_prompt"] == "system"
    assert row["input_tokens"] == 10
    assert row["output_tokens"] == 5
    assert row["billing_provider"] == "openai"
    assert row["pricing_version"] == "v1"


def test_cli_session_store_meta_and_delete(tmp_path):
    store = open_cli_session_store(tmp_path / "state.db")

    store.set_meta("k", "v")
    assert store.get_meta("k") == "v"
    store.create_session("s1", "cli")
    store.append_message("s1", "user", {"kind": "json"})
    assert json.loads(store.get_session("s1")["model_config"] or "{}") == {}
    assert store.delete_session("s1") is True
    assert store.get_session("s1") is None


def test_cli_session_store_exports_counts_and_prunes_cli_sessions(tmp_path):
    db_path = tmp_path / "state.db"
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    store = open_cli_session_store(db_path)

    store.create_session("old", "cli")
    store.create_session("new", "cli")
    store.append_message("old", "user", "stale")
    store.append_message("new", "user", "keep")
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
    assert store.message_count() == 2
    assert store.message_count("old") == 1
    assert store.export_session("old")["messages"][0]["content"] == "stale"
    assert {session["id"] for session in store.export_all(source="cli")} == {"old", "new"}

    assert store.prune_sessions(older_than_days=1, source="cli", sessions_dir=sessions_dir) == 1
    assert store.get_session("old") is None
    assert store.get_session("new") is not None
    assert not (sessions_dir / "old.jsonl").exists()


def test_cli_session_store_replaces_and_searches_messages(tmp_path):
    store = open_cli_session_store(tmp_path / "state.db")

    store.create_session("acp-1", "acp", model_config={"cwd": "/work"})
    store.replace_messages(
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

    replay = store.get_messages_as_conversation("acp-1")
    assert [message["role"] for message in replay] == ["user", "assistant"]
    assert replay[1]["reasoning"] == "needs file lookup"
    assert replay[1]["tool_calls"] == [{"function": {"name": "read_file"}}]
    assert store.search_sessions(source="acp")[0]["id"] == "acp-1"
    assert store.search_messages("nginx", source_filter=["acp"])[0]["session_id"] == "acp-1"

    store.replace_messages("acp-1", [{"role": "user", "content": "replacement only"}])
    assert [message["content"] for message in store.get_messages_as_conversation("acp-1")] == [
        "replacement only"
    ]


def test_cli_session_store_exposes_agent_profile_registry(tmp_path):
    store = open_cli_session_store(tmp_path / "state.db")

    profile = store.upsert_agent_profile(
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
    draft = store.upsert_agent_profile_draft(
        draft_id="draft-1",
        draft_kind="revision",
        base_agent_profile_id=profile["id"],
        target_agent_profile_id=profile["id"],
        source_session_id="session-1",
        name="Research Agent draft",
        recommended_toolsets=["file"],
    )

    assert profile["runtimeScopeKey"] == "profile:agent-1"
    assert store.get_agent_profile_by_slug("research-agent")["id"] == "agent-1"
    assert store.list_agent_profiles()[0]["agentProfileVersionId"] == "snapshot-1"
    assert store.get_agent_profile_draft(draft["id"])["draftKind"] == "revision"
    assert store.list_agent_profile_drafts(source_session_id="session-1")[0]["id"] == "draft-1"
    assert store.discard_agent_profile_draft("draft-1")["status"] == "discarded"
