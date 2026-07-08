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
    _contains_cjk as _recall_contains_cjk,
    _sanitize_fts5_query as _recall_sanitize_fts5_query,
)
from channels.platforms.telegram_topic_store import TelegramTopicStateMixin
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
from hermes_agent.storage.state_mixins.activities import ActivitiesMixin
from hermes_agent.storage.state_mixins.agent_profiles import AgentProfileStateMixin
from hermes_agent.storage.state_mixins.branch import BranchStateMixin
from hermes_agent.storage.state_mixins.member_chat import MemberChatStateMixin
from hermes_agent.application.state_facade.message_facade import MessageStateFacadeMixin
from hermes_agent.storage.state_mixins.participants import ParticipantsMixin
from hermes_agent.domain.run_event_codec import decode_run_event_row
from hermes_agent.domain.run_event_codec import update_run_event_frame_columns
from hermes_agent.domain.run_event_index import project_run_event_search_index
from hermes_agent.domain.run_event_index import runtime_source_seq_from_event
from hermes_agent.domain.session_runtime_state import session_info_record
from hermes_agent.storage.state_mixins.runs import RunStateMixin
from hermes_agent.read_models.tool_events import backfill_tool_events_from_run_events
from hermes_agent.storage.state_mixins.team_capabilities import TeamCapabilityStateMixin
from hermes_team_mission.state.session_mixin import TeamMissionStateMixin
from hermes_agent.storage.state_mixins.team_registry import TeamRegistryStateMixin
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


def format_session_db_unavailable(prefix: str = "Session store not available") -> str:
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


