"""Managed model-connection resolution through the Desktop credential boundary."""

from __future__ import annotations

import json
from typing import Any, Callable, Mapping


def resolve_explicit_oauth_runtime(
    *,
    provider: str,
    requested_provider: str,
    explicit_api_key: str,
    explicit_base_url: str,
    provider_registry: Mapping[str, Any],
) -> dict[str, Any] | None:
    """Resolve Broker-provided OAuth tokens without consulting auth files."""

    if provider not in {"qwen-oauth", "minimax-oauth"}:
        return None
    provider_config = provider_registry.get(provider)
    default_base_url = str(
        getattr(provider_config, "inference_base_url", "") or ""
    )
    return {
        "provider": provider,
        "api_mode": (
            "anthropic_messages"
            if provider == "minimax-oauth"
            else "chat_completions"
        ),
        "base_url": (explicit_base_url or default_base_url).rstrip("/"),
        "api_key": explicit_api_key,
        "source": "explicit",
        "requested_provider": requested_provider,
    }


def resolve_managed_connection_runtime(
    *,
    connection_id: str,
    requested: str | None,
    target_model: str | None,
    runtime_executor: str | None,
    codex_home: str | None,
    credential_purpose: str,
    provider_registry: Mapping[str, Any],
    normalize_runtime_executor: Callable[[str | None], str],
    resolve_runtime: Callable[..., dict[str, Any]],
    logger: Any,
) -> dict[str, Any]:
    """Resolve one owner-scoped connection through the credential Broker."""

    from hermes_cli.credential_resolver import runtime_credential_resolver
    from hermes_cli.model_connections import (
        ModelConnectionError,
        ModelConnectionRepository,
    )

    repository = ModelConnectionRepository.for_runtime()
    connection = repository.get(connection_id)
    if connection is None:
        provider_id = (
            connection_id.split(":", 1)[1].strip()
            if connection_id.startswith("builtin:")
            else ""
        )
        requested_provider = str(requested or provider_id).strip()
        provider_config = provider_registry.get(provider_id)
        auth_type = str(
            getattr(provider_config, "auth_type", "") or ""
        ).strip()
        no_secret_runtime = auth_type in {"", "none"}
        codex_runtime = (
            normalize_runtime_executor(runtime_executor) == "codex_app_server"
        )
        if (
            provider_id
            and requested_provider.lower() == provider_id.lower()
            and (no_secret_runtime or codex_runtime)
        ):
            runtime = resolve_runtime(
                requested=provider_id,
                target_model=target_model,
                runtime_executor=runtime_executor,
                codex_home=codex_home,
            )
            runtime.update(
                {
                    "requested_provider": provider_id,
                    "connection_id": connection_id,
                    "connection_revision": 0,
                    "credential_generation": 0,
                    "credential_lease_id": None,
                    "source": (
                        "codex-external-runtime"
                        if codex_runtime
                        else "managed-no-auth-builtin"
                    ),
                }
            )
            return runtime
        raise ModelConnectionError(
            "MODEL_CONNECTION_NOT_FOUND",
            "model connection was not found",
        )
    if not bool(connection.get("enabled", True)):
        raise ModelConnectionError(
            "MODEL_CONNECTION_DISABLED",
            "model connection is disabled",
        )
    provider_id = str(connection.get("provider_id") or "").strip()
    requested_provider = str(requested or provider_id).strip()
    if requested_provider.lower() != provider_id.lower():
        raise ModelConnectionError(
            "MODEL_CONNECTION_PROVIDER_MISMATCH",
            "requested provider does not match the connection",
        )

    credential_ref = str(connection.get("credential_ref") or "").strip()
    resolved_credential = None
    resolver = None
    explicit_api_key: str | None = None
    credential_base_url: str | None = None
    if credential_ref:
        resolver = runtime_credential_resolver()
        resolved_credential = resolver.resolve(
            credential_ref,
            purpose=credential_purpose,
            connection_id=connection_id,
        )
        resolved_secret_kind = str(
            getattr(resolved_credential, "secret_kind", "api_key") or "api_key"
        )
        if resolved_secret_kind == "oauth_bundle":
            try:
                bundle = json.loads(resolved_credential.value)
            except (TypeError, ValueError) as exc:
                raise ModelConnectionError(
                    "CREDENTIAL_OAUTH_BUNDLE_INVALID",
                    "managed OAuth credential bundle is invalid",
                ) from exc
            if not isinstance(bundle, dict):
                raise ModelConnectionError(
                    "CREDENTIAL_OAUTH_BUNDLE_INVALID",
                    "managed OAuth credential bundle must be an object",
                )
            bundle_provider = str(bundle.get("provider_id") or "").strip()
            if bundle_provider and bundle_provider.lower() != provider_id.lower():
                raise ModelConnectionError(
                    "CREDENTIAL_OAUTH_PROVIDER_MISMATCH",
                    "managed OAuth credential belongs to another provider",
                )
            if provider_id == "nous":
                explicit_api_key = str(bundle.get("agent_key") or "").strip()
                credential_base_url = str(
                    bundle.get("inference_base_url") or ""
                ).strip()
            else:
                explicit_api_key = str(
                    bundle.get("access_token")
                    or bundle.get("accessToken")
                    or ""
                ).strip()
                credential_base_url = str(
                    bundle.get("base_url")
                    or bundle.get("inference_base_url")
                    or ""
                ).strip()
            if not explicit_api_key:
                raise ModelConnectionError(
                    "CREDENTIAL_OAUTH_BUNDLE_INVALID",
                    "managed OAuth credential has no inference token",
                )
        else:
            explicit_api_key = resolved_credential.value
    else:
        provider_config = provider_registry.get(provider_id)
        auth_type = str(
            getattr(provider_config, "auth_type", "") or ""
        ).strip()
        if str(connection.get("kind") or "") == "builtin" and auth_type not in {
            "",
            "none",
        }:
            raise ModelConnectionError(
                "CREDENTIAL_REQUIRED",
                "model connection credential is not configured",
            )

    try:
        runtime = resolve_runtime(
            requested=provider_id,
            explicit_api_key=explicit_api_key,
            explicit_base_url=str(connection.get("base_url") or "").strip()
            or credential_base_url
            or None,
            target_model=target_model,
            runtime_executor=runtime_executor,
            codex_home=codex_home,
        )
    except Exception:
        if resolved_credential is not None and resolver is not None:
            try:
                resolver.report_result(
                    resolved_credential.lease_id,
                    outcome="resolution_failed",
                    error_code="RUNTIME_PROVIDER_RESOLUTION_FAILED",
                )
            except Exception:
                logger.debug(
                    "managed credential failure report failed",
                    exc_info=True,
                )
        raise

    configured_api_mode = str(connection.get("api_mode") or "").strip()
    configured_base_url = str(connection.get("base_url") or "").strip()
    runtime.update(
        {
            "requested_provider": provider_id,
            "connection_id": connection_id,
            "connection_revision": int(connection.get("revision") or 0),
            "credential_generation": (
                int(resolved_credential.generation)
                if resolved_credential is not None
                else 0
            ),
            "credential_lease_id": (
                resolved_credential.lease_id
                if resolved_credential is not None
                else None
            ),
            "credential_secret_kind": (
                str(
                    getattr(
                        resolved_credential,
                        "secret_kind",
                        "api_key",
                    )
                    or "api_key"
                )
                if resolved_credential is not None
                else None
            ),
            "source": (
                "dovie-credential-broker"
                if resolved_credential is not None
                else "managed-no-auth-connection"
            ),
        }
    )
    if configured_base_url:
        runtime["base_url"] = configured_base_url.rstrip("/")
    if configured_api_mode:
        runtime["api_mode"] = configured_api_mode
    if not runtime.get("api_key"):
        runtime["api_key"] = "no-key-required"
    return runtime


__all__ = [
    "resolve_explicit_oauth_runtime",
    "resolve_managed_connection_runtime",
]
