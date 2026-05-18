from __future__ import annotations

import os
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from hermes_constants import get_hermes_home

MASKED_SECRET = "********"

_QR_PLATFORMS = {"dingtalk", "feishu", "qqbot", "weixin"}
_PAIRING_PLATFORMS = {
    "bluebubbles",
    "discord",
    "feishu",
    "mattermost",
    "qqbot",
    "slack",
    "telegram",
    "wecom",
    "weixin",
}
_MULTI_ACCOUNT_PLATFORMS = {"feishu"}
_CHANNEL_DIRECTORY_PLATFORMS = {
    "discord",
    "feishu",
    "mattermost",
    "matrix",
    "slack",
    "telegram",
    "wecom",
    "weixin",
}
_QR_SESSIONS: dict[str, dict[str, Any]] = {}
_QR_SESSION_MAX_AGE_SECONDS = 7200


def platform_catalog(load_cfg: Callable[[], dict]) -> dict[str, Any]:
    cfg = load_cfg()
    runtime = _read_runtime_status()
    runtime_platforms = runtime.get("platforms") if isinstance(runtime, dict) else {}
    if not isinstance(runtime_platforms, dict):
        runtime_platforms = {}

    platforms = []
    for index, platform in enumerate(_all_platforms()):
        key = _platform_key(platform)
        if not key:
            continue
        config = _platform_config(cfg, key)
        runtime_state = runtime_platforms.get(key) if isinstance(runtime_platforms.get(key), dict) else {}
        enabled = _bool(config.get("enabled"), default=False)
        configured_status = _plain_platform_status(platform)
        status = _normalize_platform_status(
            configured_status=configured_status,
            enabled=enabled,
            runtime_state=str(runtime_state.get("state") or ""),
            error_message=str(runtime_state.get("error_message") or ""),
        )
        platforms.append(
            {
                "id": key,
                "label": str(platform.get("label") or key),
                "description": _platform_description(platform),
                "source": "plugin" if platform.get("_registry_entry") else "builtin",
                "order": index * 10,
                "installed": True,
                "enabled": enabled,
                "status": status,
                "statusText": _status_text(status),
                "connectionText": _connection_text(status, configured_status, runtime_state),
                "capabilities": _capabilities(key),
                "setupInstructions": list(platform.get("setup_instructions") or []),
                "error": _runtime_error(runtime_state),
            }
        )

    return {
        "gateway": _gateway_summary(runtime),
        "platforms": platforms,
    }


def platform_status(load_cfg: Callable[[], dict]) -> dict[str, Any]:
    catalog = platform_catalog(load_cfg)
    return {
        "gateway": catalog["gateway"],
        "platforms": [
            {
                "id": item["id"],
                "enabled": item["enabled"],
                "status": item["status"],
                "statusText": item["statusText"],
                "connectionText": item["connectionText"],
                "error": item.get("error"),
            }
            for item in catalog["platforms"]
        ],
    }


def platform_schema(platform_id: str, load_cfg: Callable[[], dict]) -> dict[str, Any]:
    key = _normalize_platform_id(platform_id)
    platform = _find_platform(key)
    if platform is None:
        raise ValueError(f"unknown platform: {platform_id}")

    cfg = load_cfg()
    config = _platform_config(cfg, key)
    fields: list[dict[str, Any]] = [
        {
            "key": "enabled",
            "label": "启用平台",
            "kind": "boolean",
            "required": False,
            "sensitive": False,
            "help": "启用后 Hermes Gateway 会在下次刷新或重启时加载该平台。",
        }
    ]
    value: dict[str, Any] = {"enabled": _bool(config.get("enabled"), default=False)}

    for var in platform.get("vars") or []:
        if not isinstance(var, dict):
            continue
        name = str(var.get("name") or "").strip()
        if not name:
            continue
        sensitive = bool(var.get("password"))
        fields.append(
            {
                "key": name,
                "label": str(var.get("prompt") or name),
                "kind": _field_kind(var),
                "required": name == str(platform.get("token_var") or ""),
                "sensitive": sensitive,
                "placeholder": "",
                "help": str(var.get("help") or ""),
            }
        )
        current = _get_env_value(name)
        value[name] = MASKED_SECRET if sensitive and current else current

    fields.extend(_generic_config_fields(config))
    value.update(_generic_config_values(config))

    return {
        "platform": key,
        "fields": fields,
        "value": value,
        "advancedValue": config,
    }


