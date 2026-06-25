"""Runtime scope helpers.

Phase 6 slimmed this module down: the legacy sub-sidecar proxy stack
(``RuntimeWorkerPool``, ``RuntimeProxyBridge``, ``proxy_to_runtime``,
``should_proxy_to_runtime``, the ``_RUNTIME_PROXY_CONTROL_METHODS`` /
``_RUNTIME_SCOPED_CONTROL_METHODS`` gates, the per-profile ws-server
spawn) is gone. The main sidecar now hosts every method in-process,
with ``enter_profile_context`` switching the ``_active_hermes_home``
ContextVar at ``handle_request`` dispatch entry so ``_get_db()``
resolves the right per-profile state.db. Methods that actually run
the agent (``run.submit`` / ``prompt.submit``) are routed through
``WorkerSupervisor`` via ``worker_runtime.primary_dispatch`` from
``ws.py``.

What remains here:
- ``RuntimeScope`` — the typed (agent_profile_id, runtime_scope_key,
  hermes_home) triple used pervasively to identify per-profile scope.
- ``runtime_scope_from_params`` / ``runtime_scope_from_request`` —
  parse a scope from a JSON-RPC request's ``params`` (handling the
  snake/camel and ``dovie_profile`` envelope variations the desktop
  client emits).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

_log = logging.getLogger(__name__)


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
