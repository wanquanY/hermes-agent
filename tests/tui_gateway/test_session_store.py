from __future__ import annotations

import logging

from tui_gateway.services.session_store import get_session_db_for_home


class _FakeSessionDB:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.maintenance_calls = 0

    def maybe_auto_compact_run_events(self):
        self.maintenance_calls += 1
        return {"skipped": False, "deleted_events": 2}


def test_get_session_db_for_home_runs_startup_run_event_maintenance(tmp_path):
    created: list[_FakeSessionDB] = []

    def factory(**kwargs):
        db = _FakeSessionDB(**kwargs)
        created.append(db)
        return db

    result = get_session_db_for_home(
        active_home=tmp_path,
        default_home=tmp_path,
        default_db=None,
        default_error=None,
        db_by_home={},
        db_error_by_home={},
        logger=logging.getLogger("test-session-store"),
        session_db_factory=factory,
    )

    assert result.db is created[0]
    assert result.default_db is created[0]
    assert created[0].maintenance_calls == 1
