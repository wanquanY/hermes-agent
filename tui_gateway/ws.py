"""WebSocket transport for the tui_gateway JSON-RPC server.

Reuses :func:`tui_gateway.server.dispatch` verbatim so every RPC method, every
slash command, every approval/clarify/sudo flow, and every agent event flows
through the same handlers whether the client is Ink over stdio or an iOS /
web client over WebSocket.

Wire protocol
-------------
Identical to stdio: newline-delimited JSON-RPC in both directions. The server
emits a ``gateway.ready`` event immediately after connection accept, then
echoes responses/events for inbound requests. No framing differences.

Mounting
--------
    from fastapi import WebSocket
    from tui_gateway.ws import handle_ws

    @app.websocket("/api/ws")
    async def ws(ws: WebSocket):
        await handle_ws(ws)
"""

from __future__ import annotations

import atexit
import asyncio
import concurrent.futures
import itertools
import json
import logging
import os
import sys
import time
from typing import Any

from tui_gateway import server

_log = logging.getLogger(__name__)

# Max seconds a pool-dispatched handler will block waiting for the event loop
# to flush a WS frame before we mark the transport dead. Protects handler
# threads from a wedged socket.
_WS_WRITE_TIMEOUT_S = 10.0


def _bounded_worker_count(env_name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.environ.get(env_name) or default)
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


# WebSocket dispatch has two very different traffic classes:
# - latency-sensitive control-plane calls that keep the UI navigable
# - background refresh / heavier calls that may touch transcript databases
# Keeping them on separate pools prevents a running turn or a slow history
# refresh from starving session switching, interrupt, approval, and resume.
_CONTROL_PLANE_METHODS = frozenset(
    {
        "approval.pending.list",
        "approval.policy.get",
        "approval.policy.set",
        "approval.respond",
        "clarify.respond",
        "config.set",
        "events.prune",
        "events.subscribe",
        "events.unsubscribe",
        "run.list",
        "run.cancel",
        "run.reserve",
        "run.fail",
        "run.status",
        "secret.respond",
        "session.interrupt",
        "session.list",
        "session.messages",
        "session.resume",
        "session.status",
        "session.usage",
        "sudo.respond",
        "workspace.current",
    }
)

_ws_control_executor = concurrent.futures.ThreadPoolExecutor(
    max_workers=_bounded_worker_count("HERMES_TUI_WS_CONTROL_WORKERS", 8, 2, 32),
    thread_name_prefix="tui-ws-control",
)
_ws_background_executor = concurrent.futures.ThreadPoolExecutor(
    max_workers=_bounded_worker_count("HERMES_TUI_WS_BACKGROUND_WORKERS", 16, 4, 64),
    thread_name_prefix="tui-ws-rpc",
)
atexit.register(lambda: _ws_control_executor.shutdown(wait=False, cancel_futures=True))
atexit.register(lambda: _ws_background_executor.shutdown(wait=False, cancel_futures=True))


def _interrupt_trace(stage: str, **fields: Any) -> None:
    if os.environ.get("HERMES_INTERRUPT_TRACE") not in {"1", "true", "TRUE", "yes", "YES"}:
        return
    safe = " ".join(f"{key}={value}" for key, value in fields.items())
    print(f"[hermes] [tui_gateway] [interrupt-trace] {stage} {safe}", file=sys.stderr, flush=True)


def _frame_meta(obj: dict) -> dict | None:
    method = str(obj.get("method") or "")
    params = obj.get("params") if isinstance(obj.get("params"), dict) else {}
    payload = params.get("payload") if isinstance(params.get("payload"), dict) else {}
    event_type = str(params.get("type") or "") if method == "event" else ""
    is_interrupt = method == "session.interrupt" or (
        method == "event"
        and event_type in {"message.complete", "error"}
        and str(payload.get("status") or "") == "interrupted"
    )
    if not is_interrupt:
        return None
    return {
        "id": obj.get("id"),
        "method": method,
        "type": event_type,
        "sid": params.get("session_id") or params.get("sessionId"),
        "stored_sid": params.get("stored_session_id"),
        "run_id": params.get("run_id") or payload.get("run_id"),
        "turn_id": params.get("turn_id") or payload.get("turn_id"),
    }


