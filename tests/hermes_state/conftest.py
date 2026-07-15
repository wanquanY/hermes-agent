from __future__ import annotations

import pytest

from hermes_agent.storage.cli_session_store import open_cli_session_store


@pytest.fixture()
def db(tmp_path):
    store = open_cli_session_store(tmp_path / "test_state.db")
    yield store
    store.close()
