from __future__ import annotations

import json
import time
from collections.abc import Callable
from typing import Any

from tui_gateway.services.artifacts import record_artifacts_from_tool_complete
from tui_gateway.services.transcript_messages import (
    serializable_tool_args as default_tool_args_payload,
    tool_context as default_tool_context,
)


DOXIE_STRUCTURED_RESULT_TOOLS = {
    "design_agent_profile",
    "create_agent_profile_draft",
    "create_agent_profile_revision_draft",
    "doxie_agent_profile_create_draft",
    "test_agent_profile",
    "doxie_automation_task_create",
    "doxie_automation_task_list",
    "doxie_automation_task_update",
    "doxie_automation_task_remove",
}


def _fmt_tool_duration(seconds: float | None) -> str:
    if seconds is None:
        return ""
    if seconds < 10:
        return f"{seconds:.1f}s"
    if seconds < 60:
        return f"{round(seconds)}s"
    mins, secs = divmod(int(round(seconds)), 60)
    return f"{mins}m {secs}s" if secs else f"{mins}m"


def _count_list(obj: object, *path: str) -> int | None:
    cur = obj
    for key in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return len(cur) if isinstance(cur, list) else None


def _tool_summary(name: str, result: str, duration_s: float | None) -> str | None:
    try:
        data = json.loads(result)
    except Exception:
        data = None

    dur = _fmt_tool_duration(duration_s)
    suffix = f" in {dur}" if dur else ""
    text = None

    if name == "web_search" and isinstance(data, dict):
        n = _count_list(data, "data", "web")
        if n is not None:
            text = f"Did {n} {'search' if n == 1 else 'searches'}"
    elif name == "web_extract" and isinstance(data, dict):
        n = _count_list(data, "results") or _count_list(data, "data", "results")
        if n is not None:
            text = f"Extracted {n} {'page' if n == 1 else 'pages'}"

    if isinstance(data, dict) and data.get("fallback_warning"):
        warning = str(data.get("fallback_warning") or "").strip()
        if warning:
            return f"{warning}{suffix}"

    return f"{text}{suffix}" if text else None


def _doxie_structured_tool_result(name: str, result: str) -> dict | None:
    if name not in DOXIE_STRUCTURED_RESULT_TOOLS:
        return None
    try:
        data = json.loads(result)
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    event_name = data.get("doxie_event")
    if name == "test_agent_profile":
        if event_name != "agent_profile_test_completed":
            return None
        return data
    if name == "doxie_automation_task_create":
        if event_name != "automation_job_created":
            return None
        job = data.get("job")
        return data if isinstance(job, dict) else None
    if name == "doxie_automation_task_list":
        if event_name != "automation_job_listed":
            return None
        return data if isinstance(data.get("jobs"), list) else None
    if name == "doxie_automation_task_update":
        if event_name != "automation_job_updated":
            return None
        return data if isinstance(data.get("job"), dict) else None
    if name == "doxie_automation_task_remove":
        if event_name != "automation_job_removed":
            return None
        return data if isinstance(data.get("job"), dict) else None
    if event_name not in {
        "agent_profile_draft_requested",
        "agent_profile_design_draft_requested",
        "agent_profile_design_draft_saved",
        "agent_profile_draft_saved",
    }:
        return None
    draft = data.get("draft")
    if not isinstance(draft, dict):
        return None
    event = {
        "doxie_event": event_name,
        "draft": draft,
    }
    operation = data.get("operation")
    if operation in {"create", "update", "upsert", "revision"}:
        event["operation"] = operation
    return event


def session_interrupted(session: dict | None) -> bool:
    if not session:
        return False
    interrupted_run_id = str(session.get("interrupted_run_id") or "")
    interrupted_turn_id = str(session.get("interrupted_turn_id") or "")
    active_run_id = str(session.get("active_run_id") or "")
    active_turn_id = str(session.get("active_turn_id") or "")
    return bool(
        (interrupted_run_id and (not active_run_id or interrupted_run_id == active_run_id))
        or (interrupted_turn_id and (not active_turn_id or interrupted_turn_id == active_turn_id))
    )


