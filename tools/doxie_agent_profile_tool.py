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
        "preview, test chat, editing, validation, testing, and publishing."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "operation": {
                "type": "string",
                "enum": ["inspect_context", "create", "update", "upsert"],
                "description": "Use inspect_context first to retrieve real design catalog data. Use create/update/upsert for draft changes.",
            },
            "draft_id": {
                "type": "string",
                "description": "Existing Doxie draft id to update. Leave empty only when creating a brand-new agent profile draft.",
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
                "description": "Profile category, such as coding, office, research, writing, or operations.",
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
            "category": "product",
            "description": "面向 PRD、需求分析、用户研究、竞品分析、路线图和跨团队沟通。",
            "recommendedToolsets": ["web", "browser", "file", "memory"],
            "recommendedSkills": [],
            "tags": ["产品经理", "PRD", "用户研究", "竞品分析"],
        },
        {
            "id": "coding-agent",
            "name": "编程助手分身",
            "category": "development",
            "description": "面向代码阅读、实现、测试、调试、重构和工程方案设计。",
            "recommendedToolsets": ["terminal", "file", "code_execution", "web"],
            "recommendedSkills": [],
            "tags": ["编程", "调试", "测试", "重构"],
        },
        {
            "id": "research-analyst",
            "name": "研究分析分身",
            "category": "research",
            "description": "面向资料检索、信息抽取、报告生成和证据链整理。",
            "recommendedToolsets": ["web", "browser", "file", "memory"],
            "recommendedSkills": [],
            "tags": ["研究", "分析", "报告", "证据"],
        },
    ]


def _list_system_toolsets() -> list[dict]:
    from toolsets import get_all_toolsets

    items: list[dict] = []
    for name, definition in sorted(get_all_toolsets().items()):
        if name == "doxie":
            continue
        tools = definition.get("tools") or []
        includes = definition.get("includes") or []
        items.append(
            {
                "name": name,
                "description": definition.get("description") or "",
                "toolCount": len(tools),
                "includes": includes,
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


def _profile_design_context_event() -> dict:
    return {
        "doxie_event": "agent_profile_design_context",
        "systemToolsets": _list_system_toolsets(),
        "installedSkillsSummary": _list_installed_skills()[:20],
        "architectureTemplates": _architecture_templates(),
        "avatarAssets": _avatar_assets(),
        "rules": {
            "skillCatalogSource": "hermes.skills",
            "useSkillsListForInstalledSkills": True,
            "useSkillManageForCreation": True,
            "instructions": [
                "recommendedToolsets must be selected from systemToolsets.name.",
                "recommendedSkills must be selected only after verifying with skills_list or skill_view.",
                "installedSkillsSummary is a non-authoritative hint only.",
                "If a requested capability is unavailable, put it into missingCapabilities or skillCreationPlans.",
                "After inspecting context, call this same tool again with operation=create/update/upsert to request the draft.",
                "Use test_agent_profile to synchronously send test messages to a draft profile and wait for its real response.",
            ],
        },
    }


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
            "category": kwargs.get("category", ""),
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


def design_agent_profile(**kwargs) -> str:
    """Inspect 分身设计 context or return a structured draft request."""
    operation = str(kwargs.get("operation") or "").strip()
    if operation == "inspect_context":
        return tool_result(_profile_design_context_event())
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

    previous_stream_delta_callback = getattr(
        parent_agent, "_delegate_child_stream_delta_callback", None
    )

    def _relay_test_delta(delta: str | None) -> None:
        if not delta:
            return
        progress_cb = getattr(parent_agent, "tool_progress_callback", None)
        if not progress_cb:
            return
        try:
            progress_cb(
                "subagent.output_delta",
                "test_agent_profile",
                str(delta),
                None,
                goal=test_message,
                status="running",
            )
        except Exception:
            pass

    setattr(parent_agent, "_delegate_child_stream_delta_callback", _relay_test_delta)
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
        if previous_stream_delta_callback is None:
            try:
                delattr(parent_agent, "_delegate_child_stream_delta_callback")
            except AttributeError:
                pass
        else:
            setattr(
                parent_agent,
                "_delegate_child_stream_delta_callback",
                previous_stream_delta_callback,
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
]:
    registry.register(
        name=_name,
        toolset="doxie",
        schema=_simple_object_schema(_name, _description, _properties),
        handler=lambda args, _handler=_handler, **kw: _handler(**args),
        emoji="🧬",
        max_result_size_chars=32_000,
    )
