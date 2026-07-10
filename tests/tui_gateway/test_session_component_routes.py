from __future__ import annotations

import importlib

from hermes_agent.storage.cli_session_store import open_cli_session_store
from tui_gateway import server

session_methods = importlib.import_module("tui_gateway.methods.session")


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
    previous_reconciled = session_methods._SESSION_INDEX_RECONCILED
    try:
        db.sessions.create("indexed-session", "tui")
        db.messages.append("indexed-session", "user", "hello")
        _install_db(monkeypatch, db)
        session_methods._SESSION_INDEX_RECONCILED = False

        response = server.handle_request(
            {
                "id": "index",
                "method": "session.index.list",
                "params": {"limit": 10},
            }
        )
    finally:
        session_methods._SESSION_INDEX_RECONCILED = previous_reconciled
        db.close()

    assert "error" not in response, response
    assert [row["id"] for row in response["result"]["sessions"]] == [
        "indexed-session"
    ]
