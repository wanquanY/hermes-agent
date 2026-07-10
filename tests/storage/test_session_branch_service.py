import json

import pytest

from hermes_agent.storage.cli_session_store import open_cli_session_store


@pytest.fixture()
def store(tmp_path):
    session_store = open_cli_session_store(tmp_path / "state.db")
    try:
        yield session_store
    finally:
        session_store.close()


def test_empty_branch_requires_explicit_policy(store):
    store.sessions.create("source", "acp")

    with pytest.raises(ValueError, match="source transcript is empty"):
        store.branches.branch_session(
            source_session_id="source",
            new_session_id="branch",
            scope="full_conversation",
        )


def test_empty_branch_persists_lineage_and_target_runtime_config(store):
    store.sessions.create(
        "source",
        "acp",
        model="source-model",
        model_config={"cwd": "/source"},
    )

    result = store.branches.branch_session(
        source_session_id="source",
        new_session_id="branch",
        scope="full_conversation",
        branch_origin="acp_fork",
        allow_empty=True,
        target_model="target-model",
        target_model_config={"cwd": "/target", "provider": "test"},
    )

    branch = store.sessions.get("branch")
    lineage = store.branches.get_session_branch_info("branch")
    assert result["message_count"] == 0
    assert result["branch_point"]["included_message_row_id"] == 0
    assert branch is not None
    assert branch["model"] == "target-model"
    assert json.loads(branch["model_config"]) == {
        "cwd": "/target",
        "provider": "test",
    }
    assert lineage is not None
    assert lineage["parent_session_id"] == "source"
    assert lineage["branch_origin"] == "acp_fork"
    assert store.sessions.get("source")["ended_at"] is None
