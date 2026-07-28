from __future__ import annotations

import json

import pytest

from hermes_cli.model_connections import ModelConnectionRepository
from hermes_cli.model_routes import ModelRouteError
from tui_gateway import server
from tui_gateway.services.model_route_runtime import (
    scoped_selections_from_state,
    selection_from_state,
)


class _Sessions:
    def __init__(self):
        self.rows = {
            "conversation-1": {
                "id": "conversation-1",
                "model": "dovie/default",
                "model_config": json.dumps({
                    "model": "dovie/default",
                    "provider": "dovie-cloud",
                    "connection_id": "cloud:dovie",
                    "model_selection": {
                        "schema_version": 1,
                        "expected_route_kind": "dovie_cloud",
                        "connection_id": "cloud:dovie",
                        "provider_id": "dovie-cloud",
                        "model_id": "dovie/default",
                        "connection_revision": 0,
                    },
                    "model_selection_revision": 1,
                }),
            }
        }

    def get(self, session_id):
        return self.rows.get(session_id)

    def update_runtime_config(self, session_id, config, *, model=None):
        row = self.rows[session_id]
        row["model_config"] = json.dumps(config)
        if model:
            row["model"] = model
        return True


class _DB:
    def __init__(self):
        self.sessions = _Sessions()


def _managed_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("DOVIE_LOCAL_OWNER_ID", "owner_12345678")
    monkeypatch.setenv("DOVIE_HERMES_CONTROL_HOME", str(tmp_path))


def test_connection_get_can_treat_absence_as_expected(monkeypatch, tmp_path):
    _managed_environment(monkeypatch, tmp_path)

    response = server.handle_request({
        "id": "connection-optional-get",
        "method": "model.connection.get",
        "params": {
            "connection_id": "builtin:kimi-coding",
            "allow_missing": True,
        },
    })

    assert response["result"]["connection"] is None


def test_picker_visibility_rpc_updates_catalog_preference(monkeypatch, tmp_path):
    _managed_environment(monkeypatch, tmp_path)
    repository = ModelConnectionRepository(
        control_home=tmp_path,
        owner_id="owner_12345678",
    )
    connection = repository.upsert(
        {
            "connection_id": "builtin:deepseek",
            "provider_id": "deepseek",
            "name": "DeepSeek",
        },
        expected_revision=0,
        operation_id="connection-create",
    )

    response = server.handle_request({
        "id": "picker-visibility-set",
        "method": "model.picker_visibility.set",
        "params": {
            "connection_id": connection["connection_id"],
            "model_id": "deepseek-chat",
            "visible": False,
            "expected_revision": connection["revision"],
            "operation_id": "picker-hide",
        },
    })

    assert response["result"]["connection"]["model_preferences"] == {
        "deepseek-chat": {"picker_visible": False}
    }


def test_run_prepare_returns_authoritative_cloud_resolution(
    monkeypatch,
    tmp_path,
):
    _managed_environment(monkeypatch, tmp_path)
    db = _DB()
    monkeypatch.setattr(server, "_get_db", lambda: db)

    response = server.handle_request({
        "id": "prepare-1",
        "method": "run.prepare",
        "params": {
            "conversation_session_id": "conversation-1",
            "expected_session_revision": 1,
        },
    })

    assert response["result"]["route"]["route_kind"] == "dovie_cloud"
    assert response["result"]["resolution_id"].startswith("route_")
    assert response["result"]["session_revision"] == 1


def test_structured_model_set_persists_direct_route(monkeypatch, tmp_path):
    _managed_environment(monkeypatch, tmp_path)
    db = _DB()
    monkeypatch.setattr(server, "_get_db", lambda: db)

    response = server.handle_request({
        "id": "set-1",
        "method": "model.set",
        "params": {
            "session_id": "conversation-1",
            "conversation_session_id": "conversation-1",
            "selection": {
                "schema_version": 1,
                "expected_route_kind": "hermes_direct",
                "connection_id": "builtin:deepseek",
                "provider_id": "deepseek",
                "model_id": "deepseek-chat",
                "connection_revision": 0,
            },
        },
    })

    assert response["result"]["route"]["route_kind"] == "hermes_direct"
    persisted = json.loads(db.sessions.rows["conversation-1"]["model_config"])
    assert persisted["connection_id"] == "builtin:deepseek"
    assert persisted["model_selection_revision"] == 2


def test_legacy_session_infers_provider_from_profile_config(monkeypatch):
    db = _DB()
    db.sessions.rows["conversation-1"]["model_config"] = json.dumps({})
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {
            "model": {
                "default": "dovie/default",
                "provider": "dovie-cloud",
            }
        },
    )

    selection, revision = selection_from_state(
        conversation_session_id="conversation-1",
        live_session=None,
        db=db,
    )

    assert selection["expected_route_kind"] == "dovie_cloud"
    assert selection["provider_id"] == "dovie-cloud"
    assert selection["model_id"] == "dovie/default"
    assert revision == 0


