"""Worker-side DB RPC proxy over the run-worker stdio protocol.

The worker process must not open ``state.db`` directly.  It sees this
object anywhere state access is required and every method call is forwarded to
the main process as a synchronous JSON-RPC request.
"""

from __future__ import annotations

import base64
import datetime as _dt
import sqlite3
import threading
import time
from dataclasses import is_dataclass, asdict
from pathlib import Path
from typing import Any, Callable, Optional


class WorkerDBProxyError(RuntimeError):
    """Base error raised by the worker DB proxy."""


class WorkerDBProxyRemoteError(WorkerDBProxyError):
    """Main process rejected or failed a DB method call."""


class WorkerDBProxyTimeoutError(WorkerDBProxyError, TimeoutError):
    """Timed out waiting for a main-process DB reply."""


class WorkerDBProxyDisconnectedError(WorkerDBProxyError, BrokenPipeError):
    """The main process disconnected while DB calls were pending."""


class WorkerDBProxyMethodError(WorkerDBProxyRemoteError):
    """The requested DB method is not exposed by the IPC whitelist."""


_BYTES_MARKER = "__worker_db_proxy_bytes__"
WORKER_DB_COMPONENT_NAMES = frozenset(
    {
        "activities",
        "analytics",
        "branches",
        "compression_leases",
        "messages",
        "maintenance",
        "metadata",
        "participants",
        "profiles",
        "runs",
        "run_event_maintenance",
        "session_index",
        "sessions",
        "team_mission_audit",
        "team_mission_conversation_deliverables",
        "team_capabilities",
        "team_mission_graphs",
        "team_mission_maintenance",
        "team_mission_node_history",
        "team_mission_rows",
        "team_missions",
        "teams",
        "telegram_topics",
        "tool_event_projection",
    }
)


def serialize_db_value(value: Any) -> Any:
    """Convert DB return values and arguments into JSON-safe values."""
    if isinstance(value, sqlite3.Row):
        return {key: serialize_db_value(value[key]) for key in value.keys()}
    if isinstance(value, bytes):
        return {_BYTES_MARKER: base64.b64encode(value).decode("ascii")}
    if isinstance(value, (Path,)):
        return str(value)
    if isinstance(value, (_dt.datetime, _dt.date, _dt.time)):
        return value.isoformat()
    if is_dataclass(value) and not isinstance(value, type):
        return serialize_db_value(asdict(value))
    if isinstance(value, dict):
        return {
            str(serialize_db_value(key)): serialize_db_value(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple, set)):
        return [serialize_db_value(item) for item in value]
    return value


