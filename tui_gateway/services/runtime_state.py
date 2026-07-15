"""Hermes-owned runtime state maintenance.

Desktop clients must not inspect or mutate Hermes SQLite files directly. This
module owns the small set of maintenance operations that still need filesystem
visibility over Hermes runtime homes.
"""

from __future__ import annotations

import hashlib
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from hermes_agent.composition.cli_session_store import open_cli_session_store
from hermes_constants import get_hermes_home
from hermes_profile_dir import resolve_default_agent_dir

_DB_SPECS = (
    {
        "kind": "state",
        "relative_path": "state.db",
        "tables": ("sessions", "runs", "messages", "run_events"),
    },
    {
        "kind": "gateway",
        "relative_path": str(Path("tui-gateway") / "state.db"),
        "tables": (
            "gateway_workspaces",
            "gateway_session_workspaces",
            "gateway_artifacts",
            "gateway_session_artifacts",
        ),
    },
    {
        "kind": "kanban",
        "relative_path": "kanban.db",
        "tables": ("tasks", "task_runs", "task_events", "task_comments", "task_links", "kanban_notify_subs"),
    },
)


def _text(value: Any = "") -> str:
    return str(value or "").strip()


def _path(value: Any) -> Path:
    return Path(str(value or "")).expanduser().resolve()


def _is_explicit_false(value: Any) -> bool:
    if value is False:
        return True
    return str(value).strip().lower() in {"false", "0", "no"}


def _safe_child(parent: Path, child: Path) -> bool:
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return child == parent


def _runtime_homes(hermes_home_root: Path) -> list[dict[str, str]]:
    root = hermes_home_root.resolve()
    homes: dict[str, dict[str, str]] = {}

    def add(home_path: Path, scope_kind: str, scope_id: str) -> None:
        normalized = home_path.resolve()
        key = str(normalized)
        if key in homes or not normalized.exists():
            return
        homes[key] = {
            "homePath": key,
            "home_path": key,
            "scopeKind": scope_kind,
            "scope_kind": scope_kind,
            "scopeId": scope_id,
            "scope_id": scope_id,
        }

    add(resolve_default_agent_dir(root), "default", "agent-default")
    for parent, scope_kind in (
        (root / "profiles", "profile"),
        (root / ".dovie" / "versions", "legacy_profile_version"),
        (root / "drafts", "draft"),
    ):
        if not parent.exists():
            continue
        for child in sorted(parent.iterdir(), key=lambda item: str(item)):
            if child.is_dir():
                add(child, scope_kind, child.name)
    return sorted(homes.values(), key=lambda item: item["homePath"])


def _file_state(file_path: Path) -> dict[str, Any]:
    try:
        stat = file_path.stat()
        modified_at = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat().replace("+00:00", "Z")
        return {
            "exists": True,
            "sizeBytes": int(stat.st_size),
            "size_bytes": int(stat.st_size),
            "modifiedAt": modified_at,
            "modified_at": modified_at,
        }
    except FileNotFoundError:
        return {
            "exists": False,
            "sizeBytes": 0,
            "size_bytes": 0,
            "modifiedAt": "",
            "modified_at": "",
        }


def _table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type IN ('table', 'view') AND name = ? LIMIT 1",
        (table_name,),
    ).fetchone()
    return bool(row)


def _count_table_rows(conn: sqlite3.Connection, table_name: str) -> int:
    if not _table_exists(conn, table_name):
        return 0
    quoted = '"' + str(table_name).replace('"', '""') + '"'
    row = conn.execute(f"SELECT COUNT(*) AS count FROM {quoted}").fetchone()
    return int(row["count"] if row else 0)


