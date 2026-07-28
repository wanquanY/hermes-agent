from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any


DEFAULT_CAPABILITY_AXES: tuple[dict[str, str], ...] = (
    {"axis_id": "product", "label": "需求/产品", "description": "需求澄清、产品判断、任务定义和方案取舍。"},
    {"axis_id": "research", "label": "研究检索", "description": "信息检索、资料筛选、事实整理和趋势分析。"},
    {"axis_id": "engineering", "label": "工程实现", "description": "代码实现、文件修改、命令执行和技术集成。"},
    {"axis_id": "data", "label": "数据分析", "description": "结构化数据处理、统计、表格和指标分析。"},
    {"axis_id": "writing", "label": "写作表达", "description": "文档、报告、总结、表达结构和交付包装。"},
    {"axis_id": "quality", "label": "质量验收", "description": "测试、审查、事实核验、验收和风险识别。"},
    {"axis_id": "automation", "label": "工具自动化", "description": "脚本、工具链、自动化流程和跨工具编排。"},
)


_AXIS_KEYWORDS: dict[str, tuple[str, ...]] = {
    "product": (
        "product", "pm", "planner", "planning", "strategy", "requirement", "requirements",
        "需求", "产品", "规划", "策略", "方案",
    ),
    "research": (
        "research", "search", "web", "browser", "analysis", "analyst", "investigate",
        "调研", "研究", "检索", "搜索", "资料", "趋势",
    ),
    "engineering": (
        "engineer", "developer", "coder", "code", "terminal", "file", "git", "python", "typescript",
        "工程", "开发", "编码", "代码", "实现", "脚本",
    ),
    "data": (
        "data", "analytics", "sql", "spreadsheet", "excel", "table", "metric", "statistics",
        "数据", "分析", "表格", "指标", "统计",
    ),
    "writing": (
        "writer", "writing", "doc", "document", "report", "content", "editor", "summary",
        "写作", "文档", "报告", "内容", "总结", "表达",
    ),
    "quality": (
        "qa", "quality", "review", "test", "verify", "verification", "checker", "acceptance",
        "质检", "质量", "审查", "测试", "验证", "验收", "校验",
    ),
    "automation": (
        "automation", "workflow", "tool", "tools", "script", "scheduler", "integration",
        "自动化", "工作流", "工具", "集成", "编排",
    ),
}


_ROLE_AXIS_HINTS: dict[str, tuple[str, ...]] = {
    "leader": ("product", "quality", "writing"),
    "lead": ("product", "quality", "writing"),
    "planner": ("product", "writing"),
    "product": ("product", "writing"),
    "researcher": ("research", "data", "writing"),
    "analyst": ("research", "data"),
    "engineer": ("engineering", "automation", "quality"),
    "developer": ("engineering", "automation"),
    "builder": ("engineering", "automation"),
    "reviewer": ("quality", "writing"),
    "tester": ("quality", "engineering"),
    "writer": ("writing", "research"),
    "synthesizer": ("writing", "product", "research"),
}


def text(value: Any) -> str:
    return str(value or "").strip()


def normalize_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        raw_items: Sequence[Any] = value.replace("\n", ",").split(",")
    elif isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        raw_items = value
    else:
        raw_items = (value,)
    result: list[str] = []
    seen: set[str] = set()
    for item in raw_items:
        normalized = text(item)
        if normalized and normalized not in seen:
            seen.add(normalized)
            result.append(normalized)
    return result


def normalize_mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def source_digest(source_packet: Mapping[str, Any]) -> str:
    return hashlib.sha256(stable_json(normalize_source_packet(source_packet)).encode("utf-8")).hexdigest()