class HermesStateStore(AgentProfileStateMixin, TeamRegistryStateMixin, TeamCapabilityStateMixin, TeamMissionStateMixin, MemberChatStateMixin, ParticipantsMixin, ActivitiesMixin, MessageStateFacadeMixin, RunStateMixin, BranchStateMixin, TelegramTopicStateMixin, StateMaintenanceMixin, SessionHandoffStateMixin):
    """
    SQLite-backed session storage with FTS5 search.

    Thread-safe for the common gateway pattern (multiple reader threads,
    single writer via WAL mode). Each method opens its own cursor.
    """

    # ── Write-contention tuning ──
    # With multiple hermes processes (gateway + CLI sessions + worktree agents)
    # all sharing one state.db, WAL write-lock contention causes visible TUI
    # freezes.  SQLite's built-in busy handler uses a deterministic sleep
    # schedule that causes convoy effects under high concurrency.
    #
    # Instead, we keep the SQLite timeout short (1s) and handle retries at the
    # application level with random jitter, which naturally staggers competing
    # writers and avoids the convoy.
    _WRITE_MAX_RETRIES = 15
    _WRITE_RETRY_MIN_S = 0.020   # 20ms
    _WRITE_RETRY_MAX_S = 0.150   # 150ms
    # Attempt a TRUNCATE WAL checkpoint every N successful writes.
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
                    or event.get("session_id")
                    or row["execution_session_id"]
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
        during Phase 0; backfill is done out-of-band by
        ``tui_gateway.services.storage_backfill_activity_id``. After backfill
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

    # =========================================================================
    # Session lifecycle
    # =========================================================================

    def _insert_session_row(
        self,
        session_id: str,
        source: str,
        model: str = None,
        model_config: Dict[str, Any] = None,
        system_prompt: str = None,
        user_id: str = None,
        parent_session_id: str = None,
        transient: bool = False,
        title: str = None,
        cwd: str = None,
        archived: bool = False,
        session_kind: str = "hermes_session",
        conversation_kind: str = "direct",
    ) -> None:
        """Shared INSERT OR IGNORE for session rows."""
        def _do(conn):
            SessionRepoImpl(conn).ensure_session_record(
                session_id,
                source,
                model=model,
                model_config=model_config,
                system_prompt=system_prompt,
                user_id=user_id,
                parent_session_id=parent_session_id,
                transient=transient,
                title=title,
                cwd=cwd,
                archived=archived,
                session_kind=session_kind,
                conversation_kind=conversation_kind,
            )
            ensure_session_counter(conn, session_id=session_id, updated_at=time.time())
        self._execute_write(_do)

    def create_session(self, session_id: str, source: str, **kwargs) -> str:
        """Create a new session record. Returns the session_id."""
        self._insert_session_row(session_id, source, **kwargs)
        return session_id

    def end_session(self, session_id: str, end_reason: str) -> None:
        """Mark a session as ended.

        No-ops when the session is already ended. The first end_reason wins:
        compression-split sessions must keep their ``end_reason = 'compression'``
        record even if a later stale ``end_session()`` call (e.g. from a
        desynced CLI session_id after ``/resume`` or ``/branch``) targets them
        with a different reason. Use ``reopen_session()`` first if you
        intentionally need to re-end a closed session with a new reason.
        """
        def _do(conn):
            SessionRepoImpl(conn).close(session_id, end_reason)
        self._execute_write(_do)

    def reopen_session(self, session_id: str) -> None:
        """Clear ended_at/end_reason so a session can be resumed."""
        def _do(conn):
            SessionRepoImpl(conn).reopen(session_id)
        self._execute_write(_do)

    def repair_orphaned_foreign_key_rows(self) -> int:
        """Repair non-authoritative index/cache rows left dangling by old builds.

        The canonical owners are ``sessions``, ``team_missions`` and
        ``team_capability_snapshots``.  Lineage/idempotency/snapshot binding
        rows only index those owners, so deleting or orphaning invalid rows is
        the only valid recovery.  This keeps ``PRAGMA foreign_key_check`` clean
        without inventing placeholder parent records.
        """

        def affected(cursor: sqlite3.Cursor) -> int:
            return max(0, int(cursor.rowcount or 0))

        def _do(conn):
            repaired = SessionRepoImpl(conn).repair_orphaned_branch_references()
            repaired += affected(conn.execute(
                """
                DELETE FROM team_capability_snapshot_bindings
                WHERE NOT EXISTS (
                    SELECT 1 FROM team_missions m
                    WHERE m.mission_id = team_capability_snapshot_bindings.mission_id
                )
                   OR NOT EXISTS (
                    SELECT 1 FROM team_capability_snapshots s
                    WHERE s.snapshot_id = team_capability_snapshot_bindings.snapshot_id
                )
                """
            ))
            return repaired

        return self._execute_write(_do)

    def update_system_prompt(self, session_id: str, system_prompt: str) -> None:
        """Store the full assembled system prompt snapshot."""
        def _do(conn):
            SessionRepoImpl(conn).update_system_prompt(session_id, system_prompt)
        self._execute_write(_do)

    def update_session_cwd(self, session_id: str, cwd: str) -> None:
        """Store the current working directory for a CLI/runtime session."""
        def _do(conn):
            SessionRepoImpl(conn).update_cwd(session_id, str(cwd or ""))
        self._execute_write(_do)

    def get_scoped_system_prompt(self, session_id: str, scope_key: str) -> Optional[str]:
        """Return the cached system prompt for one execution scope."""
        sid = str(session_id or "").strip()
        scope = str(scope_key or "").strip()
        if not sid or not scope:
            return None
        with self._lock:
            cursor = self._conn.execute(
                """
                SELECT system_prompt
                  FROM session_system_prompts
                 WHERE session_id = ? AND scope_key = ?
                """,
                (sid, scope),
            )
            row = cursor.fetchone()
        if not row:
            return None
        value = row["system_prompt"]
        return str(value) if value is not None else None

    def update_scoped_system_prompt(
        self,
        session_id: str,
        scope_key: str,
        system_prompt: str,
    ) -> None:
        """Store a system prompt snapshot for one execution scope."""
        sid = str(session_id or "").strip()
        scope = str(scope_key or "").strip()
        if not sid or not scope:
            return

        def _do(conn):
            conn.execute(
                """
                INSERT INTO session_system_prompts
                    (session_id, scope_key, system_prompt, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(session_id, scope_key)
                DO UPDATE SET
                    system_prompt = excluded.system_prompt,
                    updated_at = excluded.updated_at
                """,
                (sid, scope, system_prompt, time.time()),
            )

        self._execute_write(_do)

    def update_token_counts(
        self,
        session_id: str,
        input_tokens: int = 0,
        output_tokens: int = 0,
        model: str = None,
        cache_read_tokens: int = 0,
        cache_write_tokens: int = 0,
        reasoning_tokens: int = 0,
        estimated_cost_usd: Optional[float] = None,
        actual_cost_usd: Optional[float] = None,
        cost_status: Optional[str] = None,
        cost_source: Optional[str] = None,
        pricing_version: Optional[str] = None,
        billing_provider: Optional[str] = None,
        billing_base_url: Optional[str] = None,
        billing_mode: Optional[str] = None,
        api_call_count: int = 0,
        absolute: bool = False,
    ) -> None:
        """Update token counters and backfill model if not already set.

        When *absolute* is False (default), values are **incremented** — use
        this for per-API-call deltas (CLI path).

        When *absolute* is True, values are **set directly** — use this when
        the caller already holds cumulative totals (gateway path, where the
        cached agent accumulates across messages).
        """
        def _do(conn):
            SessionRepoImpl(conn).update_token_counts(
                session_id,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                model=model,
                cache_read_tokens=cache_read_tokens,
                cache_write_tokens=cache_write_tokens,
                reasoning_tokens=reasoning_tokens,
                estimated_cost_usd=estimated_cost_usd,
                actual_cost_usd=actual_cost_usd,
                cost_status=cost_status,
                cost_source=cost_source,
                pricing_version=pricing_version,
                billing_provider=billing_provider,
                billing_base_url=billing_base_url,
                billing_mode=billing_mode,
                api_call_count=api_call_count,
                absolute=absolute,
            )
        self._execute_write(_do)

    def ensure_session(
        self,
        session_id: str,
        source: str = "unknown",
        model: str = None,
        **kwargs,
    ) -> str:
        """Ensure a session row exists (INSERT OR IGNORE). Accepts optional kwargs."""
        self._insert_session_row(session_id, source, model=model, **kwargs)
        return session_id

    def prune_empty_ghost_sessions(self, sessions_dir: "Optional[Path]" = None) -> int:
        """Remove empty TUI ghost sessions (no messages, no title, >24hr old)."""
        cutoff = time.time() - 86400  # Only sessions older than 24 hours

        def _do(conn):
            return SessionRepoImpl(conn).prune_empty_ghost_sessions(cutoff=cutoff)

        removed_ids = self._execute_write(_do) or []
        # Clean up any on-disk session files (belt-and-suspenders)
        if sessions_dir and removed_ids:
            for sid in removed_ids:
                self._remove_session_files(sessions_dir, sid)
        return len(removed_ids)

    def finalize_orphaned_compression_sessions(self) -> int:
        """Mark orphaned compression continuation sessions as ended.

        Targets child sessions that were never finalized: parent is ended
        with reason='compression', child has messages but no end_reason/ended_at
        and api_call_count=0.  Non-destructive: preserves all messages and sets
        end_reason='orphaned_compression'.  Fix for #20001.
        """
        def _do(conn):
            return SessionRepoImpl(conn).finalize_orphaned_compression_sessions()

        return self._execute_write(_do) or 0

    def get_session(self, session_id: str) -> Optional[Dict[str, Any]]:
        """Get a session by ID."""
        with self._lock:
            cursor = self._conn.execute(
                "SELECT * FROM sessions WHERE id = ?", (session_id,)
            )
            row = cursor.fetchone()
        return dict(row) if row else None

    def resolve_session_id(self, session_id_or_prefix: str) -> Optional[str]:
        """Resolve an exact or uniquely prefixed session ID to the full ID.

        Returns the exact ID when it exists. Otherwise treats the input as a
        prefix and returns the single matching session ID if the prefix is
        unambiguous. Returns None for no matches or ambiguous prefixes.
        """
        exact = self.get_session(session_id_or_prefix)
        if exact:
            return exact["id"]

        escaped = (
            session_id_or_prefix
            .replace("\\", "\\\\")
            .replace("%", "\\%")
            .replace("_", "\\_")
        )
        with self._lock:
            cursor = self._conn.execute(
                "SELECT id FROM sessions WHERE id LIKE ? ESCAPE '\\' ORDER BY started_at DESC LIMIT 2",
                (f"{escaped}%",),
            )
            matches = [row["id"] for row in cursor.fetchall()]
        if len(matches) == 1:
            return matches[0]
        return None

    # Maximum length for session titles
    MAX_TITLE_LENGTH = 100

    @staticmethod
    def sanitize_title(title: Optional[str]) -> Optional[str]:
        """Validate and sanitize a session title.

        - Strips leading/trailing whitespace
        - Removes ASCII control characters (0x00-0x1F, 0x7F) and problematic
          Unicode control chars (zero-width, RTL/LTR overrides, etc.)
        - Collapses internal whitespace runs to single spaces
        - Normalizes empty/whitespace-only strings to None
        - Enforces MAX_TITLE_LENGTH

        Returns the cleaned title string or None.
        Raises ValueError if the title exceeds MAX_TITLE_LENGTH after cleaning.
        """
        if not title:
            return None

        # Remove ASCII control characters (0x00-0x1F, 0x7F) but keep
        # whitespace chars (\t=0x09, \n=0x0A, \r=0x0D) so they can be
        # normalized to spaces by the whitespace collapsing step below
        cleaned = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', '', title)

        # Remove problematic Unicode control characters:
        # - Zero-width chars (U+200B-U+200F, U+FEFF)
        # - Directional overrides (U+202A-U+202E, U+2066-U+2069)
        # - Object replacement (U+FFFC), interlinear annotation (U+FFF9-U+FFFB)
        cleaned = re.sub(
            r'[\u200b-\u200f\u2028-\u202e\u2060-\u2069\ufeff\ufffc\ufff9-\ufffb]',
            '', cleaned,
        )

        # Collapse internal whitespace runs and strip
        cleaned = re.sub(r'\s+', ' ', cleaned).strip()

        if not cleaned:
            return None

        if len(cleaned) > HermesStateStore.MAX_TITLE_LENGTH:
            raise ValueError(
                f"Title too long ({len(cleaned)} chars, max {HermesStateStore.MAX_TITLE_LENGTH})"
            )

        return cleaned

    def set_session_title(self, session_id: str, title: str, *, title_source: str = "user") -> bool:
        """Set or update a session's title.

        Returns True if session was found and title was set.
        Raises ValueError if title is already in use by another session,
        or if the title fails validation (too long, invalid characters).
        Empty/whitespace-only strings are normalized to None (clearing the title).

        ``title`` remains the legacy unique Hermes title used by CLI resume and
        platform integrations. ``display_title`` is the non-unique product title
        Dovie shows in history. Auto-generated summary titles are no longer a
        valid write source; ``title_source="auto"`` is ignored so canonical
        titles cannot regress behind first-user-message display titles.
        """
        normalized_source = str(title_source or "user").strip().lower() or "user"
        if normalized_source == "auto":
            return False
        def _do(conn):
            return 1 if SessionRepoImpl(conn).set_title(
                session_id,
                title,
                title_source=normalized_source,
            ) else 0
        rowcount = self._execute_write(_do)
        return rowcount > 0

    def get_session_title(self, session_id: str) -> Optional[str]:
        """Get the title for a session, or None."""
        with self._lock:
            cursor = self._conn.execute(
                "SELECT title FROM sessions WHERE id = ?", (session_id,)
            )
            row = cursor.fetchone()
        return row["title"] if row else None

    def get_session_by_title(self, title: str) -> Optional[Dict[str, Any]]:
        """Look up a session by exact title. Returns session dict or None."""
        with self._lock:
            cursor = self._conn.execute(
                "SELECT * FROM sessions WHERE title = ?", (title,)
            )
            row = cursor.fetchone()
        return dict(row) if row else None

    def resolve_session_by_title(self, title: str) -> Optional[str]:
        """Resolve a title to a session ID, preferring the latest in a lineage.

        If the exact title exists, returns that session's ID.
        If not, searches for "title #N" variants and returns the latest one.
        If the exact title exists AND numbered variants exist, returns the
        latest numbered variant (the most recent continuation).
        """
        # First try exact match
        exact = self.get_session_by_title(title)

        # Also search for numbered variants: "title #2", "title #3", etc.
        # Escape SQL LIKE wildcards (%, _) in the title to prevent false matches
        escaped = title.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        with self._lock:
            cursor = self._conn.execute(
                "SELECT id, title, started_at FROM sessions "
                "WHERE title LIKE ? ESCAPE '\\' ORDER BY started_at DESC",
                (f"{escaped} #%",),
            )
            numbered = cursor.fetchall()

        if numbered:
            # Return the most recent numbered variant
            return numbered[0]["id"]
        elif exact:
            return exact["id"]
        return None

    def get_next_title_in_lineage(self, base_title: str) -> str:
        """Generate the next title in a lineage (e.g., "my session" → "my session #2").

        Strips any existing " #N" suffix to find the base name, then finds
        the highest existing number and increments.
        """
        # Strip existing #N suffix to find the true base
        match = re.match(r'^(.*?) #(\d+)$', base_title)
        if match:
            base = match.group(1)
        else:
            base = base_title

        # Find all existing numbered variants
        # Escape SQL LIKE wildcards (%, _) in the base to prevent false matches
        escaped = base.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        with self._lock:
            cursor = self._conn.execute(
                "SELECT title FROM sessions WHERE title = ? OR title LIKE ? ESCAPE '\\'",
                (base, f"{escaped} #%"),
            )
            existing = [row["title"] for row in cursor.fetchall()]

        if not existing:
            return base  # No conflict, use the base name as-is

        # Find the highest number
        max_num = 1  # The unnumbered original counts as #1
        for t in existing:
            m = re.match(r'^.* #(\d+)$', t)
            if m:
                max_num = max(max_num, int(m.group(1)))

        return f"{base} #{max_num + 1}"

    def get_compression_tip(self, session_id: str) -> Optional[str]:
        """Walk the compression-continuation chain forward and return the tip.

        A compression continuation is a child session where:
        1. The parent's ``end_reason = 'compression'``
        2. The child was created AFTER the parent was ended (started_at >= ended_at)

        The second condition distinguishes compression continuations from
        delegate subagents or branch children, which can also have a
        ``parent_session_id`` but were created while the parent was still live.

        Returns the session_id of the latest continuation in the chain, or the
        input ``session_id`` if it isn't part of a compression chain (or if the
        input itself doesn't exist).
        """
        current = session_id
        # Bound the walk defensively — compression chains this deep are
        # pathological and shouldn't happen in practice. 100 = plenty.
        for _ in range(100):
            with self._lock:
                cursor = self._conn.execute(
                    "SELECT id FROM sessions "
                    "WHERE parent_session_id = ? "
                    "  AND started_at >= ("
                    "      SELECT ended_at FROM sessions "
                    "      WHERE id = ? AND end_reason = 'compression'"
                    "  ) "
                    "ORDER BY started_at DESC LIMIT 1",
                    (current, current),
                )
                row = cursor.fetchone()
            if row is None:
                return current
            current = row["id"]
        return current

    def list_sessions_rich(
        self,
        source: str = None,
        exclude_sources: List[str] = None,
        limit: int = 20,
        offset: int = 0,
        include_children: bool = False,
        project_compression_tips: bool = True,
        order_by_last_active: bool = False,
        page_cursor: Optional[Dict[str, Any]] = None,
        id_query: str = None,
    ) -> List[Dict[str, Any]]:
        return SessionListReadModel(self._conn).list(
            SessionListQuery(
                source=source,
                exclude_sources=tuple(exclude_sources or ()),
                limit=limit,
                offset=offset,
                include_children=include_children,
                project_compression_tips=project_compression_tips,
                order_by_last_active=order_by_last_active,
                page_cursor=page_cursor,
                id_query=id_query,
            )
        )

    # ------------------------------------------------------------------
    # Control-plane session_index (write-time projection; single-query read)
    # ------------------------------------------------------------------
    _SESSION_INDEX_COLUMNS = (
        "session_id", "owner_agent_profile_id", "owner_profile_version_id",
        "runtime_scope_key", "title", "preview", "source", "transient",
        "session_kind", "conversation_kind", "status", "running",
        "waiting_approval", "active_run_id", "active_execution_session_id",
        "pending_approval_count", "team_id", "mission_id", "conversation_id",
        "message_count", "started_at", "updated_at", "last_activity",
    )

    @staticmethod
    def _session_index_row_to_item(row: sqlite3.Row) -> Dict[str, Any]:
        item = {key: row[key] for key in row.keys()}
        for flag in (
            "transient", "running", "waiting_approval",
            "derived_running", "derived_waiting_approval",
        ):
            if flag in item:
                item[flag] = bool(item.get(flag))
        for count_field in ("active_activity_count", "unread_completion_count"):
            item[count_field] = int(item.get(count_field) or 0)
        if (
            item.get("conversation_kind") == "team"
            and item.get("conversation_id")
            and "conversation_has_active_mission" in item
        ):
            item["running"] = bool(item.get("conversation_has_active_mission"))
        item.pop("conversation_has_active_mission", None)
        team_context = {
            "team_id": str(item.get("team_context_team_id") or ""),
            "team_conversation_id": str(item.get("team_context_conversation_id") or ""),
            "mission_id": str(item.get("team_context_mission_id") or ""),
            "member_id": str(item.get("team_context_member_id") or ""),
        }
        item["team_context"] = (
            team_context
            if any(team_context.values()) or item.get("conversation_kind") == "team"
            else None
        )
        item["derived_state"] = {
            "running": bool(item.get("derived_running")),
            "waiting_approval": bool(item.get("derived_waiting_approval")),
            "terminal_status": item.get("derived_terminal_status") or None,
        }
        item["_page_cursor"] = {
            "updated_at": row["updated_at"],
            "started_at": row["started_at"],
            "session_id": row["session_id"],
        }
        return item

    def upsert_session_index(
        self,
        *,
        session_id: str,
        owner_agent_profile_id: str = "",
        owner_profile_version_id: str = "",
        runtime_scope_key: str = "",
        title: str = "",
        preview: str = "",
        source: str = "unknown",
        transient: bool = False,
        session_kind: str = "hermes_session",
        conversation_kind: Optional[str] = None,
        status: str = "idle",
        running: bool = False,
        waiting_approval: bool = False,
        active_run_id: str = "",
        active_execution_session_id: str = "",
        pending_approval_count: int = 0,
        team_id: str = "",
        mission_id: str = "",
        conversation_id: str = "",
        message_count: int = 0,
        started_at: Optional[float] = None,
        updated_at: Optional[float] = None,
        last_activity: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Full upsert of a control-plane session_index row (idempotent by id)."""
        sid = str(session_id or "").strip()
        if not sid:
            raise ValueError("session_id required for upsert_session_index")
        now = time.time()
        started = float(started_at if started_at is not None else now)
        updated = float(updated_at if updated_at is not None else now)
        normalized_source = str(source or "unknown")
        normalized_session_kind = str(session_kind or "hermes_session")
        normalized_conversation_kind = str(conversation_kind or "").strip().lower()
        if normalized_conversation_kind not in {"direct", "team"}:
            normalized_conversation_kind = (
                "team"
                if normalized_source == "team_mission" or normalized_session_kind == "team_mission"
                else "direct"
            )
        values = {
            "session_id": sid,
            "owner_agent_profile_id": str(owner_agent_profile_id or ""),
            "owner_profile_version_id": str(owner_profile_version_id or ""),
            "runtime_scope_key": str(runtime_scope_key or ""),
            "title": str(title or ""),
            "preview": str(preview or ""),
            "source": normalized_source,
            "transient": 1 if transient else 0,
            "session_kind": normalized_session_kind,
            "conversation_kind": normalized_conversation_kind,
            "status": str(status or "idle"),
            "running": 1 if running else 0,
            "waiting_approval": 1 if waiting_approval else 0,
            "active_run_id": str(active_run_id or ""),
            "active_execution_session_id": str(active_execution_session_id or ""),
            "pending_approval_count": int(pending_approval_count or 0),
            "team_id": str(team_id or ""),
            "mission_id": str(mission_id or ""),
            "conversation_id": str(conversation_id or ""),
            "message_count": int(message_count or 0),
            "started_at": started,
            "updated_at": updated,
            "last_activity": last_activity,
        }

        def _do(conn: sqlite3.Connection) -> Dict[str, Any]:
            return SessionRepoImpl(conn).upsert_session_index(values)

        return self._execute_write(_do)

    def delete_session_index(self, session_id: str) -> int:
        sid = str(session_id or "").strip()
        if not sid:
            return 0

        def _do(conn: sqlite3.Connection) -> int:
            return 1 if SessionRepoImpl(conn).delete_index(sid) else 0

        return self._execute_write(_do)

    def get_session_index(self, session_id: str) -> Optional[Dict[str, Any]]:
        """Return the exact control-plane session_index row for a session."""
        sid = str(session_id or "").strip()
        if not sid:
            return None
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM session_index WHERE session_id = ?",
                (sid,),
            ).fetchone()
        return self._session_index_row_to_item(row) if row else None

    def _repair_session_index_terminal_active_runs_locked(self, conn: sqlite3.Connection) -> int:
        """Clear stale sidebar state once its conversation has no active runs.

        Team mission rows need a narrower rule than regular chat rows: mission
        cancel is activity-scoped, so a terminal run must not collapse the whole
        conversation while a sibling mission or another conversation run remains
        active.
        """
        try:
            return SessionIndexReconciler(conn).repair_terminal_active_runs()
        except sqlite3.OperationalError:
            return 0

    def list_session_index(
        self,
        *,
        limit: int = 200,
        cursor: Optional[Dict[str, Any]] = None,
        include_transient: bool = False,
        conversation_kind: Optional[str] = None,
    ) -> Dict[str, Any]:
        with self._lock:
            self._repair_session_index_terminal_active_runs_locked(self._conn)
            repair_team_runtime_scope = getattr(
                self,
                "_repair_session_index_active_team_runtime_scope_locked",
                None,
            )
            if callable(repair_team_runtime_scope):
                repair_team_runtime_scope(self._conn)
            return SessionIndexReadModel(self._conn).list(
                SessionIndexQuery(
                    limit=limit,
                    cursor=cursor,
                    include_transient=include_transient,
                    conversation_kind=conversation_kind,
                )
            )

    def reconcile_session_index(
        self,
        *,
        exclude_sources: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Backfill/repair the index from the source of truth."""

        def _do(conn: sqlite3.Connection) -> Dict[str, Any]:
            return SessionIndexReconciler(conn).reconcile(exclude_sources=exclude_sources)

        return self._execute_write(_do)

    def _get_session_rich_row(self, session_id: str) -> Optional[Dict[str, Any]]:
        """Fetch a single session with the same enriched columns as
        ``list_sessions_rich`` (preview + last_active). Returns None if the
        session doesn't exist.
        """
        query = """
            SELECT s.*,
                COALESCE(s.preview, '') AS _preview_summary,
                COALESCE(s.last_active, s.started_at) AS _last_active_summary
            FROM sessions s
            WHERE s.id = ?
        """
        with self._lock:
            cursor = self._conn.execute(query, (session_id,))
            row = cursor.fetchone()
        if not row:
            return None
        s = dict(row)
        s["preview"] = str(s.pop("_preview_summary", s.get("preview") or "") or "")
        s["last_active"] = s.pop("_last_active_summary", s.get("last_active") or s.get("started_at") or 0)
        return s

    def search_sessions(
        self,
        source: str = None,
        limit: int = 20,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        """List sessions, optionally filtered by source.

        Returns rows with the denormalized ``last_active`` list field,
        falling back to ``started_at``, ordered by most-recently-used first.
        """
        select_with_last_active = (
            "SELECT s.*, COALESCE(s.last_active, s.started_at) AS _last_active_summary "
            "FROM sessions s "
        )
        with self._lock:
            if source:
                cursor = self._conn.execute(
                    f"{select_with_last_active}"
                    "WHERE s.source = ? "
                    "ORDER BY _last_active_summary DESC, s.started_at DESC, s.id DESC LIMIT ? OFFSET ?",
                    (source, limit, offset),
                )
            else:
                cursor = self._conn.execute(
                    f"{select_with_last_active}"
                    "ORDER BY _last_active_summary DESC, s.started_at DESC, s.id DESC LIMIT ? OFFSET ?",
                    (limit, offset),
                )
            rows = cursor.fetchall()
        sessions = []
        for row in rows:
            session = dict(row)
            session["last_active"] = session.pop(
                "_last_active_summary",
                session.get("last_active") or session.get("started_at") or 0,
            )
            sessions.append(session)
        return sessions

    # =========================================================================
    # Utility
    # =========================================================================

    def session_count(self, source: str = None) -> int:
        """Count sessions, optionally filtered by source."""
        with self._lock:
            if source:
                cursor = self._conn.execute(
                    "SELECT COUNT(*) FROM sessions WHERE source = ?", (source,)
                )
            else:
                cursor = self._conn.execute("SELECT COUNT(*) FROM sessions")
            return cursor.fetchone()[0]

    def message_count(self, session_id: str = None) -> int:
        """Count messages, optionally for a specific session."""
        with self._lock:
            if session_id:
                cursor = self._conn.execute(
                    "SELECT COUNT(*) FROM messages WHERE session_id = ?", (session_id,)
                )
            else:
                cursor = self._conn.execute("SELECT COUNT(*) FROM messages")
            return cursor.fetchone()[0]

    # =========================================================================
    # Export and cleanup
    # =========================================================================

    def export_session(self, session_id: str) -> Optional[Dict[str, Any]]:
        """Export a single session with all its messages as a dict."""
        session = self.get_session(session_id)
        if not session:
            return None
        messages = self.get_messages(session_id)
        return {**session, "messages": messages}

    def export_all(self, source: str = None) -> List[Dict[str, Any]]:
        """
        Export all sessions (with messages) as a list of dicts.
        Suitable for writing to a JSONL file for backup/analysis.
        """
        sessions = self.search_sessions(source=source, limit=100000)
        results = []
        for session in sessions:
            messages = self.get_messages(session["id"])
            results.append({**session, "messages": messages})
        return results

    def clear_messages(self, session_id: str) -> None:
        """Delete all messages for a session and reset its counters."""
        def _do(conn):
            MessageRepoImpl(conn).delete_by_session(session_id)
            SessionRepoImpl(conn).reset_message_projection(session_id)
        self._execute_write(_do)

    @staticmethod
    def _remove_session_files(sessions_dir: Optional[Path], session_id: str) -> None:
        """Remove on-disk transcript files for a session.

        Cleans up ``{session_id}.json``, ``{session_id}.jsonl``, and any
        ``request_dump_{session_id}_*.json`` files left by the gateway.
        Silently skips files that don't exist and swallows OSError so a
        filesystem hiccup never blocks a DB operation.
        """
        if sessions_dir is None:
            return
        for suffix in (".json", ".jsonl"):
            p = sessions_dir / f"{session_id}{suffix}"
            try:
                p.unlink(missing_ok=True)
            except OSError as exc:
                logger.debug("failed to remove state session file %s: %s", p, exc)
        # request_dump files use session_id as a prefix component
        try:
            for p in sessions_dir.glob(f"request_dump_{session_id}_*.json"):
                try:
                    p.unlink(missing_ok=True)
                except OSError as exc:
                    logger.debug("failed to remove state request dump %s: %s", p, exc)
        except OSError as exc:
            logger.debug("failed to enumerate state request dumps for %s: %s", session_id, exc)

    def delete_session(
        self,
        session_id: str,
        sessions_dir: Optional[Path] = None,
    ) -> bool:
        """Delete a session and all its messages.

        Child sessions are orphaned (parent_session_id set to NULL) rather
        than cascade-deleted, so they remain accessible independently.
        When *sessions_dir* is provided, also removes on-disk transcript
        files (``.json`` / ``.jsonl`` / ``request_dump_*``) for the deleted
        session. Returns True if the session was found and deleted.
        """
        stable = str(session_id or "").strip()
        if not stable:
            return False
        return SessionDeletionService(self._conn).delete(
            stable,
            sessions_dir=sessions_dir,
        ).session_deleted

    def prune_sessions(
        self,
        older_than_days: int = 90,
        source: str = None,
        sessions_dir: Optional[Path] = None,
    ) -> int:
        """Delete sessions older than N days. Returns count of deleted sessions.

        Only prunes ended sessions (not active ones).  Child sessions outside
        the prune window are orphaned (parent_session_id set to NULL) rather
        than cascade-deleted.  When *sessions_dir* is provided, also removes
        on-disk transcript files (``.json`` / ``.jsonl`` /
        ``request_dump_*``) for every pruned session, outside the DB
        transaction.
        """
        cutoff = time.time() - (older_than_days * 86400)
        removed_ids: list[str] = []

        if source:
            cursor = self._conn.execute(
                """SELECT id FROM sessions
                   WHERE started_at < ? AND ended_at IS NOT NULL AND source = ?""",
                (cutoff, source),
            )
        else:
            cursor = self._conn.execute(
                "SELECT id FROM sessions WHERE started_at < ? AND ended_at IS NOT NULL",
                (cutoff,),
            )
        removed_ids = [str(row["id"] or "") for row in cursor.fetchall() if str(row["id"] or "")]
        if not removed_ids:
            return 0
        deleted = 0
        deletion = SessionDeletionService(self._conn)
        for sid in removed_ids:
            if deletion.delete(sid, sessions_dir=sessions_dir).session_deleted:
                deleted += 1
        return deleted

    # ── Meta key/value (for scheduler bookkeeping) ──

    def get_meta(self, key: str) -> Optional[str]:
        """Read a value from the state_meta key/value store."""
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM state_meta WHERE key = ?", (key,)
            ).fetchone()
        if row is None:
            return None
        return row["value"] if isinstance(row, sqlite3.Row) else row[0]

    def set_meta(self, key: str, value: str) -> None:
        """Write a value to the state_meta key/value store."""
        def _do(conn):
            conn.execute(
                "INSERT INTO state_meta (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )
        self._execute_write(_do)

from hermes_agent.domain.compression_lock import install_compression_lock_methods

install_compression_lock_methods(HermesStateStore)
