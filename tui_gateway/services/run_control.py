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

from agent.dovie_diagnostics import emit_dovie_diagnostic, emit_dovie_runtime_diagnostic
from hermes_agent.domain.participants import agent_participant_id
from hermes_agent.domain.participants import leader_participant_id
from hermes_agent.domain.participants import member_participant_id
from hermes_agent.domain.run_state_machine import ACTIVE_RUN_STATUSES
from hermes_agent.domain.run_state_machine import TERMINAL_RUN_STATUSES
from hermes_runtime_event_payloads import primary_deliverable_text
from hermes_team_mission.domain.node_kinds import normalize_team_mission_node_kind
from tui_gateway.services import team_mission_activity_events as _team_activity_events
from tui_gateway.services import runtime_streams as _runtime_streams
from tui_gateway.services import runtime_event_protocol as _runtime_event_protocol
from tui_gateway.services.subscription_poll_lifecycle import SubscriptionPollLifecycle
from tui_gateway.services.run_control_events import (
    delta_event_for_subscription as _delta_event_for_subscription,
    event_run_id as _event_run_id,
    event_runtime_scope_key as _event_runtime_scope_key,
    event_turn_id as _event_turn_id,
    payload_status as _payload_status,
    remember_stream_delivery as _remember_stream_delivery,
    remember_terminal_delivery as _remember_direct_terminal_delivery,
    conversation_session_id as _conversation_session_id,
    stamp_session_identity as _stamp_session_identity,
    stream_text_delta as _stream_text_delta,
    terminal_delivery_identity as _terminal_delivery_identity,
)
from tui_gateway.services.run_events import list_runtime_events
from tui_gateway.transport import Transport

if TYPE_CHECKING:
    from hermes_team_mission.domain.run_context import RunContext

_MAX_EVENTS_PER_SESSION = 2000
_LOCK_TYPE = type(threading.Lock())
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


class WorkerTerminalOwnershipError(RuntimeError):
    """A worker attempted to persist a terminal run transition directly."""


_STREAM_TRACE_EVENT_TYPES = {
    "message.start",
    "message.delta",
    "message.complete",
    "reasoning.delta",
    "thinking.delta",
    "subagent.output_delta",
    "subagent.reasoning_delta",
    "subagent.progress",
    "subagent.tool",
}
_STREAM_CHECKPOINT_BOUNDARY_TYPES = {
    "message.complete",
    "error",
    "session.interrupted",
    "session.recalled",
    "tool.start",
    "tool.complete",
    "approval.request",
    "clarify.request",
    "secret.request",
    "sudo.request",
    "input_approval.request",
    "subagent.tool",
    "subagent.progress",
    "subagent.complete",
    "agent_profile_test.tool",
    "agent_profile_test.progress",
    "agent_profile_test.complete",
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
        "payload_offset": payload.get("offset"),
        "subagent_id": str(payload.get("subagent_id") or payload.get("subagentId") or ""),
        "delegate_call_id": str(
            payload.get("delegate_call_id") or payload.get("delegateCallId") or ""
        ),
        "source": str(payload.get("source") or ""),
    }


def _trace_stream_route(stage: str, **fields: Any) -> None:
    emit_dovie_diagnostic("[dovie-stream-route]", {"stage": stage, **fields})


def _trace_team_runtime_chain(stage: str, **fields: Any) -> None:
    emit_dovie_diagnostic("[dovie-team-runtime-chain]", {"stage": stage, **fields})


def _team_activity_terminal_log(stage: str, **fields: Any) -> None:
    emit_dovie_runtime_diagnostic("dovie-team-activity", stage, fields)


_lock = threading.RLock()
_events_by_session: dict[str, deque[dict[str, Any]]] = defaultdict(
    lambda: deque(maxlen=_MAX_EVENTS_PER_SESSION)
)
_subscribers_by_session: dict[str, set[Transport]] = defaultdict(set)
_subscriptions_by_id: dict[str, dict[str, Any]] = {}
_subscription_ids_by_session: dict[str, set[str]] = defaultdict(set)
_subscription_ids_by_activity: dict[str, set[str]] = defaultdict(set)
_subscription_ids_by_transport: dict[Transport, set[str]] = defaultdict(set)
_run_state_by_id: dict[str, dict[str, Any]] = {}
_run_ids_by_session: dict[str, list[str]] = defaultdict(list)
_last_seq_by_session: dict[str, int] = defaultdict(int)
_participant_id_by_resolution_key: dict[tuple[str, str, str, str, str], str] = {}
_team_mission_binding_by_run: dict[str, dict[str, Any]] = {}
_subscription_poller_thread: threading.Thread | None = None
_subscription_poll_lifecycle = SubscriptionPollLifecycle()
_team_mission_ready_scheduler: Any = None


def _reset_for_tests() -> None:
    with _lock:
        subscription_ids = set(_subscriptions_by_id)
        _events_by_session.clear()
        _subscribers_by_session.clear()
        _subscriptions_by_id.clear()
        _subscription_ids_by_session.clear()
        _subscription_ids_by_activity.clear()
        _subscription_ids_by_transport.clear()
        _run_state_by_id.clear()
        _run_ids_by_session.clear()
        _last_seq_by_session.clear()
        _participant_id_by_resolution_key.clear()
        _team_mission_binding_by_run.clear()
    _subscription_poll_lifecycle.wait(subscription_ids)
    _runtime_streams.reset_for_tests()
    _runtime_event_protocol.reset_for_tests()


def _db_method(db: Any, name: str):
    if db is None or db.__class__.__module__.startswith("unittest.mock"):
        return None
    method = getattr(db, name, None)
    return method if callable(method) else None


def _run_method(db: Any, name: str):
    if db is None or db.__class__.__module__.startswith("unittest.mock"):
        return None
    method = getattr(db.runs, name, None)
    return method if callable(method) else None


def _has_run_state_schema(conn: Any) -> bool:
    try:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'runs'"
        ).fetchone()
    except Exception:
        return False
    return row is not None


def _db_label(db: Any = None) -> str:
    value = getattr(db, "db_path", "") if db is not None else ""
    return str(value or "")


def _memory_scope_key(db: Any = None) -> str:
    """Return the control-plane memory scope for a concrete DB/profile.

    Live run ids are not globally unique across profile homes. Persisted state
    is scoped by SQLite DB, so the in-process mirror must use the same boundary
    instead of keying by bare run_id.
    """
    return _db_label(db).strip()


def _memory_run_key(run_id: str, db: Any = None) -> str:
    normalized = str(run_id or "").strip()
    scope = _memory_scope_key(db)
    return f"{scope}\x1f{normalized}" if scope and normalized else normalized


def _memory_session_key(session_id: str, db: Any = None) -> str:
    normalized = str(session_id or "").strip()
    scope = _memory_scope_key(db)
    return f"{scope}\x1f{normalized}" if scope and normalized else normalized


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
        "session_id": str(run.get("session_id") or run.get("conversation_session_id") or ""),
        "turn_id": str(run.get("turn_id") or ""),
        "runtime_scope_key": str(run.get("runtime_scope_key") or ""),
        "execution_session_id": str(run.get("execution_session_id") or ""),
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


