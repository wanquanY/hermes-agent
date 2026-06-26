from __future__ import annotations

from collections.abc import Mapping
from typing import Any


CANONICAL_TEAM_MISSION_NODE_KINDS = {
    "root",
    "worker",
    "discussion",
    "verifier",
    "synthesis",
    "approval_gate",
}

TEAM_MISSION_CONTROL_NODE_KINDS = {"root", "approval_gate", "verifier", "synthesis"}
TEAM_MISSION_WORK_NODE_KIND = "worker"
TEAM_MISSION_SYNTHESIS_NODE_KIND = "synthesis"
TEAM_MISSION_VERIFIER_NODE_KIND = "verifier"

_CONTROL_KIND_ALIASES = {
    "approval": "approval_gate",
    "approval-gate": "approval_gate",
    "approval gate": "approval_gate",
    "approvalgate": "approval_gate",
    "final": "synthesis",
    "finalizer": "synthesis",
    "finalise": "synthesis",
    "finalize": "synthesis",
    "synthesizer": "synthesis",
    "summary": "synthesis",
    "verify": "verifier",
    "verification_gate": "verifier",
    "verification-gate": "verifier",
}

_WORK_TYPE_KIND_ALIASES = {
    "analysis",
    "coding",
    "code",
    "engineering",
    "implementation",
    "implementer",
    "planning",
    "qa",
    "quality",
    "research",
    "review",
    "test",
    "testing",
    "validation",
    "verification",
    "writer",
    "writing",
}


def _text(value: Any) -> str:
    return str(value or "").strip()


def normalize_team_mission_node_kind(value: Any, *, default: str = TEAM_MISSION_WORK_NODE_KIND) -> str:
    kind = _text(value).lower().replace(" ", "_")
    if not kind:
        return default
    if kind in CANONICAL_TEAM_MISSION_NODE_KINDS:
        return kind
    if kind in _CONTROL_KIND_ALIASES:
        return _CONTROL_KIND_ALIASES[kind]
    if kind in _WORK_TYPE_KIND_ALIASES:
        return TEAM_MISSION_WORK_NODE_KIND
    return default


def team_mission_node_work_type(value: Any) -> str:
    raw = _text(value).lower().replace(" ", "_")
    if not raw:
        return ""
    canonical = normalize_team_mission_node_kind(raw)
    if canonical == TEAM_MISSION_WORK_NODE_KIND and raw != TEAM_MISSION_WORK_NODE_KIND:
        return raw
    return ""


def metadata_with_normalized_node_kind(
    metadata: Mapping[str, Any] | None,
    *,
    raw_kind: Any,
    canonical_kind: str,
) -> dict[str, Any]:
    normalized = dict(metadata or {})
    raw = _text(raw_kind)
    if not raw:
        return normalized
    if raw != canonical_kind:
        normalized.setdefault("original_kind", raw)
        normalized.setdefault("originalKind", raw)
    work_type = team_mission_node_work_type(raw)
    if canonical_kind == TEAM_MISSION_WORK_NODE_KIND and work_type:
        normalized.setdefault("work_type", work_type)
        normalized.setdefault("workType", work_type)
    return normalized


def is_team_mission_control_node_kind(value: Any) -> bool:
    return normalize_team_mission_node_kind(value) in TEAM_MISSION_CONTROL_NODE_KINDS


def is_team_mission_synthesis_node_kind(value: Any) -> bool:
    return normalize_team_mission_node_kind(value) == TEAM_MISSION_SYNTHESIS_NODE_KIND
