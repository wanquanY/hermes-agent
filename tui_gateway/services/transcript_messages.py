from __future__ import annotations

import json
from typing import Any


def tool_context(name: str, args: dict) -> str:
    try:
        from agent.display import build_tool_preview

        return build_tool_preview(name, args, max_len=80) or ""
    except Exception:
        return ""


def serializable_tool_args(args: dict | None) -> dict:
    if not isinstance(args, dict):
        return {}
    try:
        json.dumps(args)
        return args
    except Exception:
        return {
            str(key): str(value)
            for key, value in args.items()
            if isinstance(key, (str, int, float, bool))
        }


def content_display_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, (int, float)):
        return str(content)
    if isinstance(content, list):
        parts = []
        for part in content:
            text = content_display_text(part).strip()
            if text:
                parts.append(text)
        return "\n".join(parts)
    if isinstance(content, dict):
        kind = content.get("type")
        if kind in {"text", "input_text", "output_text"}:
            return str(content.get("text") or content.get("content") or "")
        if kind in {"image_url", "input_image", "image"}:
            return "[image]"
        if kind in {"input_audio", "audio"}:
            return "[audio]"
        if kind:
            return f"[{kind}]"
        if "text" in content:
            return str(content.get("text") or "")
        return "[structured content]"
    return str(content)


def history_to_messages(history: list[dict]) -> list[dict]:
    messages = []
    tool_call_args = {}

    for message in history:
        if not isinstance(message, dict):
            continue
        role = message.get("role")
        if role not in {"user", "assistant", "tool", "system"}:
            continue
        content_text = content_display_text(message.get("content"))
        reasoning_text = ""
        if role == "assistant":
            reasoning_text = content_display_text(
                message.get("reasoning") or message.get("reasoning_content")
            )
        if role == "assistant" and message.get("tool_calls"):
            for tool_call in message["tool_calls"]:
                function = tool_call.get("function", {})
                tool_call_id = tool_call.get("id", "")
                if tool_call_id and function.get("name"):
                    try:
                        args = json.loads(function.get("arguments", "{}"))
                    except (json.JSONDecodeError, TypeError):
                        args = {}
                    tool_call_args[tool_call_id] = (function["name"], args)
            if not content_text.strip() and not reasoning_text.strip():
                continue
        if role == "tool":
            tool_call_id = message.get("tool_call_id", "")
            tool_info = tool_call_args.get(tool_call_id) if tool_call_id else None
            name = (tool_info[0] if tool_info else None) or message.get("tool_name") or "tool"
            args = (tool_info[1] if tool_info else None) or {}
            item = {
                "role": "tool",
                "name": name,
                "context": tool_context(name, args),
            }
            if tool_call_id:
                item["tool_call_id"] = str(tool_call_id)
            if args:
                item["arguments"] = serializable_tool_args(args)
            if content_text.strip():
                item["result_text"] = content_text
            if message.get("id") is not None:
                item["message_id"] = str(message.get("id"))
            if message.get("timestamp") is not None:
                item["timestamp"] = message.get("timestamp")
            if isinstance(message.get("metadata"), dict):
                item["metadata"] = dict(message["metadata"])
            messages.append(item)
            continue
        if not content_text.strip() and not reasoning_text.strip():
            continue
        item = {"role": role, "text": content_text}
        if message.get("id") is not None:
            item["message_id"] = str(message.get("id"))
        if message.get("timestamp") is not None:
            item["timestamp"] = message.get("timestamp")
        if isinstance(message.get("metadata"), dict):
            item["metadata"] = dict(message["metadata"])
        if reasoning_text.strip():
            item["reasoning"] = reasoning_text
        messages.append(item)

    return messages
