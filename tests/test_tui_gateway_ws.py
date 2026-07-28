import asyncio
import concurrent.futures
import json
import threading

from tui_gateway import server
from tui_gateway import ws as ws_module
from tui_gateway.ws import WSTransport, handle_ws


class BlockingFakeWebSocket:
    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.first_send_started = asyncio.Event()
        self.release_first_send = asyncio.Event()

    async def send_text(self, line: str) -> None:
        self.sent.append(json.loads(line))
        if len(self.sent) == 1:
            self.first_send_started.set()
            await self.release_first_send.wait()


def test_ws_transport_prioritizes_rpc_response_over_queued_events():
    async def run() -> None:
        fake_ws = BlockingFakeWebSocket()
        transport = WSTransport(fake_ws, asyncio.get_running_loop())

        transport.write({"jsonrpc": "2.0", "method": "event", "params": {"type": "message.delta"}})
        transport.write({"jsonrpc": "2.0", "method": "event", "params": {"type": "tool.start"}})
        await fake_ws.first_send_started.wait()

        response_task = asyncio.create_task(
            transport.write_async({"jsonrpc": "2.0", "id": "interrupt", "result": {"status": "interrupted"}})
        )
        await asyncio.sleep(0)
        fake_ws.release_first_send.set()

        assert await asyncio.wait_for(response_task, timeout=1)
        await asyncio.sleep(0)
        transport.close()

        assert [frame.get("id") or frame.get("params", {}).get("type") for frame in fake_ws.sent[:3]] == [
            "message.delta",
            "interrupt",
            "tool.start",
        ]

    asyncio.run(run())


def test_ws_transport_batches_stream_frames_and_flushes_before_control_frame():
    class FakeWebSocket:
        def __init__(self) -> None:
            self.sent: list[dict] = []

        async def send_text(self, line: str) -> None:
            self.sent.append(json.loads(line))

    async def run() -> None:
        fake_ws = FakeWebSocket()
        transport = WSTransport(fake_ws, asyncio.get_running_loop())
        assert transport.write({"method": "event", "params": {"type": "message.delta", "transient": True, "payload": {"delta": "a"}}})
        assert transport.write({"method": "event", "params": {"type": "subagent.output_delta", "transient": True, "payload": {"delta": "b"}}})
        assert fake_ws.sent == []

        assert await transport.write_async({"id": "control", "result": {"ok": True}})
        assert [frame.get("id") or frame["params"]["type"] for frame in fake_ws.sent] == [
            "message.delta",
            "subagent.output_delta",
            "control",
        ]
        transport.close()

    asyncio.run(run())


def test_ws_transport_coalesces_contiguous_utf16_stream_and_preserves_tool_boundary():
    class FakeWebSocket:
        def __init__(self) -> None:
            self.sent: list[dict] = []

        async def send_text(self, line: str) -> None:
            self.sent.append(json.loads(line))

    def delta(text: str, offset: int, source_seq: int) -> dict:
        return {
            "method": "event",
            "params": {
                "type": "message.delta",
                "transient": True,
                "conversation_session_id": "session-1",
                "execution_session_id": "runtime-1",
                "run_id": "run-1",
                "turn_id": "turn-1",
                "runtime_source_seq": source_seq,
                "text_stream": {
                    "mode": "append",
                    "delta": text,
                    "text": text,
                    "offset": offset,
                },
                "payload": {
                    "mode": "append",
                    "delta": text,
                    "text": text,
                    "offset": offset,
                },
            },
        }

    async def run() -> None:
        fake_ws = FakeWebSocket()
        transport = WSTransport(fake_ws, asyncio.get_running_loop())
        assert transport.write(delta("A", 0, 10))
        assert transport.write(delta("😀", 1, 11))
        assert transport.write(delta("中", 3, 12))
        assert transport.write({
            "method": "event",
            "params": {
                "type": "tool.start",
                "seq": 1,
                "runtime_source_seq": 13,
                "payload": {"tool_id": "tool-1", "name": "terminal"},
            },
        })
        assert transport.write(delta("B", 4, 14))
        await transport.write_async({"id": "control", "result": {"ok": True}})

        assert [
            frame.get("id") or frame["params"]["type"]
            for frame in fake_ws.sent
        ] == ["message.delta", "tool.start", "message.delta", "control"]
        merged = fake_ws.sent[0]["params"]
        assert merged["runtime_source_seq"] == 12
        assert merged["payload"]["delta"] == "A😀中"
        assert merged["payload"]["text"] == "A😀中"
        assert merged["payload"]["offset"] == 0
        assert merged["text_stream"]["delta"] == "A😀中"
        assert merged["text_stream"]["text"] == "A😀中"
        assert merged["text_stream"]["offset"] == 0
        transport.close()

    asyncio.run(run())