def normalize_source_packet(source_packet: Mapping[str, Any] | None) -> dict[str, Any]:
    raw = normalize_mapping(source_packet)
    team = normalize_mapping(raw.get("team"))
    members = raw.get("members")
    normalized_members: list[dict[str, Any]] = []
    for item in members if isinstance(members, Sequence) and not isinstance(members, (str, bytes)) else []:
        member = normalize_mapping(item)
        profile = normalize_mapping(member.get("profile"))
        normalized_members.append({
            "memberId": text(member.get("memberId") or member.get("member_id")),
            "agentProfileId": text(member.get("agentProfileId") or member.get("agent_profile_id") or member.get("profile_id")),
            "agentProfileVersionId": text(
                member.get("agentProfileVersionId")
                or member.get("agent_profile_version_id")
                or member.get("profile_version_id")
            ),
            "displayName": text(member.get("displayName") or member.get("display_name") or member.get("name")),
            "avatar": text(member.get("avatar")),
            "role": text(member.get("role") or "member"),
            "capabilityTags": normalize_list(member.get("capabilityTags") or member.get("capability_tags")),
            "autoAssignable": member.get("autoAssignable", member.get("auto_assignable", True)) is not False,
            "maxConcurrentNodes": max(1, int(member.get("maxConcurrentNodes") or member.get("max_concurrent_nodes") or 1)),
            "permissionMode": text(member.get("permissionMode") or member.get("permission_mode")),
            "profile": {
                "description": text(profile.get("description") or member.get("profileDescription") or member.get("profile_description")),
                "category": text(profile.get("category")),
                "tags": normalize_list(profile.get("tags")),
                "defaultToolsets": normalize_list(profile.get("defaultToolsets") or profile.get("default_toolsets")),
                "recommendedSkills": normalize_list(profile.get("recommendedSkills") or profile.get("recommended_skills")),
                "soulDigest": text(profile.get("soulDigest") or profile.get("soul_digest")),
                "soulExcerpt": text(profile.get("soulExcerpt") or profile.get("soul_excerpt")),
            },
            "userOverrides": [
                normalize_mapping(override)
                for override in (member.get("userOverrides") or member.get("user_overrides") or [])
                if isinstance(override, Mapping)
            ],
        })
    return {
        "teamId": text(raw.get("teamId") or raw.get("team_id")),
        "teamRevision": text(raw.get("teamRevision") or raw.get("team_revision")),
        "team": {
            "name": text(team.get("name") or raw.get("teamName") or raw.get("team_name")),
            "description": text(team.get("description") or raw.get("teamDescription") or raw.get("team_description")),
            "defaultMode": text(team.get("defaultMode") or team.get("default_mode") or raw.get("defaultMode") or raw.get("default_mode")),
            "policy": normalize_mapping(team.get("policy") or raw.get("policy")),
        },
        "members": normalized_members,
    }


def build_snapshot_view(
    *,
    snapshot_id: str,
    team_id: str,
    version: int,
    source_packet: Mapping[str, Any],
    digest: str = "",
    status: str = "ready",
    stale_reason: str = "",
    generated_at: float = 0,
    updated_at: float = 0,
) -> dict[str, Any]:
    packet = normalize_source_packet(source_packet)
    resolved_team_id = text(team_id or packet.get("teamId"))
    resolved_digest = text(digest) or source_digest(packet)
    members = [_member_profile(member) for member in packet.get("members", []) if isinstance(member, Mapping)]
    axes = [dict(axis) for axis in DEFAULT_CAPABILITY_AXES]
    evidence_refs = _team_evidence_refs(packet, members)
    return {
        "snapshot_id": text(snapshot_id),
        "team_id": resolved_team_id,
        "version": int(version),
        "status": text(status) or "ready",
        "source_digest": resolved_digest,
        "source_packet_digest": resolved_digest,
        "team_profile": _team_profile(packet, members, evidence_refs),
        "member_profiles": members,
        "capability_axes": axes,
        "assignment_policy": _assignment_policy(packet, members),
        "evidence_refs": evidence_refs,
        "stale_reason": text(stale_reason),
        "generated_at": float(generated_at or 0),
        "updated_at": float(updated_at or 0),
    }


