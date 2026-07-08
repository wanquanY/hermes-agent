"""Unit tests for ``tui_gateway.services.worker_runtime``.

Covers:
- ``worker_supervisor`` / ``worker_frame_router`` singleton identity
- supervisor callbacks are bound to the router
- ``shutdown_run_worker_runtime`` is no-op when nothing spawned
- ``shutdown_run_worker_runtime`` clears singletons and calls
  ``supervisor.shutdown_all``
- ``_reset_for_tests`` clears singletons without touching subprocesses
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from hermes_team_mission.gateway import runtime_methods
from tui_gateway.services import worker_runtime
from tui_gateway.services.runtime_scope import RuntimeScope
from hermes_agent.orchestration.worker_frame_router import WorkerFrameRouter
from hermes_agent.orchestration.worker_supervisor import RunWorker, WorkerSupervisor


@pytest.fixture(autouse=True)
def _reset_singletons():
    worker_runtime._reset_for_tests()
    yield
    worker_runtime._reset_for_tests()


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


@pytest.mark.asyncio
async def test_team_mission_proxy_run_submit_uses_control_plane_transport(monkeypatch) -> None:
    loop = asyncio.get_running_loop()
    worker_runtime.remember_worker_runtime_loop(loop)
    captured: dict[str, object] = {}

    async def fake_primary_dispatch(req, transport):
        captured["req"] = req
        captured["transport"] = transport
        await transport.write_async(
            {"jsonrpc": "2.0", "id": req["id"], "result": {"status": "queued"}}
        )
        return True

    monkeypatch.setattr(worker_runtime, "primary_dispatch", fake_primary_dispatch)

    result = await asyncio.to_thread(
        runtime_methods._proxy_run_submit_via_worker,
        {
            "conversation_session_id": "team:mission-1:node:root",
            "run_id": "run-1",
            "turn_id": "turn-1",
            "runtime_scope_key": "profile:agent-default",
            "agent_profile_id": "agent-default",
        },
    )

    assert result == {"ok": True}
    assert isinstance(captured["transport"], worker_runtime.ControlPlaneTransport)
    assert captured["req"]["method"] == "run.submit"


# ── primary_dispatch (Phase 5c) ──────────────────────────────────────


class _RecordingTransport:
    def __init__(self) -> None:
        self.written: list[dict] = []

    async def write_async(self, frame: dict) -> bool:
        self.written.append(frame)
        return True


class _FakeProcess:
    def __init__(self) -> None:
        self.pid = 4242
        self.returncode = None


def _fake_worker(scope: RuntimeScope) -> RunWorker:
    return RunWorker(
        scope=scope,
        process=_FakeProcess(),
        inbound_queue=asyncio.Queue(),
        created_at=0.0,
        last_used_at=0.0,
    )


def _scoped_prompt_submit(text: str = "hi", **extra) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": "rpc-1",
        "method": "prompt.submit",
        "params": {
            "text": text,
            "conversation_session_id": "sess-1",
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
        async def ensure(self, scope, env_overrides=None):
            return _fake_worker(scope)

        async def send(self, scope_key, conversation_id, frame):
            sent.append((scope_key, conversation_id, frame))
            return True

    class _FakeRouter:
        def record_run_start(self, **kwargs):
            pass

        def forget_run(self, run_id):
            pass

    fake_sup = _FakeSup()
    monkeypatch.setattr(worker_runtime, "worker_supervisor", lambda: fake_sup)
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
            "params": {"text": "hi", "conversation_session_id": "sess-1"},
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
    req["params"].pop("conversation_session_id")
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
        async def ensure(self, scope, env_overrides=None):
            ensure_calls.append(scope)
            return _fake_worker(scope)

        async def send(self, scope_key, conversation_id, frame):
            sent_frames.append((scope_key, conversation_id, frame))
            return True

    fake_sup = _FakeSupervisor()

    class _FakeRouter:
        def __init__(self):
            self.starts = []

        def record_run_start(
            self,
            *,
            scope_key,
            conversation_id,
            run_id,
            conversation_session_id,
            turn_id,
        ):
            self.starts.append(
                {"scope_key": scope_key, "run_id": run_id,
                 "conversation_id": conversation_id,
                 "conversation_session_id": conversation_session_id, "turn_id": turn_id}
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
    assert ensure_calls[0].conversation_id == "sess-1"
    assert len(sent_frames) == 1
    scope_key, conversation_id, frame = sent_frames[0]
    assert scope_key == "profile:test"
    assert conversation_id == "sess-1"
    from tui_gateway.run_worker import RunStartFrame
    assert isinstance(frame, RunStartFrame)
    assert frame.conversation_session_id == "sess-1"
    assert frame.prompt == "hello"
    # Params include everything EXCEPT the keys we already lifted into
    # named fields.
    assert "text" not in frame.params
    assert "conversation_session_id" not in frame.params
    assert frame.params["runtime_scope_key"] == "profile:test"
    assert frame.params["conversation_id"] == "sess-1"
    # router recorded the run
    assert len(fake_router.starts) == 1
    assert fake_router.starts[0]["conversation_id"] == "sess-1"
    assert fake_router.starts[0]["conversation_session_id"] == "sess-1"
    # ack returned
    assert len(transport.written) == 1
    result = transport.written[0]["result"]
    assert result["status"] == "queued"
    assert result["source"] == "primary-run-worker"


@pytest.mark.asyncio
async def test_primary_dispatch_injects_session_workspace_context(monkeypatch, tmp_path) -> None:
    transport = _RecordingTransport()
    sent_frames: list = []
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()

    class _FakeSupervisor:
        async def ensure(self, scope, env_overrides=None):
            return _fake_worker(scope)

        async def send(self, scope_key, conversation_id, frame):
            sent_frames.append((scope_key, conversation_id, frame))
            return True

    class _FakeRouter:
        def record_run_start(self, **kwargs):
            pass

        def forget_run(self, run_id):
            pass

    def _workspace_context(session_id, params):
        assert session_id == "sess-1"
        assert params["conversation_session_id"] == "sess-1"
        return {
            "cwd": str(workspace_root),
            "workspace": {
                "id": "workspace-1",
                "name": "Workspace One",
                "path": str(workspace_root),
                "kind": "local",
            },
            "source": "session_workspace_binding",
        }

    fake_sup = _FakeSupervisor()
    monkeypatch.setattr(worker_runtime, "worker_supervisor", lambda: fake_sup)
    monkeypatch.setattr(worker_runtime, "worker_frame_router", lambda: _FakeRouter())
    monkeypatch.setattr(worker_runtime, "session_workspace_run_context", _workspace_context)

    handled = await worker_runtime.primary_dispatch(_scoped_prompt_submit(), transport)

    assert handled is True
    _scope_key, _conversation_id, frame = sent_frames[0]
    assert frame.params["cwd"] == str(workspace_root)
    assert frame.params["workspace"]["id"] == "workspace-1"
    assert "conversation_session_id" not in frame.params


@pytest.mark.asyncio
async def test_primary_dispatch_rejects_invalid_session_workspace(monkeypatch) -> None:
    transport = _RecordingTransport()

    class _Supervisor:
        async def ensure(self, scope, env_overrides=None):
            raise AssertionError("supervisor must not start for invalid workspace")

    monkeypatch.setattr(worker_runtime, "worker_supervisor", lambda: _Supervisor())
    monkeypatch.setattr(
        worker_runtime,
        "session_workspace_run_context",
        lambda _session_id, _params: (_ for _ in ()).throw(
            ValueError("cwd does not exist or is not a directory: /missing")
        ),
    )

    handled = await worker_runtime.primary_dispatch(_scoped_prompt_submit(), transport)

    assert handled is True
    assert transport.written[0]["error"]["code"] == 4002
    assert "/missing" in transport.written[0]["error"]["message"]


@pytest.mark.asyncio
async def test_primary_dispatch_acks_error_when_send_fails(monkeypatch) -> None:
    transport = _RecordingTransport()
    forgot: list[str] = []

    class _FailingSupervisor:
        async def ensure(self, scope, env_overrides=None):
            return _fake_worker(scope)

        async def send(self, scope_key, conversation_id, frame):
            return False

    class _Router:
        def record_run_start(self, **kwargs):
            pass

        def forget_run(self, run_id):
            forgot.append(run_id)

    fake_sup = _FailingSupervisor()
    monkeypatch.setattr(worker_runtime, "worker_supervisor", lambda: fake_sup)
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
        async def ensure(self, scope, env_overrides=None):
            raise RuntimeError("spawn failed")

        async def send(self, scope_key, conversation_id, frame):
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
