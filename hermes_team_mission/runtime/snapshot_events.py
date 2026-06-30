from __future__ import annotations

from typing import Any


def _text(value: Any) -> str:
    return str(value or "").strip()


def append_team_mission_snapshot_updated(
    db: Any,
    *,
    mission_id: str,
    reason: str,
    node_id: str = "",
    run_id: str = "",
    status: str = "",
    result_id: str = "",
) -> dict[str, Any]:
    """Emit the canonical invalidation event for Team Mission snapshot readers."""
    mission_id = _text(mission_id)
    reason = _text(reason)
    if not mission_id or not reason:
        return {}
    append = getattr(db, "append_team_mission_structural_event", None)
    if not callable(append):
        return {}
    payload: dict[str, Any] = {
        "mission_id": mission_id,
        "missionId": mission_id,
        "activity_id": f"mission:{mission_id}",
        "activityId": f"mission:{mission_id}",
        "reason": reason,
    }
    if node_id:
        payload["node_id"] = _text(node_id)
        payload["nodeId"] = _text(node_id)
    if run_id:
        payload["run_id"] = _text(run_id)
        payload["runId"] = _text(run_id)
    if status:
        payload["status"] = _text(status)
    if result_id:
        payload["result_id"] = _text(result_id)
        payload["resultId"] = _text(result_id)
    source_event: dict[str, Any] = {
        "type": "mission.snapshot.updated",
        "payload": payload,
    }
    if run_id:
        source_event["run_id"] = _text(run_id)
    dedupe_key = ":".join(
        [
            "snapshot-updated",
            mission_id,
            reason,
            _text(node_id),
            _text(run_id),
            _text(status),
            _text(result_id),
        ]
    )
    return append(
        mission_id=mission_id,
        source_event=source_event,
        identity={"mission_id": mission_id},
        dedupe_key=dedupe_key,
    )