def deserialize_db_value(value: Any) -> Any:
    """Restore special proxy encodings in JSON-RPC replies."""
    if isinstance(value, dict):
        if set(value.keys()) == {_BYTES_MARKER}:
            encoded = value.get(_BYTES_MARKER)
            if isinstance(encoded, str):
                return base64.b64decode(encoded.encode("ascii"))
        return {key: deserialize_db_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [deserialize_db_value(item) for item in value]
    return value


class _PendingCall:
    def __init__(self) -> None:
        self.event = threading.Event()
        self.reply: Optional[dict[str, Any]] = None


class WorkerDBProxy:
    """Duck-typed worker-side state access proxy.

    ``ipc_writer`` must expose ``write_json(dict)``.  ``ipc_reader`` is
    accepted for API symmetry with the protocol but replies are normally
    delivered by ``WorkerProtocol`` through :meth:`handle_reply`, keeping a
    single reader on worker stdin.
    """

    def __init__(
        self,
        ipc_writer: Any,
        ipc_reader: Any = None,
        *,
        timeout_s: float = 30.0,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._ipc_writer = ipc_writer
        self._ipc_reader = ipc_reader
        self._timeout_s = float(timeout_s or 30.0)
        self._monotonic = monotonic
        self._request_id_counter = 0
        self._pending: dict[str, _PendingCall] = {}
        self._lock = threading.RLock()
        self._closed = False

    def __getattr__(self, name: str):
        if name.startswith("_"):
            raise AttributeError(name)
        if name in WORKER_DB_COMPONENT_NAMES:
            return _WorkerDBComponentProxy(self, name, conversation_session_id="")

        def proxy(*args: Any, **kwargs: Any) -> Any:
            return self._call(name, args, kwargs, conversation_session_id="")

        return proxy

    @property
    def db_path(self) -> str:
        return "worker-db-proxy"

    def scoped(self, conversation_session_id: str) -> "_ScopedWorkerDBProxy":
        return _ScopedWorkerDBProxy(self, str(conversation_session_id or "").strip())

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            pending = list(self._pending.values())
            self._pending.clear()
        for call in pending:
            call.reply = {
                "jsonrpc": "2.0",
                "id": "",
                "error": {
                    "code": -32098,
                    "type": "WorkerDBProxyDisconnectedError",
                    "message": "main process disconnected",
                },
            }
            call.event.set()

    def handle_reply(self, reply: dict[str, Any]) -> bool:
        req_id = str(reply.get("id") or "")
        if not req_id:
            return False
        with self._lock:
            pending = self._pending.get(req_id)
        if pending is None:
            return False
        pending.reply = reply
        pending.event.set()
        return True

    def _next_id(self) -> str:
        with self._lock:
            if self._closed:
                raise WorkerDBProxyDisconnectedError("main process disconnected")
            self._request_id_counter += 1
            return str(self._request_id_counter)

    def _call(
        self,
        method: str,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
        *,
        conversation_session_id: str,
    ) -> Any:
        req_id = self._next_id()
        pending = _PendingCall()
        with self._lock:
            if self._closed:
                raise WorkerDBProxyDisconnectedError("main process disconnected")
            self._pending[req_id] = pending
        request = {
            "jsonrpc": "2.0",
            "id": req_id,
            "method": f"db.{method}",
            "db_scope": {"conversation_session_id": conversation_session_id},
            "params": [
                serialize_db_value(list(args)),
                serialize_db_value(dict(kwargs)),
            ],
        }
        try:
            self._ipc_writer.write_json(request)
            reply = self._wait_reply(req_id, pending, timeout=self._timeout_s)
        finally:
            with self._lock:
                self._pending.pop(req_id, None)
        if "error" in reply:
            raise _remote_error(reply["error"])
        return deserialize_db_value(reply.get("result"))

    def _wait_reply(
        self,
        req_id: str,
        pending: _PendingCall,
        *,
        timeout: float,
    ) -> dict[str, Any]:
        deadline = self._monotonic() + max(0.001, timeout)
        while True:
            remaining = deadline - self._monotonic()
            if remaining <= 0:
                raise WorkerDBProxyTimeoutError(
                    f"timed out waiting for db RPC reply id={req_id}"
                )
            if pending.event.wait(min(remaining, 0.1)):
                reply = pending.reply or {}
                if _is_disconnect_reply(reply):
                    raise WorkerDBProxyDisconnectedError("main process disconnected")
                return reply
            with self._lock:
                if self._closed:
                    raise WorkerDBProxyDisconnectedError("main process disconnected")


def _remote_error(error: Any) -> WorkerDBProxyRemoteError:
    if not isinstance(error, dict):
        return WorkerDBProxyRemoteError(str(error))
    message = str(error.get("message") or error)
    error_type = str(error.get("type") or "")
    if error_type == "WorkerDBProxyMethodError":
        return WorkerDBProxyMethodError(message)
    return WorkerDBProxyRemoteError(message)


def _is_disconnect_reply(reply: dict[str, Any]) -> bool:
    error = reply.get("error") if isinstance(reply, dict) else None
    return isinstance(error, dict) and error.get("type") == "WorkerDBProxyDisconnectedError"


class _ScopedWorkerDBProxy:
    def __init__(self, root: WorkerDBProxy, conversation_session_id: str) -> None:
        self._root = root
        self._conversation_session_id = conversation_session_id

    @property
    def db_path(self) -> str:
        if self._conversation_session_id:
            return f"worker-db-proxy:{self._conversation_session_id}"
        return "worker-db-proxy"

    def close(self) -> None:
        return None

    def __getattr__(self, name: str):
        if name.startswith("_"):
            raise AttributeError(name)
        if name in WORKER_DB_COMPONENT_NAMES:
            return _WorkerDBComponentProxy(
                self._root,
                name,
                conversation_session_id=self._conversation_session_id,
            )

        def proxy(*args: Any, **kwargs: Any) -> Any:
            return self._root._call(
                name,
                args,
                kwargs,
                conversation_session_id=self._conversation_session_id,
            )

        return proxy


class _WorkerDBComponentProxy:
    def __init__(
        self,
        root: WorkerDBProxy,
        component: str,
        *,
        conversation_session_id: str,
    ) -> None:
        self._root = root
        self._component = component
        self._conversation_session_id = conversation_session_id

    def __getattr__(self, name: str):
        if name.startswith("_"):
            raise AttributeError(name)

        def proxy(*args: Any, **kwargs: Any) -> Any:
            return self._root._call(
                f"{self._component}.{name}",
                args,
                kwargs,
                conversation_session_id=self._conversation_session_id,
            )

        return proxy


_default_proxy: WorkerDBProxy | None = None
_default_proxy_lock = threading.RLock()


def set_default_worker_db_proxy(proxy: WorkerDBProxy | None) -> None:
    global _default_proxy
    with _default_proxy_lock:
        _default_proxy = proxy


def get_default_worker_db_proxy() -> WorkerDBProxy | None:
    with _default_proxy_lock:
        return _default_proxy
