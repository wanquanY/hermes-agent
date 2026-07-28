from __future__ import annotations

import hashlib
import json
import logging
from typing import Any

from hermes_team_mission.runtime.history import get_team_mission_node_runtime_history
from hermes_team_mission.runtime.team_transcript_writer import main_transcript_message_decision
from tui_gateway.methods._shared import bind_server_globals
from tui_gateway.services import run_control, team_mission_activity_events
from tui_gateway.services.message_owner_projection import (
    MessageOwnerResolutionError,
    project_render_message_owners,
)
from tui_gateway.services.run_events import list_mission_activity_events

_server = bind_server_globals(globals())
logger = logging.getLogger(__name__)

_SNAPSHOT_SCHEMA_VERSION = "2026-06-16"


def _text(value: Any) -> str:
    return str(value or "").strip()


def _truthy(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return _text(value).lower() in {"1", "true", "yes", "on"}


def _bounded_limit(value: Any, *, default: int, maximum: int) -> int:
    try:
        parsed = int(value if value is not None else default)
    except (TypeError, ValueError):
        parsed = default
    return max(1, min(parsed, maximum))


# The desktop WebSocket client rejects a single frame larger than ~4 MiB with
# close code 1009 ("message too big"), which aborts the render request AND
# tears down the gateway connection. The render bundles messages + runEvents +
# graph into ONE frame, so a long-running conversation can blow past the limit.
# Keep the serialized result safely under it (headroom for the JSON-RPC
# envelope + WS framing).
_RENDER_MAX_BYTES = 3_500_000
_RENDER_RUN_BASELINE_LIMIT_PER_RUN = 5_000
_TERMINAL_MISSION_STATUSES = {"completed", "failed", "cancelled", "canceled", "interrupted"}
_TERMINAL_RUN_STATUSES = {"completed", "failed", "cancelled", "canceled", "interrupted"}


def _payload_byte_size(obj: Any) -> int:
    try:
        return len(json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8"))
    except Exception:
        return 0


def _ordinary_render_run_events(
    session_id: str,
    events: list[Any],
    messages: list[dict[str, Any]],
    runs: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    db = _get_db()
    active_run_id = ""
    try:
        status = db.runs.session_status(session_id) if db is not None and session_id else {}
    except Exception:
        status = {}
    if isinstance(status, dict):
        active_run_id = _text(status.get("active_run_id"))
    return _filter_render_run_events(
        events,
        active_run_ids={active_run_id} if active_run_id else set(),
        terminal_tail_run_ids=_unmaterialized_terminal_run_ids(runs, messages),
        messages=messages,
        include_completed_artifacts=True,
    )


def _run_ids_from_render_messages(messages: list[dict[str, Any]]) -> list[str]:
    run_ids = sorted(item for item in _covered_render_run_ids(messages) if item)
    return run_ids


def _canonical_snapshot_runs(
    session_id: str,
    messages: list[dict[str, Any]],
    *,
    active_run_ids: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Return owner run state for the visible transcript window.

    Messages establish which historic runs are in the requested page, while
    ``active_run_ids`` keeps a just-started run visible before its first
    transcript row lands. Run status must come from the run aggregate rather
    than being reconstructed from message roles in a renderer.
    """
    visible_run_ids = _covered_render_run_ids(messages)
    visible_run_ids.update(
        _text(run_id) for run_id in (active_run_ids or set()) if _text(run_id)
    )
    if not session_id or not visible_run_ids:
        return []
    try:
        candidates = run_control.list_runs(
            session_id,
            db=_get_db(),
            limit=max(200, len(visible_run_ids)),
        )
    except Exception as exc:
        logger.warning(
            "conversation.render_snapshot canonical runs hydrate skipped session_id=%s: %s",
            session_id,
            exc,
        )
        return []
    return [
        dict(run)
        for run in candidates
        if isinstance(run, dict) and _text(run.get("run_id")) in visible_run_ids
    ]


def _message_response_run_ids(messages: list[dict[str, Any]]) -> set[str]:
    response_run_ids: set[str] = set()
    for message in messages:
        if _text(message.get("role")).lower() != "assistant":
            continue
        metadata = _message_metadata(message)
        run_id = _text(metadata.get("run_id") or metadata.get("runId"))
        if run_id:
            response_run_ids.add(run_id)
    return response_run_ids


def _unmaterialized_terminal_run_ids(
    runs: list[dict[str, Any]],
    messages: list[dict[str, Any]],
) -> set[str]:
    """Find terminal runs whose tail must remain recoverable from the ledger.

    Successful runs are compacted once their assistant response is materialized
    as transcript rows. Non-success terminal runs may stop between checkpoints
    (including while an interaction is blocking), so their uncovered tail
    remains authoritative even if an earlier assistant segment was persisted.
    """
    response_run_ids = _message_response_run_ids(messages)
    return {
        _text(run.get("run_id"))
        for run in runs
        if _text(run.get("run_id"))
        and _text(run.get("status")).lower() in _TERMINAL_RUN_STATUSES
        and (
            _text(run.get("status")).lower() != "completed"
            or _text(run.get("run_id")) not in response_run_ids
        )
    }


def _merge_render_run_events(
    page_events: list[Any],
    baseline_events: list[Any],
) -> list[dict[str, Any]]:
    """Merge canonical event pages without introducing a second event order.

    The session event page and the per-run recovery baseline are two read
    windows over the same canonical ledger. Positive ``seq`` is unique within
    a conversation session, so it is the only durable merge identity.
    """

    by_seq: dict[int, dict[str, Any]] = {}
    unsequenced: list[dict[str, Any]] = []
    for source in (page_events, baseline_events):
        for raw in source:
            if not isinstance(raw, dict):
                continue
            event = dict(raw)
            try:
                seq = int(event.get("seq") or 0)
            except (TypeError, ValueError):
                seq = 0
            if seq > 0:
                by_seq[seq] = event
            else:
                unsequenced.append(event)
    return [by_seq[seq] for seq in sorted(by_seq)] + unsequenced


def _render_run_event_baseline(
    run_ids: set[str],
) -> tuple[list[dict[str, Any]], int | None]:
    """Read the canonical recovery baseline for explicitly visible runs.

    A conversation-wide event page is independently paginated and may contain
    only old history. Active and unmaterialized terminal runs therefore need a
    run-addressed read before the snapshot can safely publish the session
    high-water mark used by the follow-up subscription. The optional replay
    cursor bounds that high-water mark when a single run exceeds the baseline
    page, so the subscription resumes at the first omitted suffix.
    """

    normalized_run_ids = sorted(
        {_text(run_id) for run_id in run_ids if _text(run_id)}
    )
    if not normalized_run_ids:
        return [], None
    db = _get_db()
    runs = getattr(db, "runs", None) if db is not None else None
    loader = getattr(runs, "list_events_by_run_ids", None)
    if not callable(loader):
        raise RuntimeError(
            "conversation.render_snapshot requires run-addressed event reads "
            "for an active or unmaterialized terminal run"
        )
    try:
        grouped = loader(
            normalized_run_ids,
            limit_per_run=_RENDER_RUN_BASELINE_LIMIT_PER_RUN,
            include_internal=False,
        )
    except Exception as exc:
        raise RuntimeError(
            "conversation.render_snapshot failed to hydrate the canonical "
            f"run-event baseline for run_ids={normalized_run_ids}"
        ) from exc
    if not isinstance(grouped, dict):
        raise RuntimeError(
            "conversation.render_snapshot run-event baseline returned an invalid result"
        )
    events = [
        dict(event)
        for run_id in normalized_run_ids
        for event in grouped.get(run_id, [])
        if isinstance(event, dict)
    ]
    replay_after_seq: int | None = None
    latest_loader = getattr(runs, "latest_event_for_run", None)
    for run_id in normalized_run_ids:
        run_events = [
            event
            for event in grouped.get(run_id, [])
            if isinstance(event, dict)
        ]
        if len(run_events) < _RENDER_RUN_BASELINE_LIMIT_PER_RUN:
            continue
        if not callable(latest_loader):
            raise RuntimeError(
                "conversation.render_snapshot requires latest-event reads "
                "when a run-event baseline reaches its page limit"
            )
        try:
            latest_event = latest_loader(run_id, include_internal=False)
        except Exception as exc:
            raise RuntimeError(
                "conversation.render_snapshot failed to verify the canonical "
                f"run-event baseline for run_id={run_id}"
            ) from exc
        loaded_last_seq = max(
            (int(event.get("seq") or 0) for event in run_events),
            default=0,
        )
        latest_seq = (
            int(latest_event.get("seq") or 0)
            if isinstance(latest_event, dict)
            else 0
        )
        if latest_seq > loaded_last_seq:
            replay_after_seq = (
                loaded_last_seq
                if replay_after_seq is None
                else min(replay_after_seq, loaded_last_seq)
            )
    return events, replay_after_seq


def _hydrate_render_run_event_baseline(
    run_events: list[Any],
    *,
    active_run_ids: set[str],
    runs: list[dict[str, Any]],
    messages: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], int | None]:
    baseline_run_ids = {
        _text(run_id) for run_id in active_run_ids if _text(run_id)
    }
    baseline_run_ids.update(_unmaterialized_terminal_run_ids(runs, messages))
    baseline_events, replay_after_seq = _render_run_event_baseline(
        baseline_run_ids
    )
    return (
        _merge_render_run_events(run_events, baseline_events),
        replay_after_seq,
    )


def _mark_transport_truncated(result: dict[str, Any]) -> None:
    result["transportTruncated"] = True
    page_info = result.get("pageInfo")
    if isinstance(page_info, dict):
        page_info["hasMore"] = True
    projection = result.get("projection")
    if isinstance(projection, dict):
        projection["transportTruncated"] = True


def _cap_list_tail(
    result: dict[str, Any],
    container: dict[str, Any],
    key: str,
    *,
    max_bytes: int,
) -> bool:
    items = container.get(key)
    if not isinstance(items, list) or not items:
        return False
    if _payload_byte_size(result) <= max_bytes:
        return False

    original_count = len(items)
    container[key] = []
    base_size = _payload_byte_size(result)
    budget = max(0, max_bytes - base_size)
    kept: list[Any] = []
    used = 0
    for item in reversed(items):
        item_size = _payload_byte_size(item) + 8  # JSON array comma/bracket headroom.
        if item_size > budget - used:
            continue
        kept.append(item)
        used += item_size
    kept.reverse()
    container[key] = kept
    while container[key] and _payload_byte_size(result) > max_bytes:
        container[key] = container[key][1:]
    return len(container[key]) < original_count


def _cap_render_result(result: dict[str, Any], *, max_bytes: int = _RENDER_MAX_BYTES) -> dict[str, Any]:
    """Trim a render result so its WS frame can't trip close code 1009.

    Drops the recoverable collections newest-kept: oldest ``runEvents`` first
    (and lowers the subscription cursor so omitted deltas replay; finished-run
    text already lives in ``messages``), then ``toolEvents`` and oldest
    ``messages`` (paginated + re-fetchable),
    until the serialized result fits. Flags ``transportTruncated`` + pageInfo
    hasMore so the client lazy-loads the remainder instead of assuming it has the
    whole history.
    """
    if _payload_byte_size(result) <= max_bytes:
        return result
    original_run_event_seqs = {
        int(event.get("seq") or 0)
        for event in result.get("runEvents") or []
        if isinstance(event, dict) and int(event.get("seq") or 0) > 0
    }
    truncated = False
    for key in ("runEvents", "toolEvents", "messages", "activityMessages"):
        truncated = _cap_list_tail(result, result, key, max_bytes=max_bytes) or truncated
        if _payload_byte_size(result) <= max_bytes:
            break
    graph = result.get("graph")
    if isinstance(graph, dict):
        for key in ("recent_messages", "recentMessages", "task_frames", "taskFrames"):
            truncated = _cap_list_tail(result, graph, key, max_bytes=max_bytes) or truncated
            if _payload_byte_size(result) <= max_bytes:
                break
    if truncated:
        _mark_transport_truncated(result)
        # The truncation marker itself adds bytes. If the payload was exactly at
        # the cap, trim one more recoverable item deterministically.
        while _payload_byte_size(result) > max_bytes:
            changed = False
            for container, key in (
                (result, "runEvents"),
                (result, "toolEvents"),
                (result, "messages"),
                (result, "activityMessages"),
                (graph, "recent_messages") if isinstance(graph, dict) else ({}, ""),
                (graph, "task_frames") if isinstance(graph, dict) else ({}, ""),
            ):
                if isinstance(container, dict) and isinstance(container.get(key), list) and container[key]:
                    container[key] = container[key][1:]
                    changed = True
                    break
            if not changed:
                break
    retained_run_event_seqs = {
        int(event.get("seq") or 0)
        for event in result.get("runEvents") or []
        if isinstance(event, dict) and int(event.get("seq") or 0) > 0
    }
    dropped_run_event_seqs = original_run_event_seqs - retained_run_event_seqs
    if dropped_run_event_seqs:
        replay_after_seq = max(0, min(dropped_run_event_seqs) - 1)
        for key in ("last_event_seq", "lastEventSeq"):
            if key in result:
                result[key] = min(
                    max(int(result.get(key) or 0), 0),
                    replay_after_seq,
                )
    return result


def _conversation_identifier(params: dict[str, Any]) -> str:
    metadata = params.get("metadata") if isinstance(params.get("metadata"), dict) else {}
    return _text(
        params.get("identifier")
        or params.get("conversation_session_id")
        or params.get("conversationSessionId")
        or params.get("session_id")
        or params.get("sessionId")
        or params.get("mission_id")
        or params.get("missionId")
        # Pre-migration request compatibility only.
        or params.get("conversation_id")
        or params.get("conversationId")
        or metadata.get("conversation_id")
        or metadata.get("conversationId")
    )


def _conversation_kind(params: dict[str, Any]) -> str:
    kind = _text(
        params.get("conversation_kind")
        or params.get("conversationKind")
        or params.get("kind")
    ).lower()
    if kind in {"team", "team_mission", "team-mission"}:
        return "team"
    if kind in {"direct", "ordinary", "hermes_session"}:
        return "direct"
    return ""


def _route_kind_from_session_index(db: Any, session_id: str) -> str:
    session_id = _text(session_id)
    if not session_id:
        return ""
    try:
        row = db.session_index.get(session_id) or {}
    except Exception as exc:
        logger.warning(
            "conversation.render_snapshot route kind lookup skipped session_id=%s: %s",
            session_id,
            exc,
        )
        return ""
    return _conversation_kind(row)


def _route_kind_from_team_conversation(db: Any, identifier: str) -> str:
    identifier = _text(identifier)
    resolver = getattr(db, "resolve_team_mission_conversation", None)
    if not identifier or not callable(resolver):
        return ""
    try:
        resolved = resolver(identifier) or {}
    except Exception as exc:
        logger.warning(
            "conversation.render_snapshot team route lookup skipped identifier=%s: %s",
            identifier,
            exc,
        )
        return ""
    conversation = resolved.get("conversation") if isinstance(resolved, dict) else {}
    if isinstance(conversation, dict) and _text(
        conversation.get("conversation_session_id")
        or conversation.get("conversationSessionId")
        or conversation.get("conversation_id")
        or conversation.get("conversationId")
    ):
        return "team"
    return ""


def _route_conversation_kind(params: dict[str, Any]) -> str:
    explicit_kind = _conversation_kind(params)
    if explicit_kind:
        return explicit_kind
    db = _get_db()
    if db is None:
        return "direct"

    session_id = _conversation_session_id(params)
    identifier = _conversation_identifier(params)
    for candidate in dict.fromkeys([session_id, identifier]):
        kind = _route_kind_from_session_index(db, candidate)
        if kind:
            return kind

    # Compatibility for callers that only pass the team conversation id. This
    # detects the conversation row itself, not active_mission_id.
    for candidate in dict.fromkeys([identifier, session_id]):
        kind = _route_kind_from_team_conversation(db, candidate)
        if kind:
            return kind
    return "direct"


def _conversation_session_id(params: dict[str, Any]) -> str:
    metadata = params.get("metadata") if isinstance(params.get("metadata"), dict) else {}
    return _text(
        params.get("conversation_session_id")
        or params.get("conversationSessionId")
        or params.get("session_id")
        or params.get("sessionId")
        or metadata.get("conversation_session_id")
        or metadata.get("conversationSessionId")
    )


def _message_params(params: dict[str, Any], session_id: str) -> dict[str, Any]:
    include_run_events = _truthy(
        params.get("include_run_events", params.get("includeRunEvents")),
        default=True,
    )
    return {
        **params,
        "session_id": session_id,
        "direction": _text(params.get("direction")) or "tail",
        "limit": _bounded_limit(params.get("limit"), default=50, maximum=200),
        "include_ancestors": _truthy(
            params.get("include_ancestors", params.get("includeAncestors")),
            default=True,
        ),
        "include_run_events": include_run_events,
        "include_tool_events": _truthy(
            params.get("include_tool_events", params.get("includeToolEvents")),
            default=False,
        ),
        "run_events_limit": _bounded_limit(
            params.get("run_events_limit", params.get("runEventsLimit")),
            default=2000,
            maximum=5000,
        ),
        "tool_events_limit": _bounded_limit(
            params.get("tool_events_limit", params.get("toolEventsLimit")),
            default=2000,
            maximum=5000,
        ),
    }


def _messages_page(
    session_id: str,
    params: dict[str, Any],
    *,
    required: bool,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    if not session_id:
        if required:
            return None, _err("conversation-render-snapshot", 4006, "session_id required")
        return {
            "messages": [],
            "toolEvents": [],
            "runEvents": [],
            "pageInfo": {},
            "branchInfo": None,
        }, None
    handler = _methods.get("session.messages")
    if not callable(handler):
        return None, _err("conversation-render-snapshot", 5008, "session.messages unavailable")
    response = handler("conversation-render-snapshot-messages", _message_params(params, session_id))
    if not isinstance(response, dict):
        return None, _err("conversation-render-snapshot", 5008, "session.messages returned invalid response")
    if response.get("error"):
        if required:
            return None, response
        return {
            "messages": [],
            "toolEvents": [],
            "runEvents": [],
            "pageInfo": {},
            "branchInfo": None,
        }, None
    result = response.get("result") if isinstance(response.get("result"), dict) else {}
    return result, None


def _participants_for_session(session_id: str) -> list[dict[str, Any]]:
    session_id = _text(session_id)
    if not session_id:
        return []
    try:
        db = _get_db()
        lister = db.participants.list_conversation_participants if db is not None else None
        if not callable(lister):
            return []
        participants = lister(session_id) or []
        return [dict(item) for item in participants if isinstance(item, dict)]
    except Exception as exc:
        logger.warning(
            "conversation.render_snapshot participants hydrate skipped session_id=%s: %s",
            session_id,
            exc,
        )
        return []


def _mission_activities_for_session(session_id: str) -> list[dict[str, Any]]:
    session_id = _text(session_id)
    if not session_id:
        return []
    try:
        db = _get_db()
        if db is None:
            return []
        activities = db.activities.list_active_missions(session_id) or []
        return [dict(item) for item in activities if isinstance(item, dict)]
    except Exception as exc:
        logger.warning(
            "conversation.render_snapshot mission activities hydrate skipped session_id=%s: %s",
            session_id,
            exc,
        )
        return []


def _activities_for_session(session_id: str) -> list[dict[str, Any]]:
    session_id = _text(session_id)
    if not session_id:
        return []
    try:
        db = _get_db()
        if db is None:
            return []
        from hermes_team_mission.read_models.conversation_activity_projection import (
            project_conversation_activities,
        )

        return project_conversation_activities(db, session_id)
    except Exception as exc:
        logger.warning(
            "conversation.render_snapshot activities hydrate skipped session_id=%s: %s",
            session_id,
            exc,
        )
        return []


def _run_event_activity_last_seq(db: Any, activity_id: str) -> int:
    activity_id = _text(activity_id)
    if not activity_id:
        return 0
    if activity_id.startswith("chat:"):
        return _run_event_session_last_seq(db, activity_id.removeprefix("chat:"))
    if activity_id.startswith("act-member_chat:"):
        parts = activity_id.split(":")
        if len(parts) >= 2:
            return _run_event_session_last_seq(db, parts[1])
    try:
        return team_mission_activity_events.activity_last_seq(activity_id, db=db)
    except Exception:
        return 0


def _run_event_session_last_seq(db: Any, session_id: str) -> int:
    session_id = _text(session_id)
    if not session_id or db is None:
        return 0
    try:
        status = db.runs.session_status(session_id)
    except Exception:
        return 0
    return max(int(status.get("last_event_seq") or 0), 0)


def _mission_activity_last_seq(db: Any, mission_id: str) -> int:
    mission_id = _text(mission_id)
    if not mission_id:
        return 0
    try:
        events = list_mission_activity_events(db, mission_id, limit=1, reverse=True)
        return max((int(event.get("seq") or 0) for event in events if isinstance(event, dict)), default=0)
    except Exception:
        return 0


def _activity_watermark(
    *,
    activity_id: str,
    last_seq: int,
    status: str,
    terminal: bool,
    replay_policy: str,
    source: str,
) -> dict[str, Any]:
    normalized_activity_id = _text(activity_id)
    normalized_status = _text(status) or ("completed" if terminal else "idle")
    normalized_policy = _text(replay_policy) or ("cursor_only" if terminal else "replay_live")
    normalized_source = _text(source)
    seq = max(0, int(last_seq or 0))
    return {
        "activity_id": normalized_activity_id,
        "activityId": normalized_activity_id,
        "last_seq": seq,
        "lastSeq": seq,
        "status": normalized_status,
        "terminal": bool(terminal),
        "replay_policy": normalized_policy,
        "replayPolicy": normalized_policy,
        "source": normalized_source,
    }


def _mission_status_for_watermark(conversation: dict[str, Any], mission: dict[str, Any], is_running: bool) -> str:
    mission_status = _text(
        mission.get("status")
        or conversation.get("mission_status")
        or conversation.get("missionStatus")
        or conversation.get("run_state")
        or conversation.get("runState")
        or conversation.get("activity_state")
        or conversation.get("activityState")
    ).lower()
    if is_running:
        return "running"
    if mission_status in _TERMINAL_MISSION_STATUSES or mission_status == "waiting_approval":
        return mission_status
    return "idle"


def _team_member_ids(team: dict[str, Any], participants: list[dict[str, Any]]) -> list[str]:
    ids: list[str] = []
    member_lists = (
        team.get("members"),
        team.get("teamMembers"),
        team.get("team_members"),
        team.get("agent_team_members"),
    )
    for members in member_lists:
        if not isinstance(members, list):
            continue
        for member in members:
            if not isinstance(member, dict):
                continue
            member_id = _text(
                member.get("member_id")
                or member.get("memberId")
                or member.get("id")
                or member.get("participant_id")
                or member.get("participantId")
            )
            role = _text(member.get("role")).lower()
            if member_id and role not in {"lead", "leader"}:
                ids.append(member_id)
    if not ids:
        for participant in participants:
            member_id = _text(
                participant.get("member_id")
                or participant.get("memberId")
                or participant.get("participant_id")
                or participant.get("participantId")
                or participant.get("id")
            )
            role = _text(participant.get("role")).lower()
            if member_id and role not in {"lead", "leader"}:
                ids.append(member_id)
    return list(dict.fromkeys(ids))


def _team_activity_watermarks(
    *,
    session_id: str,
    conversation: dict[str, Any],
    mission: dict[str, Any],
    team: dict[str, Any],
    participants: list[dict[str, Any]],
    mission_activities: list[dict[str, Any]],
    activities: list[dict[str, Any]],
    is_running: bool,
) -> list[dict[str, Any]]:
    db = _get_db()
    if db is None:
        return []
    normalized_session_id = _text(session_id)
    status = _mission_status_for_watermark(conversation, mission, is_running)
    terminal = status in _TERMINAL_MISSION_STATUSES or (not is_running and status == "idle")
    replay_policy = "replay_live" if is_running else "cursor_only"
    watermarks: list[dict[str, Any]] = []

    chat_activity_id = f"chat:{normalized_session_id}" if normalized_session_id else ""
    if chat_activity_id:
        watermarks.append(_activity_watermark(
            activity_id=chat_activity_id,
            last_seq=max(
                _run_event_activity_last_seq(db, chat_activity_id),
                _run_event_session_last_seq(db, normalized_session_id),
            ),
            status=status,
            terminal=terminal,
            replay_policy=replay_policy,
            source="run_events",
        ))

    mission_ids = []
    mission_id = _text(mission.get("mission_id") or mission.get("missionId"))
    if mission_id:
        mission_ids.append(mission_id)
    for activity in mission_activities:
        if not isinstance(activity, dict):
            continue
        activity_mission_id = _text(
            activity.get("target_mission_id")
            or activity.get("targetMissionId")
            or activity.get("mission_id")
            or activity.get("missionId")
        )
        if activity_mission_id:
            mission_ids.append(activity_mission_id)
    for item in dict.fromkeys(mission_ids):
        watermarks.append(_activity_watermark(
            activity_id=f"mission:{item}",
            last_seq=_mission_activity_last_seq(db, item),
            status=status if item == mission_id else "completed",
            terminal=terminal if item == mission_id else True,
            replay_policy="cursor_only" if (terminal or item != mission_id) else "replay_live",
            source="run_events",
        ))

    for member_id in _team_member_ids(team, participants):
        activity_id = f"act-member_chat:{normalized_session_id}:{member_id}" if normalized_session_id else ""
        if not activity_id:
            continue
        watermarks.append(_activity_watermark(
            activity_id=activity_id,
            last_seq=_run_event_activity_last_seq(db, activity_id),
            status=status,
            terminal=terminal,
            replay_policy=replay_policy,
            source="run_events",
        ))

    for activity in activities:
        if not isinstance(activity, dict):
            continue
        activity_id = _text(activity.get("activity_id") or activity.get("activityId"))
        if not activity_id.startswith("act-node:"):
            continue
        activity_status = _text(activity.get("status") or activity.get("state")).lower() or "pending"
        activity_terminal = activity_status in _TERMINAL_MISSION_STATUSES
        watermarks.append(_activity_watermark(
            activity_id=activity_id,
            last_seq=_run_event_activity_last_seq(db, activity_id),
            status=activity_status,
            terminal=activity_terminal,
            replay_policy="cursor_only" if activity_terminal else "replay_live",
            source="run_events",
        ))

    deduped: dict[str, dict[str, Any]] = {}
    for watermark in watermarks:
        activity_id = _text(watermark.get("activity_id"))
        if activity_id:
            deduped[activity_id] = watermark
    return list(deduped.values())


def _team_activity_messages(
    *,
    session_id: str,
    activities: list[dict[str, Any]],
    limit_per_activity: int = 50,
) -> list[dict[str, Any]]:
    """Project node transcripts beside, but never into, the group-chat feed.

    ``messages`` remains the owning conversation transcript.  Activity
    transcripts are a separate snapshot lane so the unified Conversation
    aggregate can hydrate graph-node details without making execution output a
    group-chat message.  Live continuation is delivered by the corresponding
    ``runtime.activity.subscribe`` stream.
    """

    db = _get_db()
    if db is None:
        return []
    projected: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for activity in activities:
        if not isinstance(activity, dict):
            continue
        activity_id = _text(activity.get("activity_id") or activity.get("activityId"))
        node_id = _text(activity.get("graph_node_id") or activity.get("graphNodeId"))
        mission_id = _text(
            activity.get("target_mission_id")
            or activity.get("targetMissionId")
            or activity.get("mission_id")
            or activity.get("missionId")
        )
        kind = _text(activity.get("kind") or activity.get("activity_kind")).lower()
        if not activity_id or kind not in {"mission", "agent_dispatch"}:
            continue
        try:
            result = get_team_mission_node_runtime_history(db, {
                "mission_id": mission_id,
                "node_id": node_id,
                "activity_id": activity_id,
                "limit": limit_per_activity,
            })
        except Exception as exc:
            logger.warning(
                "conversation.render_snapshot activity transcript hydrate skipped "
                "session_id=%s activity_id=%s: %s",
                session_id,
                activity_id,
                exc,
            )
            continue
        messages = result.get("messages") if isinstance(result, dict) else []
        if not isinstance(messages, list):
            continue
        owner_participant_id = _text(
            activity.get("owner_participant_id") or activity.get("ownerParticipantId")
        )
        for raw_message in messages:
            if not isinstance(raw_message, dict):
                continue
            message = dict(raw_message)
            source_message_id = _message_id(message)
            dedupe_key = (activity_id, source_message_id)
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            metadata = dict(_message_metadata(message))
            metadata["activity_id"] = activity_id
            metadata["activity_kind"] = kind
            if node_id:
                metadata.setdefault("node_id", node_id)
            if mission_id:
                metadata.setdefault("mission_id", mission_id)
            message["metadata"] = metadata
            role = _text(message.get("role")).lower()
            if role == "user":
                message["participant_id"] = "user"
            elif owner_participant_id and role in {"assistant", "tool"}:
                message["participant_id"] = owner_participant_id
            if source_message_id:
                message["source_message_id"] = source_message_id
                message["message_id"] = f"{activity_id}:{source_message_id}"
            projected.append(message)
    return sorted(
        projected,
        key=lambda message: (
            float(message.get("timestamp") or message.get("created_at") or 0),
            _text(message.get("message_id") or message.get("id")),
        ),
    )


def _record(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _message_metadata(message: dict[str, Any]) -> dict[str, Any]:
    return _record(message.get("metadata"))


def _message_text(message: dict[str, Any]) -> str:
    return str(message.get("text") or message.get("content") or "")


def _diagnostic_text_summary(value: Any) -> dict[str, Any]:
    text = str(value or "")
    return {
        "len": len(text),
        "sha1": hashlib.sha1(text.encode("utf-8", errors="replace")).hexdigest()[:12],
        "preview": text[:80].replace("\n", "\\n"),
    }


def _diagnostic_message_summary(index: int, message: dict[str, Any]) -> dict[str, Any]:
    metadata = _message_metadata(message)
    team_mission = _record(metadata.get("team_mission") or metadata.get("teamMission"))
    decision = main_transcript_message_decision(message)
    return {
        "idx": index,
        "role": _text(message.get("role")),
        "message_id": _message_id(message),
        "conversation_message_id": _text(
            message.get("conversation_message_id") or message.get("conversationMessageId")
        ),
        "participant_id": _text(message.get("participant_id") or message.get("participantId")),
        "run_id": _text(metadata.get("run_id") or metadata.get("runId")),
        "turn_id": _text(metadata.get("turn_id") or metadata.get("turnId")),
        "activity_id": _text(metadata.get("activity_id") or metadata.get("activityId")),
        "activity_kind": _text(metadata.get("activity_kind") or metadata.get("activityKind")),
        "runtime_activity_kind": _text(metadata.get("runtime_activity_kind") or metadata.get("runtimeActivityKind")),
        "transcript_activity_kind": _text(
            metadata.get("transcript_activity_kind") or metadata.get("transcriptActivityKind")
        ),
        "team_mission_kind": _text(team_mission.get("kind")),
        "team_mission_source_node_id": _text(team_mission.get("source_node_id") or team_mission.get("sourceNodeId")),
        "decision": decision,
        "text": _diagnostic_text_summary(_message_text(message)),
    }


def _emit_team_render_diagnostic(stage: str, **fields: Any) -> None:
    try:
        from agent.dovie_diagnostics import emit_dovie_diagnostic

        emit_dovie_diagnostic("[dovie-team-render-debug]", {"stage": stage, **fields})
    except Exception:
        pass


def _message_id(message: dict[str, Any]) -> str:
    return _text(
        message.get("conversation_message_id")
        or message.get("conversationMessageId")
        or message.get("message_id")
        or message.get("messageId")
        or message.get("id")
    )


def _message_source_run_id(message: dict[str, Any]) -> str:
    metadata = _message_metadata(message)
    team_mission = _record(metadata.get("team_mission") or metadata.get("teamMission"))
    return _text(
        metadata.get("source_run_id")
        or metadata.get("sourceRunId")
        or team_mission.get("source_run_id")
        or team_mission.get("sourceRunId")
    )


def _covered_render_run_ids(messages: list[dict[str, Any]]) -> set[str]:
    covered: set[str] = set()
    for message in messages:
        metadata = _message_metadata(message)
        for value in (
            metadata.get("run_id"),
            metadata.get("runId"),
            metadata.get("original_run_id"),
            _message_source_run_id(message),
        ):
            text = _text(value)
            if text:
                covered.add(text)
    return covered


def _event_run_id(event: Any) -> str:
    if not isinstance(event, dict):
        return ""
    payload = _record(event.get("payload"))
    return _text(
        event.get("run_id")
        or payload.get("run_id")
        or payload.get("runId")
        or payload.get("produced_by_run_id")
        or payload.get("producedByRunId")
    )


def _run_turn_segment_key(
    *,
    run_id: Any,
    turn_id: Any,
    segment: Any,
    role: Any = "",
) -> str:
    run_id = _text(run_id)
    turn_id = _text(turn_id)
    if not run_id or not turn_id:
        return ""
    try:
        segment_index = max(0, int(segment or 0))
    except (TypeError, ValueError):
        segment_index = 0
    return "|".join((run_id, turn_id, str(segment_index), _text(role).lower()))


def _covered_render_facts(messages: list[dict[str, Any]]) -> dict[str, set[str]]:
    facts = {"messages": set(), "tools": set(), "reasoning": set()}
    for message in messages:
        metadata = _message_metadata(message)
        role = _text(message.get("role")).lower()
        message_id = _message_id(message)
        if message_id:
            facts["messages"].add(message_id)
        tool_call_id = _text(
            message.get("tool_call_id")
            or message.get("toolCallId")
            or metadata.get("tool_call_id")
            or metadata.get("toolCallId")
            or metadata.get("tool_id")
        )
        if tool_call_id:
            facts["tools"].add(tool_call_id)
        run_id = metadata.get("run_id") or metadata.get("runId")
        turn_id = metadata.get("turn_id") or metadata.get("turnId")
        segment = metadata.get("assistant_segment_index") or metadata.get("assistantSegmentIndex")
        fact_key = _run_turn_segment_key(
            run_id=run_id,
            turn_id=turn_id,
            segment=segment,
            role=role,
        )
        if fact_key and (message.get("text") or message.get("reasoning")):
            facts["messages"].add(fact_key)
        if fact_key and message.get("reasoning"):
            facts["reasoning"].add(fact_key)
    return facts


def _run_event_is_covered(
    event: dict[str, Any],
    covered: dict[str, set[str]],
    *,
    inferred_segment: int = 0,
) -> bool:
    payload = _record(event.get("payload"))
    event_type = _text(event.get("type")).lower()
    message_id = _text(
        payload.get("message_id")
        or payload.get("messageId")
        or event.get("projected_message_id")
        or event.get("projectedMessageId")
        or event.get("_projected_message_id")
    )
    if message_id and message_id in covered["messages"]:
        return True
    tool_call_id = _text(
        payload.get("tool_call_id")
        or payload.get("toolCallId")
        or payload.get("tool_id")
        or payload.get("toolId")
    )
    if event_type.startswith("tool.") and tool_call_id in covered["tools"]:
        return True
    role = _text(payload.get("role") or "assistant").lower()
    segment = payload.get("assistant_segment_index")
    if segment is None:
        segment = payload.get("assistantSegmentIndex")
    if segment is None:
        segment = inferred_segment
    fact_key = _run_turn_segment_key(
        run_id=_event_run_id(event),
        turn_id=event.get("turn_id") or payload.get("turn_id") or payload.get("turnId"),
        segment=segment,
        role=role,
    )
    if event_type.startswith("message.") and fact_key in covered["messages"]:
        return True
    if (
        event_type.startswith("reasoning.")
        or event_type.startswith("thinking.")
    ) and fact_key in covered["reasoning"]:
        return True
    return False


def _team_snapshot_active_chat_run_ids(conversation: dict[str, Any]) -> set[str]:
    """Return only live chat response runs for the conversation.

    A mission run is an independent background activity. Treating its
    ``active_run_id`` as a leader-chat run replays completed chat history while
    a team task is running, which makes the render snapshot non-idempotent.
    """
    active_run_id = _text(
        conversation.get("active_run_id") or conversation.get("activeRunId")
    )
    return {active_run_id} if active_run_id else set()


def _filter_render_run_events(
    run_events: list[Any],
    *,
    active_run_ids: set[str],
    terminal_tail_run_ids: set[str] | None = None,
    messages: list[dict[str, Any]],
    include_completed_artifacts: bool,
) -> list[dict[str, Any]]:
    normalized_events = [dict(event) for event in run_events if isinstance(event, dict)]
    active_run_ids = {_text(run_id) for run_id in active_run_ids if _text(run_id)}
    terminal_tail_run_ids = {
        _text(run_id) for run_id in (terminal_tail_run_ids or set()) if _text(run_id)
    }
    covered_run_ids = _covered_render_run_ids(messages)
    if (
        not active_run_ids
        and not terminal_tail_run_ids
        and not (include_completed_artifacts and covered_run_ids)
    ):
        return []
    covered_facts = _covered_render_facts(messages)
    segment_by_turn: dict[str, int] = {}
    observed_tools_by_turn: dict[str, set[str]] = {}
    filtered: list[dict[str, Any]] = []
    for event in normalized_events:
        run_id = _event_run_id(event)
        is_active_run = bool(run_id and run_id in active_run_ids)
        is_unmaterialized_terminal_tail = bool(
            run_id and run_id in terminal_tail_run_ids
        )
        is_visible_completed_artifact = bool(
            include_completed_artifacts
            and run_id
            and run_id in covered_run_ids
            and _text(event.get("type")).startswith("artifact.")
        )
        if (
            not is_active_run
            and not is_unmaterialized_terminal_tail
            and not is_visible_completed_artifact
        ):
            continue
        payload = _record(event.get("payload"))
        turn_id = _text(event.get("turn_id") or payload.get("turn_id") or payload.get("turnId"))
        turn_key = f"{run_id}|{turn_id}" if run_id and turn_id else run_id
        inferred_segment = segment_by_turn.get(turn_key, 0)
        if not _run_event_is_covered(
            event,
            covered_facts,
            inferred_segment=inferred_segment,
        ):
            filtered.append(event)
        event_type = _text(event.get("type")).lower()
        if event_type.startswith("tool.") and turn_key:
            tool_id = _text(
                payload.get("tool_call_id")
                or payload.get("toolCallId")
                or payload.get("tool_id")
                or payload.get("toolId")
                or event.get("seq")
            )
            seen = observed_tools_by_turn.setdefault(turn_key, set())
            if tool_id and tool_id not in seen:
                seen.add(tool_id)
                segment_by_turn[turn_key] = inferred_segment + 1
    return filtered


def _filter_team_render_run_events(
    run_events: list[Any],
    *,
    conversation: dict[str, Any],
    messages: list[dict[str, Any]],
    runs: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    return _filter_render_run_events(
        run_events,
        active_run_ids=_team_snapshot_active_chat_run_ids(conversation),
        terminal_tail_run_ids=_unmaterialized_terminal_run_ids(runs, messages),
        messages=messages,
        include_completed_artifacts=False,
    )


def _team_conversation_status_projection(conversation_session_id: str) -> dict[str, Any]:
    conversation_session_id = _text(conversation_session_id)
    if not conversation_session_id:
        return {}
    try:
        db = _get_db()
        projector = getattr(db, "get_team_mission_conversation_status_projection", None) if db is not None else None
        if not callable(projector):
            return {}
        projection = projector(conversation_session_id) or {}
        return dict(projection) if isinstance(projection, dict) else {}
    except Exception as exc:
        logger.warning(
            "conversation.render_snapshot status projection skipped conversation_session_id=%s: %s",
            conversation_session_id,
            exc,
        )
        return {}


def _team_conversation_is_running(
    *,
    conversation: dict[str, Any],
    mission: dict[str, Any],
    status_projection: dict[str, Any] | None = None,
) -> bool:
    conversation_session_id = _text(
        conversation.get("conversation_session_id")
        or conversation.get("conversationSessionId")
    )
    projection = status_projection if isinstance(status_projection, dict) else {}
    if not projection:
        projection = _team_conversation_status_projection(conversation_session_id)
    if projection:
        return bool(projection.get("running")) or _text(
            projection.get("projected_state")
            or projection.get("run_state")
            or projection.get("runState")
            or projection.get("activity_state")
            or projection.get("activityState")
        ).lower() == "running"
    return bool(
        conversation.get("running")
        or conversation.get("active_run_id")
        or conversation.get("activeRunId")
        or mission.get("active_run_id")
        or mission.get("activeRunId")
    )


def _filter_main_transcript_messages(messages: list[Any]) -> list[dict[str, Any]]:
    return [
        dict(message)
        for message in messages
        if isinstance(message, dict) and main_transcript_message_decision(message).get("include")
    ]


def _team_page_info_for_visible_messages(
    page_info: Any,
    *,
    raw_messages: list[Any],
    visible_messages: list[dict[str, Any]],
) -> dict[str, Any]:
    info = dict(page_info) if isinstance(page_info, dict) else {}
    raw_count = len([message for message in raw_messages if isinstance(message, dict)])
    visible_count = len(visible_messages)
    if raw_count == visible_count:
        return info
    for key in ("totalCount", "total_count", "returnedCount", "returned_count"):
        if key in info:
            info[key] = visible_count
    info["filteredByTranscriptActivity"] = True
    info["visibleCount"] = visible_count
    return info


def _team_graph_with_visible_messages(
    graph: dict[str, Any],
    *,
    messages: list[dict[str, Any]],
    page_info: dict[str, Any],
) -> dict[str, Any]:
    next_graph = dict(graph) if isinstance(graph, dict) else {}
    visible_messages = [dict(message) for message in messages]
    next_graph["recent_messages"] = visible_messages
    next_graph["recentMessages"] = visible_messages
    next_graph["message_page_info"] = dict(page_info)
    next_graph["messagePageInfo"] = dict(page_info)
    last_message = dict(visible_messages[-1]) if visible_messages else {}
    next_graph["last_message"] = last_message
    next_graph["lastMessage"] = last_message
    return next_graph


def _team_conversation_snapshot(
    rid: Any,
    params: dict[str, Any],
    *,
    projection_source: str = "conversation.render_snapshot",
) -> dict[str, Any]:
    identifier = _conversation_identifier(params)
    if not identifier:
        return _err(rid, 4006, "conversation identifier required")
    handler = _methods.get("team_mission.conversation.resolve")
    if not callable(handler):
        return _err(rid, 5008, "team_mission.conversation.resolve unavailable")
    response = handler(
        "conversation-render-snapshot-resolve",
        {
            **params,
            "identifier": identifier,
        },
    )
    if not isinstance(response, dict):
        return _err(rid, 5008, "team_mission.conversation.resolve returned invalid response")
    if response.get("error"):
        return response
    resolved = response.get("result") if isinstance(response.get("result"), dict) else {}
    conversation = resolved.get("conversation") if isinstance(resolved.get("conversation"), dict) else {}
    if not _text(
        conversation.get("conversation_session_id")
        or conversation.get("conversationSessionId")
    ):
        logger.error(
            "team_mission.conversation.resolve returned conversation without canonical id: identifier=%s",
            identifier,
        )
        return _err(rid, 5008, "team_mission resolve returned conversation without conversation_session_id")
    graph = resolved.get("graph") if isinstance(resolved.get("graph"), dict) else {}
    team = resolved.get("team") if isinstance(resolved.get("team"), dict) else {}
    graph_conversation = graph.get("conversation") if isinstance(graph.get("conversation"), dict) else {}
    session_id = _text(
        conversation.get("conversation_session_id")
        or conversation.get("conversationSessionId")
        or graph_conversation.get("conversation_session_id")
        or graph_conversation.get("conversationSessionId")
        or _conversation_session_id(params)
    )
    page, error = _messages_page(session_id, params, required=False)
    if error:
        return error
    raw_messages = list(page.get("messages") or []) if isinstance(page, dict) else []
    messages = _filter_main_transcript_messages(raw_messages)
    raw_message_summaries = [
        _diagnostic_message_summary(index, message)
        for index, message in enumerate(raw_messages)
        if isinstance(message, dict)
    ]
    filtered_message_summaries = [
        summary for summary in raw_message_summaries
        if not _record(summary.get("decision")).get("include")
    ]
    raw_run_events = list(page.get("runEvents") or []) if isinstance(page, dict) else []
    active_chat_run_ids = _team_snapshot_active_chat_run_ids(conversation)
    runs = _canonical_snapshot_runs(
        session_id,
        messages,
        active_run_ids=active_chat_run_ids,
    )
    raw_run_events, replay_after_seq = _hydrate_render_run_event_baseline(
        raw_run_events,
        active_run_ids=active_chat_run_ids,
        runs=runs,
        messages=messages,
    )
    participants = _participants_for_session(session_id)
    try:
        messages = project_render_message_owners(
            messages,
            run_events=raw_run_events,
            participants=participants,
            allow_single_execution_participant=True,
        )
    except MessageOwnerResolutionError as exc:
        return _err(rid, 5008, str(exc))
    # Completed tool calls are transcript rows. The render snapshot exposes
    # only a transcript prefix plus the active chat run tail; a second historic
    # tool-event lane would create another ordering authority in the client.
    tool_events: list[dict[str, Any]] = []
    page_info = (
        page.get("pageInfo")
        if isinstance(page, dict) and isinstance(page.get("pageInfo"), dict)
        else graph.get("message_page_info") or graph.get("messagePageInfo") or {}
    )
    page_info = _team_page_info_for_visible_messages(
        page_info,
        raw_messages=raw_messages,
        visible_messages=messages,
    )
    graph = _team_graph_with_visible_messages(
        graph,
        messages=messages,
        page_info=page_info,
    )
    mission = resolved.get("mission") if isinstance(resolved.get("mission"), dict) else {}
    mission_present = bool(_text(mission.get("mission_id") or mission.get("missionId")))
    if not mission_present:
        mission = {}
    status_projection = _team_conversation_status_projection(session_id)
    is_running = _team_conversation_is_running(
        conversation=conversation,
        mission=mission,
        status_projection=status_projection,
    )
    mission_activities = _mission_activities_for_session(session_id)
    activities = _activities_for_session(session_id)
    activity_watermarks = _team_activity_watermarks(
        session_id=session_id,
        conversation=conversation,
        mission=mission,
        team=team,
        participants=participants,
        mission_activities=mission_activities,
        activities=activities,
        is_running=is_running,
    )
    activity_messages = _team_activity_messages(
        session_id=session_id,
        activities=activities,
    )
    _emit_team_render_diagnostic(
        "team-conversation-snapshot-filter",
        request_id=str(rid),
        identifier=identifier,
        conversation_session_id=session_id,
        session_id=session_id,
        mission_id=_text(mission.get("mission_id") or mission.get("missionId")),
        raw_message_count=len(raw_message_summaries),
        visible_message_count=len(messages),
        filtered_message_count=len(filtered_message_summaries),
        raw_run_event_count=len(raw_run_events),
        tool_event_count=len(tool_events),
        is_running=is_running,
        filtered_samples=filtered_message_summaries[:16],
        raw_samples=raw_message_summaries[:24],
    )
    runs = _canonical_snapshot_runs(
        session_id,
        messages,
        active_run_ids=active_chat_run_ids,
    )
    run_events = _filter_team_render_run_events(
        raw_run_events,
        conversation=conversation,
        messages=messages,
        runs=runs,
    )
    last_event_seq = _run_event_session_last_seq(_get_db(), session_id)
    if replay_after_seq is not None:
        last_event_seq = min(last_event_seq, replay_after_seq)
    branch_info = page.get("branchInfo") if isinstance(page, dict) else None
    return _ok(
        rid,
        _cap_render_result({
            "kind": "team_mission",
            "schemaVersion": _SNAPSHOT_SCHEMA_VERSION,
            "renderReady": True,
            "conversation_session_id": session_id,
            "session_id": session_id,
            "conversation": conversation,
            "mission": mission,
            "missions": mission_activities,
            "activities": activities,
            "missionPresent": mission_present,
            "mission_present": mission_present,
            "team": team,
            "graph": graph,
            "participants": participants,
            "messages": messages,
            "activityMessages": activity_messages,
            "toolEvents": tool_events,
            "runs": runs,
            "runEvents": run_events,
            "last_event_seq": last_event_seq,
            "lastEventSeq": last_event_seq,
            "activityWatermarks": activity_watermarks,
            "activity_watermarks": activity_watermarks,
            "pageInfo": page_info if isinstance(page_info, dict) else {},
            "branchInfo": branch_info if isinstance(branch_info, dict) else None,
            "projection": {
                **status_projection,
                "schemaVersion": _SNAPSHOT_SCHEMA_VERSION,
                "source": projection_source,
                "renderReady": True,
                "activityWatermarks": activity_watermarks,
                "activity_watermarks": activity_watermarks,
                "visibleWindow": {
                    "direction": _text(params.get("direction")) or "tail",
                    "limit": _bounded_limit(params.get("limit"), default=50, maximum=200),
                },
            },
        }),
    )


def _ordinary_conversation_snapshot(rid: Any, params: dict[str, Any]) -> dict[str, Any]:
    session_id = _conversation_session_id(params) or _conversation_identifier(params)
    page, error = _messages_page(session_id, params, required=True)
    if error:
        return error
    page = page or {}
    participants = _participants_for_session(session_id)
    raw_messages = list(page.get("messages") or [])
    raw_run_events = list(page.get("runEvents") or [])
    status = {}
    try:
        status = _get_db().runs.session_status(session_id) if _get_db() is not None else {}
    except Exception:
        status = {}
    active_run_id = _text(status.get("active_run_id")) if isinstance(status, dict) else ""
    active_run_ids = {active_run_id} if active_run_id else set()
    runs = _canonical_snapshot_runs(
        session_id,
        raw_messages,
        active_run_ids=active_run_ids,
    )
    raw_run_events, replay_after_seq = _hydrate_render_run_event_baseline(
        raw_run_events,
        active_run_ids=active_run_ids,
        runs=runs,
        messages=raw_messages,
    )
    try:
        messages = project_render_message_owners(
            raw_messages,
            run_events=raw_run_events,
            participants=participants,
            allow_single_execution_participant=True,
        )
    except MessageOwnerResolutionError as exc:
        return _err(rid, 5008, str(exc))
    runs = _canonical_snapshot_runs(
        session_id,
        messages,
        active_run_ids=active_run_ids,
    )
    last_event_seq = _run_event_session_last_seq(_get_db(), session_id)
    if replay_after_seq is not None:
        last_event_seq = min(last_event_seq, replay_after_seq)
    return _ok(
        rid,
        _cap_render_result({
            "kind": "ordinary",
            "schemaVersion": _SNAPSHOT_SCHEMA_VERSION,
            "renderReady": True,
            "conversation_session_id": session_id,
            "session_id": session_id,
            "participants": participants,
            "messages": messages,
            "toolEvents": [],
            "runs": runs,
            "runEvents": _ordinary_render_run_events(
                session_id,
                raw_run_events,
                messages,
                runs,
            ),
            "last_event_seq": last_event_seq,
            "lastEventSeq": last_event_seq,
            "pageInfo": page.get("pageInfo") if isinstance(page.get("pageInfo"), dict) else {},
            "branchInfo": page.get("branchInfo") if isinstance(page.get("branchInfo"), dict) else None,
            "projection": {
                "schemaVersion": _SNAPSHOT_SCHEMA_VERSION,
                "source": "conversation.render_snapshot",
                "renderReady": True,
                "visibleWindow": {
                    "direction": _text(params.get("direction")) or "tail",
                    "limit": _bounded_limit(params.get("limit"), default=50, maximum=200),
                },
            },
        }),
    )


@method("conversation.render_snapshot")
def _(rid, params: dict) -> dict:
    params = params if isinstance(params, dict) else {}
    if _route_conversation_kind(params) == "team":
        return _team_conversation_snapshot(rid, params)
    return _ordinary_conversation_snapshot(rid, params)


@method("team_mission.conversation.render")
def _(rid, params: dict) -> dict:
    params = params if isinstance(params, dict) else {}
    return _team_conversation_snapshot(
        rid,
        {
            **params,
            "kind": "team_mission",
        },
        projection_source="team_mission.conversation.render",
    )
