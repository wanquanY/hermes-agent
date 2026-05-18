"""Cua-driver backend (macOS only).

Speaks MCP over stdio to `cua-driver`. The Python `mcp` SDK is async, so we
run a dedicated asyncio event loop on a background thread and marshal sync
calls through it.

Install: `/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/trycua/cua/main/libs/cua-driver/scripts/install.sh)"`

After install, `cua-driver` is on $PATH and supports `cua-driver mcp` (stdio
transport) which is what we invoke.

The private SkyLight SPIs cua-driver uses (SLEventPostToPid, SLPSPostEvent-
RecordTo, _AXObserverAddNotificationAndCheckRemote) are not Apple-public and
can break on OS updates. Pin the installed version via `HERMES_CUA_DRIVER_
VERSION` if you want reproducibility across an OS bump.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import logging
import os
import platform
import re
import shutil
import subprocess
import sys
import threading
from concurrent.futures import Future
from typing import Any, Dict, List, Optional, Tuple

from tools.computer_use.backend import (
    ActionResult,
    CaptureResult,
    ComputerUseBackend,
    UIElement,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Version pinning
# ---------------------------------------------------------------------------

PINNED_CUA_DRIVER_VERSION = os.environ.get("HERMES_CUA_DRIVER_VERSION", "0.5.0")

_CUA_DRIVER_CMD = os.environ.get("HERMES_CUA_DRIVER_CMD", "cua-driver")
_CUA_DRIVER_ARGS = ["mcp"]  # stdio MCP transport

# Regex to parse list_windows text output lines:
#   "- AppName (pid 12345) "Title" [window_id: 67890]"
_WINDOW_LINE_RE = re.compile(
    r'^-\s+(.+?)\s+\(pid\s+(\d+)\)\s+.*\[window_id:\s+(\d+)\]',
    re.MULTILINE,
)

# Regex to parse element lines from get_window_state AX tree markdown:
#   "  - [N] AXRole "label""
_ELEMENT_LINE_RE = re.compile(
    r'^\s*-\s+\[(\d+)\]\s+(\w+)(?:\s+"([^"]*)")?',
    re.MULTILINE,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _is_macos() -> bool:
    return sys.platform == "darwin"


def _is_arm_mac() -> bool:
    return _is_macos() and platform.machine() == "arm64"


def cua_driver_binary_available() -> bool:
    """True if `cua-driver` is on $PATH or HERMES_CUA_DRIVER_CMD resolves."""
    return bool(shutil.which(_CUA_DRIVER_CMD))


def cua_driver_install_hint() -> str:
    return (
        "cua-driver is not installed. Install with one of:\n"
        "  hermes computer-use install\n"
        "Or run the upstream installer directly:\n"
        '  /bin/bash -c "$(curl -fsSL '
        'https://raw.githubusercontent.com/trycua/cua/main/libs/cua-driver/scripts/install.sh)"\n'
        "Or run `hermes tools` and enable the Computer Use toolset to install it automatically."
    )


def _parse_windows_from_text(text: str) -> List[Dict[str, Any]]:
    """Parse window records from list_windows text output."""
    windows = []
    for m in _WINDOW_LINE_RE.finditer(text):
        windows.append({
            "app_name": m.group(1).strip(),
            "pid": int(m.group(2)),
            "window_id": int(m.group(3)),
            "off_screen": "[off-screen]" in m.group(0),
        })
    return windows


def _int_or_none(value: Any) -> Optional[int]:
    try:
        if value is None:
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _float_or_default(value: Any, default: float = 1.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _normalize_bounds(value: Any) -> Optional[Tuple[int, int, int, int]]:
    """Normalize common rect shapes into (x, y, width, height)."""
    if isinstance(value, dict):
        x = _int_or_none(value.get("x", value.get("left", value.get("minX"))))
        y = _int_or_none(value.get("y", value.get("top", value.get("minY"))))
        w = _int_or_none(value.get("w", value.get("width")))
        h = _int_or_none(value.get("h", value.get("height")))
        if x is not None and y is not None and w is not None and h is not None:
            return (x, y, w, h)
        right = _int_or_none(value.get("right", value.get("maxX")))
        bottom = _int_or_none(value.get("bottom", value.get("maxY")))
        if x is not None and y is not None and right is not None and bottom is not None:
            return (x, y, right - x, bottom - y)
    if isinstance(value, (list, tuple)) and len(value) == 4:
        try:
            return tuple(int(v) for v in value)  # type: ignore[return-value]
        except (TypeError, ValueError):
            return None
    return None


def _normalize_window_record(raw: Dict[str, Any]) -> Dict[str, Any]:
    """Convert cua-driver window records into Hermes' stable target shape."""
    pid = int(raw["pid"])
    window_id = int(raw["window_id"])
    bounds = (
        _normalize_bounds(raw.get("bounds"))
        or _normalize_bounds(raw.get("frame"))
        or _normalize_bounds(raw.get("rect"))
        or _normalize_bounds(raw.get("window_bounds"))
    )
    display_id = (
        raw.get("display_id")
        or raw.get("displayId")
        or raw.get("screen_id")
        or raw.get("screenId")
        or raw.get("display")
        or ""
    )
    app_name = raw.get("app_name", raw.get("appName", ""))
    bundle_id = raw.get("bundle_id", raw.get("bundleId", ""))
    title = raw.get("title", raw.get("window_title", raw.get("windowTitle", "")))
    target_id = f"window:{window_id}"
    return {
        "target_id": target_id,
        "target_kind": "window",
        "app_name": app_name,
        "bundle_id": bundle_id,
        "pid": pid,
        "window_id": window_id,
        "display_id": str(display_id) if display_id is not None else "",
        "title": title,
        "bounds": bounds,
        "scale_factor": _float_or_default(
            raw.get("scale_factor", raw.get("scaleFactor")), 1.0,
        ),
        "off_screen": not bool(raw.get("is_on_screen", not raw.get("off_screen", False))),
        "z_index": int(raw.get("z_index", raw.get("zIndex", 0)) or 0),
        "space_id": raw.get("space_id", raw.get("spaceId")),
    }


