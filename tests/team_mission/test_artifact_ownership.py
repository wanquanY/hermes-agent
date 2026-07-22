from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from hermes_constants import reset_hermes_home_override
from hermes_constants import set_hermes_home_override
from hermes_team_mission.gateway.leader_report_runtime import (
    _register_leader_report_artifacts,
)
from hermes_team_mission.tools.deliverable import _canonical_run_artifact_refs
from tui_gateway.services.persistence.gateway_store import GatewayStateStore


def test_handoff_uses_durable_run_artifacts_when_model_reference_is_empty():
    artifact = {
        "id": "artifact:c",
        "path": "/workspace/c.txt",
        "title": "c.txt",
        "mime_type": "text/plain",
    }
    db = SimpleNamespace(
        runs=SimpleNamespace(
            list_events=lambda *_args, **_kwargs: [
                {"type": "artifact.created", "payload": artifact}
            ]
        )
    )

    refs = _canonical_run_artifact_refs(
        db,
        binding={"session_id": "team:mission-1:node:create-c"},
        run_id="run-c",
        submitted_refs=[{}],
    )

    assert refs == [
        {
            "id": "artifact:c",
            "path": "/workspace/c.txt",
            "title": "c.txt",
            "kind": "file",
            "mime_type": "text/plain",
            "mimeType": "text/plain",
        }
    ]


def test_leader_report_promotes_artifacts_to_control_plane_registry(
    tmp_path: Path,
    monkeypatch,
):
    control_home = tmp_path / "control"
    worker_home = tmp_path / "worker"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    output = workspace / "result.txt"
    output.write_text("done", encoding="utf-8")
    monkeypatch.delenv("DOVIE_HERMES_CONTROL_HOME", raising=False)
    monkeypatch.setenv("HERMES_HOME", str(control_home))

    token = set_hermes_home_override(worker_home)
    try:
        refs = _register_leader_report_artifacts(
            conversation_session_id="team-conversation-1",
            workspace_context={
                "cwd": str(workspace),
                "workspace": {
                    "id": "workspace-1",
                    "name": "workspace",
                    "path": str(workspace),
                    "kind": "local",
                },
            },
            mission_id="mission-1",
            result_id="result-1",
            run_id="leader-report-1",
            artifact_refs=[{"path": str(output), "kind": "file"}],
        )
    finally:
        reset_hermes_home_override(token)

    assert refs[0]["id"].startswith("artifact:")
    control_store = GatewayStateStore(control_home / "tui-gateway" / "state.db")
    stored = control_store.list_artifacts(session_id="team-conversation-1", limit=10)
    assert [item["path"] for item in stored] == [str(output)]
    assert stored[0]["origin"] == {
        "event": "team_mission.leader_report",
        "run_id": "leader-report-1",
        "mission_id": "mission-1",
        "result_id": "result-1",
        "visibility": "conversation_report",
    }
    assert not (worker_home / "tui-gateway" / "state.db").exists()
