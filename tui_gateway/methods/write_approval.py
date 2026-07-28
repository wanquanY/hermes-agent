"""Profile-scoped JSON-RPC review surface for persistent agent writes."""

from __future__ import annotations

from typing import Any

from tui_gateway.methods._shared import bind_server_globals

_server = bind_server_globals(globals())

_SUBSYSTEMS = frozenset({"memory", "skills"})


def _subsystem(params: dict[str, Any]) -> str:
    return str(params.get("subsystem") or "").strip().lower()


def _require_subsystem(rid, params: dict[str, Any]):
    subsystem = _subsystem(params)
    if subsystem not in _SUBSYSTEMS:
        return "", _err(rid, 4002, "subsystem must be 'memory' or 'skills'")
    return subsystem, None


def _public_record(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(record.get("id") or ""),
        "subsystem": str(record.get("subsystem") or ""),
        "action": str(record.get("action") or ""),
        "summary": str(record.get("summary") or ""),
        "origin": str(record.get("origin") or "foreground"),
        "createdAt": float(record.get("created_at") or 0),
    }


def _status_payload(subsystem: str) -> dict[str, Any]:
    from tools import write_approval as wa

    return {
        "subsystem": subsystem,
        "enabled": wa.write_approval_enabled(subsystem),
        "pendingCount": wa.pending_count(subsystem),
    }


@method("write_approval.status")
def _(rid, params: dict) -> dict:
    subsystem = _subsystem(params)
    if subsystem:
        if subsystem not in _SUBSYSTEMS:
            return _err(rid, 4002, "subsystem must be 'memory' or 'skills'")
        return _ok(rid, _status_payload(subsystem))
    return _ok(rid, {"subsystems": [_status_payload(name) for name in sorted(_SUBSYSTEMS)]})


@method("write_approval.list")
def _(rid, params: dict) -> dict:
    subsystem, error = _require_subsystem(rid, params)
    if error:
        return error
    from tools import write_approval as wa

    records = [_public_record(record) for record in wa.list_pending(subsystem)]
    return _ok(
        rid,
        {
            **_status_payload(subsystem),
            "pending": records,
        },
    )


@method("write_approval.detail")
def _(rid, params: dict) -> dict:
    subsystem, error = _require_subsystem(rid, params)
    if error:
        return error
    pending_id = str(params.get("id") or params.get("pending_id") or "").strip()
    if not pending_id:
        return _err(rid, 4002, "pending write id required")

    from tools import write_approval as wa

    record = wa.get_pending(subsystem, pending_id)
    if not record:
        return _err(rid, 4044, "pending write not found")
    result = {"pending": _public_record(record)}
    if subsystem == wa.MEMORY:
        result["payload"] = dict(record.get("payload") or {})
    else:
        result["diff"] = wa.skill_pending_diff(record)
    return _ok(rid, result)


@method("write_approval.approve")
def _(rid, params: dict) -> dict:
    subsystem, error = _require_subsystem(rid, params)
    if error:
        return error
    target = str(params.get("id") or params.get("pending_id") or "").strip()
    if not target and bool(params.get("all")):
        target = "all"
    if not target:
        return _err(rid, 4002, "pending write id required (or all=true)")

    from hermes_cli.write_approval_commands import approve_pending

    memory_store = None
    if subsystem == "memory":
        from tools.memory_tool import load_on_disk_store

        memory_store = load_on_disk_store()
    result = approve_pending(subsystem, target, memory_store=memory_store)
    if result.get("not_found"):
        return _err(rid, 4044, result.get("message") or "pending write not found")
    return _ok(rid, result)


@method("write_approval.reject")
def _(rid, params: dict) -> dict:
    subsystem, error = _require_subsystem(rid, params)
    if error:
        return error
    target = str(params.get("id") or params.get("pending_id") or "").strip()
    if not target and bool(params.get("all")):
        target = "all"
    if not target:
        return _err(rid, 4002, "pending write id required (or all=true)")

    from hermes_cli.write_approval_commands import reject_pending

    result = reject_pending(subsystem, target)
    if result.get("not_found"):
        return _err(rid, 4044, result.get("message") or "pending write not found")
    return _ok(rid, result)


@method("write_approval.configure")
def _(rid, params: dict) -> dict:
    subsystem, error = _require_subsystem(rid, params)
    if error:
        return error
    enabled = params.get("enabled")
    if not isinstance(enabled, bool):
        return _err(rid, 4002, "enabled must be a boolean")

    try:
        from hermes_cli.config import load_config, save_config

        config = load_config()
        section = config.get(subsystem)
        if not isinstance(section, dict):
            section = {}
            config[subsystem] = section
        section["write_approval"] = enabled
        save_config(config)
    except Exception as exc:
        return _err(rid, 5001, f"failed to persist write-approval config: {exc}")
    return _ok(rid, _status_payload(subsystem))