def _inspect_database(home: dict[str, str], spec: dict[str, Any]) -> dict[str, Any]:
    db_path = Path(home["homePath"]) / str(spec["relative_path"])
    sidecar_paths = [Path(f"{db_path}-wal"), Path(f"{db_path}-shm")]
    stat = _file_state(db_path)
    sidecars = [
        {
            "path": str(sidecar),
            **_file_state(sidecar),
        }
        for sidecar in sidecar_paths
    ]
    base = {
        "kind": spec["kind"],
        "path": str(db_path),
        "relativePath": spec["relative_path"],
        "relative_path": spec["relative_path"],
        **stat,
        "sidecars": sidecars,
        "counts": {table: 0 for table in spec["tables"]},
        "totalRows": 0,
        "total_rows": 0,
        "empty": False,
        "error": "",
    }
    if not stat["exists"]:
        return base
    try:
        conn = sqlite3.connect(str(db_path), timeout=1.0)
        conn.row_factory = sqlite3.Row
        try:
            counts = {table: _count_table_rows(conn, table) for table in spec["tables"]}
        finally:
            conn.close()
        total_rows = sum(int(value or 0) for value in counts.values())
        return {
            **base,
            "counts": counts,
            "totalRows": total_rows,
            "total_rows": total_rows,
            "empty": total_rows == 0,
        }
    except Exception as exc:
        return {**base, "error": str(exc)}


def inspect_runtime_state(*, hermes_home_root: str | Path | None = None) -> dict[str, Any]:
    root = _path(hermes_home_root or get_hermes_home())
    runtime_homes = []
    for home in _runtime_homes(root):
        databases = [_inspect_database(home, spec) for spec in _DB_SPECS]
        total_size = sum(
            int(db.get("sizeBytes") or 0)
            + sum(int(sidecar.get("sizeBytes") or 0) for sidecar in db.get("sidecars") or [])
            for db in databases
        )
        runtime_homes.append({
            **home,
            "databases": databases,
            "existingDatabaseCount": sum(1 for db in databases if db.get("exists")),
            "existing_database_count": sum(1 for db in databases if db.get("exists")),
            "emptyDatabaseCount": sum(1 for db in databases if db.get("exists") and db.get("empty")),
            "empty_database_count": sum(1 for db in databases if db.get("exists") and db.get("empty")),
            "totalSizeBytes": total_size,
            "total_size_bytes": total_size,
        })
    return {
        "hermesHomeRoot": str(root),
        "hermes_home_root": str(root),
        "runtimeHomes": runtime_homes,
        "runtime_homes": runtime_homes,
        "summary": {
            "runtimeHomeCount": len(runtime_homes),
            "runtime_home_count": len(runtime_homes),
            "existingDatabaseCount": sum(home["existingDatabaseCount"] for home in runtime_homes),
            "existing_database_count": sum(home["existingDatabaseCount"] for home in runtime_homes),
            "emptyDatabaseCount": sum(home["emptyDatabaseCount"] for home in runtime_homes),
            "empty_database_count": sum(home["emptyDatabaseCount"] for home in runtime_homes),
            "totalSizeBytes": sum(home["totalSizeBytes"] for home in runtime_homes),
            "total_size_bytes": sum(home["totalSizeBytes"] for home in runtime_homes),
        },
    }


def prune_empty_runtime_state(*, hermes_home_root: str | Path | None = None, dry_run: Any = True) -> dict[str, Any]:
    root = _path(hermes_home_root or get_hermes_home())
    normalized_dry_run = not _is_explicit_false(dry_run)
    report = inspect_runtime_state(hermes_home_root=root)
    pruned: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    for home in report["runtimeHomes"]:
        for database in home.get("databases") or []:
            if not database.get("exists") or not database.get("empty") or database.get("error"):
                continue
            targets = [Path(database["path"]), *[Path(sidecar["path"]) for sidecar in database.get("sidecars") or [] if sidecar.get("exists")]]
            entry = {
                "homePath": home["homePath"],
                "home_path": home["homePath"],
                "scopeKind": home["scopeKind"],
                "scope_kind": home["scopeKind"],
                "scopeId": home["scopeId"],
                "scope_id": home["scopeId"],
                "kind": database["kind"],
                "path": database["path"],
                "sidecars": [str(target) for target in targets[1:]],
                "dryRun": normalized_dry_run,
                "dry_run": normalized_dry_run,
            }
            if not normalized_dry_run:
                try:
                    for target in targets:
                        target.unlink(missing_ok=True)
                except Exception as exc:
                    errors.append({**entry, "error": str(exc)})
                    continue
            pruned.append(entry)
    return {
        **report,
        "dryRun": normalized_dry_run,
        "dry_run": normalized_dry_run,
        "pruned": pruned,
        "errors": errors,
        "summary": {
            **report["summary"],
            "prunedCount": len(pruned),
            "pruned_count": len(pruned),
            "errorCount": len(errors),
            "error_count": len(errors),
        },
    }