def patch_platform_config(
    platform_id: str,
    patch: dict[str, Any],
    load_cfg: Callable[[], dict],
    save_cfg: Callable[[dict], None],
) -> dict[str, Any]:
    key = _normalize_platform_id(platform_id)
    if _find_platform(key) is None:
        raise ValueError(f"unknown platform: {platform_id}")
    if not isinstance(patch, dict):
        raise ValueError("config patch must be an object")

    cfg = load_cfg()
    platforms = cfg.setdefault("platforms", {})
    if not isinstance(platforms, dict):
        platforms = {}
        cfg["platforms"] = platforms
    platform_cfg = platforms.setdefault(key, {})
    if not isinstance(platform_cfg, dict):
        platform_cfg = {}
        platforms[key] = platform_cfg

    env_updates: dict[str, str] = {}
    config_changed = False

    for raw_key, raw_value in patch.items():
        field_key = str(raw_key or "").strip()
        if not field_key:
            continue
        if field_key.isupper():
            if raw_value == MASKED_SECRET:
                continue
            env_updates[field_key] = "" if raw_value is None else str(raw_value)
            continue
        if field_key == "enabled":
            platform_cfg["enabled"] = _bool(raw_value, default=False)
            config_changed = True
        elif field_key == "reply_to_mode":
            platform_cfg["reply_to_mode"] = str(raw_value or "first")
            config_changed = True
        elif field_key == "gateway_restart_notification":
            platform_cfg["gateway_restart_notification"] = _bool(raw_value, default=True)
            config_changed = True
        elif field_key == "home_channel":
            platform_cfg["home_channel"] = _coerce_home_channel(key, raw_value)
            config_changed = True
        elif field_key == "extra":
            platform_cfg["extra"] = raw_value if isinstance(raw_value, dict) else {}
            config_changed = True

    if config_changed:
        save_cfg(cfg)
    for name, value in env_updates.items():
        _save_env_value(name, value)

    return {
        "platform": key,
        "saved": True,
        "envUpdated": sorted(env_updates),
        "configUpdated": config_changed,
        "restartRequired": True,
    }


def connect_platform(
    platform_id: str,
    load_cfg: Callable[[], dict],
    save_cfg: Callable[[dict], None],
) -> dict[str, Any]:
    return patch_platform_config(platform_id, {"enabled": True}, load_cfg, save_cfg)


def start_gateway_runtime(load_cfg: Callable[[], dict], *, force_restart: bool = False) -> dict[str, Any]:
    hermes_home = get_hermes_home()
    cfg = load_cfg()
    enabled_platforms = [
        key
        for key, value in (cfg.get("platforms") or {}).items()
        if isinstance(value, dict) and _bool(value.get("enabled"), default=False)
    ]
    if not enabled_platforms:
        raise ValueError("no enabled messaging platform")

    pid = _running_gateway_pid()
    if pid is not None and not force_restart:
        return {
            "running": True,
            "started": False,
            "pid": pid,
            "platforms": sorted(enabled_platforms),
            "logPath": str(_gateway_runtime_log_path()),
            "hermesHome": str(hermes_home),
        }

    log_path = _gateway_runtime_log_path()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["HERMES_HOME"] = str(hermes_home)
    env["DOXIE_MANAGED_HERMES_GATEWAY"] = "1"

    with open(log_path, "a", encoding="utf-8") as log:
        log.write(f"\n[{time.strftime('%Y-%m-%dT%H:%M:%S%z')}] starting Hermes messaging gateway\n")
        log.write(f"HERMES_HOME={hermes_home}\n")
        log.flush()
        cmd = [sys.executable, "-m", "hermes_cli.main", "gateway", "run", "-v", "--replace"]
        process = subprocess.Popen(
            cmd,
            cwd=os.getcwd(),
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )

    time.sleep(0.25)
    code = process.poll()
    if code is not None:
        raise RuntimeError(f"Hermes messaging gateway exited during startup with code {code}; see {log_path}")

    return {
        "running": True,
        "started": True,
        "pid": process.pid,
        "replacedPid": pid,
        "platforms": sorted(enabled_platforms),
        "logPath": str(log_path),
        "hermesHome": str(hermes_home),
    }


