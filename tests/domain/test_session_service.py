from __future__ import annotations

import time

from hermes_agent.storage.cli_session_store import open_cli_session_store


def test_update_source_projects_session_and_index(tmp_path):
    db = open_cli_session_store(tmp_path / "state.db")
    try:
        db.sessions.create("session-1", source="tui")

        changed = db.sessions.update_source("session-1", "team_mission")

        assert changed == 1
        assert db.sessions.get("session-1")["source"] == "team_mission"
        assert db.session_index.get("session-1")["source"] == "team_mission"
        assert db.session_index.get("session-1")["conversation_kind"] == "team"
    finally:
        db.close()


def test_scoped_system_prompt_isolated_from_conversation_prompt(tmp_path):
    db = open_cli_session_store(tmp_path / "state.db")
    try:
        db.sessions.create("session-1", source="team_mission")
        db.sessions.update_system_prompt("session-1", "conversation prompt")

        db.sessions.update_scoped_system_prompt(
            "session-1",
            "member-chat:session-1:frontend",
            "member prompt",
        )

        assert (
            db.sessions.get_scoped_system_prompt(
                "session-1",
                "member-chat:session-1:frontend",
            )
            == "member prompt"
        )
        assert db.sessions.get("session-1")["system_prompt"] == "conversation prompt"
    finally:
        db.close()


def test_token_usage_updates_are_incremental_and_backfill_model(tmp_path):
    db = open_cli_session_store(tmp_path / "state.db")
    try:
        db.sessions.create("session-1", source="cli")

        db.sessions.update_token_counts(
            "session-1",
            input_tokens=100,
            output_tokens=20,
            api_call_count=1,
            model="test-model",
        )
        db.sessions.update_token_counts(
            "session-1",
            input_tokens=50,
            output_tokens=10,
            api_call_count=1,
        )

        session = db.sessions.get("session-1")
        assert session["input_tokens"] == 150
        assert session["output_tokens"] == 30
        assert session["api_call_count"] == 2
        assert session["model"] == "test-model"
    finally:
        db.close()


def test_resolve_id_accepts_only_an_unambiguous_escaped_prefix(tmp_path):
    db = open_cli_session_store(tmp_path / "state.db")
    try:
        db.sessions.create("20260315_092437_c9a6ff", source="cli")
        db.sessions.create("20260315X092437_c9a6ff", source="cli")

        assert db.sessions.resolve_id("20260315_092437") == "20260315_092437_c9a6ff"
        assert db.sessions.resolve_id("20260315") is None
    finally:
        db.close()


def test_list_rich_cursor_and_compression_lineage_projection(tmp_path):
    db = open_cli_session_store(tmp_path / "state.db")
    try:
        started_at = time.time() - 100
        for index, session_id in enumerate(("session-1", "session-2", "session-3")):
            db.sessions.create(session_id, source="cli")
            db._conn.execute(  # noqa: SLF001
                "UPDATE sessions SET started_at = ? WHERE id = ?",
                (started_at + index, session_id),
            )
        db._conn.commit()  # noqa: SLF001

        first = db.sessions.list_rich(limit=2)
        second = db.sessions.list_rich(limit=2, page_cursor=first[-1]["_page_cursor"])

        assert [row["id"] for row in first] == ["session-3", "session-2"]
        assert [row["id"] for row in second] == ["session-1"]

        root = "20260602_235959_root99"
        tip = "20260603_010000_tip01"
        db.sessions.create(root, source="cli")
        db.sessions.end(root, "compression")
        db.sessions.create(tip, source="cli", parent_session_id=root)

        matches = db.sessions.search_by_id("root99")

        assert [row["id"] for row in matches] == [tip]
        assert matches[0]["_lineage_root_id"] == root
    finally:
        db.close()
