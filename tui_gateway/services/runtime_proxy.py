from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import socket
import subprocess
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

_log = logging.getLogger(__name__)

_RUNTIME_PROXY_CONTROL_METHODS = frozenset(
    {
        "approval.pending.list",
        "approval.policy.get",
        "approval.policy.set",
        "approval.respond",
        "artifacts.list",
        "clarify.respond",
        "cron.manage",
        "events.prune",
        "events.subscribe",
        "events.unsubscribe",
        "model.options",
        "profile.prepare_runtime",
        "runtime.ensure",
        "run.cancel",
        "run.events",
        "run.fail",
        "run.list",
        "run.reserve",
        "run.status",
        "runtime.status",
        "secret.respond",
        "session.create",
        "session.delete",
        "session.list",
        "session.messages",
        "session.status",
        "session.usage",
        "skills.list",
        "skills.manage",
        "skills.reload",
        "sudo.respond",
        "toolsets.list",
        "tools.configure",
        "tools.prepare",
        "workspace.current",
        "workspace.list",
    }
)
_RUNTIME_SCOPED_CONTROL_METHODS = frozenset(
    {
        "clarify.respond",
        "cron.manage",
        "run.cancel",
        "secret.respond",
        "session.create",
        "sudo.respond",
    }
)
_RUNTIME_CONNECT_ATTEMPTS = 40
_RUNTIME_CONNECT_DELAY_S = 0.05
_DEFAULT_IDLE_TIMEOUT_S = 30 * 60


class AsyncFrameTransport(Protocol):
    async def write_async(self, obj: dict) -> bool: ...


@dataclass(frozen=True)
class RuntimeScope:
    agent_profile_id: str = ""
    agent_profile_version_id: str = ""
    runtime_scope_key: str = ""
    hermes_home: str = ""

    @property
    def has_scope(self) -> bool:
        return bool(
            self.agent_profile_id
            or self.agent_profile_version_id
            or self.runtime_scope_key
        )


@dataclass
class RuntimeWorker:
    scope: RuntimeScope
    process: subprocess.Popen
    port: int
    token: str
    created_at: float
    last_started_at: float
    last_used_at: float
    restart_count: int = 0
    bridge_count: int = 0
    last_exit_at: float = 0
    last_error: str = ""

    @property
    def scope_key(self) -> str:
        return self.scope.runtime_scope_key

    def running(self) -> bool:
        return self.process.poll() is None

    def mark_used(self) -> None:
        self.last_used_at = time.time()

    def status(self) -> dict[str, Any]:
        running = self.running()
        return {
            "scopeKey": self.scope_key,
            "agentProfileId": self.scope.agent_profile_id or None,
            "agentProfileVersionId": self.scope.agent_profile_version_id or None,
            "hermesHome": self.scope.hermes_home or None,
            "pid": self.process.pid if running else None,
            "port": self.port if running else None,
            "running": running,
            "healthy": running,
            "bridgeCount": self.bridge_count,
            "createdAt": self.created_at,
            "lastStartedAt": self.last_started_at,
            "lastUsedAt": self.last_used_at,
            "lastExitAt": self.last_exit_at or None,
            "restartCount": self.restart_count,
            "lastError": self.last_error or None,
        }


def runtime_scope_from_params(params: dict[str, Any]) -> RuntimeScope:
    profile = params.get("doxie_profile")
    if not isinstance(profile, dict):
        profile = {}
    profile_id = str(
        params.get("agentProfileId")
        or params.get("agent_profile_id")
        or profile.get("id")
        or profile.get("agent_profile_id")
        or ""
    ).strip()
    version_id = str(
        params.get("agentProfileVersionId")
        or params.get("agent_profile_version_id")
        or params.get("versionId")
        or params.get("version_id")
        or profile.get("agentProfileVersionId")
        or profile.get("agent_profile_version_id")
        or ""
    ).strip()
    scope_key = str(
        params.get("runtimeScopeKey")
        or params.get("runtime_scope_key")
        or profile.get("runtimeScopeKey")
        or profile.get("runtime_scope_key")
        or ""
    ).strip()
    if not scope_key:
        if profile_id and version_id:
            scope_key = f"profile:{profile_id}:version:{version_id}"
        elif profile_id:
            scope_key = f"profile:{profile_id}"
    hermes_home = str(
        profile.get("hermesHomePath")
        or profile.get("hermes_home_path")
        or params.get("hermesHomePath")
        or params.get("hermes_home_path")
        or ""
    ).strip()
    return RuntimeScope(
        agent_profile_id=profile_id,
        agent_profile_version_id=version_id,
        runtime_scope_key=scope_key,
        hermes_home=hermes_home,
    )


