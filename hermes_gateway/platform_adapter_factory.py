"""Platform adapter construction for the gateway runtime."""

from __future__ import annotations

import logging
import os
from typing import Any, Callable, Optional

from channels.platforms.base import BasePlatformAdapter
from hermes_cli.config import cfg_get
from hermes_gateway.config import Platform

logger = logging.getLogger(__name__)


UserConfigLoader = Callable[[], dict]


def create_platform_adapter(
    platform: Platform,
    config: Any,
    *,
    group_sessions_per_user: bool,
    thread_sessions_per_user: bool,
    gateway_runner: Any = None,
    load_user_config: Optional[UserConfigLoader] = None,
) -> Optional[BasePlatformAdapter]:
    """Create the adapter for a configured platform.

    The factory owns adapter selection and platform-specific construction.
    The runner remains responsible for lifecycle wiring: message handlers,
    fatal-error handlers, session store, connection, and shutdown.
    """
    if hasattr(config, "extra") and isinstance(config.extra, dict):
        config.extra.setdefault("group_sessions_per_user", group_sessions_per_user)
        config.extra.setdefault("thread_sessions_per_user", thread_sessions_per_user)

    plugin_adapter = _create_plugin_adapter(platform, config)
    if plugin_adapter is not _PLUGIN_MISS:
        return plugin_adapter

    if platform == Platform.TELEGRAM:
        return _create_telegram_adapter(config, load_user_config=load_user_config)

    if platform == Platform.DISCORD:
        from channels.platforms.discord import DiscordAdapter, check_discord_requirements

        if not check_discord_requirements():
            logger.warning("Discord: discord.py not installed")
            return None
        adapter = DiscordAdapter(config)
        adapter.gateway_runner = gateway_runner
        return adapter

    if platform == Platform.WHATSAPP:
        from channels.platforms.whatsapp import WhatsAppAdapter, check_whatsapp_requirements

        if not check_whatsapp_requirements():
            logger.warning("WhatsApp: Node.js not installed or bridge not configured")
            return None
        return WhatsAppAdapter(config)

    if platform == Platform.SLACK:
        from channels.platforms.slack import SlackAdapter, check_slack_requirements

        if not check_slack_requirements():
            logger.warning("Slack: slack-bolt not installed. Run: pip install 'hermes-agent[slack]'")
            return None
        return SlackAdapter(config)

    if platform == Platform.SIGNAL:
        from channels.platforms.signal import SignalAdapter, check_signal_requirements

        if not check_signal_requirements():
            logger.warning("Signal: SIGNAL_HTTP_URL or SIGNAL_ACCOUNT not configured")
            return None
        return SignalAdapter(config)

    if platform == Platform.HOMEASSISTANT:
        from channels.platforms.homeassistant import HomeAssistantAdapter, check_ha_requirements

        if not check_ha_requirements():
            logger.warning("HomeAssistant: aiohttp not installed or HASS_TOKEN not set")
            return None
        return HomeAssistantAdapter(config)

    if platform == Platform.EMAIL:
        from channels.platforms.email import EmailAdapter, check_email_requirements

        if not check_email_requirements():
            logger.warning("Email: EMAIL_ADDRESS, EMAIL_PASSWORD, EMAIL_IMAP_HOST, or EMAIL_SMTP_HOST not set")
            return None
        return EmailAdapter(config)

    if platform == Platform.SMS:
        from channels.platforms.sms import SmsAdapter, check_sms_requirements

        if not check_sms_requirements():
            logger.warning("SMS: aiohttp not installed or TWILIO_ACCOUNT_SID/TWILIO_AUTH_TOKEN not set")
            return None
        return SmsAdapter(config)

    if platform == Platform.DINGTALK:
        from channels.platforms.dingtalk import DingTalkAdapter, check_dingtalk_requirements

        if not check_dingtalk_requirements():
            logger.warning("DingTalk: dingtalk-stream not installed or DINGTALK_CLIENT_ID/SECRET not set")
            return None
        return DingTalkAdapter(config)

    if platform == Platform.FEISHU:
        from channels.platforms.feishu import FeishuAdapter, check_feishu_requirements

        if not check_feishu_requirements():
            logger.warning("Feishu: lark-oapi not installed or FEISHU_APP_ID/SECRET not set")
            return None
        return FeishuAdapter(config)

    if platform == Platform.WECOM_CALLBACK:
        from channels.platforms.wecom_callback import (
            WecomCallbackAdapter,
            check_wecom_callback_requirements,
        )

        if not check_wecom_callback_requirements():
            logger.warning("WeComCallback: aiohttp/httpx not installed")
            return None
        return WecomCallbackAdapter(config)

    if platform == Platform.WECOM:
        from channels.platforms.wecom import WeComAdapter, check_wecom_requirements

        if not check_wecom_requirements():
            logger.warning("WeCom: aiohttp not installed or WECOM_BOT_ID/SECRET not set")
            return None
        return WeComAdapter(config)

    if platform == Platform.WEIXIN:
        from channels.platforms.weixin import WeixinAdapter, check_weixin_requirements

        if not check_weixin_requirements():
            logger.warning("Weixin: aiohttp/cryptography not installed")
            return None
        return WeixinAdapter(config)

    if platform == Platform.MATTERMOST:
        from channels.platforms.mattermost import MattermostAdapter, check_mattermost_requirements

        if not check_mattermost_requirements():
            logger.warning("Mattermost: MATTERMOST_TOKEN or MATTERMOST_URL not set, or aiohttp missing")
            return None
        return MattermostAdapter(config)

    if platform == Platform.MATRIX:
        from channels.platforms.matrix import MatrixAdapter, check_matrix_requirements

        if not check_matrix_requirements():
            logger.warning("Matrix: mautrix not installed or credentials not set. Run: pip install 'mautrix[encryption]'")
            return None
        return MatrixAdapter(config)

    if platform == Platform.API_SERVER:
        from channels.platforms.api_server import APIServerAdapter, check_api_server_requirements

        if not check_api_server_requirements():
            logger.warning("API Server: aiohttp not installed")
            return None
        return APIServerAdapter(config)

    if platform == Platform.WEBHOOK:
        from channels.platforms.webhook import WebhookAdapter, check_webhook_requirements

        if not check_webhook_requirements():
            logger.warning("Webhook: aiohttp not installed")
            return None
        adapter = WebhookAdapter(config)
        adapter.gateway_runner = gateway_runner
        return adapter

    if platform == Platform.MSGRAPH_WEBHOOK:
        from channels.platforms.msgraph_webhook import (
            MSGraphWebhookAdapter,
            check_msgraph_webhook_requirements,
        )

        if not check_msgraph_webhook_requirements():
            logger.warning("MSGraph webhook: aiohttp not installed")
            return None
        return MSGraphWebhookAdapter(config)

    if platform == Platform.BLUEBUBBLES:
        from channels.platforms.bluebubbles import BlueBubblesAdapter, check_bluebubbles_requirements

        if not check_bluebubbles_requirements():
            logger.warning("BlueBubbles: aiohttp/httpx missing or BLUEBUBBLES_SERVER_URL/BLUEBUBBLES_PASSWORD not configured")
            return None
        return BlueBubblesAdapter(config)

    if platform == Platform.QQBOT:
        from channels.platforms.qqbot import QQAdapter, check_qq_requirements

        if not check_qq_requirements():
            logger.warning("QQBot: aiohttp/httpx missing or QQ_APP_ID/QQ_CLIENT_SECRET not configured")
            return None
        return QQAdapter(config)

    if platform == Platform.YUANBAO:
        from channels.platforms.yuanbao import YuanbaoAdapter, WEBSOCKETS_AVAILABLE

        if not WEBSOCKETS_AVAILABLE:
            logger.warning("Yuanbao: websockets not installed. Run: pip install websockets")
            return None
        return YuanbaoAdapter(config)

    return None


