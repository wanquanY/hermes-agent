# ruff: noqa: F401,F403,F405,F821,ARG001
from __future__ import annotations

import asyncio
import hashlib
import os
import threading
from pathlib import Path
from typing import Any, Awaitable, TypeVar

from tui_gateway.methods._shared import bind_server_globals

_server = bind_server_globals(globals())

_T = TypeVar("_T")

_MANAGED_PROXY_PATHS: tuple[tuple[str, str], ...] = (
    ("DOVIE_MINERU_PROXY_URL", "/api/v1/llm-proxy/v1/document-parse"),
    ("DOVIE_SERPER_PROXY_URL", "/api/v1/llm-proxy/v1/serper-search"),
    ("DOVIE_WEB_PARSE_PROXY_URL", "/api/v1/llm-proxy/v1/web-page-parse"),
    ("DOVIE_IMAGE_GENERATE_PROXY_URL", "/api/v1/llm-proxy/v1/image-generate"),
    ("DOVIE_VIDEO_GENERATE_PROXY_URL", "/api/v1/llm-proxy/v1/video-generate"),
    ("DOVIE_SKILL_CATEGORIES_URL", "/api/v1/llm-proxy/v1/skill-market/categories"),
)
_LOCAL_OWNER_ENV = "DOVIE_LOCAL_OWNER_ID"
_CREDENTIAL_BROKER_BOOTSTRAP_ENV = "DOVIE_CREDENTIAL_BROKER_BOOTSTRAP_FILE"


def _has_any(params: dict[str, Any], keys: tuple[str, ...]) -> bool:
    return any(key in params for key in keys)


def _text_param(params: dict[str, Any], keys: tuple[str, ...]) -> str:
    for key in keys:
        if key in params:
            return str(params.get(key) or "").strip()
    return ""


def _fingerprint(runtime_token: str, api_origin: str) -> str:
    return hashlib.sha256(f"{runtime_token}|{api_origin}".encode()).hexdigest()[:12]


def _normalize_api_origin(api_origin: str) -> str:
    return str(api_origin or "").strip().rstrip("/")


def _managed_proxy_env(api_origin: str) -> dict[str, str]:
    normalized = _normalize_api_origin(api_origin)
    if not normalized:
        return {key: "" for key, _path in _MANAGED_PROXY_PATHS}
    return {key: f"{normalized}{path}" for key, path in _MANAGED_PROXY_PATHS}


def _apply_env_update(key: str, value: str) -> None:
    if value:
        os.environ[key] = value
    else:
        os.environ.pop(key, None)


def _validated_credential_boundary(
    params: dict[str, Any],
) -> tuple[bool, str, str]:
    owner_keys = ("local_owner_id", "localOwnerId")
    bootstrap_keys = (
        "credential_broker_bootstrap_path",
        "credentialBrokerBootstrapPath",
    )
    owner_present = _has_any(params, owner_keys)
    bootstrap_present = _has_any(params, bootstrap_keys)
    if owner_present != bootstrap_present:
        raise ValueError(
            "local owner and credential broker bootstrap must be updated together"
        )
    if not owner_present:
        return False, "", ""
    owner_id = _text_param(params, owner_keys)
    bootstrap_path = _text_param(params, bootstrap_keys)
    if bool(owner_id) != bool(bootstrap_path):
        raise ValueError(
            "local owner and credential broker bootstrap must both be set or cleared"
        )
    if owner_id:
        from hermes_cli.credential_resolver import DovieBrokerCredentialResolver
        from hermes_cli.model_connections import validate_local_owner_id

        validate_local_owner_id(owner_id)
        DovieBrokerCredentialResolver(Path(bootstrap_path)).validate_boundary(owner_id)
    return True, owner_id, bootstrap_path


async def apply_runtime_cloud_proxy_update(params: dict[str, Any]) -> dict[str, Any]:
    """Update runtime cloud proxy env in the gateway and live workers.

    Missing fields are left unchanged. Present fields with an empty string
    delete that environment variable, matching ``RuntimeEnvUpdateFrame``.
    """
    params = params or {}
    token_keys = ("runtime_token", "runtimeToken")
    origin_keys = ("api_origin", "apiOrigin")
    token_present = _has_any(params, token_keys)
    origin_present = _has_any(params, origin_keys)
    runtime_token = _text_param(params, token_keys)
    api_origin = _normalize_api_origin(_text_param(params, origin_keys))
    boundary_present, local_owner_id, credential_broker_bootstrap_path = (
        _validated_credential_boundary(params)
    )

    env_updates: dict[str, str] = {}
    if token_present:
        _apply_env_update("DOVIE_LLM_RUNTIME_TOKEN", runtime_token)
        env_updates["DOVIE_LLM_RUNTIME_TOKEN"] = runtime_token
        # Codex platform-mode subprocesses read the same runtime token from
        # DOXIE_PLATFORM_API_KEY (their config.toml pins it as env_key for the
        # doxie provider). Keep the two names in sync so a token refresh
        # propagates to codex too — otherwise the first codex platform turn
        # after login/refresh fails with "Missing environment variable".
        _apply_env_update("DOXIE_PLATFORM_API_KEY", runtime_token)
        env_updates["DOXIE_PLATFORM_API_KEY"] = runtime_token

    if origin_present:
        _apply_env_update("DOVIE_API_ORIGIN", api_origin)
        env_updates["DOVIE_API_ORIGIN"] = api_origin
        proxy_env = _managed_proxy_env(api_origin)
        env_updates.update(proxy_env)
        for key, value in proxy_env.items():
            _apply_env_update(key, value)

    if boundary_present:
        _apply_env_update(_LOCAL_OWNER_ENV, local_owner_id)
        _apply_env_update(
            _CREDENTIAL_BROKER_BOOTSTRAP_ENV,
            credential_broker_bootstrap_path,
        )
        env_updates[_LOCAL_OWNER_ENV] = local_owner_id
        env_updates[_CREDENTIAL_BROKER_BOOTSTRAP_ENV] = (
            credential_broker_bootstrap_path
        )

    resulting_token = os.environ.get("DOVIE_LLM_RUNTIME_TOKEN", "")
    resulting_origin = os.environ.get("DOVIE_API_ORIGIN", "")

    workers_notified = 0
    if env_updates:
        from hermes_agent.orchestration.worker_runtime import worker_supervisor

        workers_notified = await worker_supervisor().broadcast_runtime_env_update(env_updates)

    result = {
        "ok": True,
        "fingerprint": _fingerprint(resulting_token, resulting_origin),
        "workers_notified": workers_notified,
    }
    if boundary_present:
        result["credential_boundary_configured"] = bool(
            local_owner_id and credential_broker_bootstrap_path
        )
    return result


def _run_coro_sync(coro: Awaitable[_T]) -> _T:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    result_box: dict[str, Any] = {}

    def run() -> None:
        try:
            result_box["result"] = asyncio.run(coro)
        except BaseException as exc:  # pragma: no cover - re-raised by caller
            result_box["error"] = exc

    thread = threading.Thread(target=run, name="runtime-cloud-proxy-update", daemon=True)
    thread.start()
    thread.join()
    if "error" in result_box:
        raise result_box["error"]
    return result_box["result"]


@method("runtime.cloud_proxy.update")
def runtime_cloud_proxy_update(rid, params: dict) -> dict:
    try:
        result = _run_coro_sync(apply_runtime_cloud_proxy_update(params or {}))
    except Exception as exc:
        return _err(rid, -32000, f"runtime cloud proxy update failed: {exc}")
    return _ok(rid, result)
