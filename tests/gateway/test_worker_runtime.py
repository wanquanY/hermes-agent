"""Unit tests for ``tui_gateway.services.worker_runtime`` — Phase 5a.

Covers:
- ``is_primary_run_worker_mode`` env-flag matrix (primary / legacy /
  default / unknown one-time-warning)
- ``worker_supervisor`` / ``worker_frame_router`` singleton identity
- supervisor callbacks are bound to the router
- ``shutdown_run_worker_runtime`` is no-op when nothing spawned
- ``shutdown_run_worker_runtime`` clears singletons and calls
  ``supervisor.shutdown_all``
- ``_reset_for_tests`` clears singletons without touching subprocesses
"""

from __future__ import annotations

import asyncio
import logging
from unittest.mock import AsyncMock

import pytest

from tui_gateway.services import worker_runtime
from tui_gateway.services.worker_frame_router import WorkerFrameRouter
from tui_gateway.services.worker_supervisor import WorkerSupervisor


@pytest.fixture(autouse=True)
def _reset_singletons():
    worker_runtime._reset_for_tests()
    yield
    worker_runtime._reset_for_tests()


@pytest.mark.parametrize(
    "value, expected",
    [
        ("primary", True),
        ("PRIMARY", True),
        ("on", True),
        ("1", True),
        ("true", True),
        ("yes", True),
        ("", False),
        ("legacy", False),
        ("off", False),
        ("0", False),
        ("false", False),
        ("no", False),
    ],
)
def test_is_primary_run_worker_mode(monkeypatch, value, expected) -> None:
    monkeypatch.setenv("DOVIE_RUN_WORKER_MODE", value)
    assert worker_runtime.is_primary_run_worker_mode() is expected


def test_is_primary_default_unset(monkeypatch) -> None:
    monkeypatch.delenv("DOVIE_RUN_WORKER_MODE", raising=False)
    assert worker_runtime.is_primary_run_worker_mode() is False


def test_unknown_value_warns_once_and_treats_as_legacy(monkeypatch, caplog) -> None:
    monkeypatch.setenv("DOVIE_RUN_WORKER_MODE", "weird-value")
    with caplog.at_level(logging.WARNING, logger="tui_gateway.services.worker_runtime"):
        assert worker_runtime.is_primary_run_worker_mode() is False
        assert worker_runtime.is_primary_run_worker_mode() is False
    # Warning emitted exactly once.
    warnings = [r for r in caplog.records if "weird-value" in r.getMessage()]
    assert len(warnings) == 1


def test_singleton_identity() -> None:
    a = worker_runtime.worker_supervisor()
    b = worker_runtime.worker_supervisor()
    assert a is b
    assert isinstance(a, WorkerSupervisor)


def test_router_singleton_identity() -> None:
    a = worker_runtime.worker_frame_router()
    b = worker_runtime.worker_frame_router()
    assert a is b
    assert isinstance(a, WorkerFrameRouter)


def test_supervisor_callbacks_bound_to_router() -> None:
    sup = worker_runtime.worker_supervisor()
    router = worker_runtime.worker_frame_router()
    # The supervisor was constructed with router callbacks; verify by
    # poking at the private slots that the WorkerSupervisor stored
    # references to the same router methods.
    assert sup._on_event == router.on_event
    assert sup._on_interactive_request == router.on_interactive_request
    assert sup._on_run_terminal == router.on_run_terminal
    assert sup._on_log == router.on_log


@pytest.mark.asyncio
async def test_shutdown_noop_when_uninitialized() -> None:
    # Nothing constructed — must not raise.
    await worker_runtime.shutdown_run_worker_runtime()


@pytest.mark.asyncio
async def test_shutdown_clears_singletons_and_calls_shutdown_all() -> None:
    sup = worker_runtime.worker_supervisor()
    # Force the supervisor to record a shutdown_all call without actually
    # spawning anything by monkeypatching.
    sup.shutdown_all = AsyncMock()
    await worker_runtime.shutdown_run_worker_runtime()
    sup.shutdown_all.assert_awaited_once()
    # Singletons reset → next accessor returns a new instance.
    assert worker_runtime.worker_supervisor() is not sup


def test_reset_for_tests_drops_singletons() -> None:
    sup = worker_runtime.worker_supervisor()
    worker_runtime._reset_for_tests()
    assert worker_runtime.worker_supervisor() is not sup


# ── primary_dispatch (Phase 5c) ──────────────────────────────────────


class _RecordingTransport:
    def __init__(self) -> None:
        self.written: list[dict] = []

    async def write_async(self, frame: dict) -> bool:
        self.written.append(frame)
        return True


def _scoped_prompt_submit(text: str = "hi", **extra) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": "rpc-1",
        "method": "prompt.submit",
        "params": {
            "text": text,
            "stored_session_id": "sess-1",
            "runtime_scope_key": "profile:test",
            "agent_profile_id": "test",
            "dovie_profile": {
                "id": "test",
                "runtimeScopeKey": "profile:test",
                "hermesHomePath": "/tmp/test-hermes-home",
            },
            **extra,
        },
    }


@pytest.mark.asyncio
async def test_primary_dispatch_skips_non_submit_methods() -> None:
    transport = _RecordingTransport()
    handled = await worker_runtime.primary_dispatch(
        {"jsonrpc": "2.0", "id": 1, "method": "events.subscribe", "params": {}},
        transport,
    )
    assert handled is False
    assert transport.written == []


