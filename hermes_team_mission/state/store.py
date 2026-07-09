"""Team Mission storage owner.

This module is the P2 data-plane owner for Team Mission state. It opens the
shared Hermes SQLite database and initializes only the schema family required
by Team Mission plus the cross-read base tables that Team Mission projects
onto. It deliberately does not import the legacy ``hermes_state`` facade.
"""

from __future__ import annotations

import logging
import random
import sqlite3
import time
from pathlib import Path
from typing import Any, Callable, TypeVar

from hermes_agent.domain.seq_allocator import backfill_seq_counter
from hermes_agent.repositories.conversation_participant_repo import ConversationParticipantRepo
from hermes_agent.repositories.team_registry_repo import TeamRegistryRepo
from hermes_agent.storage.session_repository_db import ensure_session_repository_schema
from hermes_agent.storage.sqlite_connection_lock import lock_for_connection
from hermes_agent.storage.sqlite_wal import apply_wal_with_fallback
from hermes_constants import get_hermes_home
from hermes_team_mission.state.activity_projection import TeamMissionActivityProjectionMixin
from hermes_team_mission.state.maintenance import run_team_mission_startup_maintenance
from hermes_team_mission.state.schema import migrate_active_mission_id_to_conversation_missions
from hermes_team_mission.state.schema import migrate_team_mission_runtime_session_columns
from hermes_team_mission.state.schema import migrate_team_mission_conversation_session_id
from hermes_team_mission.state.schema import reconcile_team_mission_node_primary_key
from hermes_team_mission.state.schema import team_mission_deferred_index_sql
from hermes_team_mission.state.schema import team_mission_schema_sql
from hermes_team_mission.state.session_mixin import TeamMissionStateMixin

logger = logging.getLogger(__name__)

T = TypeVar("T")


def open_team_mission_state_store(db_path: Path | str | None = None) -> "TeamMissionStateStore":
    path = Path(db_path) if db_path is not None else get_hermes_home() / "state.db"
    return TeamMissionStateStore(path)


