"""Shared Team Mission profile tool helpers."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def text(value: Any) -> str:
    return str(value or "").strip()


def metadata(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def list_text(value: Any, limit: int = 6) -> list[str]:
    if not isinstance(value, list):
        return []
    items: list[str] = []
    for item in value:
        item_text = text(item)
        if item_text:
            items.append(item_text)
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
    for item in value[:6]:
        if not isinstance(item, Mapping):
            continue
        kind = text(item.get("kind") or item.get("type"))
        item_text = text(item.get("text") or item.get("description") or item.get("hint"))
        if kind or item_text:
            hints.append({"kind": kind, "text": item_text})
    return hints


def compact_team_profile_snapshot(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    """Return the bounded shape consumed by Leader turns and tool UI."""

    team_profile = snapshot.get("team_profile") if isinstance(snapshot.get("team_profile"), Mapping) else {}
    member_profiles = snapshot.get("member_profiles") if isinstance(snapshot.get("member_profiles"), list) else []
    compact_members: list[dict[str, Any]] = []
    for member in member_profiles:
        if not isinstance(member, Mapping):
            continue
        compact_member: dict[str, Any] = {
            "member_id": text(member.get("member_id") or member.get("memberId")),
            "agent_profile_id": text(member.get("agent_profile_id") or member.get("agentProfileId")),
            "display_name": text(member.get("display_name") or member.get("displayName") or member.get("name")),
            "role": text(member.get("role") or member.get("role_label") or member.get("roleLabel")),
            "profile_description": text(member.get("profile_description") or member.get("description") or member.get("summary")),
            "capability_tags": list_text(member.get("capability_tags") or member.get("capabilityTags"), 8),
            "default_toolsets": list_text(member.get("default_toolsets") or member.get("defaultToolsets"), 8),
            "recommended_skills": list_text(member.get("recommended_skills") or member.get("recommendedSkills"), 6),
            "strengths": list_text(member.get("strengths"), 6),
            "limitations": list_text(member.get("limitations"), 6),
            "best_for_tasks": list_text(member.get("best_for_tasks") or member.get("bestForTasks"), 6),
            "avoid_tasks": list_text(member.get("avoid_tasks") or member.get("avoidTasks"), 6),
            "assignment_hints": compact_assignment_hints(member.get("assignment_hints") or member.get("assignmentHints")),
            "radar_scores": compact_radar_scores(member.get("radar_scores") or member.get("radarScores")),
        }
        max_concurrent = member.get("max_concurrent_nodes", member.get("maxConcurrentNodes"))
        try:
            compact_member["max_concurrent_nodes"] = int(max_concurrent)
        except Exception:
            pass
        compact_members.append({key: value for key, value in compact_member.items() if value not in ("", [], {})})

    return {
        "snapshot_id": text(snapshot.get("snapshot_id") or snapshot.get("snapshotId")),
        "team_id": text(snapshot.get("team_id") or snapshot.get("teamId")),
        "version": snapshot.get("version"),
        "status": text(snapshot.get("status")),
        "team_profile": {
            "display_name": text(team_profile.get("display_name") or team_profile.get("displayName")),
            "collaboration_mode": text(team_profile.get("collaboration_mode") or team_profile.get("collaborationMode")),
            "positioning": text(
                team_profile.get("positioning")
                or team_profile.get("profile_description")
                or team_profile.get("description")
                or team_profile.get("summary")
            ),
            "strengths": list_text(team_profile.get("strengths"), 8),
            "limitations": list_text(team_profile.get("limitations"), 8),
            "suitable_tasks": list_text(team_profile.get("suitable_tasks") or team_profile.get("suitableTasks"), 8),
            "risk_tasks": list_text(team_profile.get("risk_tasks") or team_profile.get("riskTasks"), 8),
            "radar_scores": compact_radar_scores(team_profile.get("radar_scores") or team_profile.get("radarScores")),
        },
        "member_profiles": compact_members,
    }


def team_capability_payload(context: Mapping[str, Any]) -> dict[str, Any]:
    payload = context.get("team_capability") if isinstance(context.get("team_capability"), Mapping) else {}
    source_packet = (
        context.get("team_capability_source_packet")
        if isinstance(context.get("team_capability_source_packet"), Mapping)
        else {}
    )
    snapshot_id = text(context.get("team_capability_snapshot_id") or context.get("teamCapabilitySnapshotId"))
    result = dict(payload)
    if source_packet:
        result.setdefault("source_packet", dict(source_packet))
    if snapshot_id:
        result.setdefault("snapshot_id", snapshot_id)
    return result


def gateway_call(method: str, params: dict[str, Any]) -> dict[str, Any]:
    try:
        from tui_gateway import server

        fn = server._methods.get(method)
        if not callable(fn):
            return {"error": {"message": f"Gateway method {method} is unavailable."}}
        return fn(None, params)
    except Exception as exc:
        return {"error": {"message": str(exc)}}


def unwrap_response(response: dict[str, Any]) -> tuple[dict[str, Any], str]:
    if not isinstance(response, dict):
        return {}, "Gateway returned an invalid response."
    error = response.get("error")
    if isinstance(error, Mapping):
        return {}, text(error.get("message")) or "Gateway method failed."
    result = response.get("result")
    return (dict(result), "") if isinstance(result, Mapping) else ({}, "")
