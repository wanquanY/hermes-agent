"""Doxie desktop browser bridge for Hermes browser tab operations."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any

DEFAULT_BROWSER_SESSION_ID = "browser:electron:default"


def _backend_bridge_config() -> tuple[str, str]:
    return (
        os.getenv("DOXIE_BACKEND_BRIDGE_URL", "").strip(),
        os.getenv("DOXIE_BACKEND_BRIDGE_TOKEN", "").strip(),
    )


def available() -> bool:
    url, token = _backend_bridge_config()
    return bool(url and token)


def browser_session_id() -> str:
    explicit = os.getenv("DOXIE_BROWSER_SESSION_ID", "").strip()
    if explicit:
        return explicit
    try:
        from gateway.session_context import get_session_env

        scoped = get_session_env("DOXIE_BROWSER_SESSION_ID", "").strip()
        if scoped:
            return scoped
    except Exception:
        pass
    return DEFAULT_BROWSER_SESSION_ID


def call(command: str, payload: dict[str, Any] | None = None, timeout: float = 30.0) -> Any:
    url, token = _backend_bridge_config()
    if not url or not token:
        raise RuntimeError("Doxie backend bridge is not available.")
    body = json.dumps({"command": command, "payload": payload or {}}, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "authorization": f"Bearer {token}",
            "content-type": "application/json; charset=utf-8",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Doxie browser bridge rejected {command}: {detail}") from exc
    if not isinstance(data, dict) or not data.get("ok"):
        raise RuntimeError(str(data.get("error") if isinstance(data, dict) else data))
    return data.get("value")


def session_from_value(value: Any) -> dict[str, Any]:
    if isinstance(value, dict) and isinstance(value.get("tabs"), list):
        return value
    if isinstance(value, list):
        sid = browser_session_id()
        for item in value:
            if isinstance(item, dict) and item.get("browserSessionId") == sid:
                return item
        for item in value:
            if isinstance(item, dict) and isinstance(item.get("tabs"), list):
                return item
    return {}


def normalize_tab(tab: dict[str, Any], active_tab_id: str = "") -> dict[str, Any]:
    tab_id = str(tab.get("tabId") or tab.get("tab_id") or "")
    return {
        "tab_id": tab_id,
        "target_id": tab_id,
        "title": str(tab.get("title") or ""),
        "url": str(tab.get("url") or "about:blank"),
        "active": bool(tab.get("active") or (active_tab_id and tab_id == active_tab_id)),
        "attached": bool(tab.get("attached", True)),
        "type": "page",
    }


def tab_payload_from_session(session: dict[str, Any]) -> dict[str, Any]:
    tabs = session.get("tabs") if isinstance(session, dict) else []
    if not isinstance(tabs, list):
        tabs = []
    active_tab_id = str(session.get("activeTabId") or session.get("active_tab_id") or "")
    normalized_tabs = [
        normalize_tab(tab, active_tab_id)
        for tab in tabs
        if isinstance(tab, dict)
    ]
    if not active_tab_id:
        active = next((tab for tab in normalized_tabs if tab.get("active")), None)
        active_tab_id = str((active or {}).get("tab_id") or "")
    return {
        "success": True,
        "tabs": normalized_tabs,
        "active_tab_id": active_tab_id,
        "provider": "doxie_desktop",
        "browser_session_id": str(session.get("browserSessionId") or browser_session_id()),
    }
