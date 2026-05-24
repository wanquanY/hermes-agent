"""Doxie agent profile design bridge tools.

The Hermes tool exposes the product-level 分身设计 workflow. Models first inspect
real local catalog data, then emit structured draft requests. Doxie owns
persistence, preview, sandbox chat, and publishing in the desktop product layer.
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from tools.registry import registry, tool_error, tool_result

AGENT_PROFILE_CATEGORIES = ["工作", "学习", "创作", "开发", "生活", "其他"]
AGENT_PROFILE_CATEGORY_ALIASES = {
    "work": "工作",
    "office": "工作",
    "productivity": "工作",
    "business": "工作",
    "product": "工作",
    "pm": "工作",
    "prd": "工作",
    "operations": "工作",
    "operation": "工作",
    "marketing": "工作",
    "market": "工作",
    "sales": "工作",
    "legal": "工作",
    "finance": "工作",
    "management": "工作",
    "project": "工作",
    "办公": "工作",
    "产品": "工作",
    "产品经理": "工作",
    "运营": "工作",
    "市场": "工作",
    "销售": "工作",
    "法务": "工作",
    "财务": "工作",
    "项目管理": "工作",
    "study": "学习",
    "learning": "学习",
    "education": "学习",
    "academic": "学习",
    "research": "学习",
    "reading": "学习",
    "reader": "学习",
    "book": "学习",
    "course": "学习",
    "paper": "学习",
    "knowledge": "学习",
    "教育": "学习",
    "研究": "学习",
    "阅读": "学习",
    "读书": "学习",
    "课程": "学习",
    "论文": "学习",
    "知识": "学习",
    "creative": "创作",
    "creation": "创作",
    "writing": "创作",
    "writer": "创作",
    "copywriting": "创作",
    "content": "创作",
    "design": "创作",
    "image": "创作",
    "video": "创作",
    "media": "创作",
    "novel": "创作",
    "写作": "创作",
    "文案": "创作",
    "内容": "创作",
    "设计": "创作",
    "图片": "创作",
    "视频": "创作",
    "小说": "创作",
    "development": "开发",
    "develop": "开发",
    "dev": "开发",
    "coding": "开发",
    "coder": "开发",
    "code": "开发",
    "programming": "开发",
    "engineering": "开发",
    "software": "开发",
    "devops": "开发",
    "data": "开发",
    "analysis": "开发",
    "编程": "开发",
    "代码": "开发",
    "工程": "开发",
    "软件": "开发",
    "数据": "开发",
    "数据分析": "开发",
    "life": "生活",
    "lifestyle": "生活",
    "personal": "生活",
    "health": "生活",
    "travel": "生活",
    "food": "生活",
    "shopping": "生活",
    "family": "生活",
    "habit": "生活",
    "emotion": "生活",
    "emotions": "生活",
    "relationship": "生活",
    "个人": "生活",
    "健康": "生活",
    "旅行": "生活",
    "饮食": "生活",
    "购物": "生活",
    "家庭": "生活",
    "习惯": "生活",
    "情感": "生活",
    "other": "其他",
    "custom": "其他",
    "general": "其他",
    "misc": "其他",
    "uncategorized": "其他",
    "自定义": "其他",
    "通用": "其他",
}


def _normalize_agent_profile_category(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return "其他"
    if raw in AGENT_PROFILE_CATEGORIES:
        return raw
    key = re.sub(r"[\s_-]+", "", raw.lower())
    return AGENT_PROFILE_CATEGORY_ALIASES.get(raw.lower()) or AGENT_PROFILE_CATEGORY_ALIASES.get(key) or AGENT_PROFILE_CATEGORY_ALIASES.get(raw) or "其他"


def _backend_bridge_config() -> tuple[str, str]:
    return (
        os.getenv("DOXIE_BACKEND_BRIDGE_URL", "").strip(),
        os.getenv("DOXIE_BACKEND_BRIDGE_TOKEN", "").strip(),
    )


def _backend_bridge_available() -> bool:
    url, token = _backend_bridge_config()
    return bool(url and token)


def _backend_call(command: str, payload: dict | None = None) -> Any:
    url, token = _backend_bridge_config()
    if not url or not token:
        raise RuntimeError("Doxie backend bridge is not available.")
    body = json.dumps({"command": command, "payload": payload or {}}, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "authorization": f"Bearer {token}",
            "content-type": "application/json; charset=utf-8",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            data = json.loads(response.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Doxie backend bridge rejected {command}: {detail}") from exc
    if not isinstance(data, dict) or not data.get("ok"):
        raise RuntimeError(str(data.get("error") if isinstance(data, dict) else data))
    return data.get("value")


def _session_design_context() -> dict:
    try:
        from gateway.session_context import get_session_env

        raw = get_session_env("HERMES_DOXIE_PRODUCT_CONTEXT", "")
    except Exception:
        raw = ""
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _contextual_payload(payload: dict | None = None) -> dict:
    context = _session_design_context()
    result = dict(payload or {})
    mapping = {
        "designMode": "designMode",
        "sourceSessionId": "sourceSessionId",
        "sourceAgentProfileId": "sourceAgentProfileId",
        "sourceRunId": "sourceRunId",
        "sourceTurnId": "sourceTurnId",
        "sourceClientMessageId": "sourceClientMessageId",
        "workspaceId": "workspaceId",
        "activeDraftId": "activeDraftId",
        "targetAgentProfileId": "targetAgentProfileId",
    }
    for source_key, target_key in mapping.items():
        value = str(context.get(source_key) or "").strip()
        if value and not str(result.get(target_key) or "").strip():
            result[target_key] = value
    return result


def _draft_targets_profile(draft_id: str, target_agent_profile_id: str) -> bool:
    if not draft_id or not target_agent_profile_id or not _backend_bridge_available():
        return False
    try:
        draft = _backend_call("doxie_agent_profile_draft_get", {"draftId": draft_id})
    except Exception:
        return False
    if not isinstance(draft, dict):
        return False
    return _first_non_empty(
        draft.get("targetAgentProfileId"),
        draft.get("baseAgentProfileId"),
        draft.get("publishedAgentProfileId"),
    ) == target_agent_profile_id


def _first_non_empty(*values: Any) -> str:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""


DESIGN_AGENT_PROFILE_SCHEMA = {
    "name": "design_agent_profile",
    "description": (
        "Design a Doxie agent profile draft through the product-level 分身设计 workflow. "
        "Use this when the user asks to create or revise a persona/agent/profile/分身. "
        "First call operation=inspect_context to get real Doxie templates, "
        "Hermes toolsets, avatar assets, and workflow rules. Use the Hermes "
        "skills toolset (skills_list/skill_view/skill_manage) as the authority "
        "for skill catalog and creation. Then create or update the draft using "
        "only real toolsets and verified skills. Put unavailable capabilities into "
        "missing_capabilities or skill_creation_plans instead of inventing enabled "
        "tools or skills. Doxie will open the right-side creation workspace for "
        "preview, test chat, editing, validation, testing, and publishing. "
        "Use operation=install_skill only after a skill is really created or "
        "installed in Hermes and the user wants it added to a draft profile."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "operation": {
                "type": "string",
                "enum": ["inspect_context", "create", "update", "upsert", "install_skill"],
                "description": "Use inspect_context first to retrieve real design catalog data. Use create/update/upsert for draft changes. Use install_skill to install an existing Hermes skill into a target draft profile.",
            },
            "catalog_kind": {
                "type": "string",
                "enum": ["overview", "toolsets", "skills", "templates", "avatars"],
                "description": "When operation=inspect_context, choose which catalog slice to inspect. Default overview returns only compact hints.",
            },
            "query": {
                "type": "string",
                "description": "Optional case-insensitive search text for inspect_context catalog slices.",
            },
            "limit": {
                "type": "integer",
                "description": "Optional maximum number of catalog entries to return for inspect_context. Defaults to a compact page.",
            },
            "draft_id": {
                "type": "string",
                "description": "Existing Doxie draft id to update. Leave empty only when creating a brand-new agent profile draft.",
            },
            "draft_reference": {
                "type": "string",
                "description": "Optional human-readable draft reference when installing a skill or testing and draft_id is unknown.",
            },
            "skill_name": {
                "type": "string",
                "description": "Existing Hermes skill name to install into the target draft when operation=install_skill.",
            },
            "name": {
                "type": "string",
                "description": "Short display name for the new agent profile.",
            },
            "description": {
                "type": "string",
                "description": "Concise card description of what this agent profile is for.",
            },
            "avatar": {
                "type": "string",
                "description": "Optional avatar text, emoji, or image URL.",
            },
            "category": {
                "type": "string",
                "enum": AGENT_PROFILE_CATEGORIES,
                "description": "分身固定分类。只能选择：工作、学习、创作、开发、生活、其他。",
            },
            "tags": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Short searchable tags.",
            },
            "recommended_toolsets": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Hermes toolsets this profile should use, for example terminal, file, web, browser, code_execution, image_gen, skills, memory.",
            },
            "recommended_skills": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Installed skills that should be recommended for this profile. Only include skills verified through skills_list or skill_view.",
            },
            "architecture_template_id": {
                "type": "string",
                "description": "Optional Doxie architecture template id used to shape this profile.",
            },
            "skill_creation_plans": {
                "type": "array",
                "description": "New skills that should be created later with user approval when no installed or marketplace skill fits.",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "name": {"type": "string"},
                        "description": {"type": "string"},
                        "capabilitySpec": {"type": "string"},
                        "requiredTools": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                    },
                },
            },
            "missing_capabilities": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Capabilities requested by the user that are not currently backed by real enabled tools or installed skills.",
            },
            "soul_markdown": {
                "type": "string",
                "description": "SOUL.md content defining the profile identity, behavior, boundaries, and response style.",
            },
            "memory_markdown": {
                "type": "string",
                "description": "Initial MEMORY.md project or capability memory for the profile.",
            },
            "user_markdown": {
                "type": "string",
                "description": "Initial USER.md user preference memory for the profile.",
            },
        },
        "required": [],
    },
}


TEST_AGENT_PROFILE_SCHEMA = {
    "name": "test_agent_profile",
    "description": (
        "Synchronously test a Doxie draft agent profile by sending a free-form "
        "message to the draft persona and waiting for its response. Use this "
        "after a draft exists when the user asks you to test, probe, compare, "
        "or evaluate the draft. The tool returns the tested profile response "
        "and runtime status; you, the main agent, must judge quality after the "
        "tool returns."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "draft_id": {
                "type": "string",
                "description": "Optional Doxie draft id to test. If omitted, Doxie resolves the active or matching draft from the current design conversation.",
            },
            "draft_reference": {
                "type": "string",
                "description": "Optional natural-language draft reference, such as the draft name, when draft_id is not known.",
            },
            "message": {
                "type": "string",
                "description": "The exact task, question, or boundary case to send to the tested draft profile.",
            },
            "expectation": {
                "type": "string",
                "description": "Optional quality expectation for the main agent to use when judging the returned response.",
            },
            "toolsets": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional narrower toolsets for the tested profile. Defaults to the draft runtime config toolsets.",
            },
        },
        "required": ["message"],
    },
}


def _avatar_assets() -> list[dict]:
    styles = [
        ("pixel-art", ["Operator", "Builder", "Analyst", "Researcher", "Designer", "Writer"]),
        ("bottts", ["Hermes", "Doxie", "Gateway", "Builder", "Operator", "Research"]),
        ("bottts-neutral", ["Atlas", "Nova", "Vector", "Pulse", "Orbit", "Signal"]),
    ]
    assets: list[dict] = []
    for style, seeds in styles:
        for seed in seeds:
            assets.append(
                {
                    "id": f"{style}:{seed}",
                    "style": style,
                    "seed": seed,
                    "url": f"https://api.dicebear.com/9.x/{style}/svg?seed={seed}",
                }
            )
    return assets


def _architecture_templates() -> list[dict]:
    return [
        {
            "id": "product-manager",
            "name": "产品经理分身",
            "category": "工作",
            "description": "面向 PRD、需求分析、用户研究、竞品分析、路线图和跨团队沟通。",
            "recommendedToolsets": ["web", "browser", "file", "memory"],
            "recommendedSkills": [],
            "tags": ["产品经理", "PRD", "用户研究", "竞品分析"],
        },
        {
            "id": "coding-agent",
            "name": "编程助手分身",
            "category": "开发",
            "description": "面向代码阅读、实现、测试、调试、重构和工程方案设计。",
            "recommendedToolsets": ["terminal", "file", "code_execution", "web"],
            "recommendedSkills": [],
            "tags": ["编程", "调试", "测试", "重构"],
        },
        {
            "id": "research-analyst",
            "name": "研究分析分身",
            "category": "学习",
            "description": "面向资料检索、信息抽取、报告生成和证据链整理。",
            "recommendedToolsets": ["web", "browser", "file", "memory"],
            "recommendedSkills": [],
            "tags": ["研究", "分析", "报告", "证据"],
        },
    ]


def _list_system_toolsets() -> list[dict]:
    from toolsets import get_internal_toolsets, get_toolset_info

    internal_toolsets = get_internal_toolsets()
    try:
        from hermes_cli.tools_config import _get_effective_configurable_toolsets

        candidates = [
            (name, description)
            for name, _label, description in _get_effective_configurable_toolsets()
        ]
    except Exception:
        from toolsets import get_all_toolsets

        candidates = [
            (name, str(definition.get("description") or ""))
            for name, definition in sorted(get_all_toolsets().items())
        ]

    items: list[dict] = []
    for name, fallback_description in candidates:
        if name in internal_toolsets:
            continue
        info = get_toolset_info(name) or {}
        items.append(
            {
                "name": name,
                "description": info.get("description") or fallback_description or "",
                "toolCount": int(info.get("tool_count") or 0),
                "includes": info.get("includes") or [],
            }
        )
    return items


def _list_installed_skills() -> list[dict]:
    try:
        from tools.skills_tool import skills_list

        payload = json.loads(skills_list())
    except Exception as exc:
        return [{"name": "skills_catalog_error", "description": str(exc), "category": "error"}]
    skills = payload.get("skills") if isinstance(payload, dict) else []
    if not isinstance(skills, list):
        return []
    result: list[dict] = []
    for skill in skills:
        if not isinstance(skill, dict):
            continue
        result.append(
            {
                "name": str(skill.get("name") or "").strip(),
                "description": str(skill.get("description") or "").strip(),
                "category": str(skill.get("category") or "").strip(),
            }
        )
    return [item for item in result if item["name"]]


def _catalog_limit(value: Any, default: int = 12, maximum: int = 50) -> int:
    try:
        parsed = int(value)
    except Exception:
        parsed = default
    return max(1, min(maximum, parsed))


def _catalog_matches(item: dict, query: str) -> bool:
    if not query:
        return True
    haystack = " ".join(str(value or "") for value in item.values()).lower()
    return query.lower() in haystack


def _filter_catalog(items: list[dict], query: str = "", limit: Any = None, default_limit: int = 12) -> tuple[list[dict], dict]:
    matched = [item for item in items if _catalog_matches(item, query)]
    page_limit = _catalog_limit(limit, default=default_limit)
    return matched[:page_limit], {
        "query": query,
        "limit": page_limit,
        "returned": min(len(matched), page_limit),
        "totalMatched": len(matched),
        "totalAvailable": len(items),
        "truncated": len(matched) > page_limit,
    }


def _profile_design_context_event(
    catalog_kind: str = "overview",
    query: str = "",
    limit: Any = None,
) -> dict:
    kind = str(catalog_kind or "overview").strip() or "overview"
    if kind not in {"overview", "toolsets", "skills", "templates", "avatars"}:
        kind = "overview"

    toolsets = _list_system_toolsets()
    skills = _list_installed_skills()
    templates = _architecture_templates()
    avatars = _avatar_assets()

    result = {
        "doxie_event": "agent_profile_design_context",
        "catalogKind": kind,
        "catalogQueries": {
            "toolsets": "Call design_agent_profile(operation='inspect_context', catalog_kind='toolsets', query='...', limit=...) for detailed toolset choices.",
            "skills": "Use skills_list/skill_view as the authoritative skill catalog; call catalog_kind='skills' only for a compact installed-skill hint page.",
            "templates": "Call catalog_kind='templates' for architecture templates.",
            "avatars": "Call catalog_kind='avatars' for avatar candidates.",
        },
        "rules": {
            "allowedCategories": AGENT_PROFILE_CATEGORIES,
            "skillCatalogSource": "hermes.skills",
            "useSkillsListForInstalledSkills": True,
            "useSkillManageForCreation": True,
            "instructions": [
                "recommendedToolsets must be selected from systemToolsets.name.",
                "recommendedSkills must be selected only after verifying with skills_list or skill_view.",
                "category must be one of 工作, 学习, 创作, 开发, 生活, 其他; do not invent new categories.",
                "installedSkillsSummary is a non-authoritative hint only.",
                "If a requested capability is unavailable, put it into missingCapabilities or skillCreationPlans.",
                "After inspecting context, call this same tool again with operation=create/update/upsert to request the draft.",
                "Use test_agent_profile to synchronously send test messages to a draft profile and wait for its real response.",
            ],
        },
    }

    if kind == "overview":
        result.update(
            {
                "summary": {
                    "toolsetCount": len(toolsets),
                    "installedSkillHintCount": len(skills),
                    "architectureTemplateCount": len(templates),
                    "avatarAssetCount": len(avatars),
                },
                "systemToolsets": [item["name"] for item in toolsets[:12]],
                "installedSkillsSummary": skills[:8],
                "architectureTemplates": templates[:3],
                "avatarAssets": avatars[:6],
            }
        )
        return result

    catalog_map = {
        "toolsets": toolsets,
        "skills": skills,
        "templates": templates,
        "avatars": avatars,
    }
    page, page_info = _filter_catalog(catalog_map[kind], str(query or "").strip(), limit)
    result["catalog"] = page
    result["page"] = page_info
    if kind == "skills":
        result["note"] = "This is only a compact installed-skill hint. Verify recommendedSkills with skills_list or skill_view before creating a draft."
    return result


def _profile_design_event(**kwargs) -> dict:
    operation = str(kwargs.get("operation") or "").strip()
    if operation not in {"create", "update", "upsert"}:
        operation = "update" if str(kwargs.get("draft_id") or "").strip() else "upsert"

    return {
        "doxie_event": "agent_profile_design_draft_requested",
        "operation": operation,
        "draft": {
            "draftId": kwargs.get("draft_id", ""),
            "name": kwargs.get("name", ""),
            "description": kwargs.get("description", ""),
            "avatar": kwargs.get("avatar", ""),
            "category": _normalize_agent_profile_category(kwargs.get("category", "")),
            "tags": kwargs.get("tags") or [],
            "architectureTemplateId": kwargs.get("architecture_template_id", ""),
            "recommendedToolsets": kwargs.get("recommended_toolsets") or [],
            "recommendedSkills": kwargs.get("recommended_skills") or [],
            "skillCreationPlans": kwargs.get("skill_creation_plans") or [],
            "missingCapabilities": kwargs.get("missing_capabilities") or [],
            "files": {
                "soulMarkdown": kwargs.get("soul_markdown", ""),
                "memoryMarkdown": kwargs.get("memory_markdown", ""),
                "userMarkdown": kwargs.get("user_markdown", ""),
            },
        },
    }


def _draft_payload_from_kwargs(**kwargs) -> dict:
    payload: dict[str, Any] = {}
    scalar_fields = {
        "draft_id": "draftId",
        "name": "name",
        "description": "description",
        "avatar": "avatar",
        "category": "category",
        "architecture_template_id": "architectureTemplateId",
        "default_model": "defaultModel",
        "default_provider": "defaultProvider",
        "default_permission_mode": "defaultPermissionMode",
    }
    for source, target in scalar_fields.items():
        if source in kwargs and kwargs.get(source) is not None:
            payload[target] = kwargs.get(source)
    array_fields = {
        "tags": "tags",
        "recommended_toolsets": "recommendedToolsets",
        "recommended_skills": "recommendedSkills",
        "skill_creation_plans": "skillCreationPlans",
        "missing_capabilities": "missingCapabilities",
    }
    for source, target in array_fields.items():
        if source in kwargs and kwargs.get(source) is not None:
            payload[target] = kwargs.get(source) or []
    files: dict[str, str] = {}
    file_fields = {
        "soul_markdown": "soulMarkdown",
        "memory_markdown": "memoryMarkdown",
        "user_markdown": "userMarkdown",
    }
    for source, target in file_fields.items():
        if source in kwargs and kwargs.get(source) is not None:
            files[target] = str(kwargs.get(source) or "")
    if files:
        payload["files"] = files
    if "category" in payload:
        payload["category"] = _normalize_agent_profile_category(payload.get("category"))
    return _contextual_payload(payload)


def _profile_design_backend_event(**kwargs) -> dict:
    operation = str(kwargs.get("operation") or "").strip()
    if operation not in {"create", "update", "upsert"}:
        operation = "update" if str(kwargs.get("draft_id") or "").strip() else "upsert"
    payload = _draft_payload_from_kwargs(**kwargs)
    explicit_draft_id = _first_non_empty(kwargs.get("draft_id"), kwargs.get("draftId"), kwargs.get("id"))
    active_draft_id = _first_non_empty(payload.get("activeDraftId"))
    draft_id = _first_non_empty(explicit_draft_id, active_draft_id)
    target_agent_profile_id = _first_non_empty(payload.get("targetAgentProfileId"), payload.get("agentProfileId"))
    should_update_active_draft = (
        operation in {"update", "upsert"}
        and draft_id
        and (
            bool(explicit_draft_id)
            or not target_agent_profile_id
            or _draft_targets_profile(draft_id, target_agent_profile_id)
        )
    )
    if should_update_active_draft:
        payload["draftId"] = draft_id
        draft = _backend_call("doxie_agent_profile_draft_update", payload)
        normalized_operation = "update"
    elif target_agent_profile_id:
        payload.pop("draftId", None)
        payload["agentProfileId"] = target_agent_profile_id
        draft = _backend_call("doxie_agent_profile_draft_create_revision", payload)
        normalized_operation = "revision"
    else:
        payload.pop("draftId", None)
        draft = _backend_call("doxie_agent_profile_draft_create", payload)
        normalized_operation = "create"
    return {
        "doxie_event": "agent_profile_design_draft_saved",
        "operation": normalized_operation,
        "draftId": draft.get("id") if isinstance(draft, dict) else "",
        "draft": draft,
    }


def _install_skill_into_draft_home(skill_name: str, target_home: Path) -> dict:
    from tools.skill_package_lifecycle import copy_installed_skill_to_home

    return copy_installed_skill_to_home(skill_name, target_home)


def _without_skill_missing_capability(items: Any, skill_name: str) -> list[str]:
    normalized = str(skill_name or "").strip()
    result = []
    for item in items if isinstance(items, list) else []:
        text = str(item or "").strip()
        if not text:
            continue
        if normalized and normalized in text and ("技能未安装" in text or "skill" in text.lower()):
            continue
        result.append(text)
    return result


def _install_skill_to_draft_event(**kwargs) -> dict:
    skill_name = _first_non_empty(
        kwargs.get("skill_name"),
        kwargs.get("skillName"),
        kwargs.get("source_skill_name"),
        kwargs.get("sourceSkillName"),
        kwargs.get("name"),
    )
    if not skill_name:
        raise RuntimeError("skill_name is required")
    if not _backend_bridge_available():
        raise RuntimeError("Doxie backend bridge is required to install a skill into a draft profile.")

    prepared = _backend_call(
        "doxie_agent_profile_draft_prepare_runtime",
        _contextual_payload(
            {
                "draftId": _first_non_empty(kwargs.get("draft_id"), kwargs.get("draftId")),
                "reference": _first_non_empty(kwargs.get("draft_reference"), kwargs.get("draftReference"), kwargs.get("reference")),
            }
        ),
    )
    if not isinstance(prepared, dict) or not prepared.get("prepared"):
        return {
            "doxie_event": "agent_profile_draft_skill_install_resolution_required",
            **(prepared if isinstance(prepared, dict) else {"status": "error"}),
            "skillName": skill_name,
        }

    draft = prepared.get("draft") if isinstance(prepared.get("draft"), dict) else {}
    draft_id = str(draft.get("id") or "").strip()
    target_home = Path(str(prepared.get("hermesHomePath") or "")).expanduser()
    if not draft_id or not target_home:
        raise RuntimeError("prepared draft runtime did not return draft id or hermesHomePath")

    installed = _install_skill_into_draft_home(skill_name, target_home)
    current_skills = [str(item).strip() for item in (draft.get("recommendedSkills") or []) if str(item).strip()]
    if installed["name"] not in current_skills:
        current_skills.append(installed["name"])
    update_payload = _contextual_payload(
        {
            "draftId": draft_id,
            "recommendedSkills": current_skills,
            "missingCapabilities": _without_skill_missing_capability(draft.get("missingCapabilities"), installed["name"]),
        }
    )
    updated_draft = _backend_call("doxie_agent_profile_draft_update", update_payload)
    return {
        "doxie_event": "agent_profile_draft_skill_installed",
        "draftId": draft_id,
        "skillName": installed["name"],
        "runtimeProfileId": prepared.get("runtimeProfileId") or f"draft:{draft_id}",
        "targetHermesHome": installed["targetHermesHome"],
        "targetSkillDir": installed["targetSkillDir"],
        "draft": updated_draft,
    }


def design_agent_profile(**kwargs) -> str:
    """Inspect 分身设计 context or return a structured draft request."""
    operation = str(kwargs.get("operation") or "").strip()
    if operation == "inspect_context":
        return tool_result(
            _profile_design_context_event(
                catalog_kind=str(kwargs.get("catalog_kind") or kwargs.get("catalogKind") or "overview"),
                query=str(kwargs.get("query") or ""),
                limit=kwargs.get("limit"),
            )
        )
    if operation == "install_skill":
        try:
            return tool_result(_install_skill_to_draft_event(**kwargs))
        except Exception as exc:
            return tool_error(f"failed to install skill into Doxie draft profile: {exc}")
    if _backend_bridge_available():
        try:
            return tool_result(_profile_design_backend_event(**kwargs))
        except Exception as exc:
            return tool_error(f"failed to save Doxie agent profile draft: {exc}")
    return tool_result(_profile_design_event(**kwargs))


def _backend_tool_result(command: str, payload: dict | None = None) -> str:
    try:
        return tool_result(_backend_call(command, _contextual_payload(payload)))
    except Exception as exc:
        return tool_error(str(exc))


def list_agent_profile_drafts(**kwargs) -> str:
    return _backend_tool_result("doxie_agent_profile_draft_list", kwargs)


def get_agent_profile_draft(**kwargs) -> str:
    return _backend_tool_result("doxie_agent_profile_draft_get", kwargs)


def resolve_agent_profile_draft(**kwargs) -> str:
    return _backend_tool_result("doxie_agent_profile_draft_resolve", kwargs)


def list_agent_profiles(**kwargs) -> str:
    return _backend_tool_result("doxie_agent_profile_list", kwargs)


def get_agent_profile(**kwargs) -> str:
    return _backend_tool_result("doxie_agent_profile_resolve", kwargs)


def list_agent_profile_versions(**kwargs) -> str:
    return _backend_tool_result("doxie_agent_profile_version_list", kwargs)


def get_agent_profile_version(**kwargs) -> str:
    return _backend_tool_result("doxie_agent_profile_version_get", kwargs)


def create_agent_profile_revision_draft(**kwargs) -> str:
    try:
        draft = _backend_call("doxie_agent_profile_draft_create_revision", _contextual_payload(kwargs))
        return tool_result(
            {
                "doxie_event": "agent_profile_design_draft_saved",
                "operation": "revision",
                "draftId": draft.get("id") if isinstance(draft, dict) else "",
                "draft": draft,
            }
        )
    except Exception as exc:
        return tool_error(str(exc))


def prepare_agent_profile_draft_runtime(**kwargs) -> str:
    return _backend_tool_result("doxie_agent_profile_draft_prepare_runtime", kwargs)


def install_skill_to_agent_profile_draft(**kwargs) -> str:
    try:
        return tool_result(_install_skill_to_draft_event(**kwargs))
    except Exception as exc:
        return tool_error(str(exc))


def _slug(value: str, fallback: str = "draft") -> str:
    text = str(value or "").strip().lower()
    text = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "-", text).strip("-")
    return text or fallback


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""


def _draft_home(draft_id: str) -> Path:
    from config import get_hermes_home

    return Path(get_hermes_home()) / "drafts" / _slug(draft_id, "draft")


def _draft_toolsets(home: Path) -> list[str]:
    config_text = _read_text(home / "config.yaml")
    if not config_text.strip():
        return []
    try:
        import yaml

        parsed = yaml.safe_load(config_text) or {}
        platform_toolsets = parsed.get("platform_toolsets") if isinstance(parsed, dict) else {}
        cli_toolsets = platform_toolsets.get("cli") if isinstance(platform_toolsets, dict) else []
        if isinstance(cli_toolsets, list):
            return [str(item).strip() for item in cli_toolsets if str(item).strip()]
    except Exception:
        pass
    return []


def _draft_context(home: Path, draft_id: str, expectation: str = "") -> str:
    soul = _read_text(home / "SOUL.md").strip()
    memory = _read_text(home / "memories" / "MEMORY.md").strip()
    user = _read_text(home / "memories" / "USER.md").strip()
    parts = [
        f"You are being tested as Doxie draft agent profile `{draft_id}`.",
        "Respond as the tested draft profile. Do not evaluate yourself unless the message asks for that.",
        "",
        "## SOUL.md",
        soul or "(empty)",
    ]
    if memory:
        parts.extend(["", "## MEMORY.md", memory])
    if user:
        parts.extend(["", "## USER.md", user])
    if expectation:
        parts.extend(["", "## Test expectation visible to the test runner", expectation])
    return "\n".join(parts)


def test_agent_profile(
    message: str,
    draft_id: str = "",
    draft_reference: str = "",
    expectation: str = "",
    toolsets: list[str] | None = None,
    parent_agent=None,
) -> str:
    normalized_draft_id = str(draft_id or "").strip()
    test_message = str(message or "").strip()
    if not test_message:
        return tool_error("message is required.")
    if parent_agent is None:
        return tool_error("test_agent_profile requires a parent agent context.")

    prepared = None
    if _backend_bridge_available():
        try:
            prepared = _backend_call(
                "doxie_agent_profile_draft_prepare_runtime",
                _contextual_payload(
                    {
                        "draftId": normalized_draft_id,
                        "reference": draft_reference,
                    }
                ),
            )
        except Exception as exc:
            return tool_error(f"failed to prepare draft runtime: {exc}")
        if not isinstance(prepared, dict) or not prepared.get("prepared"):
            payload = prepared if isinstance(prepared, dict) else {"status": "error"}
            payload = {
                "doxie_event": "agent_profile_test_resolution_required",
                **payload,
                "message": test_message,
                "expectation": str(expectation or "").strip(),
            }
            return tool_result(payload)
        draft = prepared.get("draft") if isinstance(prepared.get("draft"), dict) else {}
        normalized_draft_id = str(draft.get("id") or normalized_draft_id).strip()
        home = Path(str(prepared.get("hermesHomePath") or ""))
    else:
        if not normalized_draft_id:
            return tool_error("draft_id is required when the Doxie backend bridge is unavailable.")
        home = _draft_home(normalized_draft_id)
    if not home.is_dir():
        return tool_error(
            f"Draft runtime is not prepared: {normalized_draft_id}. "
            "Doxie could not prepare its transient runtime files."
        )
    if not (home / "SOUL.md").is_file():
        return tool_error(f"Draft runtime is missing SOUL.md: {normalized_draft_id}.")

    selected_toolsets = [
        str(item).strip()
        for item in (toolsets or _draft_toolsets(home))
        if str(item).strip()
    ]
    context = _draft_context(home, normalized_draft_id, str(expectation or "").strip())
    started = time.monotonic()
    from tools.delegate_tool import delegate_task

    previous_child_transient_session = getattr(
        parent_agent, "_delegate_child_transient_session", None
    )
    previous_child_progress_suppressed = getattr(
        parent_agent, "_delegate_child_progress_suppressed", None
    )
    previous_child_output_delta_enabled = getattr(
        parent_agent, "_delegate_child_output_delta_enabled", None
    )
    previous_child_output_tool_name = getattr(
        parent_agent, "_delegate_child_output_tool_name", None
    )
    setattr(parent_agent, "_delegate_child_transient_session", True)
    setattr(parent_agent, "_delegate_child_progress_suppressed", True)
    setattr(parent_agent, "_delegate_child_output_delta_enabled", True)
    setattr(parent_agent, "_delegate_child_output_tool_name", "test_agent_profile")
    try:
        raw = delegate_task(
            goal=test_message,
            context=context,
            # Pass a truthy unknown sentinel when the draft has no toolsets so
            # delegate_task does not inherit the parent turn's doxie/skills tools.
            toolsets=selected_toolsets or ["__doxie_no_tools__"],
            parent_agent=parent_agent,
        )
    finally:
        if previous_child_transient_session is None:
            try:
                delattr(parent_agent, "_delegate_child_transient_session")
            except AttributeError:
                pass
        else:
            setattr(
                parent_agent,
                "_delegate_child_transient_session",
                previous_child_transient_session,
            )
        if previous_child_progress_suppressed is None:
            try:
                delattr(parent_agent, "_delegate_child_progress_suppressed")
            except AttributeError:
                pass
        else:
            setattr(
                parent_agent,
                "_delegate_child_progress_suppressed",
                previous_child_progress_suppressed,
            )
        if previous_child_output_delta_enabled is None:
            try:
                delattr(parent_agent, "_delegate_child_output_delta_enabled")
            except AttributeError:
                pass
        else:
            setattr(
                parent_agent,
                "_delegate_child_output_delta_enabled",
                previous_child_output_delta_enabled,
            )
        if previous_child_output_tool_name is None:
            try:
                delattr(parent_agent, "_delegate_child_output_tool_name")
            except AttributeError:
                pass
        else:
            setattr(
                parent_agent,
                "_delegate_child_output_tool_name",
                previous_child_output_tool_name,
            )
    duration = round(time.monotonic() - started, 2)
    try:
        delegated = json.loads(raw)
    except Exception:
        delegated = {"status": "error", "summary": raw}

    results = delegated.get("results") if isinstance(delegated, dict) else None
    first = results[0] if isinstance(results, list) and results else delegated
    if not isinstance(first, dict):
        first = {"status": "error", "summary": str(first)}

    status = str(first.get("status") or delegated.get("status") or "completed")
    if status == "success":
        status = "completed"
    response = str(first.get("summary") or first.get("result") or "").strip()
    payload: dict[str, Any] = {
        "doxie_event": "agent_profile_test_completed",
        "draftId": normalized_draft_id,
        "preparedAt": prepared.get("preparedAt") if isinstance(prepared, dict) else "",
        "status": status,
        "message": test_message,
        "expectation": str(expectation or "").strip(),
        "response": response,
        "durationSeconds": duration,
        "toolsets": selected_toolsets,
        "subagent": {
            "status": status,
            "apiCalls": first.get("api_calls"),
            "toolCount": first.get("tool_count"),
            "error": first.get("error"),
        },
    }
    return tool_result(payload)


registry.register(
    name="design_agent_profile",
    toolset="doxie",
    schema=DESIGN_AGENT_PROFILE_SCHEMA,
    handler=lambda args, **kw: design_agent_profile(**args),
    emoji="🧬",
    max_result_size_chars=32_000,
)

registry.register(
    name="test_agent_profile",
    toolset="doxie",
    schema=TEST_AGENT_PROFILE_SCHEMA,
    handler=lambda args, **kw: test_agent_profile(
        draft_id=args.get("draft_id"),
        draft_reference=args.get("draft_reference", ""),
        message=args.get("message"),
        expectation=args.get("expectation", ""),
        toolsets=args.get("toolsets"),
        parent_agent=kw.get("parent_agent"),
    ),
    emoji="🧪",
    max_result_size_chars=32_000,
)


def _simple_object_schema(name: str, description: str, properties: dict | None = None) -> dict:
    return {
        "name": name,
        "description": description,
        "parameters": {
            "type": "object",
            "properties": properties or {},
            "required": [],
        },
    }


for _name, _description, _handler, _properties in [
    (
        "list_agent_profile_drafts",
        "List Doxie agent profile drafts visible to the current design conversation. Use before testing when draft_id is unknown.",
        list_agent_profile_drafts,
        {
            "reference": {"type": "string"},
            "includePublished": {"type": "boolean"},
        },
    ),
    (
        "get_agent_profile_draft",
        "Get a Doxie agent profile draft by id.",
        get_agent_profile_draft,
        {"draftId": {"type": "string"}},
    ),
    (
        "resolve_agent_profile_draft",
        "Resolve the active or matching Doxie draft from the current design conversation.",
        resolve_agent_profile_draft,
        {"reference": {"type": "string"}, "draftId": {"type": "string"}},
    ),
    (
        "list_agent_profiles",
        "List published Doxie agent profiles.",
        list_agent_profiles,
        {"query": {"type": "string"}},
    ),
    (
        "get_agent_profile",
        "Get a published Doxie agent profile by id or slug.",
        get_agent_profile,
        {"agentProfileId": {"type": "string"}, "slug": {"type": "string"}},
    ),
    (
        "list_agent_profile_versions",
        "List immutable published versions for a Doxie agent profile.",
        list_agent_profile_versions,
        {"agentProfileId": {"type": "string"}},
    ),
    (
        "get_agent_profile_version",
        "Get an immutable published Doxie agent profile version.",
        get_agent_profile_version,
        {"agentProfileId": {"type": "string"}, "versionId": {"type": "string"}},
    ),
    (
        "create_agent_profile_revision_draft",
        "Create an editable revision draft from an existing published Doxie agent profile/version.",
        create_agent_profile_revision_draft,
        {
            "agentProfileId": {"type": "string"},
            "targetAgentProfileId": {"type": "string"},
            "versionId": {"type": "string"},
        },
    ),
    (
        "prepare_agent_profile_draft_runtime",
        "Prepare the transient Hermes runtime files for a Doxie draft profile.",
        prepare_agent_profile_draft_runtime,
        {"draftId": {"type": "string"}, "reference": {"type": "string"}},
    ),
    (
        "install_skill_to_agent_profile_draft",
        "Install an existing Hermes skill into a Doxie draft profile runtime scope, then add it to the draft recommendedSkills. Use after creating a reusable skill with skill_manage and confirming it should belong to the draft.",
        install_skill_to_agent_profile_draft,
        {
            "draftId": {"type": "string"},
            "draft_id": {"type": "string"},
            "reference": {"type": "string"},
            "draft_reference": {"type": "string"},
            "skill_name": {"type": "string"},
            "skillName": {"type": "string"},
        },
    ),
]:
    registry.register(
        name=_name,
        toolset="doxie",
        schema=_simple_object_schema(_name, _description, _properties),
        handler=lambda args, _handler=_handler, **kw: _handler(**args),
        emoji="🧬",
        max_result_size_chars=32_000,
    )
