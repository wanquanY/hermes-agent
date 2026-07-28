from __future__ import annotations

from pathlib import Path

from hermes_agent.composition.cli_session_store import open_cli_session_store
from hermes_team_mission.domain.runtime_identity import runtime_event_identity
from hermes_team_mission.read_models.row_mapper import TeamMissionRowMapper


def test_runtime_event_identity_is_a_domain_function() -> None:
    identity = runtime_event_identity(
        mission={
            "mission_id": "mission-1",
            "conversation_id": "conversation-1",
            "leader_session_id": "conversation-session-1",
            "metadata": {"task_id": "task-1"},
        },
        node={
            "node_id": "worker-1",
            "kind": "worker",
            "output_contract": {"format": "markdown"},
        },
        binding={
            "mission_id": "mission-1",
            "node_id": "worker-1",
            "session_id": "runtime-conversation-1",
            "execution_session_id": "execution-1",
            "runtime_scope_key": "scope-1",
            "metadata": {
                "run_context_json": '{"participant_id":"participant-1"}'
            },
        },
    )

    assert identity["conversation_session_id"] == "conversation-session-1"
    assert identity["runtime_conversation_session_id"] == "runtime-conversation-1"
    assert identity["execution_session_id"] == "execution-1"
    assert identity["canonical_node_id"] == "mission-1:worker-1"
    assert identity["participant_id"] == "participant-1"
    assert identity["task_id"] == "task-1"


def test_cli_store_composes_row_mapper_and_graph_reads_use_it(tmp_path: Path) -> None:
    store = open_cli_session_store(tmp_path / "state.db")
    try:
        assert isinstance(store.team_mission_rows, TeamMissionRowMapper)
        assert "TeamMissionRowsMixin" not in {
            base.__name__ for base in type(store).__mro__
        }

        store.upsert_team_mission(
            mission_id="mission-1",
            title="Mission",
            objective="Build",
            mode="supervised_mission",
            status="running",
        )
        store.upsert_team_mission_node(
            mission_id="mission-1",
            node_id="worker-1",
            kind="worker",
            title="Worker",
            status="ready",
        )

        graph = store.team_mission_graphs.get_team_mission_graph("mission-1")

        assert graph["mission"]["mission_id"] == "mission-1"
        assert [node["node_id"] for node in graph["nodes"]] == ["worker-1"]

        store.runs.upsert(
            run_id="run-1",
            session_id="runtime-conversation-1",
            status="running",
        )
        store.bind_team_mission_run(
            mission_id="mission-1",
            node_id="worker-1",
            run_id="run-1",
            session_id="runtime-conversation-1",
            execution_session_id="execution-1",
            runtime_scope_key="scope-1",
            role="worker",
        )
        stored_event = store.append_team_mission_event_for_run(
            run_id="run-1",
            event={
                "type": "message.delta",
                "run_id": "run-1",
                "payload": {"delta": "working"},
            },
        )

        assert stored_event["type"] == "team_mission.runtime.event"
    finally:
        store.close()


def test_rows_mixin_module_is_deleted() -> None:
    assert not Path("hermes_team_mission/state/session_rows.py").exists()
    event_log_source = Path("hermes_team_mission/state/event_log.py").read_text()
    assert "_team_mission_runtime_event_identity" not in event_log_source
    assert "runtime-event-drop-no-identity-builder" not in event_log_source
