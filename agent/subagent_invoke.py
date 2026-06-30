"""Synchronous in-process subagent invocation."""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Optional

from hermes_constants import (
    get_default_hermes_root,
    reset_hermes_home_override,
    set_hermes_home_override,
)
from hermes_profile_dir import resolve_default_agent_dir


@dataclass
class SubagentResult:
    output: str
    tool_calls: list[dict]
    usage: dict
    error: Optional[str] = None
    elapsed_s: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)


def invoke_subagent(
    caller_agent: "AIAgent",
    target_profile_id: str,
    prompt: str,
    *,
    files: Optional[list[str]] = None,
    tools_subset: Optional[list[str]] = None,
    timeout_s: float = 120.0,
    max_turns: int = 10,
) -> SubagentResult:
    """Invoke another Hermes profile as a synchronous in-process subagent."""
    started = time.monotonic()
    target_home = _resolve_target_profile_dir(target_profile_id)
    client = _caller_llm_client(caller_agent)
    if client is None:
        raise ValueError("caller_agent has no reusable LLM client")

    token = set_hermes_home_override(target_home)
    try:
        return _invoke_subagent_scoped(
            caller_agent,
            client,
            target_home,
            prompt,
            files=files,
            tools_subset=tools_subset,
            timeout_s=timeout_s,
            max_turns=max_turns,
            started=started,
        )
    finally:
        reset_hermes_home_override(token)


def _invoke_subagent_scoped(
    caller_agent: Any,
    client: Any,
    target_home: Path,
    prompt: str,
    *,
    files: Optional[list[str]],
    tools_subset: Optional[list[str]],
    timeout_s: float,
    max_turns: int,
    started: float,
) -> SubagentResult:
    tool_defs = _subagent_tool_definitions(caller_agent, tools_subset)
    valid_tool_names = {
        tool.get("function", {}).get("name")
        for tool in tool_defs
        if tool.get("function", {}).get("name")
    }
    system_prompt = _build_target_system_prompt(target_home, valid_tool_names)
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": _build_user_prompt(prompt, files)},
    ]
    tool_calls: list[dict] = []
    usage = _empty_usage()
    output = ""

    for turn_index in range(max(1, int(max_turns))):
        elapsed = time.monotonic() - started
        if elapsed >= timeout_s:
            return SubagentResult(
                output="",
                tool_calls=tool_calls,
                usage=usage,
                error=_timeout_error(timeout_s),
                elapsed_s=elapsed,
            )

        try:
            response = _create_chat_completion(
                caller_agent,
                client,
                messages,
                tool_defs,
                timeout_s=max(0.001, timeout_s - elapsed),
            )
        except Exception as exc:
            return SubagentResult(
                output="",
                tool_calls=tool_calls,
                usage=usage,
                error=f"{type(exc).__name__}: {exc}",
                elapsed_s=time.monotonic() - started,
            )

        usage = _merge_usage(usage, _usage_dict(response, caller_agent))
        elapsed = time.monotonic() - started
        if elapsed >= timeout_s:
            return SubagentResult(
                output="",
                tool_calls=tool_calls,
                usage=usage,
                error=_timeout_error(timeout_s),
                elapsed_s=elapsed,
            )

        message = _response_message(response)
        assistant_tool_calls = _message_tool_calls(message)
        content = _message_content(message)

        if not assistant_tool_calls:
            output = content
            return SubagentResult(
                output=output,
                tool_calls=tool_calls,
                usage=usage,
                elapsed_s=time.monotonic() - started,
            )

        messages.append(_assistant_message_dict(message, assistant_tool_calls, content))
        for tool_call in assistant_tool_calls:
            record, result = _dispatch_subagent_tool(
                caller_agent,
                tool_call,
                valid_tool_names,
                tool_defs,
            )
            tool_calls.append(record)
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": record.get("id") or f"subagent_tool_{turn_index}",
                    "name": record["name"],
                    "content": result,
                }
            )

    return SubagentResult(
        output=output,
        tool_calls=tool_calls,
        usage=usage,
        error=f"max_turns_reached_{max_turns}",
        elapsed_s=time.monotonic() - started,
    )


def _resolve_target_profile_dir(target_profile_id: str) -> Path:
    profile_id = str(target_profile_id or "").strip()
    root = get_default_hermes_root()
    if profile_id in {"", "default", "agent-default"}:
        target = resolve_default_agent_dir(root)
    else:
        target = root / "profiles" / profile_id

    if not target.is_dir():
        raise ValueError(f"target profile missing: {profile_id or 'default'}")
    return target


def _caller_llm_client(caller_agent: Any) -> Any:
    return getattr(caller_agent, "llm_client", None) or getattr(caller_agent, "client", None)