def _request_method(req: dict) -> str:
    return str(req.get("method") or "")


def _executor_for_request(req: dict) -> concurrent.futures.ThreadPoolExecutor:
    if _request_method(req) in _CONTROL_PLANE_METHODS:
        return _ws_control_executor
    return _ws_background_executor

# Keep starlette optional at import time; handle_ws uses the real class when
# it's available and falls back to a generic Exception sentinel otherwise.
try:
    from starlette.websockets import WebSocketDisconnect as _WebSocketDisconnect
except ImportError:  # pragma: no cover - starlette is a required install path
    _WebSocketDisconnect = Exception  # type: ignore[assignment]


class WSTransport:
    """Per-connection WS transport.

    ``write`` is safe to call from any thread *other than* the event loop
    thread that owns the socket. Pool workers (the only real caller) run in
    their own threads, so marshalling onto the loop via
    :func:`asyncio.run_coroutine_threadsafe` + ``future.result()`` is correct
    and deadlock-free there.

    When called from the loop thread itself (e.g. by ``handle_ws`` for an
    inline response) the same call would deadlock: we'd schedule work onto
    the loop we're currently blocking. We detect that case and fire-and-
    forget instead. Callers that need to know when the bytes are on the wire
    should use :meth:`write_async` from the loop thread.
    """

    def __init__(self, ws: Any, loop: asyncio.AbstractEventLoop) -> None:
        self._ws = ws
        self._loop = loop
        self._closed = False
        self._seq = itertools.count()
        self._queue: asyncio.PriorityQueue[tuple[int, int, str, asyncio.Future]] = asyncio.PriorityQueue()
        self._writer_task = loop.create_task(self._writer())

    def write(self, obj: dict) -> bool:
        if self._closed:
            return False

        line = json.dumps(obj, ensure_ascii=False)
        priority = self._priority(obj)
        meta = _frame_meta(obj)

        try:
            on_loop = asyncio.get_running_loop() is self._loop
        except RuntimeError:
            on_loop = False

        if on_loop:
            # Fire-and-forget — don't block the loop waiting on itself. The
            # dedicated writer still preserves response/event priority.
            self._loop.create_task(self._send_queued(line, priority))
            if meta and (meta.get("method") == "session.interrupt" or meta.get("type") == "message.complete"):
                _interrupt_trace("ws.transport.write.on_loop", priority=priority, **meta)
            return True

        try:
            started = time.time()
            fut = asyncio.run_coroutine_threadsafe(self._send_queued(line, priority), self._loop)
            ok = bool(fut.result(timeout=_WS_WRITE_TIMEOUT_S))
            if meta and (meta.get("method") == "session.interrupt" or meta.get("type") == "message.complete"):
                _interrupt_trace("ws.transport.write.done", ok=ok, elapsed_ms=int((time.time() - started) * 1000), priority=priority, **meta)
            return ok
        except Exception as exc:
            self._closed = True
            if meta:
                _interrupt_trace("ws.transport.write.failed", error=type(exc).__name__, priority=priority, **meta)
            _log.debug("ws write failed: %s", exc)
            return False

    async def write_async(self, obj: dict) -> bool:
        """Send from the owning event loop. Awaits until the frame is on the wire."""
        if self._closed:
            return False
        meta = _frame_meta(obj)
        started = time.time()
        ok = await self._send_queued(json.dumps(obj, ensure_ascii=False), self._priority(obj))
        if meta and (meta.get("method") == "session.interrupt" or meta.get("type") == "message.complete"):
            _interrupt_trace("ws.transport.write_async.done", ok=ok, elapsed_ms=int((time.time() - started) * 1000), priority=self._priority(obj), **meta)
        return ok

    def _priority(self, obj: dict) -> int:
        # JSON-RPC responses are control-plane frames. They must not sit behind
        # high-volume streaming events such as message.delta/tool progress.
        return 1 if obj.get("method") == "event" else 0

    async def _send_queued(self, line: str, priority: int) -> bool:
        if self._closed:
            return False
        done = self._loop.create_future()
        await self._queue.put((priority, next(self._seq), line, done))
        return bool(await done)

    async def _writer(self) -> None:
        while True:
            _priority, _seq, line, done = await self._queue.get()
            ok = False
            try:
                if not self._closed:
                    await self._ws.send_text(line)
                    ok = not self._closed
            except Exception as exc:
                self._closed = True
                _log.debug("ws send failed: %s", exc)
            finally:
                if not done.done():
                    done.set_result(ok)

    def close(self) -> None:
        self._closed = True
        self._writer_task.cancel()
        while True:
            try:
                _priority, _seq, _line, done = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            if not done.done():
                done.set_result(False)


