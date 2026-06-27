from __future__ import annotations

import hashlib
import json
from typing import Any

from agent.dovie_diagnostics import emit_dovie_diagnostic


_PERSONA_MARKERS = (
    "Hermes",
    "Hermes Agent",
    "Claude",
    "Anthropic",
    "Dovie",
    "小多",
    "SOUL.md",
    "Model:",
    "Provider:",
    "Active Hermes profile",
)


def persona_text_probe(value: Any, *, preview_chars: int = 120) -> dict[str, Any]:
    text = str(value or "")
    lowered = text.lower()
    return {
        "len": len(text),
        "sha1": hashlib.sha1(text.encode("utf-8", errors="replace")).hexdigest()[:12],
        "markers": [
            marker
            for marker in _PERSONA_MARKERS
            if marker.lower() in lowered
        ],
        "preview": text[:preview_chars].replace("\n", "\\n"),
    }


def run_context_probe(context: Any) -> dict[str, str]:
    if context is None:
        return {}
    return {
        "conversation_session_id": str(getattr(context, "conversation_session_id", "") or ""),
        "participant_id": str(getattr(context, "participant_id", "") or ""),
        "activity_id": str(getattr(context, "activity_id", "") or ""),
        "activity_kind": str(getattr(context, "activity_kind", "") or ""),
        "execution_scope_key": str(getattr(context, "execution_scope_key", "") or ""),
        "control_home": str(getattr(context, "control_home", "") or ""),
        "execution_home": str(getattr(context, "execution_home", "") or ""),
    }


def dovie_context_probe(raw_context: Any) -> dict[str, Any]:
    if not isinstance(raw_context, dict):
        return {"type": type(raw_context).__name__, "present": bool(raw_context)}
    team_mission = raw_context.get("team_mission") or raw_context.get("teamMission")
    team_mission = team_mission if isinstance(team_mission, dict) else {}
    return {
        "type": "dict",
        "keys": sorted(str(key) for key in raw_context.keys()),
        "team_mission": {
            "kind": str(team_mission.get("kind") or ""),
            "mission_id": str(team_mission.get("mission_id") or team_mission.get("missionId") or ""),
            "conversation_id": str(
                team_mission.get("conversation_id")
                or team_mission.get("conversationId")
                or ""
            ),
            "conversation_session_id": str(
                team_mission.get("conversation_session_id")
                or team_mission.get("conversationSessionId")
                or ""
            ),
            "team_id": str(team_mission.get("team_id") or team_mission.get("teamId") or ""),
            "delegate_inherits_parent_tools": bool(
                team_mission.get("delegate_inherits_parent_tools")
                or team_mission.get("delegateInheritsParentTools")
            ),
        },
    }


def coerce_dovie_context(raw_context: Any) -> Any:
    if isinstance(raw_context, dict):
        return raw_context
    if isinstance(raw_context, str):
        text = raw_context.strip()
        if text.startswith("{"):
            try:
                return json.loads(text)
            except Exception:
                return raw_context
    return raw_context


def trace_persona_chain(agent: Any, stage: str, **fields: Any) -> None:
    run_id = str(getattr(agent, "_hermes_active_run_id", "") or "")
    turn_id = str(getattr(agent, "_hermes_active_turn_id", "") or "")
    runtime_scope_key = str(getattr(agent, "_hermes_active_runtime_scope_key", "") or "")
    if not run_id and not runtime_scope_key:
        return
    context = getattr(agent, "run_context", None) or getattr(agent, "_run_context", None)
    emit_dovie_diagnostic(
        "[h10-trace persona-chain]",
        {
            "stage": stage,
            "session_id": str(getattr(agent, "session_id", "") or ""),
            "run_id": run_id,
            "turn_id": turn_id,
            "runtime_scope_key": runtime_scope_key,
            "agent_model": str(getattr(agent, "model", "") or ""),
            "agent_provider": str(getattr(agent, "provider", "") or ""),
            "agent_platform": str(getattr(agent, "platform", "") or ""),
            "run_context": run_context_probe(context),
            **fields,
        },
    )


def trace_persona_payload(stage: str, **fields: Any) -> None:
    emit_dovie_diagnostic("[h10-trace persona-chain]", {"stage": stage, **fields})
