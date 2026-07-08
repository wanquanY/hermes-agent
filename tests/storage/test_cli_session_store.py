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


def test_cli_session_store_updates_usage_and_system_prompt(tmp_path):
    store = open_cli_session_store(tmp_path / "state.db")

    store.create_session("s1", "cli")
    store.update_system_prompt("s1", "system")
    store.update_token_counts(
        "s1",
        input_tokens=10,
        output_tokens=5,
        billing_provider="openai",
        pricing_version="v1",
    )

    row = store.get_session("s1")
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
