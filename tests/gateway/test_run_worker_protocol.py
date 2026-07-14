"""Unit tests for ``tui_gateway.run_worker`` — Phase 4a codec + loop.

Covers:
- ``decode_incoming`` round-trips for every inbound op
- malformed input → ``FrameDecodeError``
- ``encode_outgoing`` for every outbound type
- ``WorkerProtocol.run`` end-to-end via in-memory queues:
    * handler invocation order
    * decode-error path (logged, loop continues)
    * shutdown frame stops the loop
    * stdin EOF stops the loop
"""

from __future__ import annotations

import asyncio
import json
import sys
from typing import AsyncIterator

import pytest

from tui_gateway.run_worker import (
    EventFrame,
    FrameDecodeError,
    InteractiveRequestFrame,
    InteractiveResponseFrame,
    LogFrame,
    RealInteractiveResponder,
    RunCancelFrame,
    RunStartFrame,
    RunTerminalFrame,
    ShutdownFrame,
    WorkerProtocol,
    WorkerReadyFrame,
    WorkerRunBackend,
    _build_default_handler,
    _StubBackend,
    decode_incoming,
    decode_outgoing,
    encode_incoming,
    encode_outgoing,
)


# ── decode_incoming ──────────────────────────────────────────────────


def test_decode_run_start_minimum_fields() -> None:
    frame = decode_incoming(
        json.dumps(
            {
                "op": "run.start",
                "run_id": "run-1",
                "turn_id": "turn-1",
                "conversation_session_id": "sess-1",
                "prompt": "hi",
            }
        )
    )
    assert isinstance(frame, RunStartFrame)
    assert frame.run_id == "run-1"
    assert frame.turn_id == "turn-1"
    assert frame.conversation_session_id == "sess-1"
    assert frame.prompt == "hi"
    assert frame.params == {}


def test_decode_run_start_with_params() -> None:
    frame = decode_incoming(
        json.dumps(
            {
                "op": "run.start",
                "run_id": "run-1",
                "turn_id": "turn-1",
                "conversation_session_id": "sess-1",
                "prompt": "",
                "params": {"model": "claude-opus", "extras": {"k": 1}},
            }
        )
    )
    assert isinstance(frame, RunStartFrame)
    assert frame.params == {"model": "claude-opus", "extras": {"k": 1}}


def test_decode_run_cancel() -> None:
    frame = decode_incoming(json.dumps({"op": "run.cancel", "run_id": "run-7"}))
    assert frame == RunCancelFrame(run_id="run-7")


def test_decode_interactive_response_each_kind() -> None:
    for kind in ("clarify", "approval", "secret", "sudo"):
        frame = decode_incoming(
            json.dumps(
                {
                    "op": "interactive.response",
                    "kind": kind,
                    "request_id": f"req-{kind}",
                    "answer": {"k": "v"},
                }
            )
        )
        assert isinstance(frame, InteractiveResponseFrame)
        assert frame.kind == kind
        assert frame.request_id == f"req-{kind}"
        assert frame.answer == {"k": "v"}


def test_decode_interactive_response_rejects_unknown_kind() -> None:
    with pytest.raises(FrameDecodeError, match="kind="):
        decode_incoming(
            json.dumps(
                {
                    "op": "interactive.response",
                    "kind": "bogus",
                    "request_id": "x",
                }
            )
        )


def test_decode_shutdown() -> None:
    assert isinstance(decode_incoming('{"op":"shutdown"}'), ShutdownFrame)


@pytest.mark.parametrize(
    "raw, match",
    [
        ("", "empty line"),
        ("not-json", "invalid JSON"),
        ("[]", "must be a JSON object"),
        ('{"foo":1}', "missing string field 'op'"),
        ('{"op":"unknown"}', "unknown op"),
        ('{"op":"run.cancel"}', "must be a string"),
        ('{"op":"run.start", "run_id":"a"}', "must be a string"),  # missing turn_id
    ],
)
def test_decode_rejects_malformed(raw: str, match: str) -> None:
    with pytest.raises(FrameDecodeError, match=match):
        decode_incoming(raw)


# ── encode_outgoing ──────────────────────────────────────────────────


def _roundtrip_out(frame) -> dict:
    return json.loads(encode_outgoing(frame))


