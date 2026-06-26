"""Team Mission failure classification and recoverability policy."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


REASON_MODEL_OUTPUT_TRUNCATED = "model_output_truncated"
REASON_TOOL_ARGS_TRUNCATED = "tool_args_truncated"
REASON_TOOL_RESULT_OVERSIZED = "tool_result_oversized"
REASON_PROVIDER_RATE_LIMITED = "provider_rate_limited"
REASON_STREAM_DROPPED = "stream_dropped"
REASON_TOOL_JSON_INVALID = "tool_json_invalid"
REASON_WORKER_CRASHED = "worker_crashed"
REASON_PROTOCOL_VIOLATION = "protocol_violation"
REASON_HEARTBEAT_STALE = "heartbeat_stale"
REASON_TIMEOUT = "timeout"
REASON_CANCELLED = "cancelled"
REASON_INTERRUPTED = "interrupted"
REASON_UNKNOWN_FAILURE = "unknown_failure"


def _text(value: Any) -> str:
    return str(value or "").strip()


def _payload_error_text(payload: Mapping[str, Any]) -> str:
    parts: list[str] = []
    for key in (
        "error",
        "message",
        "final_response",
        "finalResponse",
        "content",
        "text",
        "status",
        "reason",
    ):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            parts.append(value.strip())
        elif isinstance(value, Mapping):
            for nested_key in ("message", "error", "code", "reason"):
                nested = value.get(nested_key)
                if isinstance(nested, str) and nested.strip():
                    parts.append(nested.strip())
    return "\n".join(parts)


def _nested_mapping(payload: Mapping[str, Any], *keys: str) -> Mapping[str, Any]:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, Mapping):
            return value
    return {}


def _payload_error_kind(payload: Mapping[str, Any]) -> str:
    error = _nested_mapping(payload, "error", "exception", "provider_error", "providerError")
    candidates = (
        payload.get("kind"),
        payload.get("error_kind"),
        payload.get("errorKind"),
        payload.get("category"),
        payload.get("type"),
        payload.get("code"),
        payload.get("status_code"),
        payload.get("statusCode"),
        error.get("kind"),
        error.get("error_kind"),
        error.get("errorKind"),
        error.get("category"),
        error.get("type"),
        error.get("code"),
        error.get("status_code"),
        error.get("statusCode"),
    )
    return " ".join(_text(item).lower() for item in candidates if _text(item))


def _retry_after_seconds(payload: Mapping[str, Any]) -> float:
    error = _nested_mapping(payload, "error", "exception", "provider_error", "providerError")
    for value in (
        payload.get("retry_after_s"),
        payload.get("retryAfterS"),
        payload.get("retry_after"),
        payload.get("retryAfter"),
        error.get("retry_after_s"),
        error.get("retryAfterS"),
        error.get("retry_after"),
        error.get("retryAfter"),
    ):
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            continue
        if parsed > 0:
            return parsed
    return 0.0


def recoverability_for_reason(reason_code: str) -> str:
    reason_code = _text(reason_code)
    if reason_code in {
        REASON_PROVIDER_RATE_LIMITED,
        REASON_STREAM_DROPPED,
        REASON_HEARTBEAT_STALE,
        REASON_MODEL_OUTPUT_TRUNCATED,
    }:
        return "retryable"
    if reason_code in {
        REASON_TOOL_ARGS_TRUNCATED,
        REASON_TOOL_RESULT_OVERSIZED,
        REASON_TOOL_JSON_INVALID,
        REASON_PROTOCOL_VIOLATION,
    }:
        return "blocked"
    if reason_code == REASON_TIMEOUT:
        return "retryable"
    if reason_code in {REASON_CANCELLED, REASON_INTERRUPTED}:
        return "terminal"
    return "blocked"


def classify_team_mission_failure(event_type: str, payload: Mapping[str, Any] | None) -> dict[str, Any]:
    event_type = _text(event_type)
    payload = payload if isinstance(payload, Mapping) else {}
    status = _text(payload.get("status")).lower()
    error_text = _payload_error_text(payload)
    normalized = error_text.lower()
    error_kind = _payload_error_kind(payload)
    reason_code = ""
    if status in {"cancelled", "canceled"} or event_type == "session.cancelled":
        reason_code = REASON_CANCELLED
    elif status == "interrupted":
        reason_code = REASON_INTERRUPTED
    elif "response truncated due to output length limit" in normalized:
        reason_code = REASON_TOOL_ARGS_TRUNCATED
    elif "truncated tool call" in normalized or "incomplete tool arguments" in normalized:
        reason_code = REASON_TOOL_ARGS_TRUNCATED
    elif "model hit max output tokens" in normalized or "remained truncated" in normalized:
        reason_code = REASON_MODEL_OUTPUT_TRUNCATED
    elif "tool result" in normalized and ("too large" in normalized or "oversized" in normalized):
        reason_code = REASON_TOOL_RESULT_OVERSIZED
    elif (
        "rate_limit" in error_kind
        or "rate-limited" in error_kind
        or "rate_limited" in error_kind
        or "too_many_requests" in error_kind
        or "throttl" in error_kind
        or "429" in error_kind
        or "rate limit" in normalized
        or "rate_limited" in normalized
        or "quota" in normalized
        or "requests per minute" in normalized
        or "tokens per minute" in normalized
        or "tpm limit" in normalized
        or "rpm limit" in normalized
        or "too many requests" in normalized
        or "throttl" in normalized
        or "429" in normalized
    ):
        reason_code = REASON_PROVIDER_RATE_LIMITED
    elif "stream" in normalized and ("dropped" in normalized or "interrupted" in normalized or "closed" in normalized):
        reason_code = REASON_STREAM_DROPPED
    elif "invalid json" in normalized:
        reason_code = REASON_TOOL_JSON_INVALID
    elif (
        "worker crashed" in normalized
        or "worker exited" in normalized
        or "process crashed" in normalized
        or "run_worker" in normalized and ("crashed" in normalized or "exited" in normalized)
    ):
        reason_code = REASON_WORKER_CRASHED
    elif "protocol violation" in normalized or "without calling" in normalized:
        reason_code = REASON_PROTOCOL_VIOLATION
    elif "heartbeat" in normalized and ("stale" in normalized or "expired" in normalized):
        reason_code = REASON_HEARTBEAT_STALE
    elif "timeout" in normalized or "timed out" in normalized:
        reason_code = REASON_TIMEOUT
    elif event_type == "error" or status in {"failed", "error"}:
        reason_code = REASON_UNKNOWN_FAILURE
    if not reason_code:
        return {}
    result: dict[str, Any] = {
        "reason_code": reason_code,
        "recoverability": recoverability_for_reason(reason_code),
        "message": error_text[:1000],
    }
    retry_after_s = _retry_after_seconds(payload)
    if retry_after_s > 0:
        result["retry_after_s"] = retry_after_s
    return result
