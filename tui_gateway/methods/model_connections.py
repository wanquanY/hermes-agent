"""Managed model connection JSON-RPC methods."""

from __future__ import annotations

from typing import Any

from tui_gateway.methods._shared import bind_server_globals

_server = bind_server_globals(globals())


def _domain_error(rid: Any, error: Exception) -> dict[str, Any]:
    from hermes_cli.credential_resolver import CredentialResolutionError
    from hermes_cli.model_connections import ModelConnectionError

    if isinstance(error, (ModelConnectionError, CredentialResolutionError)):
        return {
            "jsonrpc": "2.0",
            "id": rid,
            "error": {
                "code": -32042,
                "message": str(error),
                "data": {
                    "error_code": error.code,
                    "details": getattr(error, "details", {}),
                },
            },
        }
    return _err(rid, 5036, str(error))


def _repository():
    from hermes_cli.model_connections import ModelConnectionRepository

    return ModelConnectionRepository.for_runtime()


def _operation_id(params: dict) -> str:
    return str(params.get("operation_id") or params.get("operationId") or "").strip()


def _expected_revision(params: dict, *, required: bool = False) -> int | None:
    raw = params.get("expected_revision", params.get("expectedRevision"))
    if raw is None and not required:
        return None
    if raw is None:
        raise ValueError("expected_revision is required")
    return int(raw)


def _emit_connection_changed(connection: dict[str, Any], operation_id: str) -> None:
    _emit(
        "model.connection.changed",
        "",
        {
            "schema_version": 1,
            "operation_id": operation_id,
            "connection_id": connection.get("connection_id"),
            "provider_id": connection.get("provider_id"),
            "revision": connection.get("revision")
            or connection.get("retired_revision"),
            "status": ("retired" if connection.get("retired_at") else "changed"),
        },
    )
    _emit(
        "model.catalog.changed",
        "",
        {
            "schema_version": 1,
            "operation_id": operation_id,
            "connection_id": connection.get("connection_id"),
        },
    )


def _clear_discovery(connection_id: str) -> None:
    from hermes_cli.model_discovery_cache import clear_discovered_models

    repository = _repository()
    clear_discovered_models(
        owner_id=repository.owner_id,
        connection_id=connection_id,
    )


@method("model.connection.list")
def _(rid, params: dict) -> dict:
    try:
        repository = _repository()
        return _ok(
            rid,
            {
                "schema_version": 1,
                "owner_id": repository.owner_id,
                "connections": repository.list(
                    include_disabled=bool(params.get("include_disabled", True))
                ),
            },
        )
    except Exception as error:
        return _domain_error(rid, error)


@method("model.connection.get")
def _(rid, params: dict) -> dict:
    try:
        connection_id = str(
            params.get("connection_id") or params.get("connectionId") or ""
        ).strip()
        connection = _repository().get(connection_id)
        if connection is None:
            if bool(params.get("allow_missing", params.get("allowMissing", False))):
                return _ok(
                    rid,
                    {
                        "schema_version": 1,
                        "connection": None,
                    },
                )
            from hermes_cli.model_connections import ModelConnectionError

            raise ModelConnectionError(
                "MODEL_CONNECTION_NOT_FOUND",
                "model connection was not found",
            )
        return _ok(rid, {"schema_version": 1, "connection": connection})
    except Exception as error:
        return _domain_error(rid, error)


@method("model.profile_default.get")
def _(rid, params: dict) -> dict:
    try:
        profile_id = str(
            params.get("profile_id")
            or params.get("profileId")
            or params.get("agent_profile_id")
            or params.get("agentProfileId")
            or ""
        ).strip()
        default = _repository().profile_default(profile_id)
        return _ok(
            rid,
            {
                "schema_version": 1,
                "profile_id": profile_id,
                "default": default,
            },
        )
    except Exception as error:
        return _domain_error(rid, error)


