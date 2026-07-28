"""Refresh managed provider clients at the inference request boundary."""

from __future__ import annotations

import json
import logging
from typing import Any, Mapping

logger = logging.getLogger(__name__)


def activate_managed_inference_route(
    agent: Any,
    route_resolution: Mapping[str, Any] | None,
) -> tuple[Any, ...]:
    """Refresh credentials and bind the resolved route to the current turn."""

    from agent.inference_route_context import push_inference_route

    refresh_managed_agent_credential(agent, route_resolution)
    runtime = (
        agent._current_main_runtime()
        if callable(getattr(agent, "_current_main_runtime", None))
        else {}
    )
    return push_inference_route(route_resolution, runtime)


def reset_managed_inference_route(tokens: tuple[Any, ...]) -> None:
    """Release a turn-scoped inference route without leaking it to later turns."""

    from agent.inference_route_context import reset_inference_route

    reset_inference_route(tokens)


def refresh_managed_agent_credential(
    agent: Any,
    route_resolution: Mapping[str, Any] | None,
) -> bool:
    """Rebuild the selected provider client when credential identity changes.

    The Broker generation is authoritative.  Route identity intentionally does
    not change during key rotation, so a long-lived session must compare the
    current credential reference/generation immediately before inference.
    """

    if not isinstance(route_resolution, Mapping):
        return False
    route = route_resolution.get("route")
    if not isinstance(route, Mapping):
        return False
    if str(route.get("cloud_usage_policy") or "") != "direct_only":
        return False
    connection_id = str(route.get("connection_id") or "").strip()
    if not connection_id or connection_id == "cloud:dovie":
        return False

    from hermes_cli.model_connections import ModelConnectionRepository

    connection = ModelConnectionRepository.for_runtime().get(connection_id)
    if not isinstance(connection, Mapping):
        # Credential-free built-ins and external Codex runtimes have no
        # managed overlay, therefore no Broker generation to refresh.
        return False
    credential_ref = str(connection.get("credential_ref") or "").strip()
    if not credential_ref:
        return False

    from hermes_cli.credential_resolver import (
        CredentialResolutionError,
        runtime_credential_resolver,
    )

    resolver = runtime_credential_resolver()
    status = resolver.status(credential_ref)
    if not status.configured or status.status not in {"active", "configured"}:
        # Never allow the cached client to become a revocation bypass.
        setattr(agent, "_managed_credential_ref", credential_ref)
        setattr(agent, "_managed_credential_generation", status.generation)
        raise CredentialResolutionError(
            "CREDENTIAL_REQUIRED",
            "managed model credential is not active",
        )
    if str(getattr(status, "secret_kind", "api_key") or "api_key") == "oauth_bundle":
        lease = resolver.resolve(
            credential_ref,
            purpose="oauth_refresh",
            connection_id=connection_id,
        )
        try:
            bundle = json.loads(lease.value)
            if not isinstance(bundle, dict):
                raise ValueError("OAuth bundle must be an object")
            from hermes_cli.managed_oauth import (
                oauth_bundle_needs_refresh,
                refresh_oauth_bundle,
            )

            if oauth_bundle_needs_refresh(bundle):
                refreshed = refresh_oauth_bundle(
                    str(route.get("provider_id") or ""),
                    bundle,
                    force=True,
                )
                status = resolver.update_oauth(
                    credential_ref,
                    connection_id=connection_id,
                    purpose="oauth_refresh",
                    expected_generation=lease.generation,
                    bundle=refreshed,
                )
            resolver.report_result(lease.lease_id, outcome="accepted")
        except Exception:
            try:
                resolver.report_result(
                    lease.lease_id,
                    outcome="resolution_failed",
                    error_code="OAUTH_REFRESH_FAILED",
                )
            except Exception:
                logger.debug(
                    "managed OAuth refresh failure report failed",
                    exc_info=True,
                )
            raise
    current_ref = str(getattr(agent, "_managed_credential_ref", "") or "")
    current_generation = int(
        getattr(agent, "_managed_credential_generation", -1) or 0
    )
    if current_ref == credential_ref and current_generation == status.generation:
        return False

    from hermes_cli.runtime_provider import resolve_runtime_provider

    runtime = resolve_runtime_provider(
        requested=str(route.get("provider_id") or ""),
        connection_id=connection_id,
        target_model=str(route.get("model_id") or ""),
        credential_purpose="inference",
    )
    agent.switch_model(
        new_model=str(route.get("model_id") or getattr(agent, "model", "")),
        new_provider=str(runtime.get("provider") or route.get("provider_id") or ""),
        api_key=runtime.get("api_key") or "",
        base_url=str(runtime.get("base_url") or ""),
        api_mode=str(runtime.get("api_mode") or ""),
    )
    setattr(agent, "_managed_connection_id", connection_id)
    setattr(agent, "_managed_credential_ref", credential_ref)
    setattr(
        agent,
        "_managed_credential_generation",
        int(runtime.get("credential_generation") or status.generation),
    )
    setattr(
        agent,
        "_managed_credential_lease_id",
        str(runtime.get("credential_lease_id") or ""),
    )
    return True


__all__ = [
    "activate_managed_inference_route",
    "refresh_managed_agent_credential",
    "reset_managed_inference_route",
]
