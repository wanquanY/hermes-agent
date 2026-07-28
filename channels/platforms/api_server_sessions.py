"""Session resource routes for the API server adapter."""

from __future__ import annotations

import json
import logging
import time
import asyncio
import re
import uuid
from typing import Any, Dict, List

try:
    from aiohttp import web
except ImportError:  # pragma: no cover - optional dependency gate
    web = None  # type: ignore[assignment]

from channels.platforms.api_server_support import (
    CHAT_COMPLETIONS_SSE_KEEPALIVE_SECONDS,
    _coerce_request_bool,
    _openai_error,
    _redact_api_error_text,
    _resolve_media_to_data_urls,
    _session_chat_user_message,
)

logger = logging.getLogger(__name__)


class APIServerSessionsMixin:
    # /api/sessions — thin client/session resource API
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_nonnegative_int(value: Any, default: int, maximum: int) -> int:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return default
        if parsed < 0:
            return default
        return min(parsed, maximum)

    @staticmethod
    def _session_response(session: Dict[str, Any]) -> Dict[str, Any]:
        """Return a stable, client-safe session representation."""
        safe_keys = (
            "id", "source", "user_id", "model", "title", "started_at", "ended_at",
            "end_reason", "message_count", "tool_call_count", "input_tokens",
            "output_tokens", "cache_read_tokens", "cache_write_tokens",
            "reasoning_tokens", "estimated_cost_usd", "actual_cost_usd",
            "api_call_count", "parent_session_id", "last_active", "preview",
            "_lineage_root_id",
        )
        payload = {key: session.get(key) for key in safe_keys if key in session}
        # Avoid exposing full system prompts/model_config through the client API;
        # callers only need to know whether those snapshots exist.
        payload["has_system_prompt"] = bool(session.get("system_prompt"))
        payload["has_model_config"] = bool(session.get("model_config"))
        return payload

    @staticmethod
    def _message_response(message: Dict[str, Any]) -> Dict[str, Any]:
        safe_keys = (
            "id", "session_id", "role", "content", "tool_call_id", "tool_calls",
            "tool_name", "timestamp", "token_count", "finish_reason", "reasoning",
            "reasoning_content",
        )
        return {key: message.get(key) for key in safe_keys if key in message}

    def _turn_transcript_messages(
        self,
        history: list[dict[str, Any]],
        user_message: Any,
        result: Dict[str, Any],
    ) -> list[dict[str, Any]]:
        """Return the assistant/tool transcript slice for the current turn."""
        raw_messages = result.get("messages")
        if isinstance(raw_messages, list):
            start = 0
            history_len = len(history) if isinstance(history, list) else 0
            for idx in range(history_len, len(raw_messages)):
                item = raw_messages[idx]
                if (
                    isinstance(item, dict)
                    and item.get("role") == "user"
                    and item.get("content") == user_message
                ):
                    start = idx + 1
                    break
            turn: list[dict[str, Any]] = []
            for item in raw_messages[start:]:
                if not isinstance(item, dict) or item.get("role") == "user":
                    continue
                turn.append(self._message_response(item))
            if turn:
                return turn

        final_response = result.get("final_response")
        if isinstance(final_response, str) and final_response:
            return [{"role": "assistant", "content": final_response}]
        return []

    async def _read_json_body(self, request: "web.Request") -> tuple[Dict[str, Any], Optional["web.Response"]]:
        try:
            body = await request.json()
        except Exception:
            return {}, web.json_response(_openai_error("Invalid JSON in request body"), status=400)
        if not isinstance(body, dict):
            return {}, web.json_response(_openai_error("Request body must be a JSON object"), status=400)
        return body, None

    async def _get_existing_session_or_404(self, session_id: str) -> tuple[Optional[Dict[str, Any]], Optional["web.Response"]]:
        db = await self._ensure_session_db_async()
        if db is None:
            return None, web.json_response(_openai_error("Session database unavailable", code="session_db_unavailable"), status=503)
        session = await asyncio.to_thread(db.sessions.get, session_id)
        if not session:
            return None, web.json_response(_openai_error(f"Session not found: {session_id}", code="session_not_found"), status=404)
        return session, None

    async def _conversation_history_for_session(self, session_id: str) -> List[Dict[str, Any]]:
        db = await self._ensure_session_db_async()
        if db is None:
            return []
        try:
            return await asyncio.to_thread(
                db.messages.all_as_conversation,
                session_id,
            )
        except Exception as exc:
            logger.warning("Failed to load session history for %s: %s", session_id, exc)
            return []

    async def _handle_list_sessions(self, request: "web.Request") -> "web.Response":
        """GET /api/sessions — list persisted Hermes sessions."""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err

        db = await self._ensure_session_db_async()
        if db is None:
            return web.json_response(_openai_error("Session database unavailable", code="session_db_unavailable"), status=503)

        limit = self._parse_nonnegative_int(request.query.get("limit"), default=50, maximum=200)
        offset = self._parse_nonnegative_int(request.query.get("offset"), default=0, maximum=1_000_000)
        source = request.query.get("source") or None
        include_children = _coerce_request_bool(request.query.get("include_children"), default=False)
        sessions = await asyncio.to_thread(
            db.sessions.list_rich,
            source=source,
            limit=limit,
            offset=offset,
            include_children=include_children,
            order_by_last_active=True,
        )
        return web.json_response({
            "object": "list",
            "data": [self._session_response(s) for s in sessions],
            "limit": limit,
            "offset": offset,
            "has_more": len(sessions) == limit,
        })

    async def _handle_create_session(self, request: "web.Request") -> "web.Response":
        """POST /api/sessions — create an empty Hermes session row."""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err
        body, err = await self._read_json_body(request)
        if err:
            return err

        db = await self._ensure_session_db_async()
        if db is None:
            return web.json_response(_openai_error("Session database unavailable", code="session_db_unavailable"), status=503)

        raw_id = body.get("id") or body.get("session_id")
        raw_session_id = raw_id if raw_id else f"api_{int(time.time())}_{uuid.uuid4().hex[:8]}"
        try:
            from hermes_agent.domain.safe_identifiers import validate_external_session_id

            session_id = validate_external_session_id(raw_session_id)
        except ValueError:
            return web.json_response(_openai_error("Invalid session ID", code="invalid_session_id"), status=400)
        if len(session_id) > self._MAX_SESSION_HEADER_LEN:
            return web.json_response(_openai_error("Session ID too long", code="invalid_session_id"), status=400)
        model = body.get("model") or self._model_name
        system_prompt = body.get("system_prompt")
        if system_prompt is not None and not isinstance(system_prompt, str):
            return web.json_response(_openai_error("system_prompt must be a string", code="invalid_system_prompt"), status=400)
        title = body.get("title")
        try:
            created = await asyncio.to_thread(
                db.sessions.create_if_absent,
                session_id,
                "api_server",
                model=str(model) if model else None,
                system_prompt=system_prompt,
                title=title,
            )
        except ValueError as exc:
            return web.json_response(
                _openai_error(str(exc), code="invalid_title"),
                status=400,
            )
        if not created:
            return web.json_response(
                _openai_error(
                    f"Session already exists: {session_id}",
                    code="session_exists",
                ),
                status=409,
            )
        session = await asyncio.to_thread(db.sessions.get, session_id) or {
            "id": session_id,
            "source": "api_server",
            "model": model,
            "title": title,
        }
        return web.json_response({"object": "hermes.session", "session": self._session_response(session)}, status=201)

    async def _handle_get_session(self, request: "web.Request") -> "web.Response":
        """GET /api/sessions/{session_id}."""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err
        session, err = await self._get_existing_session_or_404(request.match_info["session_id"])
        if err:
            return err
        return web.json_response({"object": "hermes.session", "session": self._session_response(session)})

    async def _handle_patch_session(self, request: "web.Request") -> "web.Response":
        """PATCH /api/sessions/{session_id} — update client-safe session metadata."""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err
        session_id = request.match_info["session_id"]
        session, err = await self._get_existing_session_or_404(session_id)
        if err:
            return err
        body, err = await self._read_json_body(request)
        if err:
            return err
        allowed = {"title", "end_reason"}
        unknown = sorted(set(body) - allowed)
        if unknown:
            return web.json_response(_openai_error(f"Unsupported session fields: {', '.join(unknown)}", code="unsupported_session_field"), status=400)

        db = await self._ensure_session_db_async()
        if "title" in body:
            try:
                await asyncio.to_thread(
                    db.sessions.set_title,
                    session_id,
                    "" if body["title"] is None else str(body["title"]),
                )
            except ValueError as exc:
                return web.json_response(_openai_error(str(exc), code="invalid_title"), status=400)
        if body.get("end_reason"):
            await asyncio.to_thread(
                db.sessions.end,
                session_id,
                str(body["end_reason"]),
            )
        session = await asyncio.to_thread(db.sessions.get, session_id) or session
        return web.json_response({"object": "hermes.session", "session": self._session_response(session)})

    async def _handle_delete_session(self, request: "web.Request") -> "web.Response":
        """DELETE /api/sessions/{session_id}."""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err
        session_id = request.match_info["session_id"]
        session, err = await self._get_existing_session_or_404(session_id)
        if err:
            return err
        db = await self._ensure_session_db_async()
        result = await asyncio.to_thread(db.sessions.delete, session_id)
        return web.json_response({
            "object": "hermes.session.deleted",
            "id": session_id,
            "deleted": result.session_deleted,
        })

    async def _handle_session_messages(self, request: "web.Request") -> "web.Response":
        """GET /api/sessions/{session_id}/messages."""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err
        session_id = request.match_info["session_id"]
        session, err = await self._get_existing_session_or_404(session_id)
        if err:
            return err
        db = await self._ensure_session_db_async()
        resolved_id = await asyncio.to_thread(db.sessions.resolve_resume_id, session_id)
        messages = await asyncio.to_thread(db.messages.list, resolved_id)
        return web.json_response({
            "object": "list",
            "session_id": resolved_id,
            "data": [self._message_response(m) for m in messages],
        })

    async def _handle_fork_session(self, request: "web.Request") -> "web.Response":
        """POST /api/sessions/{session_id}/fork — create a child transcript session."""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err
        source_id = request.match_info["session_id"]
        source, err = await self._get_existing_session_or_404(source_id)
        if err:
            return err
        body, err = await self._read_json_body(request)
        if err:
            return err
        db = await self._ensure_session_db_async()
        raw_fork_id = body.get("id") or body.get("session_id") or f"api_{int(time.time())}_{uuid.uuid4().hex[:8]}"
        try:
            from hermes_agent.domain.safe_identifiers import validate_external_session_id

            fork_id = validate_external_session_id(raw_fork_id)
        except ValueError:
            return web.json_response(_openai_error("Invalid session ID", code="invalid_session_id"), status=400)
        if await asyncio.to_thread(db.sessions.get, fork_id):
            return web.json_response(_openai_error(f"Session already exists: {fork_id}", code="session_exists"), status=409)

        title = body.get("title")
        try:
            branch = await asyncio.to_thread(
                db.branches.branch_session,
                source_session_id=source_id,
                new_session_id=fork_id,
                scope="full_conversation",
                title=str(title) if title is not None else None,
                branch_origin="api_session_fork",
            )
        except ValueError as exc:
            return web.json_response(_openai_error(str(exc), code="invalid_branch"), status=400)
        fork = await asyncio.to_thread(db.sessions.get, fork_id) or {"id": fork_id}
        fork["parent_session_id"] = branch["parent_session_id"]
        fork["_lineage_root_id"] = branch["root_session_id"]
        return web.json_response({"object": "hermes.session", "session": self._session_response(fork)}, status=201)

    async def _handle_session_chat(self, request: "web.Request") -> "web.Response":
        """POST /api/sessions/{session_id}/chat — one synchronous agent turn."""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err
        gateway_session_key, key_err = self._parse_session_key_header(request)
        if key_err is not None:
            return key_err
        session_id = request.match_info["session_id"]
        session, err = await self._get_existing_session_or_404(session_id)
        if err:
            return err
        body, err = await self._read_json_body(request)
        if err:
            return err
        user_message, err = _session_chat_user_message(body)
        if err is not None:
            return err
        system_prompt = body.get("system_message") or body.get("instructions")
        if system_prompt is not None and not isinstance(system_prompt, str):
            return web.json_response(_openai_error("system_message must be a string", code="invalid_system_message"), status=400)
        history = await self._conversation_history_for_session(session_id)
        route = self._resolve_route(body.get("model") or session.get("model"))
        result, usage = await self._run_agent(
            user_message=user_message,
            conversation_history=history,
            ephemeral_system_prompt=system_prompt,
            session_id=session_id,
            gateway_session_key=gateway_session_key,
            route=route,
        )
        effective_session_id = result.get("session_id") if isinstance(result, dict) else session_id
        final_response = _resolve_media_to_data_urls(
            result.get("final_response", "") if isinstance(result, dict) else ""
        )
        headers = {"X-Hermes-Session-Id": effective_session_id or session_id}
        if gateway_session_key:
            headers["X-Hermes-Session-Key"] = gateway_session_key
        return web.json_response(
            {
                "object": "hermes.session.chat.completion",
                "session_id": effective_session_id or session_id,
                "message": {"role": "assistant", "content": final_response},
                "usage": usage,
            },
            headers=headers,
        )

    async def _handle_session_chat_stream(self, request: "web.Request") -> "web.StreamResponse":
        """POST /api/sessions/{session_id}/chat/stream — SSE wrapper over _run_agent."""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err
        gateway_session_key, key_err = self._parse_session_key_header(request)
        if key_err is not None:
            return key_err
        session_id = request.match_info["session_id"]
        session, err = await self._get_existing_session_or_404(session_id)
        if err:
            return err
        body, err = await self._read_json_body(request)
        if err:
            return err
        user_message, err = _session_chat_user_message(body)
        if err is not None:
            return err
        system_prompt = body.get("system_message") or body.get("instructions")
        if system_prompt is not None and not isinstance(system_prompt, str):
            return web.json_response(_openai_error("system_message must be a string", code="invalid_system_message"), status=400)
        route = self._resolve_route(body.get("model") or session.get("model"))

        loop = asyncio.get_running_loop()
        queue: "asyncio.Queue[Optional[tuple[str, Dict[str, Any]]]]" = asyncio.Queue()
        message_id = f"msg_{uuid.uuid4().hex}"
        run_id = f"run_{uuid.uuid4().hex}"
        seq = 0

        def _event_payload(name: str, payload: Dict[str, Any]) -> tuple[str, Dict[str, Any]]:
            nonlocal seq
            seq += 1
            payload.setdefault("session_id", session_id)
            payload.setdefault("run_id", run_id)
            payload.setdefault("seq", seq)
            payload.setdefault("ts", time.time())
            return name, payload

        def _enqueue(name: str, payload: Dict[str, Any]) -> None:
            event = _event_payload(name, payload)
            try:
                running_loop = asyncio.get_running_loop()
            except RuntimeError:
                running_loop = None
            try:
                if running_loop is loop:
                    queue.put_nowait(event)
                else:
                    loop.call_soon_threadsafe(queue.put_nowait, event)
            except RuntimeError:
                pass

        def _delta(delta: str) -> None:
            if delta:
                _enqueue("assistant.delta", {"message_id": message_id, "delta": delta})

        def _tool_progress(event_type: str, tool_name: str = None, preview: str = None, args=None, **kwargs) -> None:
            if event_type == "reasoning.available":
                _enqueue("tool.progress", {"message_id": message_id, "tool_name": tool_name or "_thinking", "delta": preview or ""})
            elif event_type in {"tool.started", "tool.completed", "tool.failed"}:
                event_name = event_type.replace("tool.", "tool.")
                _enqueue(event_name, {"message_id": message_id, "tool_name": tool_name, "preview": preview, "args": args})

        async def _run_and_signal() -> None:
            try:
                await queue.put(_event_payload("run.started", {"user_message": {"role": "user", "content": user_message}}))
                await queue.put(_event_payload("message.started", {"message": {"id": message_id, "role": "assistant"}}))
                history = await self._conversation_history_for_session(session_id)
                result, usage = await self._run_agent(
                    user_message=user_message,
                    conversation_history=history,
                    ephemeral_system_prompt=system_prompt,
                    session_id=session_id,
                    stream_delta_callback=_delta,
                    tool_progress_callback=_tool_progress,
                    gateway_session_key=gateway_session_key,
                    route=route,
                )
                final_response = _resolve_media_to_data_urls(
                    result.get("final_response", "")
                    if isinstance(result, dict)
                    else ""
                )
                effective_session_id = result.get("session_id", session_id) if isinstance(result, dict) else session_id
                turn_messages = self._turn_transcript_messages(history, user_message, result) if isinstance(result, dict) else []
                await queue.put(_event_payload("assistant.completed", {
                    "session_id": effective_session_id,
                    "message_id": message_id,
                    "content": final_response,
                    "completed": True,
                    "partial": False,
                    "interrupted": False,
                }))
                await queue.put(_event_payload("run.completed", {
                    "session_id": effective_session_id,
                    "message_id": message_id,
                    "completed": True,
                    "messages": turn_messages,
                    "usage": usage,
                }))
            except Exception as exc:
                logger.exception("[api_server] session chat stream failed")
                await queue.put(
                    _event_payload(
                        "error",
                        {"message": _redact_api_error_text(exc)},
                    )
                )
            finally:
                await queue.put(_event_payload("done", {}))
                await queue.put(None)

        task = asyncio.create_task(_run_and_signal())
        try:
            self._background_tasks.add(task)
        except TypeError:
            pass
        if hasattr(task, "add_done_callback"):
            task.add_done_callback(self._background_tasks.discard)

        headers = {
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "X-Hermes-Session-Id": session_id,
        }
        if gateway_session_key:
            headers["X-Hermes-Session-Key"] = gateway_session_key
        response = web.StreamResponse(status=200, headers=headers)
        await response.prepare(request)
        last_write = time.monotonic()
        try:
            while True:
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=CHAT_COMPLETIONS_SSE_KEEPALIVE_SECONDS)
                except asyncio.TimeoutError:
                    await response.write(b": keepalive\n\n")
                    last_write = time.monotonic()
                    continue
                if item is None:
                    break
                name, payload = item
                data = json.dumps(payload, ensure_ascii=False)
                await response.write(f"event: {name}\ndata: {data}\n\n".encode("utf-8"))
                last_write = time.monotonic()
        except (asyncio.CancelledError, ConnectionResetError):
            task.cancel()
            raise
        except Exception as exc:
            logger.debug("[api_server] session SSE stream error: %s", exc)
        return response
