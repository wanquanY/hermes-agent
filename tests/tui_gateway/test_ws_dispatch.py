import asyncio
import json
import sys
import types

import pytest

from tui_gateway import ws
from tui_gateway.services import runtime_proxy


def test_workspace_current_uses_control_plane_executor():
    executor = ws._executor_for_request(  # noqa: SLF001
        {"id": "1", "method": "workspace.current", "params": {"session_id": "s"}}
    )

    assert executor is ws._ws_control_executor  # noqa: SLF001


def test_session_list_uses_control_plane_executor():
    executor = ws._executor_for_request(  # noqa: SLF001
        {"id": "1", "method": "session.list", "params": {}}
    )

    assert executor is ws._ws_control_executor  # noqa: SLF001


def test_run_status_uses_control_plane_executor():
    executor = ws._executor_for_request(  # noqa: SLF001
        {"id": "1", "method": "run.status", "params": {"stored_session_id": "s"}}
    )

    assert executor is ws._ws_control_executor  # noqa: SLF001


def test_events_unsubscribe_uses_control_plane_executor():
    executor = ws._executor_for_request(  # noqa: SLF001
        {"id": "1", "method": "events.unsubscribe", "params": {"subscription_id": "sub"}}
    )

    assert executor is ws._ws_control_executor  # noqa: SLF001


def test_prompt_submit_with_profile_scope_is_proxied_to_runtime_worker():
    assert runtime_proxy.should_proxy_to_runtime(
        {
            "id": "1",
            "method": "prompt.submit",
            "params": {
                "runtime_scope_key": "profile:agent-a",
                "doxie_profile": {
                    "id": "agent-a",
                    "hermesHomePath": "/tmp/hermes-agent-a",
                },
            },
        }
    )


def test_control_plane_session_list_is_not_proxied_to_runtime_worker():
    assert not runtime_proxy.should_proxy_to_runtime(
        {
            "id": "1",
            "method": "session.list",
            "params": {
                "runtime_scope_key": "profile:agent-a",
                "doxie_profile": {
                    "id": "agent-a",
                    "hermesHomePath": "/tmp/hermes-agent-a",
                },
            },
        }
    )


def test_profile_scoped_cron_manage_is_proxied_to_runtime_worker():
    assert runtime_proxy.should_proxy_to_runtime(
        {
            "id": "1",
            "method": "cron.manage",
            "params": {
                "action": "list",
                "doxie_profile": {
                    "id": "agent-a",
                    "runtimeScopeKey": "profile:agent-a",
                    "hermesHomePath": "/tmp/hermes-agent-a",
                },
            },
        }
    )


@pytest.mark.parametrize(
    "method",
    [
        "approval.pending.list",
        "approval.policy.get",
        "approval.policy.set",
        "approval.respond",
    ],
)
def test_profile_scoped_approval_methods_are_proxied_to_runtime_worker(method):
    assert runtime_proxy.should_proxy_to_runtime(
        {
            "id": "1",
            "method": method,
            "params": {
                "session_id": "stored-session-1",
                "doxie_profile": {
                    "id": "agent-a",
                    "runtimeScopeKey": "profile:agent-a",
                    "hermesHomePath": "/tmp/hermes-agent-a",
                },
            },
        }
    )


@pytest.mark.parametrize(
    "method",
    [
        "skills.reload",
        "tools.configure",
    ],
)
def test_profile_scoped_runtime_mutation_methods_are_proxied_to_runtime_worker(method):
    assert runtime_proxy.should_proxy_to_runtime(
        {
            "id": "1",
            "method": method,
            "params": {
                "session_id": "stored-session-1",
                "doxie_profile": {
                    "id": "agent-a",
                    "runtimeScopeKey": "profile:agent-a",
                    "hermesHomePath": "/tmp/hermes-agent-a",
                },
            },
        }
    )


def test_runtime_ensure_stays_on_control_plane():
    assert not runtime_proxy.should_proxy_to_runtime(
        {
            "id": "1",
            "method": "runtime.ensure",
            "params": {
                "runtime_scope_key": "profile:agent-a",
                "doxie_profile": {
                    "id": "agent-a",
                    "hermesHomePath": "/tmp/hermes-agent-a",
                },
            },
        }
    )


