from __future__ import annotations

from hermes_agent.composition.cli_session_store import open_cli_session_store


def test_session_service_lists_user_sessions_without_internal_sources(tmp_path):
    db = open_cli_session_store(tmp_path / "state.db")
    try:
        db.sessions.create("user-session", "tui", title="User session")
        db.sessions.create("tool-session", "tool", title="Internal tool")

        sessions = db.sessions.list(
            exclude_sources=("tool", "cron"),
            order_by_last_active=True,
        )
    finally:
        db.close()

    assert [session["id"] for session in sessions] == ["user-session"]


def test_session_service_list_preserves_keyset_cursor(tmp_path):
    db = open_cli_session_store(tmp_path / "state.db")
    try:
        db.sessions.create("session-1", "tui")
        db.sessions.create("session-2", "tui")
        first_page = db.sessions.list(limit=1, order_by_last_active=True)
        second_page = db.sessions.list(
            limit=1,
            order_by_last_active=True,
            page_cursor=first_page[0]["_page_cursor"],
        )
    finally:
        db.close()

    assert len(first_page) == 1
    assert len(second_page) == 1
    assert first_page[0]["id"] != second_page[0]["id"]


def test_session_service_resolves_id_and_title_to_one_shape(tmp_path):
    db = open_cli_session_store(tmp_path / "state.db")
    try:
        db.sessions.create("session-1", "tui", title="Named session")
        by_id = db.sessions.resolve_reference("session-1")
        by_title = db.sessions.resolve_reference("Named session")
    finally:
        db.close()

    assert by_id[0] == "session-1"
    assert by_title[0] == "session-1"
    assert by_id[1] == by_title[1]