@method("model.profile_default.set")
def _(rid, params: dict) -> dict:
    try:
        profile_id = str(
            params.get("profile_id")
            or params.get("profileId")
            or params.get("agent_profile_id")
            or params.get("agentProfileId")
            or ""
        ).strip()
        selection = params.get("selection") or params.get("model_selection")
        from tui_gateway.services.model_route_runtime import route_snapshot

        snapshot = route_snapshot(selection)
        operation_id = _operation_id(params)
        default = _repository().set_profile_default(
            profile_id,
            snapshot["selection"],
            expected_revision=_expected_revision(params),
            operation_id=operation_id,
        )
        _emit(
            "model.profile_default.changed",
            "",
            {
                "schema_version": 1,
                "operation_id": operation_id,
                "profile_id": profile_id,
                "revision": default["revision"],
                "selection": default["selection"],
            },
        )
        return _ok(
            rid,
            {
                "schema_version": 1,
                "operation_id": operation_id,
                "default": default,
                **snapshot,
            },
        )
    except Exception as error:
        from tui_gateway.services.model_route_runtime import route_error_response

        response = route_error_response(rid, error)
        return response or _domain_error(rid, error)


@method("model.connection.upsert")
def _(rid, params: dict) -> dict:
    try:
        operation_id = _operation_id(params)
        raw_connection = params.get("connection") or params
        connection = _repository().upsert(
            raw_connection,
            expected_revision=_expected_revision(params),
            operation_id=operation_id,
        )
        _clear_discovery(str(connection.get("connection_id") or ""))
        _emit_connection_changed(connection, operation_id)
        return _ok(
            rid,
            {
                "schema_version": 1,
                "operation_id": operation_id,
                "connection": connection,
            },
        )
    except Exception as error:
        return _domain_error(rid, error)


@method("model.connection.delete")
def _(rid, params: dict) -> dict:
    try:
        operation_id = _operation_id(params)
        connection = _repository().retire(
            str(params.get("connection_id") or params.get("connectionId") or ""),
            expected_revision=int(_expected_revision(params, required=True)),
            operation_id=operation_id,
        )
        _clear_discovery(str(connection.get("connection_id") or ""))
        _emit_connection_changed(connection, operation_id)
        return _ok(
            rid,
            {
                "schema_version": 1,
                "operation_id": operation_id,
                "connection": connection,
            },
        )
    except Exception as error:
        return _domain_error(rid, error)


@method("model.connection.activate")
def _(rid, params: dict) -> dict:
    try:
        operation_id = _operation_id(params)
        repository = _repository()
        connection_id = str(
            params.get("connection_id") or params.get("connectionId") or ""
        ).strip()
        existing = repository.get(connection_id)
        if existing is None:
            from hermes_cli.model_connections import ModelConnectionError

            raise ModelConnectionError(
                "MODEL_CONNECTION_NOT_FOUND",
                "model connection was not found",
            )
        connection = repository.upsert(
            {**existing, "enabled": True},
            expected_revision=_expected_revision(params, required=True),
            operation_id=operation_id,
        )
        _clear_discovery(connection_id)
        _emit_connection_changed(connection, operation_id)
        return _ok(
            rid,
            {
                "schema_version": 1,
                "operation_id": operation_id,
                "connection": connection,
            },
        )
    except Exception as error:
        return _domain_error(rid, error)


@method("model.manual_model.upsert")
def _(rid, params: dict) -> dict:
    try:
        operation_id = _operation_id(params)
        connection = _repository().manual_model_upsert(
            str(params.get("connection_id") or params.get("connectionId") or ""),
            params.get("model") or params.get("model_descriptor") or {},
            expected_revision=int(_expected_revision(params, required=True)),
            operation_id=operation_id,
        )
        _emit_connection_changed(connection, operation_id)
        return _ok(
            rid,
            {
                "schema_version": 1,
                "operation_id": operation_id,
                "connection": connection,
            },
        )
    except Exception as error:
        return _domain_error(rid, error)