def _image_size_from_b64(data: str) -> Tuple[int, int]:
    """Read PNG/JPEG dimensions without pulling in imaging dependencies."""
    try:
        raw = base64.b64decode(data, validate=False)
    except (binascii.Error, ValueError):
        return (0, 0)
    if raw.startswith(b"\x89PNG\r\n\x1a\n") and len(raw) >= 24:
        return (int.from_bytes(raw[16:20], "big"), int.from_bytes(raw[20:24], "big"))
    if raw.startswith(b"\xff\xd8"):
        i = 2
        while i + 9 < len(raw):
            if raw[i] != 0xFF:
                i += 1
                continue
            marker = raw[i + 1]
            i += 2
            if marker in {0xD8, 0xD9}:
                continue
            if i + 2 > len(raw):
                break
            length = int.from_bytes(raw[i:i + 2], "big")
            if length < 2 or i + length > len(raw):
                break
            if 0xC0 <= marker <= 0xC3 and i + 7 < len(raw):
                return (
                    int.from_bytes(raw[i + 5:i + 7], "big"),
                    int.from_bytes(raw[i + 3:i + 5], "big"),
                )
            i += length
    return (0, 0)


def _parse_elements_from_tree(markdown: str) -> List[UIElement]:
    """Parse UIElement list from get_window_state AX tree markdown."""
    elements = []
    for m in _ELEMENT_LINE_RE.finditer(markdown):
        elements.append(UIElement(
            index=int(m.group(1)),
            role=m.group(2),
            label=m.group(3) or "",
            bounds=(0, 0, 0, 0),
        ))
    return elements


def _split_tree_text(full_text: str) -> Tuple[str, str]:
    """Split get_window_state text into (summary_line, tree_markdown)."""
    lines = full_text.split("\n", 1)
    summary = lines[0]
    tree = lines[1] if len(lines) > 1 else ""
    return summary, tree


def _parse_key_combo(keys: str) -> Tuple[Optional[str], List[str]]:
    """Parse a key string like 'cmd+s' into (key, modifiers).

    Returns (key, modifiers) where key is the non-modifier key and modifiers
    is a list of modifier names (cmd, shift, option, ctrl).
    """
    MODIFIER_NAMES = {"cmd", "command", "shift", "option", "alt", "ctrl", "control", "fn"}
    KEY_ALIASES = {"command": "cmd", "alt": "option", "control": "ctrl"}

    parts = [p.strip().lower() for p in re.split(r'[+\-]', keys) if p.strip()]
    modifiers = []
    key = None
    for part in parts:
        normalized = KEY_ALIASES.get(part, part)
        if normalized in MODIFIER_NAMES:
            modifiers.append(normalized)
        else:
            key = part  # last non-modifier wins
    return key, modifiers


