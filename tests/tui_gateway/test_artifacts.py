import json

from tui_gateway.services.artifacts import (
    artifact_created_payloads_from_tool_complete,
    artifact_payloads_from_tool_complete,
    artifact_target_paths,
    capture_workspace_artifact_snapshot,
    delete_session_artifacts,
    list_artifacts,
    prune_artifacts,
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


def test_artifact_target_paths_extracts_explicit_domain_tool_artifacts():
    assert artifact_target_paths(
        "dovie_presentation_generate",
        {},
        json.dumps(
            {
                "status": "completed",
                "artifacts": [
                    {
                        "path": "/workspace/review.pptx",
                        "mime_type": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
                        "operation": "created",
                    }
                ],
            }
        ),
    ) == ["/workspace/review.pptx"]


def test_editable_presentation_artifact_is_registered_from_explicit_contract():
    assert artifact_target_paths(
        "dovie_presentation_build",
        {},
        json.dumps(
            {
                "status": "completed",
                "artifacts": [
                    {
                        "path": "/workspace/editable-review.pptx",
                        "mime_type": (
                            "application/vnd.openxmlformats-officedocument."
                            "presentationml.presentation"
                        ),
                        "operation": "created",
                    }
                ],
            }
        ),
    ) == ["/workspace/editable-review.pptx"]


def test_artifact_payloads_preserve_modified_presentation_operation(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    artifact = workspace / "review.pptx"
    artifact.write_bytes(b"revised-pptx")

    payloads = artifact_payloads_from_tool_complete(
        tool_call_id="tool-presentation-revision",
        name="dovie_presentation_regenerate_slide",
        args={"page_number": 2},
        result=json.dumps(
            {
                "status": "completed",
                "artifacts": [
                    {
                        "path": str(artifact),
                        "mime_type": (
                            "application/vnd.openxmlformats-officedocument."
                            "presentationml.presentation"
                        ),
                        "operation": "modified",
                    }
                ],
            }
        ),
        cwd=str(workspace),
        workspace={"id": "workspace-test", "path": str(workspace)},
    )

    assert len(payloads) == 1
    assert payloads[0]["path"] == str(artifact)
    assert payloads[0]["operation"] == "modified"
    assert payloads[0]["origin"]["tool_name"] == (
        "dovie_presentation_regenerate_slide"
    )


def test_artifact_created_payloads_validate_explicit_domain_tool_artifacts(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    artifact = workspace / "review.pptx"
    artifact.write_bytes(b"pptx")
    outside = tmp_path / "outside.pptx"
    outside.write_bytes(b"outside")

    payloads = artifact_created_payloads_from_tool_complete(
        tool_call_id="tool-presentation",
        name="dovie_presentation_generate",
        args={},
        result=json.dumps(
            {
                "status": "completed",
                "artifacts": [
                    {
                        "path": str(artifact),
                        "title": "Quarterly review",
                        "mime_type": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
                        "operation": "created",
                    },
                    {"path": str(outside), "operation": "created"},
                ],
            }
        ),
        cwd=str(workspace),
        workspace={"id": "workspace-test", "path": str(workspace)},
    )

    assert len(payloads) == 1
    assert payloads[0]["path"] == str(artifact)
    assert payloads[0]["mime_type"] == (
        "application/vnd.openxmlformats-officedocument.presentationml.presentation"
    )
    assert payloads[0]["title"] == "Quarterly review"
    assert payloads[0]["origin"]["tool_name"] == "dovie_presentation_generate"


def test_artifact_payloads_extract_patch_files_deleted(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    deleted = workspace / "old.md"

    payloads = artifact_payloads_from_tool_complete(
        tool_call_id="tool-delete",
        name="patch",
        args={"mode": "patch"},
        result=json.dumps({
            "success": True,
            "files_deleted": [str(deleted)],
        }),
        cwd=str(workspace),
        workspace={"id": "workspace-test", "path": str(workspace)},
    )

    assert len(payloads) == 1
    assert artifact_created_payloads_from_tool_complete(
        tool_call_id="tool-delete",
        name="patch",
        args={"mode": "patch"},
        result=json.dumps({
            "success": True,
            "files_deleted": [str(deleted)],
        }),
        cwd=str(workspace),
        workspace={"id": "workspace-test", "path": str(workspace)},
    ) == []
    assert payloads[0]["operation"] == "deleted"
    assert payloads[0]["path"] == str(deleted)
    assert payloads[0]["relative_path"] == "old.md"
    assert payloads[0]["relativePath"] == "old.md"
    assert payloads[0]["availability"] == "missing"
    assert payloads[0]["origin"] == {
        "event": "tool.complete",
        "tool_id": "tool-delete",
        "tool_name": "patch",
        "operation": "deleted",
    }


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


def test_terminal_tool_complete_payloads_use_workspace_diff(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    existing = workspace / "existing.md"
    existing.write_text("# before\n", encoding="utf-8")

    snapshot = capture_workspace_artifact_snapshot(
        name="terminal",
        cwd=str(workspace),
        workspace={"id": "workspace-test", "path": str(workspace)},
    )

    existing.write_text("# after\n", encoding="utf-8")
    created = workspace / "created.txt"
    created.write_text("hello\n", encoding="utf-8")

    payloads = artifact_created_payloads_from_tool_complete(
        tool_call_id="tool-terminal",
        name="terminal",
        args={"command": "printf hello > created.txt"},
        result=json.dumps({"output": "", "exit_code": 0, "error": None}),
        cwd=str(workspace),
        workspace={"id": "workspace-test", "path": str(workspace)},
        workspace_snapshot=snapshot,
    )

    by_path = {item["path"]: item for item in payloads}
    assert set(by_path) == {str(created), str(existing)}
    assert by_path[str(created)]["origin"]["operation"] == "created"
    assert by_path[str(existing)]["origin"]["operation"] == "modified"
    assert by_path[str(created)]["origin"]["source"] == "workspace_diff"


def test_terminal_tool_complete_payloads_include_deleted_workspace_diff(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    deleted = workspace / "deleted.md"
    deleted.write_text("# before\n", encoding="utf-8")

    snapshot = capture_workspace_artifact_snapshot(
        name="terminal",
        cwd=str(workspace),
        workspace={"id": "workspace-test", "path": str(workspace)},
    )

    deleted.unlink()

    payloads = artifact_payloads_from_tool_complete(
        tool_call_id="tool-terminal",
        name="terminal",
        args={"command": "rm deleted.md"},
        result=json.dumps({"output": "", "exit_code": 0, "error": None}),
        cwd=str(workspace),
        workspace={"id": "workspace-test", "path": str(workspace)},
        workspace_snapshot=snapshot,
    )

    assert len(payloads) == 1
    assert payloads[0]["operation"] == "deleted"
    assert payloads[0]["path"] == str(deleted)
    assert payloads[0]["origin"]["operation"] == "deleted"
    assert payloads[0]["origin"]["source"] == "workspace_diff"


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


def test_record_artifacts_persists_remote_image_generation_output(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setattr(gateway_store, "get_hermes_home", lambda: tmp_path)
    gateway_store._DEFAULT_STORES.clear()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    image_url = "https://cdn.example.com/images/generated-cat.png"

    [created] = record_artifacts_from_tool_complete(
        session_id="session-1",
        tool_call_id="image-tool-1",
        name="dovie_image_generate",
        args={"prompt": "cat"},
        result=json.dumps({
            "success": True,
            "status": "completed",
            "image_urls": [image_url],
        }),
        cwd=str(workspace),
        workspace={"id": "workspace-test", "path": str(workspace)},
        origin={"run_id": "run-1", "turn_id": "turn-1"},
    )

    assert created["path"] == image_url
    assert created["url"] == image_url
    assert created["source"] == image_url
    assert created["relative_path"] == "generated-cat.png"
    assert created["title"] == "generated-cat.png"
    assert created["mime_type"] == "image/png"
    assert created["availability"] == "available"
    assert created["origin"] == {
        "event": "tool.complete",
        "tool_id": "image-tool-1",
        "tool_name": "dovie_image_generate",
        "source": "remote_media",
        "url": image_url,
        "canonical_identity": created["origin"]["canonical_identity"],
        "run_id": "run-1",
        "turn_id": "turn-1",
    }
    [listed] = list_artifacts(session_id="session-1")
    assert listed["id"] == created["id"]
    assert listed["path"] == image_url
    assert listed["url"] == image_url
    assert listed["source"] == image_url
    assert listed["availability"] == "available"
    assert listed["origin"] == created["origin"]


def test_record_artifacts_uses_materialized_image_as_single_canonical_artifact(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setattr(gateway_store, "get_hermes_home", lambda: tmp_path)
    gateway_store._DEFAULT_STORES.clear()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    image = workspace / "assets" / "generated-cat.png"
    image.parent.mkdir()
    image.write_bytes(b"png")
    image_url = "https://cdn.example.com/images/generated-cat.png"

    [created] = record_artifacts_from_tool_complete(
        session_id="session-1",
        tool_call_id="image-tool-1",
        name="dovie_image_generate",
        args={
            "prompt": "cat",
            "output_directory": "assets",
        },
        result=json.dumps(
            {
                "success": True,
                "status": "completed",
                "image_urls": [image_url],
                "materialized": True,
                "artifacts": [
                    {
                        "path": str(image),
                        "title": "generated-cat.png",
                        "mime_type": "image/png",
                        "operation": "created",
                        "source_url": image_url,
                    }
                ],
            }
        ),
        cwd=str(workspace),
        workspace={"id": "workspace-test", "path": str(workspace)},
        origin={"run_id": "run-1", "turn_id": "turn-1"},
    )

    assert created["path"] == str(image)
    assert created["origin"]["source"] == "materialized_remote_media"
    assert created["origin"]["source_url"] == image_url
    assert created["origin"]["canonical_identity"].startswith("remote-image:")
    assert created["origin"]["materialized_from"].startswith("artifact:")
    assert len(list_artifacts(session_id="session-1")) == 1


def test_record_artifacts_ignores_failed_remote_image_generation(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setattr(gateway_store, "get_hermes_home", lambda: tmp_path)
    gateway_store._DEFAULT_STORES.clear()
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    assert record_artifacts_from_tool_complete(
        session_id="session-1",
        tool_call_id="image-tool-1",
        name="dovie_image_generate",
        args={"prompt": "cat"},
        result=json.dumps({
            "success": False,
            "status": "failed",
            "image_urls": ["https://cdn.example.com/images/should-not-render.png"],
        }),
        cwd=str(workspace),
        workspace={"id": "workspace-test", "path": str(workspace)},
    ) == []
    assert list_artifacts(session_id="session-1") == []


def test_record_artifacts_removes_deleted_file_from_registry(monkeypatch, tmp_path):
    monkeypatch.setattr(gateway_store, "get_hermes_home", lambda: tmp_path)
    gateway_store._DEFAULT_STORES.clear()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    artifact = workspace / "old.md"
    artifact.write_text("# old\n", encoding="utf-8")

    [created] = record_artifacts_from_tool_complete(
        session_id="session-1",
        tool_call_id="tool-create",
        name="write_file",
        args={"path": "old.md"},
        result=json.dumps({"bytes_written": artifact.stat().st_size}),
        cwd=str(workspace),
        workspace={"id": "workspace-test", "path": str(workspace)},
    )
    artifact.unlink()

    [deleted] = record_artifacts_from_tool_complete(
        session_id="session-1",
        tool_call_id="tool-delete",
        name="patch",
        args={"mode": "patch"},
        result=json.dumps({
            "success": True,
            "files_deleted": [str(artifact)],
        }),
        cwd=str(workspace),
        workspace={"id": "workspace-test", "path": str(workspace)},
    )

    assert deleted["id"] == created["id"]
    assert deleted["operation"] == "deleted"
    assert deleted["availability"] == "missing"
    assert deleted["origin"]["tool_id"] == "tool-delete"
    assert deleted["origin"]["operation"] == "deleted"
    assert list_artifacts(session_id="session-1") == []


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


def test_prune_artifacts_removes_old_metadata_without_deleting_workspace_files(monkeypatch, tmp_path):
    monkeypatch.setattr(gateway_store, "get_hermes_home", lambda: tmp_path)
    gateway_store._DEFAULT_STORES.clear()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    old_artifact = workspace / "old.md"
    keep_artifact = workspace / "keep.md"
    unrelated_orphan = workspace / "orphan.md"
    old_artifact.write_text("# old\n", encoding="utf-8")
    keep_artifact.write_text("# keep\n", encoding="utf-8")
    unrelated_orphan.write_text("# orphan\n", encoding="utf-8")

    record_artifacts_from_tool_complete(
        session_id="session-1",
        tool_call_id="tool-old",
        name="write_file",
        args={"path": "old.md"},
        result=json.dumps({"bytes_written": old_artifact.stat().st_size}),
        cwd=str(workspace),
        workspace={"id": "workspace-test", "path": str(workspace)},
    )
    record_artifacts_from_tool_complete(
        session_id="session-1",
        tool_call_id="tool-keep",
        name="write_file",
        args={"path": "keep.md"},
        result=json.dumps({"bytes_written": keep_artifact.stat().st_size}),
        cwd=str(workspace),
        workspace={"id": "workspace-test", "path": str(workspace)},
    )
    record_artifacts_from_tool_complete(
        session_id="session-2",
        tool_call_id="tool-orphan",
        name="write_file",
        args={"path": "orphan.md"},
        result=json.dumps({"bytes_written": unrelated_orphan.stat().st_size}),
        cwd=str(workspace),
        workspace={"id": "workspace-test", "path": str(workspace)},
    )
    store = gateway_store.get_gateway_state_store()
    old_timestamp = 1.0
    with store._connect() as conn:
        conn.execute(
            "UPDATE gateway_session_artifacts SET last_seen_at = ? WHERE session_id = ? AND artifact_id IN "
            "(SELECT id FROM gateway_artifacts WHERE path = ?)",
            (old_timestamp, "session-1", str(old_artifact)),
        )
        conn.execute(
            "UPDATE gateway_artifacts SET updated_at = ? WHERE path = ?",
            (old_timestamp, str(old_artifact)),
        )
        conn.execute(
            "DELETE FROM gateway_session_artifacts WHERE artifact_id IN "
            "(SELECT id FROM gateway_artifacts WHERE path = ?)",
            (str(unrelated_orphan),),
        )

    result = prune_artifacts(session_id="session-1", retention_days=1)
    listed = list_artifacts(session_id="session-1")
    workspace_paths = {artifact["path"] for artifact in list_artifacts(workspace_id="workspace-test")}

    assert result["deleted_artifact_links"] == 1
    assert result["deleted_artifacts"] == 1
    assert result["physical_files_deleted"] == 0
    assert [artifact["path"] for artifact in listed] == [str(keep_artifact)]
    assert str(unrelated_orphan) in workspace_paths
    assert old_artifact.exists()
    assert keep_artifact.exists()
    assert unrelated_orphan.exists()


def test_prune_artifacts_caps_session_links_but_keeps_shared_artifacts(monkeypatch, tmp_path):
    monkeypatch.setattr(gateway_store, "get_hermes_home", lambda: tmp_path)
    gateway_store._DEFAULT_STORES.clear()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    shared_artifact = workspace / "shared.md"
    older_artifact = workspace / "older.md"
    shared_artifact.write_text("# shared\n", encoding="utf-8")
    older_artifact.write_text("# older\n", encoding="utf-8")

    record_artifacts_from_tool_complete(
        session_id="session-1",
        tool_call_id="tool-shared-1",
        name="write_file",
        args={"path": "shared.md"},
        result=json.dumps({"bytes_written": shared_artifact.stat().st_size}),
        cwd=str(workspace),
        workspace={"id": "workspace-test", "path": str(workspace)},
    )
    record_artifacts_from_tool_complete(
        session_id="session-2",
        tool_call_id="tool-shared-2",
        name="write_file",
        args={"path": "shared.md"},
        result=json.dumps({"bytes_written": shared_artifact.stat().st_size}),
        cwd=str(workspace),
        workspace={"id": "workspace-test", "path": str(workspace)},
    )
    record_artifacts_from_tool_complete(
        session_id="session-1",
        tool_call_id="tool-older",
        name="write_file",
        args={"path": "older.md"},
        result=json.dumps({"bytes_written": older_artifact.stat().st_size}),
        cwd=str(workspace),
        workspace={"id": "workspace-test", "path": str(workspace)},
    )
    store = gateway_store.get_gateway_state_store()
    with store._connect() as conn:
        conn.execute(
            "UPDATE gateway_session_artifacts SET last_seen_at = 1 WHERE session_id = ? AND artifact_id IN "
            "(SELECT id FROM gateway_artifacts WHERE path = ?)",
            ("session-1", str(shared_artifact)),
        )

    result = prune_artifacts(session_id="session-1", max_artifacts_per_session=1)

    assert result["deleted_artifact_links"] == 1
    assert result["deleted_artifacts"] == 0
    assert [artifact["path"] for artifact in list_artifacts(session_id="session-1")] == [str(older_artifact)]
    assert [artifact["path"] for artifact in list_artifacts(session_id="session-2")] == [str(shared_artifact)]


def test_delete_session_artifacts_removes_links_and_orphan_metadata_without_deleting_files(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setattr(gateway_store, "get_hermes_home", lambda: tmp_path)
    gateway_store._DEFAULT_STORES.clear()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    shared_artifact = workspace / "shared.md"
    owned_artifact = workspace / "owned.md"
    shared_artifact.write_text("# shared\n", encoding="utf-8")
    owned_artifact.write_text("# owned\n", encoding="utf-8")

    record_artifacts_from_tool_complete(
        session_id="session-1",
        tool_call_id="tool-shared-1",
        name="write_file",
        args={"path": "shared.md"},
        result=json.dumps({"bytes_written": shared_artifact.stat().st_size}),
        cwd=str(workspace),
        workspace={"id": "workspace-test", "path": str(workspace)},
    )
    record_artifacts_from_tool_complete(
        session_id="session-2",
        tool_call_id="tool-shared-2",
        name="write_file",
        args={"path": "shared.md"},
        result=json.dumps({"bytes_written": shared_artifact.stat().st_size}),
        cwd=str(workspace),
        workspace={"id": "workspace-test", "path": str(workspace)},
    )
    record_artifacts_from_tool_complete(
        session_id="session-1",
        tool_call_id="tool-owned",
        name="write_file",
        args={"path": "owned.md"},
        result=json.dumps({"bytes_written": owned_artifact.stat().st_size}),
        cwd=str(workspace),
        workspace={"id": "workspace-test", "path": str(workspace)},
    )

    result = delete_session_artifacts(["session-1"])
    workspace_paths = {artifact["path"] for artifact in list_artifacts(workspace_id="workspace-test")}

    assert result["deleted_artifact_links"] == 2
    assert result["deleted_artifacts"] == 1
    assert result["physical_files_deleted"] == 0
    assert list_artifacts(session_id="session-1") == []
    assert [artifact["path"] for artifact in list_artifacts(session_id="session-2")] == [str(shared_artifact)]
    assert str(shared_artifact) in workspace_paths
    assert str(owned_artifact) not in workspace_paths
    assert shared_artifact.exists()
    assert owned_artifact.exists()