class _PluginMiss:
    pass


_PLUGIN_MISS = _PluginMiss()


def _create_plugin_adapter(platform: Platform, config: Any) -> Optional[BasePlatformAdapter] | _PluginMiss:
    try:
        from channels.platform_registry import platform_registry

        if platform_registry.is_registered(platform.value):
            adapter = platform_registry.create_adapter(platform.value, config)
            if adapter is not None:
                return adapter
            logger.error(
                "Platform '%s' is registered but adapter creation failed "
                "(check dependencies and config)",
                platform.value,
            )
            return None
    except Exception as exc:
        logger.debug("Platform registry lookup for '%s' failed: %s", platform.value, exc)
    return _PLUGIN_MISS


def _create_telegram_adapter(
    config: Any,
    *,
    load_user_config: Optional[UserConfigLoader],
) -> Optional[BasePlatformAdapter]:
    from channels.platforms.telegram import TelegramAdapter, check_telegram_requirements

    if not check_telegram_requirements():
        logger.warning("Telegram: python-telegram-bot not installed")
        return None
    adapter = TelegramAdapter(config)
    adapter._notifications_mode = _resolve_telegram_notifications_mode(load_user_config)
    return adapter


def _resolve_telegram_notifications_mode(load_user_config: Optional[UserConfigLoader]) -> str:
    notify_mode = os.getenv("HERMES_TELEGRAM_NOTIFICATIONS", "")
    if not notify_mode and load_user_config is not None:
        try:
            raw = cfg_get(
                load_user_config(),
                "display",
                "platforms",
                "telegram",
                "notifications",
            )
            if raw not in {None, ""}:
                notify_mode = str(raw).strip().lower()
        except Exception:
            logger.debug("Could not read telegram notification mode from config", exc_info=True)
    notify_mode = notify_mode or "important"
    if notify_mode not in {"all", "important"}:
        logger.warning(
            "Unknown telegram notifications mode '%s', "
            "defaulting to 'important' (valid: all, important)",
            notify_mode,
        )
        notify_mode = "important"
    return notify_mode
