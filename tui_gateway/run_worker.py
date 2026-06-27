"""Run-worker entry: stdin/stdout JSON-line subprocess that hosts the
LLM runtime for one ``runtime_scope_key`` profile.

Replaces the sub-sidecar process spawned by ``RuntimeWorkerPool``. The
new worker does NOT open a websocket server — the main sidecar drives
it over the worker's stdin and reads events back from its stdout.

Protocol (one JSON object per line, UTF-8, ``\\n``-terminated):

    inbound (main → worker)
      {"op":"run.start", "run_id", "turn_id", "stored_session_id",
       "prompt", "params"}
      {"op":"run.cancel", "run_id"}
      {"op":"interactive.response", "kind", "request_id", "answer"}
      {"op":"runtime.env.update", "env_updates": {"KEY": "value"}}
      {"op":"shutdown"}

    outbound (worker → main)
      {"op":"event", "params": {...}}            # 1:1 with the legacy
                                                 # worker→main ws event
                                                 # payload
      {"op":"interactive.request", "kind", "request_id", "payload"}
      {"op":"run.terminal", "run_id", "status"}
      {"op":"log", "level", "text"}

Phase 4a (this file) implemented the codec + a stub run loop suitable
for unit tests. Phase 4c wired the run handler into the real agent
library. Phase 5+ made this the sole worker-spawning path; the legacy
``RuntimeWorkerPool`` proxy was deleted in Phase 6.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Awaitable, Callable, Optional, Union


# Capture the REAL stdout immediately at module import — BEFORE any
# transitive import of ``tui_gateway.server`` (the agent runner pulls
# it in lazily), which does ``sys.stdout = sys.stderr`` at module
# load to keep stray library ``print()`` calls out of the JSON-RPC
# protocol. If we don't snapshot here, every ``_flush_stdout`` later
# writes to stderr instead of the protocol pipe and the supervisor
# never sees a single outbound frame.
_real_stdout = sys.stdout
_stdout_write_lock = threading.RLock()


# ── Inbound frame types (main → worker) ──────────────────────────────


@dataclass(frozen=True)
class RunStartFrame:
    run_id: str
    turn_id: str
    stored_session_id: str
    prompt: str
    params: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RunCancelFrame:
    run_id: str


_INTERACTIVE_KINDS = frozenset({"clarify", "approval", "secret", "sudo"})


@dataclass(frozen=True)
class InteractiveResponseFrame:
    kind: str
    request_id: str
    answer: Any


@dataclass(frozen=True)
class ActivityEventFrame:
    kind: str
    event: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RuntimeEnvUpdateFrame:
    env_updates: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ShutdownFrame:
    pass


@dataclass(frozen=True)
class DBRpcReplyFrame:
    id: str
    result: Any = None
    error: Any = None


IncomingFrame = Union[
    RunStartFrame,
    RunCancelFrame,
    InteractiveResponseFrame,
    ActivityEventFrame,
    RuntimeEnvUpdateFrame,
    ShutdownFrame,
    DBRpcReplyFrame,
]


# ── Outbound frame types (worker → main) ─────────────────────────────


@dataclass(frozen=True)
class EventFrame:
    """``params`` is the 1:1 worker→main event payload from the legacy
    ws bridge — keep it opaque so existing renderer code on the main
    side keeps working unchanged."""

    params: dict[str, Any]


@dataclass(frozen=True)
class InteractiveRequestFrame:
    kind: str
    request_id: str
    payload: dict[str, Any] = field(default_factory=dict)
    # Worker-known context. Optional so older worker builds (or echo-
    # only test stubs) keep working — the router cross-fills from its
    # ``run_table`` when these are empty.
    stored_session_id: str = ""


@dataclass(frozen=True)
class RunTerminalFrame:
    run_id: str
    status: str
    stored_session_id: str = ""
    turn_id: str = ""
    message: str = ""


@dataclass(frozen=True)
class LogFrame:
    level: str
    text: str


@dataclass(frozen=True)
class DBRpcRequestFrame:
    id: str
    method: str
    params: Any = None
    db_scope: dict[str, Any] = field(default_factory=dict)


OutgoingFrame = Union[
    EventFrame,
    InteractiveRequestFrame,
    RunTerminalFrame,
    LogFrame,
    DBRpcRequestFrame,
]


# ── Codec ────────────────────────────────────────────────────────────


class FrameDecodeError(ValueError):
    """Raised for malformed inbound frames. The caller decides whether
    to drop the frame, surface a log, or terminate the worker."""


def _require_str(obj: dict, key: str, *, op: str) -> str:
    value = obj.get(key)
    if not isinstance(value, str):
        raise FrameDecodeError(f"{op}: field {key!r} must be a string")
    return value


def _optional_str(obj: dict, key: str, default: str = "") -> str:
    value = obj.get(key, default)
    if value is None:
        return default
    if not isinstance(value, str):
        raise FrameDecodeError(f"field {key!r} must be a string or null")
    return value


def _optional_mapping(obj: dict, key: str) -> dict[str, Any]:
    value = obj.get(key)
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise FrameDecodeError(f"field {key!r} must be an object")
    return value


def _string_mapping(obj: dict, key: str, *, op: str) -> dict[str, str]:
    value = obj.get(key)
    if not isinstance(value, dict):
        raise FrameDecodeError(f"{op}: field {key!r} must be an object")
    result: dict[str, str] = {}
    for raw_key, raw_value in value.items():
        if not isinstance(raw_key, str):
            raise FrameDecodeError(f"{op}: env update keys must be strings")
        if not isinstance(raw_value, str):
            raise FrameDecodeError(
                f"{op}: env update value for {raw_key!r} must be a string"
            )
        result[raw_key] = raw_value
    return result


def decode_incoming(line: str) -> IncomingFrame:
    """Parse one stdin line into a typed inbound frame."""
    stripped = line.strip()
    if not stripped:
        raise FrameDecodeError("empty line")
    try:
        obj = json.loads(stripped)
    except json.JSONDecodeError as exc:
        raise FrameDecodeError(f"invalid JSON: {exc}") from exc
    if not isinstance(obj, dict):
        raise FrameDecodeError("frame must be a JSON object")
    op = obj.get("op")
    if not isinstance(op, str):
        if obj.get("jsonrpc") == "2.0" and "id" in obj and (
            "result" in obj or "error" in obj
        ):
            return DBRpcReplyFrame(
                id=str(obj.get("id") or ""),
                result=obj.get("result"),
                error=obj.get("error"),
            )
        raise FrameDecodeError("frame missing string field 'op'")

    if op == "run.start":
        return RunStartFrame(
            run_id=_require_str(obj, "run_id", op=op),
            turn_id=_require_str(obj, "turn_id", op=op),
            stored_session_id=_require_str(obj, "stored_session_id", op=op),
            prompt=_optional_str(obj, "prompt"),
            params=_optional_mapping(obj, "params"),
        )
    if op == "run.cancel":
        return RunCancelFrame(run_id=_require_str(obj, "run_id", op=op))
    if op == "interactive.response":
        kind = _require_str(obj, "kind", op=op)
        if kind not in _INTERACTIVE_KINDS:
            raise FrameDecodeError(
                f"interactive.response: kind={kind!r} not in {sorted(_INTERACTIVE_KINDS)}"
            )
        return InteractiveResponseFrame(
            kind=kind,
            request_id=_require_str(obj, "request_id", op=op),
            answer=obj.get("answer"),
        )
    if op == "event":
        kind = _require_str(obj, "kind", op=op)
        event = obj.get("event")
        if event is None:
            event = obj.get("params")
        if event is None:
            event = obj.get("payload")
        if not isinstance(event, dict):
            raise FrameDecodeError("event: field 'event' must be an object")
        return ActivityEventFrame(kind=kind, event=event)
    if op == "runtime.env.update":
        return RuntimeEnvUpdateFrame(
            env_updates=_string_mapping(obj, "env_updates", op=op)
        )
    if op == "shutdown":
        return ShutdownFrame()

    raise FrameDecodeError(f"unknown op {op!r}")


def encode_incoming(frame: IncomingFrame) -> str:
    """Serialize a main→worker frame (used by the supervisor side)."""
    if isinstance(frame, RunStartFrame):
        body: dict[str, Any] = {
            "op": "run.start",
            "run_id": frame.run_id,
            "turn_id": frame.turn_id,
            "stored_session_id": frame.stored_session_id,
            "prompt": frame.prompt,
            "params": frame.params,
        }
    elif isinstance(frame, RunCancelFrame):
        body = {"op": "run.cancel", "run_id": frame.run_id}
    elif isinstance(frame, InteractiveResponseFrame):
        body = {
            "op": "interactive.response",
            "kind": frame.kind,
            "request_id": frame.request_id,
            "answer": frame.answer,
        }
    elif isinstance(frame, ActivityEventFrame):
        body = {"op": "event", "kind": frame.kind, "event": frame.event}
    elif isinstance(frame, RuntimeEnvUpdateFrame):
        body = {"op": "runtime.env.update", "env_updates": frame.env_updates}
    elif isinstance(frame, ShutdownFrame):
        body = {"op": "shutdown"}
    elif isinstance(frame, DBRpcReplyFrame):
        body = {"jsonrpc": "2.0", "id": frame.id}
        if frame.error is not None:
            body["error"] = frame.error
        else:
            body["result"] = frame.result
    else:  # pragma: no cover — exhausted by Union
        raise TypeError(f"unknown incoming frame type: {type(frame)!r}")
    return json.dumps(body, ensure_ascii=False, separators=(",", ":"))


def decode_outgoing(line: str) -> OutgoingFrame:
    """Parse one worker stdout line into a typed outbound frame.

    Used by ``WorkerSupervisor`` on the main-sidecar side. Symmetrical
    with ``encode_outgoing`` on the worker side."""
    stripped = line.strip()
    if not stripped:
        raise FrameDecodeError("empty line")
    try:
        obj = json.loads(stripped)
    except json.JSONDecodeError as exc:
        raise FrameDecodeError(f"invalid JSON: {exc}") from exc
    if not isinstance(obj, dict):
        raise FrameDecodeError("frame must be a JSON object")
    op = obj.get("op")
    if not isinstance(op, str):
        method = obj.get("method")
        if obj.get("jsonrpc") == "2.0" and isinstance(method, str):
            return DBRpcRequestFrame(
                id=str(obj.get("id") or ""),
                method=method,
                params=obj.get("params"),
                db_scope=_optional_mapping(obj, "db_scope"),
            )
        raise FrameDecodeError("frame missing string field 'op'")

    if op == "event":
        return EventFrame(params=_optional_mapping(obj, "params"))
    if op == "interactive.request":
        return InteractiveRequestFrame(
            kind=_require_str(obj, "kind", op=op),
            request_id=_require_str(obj, "request_id", op=op),
            payload=_optional_mapping(obj, "payload"),
            stored_session_id=_optional_str(obj, "stored_session_id"),
        )
    if op == "run.terminal":
        return RunTerminalFrame(
            run_id=_require_str(obj, "run_id", op=op),
            status=_require_str(obj, "status", op=op),
            stored_session_id=_optional_str(obj, "stored_session_id"),
            turn_id=_optional_str(obj, "turn_id"),
            message=_optional_str(obj, "message"),
        )
    if op == "log":
        return LogFrame(
            level=_require_str(obj, "level", op=op),
            text=_require_str(obj, "text", op=op),
        )
    raise FrameDecodeError(f"unknown outbound op {op!r}")


def encode_outgoing(frame: OutgoingFrame) -> str:
    """Serialize one outbound frame to a single line (no trailing newline)."""
    if isinstance(frame, EventFrame):
        body = {"op": "event", "params": frame.params}
    elif isinstance(frame, InteractiveRequestFrame):
        body = {
            "op": "interactive.request",
            "kind": frame.kind,
            "request_id": frame.request_id,
            "payload": frame.payload,
        }
        if frame.stored_session_id:
            body["stored_session_id"] = frame.stored_session_id
    elif isinstance(frame, RunTerminalFrame):
        body = {
            "op": "run.terminal",
            "run_id": frame.run_id,
            "status": frame.status,
        }
        if frame.stored_session_id:
            body["stored_session_id"] = frame.stored_session_id
        if frame.turn_id:
            body["turn_id"] = frame.turn_id
        if frame.message:
            body["message"] = frame.message
    elif isinstance(frame, LogFrame):
        body = {"op": "log", "level": frame.level, "text": frame.text}
    elif isinstance(frame, DBRpcRequestFrame):
        body = {
            "jsonrpc": "2.0",
            "id": frame.id,
            "method": frame.method,
            "params": frame.params,
        }
        if frame.db_scope:
            body["db_scope"] = frame.db_scope
    else:  # pragma: no cover — exhausted by Union
        raise TypeError(f"unknown outgoing frame type: {type(frame)!r}")
    # ``ensure_ascii=False`` keeps non-ASCII frames compact (event
    # payloads carry assistant text that's often CJK); ``separators``
    # drops whitespace so the line is parseable in lockstep with `\\n`.
    return json.dumps(body, ensure_ascii=False, separators=(",", ":"))


# ── Async runner ────────────────────────────────────────────────────


# Handler signature: receives one inbound frame, may emit any number of
# outbound frames via the supplied ``emit`` coroutine. Returning ``None``
# is fine; raising propagates out of the read loop and terminates the
# worker (the caller logs and exits non-zero).
FrameHandler = Callable[
    ["WorkerProtocol", IncomingFrame], Awaitable[None]
]


class WorkerProtocol:
    """Owns the worker's stdin reader and stdout writer plus the run
    loop. Constructor takes injectable async I/O so tests can drive
    the protocol via in-memory queues."""

    def __init__(
        self,
        *,
        lines_in: AsyncIterator[str],
        emit: Callable[[str], Awaitable[None]],
        handler: FrameHandler,
        on_decode_error: Optional[Callable[[FrameDecodeError, str], Awaitable[None]]] = None,
        db_reply_handler: Optional[Callable[[dict[str, Any]], bool]] = None,
    ) -> None:
        self._lines_in = lines_in
        self._emit_raw = emit
        self._handler = handler
        self._on_decode_error = on_decode_error
        self._db_reply_handler = db_reply_handler
        self._shutdown = asyncio.Event()

    async def emit(self, frame: OutgoingFrame) -> None:
        await self._emit_raw(encode_outgoing(frame) + "\n")

    async def emit_log(self, level: str, text: str) -> None:
        await self.emit(LogFrame(level=level, text=text))

    def request_shutdown(self) -> None:
        self._shutdown.set()

    @property
    def shutdown_requested(self) -> bool:
        return self._shutdown.is_set()

    async def run(self) -> None:
        """Read frames until shutdown is requested or stdin closes.

        Decode errors are logged via ``on_decode_error`` (or emitted as
        a ``log`` frame if no handler is supplied) and the loop
        continues. Handler exceptions propagate out — the caller logs
        and terminates the process.

        Run-start frames are dispatched on a **background task** so the
        run loop stays free to read further frames (notably
        ``RunCancelFrame``) while the agent is executing — otherwise
        stdin reads stall until the agent thread joins, the cancel
        sits buffered in the pipe, and ``backend.cancel`` only sees
        the request AFTER the run already terminated.
        """
        background_tasks: set[asyncio.Task[Any]] = set()
        try:
            async for line in self._lines_in:
                if self._shutdown.is_set():
                    return
                try:
                    frame = decode_incoming(line)
                except FrameDecodeError as exc:
                    if self._on_decode_error is not None:
                        await self._on_decode_error(exc, line)
                    else:
                        await self.emit_log("error", f"decode error: {exc}")
                    continue

                if isinstance(frame, ShutdownFrame):
                    self.request_shutdown()
                    return

                if isinstance(frame, DBRpcReplyFrame):
                    if self._db_reply_handler is not None:
                        self._db_reply_handler(
                            {
                                "jsonrpc": "2.0",
                                "id": frame.id,
                                **(
                                    {"error": frame.error}
                                    if frame.error is not None
                                    else {"result": frame.result}
                                ),
                            }
                        )
                    else:
                        await self.emit_log(
                            "warn", f"db reply with no handler id={frame.id}"
                        )
                    continue

                if isinstance(frame, RunStartFrame):
                    task = asyncio.create_task(self._handler(self, frame))
                    background_tasks.add(task)
                    task.add_done_callback(background_tasks.discard)
                elif isinstance(frame, ActivityEventFrame):
                    await self._handler(self, frame)
                else:
                    # RunCancelFrame / InteractiveResponseFrame complete fast;
                    # awaiting inline keeps ordering deterministic (a cancel
                    # that lands right after a response is processed in
                    # arrival order, not racing with whatever the response
                    # unblocked).
                    await self._handler(self, frame)
        finally:
            # Drain any in-flight run starts before returning. Tests and
            # callers depend on emit completing before ``run()`` exits;
            # leaving background tasks pending would also drop the
            # terminal RunTerminalFrame the backend emits at run end.
            if background_tasks:
                await asyncio.gather(*background_tasks, return_exceptions=True)


async def _stdin_lines() -> AsyncIterator[str]:
    """Async iterator over ``sys.stdin`` lines, scheduled off the event
    loop so blocking reads don't stall it."""
    loop = asyncio.get_running_loop()
    while True:
        line = await loop.run_in_executor(None, sys.stdin.readline)
        if not line:
            return
        yield line


