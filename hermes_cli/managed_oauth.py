"""Managed Desktop OAuth orchestration over Hermes provider state machines.

The existing Hermes browser/device flows remain the provider authority.  A
managed invocation injects a credential sink so completed token bundles go
directly to Electron's encrypted Broker instead of Hermes auth files.
"""

from __future__ import annotations

import asyncio
import base64
import json
import time
from datetime import datetime
from typing import Any, Callable, Mapping

from hermes_cli.credential_resolver import (
    CredentialResolutionError,
    credential_status_dict,
    runtime_credential_resolver,
)
from hermes_cli.model_connections import (
    ModelConnectionError,
    ModelConnectionRepository,
)

_CATALOG = {
    "anthropic": {
        "flow": "pkce",
        "command": "hermes auth add anthropic",
        "docs_url": "https://docs.claude.com/en/api/getting-started",
    },
    "nous": {
        "flow": "device_code",
        "command": "hermes auth add nous",
        "docs_url": "https://portal.nousresearch.com",
    },
    "openai-codex": {
        "flow": "device_code",
        "command": "hermes auth add openai-codex",
        "docs_url": "https://platform.openai.com/docs",
    },
    "qwen-oauth": {
        "flow": "external",
        "command": "qwen auth qwen-oauth",
        "docs_url": "https://github.com/QwenLM/qwen-code",
    },
    "minimax-oauth": {
        "flow": "device_code",
        "command": "hermes auth add minimax-oauth",
        "docs_url": "https://www.minimax.io",
    },
}


def oauth_catalog() -> list[dict[str, Any]]:
    return [
        {"provider_id": provider_id, **descriptor}
        for provider_id, descriptor in _CATALOG.items()
    ]


def managed_oauth_descriptor(provider_id: str) -> dict[str, Any] | None:
    descriptor = _CATALOG.get(str(provider_id or "").strip())
    return {"provider_id": provider_id, **descriptor} if descriptor else None


def _managed_connection(
    connection_id: str,
    provider_id: str,
    credential_ref: str,
) -> dict[str, Any]:
    connection = ModelConnectionRepository.for_runtime().get(connection_id)
    if not isinstance(connection, dict):
        raise ModelConnectionError(
            "MODEL_CONNECTION_NOT_FOUND",
            "model connection was not found",
        )
    if str(connection.get("provider_id") or "") != provider_id:
        raise ModelConnectionError(
            "MODEL_CONNECTION_PROVIDER_MISMATCH",
            "OAuth provider does not match the model connection",
        )
    if str(connection.get("credential_ref") or "") != credential_ref:
        raise ModelConnectionError(
            "MODEL_CONNECTION_CREDENTIAL_MISMATCH",
            "OAuth credential does not match the model connection",
        )
    if provider_id not in _CATALOG:
        raise ModelConnectionError(
            "MODEL_OAUTH_UNSUPPORTED",
            "provider has no managed OAuth flow",
        )
    return connection


def _credential_sink(
    *,
    connection_id: str,
    credential_ref: str,
    expected_generation: int,
) -> Callable[[Mapping[str, Any]], dict[str, Any]]:
    resolver = runtime_credential_resolver()

    def store(bundle: Mapping[str, Any]) -> dict[str, Any]:
        status = resolver.update_oauth(
            credential_ref,
            connection_id=connection_id,
            purpose="oauth_bootstrap",
            expected_generation=expected_generation,
            bundle=bundle,
        )
        return credential_status_dict(status)

    return store


