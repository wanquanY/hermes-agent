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

import asyncio
import concurrent.futures
import json
import logging
import threading
import time
import uuid
from collections import deque
from typing import Any

from tui_gateway import server
from tui_gateway.services.contract_capabilities import timeline_contract_ready_payload
from tui_gateway.services.runtime_scope import runtime_scope_from_request
from hermes_agent.orchestration.worker_runtime import primary_dispatch

_log = logging.getLogger(__name__)

# Max seconds a pool-dispatched handler will block waiting for the event loop
# to flush a WS frame before we mark the transport dead. Protects handler
# threads from a wedged socket.
_WS_WRITE_TIMEOUT_S = 10.0
_WS_LARGE_FRAME_BYTES = 512 * 1024
_STREAMING_EVENT_TYPES = frozenset(
    {
        "message.delta",
        "reasoning.delta",
        "thinking.delta",
        "subagent.output_delta",
        "subagent.reasoning_delta",
        "agent_profile_test.output_delta",
        "agent_profile_test.thinking",
    }
)
_TOKEN_COALESCE_S = 0.033
_STREAM_TEXT_FIELDS = ("delta", "text", "output", "content")
_WS_DIAGNOSTIC_METHODS = frozenset(
    {
        "approval.respond",
        "events.subscribe",
        "events.unsubscribe",
        "run.events",
        "team_mission.message.submit",
        "team_mission.node.history",
    }
)
_WS_CONTROL_METHODS = frozenset(
    {
        "events.compact",
        "conversation.activity.list",
        "conversation.render_snapshot",
        "events.prune",
        "events.subscribe",
        "events.unsubscribe",
        "profile.archive",
        "profile.draft.discard",
        "profile.draft.get",
        "profile.draft.list",
        "profile.draft.upsert",
        "profile.get",
        "profile.growth.summary",
        "profile.list",
        "profile.upsert",
        "run.cancel",
        "run.events",
        "run.fail",
        "run.list",
        "run.reserve",
        "run.status",
        "session.list",
        "session.messages",
        "session.message_metadata.merge",
        "session.status",
        "session.title",
        "team_mission.conversation.delete",
        "team_mission.conversation.ensure",
        "team_mission.conversation.list",
        "team_mission.conversation.participants",
        "team_mission.conversation.execution_session_ids",
        "team_mission.message.submit",
        "team_mission.conversation.rename",
        "team_mission.conversation.render",
        "team_mission.conversation.resolve",
        "team_mission.node.history",
        "workspace.current",
        "workspace.session.bind",
        "workspace.session.current",
        "workspace.session.delete",
        "workspace.session.list",
        "workspace.list",
    }
)


def _positive_int(value: Any) -> int:
    try:
        parsed = int(value or 0)
    except (TypeError, ValueError):
        return 0
    return parsed if parsed > 0 else 0


def _utf16_length(value: str) -> int:
    return len(str(value or "").encode("utf-16-le")) // 2


def _stream_payload(frame: dict[str, Any]) -> dict[str, Any]:
    params = frame.get("params") if isinstance(frame.get("params"), dict) else {}
    return params.get("payload") if isinstance(params.get("payload"), dict) else {}


def _stream_text(payload: dict[str, Any]) -> str:
    text_stream = payload.get("text_stream") if isinstance(payload.get("text_stream"), dict) else {}
    for field in _STREAM_TEXT_FIELDS:
        value = text_stream.get(field)
        if value is not None and str(value) != "":
            return str(value)
    for field in _STREAM_TEXT_FIELDS:
        value = payload.get(field)
        if value is not None and str(value) != "":
            return str(value)
    return ""


def _stream_offset(payload: dict[str, Any]) -> int | None:
    text_stream = payload.get("text_stream") if isinstance(payload.get("text_stream"), dict) else {}
    for value in (text_stream.get("offset"), payload.get("offset")):
        if value is None or value == "":
            continue
        try:
            return max(0, int(value))
        except (TypeError, ValueError):
            continue
    return None


def _stream_mode(payload: dict[str, Any]) -> str:
    text_stream = payload.get("text_stream") if isinstance(payload.get("text_stream"), dict) else {}
    return str(text_stream.get("mode") or payload.get("mode") or "append").strip().lower()


