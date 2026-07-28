"""Read-only Gateway projection for workspace verification evidence."""

from __future__ import annotations

from dataclasses import asdict, is_dataclass
from typing import Any

from agent.coding_context import project_facts_for
from tui_gateway.methods._shared import bind_server_globals


_server = bind_server_globals(globals())


def _text(value: Any) -> str:
    return str(value or "").strip()


def _live_workspace(scope_id: str) -> str:
    with _sessions_lock:
        session = _sessions.get(scope_id)
        if not isinstance(session, dict):
            session = next(
                (
                    value
                    for value in _sessions.values()
                    if isinstance(value, dict)
                    and _text(value.get("session_key")) == scope_id
                ),
                None,
            )
        if not isinstance(session, dict):
            return ""
        agent = session.get("agent")
        return _text(
            session.get("cwd")
            or getattr(agent, "session_cwd", "")
            or (session.get("workspace") or {}).get("path")
        )


def _stored_workspace(db: Any, scope_id: str) -> str:
    try:
        row = db.sessions.get(scope_id)
    except Exception:
        return ""
    return _text(row.get("cwd")) if isinstance(row, dict) else ""


def _payload(value: Any) -> dict[str, Any]:
    if value is None:
        return {
            "status": "not_applicable",
            "edit_generation": 0,
            "last_verified_generation": -1,
            "changed_paths": [],
            "evidence": None,
        }
    if is_dataclass(value):
        payload = asdict(value)
        payload["changed_paths"] = list(payload.get("changed_paths") or ())
        return payload
    if isinstance(value, dict):
        return dict(value)
    raise TypeError("verification service returned an unsupported projection")


@method("verification.status")
def verification_status(rid, params: dict) -> dict:
    scope_id = _text(
        params.get("conversation_session_id")
        or params.get("conversationSessionId")
        or params.get("session_id")
        or params.get("sessionId")
    )
    if not scope_id:
        return _err(rid, 4006, "session_id required")
    db = _get_db()
    if db is None:
        return _db_unavailable_error(rid, code=5000)
    cwd = _text(
        params.get("cwd")
        or params.get("workspace_root")
        or params.get("workspaceRoot")
    )
    if not cwd:
        cwd = _live_workspace(scope_id) or _stored_workspace(db, scope_id)
    if not cwd:
        return _ok(
            rid,
            {
                "verification": {
                    "scope_id": scope_id,
                    "workspace_root": "",
                    **_payload(None),
                },
            },
        )
    try:
        result = db.verification.status(scope_id, cwd)
        payload = _payload(result)
    except Exception as exc:
        return _err(rid, 5000, f"verification status failed: {exc}")
    payload.setdefault("scope_id", scope_id)
    payload.setdefault("workspace_root", cwd)
    return _ok(rid, {"verification": payload})


@method("project.facts")
def project_facts(rid, params: dict) -> dict:
    cwd = _text(
        params.get("cwd")
        or params.get("workspace_root")
        or params.get("workspaceRoot")
    )
    try:
        facts = project_facts_for(cwd or None)
    except Exception as exc:
        return _err(rid, 5000, f"project facts failed: {exc}")
    return _ok(rid, {"facts": facts})


__all__ = ["project_facts", "verification_status"]
