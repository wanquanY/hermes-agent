"""Content-free provider telemetry for Dovie conversation diagnostics.

This module owns the contract at the only layer that can observe the real
provider request lifecycle.  Events are live-only: the gateway worker bridge
forwards them to the main process, ``runtime_event_protocol`` classifies them
as diagnostics, and no row is added to the durable conversation ledger.

Only identifiers, bounded categorical labels, counters, and timings are
allowed.  Prompt/response text, URLs, headers, credentials, and exception
messages must never enter this contract.
"""

from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any


PROVIDER_TELEMETRY_EVENT_TYPE = "runtime.provider.telemetry"
PROVIDER_TELEMETRY_SCHEMA = "hermes.provider-telemetry.v1"

_SAFE_LABEL = re.compile(r"^[A-Za-z0-9_.:/+-]{1,128}$")
_VALID_STAGES = frozenset(
    {
        "provider.call.started",
        "provider.call.completed",
        "provider.call.failed",
        "provider.call.cancelled",
        "provider.attempt.started",
        "provider.response.headers",
        "provider.response.first_event",
        "provider.response.first_delta",
        "provider.attempt.retry_scheduled",
        "provider.attempt.failed",
        "provider.stream.stalled",
        "provider.fallback.activated",
        "provider.fallback.exhausted",
    }
)


def _bounded_label(value: Any) -> str:
    text = str(value or "").strip()
    return text if _SAFE_LABEL.fullmatch(text) else ""


def _non_negative_number(value: Any) -> float | int | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number < 0 or number != number or number in {float("inf"), float("-inf")}:
        return None
    if number.is_integer():
        return int(number)
    return round(number, 3)


def _error_class(error: BaseException | None) -> str:
    if error is None:
        return ""
    return _bounded_label(type(error).__name__)


def classify_provider_failure(error: BaseException | None) -> str:
    """Return a stable category without copying an exception message."""
    if error is None:
        return ""
    name = type(error).__name__.lower()
    if isinstance(error, InterruptedError) or "cancel" in name or "interrupt" in name:
        return "interrupted"
    if "timeout" in name:
        return "timeout"
    if any(token in name for token in ("connect", "network", "socket")):
        return "connection"
    if any(token in name for token in ("protocol", "stream", "parse", "decode")):
        return "protocol"
    status_code_value = getattr(error, "status_code", None)
    try:
        status_code = int(status_code_value) if status_code_value is not None else 0
    except (TypeError, ValueError):
        status_code = 0
    if status_code == 429:
        return "rate_limit"
    if status_code in {401, 403}:
        return "authentication"
    if status_code >= 500:
        return "upstream"
    return "provider_error"


def _active_identity(agent: Any) -> dict[str, str]:
    context = None
    active_context = getattr(agent, "_active_run_context", None)
    if callable(active_context):
        try:
            context = active_context()
        except Exception:
            context = None
    conversation_session_id = str(
        getattr(context, "conversation_session_id", "") if context is not None else ""
    ).strip()
    conversation_session_id = conversation_session_id or str(
        getattr(agent, "session_id", "") or ""
    ).strip()
    runtime_scope_key = str(
        getattr(agent, "_hermes_active_runtime_scope_key", "") or ""
    ).strip()
    if not runtime_scope_key and context is not None:
        runtime_scope_key = str(
            getattr(context, "execution_scope_key", "") or ""
        ).strip()
    return {
        "conversation_session_id": conversation_session_id,
        "execution_session_id": str(getattr(agent, "session_id", "") or "").strip(),
        "runtime_scope_key": runtime_scope_key,
        "run_id": str(getattr(agent, "_hermes_active_run_id", "") or "").strip(),
        "turn_id": str(getattr(agent, "_hermes_active_turn_id", "") or "").strip(),
        "activity_id": str(
            getattr(context, "activity_id", "") if context is not None else ""
        ).strip(),
        "participant_id": str(
            getattr(context, "participant_id", "") if context is not None else ""
        ).strip(),
    }


