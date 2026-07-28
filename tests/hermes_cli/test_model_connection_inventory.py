"""V2 inventory projection stays additive and connection-aware."""

from pathlib import Path

import yaml

from hermes_cli.model_connection_inventory import project_model_options_v2
from hermes_cli.model_connections import ModelConnectionRepository
from hermes_cli.model_discovery_cache import store_discovered_models


def test_projection_adds_stable_identity_and_manual_models(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("DOVIE_LOCAL_OWNER_ID", "owner_12345678")
    repository = ModelConnectionRepository(
        control_home=tmp_path,
        owner_id="owner_12345678",
    )
    repository.upsert(
        {
            "connection_id": "builtin:deepseek",
            "provider_id": "deepseek",
            "credential_ref": "dovie-secure://model-credentials/cred_1",
            "models": [{"id": "manual-new", "display_name": "Manual New"}],
        },
        expected_revision=0,
        operation_id="op-create",
    )
    payload = project_model_options_v2(
        {
            "providers": [
                {
                    "slug": "deepseek",
                    "name": "DeepSeek",
                    "authenticated": False,
                    "models": ["deepseek-chat"],
                }
            ],
            "model": "manual-new",
            "provider": "deepseek",
        },
        repository,
    )

    assert payload["schema_version"] == 2
    assert payload["owner_id"] == "owner_12345678"
    assert payload["current"] == {
        "provider_id": "deepseek",
        "connection_id": "builtin:deepseek",
        "model_id": "manual-new",
    }
    provider = payload["providers"][0]
    assert provider["connection_id"] == "builtin:deepseek"
    assert provider["credential_status"]["configured"] is True
    assert provider["models"] == ["deepseek-chat", "manual-new"]
    assert provider["model_descriptors"][1]["origin"] == "manual"
    assert all(
        descriptor["picker_visible"] is False
        for descriptor in provider["model_descriptors"]
    )
    assert provider["api_mode"] == "chat_completions"


def test_projection_appends_custom_connections(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("DOVIE_LOCAL_OWNER_ID", "owner_12345678")
    repository = ModelConnectionRepository(
        control_home=tmp_path,
        owner_id="owner_12345678",
    )
    repository.upsert(
        {
            "connection_id": "custom:ollama",
            "provider_id": "custom",
            "name": "Ollama",
            "base_url": "http://127.0.0.1:11434/v1",
            "api_mode": "chat_completions",
            "models": [{"id": "qwen3", "display_name": "Qwen 3"}],
        },
        expected_revision=0,
        operation_id="op-create",
    )

    payload = project_model_options_v2(
        {"providers": [], "model": "", "provider": ""},
        repository,
    )

    assert payload["providers"][0]["connection_id"] == "custom:ollama"
    assert payload["providers"][0]["models"] == ["qwen3"]
    assert payload["providers"][0]["availability"] == "ready"


def test_projection_adds_process_local_discovered_models(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("DOVIE_LOCAL_OWNER_ID", "owner_discovery")
    repository = ModelConnectionRepository(
        control_home=tmp_path,
        owner_id="owner_discovery",
    )
    connection = repository.upsert(
        {
            "connection_id": "custom:discovery",
            "provider_id": "custom",
            "name": "Discovery",
            "base_url": "http://127.0.0.1:11434/v1",
            "api_mode": "chat_completions",
        },
        expected_revision=0,
        operation_id="op-discovery",
    )
    store_discovered_models(
        owner_id=repository.owner_id,
        connection_id=connection["connection_id"],
        connection_revision=connection["revision"],
        model_ids=["remote-model"],
    )

    payload = project_model_options_v2(
        {"providers": [], "model": "", "provider": ""},
        repository,
    )

    descriptor = payload["providers"][0]["model_descriptors"][0]
    assert descriptor["id"] == "remote-model"
    assert descriptor["origin"] == "discovered"
    assert descriptor["picker_visible"] is False


def test_projection_exposes_picker_visibility_without_removing_models(
    tmp_path: Path,
    monkeypatch,
):
    monkeypatch.setenv("DOVIE_LOCAL_OWNER_ID", "owner_visibility")
    repository = ModelConnectionRepository(
        control_home=tmp_path,
        owner_id="owner_visibility",
    )
    connection = repository.upsert(
        {
            "connection_id": "builtin:deepseek",
            "provider_id": "deepseek",
            "credential_ref": "dovie-secure://model-credentials/cred_1",
        },
        expected_revision=0,
        operation_id="op-create",
    )
    repository.set_model_picker_visibility(
        connection["connection_id"],
        "deepseek-chat",
        False,
        expected_revision=connection["revision"],
        operation_id="op-hide",
    )

    payload = project_model_options_v2(
        {
            "providers": [
                {
                    "slug": "deepseek",
                    "name": "DeepSeek",
                    "authenticated": False,
                    "models": ["deepseek-chat", "deepseek-reasoner"],
                }
            ],
            "model": "",
            "provider": "",
        },
        repository,
    )

    provider = payload["providers"][0]
    assert provider["models"] == ["deepseek-chat", "deepseek-reasoner"]
    descriptors = {
        descriptor["id"]: descriptor for descriptor in provider["model_descriptors"]
    }
    assert descriptors["deepseek-chat"]["picker_visible"] is False
    assert descriptors["deepseek-reasoner"]["picker_visible"] is False


def test_projection_migrates_legacy_connections_to_hidden_by_default(
    tmp_path: Path,
    monkeypatch,
):
    monkeypatch.setenv("DOVIE_LOCAL_OWNER_ID", "owner_legacy01")
    repository = ModelConnectionRepository(
        control_home=tmp_path,
        owner_id="owner_legacy01",
    )
    repository.upsert(
        {
            "connection_id": "builtin:deepseek",
            "provider_id": "deepseek",
            "credential_ref": "dovie-secure://model-credentials/cred_legacy",
        },
        expected_revision=0,
        operation_id="op-create",
    )
    document = yaml.safe_load(repository.path.read_text(encoding="utf-8"))
    document["connections"]["builtin:deepseek"].pop(
        "picker_visibility_default",
        None,
    )
    repository.path.write_text(
        yaml.safe_dump(document, sort_keys=False),
        encoding="utf-8",
    )

    payload = project_model_options_v2(
        {
            "providers": [
                {
                    "slug": "deepseek",
                    "name": "DeepSeek",
                    "authenticated": False,
                    "models": ["deepseek-chat"],
                }
            ],
            "model": "",
            "provider": "",
        },
        repository,
    )

    assert payload["providers"][0]["model_descriptors"][0]["picker_visible"] is False


def test_projection_keeps_unadded_builtin_models_visible(
    tmp_path: Path,
    monkeypatch,
):
    monkeypatch.setenv("DOVIE_LOCAL_OWNER_ID", "owner_builtin1")
    repository = ModelConnectionRepository(
        control_home=tmp_path,
        owner_id="owner_builtin1",
    )

    payload = project_model_options_v2(
        {
            "providers": [
                {
                    "slug": "moa",
                    "name": "Mixture of Agents",
                    "auth_type": "virtual",
                    "authenticated": True,
                    "models": ["default"],
                }
            ],
            "model": "",
            "provider": "",
        },
        repository,
    )

    assert payload["providers"][0]["model_descriptors"][0]["picker_visible"] is True


def test_projection_seeds_curated_models_for_managed_builtin_connection(
    tmp_path: Path,
    monkeypatch,
):
    monkeypatch.setenv("DOVIE_LOCAL_OWNER_ID", "owner_curated1")
    repository = ModelConnectionRepository(
        control_home=tmp_path,
        owner_id="owner_curated1",
    )
    repository.upsert(
        {
            "connection_id": "builtin:kimi-coding",
            "provider_id": "kimi-coding",
            "kind": "builtin",
            "name": "Kimi Coding",
            "credential_ref": "dovie-secure://model-credentials/cred_kimi",
        },
        expected_revision=0,
        operation_id="op-kimi-curated",
    )

    payload = project_model_options_v2(
        {
            "providers": [
                {
                    "slug": "kimi-coding",
                    "name": "Kimi / Kimi Coding Plan",
                    "authenticated": False,
                    "models": [],
                }
            ],
            "model": "",
            "provider": "",
        },
        repository,
    )

    provider = payload["providers"][0]
    assert provider["connection_id"] == "builtin:kimi-coding"
    assert "kimi-k2.6" in provider["models"]
    descriptor = next(
        descriptor
        for descriptor in provider["model_descriptors"]
        if descriptor["id"] == "kimi-k2.6"
    )
    assert descriptor["origin"] == "curated"
    assert descriptor["reasoning_enabled"] is True
    assert descriptor["reasoning_efforts"] == [
        "none",
        "enabled",
        "low",
        "medium",
        "high",
        "max",
        "xhigh",
    ]
    assert descriptor["default_reasoning_effort"] == "enabled"
    assert descriptor["reasoning_format"] == "thinking_blocks"


def test_projection_keeps_manual_capabilities_authoritative(
    tmp_path: Path,
    monkeypatch,
):
    monkeypatch.setenv("DOVIE_LOCAL_OWNER_ID", "owner_manualcap")
    repository = ModelConnectionRepository(
        control_home=tmp_path,
        owner_id="owner_manualcap",
    )
    repository.upsert(
        {
            "connection_id": "custom:private",
            "provider_id": "custom",
            "kind": "custom_endpoint",
            "name": "Private",
            "base_url": "https://models.example/v1",
            "api_mode": "chat_completions",
            "models": [
                {
                    "id": "private-reasoner",
                    "display_name": "Private Reasoner",
                    "context_window": 131072,
                    "vision_enabled": True,
                    "reasoning_enabled": True,
                    "reasoning_efforts": ["none", "low", "high"],
                    "default_reasoning_effort": "high",
                    "reasoning_format": "reasoning_content",
                }
            ],
        },
        expected_revision=0,
        operation_id="op-private",
    )

    payload = project_model_options_v2(
        {"providers": [], "model": "", "provider": ""},
        repository,
    )

    descriptor = payload["providers"][0]["model_descriptors"][0]
    assert descriptor["context_window"] == 131072
    assert descriptor["vision_enabled"] is True
    assert descriptor["reasoning_enabled"] is True
    assert descriptor["reasoning_efforts"] == ["none", "low", "high"]
    assert descriptor["default_reasoning_effort"] == "high"
    assert descriptor["reasoning_format"] == "reasoning_content"


def test_projection_exposes_limited_validation_without_claiming_readiness(
    tmp_path: Path,
    monkeypatch,
):
    monkeypatch.setenv("DOVIE_LOCAL_OWNER_ID", "owner_limited1")
    repository = ModelConnectionRepository(
        control_home=tmp_path,
        owner_id="owner_limited1",
    )
    connection = repository.upsert(
        {
            "connection_id": "custom:no-catalog",
            "provider_id": "custom",
            "name": "No Catalog",
            "base_url": "https://models.example/v1",
            "api_mode": "chat_completions",
            "models": [{"id": "private-model"}],
        },
        expected_revision=0,
        operation_id="op-limited",
    )
    store_discovered_models(
        owner_id=repository.owner_id,
        connection_id=connection["connection_id"],
        connection_revision=connection["revision"],
        model_ids=[],
        status="limited",
        limited=True,
        reachable=None,
    )

    payload = project_model_options_v2(
        {"providers": [], "model": "", "provider": ""},
        repository,
    )

    provider = payload["providers"][0]
    assert provider["validation"]["status"] == "limited"
    assert provider["validation"]["limited"] is True
    assert provider["validation"]["reachable"] is None
    assert provider["notice_code"] == "validation_limited"
    assert "inference has not been tested" in provider["warning"]
    assert provider["models"] == ["private-model"]


def test_projection_does_not_promote_cli_hints_to_desktop_notices(
    tmp_path: Path,
    monkeypatch,
):
    monkeypatch.setenv("DOVIE_LOCAL_OWNER_ID", "owner_notice01")
    repository = ModelConnectionRepository(
        control_home=tmp_path,
        owner_id="owner_notice01",
    )

    payload = project_model_options_v2(
        {
            "providers": [
                {
                    "slug": "moa",
                    "name": "Mixture of Agents",
                    "auth_type": "virtual",
                    "authenticated": True,
                    "models": ["default"],
                    "warning": (
                        "The aggregator acts after the configured reference models."
                    ),
                },
                {
                    "slug": "nous",
                    "name": "Nous Portal",
                    "auth_type": "oauth_device_code",
                    "authenticated": False,
                    "models": [],
                    "warning": ("run `hermes model` to configure (oauth_device_code)"),
                },
            ],
            "model": "",
            "provider": "",
        },
        repository,
    )

    providers = {
        row["provider_id"]: row
        for row in payload["providers"]
        if not row.get("is_connection_template")
    }
    assert "notice_code" not in providers["moa"]
    assert "notice_code" not in providers["nous"]


def test_projection_exposes_managed_auth_methods_and_secret_kind(
    tmp_path: Path,
    monkeypatch,
):
    monkeypatch.setenv("DOVIE_LOCAL_OWNER_ID", "owner_authmethod")
    repository = ModelConnectionRepository(
        control_home=tmp_path,
        owner_id="owner_authmethod",
    )
    repository.upsert(
        {
            "connection_id": "builtin:anthropic",
            "provider_id": "anthropic",
            "credential_ref": "dovie-secure://model-credentials/cred_oauth",
        },
        expected_revision=0,
        operation_id="op-auth-method",
    )

    payload = project_model_options_v2(
        {
            "providers": [
                {
                    "slug": "anthropic",
                    "name": "Anthropic",
                    "auth_type": "api_key",
                    "authenticated": False,
                    "models": ["claude-sonnet-4"],
                }
            ],
            "model": "",
            "provider": "",
        },
        repository,
        credential_status_reader=lambda _reference: {
            "configured": True,
            "status": "active",
            "generation": 8,
            "secret_kind": "oauth_bundle",
        },
    )

    provider = payload["providers"][0]
    assert provider["auth_methods"] == ["api_key", "oauth"]
    assert provider["managed_oauth"]["flow"] == "pkce"
    assert provider["credential_status"]["secret_kind"] == "oauth_bundle"


def test_projection_keeps_dovie_cloud_out_of_user_connection_inventory(
    tmp_path: Path,
    monkeypatch,
):
    monkeypatch.setenv("DOVIE_LOCAL_OWNER_ID", "owner_platform1")
    repository = ModelConnectionRepository(
        control_home=tmp_path,
        owner_id="owner_platform1",
    )

    payload = project_model_options_v2(
        {
            "providers": [
                {
                    "slug": "dovie-cloud",
                    "name": "Dovie Cloud",
                    "auth_type": "api_key",
                    "authenticated": True,
                    "models": ["kimi-k3"],
                },
                {
                    "slug": "deepseek",
                    "name": "DeepSeek",
                    "auth_type": "api_key",
                    "authenticated": False,
                    "models": ["deepseek-chat"],
                },
            ],
            "model": "kimi-k3",
            "provider": "dovie-cloud",
        },
        repository,
    )

    managed_rows = [
        row for row in payload["providers"] if not row.get("is_connection_template")
    ]
    assert [row["provider_id"] for row in managed_rows] == ["deepseek"]
    template = next(
        row for row in payload["providers"] if row.get("is_connection_template")
    )
    assert template["provider_id"] == "custom"
    assert template["configuration"]["endpoint"]["mode"] == "required"
    assert template["configuration"]["credential"]["mode"] == "optional"
    assert template["configuration"]["api_mode"]["default"] == ""
    assert payload["current"] == {
        "provider_id": "dovie-cloud",
        "connection_id": "cloud:dovie",
        "model_id": "kimi-k3",
    }


def test_projection_exposes_provider_owned_configuration_requirements(
    tmp_path: Path,
    monkeypatch,
):
    monkeypatch.setenv("DOVIE_LOCAL_OWNER_ID", "owner_config01")
    repository = ModelConnectionRepository(
        control_home=tmp_path,
        owner_id="owner_config01",
    )

    payload = project_model_options_v2(
        {
            "providers": [
                {
                    "slug": "azure-foundry",
                    "name": "Azure Foundry",
                    "auth_type": "api_key",
                    "key_env": "AZURE_FOUNDRY_API_KEY",
                    "authenticated": False,
                    "models": [],
                },
                {
                    "slug": "bedrock",
                    "name": "AWS Bedrock",
                    "auth_type": "aws_sdk",
                    "authenticated": False,
                    "models": [],
                },
                {
                    "slug": "custom",
                    "name": "Custom endpoint",
                    "auth_type": "api_key",
                    "authenticated": False,
                    "models": [],
                },
            ],
            "model": "",
            "provider": "",
        },
        repository,
    )

    azure = next(
        row for row in payload["providers"] if row["provider_id"] == "azure-foundry"
    )
    assert azure["configuration"]["endpoint"]["mode"] == "required"
    assert azure["configuration"]["api_mode"]["configurable"] is True
    assert azure["configuration"]["credential"] == {
        "mode": "required",
        "auth_type": "api_key",
        "key_label": "AZURE_FOUNDRY_API_KEY",
    }

    bedrock = next(
        row for row in payload["providers"] if row["provider_id"] == "bedrock"
    )
    assert bedrock["auth_methods"] == ["aws_sdk"]
    assert bedrock["configuration"]["credential"]["mode"] == "external"

    custom_rows = [
        row for row in payload["providers"] if row["provider_id"] == "custom"
    ]
    assert len(custom_rows) == 1
    assert custom_rows[0]["is_connection_template"] is True
    assert custom_rows[0]["configuration"]["connection_kind"] == "custom_endpoint"


def test_projection_recovers_registry_auth_for_rows_and_orphaned_connections(
    tmp_path: Path,
    monkeypatch,
):
    monkeypatch.setenv("DOVIE_LOCAL_OWNER_ID", "owner_registry1")
    repository = ModelConnectionRepository(
        control_home=tmp_path,
        owner_id="owner_registry1",
    )
    repository.upsert(
        {
            "connection_id": "builtin:nous",
            "provider_id": "nous",
            "name": "Nous Portal",
        },
        expected_revision=0,
        operation_id="op-nous",
    )

    payload = project_model_options_v2(
        {
            "providers": [
                {
                    "slug": "copilot",
                    "name": "GitHub Copilot",
                    "auth_type": "api_key",
                    "authenticated": True,
                    "models": [],
                }
            ],
            "model": "",
            "provider": "",
        },
        repository,
    )

    providers = {
        row["provider_id"]: row
        for row in payload["providers"]
        if not row.get("is_connection_template")
    }
    copilot = providers["copilot"]
    assert copilot["auth_type"] == "copilot"
    assert copilot["auth_methods"] == ["copilot"]
    assert copilot["configuration"]["credential"]["mode"] == "external"

    nous = providers["nous"]
    assert nous["auth_type"] == "oauth_device_code"
    assert nous["auth_methods"] == ["oauth"]
    assert nous["configuration"]["credential"]["mode"] == "external"
