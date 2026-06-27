# ruff: noqa: F401,F403,F405,F821,ARG001
from __future__ import annotations

import asyncio
import hashlib
import os
import threading
from typing import Any, Awaitable, TypeVar

from tui_gateway.methods._shared import bind_server_globals

_server = bind_server_globals(globals())

_T = TypeVar("_T")


def _has_any(params: dict[str, Any], keys: tuple[str, ...]) -> bool:
    return any(key in params for key in keys)


def _text_param(params: dict[str, Any], keys: tuple[str, ...]) -> str:
    for key in keys:
        if key in params:
            return str(params.get(key) or "").strip()
    return ""


def _fingerprint(runtime_token: str, api_origin: str) -> str:
    return hashlib.sha256(f"{runtime_token}|{api_origin}".encode()).hexdigest()[:12]


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
    api_origin = _text_param(params, origin_keys)

    env_updates: dict[str, str] = {}
    if token_present:
        if runtime_token:
            os.environ["DOVIE_LLM_RUNTIME_TOKEN"] = runtime_token
        else:
            os.environ.pop("DOVIE_LLM_RUNTIME_TOKEN", None)
        env_updates["DOVIE_LLM_RUNTIME_TOKEN"] = runtime_token

    if origin_present:
        if api_origin:
            os.environ["DOVIE_API_ORIGIN"] = api_origin
        else:
            os.environ.pop("DOVIE_API_ORIGIN", None)
        env_updates["DOVIE_API_ORIGIN"] = api_origin

    resulting_token = os.environ.get("DOVIE_LLM_RUNTIME_TOKEN", "")
    resulting_origin = os.environ.get("DOVIE_API_ORIGIN", "")

    workers_notified = 0
    if env_updates:
        from tui_gateway.services.worker_runtime import worker_supervisor

        workers_notified = await worker_supervisor().broadcast_runtime_env_update(env_updates)

    return {
        "ok": True,
        "fingerprint": _fingerprint(resulting_token, resulting_origin),
        "workers_notified": workers_notified,
    }


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
