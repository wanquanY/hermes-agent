# ruff: noqa: F401,F403,F405,F821,ARG001
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from tui_gateway.methods._shared import bind_server_globals

_server = bind_server_globals(globals())


def _jsonrpc_error(rid, code: int, message: str, *, data: dict | None = None) -> dict:
    error = {"code": code, "message": message}
    if data:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": rid, "error": error}


def _required_codex_home(params: dict | None) -> str:
    raw = str((params or {}).get("codex_home") or (params or {}).get("codexHome") or "").strip()
    if not raw:
        raise ValueError("codex_home is required")
    path = Path(raw)
    if not path.is_absolute():
        raise ValueError("codex_home must be an absolute path")
    return str(path)


def _expires_at_from_access_token(access_token: Any) -> str | None:
    if not isinstance(access_token, str) or not access_token.strip():
        return None
    try:
        from hermes_cli.auth import _decode_jwt_claims

        claims = _decode_jwt_claims(access_token)
    except Exception:
        return None
    exp = claims.get("exp")
    if not isinstance(exp, (int, float)):
        return None
    return datetime.fromtimestamp(float(exp), tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _account_last4(value: Any) -> str | None:
    text = str(value or "").strip()
    return text[-4:] if text else None


def _status_payload(codex_home: str) -> dict:
    from hermes_cli.auth import (
        _codex_access_token_is_expiring,
        _codex_home_auth_path,
        _load_codex_home_auth,
    )

    auth_path = _codex_home_auth_path(codex_home)
    if not auth_path.exists():
        return {
            "state": "missing",
            "auth_mode": None,
            "expires_at": None,
            "account_id": None,
        }
    payload = _load_codex_home_auth(codex_home)
    tokens = payload.get("tokens") if isinstance(payload, dict) else None
    access_token = tokens.get("access_token") if isinstance(tokens, dict) else None
    auth_mode = str((payload or {}).get("auth_mode") or "").strip().lower() or None
    if isinstance(access_token, str) and access_token.strip():
        return {
            "state": "expired" if _codex_access_token_is_expiring(access_token, 0) else "logged_in",
            "auth_mode": auth_mode or "chatgpt",
            "expires_at": _expires_at_from_access_token(access_token),
            "account_id": _account_last4((payload or {}).get("account_id") or tokens.get("account_id")),
        }
    api_key = str((payload or {}).get("api_key") or "").strip()
    if api_key:
        return {
            "state": "logged_in",
            "auth_mode": "api_key",
            "expires_at": None,
            "account_id": None,
        }
    return {
        "state": "missing",
        "auth_mode": None,
        "expires_at": None,
        "account_id": None,
    }


def _auth_error(rid, exc: Exception) -> dict:
    code = getattr(exc, "code", None) or exc.__class__.__name__
    return _jsonrpc_error(
        rid,
        4003,
        f"{code}: {exc}",
        data={"code": str(code)},
    )


@method("codex.auth.status")
def _(rid, params: dict) -> dict:
    try:
        codex_home = _required_codex_home(params)
    except ValueError as exc:
        return _err(rid, 4002, str(exc))
    return _ok(rid, _status_payload(codex_home))


@method("codex.auth.login.start")
def _(rid, params: dict) -> dict:
    try:
        _required_codex_home(params)
    except ValueError as exc:
        return _err(rid, 4002, str(exc))
    channel = str((params or {}).get("channel") or "device_code").strip()
    if channel != "device_code":
        return _err(rid, 4002, "channel must be device_code")
    try:
        from hermes_cli.auth import CODEX_OAUTH_CLIENT_ID, _codex_device_code_request

        payload = _codex_device_code_request(CODEX_OAUTH_CLIENT_ID)
    except Exception as exc:
        return _auth_error(rid, exc)
    return _ok(rid, payload)


@method("codex.auth.login.poll")
def _(rid, params: dict) -> dict:
    try:
        codex_home = _required_codex_home(params)
    except ValueError as exc:
        return _err(rid, 4002, str(exc))
    device_auth_id = str((params or {}).get("device_auth_id") or (params or {}).get("deviceAuthId") or "").strip()
    if not device_auth_id:
        return _err(rid, 4002, "device_auth_id is required")
    try:
        from hermes_cli.auth import (
            CODEX_OAUTH_CLIENT_ID,
            _codex_device_code_poll_once,
            _save_codex_tokens,
        )

        poll = _codex_device_code_poll_once(device_auth_id, CODEX_OAUTH_CLIENT_ID)
        if poll.get("state") == "pending":
            return _ok(rid, {"state": "pending"})
        _save_codex_tokens(
            poll["tokens"],
            poll.get("last_refresh"),
            codex_home=codex_home,
        )
        return _ok(
            rid,
            {
                "state": "logged_in",
                "auth_mode": "chatgpt",
                "expires_at": _expires_at_from_access_token(poll["tokens"].get("access_token")),
            },
        )
    except Exception as exc:
        return _auth_error(rid, exc)


@method("codex.auth.logout")
def _(rid, params: dict) -> dict:
    try:
        codex_home = _required_codex_home(params)
    except ValueError as exc:
        return _err(rid, 4002, str(exc))
    from hermes_cli.auth import _codex_home_auth_path

    auth_path = _codex_home_auth_path(codex_home)
    try:
        auth_path.unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:
        return _jsonrpc_error(rid, 5001, f"failed to remove Codex auth: {exc}")
    return _ok(rid, {"state": "missing"})


@method("codex.auth.import_cli")
def _(rid, params: dict) -> dict:
    try:
        codex_home = _required_codex_home(params)
    except ValueError as exc:
        return _err(rid, 4002, str(exc))
    try:
        from hermes_cli.auth import _import_codex_cli_tokens, _save_codex_tokens

        tokens = _import_codex_cli_tokens()
        if not tokens:
            return _ok(rid, {"state": "not_found"})
        _save_codex_tokens(tokens, codex_home=codex_home)
        return _ok(rid, {"state": "logged_in"})
    except Exception as exc:
        return _auth_error(rid, exc)
