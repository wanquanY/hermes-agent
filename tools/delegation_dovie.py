"""Dovie attribution propagation for delegated child agents."""

from __future__ import annotations

import logging
from typing import Any, Dict

logger = logging.getLogger(__name__)
_DOVIE_SUBAGENT_ROLE = "subagent"

def _dovie_text(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip()


def _resolve_child_dovie_executing_profile_id(child: Any) -> str:
    for attr in (
        "_dovie_agent_profile_id",
        "dovie_agent_profile_id",
        "agent_profile_id",
        "_agent_profile_id",
        "profile_id",
        "_profile_id",
        "target_profile_id",
        "_target_profile_id",
    ):
        value = _dovie_text(getattr(child, attr, ""))
        if value:
            return value
    return ""


def _merge_child_dovie_extra_headers(child: Any, headers: Dict[str, str]) -> None:
    if not headers:
        return
    overrides = getattr(child, "request_overrides", None)
    merged_overrides: Dict[str, Any] = dict(overrides) if isinstance(overrides, dict) else {}
    existing_headers = merged_overrides.get("extra_headers")
    merged_headers: Dict[str, str] = (
        dict(existing_headers) if isinstance(existing_headers, dict) else {}
    )
    merged_headers.update(headers)
    merged_overrides["extra_headers"] = merged_headers
    try:
        child.request_overrides = merged_overrides
    except Exception:
        pass


def _prepare_child_dovie_attribution(child: Any) -> bool:
    """Attach Dovie executing-agent attribution metadata to a subagent."""
    executing_profile_id = _resolve_child_dovie_executing_profile_id(child)
    if not executing_profile_id:
        return False
    try:
        child._dovie_child_executing_agent_profile_id = executing_profile_id
        child._dovie_child_agent_role = _DOVIE_SUBAGENT_ROLE
    except Exception:
        pass

    try:
        from agent.dovie_attribution import (
            build_dovie_attribution_overlay_headers,
            dovie_child_run_overlay,
        )

        with dovie_child_run_overlay(executing_profile_id, _DOVIE_SUBAGENT_ROLE):
            headers = build_dovie_attribution_overlay_headers()
        _merge_child_dovie_extra_headers(child, headers)
    except Exception as exc:
        logger.debug("Failed to prepare Dovie child attribution: %s", exc)
    return True


def _run_child_conversation_with_dovie_attribution(
    child: Any,
    *,
    goal: str,
    child_task_id: str,
) -> Any:
    executing_profile_id = _dovie_text(
        getattr(child, "_dovie_child_executing_agent_profile_id", "")
    )
    if not executing_profile_id:
        return child.run_conversation(
            user_message=goal,
            task_id=child_task_id,
        )
    agent_role = _dovie_text(
        getattr(child, "_dovie_child_agent_role", "")
    ) or _DOVIE_SUBAGENT_ROLE
    from agent.dovie_attribution import dovie_child_run_overlay

    with dovie_child_run_overlay(executing_profile_id, agent_role):
        return child.run_conversation(
            user_message=goal,
            task_id=child_task_id,
        )



# ---------------------------------------------------------------------------