def snapshot_storage_fields(snapshot: Mapping[str, Any]) -> dict[str, str]:
    return {
        "team_profile_json": stable_json(snapshot.get("team_profile") or {}),
        "member_profiles_json": stable_json(snapshot.get("member_profiles") or []),
        "capability_axes_json": stable_json(snapshot.get("capability_axes") or []),
        "assignment_policy_json": stable_json(snapshot.get("assignment_policy") or {}),
        "evidence_refs_json": stable_json(snapshot.get("evidence_refs") or []),
    }


def snapshot_from_storage(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "snapshot_id": text(row.get("snapshot_id")),
        "team_id": text(row.get("team_id")),
        "version": int(row.get("version") or 0),
        "status": text(row.get("status") or "ready"),
        "source_digest": text(row.get("source_digest")),
        "source_packet_digest": text(row.get("source_packet_digest")),
        "team_profile": _loads(row.get("team_profile_json"), {}),
        "member_profiles": _loads(row.get("member_profiles_json"), []),
        "capability_axes": _loads(row.get("capability_axes_json"), []),
        "assignment_policy": _loads(row.get("assignment_policy_json"), {}),
        "evidence_refs": _loads(row.get("evidence_refs_json"), []),
        "stale_reason": text(row.get("stale_reason")),
        "generated_at": float(row.get("generated_at") or 0),
        "updated_at": float(row.get("updated_at") or 0),
    }


def _loads(value: Any, fallback: Any) -> Any:
    if not value:
        return fallback
    try:
        return json.loads(str(value))
    except Exception:
        return fallback


def _member_profile(member: Mapping[str, Any]) -> dict[str, Any]:
    profile = normalize_mapping(member.get("profile"))
    role = text(member.get("role") or "member")
    tags = normalize_list(member.get("capabilityTags")) + normalize_list(profile.get("tags"))
    toolsets = normalize_list(profile.get("defaultToolsets"))
    skills = normalize_list(profile.get("recommendedSkills"))
    description = text(profile.get("description"))
    soul_excerpt = text(profile.get("soulExcerpt"))
    search_text = " ".join([role, description, soul_excerpt, " ".join(tags), " ".join(toolsets), " ".join(skills)]).lower()
    radar_scores = [_radar_score(axis, role=role, search_text=search_text, tags=tags, toolsets=toolsets, skills=skills) for axis in DEFAULT_CAPABILITY_AXES]
    sorted_scores = sorted(radar_scores, key=lambda item: item["score"], reverse=True)
    best_axes = [score["label"] for score in sorted_scores if score["score"] >= 3][:3]
    weak_axes = [score["label"] for score in sorted_scores if score["score"] <= 2][:2]
    display_name = text(member.get("displayName") or member.get("memberId") or member.get("agentProfileId") or "member")
    evidence_refs = _member_evidence_refs(member)
    best_for = _task_hints(best_axes, role=role)
    avoid = _avoid_hints(weak_axes)
    return {
        "member_id": text(member.get("memberId")),
        "agent_profile_id": text(member.get("agentProfileId")),
        "agent_profile_version_id": text(member.get("agentProfileVersionId")),
        "display_name": display_name,
        "avatar": text(member.get("avatar")),
        "team_role": role,
        "profile_description": description,
        "capability_tags": normalize_list(member.get("capabilityTags")),
        "default_toolsets": toolsets,
        "recommended_skills": skills,
        "permission_mode": text(member.get("permissionMode")),
        "auto_assignable": member.get("autoAssignable") is not False,
        "max_concurrent_nodes": max(1, int(member.get("maxConcurrentNodes") or 1)),
        "strengths": best_axes,
        "limitations": weak_axes,
        "best_for_tasks": best_for,
        "avoid_tasks": avoid,
        "radar_scores": radar_scores,
        "assignment_hints": [
            {"kind": "best_for", "text": hint}
            for hint in best_for
        ] + [
            {"kind": "avoid", "text": hint}
            for hint in avoid
        ],
        "evidence_refs": evidence_refs,
    }


