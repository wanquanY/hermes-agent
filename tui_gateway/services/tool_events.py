from __future__ import annotations

import json
import time
from collections.abc import Callable
from typing import Any

from tui_gateway.services.artifacts import (
    capture_workspace_artifact_snapshot,
    record_artifacts_from_tool_complete,
)
from tui_gateway.services.transcript_messages import (
    serializable_tool_args as default_tool_args_payload,
    tool_context as default_tool_context,
)


DOVIE_STRUCTURED_RESULT_TOOLS = {
    "design_agent_profile",
    "create_agent_profile_draft",
    "create_agent_profile_revision_draft",
    "dovie_agent_profile_create_draft",
    "test_agent_profile",
    "dovie_automation_task_create",
    "dovie_automation_task_list",
    "dovie_automation_task_update",
    "dovie_automation_task_remove",
    "team_mission_start_task",
    "team_mission_node_create",
    "team_mission_edge_create",
    "team_mission_plan_complete",
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


def _dovie_structured_tool_result(name: str, result: str) -> dict | None:
    if name not in DOVIE_STRUCTURED_RESULT_TOOLS:
        return None
    try:
        data = json.loads(result)
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    event_name = data.get("dovie_event")
    if name == "test_agent_profile":
        if event_name != "agent_profile_test_completed":
            return None
        return data
    if name == "dovie_automation_task_create":
        if event_name != "automation_job_created":
            return None
        job = data.get("job")
        return data if isinstance(job, dict) else None
    if name == "dovie_automation_task_list":
        if event_name != "automation_job_listed":
            return None
        return data if isinstance(data.get("jobs"), list) else None
    if name == "dovie_automation_task_update":
        if event_name != "automation_job_updated":
            return None
        return data if isinstance(data.get("job"), dict) else None
    if name == "dovie_automation_task_remove":
        if event_name != "automation_job_removed":
            return None
        return data if isinstance(data.get("job"), dict) else None
    if name == "team_mission_start_task":
        control = data.get("hermes_control")
        if not isinstance(control, dict):
            return None
        if control.get("kind") != "team_mission_started":
            return None
        mission_id = str(data.get("mission_id") or "").strip()
        conversation_id = str(data.get("conversation_id") or "").strip()
        if not mission_id or not conversation_id:
            return None
        return {
            "dovie_event": "team_mission_started",
            "mission_id": mission_id,
            "conversation_id": conversation_id,
            "task_id": str(data.get("task_id") or "").strip(),
            "node": data.get("node") if isinstance(data.get("node"), dict) else {},
            "run": data.get("run") if isinstance(data.get("run"), dict) else {},
            "graph_summary": data.get("graph_summary") if isinstance(data.get("graph_summary"), dict) else {},
            "submission_status": str(data.get("submission_status") or "").strip(),
            "task_status": str(data.get("task_status") or control.get("mission_status") or "").strip(),
            "await_final_deliverable": bool(data.get("await_final_deliverable") or control.get("await_final_deliverable")),
        }
    if name == "team_mission_node_create":
        node = data.get("node")
        if not isinstance(node, dict):
            return None
        return {
            "dovie_event": "team_mission_node_created",
            "success": bool(data.get("success")),
            "mission_id": str(data.get("mission_id") or "").strip(),
            "node": node,
            "graph_summary": data.get("graph_summary") if isinstance(data.get("graph_summary"), dict) else {},
        }
    if name == "team_mission_edge_create":
        edge = data.get("edge")
        if not isinstance(edge, dict):
            return None
        return {
            "dovie_event": "team_mission_edge_created",
            "success": bool(data.get("success")),
            "mission_id": str(data.get("mission_id") or "").strip(),
            "edge": edge,
            "graph_summary": data.get("graph_summary") if isinstance(data.get("graph_summary"), dict) else {},
        }
    if name == "team_mission_plan_complete":
        if not data.get("mission_id"):
            return None
        return {
            "dovie_event": "team_mission_plan_completed",
            "success": bool(data.get("success")),
            "mission_id": str(data.get("mission_id") or "").strip(),
            "mission_status": str(data.get("mission_status") or "").strip(),
            "approval_requests": data.get("approval_requests") if isinstance(data.get("approval_requests"), list) else [],
            "auto_start_ready_nodes": bool(data.get("auto_start_ready_nodes")),
            "graph_summary": data.get("graph_summary") if isinstance(data.get("graph_summary"), dict) else {},
        }
    if name == "design_agent_profile" and event_name == "agent_profile_design_context":
        return data
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
        "dovie_event": event_name,
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
        before_tool_boundary: Callable[[str, str], Any] | None = None,
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
        self._before_tool_boundary = before_tool_boundary
        self._thinking_event = thinking_event

    def _notify_tool_boundary(self, sid: str, event_type: str) -> None:
        if self._before_tool_boundary is None:
            return
        try:
            self._before_tool_boundary(sid, event_type)
        except Exception:
            pass

    def on_tool_generating(self, sid: str, name: str | None) -> None:
        session = self._sessions.get(sid)
        if session_interrupted(session):
            return
        self._notify_tool_boundary(sid, "tool.generating")
        if self._tool_progress_enabled(sid):
            self._emit("tool.generating", sid, {"name": name})

    def on_reasoning_delta(self, sid: str, text: str) -> None:
        payload = {
            "text": text,
            "source": "provider_reasoning",
            **({"verbose": True} if self._session_verbose(sid) else {}),
        }
        self._emit("reasoning.delta", sid, payload)

    def on_tool_start(self, sid: str, tool_call_id: str, name: str, args: dict) -> None:
        session = self._sessions.get(sid)
        if session_interrupted(session):
            return
        self._notify_tool_boundary(sid, "tool.start")
        enabled = self._tool_progress_enabled(sid)
        if session is not None:
            try:
                from agent.display import capture_local_edit_snapshot

                snapshot = capture_local_edit_snapshot(name, args)
                if snapshot is not None:
                    session.setdefault("edit_snapshots", {})[tool_call_id] = snapshot
            except Exception:
                pass
            try:
                artifact_snapshot = capture_workspace_artifact_snapshot(
                    name=name,
                    cwd=self._session_cwd(session),
                    workspace=dict(session.get("workspace") or {}),
                )
                if artifact_snapshot is not None:
                    session.setdefault("artifact_snapshots", {})[tool_call_id] = artifact_snapshot
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
        artifact_snapshot = None
        started_at = None
        if session is not None:
            snapshot = session.setdefault("edit_snapshots", {}).pop(tool_call_id, None)
            artifact_snapshot = session.setdefault("artifact_snapshots", {}).pop(tool_call_id, None)
            started_at = session.setdefault("tool_started_at", {}).pop(tool_call_id, None)
        duration_s = time.time() - started_at if started_at else None
        if duration_s is not None:
            payload["duration_s"] = duration_s
        summary = _tool_summary(name, result, duration_s)
        if summary:
            payload["summary"] = summary
        if self._tool_result_text:
            result_text = self._tool_result_text(result)
            if result_text:
                payload["result_text"] = result_text
        dovie_result = _dovie_structured_tool_result(name, result)
        if dovie_result:
            payload["result"] = dovie_result
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
        if enabled or payload.get("inline_diff") or dovie_result:
            self._emit("tool.complete", sid, payload)
            if name == "test_agent_profile":
                self._emit("agent_profile_test.complete", sid, payload)
        self.emit_artifacts_from_tool_complete(
            sid,
            tool_call_id,
            name,
            args,
            result,
            artifact_snapshot=artifact_snapshot,
        )

    def emit_artifacts_from_tool_complete(
        self,
        sid: str,
        tool_call_id: str,
        name: str,
        args: dict,
        result: str,
        artifact_snapshot=None,
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
            workspace_snapshot=artifact_snapshot,
        ):
            event_type = "artifact.deleted" if payload.get("operation") == "deleted" else "artifact.created"
            self._emit(event_type, sid, payload)

    def on_tool_progress(
        self,
        sid: str,
        event_type: str,
        name: str | None = None,
        preview: str | None = None,
        _args: dict | None = None,
        **kwargs,
    ) -> None:
        session = self._sessions.get(sid)
        if not self._tool_progress_enabled(sid):
            return
        # Cancellation closes the content stream, but the terminal lifecycle
        # fact must still cross the worker boundary so history and status can
        # converge. Dropping subagent.complete here leaves the child running
        # forever in every downstream read model.
        if session_interrupted(session) and event_type != "subagent.complete":
            return
        if event_type == "tool.started" and name:
            self._emit("tool.progress", sid, {"name": name, "preview": preview or ""})
            return
        if event_type == "tool.output_risk" and name:
            metadata = kwargs.get("risk_metadata")
            if not isinstance(metadata, dict):
                return
            self._emit(
                "tool.output_risk",
                sid,
                {
                    "tool_id": str(kwargs.get("tool_call_id") or ""),
                    "name": str(name),
                    "risk": str(metadata.get("risk") or "low"),
                    "findings": [
                        str(item) for item in metadata.get("findings", [])
                    ],
                    "redacted": bool(metadata.get("redacted", False)),
                },
            )
            return
        if event_type == "reasoning.available" and preview:
            payload: dict[str, object] = {"text": str(preview)}
            if self._session_verbose(sid):
                payload["verbose"] = True
            self._emit("reasoning.available", sid, payload)
            return
        if not event_type.startswith("subagent."):
            return
        pending_turn = session.get("pending_turn") if isinstance(session, dict) else {}
        pending_turn = pending_turn if isinstance(pending_turn, dict) else {}
        payload = {
            key: value
            for key, value in {
                "goal": str(kwargs.get("goal") or ""),
                "task_count": int(kwargs.get("task_count") or 1),
                "task_index": int(kwargs.get("task_index") or 0),
                # Delegated children can outlive their parent turn.  Prefer the
                # immutable dispatch origin over mutable session state so a late
                # terminal event is neither dropped nor attached to a newer run.
                "run_id": str(
                    kwargs.get("run_id")
                    or (session or {}).get("active_run_id")
                    or ""
                ),
                "turn_id": str(
                    kwargs.get("turn_id")
                    or (session or {}).get("active_turn_id")
                    or ""
                ),
                "client_message_id": str(
                    kwargs.get("client_message_id")
                    or pending_turn.get("client_message_id")
                    or ""
                ),
                "runtime_scope_key": str(kwargs.get("runtime_scope_key") or ""),
                "activity_id": str(kwargs.get("activity_id") or ""),
            }.items()
            if value != ""
        }
        for field in (
            "subagent_id",
            "parent_id",
            "model",
            "provider",
            "status",
            "summary",
            "role",
            "context",
            "dispatch_message",
            "delegation_tool_name",
            "delegate_call_id",
            "tool_call_id",
            "tool_id",
            "agent_profile_id",
            "agent_profile_version_id",
            "agent_name",
            "agent_avatar",
        ):
            if kwargs.get(field):
                payload[field] = str(kwargs[field])
        if event_type in {"subagent.output_delta", "subagent.reasoning_delta"}:
            mode = str(kwargs.get("mode") or "append").strip().lower()
            payload["mode"] = mode if mode in {"append", "snapshot", "replace", "cumulative"} else "append"
            payload["delta"] = str(kwargs.get("delta") if kwargs.get("delta") is not None else preview or "")
            if kwargs.get("offset") is not None:
                try:
                    payload["offset"] = max(0, int(kwargs["offset"]))
                except (TypeError, ValueError):
                    pass
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
        if _args:
            payload["arguments"] = self._tool_args_payload(_args)
        if preview:
            payload["text"] = str(preview)
        if kwargs.get("duration_seconds") is not None:
            payload["duration_seconds"] = float(kwargs["duration_seconds"])
        raw_result = kwargs.get("result")
        if event_type == "subagent.tool" and raw_result is not None:
            result_str = raw_result if isinstance(raw_result, str) else str(raw_result)
            summary = _tool_summary(str(name or ""), result_str, payload.get("duration_seconds"))
            if summary:
                payload["summary"] = summary
            if self._tool_result_text:
                result_text = self._tool_result_text(result_str)
                if result_text:
                    payload["result_text"] = result_text
            dovie_result = _dovie_structured_tool_result(str(name or ""), result_str)
            if dovie_result:
                payload["result"] = dovie_result
            try:
                from agent.display import render_edit_diff_with_delta

                rendered: list[str] = []
                if render_edit_diff_with_delta(
                    str(name or ""),
                    result_str,
                    function_args=_args,
                    snapshot=None,
                    print_fn=rendered.append,
                ):
                    payload["inline_diff"] = "\n".join(rendered)
            except Exception:
                pass
        if preview and event_type == "subagent.tool":
            payload["tool_preview"] = str(preview)
            payload["text"] = str(preview)
        delegation_tool_name = str(payload.get("delegation_tool_name") or "").strip()
        if name == "test_agent_profile" or delegation_tool_name == "test_agent_profile":
            payload.pop("context", None)
            payload.pop("dispatch_message", None)
            mapped_type = {
                "subagent.spawn_requested": "agent_profile_test.progress",
                "subagent.start": "agent_profile_test.progress",
                "subagent.output_delta": "agent_profile_test.output_delta",
                "subagent.reasoning_delta": "agent_profile_test.thinking",
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
            "tool_gen_callback": lambda name: self.on_tool_generating(sid, name),
            "thinking_callback": lambda text: self._emit(self._thinking_event, sid, {"text": text}),
            "reasoning_callback": lambda text: self.on_reasoning_delta(sid, text),
            "status_callback": lambda kind, text=None: status_update(
                sid, str(kind), None if text is None else str(text)
            ),
            "clarify_callback": lambda q, c: block("clarify.request", sid, {"question": q, "choices": c}),
            "read_terminal_callback": lambda start=None, count=None: block(
                "terminal.read.request",
                sid,
                {
                    key: value
                    for key, value in (("start", start), ("count", count))
                    if value is not None
                },
                timeout=30,
            ),
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
