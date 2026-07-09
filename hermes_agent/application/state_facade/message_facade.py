from __future__ import annotations

import json
import sqlite3
import time
from typing import Any, Dict, List, Optional

from agent.memory_manager import sanitize_context
from hermes_agent.read_models.message_history import MessageHistoryReadModel, MessagePageQuery
from hermes_agent.read_models.session_recall import SessionRecallReadModel
from hermes_agent.read_models.session_recall import (
    _contains_cjk as _recall_contains_cjk,
    _sanitize_fts5_query as _recall_sanitize_fts5_query,
)
from hermes_agent.repositories.message_repo import MessageRepoImpl, MessageRepository
from hermes_agent.repositories.session_repo import SessionRepoImpl


class MessageStateFacadeMixin:
        _CONTENT_JSON_PREFIX = "\x00json:"

        def _encode_content(cls, content: Any) -> Any:
            """Serialize structured (list/dict) message content for sqlite.

            sqlite3 can only bind ``str``, ``bytes``, ``int``, ``float``, and ``None``
            to query parameters. Multimodal messages have ``content`` as a list of
            parts (``[{"type": "text", ...}, {"type": "image_url", ...}]``), which
            raises ``ProgrammingError: Error binding parameter N: type 'list' is
            not supported`` when bound directly.

            Returns the value unchanged when it's already a safe scalar, or a
            sentinel-prefixed JSON string for lists/dicts. Paired with
            :meth:`_decode_content` on read.
            """
            if content is None or isinstance(content, (str, bytes, int, float)):
                return content
            try:
                return cls._CONTENT_JSON_PREFIX + json.dumps(content)
            except (TypeError, ValueError):
                # Last-resort fallback: stringify so persistence never fails.
                return str(content)

        @classmethod
        def _decode_content(cls, content: Any) -> Any:
            """Reverse :meth:`_encode_content`; returns scalars unchanged."""
            if isinstance(content, str) and content.startswith(cls._CONTENT_JSON_PREFIX):
                try:
                    return json.loads(content[len(cls._CONTENT_JSON_PREFIX):])
                except (json.JSONDecodeError, TypeError):
                    logger.warning(
                        "Failed to decode JSON-encoded message content; "
                        "returning raw string"
                    )
                    return content
            return content

        @classmethod
        def _message_preview_text(cls, content: Any, limit: int = 60) -> str:
            """Return the compact user-facing preview stored on ``sessions``."""
            decoded = cls._decode_content(content)
            if isinstance(decoded, list):
                parts: list[str] = []
                for item in decoded:
                    if isinstance(item, dict):
                        parts.append(str(item.get("text") or item.get("content") or ""))
                    else:
                        parts.append(str(item or ""))
                preview = " ".join(part for part in parts if part).strip()
                if not preview and decoded:
                    preview = "[multimodal content]"
            elif isinstance(decoded, dict):
                preview = str(decoded.get("text") or decoded.get("content") or "").strip()
            else:
                preview = str(decoded or "").strip()
            preview = " ".join(preview.split())
            if len(preview) > limit:
                return preview[:limit] + "..."
            return preview

        @classmethod
        def _message_display_title_text(cls, content: Any, limit: int = 100) -> str:
            """Return a deterministic product title derived from the first user message."""
            decoded = cls._decode_content(content)
            if isinstance(decoded, list):
                parts: list[str] = []
                for item in decoded:
                    if isinstance(item, dict):
                        parts.append(str(item.get("text") or item.get("content") or ""))
                    else:
                        parts.append(str(item or ""))
                title = " ".join(part for part in parts if part).strip()
                if not title and decoded:
                    title = "[multimodal content]"
            elif isinstance(decoded, dict):
                title = str(decoded.get("text") or decoded.get("content") or "").strip()
            else:
                title = str(decoded or "").strip()
            title = " ".join(title.split())
            if len(title) > limit:
                return title[:limit].rstrip()
            return title

        def _rebuild_session_list_summary(self, conn: sqlite3.Connection, session_id: str) -> None:
            """Recompute list summary fields after active-message set changes."""
            MessageRepository(conn, SessionRepoImpl(conn)).rebuild_session_projection(session_id)

        def append_message(
            self,
            session_id: str,
            role: str,
            content: str = None,
            participant_id: str = "",
            tool_name: str = None,
            tool_calls: Any = None,
            tool_call_id: str = None,
            token_count: int = None,
            finish_reason: str = None,
            reasoning: str = None,
            reasoning_content: str = None,
            reasoning_details: Any = None,
            codex_reasoning_items: Any = None,
            codex_message_items: Any = None,
            platform_message_id: str = None,
            conversation_message_id: str = "",
            metadata: Any = None,
        ) -> int:
            """
            Append a message to a session. Returns the message row ID.

            Also increments the session's message_count (and tool_call_count
            if role is 'tool' or tool_calls is present).

            ``platform_message_id`` is the external messaging platform's own
            message ID (e.g. Telegram update_id, Yuanbao msg_id).  It is
            independent of the SQLite autoincrement primary key and is used by
            platform-specific flows like yuanbao's recall guard to redact a
            message by its platform-side identifier.
            """
            message = {
                "role": role,
                "content": content,
                "participant_id": str(participant_id or "").strip(),
                "tool_name": tool_name,
                "tool_calls": tool_calls,
                "tool_call_id": tool_call_id,
                "token_count": token_count,
                "finish_reason": finish_reason,
                "reasoning": reasoning,
                "reasoning_content": reasoning_content,
                "reasoning_details": reasoning_details,
                "codex_reasoning_items": codex_reasoning_items,
                "codex_message_items": codex_message_items,
                "platform_message_id": platform_message_id,
                "conversation_message_id": str(conversation_message_id or "").strip(),
                "metadata": metadata if isinstance(metadata, dict) else {},
            }
            return MessageRepository(
                self._conn,
                SessionRepoImpl(self._conn),
            ).append_conversation_message(session_id, message)

        def replace_messages(self, session_id: str, messages: List[Dict[str, Any]]) -> None:
            """Atomically replace every message for a session.

            Used by transcript-rewrite flows such as /retry, /undo, and /compress.
            The delete + reinsert sequence must commit as one transaction so a
            mid-rewrite failure does not leave SQLite with a partial transcript.
            """
            rewrite_messages = []
            for message in messages:
                next_message = dict(message)
                next_message.pop("timestamp", None)
                rewrite_messages.append(next_message)
            MessageRepository(self._conn, SessionRepoImpl(self._conn)).replace_conversation(
                session_id,
                rewrite_messages,
            )

        def get_messages(
            self,
            session_id: str,
            include_inactive: bool = False,
        ) -> List[Dict[str, Any]]:
            """Load messages for a session, ordered by insertion order.

            Soft-deleted rewind rows are hidden by default and remain available via
            ``include_inactive=True`` for audit/debug views.
            """
            active_clause = "" if include_inactive else " AND active = 1"
            with self._lock:
                cursor = self._conn.execute(
                    "SELECT * FROM messages WHERE session_id = ?"
                    f"{active_clause} ORDER BY id",
                    (session_id,),
                )
                rows = cursor.fetchall()
            result = []
            for row in rows:
                msg = dict(row)
                if "content" in msg:
                    msg["content"] = self._decode_content(msg["content"])
                if msg.get("tool_calls"):
                    try:
                        msg["tool_calls"] = json.loads(msg["tool_calls"])
                    except (json.JSONDecodeError, TypeError):
                        logger.warning("Failed to deserialize tool_calls in get_messages, falling back to []")
                        msg["tool_calls"] = []
                if msg.get("metadata_json"):
                    try:
                        msg["metadata"] = json.loads(msg["metadata_json"])
                    except (json.JSONDecodeError, TypeError):
                        logger.warning("Failed to deserialize metadata_json in get_messages, falling back to None")
                        msg["metadata"] = None
                result.append(msg)
            return result

        @staticmethod
        def _decode_message_metadata_json(raw: Any) -> Dict[str, Any]:
            if not raw:
                return {}
            try:
                value = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                logger.warning("Failed to deserialize message metadata")
                return {}
            return value if isinstance(value, dict) else {}

        @staticmethod
        def _merge_message_metadata(current: Any, patch: Dict[str, Any]) -> Dict[str, Any]:
            base = dict(current) if isinstance(current, dict) else {}
            for key, value in patch.items():
                if isinstance(value, dict) and isinstance(base.get(key), dict):
                    base[key] = MessageStateFacadeMixin._merge_message_metadata(base[key], value)
                else:
                    base[key] = value
            return base

        @staticmethod
        def _metadata_matches_turn(metadata: Any, *, run_id: str, turn_id: str, client_message_id: str) -> bool:
            if not isinstance(metadata, dict):
                return False
            return any(
                expected and str(metadata.get(key) or "").strip() == expected
                for key, expected in (
                    ("run_id", run_id),
                    ("turn_id", turn_id),
                    ("client_message_id", client_message_id),
                )
            )

        @staticmethod
        def _conversation_message_metadata(message: Dict[str, Any]) -> Dict[str, Any]:
            metadata = message.get("metadata")
            return metadata if isinstance(metadata, dict) else {}

        @staticmethod
        def _conversation_message_storage_id(message: Dict[str, Any]) -> int:
            value = message.get("message_id") or message.get("id")
            try:
                return int(value)
            except (TypeError, ValueError):
                return 0

        @classmethod
        def _conversation_message_run_id(cls, message: Dict[str, Any]) -> str:
            metadata = cls._conversation_message_metadata(message)
            return str(metadata.get("run_id") or metadata.get("runId") or "").strip()

        @classmethod
        def _conversation_message_turn_id(cls, message: Dict[str, Any]) -> str:
            metadata = cls._conversation_message_metadata(message)
            return str(metadata.get("turn_id") or metadata.get("turnId") or "").strip()

        @classmethod
        def _conversation_message_participant_id(cls, message: Dict[str, Any]) -> str:
            metadata = cls._conversation_message_metadata(message)
            return str(
                message.get("participant_id")
                or message.get("participantId")
                or metadata.get("participant_id")
                or metadata.get("participantId")
                or ""
            ).strip()

        def _session_ids_are_team_conversation(self, session_ids: List[str]) -> bool:
            for sid in session_ids:
                if str(sid or "").strip().startswith("team-session-team-conversation-"):
                    return True
                try:
                    row = self.get_session_index(str(sid or ""))
                except Exception:
                    row = None
                if isinstance(row, dict) and str(row.get("conversation_kind") or "").strip().lower() == "team":
                    return True
            return False

        def _backfill_team_transcript_projections(self, session_ids: List[str]) -> int:
            target_session_ids = [
                str(session_id or "").strip()
                for session_id in session_ids
                if str(session_id or "").strip()
            ]
            if not target_session_ids:
                return 0
            try:
                from hermes_team_mission.runtime.team_transcript_writer import (
                    backfill_projected_message_artifacts_locked,
                    backfill_projected_message_tool_calls_locked,
                    backfill_unprojected_message_complete_events_locked,
                )
            except Exception:
                return 0

            def _do(conn: sqlite3.Connection) -> int:
                projected = backfill_unprojected_message_complete_events_locked(
                    self,
                    conn,
                    session_ids=target_session_ids,
                )
                artifacts = backfill_projected_message_artifacts_locked(
                    self,
                    conn,
                    session_ids=target_session_ids,
                )
                # Heal historical team leader assistant rows whose
                # tool_calls column was NEVER populated (the projection path
                # simply didn't write it — see
                # ``_upsert_team_message_by_id_locked``). Without this
                # backfill, existing conversations continue to render
                # without the ``team_mission_start_task`` card even after
                # the write path is fixed.
                tool_calls = backfill_projected_message_tool_calls_locked(
                    self,
                    conn,
                    session_ids=target_session_ids,
                )
                return projected + artifacts + tool_calls

            try:
                return int(self._execute_write(_do) or 0)
            except Exception as exc:
                logger.debug("team transcript projection backfill skipped: %s", exc)
                return 0

        @staticmethod
        def _strip_storage_fields(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
            stripped: List[Dict[str, Any]] = []
            for message in messages:
                next_message = dict(message)
                next_message.pop("message_id", None)
                next_message.pop("timestamp", None)
                stripped.append(next_message)
            return stripped

        @staticmethod
        def _team_main_transcript_messages(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
            """Return the canonical user-visible transcript projection for team sessions."""
            try:
                from hermes_team_mission.runtime.team_transcript_writer import is_main_transcript_message
            except Exception:
                return list(messages)
            visible: List[Dict[str, Any]] = []
            seen_stable_ids: set[str] = set()
            for message in messages:
                if not isinstance(message, dict) or not is_main_transcript_message(message):
                    continue
                stable_id = MessageStateFacadeMixin._team_transcript_stable_message_id(message)
                if stable_id:
                    if stable_id in seen_stable_ids:
                        continue
                    seen_stable_ids.add(stable_id)
                visible.append(message)
            return visible

        @staticmethod
        def _team_transcript_stable_message_id(message: Dict[str, Any]) -> str:
            metadata = message.get("metadata") if isinstance(message.get("metadata"), dict) else {}
            return str(
                message.get("conversation_message_id")
                or message.get("conversationMessageId")
                or metadata.get("conversation_message_id")
                or metadata.get("conversationMessageId")
                or ""
            ).strip()

        @staticmethod
        def _page_canonical_messages(
            messages: List[Dict[str, Any]],
            *,
            direction: str,
            cursor_id: Optional[int],
            limit: int,
        ) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
            def _message_id(message: Dict[str, Any]) -> int:
                value = message.get("message_id") or message.get("id")
                try:
                    return int(value)
                except (TypeError, ValueError):
                    return 0

            total_count = len(messages)
            normalized_direction = str(direction or "tail").lower()
            if normalized_direction == "before" and cursor_id is not None:
                before = [message for message in messages if _message_id(message) < cursor_id]
                selected = before[-limit:]
                has_more_before = len(before) > len(selected)
                has_more_after = bool(selected)
            elif normalized_direction == "after" and cursor_id is not None:
                after = [message for message in messages if _message_id(message) > cursor_id]
                selected = after[:limit]
                has_more_before = bool(selected)
                has_more_after = len(after) > len(selected)
            else:
                selected = messages[-limit:]
                has_more_before = len(messages) > len(selected)
                has_more_after = False

            first_id = _message_id(selected[0]) if selected else None
            last_id = _message_id(selected[-1]) if selected else None
            return selected, {
                "prev_cursor_id": first_id if has_more_before else None,
                "next_cursor_id": last_id if has_more_after else None,
                "hasMoreBefore": has_more_before,
                "hasMoreAfter": has_more_after,
                "totalCount": total_count,
            }

        def get_message_by_conversation_message_id(
            self,
            session_id: str,
            conversation_message_id: str,
            *,
            include_inactive: bool = False,
        ) -> Optional[Dict[str, Any]]:
            """Return one visible transcript row by its stable conversation id."""
            conversation_session_id = str(session_id or "").strip()
            stable_message_id = str(conversation_message_id or "").strip()
            if not conversation_session_id or not stable_message_id:
                return None
            active_clause = "" if include_inactive else " AND active = 1"
            with self._lock:
                row = self._conn.execute(
                    f"SELECT {self._conversation_message_columns()} "
                    "FROM messages "
                    "WHERE session_id = ? AND conversation_message_id = ? "
                    f"{active_clause} "
                    "ORDER BY id LIMIT 1",
                    (conversation_session_id, stable_message_id),
                ).fetchone()
            if row is None:
                return None
            return self._message_row_as_conversation(
                row,
                include_storage_metadata=True,
            )

        def _upsert_team_message_by_id_locked(
            self,
            conn: sqlite3.Connection,
            *,
            session_id: str,
            conversation_message_id: str,
            role: str,
            content: Any,
            participant_id: str,
            metadata: Dict[str, Any],
            status: str = "",
            reasoning: Any = "",
            timestamp: float | None = None,
            tool_calls: Any = None,
        ) -> Dict[str, Any]:
            return MessageRepository(conn, SessionRepoImpl(conn)).upsert_team_message_by_id_locked(
                session_id=session_id,
                conversation_message_id=conversation_message_id,
                role=role,
                content=content,
                participant_id=participant_id,
                metadata=metadata,
                status=status,
                reasoning=reasoning,
                tool_calls=tool_calls,
                timestamp=timestamp,
            )

        def _upsert_team_message_by_id(
            self,
            *,
            session_id: str,
            conversation_message_id: str,
            role: str,
            content: Any,
            participant_id: str,
            metadata: Dict[str, Any],
            status: str = "",
            reasoning: Any = "",
            tool_calls: Any = None,
        ) -> Dict[str, Any]:
            """Insert or update an explicitly owned team transcript row by id."""
            return MessageRepository(
                self._conn,
                SessionRepoImpl(self._conn),
            ).upsert_team_message_by_id(
                session_id=session_id,
                conversation_message_id=conversation_message_id,
                role=role,
                content=content,
                participant_id=participant_id,
                metadata=metadata,
                status=status,
                reasoning=reasoning,
                tool_calls=tool_calls,
            )

        def upsert_projected_conversation_message(
            self,
            *,
            session_id: str,
            conversation_message_id: str,
            role: str,
            content: Any,
            participant_id: str,
            metadata: Dict[str, Any],
            status: str = "",
            reasoning: Any = "",
        ) -> Dict[str, Any]:
            """Deprecated compatibility shim for legacy DB RPC callers.

            New team transcript writes must go through the explicit writer subsystem
            in ``hermes_team_mission.runtime.team_transcript_writer``.
            """
            warnings.warn(
                "upsert_projected_conversation_message is deprecated; use the team transcript writer",
                DeprecationWarning,
                stacklevel=2,
            )
            return self._upsert_team_message_by_id(
                session_id=session_id,
                conversation_message_id=conversation_message_id,
                role=role,
                content=content,
                participant_id=participant_id,
                metadata=metadata,
                status=status,
                reasoning=reasoning,
            )

        def merge_message_metadata(
            self,
            session_id: str,
            metadata: Dict[str, Any],
            *,
            message_id: str | int | None = None,
            role: str | None = None,
            run_id: str | None = None,
            turn_id: str | None = None,
            client_message_id: str | None = None,
        ) -> Optional[Dict[str, Any]]:
            """Merge metadata into a stored message and return the updated message."""
            if not isinstance(metadata, dict) or not metadata:
                return None
            return MessageRepository(self._conn, SessionRepoImpl(self._conn)).merge_metadata(
                session_id,
                metadata,
                message_id=message_id,
                role=role,
                run_id=run_id,
                turn_id=turn_id,
                client_message_id=client_message_id,
            )

        def get_messages_around(
            self,
            session_id: str,
            around_message_id: int,
            window: int = 5,
            include_inactive: bool = False,
        ) -> Dict[str, Any]:
            return SessionRecallReadModel(self._conn).get_messages_around(
                session_id,
                around_message_id,
                window=window,
                include_inactive=include_inactive,
            )

        def get_anchored_view(
            self,
            session_id: str,
            around_message_id: int,
            window: int = 5,
            bookend: int = 3,
            keep_roles: Optional[Tuple[str, ...]] = ("user", "assistant"),
            include_inactive: bool = False,
        ) -> Dict[str, Any]:
            return SessionRecallReadModel(self._conn).get_anchored_view(
                session_id,
                around_message_id,
                window=window,
                bookend=bookend,
                keep_roles=keep_roles,
                include_inactive=include_inactive,
            )

        def resolve_resume_session_id(self, session_id: str) -> str:
            return SessionRecallReadModel(self._conn).resolve_resume_session_id(session_id)

        def _message_row_as_conversation(
            self,
            row,
            *,
            include_storage_metadata: bool = False,
        ) -> Dict[str, Any]:
            content = self._decode_content(row["content"])
            if row["role"] in {"user", "assistant"} and isinstance(content, str):
                content = sanitize_context(content).strip()
            msg = {"role": row["role"], "content": content}
            if include_storage_metadata:
                msg["message_id"] = str(row["id"])
                msg["timestamp"] = row["timestamp"]
            elif row["platform_message_id"]:
                # Surface the platform-side message id (e.g. yuanbao msg_id,
                # telegram update_id) so platform-specific flows like recall
                # can match by external identifier instead of having to fall
                # back to content-match heuristics.  Exposed as ``message_id``
                # for backward compatibility with the JSONL transcript shape.
                msg["message_id"] = row["platform_message_id"]
            conversation_message_id = str(row["conversation_message_id"] or "").strip()
            if conversation_message_id:
                msg["conversation_message_id"] = conversation_message_id
            participant_id = str(row["participant_id"] or "").strip()
            if participant_id:
                msg["participant_id"] = participant_id
            if row["tool_call_id"]:
                msg["tool_call_id"] = row["tool_call_id"]
            if row["tool_name"]:
                msg["tool_name"] = row["tool_name"]
            if row["tool_calls"]:
                try:
                    msg["tool_calls"] = json.loads(row["tool_calls"])
                except (json.JSONDecodeError, TypeError):
                    logger.warning("Failed to deserialize tool_calls in conversation replay, falling back to []")
                    msg["tool_calls"] = []
            if row["role"] == "assistant":
                if row["finish_reason"]:
                    msg["finish_reason"] = row["finish_reason"]
                if row["reasoning"]:
                    msg["reasoning"] = row["reasoning"]
                if row["reasoning_content"] is not None:
                    msg["reasoning_content"] = row["reasoning_content"]
                if row["reasoning_details"]:
                    try:
                        msg["reasoning_details"] = json.loads(row["reasoning_details"])
                    except (json.JSONDecodeError, TypeError):
                        logger.warning("Failed to deserialize reasoning_details, falling back to None")
                        msg["reasoning_details"] = None
                if row["codex_reasoning_items"]:
                    try:
                        msg["codex_reasoning_items"] = json.loads(row["codex_reasoning_items"])
                    except (json.JSONDecodeError, TypeError):
                        logger.warning("Failed to deserialize codex_reasoning_items, falling back to None")
                        msg["codex_reasoning_items"] = None
                if row["codex_message_items"]:
                    try:
                        msg["codex_message_items"] = json.loads(row["codex_message_items"])
                    except (json.JSONDecodeError, TypeError):
                        logger.warning("Failed to deserialize codex_message_items, falling back to None")
                        msg["codex_message_items"] = None
            if row["metadata_json"]:
                try:
                    msg["metadata"] = json.loads(row["metadata_json"])
                except (json.JSONDecodeError, TypeError):
                    logger.warning("Failed to deserialize message metadata, falling back to None")
                    msg["metadata"] = None
            return msg

        def _conversation_message_columns(self) -> str:
            return (
                "id, session_id, role, content, participant_id, tool_call_id, tool_calls, tool_name, timestamp, "
                "finish_reason, reasoning, reasoning_content, reasoning_details, "
                "codex_reasoning_items, codex_message_items, platform_message_id, conversation_message_id, metadata_json"
            )

        @staticmethod
        def _message_row_turn_metadata(row) -> Dict[str, str]:
            try:
                raw_metadata = row["metadata_json"]
            except (KeyError, IndexError):
                return {}
            if not raw_metadata:
                return {}
            try:
                metadata = json.loads(raw_metadata)
            except (json.JSONDecodeError, TypeError):
                logger.warning("Failed to deserialize message metadata for turn expansion")
                return {}
            if not isinstance(metadata, dict):
                return {}

            identity: Dict[str, str] = {}
            for key in ("turn_id", "run_id", "client_message_id"):
                value = metadata.get(key)
                if value is None:
                    continue
                text = str(value).strip()
                if text:
                    identity[key] = text
            return identity

        def _expand_message_page_rows_to_turn_boundaries(
            self,
            rows: List[Any],
            *,
            session_ids: List[str],
            columns: str,
            include_inactive: bool = False,
        ) -> List[Any]:
            if not rows:
                return rows

            selected_identity_values: Dict[str, set[str]] = {
                "turn_id": set(),
                "run_id": set(),
                "client_message_id": set(),
            }
            for row in rows:
                identity = self._message_row_turn_metadata(row)
                for key, value in identity.items():
                    selected_identity_values[key].add(value)

            if not any(selected_identity_values.values()):
                return rows

            placeholders = ",".join("?" for _ in session_ids)
            active_clause = "" if include_inactive else " AND active = 1"
            candidate_rows = self._conn.execute(
                f"SELECT {columns} FROM messages "
                f"WHERE session_id IN ({placeholders}) AND metadata_json IS NOT NULL "
                f"{active_clause} "
                "ORDER BY id",
                tuple(session_ids),
            ).fetchall()

            rows_by_id = {int(row["id"]): row for row in rows}
            matched_min_id_by_session: Dict[str, int] = {}
            matched_user_sessions: set[str] = set()

            for row in candidate_rows:
                identity = self._message_row_turn_metadata(row)
                if not any(
                    value in selected_identity_values[key]
                    for key, value in identity.items()
                    if key in selected_identity_values
                ):
                    continue

                row_id = int(row["id"])
                rows_by_id[row_id] = row
                row_session_id = str(row["session_id"])
                current_min = matched_min_id_by_session.get(row_session_id)
                if current_min is None or row_id < current_min:
                    matched_min_id_by_session[row_session_id] = row_id
                if row["role"] == "user":
                    matched_user_sessions.add(row_session_id)

            for row_session_id, first_matched_id in matched_min_id_by_session.items():
                if row_session_id in matched_user_sessions:
                    continue
                previous_user = self._conn.execute(
                    f"SELECT {columns} FROM messages "
                    "WHERE session_id = ? AND role = 'user' AND id < ? "
                    f"{active_clause} "
                    "ORDER BY id DESC LIMIT 1",
                    (row_session_id, first_matched_id),
                ).fetchone()
                if previous_user is not None:
                    rows_by_id[int(previous_user["id"])] = previous_user

            return [rows_by_id[row_id] for row_id in sorted(rows_by_id)]

        def _has_messages_on_page_side(
            self,
            session_ids: List[str],
            *,
            row_id: Optional[int],
            side: str,
            include_inactive: bool = False,
        ) -> bool:
            if row_id is None:
                return False
            placeholders = ",".join("?" for _ in session_ids)
            operator = "<" if side == "before" else ">"
            active_clause = "" if include_inactive else " AND active = 1"
            row = self._conn.execute(
                f"SELECT 1 FROM messages "
                f"WHERE session_id IN ({placeholders}) AND id {operator} ? "
                f"{active_clause} "
                "LIMIT 1",
                tuple(session_ids) + (row_id,),
            ).fetchone()
            return row is not None

        def get_messages_as_conversation(
            self,
            session_id: str,
            include_ancestors: bool = False,
            include_storage_metadata: bool = False,
            include_inactive: bool = False,
        ) -> List[Dict[str, Any]]:
            return MessageHistoryReadModel(self._conn).all_as_conversation(
                session_id,
                include_ancestors=include_ancestors,
                include_storage_metadata=include_storage_metadata,
                include_inactive=include_inactive,
            )

        def get_conversation_message_read_model(
            self,
            session_id: str,
            include_ancestors: bool = False,
            include_storage_metadata: bool = False,
            include_inactive: bool = False,
        ) -> List[Dict[str, Any]]:
            """Return the canonical visible transcript read model.

            Team conversations are written by the runtime-event projector; direct
            conversations still use the legacy append writer during migration. Both
            paths converge here so LLM hydration and render snapshot read one
            visible transcript model.
            """
            session_ids = [session_id]
            if include_ancestors:
                session_ids = self._session_lineage_root_to_tip(session_id)
            if self._session_ids_are_team_conversation(session_ids):
                self._backfill_team_transcript_projections(session_ids)
                messages = self.get_messages_as_conversation(
                    session_id,
                    include_ancestors=include_ancestors,
                    include_storage_metadata=True,
                    include_inactive=include_inactive,
                )
                messages = self._team_main_transcript_messages(messages)
                if include_ancestors:
                    filtered_messages: List[Dict[str, Any]] = []
                    for message in messages:
                        if self._is_duplicate_replayed_user_message(filtered_messages, message):
                            continue
                        filtered_messages.append(message)
                    messages = filtered_messages
                if not include_storage_metadata:
                    messages = self._strip_storage_fields(messages)
                return messages
            return self.get_messages_as_conversation(
                session_id,
                include_ancestors=include_ancestors,
                include_storage_metadata=include_storage_metadata,
                include_inactive=include_inactive,
            )

        def get_messages_page_as_conversation(
            self,
            session_id: str,
            direction: str = "tail",
            cursor_id: Optional[int] = None,
            limit: int = 50,
            include_ancestors: bool = False,
            include_inactive: bool = False,
        ) -> Dict[str, Any]:
            """Load one stable page of conversation messages with storage cursors.

            ``direction`` accepts:
              - ``tail``: newest ``limit`` messages, returned oldest-to-newest.
              - ``before``: ``limit`` messages older than ``cursor_id``.
              - ``after``: ``limit`` messages newer than ``cursor_id``.

            Cursors are SQLite message row ids.  The returned messages include a
            string ``message_id`` based on that row id so clients can dedupe pages
            without relying on mutable text content.
            """
            try:
                page_limit = int(limit)
            except (TypeError, ValueError):
                page_limit = 50
            page_limit = max(1, min(page_limit, 500))

            session_ids = [session_id]
            if include_ancestors:
                session_ids = self._session_lineage_root_to_tip(session_id)

            normalized_direction = str(direction or "tail").lower()
            if normalized_direction not in {"tail", "before", "after"}:
                normalized_direction = "tail"

            if self._session_ids_are_team_conversation(session_ids):
                self._backfill_team_transcript_projections(session_ids)
                with self._lock:
                    placeholders = ",".join("?" for _ in session_ids)
                    active_clause = "" if include_inactive else " AND active = 1"
                    # Order by timestamp first (matches the non-team branch at
                    # line ~6427 and the test invariant in
                    # test_team_transcript_writer_raw_segments._stored_messages_by_timestamp).
                    # ``project_message_complete_event_locked`` upserts the
                    # assistant row for the FINAL segment first (higher
                    # message_seq_in_run), then
                    # ``_project_reconstructed_assistant_segments_locked``
                    # back-fills earlier raw segments (pre-tool text). Those
                    # back-filled rows have SMALLER timestamps but LARGER
                    # ids — ordering by id alone yields post-tool text
                    # before pre-tool text and the tool card between them,
                    # which is the "顺序又乱" symptom.
                    rows = self._conn.execute(
                        f"SELECT {self._conversation_message_columns()} "
                        f"FROM messages WHERE session_id IN ({placeholders})"
                        f"{active_clause} ORDER BY timestamp ASC, id ASC",
                        tuple(session_ids),
                    ).fetchall()
                messages = [
                    self._message_row_as_conversation(
                        row,
                        include_storage_metadata=True,
                    )
                    for row in rows
                ]
                messages = self._team_main_transcript_messages(messages)
                if include_ancestors:
                    filtered_messages: List[Dict[str, Any]] = []
                    for message in messages:
                        if self._is_duplicate_replayed_user_message(filtered_messages, message):
                            continue
                        filtered_messages.append(message)
                    messages = filtered_messages
                selected, page_info = self._page_canonical_messages(
                    messages,
                    direction=normalized_direction,
                    cursor_id=cursor_id,
                    limit=page_limit,
                )
                return {
                    "messages": selected,
                    "pageInfo": page_info,
                }

            return MessageHistoryReadModel(self._conn).page_as_conversation(
                session_id,
                MessagePageQuery(
                    direction=normalized_direction,
                    cursor_id=cursor_id,
                    limit=page_limit,
                    include_ancestors=include_ancestors,
                    include_inactive=include_inactive,
                ),
            )

        def _session_lineage_root_to_tip(self, session_id: str) -> List[str]:
            if not session_id:
                return [session_id]

            with self._lock:
                return self._session_branch_service()._replayable_lineage_root_to_tip_conn(
                    self._conn,
                    session_id,
                )

        @staticmethod
        def _is_duplicate_replayed_user_message(messages: List[Dict[str, Any]], msg: Dict[str, Any]) -> bool:
            if msg.get("role") != "user":
                return False
            content = msg.get("content")
            if not isinstance(content, str) or not content:
                return False
            for prev in reversed(messages):
                if prev.get("role") == "user" and prev.get("content") == content:
                    return True
                if prev.get("role") == "assistant" and (prev.get("content") or prev.get("tool_calls")):
                    return False
            return False

        # =========================================================================
        # Rewind (soft-delete)
        # =========================================================================

        def rewind_to_message(
            self,
            session_id: str,
            target_message_id: int,
        ) -> Dict[str, Any]:
            """Soft-delete the target user message and every following row.

            The rows remain on disk with ``active=0`` for audit/debug views. Normal
            transcript reads, search, resume and pagination ignore inactive rows by
            default. The target row is included in the soft-delete so callers can
            prefill it into the composer without duplicating it in the replayed
            context.
            """
            with self._lock:
                row = self._conn.execute(
                    "SELECT * FROM messages WHERE id = ? AND session_id = ?",
                    (target_message_id, session_id),
                ).fetchone()
            if row is None:
                raise ValueError(
                    f"message {target_message_id} not found in session {session_id}"
                )

            target_row = dict(row)
            if target_row.get("role") != "user":
                raise ValueError(
                    "rewind target must be a 'user' message "
                    f"(got role={target_row.get('role')!r}, id={target_message_id})"
                )
            target_row["content"] = self._decode_content(target_row.get("content"))

            def _do(conn):
                ids = MessageRepoImpl(conn).deactivate_from(session_id, target_message_id)
                SessionRepoImpl(conn).increment_rewind_count(session_id)
                self._rebuild_session_list_summary(conn, session_id)
                return ids

            rewound_ids = self._execute_write(_do)

            with self._lock:
                row = self._conn.execute(
                    "SELECT MAX(id) FROM messages WHERE session_id = ? AND active = 1",
                    (session_id,),
                ).fetchone()
            new_head_id = row[0] if row and row[0] is not None else None

            return {
                "rewound_count": len(rewound_ids),
                "target_message": target_row,
                "new_head_id": new_head_id,
            }

        def restore_rewound(self, session_id: str, since_message_id: int) -> int:
            """Restore inactive rows from ``since_message_id`` onward."""

            def _do(conn):
                restored = MessageRepoImpl(conn).restore_from(session_id, since_message_id)
                self._rebuild_session_list_summary(conn, session_id)
                return restored

            return self._execute_write(_do)

        def list_recent_user_messages(
            self,
            session_id: str,
            limit: int = 20,
            include_inactive: bool = False,
        ) -> List[Dict[str, Any]]:
            return MessageHistoryReadModel(self._conn).list_recent_user_messages(
                session_id,
                limit=limit,
                include_inactive=include_inactive,
            )

        def search_messages(
            self,
            query: str,
            source_filter: List[str] = None,
            exclude_sources: List[str] = None,
            role_filter: List[str] = None,
            limit: int = 20,
            offset: int = 0,
            sort: str = None,
            include_inactive: bool = False,
        ) -> List[Dict[str, Any]]:
            return SessionRecallReadModel(self._conn).search_messages(
                query,
                source_filter=source_filter,
                exclude_sources=exclude_sources,
                role_filter=role_filter,
                limit=limit,
                offset=offset,
                sort=sort,
                include_inactive=include_inactive,
            )

        def search_sessions_by_id(
            self,
            query: str,
            limit: int = 20,
        ) -> List[Dict[str, Any]]:
            """Search surfaced sessions by exact/prefix/substring session id.

            Matching checks each surfaced row's id and projected compression root
            id, while ``list_sessions_rich(id_query=...)`` pushes the candidate
            filter into SQL so desktop/web search does not scan every session row.
            """
            needle = (query or "").strip().lower()
            try:
                bounded_limit = max(1, min(int(limit), 100))
            except (TypeError, ValueError):
                bounded_limit = 20
            if not needle:
                return []

            candidates = self.list_sessions_rich(
                limit=max(bounded_limit * 4, bounded_limit),
                offset=0,
                order_by_last_active=True,
                id_query=needle,
            )

            def score(row: Dict[str, Any]) -> int:
                ids = [str(row.get("id") or ""), str(row.get("_lineage_root_id") or "")]
                normalized = [value.lower() for value in ids if value]
                if any(value == needle for value in normalized):
                    return 0
                if any(value.startswith(needle) for value in normalized):
                    return 1
                return 2

            ranked = sorted(
                enumerate(candidates),
                key=lambda item: (score(item[1]), item[0]),
            )
            return [row for _, row in ranked[:bounded_limit]]
