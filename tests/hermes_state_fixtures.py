"""Shared fixtures for CliSessionStore tests."""

import pytest

from hermes_agent.storage.cli_session_store import open_cli_session_store


@pytest.fixture()
def db(tmp_path):
    """Create a CliSessionStore with a temp database file."""
    db_path = tmp_path / "test_state.db"
    session_db = open_cli_session_store(db_path=db_path)
    yield session_db
    session_db.close()