def merge_profile_runtime_state(*, hermes_home_root: str | Path | None = None, profiles_root: str | Path | None = None) -> dict[str, Any]:
    root = _path(hermes_home_root or get_hermes_home())
    source_root = _path(profiles_root or root / "profiles")
    target_path = root / "state.db"
    migrated: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    if not target_path.exists() or not source_root.exists():
        return {"migrated": migrated, "skipped": skipped, "targetPath": str(target_path), "target_path": str(target_path)}
    for child in sorted(source_root.iterdir(), key=lambda item: str(item)):
        if not child.is_dir() or child.name.startswith("."):
            continue
        source_path = child / "state.db"
        if not source_path.exists() or source_path.resolve() == target_path.resolve():
            continue
        try:
            result = _merge_sqlite_state_database(source_path, target_path)
            if any(int(result.get(key) or 0) > 0 for key in ("sessionsInserted", "messagesInserted", "runsInserted", "runEventsInserted")):
                migrated.append({"from": str(source_path), "to": str(target_path), "source": "profile_runtime_state", **result})
        except Exception as exc:
            skipped.append({"from": str(source_path), "to": str(target_path), "reason": str(exc)})
    return {"migrated": migrated, "skipped": skipped, "targetPath": str(target_path), "target_path": str(target_path)}


def _quote_identifier(value: str) -> str:
    return '"' + str(value).replace('"', '""') + '"'


def _table_exists_in_schema(conn: sqlite3.Connection, schema: str, table_name: str) -> bool:
    row = conn.execute(
        f"SELECT name FROM {_quote_identifier(schema)}.sqlite_master WHERE type = 'table' AND name = ? LIMIT 1",
        (table_name,),
    ).fetchone()
    return bool(row)


def _table_column_details(conn: sqlite3.Connection, schema: str, table_name: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        f"PRAGMA {_quote_identifier(schema)}.table_info({_quote_identifier(table_name)})",
    ).fetchall()
    return [
        {
            "name": str(row["name"] or ""),
            "type": str(row["type"] or ""),
            "pk": int(row["pk"] or 0),
        }
        for row in rows
        if str(row["name"] or "")
    ]


def _common_columns(source_columns: list[dict[str, Any]], target_columns: list[dict[str, Any]], exclude: set[str] | None = None) -> list[str]:
    excluded = exclude or set()
    target_names = {str(column.get("name") or "") for column in target_columns}
    return [
        str(column.get("name") or "")
        for column in source_columns
        if column.get("name") and column.get("name") not in excluded and column.get("name") in target_names
    ]


def _has_integer_primary_key(columns: list[dict[str, Any]], column_name: str) -> bool:
    for column in columns:
        if column.get("name") == column_name and int(column.get("pk") or 0) > 0:
            return "INT" in str(column.get("type") or "").upper()
    return False


def _source_session_select_expression(column_name: str) -> str:
    source = f"legacy.{_quote_identifier('sessions')}.{_quote_identifier(column_name)}"
    if column_name != "title":
        return source
    return f"""
    CASE
      WHEN {source} IS NOT NULL
        AND EXISTS (
          SELECT 1
          FROM main.{_quote_identifier('sessions')} existing
          WHERE existing.title = {source}
        )
      THEN NULL
      ELSE {source}
    END
    """


def _insert_rows_for_missing_sessions(
    conn: sqlite3.Connection,
    *,
    table_name: str,
    missing_temp_table: str,
    order_columns: list[str],
    or_ignore: bool = True,
) -> int:
    if not _table_exists_in_schema(conn, "legacy", table_name) or not _table_exists_in_schema(conn, "main", table_name):
        return 0
    source_columns = _table_column_details(conn, "legacy", table_name)
    target_columns = _table_column_details(conn, "main", table_name)
    excluded = {"id"} if _has_integer_primary_key(target_columns, "id") else set()
    columns = _common_columns(source_columns, target_columns, excluded)
    if "session_id" not in columns:
        return 0
    quoted_table = _quote_identifier(table_name)
    columns_sql = ", ".join(_quote_identifier(column) for column in columns)
    select_sql = ", ".join(f"source.{_quote_identifier(column)}" for column in columns)
    order_sql = (
        "ORDER BY " + ", ".join(f"source.{_quote_identifier(column)}" for column in order_columns)
        if order_columns
        else ""
    )
    before = conn.execute(f"SELECT COUNT(*) AS count FROM main.{quoted_table}").fetchone()["count"]
    conn.execute(
        f"""
        INSERT {"OR IGNORE " if or_ignore else ""}INTO main.{quoted_table} ({columns_sql})
        SELECT {select_sql}
        FROM legacy.{quoted_table} source
        JOIN temp.{_quote_identifier(missing_temp_table)} missing
          ON missing.id = source.session_id
        {order_sql}
        """
    )
    after = conn.execute(f"SELECT COUNT(*) AS count FROM main.{quoted_table}").fetchone()["count"]
    return max(0, int(after or 0) - int(before or 0))


