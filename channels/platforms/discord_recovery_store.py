"""Profile-pinned durable storage for Discord message recovery."""

from __future__ import annotations

import datetime as dt
import logging
import os
import sqlite3
import threading
from contextlib import suppress
from pathlib import Path
from typing import Any, Callable

from hermes_agent.storage.sqlite_wal import configure_sqlite_connection

logger = logging.getLogger(__name__)

_DB_FILENAME = "discord_message_recovery.db"
_RETENTION_DAYS = 30


class DiscordRecoveryStore:
    """Small SQLite ledger for completed and recoverable Discord messages."""

    def __init__(self, hermes_home: Path) -> None:
        self._lock = threading.Lock()
        self._initialized = False
        self._hermes_home = Path(hermes_home)

    def path(self) -> Path:
        directory = self._hermes_home / "gateway"
        directory.mkdir(parents=True, exist_ok=True)
        return directory / _DB_FILENAME

    def call(
        self,
        operation: Callable[[sqlite3.Connection], Any],
        default: Any = None,
    ) -> Any:
        try:
            with self._lock:
                path = self.path()
                connection = sqlite3.connect(path, timeout=0.1)
                try:
                    if not self._initialized:
                        self._initialize(connection)
                        self._initialized = True
                        with suppress(OSError):
                            os.chmod(path, 0o600)
                    result = operation(connection)
                    connection.commit()
                    return result
                finally:
                    connection.close()
        except Exception as error:
            logger.warning("Discord recovery ledger unavailable: %s", error)
            return default

    def _initialize(self, connection: sqlite3.Connection) -> None:
        configure_sqlite_connection(
            connection,
            db_label=str(self.path()),
            busy_timeout_ms=100,
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS discord_messages (
                message_id TEXT PRIMARY KEY,
                channel_id TEXT,
                thread_id TEXT,
                parent_channel_id TEXT,
                author_id TEXT,
                created_at TEXT,
                status TEXT NOT NULL,
                replied INTEGER NOT NULL DEFAULT 0,
                emoji_ack INTEGER NOT NULL DEFAULT 0,
                outage_response INTEGER NOT NULL DEFAULT 0,
                response_message_id TEXT,
                attempts INTEGER NOT NULL DEFAULT 0,
                last_attempt_at TEXT,
                last_error TEXT,
                updated_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS discord_recovery_scans (
                scan_id TEXT PRIMARY KEY,
                started_at TEXT NOT NULL,
                completed_at TEXT,
                status TEXT NOT NULL,
                channels TEXT NOT NULL,
                window_seconds REAL NOT NULL,
                limit_count INTEGER NOT NULL,
                scanned INTEGER NOT NULL DEFAULT 0,
                missed INTEGER NOT NULL DEFAULT 0,
                dispatched INTEGER NOT NULL DEFAULT 0,
                error TEXT
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS discord_recovery_cursors (
                channel_id TEXT PRIMARY KEY,
                last_message_id TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        cutoff = (
            dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=_RETENTION_DAYS)
        ).isoformat()
        connection.execute(
            "DELETE FROM discord_messages WHERE updated_at < ?",
            (cutoff,),
        )
        connection.execute(
            "DELETE FROM discord_recovery_scans "
            "WHERE COALESCE(completed_at, started_at) < ?",
            (cutoff,),
        )
        connection.execute(
            "DELETE FROM discord_recovery_cursors WHERE updated_at < ?",
            (cutoff,),
        )