def _stdout_writer() -> Callable[[str], Awaitable[None]]:
    lock = asyncio.Lock()

    async def write(payload: str) -> None:
        # ``sys.stdout.write`` + ``flush`` is safe to call under the
        # lock because we serialize all emits through one coroutine.
        async with lock:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, _flush_stdout, payload)

    return write


def _flush_stdout(payload: str) -> None:
    # Use the snapshot captured at module import — ``sys.stdout`` will
    # have been redirected to stderr by the time the agent runner has
    # imported ``tui_gateway.server``.
    with _stdout_write_lock:
        _real_stdout.write(payload)
        _real_stdout.flush()


class _StdoutJsonRpcWriter:
    def write_json(self, obj: dict[str, Any]) -> None:
        _flush_stdout(json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n")


# ── Run backend + interactive responder (worker-side dispatch) ──────


# Coroutine the backend calls to send frames out. Bound to
# ``WorkerProtocol.emit`` at run time.
Emit = Callable[[OutgoingFrame], Awaitable[None]]


class WorkerRunBackend:
    """Abstract base for what happens when a ``RunStartFrame`` arrives.

    Phase 5b ships ``_StubBackend`` (echoes a stubbed terminal) as the
    default. Phase 5b.2 replaces it with an agent-running backend that
    invokes the same ``_execute_prompt_submit`` path the legacy worker
    used, redirecting ``run_control.publish_recorded_event`` to
    ``emit(EventFrame(params))``.

    Methods are async to keep the call sites uniform — even the stub
    awaits a no-op so the run loop stays cancel-aware."""

    async def start(self, frame: RunStartFrame, emit: Emit) -> None:  # pragma: no cover - abstract
        raise NotImplementedError

    async def cancel(self, run_id: str) -> None:
        return None

    async def shutdown(self) -> None:
        return None


class _StubBackend(WorkerRunBackend):
    """Default backend used until Phase 5b.2 wires real agent execution."""

    async def start(self, frame: RunStartFrame, emit: Emit) -> None:
        await emit(LogFrame(level="info", text=f"stub: run.start run_id={frame.run_id}"))
        await emit(RunTerminalFrame(run_id=frame.run_id, status="stubbed"))


class WorkerInteractiveResponder:
    """Routes an ``InteractiveResponseFrame`` to whatever in-process
    registry holds the worker thread blocked on the matching pending
    request. Injectable so tests don't need to fake the tools/* modules."""

    async def resolve(self, frame: InteractiveResponseFrame) -> bool:  # pragma: no cover - abstract
        raise NotImplementedError


class RealInteractiveResponder(WorkerInteractiveResponder):
    """Production responder. Lazily imports the per-kind registry the
    first time it's needed so ``run_worker`` stays import-cheap for unit
    tests that monkey-patch the resolve functions.

    Kind → registry:
        clarify   → ``tools.clarify_gateway.resolve_gateway_clarify``
                     (keyed by ``clarify_id``)
        approval  → ``tools.approval.resolve_gateway_approval``
                     (keyed by ``session_key`` — caller's ``request_id``)
        secret/sudo → ``tui_gateway.methods.prompt._pending``
                     (keyed by ``request_id`` — same dict, different
                      answer-field name in the original handler)
    """

    async def resolve(self, frame: InteractiveResponseFrame) -> bool:
        # Try the kind-specific registry FIRST (legacy/tools-driven
        # flows: ``tools.clarify_gateway.register`` /
        # ``tools.approval.submit_pending``). Fall through to the
        # generic ``server._pending`` dict (the Dovie-native
        # ``server._block`` path) because the desktop's clarify tool
        # bypasses ``clarify_gateway.register`` entirely and just
        # rid-keyed-blocks on ``_pending`` instead — the
        # kind-specific resolver returns False for that flow, and
        # the answer would otherwise vanish.
        answer = frame.answer
        answer_text = _stringify_answer(answer)
        if frame.kind == "clarify":
            from tools.clarify_gateway import resolve_gateway_clarify
            if resolve_gateway_clarify(frame.request_id, answer_text):
                return True
        elif frame.kind == "approval":
            from tools.approval import resolve_gateway_approval
            choice = answer_text or "once"
            if resolve_gateway_approval(frame.request_id, choice) > 0:
                return True
        return _resolve_generic_pending(frame.request_id, answer_text)


def _stringify_answer(answer: Any) -> str:
    if answer is None:
        return ""
    if isinstance(answer, str):
        return answer
    return json.dumps(answer, ensure_ascii=False, separators=(",", ":"))


def _resolve_generic_pending(request_id: str, answer: str) -> bool:
    """Unblock a secret/sudo waiter parked in
    ``tui_gateway.methods.prompt._pending``. Returns True iff a pending
    entry existed."""
    try:
        from tui_gateway.methods import prompt as _prompt_mod
    except Exception:
        return False
    lock = getattr(_prompt_mod, "_prompt_lock", None)
    pending = getattr(_prompt_mod, "_pending", None)
    answers = getattr(_prompt_mod, "_answers", None)
    if lock is None or pending is None or answers is None:
        return False
    with lock:
        entry = pending.get(request_id)
        if entry is None:
            return False
        _, event = entry
        answers[request_id] = answer
        event.set()
    return True


def _build_default_handler(
    backend: WorkerRunBackend,
    responder: WorkerInteractiveResponder,
    active_runs: set[str],
) -> FrameHandler:
    """Wraps a backend + responder into the ``FrameHandler`` shape the
    run loop expects."""

    async def handler(proto: WorkerProtocol, frame: IncomingFrame) -> None:
        if isinstance(frame, RunStartFrame):
            active_runs.add(frame.run_id)
            try:
                await backend.start(frame, proto.emit)
            except Exception as exc:
                # The backend SHOULD emit its own terminal frame on
                # failure; if it doesn't (or raises before emitting),
                # synthesize one so the main side doesn't hang.
                await proto.emit(
                    RunTerminalFrame(
                        run_id=frame.run_id,
                        status="failed",
                        stored_session_id=frame.stored_session_id,
                        turn_id=frame.turn_id,
                        message=str(exc),
                    )
                )
            finally:
                active_runs.discard(frame.run_id)
        elif isinstance(frame, RunCancelFrame):
            await backend.cancel(frame.run_id)
        elif isinstance(frame, InteractiveResponseFrame):
            resolved = await responder.resolve(frame)
            if not resolved:
                await proto.emit_log(
                    "warn",
                    f"interactive.response: no pending {frame.kind} "
                    f"for request_id={frame.request_id}",
                )
        elif isinstance(frame, ActivityEventFrame):
            if frame.kind != "activity":
                await proto.emit_log("warn", f"event: unsupported kind={frame.kind!r}")
                return
            try:
                from agent.activity_event_bus import get_default_activity_event_bus

                bus = get_default_activity_event_bus()
                if bus is not None:
                    bus.push(frame.event)
            except Exception as exc:
                await proto.emit_log("warn", f"activity event route failed: {exc}")
        elif isinstance(frame, RuntimeEnvUpdateFrame):
            for key, value in frame.env_updates.items():
                if value:
                    os.environ[key] = value
                else:
                    os.environ.pop(key, None)
            await proto.emit_log(
                "info",
                f"[worker] runtime env updated keys={list(frame.env_updates.keys())}",
            )

    return handler


def _build_default_backend() -> WorkerRunBackend:
    """Production backend factory.

    Phase 5c.2 binds the runner to ``tui_gateway.services.agent_runner.
    run_agent`` — the real ``_execute_prompt_submit`` invocation path.
    If either ``AgentRunBackend`` or ``agent_runner`` is unavailable
    (e.g. a unit-test launch of run_worker without the agent stack
    installed), fall back to ``_StubBackend`` so the protocol still
    round-trips cleanly.
    """
    try:
        from tui_gateway.services.agent_run_backend import AgentRunBackend
        from tui_gateway.services.agent_runner import run_agent, setup_worker_environment
    except Exception:
        return _StubBackend()
    try:
        # Eagerly run the env setup so the first ``run.start`` frame
        # doesn't pay the (heavy) Hermes server import cost on the
        # critical path. The agent runner's lazy guard tolerates a
        # repeated call.
        setup_worker_environment()
    except Exception:
        # Setup failure shouldn't crash the worker — the runner will
        # retry, and any subsequent run.start will surface the error
        # as a failed RunTerminalFrame.
        pass
    return AgentRunBackend(runner=run_agent)


async def _main_async() -> int:
    from agent.activity_event_bus import (
        ActivityEventBus,
        set_default_activity_event_bus,
    )
    from tui_gateway.services.worker_db_proxy import (
        WorkerDBProxy,
        set_default_worker_db_proxy,
    )
    from tui_gateway.services.worker_rpc_proxy import (
        WorkerRpcProxy,
        set_default_worker_rpc_proxy,
    )

    db_proxy = WorkerDBProxy(_StdoutJsonRpcWriter())
    rpc_proxy = WorkerRpcProxy(_StdoutJsonRpcWriter())
    activity_bus = ActivityEventBus()
    set_default_worker_db_proxy(db_proxy)
    set_default_worker_rpc_proxy(rpc_proxy)
    set_default_activity_event_bus(activity_bus)
    backend: WorkerRunBackend = _build_default_backend()
    responder: WorkerInteractiveResponder = RealInteractiveResponder()
    active_runs: set[str] = set()

    def _handle_jsonrpc_reply(reply: dict[str, Any]) -> bool:
        return rpc_proxy.handle_reply(reply) or db_proxy.handle_reply(reply)

    proto = WorkerProtocol(
        lines_in=_stdin_lines(),
        emit=_stdout_writer(),
        handler=_build_default_handler(backend, responder, active_runs),
        db_reply_handler=_handle_jsonrpc_reply,
    )
    await proto.emit_log("info", "run_worker: started")
    try:
        await proto.run()
    finally:
        try:
            await backend.shutdown()
        except Exception:
            pass
        db_proxy.close()
        rpc_proxy.close()
        set_default_worker_db_proxy(None)
        set_default_worker_rpc_proxy(None)
        set_default_activity_event_bus(None)
        await proto.emit_log("info", "run_worker: exiting")
    return 0


def main() -> None:  # pragma: no cover — process entry
    raise SystemExit(asyncio.run(_main_async()))


if __name__ == "__main__":  # pragma: no cover
    main()
