"""HTTP route registry and platform callback ingress for the API server."""

from __future__ import annotations

import asyncio
import logging
import re
from contextlib import nullcontext
from contextvars import ContextVar
from typing import Any, List, Optional

try:
    from aiohttp import web
except ImportError:  # pragma: no cover - optional dependency gate
    web = None  # type: ignore[assignment]

from channels.platforms.api_server_support import _openai_error

logger = logging.getLogger(__name__)
_api_request_profile: ContextVar[Optional[str]] = ContextVar(
    "api_request_profile",
    default=None,
)
_PROFILE_REJECTED = object()


class APIServerRoutingMixin:
    """Own API route discovery and authenticated platform event dispatch."""

    @staticmethod
    def _clean_log_value(value: Any, *, max_len: int = 200) -> str:
        """Remove log-control characters and bound untrusted metadata."""
        if value is None:
            return ""
        return str(value).replace("\r", " ").replace("\n", " ").strip()[:max_len]

    def _request_audit_context(self, request: "web.Request") -> dict[str, str]:
        peer_ip = ""
        try:
            transport = request.transport
            peer = transport.get_extra_info("peername") if transport else None
            if isinstance(peer, (tuple, list)) and peer:
                peer_ip = str(peer[0])
        except Exception:
            pass
        return {
            "remote": self._clean_log_value(
                getattr(request, "remote", "") or peer_ip
            ),
            "peer_ip": self._clean_log_value(peer_ip),
            "forwarded_for": self._clean_log_value(
                request.headers.get("X-Forwarded-For", "")
            ),
            "real_ip": self._clean_log_value(
                request.headers.get("X-Real-IP", "")
            ),
            "method": self._clean_log_value(request.method, max_len=16),
            "path": self._clean_log_value(request.path_qs, max_len=500),
            "user_agent": self._clean_log_value(
                request.headers.get("User-Agent", ""),
                max_len=300,
            ),
        }

    def _request_audit_log_suffix(self, request: "web.Request") -> str:
        context = self._request_audit_context(request)
        fields = [
            f"{key}={value!r}"
            for key, value in context.items()
            if value
        ]
        return " ".join(fields) if fields else "source='unknown'"

    def _cron_origin_from_request(self, request: "web.Request") -> dict[str, str]:
        """Capture bounded provenance without treating proxy headers as trust."""
        context = self._request_audit_context(request)
        origin = {"platform": "api_server", "chat_id": "api"}
        mappings = {
            "remote": "source_ip",
            "peer_ip": "peer_ip",
            "forwarded_for": "forwarded_for",
            "real_ip": "real_ip",
            "user_agent": "user_agent",
        }
        for source, target in mappings.items():
            if context.get(source):
                origin[target] = context[source]
        return origin

    def _readiness_work_counts(self) -> tuple[int, int, int]:
        """Return bounded counts from subsystem-owned public state."""
        try:
            active_api_work = sum(
                item.surface == "api_server"
                for item in self._active_work_registry.snapshot()
            )
        except Exception:
            active_api_work = sum(
                status.get("status")
                in {"queued", "running", "waiting_for_approval", "stopping"}
                for status in self._run_statuses.values()
            )
        process_depth = 0
        active_delegations = 0
        try:
            from tools.process_registry import process_registry

            process_depth = process_registry.completion_queue.qsize()
        except Exception:
            pass
        try:
            from tools.async_delegation import active_count

            active_delegations = active_count()
        except Exception:
            pass
        return active_api_work, process_depth, active_delegations

    def _resolve_request_profile(self, request: "web.Request"):
        profile = str(request.match_info.get("profile") or "").strip()
        if not profile:
            return None
        runner = getattr(self, "gateway_runner", None)
        config = getattr(runner, "config", None)
        if not getattr(config, "multiplex_profiles", False):
            return None
        try:
            from hermes_cli.profiles import profiles_to_serve

            served = {
                name for name, _home in profiles_to_serve(multiplex=True)
            }
        except Exception:
            return _PROFILE_REJECTED
        return profile if profile in served else _PROFILE_REJECTED

    def _profile_scope(self, profile: Optional[str]):
        runner = getattr(self, "gateway_runner", None)
        config = getattr(runner, "config", None)
        if not getattr(config, "multiplex_profiles", False):
            return nullcontext()
        if runner is not None:
            from hermes_gateway.profile_runtime import profile_runtime_for

            return profile_runtime_for(runner).scope_for_profile(profile)
        from hermes_constants import get_hermes_home
        from hermes_gateway.profile_runtime import profile_runtime_scope

        return profile_runtime_scope(get_hermes_home())

    def _make_profile_prefix_middleware(self):
        @web.middleware
        async def profile_prefix_middleware(request: "web.Request", handler):
            profile = self._resolve_request_profile(request)
            if profile is _PROFILE_REJECTED:
                return web.json_response(
                    {"error": "Unknown or unconfigured profile"},
                    status=404,
                )
            token = _api_request_profile.set(profile)
            try:
                with self._profile_scope(profile):
                    return await handler(request)
            finally:
                _api_request_profile.reset(token)

        return profile_prefix_middleware

    @staticmethod
    def _request_profile() -> Optional[str]:
        return _api_request_profile.get()

    @staticmethod
    def _normalize_callback_platform(value: str) -> str:
        normalized = (value or "").strip().lower().replace("-", "_")
        if not re.fullmatch(r"[a-z0-9_]+", normalized):
            return ""
        return normalized

    def _get_platform_callback_adapter(
        self,
        request: "web.Request",
        platform_name: str,
    ) -> Optional[Any]:
        injected = request.app.get("platform_event_adapters")
        if isinstance(injected, dict):
            adapter = injected.get(platform_name)
            if adapter is not None:
                return adapter

        adapter = request.app.get(f"{platform_name}_adapter")
        if adapter is not None:
            return adapter

        runner = getattr(self, "gateway_runner", None) or request.app.get(
            "gateway_runner"
        )
        try:
            from channels.config import Platform
            from channels.session_identity import SessionSource
            from hermes_gateway.platform_runtime import platform_runtime_for

            if runner is not None:
                source = SessionSource(
                    platform=Platform(platform_name),
                    chat_id="",
                    profile=self._request_profile(),
                )
                return platform_runtime_for(runner).adapter_for_source(source)
        except Exception:
            adapters = getattr(runner, "adapters", None) or {}
            for platform, candidate in adapters.items():
                if getattr(platform, "value", platform) == platform_name:
                    return candidate
        return None

    async def _handle_platform_event_callback(
        self,
        request: "web.Request",
    ) -> "web.Response":
        platform_name = self._normalize_callback_platform(
            request.match_info.get("platform", "")
        )
        if not platform_name:
            return web.json_response(
                _openai_error("Invalid platform name", code="invalid_platform"),
                status=400,
            )

        adapter = self._get_platform_callback_adapter(request, platform_name)
        if adapter is None:
            return web.json_response(
                _openai_error(
                    "Platform adapter is not connected",
                    code="platform_unavailable",
                ),
                status=503,
            )

        verifier = getattr(adapter, "verify_http_event_request", None)
        dispatcher = getattr(adapter, "dispatch_http_event", None)
        if not callable(verifier) or not callable(dispatcher):
            return web.json_response(
                _openai_error(
                    "Platform adapter does not support HTTP events",
                    code="platform_http_events_unsupported",
                ),
                status=503,
            )

        auth_header = request.headers.get("Authorization", "")
        try:
            if asyncio.iscoroutinefunction(verifier):
                ok, code = await verifier(auth_header)
            else:
                ok, code = await asyncio.to_thread(verifier, auth_header)
        except Exception:
            logger.exception(
                "Platform HTTP event verifier failed for %s",
                platform_name,
            )
            ok, code = False, "platform_event_verifier_error"
        if not ok:
            return web.json_response(
                _openai_error(
                    "Invalid platform event authorization",
                    code=code or "invalid_platform_event_authorization",
                ),
                status=401,
            )

        try:
            payload = await request.json()
        except Exception:
            return web.json_response(
                _openai_error(
                    "Invalid JSON in platform event",
                    code="invalid_json",
                ),
                status=400,
            )
        if not isinstance(payload, dict):
            return web.json_response(
                _openai_error(
                    "Platform event must be a JSON object",
                    code="invalid_request",
                ),
                status=400,
            )

        try:
            result = await dispatcher(payload)
        except Exception:
            logger.exception(
                "Platform HTTP event dispatch failed for %s",
                platform_name,
            )
            return web.json_response(
                _openai_error(
                    "Platform event dispatch failed",
                    err_type="server_error",
                    code="platform_event_dispatch_failed",
                ),
                status=500,
            )
        return web.json_response(result if isinstance(result, dict) else {})

    def _http_route_table(self) -> List[tuple]:
        """Return the canonical HTTP route contract registered at startup."""
        return [
            ("GET", "/health", self._handle_health),
            ("GET", "/health/detailed", self._handle_health_detailed),
            ("GET", "/v1/health", self._handle_health),
            ("GET", "/v1/models", self._handle_models),
            ("GET", "/v1/capabilities", self._handle_capabilities),
            ("GET", "/v1/skills", self._handle_skills),
            ("GET", "/v1/toolsets", self._handle_toolsets),
            ("GET", "/api/sessions", self._handle_list_sessions),
            ("POST", "/api/sessions", self._handle_create_session),
            ("GET", "/api/sessions/{session_id}", self._handle_get_session),
            ("PATCH", "/api/sessions/{session_id}", self._handle_patch_session),
            ("DELETE", "/api/sessions/{session_id}", self._handle_delete_session),
            (
                "GET",
                "/api/sessions/{session_id}/messages",
                self._handle_session_messages,
            ),
            (
                "POST",
                "/api/sessions/{session_id}/fork",
                self._handle_fork_session,
            ),
            (
                "POST",
                "/api/sessions/{session_id}/chat",
                self._handle_session_chat,
            ),
            (
                "POST",
                "/api/sessions/{session_id}/chat/stream",
                self._handle_session_chat_stream,
            ),
            ("POST", "/v1/chat/completions", self._handle_chat_completions),
            ("POST", "/v1/responses", self._handle_responses),
            ("GET", "/v1/responses/{response_id}", self._handle_get_response),
            (
                "DELETE",
                "/v1/responses/{response_id}",
                self._handle_delete_response,
            ),
            (
                "POST",
                "/api/platforms/{platform}/events",
                self._handle_platform_event_callback,
            ),
            ("GET", "/api/jobs", self._handle_list_jobs),
            ("POST", "/api/jobs", self._handle_create_job),
            ("GET", "/api/jobs/{job_id}", self._handle_get_job),
            ("PATCH", "/api/jobs/{job_id}", self._handle_update_job),
            ("DELETE", "/api/jobs/{job_id}", self._handle_delete_job),
            ("POST", "/api/jobs/{job_id}/pause", self._handle_pause_job),
            ("POST", "/api/jobs/{job_id}/resume", self._handle_resume_job),
            ("POST", "/api/jobs/{job_id}/run", self._handle_run_job),
            ("POST", "/api/cron/fire", self._handle_cron_fire),
            ("POST", "/v1/runs", self._handle_runs),
            ("GET", "/v1/runs/{run_id}", self._handle_get_run),
            ("GET", "/v1/runs/{run_id}/events", self._handle_run_events),
            (
                "POST",
                "/v1/runs/{run_id}/approval",
                self._handle_run_approval,
            ),
            ("POST", "/v1/runs/{run_id}/stop", self._handle_stop_run),
        ]
