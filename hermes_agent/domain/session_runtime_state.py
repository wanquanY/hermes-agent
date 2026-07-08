"""Runtime state projection helpers for Hermes session storage."""

from __future__ import annotations

import hashlib
import json
from typing import Any


def json_dumps_compact(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def json_loads(value: str | None, fallback: Any) -> Any:
    if not value:
        return fallback
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return fallback


def session_info_payload_hash(payload: dict[str, Any]) -> str:
    normalized = payload if isinstance(payload, dict) else {}
    return hashlib.sha256(json_dumps_compact(normalized).encode("utf-8")).hexdigest()


def session_info_profile_json(payload: dict[str, Any]) -> str:
    if not isinstance(payload, dict):
        return "{}"
    for key in ("profile", "agent_profile", "agentProfile", "agent_profile_json"):
        value = payload.get(key)
        if isinstance(value, dict):
            return json_dumps_compact(value)
        if isinstance(value, str) and value.strip():
            parsed = json_loads(value, None)
            if isinstance(parsed, dict):
                return json_dumps_compact(parsed)
    return "{}"


def session_info_provider(payload: dict[str, Any]) -> str:
    if not isinstance(payload, dict):
        return ""
    provider = str(payload.get("provider") or payload.get("billing_provider") or "").strip()
    if provider:
        return provider
    descriptor = payload.get("model_descriptor") or payload.get("modelDescriptor")
    if isinstance(descriptor, dict):
        return str(descriptor.get("provider") or descriptor.get("provider_id") or "").strip()
    return ""


def session_info_record(
    *,
    session_id: str,
    payload: dict[str, Any],
    runtime_scope_key: str = "",
    runtime_session_id: str = "",
    run_id: str = "",
    turn_id: str = "",
    updated_at: float = 0.0,
    source_seq: int = 0,
) -> dict[str, Any]:
    normalized_payload = payload if isinstance(payload, dict) else {}
    return {
        "session_id": str(session_id or "").strip(),
        "runtime_scope_key": str(runtime_scope_key or "").strip(),
        "runtime_session_id": str(runtime_session_id or "").strip(),
        "run_id": str(run_id or "").strip(),
        "turn_id": str(turn_id or "").strip(),
        "status": str(normalized_payload.get("status") or "").strip(),
        "model": str(normalized_payload.get("model") or "").strip(),
        "provider": session_info_provider(normalized_payload),
        "profile_json": session_info_profile_json(normalized_payload),
        "payload_hash": session_info_payload_hash(normalized_payload),
        "updated_at": float(updated_at or 0.0),
        "source_seq": int(source_seq or 0),
    }


def session_runtime_state_from_row(row: Any) -> dict[str, Any]:
    if row is None:
        return {}

    def value(key: str, default: Any = "") -> Any:
        try:
            return row[key]
        except (KeyError, IndexError, TypeError):
            return default

    return {
        "session_id": str(value("session_id") or ""),
        "runtime_scope_key": str(value("runtime_scope_key") or ""),
        "runtime_session_id": str(value("runtime_session_id") or ""),
        "run_id": str(value("run_id") or ""),
        "turn_id": str(value("turn_id") or ""),
        "status": str(value("status") or ""),
        "model": str(value("model") or ""),
        "provider": str(value("provider") or ""),
        "profile": json_loads(value("profile_json"), {}),
        "payload_hash": str(value("payload_hash") or ""),
        "updated_at": float(value("updated_at", 0) or 0),
        "source_seq": int(value("source_seq", 0) or 0),
    }


def session_runtime_identity_matches(row: Any, record: dict[str, Any]) -> bool:
    if row is None:
        return False
    for key in ("runtime_scope_key", "runtime_session_id", "run_id", "turn_id"):
        try:
            current = row[key]
        except (KeyError, IndexError, TypeError):
            current = ""
        if str(current or "").strip() != str(record.get(key) or "").strip():
            return False
    try:
        current_hash = row["payload_hash"]
    except (KeyError, IndexError, TypeError):
        current_hash = ""
    return str(current_hash or "").strip() == str(record.get("payload_hash") or "").strip()


__all__ = [
    "json_dumps_compact",
    "json_loads",
    "session_info_payload_hash",
    "session_info_profile_json",
    "session_info_provider",
    "session_info_record",
    "session_runtime_identity_matches",
    "session_runtime_state_from_row",
]