def test_encode_event() -> None:
    out = _roundtrip_out(EventFrame(params={"type": "message.delta", "text": "x"}))
    assert out == {"op": "event", "params": {"type": "message.delta", "text": "x"}}


def test_encode_interactive_request() -> None:
    out = _roundtrip_out(
        InteractiveRequestFrame(
            kind="clarify",
            request_id="req-1",
            payload={"question": "ok?"},
        )
    )
    assert out == {
        "op": "interactive.request",
        "kind": "clarify",
        "request_id": "req-1",
        "payload": {"question": "ok?"},
    }


def test_encode_run_terminal() -> None:
    out = _roundtrip_out(RunTerminalFrame(run_id="run-1", status="failed"))
    assert out == {"op": "run.terminal", "run_id": "run-1", "status": "failed"}


def test_encode_log() -> None:
    out = _roundtrip_out(LogFrame(level="warn", text="something"))
    assert out == {"op": "log", "level": "warn", "text": "something"}


def test_encode_worker_ready() -> None:
    frame = WorkerReadyFrame(
        ready=True,
        bootstrap_ms=123.5,
        stages_ms={"agent_modules": 100.0},
    )
    assert _roundtrip_out(frame) == {
        "op": "worker.ready",
        "ready": True,
        "bootstrap_ms": 123.5,
        "stages_ms": {"agent_modules": 100.0},
    }


def test_encode_no_trailing_newline() -> None:
    assert "\n" not in encode_outgoing(LogFrame(level="info", text="x"))


def test_encode_non_ascii_payload_compact() -> None:
    out = encode_outgoing(EventFrame(params={"text": "你好"}))
    assert "你好" in out
    assert " " not in out  # compact separators


# ── Reverse codec (used by main-side WorkerSupervisor) ───────────────


@pytest.mark.parametrize(
    "frame",
    [
        RunStartFrame(
            run_id="r1", turn_id="t1", conversation_session_id="s1",
            prompt="hi", params={"model": "claude-opus"},
        ),
        RunCancelFrame(run_id="r2"),
        InteractiveResponseFrame(kind="clarify", request_id="req-1", answer="yes"),
        InteractiveResponseFrame(kind="approval", request_id="req-2", answer={"choice": "deny"}),
        ShutdownFrame(),
    ],
)
def test_incoming_encode_decode_roundtrip(frame) -> None:
    assert decode_incoming(encode_incoming(frame)) == frame


@pytest.mark.parametrize(
    "frame",
    [
        EventFrame(params={"type": "message.delta", "text": "你好"}),
        InteractiveRequestFrame(kind="clarify", request_id="x", payload={"q": "ok?"}),
        RunTerminalFrame(run_id="r1", status="completed"),
        LogFrame(level="info", text="boot"),
        WorkerReadyFrame(ready=True, bootstrap_ms=10.0, stages_ms={"backend": 1.0}),
    ],
)
def test_outgoing_encode_decode_roundtrip(frame) -> None:
    assert decode_outgoing(encode_outgoing(frame)) == frame


@pytest.mark.parametrize(
    "raw, match",
    [
        ("", "empty line"),
        ("not-json", "invalid JSON"),
        ('{"op":"event"}', None),  # params defaults to {} — should succeed
        ('{"op":"unknown"}', "unknown outbound op"),
        ('{"op":"run.terminal", "run_id":"r"}', "must be a string"),  # missing status
        ('{"op":"log", "level":"info"}', "must be a string"),  # missing text
    ],
)
def test_decode_outgoing_validation(raw: str, match) -> None:
    if match is None:
        # success path: empty event params decode to EventFrame(params={})
        assert decode_outgoing(raw) == EventFrame(params={})
    else:
        with pytest.raises(FrameDecodeError, match=match):
            decode_outgoing(raw)


# ── WorkerProtocol.run via in-memory queues ──────────────────────────


class _Sink:
    """Captures emitted lines and exposes them as plain strings."""

    def __init__(self) -> None:
        self.lines: list[str] = []

    async def write(self, payload: str) -> None:
        self.lines.append(payload)

    def decoded(self) -> list[dict]:
        return [json.loads(line.rstrip("\n")) for line in self.lines]


async def _lines_from(items: list[str]) -> AsyncIterator[str]:
    for item in items:
        yield item