def _stream_identity(frame: dict[str, Any]) -> tuple[str, ...]:
    params = frame.get("params") if isinstance(frame.get("params"), dict) else {}
    payload = params.get("payload") if isinstance(params.get("payload"), dict) else {}
    return tuple(
        str(value or "").strip()
        for value in (
            params.get("type"),
            params.get("conversation_session_id") or params.get("session_id"),
            params.get("execution_session_id"),
            params.get("run_id"),
            params.get("turn_id"),
            params.get("runtime_scope_key"),
            params.get("activity_id") or params.get("activityId"),
            params.get("participant_id") or params.get("participantId"),
            payload.get("subagent_id") or payload.get("subagentId"),
            payload.get("delegate_call_id") or payload.get("delegateCallId"),
            payload.get("client_message_id") or payload.get("clientMessageId"),
            payload.get("message_id") or payload.get("messageId"),
            payload.get("segment_id") or payload.get("segmentId"),
            payload.get("stream_id") or payload.get("streamId"),
            payload.get("test_run_id") or payload.get("testRunId"),
            payload.get("source"),
        )
    )


def _merge_stream_frames(
    first: dict[str, Any],
    second: dict[str, Any],
) -> dict[str, Any] | None:
    if _stream_identity(first) != _stream_identity(second):
        return None
    first_payload = _stream_payload(first)
    second_payload = _stream_payload(second)
    if _stream_mode(first_payload) != "append" or _stream_mode(second_payload) != "append":
        return None
    first_text = _stream_text(first_payload)
    second_text = _stream_text(second_payload)
    if not first_text or not second_text:
        return None
    first_offset = _stream_offset(first_payload)
    second_offset = _stream_offset(second_payload)
    if (first_offset is None) != (second_offset is None):
        return None
    if first_offset is not None and second_offset != first_offset + _utf16_length(first_text):
        return None

    merged_text = first_text + second_text
    merged = dict(first)
    first_params = first.get("params") if isinstance(first.get("params"), dict) else {}
    second_params = second.get("params") if isinstance(second.get("params"), dict) else {}
    merged_params = {**first_params, **second_params}
    merged_payload = {**first_payload, **second_payload}
    for field in _STREAM_TEXT_FIELDS:
        if field in first_payload or field in second_payload:
            merged_payload[field] = merged_text
    if first_offset is not None:
        merged_payload["offset"] = first_offset

    first_text_stream = (
        first_payload.get("text_stream")
        if isinstance(first_payload.get("text_stream"), dict)
        else {}
    )
    second_text_stream = (
        second_payload.get("text_stream")
        if isinstance(second_payload.get("text_stream"), dict)
        else {}
    )
    if first_text_stream or second_text_stream:
        merged_text_stream = {**first_text_stream, **second_text_stream}
        for field in _STREAM_TEXT_FIELDS:
            if field in first_text_stream or field in second_text_stream:
                merged_text_stream[field] = merged_text
        if first_offset is not None:
            merged_text_stream["offset"] = first_offset
        merged_payload["text_stream"] = merged_text_stream
    first_event_text_stream = (
        first_params.get("text_stream")
        if isinstance(first_params.get("text_stream"), dict)
        else {}
    )
    second_event_text_stream = (
        second_params.get("text_stream")
        if isinstance(second_params.get("text_stream"), dict)
        else {}
    )
    if first_event_text_stream or second_event_text_stream:
        merged_event_text_stream = {
            **first_event_text_stream,
            **second_event_text_stream,
        }
        for field in _STREAM_TEXT_FIELDS:
            if field in first_event_text_stream or field in second_event_text_stream:
                merged_event_text_stream[field] = merged_text
        if first_offset is not None:
            merged_event_text_stream["offset"] = first_offset
        merged_params["text_stream"] = merged_event_text_stream
    merged_params["payload"] = merged_payload
    merged["params"] = merged_params
    return merged


def _coalesce_stream_lines(lines: list[str]) -> list[str]:
    coalesced: list[dict[str, Any] | str] = []
    for line in lines:
        try:
            frame = json.loads(line)
        except (TypeError, ValueError):
            coalesced.append(line)
            continue
        if not isinstance(frame, dict):
            coalesced.append(line)
            continue
        previous = coalesced[-1] if coalesced else None
        if isinstance(previous, dict):
            merged = _merge_stream_frames(previous, frame)
            if merged is not None:
                coalesced[-1] = merged
                continue
        coalesced.append(frame)
    return [
        item if isinstance(item, str) else json.dumps(item, ensure_ascii=False)
        for item in coalesced
    ]
