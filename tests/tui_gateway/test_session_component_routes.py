from __future__ import annotations

import importlib
import time

from hermes_agent.composition.cli_session_store import open_cli_session_store
from tui_gateway import server

session_methods = importlib.import_module("tui_gateway.methods.session")
live_session_methods = importlib.import_module("tui_gateway.methods.live_session")
insights_methods = importlib.import_module("tui_gateway.methods.insights_rollback")


def _install_db(monkeypatch, db) -> None:
    monkeypatch.setattr(session_methods, "_get_db", lambda: db)
    monkeypatch.setattr(server, "_get_db", lambda: db)


def test_session_list_route_reads_through_session_component(tmp_path, monkeypatch):
    db = open_cli_session_store(tmp_path / "state.db")
    try:
        db.sessions.create("user-session", "tui")
        db.messages.append("user-session", "user", "hello")
        db.sessions.create("tool-session", "tool")
        _install_db(monkeypatch, db)

        response = server.handle_request(
            {
                "id": "list",
                "method": "session.list",
                "params": {"limit": 10},
            }
        )
    finally:
        db.close()

    assert "error" not in response, response
    assert [row["id"] for row in response["result"]["sessions"]] == [
        "user-session"
    ]


def test_session_index_route_reads_through_index_component(tmp_path, monkeypatch):
    db = open_cli_session_store(tmp_path / "state.db")
    try:
        db.sessions.create("indexed-session", "tui")
        db.messages.append("indexed-session", "user", "hello")
        _install_db(monkeypatch, db)
        response = server.handle_request(
            {
                "id": "index",
                "method": "session.index.list",
                "params": {"limit": 10},
            }
        )
    finally:
        db.close()

    assert "error" not in response, response
    assert [row["id"] for row in response["result"]["sessions"]] == [
        "indexed-session"
    ]


def test_session_create_route_writes_through_session_component(
    tmp_path,
    monkeypatch,
):
    db = open_cli_session_store(tmp_path / "state.db")
    try:
        monkeypatch.setattr(
            session_methods,
            "_db_for_session_request",
            lambda _params, _session_id="": db,
        )
        monkeypatch.setattr(
            session_methods,
            "_bind_session_workspace",
            lambda **_options: {},
        )
        monkeypatch.setattr(session_methods, "_new_session_key", lambda: "created-session")
        monkeypatch.setattr(session_methods, "_resolve_model", lambda: "test-model")

        response = server.handle_request(
            {
                "id": "create",
                "method": "session.create",
                "params": {"control_plane_only": True},
            }
        )
        stored = db.sessions.get("created-session")
    finally:
        db.close()

    assert "error" not in response, response
    assert response["result"]["session_id"] == "created-session"
    assert stored is not None
    assert stored["source"] == "tui"
    assert stored["model"] == "test-model"


def test_session_delete_route_uses_session_lifecycle_component(
    tmp_path,
    monkeypatch,
):
    db = open_cli_session_store(tmp_path / "state.db")
    try:
        db.sessions.create("delete-session", "tui")
        db.messages.append("delete-session", "user", "delete me")
        _install_db(monkeypatch, db)
        monkeypatch.setattr(session_methods, "get_hermes_home", lambda: tmp_path)

        response = server.handle_request(
            {
                "id": "delete",
                "method": "session.delete",
                "params": {"session_id": "delete-session"},
            }
        )
        stored = db.sessions.get("delete-session")
    finally:
        db.close()

    assert "error" not in response, response
    assert response["result"]["deleted"] == "delete-session"
    assert stored is None


def test_session_title_route_resolves_and_updates_through_session_component(
    tmp_path,
    monkeypatch,
):
    db = open_cli_session_store(tmp_path / "state.db")
    try:
        db.sessions.create("title-session", "tui", title="Before")
        _install_db(monkeypatch, db)

        response = server.handle_request(
            {
                "id": "title",
                "method": "session.title",
                "params": {
                    "session_id": "Before",
                    "title": "After",
                },
            }
        )
        stored = db.sessions.get("title-session")
    finally:
        db.close()

    assert "error" not in response, response
    assert response["result"] == {"pending": False, "title": "After"}
    assert stored is not None
    assert stored["title"] == "After"


def test_live_session_title_reads_through_session_component(monkeypatch):
    class SessionQueries:
        def get_title(self, session_id: str) -> str:
            assert session_id == "stored-session"
            return "Persisted title"

    state = type("StateRoot", (), {"sessions": SessionQueries()})()
    monkeypatch.setattr(live_session_methods, "_get_db", lambda: state)

    title = live_session_methods._session_live_title(
        {"pending_title": "Pending title"},
        "stored-session",
    )

    assert title == "Persisted title"


def test_insights_route_reads_through_session_component(monkeypatch):
    now = time.time()

    class SessionQueries:
        def list_rich(self, *, limit: int):
            assert limit == 500
            return [
                {"started_at": now, "message_count": 3},
                {"started_at": now - 60 * 86400, "message_count": 20},
            ]

    state = type("StateRoot", (), {"sessions": SessionQueries()})()
    monkeypatch.setattr(insights_methods, "_get_db", lambda: state)

    response = server.handle_request(
        {
            "id": "insights",
            "method": "insights.get",
            "params": {"days": 30},
        }
    )

    assert "error" not in response, response
    assert response["result"] == {"days": 30, "sessions": 1, "messages": 3}
