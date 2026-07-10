"""Read-only usage analytics for persisted sessions."""

from __future__ import annotations

import json
import sqlite3
import time
from collections import Counter
from typing import Any


class SessionAnalyticsService:
    SESSION_COLUMNS = (
        "id, source, model, started_at, ended_at, message_count, tool_call_count, "
        "input_tokens, output_tokens, cache_read_tokens, cache_write_tokens, "
        "billing_provider, billing_base_url, billing_mode, estimated_cost_usd, "
        "actual_cost_usd, cost_status, cost_source"
    )
    SESSIONS_WITH_SOURCE_QUERY = (
        f"SELECT {SESSION_COLUMNS} FROM sessions"
        " WHERE started_at >= ? AND source = ?"
        " ORDER BY started_at DESC"
    )
    SESSIONS_QUERY = (
        f"SELECT {SESSION_COLUMNS} FROM sessions"
        " WHERE started_at >= ?"
        " ORDER BY started_at DESC"
    )

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def insight_sessions(
        self,
        cutoff: float,
        source: str | None = None,
    ) -> list[dict[str, Any]]:
        if source:
            rows = self._conn.execute(
                self.SESSIONS_WITH_SOURCE_QUERY,
                (cutoff, source),
            ).fetchall()
        else:
            rows = self._conn.execute(self.SESSIONS_QUERY, (cutoff,)).fetchall()
        return [dict(row) for row in rows]

    def insight_tool_usage(
        self,
        cutoff: float,
        source: str | None = None,
    ) -> list[dict[str, Any]]:
        source_clause = " AND s.source = ?" if source else ""
        params = (cutoff, source) if source else (cutoff,)
        explicit_rows = self._conn.execute(
            "SELECT m.tool_name, COUNT(*) AS count "
            "FROM messages m JOIN sessions s ON s.id = m.session_id "
            "WHERE s.started_at >= ?"
            f"{source_clause} "
            "AND m.role = 'tool' AND m.tool_name IS NOT NULL "
            "GROUP BY m.tool_name ORDER BY count DESC",
            params,
        ).fetchall()
        assistant_rows = self._conn.execute(
            "SELECT m.tool_calls "
            "FROM messages m JOIN sessions s ON s.id = m.session_id "
            "WHERE s.started_at >= ?"
            f"{source_clause} "
            "AND m.role = 'assistant' AND m.tool_calls IS NOT NULL",
            params,
        ).fetchall()

        explicit = Counter({str(row["tool_name"]): int(row["count"] or 0) for row in explicit_rows})
        declared: Counter[str] = Counter()
        for row in assistant_rows:
            try:
                calls = row["tool_calls"]
                if isinstance(calls, str):
                    calls = json.loads(calls)
            except (json.JSONDecodeError, TypeError):
                continue
            if not isinstance(calls, list):
                continue
            for call in calls:
                function = call.get("function", {}) if isinstance(call, dict) else {}
                name = function.get("name")
                if name:
                    declared[str(name)] += 1

        if not explicit:
            merged = declared
        elif not declared:
            merged = explicit
        else:
            merged = Counter({
                tool: max(explicit.get(tool, 0), declared.get(tool, 0))
                for tool in set(explicit) | set(declared)
            })
        return [
            {"tool_name": name, "count": count}
            for name, count in merged.most_common()
        ]

    def insight_skill_usage(
        self,
        cutoff: float,
        source: str | None = None,
    ) -> list[dict[str, Any]]:
        source_clause = " AND s.source = ?" if source else ""
        params = (cutoff, source) if source else (cutoff,)
        rows = self._conn.execute(
            "SELECT m.tool_calls, m.timestamp "
            "FROM messages m JOIN sessions s ON s.id = m.session_id "
            "WHERE s.started_at >= ?"
            f"{source_clause} "
            "AND m.role = 'assistant' AND m.tool_calls IS NOT NULL",
            params,
        ).fetchall()
        skills: dict[str, dict[str, Any]] = {}
        for row in rows:
            try:
                calls = row["tool_calls"]
                if isinstance(calls, str):
                    calls = json.loads(calls)
            except (json.JSONDecodeError, TypeError):
                continue
            if not isinstance(calls, list):
                continue
            for call in calls:
                if not isinstance(call, dict):
                    continue
                function = call.get("function", {})
                tool_name = function.get("name")
                if tool_name not in {"skill_view", "skill_manage"}:
                    continue
                args = function.get("arguments")
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except (json.JSONDecodeError, TypeError):
                        continue
                if not isinstance(args, dict):
                    continue
                skill_name = args.get("name")
                if not isinstance(skill_name, str) or not skill_name.strip():
                    continue
                entry = skills.setdefault(
                    skill_name,
                    {
                        "skill": skill_name,
                        "view_count": 0,
                        "manage_count": 0,
                        "last_used_at": None,
                    },
                )
                entry["view_count" if tool_name == "skill_view" else "manage_count"] += 1
                timestamp = row["timestamp"]
                if timestamp is not None and (
                    entry["last_used_at"] is None
                    or timestamp > entry["last_used_at"]
                ):
                    entry["last_used_at"] = timestamp
        return list(skills.values())

    def insight_message_stats(
        self,
        cutoff: float,
        source: str | None = None,
    ) -> dict[str, Any]:
        source_clause = " AND s.source = ?" if source else ""
        params = (cutoff, source) if source else (cutoff,)
        row = self._conn.execute(
            "SELECT COUNT(*) AS total_messages, "
            "SUM(CASE WHEN m.role = 'user' THEN 1 ELSE 0 END) AS user_messages, "
            "SUM(CASE WHEN m.role = 'assistant' THEN 1 ELSE 0 END) AS assistant_messages, "
            "SUM(CASE WHEN m.role = 'tool' THEN 1 ELSE 0 END) AS tool_messages "
            "FROM messages m JOIN sessions s ON s.id = m.session_id "
            "WHERE s.started_at >= ?"
            f"{source_clause}",
            params,
        ).fetchone()
        return dict(row) if row else {
            "total_messages": 0,
            "user_messages": 0,
            "assistant_messages": 0,
            "tool_messages": 0,
        }

    @staticmethod
    def skill_breakdown(skill_usage: list[dict[str, Any]]) -> dict[str, Any]:
        total_loads = sum(int(skill["view_count"]) for skill in skill_usage)
        total_edits = sum(int(skill["manage_count"]) for skill in skill_usage)
        total_actions = total_loads + total_edits
        top_skills = []
        for skill in skill_usage:
            total_count = int(skill["view_count"]) + int(skill["manage_count"])
            top_skills.append({
                **skill,
                "total_count": total_count,
                "percentage": (total_count / total_actions * 100) if total_actions else 0,
            })
        top_skills.sort(
            key=lambda skill: (
                skill["total_count"],
                skill["view_count"],
                skill["manage_count"],
                skill.get("last_used_at") or 0,
                skill["skill"],
            ),
            reverse=True,
        )
        return {
            "summary": {
                "total_skill_loads": total_loads,
                "total_skill_edits": total_edits,
                "total_skill_actions": total_actions,
                "distinct_skills_used": len(skill_usage),
            },
            "top_skills": top_skills,
        }

    def usage(self, days: int = 30) -> dict[str, Any]:
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
        totals = dict(
            self._conn.execute(
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
            ).fetchone()
        )
        return {
            "daily": [dict(row) for row in daily_rows],
            "by_model": [dict(row) for row in model_rows],
            "totals": totals,
            "period_days": days,
            "skills": self.skill_breakdown(self.insight_skill_usage(cutoff)),
        }

    def models(self, days: int = 30) -> dict[str, Any]:
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
        totals = dict(
            self._conn.execute(
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
            ).fetchone()
        )
        return {"rows": [dict(row) for row in rows], "totals": totals, "period_days": days}
__all__ = ["SessionAnalyticsService"]
