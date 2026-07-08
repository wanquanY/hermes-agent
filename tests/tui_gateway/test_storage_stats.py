from __future__ import annotations

from hermes_state import SessionDB
from tui_gateway.services.persistence.gateway_store import GatewayStateStore
from tui_gateway.services.storage_stats import collect_storage_stats


def test_collect_storage_stats_reports_runtime_tables_artifacts_and_logs(tmp_path):
    hermes_home = tmp_path / "hermes-home"
    hermes_home.mkdir()
    db = SessionDB(hermes_home / "state.db")
    try:
        db.create_session("session-1", "dovie")
        db.append_message(
            "session-1",
            "user",
            "hello",
            metadata={"client_message_id": "client-1"},
        )
        db.append_message(
            "session-1",
            "assistant",
            "world",
            reasoning="thinking",
        )
        db.append_run_event(
            "session-1",
            {
                "type": "message.delta",
                "session_id": "runtime-1",
                "conversation_session_id": "session-1",
                "run_id": "run-1",
                "turn_id": "turn-1",
                "seq": 1,
                "payload": {"delta": "stream"},
            },
        )
    finally:
        db.close()

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    artifact_path = workspace / "report.md"
    artifact_path.write_text("# report\n", encoding="utf-8")
    gateway_store = GatewayStateStore(hermes_home / "tui-gateway" / "state.db")
    gateway_store.upsert_workspace({
        "id": "workspace-1",
        "name": "workspace",
        "path": str(workspace),
        "kind": "local",
    })
    gateway_store.upsert_artifact(
        artifact={
            "id": "artifact-1",
            "workspace_id": "workspace-1",
            "path": str(artifact_path),
            "relative_path": "report.md",
            "title": "report.md",
            "mime_type": "text/markdown",
            "size_bytes": artifact_path.stat().st_size,
            "origin": {"run_id": "run-1"},
        },
        session_id="session-1",
    )

    log_path = hermes_home / "logs" / "runtime-worker.log"
    log_path.parent.mkdir()
    log_path.write_text("worker log\n", encoding="utf-8")

    stats = collect_storage_stats(hermes_home=hermes_home)

    assert stats["state_db"]["tables"]["sessions"]["rows"] == 1
    assert stats["state_db"]["tables"]["messages"]["rows"] == 2
    assert stats["state_db"]["tables"]["run_events"]["rows"] == 1
    assert stats["state_db"]["tables"]["run_event_search_index"]["rows"] == 1
    assert stats["state_db"]["tables"]["session_runtime_state"]["rows"] == 0
    assert stats["state_db"]["tables"]["tool_events"]["rows"] == 0
    assert stats["state_db"]["tables"]["messages"]["payload_bytes"] > 0
    assert stats["state_db"]["tables"]["run_events"]["payload_bytes"] > 0
    assert stats["gateway_db"]["tables"]["gateway_artifacts"]["rows"] == 1
    assert stats["gateway_db"]["tables"]["gateway_session_artifacts"]["rows"] == 1
    assert stats["directories"]["logs"]["file_count"] == 1
    assert stats["directories"]["logs"]["size_bytes"] >= len("worker log\n")
    assert stats["workspace_files"]["registered_count"] == 1
    assert stats["workspace_files"]["registered_size_bytes"] == artifact_path.stat().st_size
    assert stats["workspace_files"]["physical_size_bytes"] == artifact_path.stat().st_size
    assert stats["summary"]["message_rows"] == 2
    assert stats["summary"]["run_event_rows"] == 1
    assert stats["summary"]["run_event_search_index_rows"] == 1
    assert stats["summary"]["session_runtime_state_rows"] == 0
    assert stats["summary"]["tool_events_rows"] == 0
    assert stats["summary"]["artifact_metadata_rows"] == 1
    assert stats["summary"]["physical_registered_workspace_file_bytes"] == artifact_path.stat().st_size


def test_collect_storage_stats_can_skip_registered_file_stat(tmp_path):
    hermes_home = tmp_path / "hermes-home"
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True)
    artifact_path = workspace / "report.md"
    artifact_path.write_text("# report\n", encoding="utf-8")
    gateway_store = GatewayStateStore(hermes_home / "tui-gateway" / "state.db")
    gateway_store.upsert_workspace({
        "id": "workspace-1",
        "name": "workspace",
        "path": str(workspace),
        "kind": "local",
    })
    gateway_store.upsert_artifact(
        artifact={
            "id": "artifact-1",
            "workspace_id": "workspace-1",
            "path": str(artifact_path),
            "relative_path": "report.md",
            "title": "report.md",
            "mime_type": "text/markdown",
            "size_bytes": artifact_path.stat().st_size,
            "origin": {},
        },
        session_id="session-1",
    )

    stats = collect_storage_stats(
        hermes_home=hermes_home,
        include_registered_file_sizes=False,
    )

    assert stats["workspace_files"]["registered_count"] == 1
    assert stats["workspace_files"]["inspected_count"] == 0
    assert stats["workspace_files"]["skipped_count"] == 1
    assert stats["workspace_files"]["physical_size_bytes"] == 0
