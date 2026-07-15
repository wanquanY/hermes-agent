from __future__ import annotations

import ast
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from hermes_team_mission.tools import profile as profile_tools


REPO_ROOT = Path(__file__).resolve().parents[1]
PROFILE_PATH = REPO_ROOT / "hermes_team_mission/tools/profile.py"
WORKER_SUPERVISOR_PATH = REPO_ROOT / "hermes_agent/orchestration/worker_supervisor.py"

FORBIDDEN_SNAPSHOT_DB_METHODS = {
    "get_team_capability_snapshot",
    "get_team_capability_snapshot_binding",
    "get_bound_team_capability_snapshot",
    "get_latest_team_capability_snapshot",
}


class _ForbiddenSnapshotDB:
    def get_team_capability_snapshot(self, *_args, **_kwargs):  # pragma: no cover - failure path only
        raise AssertionError("planning leader tool must use gateway RPC")

    def get_team_capability_snapshot_binding(self, *_args, **_kwargs):  # pragma: no cover - failure path only
        raise AssertionError("planning leader tool must use gateway RPC")

    def get_bound_team_capability_snapshot(self, *_args, **_kwargs):  # pragma: no cover - failure path only
        raise AssertionError("planning leader tool must use gateway RPC")

    def get_latest_team_capability_snapshot(self, *_args, **_kwargs):  # pragma: no cover - failure path only
        raise AssertionError("planning leader tool must use gateway RPC")


def _leader_run_ctx() -> tuple[Any, str, dict[str, Any], dict[str, Any], dict[str, Any]]:
    return (
        _ForbiddenSnapshotDB(),
        "run-leader",
        {"mission_id": "mission-1", "node_id": "root"},
        {
            "mission_id": "mission-1",
            "team_id": "team-1",
            "status": "planning",
            "metadata": {"team_capability_snapshot": {"snapshot_id": "snapshot-1"}},
        },
        {"node_id": "root", "kind": "root", "metadata": {"role": "leader", "phase": "planning"}},
    )


def _gateway_success(snapshot: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "result": {
            "mission_id": "mission-1",
            "team_id": "team-1",
            "source": "mission_binding",
            "binding": {"mission_id": "mission-1", "snapshot_id": "snapshot-1"},
            "snapshot": snapshot
            or {
                "snapshot_id": "snapshot-1",
                "team_id": "team-1",
                "version": 1,
                "status": "ready",
                "team_profile": {
                    "display_name": "Launch Team",
                    "collaboration_mode": "supervised_mission",
                    "positioning": "Build and verify.",
                },
                "member_profiles": [
                    {
                        "member_id": "builder",
                        "agent_profile_id": "profile-builder",
                        "display_name": "Builder",
                        "role": "engineer",
                        "profile_description": "Builds the implementation.",
                        "capability_tags": ["code"],
                    }
                ],
            },
        }
    }


def _json(raw: str) -> dict[str, Any]:
    return json.loads(raw)


def test_handle_leader_run_team_profile_calls_gateway_team_profile_rpc(monkeypatch) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(profile_tools, "_leader_run_context", lambda _args, _parent_agent=None: _leader_run_ctx())
    monkeypatch.setattr(
        profile_tools,
        "gateway_call",
        lambda method, params: calls.append((method, params)) or _gateway_success(),
    )

    result = _json(profile_tools._handle_leader_run_team_profile({}, SimpleNamespace()))

    assert result["success"] is True
    assert calls == [
        (
            "team_mission.team_profile.get",
            {
                "mission_id": "mission-1",
                "snapshot_id": "snapshot-1",
                "team_id": "team-1",
                "node_id": "root",
            },
        )
    ]


def test_handle_leader_run_team_profile_does_not_call_db_get_team_capability_snapshot(monkeypatch) -> None:
    monkeypatch.setattr(profile_tools, "_leader_run_context", lambda _args, _parent_agent=None: _leader_run_ctx())
    monkeypatch.setattr(profile_tools, "gateway_call", lambda _method, _params: _gateway_success())

    result = _json(profile_tools._handle_leader_run_team_profile({}, SimpleNamespace()))

    assert result["success"] is True
    assert result["source"] == "mission_binding"


def test_handle_leader_run_team_profile_returns_snapshot_when_rpc_succeeds(monkeypatch) -> None:
    monkeypatch.setattr(profile_tools, "_leader_run_context", lambda _args, _parent_agent=None: _leader_run_ctx())
    monkeypatch.setattr(profile_tools, "gateway_call", lambda _method, _params: _gateway_success())

    result = _json(profile_tools._handle_leader_run_team_profile({"detail": "assignment"}, SimpleNamespace()))

    assert result["success"] is True
    assert result["mission_id"] == "mission-1"
    assert result["binding"] == {"mission_id": "mission-1", "snapshot_id": "snapshot-1"}
    assert result["node"] == {"node_id": "root", "kind": "root", "role": "leader", "phase": "planning"}
    assert result["snapshot"]["snapshot_id"] == "snapshot-1"
    assert result["snapshot"]["member_profiles"][0]["member_id"] == "builder"