def disconnect_platform(
    platform_id: str,
    load_cfg: Callable[[], dict],
    save_cfg: Callable[[dict], None],
) -> dict[str, Any]:
    return patch_platform_config(platform_id, {"enabled": False}, load_cfg, save_cfg)


def start_qr_login(platform_id: str) -> dict[str, Any]:
    key = _normalize_platform_id(platform_id)
    if key not in _QR_PLATFORMS:
        raise ValueError(f"platform does not support QR login: {platform_id}")

    now = time.time()
    _prune_qr_sessions(now)
    flow_id = uuid.uuid4().hex

    if key == "dingtalk":
        session = _start_dingtalk_qr()
    elif key == "feishu":
        session = _start_feishu_qr()
    elif key == "qqbot":
        session = _start_qqbot_qr()
    elif key == "weixin":
        session = _start_weixin_qr()
    else:
        raise ValueError(f"QR login is not implemented for platform: {platform_id}")

    expires_at = float(session.get("expires_at") or now + _QR_SESSION_MAX_AGE_SECONDS)
    session.update({
        "flow_id": flow_id,
        "platform": key,
        "created_at": now,
        "expires_at": expires_at,
    })
    _QR_SESSIONS[flow_id] = session

    return {
        "platform": key,
        "flowId": flow_id,
        "status": "pending",
        "qrUrl": session.get("qr_url") or "",
        "qrData": session.get("qr_data") or session.get("qr_url") or "",
        "expiresAt": expires_at,
        "intervalSeconds": int(session.get("interval") or 3),
    }


def poll_qr_login(
    platform_id: str,
    flow_id: str,
    load_cfg: Callable[[], dict],
    save_cfg: Callable[[dict], None],
) -> dict[str, Any]:
    key = _normalize_platform_id(platform_id)
    session = _QR_SESSIONS.get(str(flow_id or ""))
    if not session or session.get("platform") != key:
        raise ValueError("QR login flow was not found or has expired")

    now = time.time()
    if now > float(session.get("expires_at") or 0):
        _QR_SESSIONS.pop(str(flow_id), None)
        return {"platform": key, "flowId": flow_id, "status": "expired"}

    if key == "dingtalk":
        result = _poll_dingtalk_qr(session, load_cfg, save_cfg)
    elif key == "feishu":
        result = _poll_feishu_qr(session, load_cfg, save_cfg)
    elif key == "qqbot":
        result = _poll_qqbot_qr(session, load_cfg, save_cfg)
    elif key == "weixin":
        result = _poll_weixin_qr(session, load_cfg, save_cfg)
    else:
        raise ValueError(f"QR login is not implemented for platform: {platform_id}")

    result = {"platform": key, "flowId": flow_id, **result}
    if result.get("status") in {"confirmed", "expired", "failed"}:
        _QR_SESSIONS.pop(str(flow_id), None)
    return result


def platform_pairings(platform_id: str | None = None) -> dict[str, Any]:
    store = _pairing_store()
    key = _normalize_platform_id(platform_id) if platform_id else None
    return {
        "pending": store.list_pending(key),
        "approved": store.list_approved(key),
    }


def approve_pairing(platform_id: str, code: str) -> dict[str, Any]:
    key = _normalize_platform_id(platform_id)
    normalized_code = str(code or "").strip()
    if not key or not normalized_code:
        raise ValueError("platform and code are required")
    result = _pairing_store().approve_code(key, normalized_code)
    if not result:
        raise ValueError("pairing code is invalid, expired, or locked out")
    return {"platform": key, "approved": True, **result}


def revoke_pairing(platform_id: str, user_id: str) -> dict[str, Any]:
    key = _normalize_platform_id(platform_id)
    normalized_user = str(user_id or "").strip()
    if not key or not normalized_user:
        raise ValueError("platform and user_id are required")
    revoked = _pairing_store().revoke(key, normalized_user)
    return {"platform": key, "user_id": normalized_user, "revoked": bool(revoked)}


