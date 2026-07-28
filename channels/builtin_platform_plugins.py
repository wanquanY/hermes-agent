"""Plugin registrations for the canonical built-in channel adapters.

The channel implementations remain in :mod:`channels.platforms` while the
gateway migration is in progress.  This module gives every built-in messaging
surface the same manifest/registry lifecycle as third-party platforms without
copying adapter implementations or teaching the plugin loader about built-ins.

The gateway consults the platform registry before its compatibility factory,
so a discovered bundled plugin is the authoritative construction path.  The
legacy imports remain stable for integrations that import adapter classes
directly.
"""

from __future__ import annotations

import importlib
import os
from dataclasses import dataclass
from functools import partial
from typing import Any, Iterable


@dataclass(frozen=True)
class BuiltinPlatformPluginSpec:
    name: str
    label: str
    module: str
    adapter_class: str
    check_function: str
    required_env: tuple[str, ...] = ()
    allowed_users_env: str = ""
    allow_all_env: str = ""
    cron_deliver_env_var: str = ""
    install_hint: str = ""
    max_message_length: int = 0
    pii_safe: bool = False
    emoji: str = "🔌"
    apply_yaml_config_function: str = ""


_SPECS: dict[str, BuiltinPlatformPluginSpec] = {
    "dingtalk": BuiltinPlatformPluginSpec(
        "dingtalk", "DingTalk", "channels.platforms.dingtalk",
        "DingTalkAdapter", "check_dingtalk_requirements",
        ("DINGTALK_CLIENT_ID", "DINGTALK_CLIENT_SECRET"),
        "DINGTALK_ALLOWED_USERS", "DINGTALK_ALLOW_ALL_USERS",
        "DINGTALK_HOME_CHANNEL", "pip install 'dingtalk-stream>=0.20' httpx",
        emoji="🐳",
    ),
    "discord": BuiltinPlatformPluginSpec(
        "discord", "Discord", "channels.platforms.discord",
        "DiscordAdapter", "check_discord_requirements", ("DISCORD_BOT_TOKEN",),
        "DISCORD_ALLOWED_USERS", "DISCORD_ALLOW_ALL_USERS", "DISCORD_HOME_CHANNEL",
        "pip install 'hermes-agent[messaging]'", 2000, emoji="🎮",
    ),
    "email": BuiltinPlatformPluginSpec(
        "email", "Email", "channels.platforms.email",
        "EmailAdapter", "check_email_requirements",
        ("EMAIL_ADDRESS", "EMAIL_PASSWORD", "EMAIL_SMTP_HOST"),
        "EMAIL_ALLOWED_USERS", "EMAIL_ALLOW_ALL_USERS", "EMAIL_HOME_ADDRESS",
        "Email uses the Python standard library", 50_000, True, "📧",
    ),
    "feishu": BuiltinPlatformPluginSpec(
        "feishu", "Feishu / Lark", "channels.platforms.feishu",
        "FeishuAdapter", "check_feishu_requirements",
        ("FEISHU_APP_ID", "FEISHU_APP_SECRET"),
        "FEISHU_ALLOWED_USERS", "FEISHU_ALLOW_ALL_USERS", "FEISHU_HOME_CHANNEL",
        "pip install 'hermes-agent[feishu]'", 8000, emoji="🪽",
    ),
    "homeassistant": BuiltinPlatformPluginSpec(
        "homeassistant", "Home Assistant", "channels.platforms.homeassistant",
        "HomeAssistantAdapter", "check_ha_requirements", ("HASS_TOKEN",),
        install_hint="pip install aiohttp", max_message_length=16_000, emoji="🏠",
    ),
    "matrix": BuiltinPlatformPluginSpec(
        "matrix", "Matrix", "channels.platforms.matrix",
        "MatrixAdapter", "check_matrix_requirements",
        ("MATRIX_HOMESERVER", "MATRIX_ACCESS_TOKEN"),
        "MATRIX_ALLOWED_USERS", "MATRIX_ALLOW_ALL_USERS", "MATRIX_HOME_ROOM",
        "pip install 'mautrix[encryption]'", 16_000, emoji="🔐",
        apply_yaml_config_function="_apply_yaml_config",
    ),
    "mattermost": BuiltinPlatformPluginSpec(
        "mattermost", "Mattermost", "channels.platforms.mattermost",
        "MattermostAdapter", "check_mattermost_requirements",
        ("MATTERMOST_URL", "MATTERMOST_TOKEN"),
        "MATTERMOST_ALLOWED_USERS", "MATTERMOST_ALLOW_ALL_USERS",
        "MATTERMOST_HOME_CHANNEL", "pip install aiohttp", 4000, emoji="💬",
    ),
    "slack": BuiltinPlatformPluginSpec(
        "slack", "Slack", "channels.platforms.slack",
        "SlackAdapter", "check_slack_requirements",
        ("SLACK_BOT_TOKEN", "SLACK_APP_TOKEN"),
        "SLACK_ALLOWED_USERS", "SLACK_ALLOW_ALL_USERS", "SLACK_HOME_CHANNEL",
        "pip install 'hermes-agent[slack]'", 39_000, emoji="💼",
    ),
    "sms": BuiltinPlatformPluginSpec(
        "sms", "SMS (Twilio)", "channels.platforms.sms",
        "SmsAdapter", "check_sms_requirements",
        ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_PHONE_NUMBER"),
        "SMS_ALLOWED_USERS", "SMS_ALLOW_ALL_USERS", "SMS_HOME_CHANNEL",
        "pip install aiohttp", 1600, True, "📱",
    ),
    "telegram": BuiltinPlatformPluginSpec(
        "telegram", "Telegram", "channels.platforms.telegram",
        "TelegramAdapter", "check_telegram_requirements", ("TELEGRAM_BOT_TOKEN",),
        "TELEGRAM_ALLOWED_USERS", "TELEGRAM_ALLOW_ALL_USERS", "TELEGRAM_HOME_CHANNEL",
        "pip install 'hermes-agent[telegram]'", 4096, emoji="✈️",
    ),
    "wecom": BuiltinPlatformPluginSpec(
        "wecom", "WeCom (Enterprise WeChat)", "channels.platforms.wecom",
        "WeComAdapter", "check_wecom_requirements", ("WECOM_BOT_ID", "WECOM_SECRET"),
        "WECOM_ALLOWED_USERS", "WECOM_ALLOW_ALL_USERS", "WECOM_HOME_CHANNEL",
        "pip install 'hermes-agent[wecom]'", 4000, emoji="💼",
    ),
    "wecom_callback": BuiltinPlatformPluginSpec(
        "wecom_callback", "WeCom Callback (self-built apps)",
        "channels.platforms.wecom_callback", "WecomCallbackAdapter",
        "check_wecom_callback_requirements",
        ("WECOM_CALLBACK_CORP_ID", "WECOM_CALLBACK_CORP_SECRET"),
        "WECOM_CALLBACK_ALLOWED_USERS", "WECOM_CALLBACK_ALLOW_ALL_USERS",
        install_hint="pip install 'hermes-agent[wecom]'", emoji="💼",
    ),
    "whatsapp": BuiltinPlatformPluginSpec(
        "whatsapp", "WhatsApp", "channels.platforms.whatsapp",
        "WhatsAppAdapter", "check_whatsapp_requirements", ("WHATSAPP_ENABLED",),
        "WHATSAPP_ALLOWED_USERS", "WHATSAPP_ALLOW_ALL_USERS", "WHATSAPP_HOME_CHANNEL",
        "WhatsApp requires the Node.js bridge", 4096, emoji="💬",
    ),
}


