"""RPC contracts for repository-backed run-event retention."""

from __future__ import annotations

from types import SimpleNamespace

from tui_gateway import server


def test_events_prune_uses_run_retention_component(monkeypatch):
    captured: dict[str, object] = {}

    class _Retention:
        def prune(self, **kwargs):
            captured.update(kwargs)
            return {"deleted_events": 7}

    db = SimpleNamespace(runs=SimpleNamespace(retention=_Retention()))
    monkeypatch.setattr(server, "_get_db", lambda: db)

    response = server.handle_request(
        {
            "id": "request-1",
            "method": "events.prune",
            "params": {
                "conversation_session_id": "stored-1",
                "retention_days": 21,
                "max_events_per_session": 8000,
            },
        }
    )

    assert captured == {
        "session_id": "stored-1",
        "retention_days": 21,
        "max_events_per_session": 8000,
    }
    assert response["result"] == {"deleted_events": 7}


def test_events_compact_uses_run_event_maintenance_component(monkeypatch):
    captured: dict[str, object] = {}

    class _Maintenance:
        def compact(self, **kwargs):
            captured.update(kwargs)
            return {"compacted_segments": 2, "deleted_events": 10}

    db = SimpleNamespace(run_event_maintenance=_Maintenance())
    monkeypatch.setattr(server, "_get_db", lambda: db)

    response = server.handle_request(
        {
            "id": "request-1",
            "method": "events.compact",
            "params": {
                "conversation_session_id": "stored-1",
                "vacuum": True,
            },
        }
    )

    assert captured == {"session_id": "stored-1", "vacuum": True}
    assert response["result"] == {"compacted_segments": 2, "deleted_events": 10}