def _live_execution_session_ids_snapshot() -> set[str]:
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
    # S8: orphan-recovery is a main-process responsibility. When the main
    # sidecar disconnects (crash/restart), worker processes keep polling
    # ``session_status`` / ``create_run_if_session_idle``, and each call
    # triggered a recovery scan that found no live main-side run owners —
    # producing noise (triage counted 79 spurious scans in one session).
    # Short-circuit in workers so only the main process runs the scan.
    from tui_gateway.process_role import is_worker_process
    if is_worker_process():
        return 0
    method = _run_method(db, "fail_orphaned")
    if method is None:
        return 0
    try:
        failed = int(
            method(
                live_execution_session_ids=_live_execution_session_ids_snapshot(),
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
    """Dispatch the Team Mission scheduler off the caller's loop.

    record_event() can be called from arbitrary contexts, including the
    worker runtime loop when worker subprocesses write terminal events back.
    Running the scheduler synchronously on that path can eventually call
    team_mission.node.start -> _proxy_run_submit_via_worker, which refuses to
    synchronously wait on the worker runtime loop. Off-loading the scheduler to
    a daemon thread breaks that loop identity and lets record_event return.
    """
    normalized_mission_id = str(mission_id or "").strip()
    if not normalized_mission_id:
        return
    with _lock:
        callback = _team_mission_ready_scheduler
    if not callable(callback):
        return

    def _run_callback() -> None:
        try:
            callback(
                mission_id=normalized_mission_id,
                db=db,
                trigger_event=trigger_event,
                run_id=run_id,
            )
        except Exception:
            logger.warning("failed to schedule Team Mission ready nodes", exc_info=True)

    threading.Thread(
        target=_run_callback,
        name=f"team-mission-scheduler:{normalized_mission_id[:8]}",
        daemon=True,
    ).start()


def _remember_subscription_delivery(
    subscription: dict[str, Any],
    event: dict[str, Any],
    *,
    direct: bool = False,
) -> None:
    cursor_field, seq = _subscription_event_cursor(subscription, event)
    if cursor_field and seq > 0:
        subscription[cursor_field] = max(int(subscription.get(cursor_field) or 0), seq)
    _remember_subscription_run(subscription, event)
    if direct or _terminal_delivery_identity(event):
        _remember_direct_terminal_delivery(subscription, event)
    if direct:
        _remember_stream_delivery(subscription, event)


def _event_activity_id(event: dict[str, Any]) -> str:
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    metadata = event.get("metadata") if isinstance(event.get("metadata"), dict) else {}
    return str(
        event.get("activity_id")
        or event.get("activityId")
        or payload.get("activity_id")
        or payload.get("activityId")
        or metadata.get("activity_id")
        or metadata.get("activityId")
        or ""
    ).strip()


def _raw_event_seq(event: dict[str, Any]) -> int:
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    try:
        return max(0, int(event.get("seq") or payload.get("seq") or 0))
    except (TypeError, ValueError):
        return 0


def _event_activity_seq(event: dict[str, Any]) -> int:
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    try:
        return max(
            0,
            int(
                event.get("activity_event_seq")
                or event.get("activityEventSeq")
                or payload.get("activity_event_seq")
                or payload.get("activityEventSeq")
                or 0
            ),
        )
    except (TypeError, ValueError):
        return 0


def _activity_subscription_uses_mission_journal_cursor(subscription: dict[str, Any]) -> bool:
    activity_id = str(subscription.get("activity_id") or "").strip()
    if not activity_id:
        return False
    if _team_activity_events.is_team_dispatch_activity_id(activity_id):
        return True
    return _team_activity_events.uses_mission_activity_journal(
        activity_id,
        db=subscription.get("db"),
    )


def _subscription_event_cursor(
    subscription: dict[str, Any],
    event: dict[str, Any],
) -> tuple[str, int]:
    if bool(event.get("transient")):
        return "", 0
    if str(subscription.get("kind") or "session") == "activity":
        activity_seq = _event_activity_seq(event)
        if activity_seq > 0:
            return "activity_event_last_seq", activity_seq
        if _activity_subscription_uses_mission_journal_cursor(subscription):
            return "", 0
    return "last_seq", _raw_event_seq(event)


def _subscription_after_seq(subscription: dict[str, Any]) -> int:
    if (
        str(subscription.get("kind") or "session") == "activity"
        and _team_activity_events.uses_mission_activity_journal(
            str(subscription.get("activity_id") or "").strip(),
            db=subscription.get("db"),
        )
    ):
        return int(subscription.get("activity_event_last_seq") or 0)
    return int(subscription.get("last_seq") or 0)


def _team_mission_run_binding(event: dict[str, Any], db: Any = None) -> dict[str, Any]:
    run_id = _event_run_id(event)
    if not run_id:
        return {}
    cache_key = _memory_run_key(run_id, db)
    with _lock:
        cached = _team_mission_binding_by_run.get(cache_key)
    if cached:
        return dict(cached)
    binding_getter = _db_method(db, "get_team_mission_run_binding")
    if binding_getter is None:
        return {}
    try:
        binding = binding_getter(run_id)
    except Exception:
        return {}
    if not isinstance(binding, dict) or not binding:
        return {}
    with _lock:
        _team_mission_binding_by_run[cache_key] = dict(binding)
    return dict(binding)


def _event_has_team_mission_run_binding(event: dict[str, Any], db: Any = None) -> bool:
    return bool(_team_mission_run_binding(event, db=db))


def _team_mission_runtime_event_allows_conversation_status(
    *,
    event_type: str,
    binding: dict[str, Any],
    db: Any = None,
) -> bool:
    if str(event_type or "").strip() not in _TEAM_MISSION_STATUS_SOURCE_EVENT_TYPES:
        return False
    mission_id = str((binding or {}).get("mission_id") or "").strip()
    node_id = str((binding or {}).get("node_id") or "").strip()
    node: dict[str, Any] = {}
    if mission_id and node_id:
        node_getter = _db_method(db, "get_team_mission_node")
        if node_getter is not None:
            try:
                candidate_node = node_getter(mission_id, node_id)
            except Exception:
                candidate_node = None
            node = dict(candidate_node) if isinstance(candidate_node, dict) else {}
    node_kind = normalize_team_mission_node_kind(node.get("kind"), default="")
    return node_kind != "synthesis"


def _on_run_event_appended(db: Any, event: dict[str, Any]) -> None:
    binding = _team_mission_run_binding(event, db=db)
    mission_id = str(binding.get("mission_id") or "").strip()
    if not mission_id:
        mission_id = _team_activity_events.mission_id_for_run_event(event, db=db)
    if not mission_id:
        return
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    activity_id = str(
        event.get("activity_id")
        or event.get("activityId")
        or payload.get("activity_id")
        or payload.get("activityId")
        or ""
    ).strip()
    session_id = str(
        event.get("conversation_session_id")
        or event.get("session_id")
        or ""
    ).strip()
    is_canonical_activity_row = bool(
        activity_id == f"mission:{mission_id}"
        and session_id == f"team:mission:{mission_id}:events"
    )
    # The mission activity journal is the sole frontend publisher. Source-run
    # rows and transient execution frames are never eligible for activity
    # delivery, even when they contain the same mission binding.
    if not is_canonical_activity_row:
        return
    _team_activity_terminal_log(
        "run-event-appended",
        mission_id=mission_id,
        event_type=str(event.get("type") or "") if isinstance(event, dict) else "",
        seq=event.get("seq") if isinstance(event, dict) else None,
        subscription_activity_count=len(_subscription_ids_by_activity),
    )
    _team_activity_events.deliver_appended_event(
        mission_id,
        event,
        lock=_lock,
        subscription_ids_by_activity=_subscription_ids_by_activity,
        subscriptions_by_id=_subscriptions_by_id,
        live_status_event_for_subscription=_team_mission_live_status_event_for_subscription,
        deliver_subscription_event=_deliver_subscription_event,
    )


def event_uses_canonical_activity_journal(
    event: dict[str, Any],
    *,
    db: Any = None,
) -> bool:
    """Return whether frontend delivery is owned by a mission journal.

    A Team Mission run event is persisted in its source execution session for
    execution history and projected into exactly one mission activity journal
    for UI delivery.  Once the run is bound to a mission, the source-session
    event bus and the gateway's legacy direct-write path must stay silent;
    otherwise the desktop receives the same semantic event from two transports.
    """

    if db is None or not isinstance(event, dict):
        return False
    return bool(_team_mission_run_binding(event, db=db))


def _ensure_run_event_listener_registered(db: Any) -> bool:
    if db is None:
        return False
    try:
        db.runs.register_event_listener(
            "runtime.activity.subscribe",
            lambda event: _on_run_event_appended(db, event),
        )
    except Exception as exc:
        _team_activity_terminal_log(
            "listener-register-failed",
            error=f"{type(exc).__name__}: {exc}",
        )
        return False
    _team_activity_terminal_log("listener-registered", source="run_events")
    return True


def remember_transport_delivery(
    transport: Transport | None,
    event: dict[str, Any],
    *,
    direct: bool = True,
) -> None:
    if transport is None or not isinstance(event, dict):
        return
    stable = _conversation_session_id(event)
    with _lock:
        for subscription_id in list(_subscription_ids_by_transport.get(transport, set())):
            subscription = _subscriptions_by_id.get(subscription_id)
            if not isinstance(subscription, dict):
                continue
            if subscription.get("transport") is not transport:
                continue
            kind = str(subscription.get("kind") or "session")
            if kind == "session":
                if stable and str(subscription.get("conversation_session_id") or "").strip() != stable:
                    continue
                if not _session_subscription_matches_event(subscription, event):
                    continue
            elif kind == "activity":
                if str(subscription.get("activity_id") or "").strip() != _event_activity_id(event):
                    continue
                _remember_subscription_run(subscription, event)
                continue
            _remember_subscription_delivery(subscription, event, direct=direct)


def _subscription_delivery_lock(subscription_id: str) -> threading.Lock | None:
    with _lock:
        subscription = _subscriptions_by_id.get(subscription_id)
        if subscription is None:
            return None
        delivery_lock = subscription.get("delivery_lock")
        if not isinstance(delivery_lock, _LOCK_TYPE):
            delivery_lock = threading.Lock()
            subscription["delivery_lock"] = delivery_lock
        return delivery_lock


def _deliver_subscription_event(
    subscription_id: str,
    transport: Transport,
    event: dict[str, Any],
) -> bool:
    """Serialize write and cursor advancement for one subscription."""
    delivery_lock = _subscription_delivery_lock(subscription_id)
    if delivery_lock is None:
        return False
    with delivery_lock:
        with _lock:
            subscription = _subscriptions_by_id.get(subscription_id)
            if subscription is None or subscription.get("transport") is not transport:
                return False
            event_for_transport = _delta_event_for_subscription(subscription, event)
            if event_for_transport is None:
                cursor_field, seq = _subscription_event_cursor(subscription, event)
                if seq > int(subscription.get(cursor_field) or 0):
                    subscription[cursor_field] = seq
                return True
            cursor_field, seq = _subscription_event_cursor(
                subscription,
                event_for_transport,
            )
            if seq > 0 and seq <= int(subscription.get(cursor_field) or 0):
                return True
        if not _write_event(transport, event_for_transport):
            return False
        with _lock:
            current = _subscriptions_by_id.get(subscription_id)
            if current is not None and current.get("transport") is transport:
                _remember_subscription_delivery(
                    current,
                    event_for_transport,
                    direct=False,
                )
        return True


def _deliver_live_subscription_event(
    transport: Transport,
    event: dict[str, Any],
) -> bool:
    """Project and atomically deliver a persisted live event."""
    if transport is None or not isinstance(event, dict):
        return False
    stable = _conversation_session_id(event)
    with _lock:
        subscription_ids = list(_subscription_ids_by_transport.get(transport, set()))
    for subscription_id in subscription_ids:
        with _lock:
            subscription = _subscriptions_by_id.get(subscription_id)
        if not isinstance(subscription, dict):
            continue
        if subscription.get("transport") is not transport:
            continue
        kind = str(subscription.get("kind") or "session")
        if kind == "session":
            if stable and str(subscription.get("conversation_session_id") or "").strip() != stable:
                continue
            if not _session_subscription_matches_event(subscription, event):
                continue
        elif kind == "activity":
            if str(subscription.get("activity_id") or "").strip() != _event_activity_id(event):
                continue
        else:
            continue
        projected = _team_mission_live_status_event_for_subscription(
            subscription,
            event,
        )
        return _deliver_subscription_event(
            subscription_id,
            transport,
            projected,
        )
    return _write_event(transport, event)


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
    run_getter = _run_method(subscription.get("db"), "get")
    run = run_getter(source_run_id) if run_getter is not None else {}
    run = run if isinstance(run, dict) else {}

    live_conversation = dict(conversation)
    live_conversation["running"] = True
    live_conversation["run_state"] = "running"
    live_conversation["activity_state"] = "running"
    live_conversation["active_run_id"] = source_run_id
    live_conversation["active_turn_id"] = str(run.get("turn_id") or binding.get("turn_id") or "")
    live_conversation["active_execution_session_id"] = str(
        run.get("execution_session_id") or binding.get("execution_session_id") or ""
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


def _terminal_status(event_type: str, payload: dict[str, Any]) -> str | None:
    if event_type == "error":
        return "failed"
    if event_type == "session.recalled":
        return "interrupted"
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


# PR-1 identity contract: event families the frontend attributes to a
# specific run lane (assistant text / reasoning / tool cards). Frames of
# these types must never leave the main process without a run identity —
# the FE timeline keys segments by (run_id, turn_id) and an empty run_id
# historically caused cross-run reasoning-text absorption (triage
# 2026-07, symptom two). Interaction requests (clarify/approval/...) are
# keyed by request_id and handled by the PendingRegistry contract, so
# they are intentionally NOT in this set.
_RUN_IDENTITY_EVENT_TYPE_PREFIXES = (
    "message.",
    "reasoning.",
    "thinking.",
    "tool.",
    "subagent.",
    "artifact.",
)
_RUN_IDENTITY_EVENT_TYPES = {"error"}


def _frame_requires_run_identity(event_type: str) -> bool:
    normalized = str(event_type or "").strip()
    if not normalized:
        return False
    if normalized in _RUN_IDENTITY_EVENT_TYPES:
        return True
    return normalized.startswith(_RUN_IDENTITY_EVENT_TYPE_PREFIXES)


def _ensure_outbound_run_identity(params: dict[str, Any]) -> None:
    """Main-side identity contract (PR-1 §4.1): run-scoped frames must not
    leave the process with an empty ``run_id``.

    Mutates ``params`` in place — callers (``publish_recorded_event``,
    ``server._emit``) deliver that same dict to subscribers, so the
    synthesized identity travels on the wire and into persistence.

    ``synthetic_run_id`` marks frames whose run identity was invented
    here: run/state bookkeeping (in-memory ``_run_state_by_id`` and the
    ``runs`` table) skips them so a synthesized id can never open a
    phantom "running" run, while the FE gets a stable orphan lane
    instead of a runId-less frame.
    """
    if not isinstance(params, dict):
        return
    event_type = str(params.get("type") or "").strip()
    if not _frame_requires_run_identity(event_type):
        return
    run_id = _event_run_id(params)
    turn_id = _event_turn_id(params)
    if run_id and turn_id:
        return
    stable = _conversation_session_id(params)
    payload = params.get("payload") if isinstance(params.get("payload"), dict) else None
    if not run_id:
        # Deterministic per (session, turn): every orphan frame of the same
        # turn lands in one synthetic lane instead of fragmenting per event.
        run_id = f"synthetic-run:{stable or 'unknown-session'}:{turn_id or 'orphan'}"
        params["run_id"] = run_id
        params["synthetic_run_id"] = True
        if payload is not None:
            payload["run_id"] = run_id
        if not turn_id:
            turn_id = f"synthetic-turn:{run_id}"
            params["turn_id"] = turn_id
            if payload is not None:
                payload["turn_id"] = turn_id
        logger.error(
            "[dovie-run-control] run-event-missing-run-id synthesized identity %s",
            _json_for_log(
                {
                    "event_type": event_type,
                    "session_id": stable,
                    "run_id": run_id,
                    "turn_id": turn_id,
                    "seq": params.get("seq"),
                }
            ),
        )
        return
    # run_id present but turn_id missing: surface the contract gap without
    # synthesizing. A synthetic turn would overwrite the real turn_id kept
    # on the runs row (append_run_event backfills runs.turn_id via
    # COALESCE(NULLIF(?, ''), ...)), which is worse than an empty turn the
    # FE can still attach by run_id alone.
    _diagnostic_warning(
        "run-event-missing-turn-id",
        event_type=event_type,
        session_id=stable,
        run_id=run_id,
        seq=params.get("seq"),
    )


def _positive_frame_int(value: Any) -> int:
    try:
        parsed = int(value or 0)
    except (TypeError, ValueError):
        return 0
    return parsed if parsed > 0 else 0


def _sync_canonical_frame_identity(
    saved: Any,
    *,
    frame: dict[str, Any],
    params: dict[str, Any],
    stable: str,
    run_id: str,
    event_type: str = "",
    db: Any = None,
) -> None:
    """Write authoritative post-persist ids back into the outbound frame.

    ``append_run_event`` may bump the requested seq (seq = max(requested,
    MAX+1) per conversation) and duplicate dispositions return the canonical
    already-persisted event. The FE event ledger is keyed by canonical
    ``run_events.seq``, so the frame delivered to subscribers afterwards
    must carry the persisted value — both ``frame`` (the copy retained in
    ``_events_by_session``) and the caller's ``params`` (the dict
    ``publish_recorded_event`` hands to transports after this returns).

    ``runtime_source_seq`` is the temporary bridge for raw-worker/canonical
    dedupe during the SeqAllocator migration. It must also be present on live
    delivery, not just replay rows, until the frontend capability deprecation
    removes that fallback.
    """
    if not isinstance(saved, dict):
        return
    canonical_seq = _positive_frame_int(saved.get("seq"))
    outbound_seq = _positive_frame_int(frame.get("seq"))
    if canonical_seq > 0 and canonical_seq != outbound_seq:
        frame["seq"] = canonical_seq
        if isinstance(params, dict):
            params["seq"] = canonical_seq
            params_payload = params.get("payload")
            if isinstance(params_payload, dict) and "seq" in params_payload:
                params_payload["seq"] = canonical_seq
        frame_payload = frame.get("payload")
        if isinstance(frame_payload, dict) and "seq" in frame_payload:
            frame_payload["seq"] = canonical_seq
        with _lock:
            if stable:
                memory_session_key = _memory_session_key(stable, db)
                _last_seq_by_session[memory_session_key] = max(
                    int(_last_seq_by_session.get(memory_session_key) or 0), canonical_seq
                )
            state = _run_state_by_id.get(_memory_run_key(run_id, db)) if run_id else None
            if state is None and run_id:
                state = _run_state_by_id.get(run_id)
            if isinstance(state, dict):
                state["last_seq"] = max(int(state.get("last_seq") or 0), canonical_seq)
        _diagnostic_warning(
            "run-event-seq-rewritten-to-canonical",
            event_type=event_type,
            session_id=stable,
            run_id=run_id,
            requested_seq=outbound_seq,
            canonical_seq=canonical_seq,
        )
    raw_frame_payload = frame.get("payload")
    frame_payload = raw_frame_payload if isinstance(raw_frame_payload, dict) else {}
    runtime_source_seq = _positive_frame_int(
        saved.get("runtime_source_seq")
        or frame.get("runtime_source_seq")
        or frame_payload.get("runtime_source_seq")
    )
    if runtime_source_seq <= 0:
        return
    frame["runtime_source_seq"] = runtime_source_seq
    if isinstance(params, dict):
        params["runtime_source_seq"] = runtime_source_seq
        params_payload = params.get("payload")
        if isinstance(params_payload, dict):
            params_payload["runtime_source_seq"] = runtime_source_seq
    if isinstance(raw_frame_payload, dict):
        frame_payload["runtime_source_seq"] = runtime_source_seq


_sync_canonical_frame_seq = _sync_canonical_frame_identity


def _active_run_ids_for_session(stable: str, db: Any = None) -> set[str]:
    active_ids: set[str] = set()
    if method := _run_method(db, "list"):
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
    memory_session_key = _memory_session_key(stable, db)
    with _lock:
        for run_key in _run_ids_by_session.get(memory_session_key, ()):
            state = _run_state_by_id.get(run_key) or {}
            if str(state.get("status") or "") in ACTIVE_RUN_STATUSES:
                run_id = str(state.get("run_id") or "").strip()
                if run_id:
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
                or str(event.get("conversation_session_id") or "")
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
            or str(event.get("conversation_session_id") or "")
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
        _poll_subscription_events_once()


def _poll_subscription_events_once() -> None:
    """Poll one registry snapshot while protecting borrowed DB handles."""

    with _lock:
        subscriptions = [
            dict(subscription)
            for subscription in _subscriptions_by_id.values()
            if subscription.get("transport") is not None
        ]
        _subscription_poll_lifecycle.start(
            str(subscription.get("id") or "") for subscription in subscriptions
        )
    for subscription in subscriptions:
        subscription_id = str(subscription.get("id") or "")
        try:
            _poll_one_subscription(subscription)
        finally:
            _subscription_poll_lifecycle.finish(subscription_id)


def _poll_one_subscription(subscription: dict[str, Any]) -> None:
    """Poll one accepted subscription without holding the registry lock."""

    db = subscription.get("db")
    if db is None:
        return
    subscription_kind = str(subscription.get("kind") or "session")
    stable = str(subscription.get("conversation_session_id") or "").strip()
    activity_id = str(subscription.get("activity_id") or "").strip()
    transport = subscription.get("transport")
    if transport is None:
        return
    if subscription_kind == "activity" and not activity_id:
        return
    if subscription_kind != "activity" and not stable:
        return
    try:
        last_seq = _subscription_after_seq(subscription)
        active_only = bool(subscription.get("active_only"))
        runtime_scope_key = str(subscription.get("runtime_scope_key") or "").strip()
        active_run_ids = set(subscription.get("active_run_ids") or set())
        if active_only:
            active_run_ids.update(_active_run_ids_for_session(stable, db=db))
        if subscription_kind == "activity":
            events = _team_activity_events.list_activity_events(
                db,
                activity_id,
                after_seq=last_seq,
                limit=_MAX_EVENTS_PER_SESSION,
                event_activity_id=_event_activity_id,
            )
        else:
            events = list_runtime_events(
                db,
                stable,
                after_seq=last_seq,
                active_only=False,
                runtime_scope_key=runtime_scope_key,
                limit=_MAX_EVENTS_PER_SESSION,
            )
    except Exception:
        logger.debug("failed to poll run event log", exc_info=True)
        return
    events = [event for event in events if isinstance(event, dict)]
    if subscription_kind != "activity":
        events = [
            event for event in events if not _is_team_mission_runtime_event(event)
        ]
        events = _filter_events_for_subscription(
            events,
            active_only=active_only,
            active_run_ids=active_run_ids,
            runtime_scope_key=runtime_scope_key,
            run_id=str(subscription.get("run_id") or ""),
        )
    if not events:
        return
    delivered_seq = last_seq
    for event in events:
        seq = int(event.get("seq") or 0)
        if seq <= delivered_seq:
            continue
        event_type = str(event.get("type") or "")
        if event_type in _STREAM_TRACE_EVENT_TYPES:
            _trace_stream_route(
                "subscription-poll-delivery",
                event_type=event_type,
                subscription_id=str(subscription.get("id") or ""),
                subscription_kind=subscription_kind,
                conversation_session_id=stable,
                mission_id="",
                activity_id=activity_id,
                run_id=_event_run_id(event),
                turn_id=_event_turn_id(event),
                runtime_scope_key=_event_runtime_scope_key(event),
                seq=seq,
                previous_delivered_seq=delivered_seq,
                transport=_transport_debug_id(transport),
                **_stream_trace_summary(event),
            )
        if not _deliver_subscription_event(
            str(subscription.get("id") or ""),
            transport,
            event,
        ):
            break
        delivered_seq = seq
    with _lock:
        current = _subscriptions_by_id.get(str(subscription.get("id") or ""))
        if current is not None:
            cursor_field = (
                "activity_event_last_seq"
                if (
                    subscription_kind == "activity"
                    and _team_activity_events.uses_mission_activity_journal(activity_id, db=db)
                )
                else "last_seq"
            )
            current[cursor_field] = max(
                int(current.get(cursor_field) or 0), delivered_seq
            )
            if active_only:
                current_active_run_ids = set(current.get("active_run_ids") or set())
                current_active_run_ids.update(active_run_ids)
                current["active_run_ids"] = current_active_run_ids


def _ensure_run(
    *,
    conversation_session_id: str,
    run_id: str,
    runtime_scope_key: str = "",
    turn_id: str = "",
    execution_session_id: str = "",
    db: Any = None,
) -> dict[str, Any]:
    now = time.time()
    memory_run_key = _memory_run_key(run_id, db)
    memory_session_key = _memory_session_key(conversation_session_id, db)
    state = _run_state_by_id.get(memory_run_key)
    if state is None:
        state = {
            "run_id": run_id,
            "turn_id": turn_id,
            "session_id": execution_session_id,
            "conversation_session_id": conversation_session_id,
            "runtime_scope_key": runtime_scope_key or conversation_session_id,
            "status": "running",
            "started_at": now,
            "updated_at": now,
            "last_seq": 0,
            "error": "",
        }
        _run_state_by_id[memory_run_key] = state
        if memory_run_key not in _run_ids_by_session[memory_session_key]:
            _run_ids_by_session[memory_session_key].append(memory_run_key)
    else:
        state["updated_at"] = now
        if turn_id:
            state["turn_id"] = turn_id
        if execution_session_id:
            state["session_id"] = execution_session_id
        if runtime_scope_key:
            state["runtime_scope_key"] = runtime_scope_key
        if conversation_session_id:
            state["conversation_session_id"] = conversation_session_id
    return state


def mark_run_started(
    *,
    conversation_session_id: str,
    execution_session_id: str,
    run_id: str,
    turn_id: str = "",
    runtime_scope_key: str = "",
    metadata: dict[str, Any] | None = None,
    db: Any = None,
) -> dict[str, Any]:
    stable = str(conversation_session_id or execution_session_id or "").strip()
    normalized_run_id = str(run_id or "").strip()
    if not stable or not normalized_run_id:
        return {}
    with _lock:
        state = _ensure_run(
            conversation_session_id=stable,
            run_id=normalized_run_id,
            runtime_scope_key=runtime_scope_key or stable,
            turn_id=turn_id,
            execution_session_id=str(execution_session_id or "").strip(),
            db=db,
        )
        state["status"] = "running"
        state["error"] = ""
        snapshot = dict(state)
    if method := _run_method(db, "upsert"):
        try:
            persisted = method(
                run_id=normalized_run_id,
                session_id=stable,
                runtime_scope_key=runtime_scope_key or stable,
                turn_id=turn_id,
                execution_session_id=str(execution_session_id or "").strip(),
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
    conversation_session_id: str,
    run_id: str,
    turn_id: str = "",
    runtime_scope_key: str = "",
    execution_session_id: str = "",
    metadata: dict[str, Any] | None = None,
    db: Any = None,
) -> dict[str, Any]:
    stable = str(conversation_session_id or execution_session_id or "").strip()
    normalized_run_id = str(run_id or "").strip()
    if not stable or not normalized_run_id:
        return {"run": None, "conflict": None}

    if method := _run_method(db, "reserve_if_idle"):
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
                execution_session_id=execution_session_id,
                status="queued",
                metadata=metadata,
            )
            run = result.get("run") if isinstance(result, dict) else None
            conflict = result.get("conflict") if isinstance(result, dict) else None
            created = bool(result.get("created")) if isinstance(result, dict) else False
            if isinstance(run, dict) and run:
                with _lock:
                    state = _ensure_run(
                        conversation_session_id=stable,
                        run_id=normalized_run_id,
                        runtime_scope_key=runtime_scope_key or stable,
                        turn_id=turn_id,
                        execution_session_id=execution_session_id,
                        db=db,
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
                        "execution_session_id": execution_session_id,
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
                    "conversation_session_id": stable,
                    "runtime_scope_key": str(persisted_status.get("runtime_scope_key") or ""),
                    "status": "running",
                    "started_at": float(persisted_status.get("run_started_at") or 0),
                    "updated_at": float(persisted_status.get("run_updated_at") or 0),
                    "last_seq": int(persisted_status.get("last_event_seq") or 0),
                },
                "created": False,
            }

    with _lock:
        memory_session_key = _memory_session_key(stable, db)
        for active_run_key in _run_ids_by_session.get(memory_session_key, ()):
            active = _run_state_by_id.get(active_run_key) or {}
            active_run_id = str(active.get("run_id") or "").strip()
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
                        "execution_session_id": execution_session_id,
                        "gateway_instance_id": _gateway_instance_id_from_metadata(metadata),
                        "gateway_pid": os.getpid(),
                    },
                    conflict=_run_summary(dict(active)),
                )
                return {"run": None, "conflict": dict(active), "created": False}
        state = _ensure_run(
            conversation_session_id=stable,
            run_id=normalized_run_id,
            runtime_scope_key=runtime_scope_key or stable,
            turn_id=turn_id,
            execution_session_id=execution_session_id,
            db=db,
        )
        state["status"] = "queued"
        if isinstance(metadata, dict) and metadata:
            current_metadata = state.get("metadata")
            if not isinstance(current_metadata, dict):
                current_metadata = {}
            current_metadata.update(metadata)
            state["metadata"] = current_metadata
        return {"run": dict(state), "conflict": None, "created": True}


