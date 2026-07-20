"""Bridge background-process terminal mirrors to the owning desktop session."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from threading import RLock
from typing import Any

from tui_gateway.services.runtime_event_protocol import event_domain_for_type


_terminal_owners_lock = RLock()
_terminal_owners: dict[str, tuple[Any, str]] = {}


def bind_desktop_terminal_owner(
    session_key: str,
    *,
    transport: Any,
    runtime_scope_key: str = "",
) -> None:
    """Bind a durable conversation id to the current desktop transport.

    Manual terminal PTYs are valid even when no in-memory agent session is
    alive. Reader threads therefore cannot rely on ``server._sessions`` to
    locate a websocket transport; the terminal surface refreshes this binding
    whenever it opens or sends an operation.
    """
    key = str(session_key or "").strip()
    if not key or transport is None:
        return
    with _terminal_owners_lock:
        _terminal_owners[key] = (transport, str(runtime_scope_key or "").strip())


def _emit_bound_terminal_event(
    event: str,
    session_key: str,
    payload: dict[str, Any],
) -> bool:
    with _terminal_owners_lock:
        binding = _terminal_owners.get(session_key)
    if binding is None:
        return False
    transport, runtime_scope_key = binding
    frame = {
        "jsonrpc": "2.0",
        "method": "event",
        "params": {
            "type": event,
            "event_domain": event_domain_for_type(event),
            "session_id": session_key,
            "conversation_session_id": session_key,
            "execution_session_id": session_key,
            "runtime_scope_key": runtime_scope_key or session_key,
            "transient": True,
            "payload": payload,
        },
    }
    try:
        return bool(transport.write(frame))
    except Exception:
        return False


def wire_agent_terminal_events(
    *,
    enabled: bool,
    sessions: Mapping[str, dict[str, Any]],
    sessions_lock: RLock,
    emit: Callable[[str, str, dict[str, Any]], None],
) -> None:
    """Install idempotent process-registry UI sinks.

    Process output originates on reader threads. Routing by ``session_key``
    prevents one Dovie conversation/runtime window from receiving another
    scope's terminal stream.
    """
    from tools.process_registry import process_registry

    if not enabled:
        return

    def owner_session_id(process_session: Any) -> str:
        owner_key = str(getattr(process_session, "session_key", "") or "").strip()
        if not owner_key:
            return ""
        with sessions_lock:
            for sid, live_session in sessions.items():
                if sid == owner_key or str(live_session.get("session_key") or "") == owner_key:
                    return str(sid)
        return ""

    def emit_terminal_event(event: str, process_session: Any, payload: dict[str, Any]) -> None:
        owner_key = str(getattr(process_session, "session_key", "") or "").strip()
        if not owner_key:
            return
        live_owner = owner_session_id(process_session)
        if live_owner:
            emit(event, live_owner, payload)
            return
        _emit_bound_terminal_event(event, owner_key, payload)

    if getattr(process_registry, "on_output", None) is None:
        def on_output(process_session: Any, chunk: str) -> None:
            emit_terminal_event(
                "agent.terminal.output",
                process_session,
                {"process_id": process_session.id, "chunk": chunk},
            )

        process_registry.on_output = on_output

    if getattr(process_registry, "on_close", None) is None:
        def on_close(process_session: Any, process_id: str) -> None:
            if process_session is None:
                return
            emit_terminal_event(
                "terminal.close",
                process_session,
                {"process_id": process_id},
            )

        process_registry.on_close = on_close
