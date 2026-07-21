"""Gateway configuration loading, validation, and environment overrides."""

from __future__ import annotations

import json
import logging

from agent.secret_scope import get_profile_env
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


_PLATFORM_PROFILE_EXTRA_KEYS: dict[Platform, tuple[str, ...]] = {
    Platform.SLACK: (
        "strict_mention",
        "allow_bots",
        "reactions",
        "allowed_channels",
    ),
    Platform.DISCORD: (
        "thread_require_mention",
        "free_response_channels",
        "auto_thread",
        "reactions",
        "ignored_channels",
        "allowed_channels",
        "no_thread_channels",
        "history_backfill",
        "history_backfill_limit",
        "allow_mentions",
    ),
    Platform.TELEGRAM: (
        "disable_topic_auto_rename",
        "mention_patterns",
        "exclusive_bot_mentions",
        "guest_mode",
        "observe_unmentioned_group_messages",
        "free_response_chats",
        "free_response_topics",
        "allowed_chats",
        "allowed_topics",
        "ignored_threads",
        "reactions",
        "proxy_url",
        "disable_link_previews",
        "group_allowed_chats",
    ),
    Platform.WHATSAPP: (
        "mention_patterns",
        "free_response_chats",
    ),
    Platform.DINGTALK: (
        "mention_patterns",
        "free_response_chats",
        "allowed_chats",
        "allowed_users",
    ),
    Platform.MATTERMOST: (
        "free_response_channels",
        "allowed_channels",
    ),
    Platform.MATRIX: (
        "free_response_rooms",
        "allowed_rooms",
        "auto_thread",
        "dm_mention_threads",
    ),
    Platform.FEISHU: ("allow_bots",),
}

_PROFILE_EXTRA_ENV_OVERRIDES: dict[tuple[Platform, str], str] = {
    (Platform.SLACK, "strict_mention"): "SLACK_STRICT_MENTION",
    (Platform.SLACK, "allow_bots"): "SLACK_ALLOW_BOTS",
    (Platform.SLACK, "reactions"): "SLACK_REACTIONS",
    (Platform.SLACK, "allowed_channels"): "SLACK_ALLOWED_CHANNELS",
    (Platform.DISCORD, "thread_require_mention"): "DISCORD_THREAD_REQUIRE_MENTION",
    (Platform.DISCORD, "free_response_channels"): "DISCORD_FREE_RESPONSE_CHANNELS",
    (Platform.DISCORD, "auto_thread"): "DISCORD_AUTO_THREAD",
    (Platform.DISCORD, "reactions"): "DISCORD_REACTIONS",
    (Platform.DISCORD, "ignored_channels"): "DISCORD_IGNORED_CHANNELS",
    (Platform.DISCORD, "allowed_channels"): "DISCORD_ALLOWED_CHANNELS",
    (Platform.DISCORD, "no_thread_channels"): "DISCORD_NO_THREAD_CHANNELS",
    (Platform.DISCORD, "history_backfill"): "DISCORD_HISTORY_BACKFILL",
    (Platform.DISCORD, "history_backfill_limit"): "DISCORD_HISTORY_BACKFILL_LIMIT",
    (
        Platform.DISCORD,
        "websocket_liveness_interval_seconds",
    ): "HERMES_DISCORD_LIVENESS_INTERVAL_SECONDS",
    (
        Platform.DISCORD,
        "websocket_liveness_failure_threshold",
    ): "HERMES_DISCORD_LIVENESS_FAILURE_THRESHOLD",
    (Platform.TELEGRAM, "mention_patterns"): "TELEGRAM_MENTION_PATTERNS",
    (
        Platform.TELEGRAM,
        "exclusive_bot_mentions",
    ): "TELEGRAM_EXCLUSIVE_BOT_MENTIONS",
    (Platform.TELEGRAM, "guest_mode"): "TELEGRAM_GUEST_MODE",
    (
        Platform.TELEGRAM,
        "observe_unmentioned_group_messages",
    ): "TELEGRAM_OBSERVE_UNMENTIONED_GROUP_MESSAGES",
    (Platform.TELEGRAM, "free_response_chats"): "TELEGRAM_FREE_RESPONSE_CHATS",
    (Platform.TELEGRAM, "free_response_topics"): "TELEGRAM_FREE_RESPONSE_TOPICS",
    (Platform.TELEGRAM, "allowed_chats"): "TELEGRAM_ALLOWED_CHATS",
    (Platform.TELEGRAM, "allowed_topics"): "TELEGRAM_ALLOWED_TOPICS",
    (Platform.TELEGRAM, "ignored_threads"): "TELEGRAM_IGNORED_THREADS",
    (Platform.TELEGRAM, "reactions"): "TELEGRAM_REACTIONS",
    (Platform.TELEGRAM, "proxy_url"): "TELEGRAM_PROXY",
    (Platform.TELEGRAM, "group_allowed_chats"): "TELEGRAM_GROUP_ALLOWED_CHATS",
    (Platform.WHATSAPP, "mention_patterns"): "WHATSAPP_MENTION_PATTERNS",
    (Platform.WHATSAPP, "free_response_chats"): "WHATSAPP_FREE_RESPONSE_CHATS",
    (Platform.DINGTALK, "mention_patterns"): "DINGTALK_MENTION_PATTERNS",
    (Platform.DINGTALK, "free_response_chats"): "DINGTALK_FREE_RESPONSE_CHATS",
    (Platform.DINGTALK, "allowed_chats"): "DINGTALK_ALLOWED_CHATS",
    (Platform.DINGTALK, "allowed_users"): "DINGTALK_ALLOWED_USERS",
    (
        Platform.MATTERMOST,
        "free_response_channels",
    ): "MATTERMOST_FREE_RESPONSE_CHANNELS",
    (Platform.MATTERMOST, "allowed_channels"): "MATTERMOST_ALLOWED_CHANNELS",
    (Platform.MATRIX, "free_response_rooms"): "MATRIX_FREE_RESPONSE_ROOMS",
    (Platform.MATRIX, "allowed_rooms"): "MATRIX_ALLOWED_ROOMS",
    (Platform.MATRIX, "auto_thread"): "MATRIX_AUTO_THREAD",
    (Platform.MATRIX, "dm_mention_threads"): "MATRIX_DM_MENTION_THREADS",
    (Platform.FEISHU, "allow_bots"): "FEISHU_ALLOW_BOTS",
}