def _merge_sqlite_state_database(source_path: Path, target_path: Path) -> dict[str, int]:
    zero = {"sessionsInserted": 0, "messagesInserted": 0, "runsInserted": 0, "runEventsInserted": 0}
    if not source_path.exists() or not target_path.exists():
        return {**zero, **{key[0].lower() + key[1:]: value for key, value in zero.items()}}
    conn = sqlite3.connect(str(target_path), timeout=5.0)
    conn.row_factory = sqlite3.Row
    missing_temp_table = f"hermes_missing_profile_sessions_{hashlib.sha256(str(source_path).encode()).hexdigest()[:12]}"
    attached = False
    try:
        conn.execute("ATTACH DATABASE ? AS legacy", (str(source_path),))
        attached = True
        if not _table_exists_in_schema(conn, "legacy", "sessions") or not _table_exists_in_schema(conn, "main", "sessions"):
            return {**zero, **{key[0].lower() + key[1:]: value for key, value in zero.items()}}

        conn.execute(f"CREATE TEMP TABLE {_quote_identifier(missing_temp_table)} (id TEXT PRIMARY KEY)")
        conn.execute(
            f"""
            INSERT OR IGNORE INTO temp.{_quote_identifier(missing_temp_table)} (id)
            SELECT legacy_sessions.id
            FROM legacy.{_quote_identifier('sessions')} legacy_sessions
            LEFT JOIN main.{_quote_identifier('sessions')} target_sessions
              ON target_sessions.id = legacy_sessions.id
            WHERE legacy_sessions.id IS NOT NULL
              AND target_sessions.id IS NULL
            """
        )
        missing_count = conn.execute(f"SELECT COUNT(*) AS count FROM temp.{_quote_identifier(missing_temp_table)}").fetchone()["count"]
        if int(missing_count or 0) == 0:
            return {**zero, **{key[0].lower() + key[1:]: value for key, value in zero.items()}}

        source_session_columns = _table_column_details(conn, "legacy", "sessions")
        target_session_columns = _table_column_details(conn, "main", "sessions")
        session_columns = _common_columns(source_session_columns, target_session_columns)
        if not session_columns:
            return {**zero, **{key[0].lower() + key[1:]: value for key, value in zero.items()}}

        session_columns_sql = ", ".join(_quote_identifier(column) for column in session_columns)
        session_select_sql = ", ".join(_source_session_select_expression(column) for column in session_columns)
        before_sessions = conn.execute(f"SELECT COUNT(*) AS count FROM main.{_quote_identifier('sessions')}").fetchone()["count"]
        conn.execute(
            f"""
            INSERT OR IGNORE INTO main.{_quote_identifier('sessions')} ({session_columns_sql})
            SELECT {session_select_sql}
            FROM legacy.{_quote_identifier('sessions')}
            JOIN temp.{_quote_identifier(missing_temp_table)} missing
              ON missing.id = legacy.{_quote_identifier('sessions')}.id
            ORDER BY legacy.{_quote_identifier('sessions')}.started_at ASC
            """
        )
        after_sessions = conn.execute(f"SELECT COUNT(*) AS count FROM main.{_quote_identifier('sessions')}").fetchone()["count"]
        sessions_inserted = max(0, int(after_sessions or 0) - int(before_sessions or 0))
        messages_inserted = _insert_rows_for_missing_sessions(
            conn,
            table_name="messages",
            missing_temp_table=missing_temp_table,
            order_columns=["session_id", "timestamp", "id"],
            or_ignore=False,
        )
        runs_inserted = _insert_rows_for_missing_sessions(
            conn,
            table_name="runs",
            missing_temp_table=missing_temp_table,
            order_columns=["started_at", "run_id"],
        )
        run_events_inserted = _insert_rows_for_missing_sessions(
            conn,
            table_name="run_events",
            missing_temp_table=missing_temp_table,
            order_columns=["session_id", "seq", "id"],
        )
        if messages_inserted > 0 and any(column.get("name") == "message_count" for column in target_session_columns):
            conn.execute(
                f"""
                UPDATE main.{_quote_identifier('sessions')}
                SET message_count = (
                  SELECT COUNT(*)
                  FROM main.{_quote_identifier('messages')} messages
                  WHERE messages.session_id = main.{_quote_identifier('sessions')}.id
                )
                WHERE id IN (SELECT id FROM temp.{_quote_identifier(missing_temp_table)})
                """
            )
        conn.commit()
        inserted = {
            "sessionsInserted": sessions_inserted,
            "messagesInserted": messages_inserted,
            "runsInserted": runs_inserted,
            "runEventsInserted": run_events_inserted,
        }
        return {**inserted, **{key[0].lower() + key[1:]: value for key, value in inserted.items()}}
    finally:
        try:
            conn.execute(f"DROP TABLE IF EXISTS temp.{_quote_identifier(missing_temp_table)}")
        except Exception:
            pass
        if attached:
            try:
                conn.execute("DETACH DATABASE legacy")
            except Exception:
                pass
        conn.close()


