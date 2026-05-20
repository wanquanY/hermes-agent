from __future__ import annotations

import argparse
import asyncio
import hmac
from urllib.parse import parse_qs, urlparse


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--token", required=True)
    return parser.parse_args()


class HermesWebSocketAdapter:
    def __init__(self, ws) -> None:
        self._ws = ws

    async def accept(self) -> None:
        return None

    async def receive_text(self) -> str:
        return await self._ws.recv()

    async def send_text(self, text: str) -> None:
        await self._ws.send(text)

    async def close(self, code: int = 1000) -> None:
        await self._ws.close(code=code)


def request_path(ws) -> str:
    request = getattr(ws, "request", None)
    path = getattr(request, "path", None)
    if path:
        return str(path)
    return str(getattr(ws, "path", "") or "")


def is_authorized(raw_path: str, expected_token: str) -> bool:
    parsed = urlparse(raw_path)
    token = (parse_qs(parsed.query).get("token") or [""])[0]
    return hmac.compare_digest(token.encode(), expected_token.encode())


async def main_async(args: argparse.Namespace) -> None:
    try:
        import websockets
        from starlette.websockets import WebSocketDisconnect
    except Exception as exc:
        raise SystemExit(f"Hermes Doxie gateway requires websockets + starlette: {exc}") from exc

    from tui_gateway import server as tui_gateway_server  # noqa: F401 - registers gateway methods
    from tui_gateway import ws as tui_gateway_ws
    from tui_gateway.services.doxie_cron_runtime import start_cron_ticker, stop_cron_ticker

    handle_ws = tui_gateway_ws.handle_ws

    class Adapter(HermesWebSocketAdapter):
        async def receive_text(self) -> str:
            try:
                return await self._ws.recv()
            except websockets.exceptions.ConnectionClosed as exc:
                raise WebSocketDisconnect(code=exc.code) from exc

    async def handler(ws) -> None:
        raw_path = request_path(ws)
        if urlparse(raw_path).path != "/api/ws":
            await ws.close(code=1008, reason="unsupported path")
            return
        if not is_authorized(raw_path, args.token):
            await ws.close(code=1008, reason="unauthorized")
            return
        await handle_ws(Adapter(ws))

    print(f"Doxie Hermes Gateway listening on ws://{args.host}:{args.port}/api/ws", flush=True)
    start_cron_ticker()
    try:
        async with websockets.serve(handler, args.host, args.port):
            await asyncio.Future()
    finally:
        stop_cron_ticker()


def main() -> None:
    try:
        asyncio.run(main_async(parse_args()))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