_ws_control_executor = concurrent.futures.ThreadPoolExecutor(
    max_workers=4,
    thread_name_prefix="tui-ws-control",
)

def _executor_for_request(req: dict) -> concurrent.futures.Executor | None:
    method = str(req.get("method") or "") if isinstance(req, dict) else ""
    if method in _WS_CONTROL_METHODS:
        return _ws_control_executor
    return None


def _request_id(req: Any) -> Any:
    return req.get("id") if isinstance(req, dict) else None


def _request_method(req: Any) -> str:
    return str(req.get("method") or "") if isinstance(req, dict) else ""


def _runtime_scope_key(req: Any) -> str:
    try:
        return runtime_scope_from_request(req).runtime_scope_key
    except Exception:
        return ""


def _frame_meta(line: str) -> dict[str, Any]:
    text = str(line or "")
    meta: dict[str, Any] = {
        "bytes": len(text.encode("utf-8")),
    }
    try:
        frame = json.loads(text)
    except Exception:
        return meta
    if not isinstance(frame, dict):
        return meta
    params = frame.get("params") if isinstance(frame.get("params"), dict) else {}
    payload = params.get("payload") if isinstance(params.get("payload"), dict) else {}
    result = frame.get("result") if isinstance(frame.get("result"), dict) else {}
    error = frame.get("error") if isinstance(frame.get("error"), dict) else {}
    meta.update(
        {
            "id": frame.get("id") or "",
            "method": str(frame.get("method") or ""),
            "response": bool(frame.get("id") and not frame.get("method")),
            "error_code": error.get("code") or "",
            "result_keys": list(result.keys())[:16] if result else [],
            "replay_event_count": len(result.get("events") or [])
            if isinstance(result.get("events"), list)
            else None,
            "replay_last_seq": result.get("last_event_seq")
            or result.get("last_seq")
            or result.get("lastSeq")
            or None,
            "subscription_id": result.get("subscription_id")
            or result.get("subscriptionId")
            or "",
            "event_type": str(params.get("type") or ""),
            "session_id": params.get("session_id") or payload.get("session_id") or "",
            "conversation_session_id": params.get("conversation_session_id")
            or payload.get("conversation_session_id")
            or "",
            "runtime_scope_key": params.get("runtime_scope_key")
            or payload.get("runtime_scope_key")
            or "",
            "run_id": params.get("run_id") or payload.get("run_id") or "",
            "turn_id": params.get("turn_id") or payload.get("turn_id") or "",
            "mission_id": params.get("mission_id") or payload.get("mission_id") or "",
            "node_id": params.get("node_id") or payload.get("node_id") or "",
        }
    )
    return meta