def _subagent_tool_definitions(
    caller_agent: Any,
    tools_subset: Optional[list[str]],
) -> list[dict]:
    from model_tools import get_tool_definitions

    caller_tool_names = _caller_tool_names(caller_agent)
    if tools_subset is not None:
        requested = {str(name).strip() for name in tools_subset if str(name).strip()}
        enabled_tools = sorted(requested & caller_tool_names) if caller_tool_names else sorted(requested)
    elif caller_tool_names:
        enabled_tools = sorted(caller_tool_names)
    else:
        enabled_tools = []

    return get_tool_definitions(
        quiet_mode=True,
        enabled_tools=enabled_tools,
    )


def _caller_tool_names(caller_agent: Any) -> set[str]:
    names = {
        str(name).strip()
        for name in (getattr(caller_agent, "valid_tool_names", None) or [])
        if str(name).strip()
    }
    if names:
        return names
    for tool in getattr(caller_agent, "tools", None) or []:
        if not isinstance(tool, dict):
            continue
        name = tool.get("function", {}).get("name")
        if name:
            names.add(str(name).strip())
    return {name for name in names if name}


def _build_target_system_prompt(target_home: Path, valid_tool_names: set[str]) -> str:
    parts: list[str] = []
    try:
        from agent.prompt_builder import build_skills_system_prompt, load_soul_md

        soul = load_soul_md()
        if soul:
            parts.append(soul)
        skills = build_skills_system_prompt(
            available_tools=valid_tool_names,
            available_toolsets=_toolsets_for_tools(valid_tool_names),
        )
        if skills:
            parts.extend(["# Skills", skills])
    except Exception:
        soul_path = target_home / "SOUL.md"
        if soul_path.is_file():
            try:
                parts.append(soul_path.read_text(encoding="utf-8").strip())
            except OSError:
                pass

    memory_context = _read_memory_context(target_home)
    if memory_context:
        parts.append(memory_context)
    if not parts:
        parts.append("You are a Hermes subagent. Complete the requested task and return a concise result.")
    return "\n\n".join(part for part in parts if part)


def _toolsets_for_tools(tool_names: set[str]) -> set[str]:
    try:
        from model_tools import get_toolset_for_tool
    except Exception:
        return set()
    result = set()
    for tool_name in tool_names:
        try:
            toolset = get_toolset_for_tool(tool_name)
        except Exception:
            toolset = None
        if toolset:
            result.add(toolset)
    return result


def _read_memory_context(target_home: Path) -> str:
    sections: list[str] = []
    for name in ("MEMORY.md", "USER.md"):
        path = target_home / "memories" / name
        try:
            content = path.read_text(encoding="utf-8").strip()
        except (FileNotFoundError, OSError, UnicodeDecodeError):
            continue
        if content:
            sections.extend([f"## {name}", content])
    if not sections:
        return ""
    return "# Persistent Memory\n\n" + "\n\n".join(sections)


def _build_user_prompt(prompt: str, files: Optional[list[str]]) -> str:
    file_context = _read_file_context(files or [])
    if not file_context:
        return str(prompt)
    return f"{prompt}\n\n# Provided Files\n\n{file_context}"


def _read_file_context(files: list[str]) -> str:
    sections: list[str] = []
    for raw_path in files:
        path = Path(str(raw_path)).expanduser()
        if not path.is_absolute():
            path = Path.cwd() / path
        try:
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            sections.append(f"## {path}\n[unreadable: {type(exc).__name__}: {exc}]")
            continue
        if len(content) > 40_000:
            content = content[:40_000] + "\n[truncated]"
        sections.append(f"## {path}\n\n```\n{content}\n```")
    return "\n\n".join(sections)


def _create_chat_completion(
    caller_agent: Any,
    client: Any,
    messages: list[dict],
    tool_defs: list[dict],
    *,
    timeout_s: float,
) -> Any:
    kwargs = {
        "model": getattr(caller_agent, "model", "") or "",
        "messages": messages,
    }
    if tool_defs:
        kwargs["tools"] = tool_defs
        kwargs["tool_choice"] = "auto"
    if timeout_s:
        kwargs["timeout"] = timeout_s

    request_overrides = getattr(caller_agent, "request_overrides", None)
    if isinstance(request_overrides, dict):
        for key, value in request_overrides.items():
            if key not in kwargs:
                kwargs[key] = value
    return client.chat.completions.create(**kwargs)


def _response_message(response: Any) -> Any:
    choices = getattr(response, "choices", None) or []
    if not choices:
        raise RuntimeError("LLM response did not include choices")
    return getattr(choices[0], "message", None)


def _message_content(message: Any) -> str:
    if message is None:
        return ""
    if isinstance(message, dict):
        return str(message.get("content") or "")
    return str(getattr(message, "content", None) or "")