def channel_directory(platform_id: str | None = None) -> dict[str, Any]:
    try:
        from gateway.channel_directory import DIRECTORY_PATH

        import json

        if not DIRECTORY_PATH.exists():
            data = {"updated_at": None, "platforms": {}}
        else:
            data = json.loads(DIRECTORY_PATH.read_text(encoding="utf-8"))
    except Exception:
        data = {"updated_at": None, "platforms": {}}

    platforms = data.get("platforms") if isinstance(data, dict) else {}
    if not isinstance(platforms, dict):
        platforms = {}
    if platform_id:
        key = _normalize_platform_id(platform_id)
        platforms = {key: platforms.get(key, [])}
    return {
        "updated_at": data.get("updated_at") if isinstance(data, dict) else None,
        "platforms": platforms,
    }


def platform_accounts(platform_id: str, load_cfg: Callable[[], dict]) -> dict[str, Any]:
    key = _normalize_platform_id(platform_id)
    cfg = load_cfg()
    config = _platform_config(cfg, key)
    accounts: list[dict[str, Any]] = []

    if key == "feishu":
        app_id = _get_env_value("FEISHU_APP_ID")
        if app_id:
            accounts.append(
                {
                    "accountId": app_id,
                    "label": _get_env_value("FEISHU_BOT_NAME") or "Feishu Bot",
                    "status": "configured",
                    "platform": key,
                }
            )
    elif key == "wecom":
        bot_id = _get_env_value("WECOM_BOT_ID")
        if bot_id:
            accounts.append(
                {
                    "accountId": bot_id,
                    "label": "WeCom Bot",
                    "status": "configured",
                    "platform": key,
                }
            )
    elif key == "weixin":
        account_id = _get_env_value("WEIXIN_ACCOUNT_ID")
        if account_id:
            accounts.append(
                {
                    "accountId": account_id,
                    "label": _get_env_value("WEIXIN_USER_ID") or "Weixin",
                    "status": "configured",
                    "platform": key,
                }
            )

    home = config.get("home_channel")
    return {"platform": key, "accounts": accounts, "homeChannel": home}


def _start_dingtalk_qr() -> dict[str, Any]:
    from hermes_cli.dingtalk_auth import begin_registration

    registration = begin_registration()
    expires_in = int(registration.get("expires_in") or 7200)
    return {
        "device_code": registration["device_code"],
        "qr_url": registration["verification_uri_complete"],
        "qr_data": registration["verification_uri_complete"],
        "interval": max(int(registration.get("interval") or 3), 2),
        "expires_at": time.time() + expires_in,
    }


def _poll_dingtalk_qr(
    session: dict[str, Any],
    load_cfg: Callable[[], dict],
    save_cfg: Callable[[dict], None],
) -> dict[str, Any]:
    from hermes_cli.dingtalk_auth import poll_registration

    result = poll_registration(str(session.get("device_code") or ""))
    status = str(result.get("status") or "").upper()
    if status == "WAITING":
        return {"status": "pending"}
    if status == "SUCCESS":
        client_id = str(result.get("client_id") or "").strip()
        client_secret = str(result.get("client_secret") or "").strip()
        if not client_id or not client_secret:
            return {"status": "failed", "message": "authorization succeeded but credentials are missing"}
        _save_env_value("DINGTALK_CLIENT_ID", client_id)
        _save_env_value("DINGTALK_CLIENT_SECRET", client_secret)
        _save_env_value("DINGTALK_ALLOW_ALL_USERS", "true")
        connect_platform("dingtalk", load_cfg, save_cfg)
        return {
            "status": "confirmed",
            "credentials": {"client_id": client_id},
            "account": {"accountId": client_id, "label": "钉钉"},
        }
    if status == "EXPIRED":
        return {"status": "expired"}
    return {"status": "failed", "message": str(result.get("fail_reason") or status or "authorization failed")}


def _start_feishu_qr() -> dict[str, Any]:
    from gateway.platforms import feishu

    domain = "feishu"
    feishu._init_registration(domain)
    registration = feishu._begin_registration(domain)
    expires_in = int(registration.get("expire_in") or 600)
    return {
        "device_code": registration["device_code"],
        "domain": domain,
        "domain_switched": False,
        "qr_url": registration["qr_url"],
        "qr_data": registration["qr_url"],
        "interval": int(registration.get("interval") or 5),
        "expires_at": time.time() + expires_in,
    }