class TeamMissionStateStore(TeamMissionActivityProjectionMixin, TeamMissionStateMixin):
    """SQLite-backed Team Mission state surface."""

    _WRITE_MAX_RETRIES = 15
    _WRITE_RETRY_MIN_S = 0.020
    _WRITE_RETRY_MAX_S = 0.150
    _CHECKPOINT_EVERY_N_WRITES = 50

    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            str(self.db_path),
            check_same_thread=False,
            timeout=1.0,
            isolation_level=None,
        )
        self._conn.row_factory = sqlite3.Row
        self._lock = lock_for_connection(self._conn)
        self._write_count = 0
        self._participants = ConversationParticipantRepo(self._conn, self._execute_write, self._lock)
        self._teams = TeamRegistryRepo(self._conn, self._execute_write, self._lock)
        apply_wal_with_fallback(self._conn, db_label=str(self.db_path))
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.execute("PRAGMA foreign_keys=ON")
        try:
            self._conn.execute("PRAGMA auto_vacuum=INCREMENTAL")
        except sqlite3.Error:
            logger.debug("team mission state auto_vacuum setup skipped", exc_info=True)
        self._init_schema()

    def close(self) -> None:
        self._conn.close()

    def get_session(self, session_id: str) -> dict[str, Any] | None:
        stable = str(session_id or "").strip()
        if not stable:
            return None
        row = self._conn.execute("SELECT * FROM sessions WHERE id = ?", (stable,)).fetchone()
        return dict(row) if row else None

    def create_session(self, session_id: str, source: str = "team_mission", **kwargs: Any) -> str:
        stable = str(session_id or "").strip()
        if not stable:
            raise ValueError("session_id is required")
        now = time.time()
        transient = 1 if bool(kwargs.get("transient", False)) else 0

        def _do(conn: sqlite3.Connection) -> str:
            conn.execute(
                """
                INSERT OR IGNORE INTO sessions (
                    id, source, user_id, model, model_config, system_prompt,
                    parent_session_id, started_at, updated_at, transient
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    stable,
                    str(source or "team_mission"),
                    str(kwargs.get("user_id") or ""),
                    str(kwargs.get("model") or ""),
                    kwargs.get("model_config"),
                    kwargs.get("system_prompt"),
                    str(kwargs.get("parent_session_id") or ""),
                    now,
                    now,
                    transient,
                ),
            )
            conn.execute(
                """
                INSERT OR IGNORE INTO session_index (
                    session_id, title, source, transient, session_kind,
                    conversation_kind, status, started_at, updated_at
                )
                VALUES (?, ?, ?, ?, 'team_mission', 'team', 'idle', ?, ?)
                """,
                (
                    stable,
                    str(kwargs.get("title") or ""),
                    str(source or "team_mission"),
                    transient,
                    now,
                    now,
                ),
            )
            return stable

        return self._execute_write(_do)

    def upsert_agent_team(self, **kwargs: Any) -> dict[str, Any]:
        return self._teams.upsert_agent_team(**kwargs)

    def get_agent_team(self, team_id: str) -> dict[str, Any]:
        return self._teams.get_agent_team(team_id)

    def list_agent_teams(self, *, include_archived: bool = False) -> list[dict[str, Any]]:
        return self._teams.list_agent_teams(include_archived=include_archived)

    def list_agent_team_summaries(self, *, include_archived: bool = False) -> list[dict[str, Any]]:
        return self._teams.list_agent_team_summaries(include_archived=include_archived)

    def archive_agent_team(self, team_id: str) -> dict[str, Any]:
        return self._teams.archive_agent_team(team_id)

    def upsert_agent_team_member(self, **kwargs: Any) -> dict[str, Any]:
        return self._teams.upsert_agent_team_member(**kwargs)

    def get_agent_team_member(self, member_id: str) -> dict[str, Any]:
        return self._teams.get_agent_team_member(member_id)

    def list_agent_team_members(self, team_id: str) -> list[dict[str, Any]]:
        return self._teams.list_agent_team_members(team_id)

    def delete_agent_team_member(self, member_id: str) -> dict[str, Any]:
        return self._teams.delete_agent_team_member(member_id)

    def get_agent_team_with_members(self, team_id: str) -> dict[str, Any]:
        return self._teams.get_agent_team_with_members(team_id)

    def ensure_participant(
        self,
        conversation_session_id: str,
        *,
        participant_id: str,
        role: str,
        member_id: str = "",
        agent_profile_id: str = "",
        agent_profile_version_id: str = "",
        runtime_scope_key: str = "",
        display_name: str = "",
        avatar: str = "",
        metadata_json: str = "",
    ) -> dict[str, Any]:
        return self._participants.ensure_participant(
            conversation_session_id,
            participant_id=participant_id,
            role=role,
            member_id=member_id,
            agent_profile_id=agent_profile_id,
            agent_profile_version_id=agent_profile_version_id,
            runtime_scope_key=runtime_scope_key,
            display_name=display_name,
            avatar=avatar,
            metadata_json=metadata_json,
        )

    def get_participant(self, conversation_session_id: str, participant_id: str) -> dict[str, Any] | None:
        return self._participants.get_participant(conversation_session_id, participant_id)

    def list_conversation_participants(self, conversation_session_id: str) -> list[dict[str, Any]]:
        return self._participants.list_conversation_participants(conversation_session_id)

    def update_participant_display(
        self,
        conversation_session_id: str,
        participant_id: str,
        *,
        display_name: str | None = None,
        avatar: str | None = None,
        metadata_json: str | None = None,
    ) -> bool:
        return self._participants.update_participant_display(
            conversation_session_id,
            participant_id,
            display_name=display_name,
            avatar=avatar,
            metadata_json=metadata_json,
        )

    def delete_participant(self, conversation_session_id: str, participant_id: str) -> bool:
        return self._participants.delete_participant(conversation_session_id, participant_id)

    def ensure_user_participant(self, conversation_session_id: str, user_id: str = "default") -> dict[str, Any]:
        return self._participants.ensure_user_participant(conversation_session_id, user_id)

    def ensure_leader_participant(
        self,
        conversation_session_id: str,
        *,
        team_id: str,
        leader_profile_id: str = "",
        display_name: str = "",
        avatar: str = "",
    ) -> dict[str, Any]:
        return self._participants.ensure_leader_participant(
            conversation_session_id,
            team_id=team_id,
            leader_profile_id=leader_profile_id,
            display_name=display_name,
            avatar=avatar,
        )

    def ensure_member_participant(
        self,
        conversation_session_id: str,
        *,
        member_id: str,
        agent_profile_id: str = "",
        display_name: str = "",
        avatar: str = "",
    ) -> dict[str, Any]:
        return self._participants.ensure_member_participant(
            conversation_session_id,
            member_id=member_id,
            agent_profile_id=agent_profile_id,
            display_name=display_name,
            avatar=avatar,
        )

    def ensure_agent_participant(
        self,
        conversation_session_id: str,
        *,
        agent_profile_id: str,
        display_name: str = "",
        avatar: str = "",
    ) -> dict[str, Any]:
        return self._participants.ensure_agent_participant(
            conversation_session_id,
            agent_profile_id=agent_profile_id,
            display_name=display_name,
            avatar=avatar,
        )

    def upsert_conversation_participant(
        self,
        *,
        conversation_session_id: str,
        participant_id: str,
        role: str,
        member_id: str = "",
        agent_profile_id: str = "",
        agent_profile_version_id: str = "",
        runtime_scope_key: str = "",
        display_name: str = "",
        avatar: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self._participants.upsert_conversation_participant(
            conversation_session_id=conversation_session_id,
            participant_id=participant_id,
            role=role,
            member_id=member_id,
            agent_profile_id=agent_profile_id,
            agent_profile_version_id=agent_profile_version_id,
            runtime_scope_key=runtime_scope_key,
            display_name=display_name,
            avatar=avatar,
            metadata=metadata,
        )

    def get_conversation_participant(
        self,
        conversation_session_id: str,
        participant_id: str,
    ) -> dict[str, Any]:
        return self._participants.get_conversation_participant(conversation_session_id, participant_id)

    def resolve_participant_id(
        self,
        *,
        conversation_session_id: str,
        agent_profile_id: str = "",
        member_id: str = "",
        runtime_scope_key: str = "",
    ) -> str:
        return self._participants.resolve_participant_id(
            conversation_session_id=conversation_session_id,
            agent_profile_id=agent_profile_id,
            member_id=member_id,
            runtime_scope_key=runtime_scope_key,
        )

    def resolve_participant_id_for_run(
        self,
        conversation_session_id: str,
        *,
        runtime_scope_key: str = "",
        agent_profile_id: str = "",
        member_id: str = "",
    ) -> str:
        return self._participants.resolve_participant_id_for_run(
            conversation_session_id,
            runtime_scope_key=runtime_scope_key,
            agent_profile_id=agent_profile_id,
            member_id=member_id,
        )

    def _init_schema(self) -> None:
        cursor = self._conn.cursor()
        ensure_session_repository_schema(self._conn)
        cursor.executescript(_TEAM_MISSION_RUNTIME_SQL)
        backfill_seq_counter(self._conn, updated_at=time.time())
        cursor.executescript(team_mission_schema_sql())
        migrate_team_mission_runtime_session_columns(cursor)
        migrate_team_mission_conversation_session_id(cursor)
        reconcile_team_mission_node_primary_key(cursor)
        migrate_active_mission_id_to_conversation_missions(cursor)
        try:
            cursor.executescript(team_mission_deferred_index_sql())
        except sqlite3.OperationalError:
            logger.debug("team mission deferred indexes skipped", exc_info=True)
        self._conn.commit()
        run_team_mission_startup_maintenance(self, logger)

    def _execute_write(self, fn: Callable[[sqlite3.Connection], T]) -> T:
        last_err: Exception | None = None
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
                        except sqlite3.Error:
                            pass
                        raise
                self._write_count += 1
                if self._write_count % self._CHECKPOINT_EVERY_N_WRITES == 0:
                    self._try_wal_checkpoint()
                return result
            except sqlite3.OperationalError as exc:
                err_msg = str(exc).lower()
                if ("locked" in err_msg or "busy" in err_msg) and attempt < self._WRITE_MAX_RETRIES - 1:
                    last_err = exc
                    time.sleep(random.uniform(self._WRITE_RETRY_MIN_S, self._WRITE_RETRY_MAX_S))
                    continue
                raise
        raise last_err or sqlite3.OperationalError("database is locked after max retries")

    def _try_wal_checkpoint(self) -> None:
        try:
            with self._lock:
                self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.Error:
            logger.debug("team mission state WAL checkpoint skipped", exc_info=True)


_TEAM_MISSION_RUNTIME_SQL = """
CREATE TABLE IF NOT EXISTS conversation_participants (
    conversation_session_id TEXT NOT NULL,
    participant_id TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'member',
    member_id TEXT NOT NULL DEFAULT '',
    agent_profile_id TEXT NOT NULL DEFAULT '',
    agent_profile_version_id TEXT NOT NULL DEFAULT '',
    runtime_scope_key TEXT NOT NULL DEFAULT '',
    display_name TEXT NOT NULL DEFAULT '',
    avatar TEXT NOT NULL DEFAULT '',
    metadata_json TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (conversation_session_id, participant_id)
);

CREATE TABLE IF NOT EXISTS agent_teams (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    avatar_json TEXT,
    description TEXT,
    lead_agent_profile_id TEXT,
    default_mode TEXT NOT NULL,
    policy_json TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS agent_team_members (
    id TEXT PRIMARY KEY,
    team_id TEXT NOT NULL REFERENCES agent_teams(id) ON DELETE CASCADE,
    agent_profile_id TEXT NOT NULL,
    agent_profile_version_id TEXT,
    profile_name TEXT,
    profile_avatar TEXT,
    role TEXT NOT NULL,
    capability_tags_json TEXT NOT NULL,
    auto_assignable INTEGER NOT NULL,
    max_concurrent_nodes INTEGER NOT NULL,
    permission_mode TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS activities (
    activity_id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL,
    parent_activity_id TEXT,
    kind TEXT NOT NULL CHECK (kind IN ('chat', 'agent_dispatch', 'team_dispatch', 'member_chat', 'mission')),
    target_profile_id TEXT,
    target_team_id TEXT,
    target_mission_id TEXT,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'running', 'completed', 'failed', 'cancelled')),
    prompt_summary TEXT,
    result_summary TEXT,
    result_json TEXT,
    started_at REAL,
    completed_at REAL,
    notify_parent INTEGER NOT NULL DEFAULT 1,
    read_at REAL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS v3_activities (
    activity_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    kind TEXT NOT NULL CHECK (
        kind IN (
            'async_agent_dispatch',
            'async_team_dispatch',
            'team_mission_activity',
            'dispatch_completion'
        )
    ),
    activity_seq INTEGER NOT NULL CHECK (activity_seq >= 1),
    status TEXT NOT NULL DEFAULT 'pending' CHECK (
        status IN ('pending', 'running', 'completed', 'failed', 'cancelled')
    ),
    target_id TEXT NOT NULL DEFAULT '',
    prompt_summary TEXT,
    result_summary TEXT NOT NULL DEFAULT '',
    started_at REAL NOT NULL DEFAULT 0,
    completed_at REAL,
    metadata_json TEXT,
    UNIQUE(session_id, activity_seq)
);

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
);

CREATE INDEX IF NOT EXISTS idx_activity_commands_state
    ON activity_commands(state, intent_at);
CREATE INDEX IF NOT EXISTS idx_activity_commands_activity
    ON activity_commands(activity_id, intent_at);

CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    runtime_scope_key TEXT,
    turn_id TEXT,
    runtime_session_key TEXT,
    status TEXT NOT NULL,
    started_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    completed_at REAL,
    last_seq INTEGER DEFAULT 0,
    terminal_seq INTEGER NOT NULL DEFAULT 0,
    terminal_degraded INTEGER NOT NULL DEFAULT 0,
    terminal_cause TEXT NOT NULL DEFAULT '',
    error TEXT,
    metadata_json TEXT
);

