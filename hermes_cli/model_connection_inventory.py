"""Additive V2 model inventory projection for managed connections."""

from __future__ import annotations

import copy
import os
from typing import Any, Callable

from hermes_cli.model_connections import (
    OWNER_ENV,
    ModelConnectionRepository,
    current_local_owner_id,
)
from hermes_cli.model_provider_identity import (
    DOVIE_CLOUD_CONNECTION_ID,
    is_dovie_cloud_connection,
    is_dovie_cloud_provider,
)
from hermes_cli.model_routes import default_api_mode

CredentialStatusReader = Callable[[str], dict[str, Any]]

_API_MODE_OPTIONS = (
    {"value": "", "label": "Auto-detect"},
    {"value": "chat_completions", "label": "OpenAI Chat Completions"},
    {"value": "codex_responses", "label": "OpenAI Responses / Codex"},
    {"value": "anthropic_messages", "label": "Anthropic Messages"},
)

_VALIDATION_LIMITED_NOTICE = "validation_limited"


def runtime_connection_repository() -> ModelConnectionRepository | None:
    """Return the owner-scoped repository when managed context is present."""

    if not str(os.environ.get(OWNER_ENV) or "").strip():
        return None
    return ModelConnectionRepository.for_runtime()


def runtime_credential_status_reader() -> CredentialStatusReader | None:
    """Return a metadata-only status reader when Desktop broker is available."""

    try:
        from hermes_cli.credential_resolver import (
            BROKER_BOOTSTRAP_ENV,
            credential_status_dict,
            runtime_credential_resolver,
        )

        if not str(os.environ.get(BROKER_BOOTSTRAP_ENV) or "").strip():
            return None
        resolver = runtime_credential_resolver()
        return lambda credential_ref: credential_status_dict(
            resolver.status(credential_ref)
        )
    except Exception:
        return None


def _credential_status(
    connection: dict[str, Any] | None,
    reader: CredentialStatusReader | None,
) -> dict[str, Any]:
    credential_ref = str((connection or {}).get("credential_ref") or "").strip()
    if not credential_ref:
        return {
            "configured": False,
            "status": "missing",
            "generation": 0,
            "secret_kind": "api_key",
        }
    if reader is None:
        return {
            "configured": True,
            "status": "configured",
            "generation": 0,
            "secret_kind": "api_key",
        }
    status = reader(credential_ref)
    return {
        "configured": bool(status.get("configured", True)),
        "status": str(status.get("status") or "configured"),
        "generation": int(status.get("generation") or 0),
        "secret_kind": str(status.get("secret_kind") or "api_key"),
    }


