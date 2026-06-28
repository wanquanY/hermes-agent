"""Read-only storage diagnostics for Hermes runtime state."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Any

from hermes_constants import get_hermes_home

STATE_DB_TABLES: tuple[str, ...] = (
    "sessions",
    "messages",
    "runs",
    "run_events",
    "run_event_search_index",
    "session_runtime_state",
    "tool_events",
    "run_event_archives",
)
GATEWAY_DB_TABLES: tuple[str, ...] = (
    "gateway_workspaces",
    "gateway_session_workspaces",
    "gateway_artifacts",
    "gateway_session_artifacts",
    "gateway_session_toolsets",
)
STATE_PAYLOAD_COLUMNS: dict[str, tuple[str, ...]] = {
    "sessions": (
        "model_config",
        "system_prompt",
        "preview",
        "title",
        "handoff_state",
        "handoff_error",
    ),
    "messages": (
        "content",
        "tool_calls",
        "reasoning",
        "reasoning_content",
        "reasoning_details",
        "codex_reasoning_items",
        "codex_message_items",
        "metadata_json",
    ),
    "runs": ("error", "metadata_json"),
    "run_events": (
        "payload_json",
        "event_json",
        "frame_blob",
        "frame_format",
        "retention_class",
        "projected_message_id",
        "projected_tool_event_id",
        "projection_state",
        "runtime_source_seq",
        "status",
    ),
    "run_event_search_index": (
        "runtime_source_seq",
        "search_text",
    ),
    "session_runtime_state": ("profile_json", "payload_hash", "status", "model", "provider"),
    "tool_events": (
        "arguments_json",
        "progress_json",
        "result_json",
        "result_text",
        "summary",
        "metadata_json",
    ),
    "run_event_archives": ("metadata_json", "reason"),
}
GATEWAY_PAYLOAD_COLUMNS: dict[str, tuple[str, ...]] = {
    "gateway_session_workspaces": ("cwd", "metadata_json"),
    "gateway_artifacts": (
        "path",
        "relative_path",
        "title",
        "mime_type",
        "origin_json",
    ),
    "gateway_session_toolsets": (
        "enabled_toolsets_json",
        "disabled_toolsets_json",
    ),
}
DIRECTORY_NAMES: tuple[str, ...] = ("logs", "traces")


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return default


def _file_stat(path: Path) -> dict[str, Any]:
    try:
        stat = path.stat()
    except OSError:
        return {
            "exists": False,
            "size_bytes": 0,
            "modified_at": 0,
        }
    return {
        "exists": True,
        "size_bytes": int(stat.st_size),
        "modified_at": float(stat.st_mtime),
    }


def _sidecar_stats(path: Path) -> list[dict[str, Any]]:
    return [
        {
            "path": str(sidecar),
            **_file_stat(sidecar),
        }
        for sidecar in (Path(f"{path}-wal"), Path(f"{path}-shm"))
    ]


def _connect_readonly(path: Path) -> sqlite3.Connection:
    uri = f"file:{path}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _quote_identifier(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def _table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type IN ('table', 'view') AND name = ? LIMIT 1",
        (table_name,),
    ).fetchone()
    return bool(row)


def _table_columns(conn: sqlite3.Connection, table_name: str) -> set[str]:
    if not _table_exists(conn, table_name):
        return set()
    return {str(row["name"]) for row in conn.execute(f"PRAGMA table_info({_quote_identifier(table_name)})")}


def _table_row_count(conn: sqlite3.Connection, table_name: str) -> int:
    if not _table_exists(conn, table_name):
        return 0
    row = conn.execute(f"SELECT COUNT(*) AS count FROM {_quote_identifier(table_name)}").fetchone()
    return _safe_int(row["count"] if row else 0)


def _table_payload_bytes(
    conn: sqlite3.Connection,
    table_name: str,
    payload_columns: tuple[str, ...],
) -> int:
    columns = _table_columns(conn, table_name)
    selected = [column for column in payload_columns if column in columns]
    if not selected:
        return 0
    expression = " + ".join(
        f"COALESCE(length({_quote_identifier(column)}), 0)"
        for column in selected
    )
    row = conn.execute(
        f"SELECT COALESCE(SUM({expression}), 0) AS size_bytes FROM {_quote_identifier(table_name)}"
    ).fetchone()
    return _safe_int(row["size_bytes"] if row else 0)


def _inspect_database(
    path: Path,
    *,
    kind: str,
    tables: tuple[str, ...],
    payload_columns: dict[str, tuple[str, ...]],
) -> dict[str, Any]:
    stat = _file_stat(path)
    sidecars = _sidecar_stats(path)
    result = {
        "kind": kind,
        "path": str(path),
        "exists": stat["exists"],
        "size_bytes": stat["size_bytes"],
        "sidecars": sidecars,
        "sidecar_size_bytes": sum(_safe_int(item.get("size_bytes")) for item in sidecars),
        "total_size_bytes": stat["size_bytes"] + sum(_safe_int(item.get("size_bytes")) for item in sidecars),
        "tables": {
            table: {"rows": 0, "payload_bytes": 0}
            for table in tables
        },
        "total_rows": 0,
        "payload_bytes": 0,
        "error": "",
    }
    if not stat["exists"]:
        return result

    try:
        with _connect_readonly(path) as conn:
            for table in tables:
                rows = _table_row_count(conn, table)
                payload_bytes = _table_payload_bytes(conn, table, payload_columns.get(table, ()))
                result["tables"][table] = {
                    "rows": rows,
                    "payload_bytes": payload_bytes,
                }
            result["total_rows"] = sum(
                _safe_int(table.get("rows"))
                for table in result["tables"].values()
            )
            result["payload_bytes"] = sum(
                _safe_int(table.get("payload_bytes"))
                for table in result["tables"].values()
            )
    except sqlite3.Error as exc:
        result["error"] = str(exc)
    return result


def _directory_usage(path: Path) -> dict[str, Any]:
    result = {
        "path": str(path),
        "exists": path.exists(),
        "size_bytes": 0,
        "file_count": 0,
        "directory_count": 0,
        "errors": [],
    }
    if not result["exists"]:
        return result

    for root, dirs, files in os.walk(path, followlinks=False):
        root_path = Path(root)
        try:
            result["size_bytes"] += root_path.lstat().st_size
            result["directory_count"] += 1
        except OSError as exc:
            result["errors"].append({"path": str(root_path), "error": str(exc)})
        for filename in files:
            file_path = root_path / filename
            try:
                result["size_bytes"] += file_path.lstat().st_size
                result["file_count"] += 1
            except OSError as exc:
                result["errors"].append({"path": str(file_path), "error": str(exc)})
        dirs.sort()
        files.sort()
    return result


def _registered_workspace_file_stats(
    gateway_db_path: Path,
    *,
    include_file_sizes: bool,
    file_limit: int,
) -> dict[str, Any]:
    result = {
        "registered_count": 0,
        "registered_size_bytes": 0,
        "inspected_count": 0,
        "skipped_count": 0,
        "existing_count": 0,
        "missing_count": 0,
        "physical_size_bytes": 0,
        "directory_count": 0,
        "errors": [],
    }
    if not gateway_db_path.exists():
        return result

    try:
        with _connect_readonly(gateway_db_path) as conn:
            if not _table_exists(conn, "gateway_artifacts"):
                return result
            summary = conn.execute(
                """
                SELECT COUNT(*) AS count,
                       COALESCE(SUM(size_bytes), 0) AS size_bytes
                FROM gateway_artifacts
                """
            ).fetchone()
            result["registered_count"] = _safe_int(summary["count"] if summary else 0)
            result["registered_size_bytes"] = _safe_int(summary["size_bytes"] if summary else 0)
            if not include_file_sizes:
                result["skipped_count"] = result["registered_count"]
                return result
            rows = conn.execute(
                """
                SELECT path
                FROM gateway_artifacts
                ORDER BY updated_at DESC, id DESC
                LIMIT ?
                """,
                (max(0, int(file_limit or 0)),),
            ).fetchall()
    except sqlite3.Error as exc:
        result["errors"].append({"path": str(gateway_db_path), "error": str(exc)})
        return result

    result["inspected_count"] = len(rows)
    result["skipped_count"] = max(0, result["registered_count"] - len(rows))
    for row in rows:
        file_path = Path(str(row["path"] or ""))
        try:
            stat = file_path.lstat()
        except OSError:
            result["missing_count"] += 1
            continue
        if file_path.is_dir():
            result["directory_count"] += 1
        result["existing_count"] += 1
        result["physical_size_bytes"] += int(stat.st_size)
    return result


def collect_storage_stats(
    *,
    hermes_home: str | Path | None = None,
    include_registered_file_sizes: bool = True,
    registered_file_limit: int = 10_000,
) -> dict[str, Any]:
    """Return read-only storage diagnostics for the current Hermes home."""
    home = Path(hermes_home or get_hermes_home()).expanduser()
    state_db = _inspect_database(
        home / "state.db",
        kind="state",
        tables=STATE_DB_TABLES,
        payload_columns=STATE_PAYLOAD_COLUMNS,
    )
    gateway_db = _inspect_database(
        home / "tui-gateway" / "state.db",
        kind="gateway",
        tables=GATEWAY_DB_TABLES,
        payload_columns=GATEWAY_PAYLOAD_COLUMNS,
    )
    directories = {
        name: _directory_usage(home / name)
        for name in DIRECTORY_NAMES
    }
    workspace_files = _registered_workspace_file_stats(
        home / "tui-gateway" / "state.db",
        include_file_sizes=include_registered_file_sizes,
        file_limit=registered_file_limit,
    )
    database_size_bytes = _safe_int(state_db.get("total_size_bytes")) + _safe_int(gateway_db.get("total_size_bytes"))
    logical_payload_bytes = _safe_int(state_db.get("payload_bytes")) + _safe_int(gateway_db.get("payload_bytes"))
    log_size_bytes = sum(_safe_int(item.get("size_bytes")) for item in directories.values())
    return {
        "hermes_home": str(home),
        "state_db": state_db,
        "gateway_db": gateway_db,
        "directories": directories,
        "workspace_files": workspace_files,
        "summary": {
            "database_size_bytes": database_size_bytes,
            "logical_payload_bytes": logical_payload_bytes,
            "message_rows": _safe_int(state_db["tables"]["messages"]["rows"]),
            "run_event_rows": _safe_int(state_db["tables"]["run_events"]["rows"]),
            "run_event_search_index_rows": _safe_int(
                state_db["tables"]["run_event_search_index"]["rows"]
            ),
            "session_runtime_state_rows": _safe_int(
                state_db["tables"]["session_runtime_state"]["rows"]
            ),
            "tool_events_rows": _safe_int(state_db["tables"]["tool_events"]["rows"]),
            "session_rows": _safe_int(state_db["tables"]["sessions"]["rows"]),
            "artifact_metadata_rows": _safe_int(gateway_db["tables"]["gateway_artifacts"]["rows"]),
            "artifact_link_rows": _safe_int(gateway_db["tables"]["gateway_session_artifacts"]["rows"]),
            "registered_workspace_file_bytes": _safe_int(workspace_files.get("registered_size_bytes")),
            "physical_registered_workspace_file_bytes": _safe_int(workspace_files.get("physical_size_bytes")),
            "log_size_bytes": log_size_bytes,
            "observed_size_bytes": (
                database_size_bytes
                + log_size_bytes
                + _safe_int(workspace_files.get("physical_size_bytes"))
            ),
        },
    }
