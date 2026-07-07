from __future__ import annotations

import sqlite3
from pathlib import Path


def _write_session_db(db_path: Path, *, session_id: str = "session-1") -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY)")
        conn.execute("CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY)")
        conn.execute("CREATE TABLE IF NOT EXISTS messages (id TEXT PRIMARY KEY)")
        conn.execute("CREATE TABLE IF NOT EXISTS run_events (id TEXT PRIMARY KEY)")
        if session_id:
            conn.execute("INSERT OR IGNORE INTO sessions (id) VALUES (?)", (session_id,))
        conn.commit()
    finally:
        conn.close()


def _write_profile_state_db(db_path: Path, sessions: list[dict]) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            """
            CREATE TABLE sessions (
                id TEXT PRIMARY KEY,
                source TEXT NOT NULL,
                started_at REAL NOT NULL,
                title TEXT,
                message_count INTEGER DEFAULT 0
            )
            """
        )
        conn.execute("CREATE UNIQUE INDEX idx_sessions_title_unique ON sessions(title) WHERE title IS NOT NULL")
        conn.execute(
            """
            CREATE TABLE messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT,
                timestamp REAL NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE runs (
                run_id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                status TEXT NOT NULL,
                started_at REAL NOT NULL,
                updated_at REAL NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE run_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                run_id TEXT,
                event_type TEXT NOT NULL,
                seq INTEGER NOT NULL,
                timestamp REAL NOT NULL,
                event_json TEXT NOT NULL,
                UNIQUE(session_id, seq)
            )
            """
        )
        for session in sessions:
            messages = session.get("messages") or []
            started_at = float(session["started_at"])
            conn.execute(
                "INSERT INTO sessions (id, source, started_at, title, message_count) VALUES (?, ?, ?, ?, ?)",
                (session["id"], "tui", started_at, session.get("title"), len(messages)),
            )
            for index, message in enumerate(messages):
                conn.execute(
                    "INSERT INTO messages (session_id, role, content, timestamp) VALUES (?, ?, ?, ?)",
                    (session["id"], message.get("role", "user"), message.get("content", ""), started_at + index),
                )
            run_id = session.get("run_id")
            if run_id:
                conn.execute(
                    "INSERT INTO runs (run_id, session_id, status, started_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                    (run_id, session["id"], "completed", started_at, started_at + 1),
                )
                conn.execute(
                    "INSERT INTO run_events (session_id, run_id, event_type, seq, timestamp, event_json) VALUES (?, ?, ?, ?, ?, ?)",
                    (session["id"], run_id, "completed", 1, started_at + 1, "{}"),
                )
        conn.commit()
    finally:
        conn.close()


def test_runtime_state_inspect_reports_hermes_owned_database_state(tmp_path: Path) -> None:
    from tui_gateway.services.runtime_state import inspect_runtime_state

    _write_session_db(tmp_path / "profiles" / "default" / "state.db")
    profile_home = tmp_path / "profiles" / "agent-a"
    _write_session_db(profile_home / "state.db", session_id="profile-session")

    result = inspect_runtime_state(hermes_home_root=tmp_path)

    assert result["hermesHomeRoot"] == str(tmp_path.resolve())
    assert result["summary"]["runtimeHomeCount"] == 2
    default_home = next(home for home in result["runtimeHomes"] if home["scopeKind"] == "default")
    default_state = next(db for db in default_home["databases"] if db["kind"] == "state")
    assert default_state["counts"]["sessions"] == 1
    assert default_state["totalRows"] == 1
    assert default_state["empty"] is False


def test_runtime_state_prune_empty_deletes_only_empty_hermes_databases(tmp_path: Path) -> None:
    from tui_gateway.services.runtime_state import prune_empty_runtime_state

    empty_db = tmp_path / "profiles" / "agent-empty" / "state.db"
    _write_session_db(empty_db, session_id="")
    non_empty_db = tmp_path / "profiles" / "agent-keep" / "state.db"
    _write_session_db(non_empty_db, session_id="profile-session")

    dry = prune_empty_runtime_state(hermes_home_root=tmp_path, dry_run=True)
    assert dry["summary"]["prunedCount"] == 1
    assert empty_db.exists()

    result = prune_empty_runtime_state(hermes_home_root=tmp_path, dry_run=False)
    assert result["summary"]["prunedCount"] == 1
    assert not empty_db.exists()
    assert non_empty_db.exists()


