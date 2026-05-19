import asyncio
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
