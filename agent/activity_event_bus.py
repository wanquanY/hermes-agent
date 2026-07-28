"""Per-worker inbox for async activity completion events."""

from __future__ import annotations

import threading
from collections import OrderedDict
from typing import Any


class ActivityEventBus:
    """Per-worker inbox for activity.* events related to this conversation.

    Worker subscribes on boot. Main pushes via IPC (event frame,
    kind="activity"). Bus stores pending events keyed by activity_id. Next LLM
    context build pulls + clears.
    """

    def __init__(self) -> None:
        self._events: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._lock = threading.RLock()

    def push(self, event: dict) -> None:
        if not isinstance(event, dict):
            return
        activity_id = str(
            event.get("activity_id")
            or event.get("activityId")
            or event.get("id")
            or ""
        ).strip()
        if not activity_id:
            return
        item = dict(event)
        item["activity_id"] = activity_id
        with self._lock:
            self._events[activity_id] = item

    def drain(self) -> list[dict]:
        with self._lock:
            items = list(self._events.values())
            self._events.clear()
            return items

    def peek_count(self) -> int:
        with self._lock:
            return len(self._events)


_default_bus: ActivityEventBus | None = None
_default_bus_lock = threading.RLock()


def set_default_activity_event_bus(bus: ActivityEventBus | None) -> None:
    global _default_bus
    with _default_bus_lock:
        _default_bus = bus


def get_default_activity_event_bus() -> ActivityEventBus | None:
    with _default_bus_lock:
        return _default_bus
