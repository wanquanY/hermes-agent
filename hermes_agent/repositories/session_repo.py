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
from dataclasses import dataclass, field
from typing import Any, Iterable, Protocol, runtime_checkable

from hermes_agent.repositories.base import RepositoryConnection


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
class BranchSpec:
    """Payload for branch() — spawn a child session from a source."""

    new_session_id: str
    branch_from_seq: int
    title: str = ""
    display_title: str = ""


class SessionNotFound(LookupError):
    """Raised when a session_id has no corresponding row."""


@runtime_checkable
class SessionRepo(Protocol):
    """Aggregate root for the ``sessions`` table family.

    Owned tables: ``sessions``, ``session_index``, ``session_branches``,
    ``session_handoffs``.
    """

    def create(self, spec: SessionSpec) -> Session: ...

    def get(self, session_id: str) -> Session | None: ...

    def list(self, filter: SessionFilter) -> Iterable[Session]: ...

    def update_index(self, session_id: str, patch: SessionIndexPatch) -> None: ...

    def get_title(self, session_id: str) -> str | None: ...

    def get_by_title(self, title: str) -> Session | None: ...

    def set_title(self, session_id: str, title: str, *, title_source: str = "user") -> bool: ...

    def resolve_resume_session_id(self, session_id: str) -> str: ...

    def branch(self, source_id: str, spec: BranchSpec) -> Session: ...

    def close(self, session_id: str, reason: str) -> None: ...

    def reopen(self, session_id: str) -> None: ...


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
        now = time.time()
        self._conn.execute(
            """
            INSERT OR REPLACE INTO sessions (
                id, source, user_id, model, model_config,
                title, display_title, display_title_source,
                session_kind, conversation_kind, parent_session_id,
                started_at, updated_at, transient
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                stable,
                str(spec.source or "unknown"),
                str(spec.user_id or ""),
                str(spec.model or ""),
                _encode_model_config(spec.model_config),
                str(spec.title or ""),
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
        # spec §4.1 — session_index row auto-provisioned so downstream
        # projections have a target for update_index().
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

    def resolve_resume_session_id(self, session_id: str) -> str:
        stable = str(session_id or "").strip()
        if not stable:
            return stable
        target = self._compression_tip(stable)
        if _session_has_messages(self._conn, target):
            return target
        current = target
        seen = {current}
        for _ in range(32):
            child_id = self._latest_child_session_id(current)
            if not child_id or child_id in seen:
                return target
            seen.add(child_id)
            if _session_has_messages(self._conn, child_id):
                return child_id
            current = child_id
        return target

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


def _session_has_messages(conn: RepositoryConnection, session_id: str) -> bool:
    if "messages" not in _table_names(conn):
        return False
    columns = _table_columns(conn, "messages")
    active_clause = " AND active = 1" if "active" in columns else ""
    row = conn.execute(
        f"SELECT 1 FROM messages WHERE session_id = ?{active_clause} LIMIT 1",
        (session_id,),
    ).fetchone()
    return row is not None


def _encode_model_config(value: dict[str, Any] | str | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


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


def _table_names(conn: RepositoryConnection) -> set[str]:
    try:
        return {
            str(row["name"] if isinstance(row, sqlite3.Row) else row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
    except Exception:
        return set()


def _row_text(row: Any, key: str, index: int) -> str:
    if isinstance(row, sqlite3.Row):
        return str(row[key] or "")
    return str(row[index] or "")


__all__ = [
    "BranchSpec",
    "Session",
    "SessionFilter",
    "SessionIndexPatch",
    "sanitize_session_title",
    "SessionNotFound",
    "SessionRepo",
    "SessionRepoImpl",
    "SessionSpec",
]