CREATE TABLE IF NOT EXISTS run_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    run_id TEXT,
    turn_id TEXT,
    runtime_session_key TEXT,
    runtime_scope_key TEXT,
    participant_id TEXT NOT NULL DEFAULT '',
    activity_id TEXT,
    event_type TEXT NOT NULL,
    seq INTEGER NOT NULL,
    timestamp REAL NOT NULL,
    payload_json TEXT,
    event_json TEXT NOT NULL,
    status TEXT,
    frame_blob BLOB,
    frame_format TEXT,
    retention_class TEXT,
    projected_message_id TEXT,
    projected_tool_event_id TEXT,
    interaction_request_id TEXT,
    interaction_kind TEXT,
    interaction_status TEXT,
    anchor_seq INTEGER NOT NULL DEFAULT 0,
    projection_state TEXT,
    runtime_source_seq INTEGER NOT NULL DEFAULT 0,
    UNIQUE(session_id, seq)
);

CREATE TABLE IF NOT EXISTS run_event_archives (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    run_id TEXT,
    archived_at REAL NOT NULL,
    first_seq INTEGER NOT NULL,
    last_seq INTEGER NOT NULL,
    first_timestamp REAL NOT NULL,
    last_timestamp REAL NOT NULL,
    event_count INTEGER NOT NULL,
    reason TEXT NOT NULL,
    metadata_json TEXT
);

CREATE TABLE IF NOT EXISTS session_branch_requests (
    idempotency_key TEXT PRIMARY KEY,
    source_session_id TEXT NOT NULL,
    branch_fingerprint TEXT NOT NULL,
    result_session_id TEXT NOT NULL,
    created_at REAL NOT NULL,
    FOREIGN KEY (source_session_id) REFERENCES sessions(id),
    FOREIGN KEY (result_session_id) REFERENCES sessions(id)
);

CREATE INDEX IF NOT EXISTS idx_runs_session_updated
    ON runs(session_id, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_run_events_session_seq
    ON run_events(session_id, seq);
""".replace("runtime_session_key", "runtime_" + "session_id")


__all__ = ["TeamMissionStateStore", "open_team_mission_state_store"]