def _radar_score(
    axis: Mapping[str, str],
    *,
    role: str,
    search_text: str,
    tags: Sequence[str],
    toolsets: Sequence[str],
    skills: Sequence[str],
) -> dict[str, Any]:
    axis_id = text(axis.get("axis_id"))
    score = 1.0
    reasons: list[str] = []
    if axis_id in _ROLE_AXIS_HINTS.get(role.lower(), ()):
        score += 1.5
        reasons.append(f"team role '{role}' maps to this capability")
    keywords = _AXIS_KEYWORDS.get(axis_id, ())
    matches = [keyword for keyword in keywords if keyword.lower() in search_text]
    if matches:
        score += min(2.0, 0.5 * len(matches))
        reasons.append("matched tags/profile/tool keywords: " + ", ".join(matches[:5]))
    if axis_id == "engineering" and any(item in {"terminal", "file", "git"} for item in (name.lower() for name in toolsets)):
        score += 0.75
        reasons.append("default toolsets include implementation tools")
    if axis_id == "automation" and any("automation" in name.lower() or "workflow" in name.lower() for name in skills + toolsets):
        score += 0.75
        reasons.append("skills/toolsets indicate workflow automation")
    final_score = max(1.0, min(5.0, round(score, 1)))
    confidence = max(0.2, min(1.0, round(0.25 + (len(matches) * 0.12) + (0.2 if reasons else 0), 2)))
    return {
        "axis_id": axis_id,
        "label": text(axis.get("label")),
        "score": final_score,
        "max_score": 5,
        "confidence": confidence,
        "reason": "; ".join(reasons) or "limited structured evidence; default baseline score",
        "evidence_refs": [],
    }


def _member_evidence_refs(member: Mapping[str, Any]) -> list[dict[str, str]]:
    refs: list[dict[str, str]] = []
    member_id = text(member.get("memberId"))
    profile = normalize_mapping(member.get("profile"))
    for source, value, label in (
        ("team.member.role", member.get("role"), "Team role"),
        ("team.member.capability_tags", ", ".join(normalize_list(member.get("capabilityTags"))), "Capability tags"),
        ("agent_profile.description", profile.get("description"), "Profile description"),
        ("agent_profile.tags", ", ".join(normalize_list(profile.get("tags"))), "Profile tags"),
        ("agent_profile.default_toolsets", ", ".join(normalize_list(profile.get("defaultToolsets"))), "Default toolsets"),
        ("agent_profile.recommended_skills", ", ".join(normalize_list(profile.get("recommendedSkills"))), "Recommended skills"),
        ("agent_profile.soul_excerpt", profile.get("soulExcerpt"), "SOUL excerpt"),
    ):
        excerpt = text(value)
        if excerpt:
            refs.append({"source": source, "source_id": member_id, "label": label, "excerpt": excerpt[:280]})
    return refs


def _team_evidence_refs(packet: Mapping[str, Any], members: Sequence[Mapping[str, Any]]) -> list[dict[str, str]]:
    team = normalize_mapping(packet.get("team"))
    refs = []
    if text(team.get("description")):
        refs.append({
            "source": "team.description",
            "source_id": text(packet.get("teamId")),
            "label": "Team description",
            "excerpt": text(team.get("description"))[:280],
        })
    for member in members:
        refs.extend(member.get("evidence_refs") or [])
    return refs


