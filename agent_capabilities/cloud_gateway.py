"""Allowlisted HTTP transport for Dovie agent capabilities."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

from agent_capabilities.credentials import CapabilityCredential


_TASK_HUB_PATHS = {
    "task_hub_search": "/api/v1/agent-capabilities/task-hub/v1/search",
    "task_hub_get_details": "/api/v1/agent-capabilities/task-hub/v1/details",
    "task_hub_list_my_tasks": "/api/v1/agent-capabilities/task-hub/v1/my-tasks",
    "task_hub_prepare_changes": "/api/v1/agent-capabilities/task-hub/v1/changes:prepare",
}


class CapabilityGatewayError(RuntimeError):
    def __init__(self, code: str, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


def call_task_hub_gateway(
    *,
    tool_name: str,
    arguments: dict[str, Any],
    tool_call_id: str,
    credential: CapabilityCredential,
    timeout_seconds: float = 20.0,
) -> dict[str, Any]:
    path = _TASK_HUB_PATHS.get(tool_name)
    if path is None:
        raise CapabilityGatewayError("unsupported_tool", "Task Hub capability tool is not allowlisted")
    body = {
        **arguments,
        "tool_call_id": str(tool_call_id or "").strip() or "unknown-tool-call",
        "conversation_id": credential.conversation_id,
        "execution_participant_id": credential.execution_participant_id,
    }
    request = urllib.request.Request(
        f"{credential.api_origin}{path}",
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {credential.token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            detail = json.loads(raw)
        except json.JSONDecodeError:
            detail = {}
        code = _error_code(detail) or ("credential_expired" if exc.code == 401 else "gateway_rejected")
        raise CapabilityGatewayError(code, "Task Hub gateway rejected the request", status=exc.code) from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise CapabilityGatewayError("gateway_unavailable", "Task Hub gateway is unavailable") from exc
    except json.JSONDecodeError as exc:
        raise CapabilityGatewayError("protocol_mismatch", "Task Hub gateway returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise CapabilityGatewayError("protocol_mismatch", "Task Hub gateway returned an invalid payload")
    return payload


def _error_code(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""
    detail = payload.get("detail")
    if isinstance(detail, dict):
        return str(detail.get("error_code") or detail.get("code") or "")
    return str(payload.get("error_code") or payload.get("code") or "")
