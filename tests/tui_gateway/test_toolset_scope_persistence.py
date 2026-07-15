from types import SimpleNamespace

import model_tools

from tui_gateway.services import toolset_scope
from tui_gateway.services.persistence.gateway_store import GatewayStateStore


def test_session_turn_merge_toolsets_are_persisted_for_runtime_rebuild(monkeypatch):
    persisted = []
    monkeypatch.setattr(
        toolset_scope,
        "persist_session_toolset_overrides",
        lambda session_id, **kwargs: persisted.append((session_id, kwargs)),
    )

    session = {}

    toolset_scope.ensure_session_turn_toolsets(
        sid="sid-design",
        session=session,
        requested_toolsets=["dovie"],
        load_enabled_toolsets=lambda: ["memory"],
        emit_session_info=lambda *_args: None,
        persist_session_id="stored-design",
    )

    assert persisted == [
        (
            "stored-design",
            {
                "enabled_toolsets": ["memory", "dovie"],
                "disabled_toolsets": None,
            },
        )
    ]


def test_session_turn_exact_toolsets_are_not_persisted(monkeypatch):
    persisted = []
    monkeypatch.setattr(
        toolset_scope,
        "persist_session_toolset_overrides",
        lambda session_id, **kwargs: persisted.append((session_id, kwargs)),
    )

    session = {}

    toolset_scope.ensure_session_turn_toolsets(
        sid="sid-team-leader",
        session=session,
        requested_toolsets=["team_mission_planning"],
        requested_disabled_toolsets=["delegation"],
        toolset_scope="exact",
        load_enabled_toolsets=lambda: ["web", "memory", "delegation"],
        load_disabled_toolsets=lambda: None,
        emit_session_info=lambda *_args: None,
        persist_session_id="stored-team",
    )

    assert persisted == []


def test_refresh_agent_tool_filter_invalidates_prompt_when_tool_surface_changes(
    monkeypatch,
):
    monkeypatch.setattr(
        model_tools,
        "get_tool_definitions",
        lambda **_kwargs: [
            {"type": "function", "function": {"name": "next_tool"}}
        ],
    )
    agent = SimpleNamespace(
        enabled_toolsets=["previous"],
        disabled_toolsets=None,
        tools=[{"type": "function", "function": {"name": "previous_tool"}}],
        valid_tool_names={"previous_tool"},
        quiet_mode=True,
        _cached_system_prompt="PROMPT_WITH_PREVIOUS_TOOL_GUIDANCE",
    )

    toolset_scope.refresh_agent_tool_filter(agent, ["next"])

    assert agent.valid_tool_names == {"next_tool"}
    assert agent._cached_system_prompt is None


def test_refresh_agent_tool_filter_preserves_prompt_when_effective_tools_do_not_change(
    monkeypatch,
):
    tool_definition = {
        "type": "function",
        "function": {"name": "stable_tool"},
    }
    monkeypatch.setattr(
        model_tools,
        "get_tool_definitions",
        lambda **_kwargs: [tool_definition],
    )
    agent = SimpleNamespace(
        enabled_toolsets=["previous-alias"],
        disabled_toolsets=None,
        tools=[tool_definition],
        valid_tool_names={"stable_tool"},
        quiet_mode=True,
        _cached_system_prompt="STILL_VALID_PROMPT",
    )

    toolset_scope.refresh_agent_tool_filter(agent, ["next-alias"])

    assert agent.valid_tool_names == {"stable_tool"}
    assert agent._cached_system_prompt == "STILL_VALID_PROMPT"


def test_gateway_state_store_round_trips_session_toolset_overrides(tmp_path):
    store = GatewayStateStore(tmp_path / "gateway-state.db")

    store.upsert_session_toolsets(
        session_id="stored-design",
        enabled_toolsets=["memory", "dovie"],
        disabled_toolsets=["delegation"],
    )

    payload = store.get_session_toolsets("stored-design")

    assert payload["session_id"] == "stored-design"
    assert payload["enabled_toolsets"] == ["memory", "dovie"]
    assert payload["disabled_toolsets"] == ["delegation"]
    assert isinstance(payload["created_at"], float)
    assert isinstance(payload["updated_at"], float)


def test_gateway_state_store_round_trips_session_workspace_bindings(tmp_path):
    workspace_path = tmp_path / "workspace"
    workspace_path.mkdir()
    store = GatewayStateStore(tmp_path / "gateway-state.db")

    persisted_workspace = store.upsert_workspace({
        "id": "workspace-1",
        "name": "Workspace One",
        "path": str(workspace_path),
        "kind": "local",
    })
    persisted_binding = store.bind_session_workspace(
        session_id="stored-session-1",
        workspace_id=persisted_workspace["id"],
        cwd=str(workspace_path),
        metadata={
            "agentProfileId": "agent-profile-1",
            "agentProfileVersionId": "version-current",
            "title": "Session title",
        },
    )

    assert persisted_binding["session_id"] == "stored-session-1"
    assert persisted_binding["id"] == "workspace-1"
    assert persisted_binding["metadata"]["agentProfileId"] == "agent-profile-1"

    loaded = store.get_session_workspace("stored-session-1")
    assert loaded["session_id"] == "stored-session-1"
    assert loaded["id"] == "workspace-1"
    assert loaded["path"] == str(workspace_path)
    assert loaded["metadata"]["agentProfileVersionId"] == "version-current"
    assert isinstance(loaded["binding_created_at"], float)
    assert isinstance(loaded["binding_updated_at"], float)

    listed = store.list_session_workspaces()
    assert [binding["session_id"] for binding in listed] == ["stored-session-1"]

    removed = store.delete_session_workspaces(["stored-session-1"])
    assert removed[0]["metadata"]["title"] == "Session title"
    assert store.get_session_workspace("stored-session-1") is None


def test_resolve_session_toolsets_uses_persisted_overrides(monkeypatch):
    monkeypatch.setattr(
        toolset_scope,
        "load_session_toolset_overrides",
        lambda session_id: {
            "enabled_toolsets": ["memory", "dovie"],
            "disabled_toolsets": ["delegation"],
        },
    )

    enabled, disabled = toolset_scope.resolve_session_toolsets(
        session=None,
        session_id="stored-design",
        load_enabled_toolsets=lambda: ["memory"],
        load_disabled_toolsets=lambda: None,
    )

    assert enabled == ["memory", "dovie"]
    assert disabled == ["delegation"]
