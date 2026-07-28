"""Goal 3 guard: team_mission_events is audit-only, not runtime replay."""

from __future__ import annotations

import inspect
import importlib


def test_runtime_activity_subscribe_does_not_read_team_mission_events():
    module = importlib.import_module("tui_gateway.services.team_mission_activity_events")
    source = inspect.getsource(module.list_activity_events)
    assert "list_team_mission_events" not in source
    assert "TeamMissionAuditLog" not in source
    assert 'source="team_mission_events"' not in source


def test_render_snapshot_watermarks_use_run_events_for_mission_activity():
    module = importlib.import_module("tui_gateway.methods.conversation_render_snapshot")
    source = inspect.getsource(module._team_activity_watermarks)
    assert "TeamMissionAuditLog" not in source
    assert "_team_mission_event_last_seq" not in source
    assert 'source="team_mission_events"' not in source
    assert 'source="run_events"' in source