def test_ws_transport_loop_stall_timeout_does_not_close_transport(monkeypatch):
    class SlowFuture:
        def result(self, timeout=None):
            raise concurrent.futures.TimeoutError()

    def fake_schedule(coroutine, _loop):
        coroutine.close()
        return SlowFuture()

    monkeypatch.setattr("agent.async_utils.safe_schedule_threadsafe", fake_schedule)

    async def run() -> None:
        transport = WSTransport(object(), asyncio.get_running_loop())
        result = await asyncio.to_thread(
            transport.write,
            {"id": "slow", "result": {"ok": True}},
        )
        assert result is True
        assert transport._closed is False  # noqa: SLF001
        transport.close()

    asyncio.run(run())


class DispatchFakeWebSocket:
    def __init__(self, frames: list[dict], release_after: threading.Event) -> None:
        self.frames = [json.dumps(frame) for frame in frames]
        self.release_after = release_after
        self.sent: list[dict] = []

    async def accept(self) -> None:
        return None

    async def receive_text(self) -> str:
        if self.frames:
            return self.frames.pop(0)
        await asyncio.to_thread(self.release_after.wait, 1)
        raise ws_module._WebSocketDisconnect()

    async def send_text(self, line: str) -> None:
        self.sent.append(json.loads(line))

    async def close(self) -> None:
        return None


def test_ws_gateway_ready_advertises_timeline_contract():
    release_after = threading.Event()
    fake_ws = DispatchFakeWebSocket([], release_after)

    async def run() -> None:
        await asyncio.wait_for(handle_ws(fake_ws), timeout=2)

    asyncio.run(run())

    ready = fake_ws.sent[0]["params"]
    payload = ready["payload"]
    assert ready["type"] == "gateway.ready"
    assert payload["contractVersion"] == "3.1"
    assert payload["capabilities"] == {
        "cursor": {
            "afterSeq": True,
            "afterId": True,
            "beforeSeq": True,
            "beforeId": True,
        },
        "history": {"canonical": False},
        "toolEvents": {"canonical": True},
    }
    assert payload["deprecations"] == []
    assert "runtimeSourceSeq" not in payload["deprecations"]


def test_ws_receive_loop_does_not_wait_for_previous_response_flush(monkeypatch):
    first_release = threading.Event()
    second_seen = threading.Event()
    fake_ws = DispatchFakeWebSocket(
        [
            {"jsonrpc": "2.0", "id": "h15", "method": "session.status", "params": {}},
            {"jsonrpc": "2.0", "id": "h16", "method": "session.interrupt", "params": {"session_id": "sid"}},
        ],
        second_seen,
    )

    def fake_dispatch(req, _transport):
        if req.get("id") == "h15":
            first_release.wait(timeout=1)
        if req.get("id") == "h16":
            second_seen.set()
        return {"jsonrpc": "2.0", "id": req.get("id"), "result": {"ok": True}}

    monkeypatch.setattr(server, "dispatch", fake_dispatch)

    async def run() -> None:
        await asyncio.wait_for(handle_ws(fake_ws), timeout=2)

    try:
        asyncio.run(run())
        assert second_seen.is_set()
    finally:
        first_release.set()


def test_ws_handler_exception_returns_request_error_and_keeps_connection(monkeypatch):
    release_after = threading.Event()
    fake_ws = DispatchFakeWebSocket(
        [
            {"jsonrpc": "2.0", "id": "failure", "method": "test.inline.failure", "params": {}},
            {"jsonrpc": "2.0", "id": "success", "method": "test.inline.success", "params": {}},
        ],
        release_after,
    )

    def failing_handler(_rid, _params):
        raise RuntimeError("boom")

    def success_handler(_rid, _params):
        release_after.set()
        return {"jsonrpc": "2.0", "id": "success", "result": {"ok": True}}

    monkeypatch.setitem(server._methods, "test.inline.failure", failing_handler)
    monkeypatch.setitem(server._methods, "test.inline.success", success_handler)

    async def run() -> None:
        await asyncio.wait_for(handle_ws(fake_ws), timeout=2)

    asyncio.run(run())

    responses = [frame for frame in fake_ws.sent if frame.get("id")]
    assert responses == [
        {
            "jsonrpc": "2.0",
            "id": "failure",
            "error": {"code": -32000, "message": "handler error: boom"},
        },
        {"jsonrpc": "2.0", "id": "success", "result": {"ok": True}},
    ]