def _poll_feishu_qr(
    session: dict[str, Any],
    load_cfg: Callable[[], dict],
    save_cfg: Callable[[dict], None],
) -> dict[str, Any]:
    from gateway.platforms import feishu

    current_domain = str(session.get("domain") or "feishu")
    base_url = feishu._accounts_base_url(current_domain)
    response = feishu._post_registration(base_url, {
        "action": "poll",
        "device_code": str(session.get("device_code") or ""),
        "tp": "ob_app",
    })

    user_info = response.get("user_info") or {}
    tenant_brand = user_info.get("tenant_brand")
    if tenant_brand == "lark" and not session.get("domain_switched"):
        current_domain = "lark"
        session["domain"] = current_domain
        session["domain_switched"] = True

    app_id = str(response.get("client_id") or "").strip()
    app_secret = str(response.get("client_secret") or "").strip()
    if app_id and app_secret:
        owner_user_id = str(
            user_info.get("open_id")
            or user_info.get("user_id")
            or user_info.get("union_id")
            or ""
        ).strip()
        bot_name = ""
        try:
            bot_info = feishu.probe_bot(app_id, app_secret, current_domain)
            bot_name = str((bot_info or {}).get("bot_name") or "")
        except Exception:
            bot_name = ""
        _save_env_value("FEISHU_APP_ID", app_id)
        _save_env_value("FEISHU_APP_SECRET", app_secret)
        _save_env_value("FEISHU_DOMAIN", current_domain)
        _save_env_value("FEISHU_CONNECTION_MODE", "websocket")
        _save_env_value("FEISHU_ALLOW_ALL_USERS", "false")
        _save_env_value("FEISHU_ALLOWED_USERS", owner_user_id)
        _save_env_value("FEISHU_GROUP_POLICY", "open")
        connect_platform("feishu", load_cfg, save_cfg)
        return {
            "status": "confirmed",
            "credentials": {"app_id": app_id, "domain": current_domain},
            "account": {"accountId": app_id, "label": bot_name or "飞书"},
        }

    error = str(response.get("error") or "").strip()
    if error in {"access_denied", "expired_token"}:
        return {"status": "expired" if error == "expired_token" else "failed", "message": error}
    return {"status": "pending"}


def _start_qqbot_qr() -> dict[str, Any]:
    from gateway.platforms.qqbot import onboard

    task_id, aes_key = onboard._create_bind_task()
    url = onboard.build_connect_url(task_id)
    return {
        "task_id": task_id,
        "aes_key": aes_key,
        "qr_url": url,
        "qr_data": url,
        "interval": int(getattr(onboard, "ONBOARD_POLL_INTERVAL", 3) or 3),
        "expires_at": time.time() + 600,
    }


def _poll_qqbot_qr(
    session: dict[str, Any],
    load_cfg: Callable[[], dict],
    save_cfg: Callable[[dict], None],
) -> dict[str, Any]:
    from gateway.platforms.qqbot import onboard

    status, app_id, encrypted_secret, user_openid = onboard._poll_bind_result(str(session.get("task_id") or ""))
    if status == onboard.BindStatus.PENDING or status == onboard.BindStatus.NONE:
        return {"status": "pending"}
    if status == onboard.BindStatus.EXPIRED:
        return {"status": "expired"}
    if status == onboard.BindStatus.COMPLETED:
        client_secret = onboard.decrypt_secret(encrypted_secret, str(session.get("aes_key") or ""))
        _save_env_value("QQ_APP_ID", app_id)
        _save_env_value("QQ_CLIENT_SECRET", client_secret)
        _save_env_value("QQ_ALLOW_ALL_USERS", "false")
        _save_env_value("QQ_ALLOWED_USERS", user_openid or "")
        if user_openid:
            _save_env_value("QQBOT_HOME_CHANNEL", user_openid)
        connect_platform("qqbot", load_cfg, save_cfg)
        return {
            "status": "confirmed",
            "credentials": {"app_id": app_id, "user_openid": user_openid},
            "account": {"accountId": app_id, "label": user_openid or "QQ 机器人"},
        }
    return {"status": "failed", "message": f"unknown QQ Bot status: {status}"}