def _should_log_frame(meta: dict[str, Any]) -> bool:
    if int(meta.get("bytes") or 0) >= _WS_LARGE_FRAME_BYTES:
        return True
    if str(meta.get("response_to_method") or "") in _WS_DIAGNOSTIC_METHODS:
        return True
    return str(meta.get("method") or "") in _WS_DIAGNOSTIC_METHODS


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
        self._connection_id = uuid.uuid4().hex[:12]
        self._created_at = time.time()
        self._closed = False
        self._sent_count = 0
        self._bytes_sent = 0
        self._last_send_meta: dict[str, Any] | None = None
        self._pending_requests: dict[str, dict[str, Any]] = {}
        self._send_queue: deque[tuple[bool, str, asyncio.Future | None]] = deque()
        self._send_worker: asyncio.Task | None = None
        self._stream_lock = threading.Lock()
        self._pending_stream_lines: list[str] = []
        self._stream_flush_handle: asyncio.TimerHandle | None = None
        self._stream_flush_armed = False

    def _diagnostics(self) -> dict[str, Any]:
        return {
            "connection_id": self._connection_id,
            "age_s": round(time.time() - self._created_at, 3),
            "closed": self._closed,
            "sent_count": self._sent_count,
            "bytes_sent": self._bytes_sent,
            "queue_size": len(self._send_queue),
            "pending_stream_count": len(self._pending_stream_lines),
            "pending_request_count": len(self._pending_requests),
            "last_send": self._last_send_meta,
        }

    def remember_request(self, req: dict[str, Any], meta: dict[str, Any]) -> None:
        request_id = req.get("id")
        method = str(req.get("method") or "")
        if request_id is None or not method:
            return
        self._pending_requests[str(request_id)] = {
            "method": method,
            "bytes": int(meta.get("bytes") or 0),
            "mission_id": meta.get("mission_id") or "",
            "conversation_session_id": meta.get("conversation_session_id") or "",
            "run_id": meta.get("run_id") or "",
            "runtime_scope_key": _runtime_scope_key(req),
            "received_at": time.time(),
        }

    def _attach_response_request_context(self, meta: dict[str, Any]) -> dict[str, Any]:
        if not meta.get("response") or not meta.get("id"):
            return meta
        request = self._pending_requests.pop(str(meta.get("id")), None)
        if not request:
            return meta
        enriched = dict(meta)
        enriched.update(
            {
                "response_to_method": request["method"],
                "response_to_mission_id": request["mission_id"],
                "response_to_conversation_session_id": request["conversation_session_id"],
                "response_to_run_id": request["run_id"],
                "response_to_runtime_scope_key": request["runtime_scope_key"],
                "request_bytes": request["bytes"],
                "request_latency_ms": int((time.time() - request["received_at"]) * 1000),
            }
        )
        return enriched

    @staticmethod
    def _is_streaming_frame(obj: dict[str, Any]) -> bool:
        params = obj.get("params") if isinstance(obj, dict) else None
        if not isinstance(params, dict) or params.get("type") not in _STREAMING_EVENT_TYPES:
            return False
        payload = params.get("payload") if isinstance(params.get("payload"), dict) else {}
        return bool(
            params.get("transient") is True
            and _positive_int(params.get("seq")) == 0
            and not payload.get("stream_checkpoint")
            and not payload.get("streamCheckpoint")
        )

    def write(self, obj: dict) -> bool:
        if self._closed:
            return False

        line = json.dumps(obj, ensure_ascii=False)
        frame_meta = _frame_meta(line)

        if self._is_streaming_frame(obj):
            with self._stream_lock:
                self._pending_stream_lines.append(line)
                if not self._stream_flush_armed:
                    self._stream_flush_armed = True
                    self._loop.call_soon_threadsafe(self._arm_stream_flush)
            return not self._closed

        try:
            on_loop = asyncio.get_running_loop() is self._loop
        except RuntimeError:
            on_loop = False

        if on_loop:
            self._enqueue_on_loop(line, priority=False, flush_streams=True)
            return True

        try:
            from agent.async_utils import safe_schedule_threadsafe
            fut = safe_schedule_threadsafe(self._send_from_worker(line), self._loop)
            if fut is None:
                self._closed = True
                _log.warning(
                    "gateway ws write failed: loop scheduling returned none %s",
                    self._diagnostics(),
                )
                return False
            fut.result(timeout=_WS_WRITE_TIMEOUT_S)
            return not self._closed
        except concurrent.futures.TimeoutError:
            # A busy agent turn can stall the event loop without closing the
            # socket. The coroutine remains scheduled and will send when the
            # loop resumes; only _safe_send may latch a real transport failure.
            _log.warning(
                "gateway ws write slow: loop stalled >%ss %s frame=%s",
                _WS_WRITE_TIMEOUT_S,
                self._diagnostics(),
                frame_meta,
            )
            return not self._closed
        except Exception as exc:
            self._closed = True
            _log.warning(
                "gateway ws write failed: %s %s %s frame=%s",
                type(exc).__name__,
                exc,
                self._diagnostics(),
                frame_meta,
            )
            return False

    async def write_async(self, obj: dict) -> bool:
        """Send from the owning event loop. Awaits until the frame is on the wire."""
        if self._closed:
            return False
        await self._enqueue_send(
            json.dumps(obj, ensure_ascii=False),
            priority=True,
            flush_streams=True,
        )
        return not self._closed

    async def _send_from_worker(self, line: str) -> bool:
        await self._enqueue_send(line, priority=False, flush_streams=True)
        return not self._closed

    def _take_pending_stream_lines(self) -> list[str]:
        with self._stream_lock:
            lines = self._pending_stream_lines
            self._pending_stream_lines = []
            self._stream_flush_armed = False
            handle = self._stream_flush_handle
            self._stream_flush_handle = None
        if handle is not None:
            handle.cancel()
        return _coalesce_stream_lines(lines)

    def _arm_stream_flush(self) -> None:
        if self._closed:
            return
        with self._stream_lock:
            if not self._stream_flush_armed or self._stream_flush_handle is not None:
                return
            self._stream_flush_handle = self._loop.call_later(
                _TOKEN_COALESCE_S,
                self._flush_stream_lines,
            )

    def _flush_stream_lines(self) -> None:
        lines = self._take_pending_stream_lines()
        if lines and not self._closed:
            self._queue_stream_lines(lines)

    def _queue_stream_lines(self, lines: list[str]) -> None:
        if self._closed:
            return
        for stream_line in lines:
            self._send_queue.append((False, stream_line, None))
        self._ensure_send_worker()

    def _enqueue_on_loop(
        self,
        line: str,
        *,
        priority: bool,
        flush_streams: bool,
    ) -> None:
        pending_streams = self._take_pending_stream_lines() if flush_streams else []
        for stream_line in pending_streams:
            self._send_queue.append((False, stream_line, None))
        item = (priority, line, None)
        if priority and not pending_streams:
            self._send_queue.appendleft(item)
        else:
            self._send_queue.append(item)
        self._ensure_send_worker()

    def _ensure_send_worker(self) -> None:
        if self._send_worker is None or self._send_worker.done():
            self._send_worker = self._loop.create_task(self._drain_send_queue())

    async def _enqueue_send(
        self,
        line: str,
        *,
        priority: bool,
        flush_streams: bool = False,
    ) -> bool:
        if self._closed:
            return False
        pending_streams = self._take_pending_stream_lines() if flush_streams else []
        for stream_line in pending_streams:
            self._send_queue.append((False, stream_line, None))
        fut = self._loop.create_future()
        item = (priority, line, fut)
        if priority and not pending_streams:
            self._send_queue.appendleft(item)
        else:
            self._send_queue.append(item)
        self._ensure_send_worker()
        await fut
        return not self._closed

    async def _drain_send_queue(self) -> None:
        while self._send_queue and not self._closed:
            _priority, line, fut = self._send_queue.popleft()
            try:
                await self._safe_send(line)
                if fut and not fut.done():
                    fut.set_result(not self._closed)
            except Exception as exc:
                if fut and not fut.done():
                    fut.set_exception(exc)
                break
        while self._send_queue:
            _priority, _line, fut = self._send_queue.popleft()
            if fut and not fut.done():
                fut.set_result(False)

    async def _safe_send(self, line: str) -> None:
        meta = self._attach_response_request_context(_frame_meta(line))
        self._last_send_meta = meta
        if _should_log_frame(meta):
            _log.info(
                "gateway ws send frame %s frame=%s",
                self._diagnostics(),
                meta,
            )
        try:
            await self._ws.send_text(line)
            self._sent_count += 1
            self._bytes_sent += int(meta.get("bytes") or 0)
        except Exception as exc:
            self._closed = True
            _log.warning(
                "gateway ws send failed: %s %s %s frame=%s",
                type(exc).__name__,
                exc,
                self._diagnostics(),
                meta,
            )

    def close(self) -> None:
        if not self._closed:
            _log.info("gateway ws transport closing %s", self._diagnostics())
        self._closed = True
        with self._stream_lock:
            self._pending_stream_lines = []
            self._stream_flush_armed = False
            handle = self._stream_flush_handle
            self._stream_flush_handle = None
        if handle is not None:
            handle.cancel()

    async def aclose(self) -> None:
        # Phase 6: legacy ``_runtime_bridges`` map deleted; the new
        # worker stack is owned by the process-wide ``WorkerSupervisor``,
        # not per-ws.
        self.close()


