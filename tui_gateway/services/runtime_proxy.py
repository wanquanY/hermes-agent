from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import secrets
import socket
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

_log = logging.getLogger(__name__)

_RUNTIME_PROXY_CONTROL_METHODS = frozenset(
    {
        "approval.pending.list",
        "approval.policy.get",
        "approval.policy.set",
        "approval.respond",
        "artifacts.list",
        "artifacts.prune",
        "clarify.respond",
        "conversation.activity.list",
        "conversation.render_snapshot",
        "cron.manage",
        "events.compact",
        "events.prune",
        "events.subscribe",
        "events.unsubscribe",
        "model.options",
        "profile.archive",
        "profile.draft.discard",
        "profile.draft.get",
        "profile.draft.list",
        "profile.draft.upsert",
        "profile.get",
        "profile.growth.summary",
        "profile.list",
        "profile.prepare_runtime",
        "profile.upsert",
        "runtime.ensure",
        "run.cancel",
        "run.events",
        "run.fail",
        "run.list",
        "run.reserve",
        "run.status",
        "runtime.status",
        "secret.respond",
        "subagent.events.list",
        "subagent.runs.list",
        "session.create",
        "session.delete",
        "session.list",
        "session.messages",
        "session.message_metadata.merge",
        "session.status",
        "session.title",
        "session.usage",
        "team_mission.conversation.delete",
        "team_mission.conversation.ensure",
        "team_mission.conversation.list",
        "team_mission.conversation.runtime_session_ids",
        "team_mission.message.submit",
        "team_mission.conversation.rename",
        "team_mission.conversation.render",
        "team_mission.conversation.resolve",
        "team_mission.events",
        "team_mission.graph",
        "team_mission.graph.reduce",
        "team_mission.node.history",
        "skills.list",
        "skills.manage",
        "skills.reload",
        "storage.stats",
        "sudo.respond",
        "toolsets.list",
        "tools.configure",
        "tools.prepare",
        "workspace.current",
        "workspace.session.bind",
        "workspace.session.current",
        "workspace.session.delete",
        "workspace.session.list",
        "workspace.list",
    }
)
_RUNTIME_SCOPED_CONTROL_METHODS = frozenset(
    {
        # Runtime-scoped writes and live runtime reads belong on the worker.
        # Persisted team mission state and graph reads must stay on the control
        # plane so restart recovery never depends on a dead worker port.
        "approval.pending.list",
        "approval.policy.get",
        "approval.policy.set",
        "approval.respond",
        "clarify.respond",
        "cron.manage",
        "events.subscribe",
        "events.unsubscribe",
        "run.cancel",
        "run.events",
        "run.list",
        "run.status",
        "secret.respond",
        "session.create",
        "skills.reload",
        "team_mission.node.update",
        "team_mission.plan.approve",
        "team_mission.plan.reject",
        "team_mission.cancel",
        "team_mission.schedule.ready",
        "toolsets.list",
        "sudo.respond",
        "tools.configure",
    }
)
_RUNTIME_CONNECT_ATTEMPTS = 40
_RUNTIME_CONNECT_DELAY_S = 0.05
_DEFAULT_IDLE_TIMEOUT_S = 30 * 60
_SIDECAR_TOKEN_ENV = "DOXIE_SIDECAR_TOKEN"
_SIDECAR_PARENT_PID_ENV = "DOXIE_SIDECAR_PARENT_PID"
_CONTROL_HOME_ENV = "DOXIE_HERMES_CONTROL_HOME"
_CRON_CONTROL_PLANE_READ_ACTIONS = frozenset({"", "list", "status", "runs"})
# Whole Team conversation writes are canonical control-plane methods. Leader
# execution is isolated by the lower run.submit runtime lease, not by proxying
# the enclosing conversation RPC into a scoped worker DB.
_TEAM_LEADER_RUNTIME_METHODS = frozenset()


class AsyncFrameTransport(Protocol):
    async def write_async(self, obj: dict) -> bool: ...