def _start_weixin_qr() -> dict[str, Any]:
    import asyncio

    async def _fetch() -> dict[str, Any]:
        from gateway.platforms import weixin

        if not weixin.AIOHTTP_AVAILABLE:
            raise RuntimeError("aiohttp is required for Weixin QR login")
        async with weixin.aiohttp.ClientSession(
            trust_env=True,
            connector=weixin._make_ssl_connector(),
        ) as session:
            return await weixin._api_get(
                session,
                base_url=weixin.ILINK_BASE_URL,
                endpoint=f"{weixin.EP_GET_BOT_QR}?bot_type=3",
                timeout_ms=weixin.QR_TIMEOUT_MS,
            )

    response = asyncio.run(_fetch())
    qrcode_value = str(response.get("qrcode") or "").strip()
    qrcode_url = str(response.get("qrcode_img_content") or "").strip()
    if not qrcode_value:
        raise RuntimeError("Weixin QR response missing qrcode")
    return {
        "qrcode": qrcode_value,
        "base_url": "",
        "qr_url": qrcode_url or qrcode_value,
        "qr_data": qrcode_url or qrcode_value,
        "interval": 2,
        "expires_at": time.time() + 480,
    }


def _poll_weixin_qr(
    session: dict[str, Any],
    load_cfg: Callable[[], dict],
    save_cfg: Callable[[dict], None],
) -> dict[str, Any]:
    import asyncio

    async def _fetch() -> dict[str, Any]:
        from gateway.platforms import weixin

        async with weixin.aiohttp.ClientSession(
            trust_env=True,
            connector=weixin._make_ssl_connector(),
        ) as client:
            return await weixin._api_get(
                client,
                base_url=str(session.get("base_url") or weixin.ILINK_BASE_URL),
                endpoint=f"{weixin.EP_GET_QR_STATUS}?qrcode={session.get('qrcode')}",
                timeout_ms=weixin.QR_TIMEOUT_MS,
            )

    from gateway.platforms import weixin
    from hermes_constants import get_hermes_home

    response = asyncio.run(_fetch())
    status = str(response.get("status") or "wait")
    if status == "wait":
        return {"status": "pending"}
    if status == "scaned":
        return {"status": "scanned", "message": "已扫码，请在微信里确认"}
    if status == "scaned_but_redirect":
        redirect_host = str(response.get("redirect_host") or "").strip()
        if redirect_host:
            session["base_url"] = f"https://{redirect_host}"
        return {"status": "scanned", "message": "已扫码，请在微信里确认"}
    if status == "expired":
        return {"status": "expired"}
    if status == "confirmed":
        account_id = str(response.get("ilink_bot_id") or "").strip()
        token = str(response.get("bot_token") or "").strip()
        base_url = str(response.get("baseurl") or weixin.ILINK_BASE_URL).strip()
        user_id = str(response.get("ilink_user_id") or "").strip()
        if not account_id or not token:
            return {"status": "failed", "message": "微信确认成功但凭据不完整"}
        weixin.save_weixin_account(
            str(get_hermes_home()),
            account_id=account_id,
            token=token,
            base_url=base_url,
            user_id=user_id,
        )
        _save_env_value("WEIXIN_ACCOUNT_ID", account_id)
        _save_env_value("WEIXIN_TOKEN", token)
        _save_env_value("WEIXIN_BASE_URL", base_url)
        _save_env_value("WEIXIN_CDN_BASE_URL", _get_env_value("WEIXIN_CDN_BASE_URL") or weixin.WEIXIN_CDN_BASE_URL)
        _save_env_value("WEIXIN_DM_POLICY", "pairing")
        _save_env_value("WEIXIN_ALLOW_ALL_USERS", "false")
        _save_env_value("WEIXIN_ALLOWED_USERS", user_id)
        _save_env_value("WEIXIN_GROUP_POLICY", "disabled")
        _save_env_value("WEIXIN_GROUP_ALLOWED_USERS", "")
        if user_id:
            _save_env_value("WEIXIN_HOME_CHANNEL", user_id)
        connect_platform("weixin", load_cfg, save_cfg)
        return {
            "status": "confirmed",
            "credentials": {"account_id": account_id, "user_id": user_id},
            "account": {"accountId": account_id, "label": user_id or "微信"},
        }
    return {"status": "failed", "message": f"未知微信授权状态: {status}"}