def emit_provider_telemetry(
    agent: Any,
    stage: str,
    *,
    provider_call_id: str = "",
    labels: dict[str, Any] | None = None,
    metrics: dict[str, Any] | None = None,
    error: BaseException | None = None,
) -> None:
    """Publish one allowlisted, live-only provider diagnostic event."""
    normalized_stage = str(stage or "").strip()
    if normalized_stage not in _VALID_STAGES:
        return
    identity = _active_identity(agent)
    if not identity["run_id"] and not identity["runtime_scope_key"]:
        return

    safe_labels: dict[str, str] = {}
    for key, value in (labels or {}).items():
        safe_key = _bounded_label(key)
        safe_value = _bounded_label(value)
        if safe_key and safe_value:
            safe_labels[safe_key] = safe_value
    safe_labels.setdefault("provider", _bounded_label(getattr(agent, "provider", "")))
    safe_labels.setdefault("model", _bounded_label(getattr(agent, "model", "")))
    safe_labels.setdefault("api_mode", _bounded_label(getattr(agent, "api_mode", "")))
    if error is not None:
        safe_labels.setdefault("error_class", _error_class(error))
        safe_labels.setdefault("reason_code", classify_provider_failure(error))
    safe_labels = {key: value for key, value in safe_labels.items() if value}

    safe_metrics: dict[str, float | int] = {}
    for key, value in (metrics or {}).items():
        safe_key = _bounded_label(key)
        safe_value = _non_negative_number(value)
        if safe_key and safe_value is not None:
            safe_metrics[safe_key] = safe_value
    if error is not None and "status_code" not in safe_metrics:
        status_code = _non_negative_number(getattr(error, "status_code", None))
        if status_code is not None:
            safe_metrics["status_code"] = status_code

    payload = {
        "schema": PROVIDER_TELEMETRY_SCHEMA,
        "stage": normalized_stage,
        "provider_call_id": str(provider_call_id or "").strip()[:256],
        "labels": safe_labels,
        "metrics": safe_metrics,
    }
    params = {
        "type": PROVIDER_TELEMETRY_EVENT_TYPE,
        "event_domain": "diagnostics",
        "transient": True,
        **{key: value for key, value in identity.items() if value},
        "payload": payload,
    }
    try:
        from tui_gateway.services import run_control

        run_control.publish_recorded_event(params, persist=False)
    except Exception:
        # Observability is strictly non-interfering with inference.
        return


