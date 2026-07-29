"""Byte-bounded pages for durable run-event replay.

Event count is not a transport bound: one tool event can be much larger than
hundreds of lifecycle events.  This module owns the wire-size contract used by
``events.subscribe`` catch-up and ``run.events`` reads.  Oversized individual
events are streamed as stateless base64 fragments so every JSON-RPC response
remains bounded without truncating the canonical event.
"""

from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass
from typing import Any


REPLAY_PROTOCOL = "paged_v1"
DEFAULT_REPLAY_PAGE_BYTES = 512 * 1024
MIN_REPLAY_PAGE_BYTES = 64 * 1024
MAX_REPLAY_PAGE_BYTES = 2 * 1024 * 1024
_RESULT_ENVELOPE_RESERVE_BYTES = 4 * 1024
_FRAGMENT_RAW_BUDGET_RATIO = 0.60


class ReplayFragmentError(ValueError):
    """The client supplied a stale or invalid fragmented-event continuation."""


@dataclass(frozen=True)
class ReplayPage:
    events: list[dict[str, Any]]
    after_seq: int
    next_after_seq: int
    replay_until_seq: int
    has_more: bool
    max_bytes: int
    fragment: dict[str, Any] | None = None

    def as_result(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "replay_protocol": REPLAY_PROTOCOL,
            "events": self.events,
            "after_seq": self.after_seq,
            "last_event_seq": self.next_after_seq,
            "next_after_seq": self.next_after_seq,
            "replay_until_seq": self.replay_until_seq,
            "has_more": self.has_more,
            "max_bytes": self.max_bytes,
        }
        if self.fragment is not None:
            result["fragment"] = self.fragment
        return result


def bounded_replay_page_bytes(value: Any) -> int:
    try:
        parsed = int(value or DEFAULT_REPLAY_PAGE_BYTES)
    except (TypeError, ValueError):
        parsed = DEFAULT_REPLAY_PAGE_BYTES
    return max(MIN_REPLAY_PAGE_BYTES, min(parsed, MAX_REPLAY_PAGE_BYTES))


