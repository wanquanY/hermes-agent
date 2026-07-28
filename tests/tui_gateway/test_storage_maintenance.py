from __future__ import annotations

import logging

from tui_gateway.services.storage_maintenance import StorageMaintenanceService


class _FakeSessionDB:
    def __init__(self, *, active_runs: list[dict[str, str]] | None = None) -> None:
        self.active_runs = active_runs or []
        self.calls: list[str] = []
        self.startup_vacuum_values: list[bool] = []
        self.run_event_maintenance = _FakeRunEventMaintenance(self)
        self.runs = _FakeRuns(self)
        self.maintenance = _FakeStorageMaintenance(self)


class _FakeRunEventMaintenance:
    def __init__(self, owner: _FakeSessionDB) -> None:
        self.owner = owner

    def maybe_auto_compact(self, *, vacuum: bool = True) -> dict[str, int | bool]:
        self.owner.calls.append("maybe_auto_compact")
        self.owner.startup_vacuum_values.append(vacuum)
        return {"skipped": False, "deleted_events": 3}

    def compact(self) -> dict[str, int]:
        self.owner.calls.append("compact_run_events")
        return {"deleted_events": 2, "compacted_segments": 1}

    def prune_duplicate_session_info(self) -> dict[str, int]:
        self.owner.calls.append("prune_duplicate_session_info_events")
        return {"deleted_events": 1}

    def backfill_frames(self) -> dict[str, int]:
        self.owner.calls.append("backfill_run_event_frame_blobs")
        return {"updated_events": 1, "remaining_events": 0}

    def reference_payloads(self) -> dict[str, int]:
        self.owner.calls.append("reference_run_event_payloads")
        return {"referenced_events": 1, "skipped_events": 0}


class _FakeRunRetention:
    def __init__(self, owner: _FakeSessionDB) -> None:
        self.owner = owner

    def prune(self) -> dict[str, int]:
        self.owner.calls.append("prune_run_events")
        return {"deleted_events": 1}


class _FakeRuns:
    def __init__(self, owner: _FakeSessionDB) -> None:
        self.owner = owner
        self.retention = _FakeRunRetention(owner)

    def list(self, *, statuses: list[str], limit: int) -> list[dict[str, str]]:
        self.owner.calls.append(f"list_runs:{limit}:{','.join(statuses)}")
        return self.owner.active_runs[:limit]


class _FakeStorageMaintenance:
    def __init__(self, owner: _FakeSessionDB) -> None:
        self.owner = owner

    def vacuum(self) -> None:
        self.owner.calls.append("vacuum")


def test_storage_maintenance_runs_registered_db_tasks_in_order(tmp_path):
    db = _FakeSessionDB()
    service = StorageMaintenanceService(
        logger=logging.getLogger("test-storage-maintenance"),
        interval_seconds=3600,
    )

    key = service.register_db(db, profile_home=tmp_path)
    result = service.run_once()

    assert result["skipped"] is False
    assert result["target_count"] == 1
    assert result["targets"][0]["key"] == key
    assert db.calls[0:5] == [
        "compact_run_events",
        "prune_duplicate_session_info_events",
        "backfill_run_event_frame_blobs",
        "reference_run_event_payloads",
        "prune_run_events",
    ]

    tasks = result["targets"][0]["tasks"]
    assert [task["task"] for task in tasks] == [
        "compact_run_events",
        "prune_duplicate_session_info_events",
        "backfill_run_event_frame_blobs",
        "reference_run_event_payloads",
        "prune_run_events",
        "vacuum",
    ]
    assert tasks[0]["status"] == "completed"
    assert tasks[1]["status"] == "completed"
    assert tasks[2]["status"] == "completed"
    assert tasks[3]["status"] == "completed"
    assert tasks[4]["status"] == "completed"
    assert tasks[5]["status"] == "skipped"
    assert tasks[5]["result"]["reason"] == "below_threshold"

    call_count = len(db.calls)
    second = service.run_once()
    second_tasks = second["targets"][0]["tasks"]
    assert [task["status"] for task in second_tasks] == [
        "skipped",
        "skipped",
        "skipped",
        "skipped",
        "skipped",
        "skipped",
    ]
    assert {task["result"]["reason"] for task in second_tasks} == {"not_due"}
    assert len(db.calls) == call_count


def test_storage_maintenance_skips_all_tasks_when_disabled(monkeypatch):
    monkeypatch.setenv("HERMES_STORAGE_MAINTENANCE_DISABLED", "1")
    db = _FakeSessionDB()
    service = StorageMaintenanceService(interval_seconds=3600)
    service.register_db(db)

    result = service.run_once()

    assert result == {"skipped": True, "reason": "disabled", "targets": []}
    assert db.calls == []


def test_storage_maintenance_startup_cycle_uses_existing_compaction_hook():
    db = _FakeSessionDB()
    service = StorageMaintenanceService(interval_seconds=3600)

    result = service.run_startup_cycle(db)

    assert result == {"skipped": False, "deleted_events": 3}
    assert db.calls == ["maybe_auto_compact"]
    assert db.startup_vacuum_values == [False]


def test_storage_maintenance_status_rpc_reports_service_state(monkeypatch):
    from tui_gateway import server
    import tui_gateway.methods.system  # noqa: F401
    import tui_gateway.services.storage_maintenance as storage_maintenance

    class _FakeService:
        running = True

        def last_results(self):
            return [{"task": "compact_run_events", "status": "completed"}]

    monkeypatch.setattr(storage_maintenance, "get_storage_maintenance_service", lambda: _FakeService())
    monkeypatch.setattr(storage_maintenance, "storage_maintenance_disabled", lambda: False)

    response = server._methods["storage.maintenance.status"](1, {})

    assert response["result"]["disabled"] is False
    assert response["result"]["running"] is True
    assert response["result"]["lastResults"][0]["task"] == "compact_run_events"


def test_storage_maintenance_run_rpc_registers_current_db(monkeypatch):
    from tui_gateway import server
    import tui_gateway.methods.system  # noqa: F401
    import tui_gateway.services.storage_maintenance as storage_maintenance

    db = _FakeSessionDB()

    class _FakeService:
        def run_once(self, *, force: bool = False):
            return {"skipped": False, "forced": force, "target_count": 1}

    captured = {}

    def fake_register(registered_db, *, profile_home="", start=True):
        captured["db"] = registered_db
        captured["profile_home"] = profile_home
        captured["start"] = start
        return _FakeService()

    monkeypatch.setattr(server, "_db", db)
    monkeypatch.setattr(storage_maintenance, "storage_maintenance_disabled", lambda: False)
    monkeypatch.setattr(storage_maintenance, "register_session_db_for_maintenance", fake_register)

    response = server._methods["storage.maintenance.run"](1, {"force": "false"})

    assert response["result"] == {"skipped": False, "forced": False, "target_count": 1}
    assert captured["db"] is db
    assert captured["start"] is True
