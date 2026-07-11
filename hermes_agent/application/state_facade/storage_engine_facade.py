#!/usr/bin/env python3
"""
SQLite State Store for Hermes Agent.

Provides persistent session storage with FTS5 full-text search, replacing
the per-session JSONL file approach. Stores session metadata, full message
history, and model configuration for CLI and gateway sessions.

Key design decisions:
- WAL mode for concurrent readers + one writer (gateway multi-platform)
- FTS5 virtual table for fast text search across all session messages
- Compression-triggered session splitting via parent_session_id chains
- Batch runner and RL trajectories are NOT stored here (separate systems)
- Session source tagging ('cli', 'telegram', 'discord', etc.) for filtering
"""

import json
import logging
import random
import re
import sqlite3
import threading
import time
import warnings
from pathlib import Path

from agent.memory_manager import sanitize_context
from hermes_agent.domain.event_ledger import EventLedger
from hermes_agent.domain.seq_allocator import ensure_session_counter
from hermes_agent.domain.conversation_participant_reconciler import ConversationParticipantReconciler
from hermes_agent.domain.mission_activity_reconciler import MissionActivityReconciler
from hermes_agent.domain.session_deletion import SessionDeletionService
from hermes_agent.domain.session_handoff_state import SessionHandoffStateMixin
from hermes_agent.domain.session_index_reconciler import SessionIndexReconciler
from hermes_agent.read_models.message_history import MessageHistoryReadModel, MessagePageQuery
from hermes_agent.read_models.session_recall import SessionRecallReadModel
from hermes_agent.read_models.session_recall import (
    contains_cjk as _recall_contains_cjk,
    sanitize_fts5_query as _recall_sanitize_fts5_query,
)
from hermes_agent.read_models.session_index import SessionIndexQuery, SessionIndexReadModel
from hermes_agent.read_models.session_list import SessionListQuery, SessionListReadModel
from hermes_agent.repositories.agent_profile_repo import AgentProfileRepoImpl
from hermes_agent.repositories.message_repo import MessageRepoImpl, MessageRepository
from hermes_agent.repositories.session_repo import SessionRepoImpl
from hermes_agent.repositories.team_mission_repo import TeamMissionRepoImpl
from hermes_agent.storage.fts_schema import FTS_SQL, FTS_TRIGRAM_SQL
from hermes_agent.storage.sqlite_wal import WAL_INCOMPAT_MARKERS as _WAL_INCOMPAT_MARKERS
from hermes_agent.storage.sqlite_wal import apply_wal_with_fallback
from hermes_agent.storage.sqlite_wal import wal_fallback_warned_paths as _wal_fallback_warned_paths
from hermes_agent.storage.state_schema import DEFERRED_INDEX_SQL
from hermes_agent.storage.state_schema import SCHEMA_SQL
from hermes_agent.storage.state_maintenance import StateMaintenanceMixin
from hermes_constants import get_hermes_home
from hermes_agent.application.state_facade.message_facade import MessageStateFacadeMixin
from hermes_agent.domain.run_event_codec import decode_run_event_row
from hermes_agent.domain.run_event_codec import update_run_event_frame_columns
from hermes_agent.domain.run_event_index import project_run_event_search_index
from hermes_agent.domain.run_event_index import runtime_source_seq_from_event
from hermes_agent.domain.session_runtime_state import session_info_record
from hermes_agent.read_models.tool_events import backfill_tool_events_from_run_events
from hermes_team_mission.state.session_mixin import TeamMissionStateMixin
from hermes_team_mission.state.schema import migrate_active_mission_id_to_conversation_missions
from hermes_team_mission.state.schema import reconcile_team_mission_node_primary_key
from hermes_team_mission.state.maintenance import run_team_mission_startup_maintenance
from hermes_team_mission.runtime.run_event_retention import RunEventRetentionPolicy
from typing import Any, Callable, Dict, List, Optional, Tuple, TypeVar

logger = logging.getLogger(__name__)


T = TypeVar("T")

DEFAULT_DB_PATH = get_hermes_home() / "state.db"

# Keep the long-standing ``hermes_state.py`` module import-compatible while
# allowing narrowly scoped submodules such as ``hermes_state.migrations``.
__path__ = [str(Path(__file__).with_name("hermes_state"))]

SCHEMA_VERSION = 46
RUN_EVENT_RETENTION_POLICY = RunEventRetentionPolicy()


def _sqlite_row_value(row: sqlite3.Row | tuple[Any, ...] | None, key: str, index: int, default: Any = None) -> Any:
    if row is None:
        return default
    try:
        return row[key]  # type: ignore[index]
    except (IndexError, KeyError, TypeError):
        try:
            return row[index]  # type: ignore[index]
        except (IndexError, TypeError):
            return default


