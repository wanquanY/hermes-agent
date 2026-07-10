"""Evidence-based migration of legacy delegated child sessions."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from hermes_agent.repositories.message_content_codec import decode_message_content
from hermes_agent.repositories.session_repo import SessionRepoImpl


_DELEGATE_TOOL_NAMES = frozenset({"delegate_task", "delegate"})


def reconcile_legacy_delegate_execution_sessions(conn: sqlite3.Connection) -> int:
    """Classify child sessions only when persisted delegation evidence agrees.

    Parent linkage alone is ambiguous because compression and user branches also
    create child sessions. A legacy row is migrated only when its first user
    message exactly matches a goal persisted in a parent ``delegate_task`` call
    and it has no user-branch lineage.
    """

    candidates = conn.execute(
        """
        SELECT s.id, s.parent_session_id
          FROM sessions s
         WHERE COALESCE(s.parent_session_id, '') != ''
           AND COALESCE(s.session_kind, 'hermes_session') != 'execution'
           AND COALESCE(s.conversation_kind, 'direct') = 'direct'
           AND NOT EXISTS (
                SELECT 1 FROM session_lineage l
                 WHERE l.session_id = s.id
                   AND l.branch_origin = 'user_message_action'
           )
        """
    ).fetchall()
    goals_by_parent: dict[str, set[str]] = {}
    sessions = SessionRepoImpl(conn)
    migrated = 0
    for row in candidates:
        child_id = str(row["id"] or "").strip()
        parent_id = str(row["parent_session_id"] or "").strip()
        first_prompt = _first_user_prompt(conn, child_id)
        if not first_prompt:
            continue
        goals = goals_by_parent.get(parent_id)
        if goals is None:
            goals = _delegated_goals(conn, parent_id)
            goals_by_parent[parent_id] = goals
        if first_prompt not in goals:
            continue
        if sessions.classify_internal_execution(child_id):
            migrated += 1
    return migrated


def _first_user_prompt(conn: sqlite3.Connection, session_id: str) -> str:
    row = conn.execute(
        """
        SELECT content FROM messages
         WHERE session_id = ? AND role = 'user' AND COALESCE(active, 1) != 0
         ORDER BY id ASC LIMIT 1
        """,
        (session_id,),
    ).fetchone()
    if row is None:
        return ""
    content = decode_message_content(row["content"], allow_legacy_json=True)
    if isinstance(content, str):
        return _normalize_text(content)
    if isinstance(content, list):
        return _normalize_text(
            "\n".join(
                str(item.get("text") or "")
                for item in content
                if isinstance(item, dict) and item.get("type") == "text"
            )
        )
    return ""


def _delegated_goals(conn: sqlite3.Connection, parent_session_id: str) -> set[str]:
    rows = conn.execute(
        """
        SELECT tool_calls FROM messages
         WHERE session_id = ? AND role = 'assistant' AND tool_calls IS NOT NULL
         ORDER BY id ASC
        """,
        (parent_session_id,),
    ).fetchall()
    goals: set[str] = set()
    for row in rows:
        calls = _json_value(row["tool_calls"], [])
        if not isinstance(calls, list):
            continue
        for call in calls:
            function = call.get("function") if isinstance(call, dict) else None
            if not isinstance(function, dict) or str(function.get("name") or "") not in _DELEGATE_TOOL_NAMES:
                continue
            arguments = _json_value(function.get("arguments"), {})
            if not isinstance(arguments, dict):
                continue
            _add_goal(goals, arguments.get("goal"))
            tasks = arguments.get("tasks")
            if isinstance(tasks, list):
                for task in tasks:
                    if isinstance(task, dict):
                        _add_goal(goals, task.get("goal"))
    return goals


def _add_goal(goals: set[str], value: Any) -> None:
    normalized = _normalize_text(value)
    if normalized:
        goals.add(normalized)


def _normalize_text(value: Any) -> str:
    return " ".join(str(value or "").split())


def _json_value(value: Any, fallback: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value) if value else fallback
    except (TypeError, json.JSONDecodeError):
        return fallback


__all__ = ["reconcile_legacy_delegate_execution_sessions"]