def next_event_seq(conversation_session_id: str, fallback_seq: int = 0, db: Any = None) -> int:
    stable = str(conversation_session_id or "").strip()
    if not stable:
        return int(fallback_seq or 0)
    persisted_next = 0
    if method := _run_method(db, "next_event_seq"):
        try:
            persisted_next = int(method(stable, fallback_seq=fallback_seq) or 0)
        except Exception:
            persisted_next = 0
    with _lock:
        next_seq = max(
            int(_last_seq_by_session.get(_memory_session_key(stable, db)) or 0) + 1,
            int(fallback_seq or 0),
            persisted_next,
        )
        _last_seq_by_session[_memory_session_key(stable, db)] = next_seq
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
    activity_id = str(getattr(run_context, "activity_id", "") or "").strip()
    activity_kind = str(getattr(run_context, "activity_kind", "") or "").strip()
    existing_conversation_session_id = str(
        frame.get("conversation_session_id")
        or payload.get("conversation_session_id")
        or payload.get("conversationSessionId")
        or payload.get("session_key")
        or ""
    ).strip()
    # Mission node activity raw runtime events are owned by the node runtime
    # session. The visible team conversation consumes the canonical
    # team_mission.runtime.event projection and final summary only.
    if activity_kind == "mission" and activity_id.startswith("act-node:"):
        if existing_conversation_session_id:
            frame["conversation_session_id"] = existing_conversation_session_id
    else:
        frame["conversation_session_id"] = run_context.conversation_session_id
    if not str(frame.get("participant_id") or "").strip():
        frame["participant_id"] = run_context.participant_id
    # ADR-0001: surface activity_id at frame top-level so append_run_event
    # picks it up via _event_activity_id() and writes it into the column.
    if activity_id and not str(frame.get("activity_id") or "").strip():
        frame["activity_id"] = activity_id
    payload["run_context"] = run_context.to_payload()
    return frame


