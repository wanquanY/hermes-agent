from __future__ import annotations


def load_busy_input_mode(load_cfg) -> str:
    display = load_cfg().get("display")
    if not isinstance(display, dict):
        display = {}
    raw = str(display.get("busy_input_mode", "") or "").strip().lower()
    return raw if raw in {"queue", "steer", "interrupt"} else "interrupt"


def coerce_statusbar(raw) -> str:
    if raw is False:
        return "off"
    if isinstance(raw, str) and (value := raw.strip().lower()) in {
        "off",
        "top",
        "bottom",
    }:
        return value
    return "top"


def display_mouse_tracking(display: dict) -> bool:
    if not isinstance(display, dict):
        return True
    raw = display.get("mouse_tracking") if "mouse_tracking" in display else display.get("tui_mouse", True)
    if raw is False or raw == 0:
        return False
    if isinstance(raw, str):
        return raw.strip().lower() not in {"0", "false", "no", "off"}
    return True
