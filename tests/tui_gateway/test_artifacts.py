import json

from tui_gateway.services.artifacts import (
    artifact_created_payloads_from_tool_complete,
    artifact_target_paths,
    list_artifacts,
    record_artifacts_from_tool_complete,
)
from tui_gateway.services.persistence import gateway_store


def test_artifact_target_paths_extracts_successful_patch_targets():
    patch_body = """*** Begin Patch
*** Add File: report.md
+hello
*** Update File: nested/file.txt
+world
*** End Patch
"""

    assert artifact_target_paths(
        "patch",
        {"mode": "patch", "patch": patch_body},
        json.dumps({"success": True}),
    ) == ["report.md", "nested/file.txt"]


def test_artifact_created_payloads_are_limited_to_workspace(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    artifact = workspace / "report.md"
    artifact.write_text("# report\n", encoding="utf-8")
    outside = tmp_path / "outside.md"
    outside.write_text("# outside\n", encoding="utf-8")

    payloads = artifact_created_payloads_from_tool_complete(
        tool_call_id="tool-1",
        name="patch",
        args={"mode": "patch"},
        result=json.dumps({
            "success": True,
            "files_created": [str(artifact), str(outside)],
        }),
        cwd=str(workspace),
        workspace={"id": "workspace-test", "path": str(workspace)},
    )

    assert len(payloads) == 1
    assert payloads[0]["id"].startswith("artifact:")
    assert payloads[0]["path"] == str(artifact)
    assert payloads[0]["relative_path"] == "report.md"
    assert payloads[0]["title"] == "report.md"
    assert payloads[0]["origin"] == {
        "event": "tool.complete",
        "tool_id": "tool-1",
        "tool_name": "patch",
    }


def test_record_artifacts_persists_and_deduplicates_by_workspace_path(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setattr(gateway_store, "get_hermes_home", lambda: tmp_path)
    gateway_store._DEFAULT_STORES.clear()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    artifact = workspace / "report.md"
    artifact.write_text("# v1\n", encoding="utf-8")

    first = record_artifacts_from_tool_complete(
        session_id="session-1",
        tool_call_id="tool-1",
        name="write_file",
        args={"path": "report.md"},
        result=json.dumps({"bytes_written": artifact.stat().st_size}),
        cwd=str(workspace),
        workspace={"id": "workspace-test", "path": str(workspace)},
    )
    artifact.write_text("# v2\n", encoding="utf-8")
    second = record_artifacts_from_tool_complete(
        session_id="session-1",
        tool_call_id="tool-2",
        name="write_file",
        args={"path": "report.md"},
        result=json.dumps({"bytes_written": artifact.stat().st_size}),
        cwd=str(workspace),
        workspace={"id": "workspace-test", "path": str(workspace)},
    )

    listed = list_artifacts(session_id="session-1")

    assert first[0]["id"] == second[0]["id"]
    assert len(listed) == 1
    assert listed[0]["path"] == str(artifact)
    assert listed[0]["workspace"]["id"] == "workspace-test"
    assert listed[0]["origin"]["tool_id"] == "tool-2"


def test_record_artifacts_preserves_turn_origin(monkeypatch, tmp_path):
    monkeypatch.setattr(gateway_store, "get_hermes_home", lambda: tmp_path)
    gateway_store._DEFAULT_STORES.clear()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    artifact = workspace / "report.md"
    artifact.write_text("# report\n", encoding="utf-8")

    [record] = record_artifacts_from_tool_complete(
        session_id="session-1",
        tool_call_id="tool-1",
        name="write_file",
        args={"path": "report.md"},
        result=json.dumps({"bytes_written": artifact.stat().st_size}),
        cwd=str(workspace),
        workspace={"id": "workspace-test", "path": str(workspace)},
        origin={
            "run_id": "run-1",
            "turn_id": "turn-1",
            "client_message_id": "msg-1",
        },
    )

    assert record["origin"] == {
        "event": "tool.complete",
        "tool_id": "tool-1",
        "tool_name": "write_file",
        "run_id": "run-1",
        "turn_id": "turn-1",
        "client_message_id": "msg-1",
    }
