"""SQLite session store for the interactive CLI.

This is the CLI-facing persistence owner used during the P2 state-facade
retirement. It deliberately talks to SQLite/repositories directly and does not
import the legacy state facade.
"""

from __future__ import annotations

import json
import re
import shutil
import sqlite3
import time
from pathlib import Path
from typing import Any

from agent.memory_manager import sanitize_context
from hermes_agent.read_models.message_history import MessageHistoryReadModel
from hermes_agent.read_models.session_recall import SessionRecallReadModel
from hermes_agent.repositories.agent_profile_repo import AgentProfileRepoImpl
from hermes_agent.repositories.message_repo import MessageRepository
from hermes_agent.repositories.session_repo import (
    SessionRepoImpl,
    SessionSpec,
    sanitize_session_title,
)
from hermes_agent.storage.session_repository_db import connect_session_repository_db
from hermes_agent.storage.sqlite_connection_lock import lock_for_connection


def open_cli_session_store(db_path: Path | str | None = None):
    return CliSessionStore(connect_session_repository_db(db_path))


class CliSessionStore:
    """Method surface currently required by CLI and AIAgent persistence."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self._lock = lock_for_connection(conn)
        self._sessions = SessionRepoImpl(conn)
        self._profiles = AgentProfileRepoImpl(conn)
        self._message_writer = MessageRepository(conn, self._sessions)
        self._messages = MessageHistoryReadModel(conn)
        self._recall = SessionRecallReadModel(conn)

    @property
    def db_path(self) -> Path:
        row = self._conn.execute("PRAGMA database_list").fetchone()
        if row is None:
            return Path("")
        file_value = row["file"] if "file" in row.keys() else row[2]
        return Path(str(file_value or ""))

    def close(self) -> None:
        self._conn.close()

    def create_session(self, session_id: str, source: str, **kwargs: Any) -> str:
        self._sessions.create(
            SessionSpec(
                session_id=str(session_id or ""),
                source=str(source or "unknown"),
                user_id=str(kwargs.get("user_id") or ""),
                model=str(kwargs.get("model") or ""),
                model_config=kwargs.get("model_config"),
                parent_session_id=str(kwargs.get("parent_session_id") or ""),
                transient=bool(kwargs.get("transient", False)),
                title=str(kwargs.get("title") or ""),
            )
        )
        system_prompt = kwargs.get("system_prompt")
        if system_prompt:
            self.update_system_prompt(session_id, str(system_prompt))
        cwd = str(kwargs.get("cwd") or "").strip()
        if cwd:
            self.update_session_cwd(session_id, cwd)
        return str(session_id or "")

    def ensure_session(self, session_id: str, source: str = "unknown", **kwargs: Any) -> str:
        stable = str(session_id or "").strip()
        if not stable:
            raise ValueError("session_id is required")
        if self.get_session(stable) is None:
            self.create_session(stable, source, **kwargs)
        return stable

    def get_session(self, session_id: str) -> dict[str, Any] | None:
        stable = str(session_id or "").strip()
        if not stable:
            return None
        row = self._conn.execute("SELECT * FROM sessions WHERE id = ?", (stable,)).fetchone()
        return dict(row) if row else None

    def resolve_session_id(self, session_id: str) -> str | None:
        stable = str(session_id or "").strip()
        if not stable:
            return None
        return stable if self.get_session(stable) is not None else None

    def end_session(self, session_id: str, end_reason: str) -> None:
        now = time.time()
        with self._lock:
            self._conn.execute(
                "UPDATE sessions SET ended_at = ?, end_reason = ?, updated_at = ? "
                "WHERE id = ? AND ended_at IS NULL",
                (now, str(end_reason or ""), now, str(session_id or "")),
            )
            self._conn.execute(
                "UPDATE session_index SET status = 'closed', running = 0, updated_at = ? "
                "WHERE session_id = ?",
                (now, str(session_id or "")),
            )
            self._conn.commit()

    def reopen_session(self, session_id: str) -> None:
        now = time.time()
        with self._lock:
            self._conn.execute(
                "UPDATE sessions SET ended_at = NULL, end_reason = NULL, updated_at = ? WHERE id = ?",
                (now, str(session_id or "")),
            )
            self._conn.execute(
                "UPDATE session_index SET status = 'idle', updated_at = ? WHERE session_id = ?",
                (now, str(session_id or "")),
            )
            self._conn.commit()

    def get_session_title(self, session_id: str) -> str | None:
        row = self._conn.execute(
            "SELECT title FROM sessions WHERE id = ?",
            (str(session_id or ""),),
        ).fetchone()
        if not row or not row["title"]:
            return None
        return str(row["title"])

    def set_session_title(self, session_id: str, title: str, *, title_source: str = "user") -> bool:
        return self._sessions.set_title(session_id, title, title_source=title_source)

    def update_session_cwd(self, session_id: str, cwd: str) -> None:
        stable = str(session_id or "").strip()
        if not stable:
            raise ValueError("session_id is required")
        now = time.time()
        with self._lock:
            self._conn.execute(
                "UPDATE sessions SET cwd = ?, updated_at = ? WHERE id = ?",
                (str(cwd or ""), now, stable),
            )
            self._conn.execute(
                "UPDATE session_index SET updated_at = ? WHERE session_id = ?",
                (now, stable),
            )
            self._conn.commit()

    def get_session_by_title(self, title: str) -> dict[str, Any] | None:
        normalized = sanitize_session_title(title)
        if not normalized:
            return None
        row = self._conn.execute("SELECT * FROM sessions WHERE title = ?", (normalized,)).fetchone()
        return dict(row) if row else None

    def resolve_session_by_title(self, title: str) -> str | None:
        normalized = sanitize_session_title(title)
        if not normalized:
            return None
        exact = self.get_session_by_title(normalized)
        escaped = normalized.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        rows = self._conn.execute(
            "SELECT id FROM sessions WHERE title LIKE ? ESCAPE '\\' "
            "ORDER BY started_at DESC, id DESC",
            (f"{escaped} #%",),
        ).fetchall()
        if rows:
            return str(rows[0]["id"])
        if exact:
            return str(exact["id"])
        return None

    def upsert_agent_profile(self, **kwargs: Any) -> dict[str, Any]:
        with self._lock:
            return self._profiles.upsert_agent_profile(**kwargs)

    def get_agent_profile(self, profile_id: str) -> dict[str, Any]:
        with self._lock:
            return self._profiles.get_agent_profile(profile_id)

    def get_agent_profile_by_slug(self, slug: str) -> dict[str, Any]:
        with self._lock:
            return self._profiles.get_agent_profile_by_slug(slug)

    def list_agent_profiles(self, *, include_archived: bool = False) -> list[dict[str, Any]]:
        with self._lock:
            return self._profiles.list_agent_profiles(include_archived=include_archived)

    def archive_agent_profile(self, profile_id: str) -> dict[str, Any]:
        with self._lock:
            return self._profiles.archive_agent_profile(profile_id)

    def agent_profile_growth_summary(
        self,
        agent_profile_id: str,
        *,
        agent_profile_version_id: str = "",
        range_preset: str = "",
        start_date: str = "",
        end_date: str = "",
    ) -> dict[str, Any]:
        with self._lock:
            return self._profiles.agent_profile_growth_summary(
                agent_profile_id,
                agent_profile_version_id=agent_profile_version_id,
                range_preset=range_preset,
                start_date=start_date,
                end_date=end_date,
            )

    def upsert_agent_profile_draft(self, **kwargs: Any) -> dict[str, Any]:
        with self._lock:
            return self._profiles.upsert_agent_profile_draft(**kwargs)

    def get_agent_profile_draft(self, draft_id: str) -> dict[str, Any]:
        with self._lock:
            return self._profiles.get_agent_profile_draft(draft_id)

    def list_agent_profile_drafts(
        self,
        *,
        include_published: bool = False,
        include_discarded: bool = False,
        statuses: list[str] | None = None,
        source_session_id: str = "",
        source_agent_profile_id: str = "",
        workspace_id: str = "",
    ) -> list[dict[str, Any]]:
        with self._lock:
            return self._profiles.list_agent_profile_drafts(
                include_published=include_published,
                include_discarded=include_discarded,
                statuses=statuses,
                source_session_id=source_session_id,
                source_agent_profile_id=source_agent_profile_id,
                workspace_id=workspace_id,
            )

    def discard_agent_profile_draft(self, draft_id: str) -> dict[str, Any]:
        with self._lock:
            return self._profiles.discard_agent_profile_draft(draft_id)

    def get_next_title_in_lineage(self, base_title: str) -> str:
        match = re.match(r"^(.*?) #(\d+)$", str(base_title or ""))
        base = match.group(1) if match else str(base_title or "")
        escaped = base.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        rows = self._conn.execute(
            "SELECT title FROM sessions WHERE title = ? OR title LIKE ? ESCAPE '\\'",
            (base, f"{escaped} #%"),
        ).fetchall()
        existing = [str(row["title"] or "") for row in rows]
        if not existing:
            return base
        max_num = 1
        for title in existing:
            suffix = re.match(r"^.* #(\d+)$", title)
            if suffix:
                max_num = max(max_num, int(suffix.group(1)))
        return f"{base} #{max_num + 1}"

    def resolve_resume_session_id(self, session_id: str) -> str:
        return self._sessions.resolve_resume_session_id(session_id)

    def get_compression_tip(self, session_id: str) -> str:
        return self._recall.get_compression_tip(session_id)

    def list_sessions_rich(
        self,
        *,
        source: str | None = None,
        limit: int = 20,
        offset: int = 0,
        exclude_sources: list[str] | None = None,
        include_children: bool = False,
        order_by_last_active: bool = True,
        **kwargs: Any,
    ) -> list[dict[str, Any]]:
        return self._recall.list_sessions_rich(
            source=source,
            exclude_sources=exclude_sources,
            limit=limit,
            offset=offset,
            include_children=include_children,
            order_by_last_active=order_by_last_active,
            **kwargs,
        )

    def search_sessions(
        self,
        source: str | None = None,
        limit: int = 20,
        offset: int = 0,
        **_kwargs: Any,
    ) -> list[dict[str, Any]]:
        return self._recall.list_sessions_rich(
            source=source,
            limit=limit,
            offset=offset,
            include_children=True,
            order_by_last_active=True,
        )

    def search_sessions_by_id(self, query: str, limit: int = 20) -> list[dict[str, Any]]:
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
            values = [
                str(row.get("id") or "").lower(),
                str(row.get("_lineage_root_id") or "").lower(),
            ]
            if any(value == needle for value in values):
                return 0
            if any(value.startswith(needle) for value in values):
                return 1
            return 2

        ranked = sorted(enumerate(candidates), key=lambda item: (score(item[1]), item[0]))
        return [row for _, row in ranked[:bounded_limit]]

    def session_count(
        self,
        source: str | None = None,
        *,
        min_message_count: int = 0,
        archived: str = "false",
        **_kwargs: Any,
    ) -> int:
        return self._recall.session_count(
            source=source,
            min_message_count=min_message_count,
            archived=archived,
        )

    def message_count(self, session_id: str | None = None) -> int:
        stable = str(session_id or "").strip()
        if stable:
            row = self._conn.execute(
                "SELECT COUNT(*) AS count FROM messages WHERE session_id = ?",
                (stable,),
            ).fetchone()
        else:
            row = self._conn.execute("SELECT COUNT(*) AS count FROM messages").fetchone()
        return int(row["count"] if row else 0)

    def search_messages(
        self,
        query: str,
        source_filter: list[str] | None = None,
        exclude_sources: list[str] | None = None,
        role_filter: list[str] | None = None,
        limit: int = 20,
        offset: int = 0,
        sort: str | None = None,
        include_inactive: bool = False,
    ) -> list[dict[str, Any]]:
        return self._recall.search_messages(
            query,
            source_filter=source_filter,
            exclude_sources=exclude_sources,
            role_filter=role_filter,
            limit=limit,
            offset=offset,
            sort=sort,
            include_inactive=include_inactive,
        )

    def get_messages(self, session_id: str, include_inactive: bool = False) -> list[dict[str, Any]]:
        active_clause = "" if include_inactive else " AND active = 1"
        rows = self._conn.execute(
            "SELECT * FROM messages WHERE session_id = ?"
            f"{active_clause} ORDER BY id",
            (str(session_id or ""),),
        ).fetchall()
        return [_message_row(row) for row in rows]

    def get_messages_as_conversation(
        self,
        session_id: str,
        include_ancestors: bool = False,
        include_storage_metadata: bool = False,
        include_inactive: bool = False,
    ) -> list[dict[str, Any]]:
        return self._messages.all_as_conversation(
            session_id,
            include_ancestors=include_ancestors,
            include_storage_metadata=include_storage_metadata,
            include_inactive=include_inactive,
        )

    def export_session(self, session_id: str) -> dict[str, Any] | None:
        session = self.get_session(session_id)
        if not session:
            return None
        return {**session, "messages": self.get_messages(session_id, include_inactive=True)}

    def export_all(self, source: str | None = None) -> list[dict[str, Any]]:
        sessions = self.search_sessions(
            source=source,
            limit=100_000,
            include_children=True,
            archived="all",
        )
        exported: list[dict[str, Any]] = []
        for session in sessions:
            session_id = str(session.get("id") or "")
            exported.append({**session, "messages": self.get_messages(session_id, include_inactive=True)})
        return exported

    def append_message(
        self,
        session_id: str,
        role: str,
        content: Any,
        *,
        participant_id: str = "",
        tool_call_id: str | None = None,
        tool_calls: Any = None,
        tool_name: str | None = None,
        token_count: int | None = None,
        finish_reason: str | None = None,
        reasoning: str | None = None,
        reasoning_content: str | None = None,
        reasoning_details: Any = None,
        codex_reasoning_items: Any = None,
        codex_message_items: Any = None,
        platform_message_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> int:
        stable = str(session_id or "").strip()
        if not stable:
            raise ValueError("session_id is required")
        if self.get_session(stable) is None:
            self.create_session(stable, source="cli")
        message: dict[str, Any] = {
            "role": str(role or "unknown"),
            "content": content,
            "participant_id": str(participant_id or ""),
            "tool_call_id": tool_call_id,
            "tool_calls": tool_calls,
            "tool_name": tool_name,
            "token_count": token_count,
            "finish_reason": finish_reason,
            "reasoning": reasoning,
            "reasoning_content": reasoning_content,
            "reasoning_details": reasoning_details,
            "codex_reasoning_items": codex_reasoning_items,
            "codex_message_items": codex_message_items,
            "platform_message_id": platform_message_id,
            "metadata": metadata,
        }
        return self._message_writer.append_conversation_message(stable, message)

    def replace_messages(self, session_id: str, messages: list[dict[str, Any]]) -> None:
        stable = str(session_id or "").strip()
        if not stable:
            raise ValueError("session_id is required")
        self._message_writer.replace_conversation(stable, messages)

    def update_token_counts(self, session_id: str, **counts: Any) -> None:
        columns = {
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
        for key, value in counts.items():
            if key in columns:
                assignments.append(f"{key} = ?")
                params.append(value)
        if not assignments:
            return
        assignments.append("updated_at = ?")
        params.extend([time.time(), str(session_id or "")])
        with self._lock:
            self._conn.execute(
                f"UPDATE sessions SET {', '.join(assignments)} WHERE id = ?",
                params,
            )
            self._conn.commit()

    def set_session_archived(self, session_id: str, archived: bool) -> bool:
        stable = str(session_id or "").strip()
        if not stable:
            return False
        with self._lock:
            cursor = self._conn.execute(
                "UPDATE sessions SET archived = ?, updated_at = ? WHERE id = ?",
                (1 if archived else 0, time.time(), stable),
            )
            self._conn.commit()
        return int(cursor.rowcount or 0) > 0

    def latest_descendant(self, session_id: str) -> tuple[str | None, list[str]]:
        stable = self.resolve_session_id(session_id)
        if not stable:
            return None, []
        rows = self._conn.execute(
            "SELECT id, parent_session_id, started_at FROM sessions"
        ).fetchall()
        children: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            item = dict(row)
            parent = str(item.get("parent_session_id") or "")
            if parent:
                children.setdefault(parent, []).append(item)

        def started(row: dict[str, Any]) -> float:
            try:
                return float(row.get("started_at") or 0)
            except (TypeError, ValueError):
                return 0.0

        current = stable
        path = [stable]
        seen = {stable}
        while children.get(current):
            candidates = [row for row in children[current] if row.get("id") not in seen]
            if not candidates:
                break
            candidates.sort(key=started, reverse=True)
            current = str(candidates[0]["id"])
            path.append(current)
            seen.add(current)
        return current, path

    def usage_analytics(self, days: int = 30) -> dict[str, Any]:
        from agent.insights import InsightsEngine

        cutoff = time.time() - (int(days or 30) * 86400)
        daily_rows = self._conn.execute(
            """
            SELECT date(started_at, 'unixepoch') AS day,
                   SUM(input_tokens) AS input_tokens,
                   SUM(output_tokens) AS output_tokens,
                   SUM(cache_read_tokens) AS cache_read_tokens,
                   SUM(reasoning_tokens) AS reasoning_tokens,
                   COALESCE(SUM(estimated_cost_usd), 0) AS estimated_cost,
                   COALESCE(SUM(actual_cost_usd), 0) AS actual_cost,
                   COUNT(*) AS sessions,
                   SUM(COALESCE(api_call_count, 0)) AS api_calls
              FROM sessions WHERE started_at > ?
             GROUP BY day ORDER BY day
            """,
            (cutoff,),
        ).fetchall()
        model_rows = self._conn.execute(
            """
            SELECT model,
                   SUM(input_tokens) AS input_tokens,
                   SUM(output_tokens) AS output_tokens,
                   COALESCE(SUM(estimated_cost_usd), 0) AS estimated_cost,
                   COUNT(*) AS sessions,
                   SUM(COALESCE(api_call_count, 0)) AS api_calls
              FROM sessions
             WHERE started_at > ? AND model IS NOT NULL
             GROUP BY model
             ORDER BY SUM(input_tokens) + SUM(output_tokens) DESC
            """,
            (cutoff,),
        ).fetchall()
        totals = dict(self._conn.execute(
            """
            SELECT SUM(input_tokens) AS total_input,
                   SUM(output_tokens) AS total_output,
                   SUM(cache_read_tokens) AS total_cache_read,
                   SUM(reasoning_tokens) AS total_reasoning,
                   COALESCE(SUM(estimated_cost_usd), 0) AS total_estimated_cost,
                   COALESCE(SUM(actual_cost_usd), 0) AS total_actual_cost,
                   COUNT(*) AS total_sessions,
                   SUM(COALESCE(api_call_count, 0)) AS total_api_calls
              FROM sessions WHERE started_at > ?
            """,
            (cutoff,),
        ).fetchone())
        insights_report = InsightsEngine(self).generate(days=days)
        return {
            "daily": [dict(row) for row in daily_rows],
            "by_model": [dict(row) for row in model_rows],
            "totals": totals,
            "period_days": days,
            "skills": insights_report.get("skills", _empty_skill_breakdown()),
        }

    def model_analytics(self, days: int = 30) -> dict[str, Any]:
        cutoff = time.time() - (int(days or 30) * 86400)
        rows = self._conn.execute(
            """
            SELECT model,
                   billing_provider,
                   SUM(input_tokens) AS input_tokens,
                   SUM(output_tokens) AS output_tokens,
                   SUM(cache_read_tokens) AS cache_read_tokens,
                   SUM(reasoning_tokens) AS reasoning_tokens,
                   COALESCE(SUM(estimated_cost_usd), 0) AS estimated_cost,
                   COALESCE(SUM(actual_cost_usd), 0) AS actual_cost,
                   COUNT(*) AS sessions,
                   SUM(COALESCE(api_call_count, 0)) AS api_calls,
                   SUM(tool_call_count) AS tool_calls,
                   MAX(started_at) AS last_used_at,
                   AVG(input_tokens + output_tokens) AS avg_tokens_per_session
              FROM sessions
             WHERE started_at > ? AND model IS NOT NULL AND model != ''
             GROUP BY model, billing_provider
             ORDER BY SUM(input_tokens) + SUM(output_tokens) DESC
            """,
            (cutoff,),
        ).fetchall()
        totals = dict(self._conn.execute(
            """
            SELECT COUNT(DISTINCT model) AS distinct_models,
                   SUM(input_tokens) AS total_input,
                   SUM(output_tokens) AS total_output,
                   SUM(cache_read_tokens) AS total_cache_read,
                   SUM(reasoning_tokens) AS total_reasoning,
                   COALESCE(SUM(estimated_cost_usd), 0) AS total_estimated_cost,
                   COALESCE(SUM(actual_cost_usd), 0) AS total_actual_cost,
                   COUNT(*) AS total_sessions,
                   SUM(COALESCE(api_call_count, 0)) AS total_api_calls
              FROM sessions
             WHERE started_at > ? AND model IS NOT NULL AND model != ''
            """,
            (cutoff,),
        ).fetchone())
        return {"rows": [dict(row) for row in rows], "totals": totals, "period_days": days}

    def update_system_prompt(self, session_id: str, system_prompt: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE sessions SET system_prompt = ?, updated_at = ? WHERE id = ?",
                (str(system_prompt or ""), time.time(), str(session_id or "")),
            )
            self._conn.commit()

    def request_handoff(self, session_id: str, platform: str) -> bool:
        with self._lock:
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
                (str(platform or ""), time.time(), str(session_id or "")),
            )
            self._conn.commit()
        return int(cursor.rowcount or 0) > 0

    def get_handoff_state(self, session_id: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT handoff_state, handoff_platform, handoff_error FROM sessions WHERE id = ?",
            (str(session_id or ""),),
        ).fetchone()
        if row is None:
            return None
        return {
            "state": row["handoff_state"],
            "platform": row["handoff_platform"],
            "error": row["handoff_error"],
        }

    def fail_handoff(self, session_id: str, error: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE sessions SET handoff_state = 'failed', handoff_error = ?, updated_at = ? WHERE id = ?",
                (str(error or ""), time.time(), str(session_id or "")),
            )
            self._conn.commit()

    def delete_session(self, session_id: str, sessions_dir: Path | None = None) -> bool:
        stable = str(session_id or "").strip()
        if not stable:
            return False
        has_session_lineage = self._table_exists("session_lineage")
        lineage_columns = self._table_columns("session_lineage") if has_session_lineage else set()
        has_branch_requests = self._table_exists("session_branch_requests")
        with self._lock:
            exists = self._conn.execute(
                "SELECT COUNT(*) AS count FROM sessions WHERE id = ?",
                (stable,),
            ).fetchone()
            if int(exists["count"] if exists else 0) == 0:
                return False
            self._conn.execute(
                "UPDATE sessions SET parent_session_id = NULL WHERE parent_session_id = ?",
                (stable,),
            )
            if "parent_session_id" in lineage_columns:
                self._conn.execute(
                    "UPDATE session_lineage SET parent_session_id = NULL WHERE parent_session_id = ?",
                    (stable,),
                )
            if has_branch_requests:
                self._conn.execute(
                    "DELETE FROM session_branch_requests WHERE source_session_id = ? OR result_session_id = ?",
                    (stable, stable),
                )
            if has_session_lineage:
                self._conn.execute("DELETE FROM session_lineage WHERE session_id = ?", (stable,))
            self._conn.execute("DELETE FROM messages WHERE session_id = ?", (stable,))
            self._conn.execute("DELETE FROM sessions WHERE id = ?", (stable,))
            self._conn.execute("DELETE FROM session_index WHERE session_id = ?", (stable,))
            self._conn.commit()
        if sessions_dir is not None:
            self._remove_session_files(Path(sessions_dir), stable)
        return True

    def prune_sessions(
        self,
        older_than_days: int = 90,
        source: str | None = None,
        sessions_dir: Path | None = None,
    ) -> int:
        cutoff = time.time() - (int(older_than_days or 0) * 86400)
        params: list[Any] = [cutoff]
        source_clause = ""
        if source:
            source_clause = " AND source = ?"
            params.append(str(source))
        rows = self._conn.execute(
            f"SELECT id FROM sessions WHERE started_at < ? AND ended_at IS NOT NULL{source_clause}",
            tuple(params),
        ).fetchall()
        session_ids = [str(row["id"] or "") for row in rows if str(row["id"] or "")]
        if not session_ids:
            return 0
        placeholders = ",".join("?" for _ in session_ids)
        has_session_lineage = self._table_exists("session_lineage")
        lineage_columns = self._table_columns("session_lineage") if has_session_lineage else set()
        has_branch_requests = self._table_exists("session_branch_requests")
        with self._lock:
            self._conn.execute(
                f"UPDATE sessions SET parent_session_id = NULL WHERE parent_session_id IN ({placeholders})",
                tuple(session_ids),
            )
            if "parent_session_id" in lineage_columns:
                self._conn.execute(
                    f"UPDATE session_lineage SET parent_session_id = NULL "
                    f"WHERE parent_session_id IN ({placeholders})",
                    tuple(session_ids),
                )
            if has_branch_requests:
                self._conn.execute(
                    f"DELETE FROM session_branch_requests "
                    f"WHERE source_session_id IN ({placeholders}) OR result_session_id IN ({placeholders})",
                    tuple(session_ids + session_ids),
                )
            if has_session_lineage:
                self._conn.execute(
                    f"DELETE FROM session_lineage WHERE session_id IN ({placeholders})",
                    tuple(session_ids),
                )
            for stable in session_ids:
                self._conn.execute("DELETE FROM messages WHERE session_id = ?", (stable,))
                self._conn.execute("DELETE FROM session_index WHERE session_id = ?", (stable,))
                self._conn.execute("DELETE FROM sessions WHERE id = ?", (stable,))
            self._conn.commit()
        if sessions_dir is not None:
            for stable in session_ids:
                self._remove_session_files(Path(sessions_dir), stable)
        return len(session_ids)

    @staticmethod
    def _remove_session_files(sessions_dir: Path, session_id: str) -> None:
        if not sessions_dir:
            return
        for suffix in (".json", ".jsonl"):
            try:
                (sessions_dir / f"{session_id}{suffix}").unlink(missing_ok=True)
            except OSError:
                pass
        try:
            for path in sessions_dir.glob(f"request_dump_{session_id}_*.json"):
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass
            legacy_dir = sessions_dir / session_id
            if legacy_dir.exists():
                shutil.rmtree(legacy_dir, ignore_errors=True)
        except OSError:
            pass

    def _table_exists(self, table: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name = ?",
            (table,),
        ).fetchone()
        return row is not None

    def _table_columns(self, table: str) -> set[str]:
        if not self._table_exists(table):
            return set()
        rows = self._conn.execute(f"PRAGMA table_info({table})").fetchall()
        return {str(row["name"]) for row in rows}

    def prune_empty_ghost_sessions(self, sessions_dir: Path | None = None) -> int:
        rows = self._conn.execute(
            """
            SELECT id FROM sessions
             WHERE COALESCE(message_count, 0) = 0
               AND (ended_at IS NOT NULL OR COALESCE(transient, 0) = 1)
            """
        ).fetchall()
        count = 0
        for row in rows:
            if self.delete_session(str(row["id"]), sessions_dir=sessions_dir):
                count += 1
        return count

    def finalize_orphaned_compression_sessions(self) -> int:
        now = time.time()
        with self._lock:
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
            self._conn.commit()
        return int(cursor.rowcount or 0)

    def maybe_auto_prune_and_vacuum(
        self,
        *,
        retention_days: int = 90,
        min_interval_hours: int = 24,
        vacuum: bool = True,
        sessions_dir: Path | None = None,
    ) -> dict[str, Any]:
        now = time.time()
        last_raw = self.get_meta("last_auto_prune")
        try:
            last = float(last_raw or 0)
        except (TypeError, ValueError):
            last = 0.0
        if last and now - last < max(0, int(min_interval_hours or 0)) * 3600:
            return {"skipped": True, "reason": "interval"}
        cutoff = now - max(1, int(retention_days or 90)) * 86400
        rows = self._conn.execute(
            "SELECT id FROM sessions WHERE ended_at IS NOT NULL AND ended_at < ?",
            (cutoff,),
        ).fetchall()
        pruned = 0
        for row in rows:
            if self.delete_session(str(row["id"]), sessions_dir=sessions_dir):
                pruned += 1
        if vacuum:
            try:
                self._conn.execute("VACUUM")
            except sqlite3.Error:
                pass
        self.set_meta("last_auto_prune", str(now))
        return {"skipped": False, "pruned_sessions": pruned}

    def get_meta(self, key: str) -> str | None:
        row = self._conn.execute("SELECT value FROM state_meta WHERE key = ?", (str(key),)).fetchone()
        return str(row["value"]) if row else None

    def set_meta(self, key: str, value: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO state_meta (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (str(key), str(value)),
            )
            self._conn.commit()


def _message_row(row: sqlite3.Row) -> dict[str, Any]:
    item = dict(row)
    item["content"] = _decode_content(item.get("content"))
    for key in ("tool_calls", "reasoning_details", "codex_reasoning_items", "codex_message_items"):
        if item.get(key):
            item[key] = _json_or(item[key], [])
    if item.get("metadata_json"):
        item["metadata"] = _json_or(item["metadata_json"], None)
    return item


def _encode_content(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False)


def _decode_content(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return value


def _json_or_none(value: Any) -> str | None:
    if value in (None, "", [], {}):
        return None
    return json.dumps(value, ensure_ascii=False)


def _json_or(raw: Any, default: Any) -> Any:
    try:
        return json.loads(raw) if raw else default
    except (json.JSONDecodeError, TypeError):
        return default


def _tool_call_count(value: Any) -> int:
    if isinstance(value, list):
        return len(value)
    return 1 if value else 0


def _message_preview_text(content: Any) -> str:
    text = _message_text(content)
    return text[:120]


def _message_display_title_text(content: Any) -> str:
    text = _message_text(content)
    return text[:80]


def _message_text(content: Any) -> str:
    if isinstance(content, str):
        return " ".join(sanitize_context(content).split())
    if isinstance(content, list):
        parts = [
            str(part.get("text") or "")
            for part in content
            if isinstance(part, dict) and part.get("type") == "text"
        ]
        return " ".join(" ".join(parts).split())
    return ""


def _to_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _empty_skill_breakdown() -> dict[str, Any]:
    return {
        "summary": {
            "total_skill_loads": 0,
            "total_skill_edits": 0,
            "total_skill_actions": 0,
            "distinct_skills_used": 0,
        },
        "top_skills": [],
    }


__all__ = ["CliSessionStore", "open_cli_session_store"]
