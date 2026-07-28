from pathlib import Path

import pytest

from hermes_cli.model_connections import ModelConnectionRepository
from hermes_cli.model_routes import (
    ModelRouteError,
    RouteResolutionService,
    RunPreparationService,
)


def _repository(tmp_path: Path) -> ModelConnectionRepository:
    return ModelConnectionRepository(
        control_home=tmp_path,
        owner_id="owner_12345678",
    )


def _direct_selection(revision: int = 1) -> dict:
    return {
        "schema_version": 1,
        "expected_route_kind": "hermes_direct",
        "connection_id": "custom:ollama",
        "provider_id": "custom",
        "model_id": "qwen3",
        "connection_revision": revision,
    }


def test_route_resolution_is_authoritative_and_fingerprinted(tmp_path: Path):
    repository = _repository(tmp_path)
    repository.upsert(
        {
            "connection_id": "custom:ollama",
            "provider_id": "custom",
            "name": "Ollama",
            "base_url": "http://127.0.0.1:11434/v1",
            "api_mode": "chat_completions",
            "models": [{"id": "qwen3"}],
        },
        expected_revision=0,
        operation_id="create-ollama",
    )

    snapshot = RouteResolutionService(repository).resolve(_direct_selection())

    assert snapshot["route"]["route_kind"] == "hermes_direct"
    assert snapshot["route"]["cloud_usage_policy"] == "direct_only"
    assert snapshot["route_fingerprint"].startswith("sha256:")


def test_expected_route_cannot_authorize_cloud_bypass(tmp_path: Path):
    service = RouteResolutionService(_repository(tmp_path))
    selection = {
        "schema_version": 1,
        "expected_route_kind": "dovie_cloud",
        "connection_id": "builtin:deepseek",
        "provider_id": "deepseek",
        "model_id": "deepseek-chat",
    }

    with pytest.raises(ModelRouteError) as error:
        service.resolve(selection)

    assert error.value.code == "MODEL_ROUTE_ASSERTION_MISMATCH"


def test_run_resolution_is_single_use_and_revision_bound(tmp_path: Path):
    repository = _repository(tmp_path)
    repository.upsert(
        {
            "connection_id": "custom:ollama",
            "provider_id": "custom",
            "name": "Ollama",
            "base_url": "http://127.0.0.1:11434/v1",
            "models": [{"id": "qwen3"}],
        },
        expected_revision=0,
        operation_id="create-ollama",
    )
    service = RunPreparationService(RouteResolutionService(repository))
    prepared = service.prepare(
        conversation_session_id="conversation-1",
        execution_target={"kind": "conversation"},
        session_revision=7,
        selection=_direct_selection(),
    )

    consumed = service.consume(
        prepared["resolution_id"],
        conversation_session_id="conversation-1",
        execution_target={"kind": "conversation"},
        session_revision=7,
        selection=_direct_selection(),
    )
    assert consumed["route"]["route_kind"] == "hermes_direct"

    with pytest.raises(ModelRouteError) as error:
        service.consume(
            prepared["resolution_id"],
            conversation_session_id="conversation-1",
            execution_target={"kind": "conversation"},
            session_revision=7,
            selection=_direct_selection(),
        )
    assert error.value.code in {
        "RUN_ROUTE_RESOLUTION_NOT_FOUND",
        "RUN_ROUTE_RESOLUTION_CONSUMED",
    }