def profile_runtime_session_exists(
    *,
    session_id: str,
    hermes_home_path: str | Path | None = None,
    runtime_scope_key: str = "",
    hermes_home_root: str | Path | None = None,
) -> dict[str, Any]:
    session_id = _text(session_id)
    if not session_id:
        raise ValueError("session_id required")
    root = _path(hermes_home_root or get_hermes_home())
    home = _resolve_profile_runtime_home(root=root, hermes_home_path=hermes_home_path, runtime_scope_key=runtime_scope_key)
    db_path = home / "state.db"
    if not db_path.exists():
        return {"exists": False, "homePath": str(home), "home_path": str(home), "dbPath": str(db_path), "db_path": str(db_path)}
    try:
        conn = sqlite3.connect(str(db_path), timeout=1.0)
        try:
            row = conn.execute("SELECT id FROM sessions WHERE id = ? LIMIT 1", (session_id,)).fetchone()
        finally:
            conn.close()
        return {"exists": bool(row), "homePath": str(home), "home_path": str(home), "dbPath": str(db_path), "db_path": str(db_path)}
    except Exception as exc:
        return {"exists": False, "homePath": str(home), "home_path": str(home), "dbPath": str(db_path), "db_path": str(db_path), "error": str(exc)}


def _resolve_profile_runtime_home(*, root: Path, hermes_home_path: str | Path | None, runtime_scope_key: str = "") -> Path:
    if hermes_home_path:
        candidate = _path(hermes_home_path)
        allowed_roots = (
            root,
            root / "profiles",
            root / ".dovie" / "versions",
            root / "drafts",
        )
        if not any(_safe_child(allowed.resolve(), candidate) for allowed in allowed_roots if allowed.exists() or allowed == root):
            raise PermissionError("profile runtime home must be inside Hermes runtime roots")
        return candidate
    scope = _text(runtime_scope_key)
    if scope.startswith("profile:"):
        if scope.split(":", 1)[1] in {"agent-default", "default"}:
            return resolve_default_agent_dir(root)
        return root / "profiles" / scope.split(":", 1)[1]
    if scope.startswith("draft:"):
        return root / "drafts" / scope.split(":", 1)[1]
    return resolve_default_agent_dir(root)


def rebase_team_mission_workspace_paths(*, old_path: str, new_path: str, db: Any | None = None) -> dict[str, Any]:
    old_path = _text(old_path)
    new_path = _text(new_path)
    if not old_path or not new_path or old_path == new_path:
        return {"updates": {}, "changed": 0}
    active_db = db
    close_db = False
    if active_db is None:
        active_db = open_cli_session_store()
        close_db = True
    try:
        updates = active_db.team_mission_maintenance.rebase_workspace_paths(
            old_path,
            new_path,
        )
        return {"updates": updates, "changed": sum(updates.values())}
    finally:
        if close_db and hasattr(active_db, "close"):
            active_db.close()
