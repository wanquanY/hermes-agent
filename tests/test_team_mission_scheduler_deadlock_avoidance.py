import asyncio
import threading
import time

from tui_gateway.services import run_control


def test_dispatch_team_mission_ready_scheduler_returns_immediately_in_caller_thread(monkeypatch):
    callback_started = threading.Event()
    callback_release = threading.Event()
    callback_finished = threading.Event()

    def callback(**_kwargs):
        callback_started.set()
        callback_release.wait(timeout=2)
        callback_finished.set()

    monkeypatch.setattr(run_control, "_team_mission_ready_scheduler", callback)

    started_at = time.perf_counter()
    run_control._dispatch_team_mission_ready_scheduler(mission_id=" mission-1 ")
    elapsed = time.perf_counter() - started_at

    try:
        assert elapsed < 0.25
        assert callback_started.wait(timeout=2)
    finally:
        callback_release.set()
    assert callback_finished.wait(timeout=2)


def test_callback_runs_in_separate_thread(monkeypatch):
    callback_thread_id = {}
    callback_finished = threading.Event()
    caller_thread_id = threading.get_ident()

    def callback(**_kwargs):
        callback_thread_id["value"] = threading.get_ident()
        callback_finished.set()

    monkeypatch.setattr(run_control, "_team_mission_ready_scheduler", callback)

    run_control._dispatch_team_mission_ready_scheduler(mission_id="mission-2")

    assert callback_finished.wait(timeout=2)
    assert callback_thread_id["value"] != caller_thread_id


def test_callback_receives_correct_mission_id_db_trigger_run_id(monkeypatch):
    received = {}
    callback_finished = threading.Event()
    db = object()

    def callback(**kwargs):
        received.update(kwargs)
        callback_finished.set()

    monkeypatch.setattr(run_control, "_team_mission_ready_scheduler", callback)

    run_control._dispatch_team_mission_ready_scheduler(
        mission_id=" mission-3 ",
        db=db,
        trigger_event="message.complete",
        run_id="run-3",
    )

    assert callback_finished.wait(timeout=2)
    assert received == {
        "mission_id": "mission-3",
        "db": db,
        "trigger_event": "message.complete",
        "run_id": "run-3",
    }


def test_callback_exception_does_not_propagate_to_caller(monkeypatch):
    callback_called = threading.Event()

    def callback(**_kwargs):
        callback_called.set()
        raise RuntimeError("scheduler failed")

    monkeypatch.setattr(run_control, "_team_mission_ready_scheduler", callback)

    run_control._dispatch_team_mission_ready_scheduler(mission_id="mission-4")

    assert callback_called.wait(timeout=2)


def test_missing_callback_does_nothing(monkeypatch):
    monkeypatch.setattr(run_control, "_team_mission_ready_scheduler", None)

    run_control._dispatch_team_mission_ready_scheduler(
        mission_id="mission-5",
        db=object(),
        trigger_event="message.complete",
        run_id="run-5",
    )


def test_callback_invoked_from_simulated_worker_runtime_loop_does_not_deadlock(monkeypatch):
    callback_finished = threading.Event()
    caller_loop = {}
    observed = {}

    def callback(**_kwargs):
        try:
            running_loop = asyncio.get_running_loop()
        except RuntimeError:
            observed["running_loop"] = None
        else:
            observed["running_loop"] = running_loop
        finally:
            callback_finished.set()

    monkeypatch.setattr(run_control, "_team_mission_ready_scheduler", callback)

    async def dispatch_from_running_loop():
        caller_loop["value"] = asyncio.get_running_loop()
        run_control._dispatch_team_mission_ready_scheduler(mission_id="mission-6")

    asyncio.run(dispatch_from_running_loop())

    assert callback_finished.wait(timeout=2)
    assert observed["running_loop"] is not caller_loop["value"]