def _parse_run_context(value: Any) -> "RunContext | None":
    if not value:
        return None
    try:
        from hermes_team_mission.domain.run_context import RunContext

        return RunContext.from_payload(value)
    except Exception:
        return None


def _run_context_from_frame(frame: dict[str, Any], run_context: "RunContext | None") -> "RunContext | None":
    if run_context is not None:
        return run_context
    payload = frame.get("payload") if isinstance(frame.get("payload"), dict) else {}
    existing = payload.get("run_context") if isinstance(payload.get("run_context"), dict) else None
    return (
        _parse_run_context(existing)
        or _parse_run_context(frame.get("run_context_json") or frame.get("runContextJson"))
        or _parse_run_context(payload.get("run_context_json") or payload.get("runContextJson"))
    )


def _participant_id_from_scope(scope: str) -> str:
    normalized = str(scope or "").strip()
    if not normalized:
        return ""
    if normalized.startswith("member-chat:"):
        member_id = normalized.rsplit(":", 1)[-1].strip()
        if not member_id:
            return ""
        return member_id if member_id.startswith("member:") else member_participant_id(member_id)
    if normalized.startswith("team:"):
        parts = [part.strip() for part in normalized.split(":") if part.strip()]
        if len(parts) >= 2 and any("leader" in part for part in parts[2:]):
            try:
                return leader_participant_id(parts[1])
            except ValueError:
                return ""
    if normalized.startswith("profile:"):
        profile_id = normalized.split("profile:", 1)[1].strip()
        return agent_participant_id(profile_id) if profile_id else ""
    return ""


def _participant_id_from_user(payload: dict[str, Any], frame: dict[str, Any]) -> str:
    role = str(frame.get("role") or payload.get("role") or "").strip().lower()
    if role != "user":
        return ""
    user_id = str(
        frame.get("user_id")
        or frame.get("userId")
        or payload.get("user_id")
        or payload.get("userId")
        or frame.get("created_by_user_id")
        or frame.get("createdByUserId")
        or payload.get("created_by_user_id")
        or payload.get("createdByUserId")
        or ""
    ).strip()
    return f"user:{user_id}" if user_id else "user"