def _profile_extra_env_name(platform: Platform, key: str) -> str | None:
    explicit = _PROFILE_EXTRA_ENV_OVERRIDES.get((platform, key))
    if explicit:
        return explicit
    prefix = platform.value.upper()
    common_suffixes = {
        "require_mention": "REQUIRE_MENTION",
        "dm_policy": "DM_POLICY",
        "allow_from": "ALLOWED_USERS",
        "group_policy": "GROUP_POLICY",
        "group_allow_from": "GROUP_ALLOWED_USERS",
    }
    suffix = common_suffixes.get(key)
    return f"{prefix}_{suffix}" if suffix else None

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

            gateway_cfg = yaml_cfg.get("gateway")
            if not isinstance(gateway_cfg, dict):
                gateway_cfg = {}

            def top_level_or_gateway(key: str):
                """Return top-level legacy value, otherwise canonical gateway value."""
                return yaml_cfg[key] if key in yaml_cfg else gateway_cfg.get(key)

            # Map config.yaml keys → GatewayConfig.from_dict() schema.
            # Key presence at the legacy top level wins over the canonical
            # gateway.* form, including explicitly empty or malformed values.
            sr = top_level_or_gateway("session_reset")
            if sr and isinstance(sr, dict):
                gw_data["default_reset_policy"] = sr

            qc = top_level_or_gateway("quick_commands")
            if qc is not None:
                if isinstance(qc, dict):
                    gw_data["quick_commands"] = qc
                else:
                    logger.warning(
                        "Ignoring invalid quick_commands in config.yaml "
                        "(expected mapping, got %s)",
                        type(qc).__name__,
                    )

            stt_cfg = top_level_or_gateway("stt")
            if isinstance(stt_cfg, dict):
                gw_data["stt"] = stt_cfg
            stt_echo = top_level_or_gateway("stt_echo_transcripts")
            if stt_echo is not None:
                gw_data["stt_echo_transcripts"] = stt_echo

            group_sessions = top_level_or_gateway("group_sessions_per_user")
            if group_sessions is not None:
                gw_data["group_sessions_per_user"] = group_sessions

            thread_sessions = top_level_or_gateway("thread_sessions_per_user")
            if thread_sessions is not None:
                gw_data["thread_sessions_per_user"] = thread_sessions

            if "multiplex_profiles" in yaml_cfg:
                gw_data["multiplex_profiles"] = yaml_cfg["multiplex_profiles"]
            elif "multiplex_profiles" in gateway_cfg:
                gw_data["multiplex_profiles"] = gateway_cfg["multiplex_profiles"]
            profile_routes = yaml_cfg.get("profile_routes")
            if profile_routes is None:
                profile_routes = gateway_cfg.get("profile_routes")
            if isinstance(profile_routes, list):
                gw_data["profile_routes"] = profile_routes

            streaming_cfg = top_level_or_gateway("streaming")
            if isinstance(streaming_cfg, dict):
                gw_data["streaming"] = streaming_cfg

            reset_triggers = top_level_or_gateway("reset_triggers")
            if reset_triggers is not None:
                gw_data["reset_triggers"] = reset_triggers

            always_log_local = top_level_or_gateway("always_log_local")
            if always_log_local is not None:
                gw_data["always_log_local"] = always_log_local

            unauthorized_dm_behavior = top_level_or_gateway("unauthorized_dm_behavior")
            if unauthorized_dm_behavior is not None:
                gw_data["unauthorized_dm_behavior"] = _normalize_unauthorized_dm_behavior(
                    unauthorized_dm_behavior,
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
                declared_extra = platform_cfg.get("extra")
                bridged = (
                    dict(declared_extra) if isinstance(declared_extra, dict) else {}
                )
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
                for key in _PLATFORM_PROFILE_EXTRA_KEYS.get(plat, ()):
                    if key in platform_cfg:
                        value = platform_cfg[key]
                        bridged[key] = dict(value) if isinstance(value, dict) else value
                if plat in {Platform.DISCORD, Platform.SLACK} and "channel_skill_bindings" in platform_cfg:
                    bridged["channel_skill_bindings"] = platform_cfg["channel_skill_bindings"]
                if plat == Platform.DISCORD:
                    discord_extra = (
                        platform_cfg.get("extra")
                        if isinstance(platform_cfg.get("extra"), dict)
                        else {}
                    )
                    for discord_key in (
                        "bots_require_inline_mention",
                        "missed_message_backfill",
                        "websocket_heartbeat_ack_max_age_seconds",
                        "websocket_max_latency_seconds",
                    ):
                        if discord_key in platform_cfg or discord_key in discord_extra:
                            value = platform_cfg.get(
                                discord_key,
                                discord_extra.get(discord_key),
                            )
                            bridged[discord_key] = (
                                dict(value) if isinstance(value, dict) else value
                            )
                    for primary_key, legacy_key in (
                        (
                            "websocket_liveness_interval_seconds",
                            "liveness_interval_seconds",
                        ),
                        (
                            "websocket_liveness_failure_threshold",
                            "liveness_failure_threshold",
                        ),
                    ):
                        value = platform_cfg.get(primary_key)
                        if value is None:
                            value = discord_extra.get(primary_key)
                        if value is None:
                            value = platform_cfg.get(legacy_key)
                        if value is None:
                            value = discord_extra.get(legacy_key)
                        if value is not None:
                            bridged[primary_key] = value
                if "channel_prompts" in platform_cfg:
                    channel_prompts = platform_cfg["channel_prompts"]
                    if isinstance(channel_prompts, dict):
                        bridged["channel_prompts"] = {str(k): v for k, v in channel_prompts.items()}
                    else:
                        bridged["channel_prompts"] = channel_prompts
                if "gateway_restart_notification" in platform_cfg:
                    bridged["gateway_restart_notification"] = platform_cfg["gateway_restart_notification"]
                enabled_was_explicit = "enabled" in platform_cfg
                reply_to_mode = platform_cfg.get("reply_to_mode")
                if reply_to_mode is None:
                    platform_extra = platform_cfg.get("extra")
                    if isinstance(platform_extra, dict):
                        reply_to_mode = platform_extra.get("reply_to_mode")
                if not bridged and not enabled_was_explicit and reply_to_mode is None:
                    continue
                plat_data, extra = _ensure_platform_extra_dict(platforms_data, plat.value)
                if enabled_was_explicit:
                    plat_data["enabled"] = platform_cfg["enabled"]
                if plat == Platform.SLACK and enabled_was_explicit:
                    extra["_enabled_explicit"] = True
                if reply_to_mode is not None:
                    plat_data["reply_to_mode"] = (
                        "off" if reply_to_mode is False else str(reply_to_mode).lower()
                    )
                for key in list(bridged):
                    env_name = _profile_extra_env_name(plat, key)
                    if env_name and get_profile_env(env_name, ""):
                        bridged.pop(key)
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