# ---------------------------------------------------------------------------
# Asyncio bridge — one long-lived loop on a background thread
# ---------------------------------------------------------------------------

class _AsyncBridge:
    """Runs one asyncio loop on a daemon thread; marshals coroutines from the caller."""

    def __init__(self) -> None:
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._ready = threading.Event()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._ready.clear()

        def _run() -> None:
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)
            self._ready.set()
            try:
                self._loop.run_forever()
            finally:
                try:
                    self._loop.close()
                except Exception:
                    pass

        self._thread = threading.Thread(target=_run, daemon=True, name="cua-driver-loop")
        self._thread.start()
        if not self._ready.wait(timeout=5.0):
            raise RuntimeError("cua-driver asyncio bridge failed to start")

    def run(self, coro, timeout: Optional[float] = 30.0) -> Any:
        if not self._loop or not self._thread or not self._thread.is_alive():
            raise RuntimeError("cua-driver bridge not started")
        fut: Future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return fut.result(timeout=timeout)

    def stop(self) -> None:
        if self._loop and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread:
            self._thread.join(timeout=2.0)
        self._thread = None
        self._loop = None


# ---------------------------------------------------------------------------
# MCP session (lazy, shared across tool calls)
# ---------------------------------------------------------------------------

class _CuaDriverSession:
    """Holds the mcp ClientSession. Spawned lazily; re-entered on drop."""

    def __init__(self, bridge: _AsyncBridge) -> None:
        self._bridge = bridge
        self._session = None
        self._exit_stack = None
        self._lock = threading.Lock()
        self._started = False

    def _require_started(self) -> None:
        if not self._started:
            raise RuntimeError("cua-driver session not started")

    def _needs_start(self) -> bool:
        return not self._started or self._session is None

    @staticmethod
    def _is_recoverable_session_error(exc: Exception) -> bool:
        message = str(exc).lower()
        return any(fragment in message for fragment in (
            "session not started",
            "bridge not started",
            "not initialized",
            "connection closed",
            "closed resource",
            "broken pipe",
            "eof",
        ))

    async def _aenter(self) -> None:
        from contextlib import AsyncExitStack
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        if not cua_driver_binary_available():
            raise RuntimeError(cua_driver_install_hint())

        params = StdioServerParameters(
            command=_CUA_DRIVER_CMD,
            args=_CUA_DRIVER_ARGS,
            env={**os.environ},
        )
        stack = AsyncExitStack()
        read, write = await stack.enter_async_context(stdio_client(params))
        session = await stack.enter_async_context(ClientSession(read, write))
        await session.initialize()
        self._exit_stack = stack
        self._session = session

    async def _aexit(self) -> None:
        if self._exit_stack is not None:
            try:
                await self._exit_stack.aclose()
            except Exception as e:
                logger.warning("cua-driver shutdown error: %s", e)
        self._exit_stack = None
        self._session = None

    def start(self) -> None:
        with self._lock:
            if self._started:
                return
            self._bridge.start()
            self._bridge.run(self._aenter(), timeout=15.0)
            self._started = True

    def stop(self) -> None:
        with self._lock:
            if not self._started:
                return
            try:
                self._bridge.run(self._aexit(), timeout=5.0)
            finally:
                self._started = False

    async def _call_tool_async(self, name: str, args: Dict[str, Any]) -> Dict[str, Any]:
        result = await self._session.call_tool(name, args)
        return _extract_tool_result(result)

    def call_tool(self, name: str, args: Dict[str, Any], timeout: float = 30.0) -> Dict[str, Any]:
        if self._needs_start():
            self.start()
        try:
            self._require_started()
            return self._bridge.run(self._call_tool_async(name, args), timeout=timeout)
        except Exception as exc:
            if not self._is_recoverable_session_error(exc):
                raise
            logger.warning("cua-driver session unhealthy during %s; restarting once: %s", name, exc)
            try:
                self.stop()
            except Exception:
                pass
            self.start()
            self._require_started()
            return self._bridge.run(self._call_tool_async(name, args), timeout=timeout)