def _prune_qr_sessions(now: float | None = None) -> None:
    current = time.time() if now is None else now
    expired = [
        flow_id
        for flow_id, session in _QR_SESSIONS.items()
        if current - float(session.get("created_at") or 0) > _QR_SESSION_MAX_AGE_SECONDS
        or current > float(session.get("expires_at") or 0)
    ]
    for flow_id in expired:
        _QR_SESSIONS.pop(flow_id, None)


def _all_platforms() -> list[dict[str, Any]]:
    try:
        from hermes_cli.gateway import _all_platforms as all_platforms

        return [p for p in all_platforms() if isinstance(p, dict)]
    except Exception:
        return []


def _find_platform(platform_id: str) -> dict[str, Any] | None:
    key = _normalize_platform_id(platform_id)
    for platform in _all_platforms():
        if _platform_key(platform) == key:
            return platform
    return None


def _plain_platform_status(platform: dict[str, Any]) -> str:
    try:
        from hermes_cli.gateway import _platform_status

        return str(_platform_status(platform) or "not configured")
    except Exception:
        return "not configured"


def _read_runtime_status() -> dict[str, Any]:
    try:
        from gateway.status import read_runtime_status

        state = read_runtime_status()
        return state if isinstance(state, dict) else {}
    except Exception:
        return {}


def _running_gateway_pid() -> int | None:
    try:
        from gateway.status import get_running_pid

        return get_running_pid(cleanup_stale=True)
    except Exception:
        return None


def _gateway_runtime_log_path() -> Path:
    try:
        from hermes_cli.config import get_hermes_home

        home = Path(get_hermes_home())
    except Exception:
        home = Path(os.getenv("HERMES_HOME") or Path.home() / ".hermes")
    return home / "logs" / "doxie-messaging-gateway.log"


def _pairing_store():
    from gateway.pairing import PairingStore

    return PairingStore()


def _get_env_value(name: str) -> str:
    try:
        from hermes_cli.config import get_env_value

        return str(get_env_value(name) or "")
    except Exception:
        return str(os.environ.get(name, "") or "")


def _save_env_value(name: str, value: str) -> None:
    from hermes_cli.config import save_env_value

    save_env_value(name, value)


def _platform_key(platform: dict[str, Any]) -> str:
    return _normalize_platform_id(str(platform.get("key") or ""))


def _normalize_platform_id(value: str | None) -> str:
    return str(value or "").strip().lower().replace(" ", "_")


def _platform_config(cfg: dict[str, Any], platform_id: str) -> dict[str, Any]:
    platforms = cfg.get("platforms") if isinstance(cfg, dict) else {}
    if not isinstance(platforms, dict):
        return {}
    config = platforms.get(platform_id)
    return config if isinstance(config, dict) else {}


