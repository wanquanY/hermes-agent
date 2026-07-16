"""Structured run routes for the API server adapter."""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from typing import Any, Dict, List, Optional

from hermes_agent.application.active_work_registry import WorkRejected

try:
    from aiohttp import web
except ImportError:  # pragma: no cover - optional dependency gate
    web = None  # type: ignore[assignment]

from channels.platforms.api_server_support import (
    _coerce_request_bool,
    _openai_error,
)

logger = logging.getLogger(__name__)


class APIServerRunsMixin:
    # /v1/runs — structured event streaming
    # ------------------------------------------------------------------

    _RUN_STREAM_TTL = 300  # seconds before orphaned runs are swept
    _RUN_STATUS_TTL = 3600  # seconds to retain terminal run status for polling

    def _set_run_status(self, run_id: str, status: str, **fields: Any) -> Dict[str, Any]:
        """Update pollable run status without exposing private agent objects."""
        now = time.time()
        current = self._run_statuses.get(run_id, {})
        current.update({
            "object": "hermes.run",
            "run_id": run_id,
            "status": status,
            "updated_at": now,
        })
        current.setdefault("created_at", fields.pop("created_at", now))
        current.update(fields)
        self._run_statuses[run_id] = current
        return current

    def _make_run_event_callback(self, run_id: str, loop: "asyncio.AbstractEventLoop"):
        """Return a tool_progress_callback that pushes structured events to the run's SSE queue."""
        def _push(event: Dict[str, Any]) -> None:
            self._set_run_status(
                run_id,
                self._run_statuses.get(run_id, {}).get("status", "running"),
                last_event=event.get("event"),
            )
            q = self._run_streams.get(run_id)
            if q is None:
                return
            try:
                loop.call_soon_threadsafe(q.put_nowait, event)
            except Exception:
                pass

        def _callback(event_type: str, tool_name: str = None, preview: str = None, args=None, **kwargs):
            ts = time.time()
            if event_type == "tool.started":
                _push({
                    "event": "tool.started",
                    "run_id": run_id,
                    "timestamp": ts,
                    "tool": tool_name,
                    "preview": preview,
                })
            elif event_type == "tool.completed":
                _push({
                    "event": "tool.completed",
                    "run_id": run_id,
                    "timestamp": ts,
                    "tool": tool_name,
                    "duration": round(kwargs.get("duration", 0), 3),
                    "error": kwargs.get("is_error", False),
                })
            elif event_type == "reasoning.available":
                _push({
                    "event": "reasoning.available",
                    "run_id": run_id,
                    "timestamp": ts,
                    "text": preview or "",
                })
            # _thinking and subagent_progress are intentionally not forwarded

        return _callback

    async def _handle_runs(self, request: "web.Request") -> "web.Response":
        """POST /v1/runs — start an agent run, return run_id immediately."""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err

        # Long-term memory scope header (see chat_completions for details).
        gateway_session_key, key_err = self._parse_session_key_header(request)
        if key_err is not None:
            return key_err

        # Enforce concurrency limit (shared across all agent-serving
        # endpoints; configurable via gateway.api_server.max_concurrent_runs).
        limited = self._concurrency_limited_response()
        if limited is not None:
            return limited

        try:
            body = await request.json()
        except Exception:
            return web.json_response(_openai_error("Invalid JSON"), status=400)

        raw_input = body.get("input")
        if not raw_input:
            return web.json_response(_openai_error("Missing 'input' field"), status=400)

        user_message = raw_input if isinstance(raw_input, str) else (raw_input[-1].get("content", "") if isinstance(raw_input, list) else "")
        if not user_message:
            return web.json_response(_openai_error("No user message found in input"), status=400)

        instructions = body.get("instructions")
        previous_response_id = body.get("previous_response_id")

        # Accept explicit conversation_history from the request body.
        # Precedence: explicit conversation_history > previous_response_id.
        conversation_history: List[Dict[str, str]] = []
        raw_history = body.get("conversation_history")
        if raw_history:
            if not isinstance(raw_history, list):
                return web.json_response(
                    _openai_error("'conversation_history' must be an array of message objects"),
                    status=400,
                )
            for i, entry in enumerate(raw_history):
                if not isinstance(entry, dict) or "role" not in entry or "content" not in entry:
                    return web.json_response(
                        _openai_error(f"conversation_history[{i}] must have 'role' and 'content' fields"),
                        status=400,
                    )
                conversation_history.append({"role": str(entry["role"]), "content": str(entry["content"])})
            if previous_response_id:
                logger.debug("Both conversation_history and previous_response_id provided; using conversation_history")

        conversation_session_id = None
        if not conversation_history and previous_response_id:
            stored = self._response_store.get(previous_response_id)
            if stored:
                conversation_history = list(stored.get("conversation_history", []))
                conversation_session_id = stored.get("session_id")
                if instructions is None:
                    instructions = stored.get("instructions")

        # When input is a multi-message array, extract all but the last
        # message as conversation history (the last becomes user_message).
        # Only fires when no explicit history was provided.
        if not conversation_history and isinstance(raw_input, list) and len(raw_input) > 1:
            for msg in raw_input[:-1]:
                if isinstance(msg, dict) and msg.get("role") and msg.get("content"):
                    content = msg["content"]
                    if isinstance(content, list):
                        # Flatten multi-part content blocks to text
                        content = " ".join(
                            part.get("text", "") for part in content
                            if isinstance(part, dict) and part.get("type") == "text"
                        )
                    conversation_history.append({"role": msg["role"], "content": str(content)})

        run_id = f"run_{uuid.uuid4().hex}"
        session_id = body.get("session_id") or conversation_session_id or run_id
        approval_session_key = gateway_session_key or session_id or run_id
        ephemeral_system_prompt = instructions
        loop = asyncio.get_running_loop()
        q: "asyncio.Queue[Optional[Dict]]" = asyncio.Queue()
        created_at = time.time()
        run_control: Dict[str, Any] = {"agent": None, "task": None}

        def _persist_drain_timeout() -> None:
            def _persist() -> None:
                self._set_run_status(
                    run_id,
                    "cancelled",
                    error="runtime drain timeout",
                    last_event="run.cancelled",
                )
                q.put_nowait({
                    "event": "run.cancelled",
                    "run_id": run_id,
                    "timestamp": time.time(),
                    "reason": "runtime_drain_timeout",
                })

            loop.call_soon_threadsafe(_persist)

        def _cancel_run_for_drain() -> None:
            agent = run_control.get("agent")
            if agent is not None:
                try:
                    agent.interrupt("API runtime drain timeout")
                except Exception:
                    logger.debug("run %s drain interrupt failed", run_id, exc_info=True)
            task = run_control.get("task")
            if task is not None and not task.done():
                loop.call_soon_threadsafe(task.cancel)

        try:
            work_lease = self._active_work_registry.register(
                kind="api_structured_run",
                surface="api_server",
                work_id=f"api-run:{run_id}",
                metadata={"run_id": run_id, "session_id": session_id},
                persist_timeout=_persist_drain_timeout,
                cancel=_cancel_run_for_drain,
            )
        except WorkRejected as exc:
            return web.json_response(
                _openai_error(str(exc), code=exc.code),
                status=503,
                headers={"Retry-After": "1"},
            )
        self._run_streams[run_id] = q
        self._run_streams_created[run_id] = created_at
        self._run_approval_sessions[run_id] = approval_session_key

        event_cb = self._make_run_event_callback(run_id, loop)

        # Also wire stream_delta_callback so message.delta events flow through.
        def _text_cb(delta: Optional[str]) -> None:
            if delta is None:
                return
            try:
                loop.call_soon_threadsafe(q.put_nowait, {
                    "event": "message.delta",
                    "run_id": run_id,
                    "timestamp": time.time(),
                    "delta": delta,
                })
            except Exception:
                pass

        self._set_run_status(
            run_id,
            "queued",
            created_at=created_at,
            session_id=session_id,
            model=body.get("model", self._model_name),
        )

        async def _run_and_close():
            try:
                self._set_run_status(run_id, "running")
                agent = self._create_agent(
                    ephemeral_system_prompt=ephemeral_system_prompt,
                    session_id=session_id,
                    stream_delta_callback=_text_cb,
                    tool_progress_callback=event_cb,
                    gateway_session_key=gateway_session_key,
                )
                self._active_run_agents[run_id] = agent
                run_control["agent"] = agent

                def _approval_notify(approval_data: Dict[str, Any]) -> None:
                    event = dict(approval_data or {})
                    if "command" in event:
                        from hermes_agent.gateway.runtime_config import redact_approval_command

                        event["command"] = redact_approval_command(event.get("command"))
                    event.update({
                        "event": "approval.request",
                        "run_id": run_id,
                        "timestamp": time.time(),
                        "choices": ["once", "session", "always", "deny"],
                    })
                    self._set_run_status(
                        run_id,
                        "waiting_for_approval",
                        last_event="approval.request",
                    )
                    try:
                        loop.call_soon_threadsafe(q.put_nowait, event)
                    except Exception:
                        pass

                def _run_sync():
                    from channels.session_context import clear_session_vars
                    from tools.approval import (
                        register_gateway_notify,
                        reset_current_session_key,
                        set_current_session_key,
                        unregister_gateway_notify,
                    )

                    effective_task_id = session_id or run_id
                    approval_token = None
                    session_tokens = []
                    try:
                        # Bind approval/session identity for this API run via
                        # contextvars so concurrent runs do not share process
                        # environment state.
                        approval_token = set_current_session_key(approval_session_key)
                        session_tokens = self._bind_api_server_session(
                            session_key=approval_session_key,
                        )
                        register_gateway_notify(approval_session_key, _approval_notify)
                        r = agent.run_conversation(
                            user_message=user_message,
                            conversation_history=conversation_history,
                            task_id=effective_task_id,
                        )
                    finally:
                        try:
                            unregister_gateway_notify(approval_session_key)
                        finally:
                            if approval_token is not None:
                                try:
                                    reset_current_session_key(approval_token)
                                except Exception:
                                    pass
                            if session_tokens:
                                try:
                                    clear_session_vars(session_tokens)
                                except Exception:
                                    pass
                    u = {
                        "input_tokens": getattr(agent, "session_prompt_tokens", 0) or 0,
                        "output_tokens": getattr(agent, "session_completion_tokens", 0) or 0,
                        "total_tokens": getattr(agent, "session_total_tokens", 0) or 0,
                    }
                    return r, u

                result, usage = await asyncio.get_running_loop().run_in_executor(None, _run_sync)
                # Check for structured failure (non-retryable client errors like
                # 401/400 return failed=True instead of raising, so the except
                # block below never fires — issue #15561).
                if isinstance(result, dict) and result.get("failed"):
                    error_msg = result.get("error") or "agent run failed"
                    q.put_nowait({
                        "event": "run.failed",
                        "run_id": run_id,
                        "timestamp": time.time(),
                        "error": error_msg,
                    })
                    self._set_run_status(
                        run_id,
                        "failed",
                        error=error_msg,
                        last_event="run.failed",
                    )
                else:
                    final_response = result.get("final_response", "") if isinstance(result, dict) else ""
                    q.put_nowait({
                        "event": "run.completed",
                        "run_id": run_id,
                        "timestamp": time.time(),
                        "output": final_response,
                        "usage": usage,
                    })
                    self._set_run_status(
                        run_id,
                        "completed",
                        output=final_response,
                        usage=usage,
                        last_event="run.completed",
                    )
            except asyncio.CancelledError:
                self._set_run_status(
                    run_id,
                    "cancelled",
                    last_event="run.cancelled",
                )
                try:
                    q.put_nowait({
                        "event": "run.cancelled",
                        "run_id": run_id,
                        "timestamp": time.time(),
                    })
                except Exception:
                    pass
                raise
            except Exception as exc:
                logger.exception("[api_server] run %s failed", run_id)
                self._set_run_status(
                    run_id,
                    "failed",
                    error=str(exc),
                    last_event="run.failed",
                )
                try:
                    q.put_nowait({
                        "event": "run.failed",
                        "run_id": run_id,
                        "timestamp": time.time(),
                        "error": str(exc),
                    })
                except Exception:
                    pass
            finally:
                # If the asyncio wrapper is cancelled (for example via
                # /stop), the executor thread can still be blocked waiting
                # on an approval Event.  Unregistering here releases those
                # waits immediately; the in-thread unregister is harmlessly
                # idempotent on normal completion.
                try:
                    from tools.approval import unregister_gateway_notify

                    unregister_gateway_notify(approval_session_key)
                except Exception:
                    pass
                # Sentinel: signal SSE stream to close
                try:
                    q.put_nowait(None)
                except Exception:
                    pass
                self._active_run_agents.pop(run_id, None)
                self._active_run_tasks.pop(run_id, None)
                self._run_approval_sessions.pop(run_id, None)
                work_lease.release()

        try:
            task = asyncio.create_task(_run_and_close())
        except Exception:
            work_lease.release()
            self._run_streams.pop(run_id, None)
            self._run_streams_created.pop(run_id, None)
            self._run_approval_sessions.pop(run_id, None)
            raise
        run_control["task"] = task
        self._active_run_tasks[run_id] = task
        try:
            self._background_tasks.add(task)
        except TypeError:
            pass
        if hasattr(task, "add_done_callback"):
            task.add_done_callback(self._background_tasks.discard)

        response_headers = (
            {"X-Hermes-Session-Key": gateway_session_key} if gateway_session_key else {}
        )
        return web.json_response(
            {"run_id": run_id, "status": "started"},
            status=202,
            headers=response_headers,
        )

    async def _handle_get_run(self, request: "web.Request") -> "web.Response":
        """GET /v1/runs/{run_id} — return pollable run status for external UIs."""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err

        run_id = request.match_info["run_id"]
        status = self._run_statuses.get(run_id)
        if status is None:
            return web.json_response(
                _openai_error(f"Run not found: {run_id}", code="run_not_found"),
                status=404,
            )
        return web.json_response(status)

    async def _handle_run_events(self, request: "web.Request") -> "web.StreamResponse":
        """GET /v1/runs/{run_id}/events — SSE stream of structured agent lifecycle events."""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err

        run_id = request.match_info["run_id"]

        # Allow subscribing slightly before the run is registered (race condition window)
        for _ in range(20):
            if run_id in self._run_streams:
                break
            await asyncio.sleep(0.05)
        else:
            return web.json_response(_openai_error(f"Run not found: {run_id}", code="run_not_found"), status=404)

        q = self._run_streams[run_id]

        response = web.StreamResponse(
            status=200,
            headers={
                "Content-Type": "text/event-stream",
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )
        await response.prepare(request)

        try:
            while True:
                try:
                    event = await asyncio.wait_for(q.get(), timeout=30.0)
                except asyncio.TimeoutError:
                    await response.write(b": keepalive\n\n")
                    continue
                if event is None:
                    # Run finished — send final SSE comment and close
                    await response.write(b": stream closed\n\n")
                    break
                payload = f"data: {json.dumps(event)}\n\n"
                await response.write(payload.encode())
        except Exception as exc:
            logger.debug("[api_server] SSE stream error for run %s: %s", run_id, exc)
        finally:
            self._run_streams.pop(run_id, None)
            self._run_streams_created.pop(run_id, None)

        return response


    async def _handle_run_approval(self, request: "web.Request") -> "web.Response":
        """POST /v1/runs/{run_id}/approval — resolve a pending run approval."""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err

        run_id = request.match_info["run_id"]
        status = self._run_statuses.get(run_id)
        if status is None:
            return web.json_response(
                _openai_error(f"Run not found: {run_id}", code="run_not_found"),
                status=404,
            )

        try:
            body = await request.json()
        except Exception:
            return web.json_response(_openai_error("Invalid JSON"), status=400)

        raw_choice = str(body.get("choice", "")).strip().lower()
        aliases = {"approve": "once", "approved": "once", "allow": "once"}
        choice = aliases.get(raw_choice, raw_choice)
        allowed = {"once", "session", "always", "deny"}
        if choice not in allowed:
            return web.json_response(
                _openai_error(
                    "Invalid approval choice; expected one of: once, session, always, deny",
                    code="invalid_approval_choice",
                ),
                status=400,
            )

        approval_session_key = self._run_approval_sessions.get(run_id)
        if not approval_session_key:
            return web.json_response(
                _openai_error(
                    f"Run has no active approval session: {run_id}",
                    code="approval_not_active",
                ),
                status=409,
            )

        resolve_all = (
            _coerce_request_bool(body.get("all"), default=False)
            or _coerce_request_bool(body.get("resolve_all"), default=False)
        )
        try:
            from tools.approval import resolve_gateway_approval

            resolved = resolve_gateway_approval(
                approval_session_key,
                choice,
                resolve_all=resolve_all,
            )
        except Exception as exc:
            logger.exception("[api_server] approval resolution failed for run %s", run_id)
            return web.json_response(_openai_error(str(exc)), status=500)

        if resolved <= 0:
            return web.json_response(
                _openai_error(
                    f"Run has no pending approval: {run_id}",
                    code="approval_not_pending",
                ),
                status=409,
            )

        self._set_run_status(run_id, "running", last_event="approval.responded")
        q = self._run_streams.get(run_id)
        if q is not None:
            try:
                q.put_nowait({
                    "event": "approval.responded",
                    "run_id": run_id,
                    "timestamp": time.time(),
                    "choice": choice,
                    "resolved": resolved,
                })
            except Exception:
                pass

        return web.json_response({
            "object": "hermes.run.approval_response",
            "run_id": run_id,
            "choice": choice,
            "resolved": resolved,
        })

    async def _handle_stop_run(self, request: "web.Request") -> "web.Response":
        """POST /v1/runs/{run_id}/stop — interrupt a running agent."""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err

        run_id = request.match_info["run_id"]
        agent = self._active_run_agents.get(run_id)
        task = self._active_run_tasks.get(run_id)

        if agent is None and task is None:
            return web.json_response(_openai_error(f"Run not found: {run_id}", code="run_not_found"), status=404)

        self._set_run_status(run_id, "stopping", last_event="run.stopping")

        if agent is not None:
            try:
                agent.interrupt("Stop requested via API")
            except Exception:
                pass

        if task is not None and not task.done():
            task.cancel()
            # Bounded wait: run_conversation() executes in the default
            # executor thread which task.cancel() cannot preempt — we rely on
            # agent.interrupt() above to break the loop. Cap the wait so a
            # slow/unresponsive interrupt can't hang this handler.
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=5.0)
            except asyncio.TimeoutError:
                logger.warning(
                    "[api_server] stop for run %s timed out after 5s; "
                    "agent may still be finishing the current step",
                    run_id,
                )
            except (asyncio.CancelledError, Exception):
                pass

        return web.json_response({"run_id": run_id, "status": "stopping"})

    async def _sweep_orphaned_runs(self) -> None:
        """Periodically clean up run streams that were never consumed."""
        while True:
            await asyncio.sleep(60)
            now = time.time()
            stale = [
                run_id
                for run_id, created_at in list(self._run_streams_created.items())
                if now - created_at > self._RUN_STREAM_TTL
            ]
            for run_id in stale:
                logger.debug("[api_server] sweeping orphaned run %s", run_id)
                try:
                    from tools.approval import unregister_gateway_notify

                    approval_session_key = self._run_approval_sessions.get(run_id)
                    if approval_session_key:
                        unregister_gateway_notify(approval_session_key)
                except Exception:
                    pass
                self._run_streams.pop(run_id, None)
                self._run_streams_created.pop(run_id, None)
                self._active_run_agents.pop(run_id, None)
                self._active_run_tasks.pop(run_id, None)
                self._run_approval_sessions.pop(run_id, None)

            stale_statuses = [
                run_id
                for run_id, status in list(self._run_statuses.items())
                if status.get("status") in {"completed", "failed", "cancelled"}
                and now - float(status.get("updated_at", 0) or 0) > self._RUN_STATUS_TTL
            ]
            for run_id in stale_statuses:
                self._run_statuses.pop(run_id, None)

    # ------------------------------------------------------------------