def _message_tool_calls(message: Any) -> list[Any]:
    if message is None:
        return []
    if isinstance(message, dict):
        return list(message.get("tool_calls") or [])
    return list(getattr(message, "tool_calls", None) or [])


def _assistant_message_dict(message: Any, tool_calls: list[Any], content: str) -> dict:
    if isinstance(message, dict):
        return {
            "role": "assistant",
            "content": content or None,
            "tool_calls": message.get("tool_calls") or [_tool_call_to_message_dict(tc) for tc in tool_calls],
        }
    return {
        "role": "assistant",
        "content": content or None,
        "tool_calls": [_tool_call_to_message_dict(tc) for tc in tool_calls],
    }


def _tool_call_to_message_dict(tool_call: Any) -> dict:
    name, arguments = _tool_call_name_args(tool_call)
    return {
        "id": _tool_call_id(tool_call),
        "type": "function",
        "function": {
            "name": name,
            "arguments": json.dumps(arguments, ensure_ascii=False),
        },
    }


def _dispatch_subagent_tool(
    caller_agent: Any,
    tool_call: Any,
    valid_tool_names: set[str],
    tool_defs: list[dict],
) -> tuple[dict, str]:
    name, arguments = _tool_call_name_args(tool_call)
    record = {
        "id": _tool_call_id(tool_call),
        "name": name,
        "arguments": arguments,
    }
    if name not in valid_tool_names:
        result = json.dumps(
            {"error": f"Tool '{name}' is not available to this subagent"},
            ensure_ascii=False,
        )
        record["error"] = "tool_not_available"
        return record, result

    from model_tools import handle_function_call

    result = handle_function_call(
        name,
        arguments,
        task_id=f"subagent:{id(caller_agent)}",
        enabled_tools=[tool["function"]["name"] for tool in tool_defs],
        parent_agent=caller_agent,
    )
    record["result"] = _json_or_text(result)
    return record, result


def _tool_call_id(tool_call: Any) -> str:
    if isinstance(tool_call, dict):
        return str(tool_call.get("id") or "")
    return str(getattr(tool_call, "id", "") or "")


def _tool_call_name_args(tool_call: Any) -> tuple[str, dict]:
    if isinstance(tool_call, dict):
        fn = tool_call.get("function") or {}
        name = fn.get("name") or tool_call.get("name") or ""
        raw_args = fn.get("arguments") or tool_call.get("arguments") or {}
    else:
        fn = getattr(tool_call, "function", None)
        name = getattr(fn, "name", "") if fn is not None else getattr(tool_call, "name", "")
        raw_args = getattr(fn, "arguments", {}) if fn is not None else getattr(tool_call, "arguments", {})
    if isinstance(raw_args, str):
        try:
            args = json.loads(raw_args) if raw_args.strip() else {}
        except ValueError:
            args = {"_raw": raw_args}
    elif isinstance(raw_args, dict):
        args = dict(raw_args)
    else:
        args = {}
    return str(name), args


def _usage_dict(response: Any, caller_agent: Any) -> dict:
    raw_usage = getattr(response, "usage", None)
    if not raw_usage:
        return _empty_usage()
    try:
        from agent.usage_pricing import normalize_usage

        normalized = normalize_usage(
            raw_usage,
            provider=getattr(caller_agent, "provider", None),
            api_mode=getattr(caller_agent, "api_mode", None),
        )
        return {
            "input_tokens": normalized.input_tokens,
            "output_tokens": normalized.output_tokens,
            "cache_read_tokens": normalized.cache_read_tokens,
            "cache_write_tokens": normalized.cache_write_tokens,
            "reasoning_tokens": normalized.reasoning_tokens,
            "total_tokens": normalized.total_tokens,
        }
    except Exception:
        prompt = int(getattr(raw_usage, "prompt_tokens", 0) or 0)
        completion = int(getattr(raw_usage, "completion_tokens", 0) or 0)
        total = int(getattr(raw_usage, "total_tokens", prompt + completion) or 0)
        return {
            "input_tokens": prompt,
            "output_tokens": completion,
            "cache_read_tokens": 0,
            "cache_write_tokens": 0,
            "reasoning_tokens": 0,
            "total_tokens": total,
        }


def _empty_usage() -> dict:
    return {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "reasoning_tokens": 0,
        "total_tokens": 0,
    }


def _merge_usage(left: dict, right: dict) -> dict:
    merged = _empty_usage()
    for key in merged:
        merged[key] = int(left.get(key, 0) or 0) + int(right.get(key, 0) or 0)
    return merged


def _json_or_text(value: str) -> Any:
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return value


def _timeout_error(timeout_s: float) -> str:
    return f"timeout_after_{int(timeout_s) if float(timeout_s).is_integer() else timeout_s}s"
