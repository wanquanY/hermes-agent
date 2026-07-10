"""Session aggregate application service."""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from typing import Any

from hermes_agent.domain.message_service import MessageService
from hermes_agent.domain.session_deletion import SessionDeletionResult, SessionDeletionService
from hermes_agent.read_models.session_list import SessionListQuery, SessionListReadModel
from hermes_agent.read_models.session_recall import SessionRecallReadModel
from hermes_agent.repositories.session_repo import SessionRepo, SessionSpec, sanitize_session_title
from hermes_agent.storage.sqlite_connection_lock import lock_for_connection
from hermes_agent.storage.unit_of_work import SqliteUnitOfWork


class SessionService:
    """Public session lifecycle and history-index boundary."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        repo: SessionRepo,
        recall: SessionRecallReadModel,
        messages: MessageService,
        unit_of_work: SqliteUnitOfWork,
        deletion: SessionDeletionService | None = None,
    ) -> None:
        self._conn = conn
        self._lock = lock_for_connection(conn)
        self._repo = repo
        self._recall = recall
        self._list = SessionListReadModel(conn)
        self._messages = messages
        self._unit_of_work = unit_of_work
        self._deletion = deletion or SessionDeletionService(
            conn,
            session_repo=repo,
            unit_of_work=unit_of_work,
        )

    def create(self, session_id: str, source: str, **fields: Any) -> str:
        stable = str(session_id or "").strip()
        if not stable:
            raise ValueError("session_id is required")
        normalized_source = str(source or "unknown").strip() or "unknown"
        transient = bool(fields.get("transient", False))
        session_kind, conversation_kind = _resolve_session_classification(
            source=normalized_source,
            transient=transient,
            session_kind=fields.get("session_kind"),
            conversation_kind=fields.get("conversation_kind"),
        )

        def write(_conn: sqlite3.Connection) -> None:
            self._repo.create(
                SessionSpec(
                    session_id=stable,
                    source=normalized_source,
                    user_id=str(fields.get("user_id") or ""),
                    model=str(fields.get("model") or ""),
                    model_config=fields.get("model_config"),
                    parent_session_id=str(fields.get("parent_session_id") or ""),
                    transient=transient,
                    title=str(fields.get("title") or ""),
                    session_kind=session_kind,
                    conversation_kind=conversation_kind,
                    owner_agent_profile_id=str(fields.get("owner_agent_profile_id") or ""),
                    owner_profile_version_id=str(fields.get("owner_profile_version_id") or ""),
                    runtime_scope_key=str(fields.get("runtime_scope_key") or ""),
                )
            )
            if fields.get("system_prompt"):
                self._repo.update_system_prompt(stable, str(fields["system_prompt"]))
            if str(fields.get("cwd") or "").strip():
                self._repo.update_cwd(stable, str(fields["cwd"]))

        self._unit_of_work.execute(write)
        return stable

    @staticmethod
    def sanitize_title(title: str) -> str:
        return sanitize_session_title(title)

    def ensure(self, session_id: str, source: str = "unknown", **fields: Any) -> str:
        stable = str(session_id or "").strip()
        if not stable:
            raise ValueError("session_id is required")
        if self.get(stable) is None:
            self.create(stable, source, **fields)
        return stable

    def get(self, session_id: str) -> dict[str, Any] | None:
        stable = str(session_id or "").strip()
        if not stable:
            return None
        with self._lock:
            row = self._conn.execute("SELECT * FROM sessions WHERE id = ?", (stable,)).fetchone()
        return dict(row) if row else None

    def resolve_id(self, session_id: str) -> str | None:
        stable = str(session_id or "").strip()
        return stable if stable and self.get(stable) is not None else None

    def end(self, session_id: str, reason: str) -> None:
        self._unit_of_work.execute(lambda _conn: self._repo.close(session_id, str(reason or "")))

    def reopen(self, session_id: str) -> None:
        self._unit_of_work.execute(lambda _conn: self._repo.reopen(session_id))

    def get_title(self, session_id: str) -> str | None:
        title = self._repo.get_title(session_id)
        return title or None

    def set_title(self, session_id: str, title: str, *, title_source: str = "user") -> bool:
        return self._unit_of_work.execute(
            lambda _conn: self._repo.set_title(session_id, title, title_source=title_source)
        )

    def update_cwd(self, session_id: str, cwd: str) -> None:
        self._unit_of_work.execute(lambda _conn: self._repo.update_cwd(session_id, str(cwd or "")))

    def get_by_title(self, title: str) -> dict[str, Any] | None:
        normalized = sanitize_session_title(title)
        if not normalized:
            return None
        with self._lock:
            row = self._conn.execute("SELECT * FROM sessions WHERE title = ?", (normalized,)).fetchone()
        return dict(row) if row else None

    def resolve_by_title(self, title: str) -> str | None:
        normalized = sanitize_session_title(title)
        if not normalized:
            return None
        exact = self.get_by_title(normalized)
        escaped = normalized.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        with self._lock:
            rows = self._conn.execute(
                "SELECT id FROM sessions WHERE title LIKE ? ESCAPE '\\' "
                "ORDER BY started_at DESC, id DESC",
                (f"{escaped} #%",),
            ).fetchall()
        if rows:
            return str(rows[0]["id"])
        return str(exact["id"]) if exact else None

    def resolve_reference(
        self,
        session_id_or_title: str,
    ) -> tuple[str, dict[str, Any] | None]:
        requested = str(session_id_or_title or "").strip()
        if not requested:
            return "", None
        session = self.get(requested)
        if session is not None:
            return requested, session
        session = self.get_by_title(requested)
        if session is None:
            return requested, None
        return str(session.get("id") or requested), session

    def next_title_in_lineage(self, base_title: str) -> str:
        match = re.match(r"^(.*?) #(\d+)$", str(base_title or ""))
        base = match.group(1) if match else str(base_title or "")
        escaped = base.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        with self._lock:
            rows = self._conn.execute(
                "SELECT title FROM sessions WHERE title = ? OR title LIKE ? ESCAPE '\\'",
                (base, f"{escaped} #%"),
            ).fetchall()
        existing = [str(row["title"] or "") for row in rows]
        if not existing:
            return base
        max_num = 1
        for existing_title in existing:
            suffix = re.match(r"^.* #(\d+)$", existing_title)
            if suffix:
                max_num = max(max_num, int(suffix.group(1)))
        return f"{base} #{max_num + 1}"

    def resolve_resume_id(self, session_id: str) -> str:
        return str(self._recall.resolve_resume_session_id(session_id) or "")

    def compression_tip(self, session_id: str) -> str:
        return self._recall.get_compression_tip(session_id)

    def list_rich(self, **query: Any) -> list[dict[str, Any]]:
        return self._recall.list_sessions_rich(**query)

    def list(
        self,
        *,
        source: str | None = None,
        exclude_sources: tuple[str, ...] = (),
        limit: int = 20,
        offset: int = 0,
        include_children: bool = False,
        project_compression_tips: bool = True,
        order_by_last_active: bool = False,
        page_cursor: dict[str, Any] | None = None,
        id_query: str | None = None,
    ) -> list[dict[str, Any]]:
        return self._list.list(
            SessionListQuery(
                source=source,
                exclude_sources=exclude_sources,
                limit=limit,
                offset=offset,
                include_children=include_children,
                project_compression_tips=project_compression_tips,
                order_by_last_active=order_by_last_active,
                page_cursor=page_cursor,
                id_query=id_query,
            )
        )

    def search(
        self,
        source: str | None = None,
        limit: int = 20,
        offset: int = 0,
        **_query: Any,
    ) -> list[dict[str, Any]]:
        return self._recall.list_sessions_rich(
            source=source,
            limit=limit,
            offset=offset,
            include_children=True,
            order_by_last_active=True,
        )

    def search_by_id(self, query: str, limit: int = 20) -> list[dict[str, Any]]:
        needle = str(query or "").strip().lower()
        if not needle:
            return []
        bounded_limit = max(1, min(_to_int(limit, 20), 100))
        candidates = self._recall.list_sessions_rich(
            limit=max(bounded_limit * 4, bounded_limit),
            offset=0,
            include_children=True,
            order_by_last_active=True,
            id_query=needle,
            archived="all",
        )

        def score(row: dict[str, Any]) -> int:
            values = [str(row.get("id") or "").lower(), str(row.get("_lineage_root_id") or "").lower()]
            if any(value == needle for value in values):
                return 0
            return 1 if any(value.startswith(needle) for value in values) else 2

        ranked = sorted(enumerate(candidates), key=lambda item: (score(item[1]), item[0]))
        return [row for _, row in ranked[:bounded_limit]]

    def count(
        self,
        source: str | None = None,
        *,
        min_message_count: int = 0,
        archived: str = "false",
        **_query: Any,
    ) -> int:
        return self._recall.session_count(
            source=source,
            min_message_count=min_message_count,
            archived=archived,
        )

    def export(self, session_id: str) -> dict[str, Any] | None:
        session = self.get(session_id)
        if not session:
            return None
        return {**session, "messages": self._messages.list(session_id, include_inactive=True)}

    def export_all(self, source: str | None = None) -> list[dict[str, Any]]:
        sessions = self.search(source=source, limit=100_000)
        return [
            {**session, "messages": self._messages.list(str(session.get("id") or ""), include_inactive=True)}
            for session in sessions
        ]

    def update_token_counts(self, session_id: str, **counts: Any) -> None:
        self._unit_of_work.execute(lambda _conn: self._repo.update_usage(session_id, counts))

    def update_runtime_config(
        self,
        session_id: str,
        model_config: dict[str, Any],
        *,
        model: str | None = None,
    ) -> bool:
        return self._unit_of_work.execute(
            lambda _conn: self._repo.update_runtime_config(
                session_id,
                model_config,
                model,
            )
        )

    def set_archived(self, session_id: str, archived: bool) -> bool:
        return self._unit_of_work.execute(lambda _conn: self._repo.set_archived(session_id, archived))

    def delete(
        self,
        session_id: str,
        *,
        sessions_dir: Path | None = None,
    ) -> SessionDeletionResult:
        return self._deletion.delete(session_id, sessions_dir=sessions_dir)

    def latest_descendant(self, session_id: str) -> tuple[str | None, list[str]]:
        stable = self.resolve_id(session_id)
        if not stable:
            return None, []
        with self._lock:
            rows = self._conn.execute("SELECT id, parent_session_id, started_at FROM sessions").fetchall()
        children: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            item = dict(row)
            parent = str(item.get("parent_session_id") or "")
            if parent:
                children.setdefault(parent, []).append(item)
        current = stable
        path = [stable]
        seen = {stable}
        while children.get(current):
            candidates = [row for row in children[current] if row.get("id") not in seen]
            if not candidates:
                break
            candidates.sort(key=lambda row: float(row.get("started_at") or 0), reverse=True)
            current = str(candidates[0]["id"])
            path.append(current)
            seen.add(current)
        return current, path

    def update_system_prompt(self, session_id: str, prompt: str) -> None:
        self._unit_of_work.execute(lambda _conn: self._repo.update_system_prompt(session_id, prompt))

    def request_handoff(self, session_id: str, platform: str) -> bool:
        return self._unit_of_work.execute(lambda _conn: self._repo.request_handoff(session_id, platform))

    def list_pending_handoffs(self) -> list[dict[str, Any]]:
        with self._lock:
            return self._repo.list_pending_handoffs()

    def claim_handoff(self, session_id: str) -> bool:
        return self._unit_of_work.execute(lambda _conn: self._repo.claim_handoff(session_id))

    def complete_handoff(self, session_id: str) -> None:
        self._unit_of_work.execute(lambda _conn: self._repo.complete_handoff(session_id))

    def handoff_state(self, session_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT handoff_state, handoff_platform, handoff_error FROM sessions WHERE id = ?",
                (str(session_id or ""),),
            ).fetchone()
        if row is None:
            return None
        return {"state": row["handoff_state"], "platform": row["handoff_platform"], "error": row["handoff_error"]}

    def fail_handoff(self, session_id: str, error: str) -> None:
        reason = str(error or "")[:500]
        self._unit_of_work.execute(lambda _conn: self._repo.fail_handoff(session_id, reason))


def _resolve_session_classification(
    *,
    source: str,
    transient: bool,
    session_kind: Any,
    conversation_kind: Any,
) -> tuple[str, str]:
    explicit_session_kind = str(session_kind or "").strip().lower()
    explicit_conversation_kind = str(conversation_kind or "").strip().lower()

    if explicit_session_kind or explicit_conversation_kind:
        if explicit_session_kind == "execution" or explicit_conversation_kind == "internal":
            return explicit_session_kind or "execution", explicit_conversation_kind or "internal"
        if explicit_session_kind == "team_mission" or explicit_conversation_kind == "team":
            return explicit_session_kind or "team_mission", explicit_conversation_kind or "team"
        return explicit_session_kind or "hermes_session", explicit_conversation_kind or "direct"

    if source == "team_mission":
        if transient:
            return "execution", "internal"
        return "team_mission", "team"
    return "hermes_session", "direct"


def _to_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


__all__ = ["SessionService"]
