"""Worker-side JSON-RPC proxy for main-process worker methods."""

from __future__ import annotations

import threading
import time
from typing import Any, Callable, Optional

from tui_gateway.services.worker_db_proxy import (
    WorkerDBProxyDisconnectedError,
    WorkerDBProxyRemoteError,
    WorkerDBProxyTimeoutError,
    deserialize_db_value,
    serialize_db_value,
)


class WorkerRpcProxy:
    """Synchronous proxy for worker -> main JSON-RPC methods.

    This shares the run-worker stdout protocol with ``WorkerDBProxy`` but
    keeps method names outside the ``db.*`` namespace.
    """

    def __init__(
        self,
        ipc_writer: Any,
        *,
        timeout_s: float = 30.0,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._ipc_writer = ipc_writer
        self._timeout_s = float(timeout_s or 30.0)
        self._monotonic = monotonic
        self._request_id_counter = 0
        self._pending: dict[str, _PendingCall] = {}
        self._lock = threading.RLock()
        self._closed = False

    def request(self, method: str, params: dict[str, Any] | None = None) -> Any:
        normalized_method = str(method or "").strip()
        if not normalized_method:
            raise ValueError("worker RPC method required")
        req_id = self._next_id()
        pending = _PendingCall()
        with self._lock:
            if self._closed:
                raise WorkerDBProxyDisconnectedError("main process disconnected")
            self._pending[req_id] = pending
        request = {
            "jsonrpc": "2.0",
            "id": req_id,
            "method": normalized_method,
            "params": serialize_db_value(params or {}),
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
            return f"worker-rpc-{self._request_id_counter}"

    def _wait_reply(
        self,
        req_id: str,
        pending: "_PendingCall",
        *,
        timeout: float,
    ) -> dict[str, Any]:
        deadline = self._monotonic() + max(0.001, timeout)
        while True:
            remaining = deadline - self._monotonic()
            if remaining <= 0:
                raise WorkerDBProxyTimeoutError(
                    f"timed out waiting for worker RPC reply id={req_id}"
                )
            if pending.event.wait(min(remaining, 0.1)):
                reply = pending.reply or {}
                if _is_disconnect_reply(reply):
                    raise WorkerDBProxyDisconnectedError("main process disconnected")
                return reply
            with self._lock:
                if self._closed:
                    raise WorkerDBProxyDisconnectedError("main process disconnected")


class _PendingCall:
    def __init__(self) -> None:
        self.event = threading.Event()
        self.reply: Optional[dict[str, Any]] = None


def _remote_error(error: Any) -> WorkerDBProxyRemoteError:
    if not isinstance(error, dict):
        return WorkerDBProxyRemoteError(str(error))
    message = str(error.get("message") or error)
    return WorkerDBProxyRemoteError(message)


def _is_disconnect_reply(reply: dict[str, Any]) -> bool:
    error = reply.get("error") if isinstance(reply, dict) else None
    return isinstance(error, dict) and error.get("type") == "WorkerDBProxyDisconnectedError"


_default_proxy: WorkerRpcProxy | None = None
_default_proxy_lock = threading.RLock()


def set_default_worker_rpc_proxy(proxy: WorkerRpcProxy | None) -> None:
    global _default_proxy
    with _default_proxy_lock:
        _default_proxy = proxy


def get_default_worker_rpc_proxy() -> WorkerRpcProxy | None:
    with _default_proxy_lock:
        return _default_proxy
