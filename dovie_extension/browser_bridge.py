"""Dovie desktop browser bridge for Hermes browser operations."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any

DEFAULT_BROWSER_SESSION_ID = "browser:electron:default"


def _safe_browser_session_segment(value: str) -> str:
    chars: list[str] = []
    last_dash = False
    for char in str(value or "").strip():
        ascii_alnum = (
            "a" <= char <= "z"
            or "A" <= char <= "Z"
            or "0" <= char <= "9"
        )
        if ascii_alnum or char in "_.:-":
            chars.append(char)
            last_dash = False
        elif not last_dash:
            chars.append("-")
            last_dash = True
    return "".join(chars).strip("-")[:160]


def browser_session_id_for_gateway_session(session_key: str) -> str:
    segment = _safe_browser_session_segment(session_key) or "default"
    return f"browser:hermes:{segment}"


def _backend_bridge_config() -> tuple[str, str]:
    return (
        os.getenv("DOVIE_BACKEND_BRIDGE_URL", "").strip(),
        os.getenv("DOVIE_BACKEND_BRIDGE_TOKEN", "").strip(),
    )


def available() -> bool:
    url, token = _backend_bridge_config()
    return bool(url and token)


def browser_session_id() -> str:
    try:
        from gateway.session_context import get_session_env

        scoped = get_session_env("DOVIE_BROWSER_SESSION_ID", "").strip()
        if scoped:
            return scoped
    except Exception:
        pass
    explicit = os.getenv("DOVIE_BROWSER_SESSION_ID", "").strip()
    if explicit:
        return explicit
    return DEFAULT_BROWSER_SESSION_ID


def call(command: str, payload: dict[str, Any] | None = None, timeout: float = 30.0) -> Any:
    url, token = _backend_bridge_config()
    if not url or not token:
        raise RuntimeError("Dovie backend bridge is not available.")
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
        raise RuntimeError(f"Dovie browser bridge rejected {command}: {detail}") from exc
    if not isinstance(data, dict) or not data.get("ok"):
        raise RuntimeError(str(data.get("error") if isinstance(data, dict) else data))
    return data.get("value")


def _request(extra: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "browserSessionId": browser_session_id(),
        **(extra or {}),
    }


def navigate(url: str, timeout: float = 70.0) -> dict[str, Any]:
    value = call(
        "browser_use_navigate",
        {"request": _request({"url": url})},
        timeout=timeout,
    )
    return value if isinstance(value, dict) else {}


def observe(max_nodes: int = 200) -> dict[str, Any]:
    value = call(
        "browser_use_observe_embedded",
        {"request": _request({"maxNodes": max_nodes})},
    )
    return value if isinstance(value, dict) else {}


def action(
    action_name: str,
    *,
    ref: str | None = None,
    text: str | None = None,
    key: str | None = None,
    delta_y: int | None = None,
) -> dict[str, Any]:
    request: dict[str, Any] = {"action": action_name}
    if ref:
        request["ref"] = ref.lstrip("@")
    if text is not None:
        request["text"] = text
    if key:
        request["key"] = key
    if delta_y is not None:
        request["deltaY"] = delta_y
    value = call("browser_use_action_embedded", {"request": _request(request)})
    return value if isinstance(value, dict) else {}


def go_back() -> dict[str, Any]:
    value = call("browser_use_go_back", {"request": _request()})
    return value if isinstance(value, dict) else {}


def go_forward() -> dict[str, Any]:
    value = call("browser_use_go_forward", {"request": _request()})
    return value if isinstance(value, dict) else {}


def _element_line(element: dict[str, Any]) -> str:
    ref = str(element.get("ref") or "").lstrip("@")
    tag_name = str(element.get("tagName") or element.get("tag_name") or "element")
    role = str(element.get("role") or "")
    name = str(element.get("name") or "")
    text = str(element.get("text") or "").strip().replace("\n", " ")
    state = element.get("state") if isinstance(element.get("state"), dict) else {}
    state_bits = [
        key
        for key in ("editable", "enabled", "visible")
        if state.get(key) is True
    ]
    label = " ".join(part for part in (name, text) if part).strip()
    suffix = f" - {label}" if label else ""
    role_part = f" role={role}" if role else ""
    state_part = f" [{' '.join(state_bits)}]" if state_bits else ""
    return f"@{ref} <{tag_name}{role_part}>{state_part}{suffix}" if ref else f"<{tag_name}{role_part}>{state_part}{suffix}"


def snapshot_payload_from_observation(observation: dict[str, Any]) -> dict[str, Any]:
    elements = observation.get("elements") or observation.get("nodes") or []
    if not isinstance(elements, list):
        elements = []
    title = str(observation.get("title") or observation.get("pageTitle") or "")
    url = str(observation.get("url") or observation.get("pageUrl") or "")
    page_text = str(observation.get("pageText") or "").strip()
    lines: list[str] = []
    if title:
        lines.append(f"Title: {title}")
    if url:
        lines.append(f"URL: {url}")
    if elements:
        if lines:
            lines.append("")
        lines.extend(_element_line(element) for element in elements if isinstance(element, dict))
    elif page_text:
        if lines:
            lines.append("")
        lines.append(page_text)
    return {
        "success": True,
        "snapshot": "\n".join(lines),
        "element_count": len(elements),
        "url": url,
        "title": title,
        "provider": "dovie_desktop",
        "browser_session_id": browser_session_id(),
    }


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
        "provider": "dovie_desktop",
        "browser_session_id": str(session.get("browserSessionId") or browser_session_id()),
    }
