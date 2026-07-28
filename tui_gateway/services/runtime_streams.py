"""In-memory text streams with durable semantic checkpoints.

Token deltas are a live transport concern, not an event-ledger row. This
module retains the authoritative text for active streams so reconnect replay
can use one snapshot and structural boundaries can persist one checkpoint.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any


TRANSIENT_STREAM_EVENT_TYPES = frozenset(
    {
        "message.delta",
        "reasoning.delta",
        "thinking.delta",
        "subagent.output_delta",
        "subagent.reasoning_delta",
        "agent_profile_test.output_delta",
        "agent_profile_test.thinking",
    }
)


@dataclass
class _StreamLane:
    key: tuple[str, ...]
    frame: dict[str, Any]
    text: str = ""
    latest_source_seq: int = 0
    checkpointed_offset: int = 0
    rewrite_revision: int = 0
    requires_snapshot_checkpoint: bool = False
    dirty: bool = False


@dataclass(frozen=True)
class PendingCheckpoint:
    key: tuple[str, ...]
    frame: dict[str, Any]
    end_offset: int
    rewrite_revision: int
    mode: str


_lock = threading.RLock()
_lanes: dict[tuple[str, ...], _StreamLane] = {}


def is_transient_stream_event(frame: dict[str, Any]) -> bool:
    if str(frame.get("type") or "").strip() not in TRANSIENT_STREAM_EVENT_TYPES:
        return False
    payload = frame.get("payload") if isinstance(frame.get("payload"), dict) else {}
    return not bool(payload.get("stream_checkpoint") or payload.get("streamCheckpoint"))


def observe(
    frame: dict[str, Any],
    *,
    db: Any = None,
    checkpoint_required: bool = True,
) -> None:
    if not is_transient_stream_event(frame):
        return
    key = _lane_key(frame, db=db)
    if not key[1] or not key[2]:
        return
    payload = frame.get("payload") if isinstance(frame.get("payload"), dict) else {}
    text_stream = frame.get("text_stream") if isinstance(frame.get("text_stream"), dict) else {}
    if not text_stream:
        text_stream = payload.get("text_stream") if isinstance(payload.get("text_stream"), dict) else {}
    incoming = _first_raw_text(
        text_stream.get("delta"),
        text_stream.get("text"),
        text_stream.get("snapshot"),
        payload.get("delta"),
        payload.get("text"),
        payload.get("snapshot"),
        payload.get("output"),
    )
    if not incoming:
        return
    mode = str(text_stream.get("mode") or payload.get("mode") or "append").strip().lower()
    offset = _optional_int(text_stream.get("offset"), payload.get("offset"))
    source_seq = _optional_int(
        frame.get("runtime_source_seq"),
        frame.get("runtimeSourceSeq"),
        frame.get("seq"),
    ) or 0
    with _lock:
        lane = _lanes.get(key)
        if lane is None:
            lane = _StreamLane(key=key, frame=_copy_frame(frame))
            _lanes[key] = lane
        # Make the operation explicit before any live fan-out. Consumers must
        # never infer append-vs-snapshot semantics from the fragment content or
        # from whether a provider happened to use `text` versus `delta`.
        payload["mode"] = mode
        if text_stream:
            text_stream["mode"] = mode
        frame["payload"] = payload
        if isinstance(frame.get("text_stream"), dict):
            frame["text_stream"]["mode"] = mode
        # Every append fragment needs an absolute UTF-16 position before it
        # leaves Hermes.  Provider adapters historically omitted the offset
        # for ordinary token chunks, which made the live transport and the
        # later durable checkpoint impossible to reconcile when they crossed
        # in flight.  The stream registry is the single owner of accumulated
        # text, so it is also the only correct place to infer this value.
        if offset is None and mode == "append":
            offset = _utf16_length(lane.text)
            payload["offset"] = offset
            if text_stream:
                text_stream["offset"] = offset
            frame["payload"] = payload
            if isinstance(frame.get("text_stream"), dict):
                frame["text_stream"]["offset"] = offset
        next_text = _merge_text(lane.text, incoming, mode=mode, offset=offset)
        if next_text != lane.text and lane.checkpointed_offset > 0:
            durable_prefix = _slice_utf16(lane.text, 0, lane.checkpointed_offset)
            next_prefix = _slice_utf16(next_text, 0, lane.checkpointed_offset)
            if (
                _utf16_length(next_text) < lane.checkpointed_offset
                or next_prefix != durable_prefix
            ):
                lane.rewrite_revision += 1
                lane.requires_snapshot_checkpoint = True
        lane.frame = _copy_frame(frame)
        lane.latest_source_seq = max(lane.latest_source_seq, source_seq)
        if next_text != lane.text:
            lane.text = next_text
            # Team Mission activity deltas are persisted fragment-by-fragment
            # in the canonical mission activity journal before fan-out.  A
            # second snapshot/checkpoint path would create another producer
            # for the same visible text.  Keep the lane only for absolute
            # offset calculation; ordinary session streams still use durable
            # checkpoints for reconnect repair.
            if checkpoint_required:
                lane.dirty = True


def pending_checkpoints(
    conversation_session_id: str,
    *,
    run_id: str = "",
    exclude_event_types: set[str] | None = None,
    db: Any = None,
) -> list[PendingCheckpoint]:
    stable = str(conversation_session_id or "").strip()
    normalized_run_id = str(run_id or "").strip()
    scope = _db_scope(db)
    with _lock:
        lanes = [
            lane
            for lane in _lanes.values()
            if lane.key[0] == scope
            and lane.key[1] == stable
            and (not normalized_run_id or lane.key[2] == normalized_run_id)
            and (not exclude_event_types or lane.key[3] not in exclude_event_types)
            and lane.dirty
            and lane.text
        ]
        lanes.sort(key=lambda lane: lane.latest_source_seq)
        return [_checkpoint(lane) for lane in lanes]


def replay_snapshots(
    conversation_session_id: str,
    *,
    run_id: str = "",
    runtime_scope_key: str = "",
    active_run_ids: set[str] | None = None,
    db: Any = None,
) -> list[dict[str, Any]]:
    stable = str(conversation_session_id or "").strip()
    normalized_run_id = str(run_id or "").strip()
    scope_key = str(runtime_scope_key or "").strip()
    db_scope = _db_scope(db)
    with _lock:
        lanes = [
            lane
            for lane in _lanes.values()
            if lane.key[0] == db_scope
            and lane.key[1] == stable
            and lane.dirty
            and lane.text
            and (not normalized_run_id or lane.key[2] == normalized_run_id)
            and (not active_run_ids or lane.key[2] in active_run_ids)
            and (
                not scope_key
                or str(lane.frame.get("runtime_scope_key") or "").strip() == scope_key
            )
        ]
        lanes.sort(key=lambda lane: lane.latest_source_seq)
        return [_replay_snapshot_frame(lane) for lane in lanes]


def mark_checkpointed(checkpoints: list[PendingCheckpoint]) -> None:
    with _lock:
        for checkpoint in checkpoints:
            lane = _lanes.get(checkpoint.key)
            if lane is None:
                continue
            if lane.rewrite_revision == checkpoint.rewrite_revision:
                lane.checkpointed_offset = max(
                    lane.checkpointed_offset,
                    checkpoint.end_offset,
                )
                if checkpoint.mode == "snapshot":
                    lane.requires_snapshot_checkpoint = False
            else:
                lane.checkpointed_offset = 0
            lane.dirty = bool(
                lane.requires_snapshot_checkpoint
                or lane.checkpointed_offset < _utf16_length(lane.text)
            )


def clear_run(conversation_session_id: str, run_id: str, *, db: Any = None) -> None:
    stable = str(conversation_session_id or "").strip()
    normalized_run_id = str(run_id or "").strip()
    scope = _db_scope(db)
    if not stable or not normalized_run_id:
        return
    with _lock:
        for key in [
            key
            for key in _lanes
            if key[0] == scope and key[1] == stable and key[2] == normalized_run_id
        ]:
            _lanes.pop(key, None)


def reset_for_tests() -> None:
    with _lock:
        _lanes.clear()


def _lane_key(frame: dict[str, Any], *, db: Any) -> tuple[str, ...]:
    payload = frame.get("payload") if isinstance(frame.get("payload"), dict) else {}
    stable = _first_text(
        frame.get("conversation_session_id"),
        payload.get("conversation_session_id"),
        frame.get("session_id"),
    )
    run_id = _first_text(frame.get("run_id"), payload.get("run_id"), payload.get("runId"))
    lane_identity = _first_text(
        payload.get("subagent_id"),
        payload.get("subagentId"),
        payload.get("test_run_id"),
        payload.get("testRunId"),
        payload.get("stream_id"),
        payload.get("streamId"),
        payload.get("segment_id"),
        payload.get("segmentId"),
        payload.get("client_message_id"),
        payload.get("clientMessageId"),
        frame.get("activity_id"),
        "default",
    )
    return (
        _db_scope(db),
        stable,
        run_id,
        str(frame.get("type") or "").strip(),
        lane_identity,
    )


def _checkpoint(lane: _StreamLane) -> PendingCheckpoint:
    end_offset = _utf16_length(lane.text)
    mode = "snapshot" if lane.requires_snapshot_checkpoint else "append"
    offset = 0 if mode == "snapshot" else lane.checkpointed_offset
    text = lane.text if mode == "snapshot" else _slice_utf16(lane.text, offset, None)
    frame = _stream_frame(lane, text=text, mode=mode, offset=offset)
    payload = frame.get("payload") if isinstance(frame.get("payload"), dict) else {}
    payload["stream_checkpoint"] = True
    payload.pop("replay_snapshot", None)
    frame["payload"] = payload
    frame.pop("seq", None)
    frame.pop("transient", None)
    return PendingCheckpoint(
        key=lane.key,
        frame=frame,
        end_offset=end_offset,
        rewrite_revision=lane.rewrite_revision,
        mode=mode,
    )


def _replay_snapshot_frame(lane: _StreamLane) -> dict[str, Any]:
    frame = _stream_frame(lane, text=lane.text, mode="snapshot", offset=0)
    payload = frame.get("payload") if isinstance(frame.get("payload"), dict) else {}
    payload["replay_snapshot"] = True
    payload.pop("stream_checkpoint", None)
    frame["payload"] = payload
    frame.pop("seq", None)
    frame["transient"] = True
    return frame


def _stream_frame(
    lane: _StreamLane,
    *,
    text: str,
    mode: str,
    offset: int,
) -> dict[str, Any]:
    frame = _copy_frame(lane.frame)
    payload = frame.get("payload") if isinstance(frame.get("payload"), dict) else {}
    payload.update({"mode": mode, "text": text, "delta": text, "offset": offset})
    if "output" in payload:
        payload["output"] = text
    if mode == "snapshot":
        payload["snapshot"] = text
    else:
        payload.pop("snapshot", None)
    text_stream = dict(
        frame.get("text_stream")
        if isinstance(frame.get("text_stream"), dict)
        else payload.get("text_stream")
        if isinstance(payload.get("text_stream"), dict)
        else {}
    )
    text_stream.update(
        {
            "event": str(frame.get("type") or ""),
            "mode": mode,
            "text": text,
            "delta": text,
            "offset": offset,
        }
    )
    frame["text_stream"] = text_stream
    payload["text_stream"] = dict(text_stream)
    frame["payload"] = payload
    if lane.latest_source_seq:
        frame["runtime_source_seq"] = lane.latest_source_seq
    return frame


def _merge_text(existing: str, incoming: str, *, mode: str, offset: int | None) -> str:
    if mode in {"snapshot", "replace", "cumulative"}:
        return incoming
    if offset is None:
        return existing + incoming
    prefix = _slice_utf16(existing, 0, offset)
    existing_fragment = _slice_utf16(existing, offset, offset + _utf16_length(incoming))
    if existing_fragment == incoming:
        return existing
    if _utf16_length(prefix) < offset:
        return existing
    suffix = _slice_utf16(existing, offset + _utf16_length(incoming), None)
    return prefix + incoming + suffix


def _slice_utf16(value: str, start: int, end: int | None) -> str:
    raw = str(value or "").encode("utf-16-le")
    start_byte = max(0, start) * 2
    end_byte = len(raw) if end is None else max(start, end) * 2
    return raw[start_byte:end_byte].decode("utf-16-le", errors="ignore")


def _utf16_length(value: str) -> int:
    return len(str(value or "").encode("utf-16-le")) // 2


def _copy_frame(frame: dict[str, Any]) -> dict[str, Any]:
    copied = dict(frame)
    copied["payload"] = dict(frame.get("payload") or {}) if isinstance(frame.get("payload"), dict) else {}
    if isinstance(frame.get("text_stream"), dict):
        copied["text_stream"] = dict(frame["text_stream"])
    return copied


def _db_scope(db: Any) -> str:
    return f"db:{id(db)}" if db is not None else "db:none"


def _first_text(*values: Any) -> str:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _first_raw_text(*values: Any) -> str:
    for value in values:
        if value is not None and str(value) != "":
            return str(value)
    return ""


def _optional_int(*values: Any) -> int | None:
    for value in values:
        if value is None or value == "":
            continue
        try:
            return max(0, int(value))
        except (TypeError, ValueError):
            continue
    return None