@pytest.mark.asyncio
async def test_run_dispatches_each_frame_and_stops_on_shutdown() -> None:
    seen: list[type] = []

    async def handler(proto: WorkerProtocol, frame) -> None:
        seen.append(type(frame))
        if isinstance(frame, RunStartFrame):
            await proto.emit(RunTerminalFrame(run_id=frame.run_id, status="ok"))

    sink = _Sink()
    proto = WorkerProtocol(
        lines_in=_lines_from(
            [
                json.dumps(
                    {
                        "op": "run.start",
                        "run_id": "r1",
                        "turn_id": "t1",
                        "conversation_session_id": "s1",
                        "prompt": "",
                    }
                ),
                json.dumps({"op": "run.cancel", "run_id": "r1"}),
                json.dumps({"op": "shutdown"}),
                json.dumps({"op": "run.cancel", "run_id": "ignored"}),
            ]
        ),
        emit=sink.write,
        handler=handler,
    )
    await proto.run()
    # ``RunStartFrame`` now dispatches as a background task so the run
    # loop stays free to read further frames while the agent runs —
    # ordering between the start handler completion and the cancel
    # handler is no longer guaranteed. Both must be dispatched; the
    # shutdown frame stops the loop before its handler runs.
    assert set(seen) == {RunStartFrame, RunCancelFrame}
    # Emitted terminal frame for the run.start
    terminals = [f for f in sink.decoded() if f.get("op") == "run.terminal"]
    assert terminals == [{"op": "run.terminal", "run_id": "r1", "status": "ok"}]


@pytest.mark.asyncio
async def test_run_logs_decode_error_and_continues() -> None:
    seen: list[type] = []

    async def handler(proto: WorkerProtocol, frame) -> None:
        seen.append(type(frame))

    sink = _Sink()
    proto = WorkerProtocol(
        lines_in=_lines_from(
            [
                "not-json",
                json.dumps({"op": "run.cancel", "run_id": "r1"}),
                json.dumps({"op": "shutdown"}),
            ]
        ),
        emit=sink.write,
        handler=handler,
    )
    await proto.run()
    assert seen == [RunCancelFrame]
    logs = [f for f in sink.decoded() if f.get("op") == "log"]
    assert any("decode error" in f["text"] for f in logs)


@pytest.mark.asyncio
async def test_run_stops_on_stdin_eof() -> None:
    seen: list[type] = []

    async def handler(proto: WorkerProtocol, frame) -> None:
        seen.append(type(frame))

    proto = WorkerProtocol(
        lines_in=_lines_from(
            [
                json.dumps({"op": "run.cancel", "run_id": "r1"}),
                # iterator exhausts here — protocol must return cleanly
            ]
        ),
        emit=_Sink().write,
        handler=handler,
    )
    await proto.run()
    assert seen == [RunCancelFrame]


@pytest.mark.asyncio
async def test_run_propagates_handler_exception() -> None:
    async def boom(proto: WorkerProtocol, frame) -> None:
        raise RuntimeError("handler crashed")

    proto = WorkerProtocol(
        lines_in=_lines_from([json.dumps({"op": "run.cancel", "run_id": "r1"})]),
        emit=_Sink().write,
        handler=boom,
    )
    with pytest.raises(RuntimeError, match="handler crashed"):
        await proto.run()


# ── Backend / interactive responder (Phase 5b) ───────────────────────


class _RecordingBackend(WorkerRunBackend):
    def __init__(self) -> None:
        self.starts: list[RunStartFrame] = []
        self.cancels: list[str] = []
        self.shutdowns: int = 0

    async def start(self, frame, emit) -> None:
        self.starts.append(frame)
        await emit(LogFrame(level="info", text=f"backend: started {frame.run_id}"))
        await emit(RunTerminalFrame(run_id=frame.run_id, status="completed"))

    async def cancel(self, run_id: str) -> None:
        self.cancels.append(run_id)

    async def shutdown(self) -> None:
        self.shutdowns += 1


class _RecordingResponder:
    def __init__(self, resolved: bool = True) -> None:
        self.resolved = resolved
        self.calls: list[InteractiveResponseFrame] = []

    async def resolve(self, frame: InteractiveResponseFrame) -> bool:
        self.calls.append(frame)
        return self.resolved


