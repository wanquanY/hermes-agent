"""Merge profile-local Hermes state databases into the root state.db.

This module intentionally uses raw sqlite3 only.  It must be runnable before
the normal SessionDB lifecycle starts and must not import hermes_state.SessionDB.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import shutil
import sqlite3
import sys
import tempfile
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional


_SOURCE_ALIAS = "source"

# Merge coverage is discovered dynamically.  This priority list only gives
# parent/control tables a stable order before dependent operational rows.
_TABLE_PRIORITY = (
    "sessions",
    "team_missions",
    "team_mission_conversations",
    "conversation_missions",
    "conversation_participants",
    "session_index",
    "messages",
    "runs",
    "run_events",
    "agent_profile_drafts",
)


@dataclasses.dataclass
class MergeResult:
    total_profile_dbs: int
    merged_rows_per_table: dict[str, int]
    skipped_dbs: list[str]
    errors: list[str]
    backups: list[str]

    def to_json_dict(self) -> dict[str, object]:
        return {
            "status": "ok",
            "total_dbs": self.total_profile_dbs,
            "merged_rows": dict(sorted(self.merged_rows_per_table.items())),
            "backups": list(self.backups),
            "skipped_dbs": list(self.skipped_dbs),
            "errors": list(self.errors),
        }


def merge_profile_dbs(
    hermes_home_root: Path,
    *,
    backup: bool = True,
    progress: Optional[Callable[[str], None]] = None,
    dry_run: bool = False,
) -> MergeResult:
    root = Path(hermes_home_root)
    profile_dbs = _discover_profile_dbs(root)
    total = len(profile_dbs)
    merged_rows: defaultdict[str, int] = defaultdict(int)
    skipped_dbs: list[str] = []
    errors: list[str] = []
    backups: list[str] = []

    root_db_path = root / "state.db"
    if not profile_dbs:
        return MergeResult(
            total_profile_dbs=0,
            merged_rows_per_table={},
            skipped_dbs=[],
            errors=[],
            backups=[],
        )
    if not root_db_path.exists():
        message = f"root state.db does not exist: {root_db_path}"
        return MergeResult(
            total_profile_dbs=total,
            merged_rows_per_table={},
            skipped_dbs=[str(path) for path in profile_dbs],
            errors=[message],
            backups=[],
        )

    conn = sqlite3.connect(str(root_db_path))
    try:
        conn.execute("PRAGMA foreign_keys=OFF")
        for index, profile_db in enumerate(profile_dbs, start=1):
            if progress:
                progress(
                    f"merging {_display_path(profile_db, root)} ({index}/{total})"
                )
            snapshot_dir: tempfile.TemporaryDirectory[str] | None = None
            snapshots: list[tuple[Path, Path]] = []
            try:
                if backup and not dry_run:
                    snapshot_dir = tempfile.TemporaryDirectory(
                        prefix="hermes-merge-profile-db-"
                    )
                    snapshots = _snapshot_db_family(
                        profile_db, Path(snapshot_dir.name)
                    )
                row_counts = _merge_one_profile_db(
                    conn,
                    root_db_path=root_db_path,
                    profile_db_path=profile_db,
                    dry_run=dry_run,
                )
            except Exception as exc:  # noqa: BLE001 - isolate one bad profile DB.
                _safe_rollback(conn)
                _safe_detach(conn)
                if snapshot_dir is not None:
                    snapshot_dir.cleanup()
                skipped_dbs.append(str(profile_db))
                errors.append(f"{profile_db}: {exc}")
                continue
            for table_name, count in row_counts.items():
                merged_rows[table_name] += count
            if backup and not dry_run:
                backups.extend(
                    str(path) for path in _materialize_snapshots(snapshots)
                )
            if snapshot_dir is not None:
                snapshot_dir.cleanup()
    finally:
        conn.close()

    return MergeResult(
        total_profile_dbs=total,
        merged_rows_per_table=dict(merged_rows),
        skipped_dbs=skipped_dbs,
        errors=errors,
        backups=backups,
    )


def _discover_profile_dbs(root: Path) -> list[Path]:
    candidates: list[Path] = []
    for parent in (root / "profiles", root / "drafts"):
        candidates.extend(parent.glob("*/state.db"))
    return sorted(path for path in candidates if path.is_file())


def _merge_one_profile_db(
    conn: sqlite3.Connection,
    *,
    root_db_path: Path,
    profile_db_path: Path,
    dry_run: bool,
) -> dict[str, int]:
    if profile_db_path.resolve() == root_db_path.resolve():
        return {}

    conn.execute(
        "ATTACH DATABASE "
        f"{_quote_sql_literal(str(profile_db_path))} AS "
        f"{_quote_identifier(_SOURCE_ALIAS)}"
    )
    try:
        root_tables = _ordinary_tables(conn, "main")
        source_tables = _ordinary_tables(conn, _SOURCE_ALIAS)
        table_names = _sort_tables(root_tables & source_tables)
        if not table_names:
            _safe_detach(conn)
            return {}

        conn.execute("BEGIN")
        row_counts: dict[str, int] = {}
        for table_name in table_names:
            columns = _common_insertable_columns(conn, table_name)
            if not columns or not _has_conflict_target(conn, "main", table_name):
                continue
            inserted = _insert_or_ignore_table(conn, table_name, columns)
            if inserted:
                row_counts[table_name] = inserted
        if dry_run:
            conn.rollback()
        else:
            conn.commit()
        return row_counts
    except Exception:
        _safe_rollback(conn)
        raise
    finally:
        _safe_detach(conn)


def _ordinary_tables(conn: sqlite3.Connection, schema: str) -> set[str]:
    quoted_schema = _quote_identifier(schema)
    try:
        rows = conn.execute(f"PRAGMA {quoted_schema}.table_list").fetchall()
    except sqlite3.OperationalError as exc:
        message = str(exc).lower()
        if "table_list" not in message and "syntax" not in message:
            raise
        rows = []

    if rows:
        return {
            str(row[1])
            for row in rows
            if str(row[2]).lower() == "table"
            and not str(row[1]).startswith("sqlite_")
        }

    rows = conn.execute(
        f"""
        SELECT name, COALESCE(sql, '') AS sql
          FROM {quoted_schema}.sqlite_master
         WHERE type = 'table'
        """
    ).fetchall()
    return {
        str(name)
        for name, sql in rows
        if not str(name).startswith("sqlite_")
        and not str(sql).lstrip().upper().startswith("CREATE VIRTUAL TABLE")
    }


def _sort_tables(table_names: set[str]) -> list[str]:
    priority = {name: index for index, name in enumerate(_TABLE_PRIORITY)}
    return sorted(
        table_names,
        key=lambda name: (priority.get(name, len(priority)), name),
    )


def _common_insertable_columns(conn: sqlite3.Connection, table_name: str) -> list[str]:
    root_columns = _insertable_columns(conn, "main", table_name)
    source_columns = set(_insertable_columns(conn, _SOURCE_ALIAS, table_name))
    return [column for column in root_columns if column in source_columns]


def _insertable_columns(
    conn: sqlite3.Connection, schema: str, table_name: str
) -> list[str]:
    rows = conn.execute(
        f"PRAGMA {_quote_identifier(schema)}.table_xinfo({_quote_sql_literal(table_name)})"
    ).fetchall()
    columns: list[str] = []
    for row in rows:
        hidden = int(row[6]) if len(row) > 6 and row[6] is not None else 0
        if hidden == 0:
            columns.append(str(row[1]))
    return columns


def _has_conflict_target(conn: sqlite3.Connection, schema: str, table_name: str) -> bool:
    rows = conn.execute(
        f"PRAGMA {_quote_identifier(schema)}.table_xinfo({_quote_sql_literal(table_name)})"
    ).fetchall()
    if any(int(row[5] or 0) > 0 for row in rows):
        return True

    index_rows = conn.execute(
        f"PRAGMA {_quote_identifier(schema)}.index_list({_quote_sql_literal(table_name)})"
    ).fetchall()
    return any(int(row[2] or 0) == 1 for row in index_rows)


def _insert_or_ignore_table(
    conn: sqlite3.Connection,
    table_name: str,
    columns: list[str],
) -> int:
    column_sql = ", ".join(_quote_identifier(column) for column in columns)
    table_sql = _quote_identifier(table_name)
    cursor = conn.execute(
        f"""
        INSERT OR IGNORE INTO main.{table_sql} ({column_sql})
        SELECT {column_sql}
          FROM {_quote_identifier(_SOURCE_ALIAS)}.{table_sql}
        """
    )
    return max(cursor.rowcount or 0, 0)


def _snapshot_db_family(db_path: Path, snapshot_dir: Path) -> list[tuple[Path, Path]]:
    snapshots: list[tuple[Path, Path]] = []
    for member in (db_path, Path(f"{db_path}-wal"), Path(f"{db_path}-shm")):
        if not member.exists():
            continue
        snapshot_path = snapshot_dir / member.name
        shutil.copy2(member, snapshot_path)
        snapshots.append((member, snapshot_path))
    return snapshots


def _materialize_snapshots(snapshots: list[tuple[Path, Path]]) -> list[Path]:
    timestamp = _timestamp()
    backups: list[Path] = []
    for original_path, snapshot_path in snapshots:
        backup_path = _unique_backup_path(original_path, timestamp)
        shutil.copy2(snapshot_path, backup_path)
        backups.append(backup_path)
    return backups


def _unique_backup_path(path: Path, timestamp: str) -> Path:
    candidate = path.with_name(f"{path.name}.merged-to-root.bak.{timestamp}")
    if not candidate.exists():
        return candidate
    counter = 1
    while True:
        numbered = path.with_name(
            f"{path.name}.merged-to-root.bak.{timestamp}.{counter}"
        )
        if not numbered.exists():
            return numbered
        counter += 1


def _timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")


def _safe_rollback(conn: sqlite3.Connection) -> None:
    try:
        if conn.in_transaction:
            conn.rollback()
    except sqlite3.Error:
        pass


def _safe_detach(conn: sqlite3.Connection) -> None:
    try:
        conn.execute(f"DETACH DATABASE {_quote_identifier(_SOURCE_ALIAS)}")
    except sqlite3.Error:
        pass


def _display_path(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def _quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _quote_sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Merge Hermes profile state.db files into root state.db."
    )
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--no-backup", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    try:
        result = merge_profile_dbs(
            args.root,
            backup=not args.no_backup,
            dry_run=args.dry_run,
        )
    except Exception as exc:  # noqa: BLE001 - CLI must return machine JSON.
        print(json.dumps({"status": "error", "error": str(exc)}), file=sys.stderr)
        return 1
    print(json.dumps(result.to_json_dict(), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