@dataclass
class ProviderCallTelemetry:
    """Thread-safe telemetry state for one logical LLM provider call."""

    agent: Any
    provider_call_id: str
    logical_attempt: int
    max_logical_attempts: int
    stream_mode: str
    started_at: float = field(default_factory=time.monotonic)
    _network_started_at: dict[int, float] = field(default_factory=dict)
    _headers_attempts: set[int] = field(default_factory=set)
    _first_event_attempts: set[int] = field(default_factory=set)
    _first_delta_attempts: set[int] = field(default_factory=set)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    @classmethod
    def start(
        cls,
        agent: Any,
        *,
        logical_attempt: int,
        max_logical_attempts: int,
        stream_mode: str,
    ) -> "ProviderCallTelemetry":
        request_id = str(getattr(agent, "_current_api_request_id", "") or "").strip()
        context = cls(
            agent=agent,
            provider_call_id=(
                f"{request_id}:logical:{max(1, int(logical_attempt or 1))}"
                if request_id
                else f"provider-call:{time.time_ns()}"
            ),
            logical_attempt=max(1, int(logical_attempt or 1)),
            max_logical_attempts=max(1, int(max_logical_attempts or 1)),
            stream_mode=_bounded_label(stream_mode) or "unknown",
        )
        context._emit("provider.call.started")
        return context

    def _base_labels(self, **extra: Any) -> dict[str, Any]:
        return {
            "stream_mode": self.stream_mode,
            **extra,
        }

    def _base_metrics(self, **extra: Any) -> dict[str, Any]:
        return {
            "logical_attempt": self.logical_attempt,
            "max_logical_attempts": self.max_logical_attempts,
            **extra,
        }

    def _emit(
        self,
        stage: str,
        *,
        labels: dict[str, Any] | None = None,
        metrics: dict[str, Any] | None = None,
        error: BaseException | None = None,
    ) -> None:
        emit_provider_telemetry(
            self.agent,
            stage,
            provider_call_id=self.provider_call_id,
            labels=self._base_labels(**(labels or {})),
            metrics=self._base_metrics(**(metrics or {})),
            error=error,
        )

    def begin_network_attempt(self, attempt: int, max_attempts: int) -> None:
        attempt = max(1, int(attempt or 1))
        with self._lock:
            self._network_started_at[attempt] = time.monotonic()
        self._emit(
            "provider.attempt.started",
            metrics={
                "network_attempt": attempt,
                "max_network_attempts": max(1, int(max_attempts or 1)),
            },
        )

    def observe_first_event(self, attempt: int) -> None:
        attempt = max(1, int(attempt or 1))
        with self._lock:
            if attempt in self._first_event_attempts:
                return
            self._first_event_attempts.add(attempt)
            started_at = self._network_started_at.get(attempt, self.started_at)
        self._emit(
            "provider.response.first_event",
            metrics={
                "network_attempt": attempt,
                "time_to_first_event_ms": (time.monotonic() - started_at) * 1_000,
            },
        )

    def observe_headers(self, attempt: int, response: Any = None) -> None:
        attempt = max(1, int(attempt or 1))
        with self._lock:
            if attempt in self._headers_attempts:
                return
            self._headers_attempts.add(attempt)
            started_at = self._network_started_at.get(attempt, self.started_at)
        status_code = _non_negative_number(getattr(response, "status_code", None))
        self._emit(
            "provider.response.headers",
            metrics={
                "network_attempt": attempt,
                "time_to_headers_ms": (time.monotonic() - started_at) * 1_000,
                **({"status_code": status_code} if status_code is not None else {}),
            },
        )

    def observe_first_delta(self, attempt: int) -> None:
        attempt = max(1, int(attempt or 1))
        with self._lock:
            if attempt in self._first_delta_attempts:
                return
            self._first_delta_attempts.add(attempt)
            started_at = self._network_started_at.get(attempt, self.started_at)
        self._emit(
            "provider.response.first_delta",
            metrics={
                "network_attempt": attempt,
                "time_to_first_delta_ms": (time.monotonic() - started_at) * 1_000,
            },
        )

    def retry_scheduled(
        self,
        *,
        failed_attempt: int,
        next_attempt: int,
        max_attempts: int,
        error: BaseException,
        retry_kind: str,
        retry_delay_ms: float = 0,
    ) -> None:
        self._emit(
            "provider.attempt.failed",
            metrics={
                "network_attempt": failed_attempt,
                "max_network_attempts": max_attempts,
            },
            error=error,
        )
        self._emit(
            "provider.attempt.retry_scheduled",
            labels={"retry_kind": retry_kind},
            metrics={
                "network_attempt": next_attempt,
                "max_network_attempts": max_attempts,
                "retry_delay_ms": retry_delay_ms,
            },
            error=error,
        )

    def stream_stalled(self, *, attempt: int, stalled_ms: float) -> None:
        self._emit(
            "provider.stream.stalled",
            metrics={"network_attempt": attempt, "stalled_ms": stalled_ms},
            labels={"reason_code": "stream_stalled"},
        )

    def completed(self) -> None:
        self._emit(
            "provider.call.completed",
            metrics={"duration_ms": (time.monotonic() - self.started_at) * 1_000},
        )

    def failed(self, error: BaseException) -> None:
        self._emit(
            "provider.call.failed",
            metrics={"duration_ms": (time.monotonic() - self.started_at) * 1_000},
            error=error,
        )

    def cancelled(self, error: BaseException | None = None) -> None:
        self._emit(
            "provider.call.cancelled",
            metrics={"duration_ms": (time.monotonic() - self.started_at) * 1_000},
            error=error,
        )


def current_provider_telemetry(agent: Any) -> ProviderCallTelemetry | None:
    context = getattr(agent, "_provider_call_telemetry", None)
    return context if isinstance(context, ProviderCallTelemetry) else None


__all__ = [
    "PROVIDER_TELEMETRY_EVENT_TYPE",
    "PROVIDER_TELEMETRY_SCHEMA",
    "ProviderCallTelemetry",
    "classify_provider_failure",
    "current_provider_telemetry",
    "emit_provider_telemetry",
]
