from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pytest

from hermes_agent.storage.cli_session_store import CliSessionStore, open_cli_session_store
from hermes_team_mission.gateway.runtime_methods import _resolve_cancel_mission_id
from tests.team_mission_gateway_test_support import team_mission_gateway
from tui_gateway import server


def _call(method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    return server.handle_request({
        "jsonrpc": "2.0",
        "id": f"test-{method}",
        "method": method,
        "params": params or {},
    })


def _assert_ok(response: dict[str, Any]) -> dict[str, Any]:
    assert "error" not in response
    return response["result"]


def _assert_error(response: dict[str, Any], code: int, message: str) -> None:
    assert response["error"] == {"code": code, "message": message}


@pytest.fixture()
def gateway_db(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> CliSessionStore:
    team_mission = team_mission_gateway()
    db = open_cli_session_store(tmp_path / "state.db")
    monkeypatch.setattr(team_mission, "_get_db", lambda: db)
    monkeypatch.setattr(server, "_db", db, raising=False)
    monkeypatch.setattr(server, "_db_error", None, raising=False)
    monkeypatch.setattr(server, "_db_by_home", {}, raising=False)
    monkeypatch.setattr(server, "_db_error_by_home", {}, raising=False)
    try:
        yield db
    finally:
        db.close()


def _seed_mission(
    db: CliSessionStore,
    *,
    mission_id: str = "mission-1",
    conversation_id: str = "conversation-1",
    conversation_session_id: str = "team-session-1",
) -> None:
    db.upsert_team_mission(
        mission_id=mission_id,
        conversation_id=conversation_id,
        team_id="team-1",
        title="Contract mission",
        objective="Validate cancel contract.",
        mode="supervised_mission",
        status="running",
        leader_session_id=conversation_session_id,
        metadata={"conversation_session_id": conversation_session_id},
    )
    db.upsert_team_mission_conversation(
        conversation_id=conversation_id,
        conversation_session_id=conversation_session_id,
        team_id="team-1",
        title="Contract conversation",
        active_mission_id=mission_id,
    )
    db.upsert_team_mission_node(
        mission_id=mission_id,
        node_id="node-root",
        kind="root",
        title="Root",
        objective="Coordinate.",
        status="running",
    )


def test_team_mission_cancel_with_unknown_mission_id_returns_4040_not_silent(
    gateway_db: CliSessionStore,
) -> None:
    response = _call("team_mission.cancel", {"mission_id": "missing-mission"})

    _assert_error(response, 4040, "team mission not found")


def test_team_mission_cancel_with_conversation_id_resolves_to_mission_id(
    gateway_db: CliSessionStore,
) -> None:
    _seed_mission(gateway_db, mission_id="mission-conv", conversation_id="conversation-conv")

    result = _assert_ok(
        _call("team_mission.cancel", {"conversation_id": "conversation-conv"})
    )

    assert result["mission_id"] == "mission-conv"
    assert result["mission_status"] == "cancelled"
    assert gateway_db.team_mission_graphs.get_team_mission_graph("mission-conv")["mission"]["status"] == "cancelled"


def test_team_mission_cancel_with_conversation_id_no_associated_mission_returns_4040(
    gateway_db: CliSessionStore,
) -> None:
    response = _call("team_mission.cancel", {"conversation_id": "conversation-empty"})

    _assert_error(response, 4040, "team mission not found")


def test_team_mission_cancel_with_conversation_session_id_resolves_to_mission_id(
    gateway_db: CliSessionStore,
) -> None:
    _seed_mission(
        gateway_db,
        mission_id="mission-session",
        conversation_id="conversation-session",
        conversation_session_id="team-session-contract",
    )

    result = _assert_ok(
        _call(
            "team_mission.cancel",
            {"conversation_session_id": "team-session-contract"},
        )
    )

    assert result["mission_id"] == "mission-session"
    assert result["mission_status"] == "cancelled"


def test_team_mission_cancel_with_only_mission_id_argument_works(
    gateway_db: CliSessionStore,
) -> None:
    _seed_mission(gateway_db, mission_id="mission-direct")

    result = _assert_ok(_call("team_mission.cancel", {"mission_id": "mission-direct"}))

    assert result["mission_id"] == "mission-direct"
    assert result["mission_status"] == "cancelled"


def test_team_mission_cancel_returns_4006_when_no_identifier_provided(
    gateway_db: CliSessionStore,
) -> None:
    response = _call("team_mission.cancel", {})

    _assert_error(response, 4006, "mission_id or conversation_id required")


def test_team_mission_cancel_logs_graph_empty_for_resolved_mission_id_when_race(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    gateway_db: CliSessionStore,
) -> None:
    _seed_mission(gateway_db, mission_id="mission-race")
    original_get_team_mission_graph = (
        gateway_db.team_mission_graphs.get_team_mission_graph
    )
    calls = {"count": 0}

    def flaky_get_team_mission_graph(mission_id: str) -> dict[str, Any]:
        calls["count"] += 1
        if calls["count"] == 1:
            return original_get_team_mission_graph(mission_id)
        return {}

    monkeypatch.setattr(
        gateway_db.team_mission_graphs,
        "get_team_mission_graph",
        flaky_get_team_mission_graph,
    )
    caplog.set_level(logging.WARNING, logger="hermes_team_mission.state.session_graph")

    response = _call("team_mission.cancel", {"mission_id": "mission-race"})

    _assert_error(response, 4040, "team mission not found")
    assert "reason=graph_empty_for_resolved_mission_id" in caplog.text
    assert "race or stale resolver" in caplog.text


def test_cancel_team_mission_state_layer_returns_empty_dict_when_graph_empty(
    gateway_db: CliSessionStore,
) -> None:
    assert gateway_db.cancel_team_mission(mission_id="missing-mission") == {}


def test_cancel_team_mission_state_layer_logs_clear_diagnostic_when_aborting(
    caplog: pytest.LogCaptureFixture,
    gateway_db: CliSessionStore,
) -> None:
    caplog.set_level(logging.WARNING, logger="hermes_team_mission.state.session_graph")

    gateway_db.cancel_team_mission(mission_id="missing-mission")

    assert "reason=graph_empty_for_resolved_mission_id" in caplog.text
    assert "get_team_mission_graph found no mission row" in caplog.text


class _MissionGraphs:
    def __init__(self, graphs: dict[str, dict[str, Any]]) -> None:
        self._graphs = graphs

    def get_team_mission_graph(self, mission_id: str) -> dict[str, Any]:
        return self._graphs.get(mission_id, {})


class _ResolverDB:
    def __init__(
        self,
        *,
        graphs: dict[str, dict[str, Any]] | None = None,
        projections: dict[str, Any] | None = None,
    ) -> None:
        self.projections = projections or {}
        self.team_mission_graphs = _MissionGraphs(graphs or {})

    def resolve_team_mission_conversation(self, identifier: str) -> Any:
        return self.projections.get(identifier, {})


def test_resolve_cancel_mission_id_returns_mission_id_when_explicit_match() -> None:
    db = _ResolverDB(graphs={"mission-1": {"mission": {"mission_id": "mission-1"}}})

    assert _resolve_cancel_mission_id(db, {"mission_id": "mission-1"}) == "mission-1"


def test_resolve_cancel_mission_id_returns_mission_id_from_conversation_id() -> None:
    db = _ResolverDB(
        graphs={"mission-1": {"mission": {"mission_id": "mission-1"}}},
        projections={"conversation-1": {"conversation": {"active_mission_id": "mission-1"}}},
    )

    assert _resolve_cancel_mission_id(db, {"conversation_id": "conversation-1"}) == "mission-1"


def test_resolve_cancel_mission_id_returns_mission_id_from_conversation_session_id() -> None:
    db = _ResolverDB(
        graphs={"mission-1": {"mission": {"mission_id": "mission-1"}}},
        projections={"team-session-1": {"mission": {"mission_id": "mission-1"}}},
    )

    assert (
        _resolve_cancel_mission_id(db, {"conversation_session_id": "team-session-1"})
        == "mission-1"
    )


def test_resolve_cancel_mission_id_returns_empty_when_no_identifier() -> None:
    assert _resolve_cancel_mission_id(_ResolverDB(), {}) == ""


def test_resolve_cancel_mission_id_returns_empty_when_identifier_does_not_resolve() -> None:
    assert _resolve_cancel_mission_id(_ResolverDB(), {"conversation_id": "missing"}) == ""


def test_resolve_cancel_mission_id_handles_resolver_returning_non_dict() -> None:
    db = _ResolverDB(projections={"conversation-1": ["not", "a", "dict"]})

    assert _resolve_cancel_mission_id(db, {"conversation_id": "conversation-1"}) == ""


def test_resolve_cancel_mission_id_returns_empty_when_graph_has_no_mission() -> None:
    db = _ResolverDB(
        projections={
            "conversation-1": {
                "conversation": {"active_mission_id": "mission-1"}
            }
        }
    )

    assert _resolve_cancel_mission_id(db, {"conversation_id": "conversation-1"}) == ""


def test_resolve_cancel_mission_id_handles_db_missing_resolve_team_mission_conversation() -> None:
    class _GraphOnlyDB:
        team_mission_graphs = _MissionGraphs({})

    db = _GraphOnlyDB()

    assert _resolve_cancel_mission_id(db, {"conversation_id": "conversation-1"}) == ""
