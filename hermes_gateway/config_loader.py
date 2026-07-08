"""Gateway configuration loading, validation, and environment overrides."""

from __future__ import annotations

import json
import logging
import os

from hermes_cli.config import get_hermes_home

from .config_model import *  # noqa: F403 - preserves the legacy config module surface
from .config_model import (
    _BUILTIN_PLATFORM_VALUES,
    _coerce_bool,
    _ensure_platform_extra_dict,
    _normalize_notice_delivery,
    _normalize_unauthorized_dm_behavior,
)
from .config_validation import _apply_env_overrides, _validate_gateway_config

logger = logging.getLogger(__name__)

def load_gateway_config() -> GatewayConfig:
    """
    Load gateway configuration from multiple sources.

    Priority (highest to lowest):
    1. Environment variables
    2. ~/.hermes/config.yaml (primary user-facing config)
    3. ~/.hermes/gateway.json (legacy — provides defaults under config.yaml)
    4. Built-in defaults
    """
    _home = get_hermes_home()
    gw_data: dict = {}

    # Legacy fallback: gateway.json provides the base layer.
    # config.yaml keys always win when both specify the same setting.
    gateway_json_path = _home / "gateway.json"
    if gateway_json_path.exists():
        try:
            with open(gateway_json_path, "r", encoding="utf-8") as f:
                gw_data = json.load(f) or {}
            logger.info(
                "Loaded legacy %s — consider moving settings to config.yaml",
                gateway_json_path,
            )
        except Exception as e:
            logger.warning("Failed to load %s: %s", gateway_json_path, e)

    # Primary source: config.yaml
    try:
        import yaml
        config_yaml_path = _home / "config.yaml"
        if config_yaml_path.exists():
            with open(config_yaml_path, encoding="utf-8") as f:
                yaml_cfg = yaml.safe_load(f) or {}

            # Map config.yaml keys → GatewayConfig.from_dict() schema.
            # Each key overwrites whatever gateway.json may have set.
            sr = yaml_cfg.get("session_reset")
            if sr and isinstance(sr, dict):
                gw_data["default_reset_policy"] = sr

            qc = yaml_cfg.get("quick_commands")
            if qc is not None:
                if isinstance(qc, dict):
                    gw_data["quick_commands"] = qc
                else:
                    logger.warning(
                        "Ignoring invalid quick_commands in config.yaml "
                        "(expected mapping, got %s)",
                        type(qc).__name__,
                    )

            stt_cfg = yaml_cfg.get("stt")
            if isinstance(stt_cfg, dict):
                gw_data["stt"] = stt_cfg

            if "group_sessions_per_user" in yaml_cfg:
                gw_data["group_sessions_per_user"] = yaml_cfg["group_sessions_per_user"]

            if "thread_sessions_per_user" in yaml_cfg:
                gw_data["thread_sessions_per_user"] = yaml_cfg["thread_sessions_per_user"]

            streaming_cfg = yaml_cfg.get("streaming")
            if not isinstance(streaming_cfg, dict):
                # Fall back to nested gateway.streaming written by
                # ``hermes config set gateway.streaming.*``
                streaming_cfg = yaml_cfg.get("gateway", {}).get("streaming")
            if isinstance(streaming_cfg, dict):
                gw_data["streaming"] = streaming_cfg

            if "reset_triggers" in yaml_cfg:
                gw_data["reset_triggers"] = yaml_cfg["reset_triggers"]

            if "always_log_local" in yaml_cfg:
                gw_data["always_log_local"] = yaml_cfg["always_log_local"]

            if "unauthorized_dm_behavior" in yaml_cfg:
                gw_data["unauthorized_dm_behavior"] = _normalize_unauthorized_dm_behavior(
                    yaml_cfg.get("unauthorized_dm_behavior"),
                    "pair",
                )

            # Merge platforms section from config.yaml into gw_data so that
            # nested keys like platforms.webhook.extra.routes are loaded.
            yaml_platforms = yaml_cfg.get("platforms")
            platforms_data = gw_data.setdefault("platforms", {})
            if not isinstance(platforms_data, dict):
                platforms_data = {}
                gw_data["platforms"] = platforms_data
            if isinstance(yaml_platforms, dict):
                for plat_name, plat_block in yaml_platforms.items():
                    if not isinstance(plat_block, dict):
                        continue
                    existing = platforms_data.get(plat_name, {})
                    if not isinstance(existing, dict):
                        existing = {}
                    # Deep-merge extra dicts so gateway.json defaults survive
                    merged_extra = {**existing.get("extra", {}), **plat_block.get("extra", {})}
                    if plat_name == Platform.SLACK.value and "enabled" in plat_block:
                        merged_extra["_enabled_explicit"] = True
                    merged = {**existing, **plat_block}
                    if merged_extra:
                        merged["extra"] = merged_extra
                    platforms_data[plat_name] = merged
                gw_data["platforms"] = platforms_data
            # Iterate built-in platforms plus any registered plugin platforms
            # so plugin authors get the same shared-key bridging (#24836).
            try:
                from hermes_cli.plugins import discover_plugins
                discover_plugins()  # idempotent
                from channels.platform_registry import platform_registry as _pr
            except Exception as e:
                logger.debug("plugin discovery skipped: %s", e)
                _pr = None

            _shared_loop_targets: list = list(Platform)
            if _pr is not None:
                for _entry in _pr.plugin_entries():
                    try:
                        _plat = Platform(_entry.name)
                    except (ValueError, KeyError):
                        continue
                    if _plat not in _shared_loop_targets:
                        _shared_loop_targets.append(_plat)

            for plat in _shared_loop_targets:
                if plat == Platform.LOCAL:
                    continue
                platform_cfg = yaml_cfg.get(plat.value)
                if not isinstance(platform_cfg, dict):
                    continue
                # Collect bridgeable keys from this platform section
                bridged = {}
                if "unauthorized_dm_behavior" in platform_cfg:
                    bridged["unauthorized_dm_behavior"] = _normalize_unauthorized_dm_behavior(
                        platform_cfg.get("unauthorized_dm_behavior"),
                        gw_data.get("unauthorized_dm_behavior", "pair"),
                    )
                if "notice_delivery" in platform_cfg:
                    bridged["notice_delivery"] = _normalize_notice_delivery(
                        platform_cfg.get("notice_delivery"),
                        "public",
                    )
                if "reply_prefix" in platform_cfg:
                    bridged["reply_prefix"] = platform_cfg["reply_prefix"]
                if "reply_in_thread" in platform_cfg:
                    bridged["reply_in_thread"] = platform_cfg["reply_in_thread"]
                if "require_mention" in platform_cfg:
                    bridged["require_mention"] = platform_cfg["require_mention"]
                if plat == Platform.TELEGRAM and "allowed_chats" in platform_cfg:
                    bridged["allowed_chats"] = platform_cfg["allowed_chats"]
                if plat == Platform.TELEGRAM and "group_allowed_chats" in platform_cfg:
                    bridged["group_allowed_chats"] = platform_cfg["group_allowed_chats"]
                if plat == Platform.TELEGRAM and "allowed_topics" in platform_cfg:
                    bridged["allowed_topics"] = platform_cfg["allowed_topics"]
                if "free_response_channels" in platform_cfg:
                    bridged["free_response_channels"] = platform_cfg["free_response_channels"]
                if "mention_patterns" in platform_cfg:
                    bridged["mention_patterns"] = platform_cfg["mention_patterns"]
                if "exclusive_bot_mentions" in platform_cfg:
                    bridged["exclusive_bot_mentions"] = platform_cfg["exclusive_bot_mentions"]
                if plat == Platform.TELEGRAM and "observe_unmentioned_group_messages" in platform_cfg:
                    bridged["observe_unmentioned_group_messages"] = platform_cfg["observe_unmentioned_group_messages"]
                if "dm_policy" in platform_cfg:
                    bridged["dm_policy"] = platform_cfg["dm_policy"]
                if "allow_from" in platform_cfg:
                    bridged["allow_from"] = platform_cfg["allow_from"]
                if "allow_admin_from" in platform_cfg:
                    bridged["allow_admin_from"] = platform_cfg["allow_admin_from"]
                if "user_allowed_commands" in platform_cfg:
                    bridged["user_allowed_commands"] = platform_cfg["user_allowed_commands"]
                if "group_policy" in platform_cfg:
                    bridged["group_policy"] = platform_cfg["group_policy"]
                if "group_allow_from" in platform_cfg:
                    bridged["group_allow_from"] = platform_cfg["group_allow_from"]
                if "group_allow_admin_from" in platform_cfg:
                    bridged["group_allow_admin_from"] = platform_cfg["group_allow_admin_from"]
                if "group_user_allowed_commands" in platform_cfg:
                    bridged["group_user_allowed_commands"] = platform_cfg["group_user_allowed_commands"]
                if plat in {Platform.DISCORD, Platform.SLACK} and "channel_skill_bindings" in platform_cfg:
                    bridged["channel_skill_bindings"] = platform_cfg["channel_skill_bindings"]
                if "channel_prompts" in platform_cfg:
                    channel_prompts = platform_cfg["channel_prompts"]
                    if isinstance(channel_prompts, dict):
                        bridged["channel_prompts"] = {str(k): v for k, v in channel_prompts.items()}
                    else:
                        bridged["channel_prompts"] = channel_prompts
                if "gateway_restart_notification" in platform_cfg:
                    bridged["gateway_restart_notification"] = platform_cfg["gateway_restart_notification"]
                enabled_was_explicit = "enabled" in platform_cfg
                if not bridged and not enabled_was_explicit:
                    continue
                plat_data, extra = _ensure_platform_extra_dict(platforms_data, plat.value)
                if enabled_was_explicit:
                    plat_data["enabled"] = platform_cfg["enabled"]
                if plat == Platform.SLACK and enabled_was_explicit:
                    extra["_enabled_explicit"] = True
                extra.update(bridged)

            # Plugin-owned YAML→env config bridges (#24836).  See
            # ``PlatformEntry.apply_yaml_config_fn`` for the hook contract.
            # Order: shared-key loop (above) → this dispatch → legacy hardcoded
            # blocks (below; no-op when a hook already set their env var) →
            # ``_apply_env_overrides()`` after ``GatewayConfig.from_dict``.
            if _pr is not None:
                for entry in _pr.all_entries():
                    if entry.apply_yaml_config_fn is None:
                        continue
                    platform_cfg = yaml_cfg.get(entry.name)
                    if not isinstance(platform_cfg, dict):
                        continue
                    try:
                        seeded = entry.apply_yaml_config_fn(yaml_cfg, platform_cfg)
                    except Exception as e:
                        logger.debug(
                            "apply_yaml_config_fn for %s raised: %s",
                            entry.name, e,
                        )
                        continue
                    if not isinstance(seeded, dict) or not seeded:
                        continue
                    _, extra = _ensure_platform_extra_dict(platforms_data, entry.name)
                    extra.update(seeded)

            # Slack settings → env vars (env vars take precedence)
            slack_cfg = yaml_cfg.get("slack", {})
            if isinstance(slack_cfg, dict):
                if "require_mention" in slack_cfg and not os.getenv("SLACK_REQUIRE_MENTION"):
                    os.environ["SLACK_REQUIRE_MENTION"] = str(slack_cfg["require_mention"]).lower()
                if "strict_mention" in slack_cfg and not os.getenv("SLACK_STRICT_MENTION"):
                    os.environ["SLACK_STRICT_MENTION"] = str(slack_cfg["strict_mention"]).lower()
                if "allow_bots" in slack_cfg and not os.getenv("SLACK_ALLOW_BOTS"):
                    os.environ["SLACK_ALLOW_BOTS"] = str(slack_cfg["allow_bots"]).lower()
                frc = slack_cfg.get("free_response_channels")
                if frc is not None and not os.getenv("SLACK_FREE_RESPONSE_CHANNELS"):
                    if isinstance(frc, list):
                        frc = ",".join(str(v) for v in frc)
                    os.environ["SLACK_FREE_RESPONSE_CHANNELS"] = str(frc)
                if "reactions" in slack_cfg and not os.getenv("SLACK_REACTIONS"):
                    os.environ["SLACK_REACTIONS"] = str(slack_cfg["reactions"]).lower()
                # allowed_channels: if set, bot ONLY responds in these channels (whitelist)
                ac = slack_cfg.get("allowed_channels")
                if ac is not None and not os.getenv("SLACK_ALLOWED_CHANNELS"):
                    if isinstance(ac, list):
                        ac = ",".join(str(v) for v in ac)
                    os.environ["SLACK_ALLOWED_CHANNELS"] = str(ac)

            # Discord settings → env vars (env vars take precedence)
            discord_cfg = yaml_cfg.get("discord", {})
            if isinstance(discord_cfg, dict):
                if "require_mention" in discord_cfg and not os.getenv("DISCORD_REQUIRE_MENTION"):
                    os.environ["DISCORD_REQUIRE_MENTION"] = str(discord_cfg["require_mention"]).lower()
                if "thread_require_mention" in discord_cfg and not os.getenv("DISCORD_THREAD_REQUIRE_MENTION"):
                    os.environ["DISCORD_THREAD_REQUIRE_MENTION"] = str(discord_cfg["thread_require_mention"]).lower()
                frc = discord_cfg.get("free_response_channels")
                if frc is not None and not os.getenv("DISCORD_FREE_RESPONSE_CHANNELS"):
                    if isinstance(frc, list):
                        frc = ",".join(str(v) for v in frc)
                    os.environ["DISCORD_FREE_RESPONSE_CHANNELS"] = str(frc)
                if "auto_thread" in discord_cfg and not os.getenv("DISCORD_AUTO_THREAD"):
                    os.environ["DISCORD_AUTO_THREAD"] = str(discord_cfg["auto_thread"]).lower()
                if "reactions" in discord_cfg and not os.getenv("DISCORD_REACTIONS"):
                    os.environ["DISCORD_REACTIONS"] = str(discord_cfg["reactions"]).lower()
                # ignored_channels: channels where bot never responds (even when mentioned)
                ic = discord_cfg.get("ignored_channels")
                if ic is not None and not os.getenv("DISCORD_IGNORED_CHANNELS"):
                    if isinstance(ic, list):
                        ic = ",".join(str(v) for v in ic)
                    os.environ["DISCORD_IGNORED_CHANNELS"] = str(ic)
                # allowed_channels: if set, bot ONLY responds in these channels (whitelist)
                ac = discord_cfg.get("allowed_channels")
                if ac is not None and not os.getenv("DISCORD_ALLOWED_CHANNELS"):
                    if isinstance(ac, list):
                        ac = ",".join(str(v) for v in ac)
                    os.environ["DISCORD_ALLOWED_CHANNELS"] = str(ac)
                # no_thread_channels: channels where bot responds directly without creating thread
                ntc = discord_cfg.get("no_thread_channels")
                if ntc is not None and not os.getenv("DISCORD_NO_THREAD_CHANNELS"):
                    if isinstance(ntc, list):
                        ntc = ",".join(str(v) for v in ntc)
                    os.environ["DISCORD_NO_THREAD_CHANNELS"] = str(ntc)
                # history_backfill: recover missed channel messages for shared sessions
                # when require_mention is active.  Fetches messages between bot turns
                # and prepends them to the user message for context.
                if "history_backfill" in discord_cfg and not os.getenv("DISCORD_HISTORY_BACKFILL"):
                    os.environ["DISCORD_HISTORY_BACKFILL"] = str(discord_cfg["history_backfill"]).lower()
                hbl = discord_cfg.get("history_backfill_limit")
                if hbl is not None and not os.getenv("DISCORD_HISTORY_BACKFILL_LIMIT"):
                    os.environ["DISCORD_HISTORY_BACKFILL_LIMIT"] = str(hbl)
                # allow_mentions: granular control over what the bot can ping.
                # Safe defaults (no @everyone/roles) are applied in the adapter;
                # these YAML keys only override when set and let users opt back
                # into unsafe modes (e.g. roles=true) if they actually want it.
                allow_mentions_cfg = discord_cfg.get("allow_mentions")
                if isinstance(allow_mentions_cfg, dict):
                    for yaml_key, env_key in (
                        ("everyone", "DISCORD_ALLOW_MENTION_EVERYONE"),
                        ("roles", "DISCORD_ALLOW_MENTION_ROLES"),
                        ("users", "DISCORD_ALLOW_MENTION_USERS"),
                        ("replied_user", "DISCORD_ALLOW_MENTION_REPLIED_USER"),
                    ):
                        if yaml_key in allow_mentions_cfg and not os.getenv(env_key):
                            os.environ[env_key] = str(allow_mentions_cfg[yaml_key]).lower()
                # reply_to_mode: top-level preferred, falls back to extra.reply_to_mode
                # YAML 1.1 parses bare 'off' as boolean False — coerce to string "off".
                _discord_extra = discord_cfg.get("extra") if isinstance(discord_cfg.get("extra"), dict) else {}
                _discord_rtm = (
                    discord_cfg["reply_to_mode"] if "reply_to_mode" in discord_cfg
                    else _discord_extra.get("reply_to_mode")
                )
                if _discord_rtm is not None and not os.getenv("DISCORD_REPLY_TO_MODE"):
                    _rtm_str = "off" if _discord_rtm is False else str(_discord_rtm).lower()
                    os.environ["DISCORD_REPLY_TO_MODE"] = _rtm_str

            # Bridge top-level require_mention to Telegram when the telegram: section
            # does not already provide one.  Users often write "require_mention: true"
            # at the top level alongside group_sessions_per_user, expecting it to work
            # the same way (#3979).
            _tl_require_mention = yaml_cfg.get("require_mention")
            if _tl_require_mention is not None:
                _tg_section = yaml_cfg.get("telegram") or {}
                if "require_mention" not in _tg_section:
                    _tg_plat = platforms_data.setdefault(Platform.TELEGRAM.value, {})
                    _tg_extra = _tg_plat.setdefault("extra", {})
                    _tg_extra.setdefault("require_mention", _tl_require_mention)

            # Telegram settings → env vars (env vars take precedence)
            telegram_cfg = yaml_cfg.get("telegram", {})
            if isinstance(telegram_cfg, dict):
                # Bridge top-level legacy `telegram.disable_topic_auto_rename` into
                # channels.platforms.telegram.extra so the runtime config sees it.
                # Read as a runtime-config flag, not env-var (no need for env override).
                if "disable_topic_auto_rename" in telegram_cfg:
                    _tg_plat = platforms_data.setdefault(Platform.TELEGRAM.value, {})
                    _tg_extra = _tg_plat.setdefault("extra", {})
                    _tg_extra.setdefault(
                        "disable_topic_auto_rename",
                        telegram_cfg["disable_topic_auto_rename"],
                    )
                # Prefer telegram.require_mention; fall back to the top-level shorthand.
                _effective_rm = telegram_cfg.get("require_mention", yaml_cfg.get("require_mention"))
                if _effective_rm is not None and not os.getenv("TELEGRAM_REQUIRE_MENTION"):
                    os.environ["TELEGRAM_REQUIRE_MENTION"] = str(_effective_rm).lower()
                if "mention_patterns" in telegram_cfg and not os.getenv("TELEGRAM_MENTION_PATTERNS"):
                    os.environ["TELEGRAM_MENTION_PATTERNS"] = json.dumps(telegram_cfg["mention_patterns"])
                if "exclusive_bot_mentions" in telegram_cfg and not os.getenv("TELEGRAM_EXCLUSIVE_BOT_MENTIONS"):
                    os.environ["TELEGRAM_EXCLUSIVE_BOT_MENTIONS"] = str(telegram_cfg["exclusive_bot_mentions"]).lower()
                if "guest_mode" in telegram_cfg and not os.getenv("TELEGRAM_GUEST_MODE"):
                    os.environ["TELEGRAM_GUEST_MODE"] = str(telegram_cfg["guest_mode"]).lower()
                if "observe_unmentioned_group_messages" in telegram_cfg and not os.getenv("TELEGRAM_OBSERVE_UNMENTIONED_GROUP_MESSAGES"):
                    os.environ["TELEGRAM_OBSERVE_UNMENTIONED_GROUP_MESSAGES"] = str(telegram_cfg["observe_unmentioned_group_messages"]).lower()
                frc = telegram_cfg.get("free_response_chats")
                if frc is not None and not os.getenv("TELEGRAM_FREE_RESPONSE_CHATS"):
                    if isinstance(frc, list):
                        frc = ",".join(str(v) for v in frc)
                    os.environ["TELEGRAM_FREE_RESPONSE_CHATS"] = str(frc)
                # allowed_chats: if set, bot ONLY responds in these group chats (whitelist)
                ac = telegram_cfg.get("allowed_chats")
                if ac is not None and not os.getenv("TELEGRAM_ALLOWED_CHATS"):
                    if isinstance(ac, list):
                        ac = ",".join(str(v) for v in ac)
                    os.environ["TELEGRAM_ALLOWED_CHATS"] = str(ac)
                allowed_topics = telegram_cfg.get("allowed_topics")
                if allowed_topics is not None and not os.getenv("TELEGRAM_ALLOWED_TOPICS"):
                    if isinstance(allowed_topics, list):
                        allowed_topics = ",".join(str(v) for v in allowed_topics)
                    os.environ["TELEGRAM_ALLOWED_TOPICS"] = str(allowed_topics)
                ignored_threads = telegram_cfg.get("ignored_threads")
                if ignored_threads is not None and not os.getenv("TELEGRAM_IGNORED_THREADS"):
                    if isinstance(ignored_threads, list):
                        ignored_threads = ",".join(str(v) for v in ignored_threads)
                    os.environ["TELEGRAM_IGNORED_THREADS"] = str(ignored_threads)
                if "reactions" in telegram_cfg and not os.getenv("TELEGRAM_REACTIONS"):
                    os.environ["TELEGRAM_REACTIONS"] = str(telegram_cfg["reactions"]).lower()
                if "proxy_url" in telegram_cfg and not os.getenv("TELEGRAM_PROXY"):
                    os.environ["TELEGRAM_PROXY"] = str(telegram_cfg["proxy_url"]).strip()
                # reply_to_mode: top-level preferred, falls back to extra.reply_to_mode
                # YAML 1.1 parses bare 'off' as boolean False — coerce to string "off".
                _telegram_extra = telegram_cfg.get("extra") if isinstance(telegram_cfg.get("extra"), dict) else {}
                _telegram_rtm = (
                    telegram_cfg["reply_to_mode"] if "reply_to_mode" in telegram_cfg
                    else _telegram_extra.get("reply_to_mode")
                )
                if _telegram_rtm is not None and not os.getenv("TELEGRAM_REPLY_TO_MODE"):
                    _rtm_str = "off" if _telegram_rtm is False else str(_telegram_rtm).lower()
                    os.environ["TELEGRAM_REPLY_TO_MODE"] = _rtm_str
                allowed_users = telegram_cfg.get("allow_from")
                if allowed_users is not None and not os.getenv("TELEGRAM_ALLOWED_USERS"):
                    if isinstance(allowed_users, list):
                        allowed_users = ",".join(str(v) for v in allowed_users)
                    os.environ["TELEGRAM_ALLOWED_USERS"] = str(allowed_users)
                group_allowed_users = telegram_cfg.get("group_allow_from")
                if group_allowed_users is not None and not os.getenv("TELEGRAM_GROUP_ALLOWED_USERS"):
                    if isinstance(group_allowed_users, list):
                        group_allowed_users = ",".join(str(v) for v in group_allowed_users)
                    os.environ["TELEGRAM_GROUP_ALLOWED_USERS"] = str(group_allowed_users)
                group_allowed_chats = telegram_cfg.get("group_allowed_chats")
                if group_allowed_chats is not None and not os.getenv("TELEGRAM_GROUP_ALLOWED_CHATS"):
                    if isinstance(group_allowed_chats, list):
                        group_allowed_chats = ",".join(str(v) for v in group_allowed_chats)
                    os.environ["TELEGRAM_GROUP_ALLOWED_CHATS"] = str(group_allowed_chats)
                for _telegram_extra_key in ("guest_mode", "disable_link_previews", "observe_unmentioned_group_messages"):
                    if _telegram_extra_key in telegram_cfg:
                        plat_data = platforms_data.setdefault(Platform.TELEGRAM.value, {})
                        if not isinstance(plat_data, dict):
                            plat_data = {}
                            platforms_data[Platform.TELEGRAM.value] = plat_data
                        extra = plat_data.setdefault("extra", {})
                        if not isinstance(extra, dict):
                            extra = {}
                            plat_data["extra"] = extra
                        extra[_telegram_extra_key] = telegram_cfg[_telegram_extra_key]
                if _telegram_extra:
                    _plat_data, _plat_extra = _ensure_platform_extra_dict(
                        platforms_data, Platform.TELEGRAM.value
                    )
                    for _telegram_extra_key, _telegram_extra_value in _telegram_extra.items():
                        _plat_extra.setdefault(_telegram_extra_key, _telegram_extra_value)

            whatsapp_cfg = yaml_cfg.get("whatsapp", {})
            if isinstance(whatsapp_cfg, dict):
                if "require_mention" in whatsapp_cfg and not os.getenv("WHATSAPP_REQUIRE_MENTION"):
                    os.environ["WHATSAPP_REQUIRE_MENTION"] = str(whatsapp_cfg["require_mention"]).lower()
                if "mention_patterns" in whatsapp_cfg and not os.getenv("WHATSAPP_MENTION_PATTERNS"):
                    os.environ["WHATSAPP_MENTION_PATTERNS"] = json.dumps(whatsapp_cfg["mention_patterns"])
                frc = whatsapp_cfg.get("free_response_chats")
                if frc is not None and not os.getenv("WHATSAPP_FREE_RESPONSE_CHATS"):
                    if isinstance(frc, list):
                        frc = ",".join(str(v) for v in frc)
                    os.environ["WHATSAPP_FREE_RESPONSE_CHATS"] = str(frc)
                if "dm_policy" in whatsapp_cfg and not os.getenv("WHATSAPP_DM_POLICY"):
                    os.environ["WHATSAPP_DM_POLICY"] = str(whatsapp_cfg["dm_policy"]).lower()
                af = whatsapp_cfg.get("allow_from")
                if af is not None and not os.getenv("WHATSAPP_ALLOWED_USERS"):
                    if isinstance(af, list):
                        af = ",".join(str(v) for v in af)
                    os.environ["WHATSAPP_ALLOWED_USERS"] = str(af)
                if "group_policy" in whatsapp_cfg and not os.getenv("WHATSAPP_GROUP_POLICY"):
                    os.environ["WHATSAPP_GROUP_POLICY"] = str(whatsapp_cfg["group_policy"]).lower()
                gaf = whatsapp_cfg.get("group_allow_from")
                if gaf is not None and not os.getenv("WHATSAPP_GROUP_ALLOWED_USERS"):
                    if isinstance(gaf, list):
                        gaf = ",".join(str(v) for v in gaf)
                    os.environ["WHATSAPP_GROUP_ALLOWED_USERS"] = str(gaf)

            # Signal settings → env vars (env vars take precedence)
            signal_cfg = yaml_cfg.get("signal", {})
            if isinstance(signal_cfg, dict):
                if "require_mention" in signal_cfg and not os.getenv("SIGNAL_REQUIRE_MENTION"):
                    os.environ["SIGNAL_REQUIRE_MENTION"] = str(signal_cfg["require_mention"]).lower()

            # DingTalk settings → env vars (env vars take precedence)
            dingtalk_cfg = yaml_cfg.get("dingtalk", {})
            if isinstance(dingtalk_cfg, dict):
                if "require_mention" in dingtalk_cfg and not os.getenv("DINGTALK_REQUIRE_MENTION"):
                    os.environ["DINGTALK_REQUIRE_MENTION"] = str(dingtalk_cfg["require_mention"]).lower()
                if "mention_patterns" in dingtalk_cfg and not os.getenv("DINGTALK_MENTION_PATTERNS"):
                    os.environ["DINGTALK_MENTION_PATTERNS"] = json.dumps(dingtalk_cfg["mention_patterns"])
                frc = dingtalk_cfg.get("free_response_chats")
                if frc is not None and not os.getenv("DINGTALK_FREE_RESPONSE_CHATS"):
                    if isinstance(frc, list):
                        frc = ",".join(str(v) for v in frc)
                    os.environ["DINGTALK_FREE_RESPONSE_CHATS"] = str(frc)
                # allowed_chats: if set, bot ONLY responds in these group chats (whitelist)
                ac = dingtalk_cfg.get("allowed_chats")
                if ac is not None and not os.getenv("DINGTALK_ALLOWED_CHATS"):
                    if isinstance(ac, list):
                        ac = ",".join(str(v) for v in ac)
                    os.environ["DINGTALK_ALLOWED_CHATS"] = str(ac)
                allowed = dingtalk_cfg.get("allowed_users")
                if allowed is not None and not os.getenv("DINGTALK_ALLOWED_USERS"):
                    if isinstance(allowed, list):
                        allowed = ",".join(str(v) for v in allowed)
                    os.environ["DINGTALK_ALLOWED_USERS"] = str(allowed)

            # Mattermost settings → env vars (env vars take precedence)
            mattermost_cfg = yaml_cfg.get("mattermost", {})
            if isinstance(mattermost_cfg, dict):
                if "require_mention" in mattermost_cfg and not os.getenv("MATTERMOST_REQUIRE_MENTION"):
                    os.environ["MATTERMOST_REQUIRE_MENTION"] = str(mattermost_cfg["require_mention"]).lower()
                frc = mattermost_cfg.get("free_response_channels")
                if frc is not None and not os.getenv("MATTERMOST_FREE_RESPONSE_CHANNELS"):
                    if isinstance(frc, list):
                        frc = ",".join(str(v) for v in frc)
                    os.environ["MATTERMOST_FREE_RESPONSE_CHANNELS"] = str(frc)
                # allowed_channels: if set, bot ONLY responds in these channels (whitelist)
                ac = mattermost_cfg.get("allowed_channels")
                if ac is not None and not os.getenv("MATTERMOST_ALLOWED_CHANNELS"):
                    if isinstance(ac, list):
                        ac = ",".join(str(v) for v in ac)
                    os.environ["MATTERMOST_ALLOWED_CHANNELS"] = str(ac)

            # Matrix settings → env vars (env vars take precedence)
            matrix_cfg = yaml_cfg.get("matrix", {})
            if isinstance(matrix_cfg, dict):
                if "require_mention" in matrix_cfg and not os.getenv("MATRIX_REQUIRE_MENTION"):
                    os.environ["MATRIX_REQUIRE_MENTION"] = str(matrix_cfg["require_mention"]).lower()
                frc = matrix_cfg.get("free_response_rooms")
                if frc is not None and not os.getenv("MATRIX_FREE_RESPONSE_ROOMS"):
                    if isinstance(frc, list):
                        frc = ",".join(str(v) for v in frc)
                    os.environ["MATRIX_FREE_RESPONSE_ROOMS"] = str(frc)
                # allowed_rooms: if set, bot ONLY responds in these rooms (whitelist)
                ar = matrix_cfg.get("allowed_rooms")
                if ar is not None and not os.getenv("MATRIX_ALLOWED_ROOMS"):
                    if isinstance(ar, list):
                        ar = ",".join(str(v) for v in ar)
                    os.environ["MATRIX_ALLOWED_ROOMS"] = str(ar)
                if "auto_thread" in matrix_cfg and not os.getenv("MATRIX_AUTO_THREAD"):
                    os.environ["MATRIX_AUTO_THREAD"] = str(matrix_cfg["auto_thread"]).lower()
                if "dm_mention_threads" in matrix_cfg and not os.getenv("MATRIX_DM_MENTION_THREADS"):
                    os.environ["MATRIX_DM_MENTION_THREADS"] = str(matrix_cfg["dm_mention_threads"]).lower()

            # Feishu settings → env vars (env vars take precedence)
            feishu_cfg = yaml_cfg.get("feishu", {})
            if isinstance(feishu_cfg, dict):
                if "allow_bots" in feishu_cfg and not os.getenv("FEISHU_ALLOW_BOTS"):
                    os.environ["FEISHU_ALLOW_BOTS"] = str(feishu_cfg["allow_bots"]).lower()

    except Exception as e:
        logger.warning(
            "Failed to process config.yaml — falling back to .env / gateway.json values. "
            "Check %s for syntax errors. Error: %s",
            _home / "config.yaml",
            e,
        )

    config = GatewayConfig.from_dict(gw_data)

    # Override with environment variables
    _apply_env_overrides(config)
    
    # --- Validate loaded values ---
    _validate_gateway_config(config)

    return config