@pytest.mark.asyncio
async def test_handler_dispatches_run_start_to_backend() -> None:
    backend = _RecordingBackend()
    responder = _RecordingResponder()
    active: set[str] = set()
    handler = _build_default_handler(backend, responder, active)
    sink = _Sink()
    proto = WorkerProtocol(
        lines_in=_lines_from(
            [
                json.dumps(
                    {
                        "op": "run.start",
                        "run_id": "r1",
                        "turn_id": "t1",
                        "conversation_session_id": "s1",
                        "prompt": "hi",
                    }
                ),
                json.dumps({"op": "shutdown"}),
            ]
        ),
        emit=sink.write,
        handler=handler,
    )
    await proto.run()
    assert len(backend.starts) == 1
    assert backend.starts[0].run_id == "r1"
    assert active == set()  # cleared after start finished
    terms = [f for f in sink.decoded() if f.get("op") == "run.terminal"]
    assert terms == [{"op": "run.terminal", "run_id": "r1", "status": "completed"}]


@pytest.mark.asyncio
async def test_handler_emits_synthesized_terminal_on_backend_exception() -> None:
    class _BoomBackend(WorkerRunBackend):
        async def start(self, frame, emit) -> None:
            raise RuntimeError("boom")

    handler = _build_default_handler(_BoomBackend(), _RecordingResponder(), set())
    sink = _Sink()
    proto = WorkerProtocol(
        lines_in=_lines_from(
            [
                json.dumps(
                    {
                        "op": "run.start",
                        "run_id": "r-fail",
                        "turn_id": "t1",
                        "conversation_session_id": "s1",
                        "prompt": "",
                    }
                ),
                json.dumps({"op": "shutdown"}),
            ]
        ),
        emit=sink.write,
        handler=handler,
    )
    await proto.run()
    terms = [f for f in sink.decoded() if f.get("op") == "run.terminal"]
    assert len(terms) == 1
    assert terms[0]["status"] == "failed"
    assert terms[0]["run_id"] == "r-fail"
    assert "boom" in terms[0]["message"]


@pytest.mark.asyncio
async def test_handler_dispatches_run_cancel() -> None:
    backend = _RecordingBackend()
    handler = _build_default_handler(backend, _RecordingResponder(), set())
    proto = WorkerProtocol(
        lines_in=_lines_from(
            [
                json.dumps({"op": "run.cancel", "run_id": "r9"}),
                json.dumps({"op": "shutdown"}),
            ]
        ),
        emit=_Sink().write,
        handler=handler,
    )
    await proto.run()
    assert backend.cancels == ["r9"]


@pytest.mark.asyncio
async def test_handler_routes_interactive_response_and_warns_on_miss() -> None:
    responder = _RecordingResponder(resolved=False)
    handler = _build_default_handler(_RecordingBackend(), responder, set())
    sink = _Sink()
    proto = WorkerProtocol(
        lines_in=_lines_from(
            [
                json.dumps(
                    {
                        "op": "interactive.response",
                        "kind": "clarify",
                        "request_id": "req-1",
                        "answer": "yes",
                    }
                ),
                json.dumps({"op": "shutdown"}),
            ]
        ),
        emit=sink.write,
        handler=handler,
    )
    await proto.run()
    assert len(responder.calls) == 1
    logs = [f for f in sink.decoded() if f.get("op") == "log"]
    assert any("no pending clarify" in f["text"] for f in logs)


@pytest.mark.asyncio
async def test_stub_backend_emits_stubbed_terminal() -> None:
    sink_lines: list[dict] = []

    async def emit(frame) -> None:
        sink_lines.append({"type": type(frame).__name__, "frame": frame})

    backend = _StubBackend()
    await backend.start(
        RunStartFrame(
            run_id="rx", turn_id="tx", conversation_session_id="sx", prompt="",
        ),
        emit,
    )
    types = [item["type"] for item in sink_lines]
    assert types == ["LogFrame", "RunTerminalFrame"]
    assert sink_lines[-1]["frame"].status == "stubbed"


# ── RealInteractiveResponder routing ────────────────────────────────


