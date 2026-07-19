"""Public Team Mission conversation identity projection.

The Team Mission store still uses ``conversation_id`` as an internal aggregate
key.  That key predates the unified conversation control plane and must not
escape through gateway responses.  Public callers identify the visible
conversation exclusively by ``conversation_session_id``.
"""

from __future__ import annotations

from typing import Any


def public_team_conversation(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    projected = dict(value)
    projected.pop("conversation_id", None)
    projected.pop("conversationId", None)
    conversation_session_id = str(
        projected.get("conversation_session_id")
        or projected.get("conversationSessionId")
        or projected.get("stable_session_id")
        or ""
    ).strip()
    if conversation_session_id:
        projected["conversation_session_id"] = conversation_session_id
        projected["conversationSessionId"] = conversation_session_id
    return projected


def public_team_conversation_result(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    projected = dict(value)
    projected.pop("conversation_id", None)
    projected.pop("conversationId", None)
    if isinstance(projected.get("conversation"), dict):
        projected["conversation"] = public_team_conversation(projected["conversation"])
    if isinstance(projected.get("graph"), dict):
        projected["graph"] = public_team_mission_graph(projected["graph"])
    return projected


def public_team_conversation_list(values: Any) -> list[dict[str, Any]]:
    if not isinstance(values, list):
        return []
    return [public_team_conversation(value) for value in values if isinstance(value, dict)]


def public_team_mission_graph(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    projected = dict(value)
    if isinstance(projected.get("conversation"), dict):
        projected["conversation"] = public_team_conversation(projected["conversation"])
    if isinstance(projected.get("mission"), dict):
        mission = dict(projected["mission"])
        mission.pop("conversation_id", None)
        mission.pop("conversationId", None)
        projected["mission"] = mission
    if isinstance(projected.get("missions"), list):
        projected["missions"] = [
            {
                key: item
                for key, item in mission.items()
                if key not in {"conversation_id", "conversationId"}
            }
            for mission in projected["missions"]
            if isinstance(mission, dict)
        ]
    return projected