class GatewayToolEventBridge:
    def __init__(
        self,
        *,
        sessions: dict[str, dict],
        emit: Callable[[str, str, dict | None], Any],
        tool_progress_enabled: Callable[[str], bool],
        session_cwd: Callable[[dict], str],
        session_verbose: Callable[[str], bool] | None = None,
        tool_context: Callable[[str, dict], str] = default_tool_context,
        tool_args_payload: Callable[[dict | None], dict] = default_tool_args_payload,
        tool_args_text: Callable[[dict], str] | None = None,
        tool_result_text: Callable[[object], str] | None = None,
        thinking_event: str = "agent.musing",
    ) -> None:
        self._sessions = sessions
        self._emit = emit
        self._tool_progress_enabled = tool_progress_enabled
        self._session_cwd = session_cwd
        self._session_verbose = session_verbose or (lambda _sid: False)
        self._tool_context = tool_context
        self._tool_args_payload = tool_args_payload
        self._tool_args_text = tool_args_text
        self._tool_result_text = tool_result_text
        self._thinking_event = thinking_event

    def on_tool_start(self, sid: str, tool_call_id: str, name: str, args: dict) -> None:
        session = self._sessions.get(sid)
        if session_interrupted(session):
            return
        enabled = self._tool_progress_enabled(sid)
        if session is not None:
            try:
                from agent.display import capture_local_edit_snapshot

                snapshot = capture_local_edit_snapshot(name, args)
                if snapshot is not None:
                    session.setdefault("edit_snapshots", {})[tool_call_id] = snapshot
            except Exception:
                pass
            session.setdefault("tool_started_at", {})[tool_call_id] = time.time()
        if enabled:
            payload = {
                "tool_id": tool_call_id,
                "name": name,
                "context": self._tool_context(name, args),
                "arguments": self._tool_args_payload(args),
            }
            if self._session_verbose(sid) and self._tool_args_text:
                args_text = self._tool_args_text(args)
                if args_text:
                    payload["args_text"] = args_text
            self._emit("tool.start", sid, payload)
            if name == "test_agent_profile":
                self._emit(
                    "agent_profile_test.start",
                    sid,
                    {
                        "tool_id": tool_call_id,
                        "name": name,
                        "arguments": self._tool_args_payload(args),
                    },
                )

    def on_tool_complete(self, sid: str, tool_call_id: str, name: str, args: dict, result: str) -> None:
        session = self._sessions.get(sid)
        if session_interrupted(session):
            return
        payload = {
            "tool_id": tool_call_id,
            "name": name,
            "context": self._tool_context(name, args),
            "arguments": self._tool_args_payload(args),
        }
        snapshot = None
        started_at = None
        if session is not None:
            snapshot = session.setdefault("edit_snapshots", {}).pop(tool_call_id, None)
            started_at = session.setdefault("tool_started_at", {}).pop(tool_call_id, None)
        duration_s = time.time() - started_at if started_at else None
        if duration_s is not None:
            payload["duration_s"] = duration_s
        summary = _tool_summary(name, result, duration_s)
        if summary:
            payload["summary"] = summary
        if self._session_verbose(sid) and self._tool_result_text:
            result_text = self._tool_result_text(result)
            if result_text:
                payload["result_text"] = result_text
        doxie_result = _doxie_structured_tool_result(name, result)
        if doxie_result:
            payload["result"] = doxie_result
        if name == "todo":
            try:
                data = json.loads(result)
                if isinstance(data, dict) and isinstance(data.get("todos"), list):
                    payload["todos"] = data.get("todos")
            except Exception:
                pass
        try:
            from agent.display import render_edit_diff_with_delta

            rendered: list[str] = []
            if render_edit_diff_with_delta(
                name,
                result,
                function_args=args,
                snapshot=snapshot,
                print_fn=rendered.append,
            ):
                payload["inline_diff"] = "\n".join(rendered)
        except Exception:
            pass
        enabled = self._tool_progress_enabled(sid)
        if enabled or payload.get("inline_diff") or doxie_result:
            self._emit("tool.complete", sid, payload)
            if name == "test_agent_profile":
                self._emit("agent_profile_test.complete", sid, payload)
        self.emit_artifacts_from_tool_complete(sid, tool_call_id, name, args, result)

    def emit_artifacts_from_tool_complete(
        self,
        sid: str,
        tool_call_id: str,
        name: str,
        args: dict,
        result: str,
    ) -> None:
        session = self._sessions.get(sid)
        if not session or session_interrupted(session) or session.get("transient"):
            return
        workspace = dict(session.get("workspace") or {})
        session_key = session.get("session_key") or sid
        origin = {
            key: value
            for key, value in {
                "run_id": str(session.get("active_run_id") or ""),
                "turn_id": str(session.get("active_turn_id") or ""),
                "client_message_id": str((session.get("pending_turn") or {}).get("client_message_id") or ""),
            }.items()
            if value
        }
        for payload in record_artifacts_from_tool_complete(
            session_id=session_key,
            tool_call_id=tool_call_id,
            name=name,
            args=args,
            result=result,
            cwd=self._session_cwd(session),
            workspace=workspace,
            origin=origin,
        ):
            self._emit("artifact.created", sid, payload)

    def on_tool_progress(
        self,
        sid: str,
        event_type: str,
        name: str | None = None,
        preview: str | None = None,
        _args: dict | None = None,
        **kwargs,
    ) -> None:
        if session_interrupted(self._sessions.get(sid)) or not self._tool_progress_enabled(sid):
            return
        if event_type == "tool.started" and name:
            self._emit("tool.progress", sid, {"name": name, "preview": preview or ""})
            return
        if event_type == "reasoning.available" and preview:
            payload: dict[str, object] = {"text": str(preview)}
            if self._session_verbose(sid):
                payload["verbose"] = True
            self._emit("reasoning.available", sid, payload)
            return
        if not event_type.startswith("subagent."):
            return
        payload = {
            "goal": str(kwargs.get("goal") or ""),
            "task_count": int(kwargs.get("task_count") or 1),
            "task_index": int(kwargs.get("task_index") or 0),
        }
        for field in ("subagent_id", "parent_id", "model", "status", "summary"):
            if kwargs.get(field):
                payload[field] = str(kwargs[field])
        if kwargs.get("depth") is not None:
            payload["depth"] = int(kwargs["depth"])
        if kwargs.get("tool_count") is not None:
            payload["tool_count"] = int(kwargs["tool_count"])
        if kwargs.get("toolsets"):
            payload["toolsets"] = [str(t) for t in kwargs["toolsets"]]
        for int_key in ("input_tokens", "output_tokens", "reasoning_tokens", "api_calls"):
            value = kwargs.get(int_key)
            if value is not None:
                try:
                    payload[int_key] = int(value)
                except (TypeError, ValueError):
                    pass
        if kwargs.get("cost_usd") is not None:
            try:
                payload["cost_usd"] = float(kwargs["cost_usd"])
            except (TypeError, ValueError):
                pass
        for list_key in ("files_read", "files_written"):
            if kwargs.get(list_key):
                payload[list_key] = [str(path) for path in kwargs[list_key]]
        if kwargs.get("output_tail"):
            payload["output_tail"] = list(kwargs["output_tail"])
        if name:
            payload["tool_name"] = str(name)
        if preview:
            payload["text"] = str(preview)
        if kwargs.get("duration_seconds") is not None:
            payload["duration_seconds"] = float(kwargs["duration_seconds"])
        if preview and event_type == "subagent.tool":
            payload["tool_preview"] = str(preview)
            payload["text"] = str(preview)
        if name == "test_agent_profile":
            mapped_type = {
                "subagent.output_delta": "agent_profile_test.output_delta",
                "subagent.thinking": "agent_profile_test.thinking",
                "subagent.tool": "agent_profile_test.tool",
                "subagent.progress": "agent_profile_test.progress",
                "subagent.complete": "agent_profile_test.complete",
            }.get(event_type)
            if mapped_type:
                self._emit(mapped_type, sid, payload)
                return
        self._emit(event_type, sid, payload)

    def agent_callbacks(
        self,
        sid: str,
        *,
        block: Callable[..., str],
        status_update: Callable[[str, str, str | None], None],
    ) -> dict:
        return {
            "tool_start_callback": lambda tc_id, name, args: self.on_tool_start(sid, tc_id, name, args),
            "tool_complete_callback": lambda tc_id, name, args, result: self.on_tool_complete(
                sid, tc_id, name, args, result
            ),
            "tool_progress_callback": lambda event_type, name=None, preview=None, args=None, **kwargs: self.on_tool_progress(
                sid, event_type, name, preview, args, **kwargs
            ),
            "tool_gen_callback": lambda name: self._tool_progress_enabled(sid)
            and self._emit("tool.generating", sid, {"name": name}),
            "thinking_callback": lambda text: self._emit(self._thinking_event, sid, {"text": text}),
            "reasoning_callback": lambda text: self._emit(
                "reasoning.delta",
                sid,
                {
                    "text": text,
                    "source": "provider_reasoning",
                    **({"verbose": True} if self._session_verbose(sid) else {}),
                },
            ),
            "status_callback": lambda kind, text=None: status_update(
                sid, str(kind), None if text is None else str(text)
            ),
            "clarify_callback": lambda q, c: block("clarify.request", sid, {"question": q, "choices": c}),
        }


def wire_secret_callbacks(sid: str, *, block: Callable[..., str]) -> None:
    from tools.terminal_tool import set_sudo_password_callback
    from tools.skills_tool import set_secret_capture_callback

    set_sudo_password_callback(lambda: block("sudo.request", sid, {}, timeout=120))

    def secret_cb(env_var, prompt, metadata=None):
        payload = {"prompt": prompt, "env_var": env_var}
        if metadata:
            payload["metadata"] = metadata
        value = block("secret.request", sid, payload)
        if not value:
            return {
                "success": True,
                "stored_as": env_var,
                "validated": False,
                "skipped": True,
                "message": "skipped",
            }
        from hermes_cli.config import save_env_value_secure

        return {
            **save_env_value_secure(env_var, value),
            "skipped": False,
            "message": "ok",
        }

    set_secret_capture_callback(secret_cb)