def event_json_bytes(event: dict[str, Any]) -> bytes:
    return json.dumps(
        event,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")


def build_replay_page(
    events: list[dict[str, Any]],
    *,
    after_seq: int,
    replay_until_seq: int,
    source_has_more: bool,
    max_bytes: Any = None,
    fragment_seq: int = 0,
    fragment_offset: int = 0,
    fragment_id: str = "",
) -> ReplayPage:
    """Build one transport-safe replay page from an ordered event window.

    ``events`` must start immediately after ``after_seq`` in the caller's
    filtered replay domain.  ``source_has_more`` says the database query found
    another event beyond the supplied window.
    """

    bounded_after = max(0, int(after_seq or 0))
    bounded_until = max(bounded_after, int(replay_until_seq or 0))
    page_bytes = bounded_replay_page_bytes(max_bytes)
    normalized = [
        event
        for event in events
        if isinstance(event, dict)
        and bounded_after < int(event.get("seq") or 0) <= bounded_until
    ]
    if fragment_seq or fragment_offset or fragment_id:
        return _fragment_page(
            normalized,
            after_seq=bounded_after,
            replay_until_seq=bounded_until,
            source_has_more=source_has_more,
            max_bytes=page_bytes,
            fragment_seq=max(0, int(fragment_seq or 0)),
            fragment_offset=max(0, int(fragment_offset or 0)),
            fragment_id=str(fragment_id or "").strip(),
        )
    if not normalized:
        return ReplayPage(
            events=[],
            after_seq=bounded_after,
            next_after_seq=bounded_until,
            replay_until_seq=bounded_until,
            has_more=False,
            max_bytes=page_bytes,
        )

    first_bytes = event_json_bytes(normalized[0])
    if len(first_bytes) + _RESULT_ENVELOPE_RESERVE_BYTES > page_bytes:
        return _event_fragment(
            normalized[0],
            serialized=first_bytes,
            after_seq=bounded_after,
            replay_until_seq=bounded_until,
            source_has_more=source_has_more or len(normalized) > 1,
            max_bytes=page_bytes,
            fragment_offset=0,
        )

    selected: list[dict[str, Any]] = []
    used = _RESULT_ENVELOPE_RESERVE_BYTES
    for event in normalized:
        serialized = event_json_bytes(event)
        item_bytes = len(serialized) + 1
        if selected and used + item_bytes > page_bytes:
            break
        if not selected and used + item_bytes > page_bytes:
            return _event_fragment(
                event,
                serialized=serialized,
                after_seq=bounded_after,
                replay_until_seq=bounded_until,
                source_has_more=source_has_more or len(normalized) > 1,
                max_bytes=page_bytes,
                fragment_offset=0,
            )
        selected.append(event)
        used += item_bytes

    last_seq = max(int(event.get("seq") or 0) for event in selected)
    has_more = len(selected) < len(normalized) or source_has_more
    return ReplayPage(
        events=selected,
        after_seq=bounded_after,
        next_after_seq=last_seq if has_more else bounded_until,
        replay_until_seq=bounded_until,
        has_more=has_more,
        max_bytes=page_bytes,
    )


def _fragment_page(
    events: list[dict[str, Any]],
    *,
    after_seq: int,
    replay_until_seq: int,
    source_has_more: bool,
    max_bytes: int,
    fragment_seq: int,
    fragment_offset: int,
    fragment_id: str,
) -> ReplayPage:
    if not events:
        raise ReplayFragmentError("fragment event is no longer available")
    event = events[0]
    event_seq = int(event.get("seq") or 0)
    if event_seq != fragment_seq:
        raise ReplayFragmentError(
            f"fragment sequence changed: expected {fragment_seq}, received {event_seq}"
        )
    serialized = event_json_bytes(event)
    expected_id = _fragment_identity(event_seq, serialized)
    if fragment_id and fragment_id != expected_id:
        raise ReplayFragmentError("fragment identity changed")
    if fragment_offset >= len(serialized):
        raise ReplayFragmentError("fragment offset is outside the event payload")
    return _event_fragment(
        event,
        serialized=serialized,
        after_seq=after_seq,
        replay_until_seq=replay_until_seq,
        source_has_more=source_has_more or len(events) > 1,
        max_bytes=max_bytes,
        fragment_offset=fragment_offset,
    )


def _event_fragment(
    event: dict[str, Any],
    *,
    serialized: bytes,
    after_seq: int,
    replay_until_seq: int,
    source_has_more: bool,
    max_bytes: int,
    fragment_offset: int,
) -> ReplayPage:
    event_seq = int(event.get("seq") or 0)
    raw_budget = max(
        1024,
        int((max_bytes - _RESULT_ENVELOPE_RESERVE_BYTES) * _FRAGMENT_RAW_BUDGET_RATIO),
    )
    end = min(len(serialized), fragment_offset + raw_budget)
    done = end >= len(serialized)
    identity = _fragment_identity(event_seq, serialized)
    fragment = {
        "id": identity,
        "event_seq": event_seq,
        "encoding": "base64-json-utf8",
        "offset": fragment_offset,
        "next_offset": end,
        "total_bytes": len(serialized),
        "data": base64.b64encode(serialized[fragment_offset:end]).decode("ascii"),
        "done": done,
    }
    has_more = not done or source_has_more
    return ReplayPage(
        events=[],
        after_seq=after_seq,
        next_after_seq=event_seq if done else after_seq,
        replay_until_seq=replay_until_seq,
        has_more=has_more,
        max_bytes=max_bytes,
        fragment=fragment,
    )


def _fragment_identity(event_seq: int, serialized: bytes) -> str:
    digest = hashlib.sha256(serialized).hexdigest()
    return f"run-event:{event_seq}:{digest}"


__all__ = [
    "DEFAULT_REPLAY_PAGE_BYTES",
    "REPLAY_PROTOCOL",
    "ReplayFragmentError",
    "ReplayPage",
    "bounded_replay_page_bytes",
    "build_replay_page",
    "event_json_bytes",
]