@pytest.mark.asyncio
async def test_primary_dispatch_intercepts_run_submit(monkeypatch) -> None:
    """frontend sends ``run.submit`` (not ``prompt.submit``) as the
    canonical chat entry — intercept it just like prompt.submit."""
    transport = _RecordingTransport()
    sent = []

    class _FakeSup:
        async def ensure(self, scope):
            return None

        async def send(self, scope_key, frame):
            sent.append((scope_key, frame))
            return True

    class _FakeRouter:
        def record_run_start(self, **kwargs):
            pass

        def forget_run(self, run_id):
            pass

    monkeypatch.setattr(worker_runtime, "worker_supervisor", lambda: _FakeSup())
    monkeypatch.setattr(worker_runtime, "worker_frame_router", lambda: _FakeRouter())

    req = _scoped_prompt_submit()
    req["method"] = "run.submit"
    handled = await worker_runtime.primary_dispatch(req, transport)
    assert handled is True
    assert len(sent) == 1
    assert transport.written[0]["result"]["status"] == "queued"


@pytest.mark.asyncio
async def test_primary_dispatch_skips_scopeless_prompt_submit() -> None:
    transport = _RecordingTransport()
    handled = await worker_runtime.primary_dispatch(
        {
            "jsonrpc": "2.0", "id": 1, "method": "prompt.submit",
            "params": {"text": "hi", "stored_session_id": "sess-1"},
        },
        transport,
    )
    # No runtime_scope_key / dovie_profile → scope.has_scope == False
    assert handled is False
    assert transport.written == []


@pytest.mark.asyncio
async def test_primary_dispatch_errors_when_no_stored_session() -> None:
    transport = _RecordingTransport()
    req = _scoped_prompt_submit()
    req["params"].pop("stored_session_id")
    handled = await worker_runtime.primary_dispatch(req, transport)
    assert handled is True
    assert len(transport.written) == 1
    assert transport.written[0]["error"]["code"] == 4006


@pytest.mark.asyncio
async def test_primary_dispatch_sends_run_start_and_acks(monkeypatch) -> None:
    """The supervisor and router are real singletons — stub the
    spawn-and-send side so we don't actually launch a subprocess."""
    transport = _RecordingTransport()
    sent_frames: list = []
    ensure_calls: list = []

    class _FakeSupervisor:
        async def ensure(self, scope):
            ensure_calls.append(scope)

        async def send(self, scope_key, frame):
            sent_frames.append((scope_key, frame))
            return True

    fake_sup = _FakeSupervisor()

    class _FakeRouter:
        def __init__(self):
            self.starts = []

        def record_run_start(self, *, scope_key, run_id, stored_session_id, turn_id):
            self.starts.append(
                {"scope_key": scope_key, "run_id": run_id,
                 "stored_session_id": stored_session_id, "turn_id": turn_id}
            )

        def forget_run(self, run_id):
            pass

    fake_router = _FakeRouter()
    monkeypatch.setattr(worker_runtime, "worker_supervisor", lambda: fake_sup)
    monkeypatch.setattr(worker_runtime, "worker_frame_router", lambda: fake_router)

    req = _scoped_prompt_submit(text="hello")
    handled = await worker_runtime.primary_dispatch(req, transport)

    assert handled is True
    assert len(ensure_calls) == 1
    assert ensure_calls[0].runtime_scope_key == "profile:test"
    assert len(sent_frames) == 1
    scope_key, frame = sent_frames[0]
    assert scope_key == "profile:test"
    from tui_gateway.run_worker import RunStartFrame
    assert isinstance(frame, RunStartFrame)
    assert frame.stored_session_id == "sess-1"
    assert frame.prompt == "hello"
    # Params include everything EXCEPT the keys we already lifted into
    # named fields.
    assert "text" not in frame.params
    assert "stored_session_id" not in frame.params
    # router recorded the run
    assert len(fake_router.starts) == 1
    assert fake_router.starts[0]["stored_session_id"] == "sess-1"
    # ack returned
    assert len(transport.written) == 1
    result = transport.written[0]["result"]
    assert result["status"] == "queued"
    assert result["source"] == "primary-run-worker"


@pytest.mark.asyncio
async def test_primary_dispatch_acks_error_when_send_fails(monkeypatch) -> None:
    transport = _RecordingTransport()
    forgot: list[str] = []

    class _FailingSupervisor:
        async def ensure(self, scope):
            return None

        async def send(self, scope_key, frame):
            return False

    class _Router:
        def record_run_start(self, **kwargs):
            pass

        def forget_run(self, run_id):
            forgot.append(run_id)

    monkeypatch.setattr(worker_runtime, "worker_supervisor", lambda: _FailingSupervisor())
    monkeypatch.setattr(worker_runtime, "worker_frame_router", lambda: _Router())

    req = _scoped_prompt_submit()
    handled = await worker_runtime.primary_dispatch(req, transport)
    assert handled is True
    assert transport.written[0]["error"]["code"] == 5022
    assert len(forgot) == 1  # router got cleaned up


@pytest.mark.asyncio
async def test_primary_dispatch_acks_error_when_ensure_raises(monkeypatch) -> None:
    transport = _RecordingTransport()

    class _BrokenSupervisor:
        async def ensure(self, scope):
            raise RuntimeError("spawn failed")

        async def send(self, scope_key, frame):
            return True

    class _Router:
        def record_run_start(self, **kwargs):
            pass

        def forget_run(self, run_id):
            pass

    monkeypatch.setattr(worker_runtime, "worker_supervisor", lambda: _BrokenSupervisor())
    monkeypatch.setattr(worker_runtime, "worker_frame_router", lambda: _Router())

    req = _scoped_prompt_submit()
    handled = await worker_runtime.primary_dispatch(req, transport)
    assert handled is True
    assert transport.written[0]["error"]["code"] == 5021
    assert "spawn failed" in transport.written[0]["error"]["message"]
