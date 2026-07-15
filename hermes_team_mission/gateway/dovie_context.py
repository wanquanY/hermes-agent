from __future__ import annotations

import logging

from .common import _dovie_product_context_from_params

_log = logging.getLogger(__name__)


def persist_mission_dovie_product_context_from_submit(db, mission: dict, params: dict) -> dict:
    """Persist the first Team Mission cloud-query context for node fallback."""
    if not isinstance(mission, dict) or not mission:
        return mission if isinstance(mission, dict) else {}
    mission_id = str(mission.get("mission_id") or mission.get("missionId") or "").strip()
    if not mission_id:
        return mission
    incoming_context = _dovie_product_context_from_params(params)
    if not incoming_context:
        return mission
    metadata = dict(mission.get("metadata") or {})
    existing_context = _dovie_product_context_from_params(
        {
            "dovie_product_context": (
                metadata.get("dovie_product_context")
                or metadata.get("dovieProductContext")
            )
        }
    )
    if existing_context:
        return mission
    next_metadata = {**metadata, "dovie_product_context": incoming_context}
    try:
        updated = db.upsert_team_mission(
            mission_id=mission_id,
            conversation_id=str(mission.get("conversation_id") or ""),
            team_id=str(mission.get("team_id") or ""),
            title=str(mission.get("title") or ""),
            objective=str(mission.get("objective") or ""),
            workspace_id=str(mission.get("workspace_id") or ""),
            workspace_path=str(mission.get("workspace_path") or ""),
            mode=str(mission.get("mode") or ""),
            status=str(mission.get("status") or ""),
            leader_session_id=str(mission.get("leader_session_id") or ""),
            metadata=next_metadata,
        )
    except Exception:
        _log.warning(
            "team_mission.dovie_context_metadata_persist_failed mission_id=%s",
            mission_id,
            exc_info=True,
        )
        return mission
    return updated if isinstance(updated, dict) and updated else {**mission, "metadata": next_metadata}