# Last HermesStateStore() init error, per-process.  Surfaced in /resume and
# related slash-command error strings so users know WHY the DB is
# unavailable instead of getting a bare "Session store not available."
# Only HermesStateStore.__init__ writes to this; kanban_db.connect() failures
# do not update it (by design — kanban failures are reported via their
# own caller's error handling, not via /resume-style slash commands).
_last_init_error: Optional[str] = None
_last_init_error_lock = threading.Lock()

def _set_last_init_error(msg: Optional[str]) -> None:
    """Record (or clear) the most recent state.db init failure.

    Thread-safe via _last_init_error_lock.  Callers pass a message to
    record a failure or None to clear.  HermesStateStore.__init__ only calls
    this to SET on failure — it deliberately does NOT clear on success,
    because in a multi-threaded caller (e.g. gateway / web_server per-
    request HermesStateStore() instantiation), a concurrent successful open
    racing past a different thread's failure would erase the cause
    string that thread's /resume handler is about to format.  Explicit
    clears (e.g. test fixtures) are still supported by passing None.
    """
    global _last_init_error
    with _last_init_error_lock:
        _last_init_error = msg


def get_last_init_error() -> Optional[str]:
    """Return the most recent state.db init failure, if any.

    Slash-command handlers (``/resume``, ``/title``, ``/history``, ``/branch``)
    call this to surface the underlying cause in their error messages when
    ``_session_db is None``.  Returns ``None`` if HermesStateStore initialized
    successfully (or hasn't been attempted).
    """
    return _last_init_error


def format_session_db_unavailable(prefix: str = "Session database not available") -> str:
    """Format a user-facing 'session store unavailable' message with cause.

    When ``HermesStateStore()`` init fails, callers set ``_session_db = None`` and
    several slash commands (/resume, /title, /history, /branch) previously
    responded with a bare ``"Session store not available."`` — no
    indication of WHY.  This helper includes the captured cause (typically
    ``"locking protocol"`` from NFS/SMB) and points users at the known
    culprit so they can fix it themselves.

    Example output:
        Session store not available: locking protocol (state.db may be
        on NFS/SMB — see https://www.sqlite.org/wal.html).
    """
    cause = get_last_init_error()
    if not cause:
        return f"{prefix}."
    hint = ""
    if any(marker in cause.lower() for marker in _WAL_INCOMPAT_MARKERS):
        hint = " (state.db may be on NFS/SMB/FUSE — see https://www.sqlite.org/wal.html)"
    return f"{prefix}: {cause}{hint}."