async def handle_ws(ws: Any) -> None:
    """Run one WebSocket session. Wire-compatible with ``tui_gateway.entry``."""
    await ws.accept()

    transport = WSTransport(ws, asyncio.get_running_loop())
    _log.info("gateway ws accepted %s", transport._diagnostics())

    await transport.write_async(
        {
            "jsonrpc": "2.0",
            "method": "event",
            "params": {
                "type": "gateway.ready",
                "payload": {
                    "skin": server.resolve_skin(),
                    **timeline_contract_ready_payload(),
                },
            },
        }
    )

    try:
        while True:
            try:
                raw = await ws.receive_text()
            except _WebSocketDisconnect as exc:
                code = getattr(exc, "code", "")
                log_disconnect = _log.info if code == 1000 else _log.warning
                log_disconnect(
                    "gateway ws client disconnected: code=%s %s",
                    code,
                    transport._diagnostics(),
                )
                break

            line = raw.strip()
            if not line:
                continue
            line_meta = _frame_meta(line)
            if _should_log_frame(line_meta):
                _log.info(
                    "gateway ws receive frame %s frame=%s",
                    transport._diagnostics(),
                    line_meta,
                )

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

            if isinstance(req, dict):
                transport.remember_request(req, line_meta)

            try:
                if await primary_dispatch(req, transport):
                    continue
            except Exception as exc:
                rid = _request_id(req)
                method = _request_method(req)
                scope_key = _runtime_scope_key(req)
                _log.exception(
                    "runtime proxy failed for method=%s id=%s scope=%s",
                    method,
                    rid,
                    scope_key,
                )
                ok = await transport.write_async(
                    {
                        "jsonrpc": "2.0",
                        "error": {
                            "code": 5020,
                            "message": f"runtime proxy failed: {exc}",
                            "data": {
                                "method": method,
                                "runtime_scope_key": scope_key,
                            },
                        },
                        "id": rid,
                    }
                )
                if not ok:
                    break
                continue

            # dispatch() may schedule long handlers on the pool; it returns
            # None in that case and the worker writes the response itself via
            # the transport we pass in (a separate thread, so transport.write
            # is the safe path there). For inline handlers it returns the
            # response dict, which we write here from the loop.
            loop = asyncio.get_running_loop()
            resp = await loop.run_in_executor(
                _executor_for_request(req),
                server.dispatch,
                req,
                transport,
            )
            if resp is not None and not await transport.write_async(resp):
                break
    finally:
        await transport.aclose()
        _log.info("gateway ws closed %s", transport._diagnostics())

        # Detach the dead transport from run_control's subscription
        # indexes. Without this, ``record_event`` keeps returning the
        # closed transport as a "subscriber" and ``_write_event`` writes
        # to it; ``WSTransport.write`` short-circuits on ``_closed`` but
        # ``_write_event`` ignores that return value and reports
        # success. The new run_worker path hits this hard because the
        # frontend ws often gets recycled (HMR, reconnect) and the
        # stale subscription silently swallows every live event until
        # the user navigates away and DB-hydrates.
        try:
            from tui_gateway.services.run_control import detach_transport
            detach_transport(transport)
        except Exception:
            _log.exception(
                "gateway ws subscription detach failed %s",
                transport._diagnostics(),
            )

        # C1 disconnect reap (ported from upstream ae94ed172): hand off to
        # server._close_sessions_for_transport, which (a) tears down sessions
        # that opted in via close_on_disconnect (dovie sidecar / dashboard
        # embed) immediately via the unified _close_session_by_id path, and
        # (b) detaches the rest by re-pointing their transport at stdio so
        # later emits don't hit a dead socket. The teardown is offloaded to a
        # thread because worker.close() + DB write inside _finalize_session
        # can take 50-200ms; running it inline would stall the uvicorn event
        # loop for any other concurrent socket.
        try:
            reaped, detached = await asyncio.to_thread(
                server._close_sessions_for_transport,
                transport,
                end_reason="ws_disconnect",
            )
            if reaped or detached:
                _log.info(
                    "gateway ws disconnect reap: reaped=%d detached=%d %s",
                    reaped, detached, transport._diagnostics(),
                )
        except Exception:
            _log.exception(
                "gateway ws disconnect reap failed %s",
                transport._diagnostics(),
            )
            # Fallback: the legacy in-place detach so a half-open transport
            # never lingers as a write target even if the reap path raised.
            for _, sess in list(server._sessions.items()):
                if sess.get("transport") is transport:
                    sess["transport"] = server._stdio_transport

        try:
            await ws.close()
        except Exception:
            pass
