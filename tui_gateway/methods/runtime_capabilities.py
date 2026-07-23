# ruff: noqa: F401,F403,F405,F821,ARG001
from __future__ import annotations

from agent_capabilities.credentials import capability_credentials
from tui_gateway.methods._shared import bind_server_globals

_server = bind_server_globals(globals())


def _invalidate_tool_surface() -> None:
    from model_tools import invalidate_tool_definitions_cache
    from tools.registry import invalidate_check_fn_cache

    invalidate_check_fn_cache()
    invalidate_tool_definitions_cache()


@method("runtime.capabilities.update")
def runtime_capabilities_update(rid, params: dict) -> dict:
    params = params or {}
    try:
        credential = capability_credentials.configure(
            capability=str(params.get("capability") or ""),
            token=str(params.get("capability_token") or params.get("capabilityToken") or ""),
            api_origin=str(params.get("api_origin") or params.get("apiOrigin") or ""),
            conversation_id=str(params.get("conversation_id") or params.get("conversationId") or ""),
            execution_participant_id=str(
                params.get("execution_participant_id") or params.get("executionParticipantId") or ""
            ),
            expires_at=float(params.get("expires_at") or params.get("expiresAt") or 0),
            on_change=_invalidate_tool_surface,
        )
    except Exception as exc:
        return _err(rid, -32602, f"runtime capability update rejected: {exc}")
    return _ok(
        rid,
        {
            "ok": True,
            "capability": credential.capability,
            "conversation_id": credential.conversation_id,
            "expires_at": credential.expires_at,
            "fingerprint": credential.fingerprint,
        },
    )


@method("runtime.capabilities.clear")
def runtime_capabilities_clear(rid, params: dict) -> dict:
    params = params or {}
    try:
        cleared = capability_credentials.clear(
            capability=str(params.get("capability") or ""),
            conversation_id=str(params.get("conversation_id") or params.get("conversationId") or ""),
            on_change=_invalidate_tool_surface,
        )
    except Exception as exc:
        return _err(rid, -32602, f"runtime capability clear rejected: {exc}")
    return _ok(rid, {"ok": True, "cleared": cleared})