@dataclass(frozen=True)
class RuntimeScope:
    agent_profile_id: str = ""
    runtime_scope_key: str = ""
    hermes_home: str = ""

    @property
    def has_scope(self) -> bool:
        return bool(
            self.agent_profile_id
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
    launch_fingerprint: str = ""
    log_handle: Any | None = field(default=None, repr=False)
    failure_reported: bool = False

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
            "hermesHome": self.scope.hermes_home or None,
            "pid": self.process.pid if running else None,
            "port": self.port if running else None,
            "running": running,
            "healthy": running,
            "launchFingerprint": self.launch_fingerprint[:12] if self.launch_fingerprint else None,
            "bridgeCount": self.bridge_count,
            "createdAt": self.created_at,
            "lastStartedAt": self.last_started_at,
            "lastUsedAt": self.last_used_at,
            "lastExitAt": self.last_exit_at or None,
            "restartCount": self.restart_count,
            "lastError": self.last_error or None,
        }

    def close_log_handle(self) -> None:
        handle = self.log_handle
        self.log_handle = None
        if handle is None:
            return
        try:
            handle.close()
        except Exception:
            pass

    def mark_failure_reported(self) -> bool:
        if self.failure_reported:
            return False
        self.failure_reported = True
        return True


@dataclass(frozen=True)
class RelayedRuntimeEventPersistence:
    delivered_transports: list[Any]
    allow_direct_relay: bool = True


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
    scope_key = str(
        params.get("runtimeScopeKey")
        or params.get("runtime_scope_key")
        or profile.get("runtimeScopeKey")
        or profile.get("runtime_scope_key")
        or ""
    ).strip()
    if not scope_key:
        if profile_id:
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
        runtime_scope_key=scope_key,
        hermes_home=hermes_home,
    )


def _request_params(req: Any) -> dict[str, Any]:
    if not isinstance(req, dict):
        return {}
    params = req.get("params")
    return params if isinstance(params, dict) else {}


def _request_method(req: Any) -> str:
    if not isinstance(req, dict):
        return ""
    return str(req.get("method") or "").strip()


def _json_object_frame(raw: Any, *, context: str) -> dict[str, Any]:
    try:
        frame = json.loads(raw)
    except (TypeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"{context} is not valid JSON") from exc
    if not isinstance(frame, dict):
        raise RuntimeError(f"{context} must be a JSON object, got {type(frame).__name__}")
    return frame


def _gateway_ready_type(frame: dict[str, Any]) -> str:
    params = frame.get("params") if isinstance(frame.get("params"), dict) else {}
    return str(params.get("type") or "")


def _team_leader_runtime_scope_candidate(value: Any) -> str:
    scope_key = str(value or "").strip()
    if not scope_key.startswith("team:"):
        return ""
    if scope_key.endswith(":leader-conversation") or scope_key.endswith(":leader"):
        return scope_key
    return ""


def _team_leader_scope_key_from_params(params: dict[str, Any]) -> str:
    runtime_context = params.get("runtime_context")
    if not isinstance(runtime_context, dict):
        runtime_context = params.get("runtimeContext")
    if not isinstance(runtime_context, dict):
        runtime_context = {}
    explicit = str(
        params.get("leader_runtime_scope_key")
        or params.get("leaderRuntimeScopeKey")
        or params.get("conversation_runtime_scope_key")
        or params.get("conversationRuntimeScopeKey")
        or params.get("team_leader_runtime_scope_key")
        or params.get("teamLeaderRuntimeScopeKey")
        or runtime_context.get("leader_runtime_scope_key")
        or runtime_context.get("leaderRuntimeScopeKey")
        or runtime_context.get("runtime_scope_key")
        or runtime_context.get("runtimeScopeKey")
        or ""
    ).strip()
    if explicit:
        return explicit
    top_level_explicit = (
        _team_leader_runtime_scope_candidate(params.get("runtime_scope_key"))
        or _team_leader_runtime_scope_candidate(params.get("runtimeScopeKey"))
    )
    if top_level_explicit:
        return top_level_explicit
    members = params.get("members")
    if isinstance(members, list):
        leader = next(
            (
                item for item in members
                if isinstance(item, dict) and str(item.get("role") or "").strip() in {"lead", "leader"}
            ),
            None,
        ) or next((item for item in members if isinstance(item, dict)), None)
        if not isinstance(leader, dict):
            leader = {}
        metadata = leader.get("metadata") if isinstance(leader, dict) and isinstance(leader.get("metadata"), dict) else {}
        member_explicit = str(
            metadata.get("leader_runtime_scope_key")
            or metadata.get("leaderRuntimeScopeKey")
            or metadata.get("conversation_runtime_scope_key")
            or metadata.get("conversationRuntimeScopeKey")
            or leader.get("leader_runtime_scope_key")
            or leader.get("leaderRuntimeScopeKey")
            or ""
        ).strip()
        if member_explicit:
            return member_explicit
    subject = str(
        params.get("conversation_id")
        or params.get("conversationId")
        or params.get("mission_id")
        or params.get("missionId")
        or ""
    ).strip()
    if subject:
        return f"team:{subject}:leader-conversation"
    return ""