def start_managed_oauth(
    *,
    provider_id: str,
    connection_id: str,
    credential_ref: str,
    expected_generation: int,
) -> dict[str, Any]:
    provider = str(provider_id or "").strip()
    connection = str(connection_id or "").strip()
    reference = str(credential_ref or "").strip()
    _managed_connection(connection, provider, reference)
    descriptor = _CATALOG[provider]
    if descriptor["flow"] == "external":
        return {
            "provider_id": provider,
            "flow": "external",
            "status": "external_required",
            "command": descriptor["command"],
            "docs_url": descriptor["docs_url"],
        }

    from hermes_cli import web_server as oauth_runtime

    sink = _credential_sink(
        connection_id=connection,
        credential_ref=reference,
        expected_generation=int(expected_generation),
    )
    if descriptor["flow"] == "pkce" and provider == "anthropic":
        result = oauth_runtime._start_anthropic_pkce(sink)
    else:
        result = asyncio.run(oauth_runtime._start_device_code_flow(provider, sink))
    return {
        "provider_id": provider,
        "connection_id": connection,
        "credential_ref": reference,
        "status": "pending",
        **result,
    }


def submit_managed_oauth(
    *,
    provider_id: str,
    session_id: str,
    code: str,
) -> dict[str, Any]:
    provider = str(provider_id or "").strip()
    if provider != "anthropic":
        raise ModelConnectionError(
            "MODEL_OAUTH_SUBMIT_UNSUPPORTED",
            "provider does not accept a pasted authorization code",
        )
    from hermes_cli import web_server as oauth_runtime

    return oauth_runtime._submit_anthropic_pkce(
        str(session_id or "").strip(),
        str(code or "").strip(),
    )


def poll_managed_oauth(
    *,
    provider_id: str,
    session_id: str,
) -> dict[str, Any]:
    from hermes_cli import web_server as oauth_runtime

    provider = str(provider_id or "").strip()
    sid = str(session_id or "").strip()
    with oauth_runtime._oauth_sessions_lock:
        session = oauth_runtime._oauth_sessions.get(sid)
        if not session:
            raise ModelConnectionError(
                "MODEL_OAUTH_SESSION_NOT_FOUND",
                "OAuth session was not found or expired",
            )
        if str(session.get("provider") or "") != provider:
            raise ModelConnectionError(
                "MODEL_OAUTH_PROVIDER_MISMATCH",
                "OAuth session belongs to another provider",
            )
        if (
            session.get("status") == "pending"
            and session.get("expires_at")
            and float(session["expires_at"]) <= time.time()
        ):
            session["status"] = "expired"
            session["credential_sink"] = None
        return {
            "provider_id": provider,
            "session_id": sid,
            "status": str(session.get("status") or "pending"),
            "error_message": str(session.get("error_message") or "") or None,
            "expires_at": session.get("expires_at"),
            "credential_generation": int(
                session.get("credential_generation") or 0
            ),
        }


def cancel_managed_oauth(
    *,
    provider_id: str,
    session_id: str,
) -> dict[str, Any]:
    from hermes_cli import web_server as oauth_runtime

    provider = str(provider_id or "").strip()
    sid = str(session_id or "").strip()
    with oauth_runtime._oauth_sessions_lock:
        session = oauth_runtime._oauth_sessions.get(sid)
        if not session:
            return {"cancelled": False, "session_id": sid}
        if str(session.get("provider") or "") != provider:
            raise ModelConnectionError(
                "MODEL_OAUTH_PROVIDER_MISMATCH",
                "OAuth session belongs to another provider",
            )
        session["status"] = "cancelled"
        session["credential_sink"] = None
    return {"cancelled": True, "session_id": sid}