def should_proxy_to_runtime(req: dict[str, Any]) -> bool:
    method = str((req or {}).get("method") or "").strip()
    if not method:
        return False
    params = req.get("params") if isinstance(req.get("params"), dict) else {}
    scope = runtime_scope_from_params(params)
    if not scope.has_scope:
        return False
    current_scope = str(os.environ.get("DOXIE_HERMES_RUNTIME_SCOPE_KEY") or "").strip()
    if current_scope and current_scope == scope.runtime_scope_key:
        return False
    if method == "session.create" and not (params.get("control_plane_only") or params.get("controlPlaneOnly")):
        return False
    if method not in _RUNTIME_PROXY_CONTROL_METHODS:
        return True
    return method in _RUNTIME_SCOPED_CONTROL_METHODS


def _reserve_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _idle_timeout_seconds() -> float:
    raw = os.environ.get("DOXIE_HERMES_RUNTIME_WORKER_IDLE_SECONDS", "").strip()
    if not raw:
        return _DEFAULT_IDLE_TIMEOUT_S
    try:
        return max(0.0, float(raw))
    except ValueError:
        return _DEFAULT_IDLE_TIMEOUT_S


class RuntimeWorkerPool:
    def __init__(self) -> None:
        self._workers: dict[str, RuntimeWorker] = {}
        self._lock = asyncio.Lock()

    async def ensure_worker(self, scope: RuntimeScope, params: dict[str, Any]) -> RuntimeWorker:
        if not scope.runtime_scope_key:
            raise RuntimeError("runtime scope key required")
        async with self._lock:
            await self._reclaim_idle_locked()
            existing = self._workers.get(scope.runtime_scope_key)
            if existing is not None and existing.running():
                existing.mark_used()
                return existing
            if existing is not None:
                existing.last_exit_at = time.time()
                self._workers.pop(scope.runtime_scope_key, None)
            worker = self._spawn_worker(scope, params)
            self._workers[scope.runtime_scope_key] = worker
            return worker

    async def retain_bridge(self, scope_key: str) -> None:
        async with self._lock:
            worker = self._workers.get(scope_key)
            if worker is not None:
                worker.bridge_count += 1
                worker.mark_used()

    async def release_bridge(self, scope_key: str) -> None:
        async with self._lock:
            worker = self._workers.get(scope_key)
            if worker is not None:
                worker.bridge_count = max(0, worker.bridge_count - 1)
                worker.mark_used()

    async def stop_worker(self, scope_key: str) -> bool:
        async with self._lock:
            worker = self._workers.pop(scope_key, None)
        if worker is None:
            return False
        await self._terminate_worker(worker)
        return True

    async def shutdown(self) -> None:
        async with self._lock:
            workers = list(self._workers.values())
            self._workers.clear()
        await asyncio.gather(
            *(self._terminate_worker(worker) for worker in workers),
            return_exceptions=True,
        )

    async def reclaim_idle(self) -> dict[str, Any]:
        async with self._lock:
            return await self._reclaim_idle_locked()

    async def ensure_worker_ready(self, scope: RuntimeScope, params: dict[str, Any]) -> RuntimeWorker:
        worker = await self.ensure_worker(scope, params)
        await self._probe_worker_ready(worker)
        worker.mark_used()
        return worker

    def snapshot(self) -> dict[str, Any]:
        workers = [worker.status() for worker in self._workers.values()]
        running = [item for item in workers if item["running"]]
        return {
            "source": "doxie-control-plane-runtime-pool",
            "workerCount": len(workers),
            "runningWorkerCount": len(running),
            "idleTimeoutSeconds": _idle_timeout_seconds(),
            "workers": sorted(workers, key=lambda item: str(item.get("scopeKey") or "")),
        }

    async def _probe_worker_ready(self, worker: RuntimeWorker) -> None:
        if not worker.running():
            raise RuntimeError(f"runtime worker {worker.scope_key} is not running")
        try:
            import websockets
        except Exception as exc:  # pragma: no cover - dependency is required for sidecar mode
            raise RuntimeError(f"Hermes runtime proxy requires websockets: {exc}") from exc

        uri = f"ws://127.0.0.1:{worker.port}/api/ws?token={worker.token}"
        last_exc: Exception | None = None
        for _ in range(_RUNTIME_CONNECT_ATTEMPTS):
            try:
                ws = await websockets.connect(uri)
                try:
                    ready_raw = await ws.recv()
                    try:
                        ready = json.loads(ready_raw)
                    except json.JSONDecodeError:
                        ready = {}
                    if ((ready.get("params") or {}).get("type")) != "gateway.ready":
                        raise RuntimeError("runtime worker did not emit gateway.ready")
                    return
                finally:
                    await ws.close()
            except Exception as exc:
                last_exc = exc
                await asyncio.sleep(_RUNTIME_CONNECT_DELAY_S)
                if not worker.running():
                    break
        raise RuntimeError(f"runtime worker ready probe failed: {last_exc}")

    def _spawn_worker(self, scope: RuntimeScope, params: dict[str, Any]) -> RuntimeWorker:
        if not scope.hermes_home:
            raise RuntimeError(f"doxie_profile.hermesHomePath required for runtime scope {scope.runtime_scope_key}")
        port = _reserve_loopback_port()
        token = secrets.token_urlsafe(24)
        env = os.environ.copy()
        profile = params.get("doxie_profile") if isinstance(params.get("doxie_profile"), dict) else {}
        profile_env = profile.get("env") if isinstance(profile.get("env"), dict) else {}
        env.update({str(k): str(v) for k, v in profile_env.items()})
        env["HERMES_HOME"] = scope.hermes_home
        env["DOXIE_HERMES_RUNTIME_SCOPE_KEY"] = scope.runtime_scope_key
        if scope.agent_profile_id:
            env["DOXIE_AGENT_PROFILE_ID"] = scope.agent_profile_id
        if scope.agent_profile_version_id:
            env["DOXIE_AGENT_PROFILE_VERSION_ID"] = scope.agent_profile_version_id
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "tui_gateway.doxie_sidecar",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--token",
                token,
            ],
            cwd=os.getcwd(),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        now = time.time()
        return RuntimeWorker(
            scope=scope,
            process=process,
            port=port,
            token=token,
            created_at=now,
            last_started_at=now,
            last_used_at=now,
        )

    async def _reclaim_idle_locked(self) -> dict[str, Any]:
        idle_timeout = _idle_timeout_seconds()
        if idle_timeout <= 0:
            return {"reclaimed": 0, "scopeKeys": [], "idleTimeoutSeconds": idle_timeout}
        timestamp = time.time()
        stale: list[RuntimeWorker] = []
        for scope_key, worker in list(self._workers.items()):
            if not worker.running():
                worker.last_exit_at = timestamp
                self._workers.pop(scope_key, None)
                continue
            if worker.bridge_count > 0:
                continue
            if timestamp - worker.last_used_at >= idle_timeout:
                stale.append(worker)
                self._workers.pop(scope_key, None)
        for worker in stale:
            await self._terminate_worker(worker)
        return {
            "reclaimed": len(stale),
            "scopeKeys": [worker.scope_key for worker in stale],
            "idleTimeoutSeconds": idle_timeout,
        }

    async def _terminate_worker(self, worker: RuntimeWorker) -> None:
        process = worker.process
        worker.last_exit_at = time.time()
        if process.poll() is not None:
            return
        try:
            process.terminate()
            await asyncio.to_thread(process.wait, 5)
        except Exception:
            try:
                process.kill()
            except Exception:
                pass