class StorageEngineMixin:
    _WRITE_MAX_RETRIES = 15
    _WRITE_RETRY_MIN_S = 0.020
    _WRITE_RETRY_MAX_S = 0.150
    _CHECKPOINT_EVERY_N_WRITES = 50
    _contains_cjk = staticmethod(_recall_contains_cjk)
    _sanitize_fts5_query = staticmethod(_recall_sanitize_fts5_query)

    def __init__(self, db_path: Path = None):
        self.db_path = db_path or DEFAULT_DB_PATH
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

        self._lock = threading.Lock()
        self._write_count = 0
        try:
            self._conn = sqlite3.connect(
                str(self.db_path),
                check_same_thread=False,
                # Short timeout — application-level retry with random jitter
                # handles contention instead of sitting in SQLite's internal
                # busy handler for up to 30s.
                timeout=1.0,
                # Autocommit mode: Python's default isolation_level=""
                # auto-starts transactions on DML, which conflicts with our
                # explicit BEGIN IMMEDIATE.  None = we manage transactions
                # ourselves.
                isolation_level=None,
            )
            self._conn.row_factory = sqlite3.Row
            apply_wal_with_fallback(self._conn, db_label="state.db")
            self._conn.execute("PRAGMA busy_timeout=5000")
            self._conn.execute("PRAGMA foreign_keys=ON")
            # 增量自动回收:删除产生的空闲页进入 freelist 并被后续写入复用,文件不再
            # 无限膨胀(团队任务的流式 delta「删了不回收」曾把 state.db 撑到 2.5GB、
            # 61% 空洞,连 15 行的会话列表查询都被拖到 ~1.8s)。对新库立即生效;已有的
            # NONE 模式库需 VACUUM 一次切换(运维侧已处理)。必须在建表前设置。
            try:
                self._conn.execute("PRAGMA auto_vacuum=INCREMENTAL")
            except Exception as exc:
                logger.debug("auto_vacuum incremental setup skipped: %s", exc)

            self._init_schema()
            try:
                participant_backfill = self.reconcile_conversation_participants_one_shot()
                if int(participant_backfill.get("inserted") or 0):
                    logger.info(
                        "backfilled %d conversation participant row(s)",
                        int(participant_backfill.get("inserted") or 0),
                    )
            except Exception as participant_backfill_exc:
                logger.warning(
                    "conversation participants startup backfill skipped: %s",
                    participant_backfill_exc,
                )
            try:
                mission_activity_backfill = self.reconcile_mission_activities_one_shot()
                if int(mission_activity_backfill.get("inserted") or 0):
                    logger.info(
                        "backfilled %d mission activity row(s)",
                        int(mission_activity_backfill.get("inserted") or 0),
                    )
            except Exception as mission_activity_backfill_exc:
                logger.warning(
                    "mission activities startup backfill skipped: %s",
                    mission_activity_backfill_exc,
                )
            run_team_mission_startup_maintenance(self, logger)
            try:
                repaired_fk_rows = self.repair_orphaned_foreign_key_rows()
                if repaired_fk_rows:
                    logger.info(
                        "repaired %d orphaned state foreign-key row(s)",
                        repaired_fk_rows,
                    )
            except Exception as repair_fk_exc:
                logger.warning(
                    "orphaned state foreign-key repair skipped: %s",
                    repair_fk_exc,
                )
            try:
                self._reclaim_freelist_on_startup()
            except Exception as reclaim_exc:
                logger.warning("state.db freelist reclaim skipped: %s", reclaim_exc)
        except Exception as exc:
            # Capture the cause so /resume and friends can surface WHY the
            # session store is unavailable instead of a bare "Session store
            # not available."  Callers that catch this exception keep their
            # existing ``self._session_db = None`` degradation path.
            #
            # Note: we deliberately do NOT clear _last_init_error on the
            # success path (no else branch).  In multi-threaded callers
            # (gateway, web_server per-request HermesStateStore()), a concurrent
            # successful open racing past this failure would erase the
            # cause that another thread's /resume is about to format.
            # Tests that need to reset the state can call
            # ``hermes_state._set_last_init_error(None)`` explicitly.
            _set_last_init_error(f"{type(exc).__name__}: {exc}")
            raise

    # ── Core write helper ──

    def _execute_write(self, fn: Callable[[sqlite3.Connection], T]) -> T:
        """Execute a write transaction with BEGIN IMMEDIATE and jitter retry.

        *fn* receives the connection and should perform INSERT/UPDATE/DELETE
        statements.  The caller must NOT call ``commit()`` — that's handled
        here after *fn* returns.

        BEGIN IMMEDIATE acquires the WAL write lock at transaction start
        (not at commit time), so lock contention surfaces immediately.
        On ``database is locked``, we release the Python lock, sleep a
        random 20-150ms, and retry — breaking the convoy pattern that
        SQLite's built-in deterministic backoff creates.

        Returns whatever *fn* returns.
        """
        last_err: Optional[Exception] = None
        for attempt in range(self._WRITE_MAX_RETRIES):
            try:
                with self._lock:
                    self._conn.execute("BEGIN IMMEDIATE")
                    try:
                        result = fn(self._conn)
                        self._conn.commit()
                    except BaseException:
                        try:
                            self._conn.rollback()
                        except Exception as exc:
                            logger.debug("rollback after failed state write failed: %s", exc)
                        raise
                # Success — periodic best-effort checkpoint.
                self._write_count += 1
                if self._write_count % self._CHECKPOINT_EVERY_N_WRITES == 0:
                    self._try_wal_checkpoint()
                return result
            except sqlite3.OperationalError as exc:
                err_msg = str(exc).lower()
                if "locked" in err_msg or "busy" in err_msg:
                    last_err = exc
                    if attempt < self._WRITE_MAX_RETRIES - 1:
                        jitter = random.uniform(
                            self._WRITE_RETRY_MIN_S,
                            self._WRITE_RETRY_MAX_S,
                        )
                        time.sleep(jitter)
                        continue
                # Non-lock error or retries exhausted — propagate.
                raise
        # Retries exhausted (shouldn't normally reach here).
        raise last_err or sqlite3.OperationalError(
            "database is locked after max retries"
        )

    def update_session_source(self, session_id: str, source: str) -> int:
        """Update one session's canonical source through the public DB surface."""
        sid = str(session_id or "").strip()
        normalized_source = str(source or "").strip()
        if not sid or not normalized_source:
            return 0

        def _do(conn: sqlite3.Connection) -> int:
            return SessionRepoImpl(conn).update_source(sid, normalized_source)

        return int(self._execute_write(_do) or 0)

    def _reclaim_freelist_on_startup(self) -> None:
        """启动时把删除留下的空闲页增量还给操作系统,防止 state.db 膨胀。

        仅对 ``auto_vacuum=INCREMENTAL`` 的库有效(NONE 模式下
        ``incremental_vacuum`` 是 no-op)。限制单次回收页数,避免积压很多时
        长时间卡住启动;日常空闲页很少,通常是毫秒级。
        """
        MIN_FREE_PAGES = 2560      # ~10MB 以下不值得回收
        MAX_PAGES = 50000          # 单次最多回收 ~200MB,避免积压时卡启动
        try:
            free_pages = int(self._conn.execute("PRAGMA freelist_count").fetchone()[0])
        except Exception:
            return
        if free_pages < MIN_FREE_PAGES:
            return
        pages = min(free_pages, MAX_PAGES)
        with self._lock:
            self._conn.execute(f"PRAGMA incremental_vacuum({pages})")
        logger.info(
            "state.db incremental_vacuum reclaimed up to %d free page(s) (freelist was %d)",
            pages, free_pages,
        )

    def _try_wal_checkpoint(self) -> None:
        """Best-effort TRUNCATE WAL checkpoint.  Never raises.

        Flushes committed WAL frames back into the main DB file and
        truncates the WAL file to zero bytes.  Keeps the WAL from
        growing unbounded when many processes hold persistent
        connections.

        PASSIVE checkpoint never truncates the WAL file; it leaves the file
        at its high-water mark until an explicit TRUNCATE checkpoint runs.
        This method is already off the hot write path and protected by
        ``self._lock``, so a short TRUNCATE checkpoint is the right tradeoff
        for long-lived desktop/gateway processes.
        """
        try:
            with self._lock:
                result = self._conn.execute(
                    "PRAGMA wal_checkpoint(TRUNCATE)"
                ).fetchone()
                if result and result[1] > 0:
                    logger.debug(
                        "WAL checkpoint: %d/%d pages checkpointed",
                        result[2], result[1],
                    )
        except Exception as exc:
            logger.debug("best-effort WAL checkpoint failed: %s", exc)

    def close(self):
        """Close the database connection.

        Attempts a TRUNCATE WAL checkpoint first so that exiting processes
        help shrink the WAL file.
        """
        with self._lock:
            if self._conn:
                try:
                    self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                except Exception as exc:
                    logger.debug("close-time WAL checkpoint failed: %s", exc)
                self._conn.close()
                self._conn = None

    @staticmethod
    def _parse_schema_columns(schema_sql: str) -> Dict[str, Dict[str, str]]:
        """Extract expected columns per table from SCHEMA_SQL.

        Uses an in-memory SQLite database to parse the SQL — SQLite itself
        handles all syntax (DEFAULT expressions with commas, inline
        REFERENCES, CHECK constraints, etc.) so there are zero regex
        edge cases.  The in-memory DB is opened, the schema DDL is
        executed, and PRAGMA table_info extracts the column metadata.

        Adding a column to SCHEMA_SQL is all that's needed; the
        reconciliation loop picks it up automatically.
        """
        ref = sqlite3.connect(":memory:")
        try:
            ref.executescript(schema_sql)
            table_columns: Dict[str, Dict[str, str]] = {}
            for (tbl,) in ref.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            ).fetchall():
                cols: Dict[str, str] = {}
                for row in ref.execute(
                    f'PRAGMA table_info("{tbl}")'
                ).fetchall():
                    # row: (cid, name, type, notnull, dflt_value, pk)
                    col_name = row[1]
                    col_type = row[2] or ""
                    notnull = row[3]
                    default = row[4]
                    pk = row[5]
                    # Reconstruct the type expression for ALTER TABLE ADD COLUMN
                    parts = [col_type] if col_type else []
                    if notnull and not pk:
                        parts.append("NOT NULL")
                    if default is not None:
                        parts.append(f"DEFAULT {default}")
                    cols[col_name] = " ".join(parts)
                table_columns[tbl] = cols
            return table_columns
        finally:
            ref.close()

    def _reconcile_columns(self, cursor: sqlite3.Cursor) -> None:
        """Ensure live tables have every column declared in SCHEMA_SQL.

        Follows the Beets/sqlite-utils pattern: the CREATE TABLE definition
        in SCHEMA_SQL is the single source of truth for the desired schema.
        On every startup this method diffs the live columns (via PRAGMA
        table_info) against the declared columns, and ADDs any that are
        missing.

        This makes column additions a declarative operation — just add
        the column to SCHEMA_SQL and it appears on the next startup.
        Version-gated migration blocks are no longer needed for ADD COLUMN.
        """
        expected = self._parse_schema_columns(SCHEMA_SQL)
        for table_name, declared_cols in expected.items():
            # Get current columns from the live table
            try:
                rows = cursor.execute(
                    f'PRAGMA table_info("{table_name}")'
                ).fetchall()
            except sqlite3.OperationalError:
                continue  # Table doesn't exist yet (shouldn't happen after executescript)
            live_cols = set()
            for row in rows:
                # PRAGMA table_info returns (cid, name, type, notnull, dflt_value, pk)
                name = row[1] if isinstance(row, (tuple, list)) else row["name"]
                live_cols.add(name)

            for col_name, col_type in declared_cols.items():
                if col_name not in live_cols:
                    safe_name = col_name.replace('"', '""')
                    try:
                        cursor.execute(
                            f'ALTER TABLE "{table_name}" ADD COLUMN "{safe_name}" {col_type}'
                        )
                    except sqlite3.OperationalError as exc:
                        # Expected: "duplicate column name" from a race or
                        # re-run.  Unexpected: "Cannot add a NOT NULL column
                        # with default value NULL" from a schema mistake.
                        # Log at DEBUG so it's visible in agent.log.
                        logger.debug(
                            "reconcile %s.%s: %s", table_name, col_name, exc,
                        )

    def _backfill_session_list_summaries(self, cursor: sqlite3.Cursor) -> None:
        """Populate denormalized list fields from active message rows.

        This is a one-time compatibility path for databases created before
        ``sessions.preview`` and ``sessions.last_active`` existed. New writes
        keep these fields current, so list endpoints do not need to aggregate
        over the messages table on every sidebar refresh.
        """
        rows = cursor.execute("SELECT id FROM sessions ORDER BY id").fetchall()
        repo = MessageRepository(cursor.connection, SessionRepoImpl(cursor.connection))
        for row in rows:
            session_id = str(_sqlite_row_value(row, "id", 0, "") or "").strip()
            if session_id:
                repo.rebuild_session_projection(session_id)

    def _migrate_agent_profile_versions_to_latest_profiles(self, cursor: sqlite3.Cursor) -> None:
        """Fold the removed profile version table into latest profile rows."""

        AgentProfileRepoImpl(cursor.connection).fold_removed_profile_versions()

    def _drop_deprecated_member_chat_runs(self, cursor: sqlite3.Cursor) -> None:
        """Physically remove the P2-P4 member-chat compatibility registry."""

        try:
            cursor.execute("DROP TABLE IF EXISTS member_chat_runs")
        except sqlite3.OperationalError as exc:
            logger.debug("member_chat_runs drop skipped: %s", exc)

    def _backfill_session_index_conversation_kind(self, cursor: sqlite3.Cursor) -> None:
        """Normalize the explicit direct/team classification for sidebar rows."""

        try:
            SessionRepoImpl(cursor.connection).normalize_index_conversation_kind()
        except sqlite3.OperationalError as exc:
            logger.debug("session_index conversation kind backfill skipped: %s", exc)

    def _backfill_session_runtime_state(self, cursor: sqlite3.Cursor) -> None:
        """Build latest-only runtime state from existing session.info frames."""

        try:
            rows = cursor.execute(
                """
                SELECT *
                FROM run_events
                WHERE event_type = 'session.info'
                ORDER BY session_id ASC, seq ASC, id ASC
                """
            ).fetchall()
        except sqlite3.OperationalError as exc:
            logger.debug("session_runtime_state backfill skipped: %s", exc)
            return

        latest_by_session: dict[str, dict[str, Any]] = {}
        for row in rows:
            event = decode_run_event_row(row)
            event = event if isinstance(event, dict) else {}
            payload = event.get("payload") if isinstance(event.get("payload"), dict) else None
            payload = payload if isinstance(payload, dict) else {}
            session_id = str(event.get("conversation_session_id") or row["session_id"] or "").strip()
            if not session_id:
                continue
            record = session_info_record(
                session_id=session_id,
                payload=payload,
                runtime_scope_key=str(event.get("runtime_scope_key") or row["runtime_scope_key"] or ""),
                execution_session_id=str(
                    event.get("execution_session_id")
                    or row["execution_session_id"]
                    or event.get("session_id")
                    or ""
                ),
                run_id=str(event.get("run_id") or row["run_id"] or ""),
                turn_id=str(event.get("turn_id") or row["turn_id"] or ""),
                updated_at=float(event.get("timestamp") or row["timestamp"] or 0),
                source_seq=int(event.get("seq") or row["seq"] or 0),
            )
            latest_by_session[session_id] = record

        for record in latest_by_session.values():
            cursor.execute(
                """
                INSERT INTO session_runtime_state (
                    session_id, runtime_scope_key, execution_session_id, run_id,
                    turn_id, status, model, provider, profile_json,
                    payload_hash, updated_at, source_seq
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(session_id) DO UPDATE SET
                    runtime_scope_key = excluded.runtime_scope_key,
                    execution_session_id = excluded.execution_session_id,
                    run_id = excluded.run_id,
                    turn_id = excluded.turn_id,
                    status = excluded.status,
                    model = excluded.model,
                    provider = excluded.provider,
                    profile_json = excluded.profile_json,
                    payload_hash = excluded.payload_hash,
                    updated_at = excluded.updated_at,
                    source_seq = excluded.source_seq
                WHERE COALESCE(excluded.source_seq, 0) >= COALESCE(session_runtime_state.source_seq, 0)
                """,
                (
                    record["session_id"],
                    record["runtime_scope_key"],
                    record["execution_session_id"],
                    record["run_id"],
                    record["turn_id"],
                    record["status"],
                    record["model"],
                    record["provider"],
                    record["profile_json"],
                    record["payload_hash"],
                    record["updated_at"],
                    record["source_seq"],
                ),
            )

    def _backfill_tool_events(self, cursor: sqlite3.Cursor) -> None:
        """Build the tool timeline read model from existing tool runtime frames."""

        backfill_tool_events_from_run_events(cursor.connection, logger=logger)

    def _backfill_run_event_frame_indexes(self, cursor: sqlite3.Cursor) -> None:
        """Build compressed frame/search-index columns for existing run_events rows."""

        try:
            rows = cursor.execute(
                """
                SELECT *
                FROM run_events
                WHERE frame_blob IS NULL
                   OR COALESCE(frame_format, '') = ''
                   OR COALESCE(retention_class, '') = ''
                   OR NOT EXISTS (
                       SELECT 1
                       FROM run_event_search_index idx
                       WHERE idx.run_event_id = run_events.id
                   )
                ORDER BY session_id ASC, seq ASC, id ASC
                """
            ).fetchall()
        except sqlite3.OperationalError as exc:
            logger.debug("run_event frame/search-index backfill skipped: %s", exc)
            return

        for row in rows:
            event = decode_run_event_row(row)
            event = event if isinstance(event, dict) else {}
            event_type = str(row["event_type"] or event.get("type") or "")
            runtime_source_seq = runtime_source_seq_from_event(event)
            update_run_event_frame_columns(
                cursor.connection,
                row_id=int(row["id"]),
                event=event,
                retention_class=RUN_EVENT_RETENTION_POLICY.classify_event_type(event_type),
                projection_state="raw",
            )
            EventLedger(cursor.connection).update_runtime_source_seq(
                row_id=int(row["id"]),
                runtime_source_seq=runtime_source_seq,
            )
            project_run_event_search_index(
                cursor.connection,
                row_id=int(row["id"]),
                session_id=str(row["session_id"] or event.get("conversation_session_id") or ""),
                seq=int(row["seq"] or event.get("seq") or 0),
                event_type=event_type,
                runtime_scope_key=str(row["runtime_scope_key"] or event.get("runtime_scope_key") or ""),
                runtime_source_seq=runtime_source_seq,
                event=event,
                updated_at=float(row["timestamp"] or event.get("timestamp") or 0),
            )

    def _migrate_run_events_activity_id(self, cursor: sqlite3.Cursor) -> None:
        """v37: add ``activity_id`` to ``run_events`` so Activity Runtime can
        query by activity dimension without scanning by session_id.

        See ADR-0001 (Activity as Runtime Primitive). The column is nullable
        during Phase 0; backfill is owned by
        ``RunEventMaintenanceService.backfill_activity_ids``. After backfill
        completes the read path may treat ``activity_id`` as authoritative,
        but the column stays nullable so legacy / orphan rows do not block
        writes.
        """
        try:
            rows = cursor.execute('PRAGMA table_info("run_events")').fetchall()
        except sqlite3.OperationalError:
            return
        names = {
            row["name"] if isinstance(row, sqlite3.Row) else row[1]
            for row in rows
        }
        if "activity_id" not in names:
            try:
                cursor.execute(
                    'ALTER TABLE "run_events" ADD COLUMN "activity_id" TEXT'
                )
            except sqlite3.OperationalError as exc:
                logger.debug("run_events.activity_id migration skipped: %s", exc)
        try:
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_run_events_activity_seq "
                "ON run_events(activity_id, seq) "
                "WHERE activity_id IS NOT NULL"
            )
        except sqlite3.OperationalError as exc:
            logger.debug("idx_run_events_activity_seq create skipped: %s", exc)

    def _migrate_activity_commands(self, cursor: sqlite3.Cursor) -> None:
        """v38: add durable Activity Command intent storage.

        ADR-0001 Phase 1.A introduces ``activity_commands`` as the command
        bus persistence layer. This migration is intentionally idempotent:
        it checks live SQLite metadata before creating the table and indexes,
        so partially migrated databases can safely reopen.
        """
        try:
            table_row = cursor.execute(
                """
                SELECT name
                  FROM sqlite_master
                 WHERE type = 'table'
                   AND name = 'activity_commands'
                """
            ).fetchone()
        except sqlite3.OperationalError:
            return
        if table_row is None:
            try:
                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS activity_commands (
                        command_id TEXT PRIMARY KEY,
                        activity_id TEXT NOT NULL,
                        kind TEXT NOT NULL CHECK (kind IN ('create', 'start', 'cancel', 'complete')),
                        payload_json TEXT NOT NULL DEFAULT '{}',
                        intent_at REAL NOT NULL,
                        state TEXT NOT NULL DEFAULT 'accepted'
                            CHECK (state IN ('accepted', 'dispatched', 'satisfied', 'failed')),
                        state_changed_at REAL NOT NULL,
                        result_event_id INTEGER,
                        error_reason TEXT,
                        metadata_json TEXT NOT NULL DEFAULT '{}',
                        FOREIGN KEY (result_event_id) REFERENCES run_events(id) ON DELETE SET NULL
                    )
                    """
                )
            except sqlite3.OperationalError as exc:
                logger.debug("activity_commands table migration skipped: %s", exc)
                return

        try:
            index_rows = cursor.execute('PRAGMA index_list("activity_commands")').fetchall()
        except sqlite3.OperationalError:
            index_rows = []
        index_names = {
            str(_sqlite_row_value(row, "name", 1, "") or "")
            for row in index_rows
        }
        if "idx_activity_commands_state" not in index_names:
            try:
                cursor.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_activity_commands_state
                        ON activity_commands(state, intent_at)
                    """
                )
            except sqlite3.OperationalError as exc:
                logger.debug("idx_activity_commands_state create skipped: %s", exc)
        if "idx_activity_commands_activity" not in index_names:
            try:
                cursor.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_activity_commands_activity
                        ON activity_commands(activity_id, intent_at)
                    """
                )
            except sqlite3.OperationalError as exc:
                logger.debug("idx_activity_commands_activity create skipped: %s", exc)

    def _migrate_run_events_participant_id(self, cursor: sqlite3.Cursor) -> None:
        """v28: add durable event speaker identity for P1 Participant rollout."""
        try:
            rows = cursor.execute('PRAGMA table_info("run_events")').fetchall()
        except sqlite3.OperationalError:
            return
        names = {
            row["name"] if isinstance(row, sqlite3.Row) else row[1]
            for row in rows
        }
        if "participant_id" not in names:
            try:
                cursor.execute(
                    'ALTER TABLE "run_events" '
                    'ADD COLUMN "participant_id" TEXT NOT NULL DEFAULT \'\''
                )
            except sqlite3.OperationalError as exc:
                logger.debug("run_events.participant_id migration skipped: %s", exc)
        try:
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_run_events_participant "
                "ON run_events(participant_id)"
            )
        except sqlite3.OperationalError as exc:
            logger.debug("idx_run_events_participant create skipped: %s", exc)

    def _migrate_messages_participant_id(self, cursor: sqlite3.Cursor) -> None:
        """v30: add durable transcript speaker identity for history rendering."""
        try:
            rows = cursor.execute('PRAGMA table_info("messages")').fetchall()
        except sqlite3.OperationalError:
            return
        names = {
            row["name"] if isinstance(row, sqlite3.Row) else row[1]
            for row in rows
        }
        if "participant_id" not in names:
            try:
                cursor.execute(
                    'ALTER TABLE "messages" '
                    'ADD COLUMN "participant_id" TEXT NOT NULL DEFAULT \'\''
                )
            except sqlite3.OperationalError as exc:
                logger.debug("messages.participant_id migration skipped: %s", exc)

    def _migrate_session_system_prompts(self, cursor: sqlite3.Cursor) -> None:
        """v31: cache system prompts by execution scope, not just transcript session."""
        try:
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS session_system_prompts (
                    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
                    scope_key TEXT NOT NULL,
                    system_prompt TEXT NOT NULL,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY (session_id, scope_key)
                )
                """
            )
        except sqlite3.OperationalError as exc:
            logger.debug("session_system_prompts migration skipped: %s", exc)

    def _migrate_activities_kind_mission_check(self, cursor: sqlite3.Cursor) -> None:
        """v29: rebuild activities so the kind CHECK accepts mission rows."""
        try:
            TeamMissionRepoImpl(cursor.connection).migrate_legacy_activities_kind_mission_check()
        except sqlite3.OperationalError as exc:
            logger.debug("activities kind mission CHECK migration skipped: %s", exc)

    def _init_schema(self):
        """Create tables and FTS if they don't exist, reconcile columns.

        Schema management follows the declarative reconciliation pattern
        (Beets, sqlite-utils): SCHEMA_SQL is the single source of truth.
        On existing databases, _reconcile_columns() diffs live columns
        against SCHEMA_SQL and ADDs any missing ones.  This eliminates
        the version-gated migration chain for column additions, making
        it impossible for reordered or inserted migrations to skip columns.

        The schema_version table is retained for future data migrations
        (transforming existing rows) which cannot be handled declaratively.
        """
        cursor = self._conn.cursor()

        cursor.executescript(SCHEMA_SQL)

        # ── Declarative column reconciliation ──────────────────────────
        # Diff live tables against SCHEMA_SQL and ADD any missing columns.
        # This is idempotent and self-healing: even if a version-gated
        # migration was skipped (e.g. due to version renumbering), the
        # column gets created here.
        self._reconcile_columns(cursor)
        self._drop_deprecated_member_chat_runs(cursor)
        self._backfill_session_index_conversation_kind(cursor)
        reconcile_team_mission_node_primary_key(cursor)
        migrate_active_mission_id_to_conversation_missions(cursor)
        self._migrate_activities_kind_mission_check(cursor)

        # Indexes that reference reconciler-added columns must be created
        # AFTER _reconcile_columns runs — declaring them in SCHEMA_SQL
        # makes the initial executescript fail on legacy DBs (the index's
        # WHERE clause references a column that doesn't exist yet).
        try:
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_messages_platform_msg_id "
                "ON messages(session_id, platform_message_id) "
                "WHERE platform_message_id IS NOT NULL"
            )
        except sqlite3.OperationalError as exc:
            logger.debug("idx_messages_platform_msg_id create skipped: %s", exc)

        try:
            cursor.executescript(DEFERRED_INDEX_SQL)
        except sqlite3.OperationalError as exc:
            logger.debug("deferred message indexes create skipped: %s", exc)

        cursor.execute("SELECT version FROM schema_version LIMIT 1")
        row = cursor.fetchone()
        if row is None:
            self._backfill_session_list_summaries(cursor)
            self._backfill_session_runtime_state(cursor)
            self._backfill_tool_events(cursor)

        from hermes_agent.storage.migrations import MigrationRunner

        MigrationRunner(cursor, self).run_all()
        self._ensure_message_fts_locked(cursor)

        # Unique title index — always ensure it exists
        try:
            cursor.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_sessions_title_unique "
                "ON sessions(title) WHERE title IS NOT NULL"
            )
        except sqlite3.OperationalError as exc:
            logger.debug("sessions title unique index ensure skipped: %s", exc)

        self._conn.commit()

    def _ensure_message_fts_locked(self, cursor: sqlite3.Cursor) -> None:
        """Ensure message FTS tables, triggers, and backfill are present.

        Some schema migrations rebuild ``messages``. SQLite drops triggers
        attached to a table when that table is dropped, so table-existence
        checks alone are insufficient.
        """
        cursor.executescript(FTS_SQL)
        cursor.executescript(FTS_TRIGRAM_SQL)
        message_count = int(cursor.execute("SELECT COUNT(*) FROM messages").fetchone()[0])
        fts_count = int(cursor.execute("SELECT COUNT(*) FROM messages_fts").fetchone()[0])
        trigram_count = int(cursor.execute("SELECT COUNT(*) FROM messages_fts_trigram").fetchone()[0])
        if fts_count != message_count:
            cursor.execute("DELETE FROM messages_fts")
            cursor.execute(
                """
                INSERT INTO messages_fts(rowid, content)
                SELECT id,
                       COALESCE(content, '') || ' ' ||
                       COALESCE(tool_name, '') || ' ' ||
                       COALESCE(tool_calls, '')
                FROM messages
                """
            )
        if trigram_count != message_count:
            cursor.execute("DELETE FROM messages_fts_trigram")
            cursor.execute(
                """
                INSERT INTO messages_fts_trigram(rowid, content)
                SELECT id,
                       COALESCE(content, '') || ' ' ||
                       COALESCE(tool_name, '') || ' ' ||
                       COALESCE(tool_calls, '')
                FROM messages
                """
            )

    def reconcile_conversation_participants_one_shot(self) -> Dict[str, Any]:
        return self._execute_write(
            lambda conn: ConversationParticipantReconciler(conn).reconcile()
        )

    def reconcile_mission_activities_one_shot(self) -> Dict[str, Any]:
        return self._execute_write(
            lambda conn: MissionActivityReconciler(conn).reconcile()
        )