def test_team_member_selection_is_scoped_without_replacing_leader(
    monkeypatch, tmp_path
):
    _managed_environment(monkeypatch, tmp_path)
    db = _DB()
    monkeypatch.setattr(server, "_get_db", lambda: db)
    member_selection = {
        "schema_version": 1,
        "expected_route_kind": "hermes_direct",
        "connection_id": "builtin:deepseek",
        "provider_id": "deepseek",
        "model_id": "deepseek-chat",
        "connection_revision": 0,
    }

    set_response = server.handle_request({
        "id": "member-set",
        "method": "model.set",
        "params": {
            "session_id": "conversation-1",
            "conversation_session_id": "conversation-1",
            "selection": member_selection,
            "execution_target": {
                "kind": "team_member",
                "target_member_id": "member-1",
            },
        },
    })
    prepared = server.handle_request({
        "id": "member-prepare",
        "method": "run.prepare",
        "params": {
            "conversation_session_id": "conversation-1",
            "execution_target": {
                "kind": "team_member",
                "target_member_id": "member-1",
            },
        },
    })

    assert set_response["result"]["route"]["route_kind"] == "hermes_direct"
    assert prepared["result"]["route"]["model_id"] == "deepseek-chat"
    persisted = json.loads(db.sessions.rows["conversation-1"]["model_config"])
    assert persisted["model_selection"]["model_id"] == "dovie/default"
    assert (
        persisted["model_selections"]["team_member:member-1"]["selection"]["model_id"]
        == "deepseek-chat"
    )

    projected = scoped_selections_from_state(
        conversation_session_id="conversation-1",
        live_session=None,
        db=db,
    )
    assert projected == {
        "team_member:member-1": {
            "selection": member_selection,
            "revision": 1,
        }
    }
    assert "route_snapshot" not in projected["team_member:member-1"]


def test_team_member_route_never_falls_back_to_leader_selection():
    db = _DB()

    with pytest.raises(ModelRouteError) as error:
        selection_from_state(
            conversation_session_id="conversation-1",
            live_session=None,
            db=db,
            execution_target={
                "kind": "team_member",
                "target_member_id": "member-missing",
            },
        )

    assert error.value.code == "MODEL_SELECTION_NOT_CONFIGURED"


def test_profile_default_rpc_persists_authoritative_selection(monkeypatch, tmp_path):
    _managed_environment(monkeypatch, tmp_path)
    selection = {
        "schema_version": 1,
        "expected_route_kind": "dovie_cloud",
        "connection_id": "cloud:dovie",
        "provider_id": "dovie-cloud",
        "model_id": "dovie/default",
        "connection_revision": 0,
    }

    saved = server.handle_request({
        "id": "profile-default-set",
        "method": "model.profile_default.set",
        "params": {
            "profile_id": "profile-1",
            "selection": selection,
            "expected_revision": 0,
            "operation_id": "profile-default-op",
        },
    })
    loaded = server.handle_request({
        "id": "profile-default-get",
        "method": "model.profile_default.get",
        "params": {"profile_id": "profile-1"},
    })

    assert saved["result"]["route"]["route_kind"] == "dovie_cloud"
    assert loaded["result"]["default"]["selection"] == selection
    assert loaded["result"]["default"]["revision"] == 1


def test_custom_connection_can_be_configured_before_adding_a_model(
    monkeypatch,
    tmp_path,
):
    _managed_environment(monkeypatch, tmp_path)
    repository = ModelConnectionRepository(
        control_home=tmp_path,
        owner_id="owner_12345678",
    )
    repository.upsert(
        {
            "connection_id": "custom:private-endpoint",
            "provider_id": "custom",
            "kind": "custom_endpoint",
            "name": "Private endpoint",
            "base_url": "https://models.example/v1",
            "api_mode": "chat_completions",
            "discover_models": True,
        },
        expected_revision=0,
        operation_id="custom-endpoint-create",
    )

    class _Profile:
        supports_health_check = True
        auth_type = "api_key"

        @staticmethod
        def fetch_models(**_kwargs):
            return None

    monkeypatch.setattr(
        "hermes_cli.runtime_provider.resolve_runtime_provider",
        lambda **_kwargs: {
            "base_url": "https://models.example/v1",
            "api_key": "no-key-required",
            "credential_generation": 0,
        },
    )
    monkeypatch.setattr(
        "providers.get_provider_profile",
        lambda _provider_id: _Profile(),
    )

    response = server.handle_request({
        "id": "validate-model-less-custom",
        "method": "model.connection.validate",
        "params": {
            "connection_id": "custom:private-endpoint",
        },
    })

    assert response["result"]["status"] == "limited"
    assert response["result"]["models"] == []
    assert response["result"]["inference_tested"] is False


def test_builtin_connection_is_limited_when_model_catalog_probe_fails(
    monkeypatch,
    tmp_path,
):
    _managed_environment(monkeypatch, tmp_path)
    repository = ModelConnectionRepository(
        control_home=tmp_path,
        owner_id="owner_12345678",
    )
    repository.upsert(
        {
            "connection_id": "builtin:kimi-coding",
            "provider_id": "kimi-coding",
            "kind": "builtin",
            "name": "Kimi Coding",
            "discover_models": True,
        },
        expected_revision=0,
        operation_id="kimi-coding-create",
    )

    class _Profile:
        supports_health_check = True
        auth_type = "api_key"

        @staticmethod
        def fetch_models(**_kwargs):
            return None

    monkeypatch.setattr(
        "hermes_cli.runtime_provider.resolve_runtime_provider",
        lambda **_kwargs: {
            "base_url": "https://api.kimi.com/coding",
            "api_key": "managed-key",
            "credential_generation": 1,
        },
    )
    monkeypatch.setattr(
        "providers.get_provider_profile",
        lambda _provider_id: _Profile(),
    )

    response = server.handle_request({
        "id": "validate-kimi-limited",
        "method": "model.connection.validate",
        "params": {
            "connection_id": "builtin:kimi-coding",
        },
    })

    assert response["result"]["status"] == "limited"
    assert response["result"]["limited"] is True
    assert response["result"]["credential_accepted"] is None
    assert response["result"]["models"] == []
    assert response["result"]["inference_tested"] is False
