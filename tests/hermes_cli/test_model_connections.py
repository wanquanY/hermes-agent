"""Managed model connection repository contracts."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from hermes_cli.model_connections import (
    ModelConnectionError,
    ModelConnectionRepository,
    model_connections_path,
)


def repository(
    tmp_path: Path, owner_id: str = "owner_12345678"
) -> ModelConnectionRepository:
    return ModelConnectionRepository(control_home=tmp_path, owner_id=owner_id)


def builtin_draft(**overrides):
    value = {
        "connection_id": "builtin:deepseek",
        "provider_id": "deepseek",
        "name": "DeepSeek",
        "credential_ref": "dovie-secure://model-credentials/cred_123",
        "models": [{"id": "deepseek-chat", "display_name": "DeepSeek Chat"}],
    }
    value.update(overrides)
    return value


def custom_draft(**overrides):
    value = {
        "connection_id": "custom:local-ollama",
        "provider_id": "custom",
        "kind": "custom_endpoint",
        "name": "Local Ollama",
        "base_url": "http://127.0.0.1:11434/v1",
        "api_mode": "chat_completions",
        "models": [{"id": "qwen3:32b", "display_name": "Qwen 32B"}],
    }
    value.update(overrides)
    return value


def test_owner_scoped_paths_and_data_are_isolated(tmp_path):
    first = repository(tmp_path, "owner_aaaaaaaa")
    second = repository(tmp_path, "owner_bbbbbbbb")

    first.upsert(
        builtin_draft(),
        expected_revision=0,
        operation_id="op-first",
    )

    assert first.get("builtin:deepseek") is not None
    assert second.get("builtin:deepseek") is None
    assert first.path != second.path
    assert first.path == model_connections_path(tmp_path, "owner_aaaaaaaa")


def test_upsert_is_atomic_revisioned_and_idempotent(tmp_path):
    repo = repository(tmp_path)
    created = repo.upsert(
        builtin_draft(),
        expected_revision=0,
        operation_id="op-create",
    )
    replayed = repo.upsert(
        builtin_draft(),
        expected_revision=0,
        operation_id="op-create",
    )

    assert created == replayed
    assert created["revision"] == 1
    assert created["scope"]["owner_id"] == "owner_12345678"
    assert created["picker_visibility_default"] is False
    stored = yaml.safe_load(repo.path.read_text(encoding="utf-8"))
    assert stored["schema_version"] == 1
    assert stored["revision"] == 1
    assert stored["owner_id"] == "owner_12345678"


def test_operation_id_reuse_and_revision_conflict_are_rejected(tmp_path):
    repo = repository(tmp_path)
    repo.upsert(
        builtin_draft(),
        expected_revision=0,
        operation_id="op-create",
    )

    with pytest.raises(ModelConnectionError) as reused:
        repo.upsert(
            builtin_draft(name="Changed"),
            expected_revision=1,
            operation_id="op-create",
        )
    assert reused.value.code == "MODEL_OPERATION_ID_REUSED"

    with pytest.raises(ModelConnectionError) as conflict:
        repo.upsert(
            builtin_draft(name="Changed"),
            expected_revision=0,
            operation_id="op-update",
        )
    assert conflict.value.code == "MODEL_CONNECTION_REVISION_CONFLICT"
    assert conflict.value.details["actual_revision"] == 1


def test_secret_fields_are_never_persisted(tmp_path):
    repo = repository(tmp_path)

    with pytest.raises(ModelConnectionError) as error:
        repo.upsert(
            builtin_draft(api_key="plaintext"),
            expected_revision=0,
            operation_id="op-secret",
        )

    assert error.value.code == "MODEL_CONNECTION_SECRET_FORBIDDEN"
    assert not repo.path.exists()


@pytest.mark.parametrize(
    "draft",
    [
        builtin_draft(
            connection_id="builtin:dovie-cloud",
            provider_id="dovie-cloud",
            name="Dovie Cloud",
        ),
        custom_draft(
            connection_id="custom:dovie-shadow",
            provider_id="dovie",
        ),
        custom_draft(
            connection_id="cloud:dovie",
            provider_id="custom",
        ),
    ],
)
def test_dovie_cloud_is_not_a_user_manageable_connection(tmp_path, draft):
    repo = repository(tmp_path)

    with pytest.raises(ModelConnectionError) as error:
        repo.upsert(
            draft,
            expected_revision=0,
            operation_id=f"op-platform-{draft['provider_id']}",
        )

    assert error.value.code == "MODEL_CONNECTION_PLATFORM_MANAGED"
    assert not repo.path.exists()


def test_remote_http_requires_explicit_consent(tmp_path):
    repo = repository(tmp_path)
    insecure = custom_draft(base_url="http://models.example.test/v1")

    with pytest.raises(ModelConnectionError) as error:
        repo.upsert(
            insecure,
            expected_revision=0,
            operation_id="op-insecure",
        )
    assert error.value.code == "MODEL_CONNECTION_INSECURE_ENDPOINT"

    created = repo.upsert(
        {**insecure, "allow_insecure_http": True},
        expected_revision=0,
        operation_id="op-insecure-confirmed",
    )
    assert created["base_url"] == "http://models.example.test/v1"
    assert "allow_insecure_http" not in created


def test_manual_models_and_custom_retirement_keep_history(tmp_path):
    repo = repository(tmp_path)
    created = repo.upsert(
        custom_draft(),
        expected_revision=0,
        operation_id="op-create",
    )
    updated = repo.manual_model_upsert(
        created["connection_id"],
        {
            "id": "vision-model",
            "display_name": "Vision",
            "context_window": 64000,
            "vision_enabled": True,
            "reasoning_enabled": True,
            "reasoning_efforts": ["none", "enabled", "low", "high"],
            "default_reasoning_effort": "enabled",
            "reasoning_format": "reasoning_content",
        },
        expected_revision=created["revision"],
        operation_id="op-model",
    )
    stored_model = updated["manual_models"]["vision-model"]
    assert stored_model["context_window"] == 64000
    assert stored_model["vision_enabled"] is True
    assert stored_model["reasoning_enabled"] is True
    assert stored_model["reasoning_efforts"] == [
        "none",
        "enabled",
        "low",
        "high",
    ]
    assert stored_model["default_reasoning_effort"] == "enabled"
    assert stored_model["reasoning_format"] == "reasoning_content"
    assert updated["model_preferences"]["vision-model"]["picker_visible"] is False

    removed = repo.manual_model_delete(
        created["connection_id"],
        "vision-model",
        expected_revision=updated["revision"],
        operation_id="op-model-delete",
    )
    assert "vision-model" not in removed["manual_models"]

    tombstone = repo.retire(
        created["connection_id"],
        expected_revision=removed["revision"],
        operation_id="op-retire",
    )
    assert repo.get(created["connection_id"]) is None
    assert tombstone["connection_id"] == created["connection_id"]
    assert tombstone["name"] == "Local Ollama"


@pytest.mark.parametrize(
    ("model", "field"),
    [
        (
            {
                "id": "bad-reasoning",
                "reasoning_enabled": "yes",
            },
            "reasoning_enabled",
        ),
        (
            {
                "id": "bad-reasoning",
                "reasoning_enabled": True,
                "reasoning_efforts": ["impossible"],
            },
            "reasoning_efforts",
        ),
        (
            {
                "id": "bad-reasoning",
                "reasoning_enabled": True,
                "reasoning_efforts": ["low", "high"],
                "default_reasoning_effort": "medium",
            },
            "default_reasoning_effort",
        ),
        (
            {
                "id": "bad-reasoning",
                "reasoning_efforts": ["low"],
            },
            "reasoning_enabled",
        ),
    ],
)
def test_manual_model_reasoning_capabilities_are_validated(
    tmp_path,
    model,
    field,
):
    repo = repository(tmp_path)
    created = repo.upsert(
        custom_draft(models=[]),
        expected_revision=0,
        operation_id="op-create",
    )

    with pytest.raises(ModelConnectionError) as error:
        repo.manual_model_upsert(
            created["connection_id"],
            model,
            expected_revision=created["revision"],
            operation_id=f"op-{field}",
        )

    assert error.value.code == "MODEL_CONNECTION_INVALID"
    assert error.value.details["field"] == field


def test_model_picker_visibility_is_revisioned_without_invalidating_routes(tmp_path):
    repo = repository(tmp_path)
    created = repo.upsert(
        builtin_draft(),
        expected_revision=0,
        operation_id="op-create",
    )

    hidden = repo.set_model_picker_visibility(
        created["connection_id"],
        "deepseek-chat",
        False,
        expected_revision=created["revision"],
        operation_id="op-hide-model",
    )

    assert hidden["model_preferences"] == {"deepseek-chat": {"picker_visible": False}}
    assert hidden["revision"] == created["revision"] + 1
    assert hidden["routing_revision"] == created["routing_revision"]

    visible = repo.set_model_picker_visibility(
        created["connection_id"],
        "deepseek-chat",
        True,
        expected_revision=hidden["revision"],
        operation_id="op-show-model",
    )

    assert visible["model_preferences"] == {
        "deepseek-chat": {"picker_visible": True}
    }
    assert visible["routing_revision"] == created["routing_revision"]


def test_builtin_retire_disconnects_overlay_without_tombstone(tmp_path):
    repo = repository(tmp_path)
    created = repo.upsert(
        builtin_draft(),
        expected_revision=0,
        operation_id="op-create",
    )
    disconnected = repo.retire(
        created["connection_id"],
        expected_revision=created["revision"],
        operation_id="op-retire",
    )

    assert disconnected["credential_ref"] is None
    assert disconnected["manual_models"] == {}
    assert repo.get(created["connection_id"])["revision"] == 2


def test_credential_rotation_does_not_change_routing_revision(tmp_path):
    repo = repository(tmp_path)
    created = repo.upsert(
        builtin_draft(),
        expected_revision=0,
        operation_id="op-create",
    )
    rotated = repo.upsert(
        builtin_draft(
            credential_ref="dovie-secure://model-credentials/cred_rotated",
        ),
        expected_revision=created["revision"],
        operation_id="op-rotate",
    )

    assert rotated["revision"] == created["revision"] + 1
    assert rotated["routing_revision"] == created["routing_revision"]

    rerouted = repo.upsert(
        {
            **rotated,
            "api_mode": "codex_responses",
        },
        expected_revision=rotated["revision"],
        operation_id="op-reroute",
    )
    assert rerouted["routing_revision"] == rotated["routing_revision"] + 1


def test_profile_defaults_are_owner_scoped_revisioned_structured_selections(tmp_path):
    repo = repository(tmp_path)
    selection = {
        "schema_version": 1,
        "expected_route_kind": "dovie_cloud",
        "connection_id": "cloud:dovie",
        "provider_id": "dovie-cloud",
        "model_id": "dovie/default",
        "connection_revision": 0,
    }

    saved = repo.set_profile_default(
        "profile-1",
        selection,
        expected_revision=0,
        operation_id="profile-default-1",
    )

    assert saved["selection"] == selection
    assert saved["revision"] == 1
    assert repo.profile_default("profile-1") == saved
    assert repository(tmp_path, "owner_bbbbbbbb").profile_default("profile-1") is None
    with pytest.raises(ModelConnectionError) as conflict:
        repo.set_profile_default(
            "profile-1",
            {**selection, "model_id": "dovie/other"},
            expected_revision=0,
            operation_id="profile-default-conflict",
        )
    assert conflict.value.code == "MODEL_PROFILE_DEFAULT_REVISION_CONFLICT"
