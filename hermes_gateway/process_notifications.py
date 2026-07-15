"""Background process notification formatting and queue ownership."""

from __future__ import annotations


def format_gateway_process_notification(evt: dict) -> str | None:
    """Format a watch pattern event from completion_queue into a message."""
    evt_type = evt.get("type", "completion")
    _sid = evt.get("session_id", "unknown")
    _cmd = evt.get("command", "unknown")

    if evt_type == "watch_disabled":
        return f"[IMPORTANT: {evt.get('message', '')}]"

    if evt_type == "watch_match":
        _pat = evt.get("pattern", "?")
        _out = evt.get("output", "")
        _sup = evt.get("suppressed", 0)
        text = (
            f"[IMPORTANT: Background process {_sid} matched "
            f'watch pattern "{_pat}".\n'
            f"Command: {_cmd}\n"
            f"Matched output:\n{_out}"
        )
        if _sup:
            text += f"\n({_sup} earlier matches were suppressed by rate limit)"
        text += "]"
        return text

    if evt_type == "async_delegation":
        from tools.process_registry import format_process_notification

        return format_process_notification(evt)

    return None


def drain_gateway_watch_events(completion_queue) -> list[dict]:
    """Drain gateway-owned watch events without spinning on requeued events."""
    watch_events: list[dict] = []
    requeue: list[dict] = []
    while not completion_queue.empty():
        try:
            evt = completion_queue.get_nowait()
        except Exception:
            break
        evt_type = evt.get("type", "completion")
        if evt_type in {"watch_match", "watch_disabled"}:
            watch_events.append(evt)
        elif evt_type == "async_delegation":
            requeue.append(evt)
    for evt in requeue:
        completion_queue.put(evt)
    return watch_events
