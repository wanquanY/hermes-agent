"""Provider-bound request reconciliation for Dovie team participants."""

from __future__ import annotations

import copy
from datetime import datetime
import logging
from pathlib import Path
import re
from typing import Any, Dict, Optional

from utils import atomic_json_write, env_var_enabled


logger = logging.getLogger(__name__)

_TRANSPORT_ONLY_KEYS = {
    "api_key",
    "base_url",
    "default_headers",
    "extra_headers",
    "headers",
    "timeout",
}


def _audit_enabled() -> bool:
    """Enable reconciliation alongside the existing Dovie stream trace."""
    return env_var_enabled("DOVIE_TEAM_REQUEST_AUDIT") or env_var_enabled(
        "DOVIE_STREAM_TRACE"
    )


def _safe_filename_part(value: Any, fallback: str) -> str:
    text = str(value or "").strip()
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", text).strip("-._")
    return safe[:160] or fallback


def _run_context_payload(context: Any) -> dict[str, Any]:
    to_payload = getattr(context, "to_payload", None)
    payload = to_payload() if callable(to_payload) else {}
    if isinstance(payload, dict) and payload:
        return payload
    return {
        key: getattr(context, key, "")
        for key in (
            "conversation_session_id",
            "participant_id",
            "activity_id",
            "activity_kind",
            "execution_scope_key",
            "control_home",
            "execution_home",
            "profile_id",
            "memory_namespace",
            "context_snapshot_id",
        )
        if getattr(context, key, "") not in (None, "")
    }


def dump_team_inference_request_audit(
    agent: Any,
    api_kwargs: Dict[str, Any],
    *,
    api_call_count: int,
    retry_count: int,
) -> Optional[Path]:
    """Persist the final provider body for a Leader/member inference call.

    The shared transcript, actor-scoped system prompt, tools and model
    parameters have all been assembled at this boundary. Transport-only values
    and HTTP headers are deliberately omitted because they are not provider
    request-body fields and may contain credentials.
    """
    if not _audit_enabled():
        return None
    try:
        context = agent._active_run_context()
    except Exception:
        context = None
    if context is None:
        return None
    participant_id = str(getattr(context, "participant_id", "") or "").strip()
    if not participant_id.startswith(("leader:", "member:")):
        return None

    try:
        context_payload = _run_context_payload(context)
        raw_body = copy.deepcopy(api_kwargs or {})
        omitted_transport_keys = sorted(
            key for key in raw_body if key in _TRANSPORT_ONLY_KEYS
        )
        body = {
            key: value
            for key, value in raw_body.items()
            if key not in _TRANSPORT_ONLY_KEYS and value is not None
        }
        conversation_session_id = str(
            context_payload.get("conversation_session_id")
            or getattr(agent, "session_id", "")
            or ""
        ).strip()
        run_id = str(getattr(agent, "_hermes_active_run_id", "") or "").strip()
        turn_id = str(getattr(agent, "_hermes_active_turn_id", "") or "").strip()
        actor_kind = "leader" if participant_id.startswith("leader:") else "member"
        now = datetime.now().astimezone()
        control_home = Path(
            str(context_payload.get("control_home") or "").strip()
            or str(getattr(agent, "logs_dir", Path.cwd()))
        ).expanduser()
        audit_dir = control_home / "logs" / "team-request-audit" / now.strftime("%Y-%m-%d")
        filename = "__".join(
            (
                now.strftime("%Y%m%dT%H%M%S_%f%z"),
                actor_kind,
                _safe_filename_part(participant_id, actor_kind),
                _safe_filename_part(run_id, "run-unknown"),
                f"call-{max(0, int(api_call_count))}",
                f"retry-{max(0, int(retry_count))}",
            )
        ) + ".json"
        audit_file = audit_dir / filename
        payload = {
            "schema_version": "dovie-team-inference-request-v1",
            "captured_at": now.isoformat(),
            "conversation_session_id": conversation_session_id,
            "participant_id": participant_id,
            "actor_kind": actor_kind,
            "run_id": run_id,
            "turn_id": turn_id,
            "api_call_count": int(api_call_count),
            "retry_count": int(retry_count),
            "provider": str(getattr(agent, "provider", "") or ""),
            "api_mode": str(getattr(agent, "api_mode", "") or ""),
            "request_url": (
                f"{str(getattr(agent, 'base_url', '') or '').rstrip('/')}"
                f"{'/responses' if getattr(agent, 'api_mode', '') == 'codex_responses' else '/chat/completions'}"
            ),
            "run_context": context_payload,
            "request_body": body,
            "omitted_transport_keys": omitted_transport_keys,
        }
        atomic_json_write(audit_file, payload, default=str)
        logger.info(
            "Team inference request audit written: participant=%s run=%s file=%s",
            participant_id,
            run_id,
            audit_file,
        )
        return audit_file
    except Exception as dump_error:
        logger.warning("Failed to write team inference request audit: %s", dump_error)
        return None


__all__ = ["dump_team_inference_request_audit"]
