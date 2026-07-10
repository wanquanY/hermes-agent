"""SessionRepo protocol + concrete impl (spec §4.1) — owns ``sessions``.

Two exports:

* ``SessionRepo`` — Protocol (structural type) used by callers to depend
  on the interface without importing the concrete class.
* ``SessionRepoImpl`` — SQLite-backed implementation. Instantiated with a
  ``RepositoryConnection``; owns only the ``sessions`` + ``session_index``
  tables (spec §4.1). Cross-table joins live in L2 domain services.

``session_id: str`` is the only domain identifier accepted by this repository.
Wire-only legacy aliases are folded before requests enter the domain layer.
"""

from __future__ import annotations

import re
import sqlite3
import time
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Iterable, Protocol, runtime_checkable

from hermes_agent.domain.run_state_machine import TERMINAL_RUN_STATUSES
from hermes_agent.domain.session_runtime_state import session_info_record
from hermes_agent.repositories.base import RepositoryConnection

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SessionSpec:
    """Creation payload for a new session (spec §4.1)."""

    session_id: str
    source: str
    title: str = ""
    display_title: str = ""
    display_title_source: str = ""
    user_id: str = ""
    model: str = ""
    model_config: dict[str, Any] | str | None = None
    transient: bool = False
    session_kind: str = "hermes_session"
    conversation_kind: str = "direct"
    owner_agent_profile_id: str = ""
    owner_profile_version_id: str = ""
    runtime_scope_key: str = ""
    parent_session_id: str = ""


@dataclass(frozen=True)
class Session:
    """Session projection returned by the repo. Read-only snapshot."""

    session_id: str
    source: str
    title: str
    display_title: str
    session_kind: str
    conversation_kind: str
    started_at: float
    updated_at: float
    ended_at: float | None = None
    parent_session_id: str = ""


@dataclass(frozen=True)
class SessionFilter:
    """Filter for list()."""

    source: str | None = None
    session_kind: str | None = None
    conversation_kind: str | None = None
    include_ended: bool = False
    limit: int = 100


@dataclass(frozen=True)
class SessionIndexPatch:
    """Partial update to session_index. Missing fields leave the value unchanged."""

    title: str | None = None
    preview: str | None = None
    status: str | None = None
    running: int | None = None
    waiting_approval: int | None = None
    active_run_id: str | None = None
    active_execution_session_id: str | None = None
    pending_approval_count: int | None = None
    message_count: int | None = None
    last_activity: float | None = None
    fields: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SessionMessageAppendProjection:
    """Session-owned projection update for one appended transcript message."""

    timestamp: float
    tool_call_count: int = 0
    user_preview: str = ""
    user_display_title: str = ""


@dataclass(frozen=True)
class SessionMessageSnapshotProjection:
    """Session-owned projection replacement for a rewritten transcript."""

    message_count: int
    tool_call_count: int
    first_user_preview: str = ""
    first_user_display_title: str = ""
    last_message_ts: float | None = None


@dataclass(frozen=True)
class SessionRunProjection:
    """Session-index projection for a run lifecycle state."""

    session_id: str
    run_id: str
    conversation_session_id: str = ""
    conversation_scope_key: str = ""
    clear_team_mission_rows: bool = False
    execution_session_id: str = ""
    runtime_scope_key: str = ""
    status: str = "running"
    updated_at: float = 0.0


@dataclass(frozen=True)
class BranchSpec:
    """Payload for branch() — spawn a child session from a source."""

    new_session_id: str
    branch_from_seq: int
    title: str = ""
    display_title: str = ""


@dataclass(frozen=True)
class MaterializedBranchSessionSpec:
    new_session_id: str
    title: str
    created_at: float
    message_count: int
    tool_call_count: int
    source_row: Any
    model: str | None = None
    model_config: dict[str, Any] | None = None


@dataclass(frozen=True)
class BranchLineageSpec:
    session_id: str
    parent_session_id: str
    root_session_id: str
    branch_from_message_row_id: int
    branch_from_turn_id: str = ""
    branch_from_run_id: str = ""
    branch_from_client_message_id: str = ""
    branch_origin: str = "user_message_action"
    branch_mode: str = "materialized_prefix"
    branch_depth: int = 1
    created_at: float = 0.0


@dataclass(frozen=True)
class BranchRequestSpec:
    idempotency_key: str
    source_session_id: str
    branch_fingerprint: str
    result_session_id: str
    created_at: float


class SessionNotFound(LookupError):
    """Raised when a session_id has no corresponding row."""


@runtime_checkable
class SessionRepo(Protocol):
    """Aggregate root for the ``sessions`` table family.

    Owned tables: ``sessions``, ``session_index``, ``session_branches``,
    ``session_handoffs``, ``session_lineage``, ``session_branch_requests``.
    """

    def create(self, spec: SessionSpec) -> Session: ...

    def get(self, session_id: str) -> Session | None: ...

    def list(self, filter: SessionFilter) -> Iterable[Session]: ...

    def update_index(self, session_id: str, patch: SessionIndexPatch) -> None: ...

    def get_title(self, session_id: str) -> str | None: ...

    def get_by_title(self, title: str) -> Session | None: ...

    def set_title(self, session_id: str, title: str, *, title_source: str = "user") -> bool: ...

    def update_cwd(self, session_id: str, cwd: str) -> bool: ...

    def update_usage(self, session_id: str, fields: dict[str, Any]) -> bool: ...

    def update_runtime_config(
        self,
        session_id: str,
        model_config: dict[str, Any],
        model: str | None = None,
    ) -> bool: ...

    def update_source(self, session_id: str, source: str) -> int: ...

    def update_token_counts(
        self,
        session_id: str,
        *,
        input_tokens: int = 0,
        output_tokens: int = 0,
        model: str | None = None,
        cache_read_tokens: int = 0,
        cache_write_tokens: int = 0,
        reasoning_tokens: int = 0,
        estimated_cost_usd: float | None = None,
        actual_cost_usd: float | None = None,
        cost_status: str | None = None,
        cost_source: str | None = None,
        pricing_version: str | None = None,
        billing_provider: str | None = None,
        billing_base_url: str | None = None,
        billing_mode: str | None = None,
        api_call_count: int = 0,
        absolute: bool = False,
    ) -> None: ...

    def set_archived(self, session_id: str, archived: bool) -> bool: ...

    def update_system_prompt(self, session_id: str, system_prompt: str) -> bool: ...

    def request_handoff(self, session_id: str, platform: str) -> bool: ...

    def list_pending_handoffs(self) -> list[dict[str, Any]]: ...

    def claim_handoff(self, session_id: str) -> bool: ...

    def complete_handoff(self, session_id: str) -> None: ...

    def fail_handoff(self, session_id: str, error: str) -> bool: ...

    def finalize_orphaned_compression_sessions(self) -> int: ...

    def prune_empty_ghost_sessions(self, *, cutoff: float) -> list[str]: ...

    def ensure_runtime_session(self, session_id: str, *, started_at: float | None = None) -> bool: ...

    def ensure_session_record(
        self,
        session_id: str,
        source: str,
        *,
        model: str | None = None,
        model_config: dict[str, Any] | None = None,
        system_prompt: str | None = None,
        user_id: str | None = None,
        parent_session_id: str | None = None,
        transient: bool = False,
        title: str | None = None,
        cwd: str | None = None,
        archived: bool = False,
        session_kind: str = "hermes_session",
        conversation_kind: str = "direct",
    ) -> bool: ...

    def exists(self, session_id: str) -> bool: ...

    def orphan_child_references(self, parent_session_id: str) -> None: ...

    def delete_branch_references(self, session_id: str) -> None: ...

    def repair_orphaned_branch_references(self) -> int: ...

    def delete_row(self, session_id: str) -> bool: ...

    def delete_index(self, session_id: str) -> bool: ...

    def create_materialized_branch_session(self, spec: MaterializedBranchSessionSpec) -> bool: ...

    def record_branch_lineage(self, spec: BranchLineageSpec) -> None: ...

    def record_branch_request(self, spec: BranchRequestSpec) -> None: ...

    def record_message_append(
        self,
        session_id: str,
        projection: SessionMessageAppendProjection,
    ) -> None: ...

    def replace_message_projection(
        self,
        session_id: str,
        projection: SessionMessageSnapshotProjection,
    ) -> None: ...

    def reset_message_projection(self, session_id: str) -> None: ...

    def touch_message_activity(self, session_id: str, timestamp: float) -> None: ...

    def project_runtime_state_event(self, event: dict[str, Any]) -> None: ...

    def project_run_state(self, projection: SessionRunProjection) -> None: ...

    def resolve_resume_session_id(self, session_id: str) -> str: ...

    def branch(self, source_id: str, spec: BranchSpec) -> Session: ...

    def close(self, session_id: str, reason: str) -> None: ...

    def reopen(self, session_id: str) -> None: ...

    def normalize_index_conversation_kind(self) -> int: ...

    def classify_internal_execution(self, session_id: str) -> bool: ...

    def increment_rewind_count(self, session_id: str) -> bool: ...


