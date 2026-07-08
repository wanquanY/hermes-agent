from __future__ import annotations

import json
import re
import sqlite3
import time
import warnings
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from hermes_agent.domain.seq_allocator import ensure_session_counter
from hermes_agent.domain.session_deletion import SessionDeletionService
from hermes_agent.domain.session_index_reconciler import SessionIndexReconciler
from hermes_agent.read_models.session_index import SessionIndexQuery, SessionIndexReadModel
from hermes_agent.read_models.session_list import SessionListQuery, SessionListReadModel
from hermes_agent.read_models.session_recall import SessionRecallReadModel
from hermes_agent.repositories.message_repo import MessageRepoImpl
from hermes_agent.repositories.session_repo import SessionRepoImpl


class SessionStateFacadeMixin:
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

        if len(cleaned) > SessionStateFacadeMixin.MAX_TITLE_LENGTH:
            raise ValueError(
                f"Title too long ({len(cleaned)} chars, max {SessionStateFacadeMixin.MAX_TITLE_LENGTH})"
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