def _team_leader_profile_scope_from_params(params: dict[str, Any]) -> RuntimeScope:
    members = params.get("members")
    if not isinstance(members, list):
        return RuntimeScope()
    leader = next(
        (
            item for item in members
            if isinstance(item, dict) and str(item.get("role") or "").strip() in {"lead", "leader"}
        ),
        None,
    ) or next((item for item in members if isinstance(item, dict)), None)
    if not isinstance(leader, dict):
        return RuntimeScope()
    profile = leader.get("doxie_profile")
    if not isinstance(profile, dict):
        profile = leader.get("doxieProfile")
    if not isinstance(profile, dict):
        profile = {}
    scoped_params = {
        "agent_profile_id": leader.get("profile_id") or leader.get("agent_profile_id") or profile.get("id"),
        "runtime_scope_key": (
            leader.get("runtime_scope_key")
            or leader.get("runtimeScopeKey")
            or profile.get("runtimeScopeKey")
            or profile.get("runtime_scope_key")
        ),
        "doxie_profile": {
            **profile,
            "id": leader.get("profile_id") or leader.get("agent_profile_id") or profile.get("id"),
            "hermesHomePath": (
                leader.get("hermes_home_path")
                or leader.get("hermesHomePath")
                or profile.get("hermesHomePath")
                or profile.get("hermes_home_path")
            ),
        },
    }
    return runtime_scope_from_params(scoped_params)


def _team_leader_runtime_scope_from_params(params: dict[str, Any]) -> RuntimeScope:
    leader_scope_key = _team_leader_scope_key_from_params(params)
    profile_scope = runtime_scope_from_params(params)
    if not profile_scope.agent_profile_id and not profile_scope.hermes_home:
        profile_scope = _team_leader_profile_scope_from_params(params)
    if leader_scope_key:
        return RuntimeScope(
            agent_profile_id=profile_scope.agent_profile_id,
            runtime_scope_key=leader_scope_key,
            hermes_home=profile_scope.hermes_home,
        )
    return profile_scope


def runtime_scope_from_request(req: Any) -> RuntimeScope:
    method = _request_method(req)
    params = _request_params(req)
    if method in _TEAM_LEADER_RUNTIME_METHODS:
        leader_scope = _team_leader_runtime_scope_from_params(params)
        if leader_scope.has_scope:
            return leader_scope
    scope = runtime_scope_from_params(params)
    return scope


def _request_with_resolved_runtime_context(req: Any) -> Any:
    if _request_method(req) not in _TEAM_LEADER_RUNTIME_METHODS:
        return req
    from hermes_team_leader_runtime_context import resolve_team_leader_runtime_request

    return resolve_team_leader_runtime_request(req)


