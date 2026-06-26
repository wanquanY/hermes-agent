"""Control-plane run registry and event log for the TUI gateway.

The gateway process may host many live runtime containers, but durable UI
state is keyed by the stored conversation session and run id.  This module
keeps that control-plane state out of ``server.py`` so route switching, replay,
and live event delivery do not depend on whichever runtime object happens to
own a session at the moment.
"""

from __future__ import annotations

import json
import logging
import os
import hashlib
import threading
import time
import uuid
from collections import defaultdict, deque
from collections.abc import Callable
from typing import Any, TYPE_CHECKING

from hermes_runtime_event_payloads import primary_deliverable_text
from agent.dovie_diagnostics import emit_dovie_diagnostic
from tui_gateway.services.run_control_events import (
    delta_event_for_subscription as _delta_event_for_subscription,
    event_run_id as _event_run_id,
    event_runtime_scope_key as _event_runtime_scope_key,
    event_turn_id as _event_turn_id,
    payload_status as _payload_status,
    remember_terminal_delivery as _remember_direct_terminal_delivery,
    stable_session_id as _stable_session_id,
    stream_text_delta as _stream_text_delta,
    terminal_delivery_identity as _terminal_delivery_identity,
)
from tui_gateway.transport import Transport

if TYPE_CHECKING:
    from hermes_team_mission.domain.run_context import RunContext

try:
    from hermes_state_runs import ACTIVE_RUN_STATUSES, TERMINAL_RUN_STATUSES
except Exception:  # pragma: no cover - keeps gateway importable in mocked tests.
    ACTIVE_RUN_STATUSES = {
        "queued",
        "starting",
        "running",
        "waiting_approval",
        "cancelling",
        "finalizing",
    }
    TERMINAL_RUN_STATUSES = {"completed", "failed", "interrupted", "cancelled"}

_MAX_EVENTS_PER_SESSION = 2000
_POLL_INTERVAL_SECONDS = 0.25
_TEAM_MISSION_RUNTIME_EVENT_TYPE = "team_mission.runtime.event"
_TEAM_MISSION_CONVERSATION_STATUS_EVENT_TYPE = "team_mission.conversation.status"
_TEAM_MISSION_TERMINAL_STATUSES = {"completed", "failed", "cancelled", "canceled", "interrupted"}
_TEAM_MISSION_STATUS_SOURCE_EVENT_TYPES = {
    "message.start",
    "message.complete",
    "error",
    "session.interrupted",
    "session.recalled",
    "mission.strategy.actions",
    "mission.approval.requested",
    "mission.plan.rejected",
    "mission.node.created",
    "mission.node.updated",
    "mission.node.started",
    "mission.node.run.bound",
    "mission.edge.created",
}
_RUN_OPENING_EVENT_TYPES = {
    "message.start",
    "message.delta",
    "reasoning.delta",
    "thinking.delta",
    "tool.start",
    "tool.generating",
    "tool.progress",
    "approval.request",
    "secret.request",
    "sudo.request",
    "input_approval.request",
    "subagent.output_delta",
    "subagent.reasoning_delta",
    "subagent.thinking",
    "agent_profile_test.output_delta",
    "agent_profile_test.thinking",
}

logger = logging.getLogger(__name__)

_STREAM_TRACE_EVENT_TYPES = {
    "message.start",
    "message.delta",
    "message.complete",
    "reasoning.delta",
    "thinking.delta",
}


def _transport_debug_id(transport: Any) -> str:
    if transport is None:
        return ""
    return f"{transport.__class__.__name__}:{id(transport):x}"


def _stream_trace_summary(event: dict[str, Any]) -> dict[str, Any]:
    text = _stream_text_delta(event)
    digest = hashlib.sha1(text.encode("utf-8", errors="replace")).hexdigest()[:12]
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    return {
        "payload_mode": str(payload.get("mode") or ""),
        "payload_len": len(text),
        "payload_sha1": digest,
        "payload_preview": text[:80].replace("\n", "\\n"),
    }


def _trace_stream_route(stage: str, **fields: Any) -> None:
    emit_dovie_diagnostic("[dovie-stream-route]", {"stage": stage, **fields})

_lock = threading.RLock()
_events_by_session: dict[str, deque[dict[str, Any]]] = defaultdict(
    lambda: deque(maxlen=_MAX_EVENTS_PER_SESSION)
)
_subscribers_by_session: dict[str, set[Transport]] = defaultdict(set)
_subscriptions_by_id: dict[str, dict[str, Any]] = {}
_subscription_ids_by_session: dict[str, set[str]] = defaultdict(set)
_subscription_ids_by_mission: dict[str, set[str]] = defaultdict(set)
_subscription_ids_by_transport: dict[Transport, set[str]] = defaultdict(set)
_run_state_by_id: dict[str, dict[str, Any]] = {}
_run_ids_by_session: dict[str, list[str]] = defaultdict(list)
_last_seq_by_session: dict[str, int] = defaultdict(int)
_subscription_poller_thread: threading.Thread | None = None
_team_mission_ready_scheduler: Any = None


def _db_method(db: Any, name: str):
    if db is None or db.__class__.__module__.startswith("unittest.mock"):
        return None
    method = getattr(db, name, None)
    return method if callable(method) else None


def _db_label(db: Any = None) -> str:
    value = getattr(db, "db_path", "") if db is not None else ""
    return str(value or "")


