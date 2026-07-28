"""SQLite repository for canonical conversation memory and context snapshots."""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any

from hermes_agent.domain.conversation_memory import (
    MemoryAccessContext,
    normalize_visibility,
    validate_memory_identity,
    visibility_allows,
)


def _text(value: Any) -> str:
    return str(value or "").strip()


def _json(value: Any, fallback: Any) -> str:
    candidate = fallback if value is None else value
    return json.dumps(candidate, ensure_ascii=False, sort_keys=True)


def _load_json(value: Any, fallback: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    raw = _text(value)
    if not raw:
        return fallback
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return fallback
    return parsed


def _row_dict(row: sqlite3.Row | None) -> dict[str, Any]:
    if row is None:
        return {}
    item = dict(row)
    for source, target, fallback in (
        ("structured_payload_json", "structured_payload", {}),
        ("visibility_json", "visibility", {}),
        ("provenance_json", "provenance", {}),
        ("supersedes_json", "supersedes", []),
        ("selected_event_ids_json", "selected_event_ids", []),
        ("selected_memory_ids_json", "selected_memory_ids", []),
        ("metadata_json", "metadata", {}),
        ("team_snapshot_json", "team_snapshot", {}),
        ("workspace_snapshot_json", "workspace_snapshot", {}),
        ("summary_json", "summary", {}),
        ("source_event_ids_json", "source_event_ids", []),
        ("source_memory_ids_json", "source_memory_ids", []),
    ):
        if source in item:
            item[target] = _load_json(item.get(source), fallback)
    return item


class ConversationMemoryRepo:
    def __init__(
        self,
        conn: sqlite3.Connection,
        execute_write: Callable[[Any], Any],
    ) -> None:
        self._conn = conn
        self._execute_write = execute_write

    def current_conversation_revision(self, conversation_session_id: str) -> int:
        conversation = _text(conversation_session_id)
        if not conversation:
            return 0
        event_row = self._conn.execute(
            "SELECT COALESCE(MAX(seq), 0) FROM run_events WHERE session_id = ?",
            (conversation,),
        ).fetchone()
        message_row = self._conn.execute(
            "SELECT COALESCE(MAX(id), 0) FROM messages WHERE session_id = ?",
            (conversation,),
        ).fetchone()
        event_revision = int(event_row[0] if event_row else 0)
        message_revision = int(message_row[0] if message_row else 0)
        return max(event_revision, message_revision)

    def create_item(
        self,
        *,
        conversation_session_id: str,
        owner_kind: str,
        owner_id: str,
        kind: str,
        content: str,
        visibility: Mapping[str, Any],
        structured_payload: Mapping[str, Any] | None = None,
        provenance: Mapping[str, Any] | None = None,
        participant_id: str = "",
        activity_id: str = "",
        node_id: str = "",
        confidence: float = 0.5,
        status: str = "proposed",
        supersedes: Sequence[str] | None = None,
        memory_id: str = "",
        valid_from: float | None = None,
        valid_until: float | None = None,
    ) -> dict[str, Any]:
        conversation = _text(conversation_session_id)
        normalized_owner_kind = _text(owner_kind)
        normalized_owner_id = _text(owner_id)
        normalized_kind = _text(kind)
        normalized_status = _text(status) or "proposed"
        validate_memory_identity(
            owner_kind=normalized_owner_kind,
            owner_id=normalized_owner_id,
            kind=normalized_kind,
            status=normalized_status,
        )
        if not conversation:
            raise ValueError("conversation_session_id required")
        normalized_content = str(content or "").strip()
        if not normalized_content:
            raise ValueError("memory content required")
        normalized_visibility = normalize_visibility(visibility)
        normalized_confidence = float(confidence)
        if not 0 <= normalized_confidence <= 1:
            raise ValueError("memory confidence must be between 0 and 1")
        identifier = _text(memory_id) or f"memory:{uuid.uuid4().hex}"
        now = time.time()

        def _do(conn: sqlite3.Connection) -> dict[str, Any]:
            if conn.execute("SELECT 1 FROM sessions WHERE id = ?", (conversation,)).fetchone() is None:
                raise ValueError("conversation memory requires an existing session")
            existing = conn.execute(
                "SELECT * FROM conversation_memory_items WHERE memory_id = ?",
                (identifier,),
            ).fetchone()
            if existing is not None:
                item = _row_dict(existing)
                immutable_identity = (
                    item.get("conversation_session_id"),
                    item.get("owner_kind"),
                    item.get("owner_id"),
                    item.get("kind"),
                    item.get("content"),
                )
                requested_identity = (
                    conversation,
                    normalized_owner_kind,
                    normalized_owner_id,
                    normalized_kind,
                    normalized_content,
                )
                if immutable_identity != requested_identity:
                    raise RuntimeError("conversation memory idempotency conflict")
                return item
            conn.execute(
                """
                INSERT INTO conversation_memory_items (
                    memory_id, conversation_session_id, owner_kind, owner_id,
                    participant_id, activity_id, node_id, kind, content,
                    structured_payload_json, visibility_json, provenance_json,
                    supersedes_json, confidence, status, revision, valid_from,
                    valid_until, invalidated_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, NULL, ?, ?)
                """,
                (
                    identifier,
                    conversation,
                    normalized_owner_kind,
                    normalized_owner_id,
                    _text(participant_id),
                    _text(activity_id),
                    _text(node_id),
                    normalized_kind,
                    normalized_content,
                    _json(structured_payload, {}),
                    _json(normalized_visibility, {}),
                    _json(provenance, {}),
                    _json(list(supersedes or []), []),
                    normalized_confidence,
                    normalized_status,
                    float(valid_from if valid_from is not None else now),
                    float(valid_until) if valid_until is not None else None,
                    now,
                    now,
                ),
            )
            row = conn.execute(
                "SELECT * FROM conversation_memory_items WHERE memory_id = ?",
                (identifier,),
            ).fetchone()
            return _row_dict(row)

        return self._execute_write(_do)

    def get_item(self, memory_id: str) -> dict[str, Any]:
        row = self._conn.execute(
            "SELECT * FROM conversation_memory_items WHERE memory_id = ?",
            (_text(memory_id),),
        ).fetchone()
        return _row_dict(row)

    def list_items(
        self,
        *,
        conversation_session_id: str = "",
        activity_id: str = "",
        owner_kind: str = "",
        owner_id: str = "",
        kinds: Sequence[str] = (),
        statuses: Sequence[str] = (),
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        for column, value in (
            ("conversation_session_id", conversation_session_id),
            ("activity_id", activity_id),
            ("owner_kind", owner_kind),
            ("owner_id", owner_id),
        ):
            if _text(value):
                clauses.append(f"{column} = ?")
                params.append(_text(value))
        normalized_kinds = [_text(value) for value in kinds if _text(value)]
        if normalized_kinds:
            clauses.append(f"kind IN ({','.join('?' for _ in normalized_kinds)})")
            params.extend(normalized_kinds)
        normalized_statuses = [_text(value) for value in statuses if _text(value)]
        if normalized_statuses:
            clauses.append(f"status IN ({','.join('?' for _ in normalized_statuses)})")
            params.extend(normalized_statuses)
        rows = self._conn.execute(
            f"""
            SELECT * FROM conversation_memory_items
             WHERE {' AND '.join(clauses) if clauses else '1 = 1'}
             ORDER BY updated_at DESC, memory_id ASC LIMIT ?
            """,
            (*params, max(1, min(int(limit), 1000))),
        ).fetchall()
        return [_row_dict(row) for row in rows]

    def update_item(
        self,
        memory_id: str,
        *,
        expected_revision: int,
        content: str | None = None,
        structured_payload: Mapping[str, Any] | None = None,
        visibility: Mapping[str, Any] | None = None,
        confidence: float | None = None,
        status: str | None = None,
    ) -> dict[str, Any]:
        identifier = _text(memory_id)
        current = self.get_item(identifier)
        if not current:
            return {}
        next_status = _text(status) or _text(current.get("status"))
        if next_status not in {"proposed", "committed", "superseded", "invalidated"}:
            raise ValueError("invalid conversation memory status")
        next_content = str(content if content is not None else current.get("content") or "").strip()
        if not next_content:
            raise ValueError("memory content required")
        next_confidence = float(
            confidence if confidence is not None else current.get("confidence") or 0
        )
        if not 0 <= next_confidence <= 1:
            raise ValueError("memory confidence must be between 0 and 1")
        next_visibility = normalize_visibility(
            visibility if visibility is not None else current.get("visibility") or {}
        )
        next_payload = (
            structured_payload
            if structured_payload is not None
            else current.get("structured_payload") or {}
        )
        now = time.time()

        def _do(conn: sqlite3.Connection) -> dict[str, Any]:
            cursor = conn.execute(
                """
                UPDATE conversation_memory_items
                   SET content = ?, structured_payload_json = ?, visibility_json = ?,
                       confidence = ?, status = ?, revision = revision + 1,
                       invalidated_at = CASE WHEN ? = 'invalidated' THEN ? ELSE invalidated_at END,
                       updated_at = ?
                 WHERE memory_id = ? AND revision = ?
                """,
                (
                    next_content,
                    _json(next_payload, {}),
                    _json(next_visibility, {}),
                    next_confidence,
                    next_status,
                    next_status,
                    now,
                    now,
                    identifier,
                    int(expected_revision),
                ),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("conversation memory revision conflict")
            return _row_dict(
                conn.execute(
                    "SELECT * FROM conversation_memory_items WHERE memory_id = ?",
                    (identifier,),
                ).fetchone()
            )

        return self._execute_write(_do)

    def upsert_edge(
        self,
        *,
        from_memory_id: str,
        relation: str,
        to_memory_id: str = "",
        metadata: Mapping[str, Any] | None = None,
        edge_id: str = "",
    ) -> dict[str, Any]:
        source = _text(from_memory_id)
        normalized_relation = _text(relation)
        if not source or not normalized_relation:
            raise ValueError("edge source and relation required")
        identifier = _text(edge_id) or f"memory-edge:{uuid.uuid4().hex}"
        now = time.time()

        def _do(conn: sqlite3.Connection) -> dict[str, Any]:
            conn.execute(
                """
                INSERT INTO conversation_memory_edges (
                    edge_id, from_memory_id, to_memory_id, relation, metadata_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(edge_id) DO UPDATE SET
                    from_memory_id = excluded.from_memory_id,
                    to_memory_id = excluded.to_memory_id,
                    relation = excluded.relation,
                    metadata_json = excluded.metadata_json
                """,
                (identifier, source, _text(to_memory_id) or None, normalized_relation, _json(metadata, {}), now),
            )
            return _row_dict(
                conn.execute(
                    "SELECT * FROM conversation_memory_edges WHERE edge_id = ?",
                    (identifier,),
                ).fetchone()
            )

        return self._execute_write(_do)

    def list_edges(
        self,
        *,
        from_memory_id: str = "",
        to_memory_id: str = "",
        relation: str = "",
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        for column, value in (
            ("from_memory_id", from_memory_id),
            ("to_memory_id", to_memory_id),
            ("relation", relation),
        ):
            if _text(value):
                clauses.append(f"{column} = ?")
                params.append(_text(value))
        rows = self._conn.execute(
            f"""
            SELECT * FROM conversation_memory_edges
             WHERE {' AND '.join(clauses) if clauses else '1 = 1'}
             ORDER BY created_at DESC, edge_id ASC LIMIT ?
            """,
            (*params, max(1, min(int(limit), 1000))),
        ).fetchall()
        return [_row_dict(row) for row in rows]

    def transition_item(
        self,
        memory_id: str,
        *,
        expected_revision: int,
        status: str,
    ) -> dict[str, Any]:
        normalized_status = _text(status)
        if normalized_status not in {"committed", "superseded", "invalidated"}:
            raise ValueError("invalid memory transition status")
        identifier = _text(memory_id)
        now = time.time()

        def _do(conn: sqlite3.Connection) -> dict[str, Any]:
            cursor = conn.execute(
                """
                UPDATE conversation_memory_items
                   SET status = ?, revision = revision + 1,
                       invalidated_at = CASE WHEN ? = 'invalidated' THEN ? ELSE invalidated_at END,
                       updated_at = ?
                 WHERE memory_id = ? AND revision = ?
                """,
                (
                    normalized_status,
                    normalized_status,
                    now,
                    now,
                    identifier,
                    int(expected_revision),
                ),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("conversation memory revision conflict")
            return _row_dict(
                conn.execute(
                    "SELECT * FROM conversation_memory_items WHERE memory_id = ?",
                    (identifier,),
                ).fetchone()
            )

        return self._execute_write(_do)

    def list_visible(
        self,
        context: MemoryAccessContext,
        *,
        statuses: Sequence[str] = ("committed",),
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        normalized_statuses = [_text(item) for item in statuses if _text(item)]
        if not normalized_statuses:
            return []
        placeholders = ",".join("?" for _ in normalized_statuses)
        rows = self._conn.execute(
            f"""
            SELECT * FROM conversation_memory_items
             WHERE conversation_session_id = ?
               AND status IN ({placeholders})
             ORDER BY updated_at DESC, memory_id ASC
             LIMIT ?
            """,
            (
                context.conversation_session_id,
                *normalized_statuses,
                max(1, min(int(limit), 1000)),
            ),
        ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            item = _row_dict(row)
            owner_kind = _text(item.get("owner_kind"))
            owner_id = _text(item.get("owner_id"))
            if owner_kind == "profile" and owner_id != context.profile_id:
                continue
            if owner_kind == "participant" and owner_id != context.actor_participant_id:
                continue
            if owner_kind == "activity" and owner_id != context.activity_id:
                continue
            if owner_kind == "node" and owner_id != context.node_id:
                continue
            if visibility_allows(item.get("visibility") or {}, context):
                result.append(item)
        return result

    def create_snapshot(
        self,
        *,
        conversation_session_id: str,
        actor_participant_id: str,
        execution_scope_key: str,
        activity_id: str = "",
        activity_kind: str = "chat",
        node_id: str = "",
        attempt_id: str = "",
        conversation_revision: int = 0,
        participant_memory_revision: int = 0,
        activity_context_revision: int = 0,
        transcript_cursor: int = 0,
        selected_event_ids: Sequence[str] = (),
        selected_memory_ids: Sequence[str] = (),
        metadata: Mapping[str, Any] | None = None,
        snapshot_id: str = "",
    ) -> dict[str, Any]:
        identifier = _text(snapshot_id) or f"context-snapshot:{uuid.uuid4().hex}"
        conversation = _text(conversation_session_id)
        participant = _text(actor_participant_id)
        scope = _text(execution_scope_key)
        if not conversation or not participant or not scope:
            raise ValueError("conversation, actor participant, and execution scope required")

        def _do(conn: sqlite3.Connection) -> dict[str, Any]:
            conn.execute(
                """
                INSERT INTO actor_context_snapshots (
                    snapshot_id, conversation_session_id, actor_participant_id,
                    execution_scope_key, activity_id, activity_kind, node_id,
                    attempt_id, conversation_revision, participant_memory_revision,
                    activity_context_revision, transcript_cursor,
                    selected_event_ids_json, selected_memory_ids_json,
                    metadata_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    identifier,
                    conversation,
                    participant,
                    scope,
                    _text(activity_id),
                    _text(activity_kind) or "chat",
                    _text(node_id),
                    _text(attempt_id),
                    max(0, int(conversation_revision)),
                    max(0, int(participant_memory_revision)),
                    max(0, int(activity_context_revision)),
                    max(0, int(transcript_cursor)),
                    _json(list(selected_event_ids), []),
                    _json(list(selected_memory_ids), []),
                    _json(metadata, {}),
                    time.time(),
                ),
            )
            return _row_dict(
                conn.execute(
                    "SELECT * FROM actor_context_snapshots WHERE snapshot_id = ?",
                    (identifier,),
                ).fetchone()
            )

        return self._execute_write(_do)

    def latest_actor_summary(
        self,
        conversation_session_id: str,
        actor_participant_id: str,
    ) -> dict[str, Any]:
        row = self._conn.execute(
            """
            SELECT * FROM actor_context_summaries
             WHERE conversation_session_id = ? AND actor_participant_id = ?
               AND status = 'active'
             ORDER BY revision DESC LIMIT 1
            """,
            (_text(conversation_session_id), _text(actor_participant_id)),
        ).fetchone()
        return _row_dict(row)

    def actor_compaction_state(
        self,
        conversation_session_id: str,
        actor_participant_id: str,
    ) -> dict[str, int]:
        row = self._conn.execute(
            """
            SELECT transcript_cursor, memory_revision
              FROM conversation_participants
             WHERE conversation_session_id = ? AND participant_id = ?
               AND status = 'active'
            """,
            (_text(conversation_session_id), _text(actor_participant_id)),
        ).fetchone()
        return {
            "transcript_cursor": int(row["transcript_cursor"] if row else 0),
            "memory_revision": int(row["memory_revision"] if row else 0),
        }

    def create_activity_snapshot(
        self,
        *,
        conversation_session_id: str,
        activity_id: str,
        objective: str,
        conversation_revision: int,
        selected_event_ids: Sequence[str] = (),
        selected_memory_ids: Sequence[str] = (),
        team_snapshot: Mapping[str, Any] | None = None,
        workspace_snapshot: Mapping[str, Any] | None = None,
        expected_revision: int = 0,
    ) -> dict[str, Any]:
        conversation = _text(conversation_session_id)
        activity = _text(activity_id)
        normalized_objective = str(objective or "").strip()
        if not conversation or not activity or not normalized_objective:
            raise ValueError("conversation, activity, and objective required")
        now = time.time()

        def _do(conn: sqlite3.Connection) -> dict[str, Any]:
            previous = conn.execute(
                """
                SELECT snapshot_id, activity_context_revision
                  FROM activity_context_snapshots
                 WHERE activity_id = ? AND status = 'active'
                 ORDER BY activity_context_revision DESC LIMIT 1
                """,
                (activity,),
            ).fetchone()
            current_revision = int(previous["activity_context_revision"] if previous else 0)
            if current_revision != int(expected_revision):
                raise RuntimeError("activity context snapshot revision conflict")
            previous_id = _text(previous["snapshot_id"] if previous else "")
            if previous_id:
                conn.execute(
                    "UPDATE activity_context_snapshots SET status = 'superseded' WHERE snapshot_id = ?",
                    (previous_id,),
                )
            snapshot_id = f"activity-snapshot:{uuid.uuid4().hex}"
            next_revision = current_revision + 1
            conn.execute(
                """
                INSERT INTO activity_context_snapshots (
                    snapshot_id, conversation_session_id, activity_id,
                    activity_context_revision, objective, conversation_revision,
                    selected_event_ids_json, selected_memory_ids_json,
                    team_snapshot_json, workspace_snapshot_json, status,
                    supersedes_snapshot_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?)
                """,
                (
                    snapshot_id,
                    conversation,
                    activity,
                    next_revision,
                    normalized_objective,
                    max(0, int(conversation_revision)),
                    _json(list(selected_event_ids), []),
                    _json(list(selected_memory_ids), []),
                    _json(team_snapshot, {}),
                    _json(workspace_snapshot, {}),
                    previous_id,
                    now,
                ),
            )
            return _row_dict(
                conn.execute(
                    "SELECT * FROM activity_context_snapshots WHERE snapshot_id = ?",
                    (snapshot_id,),
                ).fetchone()
            )

        return self._execute_write(_do)

    def latest_activity_snapshot(self, activity_id: str) -> dict[str, Any]:
        row = self._conn.execute(
            """
            SELECT * FROM activity_context_snapshots
             WHERE activity_id = ? AND status = 'active'
             ORDER BY activity_context_revision DESC LIMIT 1
            """,
            (_text(activity_id),),
        ).fetchone()
        return _row_dict(row)

    def latest_activity_summary(self, activity_id: str) -> dict[str, Any]:
        row = self._conn.execute(
            """
            SELECT * FROM activity_context_summaries
             WHERE activity_id = ? AND status = 'active'
             ORDER BY revision DESC LIMIT 1
            """,
            (_text(activity_id),),
        ).fetchone()
        return _row_dict(row)

    def latest_activity_summary_revision(self, activity_id: str) -> int:
        row = self.latest_activity_summary(activity_id)
        return int(row.get("revision") or 0)

    def latest_node_attempt_summary(
        self,
        activity_id: str,
        node_id: str,
        attempt_id: str,
    ) -> dict[str, Any]:
        row = self._conn.execute(
            """
            SELECT * FROM node_attempt_summaries
             WHERE activity_id = ? AND node_id = ? AND attempt_id = ?
               AND status = 'active'
             ORDER BY revision DESC LIMIT 1
            """,
            (_text(activity_id), _text(node_id), _text(attempt_id)),
        ).fetchone()
        return _row_dict(row)

    def latest_node_summary_revision(
        self,
        activity_id: str,
        node_id: str,
        attempt_id: str,
    ) -> int:
        row = self.latest_node_attempt_summary(activity_id, node_id, attempt_id)
        return int(row.get("revision") or 0)

    def commit_activity_summary(
        self,
        *,
        conversation_session_id: str,
        activity_id: str,
        snapshot_id: str,
        expected_revision: int,
        activity_context_revision: int,
        summary: Mapping[str, Any],
        source_event_ids: Sequence[str],
        source_memory_ids: Sequence[str] = (),
    ) -> dict[str, Any]:
        conversation = _text(conversation_session_id)
        activity = _text(activity_id)
        now = time.time()

        def _do(conn: sqlite3.Connection) -> dict[str, Any]:
            previous = conn.execute(
                """
                SELECT summary_id, revision FROM activity_context_summaries
                 WHERE activity_id = ? AND status = 'active'
                 ORDER BY revision DESC LIMIT 1
                """,
                (activity,),
            ).fetchone()
            current_revision = int(previous["revision"] if previous else 0)
            if current_revision != int(expected_revision):
                raise RuntimeError("activity context summary revision conflict")
            previous_id = _text(previous["summary_id"] if previous else "")
            if previous_id:
                conn.execute(
                    """
                    UPDATE activity_context_summaries
                       SET status = 'superseded', updated_at = ?
                     WHERE summary_id = ? AND status = 'active'
                    """,
                    (now, previous_id),
                )
            summary_id = f"activity-summary:{uuid.uuid4().hex}"
            conn.execute(
                """
                INSERT INTO activity_context_summaries (
                    summary_id, conversation_session_id, activity_id,
                    snapshot_id, activity_context_revision, summary_json,
                    source_event_ids_json, source_memory_ids_json, revision,
                    status, supersedes_summary_id, invalidated_reason,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, '', ?, ?)
                """,
                (
                    summary_id,
                    conversation,
                    activity,
                    _text(snapshot_id),
                    max(0, int(activity_context_revision)),
                    _json(summary, {}),
                    _json(list(source_event_ids), []),
                    _json(list(source_memory_ids), []),
                    current_revision + 1,
                    previous_id,
                    now,
                    now,
                ),
            )
            return _row_dict(
                conn.execute(
                    "SELECT * FROM activity_context_summaries WHERE summary_id = ?",
                    (summary_id,),
                ).fetchone()
            )

        return self._execute_write(_do)

    def commit_node_attempt_summary(
        self,
        *,
        conversation_session_id: str,
        activity_id: str,
        node_id: str,
        attempt_id: str,
        snapshot_id: str,
        expected_revision: int,
        node_attempt_revision: int,
        summary: Mapping[str, Any],
        source_event_ids: Sequence[str],
    ) -> dict[str, Any]:
        conversation = _text(conversation_session_id)
        activity = _text(activity_id)
        node = _text(node_id)
        attempt = _text(attempt_id)
        now = time.time()

        def _do(conn: sqlite3.Connection) -> dict[str, Any]:
            previous = conn.execute(
                """
                SELECT summary_id, revision FROM node_attempt_summaries
                 WHERE activity_id = ? AND node_id = ? AND attempt_id = ?
                   AND status = 'active'
                 ORDER BY revision DESC LIMIT 1
                """,
                (activity, node, attempt),
            ).fetchone()
            current_revision = int(previous["revision"] if previous else 0)
            if current_revision != int(expected_revision):
                raise RuntimeError("node attempt summary revision conflict")
            previous_id = _text(previous["summary_id"] if previous else "")
            if previous_id:
                conn.execute(
                    """
                    UPDATE node_attempt_summaries
                       SET status = 'superseded', updated_at = ?
                     WHERE summary_id = ? AND status = 'active'
                    """,
                    (now, previous_id),
                )
            summary_id = f"node-summary:{uuid.uuid4().hex}"
            conn.execute(
                """
                INSERT INTO node_attempt_summaries (
                    summary_id, conversation_session_id, activity_id, node_id,
                    attempt_id, snapshot_id, node_attempt_revision,
                    summary_json, source_event_ids_json, revision, status,
                    supersedes_summary_id, invalidated_reason, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, '', ?, ?)
                """,
                (
                    summary_id,
                    conversation,
                    activity,
                    node,
                    attempt,
                    _text(snapshot_id),
                    max(0, int(node_attempt_revision)),
                    _json(summary, {}),
                    _json(list(source_event_ids), []),
                    current_revision + 1,
                    previous_id,
                    now,
                    now,
                ),
            )
            return _row_dict(
                conn.execute(
                    "SELECT * FROM node_attempt_summaries WHERE summary_id = ?",
                    (summary_id,),
                ).fetchone()
            )

        return self._execute_write(_do)

    def try_acquire_compaction_lease(
        self,
        scope_key: str,
        *,
        ttl_seconds: float = 120.0,
    ) -> str:
        scope = _text(scope_key)
        if not scope:
            return ""
        holder = uuid.uuid4().hex
        now = time.time()

        def _do(conn: sqlite3.Connection) -> str:
            conn.execute(
                "DELETE FROM context_compaction_leases WHERE expires_at <= ?",
                (now,),
            )
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO context_compaction_leases (
                    scope_key, holder, expires_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (scope, holder, now + max(1.0, float(ttl_seconds)), now, now),
            )
            return holder if cursor.rowcount == 1 else ""

        return self._execute_write(_do)

    def release_compaction_lease(self, scope_key: str, holder: str) -> bool:
        scope = _text(scope_key)
        owner = _text(holder)
        if not scope or not owner:
            return False

        def _do(conn: sqlite3.Connection) -> bool:
            cursor = conn.execute(
                "DELETE FROM context_compaction_leases WHERE scope_key = ? AND holder = ?",
                (scope, owner),
            )
            return cursor.rowcount == 1

        return bool(self._execute_write(_do))

    def commit_actor_summary(
        self,
        *,
        conversation_session_id: str,
        actor_participant_id: str,
        snapshot_id: str,
        expected_memory_revision: int,
        from_seq: int,
        to_seq: int,
        conversation_revision: int,
        summary: Mapping[str, Any],
        source_event_ids: Sequence[str],
    ) -> dict[str, Any]:
        conversation = _text(conversation_session_id)
        participant = _text(actor_participant_id)
        now = time.time()

        def _do(conn: sqlite3.Connection) -> dict[str, Any]:
            participant_row = conn.execute(
                """
                SELECT memory_revision FROM conversation_participants
                 WHERE conversation_session_id = ? AND participant_id = ?
                   AND status = 'active'
                """,
                (conversation, participant),
            ).fetchone()
            current_memory_revision = int(participant_row[0]) if participant_row else -1
            if current_memory_revision != int(expected_memory_revision):
                raise RuntimeError("participant actor summary revision conflict")
            previous = conn.execute(
                """
                SELECT summary_id, revision FROM actor_context_summaries
                 WHERE conversation_session_id = ? AND actor_participant_id = ?
                   AND status = 'active'
                 ORDER BY revision DESC LIMIT 1
                """,
                (conversation, participant),
            ).fetchone()
            previous_id = _text(previous["summary_id"] if previous else "")
            next_revision = int(previous["revision"] if previous else 0) + 1
            if previous_id:
                conn.execute(
                    """
                    UPDATE actor_context_summaries
                       SET status = 'superseded', updated_at = ?
                     WHERE summary_id = ? AND status = 'active'
                    """,
                    (now, previous_id),
                )
            summary_id = f"actor-summary:{uuid.uuid4().hex}"
            conn.execute(
                """
                INSERT INTO actor_context_summaries (
                    summary_id, conversation_session_id, actor_participant_id,
                    snapshot_id, from_seq, to_seq, conversation_revision,
                    participant_memory_revision, summary_json,
                    source_event_ids_json, revision, status,
                    supersedes_summary_id, invalidated_reason, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, '', ?, ?)
                """,
                (
                    summary_id,
                    conversation,
                    participant,
                    _text(snapshot_id),
                    max(0, int(from_seq)),
                    max(int(from_seq), int(to_seq)),
                    max(0, int(conversation_revision)),
                    current_memory_revision + 1,
                    _json(summary, {}),
                    _json(list(source_event_ids), []),
                    next_revision,
                    previous_id,
                    now,
                    now,
                ),
            )
            cursor = conn.execute(
                """
                UPDATE conversation_participants
                   SET transcript_cursor = MAX(transcript_cursor, ?),
                       memory_revision = memory_revision + 1,
                       updated_at = ?
                 WHERE conversation_session_id = ? AND participant_id = ?
                   AND memory_revision = ? AND status = 'active'
                """,
                (max(0, int(to_seq)), now, conversation, participant, current_memory_revision),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("participant actor summary revision conflict")
            return _row_dict(
                conn.execute(
                    "SELECT * FROM actor_context_summaries WHERE summary_id = ?",
                    (summary_id,),
                ).fetchone()
            )

        return self._execute_write(_do)

    def invalidate_summaries_for_events(
        self,
        source_event_ids: Iterable[str],
        *,
        reason: str,
    ) -> list[str]:
        targets = {_text(item) for item in source_event_ids if _text(item)}
        if not targets:
            return []
        now = time.time()

        def _do(conn: sqlite3.Connection) -> list[str]:
            invalidated: list[str] = []
            for table in (
                "actor_context_summaries",
                "activity_context_summaries",
                "node_attempt_summaries",
            ):
                rows = conn.execute(
                    f"SELECT * FROM {table} WHERE status = 'active'"
                ).fetchall()
                for row in rows:
                    sources = set(_load_json(row["source_event_ids_json"], []))
                    if not sources.intersection(targets):
                        continue
                    summary_id = _text(row["summary_id"])
                    conn.execute(
                        f"""
                        UPDATE {table}
                           SET status = 'invalidated', invalidated_reason = ?, updated_at = ?
                         WHERE summary_id = ? AND status = 'active'
                        """,
                        (_text(reason), now, summary_id),
                    )
                    if table == "actor_context_summaries":
                        conn.execute(
                            """
                            UPDATE conversation_participants
                               SET transcript_cursor = MIN(transcript_cursor, ?),
                                   memory_revision = memory_revision + 1,
                                   updated_at = ?
                             WHERE conversation_session_id = ?
                               AND participant_id = ? AND status = 'active'
                            """,
                            (
                                max(0, int(row["from_seq"] or 0) - 1),
                                now,
                                _text(row["conversation_session_id"]),
                                _text(row["actor_participant_id"]),
                            ),
                        )
                    invalidated.append(summary_id)
            rows = conn.execute(
                """
                SELECT memory_id, provenance_json
                  FROM conversation_memory_items
                 WHERE status = 'proposed'
                """
            ).fetchall()
            for row in rows:
                provenance = _load_json(row["provenance_json"], {})
                sources = set(provenance.get("source_event_ids") or []) if isinstance(provenance, dict) else set()
                if not sources.intersection(targets):
                    continue
                memory_id = _text(row["memory_id"])
                conn.execute(
                    """
                    UPDATE conversation_memory_items
                       SET status = 'invalidated', invalidated_at = ?,
                           revision = revision + 1, updated_at = ?
                     WHERE memory_id = ? AND status = 'proposed'
                    """,
                    (now, now, memory_id),
                )
                invalidated.append(memory_id)
            return invalidated

        return self._execute_write(_do)


__all__ = ["ConversationMemoryRepo"]