def builtin_platform_plugin_specs() -> tuple[BuiltinPlatformPluginSpec, ...]:
    """Return stable, immutable metadata for bundled platform plugins."""
    return tuple(_SPECS.values())


def _configured(spec: BuiltinPlatformPluginSpec, config: Any) -> bool:
    token = str(getattr(config, "token", "") or "").strip()
    api_key = str(getattr(config, "api_key", "") or "").strip()
    extra = getattr(config, "extra", {}) or {}
    if token or api_key:
        return True
    if spec.name == "whatsapp":
        return True
    if spec.name == "email":
        return bool(extra.get("address") or os.getenv("EMAIL_ADDRESS"))
    if spec.name == "sms":
        return bool(os.getenv("TWILIO_ACCOUNT_SID"))
    if spec.name == "feishu":
        return bool(extra.get("app_id") or os.getenv("FEISHU_APP_ID"))
    if spec.name == "dingtalk":
        return bool(
            (extra.get("client_id") or os.getenv("DINGTALK_CLIENT_ID"))
            and (extra.get("client_secret") or os.getenv("DINGTALK_CLIENT_SECRET"))
        )
    if spec.name == "wecom":
        return bool(extra.get("bot_id") or os.getenv("WECOM_BOT_ID"))
    if spec.name == "wecom_callback":
        return bool(
            extra.get("corp_id")
            or extra.get("apps")
            or os.getenv("WECOM_CALLBACK_CORP_ID")
        )
    return all(bool(os.getenv(name)) for name in spec.required_env)


