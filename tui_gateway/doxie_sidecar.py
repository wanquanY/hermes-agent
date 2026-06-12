from __future__ import annotations

import argparse
import asyncio
import hmac
import os
import threading
import time
from urllib.parse import parse_qs, urlparse

SIDECAR_TOKEN_ENV = "DOXIE_SIDECAR_TOKEN"
SIDECAR_PARENT_PID_ENV = "DOXIE_SIDECAR_PARENT_PID"
_PARENT_WATCHDOG_INTERVAL_SECONDS = 2.0
_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1", "[::1]"}
_NATIVE_ELECTRON_ORIGIN_SCHEMES = {"doxie", "doxie-attachment", "doxie-hermes", "file"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--token", default="")
    return parser.parse_args()


def resolve_token(args: argparse.Namespace) -> str:
    token = os.getenv(SIDECAR_TOKEN_ENV, "").strip()
    if token:
        return token
    legacy = str(getattr(args, "token", "") or "").strip()
    if legacy:
        return legacy
    raise SystemExit(f"Hermes Doxie gateway requires {SIDECAR_TOKEN_ENV}")


def expected_parent_pid() -> int:
    raw = os.getenv(SIDECAR_PARENT_PID_ENV, "").strip()
    if not raw:
        return 0
    try:
        return max(0, int(raw))
    except ValueError:
        return 0


def parent_process_still_owns_sidecar(parent_pid: int) -> bool:
    if parent_pid <= 0:
        return True
    getppid = getattr(os, "getppid", None)
    if callable(getppid) and int(getppid()) != parent_pid:
        return False
    try:
        os.kill(parent_pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def start_parent_watchdog() -> threading.Thread | None:
    parent_pid = expected_parent_pid()
    if parent_pid <= 0:
        return None

    def watch_parent() -> None:
        while True:
            time.sleep(_PARENT_WATCHDOG_INTERVAL_SECONDS)
            if not parent_process_still_owns_sidecar(parent_pid):
                try:
                    from tui_gateway import server as tui_gateway_server

                    tui_gateway_server._shutdown_sessions()
                except Exception as exc:
                    print(
                        f"[doxie-sidecar] shutdown session cleanup failed before parent-exit: {exc}",
                        flush=True,
                    )
                os._exit(0)

    thread = threading.Thread(
        target=watch_parent,
        daemon=True,
        name="doxie-sidecar-parent-watchdog",
    )
    thread.start()
    return thread


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


def _normalize_host(value: str) -> str:
    raw = str(value or "").strip().lower()
    if raw.startswith("["):
        end = raw.find("]")
        return raw[: end + 1] if end >= 0 else raw
    return raw.split(":", 1)[0]


def is_allowed_host(value: str) -> bool:
    return _normalize_host(value) in _LOOPBACK_HOSTS


def is_allowed_origin(value: str | None) -> bool:
    raw = str(value or "").strip()
    if not raw:
        return True
    if raw == "null":
        return False
    parsed = urlparse(raw)
    return parsed.scheme in _NATIVE_ELECTRON_ORIGIN_SCHEMES


def request_header(ws, name: str) -> str:
    request = getattr(ws, "request", None)
    headers = getattr(request, "headers", None)
    if headers is not None:
        try:
            return str(headers.get(name, "") or "")
        except Exception:
            return ""
    legacy_headers = getattr(ws, "request_headers", None)
    if legacy_headers is not None:
        try:
            return str(legacy_headers.get(name, "") or "")
        except Exception:
            return ""
    return ""


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
    expected_token = resolve_token(args)
    start_parent_watchdog()

    class Adapter(HermesWebSocketAdapter):
        async def receive_text(self) -> str:
            try:
                return await self._ws.recv()
            except websockets.exceptions.ConnectionClosed as exc:
                raise WebSocketDisconnect(code=exc.code) from exc

    async def handler(ws) -> None:
        if not is_allowed_host(request_header(ws, "Host")):
            await ws.close(code=1008, reason="forbidden host")
            return
        if not is_allowed_origin(request_header(ws, "Origin")):
            await ws.close(code=1008, reason="forbidden origin")
            return
        raw_path = request_path(ws)
        if urlparse(raw_path).path != "/api/ws":
            await ws.close(code=1008, reason="unsupported path")
            return
        if not is_authorized(raw_path, expected_token):
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
