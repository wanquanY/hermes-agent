"""Runtime lease helpers for gateway-controlled runs.

The run registry owns durable run state.  This module owns the narrow task of
finding or recreating an in-process execution container for a stored session.
It deliberately has no knowledge of JSON-RPC methods beyond callback hooks so
the gateway method layer does not become the runtime state machine.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from tui_gateway.transport import Transport


@dataclass(frozen=True)
class RuntimeLease:
    execution_session_id: str
    session: dict[str, Any]
    reused: bool


@dataclass(frozen=True)
class RuntimeLeaseError:
    response: dict[str, Any]


ResolveRuntimeSession = Callable[[str], tuple[str, dict[str, Any] | None]]
ResumeRuntimeSession = Callable[[str, dict[str, Any]], dict[str, Any]]
SessionLookup = Callable[[str], dict[str, Any] | None]


def _is_execution_runtime(session: dict[str, Any]) -> bool:
    """Return True only for sessions that can become an agent execution host."""

    return session.get("agent") is not None or session.get("agent_ready") is not None


def _context_mode_from_params(params: dict[str, Any] | None) -> str:
    raw = (
        (params or {}).get("agent_context_mode")
        or (params or {}).get("agentContextMode")
        or (params or {}).get("runtime_context_mode")
        or (params or {}).get("runtimeContextMode")
        or ""
    )
    mode = str(raw or "").strip().lower().replace("-", "_")
    if mode in {"team_leader", "leader_conversation"}:
        return "team_leader"
    if mode in {"profile", "profile_conversation", "default"}:
        return "profile"
    return ""


def _context_mode_matches(session: dict[str, Any], params: dict[str, Any]) -> bool:
    expected = _context_mode_from_params(params)
    if not expected:
        return True
    current = _context_mode_from_params(session)
    return not current or current == expected


def acquire_runtime_lease(
    *,
    rid: str,
    conversation_session_id: str,
    params: dict[str, Any],
    resolve_runtime_session: ResolveRuntimeSession,
    resume_runtime_session: ResumeRuntimeSession,
    session_lookup: SessionLookup,
    transport: Transport | None,
    fallback_transport: Transport | None,
    runtime_scope_key: str = "",
) -> RuntimeLease | RuntimeLeaseError:
    """Return a live execution container for a stored session.

    Existing runtimes are reused by stored session id; otherwise the caller's
    resume callback reconstructs a lightweight runtime with no transcript
    hydration. The durable business state remains in the session/run registries.
    """
    target = str(conversation_session_id or "").strip()
    if not target:
        return RuntimeLeaseError(
            {
                "jsonrpc": "2.0",
                "id": rid,
                "error": {
                    "code": 4006,
                    "message": "conversation_session_id or session_id required",
                },
            }
        )

    expected_scope = str(runtime_scope_key or "").strip()
    sid, session = resolve_runtime_session(target)
    if session is not None and _is_execution_runtime(session):
        current_scope = str(
            session.get("runtime_scope_key")
            or session.get("active_runtime_scope_key")
            or ""
        ).strip()
        if (
            (not expected_scope or not current_scope or current_scope == expected_scope)
            and _context_mode_matches(session, params)
        ):
            session["transport"] = transport or session.get("transport") or fallback_transport
            return RuntimeLease(execution_session_id=sid, session=session, reused=True)

    resume = resume_runtime_session(
        rid,
        {
            **params,
            "session_id": target,
            **({"runtime_scope_key": expected_scope} if expected_scope else {}),
            "hydrate": "none",
            "message_limit": 0,
            "_runtime_attach": True,
        },
    )
    if isinstance(resume, dict) and resume.get("error"):
        return RuntimeLeaseError(resume)
    result = resume.get("result") if isinstance(resume, dict) else {}
    sid = str((result or {}).get("session_id") or "").strip()
    if not sid:
        return RuntimeLeaseError(
            {
                "jsonrpc": "2.0",
                "id": rid,
                "error": {
                    "code": 5000,
                    "message": "runtime session resume did not return session_id",
                },
            }
        )
    session = session_lookup(sid)
    if session is None or not _is_execution_runtime(session):
        return RuntimeLeaseError(
            {
                "jsonrpc": "2.0",
                "id": rid,
                "error": {
                    "code": 5000,
                    "message": "runtime session resumed but is not executable",
                },
            }
        )
    session["transport"] = transport or session.get("transport") or fallback_transport
    if expected_scope:
        session["runtime_scope_key"] = expected_scope
    return RuntimeLease(execution_session_id=sid, session=session, reused=False)
