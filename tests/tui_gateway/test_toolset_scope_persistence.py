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
