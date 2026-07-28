from __future__ import annotations

from run_agent import AIAgent
from hermes_agent.composition.cli_session_store import open_cli_session_store


def test_agent_persists_internal_execution_classification(tmp_path) -> None:
    db = open_cli_session_store(tmp_path / "state.db")
    agent = AIAgent.__new__(AIAgent)
    agent._session_persistence_disabled = False
    agent._session_db = db
    agent._session_db_created = False
    agent._session_init_model_config = None
    agent._cached_system_prompt = ""
    agent._parent_session_id = None
    agent._session_kind = "execution"
    agent._conversation_kind = "internal"
    agent.session_id = "subagent-session-1"
    agent.platform = "tool"
    agent.model = "test-model"

    agent._ensure_db_session()

    row = db.sessions.get("subagent-session-1")
    assert row is not None
    assert row["session_kind"] == "execution"
    assert row["conversation_kind"] == "internal"
    assert db.sessions.list_rich(include_children=True) == []
    assert [
        item["id"]
        for item in db.sessions.list_rich(
            include_children=True,
            include_internal=True,
        )
    ] == ["subagent-session-1"]