@method("model.manual_model.delete")
def _(rid, params: dict) -> dict:
    try:
        operation_id = _operation_id(params)
        connection = _repository().manual_model_delete(
            str(params.get("connection_id") or params.get("connectionId") or ""),
            str(params.get("model_id") or params.get("modelId") or ""),
            expected_revision=int(_expected_revision(params, required=True)),
            operation_id=operation_id,
        )
        _emit_connection_changed(connection, operation_id)
        return _ok(
            rid,
            {
                "schema_version": 1,
                "operation_id": operation_id,
                "connection": connection,
            },
        )
    except Exception as error:
        return _domain_error(rid, error)


@method("model.picker_visibility.set")
def _(rid, params: dict) -> dict:
    try:
        operation_id = _operation_id(params)
        connection = _repository().set_model_picker_visibility(
            str(params.get("connection_id") or params.get("connectionId") or ""),
            str(params.get("model_id") or params.get("modelId") or ""),
            bool(params.get("visible", True)),
            expected_revision=int(_expected_revision(params, required=True)),
            operation_id=operation_id,
        )
        _emit_connection_changed(connection, operation_id)
        return _ok(
            rid,
            {
                "schema_version": 1,
                "operation_id": operation_id,
                "connection": connection,
            },
        )
    except Exception as error:
        return _domain_error(rid, error)


@method("model.connection.validate")
def _(rid, params: dict) -> dict:
    """Resolve the managed credential and probe the provider model endpoint."""

    try:
        connection_id = str(
            params.get("connection_id") or params.get("connectionId") or ""
        ).strip()
        connection = _repository().get(connection_id)
        if connection is None:
            from hermes_cli.model_connections import ModelConnectionError

            raise ModelConnectionError(
                "MODEL_CONNECTION_NOT_FOUND",
                "model connection was not found",
            )
        from hermes_cli.runtime_provider import resolve_runtime_provider

        runtime = resolve_runtime_provider(
            requested=str(connection.get("provider_id") or ""),
            connection_id=connection_id,
            target_model=str(params.get("model_id") or "") or None,
            credential_purpose="validation",
        )
        base_url = str(runtime.get("base_url") or "").rstrip("/")
        if not base_url:
            raise ValueError("provider runtime has no base URL")
        from providers import get_provider_profile

        provider_id = str(connection.get("provider_id") or "").strip()
        profile = get_provider_profile(provider_id)
        timeout = max(
            1.0,
            min(float(params.get("timeout_seconds") or 10.0), 30.0),
        )
        discover_models = bool(connection.get("discover_models", True))
        limited = bool(
            not discover_models or (profile and not profile.supports_health_check)
        )
        if limited:
            discovered_models: list[str] = []
        else:
            api_key = runtime.get("api_key")
            discovered = (
                profile.fetch_models(
                    api_key=(
                        api_key
                        if isinstance(api_key, str)
                        and api_key
                        and api_key != "no-key-required"
                        else None
                    ),
                    base_url=base_url,
                    timeout=timeout,
                )
                if profile is not None
                else None
            )
            if discovered is None:
                # ``ProviderProfile.fetch_models`` intentionally collapses an
                # unsupported catalog, transport failure, and unrecognised
                # catalog response to ``None``. It therefore cannot prove
                # that a credential or inference route is invalid. Persist the
                # connection and report an honest limited observation instead
                # of rejecting a valid provider such as Kimi Coding, whose
                # inference endpoint does not expose a compatible /models API.
                limited = True
                discovered = []
            discovered_models = [
                str(model_id) for model_id in discovered if str(model_id or "").strip()
            ][:200]
        from hermes_cli.model_discovery_cache import store_discovered_models

        store_discovered_models(
            owner_id=_repository().owner_id,
            connection_id=connection_id,
            connection_revision=int(connection["revision"]),
            model_ids=discovered_models,
            status="limited" if limited else "ready",
            limited=limited,
            reachable=None if limited else True,
        )
        return _ok(
            rid,
            {
                "schema_version": 1,
                "connection_id": connection_id,
                "connection_revision": connection["revision"],
                "status": "limited" if limited else "ready",
                "limited": limited,
                "reachable": None if limited else True,
                "credential_accepted": None if limited else True,
                "inference_tested": False,
                "credential_generation": int(runtime.get("credential_generation") or 0),
                "models": discovered_models,
            },
        )
    except Exception as error:
        return _domain_error(rid, error)


