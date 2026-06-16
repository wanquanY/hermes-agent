from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from hermes_team_mission_artifact_refs import artifact_refs_from_event


def text(value: Any) -> str:
    return str(value or "").strip()


def json_list(value: Any) -> list[Any]:
    loaded = None
    if isinstance(value, str) and value:
        try:
            loaded = json.loads(value)
        except (TypeError, json.JSONDecodeError):
            loaded = None
    if loaded is None:
        loaded = value
    if isinstance(loaded, list):
        return loaded
    if isinstance(loaded, tuple):
        return list(loaded)
    if loaded in (None, ""):
        return []
    return [loaded]


def dedupe_text(values: Any) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for raw in json_list(values):
        item = text(raw)
        if item and item not in seen:
            seen.add(item)
            result.append(item)
    return result


def stable_id(prefix: str, *parts: Any) -> str:
    payload = "\x1f".join(text(part) for part in parts)
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]
    return f"{prefix}-{digest}"


def stringify_content(value: Any, *, limit: int = 2000) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        result = value
    elif isinstance(value, list):
        parts: list[str] = []
        for item in value:
            if isinstance(item, dict):
                if item.get("type") == "text":
                    parts.append(text(item.get("text")))
                elif item.get("text"):
                    parts.append(text(item.get("text")))
            else:
                parts.append(text(item))
        result = "\n".join(part for part in parts if part)
    elif isinstance(value, dict):
        result = text(value.get("text") or value.get("content") or value.get("delta"))
        if not result:
            try:
                result = json.dumps(value, ensure_ascii=False, sort_keys=True)
            except (TypeError, ValueError):
                result = str(value)
    else:
        result = str(value)
    result = re.sub(r"\s+", " ", result).strip()
    if len(result) > limit:
        return result[: max(0, limit - 3)].rstrip() + "..."
    return result


def event_text(event: dict[str, Any]) -> str:
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    event_type = text(event.get("type"))
    if event_type == "message.delta":
        return stringify_content(payload.get("delta") or payload.get("text") or payload.get("content"), limit=4000)
    if event_type == "message.complete":
        return stringify_content(
            payload.get("text")
            or payload.get("content")
            or payload.get("final")
            or payload.get("message"),
            limit=4000,
        )
    if event_type == "tool.complete":
        name = text(payload.get("name") or payload.get("tool_name"))
        result = stringify_content(payload.get("result") or payload.get("output"), limit=1200)
        if name and result:
            return f"{name}: {result}"
        return result
    return ""


def event_artifacts(event: dict[str, Any]) -> list[dict[str, Any]]:
    return artifact_refs_from_event(event)


def tokenize(value: str) -> set[str]:
    return {
        token
        for token in re.findall(r"[\w\-]{2,}|[\u4e00-\u9fff]{1,}", (value or "").lower())
        if token
    }


MEMORY_TERMINAL_STATUSES = {"completed", "verified", "failed", "cancelled", "canceled", "interrupted", "blocked"}
MEMORY_COMMITTED_STATUS = "committed"
MEMORY_VISIBLE_TO_WORKER = {"team"}