def _extract_tool_result(mcp_result: Any) -> Dict[str, Any]:
    """Convert an mcp CallToolResult into a plain dict.

    cua-driver returns a mix of text parts, image parts, and structuredContent.
    We flatten into:
      {
        "data": <text or parsed json>,
        "images": [b64, ...],
        "structuredContent": <dict|None>,
        "isError": bool,
      }
    structuredContent is populated from the MCP result's structuredContent field
    (MCP spec §2024-11-05+) and takes precedence for structured data like
    list_windows window arrays.
    """
    data: Any = None
    images: List[str] = []
    is_error = bool(getattr(mcp_result, "isError", False))
    structured: Optional[Dict] = getattr(mcp_result, "structuredContent", None) or None
    text_chunks: List[str] = []
    for part in getattr(mcp_result, "content", []) or []:
        ptype = getattr(part, "type", None)
        if ptype == "text":
            text_chunks.append(getattr(part, "text", "") or "")
        elif ptype == "image":
            b64 = getattr(part, "data", None)
            if b64:
                images.append(b64)
    if text_chunks:
        joined = "\n".join(t for t in text_chunks if t)
        try:
            data = json.loads(joined) if joined.strip().startswith(("{", "[")) else joined
        except json.JSONDecodeError:
            data = joined
    return {"data": data, "images": images, "structuredContent": structured, "isError": is_error}


# ---------------------------------------------------------------------------
# The backend itself
# ---------------------------------------------------------------------------