def _model_descriptor(
    model_id: str,
    *,
    provider_id: str,
    base_url: str,
    api_mode: str,
    origin: str,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    from hermes_cli.model_capability_inventory import model_capability_metadata

    metadata = dict(metadata or {})
    return {
        **model_capability_metadata(
            provider_id,
            model_id,
            base_url=base_url,
            api_mode=api_mode,
        ),
        **metadata,
        "id": model_id,
        "display_name": str(
            metadata.get("display_name") or metadata.get("name") or model_id
        ),
        "origin": origin,
    }


def _descriptors_for_row(
    row: dict[str, Any],
    connection: dict[str, Any] | None,
    *,
    discovered: tuple[str, ...] = (),
) -> list[dict[str, Any]]:
    descriptors: dict[str, dict[str, Any]] = {}
    provider_id = str(
        (connection or {}).get("provider_id")
        or row.get("provider_id")
        or row.get("slug")
        or ""
    ).strip()
    base_url = str(
        (connection or {}).get("base_url")
        or row.get("base_url")
        or row.get("api_url")
        or ""
    ).strip()
    configured_api_mode = str(
        (connection or {}).get("api_mode") or row.get("api_mode") or ""
    ).strip()
    api_mode = configured_api_mode or default_api_mode(provider_id, connection)
    row_models = list(row.get("models") or [])
    if not row_models and connection and str(connection.get("kind") or "") == "builtin":
        # Managed Desktop credentials do not enter process environment
        # variables, so the legacy authenticated-provider listing cannot see
        # them. Once a managed built-in connection exists, seed its V2
        # descriptors from Hermes' own curated catalog and then merge
        # discovered/manual models below.
        from hermes_cli.models import _PROVIDER_MODELS

        row_models = list(_PROVIDER_MODELS.get(provider_id, ()))
    for model in row_models:
        model_id = str(
            model.get("id") if isinstance(model, dict) else model or ""
        ).strip()
        if not model_id:
            continue
        metadata = model if isinstance(model, dict) else None
        descriptors[model_id] = _model_descriptor(
            model_id,
            provider_id=provider_id,
            base_url=base_url,
            api_mode=api_mode,
            origin=str((metadata or {}).get("origin") or "curated"),
            metadata=metadata,
        )
    for model_id in discovered:
        normalized_id = str(model_id or "").strip()
        if not normalized_id or normalized_id in descriptors:
            continue
        descriptors[normalized_id] = _model_descriptor(
            normalized_id,
            provider_id=provider_id,
            base_url=base_url,
            api_mode=api_mode,
            origin="discovered",
        )
    manual_models = (connection or {}).get("manual_models")
    if isinstance(manual_models, dict):
        for model_id, metadata in manual_models.items():
            normalized_id = str(model_id or "").strip()
            if not normalized_id:
                continue
            descriptors[normalized_id] = _model_descriptor(
                normalized_id,
                provider_id=provider_id,
                base_url=base_url,
                api_mode=api_mode,
                origin="manual",
                metadata=metadata if isinstance(metadata, dict) else None,
            )
    model_preferences = (connection or {}).get("model_preferences")
    preferences = model_preferences if isinstance(model_preferences, dict) else {}
    picker_visibility_default = (
        bool(connection.get("picker_visibility_default", False))
        if connection is not None
        else True
    )
    for model_id, descriptor in descriptors.items():
        raw_preference = preferences.get(model_id)
        preference = raw_preference if isinstance(raw_preference, dict) else {}
        descriptor["picker_visible"] = bool(
            preference.get("picker_visible", picker_visibility_default)
        )
    return list(descriptors.values())


def _availability(
    *,
    enabled: bool,
    credential: dict[str, Any],
    authenticated: bool,
    credential_required: bool,
) -> str:
    if not enabled:
        return "disabled"
    status = str(credential.get("status") or "")
    if status in {"staged", "validating", "pending"}:
        return "credential_pending"
    if status in {"invalid", "auth_rejected"}:
        return "credential_invalid"
    if status in {"revoked", "missing"} and credential_required and not authenticated:
        return "credential_required"
    return "ready"


def _provider_auth_type(
    provider_id: str,
    declared_auth_type: str | None = None,
) -> str:
    """Resolve authentication semantics from Hermes' provider registry."""

    declared = str(declared_auth_type or "").strip().lower()
    profile_auth = ""
    try:
        from providers import get_provider_profile

        profile = get_provider_profile(provider_id)
        profile_auth = str(getattr(profile, "auth_type", "") or "").strip().lower()
    except Exception:
        pass
    # ProviderProfile owns the runtime authentication mechanism. Legacy
    # inventory rows historically labeled every configured provider as
    # ``api_key``; never let that lossy hint override a bespoke Hermes flow.
    if profile_auth and profile_auth != "api_key":
        return profile_auth
    if declared:
        return declared
    if profile_auth:
        return profile_auth
    try:
        from hermes_cli.auth import PROVIDER_REGISTRY

        provider_config = PROVIDER_REGISTRY.get(provider_id)
        registered = (
            str(getattr(provider_config, "auth_type", "") or "").strip().lower()
        )
        if registered:
            return registered
    except Exception:
        pass
    return "api_key"


def _provider_configuration(
    provider_id: str,
    *,
    auth_type: str,
    row: dict[str, Any] | None = None,
    connection: dict[str, Any] | None = None,
    template: bool = False,
) -> dict[str, Any]:
    """Expose Hermes-owned connection requirements to non-CLI clients."""

    row = row or {}
    connection = connection or {}
    try:
        from hermes_cli.auth import PROVIDER_REGISTRY

        provider_config = PROVIDER_REGISTRY.get(provider_id)
    except Exception:
        provider_config = None
    try:
        from providers import get_provider_profile

        profile = get_provider_profile(provider_id)
    except Exception:
        profile = None

    connection_kind = (
        "custom_endpoint"
        if template or str(connection.get("kind") or "") == "custom_endpoint"
        else "builtin"
    )
    default_base_url = str(
        connection.get("base_url")
        or row.get("base_url")
        or row.get("api_url")
        or getattr(provider_config, "inference_base_url", "")
        or getattr(profile, "base_url", "")
        or ""
    ).strip()
    base_url_required = connection_kind == "custom_endpoint" or (
        provider_id == "azure-foundry" and not default_base_url
    )
    base_url_optional = provider_id in {"lmstudio"}
    endpoint_mode = (
        "required"
        if base_url_required
        else "optional"
        if base_url_optional
        else "fixed"
    )

    normalized_auth = _provider_auth_type(provider_id, auth_type)
    if connection_kind == "custom_endpoint":
        credential_mode = "optional"
        key_label = "API Key"
    elif normalized_auth == "api_key":
        credential_mode = "required"
        key_label = str(
            row.get("key_env")
            or (
                provider_config.api_key_env_vars[0]
                if provider_config and provider_config.api_key_env_vars
                else ""
            )
            or "API Key"
        )
    elif normalized_auth in {"", "none", "virtual"}:
        credential_mode = "none"
        key_label = ""
    else:
        credential_mode = "external"
        key_label = ""

    api_mode_configurable = connection_kind == "custom_endpoint" or (
        provider_id == "azure-foundry"
    )
    configured_api_mode = str(
        connection.get("api_mode")
        or row.get("api_mode")
        or (
            ""
            if connection_kind == "custom_endpoint"
            else getattr(profile, "api_mode", "")
        )
        or ""
    ).strip()
    return {
        "schema_version": 1,
        "connection_kind": connection_kind,
        "template": bool(template),
        "repeatable": connection_kind == "custom_endpoint",
        "credential": {
            "mode": credential_mode,
            "auth_type": normalized_auth,
            "key_label": key_label,
        },
        "endpoint": {
            "mode": endpoint_mode,
            "default_base_url": default_base_url,
        },
        "api_mode": {
            "configurable": api_mode_configurable,
            "default": configured_api_mode,
            "options": list(_API_MODE_OPTIONS) if api_mode_configurable else [],
        },
        "discover_models": {
            "configurable": connection_kind == "custom_endpoint",
            "default": bool(connection.get("discover_models", True)),
        },
        "allow_insecure_http": {
            "configurable": connection_kind == "custom_endpoint",
        },
    }


def _auth_methods(provider_id: str, auth_type: str) -> tuple[list[str], Any]:
    from hermes_cli.managed_oauth import managed_oauth_descriptor

    normalized_auth = str(auth_type or "api_key").strip().lower()
    managed_oauth = managed_oauth_descriptor(provider_id)
    methods: list[str] = []
    if normalized_auth == "api_key":
        methods.append("api_key")
    elif normalized_auth.startswith("oauth"):
        methods.append("oauth")
    elif normalized_auth in {"", "none", "virtual"}:
        methods.append("none")
    else:
        methods.append(normalized_auth)
    if managed_oauth and "oauth" not in methods:
        methods.append("oauth")
    return methods, managed_oauth


def _custom_endpoint_template() -> dict[str, Any]:
    auth_methods, managed_oauth = _auth_methods("custom", "api_key")
    return {
        "slug": "custom",
        "provider_id": "custom",
        "connection_id": "template:custom-endpoint",
        "connection_revision": 0,
        "name": "Custom Endpoint",
        "is_current": False,
        "is_user_defined": False,
        "is_connection_template": True,
        "connection_kind": "custom_endpoint",
        "source": "model-connection-template",
        "models": [],
        "model_descriptors": [],
        "total_models": 0,
        "authenticated": False,
        "auth_type": "api_key",
        "auth_methods": auth_methods,
        "managed_oauth": managed_oauth,
        "credential_status": {
            "configured": False,
            "status": "missing",
            "generation": 0,
            "secret_kind": "api_key",
        },
        "enabled": False,
        "availability": "credential_required",
        "discover_models": True,
        "configuration": _provider_configuration(
            "custom",
            auth_type="api_key",
            template=True,
        ),
    }


def project_model_options_v2(
    payload: dict[str, Any],
    repository: ModelConnectionRepository,
    *,
    credential_status_reader: CredentialStatusReader | None = None,
) -> dict[str, Any]:
    """Project legacy inventory rows plus owner-scoped connection overlays.

    V1 fields remain intact. V2 descriptors and connection identity are
    additive so standalone TUI/Dashboard clients continue to function.
    """

    projected = copy.deepcopy(payload)
    connections = repository.list()
    by_provider = {
        str(connection.get("provider_id") or "").lower(): connection
        for connection in connections
        if str(connection.get("kind") or "") == "builtin"
        and not is_dovie_cloud_connection(
            connection.get("connection_id"),
            connection.get("provider_id"),
        )
    }
    by_id = {
        str(connection.get("connection_id") or ""): connection
        for connection in connections
        if not is_dovie_cloud_connection(
            connection.get("connection_id"),
            connection.get("provider_id"),
        )
    }
    rows: list[dict[str, Any]] = []
    seen_connections: set[str] = set()

    for source_row in projected.get("providers") or []:
        if not isinstance(source_row, dict):
            continue
        row = dict(source_row)
        provider_id = str(row.get("provider_id") or row.get("slug") or "").strip()
        if is_dovie_cloud_provider(provider_id) or provider_id.lower() == "custom":
            continue
        connection = by_provider.get(provider_id.lower())
        connection_id = str(
            (connection or {}).get("connection_id") or f"builtin:{provider_id}"
        )
        credential = _credential_status(connection, credential_status_reader)
        authenticated = bool(row.get("authenticated"))
        auth_type = _provider_auth_type(provider_id, row.get("auth_type"))
        credential_required = auth_type not in {
            "",
            "none",
            "virtual",
        }
        auth_methods, managed_oauth = _auth_methods(provider_id, auth_type)
        enabled = bool((connection or {}).get("enabled", True))
        from hermes_cli.model_discovery_cache import (
            discovered_models,
            validation_observation,
        )

        descriptors = _descriptors_for_row(
            row,
            connection,
            discovered=discovered_models(
                owner_id=repository.owner_id,
                connection_id=connection_id,
                connection_revision=int((connection or {}).get("revision") or 0),
            ),
        )
        validation = validation_observation(
            owner_id=repository.owner_id,
            connection_id=connection_id,
            connection_revision=int((connection or {}).get("revision") or 0),
        )
        row.update({
            "provider_id": provider_id,
            "connection_id": connection_id,
            "connection_revision": int(
                (connection or {}).get("routing_revision")
                or (connection or {}).get("revision")
                or 0
            ),
            "model_descriptors": descriptors,
            "models": [descriptor["id"] for descriptor in descriptors],
            "credential_ref": (connection or {}).get("credential_ref"),
            "credential_status": credential,
            "auth_type": auth_type,
            "auth_methods": auth_methods,
            "managed_oauth": managed_oauth,
            "enabled": enabled,
            "availability": _availability(
                enabled=enabled,
                credential=credential,
                authenticated=authenticated,
                credential_required=credential_required,
            ),
            "api_mode": default_api_mode(provider_id, connection),
            "configuration": _provider_configuration(
                provider_id,
                auth_type=auth_type,
                row=row,
                connection=connection,
            ),
            "validation": validation,
            **(
                {
                    "notice_code": _VALIDATION_LIMITED_NOTICE,
                    "warning": (
                        "Model discovery or credential validation is limited; "
                        "inference has not been tested."
                    ),
                }
                if validation and validation.get("limited")
                else {}
            ),
        })
        rows.append(row)
        seen_connections.add(connection_id)

    for connection_id, connection in by_id.items():
        if connection_id in seen_connections:
            continue
        provider_id = str(connection.get("provider_id") or "custom")
        credential = _credential_status(connection, credential_status_reader)
        from hermes_cli.model_discovery_cache import (
            discovered_models,
            validation_observation,
        )

        descriptors = _descriptors_for_row(
            {},
            connection,
            discovered=discovered_models(
                owner_id=repository.owner_id,
                connection_id=connection_id,
                connection_revision=int(connection.get("revision") or 0),
            ),
        )
        enabled = bool(connection.get("enabled", True))
        credential_required = bool(connection.get("credential_ref"))
        auth_type = _provider_auth_type(provider_id)
        auth_methods, managed_oauth = _auth_methods(provider_id, auth_type)
        validation = validation_observation(
            owner_id=repository.owner_id,
            connection_id=connection_id,
            connection_revision=int(connection.get("revision") or 0),
        )
        rows.append({
            "slug": connection_id,
            "provider_id": provider_id,
            "connection_id": connection_id,
            "connection_revision": int(
                connection.get("routing_revision") or connection.get("revision") or 0
            ),
            "name": str(connection.get("name") or connection_id),
            "is_current": False,
            "is_user_defined": True,
            "source": "model-connection",
            "models": [descriptor["id"] for descriptor in descriptors],
            "model_descriptors": descriptors,
            "total_models": len(descriptors),
            "authenticated": bool(credential["configured"]),
            "credential_ref": connection.get("credential_ref"),
            "credential_status": credential,
            "enabled": enabled,
            "availability": _availability(
                enabled=enabled,
                credential=credential,
                authenticated=bool(credential["configured"]),
                credential_required=credential_required,
            ),
            "base_url": connection.get("base_url"),
            "api_mode": connection.get("api_mode"),
            "discover_models": bool(connection.get("discover_models", True)),
            "auth_type": auth_type,
            "auth_methods": auth_methods,
            "managed_oauth": managed_oauth,
            "configuration": _provider_configuration(
                provider_id,
                auth_type=auth_type,
                connection=connection,
            ),
            "validation": validation,
            **(
                {
                    "notice_code": _VALIDATION_LIMITED_NOTICE,
                    "warning": (
                        "Model discovery or credential validation is limited; "
                        "inference has not been tested."
                    ),
                }
                if validation and validation.get("limited")
                else {}
            ),
        })

    rows.append(_custom_endpoint_template())

    current_provider = str(projected.get("provider") or "")
    current_model = str(projected.get("model") or "")
    current_row = next(
        (
            row
            for row in rows
            if str(row.get("provider_id") or "").lower() == current_provider.lower()
        ),
        None,
    )
    projected.update({
        "schema_version": 2,
        "providers": rows,
        "current": {
            "provider_id": current_provider,
            "connection_id": str(
                DOVIE_CLOUD_CONNECTION_ID
                if is_dovie_cloud_provider(current_provider)
                else (
                    (current_row or {}).get("connection_id")
                    or (f"builtin:{current_provider}" if current_provider else "")
                )
            ),
            "model_id": current_model,
        },
        "owner_id": current_local_owner_id(),
    })
    return projected


__all__ = [
    "project_model_options_v2",
    "runtime_connection_repository",
    "runtime_credential_status_reader",
]
