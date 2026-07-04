"""Per-request Dovie attribution headers.

The Dovie cloud query context is turn-local state carried in
``HERMES_DOVIE_PRODUCT_CONTEXT``.  Do not snapshot it into SDK
``default_headers``: provider clients are cached across turns.

Context propagation has two process-local owners. The main gateway
process sets this ContextVar in ``tui_gateway.methods.prompt`` through
``_set_session_context`` for in-process consumers. Worker subprocesses
receive the same value on the ``run.start`` frame and set it in
``tui_gateway.run_worker`` before handling the turn. Review both paths
before changing either side; ContextVars do not cross process boundaries.
Worker-side agent runner threads propagate a ``copy_context()`` snapshot
at ``AgentRunBackend.start``; do not rely on ``threading.Thread`` to
inherit ContextVars when changing worker/agent_run_backend thread
boundaries. Agent-internal ``chat.completions.create`` calls spawn
another ``threading.Thread`` for the real HTTP request in
``chat_completion_helpers``; that layer also needs ``copy_context()``
propagation, so check this constraint before changing those thread
spawn points.
"""

from __future__ import annotations

import json
from typing import Any, Iterable, Mapping

_HEADER_VALUE_LIMIT = 512

_CLOUD_QUERY_HEADER_KEYS = {
    "query_id": "X-Dovie-Query-Id",
    "root_query_id": "X-Dovie-Root-Query-Id",
    "agent_run_id": "X-Dovie-Agent-Run-Id",
    "query_context_token": "X-Dovie-Query-Context-Token",
}


def _first_present(mapping: Mapping[str, Any], keys: Iterable[str]) -> Any:
    for key in keys:
        value = mapping.get(key)
        if value is not None and value != "":
            return value
    return None


def _clean_header_value(value: Any) -> str:
    if value is None:
        return ""
    cleaned = str(value).replace("\r", " ").replace("\n", " ").strip()
    if len(cleaned) > _HEADER_VALUE_LIMIT:
        cleaned = cleaned[:_HEADER_VALUE_LIMIT]
    return cleaned


def _put_header(headers: dict[str, str], name: str, value: Any) -> None:
    cleaned = _clean_header_value(value)
    if cleaned:
        headers[name] = cleaned


def build_dovie_attribution_headers() -> dict[str, str]:
    """Build Dovie attribution headers from the current task context.

    Malformed or absent context produces no headers.  Header values are
    sanitized for HTTP header safety and capped to a bounded size.
    """
    try:
        from gateway.session_context import get_session_env

        raw_context = get_session_env("HERMES_DOVIE_PRODUCT_CONTEXT", "")
        if not raw_context:
            return {}
        parsed = json.loads(raw_context)
        if not isinstance(parsed, dict):
            return {}

        headers: dict[str, str] = {}
        cloud_query = parsed.get("cloud_query") or parsed.get("cloudQuery") or {}
        if isinstance(cloud_query, dict):
            for source_key, header_name in _CLOUD_QUERY_HEADER_KEYS.items():
                _put_header(headers, header_name, cloud_query.get(source_key))

        source_profile_id = _first_present(
            parsed,
            (
                "sourceAgentProfileId",
                "source_agent_profile_id",
            ),
        )
        root_profile_id = (
            _first_present(
                parsed,
                (
                    "root_agent_profile_id",
                    "rootAgentProfileId",
                ),
            )
            or source_profile_id
        )
        executing_profile_id = (
            _first_present(
                parsed,
                (
                    "executing_agent_profile_id",
                    "executingAgentProfileId",
                ),
            )
            or source_profile_id
            or root_profile_id
        )
        _put_header(headers, "X-Dovie-Root-Agent-Profile-Id", root_profile_id)
        _put_header(headers, "X-Dovie-Executing-Agent-Profile-Id", executing_profile_id)
        _put_header(
            headers,
            "X-Dovie-Agent-Role",
            _first_present(
                parsed,
                (
                    "agent_role",
                    "agentRole",
                ),
            ),
        )

        _put_header(
            headers,
            "X-Dovie-Conversation-Session-Id",
            _first_present(
                parsed,
                (
                    "sourceSessionId",
                    "source_session_id",
                    "conversation_session_id",
                    "conversationSessionId",
                ),
            ),
        )
        _put_header(
            headers,
            "X-Dovie-Run-Id",
            _first_present(parsed, ("sourceRunId", "source_run_id", "run_id", "runId")),
        )
        _put_header(
            headers,
            "X-Dovie-Turn-Id",
            _first_present(parsed, ("sourceTurnId", "source_turn_id", "turn_id", "turnId")),
        )
        _put_header(
            headers,
            "X-Dovie-Client-Message-Id",
            _first_present(
                parsed,
                (
                    "sourceClientMessageId",
                    "source_client_message_id",
                    "client_message_id",
                    "clientMessageId",
                ),
            ),
        )
        return headers
    except Exception:
        return {}


def dovie_attribution_request_hook(request: Any) -> None:
    """Sync httpx request hook that injects Dovie attribution headers."""
    for name, value in build_dovie_attribution_headers().items():
        if name not in request.headers:
            request.headers[name] = value


async def dovie_attribution_async_request_hook(request: Any) -> None:
    """Async httpx request hook that injects Dovie attribution headers."""
    dovie_attribution_request_hook(request)


def _hook_for_http_client(http_client: Any):
    try:
        import httpx

        if isinstance(http_client, httpx.AsyncClient):
            return dovie_attribution_async_request_hook
        if isinstance(http_client, httpx.Client):
            return dovie_attribution_request_hook
    except Exception:
        pass
    return None


def _declared_attr(obj: Any, attr: str) -> Any:
    try:
        obj_vars = vars(obj)
    except TypeError:
        return getattr(obj, attr, None)
    return obj_vars.get(attr)


def _http_client_candidates(
    client_or_http_client: Any,
    seen: set[int] | None = None,
) -> Iterable[Any]:
    if client_or_http_client is None:
        return
    if seen is None:
        seen = set()
    obj_id = id(client_or_http_client)
    if obj_id in seen:
        return
    seen.add(obj_id)
    yield client_or_http_client
    for attr in ("_client", "_http_client", "_http"):
        candidate = _declared_attr(client_or_http_client, attr)
        if candidate is not None:
            yield candidate
    real_client = _declared_attr(client_or_http_client, "_real_client")
    if real_client is not None and real_client is not client_or_http_client:
        yield from _http_client_candidates(real_client, seen)


def attach_dovie_attribution_request_hook(client_or_http_client: Any) -> Any:
    """Append the Dovie request hook to an SDK or raw httpx client.

    Existing hooks are preserved and the hook is added at most once.  The
    input object is returned so callers can use this inline at construction
    or return sites.
    """
    try:
        for http_client in _http_client_candidates(client_or_http_client):
            hook = _hook_for_http_client(http_client)
            if hook is None:
                continue
            event_hooks = getattr(http_client, "event_hooks", None)
            if not isinstance(event_hooks, dict):
                continue
            request_hooks = event_hooks.setdefault("request", [])
            if not isinstance(request_hooks, list):
                request_hooks = list(request_hooks)
                event_hooks["request"] = request_hooks
            if hook not in request_hooks:
                request_hooks.append(hook)
    except Exception:
        pass
    return client_or_http_client


__all__ = [
    "attach_dovie_attribution_request_hook",
    "build_dovie_attribution_headers",
    "dovie_attribution_async_request_hook",
    "dovie_attribution_request_hook",
]