@pytest.mark.asyncio
async def test_runtime_proxy_keeps_bridge_open_for_streaming_events(monkeypatch):
    class FakeRuntimeSocket:
        def __init__(self):
            self.sent = []
            self.closed = False
            self.frames = asyncio.Queue()
            self.frames.put_nowait(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "method": "event",
                        "params": {"type": "gateway.ready"},
                    }
                )
            )

        async def send(self, raw):
            self.sent.append(json.loads(raw))

        async def recv(self):
            return await self.frames.get()

        async def close(self):
            self.closed = True

    class FakeClientSocket:
        def __init__(self):
            self.sent = []

        async def send_text(self, raw):
            self.sent.append(json.loads(raw))

    runtime_socket = FakeRuntimeSocket()

    async def fake_connect(_uri):
        return runtime_socket

    class FakeProcess:
        pid = 12345

        def poll(self):
            return None

    worker = runtime_proxy.RuntimeWorker(
        scope=runtime_proxy.RuntimeScope(
            agent_profile_id="agent-a",
            runtime_scope_key="profile:agent-a",
            hermes_home="/tmp/hermes-agent-a",
        ),
        process=FakeProcess(),
        port=19450,
        token="token",
        created_at=1,
        last_started_at=1,
        last_used_at=1,
    )

    class FakePool:
        async def ensure_worker(self, _scope, _params):
            return worker

        async def retain_bridge(self, scope_key):
            worker.bridge_count += 1

        async def release_bridge(self, scope_key):
            worker.bridge_count -= 1

    monkeypatch.setitem(sys.modules, "websockets", types.SimpleNamespace(connect=fake_connect))
    monkeypatch.setattr(runtime_proxy, "runtime_proxy_pool", lambda: FakePool())

    transport = ws.WSTransport(FakeClientSocket(), asyncio.get_running_loop())
    req = {
        "jsonrpc": "2.0",
        "id": "1",
        "method": "prompt.submit",
        "params": {
            "runtime_scope_key": "profile:agent-a",
            "doxie_profile": {
                "id": "agent-a",
                "hermesHomePath": "/tmp/hermes-agent-a",
            },
        },
    }

    assert await runtime_proxy.proxy_to_runtime(req, transport)
    assert runtime_socket.sent == [req]

    runtime_socket.frames.put_nowait(
        json.dumps({"jsonrpc": "2.0", "id": "1", "result": {"status": "streaming"}})
    )
    runtime_socket.frames.put_nowait(
        json.dumps(
            {
                "jsonrpc": "2.0",
                "method": "event",
                "params": {
                    "type": "message.delta",
                    "session_id": "s",
                    "payload": {"text": "hello"},
                },
            }
        )
    )
    for _ in range(20):
        if len(transport._ws.sent) >= 2:  # noqa: SLF001
            break
        await asyncio.sleep(0.01)

    assert transport._ws.sent == [  # noqa: SLF001
        {"jsonrpc": "2.0", "id": "1", "result": {"status": "streaming"}},
        {
            "jsonrpc": "2.0",
            "method": "event",
            "params": {
                "type": "message.delta",
                "session_id": "s",
                "payload": {"text": "hello"},
            },
        },
    ]
    assert not runtime_socket.closed

    await transport.aclose()
    assert runtime_socket.closed
    assert worker.bridge_count == 0


@pytest.mark.asyncio
async def test_runtime_worker_pool_reuses_and_reclaims_idle_workers(monkeypatch):
    class FakeProcess:
        next_pid = 20000

        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs
            self.pid = FakeProcess.next_pid
            FakeProcess.next_pid += 1
            self.terminated = False
            self.killed = False

        def poll(self):
            return 0 if self.terminated or self.killed else None

        def terminate(self):
            self.terminated = True

        def kill(self):
            self.killed = True

        def wait(self, _timeout=None):
            self.terminated = True
            return 0

    now = 1000.0

    monkeypatch.setattr(runtime_proxy.time, "time", lambda: now)
    monkeypatch.setattr(runtime_proxy.subprocess, "Popen", FakeProcess)
    monkeypatch.setattr(runtime_proxy, "_reserve_loopback_port", lambda: 21000)
    monkeypatch.setenv("DOXIE_HERMES_RUNTIME_WORKER_IDLE_SECONDS", "10")

    pool = runtime_proxy.RuntimeWorkerPool()
    scope = runtime_proxy.RuntimeScope(
        agent_profile_id="agent-a",
        runtime_scope_key="profile:agent-a",
        hermes_home="/tmp/hermes-agent-a",
    )
    params = {
        "doxie_profile": {
            "id": "agent-a",
            "hermesHomePath": "/tmp/hermes-agent-a",
            "env": {"FEISHU_APP_SECRET": "secret"},
        }
    }

    first = await pool.ensure_worker(scope, params)
    second = await pool.ensure_worker(scope, params)
    assert second is first
    assert first.process.kwargs["env"]["HERMES_HOME"] == "/tmp/hermes-agent-a"
    assert first.process.kwargs["env"]["FEISHU_APP_SECRET"] == "secret"

    now = 1012.0
    reclaimed = await pool.reclaim_idle()
    assert reclaimed["scopeKeys"] == ["profile:agent-a"]
    assert first.process.terminated
    assert pool.snapshot()["runningWorkerCount"] == 0


@pytest.mark.asyncio
async def test_runtime_worker_pool_does_not_reclaim_active_bridge(monkeypatch):
    class FakeProcess:
        pid = 22000

        def __init__(self, *args, **kwargs):
            self.terminated = False

        def poll(self):
            return 0 if self.terminated else None

        def terminate(self):
            self.terminated = True

        def wait(self, _timeout=None):
            self.terminated = True
            return 0

    now = 2000.0

    monkeypatch.setattr(runtime_proxy.time, "time", lambda: now)
    monkeypatch.setattr(runtime_proxy.subprocess, "Popen", FakeProcess)
    monkeypatch.setattr(runtime_proxy, "_reserve_loopback_port", lambda: 22000)
    monkeypatch.setenv("DOXIE_HERMES_RUNTIME_WORKER_IDLE_SECONDS", "10")

    pool = runtime_proxy.RuntimeWorkerPool()
    scope = runtime_proxy.RuntimeScope(
        agent_profile_id="agent-a",
        runtime_scope_key="profile:agent-a",
        hermes_home="/tmp/hermes-agent-a",
    )
    worker = await pool.ensure_worker(scope, {"doxie_profile": {"hermesHomePath": "/tmp/hermes-agent-a"}})
    await pool.retain_bridge(worker.scope_key)

    now = 2012.0
    reclaimed = await pool.reclaim_idle()
    assert reclaimed["reclaimed"] == 0
    assert worker.running()
    assert pool.snapshot()["runningWorkerCount"] == 1

    await pool.release_bridge(worker.scope_key)
    now = 2024.0
    reclaimed = await pool.reclaim_idle()
    assert reclaimed["reclaimed"] == 1