def import_external_oauth(
    *,
    provider_id: str,
    connection_id: str,
    credential_ref: str,
    expected_generation: int,
) -> dict[str, Any]:
    provider = str(provider_id or "").strip()
    if provider != "qwen-oauth":
        raise ModelConnectionError(
            "MODEL_OAUTH_IMPORT_UNSUPPORTED",
            "provider does not expose an external OAuth import",
        )
    _managed_connection(connection_id, provider, credential_ref)
    from hermes_cli.auth import _read_qwen_cli_tokens

    tokens = _read_qwen_cli_tokens()
    access_token = str(tokens.get("access_token") or "").strip()
    if not access_token:
        raise CredentialResolutionError(
            "CREDENTIAL_REQUIRED",
            "Qwen CLI has no OAuth credential to import",
        )
    bundle = {
        "provider_id": provider,
        "access_token": access_token,
        "refresh_token": str(tokens.get("refresh_token") or ""),
        "token_type": str(tokens.get("token_type") or "Bearer"),
        "resource_url": str(tokens.get("resource_url") or "portal.qwen.ai"),
        "expires_at_ms": int(tokens.get("expiry_date") or 0),
        "external_source": "qwen-cli",
    }
    return _credential_sink(
        connection_id=connection_id,
        credential_ref=credential_ref,
        expected_generation=expected_generation,
    )(bundle)


def _jwt_expiry(value: str) -> float:
    try:
        payload = value.split(".")[1]
        decoded = base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))
        data = json.loads(decoded.decode("utf-8"))
        return float(data.get("exp") or 0)
    except Exception:
        return 0.0


def _bundle_expiry(bundle: Mapping[str, Any]) -> float:
    for key in ("agent_key_expires_at", "expires_at", "expiresAt"):
        value = bundle.get(key)
        if not value:
            continue
        try:
            return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
        except (TypeError, ValueError):
            continue
    for key in ("expires_at_ms", "expiry_date", "expiresAtMs"):
        try:
            value = float(bundle.get(key) or 0)
        except (TypeError, ValueError):
            value = 0
        if value > 0:
            return value / 1000.0
    return _jwt_expiry(str(bundle.get("access_token") or ""))


def oauth_bundle_needs_refresh(
    bundle: Mapping[str, Any],
    *,
    skew_seconds: int = 300,
) -> bool:
    expiry = _bundle_expiry(bundle)
    return bool(expiry and expiry <= time.time() + max(30, int(skew_seconds)))


def refresh_oauth_bundle(
    provider_id: str,
    bundle: Mapping[str, Any],
    *,
    force: bool = False,
) -> dict[str, Any]:
    """Refresh a Broker-owned bundle without writing Hermes auth state."""

    provider = str(provider_id or "").strip()
    current = dict(bundle)
    if not force and not oauth_bundle_needs_refresh(current):
        return current
    if provider == "anthropic":
        from agent.anthropic_adapter import refresh_anthropic_oauth_pure

        refreshed = refresh_anthropic_oauth_pure(
            str(current.get("refresh_token") or "")
        )
        current.update(refreshed)
    elif provider == "openai-codex":
        from hermes_cli.auth import refresh_codex_oauth_pure

        refreshed = refresh_codex_oauth_pure(
            str(current.get("access_token") or ""),
            str(current.get("refresh_token") or ""),
        )
        current.update(refreshed)
    elif provider == "nous":
        from hermes_cli.auth import refresh_nous_oauth_from_state

        current = refresh_nous_oauth_from_state(current, force_refresh=True)
        current["provider_id"] = provider
    elif provider == "minimax-oauth":
        from hermes_cli.auth import _refresh_minimax_oauth_state

        current = _refresh_minimax_oauth_state(
            current,
            force=True,
            persist=False,
        )
    elif provider == "qwen-oauth":
        from hermes_cli.auth import _refresh_qwen_cli_tokens

        current = _refresh_qwen_cli_tokens(current, persist=False)
        current["expires_at_ms"] = int(current.get("expiry_date") or 0)
    else:
        raise ModelConnectionError(
            "MODEL_OAUTH_REFRESH_UNSUPPORTED",
            "provider does not support managed OAuth refresh",
        )
    current["provider_id"] = provider
    return current


__all__ = [
    "cancel_managed_oauth",
    "import_external_oauth",
    "managed_oauth_descriptor",
    "oauth_bundle_needs_refresh",
    "oauth_catalog",
    "poll_managed_oauth",
    "refresh_oauth_bundle",
    "start_managed_oauth",
    "submit_managed_oauth",
]