def _bool(value: Any, *, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off"}:
            return False
        return default
    return bool(value)


def _normalize_platform_status(
    *,
    configured_status: str,
    enabled: bool,
    runtime_state: str,
    error_message: str,
) -> str:
    normalized_runtime = runtime_state.strip().lower()
    if error_message or normalized_runtime in {"fatal", "error"}:
        return "error"
    if normalized_runtime in {"connected", "ready", "running"}:
        return "connected"
    if normalized_runtime in {"starting", "connecting", "initializing"}:
        return "connecting"
    if normalized_runtime in {"degraded", "backoff"}:
        return "degraded"

    normalized = configured_status.strip().lower()
    if not enabled and normalized in {"configured", "configured + paired", "partially configured"}:
        return "disabled"
    if "not configured" in normalized:
        return "not_configured"
    if "not paired" in normalized:
        return "configured"
    if "partial" in normalized:
        return "configured"
    if "configured" in normalized:
        return "configured"
    return "not_configured"


def _status_text(status: str) -> str:
    return {
        "not_configured": "未配置",
        "needs_install": "需安装",
        "configured": "已配置",
        "connecting": "连接中",
        "connected": "已连接",
        "degraded": "恢复中",
        "error": "异常",
        "disabled": "已停用",
    }.get(status, "未知")


def _connection_text(status: str, configured_status: str, runtime_state: dict[str, Any]) -> str:
    message = str(runtime_state.get("error_message") or "").strip()
    if message:
        return message
    if status == "connected":
        return "平台监听正在运行"
    if status == "connecting":
        return "Hermes Gateway 正在连接该平台"
    if status == "configured":
        return "配置已保存，等待 Gateway 加载或重启"
    if status == "disabled":
        return "配置存在，但平台当前未启用"
    if status == "degraded":
        return "连接暂时中断，Hermes 会继续恢复"
    if configured_status and configured_status != "not configured":
        return configured_status
    return "填写配置后即可连接"


def _platform_description(platform: dict[str, Any]) -> str:
    instructions = platform.get("setup_instructions")
    if isinstance(instructions, list) and instructions:
        return str(instructions[0]).lstrip("0123456789. ").strip()
    token_var = str(platform.get("token_var") or "").strip()
    if token_var:
        return f"通过 {token_var} 配置 Hermes Gateway 远程入口"
    return "Hermes Gateway 消息平台入口"


def _capabilities(platform_id: str) -> dict[str, bool]:
    return {
        "config": True,
        "qrLogin": platform_id in _QR_PLATFORMS,
        "pairing": platform_id in _PAIRING_PLATFORMS,
        "accounts": platform_id in _MULTI_ACCOUNT_PLATFORMS or platform_id in {"wecom", "weixin"},
        "channelDirectory": platform_id in _CHANNEL_DIRECTORY_PLATFORMS,
        "install": False,
        "multipleAccounts": platform_id in _MULTI_ACCOUNT_PLATFORMS,
    }


def _runtime_error(runtime_state: dict[str, Any]) -> dict[str, Any] | None:
    message = str(runtime_state.get("error_message") or "").strip()
    if not message:
        return None
    return {
        "code": runtime_state.get("error_code"),
        "message": message,
        "updatedAt": runtime_state.get("updated_at"),
    }


def _gateway_summary(runtime: dict[str, Any]) -> dict[str, Any]:
    return {
        "state": runtime.get("gateway_state") or "unknown",
        "activeAgents": runtime.get("active_agents") or 0,
        "restartRequested": bool(runtime.get("restart_requested")),
        "exitReason": runtime.get("exit_reason"),
        "updatedAt": runtime.get("updated_at"),
    }


def _field_kind(var: dict[str, Any]) -> str:
    if var.get("is_allowlist"):
        return "array"
    return "string"


def _generic_config_fields(config: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "key": "reply_to_mode",
            "label": "回复模式",
            "kind": "enum",
            "required": False,
            "sensitive": False,
            "options": [
                {"label": "默认", "value": "first"},
                {"label": "关闭线程", "value": "off"},
                {"label": "全部线程回复", "value": "all"},
            ],
            "help": "支持线程回复的平台会使用该设置。",
        },
        {
            "key": "gateway_restart_notification",
            "label": "Gateway 重启通知",
            "kind": "boolean",
            "required": False,
            "sensitive": False,
            "help": "控制该平台是否接收 Gateway 上线或重启通知。",
        },
    ] if config else []


def _generic_config_values(config: dict[str, Any]) -> dict[str, Any]:
    if not config:
        return {}
    return {
        "reply_to_mode": config.get("reply_to_mode", "first"),
        "gateway_restart_notification": _bool(
            config.get("gateway_restart_notification"),
            default=True,
        ),
    }


def _coerce_home_channel(platform_id: str, raw_value: Any) -> dict[str, Any] | None:
    if raw_value in {None, ""}:
        return None
    if isinstance(raw_value, dict):
        chat_id = str(raw_value.get("chat_id") or raw_value.get("id") or "").strip()
        name = str(raw_value.get("name") or chat_id or "Home")
        thread_id = str(raw_value.get("thread_id") or "").strip()
        if not chat_id:
            return None
        result = {"platform": platform_id, "chat_id": chat_id, "name": name}
        if thread_id:
            result["thread_id"] = thread_id
        return result
    chat_id = str(raw_value).strip()
    if not chat_id:
        return None
    return {"platform": platform_id, "chat_id": chat_id, "name": "Home"}
