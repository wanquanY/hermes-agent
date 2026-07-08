"""Session branching storage primitives for Hermes state."""

import hashlib
import json
import re
import sqlite3
import time
from typing import Any, Dict, List, Optional, Tuple

from hermes_agent.repositories.message_repo import MessageRepoImpl
from hermes_agent.repositories.session_repo import (
    BranchLineageSpec,
    BranchRequestSpec,
    MaterializedBranchSessionSpec,
    SessionRepoImpl,
)


class BranchStateMixin:
    @staticmethod
    def _branch_fingerprint(
        source_session_id: str,
        branch_point: Optional[Dict[str, Any]],
        scope: str,
        title: Optional[str],
    ) -> str:
        payload = {
            "source_session_id": source_session_id,
            "branch_point": branch_point or {},
            "scope": scope,
            "title": title or "",
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    def _replayable_lineage_root_to_tip_conn(
        self,
        conn: sqlite3.Connection,
        session_id: str,
    ) -> List[str]:
        """Return the replayable transcript chain for parent-linked sessions.

        User-created branches store their lineage in ``session_lineage`` and
        materialize their initial transcript as normal messages. They must not
        be replayed through ``parent_session_id`` ancestors, or the UI would
        display the copied prefix twice. Existing parent-linked continuations
        and tests still rely on ``include_ancestors=True`` replaying ordinary
        parent chains.
        """
        if not session_id:
            return [session_id]

        chain = [session_id]
        current = session_id
        seen = {session_id}
        for _ in range(100):
            row = conn.execute(
                """
                SELECT
                    child.parent_session_id AS parent_session_id,
                    parent.end_reason AS parent_end_reason,
                    parent.ended_at AS parent_ended_at,
                    child.started_at AS child_started_at,
                    lineage.branch_origin AS branch_origin
                FROM sessions child
                LEFT JOIN sessions parent ON parent.id = child.parent_session_id
                LEFT JOIN session_lineage lineage ON lineage.session_id = child.id
                WHERE child.id = ?
                """,
                (current,),
            ).fetchone()
            if row is None:
                break
            parent_id = str(row["parent_session_id"] or "")
            if not parent_id or parent_id in seen:
                break
            if row["branch_origin"] == "user_message_action":
                break
            parent_ended_at = row["parent_ended_at"]
            child_started_at = row["child_started_at"]
            if (
                row["parent_end_reason"] == "branched"
                and parent_ended_at is not None
                and child_started_at >= parent_ended_at
            ):
                break
            chain.append(parent_id)
            seen.add(parent_id)
            current = parent_id
        return list(reversed(chain))

    def _resolve_branch_message_row_conn(
        self,
        conn: sqlite3.Connection,
        source_session_ids: List[str],
        branch_point: Optional[Dict[str, Any]],
        scope: str,
    ) -> Tuple[int, Dict[str, str]]:
        placeholders = ",".join("?" for _ in source_session_ids)
        normalized_scope = str(scope or "through_turn").strip() or "through_turn"
        point = branch_point if isinstance(branch_point, dict) else {}
        normalized_point = {
            "message_id": str(point.get("message_id") or point.get("messageId") or "").strip(),
            "turn_id": str(point.get("turn_id") or point.get("turnId") or "").strip(),
            "run_id": str(point.get("run_id") or point.get("runId") or "").strip(),
            "client_message_id": str(
                point.get("client_message_id")
                or point.get("clientMessageId")
                or ""
            ).strip(),
        }

        if normalized_point["message_id"]:
            try:
                message_row_id = int(normalized_point["message_id"])
            except (TypeError, ValueError) as exc:
                raise ValueError("branch point not found") from exc
            row = conn.execute(
                f"SELECT id FROM messages WHERE id = ? AND session_id IN ({placeholders})",
                (message_row_id, *source_session_ids),
            ).fetchone()
            if row is None:
                raise ValueError("branch point not found")
            return int(row["id"]), normalized_point

        identity_keys = ("turn_id", "run_id", "client_message_id")
        if any(normalized_point[key] for key in identity_keys):
            rows = conn.execute(
                f"SELECT id, metadata_json FROM messages "
                f"WHERE session_id IN ({placeholders}) AND metadata_json IS NOT NULL "
                "ORDER BY id",
                tuple(source_session_ids),
            ).fetchall()
            matched_ids: List[int] = []
            for row in rows:
                try:
                    metadata = json.loads(row["metadata_json"] or "{}")
                except (json.JSONDecodeError, TypeError):
                    continue
                if not isinstance(metadata, dict):
                    continue
                for key in identity_keys:
                    expected = normalized_point[key]
                    if expected and str(metadata.get(key) or "").strip() == expected:
                        matched_ids.append(int(row["id"]))
                        break
            if not matched_ids:
                raise ValueError("branch point not found")
            return max(matched_ids), normalized_point

        if normalized_scope != "full_conversation":
            raise ValueError("branch point required")

        row = conn.execute(
            f"SELECT MAX(id) AS max_id FROM messages WHERE session_id IN ({placeholders})",
            tuple(source_session_ids),
        ).fetchone()
        max_id = int(row["max_id"] or 0) if row else 0
        if max_id <= 0:
            raise ValueError("source transcript is empty")
        return max_id, normalized_point

    def _branch_lineage_seed_conn(
        self,
        conn: sqlite3.Connection,
        source_session_id: str,
    ) -> Tuple[str, int]:
        row = conn.execute(
            "SELECT root_session_id, branch_depth FROM session_lineage WHERE session_id = ?",
            (source_session_id,),
        ).fetchone()
        if row is None:
            return source_session_id, 1
        return str(row["root_session_id"] or source_session_id), int(row["branch_depth"] or 0) + 1

    def _next_title_in_lineage_conn(
        self,
        conn: sqlite3.Connection,
        base_title: str,
    ) -> str:
        match = re.match(r"^(.*?) #(\d+)$", base_title)
        base = match.group(1) if match else base_title
        escaped = base.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        rows = conn.execute(
            "SELECT title FROM sessions WHERE title = ? OR title LIKE ? ESCAPE '\\'",
            (base, f"{escaped} #%"),
        ).fetchall()
        existing = [row["title"] for row in rows]
        if not existing:
            return base
        max_num = 1
        for title in existing:
            numbered = re.match(r"^.* #(\d+)$", title or "")
            if numbered:
                max_num = max(max_num, int(numbered.group(1)))
        return f"{base} #{max_num + 1}"

    def _branch_session_result_conn(
        self,
        conn: sqlite3.Connection,
        session_id: str,
        *,
        replayed: bool = False,
    ) -> Dict[str, Any]:
        row = conn.execute(
            """
            SELECT s.id, s.title, s.message_count, l.parent_session_id,
                   l.root_session_id, l.branch_from_message_row_id,
                   l.branch_from_turn_id, l.branch_from_run_id,
                   l.branch_from_client_message_id, l.branch_depth,
                   l.branch_origin, l.branch_mode
            FROM sessions s
            LEFT JOIN session_lineage l ON l.session_id = s.id
            WHERE s.id = ?
            """,
            (session_id,),
        ).fetchone()
        if row is None:
            raise ValueError("branch result session not found")
        return {
            "conversation_session_id": row["id"],
            "parent_session_id": row["parent_session_id"] or "",
            "root_session_id": row["root_session_id"] or row["id"],
            "title": row["title"] or "",
            "message_count": int(row["message_count"] or 0),
            "branch_depth": int(row["branch_depth"] or 0),
            "branch_origin": row["branch_origin"] or "",
            "branch_mode": row["branch_mode"] or "",
            "branch_point": {
                "included_message_row_id": int(row["branch_from_message_row_id"] or 0),
                "turn_id": row["branch_from_turn_id"] or "",
                "run_id": row["branch_from_run_id"] or "",
                "client_message_id": row["branch_from_client_message_id"] or "",
            },
            "replayed": replayed,
        }

    def get_session_branch_info(self, session_id: str) -> Optional[Dict[str, Any]]:
        session_id = str(session_id or "").strip()
        if not session_id:
            return None
        with self._lock:
            row = self._conn.execute(
                """
                SELECT session_id, parent_session_id, root_session_id,
                       branch_from_message_row_id, branch_from_turn_id,
                       branch_from_run_id, branch_from_client_message_id,
                       branch_origin, branch_mode, branch_depth, created_at
                FROM session_lineage
                WHERE session_id = ?
                """,
                (session_id,),
            ).fetchone()
        if row is None:
            return None
        return {
            "session_id": row["session_id"],
            "parent_session_id": row["parent_session_id"] or "",
            "root_session_id": row["root_session_id"] or row["session_id"],
            "branch_from_message_row_id": int(row["branch_from_message_row_id"] or 0),
            "branch_from_turn_id": row["branch_from_turn_id"] or "",
            "branch_from_run_id": row["branch_from_run_id"] or "",
            "branch_from_client_message_id": row["branch_from_client_message_id"] or "",
            "branch_origin": row["branch_origin"] or "",
            "branch_mode": row["branch_mode"] or "",
            "branch_depth": int(row["branch_depth"] or 0),
            "created_at": row["created_at"] or 0,
        }

    def branch_session(
        self,
        *,
        source_session_id: str,
        new_session_id: str,
        branch_point: Optional[Dict[str, Any]] = None,
        scope: str = "through_turn",
        title: Optional[str] = None,
        idempotency_key: Optional[str] = None,
        branch_origin: str = "user_message_action",
    ) -> Dict[str, Any]:
        """Create a non-destructive user branch as a normal stored session.

        The new session's initial transcript is materialized into the standard
        ``messages`` table so all existing readers keep using the same storage
        path. User branch ancestry is recorded in ``session_lineage`` instead
        of ``sessions.parent_session_id`` because the latter is used by
        compression transcript replay.
        """
        source_session_id = str(source_session_id or "").strip()
        new_session_id = str(new_session_id or "").strip()
        if not source_session_id:
            raise ValueError("source session required")
        if not new_session_id:
            raise ValueError("new session required")
        normalized_scope = str(scope or "through_turn").strip() or "through_turn"
        requested_title = self.sanitize_title(title) if title else None
        fingerprint = self._branch_fingerprint(
            source_session_id,
            branch_point,
            normalized_scope,
            requested_title,
        )
        branch_origin = str(branch_origin or "user_message_action").strip() or "user_message_action"

        def _do(conn):
            source = conn.execute(
                "SELECT * FROM sessions WHERE id = ?",
                (source_session_id,),
            ).fetchone()
            if source is None:
                raise ValueError("source session not found")

            if idempotency_key:
                existing_request = conn.execute(
                    "SELECT branch_fingerprint, result_session_id FROM session_branch_requests "
                    "WHERE idempotency_key = ?",
                    (idempotency_key,),
                ).fetchone()
                if existing_request is not None:
                    if existing_request["branch_fingerprint"] != fingerprint:
                        raise ValueError("idempotency key conflicts with different branch point")
                    return self._branch_session_result_conn(
                        conn,
                        str(existing_request["result_session_id"]),
                        replayed=True,
                    )

            source_session_ids = self._replayable_lineage_root_to_tip_conn(
                conn,
                source_session_id,
            )
            included_row_id, normalized_point = self._resolve_branch_message_row_conn(
                conn,
                source_session_ids,
                branch_point,
                normalized_scope,
            )
            placeholders = ",".join("?" for _ in source_session_ids)
            source_params = tuple(source_session_ids) + (included_row_id,)
            count_row = conn.execute(
                f"""
                SELECT
                    COUNT(*) AS message_count,
                    COALESCE(SUM(
                        CASE
                            WHEN tool_calls IS NULL OR tool_calls = '' THEN 0
                            WHEN json_valid(tool_calls) AND json_type(tool_calls) = 'array'
                                THEN json_array_length(tool_calls)
                            ELSE 1
                        END
                    ), 0) AS tool_call_count
                FROM messages
                WHERE session_id IN ({placeholders}) AND id <= ?
                """,
                source_params,
            ).fetchone()
            message_count = int(count_row["message_count"] or 0)
            if message_count <= 0:
                raise ValueError("source transcript is empty")
            tool_call_count = int(count_row["tool_call_count"] or 0)

            created_at = time.time()
            base_title = requested_title or source["title"] or "branch"
            branch_title = requested_title or self._next_title_in_lineage_conn(
                conn,
                str(base_title),
            )
            session_repo = SessionRepoImpl(conn)
            session_repo.create_materialized_branch_session(
                MaterializedBranchSessionSpec(
                    new_session_id=new_session_id,
                    title=branch_title,
                    created_at=created_at,
                    message_count=message_count,
                    tool_call_count=tool_call_count,
                    source_row=source,
                )
            )

            root_session_id, branch_depth = self._branch_lineage_seed_conn(
                conn,
                source_session_id,
            )
            session_repo.record_branch_lineage(
                BranchLineageSpec(
                    session_id=new_session_id,
                    parent_session_id=source_session_id,
                    root_session_id=root_session_id,
                    branch_from_message_row_id=included_row_id,
                    branch_from_turn_id=str(normalized_point.get("turn_id") or ""),
                    branch_from_run_id=str(normalized_point.get("run_id") or ""),
                    branch_from_client_message_id=str(
                        normalized_point.get("client_message_id") or ""
                    ),
                    branch_origin=branch_origin,
                    branch_mode="materialized_prefix",
                    branch_depth=branch_depth,
                    created_at=created_at,
                )
            )

            copy_started_at = created_at
            MessageRepoImpl(conn).copy_branch_prefix(
                list(source_session_ids),
                included_row_id,
                new_session_id,
                copy_started_at,
            )

            if idempotency_key:
                session_repo.record_branch_request(
                    BranchRequestSpec(
                        idempotency_key=idempotency_key,
                        source_session_id=source_session_id,
                        branch_fingerprint=fingerprint,
                        result_session_id=new_session_id,
                        created_at=created_at,
                    )
                )
            return self._branch_session_result_conn(conn, new_session_id)

        return self._execute_write(_do)
