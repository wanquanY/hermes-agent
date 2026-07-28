"""Gateway run-event reads use the run application component."""

from __future__ import annotations

from types import SimpleNamespace

from tui_gateway.services.run_events import (
    list_activity_events,
    list_filtered_events,
    list_mission_activity_events,
    list_runtime_events,
    list_tool_events,
)


class _Runs:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple, dict]] = []

    def _record(self, name, args, options):
        self.calls.append((name, args, options))
        return [{"type": name}]

    def list_events(self, *args, **options):
        return self._record("runtime", args, options)

    def list_filtered_events(self, *args, **options):
        return self._record("filtered", args, options)

    def list_tool_events(self, *args, **options):
        return self._record("tool", args, options)

    def list_events_by_activity(self, *args, **options):
        return self._record("activity", args, options)

    def list_events_by_mission_activity(self, *args, **options):
        return self._record("mission", args, options)


def test_run_event_accessors_delegate_to_run_component():
    runs = _Runs()
    db = SimpleNamespace(runs=runs)

    assert list_runtime_events(db, "session-1", before_seq=9) == [
        {"type": "runtime"}
    ]
    assert list_filtered_events(db, "session-1", event_type_prefix="subagent.") == [
        {"type": "filtered"}
    ]
    assert list_tool_events(db, "session-1", after_seq=3) == [{"type": "tool"}]
    assert list_activity_events(db, "activity-1", limit=4) == [
        {"type": "activity"}
    ]
    assert list_mission_activity_events(db, "mission-1", reverse=True) == [
        {"type": "mission"}
    ]

    assert [call[0] for call in runs.calls] == [
        "runtime",
        "filtered",
        "tool",
        "activity",
        "mission",
    ]
    assert runs.calls[0][2]["before_seq"] == 9
    assert runs.calls[1][2]["event_type_prefix"] == "subagent."
    assert runs.calls[2][2]["after_seq"] == 3
    assert runs.calls[3][1] == ("activity-1",)
    assert runs.calls[4][2]["reverse"] is True