def test_handle_leader_run_team_profile_returns_error_when_rpc_fails(monkeypatch) -> None:
    monkeypatch.setattr(profile_tools, "_leader_run_context", lambda _args, _parent_agent=None: _leader_run_ctx())
    monkeypatch.setattr(
        profile_tools,
        "gateway_call",
        lambda _method, _params: {"error": {"message": "team capability snapshot not found"}},
    )

    result = _json(profile_tools._handle_leader_run_team_profile({}, SimpleNamespace()))

    assert result["error"] == "team capability snapshot not found"


def test_leader_run_profile_params_carries_mission_id() -> None:
    params = profile_tools._leader_run_profile_params("mission-1", {}, {})

    assert params["mission_id"] == "mission-1"


def test_leader_run_profile_params_carries_snapshot_id_from_mission_metadata() -> None:
    params = profile_tools._leader_run_profile_params(
        "mission-1",
        {"metadata": {"team_capability_snapshot": {"snapshot_id": "snapshot-1"}}},
        {},
    )

    assert params["snapshot_id"] == "snapshot-1"


def test_leader_run_profile_params_carries_team_id_when_mission_has_it() -> None:
    params = profile_tools._leader_run_profile_params("mission-1", {"team_id": "team-1"}, {})

    assert params["team_id"] == "team-1"


def test_leader_run_profile_params_carries_node_id_when_present() -> None:
    params = profile_tools._leader_run_profile_params("mission-1", {}, {"node_id": "root"})

    assert params["node_id"] == "root"


def _db_rpc_allowed_methods_from_ast() -> set[str]:
    tree = ast.parse(WORKER_SUPERVISOR_PATH.read_text(encoding="utf-8"), filename=str(WORKER_SUPERVISOR_PATH))
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if not any(isinstance(target, ast.Name) and target.id == "DB_RPC_ALLOWED_METHODS" for target in node.targets):
            continue
        value = node.value
        assert isinstance(value, ast.Call)
        assert value.args
        literal = value.args[0]
        assert isinstance(literal, (ast.Set, ast.List, ast.Tuple))
        return {entry.value for entry in literal.elts if isinstance(entry, ast.Constant) and isinstance(entry.value, str)}
    raise AssertionError("DB_RPC_ALLOWED_METHODS not found")


def _profile_attribute_names_from_ast() -> set[str]:
    tree = ast.parse(PROFILE_PATH.read_text(encoding="utf-8"), filename=str(PROFILE_PATH))
    return {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}


def test_db_rpc_allowed_methods_does_not_include_team_capability_snapshot() -> None:
    allowed = _db_rpc_allowed_methods_from_ast()

    assert not (FORBIDDEN_SNAPSHOT_DB_METHODS & allowed)


def test_profile_module_does_not_reference_get_team_capability_snapshot() -> None:
    assert "get_team_capability_snapshot" not in _profile_attribute_names_from_ast()


def test_profile_module_does_not_reference_get_team_capability_snapshot_binding() -> None:
    assert "get_team_capability_snapshot_binding" not in _profile_attribute_names_from_ast()


def test_profile_module_does_not_reference_get_bound_team_capability_snapshot() -> None:
    assert "get_bound_team_capability_snapshot" not in _profile_attribute_names_from_ast()


def test_profile_module_does_not_reference_get_latest_team_capability_snapshot() -> None:
    assert "get_latest_team_capability_snapshot" not in _profile_attribute_names_from_ast()


def test_handle_leader_team_profile_still_works_via_gateway_call_path(monkeypatch) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(
        profile_tools,
        "_leader_team_context",
        lambda: {
            "kind": "leader_conversation",
            "conversation_id": "conversation-1",
            "conversation_session_id": "session-1",
            "team_id": "team-1",
        },
    )
    monkeypatch.setattr(
        profile_tools,
        "_get_db",
        lambda _parent_agent=None: SimpleNamespace(
            resolve_team_mission_conversation=lambda _identifier: {
                "mission": {"mission_id": "mission-1"},
                "graph": {"mission": {"mission_id": "mission-1"}},
            }
        ),
    )
    monkeypatch.setattr(
        profile_tools,
        "gateway_call",
        lambda method, params: calls.append((method, params)) or _gateway_success(),
    )

    result = _json(profile_tools._handle_leader_team_profile({}, SimpleNamespace()))

    assert result["success"] is True
    assert result["snapshot"]["snapshot_id"] == "snapshot-1"
    assert calls == [
        (
            "team_mission.team_profile.get",
            {
                "team_id": "team-1",
                "conversation_id": "conversation-1",
                "conversation_session_id": "session-1",
                "mission_id": "mission-1",
            },
        )
    ]