class CuaDriverBackend(ComputerUseBackend):
    """Default computer-use backend. macOS-only via cua-driver MCP."""

    def __init__(self) -> None:
        self._bridge = _AsyncBridge()
        self._session = _CuaDriverSession(self._bridge)
        # Sticky context — updated by capture(), used by action tools.
        self._active_pid: Optional[int] = None
        self._active_window_id: Optional[int] = None
        self._active_target: Optional[Dict[str, Any]] = None

    # ── Lifecycle ──────────────────────────────────────────────────
    def start(self) -> None:
        self._session.start()

    def stop(self) -> None:
        try:
            self._session.stop()
        finally:
            self._bridge.stop()

    def is_available(self) -> bool:
        if not _is_macos():
            return False
        return cua_driver_binary_available()

    def _list_windows(self) -> List[Dict[str, Any]]:
        """Return normalized on-screen window targets sorted front to back."""
        lw_out = self._session.call_tool("list_windows", {"on_screen_only": True})
        sc = lw_out.get("structuredContent") or {}
        raw_windows = sc.get("windows") if sc else None
        if raw_windows:
            windows = [
                _normalize_window_record(w)
                for w in raw_windows
                if isinstance(w, dict) and w.get("pid") is not None and w.get("window_id") is not None
            ]
        else:
            raw_text = lw_out["data"] if isinstance(lw_out["data"], str) else ""
            windows = [_normalize_window_record(w) for w in _parse_windows_from_text(raw_text)]
        windows.sort(key=lambda w: w["z_index"])
        return windows

    def _list_displays(self) -> List[Dict[str, Any]]:
        """Best-effort display catalog. Older cua-driver builds may not expose it."""
        for tool_name in ("list_displays", "get_displays"):
            try:
                out = self._session.call_tool(tool_name, {})
            except Exception:
                continue
            sc = out.get("structuredContent") or {}
            raw_displays = sc.get("displays") or sc.get("screens")
            if not raw_displays and isinstance(out.get("data"), dict):
                raw_displays = out["data"].get("displays") or out["data"].get("screens")
            if not isinstance(raw_displays, list):
                continue
            displays = []
            for item in raw_displays:
                if not isinstance(item, dict):
                    continue
                display_id = item.get("display_id") or item.get("displayId") or item.get("id")
                displays.append({
                    "display_id": str(display_id) if display_id is not None else "",
                    "bounds": _normalize_bounds(item.get("bounds") or item.get("frame")),
                    "scale_factor": _float_or_default(
                        item.get("scale_factor", item.get("scaleFactor")), 1.0,
                    ),
                    "is_main": bool(item.get("is_main", item.get("main", False))),
                })
            return displays
        return []

    def list_targets(self) -> Dict[str, Any]:
        windows = self._list_windows()
        displays = self._list_displays()
        return {
            "displays": displays,
            "windows": windows,
            "active_target_id": self._active_target.get("target_id") if self._active_target else None,
            "coordinate_space": "window",
        }

    # ── Capture ────────────────────────────────────────────────────
    def capture(
        self,
        mode: str = "som",
        app: Optional[str] = None,
        target_id: Optional[str] = None,
    ) -> CaptureResult:
        """Capture the frontmost on-screen window (optionally filtered by app name).

        Maps hermes `capture(mode, app)` → cua-driver `list_windows` +
        `get_window_state` (ax/som) or `screenshot` (vision).
        """
        # Step 1: enumerate windows to find a stable target.
        windows = self._list_windows()

        if not windows:
            return CaptureResult(mode=mode, width=0, height=0, png_b64=None,
                                 elements=[], app="", window_title="", png_bytes_len=0,
                                 warnings=["no on-screen windows returned by cua-driver"])

        if target_id:
            filtered = [w for w in windows if w.get("target_id") == target_id]
            if filtered:
                windows = filtered
        elif app:
            app_lower = app.lower()
            filtered = [
                w for w in windows
                if app_lower in str(w.get("app_name", "")).lower()
                or app_lower in str(w.get("bundle_id", "")).lower()
            ]
            if filtered:
                windows = filtered
        elif self._active_window_id is not None:
            filtered = [w for w in windows if w.get("window_id") == self._active_window_id]
            if filtered:
                windows = filtered

        # Pick first on-screen window (sorted by z_index / z-order above).
        target = next((w for w in windows if not w.get("off_screen")), windows[0])
        self._active_pid = target["pid"]
        self._active_window_id = target["window_id"]
        self._active_target = target
        app_name = target["app_name"]

        # Step 2: capture.
        png_b64: Optional[str] = None
        elements: List[UIElement] = []
        width = height = 0
        window_title = ""
        warnings: List[str] = []

        if mode == "vision":
            # screenshot tool: just the PNG, no AX walk.
            sc_out = self._session.call_tool(
                "screenshot",
                {"window_id": self._active_window_id, "format": "jpeg", "quality": 85},
            )
            if sc_out["images"]:
                png_b64 = sc_out["images"][0]
                width, height = _image_size_from_b64(png_b64)
        else:
            # get_window_state: AX tree + optional screenshot.
            gws_out = self._session.call_tool(
                "get_window_state",
                {"pid": self._active_pid, "window_id": self._active_window_id},
            )
            text = gws_out["data"] if isinstance(gws_out["data"], str) else ""
            summary, tree = _split_tree_text(text)
            structured = gws_out.get("structuredContent") or {}

            raw_elements = structured.get("elements") or structured.get("ui_elements")
            if isinstance(raw_elements, list):
                elements = [_parse_element(e) for e in raw_elements if isinstance(e, dict)]
            elif tree and not gws_out["images"]:
                # ax mode — no screenshot
                elements = _parse_elements_from_tree(tree)
            elif gws_out["images"]:
                png_b64 = gws_out["images"][0]
                elements = _parse_elements_from_tree(tree)
            if gws_out["images"]:
                png_b64 = gws_out["images"][0]
                width, height = _image_size_from_b64(png_b64)

            # Extract window title from the AX tree first AXWindow line.
            wt = re.search(r'AXWindow\s+"([^"]+)"', tree)
            if wt:
                window_title = wt.group(1)
            if elements and all(e.bounds == (0, 0, 0, 0) for e in elements):
                warnings.append(
                    "accessibility tree returned zero element bounds; use vision mode "
                    "or a visual/OCR fallback for this app",
                )
            if elements and not any(e.label for e in elements):
                warnings.append(
                    "accessibility tree returned empty labels; target app may not expose "
                    "WebView content through macOS AX",
                )

        png_bytes_len = 0
        if png_b64:
            try:
                png_bytes_len = len(base64.b64decode(png_b64, validate=False))
            except Exception:
                png_bytes_len = len(png_b64) * 3 // 4

        return CaptureResult(
            mode=mode,
            width=width,
            height=height,
            png_b64=png_b64,
            elements=elements,
            app=app_name,
            window_title=window_title or str(target.get("title", "") or ""),
            png_bytes_len=png_bytes_len,
            target_id=str(target.get("target_id", "")),
            pid=int(target.get("pid", 0) or 0),
            window_id=int(target.get("window_id", 0) or 0),
            display_id=str(target.get("display_id", "") or ""),
            window_bounds=target.get("bounds"),
            capture_bounds=target.get("bounds"),
            coordinate_space="window",
            scale_factor=float(target.get("scale_factor", 1.0) or 1.0),
            warnings=warnings,
        )

    # ── Pointer ────────────────────────────────────────────────────
    def click(
        self,
        *,
        element: Optional[int] = None,
        x: Optional[int] = None,
        y: Optional[int] = None,
        button: str = "left",
        click_count: int = 1,
        modifiers: Optional[List[str]] = None,
    ) -> ActionResult:
        pid = self._active_pid
        if pid is None:
            return ActionResult(ok=False, action="click",
                                message="No active window — call capture() first.")

        # Choose tool based on button and click_count.
        if button == "right":
            tool = "right_click"
        elif click_count == 2:
            tool = "double_click"
        else:
            tool = "click"

        args: Dict[str, Any] = {"pid": pid}
        if element is not None:
            if self._active_window_id is None:
                return ActionResult(ok=False, action=tool,
                                    message="No active window_id for element_index click.")
            args["element_index"] = element
            args["window_id"] = self._active_window_id
        elif x is not None and y is not None:
            args["x"] = x
            args["y"] = y
        else:
            return ActionResult(ok=False, action=tool,
                                message="click requires element= or x/y.")
        if modifiers:
            args["modifier"] = modifiers

        return self._action(tool, args)

    def drag(
        self,
        *,
        from_element: Optional[int] = None,
        to_element: Optional[int] = None,
        from_xy: Optional[Tuple[int, int]] = None,
        to_xy: Optional[Tuple[int, int]] = None,
        button: str = "left",
        modifiers: Optional[List[str]] = None,
    ) -> ActionResult:
        # cua-driver does not expose a drag tool.
        return ActionResult(ok=False, action="drag",
                            message="drag is not supported by the cua-driver backend.")

    def scroll(
        self,
        *,
        direction: str,
        amount: int = 3,
        element: Optional[int] = None,
        x: Optional[int] = None,
        y: Optional[int] = None,
        modifiers: Optional[List[str]] = None,
    ) -> ActionResult:
        pid = self._active_pid
        if pid is None:
            return ActionResult(ok=False, action="scroll",
                                message="No active window — call capture() first.")
        args: Dict[str, Any] = {
            "pid": pid,
            "direction": direction,
            "amount": max(1, min(50, amount)),
        }
        if element is not None and self._active_window_id is not None:
            args["element_index"] = element
            args["window_id"] = self._active_window_id
        elif x is not None and y is not None:
            args["x"] = x
            args["y"] = y
        return self._action("scroll", args)

    # ── Keyboard ───────────────────────────────────────────────────
    def type_text(self, text: str) -> ActionResult:
        pid = self._active_pid
        if pid is None:
            return ActionResult(ok=False, action="type_text",
                                message="No active window — call capture() first.")
        # Safari WebKit AXTextField does not accept AX attribute writes (type_text),
        # so use type_text_chars which synthesises individual key events instead.
        # This works universally across all macOS apps in background mode.
        return self._action("type_text_chars", {"pid": pid, "text": text})

    def key(self, keys: str) -> ActionResult:
        pid = self._active_pid
        if pid is None:
            return ActionResult(ok=False, action="key",
                                message="No active window — call capture() first.")

        key_name, modifiers = _parse_key_combo(keys)
        if not key_name:
            return ActionResult(ok=False, action="key",
                                message=f"Could not parse key from '{keys}'.")

        if modifiers:
            # hotkey requires at least one modifier + one key.
            return self._action("hotkey", {"pid": pid, "keys": modifiers + [key_name]})
        else:
            return self._action("press_key", {"pid": pid, "key": key_name})

    # ── Value setter ────────────────────────────────────────────────
    def set_value(self, value: str, element: Optional[int] = None) -> ActionResult:
        """Set a value on an element. Handles AXPopUpButton selects natively."""
        pid = self._active_pid
        window_id = self._active_window_id
        if pid is None or window_id is None:
            return ActionResult(ok=False, action="set_value",
                                message="No active window — call capture() first.")
        if element is None:
            return ActionResult(ok=False, action="set_value",
                                message="set_value requires element= (element index).")
        args: Dict[str, Any] = {
            "pid": pid,
            "window_id": window_id,
            "element_index": element,
            "value": value,
        }
        return self._action("set_value", args)

    # ── Introspection ──────────────────────────────────────────────
    def list_apps(self) -> List[Dict[str, Any]]:
        out = self._session.call_tool("list_apps", {})
        data = out["data"]
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            return data.get("apps", [])
        # list_apps returns plain text — parse app lines.
        if isinstance(data, str):
            apps = []
            for line in data.splitlines():
                m = re.search(r'(.+?)\s+\(pid\s+(\d+)\)', line)
                if m:
                    apps.append({"name": m.group(1).strip(), "pid": int(m.group(2))})
            return apps
        return []

    def focus_app(self, app: str, raise_window: bool = False) -> ActionResult:
        """Target an app for subsequent actions without stealing system focus.

        cua-driver background-automation never needs to bring a window to the
        front: capture(app=...) already selects the right window via
        list_windows. We implement focus_app as a pure window-selector —
        enumerate on-screen windows, find the best match for *app*, and store
        its pid/window_id so that subsequent click/type calls hit the right
        process.

        raise_window=True is intentionally ignored: stealing the user's focus
        is exactly what this backend is designed to avoid.
        """
        windows = self._list_windows()

        app_lower = app.lower()
        matched = [
            w for w in windows
            if app_lower in str(w.get("app_name", "")).lower()
            or app_lower in str(w.get("bundle_id", "")).lower()
        ]
        target = matched[0] if matched else (windows[0] if windows else None)
        if target:
            self._active_pid = target["pid"]
            self._active_window_id = target["window_id"]
            self._active_target = target
            return ActionResult(
                ok=True, action="focus_app",
                message=f"Targeted {target['app_name']} (pid {self._active_pid}, "
                        f"window {self._active_window_id}) without raising window.",
                meta={
                    "target_id": target.get("target_id"),
                    "window_id": self._active_window_id,
                    "pid": self._active_pid,
                    "display_id": target.get("display_id", ""),
                    "window_bounds": target.get("bounds"),
                    "coordinate_space": "window",
                    "raise_window_supported": False,
                    "raise_window_requested": bool(raise_window),
                },
            )
        return ActionResult(ok=False, action="focus_app",
                            message=f"No on-screen window found for app '{app}'.")

    # ── Internal ───────────────────────────────────────────────────
    def _action(self, name: str, args: Dict[str, Any]) -> ActionResult:
        try:
            out = self._session.call_tool(name, args)
        except Exception as e:
            logger.exception("cua-driver %s call failed", name)
            return ActionResult(ok=False, action=name, message=f"cua-driver error: {e}")
        ok = not out["isError"]
        message = ""
        data = out["data"]
        if isinstance(data, dict):
            message = str(data.get("message", ""))
        elif isinstance(data, str):
            message = data
        return ActionResult(ok=ok, action=name, message=message,
                            meta=data if isinstance(data, dict) else {})


def _parse_element(d: Dict[str, Any]) -> UIElement:
    bounds = d.get("bounds") or (0, 0, 0, 0)
    if isinstance(bounds, dict):
        bounds = (
            int(bounds.get("x", 0)),
            int(bounds.get("y", 0)),
            int(bounds.get("w", bounds.get("width", 0))),
            int(bounds.get("h", bounds.get("height", 0))),
        )
    elif isinstance(bounds, (list, tuple)) and len(bounds) == 4:
        bounds = tuple(int(v) for v in bounds)
    else:
        bounds = (0, 0, 0, 0)
    return UIElement(
        index=int(d.get("index", 0)),
        role=str(d.get("role", "") or ""),
        label=str(d.get("label", "") or ""),
        bounds=bounds,  # type: ignore[arg-type]
        app=str(d.get("app", "") or ""),
        pid=int(d.get("pid", 0) or 0),
        window_id=int(d.get("windowId", d.get("window_id", 0)) or 0),
        attributes={k: v for k, v in d.items()
                    if k not in {"index", "role", "label", "bounds", "app", "pid", "windowId", "window_id"}},
    )
