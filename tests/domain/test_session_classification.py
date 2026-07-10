from __future__ import annotations

from hermes_agent.domain.message_service import MessageService
from hermes_agent.domain.session_service import SessionService
from hermes_agent.read_models.session_recall import SessionRecallReadModel
from hermes_agent.repositories.session_repo import SessionRepoImpl, SessionSpec
from hermes_agent.storage.session_repository_db import connect_session_repository_db
from hermes_agent.storage.sqlite_connection_lock import lock_for_connection
from hermes_agent.storage.unit_of_work import SqliteUnitOfWork


def _session_service(tmp_path):
    conn = connect_session_repository_db(tmp_path / "state.db")
    repo = SessionRepoImpl(conn)
    unit_of_work = SqliteUnitOfWork(conn, lock_for_connection(conn))
    messages = MessageService(conn, repo)
    return conn, repo, SessionService(
        conn,
        repo,
        SessionRecallReadModel(conn),
        messages,
        unit_of_work,
    )


def test_team_mission_conversation_classification_is_written_to_both_tables(tmp_path):
    conn, repo, sessions = _session_service(tmp_path)
    try:
        sessions.create("conversation-1", source="team_mission", transient=False)

        session = repo.get("conversation-1")
        index = conn.execute(
            "SELECT session_kind, conversation_kind FROM session_index WHERE session_id = ?",
            ("conversation-1",),
        ).fetchone()

        assert session is not None
        assert session.session_kind == "team_mission"
        assert session.conversation_kind == "team"
        assert tuple(index) == ("team_mission", "team")
    finally:
        conn.close()


def test_transient_team_mission_runtime_session_is_internal_and_not_surfaced(tmp_path):
    conn, repo, sessions = _session_service(tmp_path)
    try:
        sessions.create("execution-1", source="team_mission", transient=True)

        session = repo.get("execution-1")
        index = conn.execute(
            "SELECT 1 FROM session_index WHERE session_id = ?",
            ("execution-1",),
        ).fetchone()

        assert session is not None
        assert session.session_kind == "execution"
        assert session.conversation_kind == "internal"
        assert index is None
    finally:
        conn.close()


def test_team_classification_upgrades_an_existing_direct_placeholder(tmp_path):
    conn, repo, sessions = _session_service(tmp_path)
    try:
        repo.create(
            SessionSpec(
                session_id="conversation-1",
                source="unknown",
                session_kind="hermes_session",
                conversation_kind="direct",
            )
        )

        sessions.create("conversation-1", source="team_mission", transient=False)

        session = repo.get("conversation-1")
        index = conn.execute(
            "SELECT source, session_kind, conversation_kind "
            "FROM session_index WHERE session_id = ?",
            ("conversation-1",),
        ).fetchone()
        assert session is not None
        assert (session.source, session.session_kind, session.conversation_kind) == (
            "team_mission",
            "team_mission",
            "team",
        )
        assert tuple(index) == ("team_mission", "team_mission", "team")
    finally:
        conn.close()


def test_explicit_execution_classification_overrides_source_defaults(tmp_path):
    conn, repo, sessions = _session_service(tmp_path)
    try:
        sessions.create(
            "execution-1",
            source="team_mission",
            transient=False,
            session_kind="execution",
            conversation_kind="internal",
        )

        session = repo.get("execution-1")
        assert session is not None
        assert session.session_kind == "execution"
        assert session.conversation_kind == "internal"
    finally:
        conn.close()


def test_bootstrap_reconciles_legacy_team_conversation_from_owner_record(tmp_path):
    db_path = tmp_path / "state.db"
    conn = connect_session_repository_db(db_path)
    repo = SessionRepoImpl(conn)
    repo.create(
        SessionSpec(
            session_id="conversation-1",
            source="team_mission",
            session_kind="hermes_session",
            conversation_kind="direct",
        )
    )
    conn.execute(
        """
        INSERT INTO team_mission_conversations (
            conversation_id, conversation_session_id, title, status,
            created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        ("team-conversation-1", "conversation-1", "Team Mission", "active", 1.0, 1.0),
    )
    conn.close()

    conn = connect_session_repository_db(db_path)
    try:
        session = SessionRepoImpl(conn).get("conversation-1")
        index = conn.execute(
            "SELECT conversation_kind FROM session_index WHERE session_id = ?",
            ("conversation-1",),
        ).fetchone()
        assert session is not None
        assert session.session_kind == "team_mission"
        assert session.conversation_kind == "team"
        assert index["conversation_kind"] == "team"
    finally:
        conn.close()


def test_bootstrap_hides_legacy_unowned_transient_team_execution(tmp_path):
    db_path = tmp_path / "state.db"
    conn = connect_session_repository_db(db_path)
    SessionRepoImpl(conn).create(
        SessionSpec(
            session_id="execution-1",
            source="team_mission",
            transient=True,
            session_kind="hermes_session",
            conversation_kind="direct",
        )
    )
    conn.close()

    conn = connect_session_repository_db(db_path)
    try:
        session = SessionRepoImpl(conn).get("execution-1")
        index = conn.execute(
            "SELECT 1 FROM session_index WHERE session_id = ?",
            ("execution-1",),
        ).fetchone()
        assert session is not None
        assert session.session_kind == "execution"
        assert session.conversation_kind == "internal"
        assert index is None
    finally:
        conn.close()