@pytest.mark.asyncio
async def test_real_responder_clarify_calls_resolve_gateway_clarify(monkeypatch) -> None:
    calls: list[tuple[str, str]] = []

    def fake_resolve(clarify_id, response) -> bool:
        calls.append((clarify_id, response))
        return True

    import tools.clarify_gateway as cg
    monkeypatch.setattr(cg, "resolve_gateway_clarify", fake_resolve)
    responder = RealInteractiveResponder()
    ok = await responder.resolve(
        InteractiveResponseFrame(kind="clarify", request_id="req-1", answer="yes")
    )
    assert ok is True
    assert calls == [("req-1", "yes")]


@pytest.mark.asyncio
async def test_real_responder_approval_calls_resolve_gateway_approval(monkeypatch) -> None:
    calls: list[tuple[str, str]] = []

    def fake_resolve(session_key, choice, resolve_all: bool = False) -> int:
        calls.append((session_key, choice))
        return 1

    import tools.approval as approval_mod
    monkeypatch.setattr(approval_mod, "resolve_gateway_approval", fake_resolve)
    responder = RealInteractiveResponder()
    ok = await responder.resolve(
        InteractiveResponseFrame(kind="approval", request_id="sess-1", answer="once")
    )
    assert ok is True
    assert calls == [("sess-1", "once")]


@pytest.mark.asyncio
async def test_real_responder_approval_blank_answer_defaults_to_once(monkeypatch) -> None:
    captured: list[tuple[str, str]] = []

    def fake_resolve(session_key, choice, resolve_all: bool = False) -> int:
        captured.append((session_key, choice))
        return 1

    import tools.approval as approval_mod
    monkeypatch.setattr(approval_mod, "resolve_gateway_approval", fake_resolve)
    responder = RealInteractiveResponder()
    await responder.resolve(
        InteractiveResponseFrame(kind="approval", request_id="sess-1", answer=None)
    )
    assert captured == [("sess-1", "once")]


@pytest.mark.asyncio
async def test_real_responder_unknown_kind_returns_false() -> None:
    # Kind validation happens in decode_incoming for inbound frames, but
    # the responder is also reachable from the constructor path. Guard
    # against future schema additions.
    responder = RealInteractiveResponder()
    # Bypass dataclass validation by building one with a kind that's not
    # in the wire schema — we can construct it directly.
    frame = InteractiveResponseFrame(kind="bogus", request_id="x", answer="")
    ok = await responder.resolve(frame)
    assert ok is False


@pytest.mark.asyncio
async def test_real_responder_secret_routes_to_prompt_pending(monkeypatch) -> None:
    import threading
    # Build a minimal stand-in for tui_gateway.methods.prompt that has
    # the registry shape the responder reads from.
    class _FakePromptMod:
        _prompt_lock = threading.Lock()
        _pending: dict = {}
        _answers: dict = {}

    fake_mod = _FakePromptMod()
    ev = threading.Event()
    fake_mod._pending["req-secret"] = (object(), ev)
    # ``from tui_gateway.methods import prompt`` (used in
    # ``_resolve_generic_pending``) consults the parent package
    # attribute first and the sys.modules entry second. If any earlier
    # test imported the real module, the package already has a cached
    # attribute — patching sys.modules alone leaves that attribute
    # pointing at the real module. Override both.
    monkeypatch.setitem(sys.modules, "tui_gateway.methods.prompt", fake_mod)
    import tui_gateway.methods as methods_pkg
    monkeypatch.setattr(methods_pkg, "prompt", fake_mod, raising=False)

    responder = RealInteractiveResponder()
    ok = await responder.resolve(
        InteractiveResponseFrame(kind="secret", request_id="req-secret", answer="abc")
    )
    assert ok is True
    assert fake_mod._answers["req-secret"] == "abc"
    assert ev.is_set()


@pytest.mark.asyncio
async def test_custom_on_decode_error_invoked() -> None:
    errors: list[tuple[str, str]] = []

    async def on_err(exc: FrameDecodeError, line: str) -> None:
        errors.append((str(exc), line))

    async def handler(proto: WorkerProtocol, frame) -> None:
        pass

    proto = WorkerProtocol(
        lines_in=_lines_from(["garbage", json.dumps({"op": "shutdown"})]),
        emit=_Sink().write,
        handler=handler,
        on_decode_error=on_err,
    )
    await proto.run()
    assert len(errors) == 1
    assert "invalid JSON" in errors[0][0]
    assert errors[0][1] == "garbage"
