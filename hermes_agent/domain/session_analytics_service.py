"""Read-only usage analytics for persisted sessions."""

from __future__ import annotations

import sqlite3
import time
from typing import Any


class SessionAnalyticsService:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def usage(self, days: int = 30) -> dict[str, Any]:
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
        insights_report = InsightsEngine(self._conn).generate(days=days)
        return {
            "daily": [dict(row) for row in daily_rows],
            "by_model": [dict(row) for row in model_rows],
            "totals": totals,
            "period_days": days,
            "skills": insights_report.get("skills", _empty_skill_breakdown()),
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


__all__ = ["SessionAnalyticsService"]
