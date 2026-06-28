"""Dovie-facing Hermes Gateway capability contract."""

from __future__ import annotations

import importlib.util
import inspect
from typing import Any

CONTRACT_VERSION = "2026-06-15"
EXTENSION_VERSION = "2026-06-15"

REQUIRED_METHODS = [
    "gateway.capabilities",
    "session.create",
    "session.list",
    "session.messages",
    "session.message_metadata.merge",
    "session.branch",
    "session.status",
    "session.usage",
    "activity.list",
    "activity.get",
    "activity.cancel",
    "activity.mark_read",
    # ADR-0001 §Phase 1.B Activity Command bus entrypoints + §Phase 2.A/2.E-1
    # activity-scoped subscription and maintenance RPCs. Replace the legacy
    # team_mission.subscribe (removed §Phase 2.E-5).
    "activity.create",
    "activity.start",
    "activity.complete",
    "activity.command.cancel",
    "runtime.activity.subscribe",
    "runtime.activity.unsubscribe",
    "runtime.activity.maintenance",
    "conversation.activity.list",
    "conversation.render_snapshot",
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
    "team_mission.create",
    "team_registry.team.upsert",
    "team_registry.team.get",
    "team_registry.team.list",
    "team_registry.team.archive",
    "team_registry.member.upsert",
    "team_registry.member.get",
    "team_registry.member.list",
    "team_registry.member.delete",
    "profile.upsert",
    "profile.get",
    "profile.growth.summary",
    "profile.list",
    "profile.archive",
    "profile.draft.upsert",
    "profile.draft.get",
    "profile.draft.list",
    "profile.draft.discard",
    "team_mission.graph",
    "team_mission.graph.reduce",
    "team_mission.events",
    "team_capability.snapshot.get",
    "team_capability.snapshot.refresh",
    "team_capability.snapshot.bind",
    "team_mission.team_profile.get",
    "team_mission.conversation.ensure",
    "team_mission.conversation.resolve",
    "team_mission.conversation.render",
    "team_mission.conversation.list",
    "team_mission.conversation.participants",
    "team_mission.conversation.runtime_session_ids",
    "team_mission.conversation.rename",
    "team_mission.conversation.delete",
    "team_mission.message.submit",
    "team_mission.cancel",
    "team_mission.node.create",
    "team_mission.edge.create",
    "team_mission.node.update",
    "team_mission.node.bind_run",
    "team_mission.node.history",
    "team_mission.node.start",
    "team_mission.plan.complete",
    "team_mission.plan.approve",
    "team_mission.schedule.ready",
    "team_mission.memory.compile",
    "team_mission.memory.pack",
    "team_mission.memory.slice",
    "team_mission.memory.list",
    "team_mission.memory.update",
    "team_mission.memory.delete",
    "team_mission.memory.events",
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
    "profile.runtime.session_exists",
    "runtime.ensure",
    "runtime.status",
    "runtime.state.inspect",
    "runtime.state.prune_empty",
    "runtime.state.merge_profile_runtime",
    "storage.stats",
    "team_mission.workspace.rebase_paths",
    "workspace.current",
    "workspace.session.current",
    "workspace.session.bind",
    "workspace.session.list",
    "workspace.session.delete",
    "workspace.list",
    "artifacts.list",
    "artifacts.register",
    "artifacts.delete",
]

REQUIRED_EVENTS = [
    "message.delta",
    "message.complete",
    "reasoning.delta",
    "reasoning.available",
    "tool.generating",
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
    "state:agent_profile_registry",
    "state:team_mission_graph",
    "state:team_registry",
    "state:team_mission_conversation",
    "state:team_mission_memory",
    "state:team_capability_snapshot",
    "state:message_reasoning",
    "state:session_search",
]

REQUIRED_RUNTIME_FEATURES = [
    "runtime:control_plane",
    "runtime:dovie_sidecar",
    "runtime:profile_scope",
    "runtime:cloud_proxy_auth",
]

