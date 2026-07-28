from __future__ import annotations

import os
import platform
import socket
import time
from collections.abc import Callable
from urllib.parse import urlparse


class BrowserGatewayError(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def resolve_browser_cdp_url() -> str:
    """Return the configured browser CDP override without network I/O."""
    env_url = os.environ.get("BROWSER_CDP_URL", "").strip()
    if env_url:
        return env_url
    try:
        from hermes_cli.config import read_raw_config

        cfg = read_raw_config()
        browser_cfg = cfg.get("browser", {}) if isinstance(cfg, dict) else {}
        if isinstance(browser_cfg, dict):
            return str(browser_cfg.get("cdp_url", "") or "").strip()
    except Exception:
        pass
    return ""


def is_default_local_cdp(parsed) -> bool:
    try:
        port = parsed.port or 80
    except ValueError:
        return False

    discovery_path = parsed.path in {"", "/", "/json", "/json/version"}
    return (
        parsed.scheme in {"http", "ws"}
        and parsed.hostname in {"127.0.0.1", "localhost"}
        and port == 9222
        and discovery_path
    )


def http_ok(url: str, timeout: float) -> bool:
    import urllib.request

    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return 200 <= getattr(resp, "status", 200) < 300
    except Exception:
        return False


def probe_urls(parsed) -> list[str]:
    scheme = {"ws": "http", "wss": "https"}.get(parsed.scheme, parsed.scheme)
    root = f"{scheme}://{parsed.netloc}".rstrip("/")
    return [f"{root}/json/version", f"{root}/json"]


def normalize_cdp_url(parsed) -> str:
    if parsed.path.startswith("/devtools/browser/"):
        return parsed.geturl()
    return parsed._replace(path="", params="", query="", fragment="").geturl()


def failure_messages(url: str, port: int, system: str) -> list[str]:
    from hermes_cli.browser_connect import manual_chrome_debug_command

    command = manual_chrome_debug_command(port, system)
    hint = (
        ["Start a Chromium-family browser with remote debugging, then retry /browser connect:", command]
        if command
        else [
            "No supported Chromium-family browser executable was found in this environment.",
            f"Install one or start a Chromium-family browser with --remote-debugging-port={port}, then retry /browser connect.",
        ]
    )
    return [
        f"Chromium-family browser is not reachable at {url}.",
        *hint,
        "Browser not connected — start a Chromium-family browser with remote debugging and retry /browser connect",
    ]


def connect_browser_cdp(
    params: dict,
    *,
    progress: Callable[[str, str], None] | None = None,
) -> dict:
    from hermes_cli.browser_connect import DEFAULT_BROWSER_CDP_URL
    from tools.browser_tool import cleanup_all_browsers

    raw_url = params.get("url")
    if raw_url is not None and not isinstance(raw_url, str):
        raise BrowserGatewayError(
            4015, f"browser url must be a string, got {type(raw_url).__name__}"
        )
    url = (raw_url or "").strip() or DEFAULT_BROWSER_CDP_URL

    system = platform.system()
    messages: list[str] = []

    def announce(message: str, *, level: str = "info") -> None:
        messages.append(message)
        if progress:
            progress(message, level)

    parsed = urlparse(url if "://" in url else f"http://{url}")
    if parsed.scheme not in {"http", "https", "ws", "wss"}:
        raise BrowserGatewayError(4015, f"unsupported browser url: {url}")
    if not parsed.hostname:
        raise BrowserGatewayError(4015, f"missing host in browser url: {url}")
    try:
        port = parsed.port or (443 if parsed.scheme in {"https", "wss"} else 80)
    except ValueError:
        raise BrowserGatewayError(4015, f"invalid port in browser url: {url}") from None

    if is_default_local_cdp(parsed):
        url = DEFAULT_BROWSER_CDP_URL
        parsed = urlparse(url)
        port = parsed.port or 9222

    try:
        if parsed.scheme in {"ws", "wss"} and parsed.path.startswith(
            "/devtools/browser/"
        ):
            try:
                with socket.create_connection((parsed.hostname, port), timeout=2.0):
                    pass
            except OSError as exc:
                raise BrowserGatewayError(
                    5031, f"could not reach browser CDP at {url}: {exc}"
                ) from exc
        else:
            probes = probe_urls(parsed)
            ok = any(http_ok(probe, timeout=2.0) for probe in probes)

            if not ok and is_default_local_cdp(parsed):
                from hermes_cli.browser_connect import try_launch_chrome_debug

                announce(
                    "Chromium-family browser isn't running with remote debugging — attempting to launch..."
                )

                if try_launch_chrome_debug(port, system):
                    for _ in range(20):
                        time.sleep(0.5)
                        if any(http_ok(probe, timeout=1.0) for probe in probes):
                            ok = True
                            break

                if ok:
                    announce(f"Chromium-family browser launched and listening on port {port}")
                else:
                    for line in failure_messages(url, port, system)[1:]:
                        announce(line, level="error")
                    return {"connected": False, "url": url, "messages": messages}
            elif not ok:
                raise BrowserGatewayError(5031, f"could not reach browser CDP at {url}")
            elif is_default_local_cdp(parsed):
                announce(f"Chromium-family browser is already listening on port {port}")

        normalized = normalize_cdp_url(parsed)

        cleanup_all_browsers()
        os.environ["BROWSER_CDP_URL"] = normalized
        cleanup_all_browsers()
    except BrowserGatewayError:
        raise
    except Exception as exc:
        raise BrowserGatewayError(5031, str(exc)) from exc

    payload: dict[str, object] = {"connected": True, "url": normalized}
    if messages:
        payload["messages"] = messages
    return payload


def disconnect_browser_cdp() -> dict:
    def reap() -> None:
        try:
            from tools.browser_tool import cleanup_all_browsers

            cleanup_all_browsers()
        except Exception:
            pass

    reap()
    os.environ.pop("BROWSER_CDP_URL", None)
    reap()
    return {"connected": False}
