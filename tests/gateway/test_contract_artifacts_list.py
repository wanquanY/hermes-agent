from __future__ import annotations

import json
from pathlib import Path


def test_artifacts_list_exposes_dovie_ui_contract_fields(monkeypatch, tmp_path: Path) -> None:
    from tui_gateway import server
    from tui_gateway.services.artifacts import record_artifacts_from_tool_complete
    from tui_gateway.services.persistence import gateway_store

    monkeypatch.setattr(gateway_store, "get_hermes_home", lambda: tmp_path / "hermes")
    gateway_store._DEFAULT_STORES.clear()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    artifact = workspace / "report.md"
    artifact.write_text("# report\n", encoding="utf-8")

    record_artifacts_from_tool_complete(
        session_id="session-1",
        tool_call_id="tool-1",
        name="write_file",
        args={"path": "report.md"},
        result=json.dumps({"bytes_written": artifact.stat().st_size}),
        cwd=str(workspace),
        workspace={"id": "workspace-1", "path": str(workspace), "name": "Workspace"},
        origin={"run_id": "run-1"},
    )

    response = server._methods["artifacts.list"](
        1,
        {"session_id": "session-1", "workspace_id": "workspace-1"},
    )

    assert "error" not in response
    [item] = response["result"]["artifacts"]
    assert item["path"] == str(artifact)
    assert item["relative_path"] == "report.md"
    assert item["relativePath"] == "report.md"
    assert item["mime"] == "text/markdown"
    assert item["mimeType"] == "text/markdown"
    assert item["kind"] == "text"
    assert item["size"] == artifact.stat().st_size
    assert item["sizeBytes"] == artifact.stat().st_size
    assert item["produced_by_run_id"] == "run-1"
    assert item["producedByRunId"] == "run-1"
    assert item["availability"] == "available"
    assert item["workspace"]["id"] == "workspace-1"
    assert item["workspacePayload"]["id"] == "workspace-1"


def test_artifacts_register_and_delete_are_hermes_owned(monkeypatch, tmp_path: Path) -> None:
    from tui_gateway import server
    from tui_gateway.services.persistence import gateway_store

    monkeypatch.setattr(gateway_store, "get_hermes_home", lambda: tmp_path / "hermes")
    gateway_store._DEFAULT_STORES.clear()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    artifact = workspace / "report.md"
    artifact.write_text("# report\n", encoding="utf-8")

    register_response = server._methods["artifacts.register"](
        1,
        {
            "session_id": "session-1",
            "path": str(artifact),
            "cwd": str(workspace),
            "workspace": {"id": "workspace-1", "path": str(workspace), "name": "Workspace"},
            "origin": {"source": "desktop_test"},
        },
    )

    assert "error" not in register_response
    registered = register_response["result"]["artifact"]
    assert registered["path"] == str(artifact)
    assert registered["workspace"]["id"] == "workspace-1"

    list_response = server._methods["artifacts.list"](
        2,
        {"session_id": "session-1", "workspace_id": "workspace-1"},
    )
    assert [item["id"] for item in list_response["result"]["artifacts"]] == [registered["id"]]

    delete_response = server._methods["artifacts.delete"](
        3,
        {"session_id": "session-1", "workspace_id": "workspace-1", "artifact_id": registered["id"]},
    )
    assert "error" not in delete_response
    assert delete_response["result"]["deleted"] is True
    assert artifact.exists()

    empty_response = server._methods["artifacts.list"](
        4,
        {"session_id": "session-1", "workspace_id": "workspace-1"},
    )
    assert empty_response["result"]["artifacts"] == []