def _team_profile(packet: Mapping[str, Any], members: Sequence[Mapping[str, Any]], evidence_refs: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    team = normalize_mapping(packet.get("team"))
    strengths = _team_strengths(members)
    gaps = _team_gaps(members)
    return {
        "summary": text(team.get("description")) or _team_summary(packet, members),
        "collaboration_mode": text(team.get("defaultMode")) or "supervised_mission",
        "radar_scores": _team_radar_scores(members),
        "strengths": strengths,
        "gaps": gaps,
        "best_for_tasks": _task_hints(strengths),
        "risky_for_tasks": _avoid_hints(gaps),
        "default_planning_guidelines": [
            "Use the member with the strongest matching capability for each executable node.",
            "Use Leader-owned nodes for orchestration, verification approval, and final synthesis when no member has a stronger fit.",
            "Respect auto_assignable, permission_mode, and max_concurrent_nodes when planning execution.",
        ],
        "evidence_refs": list(evidence_refs)[:30],
    }


def _assignment_policy(packet: Mapping[str, Any], members: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    team = normalize_mapping(packet.get("team"))
    policy = normalize_mapping(team.get("policy"))
    return {
        "max_parallel_nodes": int(policy.get("maxParallelNodes") or policy.get("max_parallel_nodes") or 3),
        "requires_explicit_owner": True,
        "auto_assignable_member_ids": [
            text(member.get("member_id"))
            for member in members
            if member.get("auto_assignable") is not False and text(member.get("member_id"))
        ],
        "owner_selection": "capability_snapshot_then_explicit_leader_fallback",
    }


def _team_summary(packet: Mapping[str, Any], members: Sequence[Mapping[str, Any]]) -> str:
    team = normalize_mapping(packet.get("team"))
    name = text(team.get("name")) or "Team"
    roles = ", ".join(text(member.get("team_role")) for member in members if text(member.get("team_role")))
    return f"{name} is configured for Team Mission collaboration" + (f" with roles: {roles}." if roles else ".")


def _team_strengths(members: Sequence[Mapping[str, Any]]) -> list[str]:
    counts: dict[str, int] = {}
    for member in members:
        for item in member.get("strengths") or []:
            label = text(item)
            if label:
                counts[label] = counts.get(label, 0) + 1
    return [item for item, _count in sorted(counts.items(), key=lambda pair: (-pair[1], pair[0]))[:5]]


def _team_gaps(members: Sequence[Mapping[str, Any]]) -> list[str]:
    if not members:
        return [axis["label"] for axis in DEFAULT_CAPABILITY_AXES]
    covered = set()
    for member in members:
        for score in member.get("radar_scores") or []:
            if isinstance(score, Mapping) and float(score.get("score") or 0) >= 3:
                covered.add(text(score.get("label")))
    return [axis["label"] for axis in DEFAULT_CAPABILITY_AXES if axis["label"] not in covered][:4]


def _team_radar_scores(members: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for axis in DEFAULT_CAPABILITY_AXES:
        axis_id = text(axis.get("axis_id"))
        scores = []
        confidences = []
        reasons = []
        for member in members:
            for score in member.get("radar_scores") or []:
                if not isinstance(score, Mapping) or text(score.get("axis_id")) != axis_id:
                    continue
                scores.append(float(score.get("score") or 0))
                confidences.append(float(score.get("confidence") or 0))
                reason = text(score.get("reason"))
                if reason:
                    reasons.append(reason)
        result.append({
            "axis_id": axis_id,
            "label": text(axis.get("label")),
            "score": round(sum(scores) / len(scores), 1) if scores else 1.0,
            "max_score": 5,
            "confidence": round(sum(confidences) / len(confidences), 2) if confidences else 0.2,
            "reason": "; ".join(reasons[:3]) if reasons else "limited structured evidence; default team baseline score",
            "evidence_refs": [],
        })
    return result


def _task_hints(labels: Sequence[str], *, role: str = "") -> list[str]:
    hints = []
    for label in labels:
        if label:
            hints.append(f"适合承担{label}相关任务")
    if role and not hints:
        hints.append(f"适合承担 {role} 职责范围内的任务")
    return hints[:4]


def _avoid_hints(labels: Sequence[str]) -> list[str]:
    return [f"证据不足时避免优先承担{label}主责节点" for label in labels if label][:4]
