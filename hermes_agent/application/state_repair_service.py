"""Explicit recovery service for malformed Hermes SQLite state databases.

The normal composition root owns schema creation and migrations. This module
is separate because malformed ``sqlite_master`` data or corrupted FTS indexes
can prevent that composition root from opening. Recovery proceeds from least
to most invasive and never mutates canonical session or message rows.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
import logging
from pathlib import Path
import shutil
import sqlite3
import time
from typing import Any


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class StateRepairReport:
    repaired: bool
    strategy: str | None = None
    backup_path: str | None = None
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def probe_state_database(db_path: Path) -> str | None:
    """Return ``None`` for a healthy database, otherwise a concise reason."""
    connection = sqlite3.connect(str(db_path), isolation_level=None)
    try:
        connection.execute("PRAGMA journal_mode").fetchone()
        rows = connection.execute("PRAGMA integrity_check").fetchall()
        problems = [str(row[0]) for row in rows if row and str(row[0]).lower() != "ok"]
        if problems:
            return "; ".join(problems[:3])
        connection.execute("SELECT COUNT(*) FROM sessions").fetchone()
        return _probe_fts_write(connection)
    except sqlite3.DatabaseError as exc:
        return str(exc)
    finally:
        connection.close()


def _probe_fts_write(connection: sqlite3.Connection) -> str | None:
    probe_id = f"_hermes_fts_health_probe_{time.time_ns()}"
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "INSERT INTO sessions (id, source, started_at) VALUES (?, ?, ?)",
            (probe_id, "_health_probe", time.time()),
        )
        connection.execute(
            "INSERT INTO messages (session_id, role, content, timestamp) "
            "VALUES (?, ?, ?, ?)",
            (probe_id, "user", "_fts_health_probe", time.time()),
        )
        connection.execute("ROLLBACK")
    except sqlite3.OperationalError as exc:
        _rollback_quietly(connection)
        message = str(exc).lower()
        if "no such table" in message or "no such column" in message:
            return None
        return str(exc)
    return None


def _rollback_quietly(connection: sqlite3.Connection) -> None:
    try:
        connection.execute("ROLLBACK")
    except sqlite3.Error:
        pass


def _backup_database(db_path: Path) -> Path | None:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = db_path.with_name(f"{db_path.name}.malformed-backup-{stamp}")
    try:
        shutil.copy2(db_path, backup_path)
        for suffix in ("-wal", "-shm"):
            sidecar = db_path.with_name(db_path.name + suffix)
            if sidecar.exists():
                shutil.copy2(
                    sidecar,
                    backup_path.with_name(backup_path.name + suffix),
                )
    except OSError as exc:
        logger.warning("Could not back up malformed DB %s: %s", db_path, exc)
        return None
    return backup_path


def repair_state_database(db_path: Path, *, backup: bool = True) -> StateRepairReport:
    """Repair FTS or duplicated-schema corruption without touching base rows."""
    path = Path(db_path)
    if not path.exists():
        return StateRepairReport(False, error=f"{path} does not exist")
    if probe_state_database(path) is None:
        return StateRepairReport(True, strategy="already_healthy")

    backup_path = _backup_database(path) if backup else None
    rendered_backup = str(backup_path) if backup_path else None

    if _rebuild_fts(path) and probe_state_database(path) is None:
        return StateRepairReport(True, "rebuild_fts", rendered_backup)
    if _deduplicate_schema(path) and probe_state_database(path) is None:
        return StateRepairReport(True, "dedup_schema", rendered_backup)
    if _drop_fts_schema(path):
        reason = probe_state_database(path)
        if reason is None:
            return StateRepairReport(True, "drop_fts_rebuild", rendered_backup)
    else:
        reason = probe_state_database(path)

    return StateRepairReport(
        False,
        backup_path=rendered_backup,
        error=reason or "automatic recovery failed",
    )


def _rebuild_fts(db_path: Path) -> bool:
    try:
        connection = sqlite3.connect(str(db_path), isolation_level=None)
        try:
            for table_name in ("messages_fts", "messages_fts_trigram"):
                try:
                    connection.execute(
                        f"INSERT INTO {table_name}({table_name}) VALUES('rebuild')"
                    )
                except sqlite3.OperationalError:
                    continue
        finally:
            connection.close()
        return True
    except sqlite3.DatabaseError as exc:
        logger.warning("state database FTS rebuild failed: %s", exc)
        return False


def _deduplicate_schema(db_path: Path) -> bool:
    try:
        connection = sqlite3.connect(str(db_path), isolation_level=None)
        try:
            connection.execute("PRAGMA writable_schema=ON")
            duplicates = connection.execute(
                "SELECT type, name, COUNT(*) AS count, MIN(rowid) AS keep "
                "FROM sqlite_master GROUP BY type, name HAVING count > 1"
            ).fetchall()
            for object_type, name, _count, keep in duplicates:
                connection.execute(
                    "DELETE FROM sqlite_master "
                    "WHERE type IS ? AND name IS ? AND rowid <> ?",
                    (object_type, name, keep),
                )
            connection.execute("PRAGMA writable_schema=OFF")
            connection.commit()
        finally:
            connection.close()
        return True
    except sqlite3.DatabaseError as exc:
        logger.warning("state database schema de-duplication failed: %s", exc)
        return False


def _drop_fts_schema(db_path: Path) -> bool:
    try:
        connection = sqlite3.connect(str(db_path), isolation_level=None)
        try:
            connection.execute("PRAGMA writable_schema=ON")
            connection.execute(
                "DELETE FROM sqlite_master WHERE name LIKE 'messages_fts%'"
            )
            connection.execute("PRAGMA writable_schema=OFF")
            connection.commit()
            connection.execute("VACUUM")
        finally:
            connection.close()
        return True
    except sqlite3.DatabaseError as exc:
        logger.warning("state database FTS schema reset failed: %s", exc)
        return False


__all__ = [
    "StateRepairReport",
    "probe_state_database",
    "repair_state_database",
]