def _json_for_log(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    except Exception:
        return repr(value)


def _diagnostic_warning(label: str, **fields: Any) -> None:
    logger.warning("[dovie-run-control] %s %s", label, _json_for_log(fields))


def _run_summary(run: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(run, dict):
        return {}
    metadata = run.get("metadata") if isinstance(run.get("metadata"), dict) else {}
    return {
        "run_id": str(run.get("run_id") or ""),
        "session_id": str(run.get("session_id") or run.get("stored_session_id") or ""),
        "turn_id": str(run.get("turn_id") or ""),
        "runtime_scope_key": str(run.get("runtime_scope_key") or ""),
        "runtime_session_id": str(run.get("runtime_session_id") or ""),
        "status": str(run.get("status") or ""),
        "updated_at": run.get("updated_at"),
        "gateway_pid": metadata.get("gateway_pid"),
        "gateway_instance_id": metadata.get("gateway_instance_id"),
        "metadata": metadata,
    }


def _gateway_instance_id_from_metadata(metadata: dict[str, Any] | None = None) -> str:
    if not isinstance(metadata, dict):
        return ""
    return str(metadata.get("gateway_instance_id") or "").strip()


def _live_runtime_session_ids_snapshot() -> set[str]:
    with _lock:
        return {
            str(run.get("session_id") or "").strip()
            for run in _run_state_by_id.values()
            if str(run.get("status") or "") in ACTIVE_RUN_STATUSES
            and str(run.get("session_id") or "").strip()
        }


def _recover_orphaned_active_runs(
    db: Any = None,
    *,
    current_gateway_instance_id: str = "",
    stale_after_seconds: float = 300.0,
) -> int:
    method = _db_method(db, "fail_orphaned_active_runs")
    if method is None:
        return 0
    try:
        failed = int(
            method(
                live_runtime_session_ids=_live_runtime_session_ids_snapshot(),
                current_pid=os.getpid(),
                current_gateway_instance_id=str(current_gateway_instance_id or "").strip(),
                stale_after_seconds=stale_after_seconds,
                owner_dead_grace_seconds=2.0,
                reason="gateway process restarted before run reached terminal state",
            )
            or 0
        )
        if failed:
            _diagnostic_warning(
                "orphaned-active-runs-recovered",
                db=_db_label(db),
                failed=failed,
                current_pid=os.getpid(),
                current_gateway_instance_id=str(current_gateway_instance_id or "").strip(),
            )
        return failed
    except Exception as exc:
        _diagnostic_warning(
            "orphaned-active-run-recovery-error",
            db=_db_label(db),
            error=str(exc),
            current_pid=os.getpid(),
            current_gateway_instance_id=str(current_gateway_instance_id or "").strip(),
        )
        return 0


def register_team_mission_ready_scheduler(callback: Any) -> None:
    global _team_mission_ready_scheduler
    with _lock:
        _team_mission_ready_scheduler = callback


def _dispatch_team_mission_ready_scheduler(
    *,
    mission_id: str,
    db: Any = None,
    trigger_event: str = "",
    run_id: str = "",
) -> None:
    normalized_mission_id = str(mission_id or "").strip()
    if not normalized_mission_id:
        return
    with _lock:
        callback = _team_mission_ready_scheduler
    if not callable(callback):
        return
    try:
        callback(
            mission_id=normalized_mission_id,
            db=db,
            trigger_event=trigger_event,
            run_id=run_id,
        )
    except Exception:
        logger.warning("failed to schedule Team Mission ready nodes", exc_info=True)


def _remember_subscription_delivery(
    subscription: dict[str, Any],
    event: dict[str, Any],
    *,
    direct: bool = False,
) -> None:
    try:
        seq = int(event.get("seq") or 0)
    except (TypeError, ValueError):
        seq = 0
    if seq > 0:
        subscription["last_seq"] = max(int(subscription.get("last_seq") or 0), seq)
    _remember_subscription_run(subscription, event)
    if direct or _terminal_delivery_identity(event):
        _remember_direct_terminal_delivery(subscription, event)


def _reserve_subscription_delivery(
    subscription: dict[str, Any],
    event: dict[str, Any],
) -> bool:
    try:
        seq = int(event.get("seq") or 0)
    except (TypeError, ValueError):
        seq = 0
    if seq <= 0:
        return True
    subscription_id = str(subscription.get("id") or "").strip()
    if not subscription_id:
        return True
    with _lock:
        current = _subscriptions_by_id.get(subscription_id)
        if current is None:
            return False
        if seq <= int(current.get("last_seq") or 0):
            return False
        current["last_seq"] = seq
    return True


def remember_transport_delivery(
    transport: Transport | None,
    event: dict[str, Any],
    *,
    direct: bool = True,
) -> None:
    if transport is None or not isinstance(event, dict):
        return
    stable = _stable_session_id(event)
    event_run_id = _event_run_id(event)
    with _lock:
        for subscription_id in list(_subscription_ids_by_transport.get(transport, set())):
            subscription = _subscriptions_by_id.get(subscription_id)
            if not isinstance(subscription, dict):
                continue
            if subscription.get("transport") is not transport:
                continue
            kind = str(subscription.get("kind") or "session")
            if kind == "session":
                if stable and str(subscription.get("stored_session_id") or "").strip() != stable:
                    continue
                if not _session_subscription_matches_event(subscription, event):
                    continue
            elif kind == "team_mission":
                payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
                event_mission_id = str(event.get("mission_id") or payload.get("mission_id") or "").strip()
                if not event_mission_id and event_run_id:
                    binding_getter = _db_method(subscription.get("db"), "get_team_mission_run_binding")
                    if binding_getter is not None:
                        try:
                            binding = binding_getter(event_run_id)
                        except Exception:
                            binding = {}
                        if isinstance(binding, dict):
                            event_mission_id = str(binding.get("mission_id") or "").strip()
                if str(subscription.get("mission_id") or "").strip() != event_mission_id:
                    continue
                _remember_subscription_run(subscription, event)
                continue
            _remember_subscription_delivery(subscription, event, direct=direct)


def _event_for_live_subscription_delivery(
    transport: Transport,
    event: dict[str, Any],
) -> dict[str, Any] | None:
    """Project a persisted event back to the live subscription ABI."""
    if transport is None or not isinstance(event, dict):
        return event
    stable = _stable_session_id(event)
    event_run_id = _event_run_id(event)
    with _lock:
        subscriptions = [
            _subscriptions_by_id.get(subscription_id)
            for subscription_id in list(_subscription_ids_by_transport.get(transport, set()))
        ]
    for subscription in subscriptions:
        if not isinstance(subscription, dict):
            continue
        if subscription.get("transport") is not transport:
            continue
        kind = str(subscription.get("kind") or "session")
        if kind == "session":
            if stable and str(subscription.get("stored_session_id") or "").strip() != stable:
                continue
            if not _session_subscription_matches_event(subscription, event):
                continue
        elif kind == "team_mission":
            payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
            event_mission_id = str(event.get("mission_id") or payload.get("mission_id") or "").strip()
            if not event_mission_id and event_run_id:
                binding_getter = _db_method(subscription.get("db"), "get_team_mission_run_binding")
                if binding_getter is not None:
                    try:
                        binding = binding_getter(event_run_id)
                    except Exception:
                        binding = {}
                    if isinstance(binding, dict):
                        event_mission_id = str(binding.get("mission_id") or "").strip()
            if str(subscription.get("mission_id") or "").strip() != event_mission_id:
                continue
        else:
            continue
        event_for_transport = _delta_event_for_subscription(subscription, event)
        if event_for_transport is None:
            _reserve_subscription_delivery(subscription, event)
            return None
        event_for_transport = _team_mission_live_status_event_for_subscription(subscription, event_for_transport)
        if not _reserve_subscription_delivery(subscription, event_for_transport):
            return None
        return event_for_transport
    return event


def _team_mission_event_mission_id(event: dict[str, Any]) -> str:
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    return str(
        event.get("mission_id")
        or event.get("missionId")
        or payload.get("mission_id")
        or payload.get("missionId")
        or ""
    ).strip()


def _is_team_mission_runtime_event(event: dict[str, Any]) -> bool:
    return str(event.get("type") or "").strip() == _TEAM_MISSION_RUNTIME_EVENT_TYPE


def _session_subscription_matches_event(
    subscription: dict[str, Any],
    event: dict[str, Any],
) -> bool:
    if _is_team_mission_runtime_event(event):
        return False
    return _event_matches_subscription(subscription, event)


def _team_mission_live_status_event_for_subscription(
    subscription: dict[str, Any],
    event: dict[str, Any],
) -> dict[str, Any]:
    # Canonical replay keeps the reducer-after-write projection. Live subscribers
    # need the source run to remain visible for the status frame emitted at the
    # terminal event boundary, before the UI receives the follow-up idle state.
    if str(event.get("type") or "").strip() != _TEAM_MISSION_CONVERSATION_STATUS_EVENT_TYPE:
        return event
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    conversation = payload.get("conversation") if isinstance(payload.get("conversation"), dict) else {}
    if not conversation or bool(conversation.get("running")):
        return event
    mission_status = str(conversation.get("mission_status") or payload.get("mission_status") or "").strip()
    if mission_status in _TEAM_MISSION_TERMINAL_STATUSES:
        return event
    source_type = str(payload.get("source_event_type") or payload.get("sourceEventType") or "").strip()
    if source_type != "message.complete":
        return event
    source_run_id = str(
        event.get("source_run_id")
        or event.get("sourceRunId")
        or payload.get("source_run_id")
        or payload.get("sourceRunId")
        or ""
    ).strip()
    if not source_run_id:
        return event
    binding_getter = _db_method(subscription.get("db"), "get_team_mission_run_binding")
    binding = binding_getter(source_run_id) if binding_getter is not None else {}
    if not isinstance(binding, dict) or not binding:
        return event
    run_getter = _db_method(subscription.get("db"), "get_run")
    run = run_getter(source_run_id) if run_getter is not None else {}
    run = run if isinstance(run, dict) else {}

    live_conversation = dict(conversation)
    live_conversation["running"] = True
    live_conversation["run_state"] = "running"
    live_conversation["activity_state"] = "running"
    live_conversation["active_run_id"] = source_run_id
    live_conversation["active_turn_id"] = str(run.get("turn_id") or binding.get("turn_id") or "")
    live_conversation["active_runtime_session_id"] = str(
        run.get("runtime_session_id") or binding.get("runtime_session_id") or ""
    )
    live_conversation["runtime_scope_key"] = str(
        run.get("runtime_scope_key") or binding.get("runtime_scope_key") or ""
    )
    live_conversation["run_started_at"] = run.get("started_at") or live_conversation.get("run_started_at") or 0
    live_conversation["run_updated_at"] = run.get("updated_at") or live_conversation.get("run_updated_at") or 0
    if int(live_conversation.get("active_node_count") or 0) <= 0:
        live_conversation["active_node_count"] = 1

    live_payload = dict(payload)
    live_payload["conversation"] = live_conversation
    live_payload["projection"] = live_conversation
    live_event = dict(event)
    live_event["payload"] = live_payload
    return live_event


def _deliver_team_mission_events(mission_id: str, events: list[dict[str, Any]]) -> None:
    mission_id = str(mission_id or "").strip()
    if not mission_id or not events:
        return
    with _lock:
        subscriptions = [
            _subscriptions_by_id.get(subscription_id)
            for subscription_id in list(_subscription_ids_by_mission.get(mission_id, set()))
        ]
    subscriptions = [item for item in subscriptions if isinstance(item, dict)]
    if not subscriptions:
        return
    for subscription in subscriptions:
        transport = subscription.get("transport")
        if transport is None:
            continue
        delivered_seq = int(subscription.get("last_seq") or 0)
        for event in events:
            if not isinstance(event, dict):
                continue
            seq = int(event.get("seq") or 0)
            if seq <= delivered_seq:
                continue
            if seq > delivered_seq + 1:
                # OUT-OF-ORDER live notify. notify_team_mission_event_listeners fires
                # OUTSIDE the seq-assignment lock (hermes_team_mission.state.event_log.append_
                # team_mission_event), so concurrent member-node appends deliver here
                # scrambled. Do NOT deliver this event or advance the cursor past the
                # gap: the in-order subscription poller backfills the skipped seqs from
                # the canonical log (it reads after last_seq). Advancing here would skip
                # the gap forever — the poller would never re-read it — which left
                # tool.complete events undelivered and node tools spinning even though
                # the backend had finished. The gap fills within one poll interval.
                continue
            event_for_transport = _delta_event_for_subscription(subscription, event)
            if event_for_transport is None:
                _reserve_subscription_delivery(subscription, event)
                delivered_seq = seq
                continue
            event_for_transport = _team_mission_live_status_event_for_subscription(subscription, event_for_transport)
            if not _reserve_subscription_delivery(subscription, event_for_transport):
                delivered_seq = max(delivered_seq, seq)
                continue
            if not _write_event(transport, event_for_transport):
                break
            remember_transport_delivery(transport, event_for_transport)
            delivered_seq = seq
        with _lock:
            current = _subscriptions_by_id.get(str(subscription.get("id") or ""))
            if current is not None:
                current["last_seq"] = max(int(current.get("last_seq") or 0), delivered_seq)


def _on_team_mission_event_appended(mission_id: str, event: dict[str, Any]) -> None:
    _deliver_team_mission_events(mission_id, [event])


try:
    from hermes_team_mission.state.event_log import register_team_mission_event_listener

    register_team_mission_event_listener(_on_team_mission_event_appended)
except Exception:  # pragma: no cover - keeps run control importable in mocked tests.
    pass


def _terminal_status(event_type: str, payload: dict[str, Any]) -> str | None:
    if event_type == "error":
        return "failed"
    if event_type != "message.complete":
        return None
    status = str(payload.get("status") or "").strip().lower()
    if status == "interrupted":
        return "interrupted"
    if status in {"cancelled", "canceled"}:
        return "cancelled"
    if status in {"error", "failed"}:
        return "failed"
    return "completed"


def _normalize_team_mission_deliverable_terminal_event(
    frame: dict[str, Any],
    *,
    run_id: str,
    db: Any = None,
) -> dict[str, Any]:
    if str(frame.get("type") or "").strip() != "message.complete":
        return frame
    payload = frame.get("payload") if isinstance(frame.get("payload"), dict) else {}
    status = str(payload.get("status") or "").strip().lower()
    if status not in {"failed", "error"}:
        return frame
    deliverable_text = primary_deliverable_text(payload)
    if not deliverable_text or not run_id:
        return frame
    binding_getter = _db_method(db, "get_team_mission_run_binding")
    if binding_getter is None:
        return frame
    try:
        binding = binding_getter(run_id)
    except Exception:
        binding = None
    if not isinstance(binding, dict) or not binding:
        return frame
    normalized_payload = dict(payload)
    original_status = str(normalized_payload.get("status") or "").strip()
    original_error = str(normalized_payload.get("message") or normalized_payload.get("error") or "").strip()
    normalized_payload["text"] = deliverable_text
    normalized_payload["status"] = "complete"
    normalized_payload["team_mission_terminal_status_recovered"] = original_status
    if original_error:
        normalized_payload["nonfatal_error"] = original_error
    return {**frame, "payload": normalized_payload}


def _event_opens_active_run(event_type: str) -> bool:
    return str(event_type or "").strip() in _RUN_OPENING_EVENT_TYPES


def _active_run_ids_for_session(stable: str, db: Any = None) -> set[str]:
    active_ids: set[str] = set()
    if method := _db_method(db, "list_runs"):
        try:
            for run in method(
                stable,
                statuses=sorted(ACTIVE_RUN_STATUSES),
                limit=100,
            ):
                run_id = str((run or {}).get("run_id") or "").strip()
                if run_id:
                    active_ids.add(run_id)
        except Exception:
            logger.debug("failed to load active run ids from db", exc_info=True)
    with _lock:
        for run_id in _run_ids_by_session.get(stable, ()):
            state = _run_state_by_id.get(run_id) or {}
            if str(state.get("status") or "") in ACTIVE_RUN_STATUSES:
                active_ids.add(run_id)
    return active_ids


def _event_frame(event: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "method": "event", "params": event}


def _write_event(transport: Transport, event: dict[str, Any]) -> bool:
    try:
        wrote = transport.write(_event_frame(event))
    except Exception:
        detach_transport(transport)
        return False
    # ``WSTransport.write`` returns False when the underlying ws is
    # already closed — without honoring that, ``publish_recorded_event``
    # would silently report ``delivered=1`` to a dead transport and
    # the frontend would never see the live stream.
    if wrote is False:
        detach_transport(transport)
        return False
    return True


def _max_event_seq(events: list[dict[str, Any]], fallback: int = 0) -> int:
    last = int(fallback or 0)
    for event in events:
        try:
            last = max(last, int((event or {}).get("seq") or 0))
        except (TypeError, ValueError):
            continue
    return last


def _filter_events_for_subscription(
    events: list[dict[str, Any]],
    *,
    active_only: bool,
    active_run_ids: set[str],
    runtime_scope_key: str = "",
    run_id: str = "",
) -> list[dict[str, Any]]:
    scope = str(runtime_scope_key or "").strip()
    if scope:
        events = [
            event
            for event in events
            if (
                _event_runtime_scope_key(event)
                or str(event.get("stored_session_id") or "")
            ).strip()
            == scope
        ]
    normalized_run_id = str(run_id or "").strip()
    if normalized_run_id:
        events = [
            event
            for event in events
            if _event_run_id(event) == normalized_run_id
        ]
    if not active_only:
        return events
    return [
        event
        for event in events
        if _event_run_id(event) in active_run_ids
    ]


def _event_matches_subscription(
    subscription: dict[str, Any],
    event: dict[str, Any],
) -> bool:
    scope = str(subscription.get("runtime_scope_key") or "").strip()
    if scope:
        event_scope = (
            _event_runtime_scope_key(event)
            or str(event.get("stored_session_id") or "")
        ).strip()
        if event_scope != scope:
            return False
    return True


def _remember_subscription_run(subscription: dict[str, Any], event: dict[str, Any]) -> None:
    if not bool(subscription.get("active_only")):
        return
    run_id = _event_run_id(event)
    if not run_id:
        return
    active_run_ids = subscription.get("active_run_ids")
    if not isinstance(active_run_ids, set):
        active_run_ids = set(active_run_ids or ())
        subscription["active_run_ids"] = active_run_ids
    active_run_ids.add(run_id)


def _start_subscription_poller_locked() -> None:
    global _subscription_poller_thread
    if _subscription_poller_thread and _subscription_poller_thread.is_alive():
        return
    _subscription_poller_thread = threading.Thread(
        target=_poll_subscription_events,
        name="hermes-run-event-log-poller",
        daemon=True,
    )
    _subscription_poller_thread.start()


def _poll_subscription_events() -> None:
    while True:
        time.sleep(_POLL_INTERVAL_SECONDS)
        with _lock:
            subscriptions = [
                dict(subscription)
                for subscription in _subscriptions_by_id.values()
                if subscription.get("transport") is not None
            ]
        if not subscriptions:
            continue
        for subscription in subscriptions:
            db = subscription.get("db")
            subscription_kind = str(subscription.get("kind") or "session")
            if subscription_kind == "team_mission":
                method = _db_method(db, "list_team_mission_events") or _db_method(db, "list_team_mission_run_events")
            else:
                method = _db_method(db, "list_run_events")
            if method is None:
                continue
            stable = str(subscription.get("stored_session_id") or "").strip()
            mission_id = str(subscription.get("mission_id") or "").strip()
            transport = subscription.get("transport")
            if transport is None:
                continue
            if subscription_kind == "team_mission" and not mission_id:
                continue
            if subscription_kind != "team_mission" and not stable:
                continue
            last_seq = int(subscription.get("last_seq") or 0)
            active_only = bool(subscription.get("active_only"))
            runtime_scope_key = str(subscription.get("runtime_scope_key") or "").strip()
            active_run_ids = set(subscription.get("active_run_ids") or set())
            if active_only:
                active_run_ids.update(_active_run_ids_for_session(stable, db=db))
            try:
                if subscription_kind == "team_mission":
                    events = method(
                        mission_id,
                        after_seq=last_seq,
                        limit=_MAX_EVENTS_PER_SESSION,
                    )
                else:
                    events = method(
                        stable,
                        after_seq=last_seq,
                        active_only=False,
                        runtime_scope_key=runtime_scope_key,
                        limit=_MAX_EVENTS_PER_SESSION,
                    )
            except Exception:
                logger.debug("failed to poll run event log", exc_info=True)
                continue
            events = [event for event in events if isinstance(event, dict)]
            if subscription_kind != "team_mission":
                events = [
                    event
                    for event in events
                    if not _is_team_mission_runtime_event(event)
                ]
                events = _filter_events_for_subscription(
                    events,
                    active_only=active_only,
                    active_run_ids=active_run_ids,
                    runtime_scope_key=runtime_scope_key,
                    run_id=str(subscription.get("run_id") or ""),
                )
            if not events:
                continue
            delivered_seq = last_seq
            for event in events:
                seq = int(event.get("seq") or 0)
                if seq <= delivered_seq:
                    continue
                event_for_transport = _delta_event_for_subscription(subscription, event)
                if event_for_transport is None:
                    _reserve_subscription_delivery(subscription, event)
                    event_type = str(event.get("type") or "")
                    if event_type in _STREAM_TRACE_EVENT_TYPES:
                        _trace_stream_route(
                            "subscription-poll-skip-direct-stream",
                            event_type=event_type,
                            subscription_id=str(subscription.get("id") or ""),
                            subscription_kind=subscription_kind,
                            stored_session_id=stable,
                            mission_id=mission_id,
                            run_id=_event_run_id(event),
                            turn_id=_event_turn_id(event),
                            runtime_scope_key=_event_runtime_scope_key(event),
                            seq=seq,
                            previous_delivered_seq=delivered_seq,
                            transport=_transport_debug_id(transport),
                            **_stream_trace_summary(event),
                        )
                    delivered_seq = seq
                    continue
                if not _reserve_subscription_delivery(subscription, event_for_transport):
                    delivered_seq = max(delivered_seq, seq)
                    continue
                event_type = str(event_for_transport.get("type") or "")
                if event_type in _STREAM_TRACE_EVENT_TYPES:
                    _trace_stream_route(
                        "subscription-poll-delivery",
                        event_type=event_type,
                        subscription_id=str(subscription.get("id") or ""),
                        subscription_kind=subscription_kind,
                        stored_session_id=stable,
                        mission_id=mission_id,
                        run_id=_event_run_id(event_for_transport),
                        turn_id=_event_turn_id(event_for_transport),
                        runtime_scope_key=_event_runtime_scope_key(event_for_transport),
                        seq=seq,
                        previous_delivered_seq=delivered_seq,
                        transport=_transport_debug_id(transport),
                        **_stream_trace_summary(event_for_transport),
                    )
                if not _write_event(transport, event_for_transport):
                    break
                delivered_seq = seq
            with _lock:
                current = _subscriptions_by_id.get(str(subscription.get("id") or ""))
                if current is not None:
                    current["last_seq"] = max(int(current.get("last_seq") or 0), delivered_seq)
                    if active_only:
                        current_active_run_ids = set(current.get("active_run_ids") or set())
                        current_active_run_ids.update(active_run_ids)
                        current["active_run_ids"] = current_active_run_ids


def _ensure_run(
    *,
    stable_session_id: str,
    run_id: str,
    runtime_scope_key: str = "",
    turn_id: str = "",
    runtime_session_id: str = "",
) -> dict[str, Any]:
    now = time.time()
    state = _run_state_by_id.get(run_id)
    if state is None:
        state = {
            "run_id": run_id,
            "turn_id": turn_id,
            "session_id": runtime_session_id,
            "stored_session_id": stable_session_id,
            "runtime_scope_key": runtime_scope_key or stable_session_id,
            "status": "running",
            "started_at": now,
            "updated_at": now,
            "last_seq": 0,
            "error": "",
        }
        _run_state_by_id[run_id] = state
        if run_id not in _run_ids_by_session[stable_session_id]:
            _run_ids_by_session[stable_session_id].append(run_id)
    else:
        state["updated_at"] = now
        if turn_id:
            state["turn_id"] = turn_id
        if runtime_session_id:
            state["session_id"] = runtime_session_id
        if runtime_scope_key:
            state["runtime_scope_key"] = runtime_scope_key
        if stable_session_id:
            state["stored_session_id"] = stable_session_id
    return state


def mark_run_started(
    *,
    stored_session_id: str,
    runtime_session_id: str,
    run_id: str,
    turn_id: str = "",
    runtime_scope_key: str = "",
    metadata: dict[str, Any] | None = None,
    db: Any = None,
) -> dict[str, Any]:
    stable = str(stored_session_id or runtime_session_id or "").strip()
    normalized_run_id = str(run_id or "").strip()
    if not stable or not normalized_run_id:
        return {}
    with _lock:
        state = _ensure_run(
            stable_session_id=stable,
            run_id=normalized_run_id,
            runtime_scope_key=runtime_scope_key or stable,
            turn_id=turn_id,
            runtime_session_id=str(runtime_session_id or "").strip(),
        )
        state["status"] = "running"
        state["error"] = ""
        snapshot = dict(state)
    if method := _db_method(db, "upsert_run"):
        try:
            persisted = method(
                run_id=normalized_run_id,
                session_id=stable,
                runtime_scope_key=runtime_scope_key or stable,
                turn_id=turn_id,
                runtime_session_id=str(runtime_session_id or "").strip(),
                status="running",
                started_at=float(snapshot.get("started_at") or time.time()),
                updated_at=float(snapshot.get("updated_at") or time.time()),
                last_seq=int(snapshot.get("last_seq") or 0),
                error="",
                metadata=metadata,
            )
            if isinstance(persisted, dict) and persisted:
                return persisted
        except Exception:
            pass
    return snapshot


def create_run_if_session_idle(
    *,
    stored_session_id: str,
    run_id: str,
    turn_id: str = "",
    runtime_scope_key: str = "",
    runtime_session_id: str = "",
    metadata: dict[str, Any] | None = None,
    db: Any = None,
) -> dict[str, Any]:
    stable = str(stored_session_id or runtime_session_id or "").strip()
    normalized_run_id = str(run_id or "").strip()
    if not stable or not normalized_run_id:
        return {"run": None, "conflict": None}

    if method := _db_method(db, "create_run_if_session_idle"):
        _recover_orphaned_active_runs(
            db,
            current_gateway_instance_id=_gateway_instance_id_from_metadata(metadata),
        )
        try:
            result = method(
                run_id=normalized_run_id,
                session_id=stable,
                runtime_scope_key=runtime_scope_key or stable,
                turn_id=turn_id,
                runtime_session_id=runtime_session_id,
                status="queued",
                metadata=metadata,
            )
            run = result.get("run") if isinstance(result, dict) else None
            conflict = result.get("conflict") if isinstance(result, dict) else None
            created = bool(result.get("created")) if isinstance(result, dict) else False
            if isinstance(run, dict) and run:
                with _lock:
                    state = _ensure_run(
                        stable_session_id=stable,
                        run_id=normalized_run_id,
                        runtime_scope_key=runtime_scope_key or stable,
                        turn_id=turn_id,
                        runtime_session_id=runtime_session_id,
                    )
                    state.update(run)
                return {"run": run, "conflict": None, "created": created}
            if isinstance(conflict, dict) and conflict:
                _diagnostic_warning(
                    "run-reservation-conflict",
                    source="db",
                    db=_db_label(db),
                    requested={
                        "run_id": normalized_run_id,
                        "session_id": stable,
                        "turn_id": turn_id,
                        "runtime_scope_key": runtime_scope_key or stable,
                        "runtime_session_id": runtime_session_id,
                        "gateway_instance_id": _gateway_instance_id_from_metadata(metadata),
                        "gateway_pid": os.getpid(),
                    },
                    conflict=_run_summary(conflict),
                )
                return {"run": None, "conflict": conflict, "created": False}
        except Exception:
            pass

    persisted_status = session_status(
        stable,
        db=db,
        current_gateway_instance_id=_gateway_instance_id_from_metadata(metadata),
    )
    if persisted_status.get("running"):
        active_run_id = str(persisted_status.get("active_run_id") or "").strip()
        if active_run_id and active_run_id != normalized_run_id:
            return {
                "run": None,
                "conflict": {
                    "run_id": active_run_id,
                    "turn_id": str(persisted_status.get("active_turn_id") or ""),
                    "session_id": stable,
                    "stored_session_id": stable,
                    "runtime_scope_key": str(persisted_status.get("runtime_scope_key") or ""),
                    "status": "running",
                    "started_at": float(persisted_status.get("run_started_at") or 0),
                    "updated_at": float(persisted_status.get("run_updated_at") or 0),
                    "last_seq": int(persisted_status.get("last_event_seq") or 0),
                },
                "created": False,
            }

    with _lock:
        for active_run_id in _run_ids_by_session.get(stable, ()):
            active = _run_state_by_id.get(active_run_id) or {}
            if (
                active_run_id != normalized_run_id
                and str(active.get("status") or "") in ACTIVE_RUN_STATUSES
            ):
                _diagnostic_warning(
                    "run-reservation-conflict",
                    source="memory",
                    db=_db_label(db),
                    requested={
                        "run_id": normalized_run_id,
                        "session_id": stable,
                        "turn_id": turn_id,
                        "runtime_scope_key": runtime_scope_key or stable,
                        "runtime_session_id": runtime_session_id,
                        "gateway_instance_id": _gateway_instance_id_from_metadata(metadata),
                        "gateway_pid": os.getpid(),
                    },
                    conflict=_run_summary(dict(active)),
                )
                return {"run": None, "conflict": dict(active), "created": False}
        state = _ensure_run(
            stable_session_id=stable,
            run_id=normalized_run_id,
            runtime_scope_key=runtime_scope_key or stable,
            turn_id=turn_id,
            runtime_session_id=runtime_session_id,
        )
        state["status"] = "queued"
        if isinstance(metadata, dict) and metadata:
            current_metadata = state.get("metadata")
            if not isinstance(current_metadata, dict):
                current_metadata = {}
            current_metadata.update(metadata)
            state["metadata"] = current_metadata
        return {"run": dict(state), "conflict": None, "created": True}


def next_event_seq(stored_session_id: str, fallback_seq: int = 0, db: Any = None) -> int:
    stable = str(stored_session_id or "").strip()
    if not stable:
        return int(fallback_seq or 0)
    persisted_next = 0
    if method := _db_method(db, "next_run_event_seq"):
        try:
            persisted_next = int(method(stable, fallback_seq=fallback_seq) or 0)
        except Exception:
            persisted_next = 0
    with _lock:
        next_seq = max(
            int(_last_seq_by_session.get(stable) or 0) + 1,
            int(fallback_seq or 0),
            persisted_next,
        )
        _last_seq_by_session[stable] = next_seq
        return next_seq


def _apply_run_context_to_frame(
    frame: dict[str, Any],
    run_context: "RunContext | None",
) -> dict[str, Any]:
    if run_context is None:
        return frame
    payload = frame.get("payload")
    if not isinstance(payload, dict):
        payload = {}
        frame["payload"] = payload
    frame["stored_session_id"] = run_context.conversation_session_id
    if not str(frame.get("participant_id") or "").strip():
        frame["participant_id"] = run_context.participant_id
    payload["run_context"] = run_context.to_payload()
    return frame


def record_event(
    params: dict[str, Any],
    owner_transport: Transport | None = None,
    skip_owner_transport: bool = False,
    db: Any = None,
    persist: bool = True,
    run_context: "RunContext | None" = None,
) -> list[Transport]:
    """Persist an event frame and return live subscriber transports to notify."""
    frame = _apply_run_context_to_frame(dict(params), run_context)
    payload = frame.get("payload") if isinstance(frame.get("payload"), dict) else {}
    stable = _stable_session_id(frame)
    run_id = _event_run_id(frame)
    turn_id = _event_turn_id(frame)
    frame = _normalize_team_mission_deliverable_terminal_event(frame, run_id=run_id, db=db)
    payload = frame.get("payload") if isinstance(frame.get("payload"), dict) else {}
    event_type = str(frame.get("type") or "").strip()
    runtime_session_id = str(frame.get("session_id") or "").strip()
    owner_metadata = frame.get("owner_metadata")
    owner_metadata = owner_metadata if isinstance(owner_metadata, dict) else {}
    now = time.time()
    frame["timestamp"] = now
    # Conversation-architecture refactor (P1-PR-B): event speaker identity is
    # authoritative when present, otherwise resolved from conversation_participants.
    # Lookup miss/error intentionally leaves participant_id blank so PR-C can
    # keep using legacy speaker fallback without silently misattributing to leader.
    participant_id = str(frame.get("participant_id") or "").strip()
    if not participant_id and isinstance(payload, dict):
        participant_id = str(payload.get("participant_id") or "").strip()
        if participant_id:
            frame["participant_id"] = participant_id
    if stable and not participant_id:
        team_identity = payload.get("team_mission") if isinstance(payload.get("team_mission"), dict) else {}
        scope_hint = (
            str(payload.get("runtime_scope_key") or "").strip()
            or str(frame.get("runtime_scope_key") or "").strip()
        )
        member_hint = (
            str(payload.get("member_id") or "").strip()
            or str(frame.get("member_id") or "").strip()
            or str(team_identity.get("member_id") or "").strip()
        )
        profile_hint = (
            str(payload.get("agent_profile_id") or "").strip()
            or str(frame.get("agent_profile_id") or "").strip()
            or str(team_identity.get("agent_profile_id") or "").strip()
        )
        if scope_hint or member_hint or profile_hint:
            resolved_participant = ""
            resolver = _db_method(db, "resolve_participant_id")
            resolver_failed = False
            if resolver:
                try:
                    resolved_participant = str(
                        resolver(
                            conversation_session_id=stable,
                            runtime_scope_key=scope_hint,
                            member_id=member_hint,
                            agent_profile_id=profile_hint,
                        )
                        or ""
                    ).strip()
                except Exception as exc:
                    resolver_failed = True
                    _diagnostic_warning(
                        "participant-resolve-error",
                        db=_db_label(db),
                        event_type=event_type,
                        session_id=stable,
                        run_id=run_id,
                        turn_id=turn_id,
                        runtime_scope_key=scope_hint,
                        member_id=member_hint,
                        agent_profile_id=profile_hint,
                        error=f"{type(exc).__name__}: {exc}",
                    )
            if resolved_participant:
                frame["participant_id"] = resolved_participant
                if isinstance(frame.get("payload"), dict):
                    frame["payload"]["participant_id"] = resolved_participant
                    payload = frame["payload"]
            elif resolver and not resolver_failed:
                _diagnostic_warning(
                    "participant-resolve-miss",
                    db=_db_label(db),
                    event_type=event_type,
                    session_id=stable,
                    run_id=run_id,
                    turn_id=turn_id,
                    runtime_scope_key=scope_hint,
                    member_id=member_hint,
                    agent_profile_id=profile_hint,
                )
    terminal_event = _terminal_status(event_type, payload)
    scheduler_mission_id = ""
    mission_events_for_fanout: list[dict[str, Any]] = []
    persisted_run_checked = False
    persisted_terminal_reopen = False
    if stable and run_id and terminal_event is None and _event_opens_active_run(event_type):
        if getter := _db_method(db, "get_run"):
            persisted_run_checked = True
            try:
                persisted_run = getter(run_id)
            except Exception:
                persisted_run = None
            if isinstance(persisted_run, dict):
                persisted_stable = str(
                    persisted_run.get("session_id")
                    or persisted_run.get("stored_session_id")
                    or ""
                ).strip()
                persisted_terminal_reopen = bool(
                    str(persisted_run.get("status") or "") in TERMINAL_RUN_STATUSES
                    and (not persisted_stable or persisted_stable == stable)
                )

    with _lock:
        if stable:
            _events_by_session[stable].append(frame)
            _last_seq_by_session[stable] = max(
                int(_last_seq_by_session.get(stable) or 0),
                int(frame.get("seq") or 0),
            )
        if stable and run_id:
            existing_state = _run_state_by_id.get(run_id)
            existing_state_stable = str((existing_state or {}).get("stored_session_id") or "").strip()
            memory_terminal_reopen = bool(
                existing_state is not None
                and (not existing_state_stable or existing_state_stable == stable)
                and str(existing_state.get("status") or "") in TERMINAL_RUN_STATUSES
                and terminal_event is None
                and _event_opens_active_run(event_type)
            )
            reopens_terminal_run = persisted_terminal_reopen or (
                memory_terminal_reopen and not persisted_run_checked
            )
            if reopens_terminal_run:
                if stable:
                    try:
                        _events_by_session[stable].remove(frame)
                    except (KeyError, ValueError):
                        pass
                return []
            should_track_run = bool(
                existing_state is not None
                or terminal_event
                or _event_opens_active_run(event_type)
            )
            if should_track_run:
                state = _ensure_run(
                    stable_session_id=stable,
                    run_id=run_id,
                    runtime_scope_key=str(
                        (payload or {}).get("runtime_scope_key")
                        or frame.get("runtime_scope_key")
                        or ""
                    ),
                    turn_id=turn_id,
                    runtime_session_id=runtime_session_id,
                )
                state["last_seq"] = int(frame.get("seq") or state.get("last_seq") or 0)
                if owner_metadata:
                    metadata = state.get("metadata")
                    if not isinstance(metadata, dict):
                        metadata = {}
                    metadata.update(owner_metadata)
                    state["metadata"] = metadata
                if terminal_event:
                    state["status"] = terminal_event
                    if terminal_event == "failed":
                        state["error"] = str(payload.get("message") or "")
                elif _event_opens_active_run(event_type):
                    state["status"] = "running"
                state["updated_at"] = now

        subscribers = set()
        if stable:
            for subscription_id in list(_subscription_ids_by_session.get(stable, set())):
                subscription = _subscriptions_by_id.get(subscription_id)
                transport = subscription.get("transport") if isinstance(subscription, dict) else None
                if (
                    transport is not None
                    and isinstance(subscription, dict)
                    and _session_subscription_matches_event(subscription, frame)
                ):
                    _remember_subscription_run(subscription, frame)
                    subscribers.add(transport)
            subscribers.update(_subscribers_by_session.get(stable, set()))
        if skip_owner_transport and owner_transport is not None:
            subscribers.discard(owner_transport)
        result = list(subscribers)
        if event_type in _STREAM_TRACE_EVENT_TYPES:
            _trace_stream_route(
                "run-control-record",
                event_type=event_type,
                session_id=runtime_session_id,
                stored_session_id=stable,
                run_id=run_id,
                turn_id=turn_id,
                runtime_scope_key=str(frame.get("runtime_scope_key") or ""),
                seq=int(frame.get("seq") or 0),
                owner_transport=_transport_debug_id(owner_transport),
                subscriber_delivery_count=len(result),
                **_stream_trace_summary(frame),
            )
    if persist and stable and (method := _db_method(db, "append_run_event")):
        prev_projecting = getattr(db, "_team_mission_projecting", False)
        try:
            # record_event performs its own canonical projection (canonicalize +
            # reduce + mirror) below, so suppress append_run_event's write-time
            # projection hook for this call and for the mirror's nested
            # append_run_event — otherwise the event would be projected twice.
            setattr(db, "_team_mission_projecting", True)
            saved = method(stable, frame)
            if (
                isinstance(saved, dict)
                and saved.get("_persistence_disposition") in {"duplicate_terminal", "ignored_after_terminal"}
            ):
                # Phase 23: duplicate persistence MUST NOT drop the live
                # subscriber delivery. The legacy ``return []`` here was
                # written for the era when the sub-sidecar's direct-relay
                # ws bridge already delivered the event to the frontend
                # AND record_event was invoked a second time at append
                # time — so dropping subscribers prevented a double
                # render. After Phase 6 there is no bridge fallback path
                # and after Phase 15 the main side IS the canonical
                # subscriber broadcaster; returning [] here means the
                # SOLE delivery for that event is lost whenever the
                # worker happens to have persisted the row first (a
                # race the agent thread enters whenever it calls
                # ``db.append_run_event`` directly from inside the
                # worker process now that Phase 8b unified the DB).
                # Skip the post-persist projection (the duplicate row
                # is already in the table) but keep the subscriber
                # broadcast list intact — the caller
                # (``publish_recorded_event``) will hand the event to
                # every transport in ``result``.
                with _lock:
                    try:
                        _events_by_session[stable].remove(frame)
                    except (KeyError, ValueError):
                        pass
                logger.debug(
                    "[doxie-run-control] record_event dup-but-delivering "
                    "stable=%s run_id=%s seq=%s subscribers=%d",
                    stable, run_id, frame.get("seq"), len(result),
                )
                return result
            if terminal_event:
                _diagnostic_warning(
                    "terminal-event-persisted",
                    db=_db_label(db),
                    event_type=event_type,
                    terminal_status=terminal_event,
                    session_id=stable,
                    run_id=run_id,
                    turn_id=turn_id,
                    runtime_scope_key=str(frame.get("runtime_scope_key") or ""),
                    runtime_session_id=runtime_session_id,
                    seq=int(frame.get("seq") or 0),
                )
            reducer = _db_method(db, "reduce_team_mission_run_event")
            if reducer is not None and run_id:
                event_for_reduce = saved if isinstance(saved, dict) else frame
                event_for_stream = frame if event_type == "message.delta" else event_for_reduce
                mission_event: dict[str, Any] = {}
                mission_event_appender = _db_method(db, "append_team_mission_event_for_run")
                if mission_event_appender is not None:
                    try:
                        candidate_event = mission_event_appender(
                            run_id=run_id,
                            event=event_for_stream,
                        )
                    except Exception:
                        candidate_event = {}
                    if (
                        isinstance(candidate_event, dict)
                        and candidate_event
                        and not candidate_event.get("_persistence_disposition")
                    ):
                        mission_event = candidate_event
                        mission_events_for_fanout.append(candidate_event)
                reduced_node = reducer(run_id=run_id, event=event_for_reduce)
                scheduler_mission_id = ""
                binding: dict[str, Any] = {}
                if isinstance(reduced_node, dict):
                    scheduler_mission_id = str(reduced_node.get("mission_id") or "").strip()
                if not scheduler_mission_id:
                    binding_getter = _db_method(db, "get_team_mission_run_binding")
                    if binding_getter is not None:
                        candidate_binding = binding_getter(run_id)
                        binding = dict(candidate_binding) if isinstance(candidate_binding, dict) else {}
                        scheduler_mission_id = str(binding.get("mission_id") or "").strip()
                if scheduler_mission_id:
                    try:
                        from hermes_team_mission.runtime.conversation_mirror import mirror_event_to_conversation

                        mirrored = mirror_event_to_conversation(
                            db,
                            mission_id=scheduler_mission_id,
                            binding=binding or None,
                            event=event_for_stream,
                            source="runtime_event",
                        )
                        if mirrored:
                            mirror_stable = _stable_session_id(mirrored)
                            mirror_subscribers = set()
                            with _lock:
                                if mirror_stable:
                                    _events_by_session[mirror_stable].append(mirrored)
                                    _last_seq_by_session[mirror_stable] = max(
                                        int(_last_seq_by_session.get(mirror_stable) or 0),
                                        int(mirrored.get("seq") or 0),
                                    )
                                    for subscription_id in list(_subscription_ids_by_session.get(mirror_stable, set())):
                                        subscription = _subscriptions_by_id.get(subscription_id)
                                        transport = subscription.get("transport") if isinstance(subscription, dict) else None
                                        if (
                                            transport is not None
                                            and isinstance(subscription, dict)
                                            and _session_subscription_matches_event(subscription, mirrored)
                                        ):
                                            _remember_subscription_run(subscription, mirrored)
                                            mirror_subscribers.add(transport)
                                    mirror_subscribers.update(_subscribers_by_session.get(mirror_stable, set()))
                            for transport in mirror_subscribers:
                                if _write_event(transport, mirrored):
                                    remember_transport_delivery(transport, mirrored)
                    except Exception:
                        logger.debug("failed to mirror Team Mission event", exc_info=True)
                    if (
                        event_type in _TEAM_MISSION_STATUS_SOURCE_EVENT_TYPES
                        and mission_event
                        and (status_appender := _db_method(db, "append_team_mission_conversation_status_event"))
                    ):
                        try:
                            status_event = status_appender(
                                mission_id=scheduler_mission_id,
                                source_event=event_for_reduce,
                                source_mission_seq=int(mission_event.get("seq") or 0),
                            )
                        except Exception:
                            status_event = {}
                        if (
                            isinstance(status_event, dict)
                            and status_event
                            and not status_event.get("_persistence_disposition")
                        ):
                            mission_events_for_fanout.append(status_event)
        except Exception as exc:
            _diagnostic_warning(
                "run-event-persist-failed",
                db=_db_label(db),
                event_type=event_type,
                terminal_status=terminal_event,
                session_id=stable,
                run_id=run_id,
                turn_id=turn_id,
                runtime_scope_key=str(frame.get("runtime_scope_key") or ""),
                runtime_session_id=runtime_session_id,
                seq=int(frame.get("seq") or 0),
                error=str(exc),
            )
            logger.warning("failed to persist run event", exc_info=True)
        finally:
            try:
                setattr(db, "_team_mission_projecting", prev_projecting)
            except Exception:
                pass
    elif terminal_event:
        _diagnostic_warning(
            "terminal-event-not-persisted-no-db-method",
            db=_db_label(db),
            event_type=event_type,
            terminal_status=terminal_event,
            session_id=stable,
            run_id=run_id,
            turn_id=turn_id,
            runtime_scope_key=str(frame.get("runtime_scope_key") or ""),
            runtime_session_id=runtime_session_id,
            seq=int(frame.get("seq") or 0),
        )
    if mission_events_for_fanout:
        fanout_mission_id = scheduler_mission_id or _team_mission_event_mission_id(mission_events_for_fanout[0])
        _deliver_team_mission_events(fanout_mission_id, mission_events_for_fanout)
    if terminal_event and scheduler_mission_id:
        _dispatch_team_mission_ready_scheduler(
            mission_id=scheduler_mission_id,
            db=db,
            trigger_event=event_type,
            run_id=run_id,
        )
    return result


def publish_recorded_event(
    params: dict[str, Any],
    owner_transport: Transport | None = None,
    skip_owner_transport: bool = False,
    db: Any = None,
    before_deliver: Callable[[], None] | None = None,
    persist: bool = True,
    run_context: "RunContext | None" = None,
) -> list[Transport]:
    """Persist an event and deliver it to live event subscribers.

    ``record_event`` is intentionally side-effect-light: callers that only need
    durable replay can persist and decide how to publish. Runtime stdout events,
    terminal events, and interrupt events need the stronger contract that every
    matching ``events.subscribe`` transport receives the same persisted frame
    immediately.

    ``skip_owner_transport`` is reserved for callers that also have a direct
    delivery path for the exact same event frame. Passing ``owner_transport``
    alone is diagnostic context; it must not suppress an explicit subscription.
    """
    publish_params = _apply_run_context_to_frame(dict(params), run_context)
    subscribers = record_event(
        publish_params,
        owner_transport=owner_transport,
        skip_owner_transport=skip_owner_transport,
        db=db,
        persist=persist,
        run_context=run_context,
    )
    if before_deliver is not None:
        before_deliver()
    delivered: list[Transport] = []
    for transport in subscribers:
        event_for_transport = _event_for_live_subscription_delivery(transport, publish_params)
        if event_for_transport is None:
            delivered.append(transport)
            continue
        if _write_event(transport, event_for_transport):
            remember_transport_delivery(transport, event_for_transport, direct=False)
            delivered.append(transport)
    return delivered


def publish_run_terminal_event(
    *,
    stored_session_id: str,
    run_id: str,
    turn_id: str = "",
    runtime_scope_key: str = "",
    runtime_session_id: str = "",
    status: str = "failed",
    message: str = "",
    db: Any = None,
    owner_transport: Transport | None = None,
) -> dict[str, Any]:
    stable = str(stored_session_id or runtime_session_id or "").strip()
    normalized_run_id = str(run_id or "").strip()
    if not stable or not normalized_run_id:
        return {}
    terminal_status = str(status or "failed").strip().lower() or "failed"
    payload_status = _payload_status(terminal_status)
    payload: dict[str, Any] = {
        "run_id": normalized_run_id,
        "turn_id": str(turn_id or "").strip(),
        "status": payload_status,
    }
    if message:
        payload["message"] = str(message)
        payload["text"] = str(message) if payload_status == "error" else ""
    else:
        payload["text"] = ""
    frame = {
        "type": "message.complete",
        "session_id": str(runtime_session_id or stable).strip(),
        "stored_session_id": stable,
        "run_id": normalized_run_id,
        "turn_id": str(turn_id or "").strip(),
        "runtime_scope_key": str(runtime_scope_key or stable).strip(),
        "seq": next_event_seq(stable, db=db),
        "owner_metadata": {
            "gateway_pid": os.getpid(),
        },
        "payload": payload,
    }
    _diagnostic_warning(
        "publish-terminal-event",
        db=_db_label(db),
        status=terminal_status,
        payload_status=payload_status,
        session_id=stable,
        run_id=normalized_run_id,
        turn_id=str(turn_id or "").strip(),
        runtime_scope_key=str(runtime_scope_key or stable).strip(),
        runtime_session_id=str(runtime_session_id or stable).strip(),
        seq=int(frame.get("seq") or 0),
        message=str(message or ""),
    )
    publish_recorded_event(frame, owner_transport=owner_transport, db=db)
    return frame


def subscribe_session(
    *,
    stored_session_id: str,
    transport: Transport | None,
    after_seq: int = 0,
    active_only: bool = False,
    runtime_scope_key: str = "",
    run_id: str = "",
    limit: int = _MAX_EVENTS_PER_SESSION,
    db: Any = None,
) -> list[dict[str, Any]]:
    _subscription_id, events = subscribe_session_with_id(
        stored_session_id=stored_session_id,
        transport=transport,
        after_seq=after_seq,
        active_only=active_only,
        runtime_scope_key=runtime_scope_key,
        run_id=run_id,
        limit=limit,
        db=db,
    )
    return events


def _remove_subscription_ids_locked(subscription_ids: set[str]) -> int:
    removed = 0
    for sub_id in set(subscription_ids or set()):
        subscription = _subscriptions_by_id.pop(sub_id, None)
        if not subscription:
            continue
        removed += 1
        sid = str(subscription.get("stored_session_id") or "")
        sub_transport = subscription.get("transport")
        if sid:
            _subscription_ids_by_session.get(sid, set()).discard(sub_id)
        mission_id = str(subscription.get("mission_id") or "")
        if mission_id:
            _subscription_ids_by_mission.get(mission_id, set()).discard(sub_id)
        if sub_transport is not None:
            _subscription_ids_by_transport.get(sub_transport, set()).discard(sub_id)
    return removed


def subscribe_session_with_id(
    *,
    stored_session_id: str,
    transport: Transport | None,
    after_seq: int = 0,
    active_only: bool = False,
    runtime_scope_key: str = "",
    run_id: str = "",
    limit: int = _MAX_EVENTS_PER_SESSION,
    db: Any = None,
    subscription_id: str = "",
) -> tuple[str, list[dict[str, Any]]]:
    stable = str(stored_session_id or "").strip()
    if not stable:
        return "", []
    scope = str(runtime_scope_key or "").strip()
    normalized_run_id = str(run_id or "").strip()
    with _lock:
        normalized_subscription_id = str(subscription_id or uuid.uuid4().hex).strip()
        active_run_ids = _active_run_ids_for_session(stable, db=db) if active_only else set()
        if transport is not None:
            duplicate_subscription_ids = {
                sub_id
                for sub_id in _subscription_ids_by_transport.get(transport, set())
                if (
                    sub_id == normalized_subscription_id
                    or (
                        str((_subscriptions_by_id.get(sub_id) or {}).get("kind") or "session") == "session"
                        and str((_subscriptions_by_id.get(sub_id) or {}).get("stored_session_id") or "").strip() == stable
                    )
                )
            }
            _remove_subscription_ids_locked(duplicate_subscription_ids)
            _subscriptions_by_id[normalized_subscription_id] = {
                "id": normalized_subscription_id,
                "kind": "session",
                "stored_session_id": stable,
                "transport": transport,
                "active_only": bool(active_only),
                "runtime_scope_key": scope,
                "run_id": normalized_run_id,
                "active_run_ids": set(active_run_ids),
                "last_seq": max(0, int(after_seq or 0)),
                "db": db,
                "created_at": time.time(),
            }
            _subscription_ids_by_session[stable].add(normalized_subscription_id)
            _subscription_ids_by_transport[transport].add(normalized_subscription_id)
            if _db_method(db, "list_run_events") is not None:
                _start_subscription_poller_locked()
        memory_events = list(_events_by_session.get(stable, ()))
    events: list[dict[str, Any]] = []
    if method := _db_method(db, "list_run_events"):
        try:
            events = method(
                stable,
                after_seq=after_seq,
                active_only=False,
                runtime_scope_key=scope,
                run_id=normalized_run_id,
                limit=limit,
            )
        except Exception:
            events = []
    events = _filter_events_for_subscription(
        [
            event
            for event in events
            if isinstance(event, dict) and not _is_team_mission_runtime_event(event)
        ],
        active_only=active_only,
        active_run_ids=active_run_ids,
        runtime_scope_key=scope,
        run_id=normalized_run_id,
    )
    memory_events = _filter_events_for_subscription(
        [
            event
            for event in memory_events
            if isinstance(event, dict) and not _is_team_mission_runtime_event(event)
        ],
        active_only=active_only,
        active_run_ids=active_run_ids,
        runtime_scope_key=scope,
        run_id=normalized_run_id,
    )
    if after_seq > 0:
        memory_events = [event for event in memory_events if int(event.get("seq") or 0) > after_seq]
    by_seq = {
        int(event.get("seq") or 0): event
        for event in events
        if isinstance(event, dict) and int(event.get("seq") or 0) > 0
    }
    for event in memory_events:
        seq = int(event.get("seq") or 0)
        if seq > 0:
            by_seq.setdefault(seq, event)
    events = [by_seq[seq] for seq in sorted(by_seq)]
    with _lock:
        subscription = _subscriptions_by_id.get(normalized_subscription_id)
        if subscription is not None:
            subscription["last_seq"] = _max_event_seq(events, after_seq)
    if after_seq <= 0:
        return normalized_subscription_id, events
    return normalized_subscription_id, [event for event in events if int(event.get("seq") or 0) > after_seq]


def subscribe_team_mission_with_id(
    *,
    mission_id: str,
    transport: Transport | None,
    after_seq: int = 0,
    limit: int = _MAX_EVENTS_PER_SESSION,
    db: Any = None,
    subscription_id: str = "",
) -> tuple[str, list[dict[str, Any]]]:
    normalized_mission_id = str(mission_id or "").strip()
    if not normalized_mission_id:
        return "", []
    with _lock:
        normalized_subscription_id = str(subscription_id or uuid.uuid4().hex).strip()
        if transport is not None:
            duplicate_subscription_ids = {
                sub_id
                for sub_id in _subscription_ids_by_transport.get(transport, set())
                if (
                    sub_id == normalized_subscription_id
                    or (
                        str((_subscriptions_by_id.get(sub_id) or {}).get("kind") or "") == "team_mission"
                        and str((_subscriptions_by_id.get(sub_id) or {}).get("mission_id") or "").strip()
                        == normalized_mission_id
                    )
                )
            }
            _remove_subscription_ids_locked(duplicate_subscription_ids)
            _subscriptions_by_id[normalized_subscription_id] = {
                "id": normalized_subscription_id,
                "kind": "team_mission",
                "mission_id": normalized_mission_id,
                "stored_session_id": "",
                "transport": transport,
                "active_only": False,
                "runtime_scope_key": "",
                "active_run_ids": set(),
                "last_seq": max(0, int(after_seq or 0)),
                "db": db,
                "created_at": time.time(),
            }
            _subscription_ids_by_mission[normalized_mission_id].add(normalized_subscription_id)
            _subscription_ids_by_transport[transport].add(normalized_subscription_id)
            if (
                _db_method(db, "list_team_mission_events") is not None
                or _db_method(db, "list_team_mission_run_events") is not None
            ):
                _start_subscription_poller_locked()
    events: list[dict[str, Any]] = []
    if method := (_db_method(db, "list_team_mission_events") or _db_method(db, "list_team_mission_run_events")):
        try:
            events = method(
                normalized_mission_id,
                after_seq=after_seq,
                limit=limit,
            )
        except Exception:
            events = []
    events = [event for event in events if isinstance(event, dict)]
    by_seq = {
        int(event.get("seq") or 0): event
        for event in events
        if isinstance(event, dict) and int(event.get("seq") or 0) > 0
    }
    events = [by_seq[seq] for seq in sorted(by_seq)]
    with _lock:
        subscription = _subscriptions_by_id.get(normalized_subscription_id)
        if subscription is not None:
            subscription["last_seq"] = _max_event_seq(events, after_seq)
    if after_seq <= 0:
        return normalized_subscription_id, events
    return normalized_subscription_id, [event for event in events if int(event.get("seq") or 0) > after_seq]


def unsubscribe_session(
    *,
    subscription_id: str = "",
    stored_session_id: str = "",
    transport: Transport | None = None,
) -> int:
    removed = 0
    normalized_subscription_id = str(subscription_id or "").strip()
    stable = str(stored_session_id or "").strip()
    with _lock:
        if normalized_subscription_id:
            ids = {normalized_subscription_id}
        elif transport is not None and stable:
            ids = {
                sub_id
                for sub_id in _subscription_ids_by_transport.get(transport, set())
                if (_subscriptions_by_id.get(sub_id) or {}).get("stored_session_id") == stable
            }
        elif transport is not None:
            ids = set(_subscription_ids_by_transport.get(transport, set()))
        else:
            ids = set()
        removed = _remove_subscription_ids_locked(ids)
        if transport is not None and stable:
            _subscribers_by_session.get(stable, set()).discard(transport)
    return removed


def detach_transport(transport: Transport | None) -> None:
    if transport is None:
        return
    with _lock:
        unsubscribe_session(transport=transport)
        for subscribers in _subscribers_by_session.values():
            subscribers.discard(transport)


def get_run(run_id: str, db: Any = None) -> dict[str, Any] | None:
    normalized = str(run_id or "").strip()
    if not normalized:
        return None
    persisted = None
    if method := _db_method(db, "get_run"):
        try:
            persisted = method(normalized)
        except Exception:
            persisted = None
    with _lock:
        state = _run_state_by_id.get(normalized)
        memory = dict(state) if state else None
    if not memory:
        return persisted if isinstance(persisted, dict) else None
    if not isinstance(persisted, dict):
        return memory
    if float(memory.get("updated_at") or 0) >= float(persisted.get("updated_at") or 0):
        return memory
    return persisted


def list_runs(
    stored_session_id: str = "",
    db: Any = None,
    runtime_scope_key: str = "",
    statuses: list[str] | None = None,
    limit: int = 200,
) -> list[dict[str, Any]]:
    stable = str(stored_session_id or "").strip()
    scope = str(runtime_scope_key or "").strip()
    normalized_statuses = [
        str(status or "").strip()
        for status in (statuses or [])
        if str(status or "").strip()
    ]
    if not stable and not scope and not normalized_statuses:
        return []
    persisted: list[dict[str, Any]] = []
    if method := _db_method(db, "list_runs"):
        try:
            persisted = method(
                stable,
                runtime_scope_key=scope,
                statuses=normalized_statuses,
                limit=limit,
            )
        except Exception:
            persisted = []
    with _lock:
        if stable:
            ids = list(_run_ids_by_session.get(stable, ()))
            memory = [dict(_run_state_by_id[run_id]) for run_id in ids if run_id in _run_state_by_id]
        else:
            memory = [dict(run) for run in _run_state_by_id.values()]
    if scope:
        memory = [run for run in memory if str(run.get("runtime_scope_key") or "") == scope]
    if normalized_statuses:
        allowed = set(normalized_statuses)
        memory = [run for run in memory if str(run.get("status") or "") in allowed]
    by_id = {
        str(run.get("run_id") or ""): run
        for run in persisted
        if isinstance(run, dict) and run.get("run_id")
    }
    for run in memory:
        run_id = str(run.get("run_id") or "")
        existing = by_id.get(run_id)
        if not existing or float(run.get("updated_at") or 0) >= float(existing.get("updated_at") or 0):
            by_id[run_id] = run
    return sorted(
        by_id.values(),
        key=lambda run: (float(run.get("updated_at") or 0), float(run.get("started_at") or 0)),
        reverse=True,
    )[:max(1, min(int(limit or 200), 1000))]


def session_status(
    stored_session_id: str,
    db: Any = None,
    *,
    current_gateway_instance_id: str = "",
) -> dict[str, Any]:
    _recover_orphaned_active_runs(
        db,
        current_gateway_instance_id=current_gateway_instance_id,
    )
    persisted_status = None
    if method := _db_method(db, "get_session_run_status"):
        try:
            persisted_status = method(stored_session_id)
        except Exception:
            persisted_status = None
    runs = list_runs(stored_session_id, db=db)
    active = [
        run for run in runs
        if str(run.get("status") or "") in ACTIVE_RUN_STATUSES
    ]
    last_seq = 0
    with _lock:
        events = _events_by_session.get(str(stored_session_id or "").strip(), ())
        for event in events:
            last_seq = max(last_seq, int(event.get("seq") or 0))
    if isinstance(persisted_status, dict):
        last_seq = max(last_seq, int(persisted_status.get("last_event_seq") or 0))
    active_run = max(active, key=lambda run: float(run.get("updated_at") or 0), default=None)
    if active_run is None and isinstance(persisted_status, dict) and persisted_status.get("running"):
        return {
            "running": True,
            "active_run_id": str(persisted_status.get("active_run_id") or ""),
            "active_turn_id": str(persisted_status.get("active_turn_id") or ""),
            "runtime_scope_key": str(persisted_status.get("runtime_scope_key") or ""),
            "run_started_at": float(persisted_status.get("run_started_at") or 0),
            "run_updated_at": float(persisted_status.get("run_updated_at") or 0),
            "last_event_seq": last_seq,
        }
    return {
        "running": bool(active_run),
        "active_run_id": str((active_run or {}).get("run_id") or ""),
        "active_turn_id": str((active_run or {}).get("turn_id") or ""),
        "runtime_scope_key": str((active_run or {}).get("runtime_scope_key") or ""),
        "run_started_at": float((active_run or {}).get("started_at") or 0),
        "run_updated_at": float((active_run or {}).get("updated_at") or 0),
        "last_event_seq": last_seq,
    }