@method("model.credential.refresh")
def _(rid, params: dict) -> dict:
    try:
        connection_id = str(
            params.get("connection_id") or params.get("connectionId") or ""
        ).strip()
        connection = _repository().get(connection_id)
        if connection is None:
            from hermes_cli.model_connections import ModelConnectionError

            raise ModelConnectionError(
                "MODEL_CONNECTION_NOT_FOUND",
                "model connection was not found",
            )
        generation = int(params.get("generation") or 0)
        change = str(params.get("change") or "refreshed")
        if change in {"revoked", "validation_failed"}:
            _clear_discovery(connection_id)
        _emit(
            "model.credential.status_changed",
            "",
            {
                "schema_version": 1,
                "connection_id": connection_id,
                "provider_id": connection.get("provider_id"),
                "credential_ref": connection.get("credential_ref"),
                "generation": generation,
                "change": change,
            },
        )
        _emit("model.catalog.changed", "", {"schema_version": 1})
        return _ok(
            rid,
            {
                "schema_version": 1,
                "connection_id": connection_id,
                "generation": generation,
                "refreshed": True,
            },
        )
    except Exception as error:
        return _domain_error(rid, error)


@method("model.oauth.start")
def _(rid, params: dict) -> dict:
    try:
        from hermes_cli.managed_oauth import start_managed_oauth

        result = start_managed_oauth(
            provider_id=str(params.get("provider_id") or ""),
            connection_id=str(params.get("connection_id") or ""),
            credential_ref=str(params.get("credential_ref") or ""),
            expected_generation=int(params.get("expected_generation") or 0),
        )
        return _ok(rid, {"schema_version": 1, **result})
    except Exception as error:
        return _domain_error(rid, error)


@method("model.oauth.submit")
def _(rid, params: dict) -> dict:
    try:
        from hermes_cli.managed_oauth import submit_managed_oauth

        result = submit_managed_oauth(
            provider_id=str(params.get("provider_id") or ""),
            session_id=str(params.get("session_id") or ""),
            code=str(params.get("code") or ""),
        )
        return _ok(rid, {"schema_version": 1, **result})
    except Exception as error:
        return _domain_error(rid, error)


@method("model.oauth.poll")
def _(rid, params: dict) -> dict:
    try:
        from hermes_cli.managed_oauth import poll_managed_oauth

        result = poll_managed_oauth(
            provider_id=str(params.get("provider_id") or ""),
            session_id=str(params.get("session_id") or ""),
        )
        return _ok(rid, {"schema_version": 1, **result})
    except Exception as error:
        return _domain_error(rid, error)


@method("model.oauth.cancel")
def _(rid, params: dict) -> dict:
    try:
        from hermes_cli.managed_oauth import cancel_managed_oauth

        result = cancel_managed_oauth(
            provider_id=str(params.get("provider_id") or ""),
            session_id=str(params.get("session_id") or ""),
        )
        return _ok(rid, {"schema_version": 1, **result})
    except Exception as error:
        return _domain_error(rid, error)


@method("model.oauth.import_external")
def _(rid, params: dict) -> dict:
    try:
        from hermes_cli.managed_oauth import import_external_oauth

        result = import_external_oauth(
            provider_id=str(params.get("provider_id") or ""),
            connection_id=str(params.get("connection_id") or ""),
            credential_ref=str(params.get("credential_ref") or ""),
            expected_generation=int(params.get("expected_generation") or 0),
        )
        return _ok(rid, {"schema_version": 1, **result})
    except Exception as error:
        return _domain_error(rid, error)
