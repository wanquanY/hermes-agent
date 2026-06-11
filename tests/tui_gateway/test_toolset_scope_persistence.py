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
        requested_toolsets=["doxie"],
        load_enabled_toolsets=lambda: ["memory"],
        emit_session_info=lambda *_args: None,
        persist_session_id="stored-design",
    )

    assert persisted == [
        (
            "stored-design",
            {
                "enabled_toolsets": ["memory", "doxie"],
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
        enabled_toolsets=["memory", "doxie"],
        disabled_toolsets=["delegation"],
    )

    payload = store.get_session_toolsets("stored-design")

    assert payload["session_id"] == "stored-design"
    assert payload["enabled_toolsets"] == ["memory", "doxie"]
    assert payload["disabled_toolsets"] == ["delegation"]
    assert isinstance(payload["created_at"], float)
    assert isinstance(payload["updated_at"], float)


def test_resolve_session_toolsets_uses_persisted_overrides(monkeypatch):
    monkeypatch.setattr(
        toolset_scope,
        "load_session_toolset_overrides",
        lambda session_id: {
            "enabled_toolsets": ["memory", "doxie"],
            "disabled_toolsets": ["delegation"],
        },
    )

    enabled, disabled = toolset_scope.resolve_session_toolsets(
        session=None,
        session_id="stored-design",
        load_enabled_toolsets=lambda: ["memory"],
        load_disabled_toolsets=lambda: None,
    )

    assert enabled == ["memory", "doxie"]
    assert disabled == ["delegation"]