class RuntimeProxyBridge:
    def __init__(
        self,
        *,
        worker: RuntimeWorker,
        transport: AsyncFrameTransport,
        pool: RuntimeWorkerPool,
    ) -> None:
        self.worker = worker
        self.scope_key = worker.scope_key
        self.transport = transport
        self.pool = pool
        self._connect_lock = asyncio.Lock()
        self._send_lock = asyncio.Lock()
        self._ws: Any | None = None
        self._reader_task: asyncio.Task | None = None
        self._closed = False
        self._retained = False

    @property
    def closed(self) -> bool:
        return self._closed

    async def send(self, req: dict[str, Any]) -> None:
        await self._ensure_connected()
        async with self._send_lock:
            if self._ws is None:
                raise RuntimeError(f"runtime bridge {self.scope_key} is not connected")
            try:
                self.worker.mark_used()
                await self._ws.send(json.dumps(req, ensure_ascii=False))
            except Exception:
                await self.close()
                raise

    async def _ensure_connected(self) -> None:
        if self._is_connected():
            return
        async with self._connect_lock:
            if self._is_connected():
                return
            if not self.worker.running():
                raise RuntimeError(f"runtime worker {self.scope_key} is not running")
            try:
                import websockets
            except Exception as exc:  # pragma: no cover - dependency is required for sidecar mode
                raise RuntimeError(f"Hermes runtime proxy requires websockets: {exc}") from exc

            uri = f"ws://127.0.0.1:{self.worker.port}/api/ws?token={self.worker.token}"
            last_exc: Exception | None = None
            for _ in range(_RUNTIME_CONNECT_ATTEMPTS):
                try:
                    ws = await websockets.connect(uri)
                    ready_raw = await ws.recv()
                    try:
                        ready = json.loads(ready_raw)
                    except json.JSONDecodeError:
                        ready = {}
                    if ((ready.get("params") or {}).get("type")) != "gateway.ready":
                        await ws.close()
                        raise RuntimeError("runtime worker did not emit gateway.ready")
                    self._ws = ws
                    self._closed = False
                    if not self._retained:
                        await self.pool.retain_bridge(self.scope_key)
                        self._retained = True
                    self._reader_task = asyncio.create_task(self._read_loop())
                    return
                except Exception as exc:
                    last_exc = exc
                    await asyncio.sleep(_RUNTIME_CONNECT_DELAY_S)
                    if not self.worker.running():
                        break
            raise RuntimeError(f"runtime worker connection failed: {last_exc}")

    def _is_connected(self) -> bool:
        return (
            not self._closed
            and self._ws is not None
            and self._reader_task is not None
            and not self._reader_task.done()
        )

    async def _read_loop(self) -> None:
        try:
            while not self._closed and self._ws is not None:
                raw = await self._ws.recv()
                try:
                    frame = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if ((frame.get("params") or {}).get("type")) == "gateway.ready":
                    continue
                self.worker.mark_used()
                if not await self.transport.write_async(frame):
                    break
        except Exception as exc:
            if not self._closed:
                _log.debug("runtime bridge %s read failed: %s", self.scope_key, exc)
        finally:
            await self.close(cancel_reader=False)

    async def close(self, *, cancel_reader: bool = True) -> None:
        if self._closed and self._ws is None:
            return
        self._closed = True
        ws = self._ws
        self._ws = None
        if ws is not None:
            try:
                await ws.close()
            except Exception:
                pass
        task = self._reader_task
        if cancel_reader and task is not None and task is not asyncio.current_task() and not task.done():
            task.cancel()
        self._reader_task = None
        if self._retained:
            self._retained = False
            await self.pool.release_bridge(self.scope_key)


_runtime_proxy_pool = RuntimeWorkerPool()


def runtime_proxy_pool() -> RuntimeWorkerPool:
    return _runtime_proxy_pool


def ensure_runtime_ready_sync(params: dict[str, Any]) -> dict[str, Any]:
    scope = runtime_scope_from_params(params)
    worker = asyncio.run(runtime_proxy_pool().ensure_worker_ready(scope, params))
    return worker.status()


async def proxy_to_runtime(req: dict[str, Any], transport: Any) -> bool:
    if not should_proxy_to_runtime(req):
        return False
    params = req.get("params") if isinstance(req.get("params"), dict) else {}
    scope = runtime_scope_from_params(params)
    pool = runtime_proxy_pool()
    worker = await pool.ensure_worker(scope, params)
    bridge = await transport.runtime_bridge(worker)
    await bridge.send(req)
    return True
