"""Runtime scope helpers.

Phase 6 slimmed this module down: the legacy sub-sidecar proxy stack
(``RuntimeWorkerPool``, ``RuntimeProxyBridge``, ``proxy_to_runtime``,
the per-profile ws-server spawn) is gone. The main sidecar now hosts
every control-plane method in-process, with ``enter_profile_context``
switching the ``_active_hermes_home`` ContextVar at ``handle_request``
dispatch entry so ``_get_db()`` resolves the right profile-scoped
control DB. Methods that actually run the agent (``run.submit`` /
``prompt.submit``) are routed through ``WorkerSupervisor`` via
``worker_runtime.primary_dispatch`` from ``ws.py``.

What remains here:
- ``RuntimeScope`` — the typed (agent_profile_id, runtime_scope_key,
  hermes_home) triple used pervasively to identify per-profile scope.
- ``runtime_scope_from_params`` / ``runtime_scope_from_request`` —
  parse a scope from a JSON-RPC request's ``params`` (handling the
  snake/camel and ``dovie_profile`` envelope variations the desktop
  client emits).
- ``should_proxy_to_runtime`` — a compatibility strategy predicate used by
  tests and routing call sites that need to classify a request without
  reintroducing the removed proxy transport.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

_log = logging.getLogger(__name__)


_CONTROL_PLANE_METHODS = frozenset(
    {
        "artifacts.list",
        "conversation.activity.list",
        "conversation.render_snapshot",
        "events.compact",
        "events.prune",
        "events.subscribe",
        "events.unsubscribe",
        "profile.archive",
        "profile.draft.discard",
        "profile.draft.get",
        "profile.draft.list",
        "profile.draft.upsert",
        "profile.get",
        "profile.growth.summary",
        "profile.list",
        "profile.upsert",
        "run.events",
        "runtime.ensure",
        "session.list",
        "session.messages",
        "session.message_metadata.merge",
        "session.status",
        "session.title",
        "team_mission.conversation.delete",
        "team_mission.conversation.ensure",
        "team_mission.conversation.list",
        "team_mission.conversation.participants",
        "team_mission.conversation.rename",
        "team_mission.conversation.render",
        "team_mission.conversation.resolve",
        "team_mission.conversation.runtime_session_ids",
        "team_mission.create",
        "team_mission.events",
        "team_mission.graph",
        "team_mission.graph.reduce",
        "team_mission.message.submit",
        "team_mission.node.history",
    }
)

_RUNTIME_AGENT_METHODS = frozenset(
    {
        "prompt.submit",
        "run.submit",
    }
)

_RUNTIME_STATE_READ_METHODS = frozenset(
    {
        "run.list",
        "run.status",
    }
)

_RUNTIME_MUTATION_METHODS = frozenset(
    {
        "team_mission.cancel",
        "team_mission.node.update",
        "team_mission.plan.approve",
        "team_mission.plan.reject",
        "team_mission.schedule.ready",
    }
)

_RUNTIME_REGISTRY_METHODS = frozenset(
    {
        "skills.reload",
        "toolsets.list",
        "tools.configure",
    }
)

_INTERACTIVE_RESPONSE_METHODS = frozenset(
    {
        "approval.respond",
        "clarify.respond",
        "secret.respond",
        "sudo.respond",
    }
)

_CONTROL_PLANE_INTERACTIVE_METHODS = frozenset(
    {
        "approval.pending.list",
        "approval.policy.get",
        "approval.policy.set",
    }
)

_CRON_RUNTIME_ACTIONS = frozenset({"add", "update", "remove", "run", "pause", "resume"})
_CRON_CONTROL_PLANE_ACTIONS = frozenset({"", "list", "status", "runs"})


@dataclass(frozen=True)
class RuntimeScope:
    agent_profile_id: str = ""
    runtime_scope_key: str = ""
    hermes_home: str = ""

    @property
    def has_scope(self) -> bool:
        return bool(self.agent_profile_id or self.runtime_scope_key)


def runtime_scope_from_params(params: dict[str, Any]) -> RuntimeScope:
    profile = params.get("dovie_profile")
    if not isinstance(profile, dict):
        profile = {}
    profile_id = str(
        params.get("agentProfileId")
        or params.get("agent_profile_id")
        or profile.get("id")
        or profile.get("agent_profile_id")
        or ""
    ).strip()
    scope_key = str(
        params.get("runtimeScopeKey")
        or params.get("runtime_scope_key")
        or profile.get("runtimeScopeKey")
        or profile.get("runtime_scope_key")
        or ""
    ).strip()
    if not scope_key and profile_id:
        scope_key = f"profile:{profile_id}"
    hermes_home = str(
        profile.get("hermesHomePath")
        or profile.get("hermes_home_path")
        or params.get("hermesHomePath")
        or params.get("hermes_home_path")
        or ""
    ).strip()
    return RuntimeScope(
        agent_profile_id=profile_id,
        runtime_scope_key=scope_key,
        hermes_home=hermes_home,
    )


def _request_params(req: Any) -> dict[str, Any]:
    if not isinstance(req, dict):
        return {}
    params = req.get("params")
    return params if isinstance(params, dict) else {}


def runtime_scope_from_request(req: Any) -> RuntimeScope:
    return runtime_scope_from_params(_request_params(req))


def _request_method(req: Any) -> str:
    if not isinstance(req, dict):
        return ""
    return str(req.get("method") or "").strip()


def _is_local_clarify_response(params: dict[str, Any]) -> bool:
    request_id = str(params.get("request_id") or params.get("requestId") or "").strip()
    if not request_id:
        return False
    try:
        from tools import clarify_gateway

        with clarify_gateway._lock:
            return request_id in clarify_gateway._entries
    except Exception:
        _log.debug("failed to inspect local clarify pending registry", exc_info=True)
        return False


def should_proxy_to_runtime(req: Any, *, resolve_team_context: bool = True) -> bool:
    """Return whether a JSON-RPC request targets worker-owned runtime state.

    This is deliberately a pure routing predicate. It preserves the old public
    contract for callers/tests that need to classify requests, while actual
    worker execution stays in ``worker_runtime.primary_dispatch``.
    """
    method = _request_method(req)
    if not method:
        return False
    params = _request_params(req)
    scope = runtime_scope_from_request(req)

    if method in _CONTROL_PLANE_METHODS or method in _CONTROL_PLANE_INTERACTIVE_METHODS:
        return False

    if method == "cron.manage":
        action = str(params.get("action") or "").strip().lower()
        control_plane_only = bool(params.get("controlPlaneOnly") or params.get("control_plane_only"))
        if control_plane_only and action in _CRON_CONTROL_PLANE_ACTIONS:
            return False
        return scope.has_scope and (action in _CRON_RUNTIME_ACTIONS or not control_plane_only)

    if method == "clarify.respond" and _is_local_clarify_response(params):
        return False

    if method in _INTERACTIVE_RESPONSE_METHODS:
        return scope.has_scope

    if method in _RUNTIME_AGENT_METHODS:
        return scope.has_scope

    if method in _RUNTIME_STATE_READ_METHODS:
        return scope.has_scope

    if method in _RUNTIME_MUTATION_METHODS:
        if not resolve_team_context:
            return False
        return scope.has_scope

    if method in _RUNTIME_REGISTRY_METHODS:
        return scope.has_scope

    return False