def test_profile_runtime_session_exists_is_scoped_to_hermes_runtime_roots(tmp_path: Path) -> None:
    from tui_gateway.services.runtime_state import profile_runtime_session_exists

    profile_home = tmp_path / "profiles" / "agent-a"
    _write_session_db(profile_home / "state.db", session_id="profile-session")

    assert profile_runtime_session_exists(
        session_id="profile-session",
        hermes_home_path=profile_home,
        hermes_home_root=tmp_path,
    )["exists"] is True
    assert profile_runtime_session_exists(
        session_id="missing",
        hermes_home_path=profile_home,
        hermes_home_root=tmp_path,
    )["exists"] is False

    outside = tmp_path.parent / "outside-profile"
    outside.mkdir(exist_ok=True)
    try:
        profile_runtime_session_exists(
            session_id="profile-session",
            hermes_home_path=outside,
            hermes_home_root=tmp_path,
        )
    except PermissionError as exc:
        assert "inside Hermes runtime roots" in str(exc)
    else:
        raise AssertionError("outside profile runtime homes must be rejected")


def test_team_mission_workspace_rebase_paths_updates_hermes_state(tmp_path: Path) -> None:
    from hermes_state import SessionDB
    from tui_gateway.services.runtime_state import rebase_team_mission_workspace_paths

    db = SessionDB(tmp_path / "state.db")
    try:
        db.upsert_team_mission(
            mission_id="mission-1",
            title="Mission",
            objective="Build",
            mode="supervised_mission",
            workspace_path="/old/workspace",
        )
        db.upsert_team_mission_conversation(
            conversation_id="conversation-1",
            stable_session_id="team-session-1",
            title="Team",
            workspace_path="/old/workspace",
        )

        result = rebase_team_mission_workspace_paths(
            old_path="/old/workspace",
            new_path="/new/workspace",
            db=db,
        )

        assert result["changed"] >= 2
        assert db.get_team_mission_graph("mission-1")["mission"]["workspace_path"] == "/new/workspace"
        assert db.get_team_mission_conversation("conversation-1")["workspace_path"] == "/new/workspace"
    finally:
        db.close()


def test_merge_profile_runtime_state_imports_missing_sessions_without_desktop_sqlite(tmp_path: Path) -> None:
    from tui_gateway.services.runtime_state import merge_profile_runtime_state

    canonical_db = tmp_path / "state.db"
    profile_db = tmp_path / "profiles" / "agent-worker" / "state.db"
    _write_profile_state_db(
        canonical_db,
        [{
            "id": "target-session",
            "started_at": 1,
            "title": "same-title",
            "messages": [{"content": "canonical"}],
        }],
    )
    _write_profile_state_db(
        profile_db,
        [{
            "id": "profile-session",
            "started_at": 2,
            "title": "same-title",
            "messages": [{"content": "profile user"}, {"role": "assistant", "content": "profile answer"}],
            "run_id": "profile-run",
        }],
    )

    result = merge_profile_runtime_state(hermes_home_root=tmp_path, profiles_root=tmp_path / "profiles")

    assert [
        {
            "sessionsInserted": item["sessionsInserted"],
            "messagesInserted": item["messagesInserted"],
            "runsInserted": item["runsInserted"],
            "runEventsInserted": item["runEventsInserted"],
        }
        for item in result["migrated"]
    ] == [{
        "sessionsInserted": 1,
        "messagesInserted": 2,
        "runsInserted": 1,
        "runEventsInserted": 1,
    }]

    conn = sqlite3.connect(str(canonical_db))
    try:
        assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 2
        assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 3
        title, message_count = conn.execute(
            "SELECT title, message_count FROM sessions WHERE id = ?",
            ("profile-session",),
        ).fetchone()
        assert title is None
        assert message_count == 2
        assert conn.execute("SELECT COUNT(*) FROM runs WHERE session_id = ?", ("profile-session",)).fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM run_events WHERE session_id = ?", ("profile-session",)).fetchone()[0] == 1
    finally:
        conn.close()


def test_runtime_state_gateway_methods_are_registered(monkeypatch, tmp_path: Path) -> None:
    from tui_gateway import server
    from tui_gateway.services import runtime_state

    _write_session_db(tmp_path / "state.db")
    monkeypatch.setattr(runtime_state, "get_hermes_home", lambda: tmp_path)

    inspect_response = server._methods["runtime.state.inspect"](1, {})
    assert "error" not in inspect_response
    assert inspect_response["result"]["summary"]["runtimeHomeCount"] == 1

    session_response = server._methods["profile.runtime.session_exists"](
        2,
        {"session_id": "session-1", "hermes_home_path": str(tmp_path)},
    )
    assert session_response["result"]["exists"] is True