METHOD_MODULES = {
    "gateway.capabilities": "tui_gateway.methods.system",
    "session.create": "tui_gateway.methods.session",
    "session.list": "tui_gateway.methods.session",
    "session.messages": "tui_gateway.methods.session",
    "session.message_metadata.merge": "tui_gateway.methods.session",
    "session.branch": "tui_gateway.methods.session_branch",
    "session.status": "tui_gateway.methods.session",
    "session.usage": "tui_gateway.methods.session",
    "activity.list": "tui_gateway.methods.activity",
    "activity.get": "tui_gateway.methods.activity",
    "activity.cancel": "tui_gateway.methods.activity",
    "activity.mark_read": "tui_gateway.methods.activity",
    "activity.create": "tui_gateway.methods.activity",
    "activity.start": "tui_gateway.methods.activity",
    "activity.complete": "tui_gateway.methods.activity",
    "activity.command.cancel": "tui_gateway.methods.activity",
    "runtime.activity.subscribe": "tui_gateway.methods.activity",
    "runtime.activity.unsubscribe": "tui_gateway.methods.activity",
    "runtime.activity.maintenance": "tui_gateway.methods.activity",
    "runtime.cloud_proxy.update": "tui_gateway.methods.runtime_cloud_proxy",
    "conversation.activity.list": "tui_gateway.methods.conversation_activity",
    "conversation.render_snapshot": "tui_gateway.methods.conversation_render_snapshot",
    "session.delete": "tui_gateway.methods.session",
    "session.title": "tui_gateway.methods.session",
    "run.reserve": "tui_gateway.methods.run",
    "run.submit": "tui_gateway.methods.run",
    "run.cancel": "tui_gateway.methods.run",
    "run.status": "tui_gateway.methods.run",
    "run.events": "tui_gateway.methods.run",
    "events.subscribe": "tui_gateway.methods.run",
    "events.unsubscribe": "tui_gateway.methods.run",
    "team_mission.create": "hermes_team_mission.gateway.conversation_methods",
    "team_registry.team.upsert": "tui_gateway.methods.team_registry",
    "team_registry.team.get": "tui_gateway.methods.team_registry",
    "team_registry.team.list": "tui_gateway.methods.team_registry",
    "team_registry.team.archive": "tui_gateway.methods.team_registry",
    "team_registry.member.upsert": "tui_gateway.methods.team_registry",
    "team_registry.member.get": "tui_gateway.methods.team_registry",
    "team_registry.member.list": "tui_gateway.methods.team_registry",
    "team_registry.member.delete": "tui_gateway.methods.team_registry",
    "profile.upsert": "tui_gateway.methods.profile_registry",
    "profile.get": "tui_gateway.methods.profile_registry",
    "profile.growth.summary": "tui_gateway.methods.profile_registry",
    "profile.list": "tui_gateway.methods.profile_registry",
    "profile.archive": "tui_gateway.methods.profile_registry",
    "profile.draft.upsert": "tui_gateway.methods.profile_registry",
    "profile.draft.get": "tui_gateway.methods.profile_registry",
    "profile.draft.list": "tui_gateway.methods.profile_registry",
    "profile.draft.discard": "tui_gateway.methods.profile_registry",
    "team_mission.graph": "hermes_team_mission.gateway.runtime_methods",
    "team_mission.graph.reduce": "hermes_team_mission.gateway.runtime_methods",
    "team_mission.events": "hermes_team_mission.gateway.runtime_methods",
    "team_capability.snapshot.get": "hermes_team_mission.gateway.conversation_methods",
    "team_capability.snapshot.refresh": "hermes_team_mission.gateway.conversation_methods",
    "team_capability.snapshot.bind": "hermes_team_mission.gateway.conversation_methods",
    "team_mission.team_profile.get": "hermes_team_mission.gateway.conversation_methods",
    "team_mission.conversation.ensure": "hermes_team_mission.gateway.conversation_methods",
    "team_mission.conversation.resolve": "hermes_team_mission.gateway.conversation_methods",
    "team_mission.conversation.render": "tui_gateway.methods.conversation_render_snapshot",
    "team_mission.conversation.list": "hermes_team_mission.gateway.conversation_methods",
    "team_mission.conversation.participants": "hermes_team_mission.gateway.conversation_methods",
    "team_mission.conversation.runtime_session_ids": "hermes_team_mission.gateway.conversation_methods",
    "team_mission.conversation.rename": "hermes_team_mission.gateway.conversation_methods",
    "team_mission.conversation.delete": "hermes_team_mission.gateway.conversation_methods",
    "team_mission.message.submit": "hermes_team_mission.gateway.runtime_methods",
    "team_mission.cancel": "hermes_team_mission.gateway.runtime_methods",
    "team_mission.node.create": "hermes_team_mission.gateway.runtime_methods",
    "team_mission.edge.create": "hermes_team_mission.gateway.runtime_methods",
    "team_mission.node.update": "hermes_team_mission.gateway.runtime_methods",
    "team_mission.node.bind_run": "hermes_team_mission.gateway.runtime_methods",
    "team_mission.node.history": "hermes_team_mission.gateway.history_methods",
    "team_mission.node.start": "hermes_team_mission.gateway.runtime_methods",
    "team_mission.plan.complete": "hermes_team_mission.gateway.runtime_methods",
    "team_mission.plan.approve": "hermes_team_mission.gateway.runtime_methods",
    "team_mission.schedule.ready": "hermes_team_mission.gateway.runtime_methods",
    "team_mission.memory.compile": "hermes_team_mission.gateway.memory_methods",
    "team_mission.memory.pack": "hermes_team_mission.gateway.memory_methods",
    "team_mission.memory.slice": "hermes_team_mission.gateway.memory_methods",
    "team_mission.memory.list": "hermes_team_mission.gateway.memory_methods",
    "team_mission.memory.update": "hermes_team_mission.gateway.memory_methods",
    "team_mission.memory.delete": "hermes_team_mission.gateway.memory_methods",
    "team_mission.memory.events": "hermes_team_mission.gateway.memory_methods",
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
    "profile.runtime.session_exists": "tui_gateway.methods.system",
    "runtime.ensure": "tui_gateway.methods.system",
    "runtime.status": "tui_gateway.methods.system",
    "runtime.state.inspect": "tui_gateway.methods.system",
    "runtime.state.prune_empty": "tui_gateway.methods.system",
    "runtime.state.merge_profile_runtime": "tui_gateway.methods.system",
    "storage.stats": "tui_gateway.methods.system",
    "team_mission.workspace.rebase_paths": "tui_gateway.methods.system",
    "workspace.current": "tui_gateway.methods.workspace_artifacts",
    "workspace.session.current": "tui_gateway.methods.workspace_artifacts",
    "workspace.session.bind": "tui_gateway.methods.workspace_artifacts",
    "workspace.session.list": "tui_gateway.methods.workspace_artifacts",
    "workspace.session.delete": "tui_gateway.methods.workspace_artifacts",
    "workspace.list": "tui_gateway.methods.workspace_artifacts",
    "artifacts.list": "tui_gateway.methods.workspace_artifacts",
    "artifacts.register": "tui_gateway.methods.workspace_artifacts",
    "artifacts.delete": "tui_gateway.methods.workspace_artifacts",
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
            "upsert_agent_team",
            "get_agent_team_with_members",
            "upsert_agent_team_member",
            "delete_agent_team_member",
        )
    ) and _session_db_schema_contains("agent_teams", "agent_team_members"):
        present.add("state:team_registry")
    if all(
        _session_db_method(name)
        for name in (
            "upsert_agent_profile",
            "get_agent_profile",
            "upsert_agent_profile_draft",
            "discard_agent_profile_draft",
        )
    ) and _session_db_schema_contains("agent_profiles", "agent_profile_drafts"):
        present.add("state:agent_profile_registry")
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
    if all(
        _session_db_method(name)
        for name in (
            "initialize_team_mission_from_strategy",
            "get_team_mission_graph",
            "cancel_team_mission",
            "list_team_mission_events",
        )
    ):
        present.add("state:team_mission_graph")
    if all(
        _session_db_method(name)
        for name in (
            "ensure_team_mission_conversation",
            "get_team_mission_conversation",
            "resolve_team_mission_conversation",
            "list_team_mission_conversations",
        )
    ) and _session_db_schema_contains("team_mission_conversations"):
        present.add("state:team_mission_conversation")
    if all(
        _session_db_method(name)
        for name in (
            "compile_team_mission_memory",
            "build_team_mission_memory_pack",
            "build_team_mission_memory_slice",
            "list_team_mission_memory_items",
        )
    ) and _session_db_schema_contains("team_mission_memory_items", "team_mission_memory_edges"):
        present.add("state:team_mission_memory")
    if all(
        _session_db_method(name)
        for name in (
            "resolve_team_capability_snapshot",
            "get_team_capability_snapshot",
            "bind_team_capability_snapshot",
        )
    ) and _session_db_schema_contains("team_capability_snapshots", "team_capability_snapshot_bindings"):
        present.add("state:team_capability_snapshot")
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
    if _module_exists("tui_gateway.dovie_sidecar"):
        present.add("runtime:dovie_sidecar")
        present.add("runtime:control_plane")
    if _module_exists("tui_gateway.services.runtime_pool"):
        present.add("runtime:profile_scope")
    if "runtime:dovie_sidecar" in present:
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
