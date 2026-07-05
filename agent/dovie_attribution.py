"""Per-request Dovie attribution headers.

The Dovie cloud query context is turn-local state carried in
``HERMES_DOVIE_PRODUCT_CONTEXT``.  Do not snapshot it into SDK
``default_headers``: provider clients are cached across turns.

The base side channel intentionally uses ``os.environ`` instead of a
turn-scoped ``ContextVar``.  Worker subprocesses receive the context on
the ``run.start`` frame, set the process environment before handling the
turn, and restore it when the turn exits.  All threads in that worker
process therefore see the same value, including nested raw
``threading.Thread`` calls inside agent/runtime code, without requiring
per-boundary propagation.

Subagent attribution is a narrower exception layered on top of the base
context.  A subagent may override only the executing profile and role while
continuing to inherit the root query token, query ids, and ``agent_run_id``
claim from the process context.  That overlay is held in a ``ContextVar`` so
concurrent delegations cannot race through ``os.environ``.

The correctness assumption is that a single worker subprocess handles
only one turn at a time.  Hermes enforces that in ``AgentRunBackend`` by
refusing a second active run in the same worker.  If that invariant ever
changes, this process-level side channel must be re-evaluated: use
per-turn subprocess isolation or a stronger per-turn isolation mechanism
before allowing concurrent turns in one worker process.

Changing Hermes' internal threading model does not require attribution
propagation changes as long as the single-worker/single-turn invariant
remains intact.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Iterable, Iterator, Mapping

_HEADER_VALUE_LIMIT = 2048  # HTTP header 允许约 8KB;JWT token 加上强 SECRET_KEY 后可达 500+ 字符,原 512 会截掉 signature 尾部导致 backend 校验失败

_DOVIE_ATTRIBUTION_OVERLAY_KEYS = frozenset(
    {
        "executing_agent_profile_id",
        "agent_role",
    }
)

DOVIE_ATTRIBUTION_OVERLAY: ContextVar[dict | None] = ContextVar(
    "DOVIE_ATTRIBUTION_OVERLAY",
    default=None,
)

_CLOUD_QUERY_HEADER_KEYS = {
    "query_id": "X-Dovie-Query-Id",
    "root_query_id": "X-Dovie-Root-Query-Id",
    "agent_run_id": "X-Dovie-Agent-Run-Id",
    "query_context_token": "X-Dovie-Query-Context-Token",
}

_DOVIE_ATTRIBUTION_OVERLAY_HEADER_KEYS = {
    "executing_agent_profile_id": "X-Dovie-Executing-Agent-Profile-Id",
    "agent_role": "X-Dovie-Agent-Role",
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


def _current_dovie_product_context() -> dict[str, Any] | None:
    from gateway.session_context import get_session_env

    raw_context = get_session_env("HERMES_DOVIE_PRODUCT_CONTEXT", "")
    if not raw_context:
        return None
    parsed = json.loads(raw_context)
    return parsed if isinstance(parsed, dict) else None


def _filtered_overlay() -> dict[str, Any]:
    overlay = DOVIE_ATTRIBUTION_OVERLAY.get()
    if not isinstance(overlay, Mapping):
        return {}
    return {
        key: overlay.get(key)
        for key in _DOVIE_ATTRIBUTION_OVERLAY_KEYS
        if key in overlay
    }


def _overlay_or_base(overlay: Mapping[str, Any], key: str, base: Any) -> Any:
    if key in overlay:
        return overlay.get(key)
    return base


@contextmanager
def dovie_child_run_overlay(
    executing_agent_profile_id: str,
    agent_role: str,
) -> Iterator[None]:
    """Temporarily override Dovie executing identity for a subagent."""

    executing_profile_id = _clean_header_value(executing_agent_profile_id)
    if not executing_profile_id:
        yield
        return

    overlay = {
        "executing_agent_profile_id": executing_profile_id,
        "agent_role": agent_role,
    }
    token = DOVIE_ATTRIBUTION_OVERLAY.set(overlay)
    try:
        yield
    finally:
        DOVIE_ATTRIBUTION_OVERLAY.reset(token)


def build_dovie_attribution_overlay_headers() -> dict[str, str]:
    """Build only the child overlay headers for per-request overrides."""
    try:
        if _current_dovie_product_context() is None:
            return {}
    except Exception:
        return {}
    overlay = _filtered_overlay()
    headers: dict[str, str] = {}
    for source_key, header_name in _DOVIE_ATTRIBUTION_OVERLAY_HEADER_KEYS.items():
        _put_header(headers, header_name, overlay.get(source_key))
    return headers


def build_dovie_attribution_headers() -> dict[str, str]:
    """Build Dovie attribution headers from the current task context.

    Malformed or absent context produces no headers.  Header values are
    sanitized for HTTP header safety and capped to a bounded size.
    """
    try:
        parsed = _current_dovie_product_context()
        if parsed is None:
            return {}

        overlay = _filtered_overlay()
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
            _overlay_or_base(
                overlay,
                "executing_agent_profile_id",
                (
                    _first_present(
                        parsed,
                        (
                            "executing_agent_profile_id",
                            "executingAgentProfileId",
                        ),
                    )
                    or source_profile_id
                    or root_profile_id
                ),
            )
        )
        _put_header(headers, "X-Dovie-Root-Agent-Profile-Id", root_profile_id)
        _put_header(headers, "X-Dovie-Executing-Agent-Profile-Id", executing_profile_id)
        _put_header(
            headers,
            "X-Dovie-Agent-Role",
            _overlay_or_base(
                overlay,
                "agent_role",
                _first_present(
                    parsed,
                    (
                        "agent_role",
                        "agentRole",
                    ),
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
    "DOVIE_ATTRIBUTION_OVERLAY",
    "attach_dovie_attribution_request_hook",
    "build_dovie_attribution_overlay_headers",
    "build_dovie_attribution_headers",
    "dovie_child_run_overlay",
    "dovie_attribution_async_request_hook",
    "dovie_attribution_request_hook",
]