def _build_adapter(spec: BuiltinPlatformPluginSpec, config: Any) -> Any:
    module = importlib.import_module(spec.module)
    adapter = getattr(module, spec.adapter_class)(config)
    if spec.name == "telegram":
        try:
            from hermes_cli.config import load_config
            from hermes_gateway.platform_adapter_factory import (
                _resolve_telegram_notifications_mode,
            )

            adapter._notifications_mode = _resolve_telegram_notifications_mode(load_config)
        except Exception:
            # The adapter already owns a safe default; plugin construction must
            # not fail because an optional display preference is unreadable.
            pass
    return adapter


def _requirements_available(spec: BuiltinPlatformPluginSpec) -> bool:
    try:
        module = importlib.import_module(spec.module)
        return bool(getattr(module, spec.check_function)())
    except (ImportError, ModuleNotFoundError):
        return False


def register_builtin_platform(ctx: Any, name: str) -> None:
    """Register one canonical channel adapter through the Plugin API."""
    try:
        spec = _SPECS[name]
    except KeyError as exc:
        raise ValueError(f"Unknown built-in platform plugin: {name}") from exc
    apply_yaml_config_fn = None
    if spec.apply_yaml_config_function:
        module = importlib.import_module(spec.module)
        apply_yaml_config_fn = getattr(module, spec.apply_yaml_config_function)
    ctx.register_platform(
        name=spec.name,
        label=spec.label,
        adapter_factory=partial(_build_adapter, spec),
        check_fn=partial(_requirements_available, spec),
        is_connected=partial(_configured, spec),
        required_env=list(spec.required_env),
        install_hint=spec.install_hint,
        allowed_users_env=spec.allowed_users_env,
        allow_all_env=spec.allow_all_env,
        cron_deliver_env_var=spec.cron_deliver_env_var,
        max_message_length=spec.max_message_length,
        pii_safe=spec.pii_safe,
        emoji=spec.emoji,
        allow_update_command=True,
        apply_yaml_config_fn=apply_yaml_config_fn,
    )


def register_builtin_platforms(ctx: Any, names: Iterable[str]) -> None:
    """Register a related group, such as both WeCom transport modes."""
    for name in names:
        register_builtin_platform(ctx, name)


__all__ = [
    "BuiltinPlatformPluginSpec",
    "builtin_platform_plugin_specs",
    "register_builtin_platform",
    "register_builtin_platforms",
]