class SessionRepoImpl:
    """SQLite-backed SessionRepo (spec §4.1)."""

    def __init__(self, conn: RepositoryConnection) -> None:
        self._conn = conn
        self._session_columns = _table_columns(conn, "sessions")

    # ------------------------------------------------------------------
    # spec §4.1 API
    # ------------------------------------------------------------------

    def create(self, spec: SessionSpec) -> Session:
        stable = str(spec.session_id or "").strip()
        if not stable:
            raise ValueError("SessionSpec.session_id is required")
        normalized_title = sanitize_session_title(spec.title)
        now = time.time()
        self._conn.execute(
            """
            INSERT INTO sessions (
                id, source, user_id, model, model_config,
                title, display_title, display_title_source,
                session_kind, conversation_kind, parent_session_id,
                started_at, updated_at, transient
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO NOTHING
            """,
            (
                stable,
                str(spec.source or "unknown"),
                str(spec.user_id or ""),
                str(spec.model or ""),
                _encode_model_config(spec.model_config),
                normalized_title,
                str(spec.display_title or ""),
                str(spec.display_title_source or ""),
                str(spec.session_kind or "hermes_session"),
                str(spec.conversation_kind or "direct"),
                str(spec.parent_session_id or "") or None,
                now,
                now,
                1 if spec.transient else 0,
            ),
        )
        # session_index is the user-visible Conversation projection. Execution
        # sessions remain durable in sessions/messages/run_events, but must not
        # become independently navigable sidebar conversations.
        if _is_user_visible_conversation(spec):
            if str(spec.conversation_kind or "").strip().lower() == "team":
                self._conn.execute(
                    """
                    UPDATE sessions
                       SET source = ?, session_kind = ?, conversation_kind = ?,
                           transient = ?, updated_at = ?
                     WHERE id = ?
                    """,
                    (
                        str(spec.source or "team_mission"),
                        str(spec.session_kind or "team_mission"),
                        "team",
                        1 if spec.transient else 0,
                        now,
                        stable,
                    ),
                )
            self._conn.execute(
                """
                INSERT OR IGNORE INTO session_index (
                    session_id, owner_agent_profile_id, owner_profile_version_id,
                    runtime_scope_key, title, source, transient, session_kind,
                    conversation_kind, started_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    stable,
                    str(spec.owner_agent_profile_id or ""),
                    str(spec.owner_profile_version_id or ""),
                    str(spec.runtime_scope_key or ""),
                    str(spec.title or ""),
                    str(spec.source or "unknown"),
                    1 if spec.transient else 0,
                    str(spec.session_kind or "hermes_session"),
                    str(spec.conversation_kind or "direct"),
                    now,
                    now,
                ),
            )
            if str(spec.conversation_kind or "").strip().lower() == "team":
                self._conn.execute(
                    """
                    UPDATE session_index
                       SET source = ?, session_kind = ?, conversation_kind = 'team',
                           transient = ?, updated_at = ?
                     WHERE session_id = ?
                    """,
                    (
                        str(spec.source or "team_mission"),
                        str(spec.session_kind or "team_mission"),
                        1 if spec.transient else 0,
                        now,
                        stable,
                    ),
                )
        else:
            # Explicit execution classification is authoritative even when a
            # placeholder row won the create race first.
            self._conn.execute(
                """
                UPDATE sessions
                   SET session_kind = ?, conversation_kind = ?,
                       parent_session_id = COALESCE(NULLIF(?, ''), parent_session_id),
                       updated_at = ?
                 WHERE id = ?
                """,
                (
                    str(spec.session_kind or "execution"),
                    str(spec.conversation_kind or "internal"),
                    str(spec.parent_session_id or ""),
                    now,
                    stable,
                ),
            )
            self._conn.execute(
                "DELETE FROM session_index WHERE session_id = ?",
                (stable,),
            )
        got = self.get(stable)
        assert got is not None, "SessionRepoImpl.create postcondition violated"
        return got

    def get(self, session_id: str) -> Session | None:
        stable = str(session_id or "").strip()
        if not stable:
            return None
        row = self._conn.execute(
            self._session_select_sql("WHERE id = ?"),
            (stable,),
        ).fetchone()
        if row is None:
            return None
        return _row_to_session(row)

    def list(self, filter: SessionFilter) -> list[Session]:
        clauses: list[str] = []
        params: list[Any] = []
        if filter.source is not None:
            clauses.append("source = ?")
            params.append(str(filter.source))
        if filter.session_kind is not None:
            clauses.append(f"{self._session_kind_expr()} = ?")
            params.append(str(filter.session_kind))
        if filter.conversation_kind is not None:
            clauses.append(f"{self._conversation_kind_expr()} = ?")
            params.append(str(filter.conversation_kind))
        if not filter.include_ended:
            clauses.append("ended_at IS NULL")
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        sql = self._session_select_sql(f"{where} ORDER BY started_at DESC LIMIT ?")
        params.append(int(filter.limit))
        rows = self._conn.execute(sql, params).fetchall()
        return [_row_to_session(r) for r in rows]

    def update_index(self, session_id: str, patch: SessionIndexPatch) -> None:
        stable = str(session_id or "").strip()
        if not stable:
            raise ValueError("session_id is required for update_index")

        assignments: list[str] = []
        params: list[Any] = []

        _apply_column(assignments, params, "title", patch.title)
        _apply_column(assignments, params, "preview", patch.preview)
        _apply_column(assignments, params, "status", patch.status)
        _apply_column(assignments, params, "running", patch.running)
        _apply_column(assignments, params, "waiting_approval", patch.waiting_approval)
        _apply_column(assignments, params, "active_run_id", patch.active_run_id)
        _apply_column(
            assignments,
            params,
            "active_execution_session_id",
            patch.active_execution_session_id,
        )
        _apply_column(
            assignments,
            params,
            "pending_approval_count",
            patch.pending_approval_count,
        )
        _apply_column(assignments, params, "message_count", patch.message_count)
        _apply_column(assignments, params, "last_activity", patch.last_activity)

        for column, value in (patch.fields or {}).items():
            _apply_column(assignments, params, column, value)

        if not assignments:
            return  # noop

        assignments.append("updated_at = ?")
        params.append(time.time())
        params.append(stable)
        self._conn.execute(
            f"UPDATE session_index SET {', '.join(assignments)} WHERE session_id = ?",
            params,
        )

    def get_title(self, session_id: str) -> str | None:
        stable = str(session_id or "").strip()
        if not stable:
            return None
        row = self._conn.execute(
            "SELECT title FROM sessions WHERE id = ?",
            (stable,),
        ).fetchone()
        return str(row["title"] or "") if row else None

    def get_by_title(self, title: str) -> Session | None:
        normalized = _sanitize_title(title)
        if not normalized:
            return None
        row = self._conn.execute(
            self._session_select_sql("WHERE title = ?"),
            (normalized,),
        ).fetchone()
        if row is None:
            return None
        return _row_to_session(row)

    def set_title(self, session_id: str, title: str, *, title_source: str = "user") -> bool:
        stable = str(session_id or "").strip()
        if not stable:
            return False
        normalized_source = str(title_source or "user").strip().lower() or "user"
        if normalized_source == "auto":
            return False
        normalized_title = sanitize_session_title(title)
        if normalized_title:
            conflict = self._conn.execute(
                "SELECT id FROM sessions WHERE title = ? AND id != ?",
                (normalized_title, stable),
            ).fetchone()
            if conflict:
                raise ValueError(
                    f"Title {normalized_title!r} is already in use by session {conflict['id']}"
                )
        now = time.time()
        assignments = ["title = ?", "display_title = COALESCE(?, '')", "display_title_source = ?"]
        values: list[Any] = [
            normalized_title,
            normalized_title or "",
            normalized_source if normalized_title else "",
        ]
        if "updated_at" in self._session_columns:
            assignments.append("updated_at = ?")
            values.append(now)
        elif "last_active" in self._session_columns:
            assignments.append("last_active = ?")
            values.append(now)
        values.append(stable)
        rowcount = int(
            self._conn.execute(
                f"UPDATE sessions SET {', '.join(assignments)} WHERE id = ?",
                values,
            ).rowcount
            or 0
        )
        if rowcount > 0:
            self._conn.execute(
                """
                UPDATE session_index
                   SET title = ?,
                       updated_at = ?
                 WHERE session_id = ?
                """,
                (normalized_title or "", time.time(), stable),
            )
        return rowcount > 0

    def update_cwd(self, session_id: str, cwd: str) -> bool:
        stable = str(session_id or "").strip()
        if not stable:
            raise ValueError("session_id is required for update_cwd")
        if "cwd" not in self._session_columns:
            return False
        now = time.time()
        rowcount = int(
            self._conn.execute(
                "UPDATE sessions SET cwd = ?, updated_at = ? WHERE id = ?",
                (str(cwd or ""), now, stable),
            ).rowcount
            or 0
        )
        if rowcount:
            self._conn.execute(
                "UPDATE session_index SET updated_at = ? WHERE session_id = ?",
                (now, stable),
            )
        return rowcount > 0

    def update_usage(self, session_id: str, fields: dict[str, Any]) -> bool:
        stable = str(session_id or "").strip()
        if not stable:
            raise ValueError("session_id is required for update_usage")
        allowed = {
            "input_tokens",
            "output_tokens",
            "cache_read_tokens",
            "cache_write_tokens",
            "reasoning_tokens",
            "api_call_count",
            "billing_provider",
            "billing_base_url",
            "billing_mode",
            "estimated_cost_usd",
            "actual_cost_usd",
            "cost_status",
            "cost_source",
            "pricing_version",
        }
        assignments: list[str] = []
        params: list[Any] = []
        for key, value in (fields or {}).items():
            if key in allowed and key in self._session_columns:
                assignments.append(f"{key} = ?")
                params.append(value)
        if not assignments:
            return False
        assignments.append("updated_at = ?")
        params.extend([time.time(), stable])
        rowcount = int(
            self._conn.execute(
                f"UPDATE sessions SET {', '.join(assignments)} WHERE id = ?",
                params,
            ).rowcount
            or 0
        )
        return rowcount > 0

    def update_source(self, session_id: str, source: str) -> int:
        stable = str(session_id or "").strip()
        normalized_source = str(source or "").strip()
        if not stable or not normalized_source:
            return 0
        cursor = self._conn.execute(
            "UPDATE sessions SET source = ? WHERE id = ? AND COALESCE(source, '') != ?",
            (normalized_source, stable, normalized_source),
        )
        source_rows = int(cursor.rowcount or 0)
        if _table_exists(self._conn, "session_index"):
            self._conn.execute(
                """
                UPDATE session_index
                   SET source = ?,
                       conversation_kind = CASE
                           WHEN ? = 'team_mission' THEN 'team'
                           ELSE conversation_kind
                       END
                 WHERE session_id = ?
                """,
                (normalized_source, normalized_source, stable),
            )
        return source_rows

    def update_token_counts(
        self,
        session_id: str,
        *,
        input_tokens: int = 0,
        output_tokens: int = 0,
        model: str | None = None,
        cache_read_tokens: int = 0,
        cache_write_tokens: int = 0,
        reasoning_tokens: int = 0,
        estimated_cost_usd: float | None = None,
        actual_cost_usd: float | None = None,
        cost_status: str | None = None,
        cost_source: str | None = None,
        pricing_version: str | None = None,
        billing_provider: str | None = None,
        billing_base_url: str | None = None,
        billing_mode: str | None = None,
        api_call_count: int = 0,
        absolute: bool = False,
    ) -> None:
        self.ensure_session_record(session_id, "unknown", model=model)
        if absolute:
            sql = """UPDATE sessions SET
                   input_tokens = ?,
                   output_tokens = ?,
                   cache_read_tokens = ?,
                   cache_write_tokens = ?,
                   reasoning_tokens = ?,
                   estimated_cost_usd = COALESCE(?, 0),
                   actual_cost_usd = CASE
                       WHEN ? IS NULL THEN actual_cost_usd
                       ELSE ?
                   END,
                   cost_status = COALESCE(?, cost_status),
                   cost_source = COALESCE(?, cost_source),
                   pricing_version = COALESCE(?, pricing_version),
                   billing_provider = COALESCE(billing_provider, ?),
                   billing_base_url = COALESCE(billing_base_url, ?),
                   billing_mode = COALESCE(billing_mode, ?),
                   model = COALESCE(model, ?),
                   api_call_count = ?
                   WHERE id = ?"""
        else:
            sql = """UPDATE sessions SET
                   input_tokens = input_tokens + ?,
                   output_tokens = output_tokens + ?,
                   cache_read_tokens = cache_read_tokens + ?,
                   cache_write_tokens = cache_write_tokens + ?,
                   reasoning_tokens = reasoning_tokens + ?,
                   estimated_cost_usd = COALESCE(estimated_cost_usd, 0) + COALESCE(?, 0),
                   actual_cost_usd = CASE
                       WHEN ? IS NULL THEN actual_cost_usd
                       ELSE COALESCE(actual_cost_usd, 0) + ?
                   END,
                   cost_status = COALESCE(?, cost_status),
                   cost_source = COALESCE(?, cost_source),
                   pricing_version = COALESCE(?, pricing_version),
                   billing_provider = COALESCE(billing_provider, ?),
                   billing_base_url = COALESCE(billing_base_url, ?),
                   billing_mode = COALESCE(billing_mode, ?),
                   model = COALESCE(model, ?),
                   api_call_count = COALESCE(api_call_count, 0) + ?
                   WHERE id = ?"""
        self._conn.execute(
            sql,
            (
                input_tokens,
                output_tokens,
                cache_read_tokens,
                cache_write_tokens,
                reasoning_tokens,
                estimated_cost_usd,
                actual_cost_usd,
                actual_cost_usd,
                cost_status,
                cost_source,
                pricing_version,
                billing_provider,
                billing_base_url,
                billing_mode,
                model,
                api_call_count,
                str(session_id or "").strip(),
            ),
        )

    def set_archived(self, session_id: str, archived: bool) -> bool:
        stable = str(session_id or "").strip()
        if not stable:
            return False
        if "archived" not in self._session_columns:
            return False
        cursor = self._conn.execute(
            "UPDATE sessions SET archived = ?, updated_at = ? WHERE id = ?",
            (1 if archived else 0, time.time(), stable),
        )
        return int(cursor.rowcount or 0) > 0

    def update_system_prompt(self, session_id: str, system_prompt: str) -> bool:
        stable = str(session_id or "").strip()
        if not stable:
            raise ValueError("session_id is required for update_system_prompt")
        if "system_prompt" not in self._session_columns:
            return False
        cursor = self._conn.execute(
            "UPDATE sessions SET system_prompt = ?, updated_at = ? WHERE id = ?",
            (str(system_prompt or ""), time.time(), stable),
        )
        return int(cursor.rowcount or 0) > 0

    def update_runtime_config(
        self,
        session_id: str,
        model_config: dict[str, Any],
        model: str | None = None,
    ) -> bool:
        session_key = str(session_id or "").strip()
        if not session_key:
            raise ValueError("session_id is required for update_runtime_config")
        encoded_config = _encode_model_config(model_config)
        next_model = str(model or "").strip()
        cursor = self._conn.execute(
            """
            UPDATE sessions
               SET model_config = ?,
                   model = CASE WHEN ? != '' THEN ? ELSE model END,
                   updated_at = ?
             WHERE id = ?
            """,
            (encoded_config, next_model, next_model, time.time(), session_key),
        )
        return int(cursor.rowcount or 0) > 0

    def request_handoff(self, session_id: str, platform: str) -> bool:
        stable = str(session_id or "").strip()
        if not stable:
            return False
        required = {"handoff_state", "handoff_platform", "handoff_error"}
        if not required.issubset(self._session_columns):
            return False
        cursor = self._conn.execute(
            """
            UPDATE sessions
               SET handoff_state = 'pending',
                   handoff_platform = ?,
                   handoff_error = NULL,
                   updated_at = ?
             WHERE id = ?
               AND (handoff_state IS NULL OR handoff_state IN ('completed', 'failed'))
            """,
            (str(platform or ""), time.time(), stable),
        )
        return int(cursor.rowcount or 0) > 0

    def list_pending_handoffs(self) -> list[dict[str, Any]]:
        if "handoff_state" not in self._session_columns:
            return []
        rows = self._conn.execute(
            "SELECT * FROM sessions "
            "WHERE handoff_state = 'pending' "
            "ORDER BY updated_at ASC, started_at ASC, id ASC"
        ).fetchall()
        return [dict(row) for row in rows]

    def fail_handoff(self, session_id: str, error: str) -> bool:
        stable = str(session_id or "").strip()
        if not stable:
            return False
        required = {"handoff_state", "handoff_error"}
        if not required.issubset(self._session_columns):
            return False
        cursor = self._conn.execute(
            "UPDATE sessions SET handoff_state = 'failed', handoff_error = ?, updated_at = ? WHERE id = ?",
            (str(error or ""), time.time(), stable),
        )
        return int(cursor.rowcount or 0) > 0

    def claim_handoff(self, session_id: str) -> bool:
        stable = str(session_id or "").strip()
        if not stable or "handoff_state" not in self._session_columns:
            return False
        cursor = self._conn.execute(
            """
            UPDATE sessions
               SET handoff_state = 'running',
                   updated_at = ?
             WHERE id = ? AND handoff_state = 'pending'
            """,
            (time.time(), stable),
        )
        return int(cursor.rowcount or 0) > 0

    def complete_handoff(self, session_id: str) -> None:
        stable = str(session_id or "").strip()
        if not stable or "handoff_state" not in self._session_columns:
            return
        self._conn.execute(
            """
            UPDATE sessions
               SET handoff_state = 'completed',
                   handoff_error = NULL,
                   updated_at = ?
             WHERE id = ?
            """,
            (time.time(), stable),
        )

    def finalize_orphaned_compression_sessions(self) -> int:
        required = {"parent_session_id", "ended_at", "end_reason"}
        if not required.issubset(self._session_columns):
            return 0
        now = time.time()
        cursor = self._conn.execute(
            """
            UPDATE sessions
               SET ended_at = COALESCE(ended_at, ?),
                   end_reason = COALESCE(end_reason, 'compression_orphan'),
                   updated_at = ?
             WHERE parent_session_id IS NOT NULL
               AND COALESCE(message_count, 0) = 0
               AND ended_at IS NULL
            """,
            (now, now),
        )
        return int(cursor.rowcount or 0)

    def prune_empty_ghost_sessions(self, *, cutoff: float) -> list[str]:
        required = {"source", "title", "ended_at", "started_at"}
        if not required.issubset(self._session_columns):
            return []
        rows = self._conn.execute(
            """
            SELECT id FROM sessions
            WHERE source = 'tui'
              AND title IS NULL
              AND ended_at IS NOT NULL
              AND started_at < ?
              AND COALESCE(message_count, 0) = 0
            """,
            (float(cutoff),),
        ).fetchall()
        ids = [str(_row_any(row, "id", "")) for row in rows]
        ids = [sid for sid in ids if sid]
        if ids:
            placeholders = ",".join("?" * len(ids))
            self._conn.execute(f"DELETE FROM sessions WHERE id IN ({placeholders})", ids)
        return ids

    def ensure_runtime_session(self, session_id: str, *, started_at: float | None = None) -> bool:
        stable = str(session_id or "").strip()
        if not stable:
            return False
        timestamp = float(started_at or time.time())
        columns: list[str] = ["id"]
        values: list[Any] = [stable]
        if "source" in self._session_columns:
            columns.append("source")
            values.append("runtime")
        if "started_at" in self._session_columns:
            columns.append("started_at")
            values.append(timestamp)
        if "updated_at" in self._session_columns:
            columns.append("updated_at")
            values.append(timestamp)
        placeholders = ", ".join("?" for _ in columns)
        cursor = self._conn.execute(
            f"""
            INSERT OR IGNORE INTO sessions ({', '.join(columns)})
            VALUES ({placeholders})
            """,
            values,
        )
        return int(cursor.rowcount or 0) > 0

    def ensure_session_record(
        self,
        session_id: str,
        source: str,
        *,
        model: str | None = None,
        model_config: dict[str, Any] | None = None,
        system_prompt: str | None = None,
        user_id: str | None = None,
        parent_session_id: str | None = None,
        transient: bool = False,
        title: str | None = None,
        cwd: str | None = None,
        archived: bool = False,
        session_kind: str = "hermes_session",
        conversation_kind: str = "direct",
    ) -> bool:
        stable = str(session_id or "").strip()
        if not stable:
            raise ValueError("session_id is required")
        now = time.time()
        values_by_column: dict[str, Any] = {
            "id": stable,
            "source": str(source or "unknown"),
            "user_id": user_id,
            "model": model,
            "model_config": json.dumps(model_config) if model_config else None,
            "system_prompt": system_prompt,
            "parent_session_id": parent_session_id,
            "started_at": now,
            "updated_at": now,
            "title": sanitize_session_title(title),
            "cwd": cwd,
            "archived": 1 if archived else 0,
            "session_kind": session_kind or "hermes_session",
            "conversation_kind": conversation_kind or "direct",
            "transient": 1 if transient else 0,
        }
        columns = [column for column in values_by_column if column in self._session_columns]
        placeholders = ", ".join("?" for _ in columns)
        cursor = self._conn.execute(
            f"INSERT OR IGNORE INTO sessions ({', '.join(columns)}) VALUES ({placeholders})",
            [values_by_column[column] for column in columns],
        )
        return int(cursor.rowcount or 0) > 0

    def exists(self, session_id: str) -> bool:
        stable = str(session_id or "").strip()
        if not stable or "id" not in self._session_columns:
            return False
        row = self._conn.execute(
            "SELECT 1 FROM sessions WHERE id = ? LIMIT 1",
            (stable,),
        ).fetchone()
        return row is not None

    def orphan_child_references(self, parent_session_id: str) -> None:
        stable = str(parent_session_id or "").strip()
        if not stable:
            return
        if "parent_session_id" in self._session_columns:
            self._conn.execute(
                "UPDATE sessions SET parent_session_id = NULL WHERE parent_session_id = ?",
                (stable,),
            )
        if _table_exists(self._conn, "session_lineage") and _column_exists(
            self._conn,
            "session_lineage",
            "parent_session_id",
        ):
            self._conn.execute(
                "UPDATE session_lineage SET parent_session_id = NULL WHERE parent_session_id = ?",
                (stable,),
            )

    def delete_branch_references(self, session_id: str) -> None:
        stable = str(session_id or "").strip()
        if not stable:
            return
        if _table_exists(self._conn, "session_branch_requests"):
            self._conn.execute(
                "DELETE FROM session_branch_requests "
                "WHERE source_session_id = ? OR result_session_id = ?",
                (stable, stable),
            )
        if _table_exists(self._conn, "session_lineage"):
            self._conn.execute("DELETE FROM session_lineage WHERE session_id = ?", (stable,))

    def repair_orphaned_branch_references(self) -> int:
        repaired = 0
        if _table_exists(self._conn, "session_lineage"):
            repaired += _affected(
                self._conn.execute(
                    """
                    DELETE FROM session_lineage
                    WHERE NOT EXISTS (
                        SELECT 1 FROM sessions s WHERE s.id = session_lineage.session_id
                    )
                    """
                )
            )
            if _column_exists(self._conn, "session_lineage", "parent_session_id"):
                repaired += _affected(
                    self._conn.execute(
                        """
                        UPDATE session_lineage
                        SET parent_session_id = NULL
                        WHERE parent_session_id IS NOT NULL
                          AND NOT EXISTS (
                              SELECT 1 FROM sessions s
                              WHERE s.id = session_lineage.parent_session_id
                          )
                        """
                    )
                )
        if _table_exists(self._conn, "session_branch_requests"):
            repaired += _affected(
                self._conn.execute(
                    """
                    DELETE FROM session_branch_requests
                    WHERE NOT EXISTS (
                        SELECT 1 FROM sessions s
                        WHERE s.id = session_branch_requests.source_session_id
                    )
                       OR NOT EXISTS (
                        SELECT 1 FROM sessions s
                        WHERE s.id = session_branch_requests.result_session_id
                    )
                    """
                )
            )
        return repaired

    def delete_row(self, session_id: str) -> bool:
        stable = str(session_id or "").strip()
        if not stable:
            return False
        cursor = self._conn.execute("DELETE FROM sessions WHERE id = ?", (stable,))
        return int(cursor.rowcount or 0) > 0

    def delete_index(self, session_id: str) -> bool:
        stable = str(session_id or "").strip()
        if not stable or not _table_exists(self._conn, "session_index"):
            return False
        cursor = self._conn.execute(
            "DELETE FROM session_index WHERE session_id = ?",
            (stable,),
        )
        return int(cursor.rowcount or 0) > 0

    def delete_index_by_sources(self, sources: tuple[str, ...]) -> int:
        normalized = tuple(
            str(source or "").strip()
            for source in sources
            if str(source or "").strip()
        )
        if not normalized or not _table_exists(self._conn, "session_index"):
            return 0
        placeholders = ",".join("?" for _ in normalized)
        cursor = self._conn.execute(
            f"DELETE FROM session_index WHERE COALESCE(source, '') IN ({placeholders})",
            normalized,
        )
        return max(0, int(cursor.rowcount or 0))

    def create_materialized_branch_session(self, spec: MaterializedBranchSessionSpec) -> bool:
        stable = str(spec.new_session_id or "").strip()
        if not stable:
            raise ValueError("new_session_id is required")
        source_row = spec.source_row
        created_at = float(spec.created_at or time.time())
        normalized_title = sanitize_session_title(spec.title)
        values_by_column: dict[str, Any] = {
            "id": stable,
            "source": _row_any(source_row, "source", ""),
            "user_id": _row_any(source_row, "user_id", ""),
            "model": (
                str(spec.model)
                if spec.model is not None
                else _row_any(source_row, "model", "")
            ),
            "model_config": (
                _encode_model_config(spec.model_config)
                if spec.model_config is not None
                else _row_any(source_row, "model_config", "")
            ),
            "system_prompt": _row_any(source_row, "system_prompt", ""),
            "parent_session_id": None,
            "started_at": created_at,
            "updated_at": created_at,
            "last_active": created_at,
            "title": normalized_title,
            "display_title": normalized_title or "",
            "display_title_source": "branch_title" if normalized_title else "",
            "transient": int(_row_any(source_row, "transient", 0) or 0),
            "message_count": int(spec.message_count or 0),
            "tool_call_count": int(spec.tool_call_count or 0),
        }
        columns = [column for column in values_by_column if column in self._session_columns]
        placeholders = ", ".join("?" for _ in columns)
        cursor = self._conn.execute(
            f"""
            INSERT INTO sessions ({', '.join(columns)})
            VALUES ({placeholders})
            """,
            [values_by_column[column] for column in columns],
        )
        created = int(cursor.rowcount or 0) > 0
        if created and _table_exists(self._conn, "session_index"):
            source_session_id = str(_row_any(source_row, "id", "") or "")
            source_index = self._conn.execute(
                "SELECT * FROM session_index WHERE session_id = ?",
                (source_session_id,),
            ).fetchone()
            index_values = dict(source_index) if source_index is not None else {
                "source": str(_row_any(source_row, "source", "unknown") or "unknown"),
                "transient": int(_row_any(source_row, "transient", 0) or 0),
                "session_kind": str(
                    _row_any(source_row, "session_kind", "hermes_session")
                    or "hermes_session"
                ),
                "conversation_kind": str(
                    _row_any(source_row, "conversation_kind", "direct") or "direct"
                ),
            }
            index_values.update(
                {
                    "session_id": stable,
                    "title": normalized_title or "",
                    "preview": "",
                    "status": "idle",
                    "running": 0,
                    "waiting_approval": 0,
                    "active_run_id": "",
                    "active_execution_session_id": "",
                    "pending_approval_count": 0,
                    "message_count": int(spec.message_count or 0),
                    "started_at": created_at,
                    "updated_at": created_at,
                    "last_activity": created_at,
                }
            )
            self.upsert_session_index(index_values)
        return created

    def record_branch_lineage(self, spec: BranchLineageSpec) -> None:
        created_at = float(spec.created_at or time.time())
        self._conn.execute(
            """
            INSERT INTO session_lineage (
                session_id, parent_session_id, root_session_id,
                branch_from_message_row_id, branch_from_turn_id,
                branch_from_run_id, branch_from_client_message_id,
                branch_origin, branch_mode, branch_depth, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(spec.session_id or ""),
                str(spec.parent_session_id or ""),
                str(spec.root_session_id or ""),
                int(spec.branch_from_message_row_id or 0),
                str(spec.branch_from_turn_id or "") or None,
                str(spec.branch_from_run_id or "") or None,
                str(spec.branch_from_client_message_id or "") or None,
                str(spec.branch_origin or "user_message_action"),
                str(spec.branch_mode or "materialized_prefix"),
                int(spec.branch_depth or 0),
                created_at,
            ),
        )

    def record_branch_request(self, spec: BranchRequestSpec) -> None:
        self._conn.execute(
            """
            INSERT INTO session_branch_requests (
                idempotency_key, source_session_id, branch_fingerprint,
                result_session_id, created_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                str(spec.idempotency_key or ""),
                str(spec.source_session_id or ""),
                str(spec.branch_fingerprint or ""),
                str(spec.result_session_id or ""),
                float(spec.created_at or time.time()),
            ),
        )

    def record_message_append(
        self,
        session_id: str,
        projection: SessionMessageAppendProjection,
    ) -> None:
        stable = str(session_id or "").strip()
        if not stable:
            raise ValueError("session_id is required for record_message_append")
        timestamp = float(projection.timestamp or time.time())
        preview = str(projection.user_preview or "")
        display_title = str(projection.user_display_title or "")
        self._conn.execute(
            """
            UPDATE sessions
               SET message_count = COALESCE(message_count, 0) + 1,
                   tool_call_count = COALESCE(tool_call_count, 0) + ?,
                   preview = CASE
                       WHEN ? != '' AND COALESCE(preview, '') = '' THEN ?
                       ELSE COALESCE(preview, '')
                   END,
                   display_title = CASE
                       WHEN ? != '' AND COALESCE(display_title, '') = '' THEN ?
                       ELSE COALESCE(display_title, '')
                   END,
                   display_title_source = CASE
                       WHEN ? != '' AND COALESCE(display_title_source, '') = ''
                           THEN 'first_user_message'
                       ELSE COALESCE(display_title_source, '')
                   END,
                   last_active = ?,
                   updated_at = ?
             WHERE id = ?
            """,
            (
                int(projection.tool_call_count or 0),
                preview,
                preview,
                display_title,
                display_title,
                display_title,
                timestamp,
                timestamp,
                stable,
            ),
        )
        self._refresh_index_from_session(stable, timestamp=timestamp)

    def replace_message_projection(
        self,
        session_id: str,
        projection: SessionMessageSnapshotProjection,
    ) -> None:
        stable = str(session_id or "").strip()
        if not stable:
            raise ValueError("session_id is required for replace_message_projection")
        last_message_ts = projection.last_message_ts
        self._conn.execute(
            """
            UPDATE sessions
               SET message_count = ?,
                   tool_call_count = ?,
                   preview = ?,
                   display_title = CASE
                       WHEN COALESCE(display_title_source, '') = 'user'
                           THEN COALESCE(display_title, '')
                       ELSE ?
                   END,
                   display_title_source = CASE
                       WHEN COALESCE(display_title_source, '') = 'user' THEN 'user'
                       WHEN ? != '' THEN 'first_user_message'
                       ELSE ''
                   END,
                   last_active = ?,
                   updated_at = COALESCE(?, updated_at)
             WHERE id = ?
            """,
            (
                int(projection.message_count),
                int(projection.tool_call_count),
                str(projection.first_user_preview or ""),
                str(projection.first_user_display_title or ""),
                str(projection.first_user_display_title or ""),
                last_message_ts,
                last_message_ts,
                stable,
            ),
        )
        self._refresh_index_from_session(stable, timestamp=last_message_ts)

    def reset_message_projection(self, session_id: str) -> None:
        stable = str(session_id or "").strip()
        if not stable:
            raise ValueError("session_id is required for reset_message_projection")
        self._conn.execute(
            """
            UPDATE sessions
               SET message_count = 0,
                   tool_call_count = 0,
                   preview = '',
                   last_active = NULL
             WHERE id = ?
            """,
            (stable,),
        )
        self._conn.execute(
            """
            UPDATE session_index
               SET preview = '',
                   message_count = 0,
                   last_activity = NULL,
                   updated_at = ?
             WHERE session_id = ?
            """,
            (time.time(), stable),
        )

    def normalize_index_conversation_kind(self) -> int:
        cursor = self._conn.execute(
            """
            UPDATE session_index
               SET conversation_kind = CASE
                   WHEN COALESCE(source, '') = 'team_mission'
                     OR COALESCE(session_kind, '') = 'team_mission'
                   THEN 'team'
                   ELSE 'direct'
               END
             WHERE COALESCE(conversation_kind, '') NOT IN ('direct', 'team')
                OR (COALESCE(source, '') = 'team_mission' AND conversation_kind != 'team')
                OR (COALESCE(session_kind, '') = 'team_mission' AND conversation_kind != 'team')
            """
        )
        return int(cursor.rowcount or 0)

    def classify_internal_execution(self, session_id: str) -> bool:
        """Make a durable session an internal execution and hide its index row."""

        canonical_session_id = str(session_id or "").strip()
        if not canonical_session_id:
            return False
        cursor = self._conn.execute(
            """
            UPDATE sessions
               SET session_kind = 'execution',
                   conversation_kind = 'internal',
                   updated_at = MAX(COALESCE(updated_at, 0), COALESCE(last_active, 0), started_at)
             WHERE id = ?
               AND (
                    COALESCE(session_kind, '') != 'execution'
                    OR COALESCE(conversation_kind, '') != 'internal'
               )
            """,
            (canonical_session_id,),
        )
        self._conn.execute(
            "DELETE FROM session_index WHERE session_id = ?",
            (canonical_session_id,),
        )
        return int(cursor.rowcount or 0) > 0

    def upsert_session_index(self, values: dict[str, Any]) -> dict[str, Any]:
        cols = list(values.keys())
        placeholders = ", ".join(f":{column}" for column in cols)
        update_cols = [column for column in cols if column != "session_id"]
        set_clause = ", ".join(f"{column}=excluded.{column}" for column in update_cols)
        self._conn.execute(
            f"INSERT INTO session_index ({', '.join(cols)}) VALUES ({placeholders}) "
            f"ON CONFLICT(session_id) DO UPDATE SET {set_clause}",
            values,
        )
        return values

    def increment_rewind_count(self, session_id: str) -> bool:
        stable = str(session_id or "").strip()
        if not stable:
            return False
        cursor = self._conn.execute(
            """
            UPDATE sessions
               SET rewind_count = COALESCE(rewind_count, 0) + 1
             WHERE id = ?
            """,
            (stable,),
        )
        return int(cursor.rowcount or 0) > 0

    def touch_message_activity(self, session_id: str, timestamp: float) -> None:
        stable = str(session_id or "").strip()
        if not stable:
            raise ValueError("session_id is required for touch_message_activity")
        ts = float(timestamp or time.time())
        self._conn.execute(
            """
            UPDATE sessions
               SET last_active = MAX(COALESCE(last_active, 0), ?)
             WHERE id = ?
            """,
            (ts, stable),
        )
        self._conn.execute(
            """
            UPDATE session_index
               SET updated_at = MAX(COALESCE(updated_at, 0), ?),
                   last_activity = MAX(COALESCE(last_activity, 0), ?)
             WHERE session_id = ?
            """,
            (ts, ts, stable),
        )

    def project_runtime_state_event(self, event: dict[str, Any]) -> None:
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        record = session_info_record(
            session_id=str(
                event.get("conversation_session_id")
                or event.get("session_id")
                or ""
            ),
            payload=payload,
            runtime_scope_key=str(event.get("runtime_scope_key") or ""),
            execution_session_id=str(event.get("execution_session_id") or ""),
            run_id=str(event.get("run_id") or ""),
            turn_id=str(event.get("turn_id") or ""),
            updated_at=float(event.get("timestamp") or 0),
            source_seq=int(event.get("seq") or 0),
        )
        self._conn.execute(
            """
            INSERT INTO session_runtime_state (
                session_id, runtime_scope_key, execution_session_id, run_id,
                turn_id, status, model, provider, profile_json,
                payload_hash, updated_at, source_seq
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            WHERE excluded.source_seq >= COALESCE(session_runtime_state.source_seq, 0)
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

    def project_run_state(self, projection: SessionRunProjection) -> None:
        sid = str(projection.session_id or "").strip()
        run_id = str(projection.run_id or "").strip()
        if not sid or not run_id:
            return
        is_active = str(projection.status or "") not in TERMINAL_RUN_STATUSES
        run_scope_key = str(projection.runtime_scope_key or "").strip()
        execution_session_id = str(projection.execution_session_id or "")
        updated_at = float(projection.updated_at or 0)
        conv_sid = str(projection.conversation_session_id or "").strip()
        conv_scope_key = str(projection.conversation_scope_key or "").strip()
        try:
            if is_active:
                self._project_active_run_to_index(
                    sid=sid,
                    conv_sid=conv_sid,
                    run_id=run_id,
                    execution_session_id=execution_session_id,
                    run_scope_key=run_scope_key,
                    conv_scope_key=conv_scope_key,
                    updated_at=updated_at,
                    status=str(projection.status or ""),
                )
                return
            self._clear_terminal_run_from_index(
                sid=sid,
                conv_sid=conv_sid,
                run_id=run_id,
                updated_at=updated_at,
                status=str(projection.status or ""),
                clear_team_mission_rows=projection.clear_team_mission_rows,
            )
        except sqlite3.OperationalError as exc:
            logger.debug("session_index run projection skipped for %s/%s: %s", sid, run_id, exc)

    def resolve_resume_session_id(self, session_id: str) -> str:
        stable = str(session_id or "").strip()
        if not stable:
            return stable
        return self._compression_tip(stable)

    def _project_active_run_to_index(
        self,
        *,
        sid: str,
        conv_sid: str,
        run_id: str,
        execution_session_id: str,
        run_scope_key: str,
        conv_scope_key: str,
        updated_at: float,
        status: str,
    ) -> None:
        if conv_sid and conv_sid != sid:
            cur = self._conn.execute(
                """
                UPDATE session_index
                   SET running = 1, status = 'running',
                       active_run_id = ?, active_execution_session_id = ?,
                       runtime_scope_key = COALESCE(NULLIF(?, ''), runtime_scope_key),
                       updated_at = MAX(updated_at, ?)
                 WHERE session_id = ?
                """,
                (run_id, execution_session_id, run_scope_key, updated_at, sid),
            )
            conv_cur = self._conn.execute(
                """
                UPDATE session_index
                   SET running = 1, status = 'running',
                       active_run_id = ?, active_execution_session_id = ?,
                       runtime_scope_key = COALESCE(NULLIF(?, ''), NULLIF(?, ''), runtime_scope_key),
                       updated_at = MAX(updated_at, ?)
                 WHERE session_id = ?
                """,
                (run_id, execution_session_id, conv_scope_key, run_scope_key, updated_at, conv_sid),
            )
            logger.debug(
                "[doxie-session-index] project_run set_running session_id=%s conv_session_id=%s run_id=%s status=%s rows=%s",
                sid,
                conv_sid,
                run_id,
                status,
                int(cur.rowcount or 0) + int(conv_cur.rowcount or 0),
            )
            return
        cur = self._conn.execute(
            """
            UPDATE session_index
               SET running = 1, status = 'running',
                   active_run_id = ?, active_execution_session_id = ?,
                   runtime_scope_key = COALESCE(NULLIF(?, ''), runtime_scope_key),
                   updated_at = MAX(updated_at, ?)
             WHERE session_id = ?
            """,
            (run_id, execution_session_id, conv_scope_key or run_scope_key, updated_at, sid),
        )
        logger.debug(
            "[doxie-session-index] project_run set_running session_id=%s run_id=%s status=%s rows=%s",
            sid,
            run_id,
            status,
            cur.rowcount,
        )

    def _clear_terminal_run_from_index(
        self,
        *,
        sid: str,
        conv_sid: str,
        run_id: str,
        updated_at: float,
        status: str,
        clear_team_mission_rows: bool,
    ) -> None:
        clear_ids = [sid]
        if conv_sid and conv_sid != sid:
            clear_ids.append(conv_sid)
        id_placeholders = ",".join("?" for _ in clear_ids)
        cur = self._conn.execute(
            f"""
            UPDATE session_index
               SET running = 0, status = 'idle',
                   active_run_id = '', active_execution_session_id = '',
                   updated_at = MAX(updated_at, ?)
             WHERE session_id IN ({id_placeholders})
               AND (active_run_id = ? OR active_run_id = '')
               AND (
                   session_kind != 'team_mission'
                   OR COALESCE(mission_id, '') = ''
                   OR ? = 1
               )
            """,
            (updated_at, *clear_ids, run_id, 1 if clear_team_mission_rows else 0),
        )
        logger.debug(
            "[doxie-session-index] project_run clear session_id=%s conv_session_id=%s run_id=%s status=%s rows=%s",
            sid,
            conv_sid,
            run_id,
            status,
            cur.rowcount,
        )

    def _session_select_sql(self, suffix: str) -> str:
        return (
            "SELECT "
            "id, source, title, "
            f"{self._display_title_expr()} AS display_title, "
            f"{self._session_kind_expr()} AS session_kind, "
            f"{self._conversation_kind_expr()} AS conversation_kind, "
            "started_at, "
            f"{self._updated_at_expr()} AS updated_at, "
            "ended_at, "
            f"{self._parent_session_id_expr()} AS parent_session_id "
            f"FROM sessions {suffix}"
        )

    def _display_title_expr(self) -> str:
        if "display_title" in self._session_columns:
            return "display_title"
        return "title"

    def _session_kind_expr(self) -> str:
        if "session_kind" in self._session_columns:
            return "session_kind"
        return "'hermes_session'"

    def _conversation_kind_expr(self) -> str:
        if "conversation_kind" in self._session_columns:
            return "conversation_kind"
        return "CASE WHEN source = 'team_mission' THEN 'team' ELSE 'direct' END"

    def _updated_at_expr(self) -> str:
        if "updated_at" in self._session_columns:
            return "updated_at"
        if "last_active" in self._session_columns:
            return "COALESCE(last_active, started_at)"
        return "started_at"

    def _parent_session_id_expr(self) -> str:
        if "parent_session_id" in self._session_columns:
            return "parent_session_id"
        return "''"

    def branch(self, source_id: str, spec: BranchSpec) -> Session:
        stable_src = str(source_id or "").strip()
        stable_new = str(spec.new_session_id or "").strip()
        if not stable_src or not stable_new:
            raise ValueError("source_id and BranchSpec.new_session_id are required")
        source = self.get(stable_src)
        if source is None:
            raise SessionNotFound(f"session {stable_src!r} does not exist")
        branch_spec = SessionSpec(
            session_id=stable_new,
            source=source.source,
            title=spec.title or source.title,
            display_title=spec.display_title or source.display_title,
            session_kind=source.session_kind,
            conversation_kind=source.conversation_kind,
            parent_session_id=stable_src,
        )
        created = self.create(branch_spec)
        # Optionally record the branch pointer for future replay/rewind flows.
        self._conn.execute(
            """
            INSERT OR IGNORE INTO session_branches (
                child_session_id, parent_session_id, branch_from_seq, created_at
            ) VALUES (?, ?, ?, ?)
            """,
            (stable_new, stable_src, int(spec.branch_from_seq), time.time()),
        )
        return created

    def close(self, session_id: str, reason: str) -> None:
        stable = str(session_id or "").strip()
        if not stable:
            raise ValueError("session_id is required for close")
        now = time.time()
        self._conn.execute(
            """
            UPDATE sessions
               SET ended_at = ?,
                   end_reason = ?,
                   updated_at = ?
             WHERE id = ?
               AND ended_at IS NULL
            """,
            (now, str(reason or ""), now, stable),
        )
        # Reflect terminal state on the session_index projection.
        self._conn.execute(
            """
            UPDATE session_index
               SET status = 'closed',
                   running = 0,
                   waiting_approval = 0,
                   active_run_id = '',
                   updated_at = ?
             WHERE session_id = ?
            """,
            (now, stable),
        )

    def _refresh_index_from_session(self, session_id: str, *, timestamp: float | None) -> None:
        stable = str(session_id or "").strip()
        if not stable:
            return
        row = self._conn.execute(
            """
            SELECT COALESCE(NULLIF(display_title, ''), NULLIF(title, ''), '') AS title,
                   COALESCE(preview, '') AS preview,
                   COALESCE(message_count, 0) AS message_count
              FROM sessions
             WHERE id = ?
            """,
            (stable,),
        ).fetchone()
        if row is None:
            return
        title = _row_text(row, "title", 0)
        preview = _row_text(row, "preview", 1)
        message_count = _row_int(row, "message_count", 2)
        now = time.time()
        last_activity = float(timestamp) if timestamp is not None else None
        if last_activity is None:
            self._conn.execute(
                """
                UPDATE session_index
                   SET title = ?,
                       preview = ?,
                       message_count = ?,
                       updated_at = ?
                 WHERE session_id = ?
                """,
                (title, preview, message_count, now, stable),
            )
            return
        self._conn.execute(
            """
            UPDATE session_index
               SET title = ?,
                   preview = ?,
                   message_count = ?,
                   updated_at = MAX(COALESCE(updated_at, 0), ?),
                   last_activity = MAX(COALESCE(last_activity, 0), ?)
             WHERE session_id = ?
            """,
            (title, preview, message_count, last_activity, last_activity, stable),
        )

    def reopen(self, session_id: str) -> None:
        stable = str(session_id or "").strip()
        if not stable:
            raise ValueError("session_id is required for reopen")
        now = time.time()
        assignments: list[str] = []
        params: list[Any] = []
        if "ended_at" in self._session_columns:
            assignments.append("ended_at = NULL")
        if "end_reason" in self._session_columns:
            assignments.append("end_reason = NULL")
        if "updated_at" in self._session_columns:
            assignments.append("updated_at = ?")
            params.append(now)
        elif "last_active" in self._session_columns:
            assignments.append("last_active = ?")
            params.append(now)
        if assignments:
            params.append(stable)
            self._conn.execute(
                f"UPDATE sessions SET {', '.join(assignments)} WHERE id = ?",
                params,
            )
        else:
            self._conn.execute("UPDATE sessions SET id = id WHERE id = ?", (stable,))
        self._conn.execute(
            """
            UPDATE session_index
               SET status = 'idle',
                   running = 0,
                   waiting_approval = 0,
                   active_run_id = '',
                   updated_at = ?
             WHERE session_id = ?
            """,
            (now, stable),
        )

    def _compression_tip(self, session_id: str) -> str:
        required = {"parent_session_id", "started_at", "ended_at", "end_reason"}
        if not required.issubset(self._session_columns):
            return session_id
        current = session_id
        seen = {current}
        for _ in range(100):
            row = self._conn.execute(
                """
                SELECT id
                  FROM sessions
                 WHERE parent_session_id = ?
                   AND started_at >= (
                       SELECT ended_at
                         FROM sessions
                        WHERE id = ?
                          AND end_reason = 'compression'
                   )
                 ORDER BY started_at DESC, id DESC
                 LIMIT 1
                """,
                (current, current),
            ).fetchone()
            if row is None:
                return current
            next_id = _row_text(row, "id", 0)
            if not next_id or next_id in seen:
                return current
            seen.add(next_id)
            current = next_id
        return current

    def _latest_child_session_id(self, session_id: str) -> str:
        if "parent_session_id" not in self._session_columns:
            return ""
        row = self._conn.execute(
            """
            SELECT id
              FROM sessions
             WHERE parent_session_id = ?
             ORDER BY started_at DESC, id DESC
             LIMIT 1
            """,
            (session_id,),
        ).fetchone()
        return _row_text(row, "id", 0) if row is not None else ""


def _apply_column(
    assignments: list[str],
    params: list[Any],
    column: str,
    value: Any,
) -> None:
    if value is None:
        return
    assignments.append(f"{column} = ?")
    params.append(value)


def _row_to_session(row: Any) -> Session:
    if isinstance(row, sqlite3.Row):
        return Session(
            session_id=str(row["id"] or ""),
            source=str(row["source"] or "unknown"),
            title=str(row["title"] or ""),
            display_title=str(row["display_title"] or ""),
            session_kind=str(row["session_kind"] or "hermes_session"),
            conversation_kind=str(row["conversation_kind"] or "direct"),
            started_at=float(row["started_at"] or 0),
            updated_at=float(row["updated_at"] or 0),
            ended_at=float(row["ended_at"]) if row["ended_at"] is not None else None,
            parent_session_id=str(row["parent_session_id"] or ""),
        )
    # Tuple fallback (positional order matches SELECT above).
    return Session(
        session_id=str(row[0] or ""),
        source=str(row[1] or "unknown"),
        title=str(row[2] or ""),
        display_title=str(row[3] or ""),
        session_kind=str(row[4] or "hermes_session"),
        conversation_kind=str(row[5] or "direct"),
        started_at=float(row[6] or 0),
        updated_at=float(row[7] or 0),
        ended_at=float(row[8]) if row[8] is not None else None,
        parent_session_id=str(row[9] or ""),
    )


def _encode_model_config(value: dict[str, Any] | str | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _is_user_visible_conversation(spec: SessionSpec) -> bool:
    session_kind = str(spec.session_kind or "hermes_session").strip().lower()
    conversation_kind = str(spec.conversation_kind or "direct").strip().lower()
    return session_kind != "execution" and conversation_kind in {"direct", "team"}


MAX_SESSION_TITLE_LENGTH = 100


def sanitize_session_title(title: str | None) -> str | None:
    if not title:
        return None
    cleaned = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", str(title))
    cleaned = re.sub(
        r"[\u200b-\u200f\u2028-\u202e\u2060-\u2069\ufeff\ufffc\ufff9-\ufffb]",
        "",
        cleaned,
    )
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if not cleaned:
        return None
    if len(cleaned) > MAX_SESSION_TITLE_LENGTH:
        raise ValueError(
            f"Title too long ({len(cleaned)} chars, max {MAX_SESSION_TITLE_LENGTH})"
        )
    return cleaned


def _sanitize_title(title: str) -> str:
    return sanitize_session_title(title) or ""


def _table_columns(conn: RepositoryConnection, table_name: str) -> set[str]:
    try:
        return {
            str(row["name"] if isinstance(row, sqlite3.Row) else row[1])
            for row in conn.execute(f"PRAGMA table_info({table_name})").fetchall()
        }
    except Exception:
        return set()


def _table_exists(conn: RepositoryConnection, table_name: str) -> bool:
    return bool(_table_columns(conn, table_name))


def ensure_session_lineage_repository_schema(conn: RepositoryConnection) -> None:
    """Upgrade legacy lineage rows to the Session aggregate's canonical shape."""
    columns = _table_columns(conn, "session_lineage")
    additions = {
        "parent_session_id": "TEXT",
        "root_session_id": "TEXT NOT NULL DEFAULT ''",
        "branch_from_message_row_id": "INTEGER",
        "branch_from_turn_id": "TEXT",
        "branch_from_run_id": "TEXT",
        "branch_from_client_message_id": "TEXT",
        "branch_origin": "TEXT NOT NULL DEFAULT 'legacy'",
        "branch_mode": "TEXT NOT NULL DEFAULT 'legacy'",
        "branch_depth": "INTEGER NOT NULL DEFAULT 0",
        "created_at": "REAL NOT NULL DEFAULT 0",
    }
    for name, ddl in additions.items():
        if name not in columns:
            conn.execute(f"ALTER TABLE session_lineage ADD COLUMN {name} {ddl}")
    conn.execute(
        "UPDATE session_lineage SET root_session_id = session_id "
        "WHERE COALESCE(root_session_id, '') = ''"
    )
    conn.execute(
        "DELETE FROM session_lineage WHERE rowid NOT IN ("
        "SELECT MAX(rowid) FROM session_lineage GROUP BY session_id"
        ")"
    )
    conn.executescript(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_session_lineage_session
            ON session_lineage(session_id);
        CREATE INDEX IF NOT EXISTS idx_session_lineage_parent
            ON session_lineage(parent_session_id, created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_session_lineage_root
            ON session_lineage(root_session_id, branch_depth, created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_session_lineage_branch_point
            ON session_lineage(branch_from_message_row_id);
        """
    )


def _column_exists(conn: RepositoryConnection, table_name: str, column_name: str) -> bool:
    return column_name in _table_columns(conn, table_name)


def _affected(cursor: Any) -> int:
    return max(0, int(getattr(cursor, "rowcount", 0) or 0))


def _row_text(row: Any, key: str, index: int) -> str:
    if isinstance(row, sqlite3.Row):
        return str(row[key] or "")
    return str(row[index] or "")


def _row_int(row: Any, key: str, index: int) -> int:
    if isinstance(row, sqlite3.Row):
        return int(row[key] or 0)
    return int(row[index] or 0)


def _row_any(row: Any, key: str, default: Any = None) -> Any:
    if isinstance(row, sqlite3.Row):
        try:
            return row[key]
        except (KeyError, IndexError):
            return default
    if isinstance(row, dict):
        return row.get(key, default)
    return getattr(row, key, default)


__all__ = [
    "BranchLineageSpec",
    "BranchRequestSpec",
    "BranchSpec",
    "MaterializedBranchSessionSpec",
    "Session",
    "SessionFilter",
    "SessionIndexPatch",
    "SessionMessageAppendProjection",
    "SessionMessageSnapshotProjection",
    "ensure_session_lineage_repository_schema",
    "sanitize_session_title",
    "SessionNotFound",
    "SessionRepo",
    "SessionRepoImpl",
    "SessionRunProjection",
    "SessionSpec",
]
