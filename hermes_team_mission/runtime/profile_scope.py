"""Shared Team Mission profile tool helpers."""

from __future__ import annotations

import os
import json
import logging
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from hermes_team_mission.context.worker_context import cap_text
from hermes_team_mission.state.store import open_team_mission_state_store

_log = logging.getLogger(__name__)

_CONTROL_PLANE_GATEWAY_METHODS = frozenset({
    "team_mission.create",
    "team_mission.team_profile.get",
})


def text(value: Any) -> str:
    return str(value or "").strip()


def metadata(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def list_text(value: Any, limit: int = 6, item_limit: int = 220) -> list[str]:
    if not isinstance(value, list):
        return []
    items: list[str] = []
    for item in value:
        item_text = text(item)
        if item_text:
            items.append(cap_text(item_text, item_limit))
        if len(items) >= limit:
            break
    return items


def compact_radar_scores(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    scores: list[dict[str, Any]] = []
    for item in value[:8]:
        if not isinstance(item, Mapping):
            continue
        axis_id = text(item.get("axis_id") or item.get("axisId") or item.get("id"))
        label = text(item.get("label") or item.get("axis_label") or item.get("axisLabel") or axis_id)
        score = item.get("score", item.get("value", item.get("confidence")))
        try:
            numeric_score = round(float(score), 2)
        except Exception:
            continue
        scores.append({
            "axis_id": axis_id,
            "label": label,
            "score": numeric_score,
        })
    return scores


def compact_assignment_hints(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list):
        return []
    hints: list[dict[str, str]] = []
    for item in value[:3]:
        if not isinstance(item, Mapping):
            continue
        kind = text(item.get("kind") or item.get("type"))
        item_text = cap_text(item.get("text") or item.get("description") or item.get("hint"), 220)
        if kind or item_text:
            hints.append({"kind": kind, "text": item_text})
    return hints


def _json_size(value: Any) -> int:
    try:
        return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")))
    except Exception:
        return len(str(value or ""))


def _fit_snapshot_budget(snapshot: dict[str, Any], max_chars: int) -> dict[str, Any]:
    """Keep the LLM-facing profile snapshot under a hard JSON budget."""

    if _json_size(snapshot) <= max_chars:
        return snapshot
    result = dict(snapshot)
    team_profile = dict(result.get("team_profile") or {})
    for key in ("radar_scores", "risk_tasks", "limitations", "strengths"):
        team_profile.pop(key, None)
        result["team_profile"] = team_profile
        if _json_size(result) <= max_chars:
            return result
    members = [dict(member) for member in result.get("member_profiles") or [] if isinstance(member, Mapping)]
    removable_member_fields = (
        "radar_scores",
        "assignment_hints",
        "avoid_tasks",
        "limitations",
        "recommended_skills",
        "strengths",
        "default_toolsets",
        "best_for_tasks",
        "capability_tags",
    )
    for field in removable_member_fields:
        for member in members:
            member.pop(field, None)
        result["member_profiles"] = members
        if _json_size(result) <= max_chars:
            return result
    for description_limit in (240, 160, 100):
        for member in members:
            if member.get("profile_description"):
                member["profile_description"] = cap_text(member.get("profile_description"), description_limit)
        result["member_profiles"] = members
        if _json_size(result) <= max_chars:
            return result
    while len(members) > 1 and _json_size(result) > max_chars:
        members.pop()
        result["member_profiles"] = members
        result["returned_member_count"] = len(members)
        result["has_more_members"] = True
    return result


def _member_allowed(member: Mapping[str, Any], member_ids: set[str]) -> bool:
    if not member_ids:
        return True
    member_id = text(member.get("member_id") or member.get("memberId"))
    return member_id in member_ids


def compact_team_profile_snapshot(
    snapshot: Mapping[str, Any],
    *,
    detail: str = "assignment",
    member_ids: set[str] | None = None,
    limit: int = 12,
    max_chars: int = 6 * 1024,
) -> dict[str, Any]:
    """Return the bounded shape consumed by Leader turns and tool UI."""

    detail = text(detail).lower() or "assignment"
    if detail not in {"compact", "assignment", "full"}:
        detail = "assignment"
    member_ids = set(member_ids or set())
    try:
        limit = max(1, min(int(limit), 50))
    except Exception:
        limit = 12
    team_profile = snapshot.get("team_profile") if isinstance(snapshot.get("team_profile"), Mapping) else {}
    member_profiles = snapshot.get("member_profiles") if isinstance(snapshot.get("member_profiles"), list) else []
    compact_members: list[dict[str, Any]] = []
    for member in member_profiles:
        if not isinstance(member, Mapping):
            continue
        if not _member_allowed(member, member_ids):
            continue
        compact_member: dict[str, Any] = {
            "member_id": text(member.get("member_id") or member.get("memberId")),
            "agent_profile_id": text(member.get("agent_profile_id") or member.get("agentProfileId")),
            "display_name": cap_text(member.get("display_name") or member.get("displayName") or member.get("name"), 120),
            "role": cap_text(member.get("role") or member.get("role_label") or member.get("roleLabel"), 120),
            "profile_description": cap_text(
                member.get("profile_description") or member.get("description") or member.get("summary"),
                320 if detail == "assignment" else 700 if detail == "full" else 180,
            ),
            "capability_tags": list_text(member.get("capability_tags") or member.get("capabilityTags"), 5),
        }
        if detail in {"assignment", "full"}:
            compact_member.update({
                "default_toolsets": list_text(member.get("default_toolsets") or member.get("defaultToolsets"), 5),
                "recommended_skills": list_text(member.get("recommended_skills") or member.get("recommendedSkills"), 3),
                "strengths": list_text(member.get("strengths"), 3),
                "limitations": list_text(member.get("limitations"), 2),
                "best_for_tasks": list_text(member.get("best_for_tasks") or member.get("bestForTasks"), 3),
                "avoid_tasks": list_text(member.get("avoid_tasks") or member.get("avoidTasks"), 2),
                "assignment_hints": compact_assignment_hints(member.get("assignment_hints") or member.get("assignmentHints")),
            })
            if detail == "full":
                compact_member["radar_scores"] = compact_radar_scores(member.get("radar_scores") or member.get("radarScores"))
        max_concurrent = member.get("max_concurrent_nodes", member.get("maxConcurrentNodes"))
        try:
            compact_member["max_concurrent_nodes"] = int(max_concurrent)
        except Exception:
            pass
        compact_members.append({key: value for key, value in compact_member.items() if value not in ("", [], {})})
        if len(compact_members) >= limit:
            break

    result = {
        "snapshot_id": text(snapshot.get("snapshot_id") or snapshot.get("snapshotId")),
        "team_id": text(snapshot.get("team_id") or snapshot.get("teamId")),
        "version": snapshot.get("version"),
        "status": text(snapshot.get("status")),
        "detail": detail,
        "member_count": len(member_profiles),
        "returned_member_count": len(compact_members),
        "has_more_members": len(member_profiles) > len(compact_members) and not member_ids,
        "team_profile": {
            "display_name": cap_text(team_profile.get("display_name") or team_profile.get("displayName"), 160),
            "collaboration_mode": cap_text(team_profile.get("collaboration_mode") or team_profile.get("collaborationMode"), 160),
            "positioning": cap_text(
                team_profile.get("positioning")
                or team_profile.get("profile_description")
                or team_profile.get("description")
                or team_profile.get("summary"),
                700 if detail in {"assignment", "full"} else 320,
            ),
            "strengths": list_text(team_profile.get("strengths"), 4),
            "limitations": list_text(team_profile.get("limitations"), 3),
            "suitable_tasks": list_text(team_profile.get("suitable_tasks") or team_profile.get("suitableTasks"), 4),
            "risk_tasks": list_text(team_profile.get("risk_tasks") or team_profile.get("riskTasks"), 3),
            **({"radar_scores": compact_radar_scores(team_profile.get("radar_scores") or team_profile.get("radarScores"))} if detail == "full" else {}),
        },
        "member_profiles": compact_members,
    }
    return _fit_snapshot_budget(result, max_chars=max_chars)


def _worker_rpc_proxy() -> Any:
    try:
        from tui_gateway.services.worker_rpc_proxy import get_default_worker_rpc_proxy

        return get_default_worker_rpc_proxy()
    except Exception:
        return None


def gateway_call(method: str, params: dict[str, Any]) -> dict[str, Any]:
    normalized_method = text(method)
    payload = dict(params) if isinstance(params, Mapping) else {}
    proxy = _worker_rpc_proxy()
    if proxy is not None:
        if normalized_method not in _CONTROL_PLANE_GATEWAY_METHODS:
            return {
                "error": {
                    "message": (
                        f"Gateway method {normalized_method} is not allowed from "
                        "Team Mission worker runtime."
                    ),
                },
            }
        try:
            _log.info(
                "[dovie-team-mission-gateway-call] route=worker-ipc method=%s param_keys=%s",
                normalized_method,
                sorted(payload.keys()),
            )
            response = proxy.request(
                "worker.team_mission_gateway_call",
                {"method": normalized_method, "params": payload},
            )
            return response if isinstance(response, dict) else {"result": response}
        except Exception as exc:
            _log.warning(
                "[dovie-team-mission-gateway-call] route=worker-ipc failed method=%s error=%s",
                normalized_method,
                exc,
            )
            return {"error": {"message": str(exc)}}
    try:
        _log.info(
            "[dovie-team-mission-gateway-call] route=local method=%s param_keys=%s",
            normalized_method,
            sorted(payload.keys()),
        )
        from tui_gateway import server

        fn = server._methods.get(normalized_method)
        if not callable(fn):
            return {"error": {"message": f"Gateway method {normalized_method} is unavailable."}}
        return fn(None, payload)
    except Exception as exc:
        return {"error": {"message": str(exc)}}


def team_mission_control_home() -> str:
    return text(os.getenv("DOVIE_HERMES_CONTROL_HOME") or os.getenv("HERMES_HOME"))


def enter_team_mission_control_home() -> Any:
    control_home = team_mission_control_home()
    if not control_home:
        return None
    try:
        from hermes_constants import set_hermes_home_override

        return set_hermes_home_override(control_home)
    except Exception:
        return None


def leave_team_mission_control_home(token: Any) -> None:
    if token is None:
        return
    try:
        from hermes_constants import reset_hermes_home_override

        reset_hermes_home_override(token)
    except Exception:
        return


def team_mission_control_db(parent_agent: Any = None, *, create_if_missing: bool = True) -> Any:
    db = getattr(parent_agent, "_session_db", None) if parent_agent is not None else None
    if _is_worker_db_proxy(db):
        return db
    explicit_control_home = text(os.getenv("DOVIE_HERMES_CONTROL_HOME"))
    if explicit_control_home:
        try:
            db_path = Path(explicit_control_home).expanduser().resolve() / "state.db"
            if not create_if_missing and not db_path.exists():
                return None
            return open_team_mission_state_store(db_path)
        except Exception:
            pass
    if db is not None:
        return db
    try:
        from tui_gateway import server

        return server._get_db()
    except Exception:
        pass
    control_home = text(os.getenv("HERMES_HOME"))
    if control_home:
        try:
            db_path = Path(control_home).expanduser().resolve() / "state.db"
            if not create_if_missing and not db_path.exists():
                return None
            return open_team_mission_state_store(db_path)
        except Exception:
            pass
    if not create_if_missing:
        return None
    return open_team_mission_state_store()


def _is_worker_db_proxy(db: Any) -> bool:
    return str(getattr(db, "db_path", "") or "").startswith("worker-db-proxy")


def unwrap_response(response: dict[str, Any]) -> tuple[dict[str, Any], str]:
    if not isinstance(response, dict):
        return {}, "Gateway returned an invalid response."
    error = response.get("error")
    if isinstance(error, Mapping):
        return {}, text(error.get("message")) or "Gateway method failed."
    result = response.get("result")
    return (dict(result), "") if isinstance(result, Mapping) else ({}, "")
