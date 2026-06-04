"""Doxie-facing Hermes Gateway capability contract."""

from __future__ import annotations

import importlib.util
import inspect
from typing import Any

CONTRACT_VERSION = "2026-05-23"
EXTENSION_VERSION = "2026-05-23"

REQUIRED_METHODS = [
    "gateway.capabilities",
    "session.create",
    "session.list",
    "session.messages",
    "session.status",
    "session.usage",
    "session.delete",
    "session.title",
    "prompt.submit",
    "run.reserve",
    "run.submit",
    "run.cancel",
    "run.status",
    "run.events",
    "events.subscribe",
    "events.unsubscribe",
    "model.set",
    "model.options",
    "approval.pending.list",
    "approval.policy.get",
    "approval.policy.set",
    "approval.respond",
    "sudo.respond",
    "secret.respond",
    "clarify.respond",
    "cron.manage",
    "skills.manage",
    "toolsets.list",
    "profile.prepare_runtime",
    "runtime.ensure",
    "runtime.status",
    "workspace.current",
    "workspace.list",
    "artifacts.list",
]

REQUIRED_EVENTS = [
    "message.delta",
    "message.complete",
    "reasoning.delta",
    "reasoning.available",
    "tool.start",
    "tool.progress",
    "tool.complete",
    "approval.request",
    "sudo.request",
    "secret.request",
    "clarify.request",
    "artifact.created",
    "subagent.output_delta",
    "subagent.reasoning_delta",
    "agent_profile_test.start",
    "agent_profile_test.output_delta",
    "agent_profile_test.thinking",
    "agent_profile_test.tool",
    "agent_profile_test.progress",
    "agent_profile_test.complete",
]

REQUIRED_STATE_FEATURES = [
    "state:runtime_scope_key",
    "state:transient_session",
    "state:run_registry",
    "state:run_event_log",
    "state:message_reasoning",
    "state:session_search",
]

REQUIRED_RUNTIME_FEATURES = [
    "runtime:control_plane",
    "runtime:doxie_sidecar",
    "runtime:profile_scope",
    "runtime:cloud_proxy_auth",
]

METHOD_MODULES = {
    "gateway.capabilities": "tui_gateway.methods.system",
    "session.create": "tui_gateway.methods.session",
    "session.list": "tui_gateway.methods.session",
    "session.messages": "tui_gateway.methods.session",
    "session.status": "tui_gateway.methods.session",
    "session.usage": "tui_gateway.methods.session",
    "session.delete": "tui_gateway.methods.session",
    "session.title": "tui_gateway.methods.session",
    "run.reserve": "tui_gateway.methods.run",
    "run.submit": "tui_gateway.methods.run",
    "run.cancel": "tui_gateway.methods.run",
    "run.status": "tui_gateway.methods.run",
    "run.events": "tui_gateway.methods.run",
    "events.subscribe": "tui_gateway.methods.run",
    "events.unsubscribe": "tui_gateway.methods.run",
    "model.set": "tui_gateway.methods.model",
    "model.options": "tui_gateway.methods.model",
    "approval.pending.list": "tui_gateway.methods.prompt",
    "approval.policy.get": "tui_gateway.methods.prompt",
    "approval.policy.set": "tui_gateway.methods.prompt",
    "approval.respond": "tui_gateway.methods.prompt",
    "sudo.respond": "tui_gateway.methods.prompt",
    "secret.respond": "tui_gateway.methods.prompt",
    "clarify.respond": "tui_gateway.methods.prompt",
    "cron.manage": "tui_gateway.methods.integrations",
    "skills.manage": "tui_gateway.methods.integrations",
    "toolsets.list": "tui_gateway.methods.integrations",
    "profile.prepare_runtime": "tui_gateway.methods.system",
    "runtime.ensure": "tui_gateway.methods.system",
    "runtime.status": "tui_gateway.methods.system",
    "workspace.current": "tui_gateway.methods.workspace_artifacts",
    "workspace.list": "tui_gateway.methods.workspace_artifacts",
    "artifacts.list": "tui_gateway.methods.workspace_artifacts",
    "prompt.submit": "tui_gateway.methods.prompt",
}


def _module_exists(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except Exception:
        return False


def _session_db_class():
    try:
        from hermes_state import SessionDB
    except Exception:
        return None
    return SessionDB


def _session_db_method(name: str) -> bool:
    cls = _session_db_class()
    return cls is not None and callable(getattr(cls, name, None))


def _session_db_schema_contains(*markers: str) -> bool:
    try:
        import hermes_state
    except Exception:
        return False
    schema = str(getattr(hermes_state, "SCHEMA_SQL", ""))
    return all(marker in schema for marker in markers)


def _method_modules_present() -> set[str]:
    return {
        method_name
        for method_name, module_name in METHOD_MODULES.items()
        if _module_exists(module_name)
    }


def _state_features_present() -> set[str]:
    present = set()
    if _session_db_schema_contains("reasoning TEXT", "reasoning_content TEXT"):
        present.add("state:message_reasoning")
    if _session_db_method("search_messages") and _session_db_method("search_sessions"):
        present.add("state:session_search")
    if all(
        _session_db_method(name)
        for name in (
            "create_run_if_session_idle",
            "get_run",
            "list_runs",
        )
    ):
        present.add("state:run_registry")
        present.add("state:runtime_scope_key")
    if all(
        _session_db_method(name)
        for name in (
            "append_run_event",
            "list_run_events",
        )
    ):
        present.add("state:run_event_log")
    create_session = getattr(_session_db_class(), "create_session", None)
    if callable(create_session):
        try:
            signature = inspect.signature(create_session)
            if "transient" in signature.parameters or any(
                parameter.kind is inspect.Parameter.VAR_KEYWORD
                for parameter in signature.parameters.values()
            ):
                if _session_db_schema_contains("transient"):
                    present.add("state:transient_session")
        except Exception:
            pass
    return present


def _runtime_features_present() -> set[str]:
    present = set()
    if _module_exists("tui_gateway.doxie_sidecar"):
        present.add("runtime:doxie_sidecar")
        present.add("runtime:control_plane")
    if _module_exists("tui_gateway.services.runtime_pool"):
        present.add("runtime:profile_scope")
    if "runtime:doxie_sidecar" in present:
        present.add("runtime:cloud_proxy_auth")
    return present


def gateway_capabilities() -> dict[str, Any]:
    methods = sorted(_method_modules_present())
    state_features = sorted(_state_features_present())
    runtime_features = sorted(_runtime_features_present())

    missing_methods = sorted(set(REQUIRED_METHODS) - set(methods))
    missing_events: list[str] = []
    missing_state_features = sorted(set(REQUIRED_STATE_FEATURES) - set(state_features))
    missing_runtime_features = sorted(set(REQUIRED_RUNTIME_FEATURES) - set(runtime_features))
    missing_capabilities = sorted(
        set(missing_methods)
        | set(missing_events)
        | set(missing_state_features)
        | set(missing_runtime_features)
    )
    return {
        "protocolVersion": CONTRACT_VERSION,
        "hermesVersion": "",
        "extensionVersion": EXTENSION_VERSION,
        "methods": methods,
        "events": list(REQUIRED_EVENTS),
        "stateFeatures": state_features,
        "runtimeFeatures": runtime_features,
        "missingMethods": missing_methods,
        "missingEvents": missing_events,
        "missingStateFeatures": missing_state_features,
        "missingRuntimeFeatures": missing_runtime_features,
        "missingCapabilities": missing_capabilities,
        "ok": not missing_capabilities,
    }