async def handle_ws(ws: Any) -> None:
    """Run one WebSocket session. Wire-compatible with ``tui_gateway.entry``."""
    await ws.accept()

    transport = WSTransport(ws, asyncio.get_running_loop())
    dispatch_tasks: set[asyncio.Task] = set()

    async def dispatch_frame(req: dict, meta: dict | None, started: float) -> None:
        try:
            loop = asyncio.get_running_loop()
            resp = await loop.run_in_executor(
                _executor_for_request(req),
                server.dispatch,
                req,
                transport,
            )
            if meta:
                _interrupt_trace("ws.dispatch.return", has_resp=resp is not None, elapsed_ms=int((time.time() - started) * 1000), **meta)
            if resp is not None:
                ok = await transport.write_async(resp)
                if meta:
                    _interrupt_trace("ws.response.flushed", ok=ok, elapsed_ms=int((time.time() - started) * 1000), **meta)
        except Exception as exc:
            if meta:
                _interrupt_trace("ws.dispatch.failed", error=type(exc).__name__, elapsed_ms=int((time.time() - started) * 1000), **meta)

    await transport.write_async(
        {
            "jsonrpc": "2.0",
            "method": "event",
            "params": {
                "type": "gateway.ready",
                "payload": {"skin": server.resolve_skin()},
            },
        }
    )

    try:
        while True:
            try:
                raw = await ws.receive_text()
            except _WebSocketDisconnect:
                break

            line = raw.strip()
            if not line:
                continue

            try:
                req = json.loads(line)
            except json.JSONDecodeError:
                ok = await transport.write_async(
                    {
                        "jsonrpc": "2.0",
                        "error": {"code": -32700, "message": "parse error"},
                        "id": None,
                    }
                )
                if not ok:
                    break
                continue

            # dispatch() may schedule long handlers on the pool; it returns
            # None in that case and the worker writes the response itself via
            # the transport we pass in. For inline handlers it returns a
            # response dict. Crucially, the receive loop must not wait for
            # dispatch or response flushing: a streaming response can apply
            # socket backpressure, and control-plane requests such as
            # session.interrupt must still be read immediately.
            meta = _frame_meta(req)
            started = time.time()
            if meta:
                _interrupt_trace("ws.receive", **meta)
            task = asyncio.create_task(dispatch_frame(req, meta, started))
            dispatch_tasks.add(task)
            task.add_done_callback(dispatch_tasks.discard)
    finally:
        for task in list(dispatch_tasks):
            task.cancel()
        transport.close()

        # Detach the transport from any sessions it owned so later emits
        # fall back to stdio instead of crashing into a closed socket.
        for _, sess in list(server._sessions.items()):
            if sess.get("transport") is transport:
                sess["transport"] = server._stdio_transport
        server._run_control.detach_transport(transport)

        try:
            await ws.close()
        except Exception:
            pass
