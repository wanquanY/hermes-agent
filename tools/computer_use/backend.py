"""Abstract backend interface for computer use.

Any implementation (cua-driver over MCP, pyautogui, noop, future Linux/Windows)
must return the shape described below. All methods synchronous; async is
handled inside the backend implementation if needed.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple


@dataclass
class UIElement:
    """One interactable element on the current screen."""

    index: int                       # 1-based SOM index
    role: str                        # AX role (AXButton, AXTextField, ...)
    label: str = ""                  # AXTitle / AXDescription / AXValue snippet
    bounds: Tuple[int, int, int, int] = (0, 0, 0, 0)  # x, y, w, h (logical px)
    app: str = ""                    # owning bundle ID or app name
    pid: int = 0                     # owning process PID
    window_id: int = 0               # SkyLight / CG window ID
    attributes: Dict[str, Any] = field(default_factory=dict)

    def center(self) -> Tuple[int, int]:
        x, y, w, h = self.bounds
        return x + w // 2, y + h // 2


@dataclass
class CaptureResult:
    """Result of a screen capture call.

    At least one of png_b64 / elements is populated depending on capture mode:
      * mode="vision" → png_b64 only
      * mode="ax"     → elements only
      * mode="som"    → both (default): PNG already has numbered overlays
                         drawn by the backend, and `elements` holds the
                         matching index → element mapping.
    """

    mode: str
    width: int                      # screenshot width (logical px, pre-Anthropic-scale)
    height: int
    png_b64: Optional[str] = None
    elements: List[UIElement] = field(default_factory=list)
    # Optional: the target app/window the elements were captured for.
    app: str = ""
    window_title: str = ""
    # Raw bytes we sent to Anthropic, for token estimation.
    png_bytes_len: int = 0
    # Native target metadata. These fields make the capture/action coordinate
    # space explicit for multi-window and multi-display sessions.
    target_id: str = ""
    pid: int = 0
    window_id: int = 0
    display_id: str = ""
    window_bounds: Optional[Tuple[int, int, int, int]] = None
    capture_bounds: Optional[Tuple[int, int, int, int]] = None
    coordinate_space: str = "window"
    scale_factor: float = 1.0
    warnings: List[str] = field(default_factory=list)


@dataclass
class ActionResult:
    """Transport result plus cua-driver's optional semantic verdict.

    ``ok`` only says the tool call ran. ``effect`` and ``verified`` say
    whether the requested UI change actually landed; ``escalation`` tells the
    agent which bounded delivery rung to try next. Older drivers omit these
    additive fields and remain compatible.
    """

    ok: bool
    action: str
    message: str = ""                # human-readable summary
    # Optional trailing screenshot — set when the caller asked for a
    # post-action capture or the backend always returns one.
    capture: Optional[CaptureResult] = None
    # Arbitrary extra fields for debugging / telemetry.
    meta: Dict[str, Any] = field(default_factory=dict)
    verified: Optional[bool] = None
    effect: Optional[str] = None
    escalation: Optional[Dict[str, Any]] = None
    path: Optional[str] = None
    degraded: Optional[bool] = None
    delivery_mode: Optional[str] = None
    code: Optional[str] = None


class ComputerUseBackend(ABC):
    """Lifecycle: `start()` before first use, `stop()` at shutdown."""

    @abstractmethod
    def start(self) -> None: ...

    @abstractmethod
    def stop(self) -> None: ...

    @abstractmethod
    def is_available(self) -> bool:
        """Return True if the backend can be used on this host right now.

        Used by check_fn gating and by the post-setup wizard.
        """

    # ── Capture ─────────────────────────────────────────────────────
    @abstractmethod
    def capture(
        self,
        mode: str = "som",
        app: Optional[str] = None,
        target_id: Optional[str] = None,
    ) -> CaptureResult: ...

    # ── Pointer actions ─────────────────────────────────────────────
    @abstractmethod
    def click(
        self,
        *,
        element: Optional[int] = None,
        x: Optional[int] = None,
        y: Optional[int] = None,
        button: str = "left",           # left | right | middle
        click_count: int = 1,
        modifiers: Optional[List[str]] = None,
        delivery_mode: Optional[str] = None,
        bring_to_front: bool = False,
    ) -> ActionResult: ...

    @abstractmethod
    def drag(
        self,
        *,
        from_element: Optional[int] = None,
        to_element: Optional[int] = None,
        from_xy: Optional[Tuple[int, int]] = None,
        to_xy: Optional[Tuple[int, int]] = None,
        button: str = "left",
        modifiers: Optional[List[str]] = None,
        delivery_mode: Optional[str] = None,
        bring_to_front: bool = False,
    ) -> ActionResult: ...

    @abstractmethod
    def scroll(
        self,
        *,
        direction: str,                 # up | down | left | right
        amount: int = 3,                # wheel ticks
        element: Optional[int] = None,
        x: Optional[int] = None,
        y: Optional[int] = None,
        modifiers: Optional[List[str]] = None,
        delivery_mode: Optional[str] = None,
        bring_to_front: bool = False,
    ) -> ActionResult: ...

    # ── Keyboard ────────────────────────────────────────────────────
    @abstractmethod
    def type_text(
        self,
        text: str,
        *,
        delivery_mode: Optional[str] = None,
        bring_to_front: bool = False,
    ) -> ActionResult: ...

    @abstractmethod
    def key(
        self,
        keys: str,
        *,
        delivery_mode: Optional[str] = None,
        bring_to_front: bool = False,
    ) -> ActionResult:
        """Send a key combo, e.g. 'cmd+s', 'ctrl+alt+t', 'return'."""

    # ── Introspection ───────────────────────────────────────────────
    @abstractmethod
    def list_apps(self) -> List[Dict[str, Any]]:
        """Return running apps with bundle IDs, PIDs, window counts."""

    def list_targets(self) -> Dict[str, Any]:
        """Return displays and windows that can be targeted.

        Backends that cannot expose display/window inventories should still
        return a structured payload with empty lists, not raise. The tool layer
        uses this as the stable target catalog for multi-display operation.
        """
        return {"displays": [], "windows": [], "active_target_id": None}

    @abstractmethod
    def focus_app(self, app: str, raise_window: bool = False) -> ActionResult:
        """Route input to `app` (by name or bundle ID). Default: focus without raise."""

    # ── Timing ──────────────────────────────────────────────────────
    def wait(self, seconds: float) -> ActionResult:
        """Default implementation: time.sleep."""
        import time
        time.sleep(max(0.0, min(seconds, 30.0)))
        return ActionResult(ok=True, action="wait", message=f"waited {seconds:.2f}s")
