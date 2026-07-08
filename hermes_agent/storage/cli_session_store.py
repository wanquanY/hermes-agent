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
from hermes_agent.repositories.session_repo import (
    SessionRepoImpl,
    SessionSpec,
    sanitize_session_title,
)
from hermes_agent.storage.session_repository_db import connect_session_repository_db
from hermes_agent.storage.sqlite_connection_lock import lock_for_connection


def open_cli_session_store(db_path: Path | str | None = None) -> "CliSessionStore":
    return CliSessionStore(connect_session_repository_db(db_path))


class CliSessionStore:
    """Method surface currently required by CLI and AIAgent persistence."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self._lock = lock_for_connection(conn)
        self._sessions = SessionRepoImpl(conn)
        self._messages = MessageHistoryReadModel(conn)
        self._recall = SessionRecallReadModel(conn)

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
        return str(session_id or "")

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

    def get_session_by_title(self, title: str) -> dict[str, Any] | None:
        normalized = sanitize_session_title(title)
        if not normalized:
            return None
        row = self._conn.execute("SELECT * FROM sessions WHERE title = ?", (normalized,)).fetchone()
        return dict(row) if row else None

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
        now = time.time()
        preview = _message_preview_text(content)
        display_title = _message_display_title_text(content) if role == "user" else ""
        with self._lock:
            cursor = self._conn.execute(
                """INSERT INTO messages (
                    session_id, role, content, participant_id, tool_call_id,
                    tool_calls, tool_name, timestamp, token_count, finish_reason,
                    reasoning, reasoning_content, reasoning_details, codex_reasoning_items,
                    codex_message_items, platform_message_id, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    stable,
                    str(role or "unknown"),
                    _encode_content(content),
                    str(participant_id or ""),
                    tool_call_id,
                    _json_or_none(tool_calls),
                    tool_name,
                    now,
                    token_count,
                    finish_reason,
                    reasoning,
                    reasoning_content,
                    _json_or_none(reasoning_details),
                    _json_or_none(codex_reasoning_items),
                    _json_or_none(codex_message_items),
                    platform_message_id,
                    _json_or_none(metadata),
                ),
            )
            message_id = int(cursor.lastrowid or 0)
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
                           WHEN ? != '' AND COALESCE(display_title_source, '') = '' THEN 'first_user_message'
                           ELSE COALESCE(display_title_source, '')
                       END,
                       last_active = ?,
                       updated_at = ?
                 WHERE id = ?
                """,
                (
                    _tool_call_count(tool_calls),
                    preview,
                    preview,
                    display_title,
                    display_title,
                    display_title,
                    now,
                    now,
                    stable,
                ),
            )
            self._conn.commit()
        return message_id

    def replace_messages(self, session_id: str, messages: list[dict[str, Any]]) -> None:
        stable = str(session_id or "").strip()
        if not stable:
            raise ValueError("session_id is required")
        now_ts = time.time()
        total_messages = 0
        total_tool_calls = 0
        first_user_preview = ""
        first_user_display_title = ""
        last_message_ts: float | None = None
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self._conn.execute("DELETE FROM messages WHERE session_id = ?", (stable,))
                for msg in messages:
                    role = str(msg.get("role") or "unknown")
                    tool_calls = msg.get("tool_calls")
                    content = msg.get("content")
                    message_ts = now_ts
                    self._conn.execute(
                        """INSERT INTO messages (
                            session_id, role, content, participant_id, tool_call_id,
                            tool_calls, tool_name, timestamp, token_count, finish_reason,
                            reasoning, reasoning_content, reasoning_details,
                            codex_reasoning_items, codex_message_items,
                            platform_message_id, metadata_json
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            stable,
                            role,
                            _encode_content(content),
                            str(msg.get("participant_id") or ""),
                            msg.get("tool_call_id"),
                            _json_or_none(tool_calls),
                            msg.get("tool_name") or msg.get("name"),
                            message_ts,
                            msg.get("token_count"),
                            msg.get("finish_reason"),
                            msg.get("reasoning") if role == "assistant" else None,
                            msg.get("reasoning_content") if role == "assistant" else None,
                            _json_or_none(msg.get("reasoning_details") if role == "assistant" else None),
                            _json_or_none(msg.get("codex_reasoning_items") if role == "assistant" else None),
                            _json_or_none(msg.get("codex_message_items") if role == "assistant" else None),
                            msg.get("platform_message_id") or msg.get("message_id"),
                            _json_or_none(msg.get("metadata")),
                        ),
                    )
                    total_messages += 1
                    total_tool_calls += _tool_call_count(tool_calls)
                    if role == "user" and not first_user_preview:
                        first_user_preview = _message_preview_text(content)
                        first_user_display_title = _message_display_title_text(content)
                    last_message_ts = message_ts
                    now_ts += 1e-6
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
                           updated_at = ?
                     WHERE id = ?
                    """,
                    (
                        total_messages,
                        total_tool_calls,
                        first_user_preview,
                        first_user_display_title,
                        first_user_display_title,
                        last_message_ts,
                        time.time(),
                        stable,
                    ),
                )
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

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
        with self._lock:
            cursor = self._conn.execute("DELETE FROM sessions WHERE id = ?", (stable,))
            self._conn.execute("DELETE FROM session_index WHERE session_id = ?", (stable,))
            self._conn.commit()
        if sessions_dir is not None:
            path = Path(sessions_dir) / stable
            if path.exists():
                shutil.rmtree(path, ignore_errors=True)
        return int(cursor.rowcount or 0) > 0

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
