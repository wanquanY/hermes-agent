"""Verify load_transcript returns SQLite messages without any JSONL file."""
from hermes_gateway.session import SessionStore
from hermes_gateway.config import GatewayConfig
from hermes_agent.repositories.session_repo import SessionRepoImpl, SessionSpec
from hermes_agent.composition.session_repository_db import connect_session_repository_db


def test_load_transcript_returns_db_messages_when_no_jsonl(tmp_path):
    """Reading a transcript must work from SQLite alone — no JSONL fallback needed.

    The gateway must go through the repository/read-model owner, not the legacy
    state facade.
    """
    conn = connect_session_repository_db(tmp_path / "state.db")
    session_repo = SessionRepoImpl(conn)
    config = GatewayConfig()
    store = SessionStore(
        sessions_dir=tmp_path,
        config=config,
        session_repo=session_repo,
        storage_conn=conn,
    )

    sid = "test-session-db-only"
    session_repo.create(SessionSpec(session_id=sid, source="test"))
    store.append_to_transcript(sid, {"role": "user", "content": "hello", "timestamp": 1.0})
    store.append_to_transcript(sid, {"role": "assistant", "content": "world", "timestamp": 2.0})

    history = store.load_transcript(sid)
    assert len(history) == 2
    assert history[0]["content"] == "hello"
    assert history[1]["content"] == "world"