def _truthy_param(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return False


def _cron_control_plane_read_requested(method: str, params: dict[str, Any]) -> bool:
    if method != "cron.manage":
        return False
    action = str(params.get("action") or "").strip().lower()
    if action not in _CRON_CONTROL_PLANE_READ_ACTIONS:
        return False
    return (
        _truthy_param(params.get("controlPlaneOnly"))
        or _truthy_param(params.get("control_plane_only"))
        or _truthy_param(params.get("readOnly"))
        or _truthy_param(params.get("read_only"))
    )


def should_proxy_to_runtime(req: Any, *, resolve_team_context: bool = True) -> bool:
    method = _request_method(req)
    if not method:
        return False
    if resolve_team_context and method in _TEAM_LEADER_RUNTIME_METHODS:
        try:
            req = _request_with_resolved_runtime_context(req)
        except ValueError:
            return False
    params = _request_params(req)
    scope = runtime_scope_from_request(req)
    if not scope.has_scope:
        return False
    current_scope = str(os.environ.get("DOXIE_HERMES_RUNTIME_SCOPE_KEY") or "").strip()
    if current_scope and current_scope == scope.runtime_scope_key:
        return False
    if _cron_control_plane_read_requested(method, params):
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


def _profile_env_from_params(params: dict[str, Any]) -> dict[str, str]:
    profile = params.get("doxie_profile") if isinstance(params.get("doxie_profile"), dict) else {}
    profile_env = profile.get("env") if isinstance(profile.get("env"), dict) else {}
    return {str(k): str(v) for k, v in profile_env.items()}


def _profile_config_fingerprint_from_params(params: dict[str, Any]) -> dict[str, Any]:
    profile = params.get("doxie_profile") if isinstance(params.get("doxie_profile"), dict) else {}
    raw_toolsets = profile.get("defaultToolsets") or profile.get("default_toolsets") or []
    if isinstance(raw_toolsets, str):
        toolsets = [item.strip() for item in raw_toolsets.replace("\n", ",").split(",") if item.strip()]
    elif isinstance(raw_toolsets, (list, tuple, set)):
        toolsets = [str(item).strip() for item in raw_toolsets if str(item).strip()]
    else:
        toolsets = []
    return {
        "profile_fingerprint": str(
            profile.get("profileFingerprint")
            or profile.get("profile_fingerprint")
            or ""
        ).strip(),
        "default_toolsets": sorted(dict.fromkeys(toolsets)),
    }


def _launch_fingerprint(scope: RuntimeScope, params: dict[str, Any]) -> str:
    try:
        from hermes_constants import get_hermes_home

        control_home = str(get_hermes_home())
    except Exception:
        control_home = str(os.environ.get("HERMES_HOME") or "")
    payload = {
        "scope": {
            "agent_profile_id": scope.agent_profile_id,
            "runtime_scope_key": scope.runtime_scope_key,
            "hermes_home": scope.hermes_home,
        },
        "profile": _profile_config_fingerprint_from_params(params),
        "env": sorted(_profile_env_from_params(params).items()),
        "control_home": control_home,
        "runtime_mode": str(os.environ.get("DOXIE_HERMES_RUNTIME_MODE") or ""),
        "source_dir": str(os.environ.get("DOXIE_HERMES_SOURCE_DIR") or ""),
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _scope_log_slug(scope_key: str) -> str:
    raw = str(scope_key or "default").strip() or "default"
    return "".join(ch if ch.isalnum() or ch in {"-", "_", "."} else "_" for ch in raw)[:120]


def _open_worker_log(scope: RuntimeScope):
    logs_dir = Path(scope.hermes_home or os.getcwd()) / "logs"
    try:
        logs_dir.mkdir(parents=True, exist_ok=True)
        path = logs_dir / f"runtime-worker-{_scope_log_slug(scope.runtime_scope_key)}.log"
        handle = open(path, "ab", buffering=0)
        header = (
            f"\n--- runtime worker start pid=pending "
            f"scope={scope.runtime_scope_key!r} at={time.time():.3f} ---\n"
        )
        handle.write(header.encode("utf-8", errors="replace"))
        return handle
    except Exception:
        return subprocess.DEVNULL


def _runtime_worker_failure_message(worker: RuntimeWorker, reason: str) -> str:
    code = worker.process.poll()
    exit_desc = f"exit_code={code}" if code is not None else "exit_code=unknown"
    return (
        "runtime worker for scoped Team conversation stopped before the run reached a terminal state "
        f"({reason}; scope={worker.scope_key}; pid={worker.process.pid}; {exit_desc})"
    )


def _run_owned_by_worker(run: dict[str, Any], worker: RuntimeWorker) -> bool:
    if str(run.get("runtime_scope_key") or "") == worker.scope_key:
        return True
    metadata = run.get("metadata") if isinstance(run.get("metadata"), dict) else {}
    try:
        owner_pid = int(metadata.get("gateway_pid") or 0)
    except (TypeError, ValueError):
        owner_pid = 0
    return bool(owner_pid and owner_pid == int(worker.process.pid or 0))


def _worker_active_runs(worker: RuntimeWorker) -> list[dict[str, Any]]:
    try:
        from tui_gateway import server as tui_gateway_server
        from tui_gateway.services import run_control
    except Exception:
        return []
    db = None
    try:
        db = tui_gateway_server._get_db()
    except Exception:
        db = None
    active_statuses = list(getattr(run_control, "ACTIVE_RUN_STATUSES", ()))
    active_runs_by_id: dict[str, dict[str, Any]] = {}
    for runtime_scope_key in (worker.scope_key, ""):
        try:
            runs = run_control.list_runs(
                "",
                db=db,
                runtime_scope_key=runtime_scope_key,
                statuses=active_statuses,
                limit=1000,
            )
        except Exception:
            runs = []
        for run in runs:
            if not isinstance(run, dict) or not _run_owned_by_worker(run, worker):
                continue
            run_id = str(run.get("run_id") or "").strip()
            if run_id:
                active_runs_by_id[run_id] = run
    return list(active_runs_by_id.values())


def _worker_has_active_runs(worker: RuntimeWorker) -> bool:
    return bool(_worker_active_runs(worker))


def _terminalize_worker_active_runs(worker: RuntimeWorker, *, reason: str, transport: AsyncFrameTransport | None = None) -> int:
    if worker.running() or not worker.mark_failure_reported():
        return 0
    try:
        from tui_gateway import server as tui_gateway_server
        from tui_gateway.services import run_control
    except Exception:
        return 0
    db = None
    try:
        db = tui_gateway_server._get_db()
    except Exception:
        db = None
    active_runs = _worker_active_runs(worker)
    message = _runtime_worker_failure_message(worker, reason)
    failed = 0
    for run in active_runs:
        run_id = str(run.get("run_id") or "").strip()
        stable = str(run.get("session_id") or run.get("stored_session_id") or "").strip()
        if not run_id or not stable:
            continue
        try:
            run_control.publish_run_terminal_event(
                stored_session_id=stable,
                run_id=run_id,
                turn_id=str(run.get("turn_id") or ""),
                runtime_scope_key=str(run.get("runtime_scope_key") or worker.scope_key),
                runtime_session_id=str(run.get("runtime_session_id") or ""),
                status="failed",
                message=message,
                db=db,
                owner_transport=transport if hasattr(transport, "write") else None,
            )
            failed += 1
        except Exception:
            continue
    if failed:
        try:
            _log.warning(
                "runtime worker %s stopped; terminalized %s active run(s)",
                worker.scope_key,
                failed,
            )
        except Exception:
            pass
    return failed


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
            if existing is not None and existing.running() and not scope.hermes_home:
                existing.mark_used()
                return existing
            target_fingerprint = _launch_fingerprint(scope, params)
            if existing is not None and existing.running():
                if existing.launch_fingerprint != target_fingerprint:
                    if existing.bridge_count > 0 or _worker_has_active_runs(existing):
                        _log.warning(
                            "runtime worker %s launch environment changed while active; keeping existing worker for in-flight scope",
                            scope.runtime_scope_key,
                        )
                        existing.mark_used()
                        return existing
                    _log.info(
                        "restarting runtime worker %s because its launch environment changed",
                        scope.runtime_scope_key,
                    )
                    if existing in self._workers.values():
                        self._workers.pop(scope.runtime_scope_key, None)
                    if existing.bridge_count <= 0 and not _worker_has_active_runs(existing):
                        await self._terminate_worker(existing)
                else:
                    existing.mark_used()
                    return existing
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
        await self.retain_bridge(worker.scope_key)
        try:
            await self._probe_worker_ready(worker)
            worker.mark_used()
        finally:
            await self.release_bridge(worker.scope_key)
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
                    ready = _json_object_frame(ready_raw, context="runtime worker ready frame")
                    if _gateway_ready_type(ready) != "gateway.ready":
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
            raise RuntimeError(f"runtime profile hermes home required for runtime scope {scope.runtime_scope_key}")
        port = _reserve_loopback_port()
        token = secrets.token_urlsafe(24)
        env = os.environ.copy()
        env.update(_profile_env_from_params(params))
        env["HERMES_HOME"] = scope.hermes_home
        try:
            from hermes_constants import get_hermes_home

            env[_CONTROL_HOME_ENV] = str(get_hermes_home())
        except Exception:
            if os.environ.get("HERMES_HOME"):
                env[_CONTROL_HOME_ENV] = os.environ["HERMES_HOME"]
        env["DOXIE_HERMES_RUNTIME_SCOPE_KEY"] = scope.runtime_scope_key
        if scope.agent_profile_id:
            env["DOXIE_AGENT_PROFILE_ID"] = scope.agent_profile_id
        env[_SIDECAR_TOKEN_ENV] = token
        env[_SIDECAR_PARENT_PID_ENV] = str(os.getpid())
        log_handle = _open_worker_log(scope)
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "tui_gateway.doxie_sidecar",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
            ],
            cwd=os.getcwd(),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=log_handle,
            stderr=subprocess.STDOUT if log_handle is not subprocess.DEVNULL else subprocess.DEVNULL,
        )
        now = time.time()
        return RuntimeWorker(
            scope=scope,
            launch_fingerprint=_launch_fingerprint(scope, params),
            process=process,
            port=port,
            token=token,
            created_at=now,
            last_started_at=now,
            last_used_at=now,
            log_handle=log_handle if log_handle is not subprocess.DEVNULL else None,
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
                _terminalize_worker_active_runs(worker, reason="runtime worker exited before idle reclaim")
                continue
            if worker.bridge_count > 0:
                continue
            if _worker_has_active_runs(worker):
                worker.mark_used()
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
            _terminalize_worker_active_runs(worker, reason="runtime worker stopped before runtime pool termination")
            worker.close_log_handle()
            return
        try:
            process.terminate()
            await asyncio.to_thread(process.wait, 5)
        except Exception:
            try:
                process.kill()
                await asyncio.to_thread(process.wait, 5)
            except Exception:
                pass
        finally:
            _terminalize_worker_active_runs(worker, reason="runtime worker stopped by runtime pool")
            worker.close_log_handle()


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
        try:
            await self._ensure_connected()
        except Exception:
            await self.close()
            raise
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
                self._closed = True
                raise RuntimeError(f"runtime worker {self.scope_key} is not running")
            try:
                import websockets
            except Exception as exc:  # pragma: no cover - dependency is required for sidecar mode
                raise RuntimeError(f"Hermes runtime proxy requires websockets: {exc}") from exc

            uri = f"ws://127.0.0.1:{self.worker.port}/api/ws?token={self.worker.token}"
            last_exc: Exception | None = None
            for _ in range(_RUNTIME_CONNECT_ATTEMPTS):
                ws = None
                try:
                    ws = await websockets.connect(uri)
                    ready_raw = await ws.recv()
                    ready = _json_object_frame(ready_raw, context="runtime worker ready frame")
                    if _gateway_ready_type(ready) != "gateway.ready":
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
                    if ws is not None and ws is not self._ws:
                        try:
                            await ws.close()
                        except Exception:
                            pass
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
        failure_reason = ""
        try:
            while not self._closed and self._ws is not None:
                raw = await self._ws.recv()
                try:
                    frame = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if not isinstance(frame, dict):
                    continue
                if _gateway_ready_type(frame) == "gateway.ready":
                    continue
                self.worker.mark_used()
                persistence = _record_relayed_runtime_event(frame, owner_transport=self.transport)
                if not persistence.allow_direct_relay:
                    continue
                delivered_transports = persistence.delivered_transports
                delivered_to_owner = any(
                    transport is self.transport
                    for transport in delivered_transports
                )
                if delivered_to_owner:
                    continue
                if not await self.transport.write_async(frame):
                    failure_reason = "owner transport closed while relaying runtime event"
                    break
                _remember_direct_runtime_delivery(self.transport, frame)
        except Exception as exc:
            failure_reason = f"runtime websocket closed: {exc}"
            if not self._closed:
                _log.debug("runtime bridge %s read failed: %s", self.scope_key, exc)
        finally:
            if not self.worker.running():
                _terminalize_worker_active_runs(
                    self.worker,
                    reason=failure_reason or "runtime worker exited",
                    transport=self.transport,
                )
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


def _persist_relayed_runtime_event(
    frame: dict[str, Any],
    *,
    owner_transport: AsyncFrameTransport | None = None,
) -> list[Any]:
    return _record_relayed_runtime_event(
        frame,
        owner_transport=owner_transport,
    ).delivered_transports


def _record_relayed_runtime_event(
    frame: dict[str, Any],
    *,
    owner_transport: AsyncFrameTransport | None = None,
) -> RelayedRuntimeEventPersistence:
    if not isinstance(frame, dict) or str(frame.get("method") or "") != "event":
        return RelayedRuntimeEventPersistence([], allow_direct_relay=True)
    params = frame.get("params") if isinstance(frame.get("params"), dict) else {}
    event_type = str(params.get("type") or "").strip()
    if not event_type:
        return RelayedRuntimeEventPersistence([], allow_direct_relay=True)
    payload = params.get("payload") if isinstance(params.get("payload"), dict) else {}
    if _runtime_event_has_relay_source_marker(params, payload):
        return RelayedRuntimeEventPersistence([], allow_direct_relay=False)
    try:
        from tui_gateway import server as tui_gateway_server
        from tui_gateway.services import run_control

        persisted = dict(params)
        stable = str(
            persisted.get("stored_session_id")
            or persisted.get("storedSessionId")
            or persisted.get("session_id")
            or ""
        ).strip()
        db = tui_gateway_server._db_for_stable_session(stable)
        if db is None:
            return RelayedRuntimeEventPersistence([], allow_direct_relay=True)
        try:
            source_seq = int(persisted.get("seq") or 0)
        except (TypeError, ValueError):
            source_seq = 0
        if source_seq > 0:
            has_frame = getattr(db, "has_run_event_frame", None)
            if callable(has_frame) and has_frame(
                stable,
                seq=source_seq,
                run_id=str(persisted.get("run_id") or ""),
                runtime_session_id=str(persisted.get("session_id") or ""),
                event_type=event_type,
            ):
                return RelayedRuntimeEventPersistence([], allow_direct_relay=True)
            persisted["runtime_source_seq"] = source_seq
            persisted["payload"] = {
                **payload,
                "runtime_source_seq": source_seq,
            }
            has_source = getattr(db, "has_run_event_source", None)
            if callable(has_source) and has_source(
                stable,
                run_id=str(persisted.get("run_id") or ""),
                runtime_session_id=str(persisted.get("session_id") or ""),
                event_type=event_type,
                runtime_source_seq=source_seq,
            ):
                return RelayedRuntimeEventPersistence([], allow_direct_relay=False)
        if stable:
            persisted["seq"] = run_control.next_event_seq(stable, db=db)
        delivered = run_control.publish_recorded_event(
            persisted,
            owner_transport=owner_transport if hasattr(owner_transport, "write") else None,
            db=db,
        )
        return RelayedRuntimeEventPersistence(delivered, allow_direct_relay=True)
    except Exception:
        _log.debug("failed to persist relayed runtime event", exc_info=True)
    return RelayedRuntimeEventPersistence([], allow_direct_relay=True)


def _runtime_event_has_relay_source_marker(
    params: dict[str, Any],
    payload: dict[str, Any],
) -> bool:
    for value in (
        params.get("runtime_source_seq"),
        params.get("runtimeSourceSeq"),
        payload.get("runtime_source_seq"),
        payload.get("runtimeSourceSeq"),
    ):
        try:
            if int(value or 0) > 0:
                return True
        except (TypeError, ValueError):
            continue
    return False


def _remember_direct_runtime_delivery(
    transport: AsyncFrameTransport,
    frame: dict[str, Any],
) -> None:
    if not isinstance(frame, dict) or str(frame.get("method") or "") != "event":
        return
    params = frame.get("params") if isinstance(frame.get("params"), dict) else {}
    if not isinstance(params, dict):
        return
    try:
        from tui_gateway.services import run_control

        run_control.remember_transport_delivery(transport, params)
    except Exception:
        _log.debug("failed to remember relayed runtime event delivery", exc_info=True)


def ensure_runtime_ready_sync(params: dict[str, Any]) -> dict[str, Any]:
    scope = runtime_scope_from_params(params)
    worker = asyncio.run(runtime_proxy_pool().ensure_worker_ready(scope, params))
    return worker.status()


async def proxy_to_runtime(req: Any, transport: Any) -> bool:
    resolved_req = _request_with_resolved_runtime_context(req)
    if not should_proxy_to_runtime(resolved_req, resolve_team_context=False):
        return False
    params = _request_params(resolved_req)
    scope = runtime_scope_from_request(resolved_req)
    pool = runtime_proxy_pool()
    worker = await pool.ensure_worker(scope, params)
    await pool.retain_bridge(worker.scope_key)
    try:
        bridge = await transport.runtime_bridge(worker)
        await bridge.send(resolved_req)
    finally:
        await pool.release_bridge(worker.scope_key)
    return True