def _stamp_participant_id(
    frame: dict[str, Any],
    *,
    stable: str = "",
    event_type: str = "",
    run_id: str = "",
    turn_id: str = "",
    db: Any = None,
    run_context: "RunContext | None" = None,
) -> str:
    payload = frame.get("payload") if isinstance(frame.get("payload"), dict) else {}
    context = _run_context_from_frame(frame, run_context)
    # Worker stamps participant_id directly via worker_publish_bridge (H6-v2);
    # RunContext/DB/scope fallback below is legacy/back-compat only.
    frame_pid = str(frame.get("participant_id") or frame.get("participantId") or "").strip()
    payload_pid = str(payload.get("participant_id") or payload.get("participantId") or "").strip()
    ctx_pid = str(context.participant_id if context is not None else "" or "").strip()
    participant_id = frame_pid or payload_pid or ctx_pid
    scope_hint = (
        str(payload.get("runtime_scope_key") or payload.get("runtimeScopeKey") or "").strip()
        or str(frame.get("runtime_scope_key") or frame.get("runtimeScopeKey") or "").strip()
        or (context.execution_scope_key if context is not None else "")
    )
    team_identity = payload.get("team_mission") if isinstance(payload.get("team_mission"), dict) else {}
    member_hint = (
        str(payload.get("member_id") or payload.get("memberId") or "").strip()
        or str(frame.get("member_id") or frame.get("memberId") or "").strip()
        or str(team_identity.get("member_id") or team_identity.get("memberId") or "").strip()
    )
    profile_hint = (
        str(payload.get("agent_profile_id") or payload.get("agentProfileId") or "").strip()
        or str(frame.get("agent_profile_id") or frame.get("agentProfileId") or "").strip()
        or str(team_identity.get("agent_profile_id") or team_identity.get("agentProfileId") or "").strip()
    )
    participant_cache_key = (
        _memory_run_key(run_id, db),
        scope_hint,
        member_hint,
        profile_hint,
        str(frame.get("role") or payload.get("role") or "").strip().lower(),
    )
    with _lock:
        participant_cached = bool(run_id and participant_cache_key in _participant_id_by_resolution_key)
        if not participant_id and participant_cached:
            participant_id = _participant_id_by_resolution_key[participant_cache_key]
    if stable and not participant_id and not participant_cached and (scope_hint or member_hint or profile_hint):
        resolver_failed = False
        if db is not None:
            try:
                participant_id = str(
                    db.participants.resolve_participant_id(
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
        if not participant_id:
            participant_id = _participant_id_from_scope(scope_hint)
        if not participant_id and member_hint:
            participant_id = member_hint if member_hint.startswith("member:") else member_participant_id(member_hint)
        if not participant_id and profile_hint:
            participant_id = agent_participant_id(profile_hint)
        if not participant_id and db is not None and not resolver_failed:
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
    if not participant_id:
        participant_id = _participant_id_from_user(payload, frame)
    if run_id:
        with _lock:
            _participant_id_by_resolution_key[participant_cache_key] = participant_id
    if not participant_id:
        return ""
    frame["participant_id"] = participant_id
    frame["participantId"] = participant_id
    payload = frame.get("payload") if isinstance(frame.get("payload"), dict) else {}
    payload["participant_id"] = participant_id
    frame["payload"] = payload
    return participant_id


def _persist_stream_checkpoints(
    *,
    conversation_session_id: str,
    run_id: str,
    boundary_event_type: str,
    db: Any,
) -> None:
    if boundary_event_type not in _STREAM_CHECKPOINT_BOUNDARY_TYPES:
        return
    excluded = {"message.delta"} if boundary_event_type == "message.complete" else set()
    pending = _runtime_streams.pending_checkpoints(
        conversation_session_id,
        run_id=run_id,
        exclude_event_types=excluded,
        db=db,
    )
    _persist_checkpoint_entries(pending, db=db)


def _persist_checkpoint_entries(
    pending: list[_runtime_streams.PendingCheckpoint],
    *,
    db: Any,
) -> None:
    completed: list[_runtime_streams.PendingCheckpoint] = []
    for checkpoint in pending:
        record_event(
            checkpoint.frame,
            db=db,
            persist=True,
            _flush_streams=False,
        )
        completed.append(checkpoint)
    _runtime_streams.mark_checkpointed(completed)


def record_event(
    params: dict[str, Any],
    owner_transport: Transport | None = None,
    skip_owner_transport: bool = False,
    db: Any = None,
    persist: bool = True,
    run_context: "RunContext | None" = None,
    _flush_streams: bool = True,
) -> list[Transport]:
    """Persist an event frame and return live subscriber transports to notify.

    TODO(PR-6 §4.4): ``clarify.request`` is currently double-written to
    ``run_events`` (same ``request_id`` produces two seqs). The dual
    publish originates in ``tools/clarify_gateway.py`` +
    ``tui_gateway/services/worker_publish_bridge.py`` +
    ``tui_gateway/services/worker_frame_router.py``, all of which are
    blacklisted for this PR. Converging to a single publish is deferred
    to a follow-up that can touch those files.
    """
    # R1: single-writer invariant. In a worker process this call is only
    # allowed to run the subscriber-fanout half; persistence of run_events
    # is the main sidecar's exclusive job (it re-invokes record_event with
    # persist=True + RunContext after ingesting the worker's stdout frame,
    # via WorkerFrameRouter._publish_event_with_db). If we let workers
    # persist here, Phase 8b's unified state.db turns any raw event into
    # two rows (one per writer) with divergent RunContext/activity/
    # participant stamping — which is exactly the "team leader tool card
    # loses its upper half" bug. See tui_gateway/process_role.py. Ignore
    # whatever the caller passed for persist — this is an architectural
    # invariant, not a caller-configurable knob.
    from tui_gateway.process_role import is_worker_process
    worker_process = is_worker_process()
    if worker_process:
        persist = False
    else:
        _stamp_session_identity(params)
        # PR-1 identity contract: enforced on the caller's dict (not just
        # our private copy) because publish_recorded_event delivers that
        # same dict to transports after this call returns. Worker-side
        # fanout never reaches FE subscribers, and worker frames re-enter
        # here on the main side, so main-only enforcement covers all
        # outbound frames without double-synthesis across processes.
        _ensure_outbound_run_identity(params)
    frame = _apply_run_context_to_frame(dict(params), run_context)
    _stamp_session_identity(frame)
    payload = frame.get("payload") if isinstance(frame.get("payload"), dict) else {}
    stable = _conversation_session_id(frame)
    run_id = _event_run_id(frame)
    turn_id = _event_turn_id(frame)
    frame = _normalize_team_mission_deliverable_terminal_event(frame, run_id=run_id, db=db)
    payload = frame.get("payload") if isinstance(frame.get("payload"), dict) else {}
    event_type = str(frame.get("type") or "").strip()
    execution_session_id = str(frame.get("execution_session_id") or "").strip()
    owner_metadata = frame.get("owner_metadata")
    owner_metadata = owner_metadata if isinstance(owner_metadata, dict) else {}
    now = time.time()
    frame["timestamp"] = now
    # CR-P0.2: persisted/published runtime events carry the first-class
    # Participant speaker id. CR-P1 will make this the frontend's only
    # authoritative speaker key instead of node/member/profile fallbacks.
    participant_id = _stamp_participant_id(
        frame,
        stable=stable,
        event_type=event_type,
        run_id=run_id,
        turn_id=turn_id,
        db=db,
        run_context=run_context,
    )
    payload = frame.get("payload") if isinstance(frame.get("payload"), dict) else {}
    terminal_event = _terminal_status(event_type, payload)
    _runtime_event_protocol.stamp_runtime_source_seq(
        frame,
        params,
        assign_if_missing=not worker_process,
    )
    team_mission_binding = (
        _team_mission_run_binding(frame, db=db)
        if not worker_process and run_id and db is not None
        else {}
    )
    transient_stream = bool(
        not worker_process and _runtime_streams.is_transient_stream_event(frame)
    )
    if transient_stream:
        persist = False
        _runtime_event_protocol.mark_transient(frame, params)
        _runtime_streams.observe(
            frame,
            db=db,
            checkpoint_required=not bool(team_mission_binding),
        )
    elif _flush_streams and not worker_process and stable and run_id:
        _persist_stream_checkpoints(
            conversation_session_id=stable,
            run_id=run_id,
            boundary_event_type=event_type,
            db=db,
        )
    synthetic_run_identity = bool(frame.get("synthetic_run_id"))
    # PR-1 transient contract: a frame that will never gain a canonical
    # run_events.seq must say so explicitly — the FE ledger only admits
    # canonical seqs, transient frames may only patch open segments.
    # Worker processes skip the stamp: their frames re-enter record_event
    # on the main side, which is the authority on whether they persist.
    will_persist = bool(
        persist and stable and _run_method(db, "append_event") is not None
    )
    if not worker_process and not will_persist and frame.get("transient") is not False:
        _runtime_event_protocol.mark_transient(frame, params)
    canonical_team_activity_append_required = bool(
        not worker_process
        and db is not None
        and team_mission_binding
        and not will_persist
    )
    scheduler_mission_id = ""
    persisted_run_checked = False
    persisted_terminal_reopen = False
    with _lock:
        cached_run_state = _run_state_by_id.get(_memory_run_key(run_id, db)) if run_id else None
        if cached_run_state is None and run_id:
            cached_run_state = _run_state_by_id.get(run_id)
    if (
        stable
        and run_id
        and not synthetic_run_identity
        and terminal_event is None
        and _event_opens_active_run(event_type)
    ):
        if cached_run_state is None and (getter := _run_method(db, "get")):
            persisted_run_checked = True
            try:
                persisted_run = getter(run_id)
            except Exception:
                persisted_run = None
            if isinstance(persisted_run, dict):
                persisted_stable = str(
                    persisted_run.get("session_id")
                    or persisted_run.get("conversation_session_id")
                    or ""
                ).strip()
                persisted_terminal_reopen = bool(
                    str(persisted_run.get("status") or "") in TERMINAL_RUN_STATUSES
                    and (not persisted_stable or persisted_stable == stable)
                )

    with _lock:
        if stable and not transient_stream:
            memory_session_key = _memory_session_key(stable, db)
            _events_by_session[memory_session_key].append(frame)
        if stable:
            memory_session_key = _memory_session_key(stable, db)
            _last_seq_by_session[memory_session_key] = max(
                int(_last_seq_by_session.get(memory_session_key) or 0),
                int(frame.get("seq") or 0),
            )
        if stable and run_id and not synthetic_run_identity:
            existing_state = _run_state_by_id.get(_memory_run_key(run_id, db))
            if existing_state is None:
                existing_state = _run_state_by_id.get(run_id)
            existing_state_stable = str((existing_state or {}).get("conversation_session_id") or "").strip()
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
                        _events_by_session[_memory_session_key(stable, db)].remove(frame)
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
                    conversation_session_id=stable,
                    run_id=run_id,
                    runtime_scope_key=str(
                        (payload or {}).get("runtime_scope_key")
                        or frame.get("runtime_scope_key")
                        or ""
                    ),
                    turn_id=turn_id,
                    execution_session_id=execution_session_id,
                    db=db,
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
        # Team Mission frontend delivery has one owner: the canonical activity
        # journal listener.  Do not also publish the source execution row to
        # session subscribers; the journal projection below is durable and is
        # used identically for live delivery, reconnect, and replay.
        if stable and not team_mission_binding:
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
        activity_id = _event_activity_id(frame)
        skip_raw_team_mission_activity_delivery = bool(
            activity_id
            and _team_activity_events.is_activity_id(activity_id)
            and _event_has_team_mission_run_binding(frame, db=db)
        )
        if activity_id and not skip_raw_team_mission_activity_delivery:
            for subscription_id in list(_subscription_ids_by_activity.get(activity_id, set())):
                subscription = _subscriptions_by_id.get(subscription_id)
                transport = subscription.get("transport") if isinstance(subscription, dict) else None
                if transport is not None and isinstance(subscription, dict):
                    _remember_subscription_run(subscription, frame)
                    subscribers.add(transport)
        if skip_owner_transport and owner_transport is not None:
            subscribers.discard(owner_transport)
        result = list(subscribers)
        if event_type in _STREAM_TRACE_EVENT_TYPES:
            _trace_stream_route(
                "run-control-record",
                event_type=event_type,
                session_id=execution_session_id,
                conversation_session_id=stable,
                run_id=run_id,
                turn_id=turn_id,
                runtime_scope_key=str(frame.get("runtime_scope_key") or ""),
                seq=int(frame.get("seq") or 0),
                owner_transport=_transport_debug_id(owner_transport),
                subscriber_delivery_count=len(result),
                **_stream_trace_summary(frame),
            )
    if canonical_team_activity_append_required:
        # Persist-before-publish is the Team Mission activity invariant.  Raw
        # execution deltas stay transient in their node session, but the
        # canvas-visible projection is appended to the dedicated mission
        # activity journal first.  The run-event append listener is then the
        # only publisher for both live delivery and replay; no transient frame
        # may bypass that journal.
        mission_event_appender = _db_method(db, "append_team_mission_event_for_run")
        if mission_event_appender is None:
            logger.error(
                "[dovie-run-control] canonical-team-activity-append-unavailable %s",
                _json_for_log({
                    "event_type": event_type,
                    "session_id": stable,
                    "run_id": run_id,
                    "mission_id": str(team_mission_binding.get("mission_id") or ""),
                }),
            )
        else:
            try:
                mission_event_appender(run_id=run_id, event=frame)
            except Exception as exc:
                # Fail closed: publishing an unjournaled fragment would
                # reintroduce the second cursor domain and make replay differ
                # from what the user saw live.
                logger.error(
                    "[dovie-run-control] canonical-team-activity-append-failed %s",
                    _json_for_log({
                        "event_type": event_type,
                        "session_id": stable,
                        "run_id": run_id,
                        "mission_id": str(team_mission_binding.get("mission_id") or ""),
                        "error": str(exc),
                    }),
                    exc_info=True,
                )
    if persist and stable and (method := _run_method(db, "append_event")):
        prev_projecting = getattr(db, "_team_mission_projecting", False)
        try:
            # record_event performs the team mission event-domain reduce + mirror
            # below, so suppress append_run_event's write-time mission-event hook
            # for this call and for the mirror's nested append_run_event.
            setattr(db, "_team_mission_projecting", True)
            saved = method(stable, frame, participant_id=participant_id)
            # PR-1 seq write-back: persistence assigns the canonical seq
            # BEFORE any transport sees the frame (delivery happens in
            # publish_recorded_event after this function returns, and the
            # poller reads from the DB). Sync it into the outbound dicts
            # so what FE receives is byte-identical to what replay serves.
            _sync_canonical_frame_identity(
                saved,
                frame=frame,
                params=params,
                stable=stable,
                run_id=run_id,
                event_type=event_type,
                db=db,
            )
            if (
                isinstance(saved, dict)
                and saved.get("_persistence_disposition") in {
                    "duplicate_terminal",
                    "ignored_after_terminal",
                    "duplicate_session_info",
                }
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
                        _events_by_session[_memory_session_key(stable, db)].remove(frame)
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
                    execution_session_id=execution_session_id,
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
                    except Exception as _mission_append_exc:
                        # S9: was silent ``candidate_event = {}`` with no log.
                        # Emit ERROR so the Team Mission event-domain append
                        # failure is visible and counted.
                        logger.error(
                            "[dovie-run-control] team-mission-event-append-failed "
                            "%s",
                            _json_for_log({
                                "event_type": event_type,
                                "session_id": stable,
                                "run_id": run_id,
                                "error": str(_mission_append_exc),
                            }),
                            exc_info=True,
                        )
                        candidate_event = {}
                    if (
                        isinstance(candidate_event, dict)
                        and candidate_event
                        and not candidate_event.get("_persistence_disposition")
                    ):
                        mission_event = candidate_event
                    _trace_team_runtime_chain(
                        "record-event-projection",
                        event_type=event_type,
                        session_id=stable,
                        execution_session_id=execution_session_id,
                        run_id=run_id,
                        turn_id=turn_id,
                        runtime_scope_key=str(frame.get("runtime_scope_key") or ""),
                        seq=int(frame.get("seq") or 0),
                        projection_created=bool(mission_event),
                        projection_disposition=(
                            candidate_event.get("_persistence_disposition", "inserted")
                            if isinstance(candidate_event, dict) and candidate_event
                            else "none"
                        ),
                        projection_seq=(
                            candidate_event.get("seq")
                            if isinstance(candidate_event, dict)
                            else None
                        ),
                        projection_type=(
                            candidate_event.get("type")
                            if isinstance(candidate_event, dict)
                            else ""
                        ),
                    )
                reduced_node = reducer(run_id=run_id, event=event_for_reduce)
                scheduler_mission_id = ""
                binding: dict[str, Any] = {}
                if isinstance(reduced_node, dict):
                    scheduler_mission_id = str(reduced_node.get("mission_id") or "").strip()
                binding_getter = _db_method(db, "get_team_mission_run_binding")
                if binding_getter is not None:
                    try:
                        candidate_binding = binding_getter(run_id)
                    except Exception:
                        candidate_binding = None
                    binding = dict(candidate_binding) if isinstance(candidate_binding, dict) else {}
                    if not scheduler_mission_id:
                        scheduler_mission_id = str(binding.get("mission_id") or "").strip()
                if scheduler_mission_id:
                    if (
                        mission_event
                        and _team_mission_runtime_event_allows_conversation_status(
                            event_type=event_type,
                            binding=binding,
                            db=db,
                        )
                        and (status_appender := _db_method(db, "append_team_mission_conversation_status_event"))
                    ):
                        try:
                            status_appender(
                                mission_id=scheduler_mission_id,
                                source_event=event_for_reduce,
                                source_mission_seq=int(mission_event.get("seq") or 0),
                            )
                        except Exception as _status_exc:
                            # S9: was bare ``pass`` — silent degradation. Emit
                            # ERROR with context so the mirror/reduce failure is
                            # visible in logs and counted by the metrics pipeline.
                            logger.error(
                                "[dovie-run-control] team-mission-conversation-status-append-failed "
                                "%s",
                                _json_for_log({
                                    "event_type": event_type,
                                    "session_id": stable,
                                    "run_id": run_id,
                                    "mission_id": scheduler_mission_id,
                                    "mission_seq": int(mission_event.get("seq") or 0) if isinstance(mission_event, dict) else 0,
                                    "error": str(_status_exc),
                                }),
                                exc_info=True,
                            )
                    if event_type == "message.complete":
                        try:
                            from hermes_team_mission.runtime.team_transcript_writer import (
                                _append_leader_report_ready_event,
                                leader_report_ready_context_for_run,
                            )

                            projected_message_id = ""
                            if isinstance(saved, dict):
                                projected_message_id = str(saved.get("_projected_message_id") or "").strip()
                            report_ready = leader_report_ready_context_for_run(
                                db,
                                run_id=run_id,
                                projected_message_id=projected_message_id,
                            )
                            if report_ready:
                                _append_leader_report_ready_event(
                                    db,
                                    mission_id=str(report_ready.get("mission_id") or report_ready.get("missionId") or ""),
                                    run_id=str(report_ready.get("run_id") or report_ready.get("runId") or run_id or ""),
                                    conversation_message_id=str(
                                        report_ready.get("leader_report_message_id")
                                        or report_ready.get("leaderReportMessageId")
                                        or ""
                                    ),
                                )
                        except Exception as _report_ready_exc:
                            # S9: was logger.debug — silent degradation.
                            # Emit ERROR so the report-ready append failure is
                            # visible and counted.
                            logger.error(
                                "[dovie-run-control] team-mission-report-ready-append-failed "
                                "%s",
                                _json_for_log({
                                    "event_type": event_type,
                                    "session_id": stable,
                                    "run_id": run_id,
                                    "error": str(_report_ready_exc),
                                }),
                                exc_info=True,
                            )
        except Exception as exc:
            # Persist failed → the frame has no canonical seq. Mark it
            # transient so the FE ledger drops it instead of admitting a
            # seq that replay/hydration will never serve (I10: degrade
            # loudly, never corrupt the ledger).
            frame["transient"] = True
            if isinstance(params, dict):
                params["transient"] = True
            _diagnostic_warning(
                "run-event-persist-failed",
                db=_db_label(db),
                event_type=event_type,
                terminal_status=terminal_event,
                session_id=stable,
                run_id=run_id,
                turn_id=turn_id,
                runtime_scope_key=str(frame.get("runtime_scope_key") or ""),
                execution_session_id=execution_session_id,
                seq=int(frame.get("seq") or 0),
                error=str(exc),
            )
            logger.warning("failed to persist run event", exc_info=True)
        finally:
            try:
                setattr(db, "_team_mission_projecting", prev_projecting)
            except Exception as _restore_exc:
                # S9: was bare ``pass``. This is cleanup (restoring the
                # projection flag) not a data path, so DEBUG is appropriate —
                # but no longer fully silent.
                logger.debug(
                    "[dovie-run-control] team-mission-projecting-flag-restore-failed "
                    "%s",
                    _json_for_log({
                        "event_type": event_type,
                        "session_id": stable,
                        "run_id": run_id,
                        "error": str(_restore_exc),
                    }),
                    exc_info=True,
                )
    elif terminal_event:
        # S1: split the old single "terminal-event-not-persisted-no-db-method"
        # label into two semantically distinct paths.
        #   * Worker side (persist forced False by R1 invariant): the event
        #     is intentionally NOT persisted here — the main sidecar will
        #     persist it after ingesting the worker's stdout frame. This is
        #     benign design noise, so emit DEBUG (not WARNING) and do NOT
        #     route it to the error metrics pipeline.
        #   * Main side (db method missing): genuine data loss — the
        #     terminal event has no canonical seq and will never appear in
        #     replay/hydration. Emit ERROR + a diagnostic so the metrics
        #     pipeline counts it.
        if worker_process:
            logger.debug(
                "[dovie-run-control] terminal-event-persist-deferred-to-main "
                "%s",
                _json_for_log({
                    "event_type": event_type,
                    "terminal_status": terminal_event,
                    "session_id": stable,
                    "run_id": run_id,
                    "turn_id": turn_id,
                    "runtime_scope_key": str(frame.get("runtime_scope_key") or ""),
                    "execution_session_id": execution_session_id,
                    "seq": int(frame.get("seq") or 0),
                }),
            )
        elif not persist and db is not None:
            logger.debug(
                "[dovie-run-control] terminal-event-broadcast-without-repersist %s",
                _json_for_log({
                    "db": _db_label(db),
                    "event_type": event_type,
                    "terminal_status": terminal_event,
                    "session_id": stable,
                    "run_id": run_id,
                    "turn_id": turn_id,
                    "runtime_scope_key": str(frame.get("runtime_scope_key") or ""),
                    "execution_session_id": execution_session_id,
                    "seq": int(frame.get("seq") or 0),
                }),
            )
        else:
            logger.error(
                "[dovie-run-control] terminal-event-dropped-no-db "
                "%s",
                _json_for_log({
                    "db": _db_label(db),
                    "event_type": event_type,
                    "terminal_status": terminal_event,
                    "session_id": stable,
                    "run_id": run_id,
                    "turn_id": turn_id,
                    "runtime_scope_key": str(frame.get("runtime_scope_key") or ""),
                    "execution_session_id": execution_session_id,
                    "seq": int(frame.get("seq") or 0),
                }),
            )
    if terminal_event and scheduler_mission_id:
        _dispatch_team_mission_ready_scheduler(
            mission_id=scheduler_mission_id,
            db=db,
            trigger_event=event_type,
            run_id=run_id,
        )
    if terminal_event and stable and run_id:
        _runtime_streams.clear_run(stable, run_id, db=db)
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
    _stamp_session_identity(publish_params)
    _stamp_participant_id(
        publish_params,
        stable=_conversation_session_id(publish_params),
        event_type=str(publish_params.get("type") or "").strip(),
        run_id=_event_run_id(publish_params),
        turn_id=_event_turn_id(publish_params),
        db=db,
        run_context=run_context,
    )
    subscribers = record_event(
        publish_params,
        owner_transport=owner_transport,
        skip_owner_transport=skip_owner_transport,
        db=db,
        persist=persist,
        run_context=run_context,
    )
    # The caller may also own a direct-delivery path. Propagate canonical seq,
    # transient classification, and normalized identity back to that frame so
    # direct and subscription transports observe the same envelope.
    params.clear()
    params.update(publish_params)
    if before_deliver is not None:
        before_deliver()
    delivered: list[Transport] = []
    for transport in subscribers:
        if _deliver_live_subscription_event(transport, publish_params):
            delivered.append(transport)
    return delivered


def terminate_run(
    *,
    conversation_session_id: str,
    run_id: str,
    turn_id: str = "",
    runtime_scope_key: str = "",
    execution_session_id: str = "",
    activity_id: str = "",
    status: str = "failed",
    message: str = "",
    cause: str = "worker_emitted",
    db: Any = None,
    owner_transport: Transport | None = None,
) -> dict[str, Any]:
    from tui_gateway.process_role import is_worker_process

    if is_worker_process():
        raise WorkerTerminalOwnershipError(
            "worker processes must emit RunTerminalFrame; "
            "the main process owns terminal persistence and delivery"
        )
    stable = str(conversation_session_id or execution_session_id or "").strip()
    normalized_run_id = str(run_id or "").strip()
    if not stable or not normalized_run_id:
        return {}
    terminal_status = str(status or "failed").strip().lower() or "failed"
    payload_status = _payload_status(terminal_status)

    # spec §7.2 — RunStateMachine.terminate_run single entrypoint. Perform the
    # atomic terminal transition (idempotent, degrades on SeqAllocatorBusy)
    # BEFORE publishing the notification frame so that subscribers observe the
    # committed state. If db is unavailable in isolated test paths, the caller
    # still gets a notification frame without persistence.
    atomic_result = None
    if db is not None:
        try:
            atomic_result = db.runs.terminate(
                run_id=normalized_run_id,
                session_id=stable,
                target_status=terminal_status,
                cause=str(cause or "worker_emitted"),
                turn_id=str(turn_id or "").strip(),
                activity_id=str(activity_id or "").strip(),
                message=message,
                runtime_scope_key=runtime_scope_key or stable,
                execution_session_id=execution_session_id,
                payload_extra={
                    "activity_id": str(activity_id or "").strip(),
                    "activityId": str(activity_id or "").strip(),
                } if str(activity_id or "").strip() else None,
            )
        except ValueError:
            logger.exception(
                "run_state_machine.terminate_run rejected run=%s session=%s status=%s",
                normalized_run_id,
                stable,
                terminal_status,
            )
            raise
        except Exception as exc:
            logger.exception(
                "run_state_machine.terminate_run failed run=%s session=%s status=%s",
                normalized_run_id,
                stable,
                terminal_status,
            )
            raise RuntimeError(
                "run_state_machine.terminate_run failed; refusing legacy terminal fallback"
            ) from exc

    payload: dict[str, Any] = {
        "run_id": normalized_run_id,
        "turn_id": str(turn_id or "").strip(),
        "status": payload_status,
    }
    normalized_activity_id = str(activity_id or "").strip()
    if normalized_activity_id:
        payload["activity_id"] = normalized_activity_id
        payload["activityId"] = normalized_activity_id
    if message:
        payload["message"] = str(message)
        payload["text"] = str(message) if payload_status == "error" else ""
    else:
        payload["text"] = ""
    if atomic_result is not None:
        payload["terminal_seq"] = atomic_result.terminal_seq
        payload["terminal_cause"] = atomic_result.cause.value
        if atomic_result.degraded:
            payload["terminal_degraded"] = True
    frame = {
        "type": "message.complete",
        "session_id": str(execution_session_id or stable).strip(),
        "conversation_session_id": stable,
        "run_id": normalized_run_id,
        "turn_id": str(turn_id or "").strip(),
        "runtime_scope_key": str(runtime_scope_key or stable).strip(),
        **({"activity_id": normalized_activity_id, "activityId": normalized_activity_id} if normalized_activity_id else {}),
        "owner_metadata": {
            "gateway_pid": os.getpid(),
        },
        "payload": payload,
    }
    if atomic_result is not None and atomic_result.terminal_seq:
        frame["seq"] = atomic_result.terminal_seq
        frame["transient"] = False
    _diagnostic_warning(
        "publish-terminal-event",
        db=_db_label(db),
        status=terminal_status,
        payload_status=payload_status,
        session_id=stable,
        run_id=normalized_run_id,
        turn_id=str(turn_id or "").strip(),
        runtime_scope_key=str(runtime_scope_key or stable).strip(),
        execution_session_id=str(execution_session_id or stable).strip(),
        seq=0,
        message=str(message or ""),
    )
    # spec §7.2 — when the domain-level atomic transition succeeded (APPLIED
    # or IDEMPOTENT_SKIP or DEGRADED), the canonical event is already persisted
    # (or intentionally skipped for DEGRADED). Pass persist=False to avoid a
    # second run_events INSERT that would silently duplicate or clash on the
    # UNIQUE(session_id, seq) constraint.
    persist_flag = atomic_result is None
    publish_recorded_event(
        frame,
        owner_transport=owner_transport,
        db=db,
        persist=persist_flag,
    )
    return frame


def publish_run_terminal_event(
    *,
    conversation_session_id: str,
    run_id: str,
    turn_id: str = "",
    runtime_scope_key: str = "",
    execution_session_id: str = "",
    activity_id: str = "",
    status: str = "failed",
    message: str = "",
    db: Any = None,
    owner_transport: Transport | None = None,
) -> dict[str, Any]:
    """Compatibility wrapper for the Phase C terminal entrypoint."""
    return terminate_run(
        conversation_session_id=conversation_session_id,
        run_id=run_id,
        turn_id=turn_id,
        runtime_scope_key=runtime_scope_key,
        execution_session_id=execution_session_id,
        activity_id=activity_id,
        status=status,
        message=message,
        db=db,
        owner_transport=owner_transport,
    )


def subscribe_session(
    *,
    conversation_session_id: str,
    transport: Transport | None,
    after_seq: int = 0,
    active_only: bool = False,
    runtime_scope_key: str = "",
    run_id: str = "",
    limit: int = _MAX_EVENTS_PER_SESSION,
    db: Any = None,
) -> list[dict[str, Any]]:
    _subscription_id, events = subscribe_session_with_id(
        conversation_session_id=conversation_session_id,
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
        sid = str(subscription.get("conversation_session_id") or "")
        sub_transport = subscription.get("transport")
        if sid:
            _subscription_ids_by_session.get(sid, set()).discard(sub_id)
        activity_id = str(subscription.get("activity_id") or "")
        if activity_id:
            _subscription_ids_by_activity.get(activity_id, set()).discard(sub_id)
        if sub_transport is not None:
            _subscription_ids_by_transport.get(sub_transport, set()).discard(sub_id)
    return removed


def subscribe_activity(
    *,
    activity_id: str,
    transport: Transport | None,
    after_seq: int = 0,
    limit: int = 2000,
    replay_mode: str = "replay_live",
    max_replay_events: int | None = None,
    db: Any = None,
) -> tuple[str, list[dict[str, Any]]]:
    """Register an activity-scoped subscription. Returns (subscription_id, replay_events)."""
    normalized_activity_id = str(activity_id or "").strip()
    if not normalized_activity_id:
        return "", []
    try:
        bounded_limit = max(1, min(int(limit or 2000), 2000))
    except (TypeError, ValueError):
        bounded_limit = 2000
    try:
        normalized_after_seq = max(0, int(after_seq or 0))
    except (TypeError, ValueError):
        normalized_after_seq = 0
    normalized_replay_mode = str(replay_mode or "replay_live").strip().lower().replace("-", "_")
    if normalized_replay_mode in {"", "default", "replay"}:
        normalized_replay_mode = "replay_live"
    if normalized_replay_mode not in {"replay_live", "live", "cursor_only"}:
        normalized_replay_mode = "replay_live"
    try:
        normalized_max_replay_events = int(max_replay_events) if max_replay_events is not None else bounded_limit
    except (TypeError, ValueError):
        normalized_max_replay_events = bounded_limit
    normalized_max_replay_events = max(0, min(normalized_max_replay_events, bounded_limit))

    normalized_subscription_id = uuid.uuid4().hex
    is_team_mission_activity = _team_activity_events.is_activity_id(normalized_activity_id)
    uses_mission_activity_journal = _team_activity_events.uses_mission_activity_journal(
        normalized_activity_id,
        db=db,
    )
    is_team_dispatch_activity = _team_activity_events.is_team_dispatch_activity_id(normalized_activity_id)
    terminal_team_mission_activity = (
        uses_mission_activity_journal
        and _team_activity_events.is_terminal_activity(normalized_activity_id, db=db)
    )
    force_cursor_only = (
        normalized_replay_mode in {"live", "cursor_only"}
        or normalized_max_replay_events <= 0
    )
    cursor_seq = max(
        normalized_after_seq,
        _team_activity_events.activity_last_seq(normalized_activity_id, db=db) if force_cursor_only else 0,
    )
    raw_initial_seq = (
        0
        if is_team_dispatch_activity
        else cursor_seq if force_cursor_only and not uses_mission_activity_journal else normalized_after_seq
    )
    activity_event_initial_seq = (
        0
        if is_team_dispatch_activity
        else cursor_seq if force_cursor_only and uses_mission_activity_journal
        else normalized_after_seq if uses_mission_activity_journal else 0
    )
    replay_after_seq = activity_event_initial_seq if uses_mission_activity_journal else normalized_after_seq
    replay_limit = 0 if force_cursor_only else normalized_max_replay_events
    _team_activity_terminal_log(
        "subscribe-start",
        activity_id=normalized_activity_id,
        after_seq=normalized_after_seq,
        limit=bounded_limit,
        replay_mode=normalized_replay_mode,
        max_replay_events=normalized_max_replay_events,
        force_cursor_only=force_cursor_only,
        cursor_seq=cursor_seq,
        terminal_team_mission_activity=terminal_team_mission_activity,
        is_team_mission_activity=is_team_mission_activity,
        uses_mission_activity_journal=uses_mission_activity_journal,
        mission_id=_team_activity_events.mission_id_for_activity(normalized_activity_id, db=db),
        node_selector=_team_activity_events.node_selector(normalized_activity_id),
        has_transport=transport is not None,
        db=_db_label(db),
    )
    _trace_team_runtime_chain(
        "subscribe-activity",
        activity_id=normalized_activity_id,
        after_seq=normalized_after_seq,
        limit=bounded_limit,
        replay_mode=normalized_replay_mode,
        max_replay_events=normalized_max_replay_events,
        force_cursor_only=force_cursor_only,
        cursor_seq=cursor_seq,
        terminal_team_mission_activity=terminal_team_mission_activity,
        is_team_mission_activity=is_team_mission_activity,
        uses_mission_activity_journal=uses_mission_activity_journal,
        mission_id=_team_activity_events.mission_id_for_activity(normalized_activity_id, db=db),
        node_selector=_team_activity_events.node_selector(normalized_activity_id),
        has_transport=transport is not None,
    )
    listener_registered = (
        _ensure_run_event_listener_registered(db)
        if is_team_mission_activity
        else False
    )
    with _lock:
        duplicate_subscription_count = 0
        if transport is not None:
            duplicate_subscription_ids = {
                sub_id
                for sub_id in _subscription_ids_by_transport.get(transport, set())
                if (
                    str((_subscriptions_by_id.get(sub_id) or {}).get("kind") or "") == "activity"
                    and str((_subscriptions_by_id.get(sub_id) or {}).get("activity_id") or "").strip()
                    == normalized_activity_id
                )
            }
            duplicate_subscription_count = len(duplicate_subscription_ids)
            _remove_subscription_ids_locked(duplicate_subscription_ids)
            _subscriptions_by_id[normalized_subscription_id] = {
                "id": normalized_subscription_id,
                "kind": "activity",
                "activity_id": normalized_activity_id,
                "conversation_session_id": "",
                "transport": transport,
                "active_only": False,
                "runtime_scope_key": "",
                "active_run_ids": set(),
                "last_seq": raw_initial_seq,
                "activity_event_last_seq": activity_event_initial_seq,
                "replay_mode": normalized_replay_mode,
                "cursor_only": force_cursor_only,
                "db": db,
                "created_at": time.time(),
            }
            _subscription_ids_by_activity[normalized_activity_id].add(normalized_subscription_id)
            _subscription_ids_by_transport[transport].add(normalized_subscription_id)
            if db is not None:
                _start_subscription_poller_locked()
        _team_activity_terminal_log(
            "subscribe-registered",
            activity_id=normalized_activity_id,
            subscription_id=normalized_subscription_id,
            duplicate_subscription_count=duplicate_subscription_count,
            listener_registered=listener_registered,
            active_subscription_count=len(_subscription_ids_by_activity.get(normalized_activity_id, set())),
            has_transport=transport is not None,
            poller_alive=bool(_subscription_poller_thread and _subscription_poller_thread.is_alive()),
        )

    events = [] if replay_limit <= 0 else _team_activity_events.list_activity_events(
        db,
        normalized_activity_id,
        after_seq=replay_after_seq,
        limit=replay_limit,
        event_activity_id=_event_activity_id,
    )
    events = [event for event in events if isinstance(event, dict)]
    events = [
        event
        for event in events
        if _event_activity_id(event) == normalized_activity_id
        and int(event.get("seq") or 0) > replay_after_seq
    ]
    _trace_team_runtime_chain(
        "subscribe-activity-replay",
        activity_id=normalized_activity_id,
        after_seq=replay_after_seq,
        requested_after_seq=normalized_after_seq,
        event_count=len(events),
        replay_limit=replay_limit,
        cursor_only=force_cursor_only,
        first_seq=int(events[0].get("seq") or 0) if events else 0,
        last_seq=int(events[-1].get("seq") or 0) if events else 0,
        event_types=[str(event.get("type") or "") for event in events[:12]],
    )
    _team_activity_terminal_log(
        "subscribe-replay",
        activity_id=normalized_activity_id,
        subscription_id=normalized_subscription_id,
        after_seq=replay_after_seq,
        requested_after_seq=normalized_after_seq,
        event_count=len(events),
        replay_limit=replay_limit,
        cursor_only=force_cursor_only,
        first_seq=int(events[0].get("seq") or 0) if events else 0,
        last_seq=int(events[-1].get("seq") or 0) if events else 0,
        event_types=[str(event.get("type") or "") for event in events[:12]],
    )
    with _lock:
        subscription = _subscriptions_by_id.get(normalized_subscription_id)
        if subscription is not None:
            if events:
                for event in events:
                    _remember_subscription_delivery(subscription, event)
            elif uses_mission_activity_journal:
                subscription["activity_event_last_seq"] = max(
                    int(subscription.get("activity_event_last_seq") or 0),
                    activity_event_initial_seq,
                )
            else:
                subscription["last_seq"] = max(
                    int(subscription.get("last_seq") or 0),
                    raw_initial_seq,
                )
    return normalized_subscription_id, events


def subscribe_session_with_id(
    *,
    conversation_session_id: str,
    transport: Transport | None,
    after_seq: int = 0,
    active_only: bool = False,
    runtime_scope_key: str = "",
    run_id: str = "",
    limit: int = _MAX_EVENTS_PER_SESSION,
    db: Any = None,
    subscription_id: str = "",
) -> tuple[str, list[dict[str, Any]]]:
    stable = str(conversation_session_id or "").strip()
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
                        and str((_subscriptions_by_id.get(sub_id) or {}).get("conversation_session_id") or "").strip() == stable
                    )
                )
            }
            _remove_subscription_ids_locked(duplicate_subscription_ids)
            _subscriptions_by_id[normalized_subscription_id] = {
                "id": normalized_subscription_id,
                "kind": "session",
                "conversation_session_id": stable,
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
            if db is not None:
                _start_subscription_poller_locked()
        memory_key = _memory_session_key(stable, db)
        memory_events = list(_events_by_session.get(memory_key, ()))
    events: list[dict[str, Any]] = []
    if db is not None:
        try:
            events = list_runtime_events(
                db,
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
    replay_snapshots = _runtime_streams.replay_snapshots(
        stable,
        run_id=normalized_run_id,
        runtime_scope_key=scope,
        active_run_ids=active_run_ids if active_only else None,
        db=db,
    )
    with _lock:
        subscription = _subscriptions_by_id.get(normalized_subscription_id)
        if subscription is not None:
            subscription["last_seq"] = _max_event_seq(events, after_seq)
    if after_seq <= 0:
        return normalized_subscription_id, [*events, *replay_snapshots]
    return normalized_subscription_id, [
        *[event for event in events if int(event.get("seq") or 0) > after_seq],
        *replay_snapshots,
    ]


def unsubscribe_activity(subscription_id: str) -> int:
    """Remove activity-scoped subscription. Returns 1 if removed, 0 if not found."""
    normalized_subscription_id = str(subscription_id or "").strip()
    if not normalized_subscription_id:
        return 0
    with _lock:
        subscription = _subscriptions_by_id.get(normalized_subscription_id)
        if not isinstance(subscription, dict):
            _team_activity_terminal_log(
                "unsubscribe-miss",
                subscription_id=normalized_subscription_id,
                reason="not_found",
            )
            return 0
        if str(subscription.get("kind") or "") != "activity":
            _team_activity_terminal_log(
                "unsubscribe-miss",
                subscription_id=normalized_subscription_id,
                reason="not_activity",
            )
            return 0
        activity_id = str(subscription.get("activity_id") or "").strip()
        removed = _remove_subscription_ids_locked({normalized_subscription_id})
        _team_activity_terminal_log(
            "unsubscribe",
            subscription_id=normalized_subscription_id,
            activity_id=activity_id,
            removed=removed,
            remaining_activity_subscription_count=len(_subscription_ids_by_activity.get(activity_id, set())),
        )
    _subscription_poll_lifecycle.wait({normalized_subscription_id})
    return removed


def unsubscribe_session(
    *,
    subscription_id: str = "",
    conversation_session_id: str = "",
    transport: Transport | None = None,
) -> int:
    removed = 0
    normalized_subscription_id = str(subscription_id or "").strip()
    stable = str(conversation_session_id or "").strip()
    with _lock:
        if normalized_subscription_id:
            ids = {normalized_subscription_id}
        elif transport is not None and stable:
            ids = {
                sub_id
                for sub_id in _subscription_ids_by_transport.get(transport, set())
                if (_subscriptions_by_id.get(sub_id) or {}).get("conversation_session_id") == stable
            }
        elif transport is not None:
            ids = set(_subscription_ids_by_transport.get(transport, set()))
        else:
            ids = set()
        removed = _remove_subscription_ids_locked(ids)
        if transport is not None and stable:
            _subscribers_by_session.get(stable, set()).discard(transport)
    _subscription_poll_lifecycle.wait(ids)
    return removed


def detach_transport(transport: Transport | None) -> None:
    if transport is None:
        return
    with _lock:
        subscription_ids = set(_subscription_ids_by_transport.get(transport, set()))
        subscriptions = [
            dict(_subscriptions_by_id[subscription_id])
            for subscription_id in _subscription_ids_by_transport.get(transport, set())
            if subscription_id in _subscriptions_by_id
        ]
        _remove_subscription_ids_locked(subscription_ids)
        for subscribers in _subscribers_by_session.values():
            subscribers.discard(transport)
    _subscription_poll_lifecycle.wait(subscription_ids)
    checkpoint_batches: list[tuple[Any, list[_runtime_streams.PendingCheckpoint]]] = []
    for subscription in subscriptions:
        db = subscription.get("db")
        if str(subscription.get("kind") or "session") == "activity":
            # Activity subscribers are replayed exclusively from their
            # canonical journal.  Persisting a stream checkpoint here would
            # manufacture a second visible version of the same text.
            pending = []
        else:
            pending = _runtime_streams.pending_checkpoints(
                str(subscription.get("conversation_session_id") or ""),
                db=db,
            )
        if pending:
            checkpoint_batches.append((db, pending))
    # Detach is the ownership boundary for the subscription's DB handle.
    # Complete the small, bounded checkpoint write before returning so callers
    # can safely close that handle without racing a daemon thread or losing the
    # final partial stream snapshot.
    seen_keys: set[tuple[str, ...]] = set()
    for db, pending in checkpoint_batches:
        unique_pending = [entry for entry in pending if entry.key not in seen_keys]
        seen_keys.update(entry.key for entry in unique_pending)
        try:
            _persist_checkpoint_entries(unique_pending, db=db)
        except Exception:
            logger.warning(
                "failed to persist stream checkpoint on transport detach",
                exc_info=True,
            )


def get_run(run_id: str, db: Any = None) -> dict[str, Any] | None:
    normalized = str(run_id or "").strip()
    if not normalized:
        return None
    persisted = None
    if method := _run_method(db, "get"):
        try:
            persisted = method(normalized)
        except Exception:
            persisted = None
    with _lock:
        state = _run_state_by_id.get(_memory_run_key(normalized, db))
        if state is None:
            state = _run_state_by_id.get(normalized)
        if state is None:
            scoped_suffix = f"\x1f{normalized}"
            for key, candidate in _run_state_by_id.items():
                if str(key).endswith(scoped_suffix):
                    state = candidate
                    break
        memory = dict(state) if state else None
    if not memory:
        return persisted if isinstance(persisted, dict) else None
    if not isinstance(persisted, dict):
        return memory
    if float(memory.get("updated_at") or 0) >= float(persisted.get("updated_at") or 0):
        return memory
    return persisted


def list_runs(
    conversation_session_id: str = "",
    db: Any = None,
    runtime_scope_key: str = "",
    statuses: list[str] | None = None,
    limit: int = 200,
) -> list[dict[str, Any]]:
    stable = str(conversation_session_id or "").strip()
    scope = str(runtime_scope_key or "").strip()
    normalized_statuses = [
        str(status or "").strip()
        for status in (statuses or [])
        if str(status or "").strip()
    ]
    if not stable and not scope and not normalized_statuses:
        return []
    persisted: list[dict[str, Any]] = []
    if method := _run_method(db, "list"):
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
            memory_session_key = _memory_session_key(stable, db)
            ids = list(_run_ids_by_session.get(memory_session_key, ()))
            if not ids and memory_session_key != stable:
                ids = list(_run_ids_by_session.get(stable, ()))
            memory = [dict(_run_state_by_id[run_key]) for run_key in ids if run_key in _run_state_by_id]
        else:
            scope_key = _memory_scope_key(db)
            memory = [
                dict(run)
                for key, run in _run_state_by_id.items()
                if not scope_key or str(key).startswith(f"{scope_key}\x1f")
            ]
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
    conversation_session_id: str,
    db: Any = None,
    *,
    current_gateway_instance_id: str = "",
) -> dict[str, Any]:
    _recover_orphaned_active_runs(
        db,
        current_gateway_instance_id=current_gateway_instance_id,
    )
    persisted_status = None
    if method := _run_method(db, "session_status"):
        try:
            persisted_status = method(conversation_session_id)
        except Exception:
            persisted_status = None
    runs = list_runs(conversation_session_id, db=db)
    active = [
        run for run in runs
        if str(run.get("status") or "") in ACTIVE_RUN_STATUSES
    ]
    last_seq = 0
    with _lock:
        stable = str(conversation_session_id or "").strip()
        events = _events_by_session.get(_memory_session_key(stable, db), ())
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
