"""Runtime scope and worker routing helpers.

The main sidecar owns control-plane JSON-RPC methods in-process. Methods that
execute agent work (``run.submit`` / ``prompt.submit``) are routed through
``WorkerSupervisor`` via ``worker_runtime.primary_dispatch``; the worker
subprocess accesses storage through ``WorkerDBProxy`` IPC.

This module owns the typed runtime scope contract and the pure routing
predicate used before dispatch.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Tuple

from hermes_constants import get_hermes_home
from hermes_profile_dir import resolve_default_agent_dir

_log = logging.getLogger(__name__)


_CONTROL_PLANE_METHODS = frozenset(
    {
        "artifacts.list",
        "conversation.activity.list",
        "conversation.render_snapshot",
        "capability.operation.get",
        "capability.operation.start",
        "dovie.capabilities.reconcile",
        "events.compact",
        "events.prune",
        "events.subscribe",
        "events.unsubscribe",
        "mcp.manage",
        "mcp.servers.list",
        "profile.archive",
        "profile.draft.discard",
        "profile.draft.get",
        "profile.draft.list",
        "profile.draft.upsert",
        "profile.get",
        "profile.growth.summary",
        "profile.learning.graph",
        "profile.learning.node.delete",
        "profile.learning.node.detail",
        "profile.learning.node.edit",
        "learning.frames",
        "learning.detail",
        "learning.delete",
        "learning.edit",
        "pet.cells",
        "pet.gallery",
        "pet.select",
        "pet.remove",
        "pet.disable",
        "pet.scale",
        "profile.list",
        "profile.upsert",
        "presentation.slide.regenerate",
        "run.events",
        "run.retry.prepare",
        "runtime.ensure",
        "session.list",
        "session.messages",
        "session.message_metadata.merge",
        "session.status",
        "session.title",
        "project.facts",
        "verification.status",
        "team_mission.conversation.delete",
        "team_mission.conversation.ensure",
        "team_mission.conversation.list",
        "team_mission.conversation.participants",
        "team_mission.conversation.rename",
        "team_mission.conversation.render",
        "team_mission.conversation.resolve",
        "team_mission.conversation.execution_session_ids",
        "team_mission.create",
        "team_mission.events",
        "team_mission.graph",
        "team_mission.graph.reduce",
        "team_mission.snapshot.get",
        "team_mission.result.get",
        "team_mission.message.submit",
        "team_mission.node.history",
        "terminal.session.close",
        "terminal.session.list",
        "terminal.session.open",
        "terminal.session.resize",
        "terminal.session.write",
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
        "reload.mcp",
        "skills.reload",
        "toolsets.list",
        "tools.configure",
        "write_approval.approve",
        "write_approval.configure",
        "write_approval.detail",
        "write_approval.list",
        "write_approval.reject",
        "write_approval.status",
    }
)

_INTERACTIVE_RESPONSE_METHODS = frozenset(
    {
        "approval.respond",
        "clarify.respond",
        "secret.respond",
        "sudo.respond",
        "terminal.list.respond",
        "terminal.read.respond",
        "terminal.write.respond",
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
    conversation_id: str = ""
    hermes_home: str = ""

    @property
    def has_scope(self) -> bool:
        return bool(self.agent_profile_id or self.runtime_scope_key)

    @property
    def worker_identity(self) -> Tuple[str, str]:
        return (self.runtime_scope_key, self.conversation_id)


def _default_agent_home_for_scope(profile_id: str, scope_key: str) -> str:
    if not profile_id and not scope_key:
        return ""
    if profile_id not in {"", "agent-default", "default"}:
        return ""
    if scope_key not in {"", "profile:agent-default", "profile:default"}:
        return ""
    try:
        return str(resolve_default_agent_dir(Path(get_hermes_home())))
    except Exception:
        return ""


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
    explicit_scope_key = str(
        params.get("runtimeScopeKey")
        or params.get("runtime_scope_key")
        or profile.get("runtimeScopeKey")
        or profile.get("runtime_scope_key")
        or ""
    ).strip()
    if explicit_scope_key.startswith(("profile:", "team:", "draft:", "member-chat:")):
        scope_key = explicit_scope_key
    elif profile_id:
        scope_key = f"profile:{profile_id}"
    else:
        scope_key = explicit_scope_key
    conversation_id = str(
        params.get("conversation_id")
        or params.get("conversationId")
        or params.get("conversation_session_id")
        or params.get("conversationSessionId")
        or params.get("session_id")
        or params.get("sessionId")
        or ""
    ).strip()
    hermes_home = str(
        profile.get("hermesHomePath")
        or profile.get("hermes_home_path")
        or params.get("hermesHomePath")
        or params.get("hermes_home_path")
        or ""
    ).strip()
    if not hermes_home:
        hermes_home = _default_agent_home_for_scope(profile_id, scope_key)
    return RuntimeScope(
        agent_profile_id=profile_id,
        runtime_scope_key=scope_key,
        conversation_id=conversation_id,
        hermes_home=hermes_home,
    )


def _request_params(req: Any) -> dict[str, Any]:
    if not isinstance(req, dict):
        return {}
    params = req.get("params")
    return params if isinstance(params, dict) else {}


def runtime_scope_from_request(req: Any) -> RuntimeScope:
    if not isinstance(req, dict):
        return RuntimeScope()
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

        return clarify_gateway.has_pending_clarify(request_id)
    except Exception:
        _log.debug("failed to inspect local clarify pending registry", exc_info=True)
        return False


def should_route_to_worker(req: Any, *, resolve_team_context: bool = True) -> bool:
    """Return whether a JSON-RPC request targets worker-owned runtime state.

    This is deliberately a pure routing predicate. Actual worker execution
    stays in ``worker_runtime.primary_dispatch``.
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
