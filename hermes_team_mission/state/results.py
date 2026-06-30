from __future__ import annotations

import json
import sqlite3
import time
from typing import Any, Dict, List

from hermes_team_mission.context.artifact_refs import dedupe_artifact_refs
from hermes_team_mission.domain.utils import stable_id


def text(value: Any) -> str:
    return str(value or "").strip()


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _json_loads(value: str | None, fallback: Any) -> Any:
    if not value:
        return fallback
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return fallback


def _row_value(row: sqlite3.Row | None, key: str, default: Any = None) -> Any:
    if row is None:
        return default
    try:
        return row[key]
    except Exception:
        return default


def row_to_mission_result(row: sqlite3.Row | None) -> Dict[str, Any]:
    if row is None:
        return {}
    artifact_refs = _json_loads(_row_value(row, "artifact_refs_json", ""), [])
    artifact_refs = artifact_refs if isinstance(artifact_refs, list) else []
    node_results = _json_loads(_row_value(row, "node_results_json", ""), [])
    node_results = node_results if isinstance(node_results, list) else []
    metadata = _json_loads(_row_value(row, "metadata_json", ""), {})
    metadata = metadata if isinstance(metadata, dict) else {}
    result_id = text(_row_value(row, "result_id"))
    mission_id = text(_row_value(row, "mission_id"))
    activity_id = text(_row_value(row, "activity_id"))
    return {
        "result_id": result_id,
        "resultId": result_id,
        "mission_id": mission_id,
        "missionId": mission_id,
        "activity_id": activity_id,
        "activityId": activity_id,
        "status": text(_row_value(row, "status")),
        "outcome": text(_row_value(row, "outcome")),
        "summary_text": text(_row_value(row, "summary_text")),
        "summaryText": text(_row_value(row, "summary_text")),
        "node_results": node_results,
        "nodeResults": node_results,
        "artifact_refs": artifact_refs,
        "artifactRefs": artifact_refs,
        "leader_report_run_id": text(_row_value(row, "leader_report_run_id")),
        "leaderReportRunId": text(_row_value(row, "leader_report_run_id")),
        "leader_report_message_id": text(_row_value(row, "leader_report_message_id")),
        "leaderReportMessageId": text(_row_value(row, "leader_report_message_id")),
        "metadata": metadata,
        "created_at": float(_row_value(row, "created_at", 0) or 0),
        "createdAt": float(_row_value(row, "created_at", 0) or 0),
        "updated_at": float(_row_value(row, "updated_at", 0) or 0),
        "updatedAt": float(_row_value(row, "updated_at", 0) or 0),
    }


def upsert_team_mission_result(
    db: Any,
    *,
    result_id: str = "",
    mission_id: str,
    activity_id: str = "",
    status: str,
    outcome: str,
    summary_text: str,
    node_results: List[Dict[str, Any]] | None = None,
    artifact_refs: List[Dict[str, Any]] | None = None,
    leader_report_run_id: str = "",
    leader_report_message_id: str = "",
    metadata: Dict[str, Any] | None = None,
    created_at: float | None = None,
    updated_at: float | None = None,
) -> Dict[str, Any]:
    mission_id = text(mission_id)
    if not mission_id:
        return {}
    normalized_result_id = text(result_id) or stable_id("mission-result", mission_id)
    normalized_activity_id = text(activity_id) or f"mission:{mission_id}"
    normalized_status = text(status) or text(outcome) or "completed"
    normalized_outcome = text(outcome) or normalized_status
    normalized_summary = text(summary_text)
    if not normalized_summary:
        normalized_summary = f"Mission {normalized_outcome}: no presentable output."
    normalized_node_results = [dict(item) for item in (node_results or []) if isinstance(item, dict)]
    normalized_artifact_refs = dedupe_artifact_refs([dict(item) for item in (artifact_refs or []) if isinstance(item, dict)])
    normalized_metadata = dict(metadata or {})
    now = time.time()
    created = float(created_at or now)
    updated = float(updated_at or now)

    def _do(conn: sqlite3.Connection) -> Dict[str, Any]:
        existing = conn.execute(
            "SELECT created_at FROM team_mission_results WHERE mission_id = ?",
            (mission_id,),
        ).fetchone()
        conn.execute(
            """
            INSERT INTO team_mission_results (
                result_id, mission_id, activity_id, status, outcome, summary_text,
                node_results_json, artifact_refs_json, leader_report_run_id,
                leader_report_message_id, metadata_json, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(mission_id) DO UPDATE SET
                activity_id = excluded.activity_id,
                status = excluded.status,
                outcome = excluded.outcome,
                summary_text = excluded.summary_text,
                node_results_json = excluded.node_results_json,
                artifact_refs_json = excluded.artifact_refs_json,
                leader_report_run_id = COALESCE(NULLIF(excluded.leader_report_run_id, ''), leader_report_run_id),
                leader_report_message_id = COALESCE(NULLIF(excluded.leader_report_message_id, ''), leader_report_message_id),
                metadata_json = excluded.metadata_json,
                updated_at = excluded.updated_at
            """,
            (
                normalized_result_id,
                mission_id,
                normalized_activity_id,
                normalized_status,
                normalized_outcome,
                normalized_summary,
                _json_dumps(normalized_node_results),
                _json_dumps(normalized_artifact_refs),
                text(leader_report_run_id),
                text(leader_report_message_id),
                _json_dumps(normalized_metadata),
                float(_row_value(existing, "created_at", created) or created),
                updated,
            ),
        )
        return row_to_mission_result(conn.execute(
            "SELECT * FROM team_mission_results WHERE mission_id = ?",
            (mission_id,),
        ).fetchone())

    return db._execute_write(_do)


def get_team_mission_result(db: Any, mission_id: str) -> Dict[str, Any]:
    mission_id = text(mission_id)
    if not mission_id:
        return {}
    with db._lock:
        row = db._conn.execute(
            "SELECT * FROM team_mission_results WHERE mission_id = ?",
            (mission_id,),
        ).fetchone()
    return row_to_mission_result(row)
