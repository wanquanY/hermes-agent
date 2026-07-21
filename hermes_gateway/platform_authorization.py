"""Gateway platform authorization policy."""

from __future__ import annotations

import logging
import os
from typing import Optional

from channels.whatsapp_identity import (
    expand_whatsapp_aliases as _expand_whatsapp_auth_aliases,
    normalize_whatsapp_identifier as _normalize_whatsapp_identifier,
)
from hermes_gateway.config import Platform
from hermes_gateway.session import SessionSource

logger = logging.getLogger(__name__)


def _auth_env(name: str, default: str = "") -> str:
    """Read authorization state from the active profile secret scope."""
    if not name:
        return default
    from agent.secret_scope import (
        current_secret_scope,
        get_secret,
        is_multiplex_active,
    )

    if current_secret_scope() is not None or is_multiplex_active():
        value = get_secret(name, default)
        return str(value or default).strip()
    return (os.getenv(name) or default).strip()


class GatewayPlatformAuthorizationMixin:
    @staticmethod
    def _authorization_values(value) -> str:
        if isinstance(value, (list, tuple, set, frozenset)):
            return ",".join(str(item).strip() for item in value if str(item).strip())
        return str(value or "").strip()

    def _adapter_authorization_config(
        self,
        source: SessionSource,
        key: str,
    ) -> str:
        adapter = self._adapter_for_source(source)
        config = getattr(adapter, "config", None)
        extra = getattr(config, "extra", None)
        if not isinstance(extra, dict):
            return ""
        return self._authorization_values(extra.get(key))

    def _pairing_store_for(self, source: SessionSource):
        profile = str(getattr(source, "profile", "") or "").strip()
        stores = getattr(self, "pairing_stores", None) or {}
        if profile and profile in stores:
            return stores[profile]
        return getattr(self, "pairing_store", None)

    def _authorization_adapter(
        self,
        platform: Optional[Platform],
        profile: Optional[str] = None,
    ):
        """Resolve the live adapter whose intake policy gates authorization."""
        if platform is None:
            return None
        source = SessionSource(
            platform=platform,
            chat_id="",
            chat_type="dm",
            profile=profile,
        )
        from hermes_gateway.platform_runtime import platform_runtime_for

        return platform_runtime_for(self).adapter_for_source(source)

    def _adapter_for_source(self, source: Optional[SessionSource]):
        """Resolve the live adapter for an inbound session source."""
        from hermes_gateway.platform_runtime import platform_runtime_for

        return platform_runtime_for(self).adapter_for_source(source)

    def _is_user_authorized(self, source: SessionSource) -> bool:
        """
        Check if a user is authorized to use the bot.

        Checks in order:
        1. Per-platform allow-all flag (e.g., DISCORD_ALLOW_ALL_USERS=true)
        2. Environment variable allowlists (TELEGRAM_ALLOWED_USERS, etc.)
        3. DM pairing approved list
        4. Global allow-all (GATEWAY_ALLOW_ALL_USERS=true)
        5. Default: deny
        """
        # Home Assistant events are system-generated (state changes), not
        # user-initiated messages.  The HASS_TOKEN already authenticates the
        # connection, so HA events are always authorized.
        # Webhook events are authenticated via HMAC signature validation in
        # the adapter itself — no user allowlist applies.
        if source.platform in {Platform.HOMEASSISTANT, Platform.WEBHOOK}:
            return True

        user_id = source.user_id

        # Telegram (and similar) authorize entire group/forum/channel chats
        # by chat ID via TELEGRAM_GROUP_ALLOWED_CHATS / QQ_GROUP_ALLOWED_USERS.
        # That allowlist is chat-scoped, so it must work even when
        # source.user_id is None — Telegram emits anonymous-admin posts,
        # sender_chat traffic, and channel broadcasts with no `from_user`,
        # and an operator who explicitly listed the chat expects those to
        # be honored. Run this check before the no-user-id guard below so
        # documented behavior matches reality
        # (website/docs/reference/environment-variables.md,
        # website/docs/user-guide/messaging/telegram.md).
        if source.chat_type in {"group", "forum", "channel"} and source.chat_id:
            chat_allowlist_env = {
                Platform.TELEGRAM: "TELEGRAM_GROUP_ALLOWED_CHATS",
                Platform.QQBOT: "QQ_GROUP_ALLOWED_USERS",
            }.get(source.platform, "")
            if chat_allowlist_env:
                raw_chat_allowlist = _auth_env(chat_allowlist_env)
                if not raw_chat_allowlist:
                    raw_chat_allowlist = self._adapter_authorization_config(
                        source,
                        "group_allowed_chats",
                    )
                if raw_chat_allowlist:
                    allowed_group_ids = {
                        cid.strip()
                        for cid in raw_chat_allowlist.split(",")
                        if cid.strip()
                    }
                    if "*" in allowed_group_ids or source.chat_id in allowed_group_ids:
                        return True

        if not user_id:
            return False

        # Discord resolves configured usernames and guild roles inside the
        # owning adapter. Those derived identities must remain adapter-local:
        # publishing them through os.environ would merge authorization state
        # across profiles. Admission already validated role membership and
        # stamps that fact on the immutable source; numeric IDs are checked
        # against the profile-specific live adapter here.
        if source.platform == Platform.DISCORD:
            adapter = self._adapter_for_source(source)
            if bool(getattr(source, "role_authorized", False)):
                return True
            allowed_user_ids = getattr(adapter, "_allowed_user_ids", set()) or set()
            if "*" in allowed_user_ids or user_id in allowed_user_ids:
                return True

        platform_env_map = {
            Platform.TELEGRAM: "TELEGRAM_ALLOWED_USERS",
            Platform.DISCORD: "DISCORD_ALLOWED_USERS",
            Platform.WHATSAPP: "WHATSAPP_ALLOWED_USERS",
            Platform.SLACK: "SLACK_ALLOWED_USERS",
            Platform.SIGNAL: "SIGNAL_ALLOWED_USERS",
            Platform.EMAIL: "EMAIL_ALLOWED_USERS",
            Platform.SMS: "SMS_ALLOWED_USERS",
            Platform.MATTERMOST: "MATTERMOST_ALLOWED_USERS",
            Platform.MATRIX: "MATRIX_ALLOWED_USERS",
            Platform.DINGTALK: "DINGTALK_ALLOWED_USERS",
            Platform.FEISHU: "FEISHU_ALLOWED_USERS",
            Platform.WECOM: "WECOM_ALLOWED_USERS",
            Platform.WECOM_CALLBACK: "WECOM_CALLBACK_ALLOWED_USERS",
            Platform.WEIXIN: "WEIXIN_ALLOWED_USERS",
            Platform.BLUEBUBBLES: "BLUEBUBBLES_ALLOWED_USERS",
            Platform.QQBOT: "QQ_ALLOWED_USERS",
            Platform.YUANBAO: "YUANBAO_ALLOWED_USERS",
        }
        platform_group_user_env_map = {
            Platform.TELEGRAM: "TELEGRAM_GROUP_ALLOWED_USERS",
        }
        platform_group_chat_env_map = {
            Platform.TELEGRAM: "TELEGRAM_GROUP_ALLOWED_CHATS",
            Platform.QQBOT: "QQ_GROUP_ALLOWED_USERS",
        }
        platform_allow_all_map = {
            Platform.TELEGRAM: "TELEGRAM_ALLOW_ALL_USERS",
            Platform.DISCORD: "DISCORD_ALLOW_ALL_USERS",
            Platform.WHATSAPP: "WHATSAPP_ALLOW_ALL_USERS",
            Platform.SLACK: "SLACK_ALLOW_ALL_USERS",
            Platform.SIGNAL: "SIGNAL_ALLOW_ALL_USERS",
            Platform.EMAIL: "EMAIL_ALLOW_ALL_USERS",
            Platform.SMS: "SMS_ALLOW_ALL_USERS",
            Platform.MATTERMOST: "MATTERMOST_ALLOW_ALL_USERS",
            Platform.MATRIX: "MATRIX_ALLOW_ALL_USERS",
            Platform.DINGTALK: "DINGTALK_ALLOW_ALL_USERS",
            Platform.FEISHU: "FEISHU_ALLOW_ALL_USERS",
            Platform.WECOM: "WECOM_ALLOW_ALL_USERS",
            Platform.WECOM_CALLBACK: "WECOM_CALLBACK_ALLOW_ALL_USERS",
            Platform.WEIXIN: "WEIXIN_ALLOW_ALL_USERS",
            Platform.BLUEBUBBLES: "BLUEBUBBLES_ALLOW_ALL_USERS",
            Platform.QQBOT: "QQ_ALLOW_ALL_USERS",
            Platform.YUANBAO: "YUANBAO_ALLOW_ALL_USERS",
        }
        # Bots admitted by {PLATFORM}_ALLOW_BOTS bypass the human allowlist (#4466).
        platform_allow_bots_map = {
            Platform.DISCORD: "DISCORD_ALLOW_BOTS",
            Platform.FEISHU: "FEISHU_ALLOW_BOTS",
        }

        # Plugin platforms: check the registry for auth env var names
        if source.platform not in platform_env_map:
            try:
                from channels.platform_registry import platform_registry

                entry = platform_registry.get(source.platform.value)
                if entry:
                    if entry.allowed_users_env:
                        platform_env_map[source.platform] = entry.allowed_users_env
                    if entry.allow_all_env:
                        platform_allow_all_map[source.platform] = entry.allow_all_env
            except Exception:
                logger.debug("Suppressed recoverable gateway exception", exc_info=True)

        # Per-platform allow-all flag (e.g., DISCORD_ALLOW_ALL_USERS=true)
        platform_allow_all_var = platform_allow_all_map.get(source.platform, "")
        if platform_allow_all_var and _auth_env(platform_allow_all_var).lower() in {"true", "1", "yes"}:
            return True

        if getattr(source, "is_bot", False):
            allow_bots_var = platform_allow_bots_map.get(source.platform)
            if allow_bots_var and _auth_env(allow_bots_var, "none").lower() in {"mentions", "all"}:
                return True

        # Discord role-based access (DISCORD_ALLOWED_ROLES): the adapter's
        # on_message pre-filter already verified role membership — if the
        # message reached here, the user passed that check. Authorize
        # directly to avoid the "no allowlists configured" branch below
        # rejecting role-only setups where DISCORD_ALLOWED_USERS is empty
        # (issue #7871).
        if (
            source.platform == Platform.DISCORD
            and _auth_env("DISCORD_ALLOWED_ROLES")
        ):
            return True

        # Check pairing store (always checked, regardless of allowlists)
        platform_name = source.platform.value if source.platform else ""
        pairing_store = self._pairing_store_for(source)
        if pairing_store is not None and pairing_store.is_approved(platform_name, user_id):
            return True

        # Check platform-specific and global allowlists
        platform_allowlist = _auth_env(platform_env_map.get(source.platform, ""))
        if not platform_allowlist:
            platform_allowlist = self._adapter_authorization_config(
                source,
                "allow_from",
            )
        group_user_allowlist = ""
        group_chat_allowlist = ""
        if source.chat_type in {"group", "forum"}:
            group_user_allowlist = _auth_env(platform_group_user_env_map.get(source.platform, ""))
            group_chat_allowlist = _auth_env(platform_group_chat_env_map.get(source.platform, ""))
            if not group_user_allowlist:
                group_user_allowlist = self._adapter_authorization_config(
                    source,
                    "group_allow_from",
                )
            if not group_chat_allowlist:
                group_chat_allowlist = self._adapter_authorization_config(
                    source,
                    "group_allowed_chats",
                )
        global_allowlist = _auth_env("GATEWAY_ALLOWED_USERS")

        if not platform_allowlist and not group_user_allowlist and not group_chat_allowlist and not global_allowlist:
            # No allowlists configured -- check global allow-all flag
            return _auth_env("GATEWAY_ALLOW_ALL_USERS").lower() in {"true", "1", "yes"}

        # Telegram can optionally authorize group traffic by chat ID.
        # Keep this separate from TELEGRAM_GROUP_ALLOWED_USERS, which gates
        # the sender user ID for group/forum messages.
        if group_chat_allowlist and source.chat_type in {"group", "forum"} and source.chat_id:
            allowed_group_ids = {
                chat_id.strip() for chat_id in group_chat_allowlist.split(",") if chat_id.strip()
            }
            if "*" in allowed_group_ids or source.chat_id in allowed_group_ids:
                return True

        # Backward-compat shim for #15027: prior to PR #17686,
        # TELEGRAM_GROUP_ALLOWED_USERS was (mis)used as a chat-ID allowlist.
        # Values starting with "-" are Telegram chat IDs, not user IDs, so if
        # users still have those in TELEGRAM_GROUP_ALLOWED_USERS we honor them
        # as chat IDs and warn once. The correct var is now
        # TELEGRAM_GROUP_ALLOWED_CHATS.
        if (
            source.platform == Platform.TELEGRAM
            and group_user_allowlist
            and source.chat_type in {"group", "forum"}
            and source.chat_id
        ):
            legacy_chat_ids = {
                v.strip()
                for v in group_user_allowlist.split(",")
                if v.strip().startswith("-")
            }
            if legacy_chat_ids:
                if not getattr(self, "_warned_telegram_group_users_legacy", False):
                    logger.warning(
                        "TELEGRAM_GROUP_ALLOWED_USERS contains chat-ID-shaped values "
                        "(%s). Treating them as chat IDs for backward compatibility. "
                        "Move chat IDs to TELEGRAM_GROUP_ALLOWED_CHATS — the _USERS var "
                        "is now for sender user IDs.",
                        ",".join(sorted(legacy_chat_ids)),
                    )
                    self._warned_telegram_group_users_legacy = True
                if source.chat_id in legacy_chat_ids:
                    return True

        # Check if user is in any allowlist. In group/forum chats,
        # TELEGRAM_GROUP_ALLOWED_USERS is the scoped allowlist and should not
        # imply DM access; TELEGRAM_ALLOWED_USERS remains the platform-wide
        # allowlist and still works everywhere for backward compatibility.
        allowed_ids = set()
        if platform_allowlist:
            allowed_ids.update(uid.strip() for uid in platform_allowlist.split(",") if uid.strip())
        if group_user_allowlist:
            allowed_ids.update(uid.strip() for uid in group_user_allowlist.split(",") if uid.strip())
        if global_allowlist:
            allowed_ids.update(uid.strip() for uid in global_allowlist.split(",") if uid.strip())

        # "*" in any allowlist means allow everyone (consistent with
        # SIGNAL_GROUP_ALLOWED_USERS precedent)
        if "*" in allowed_ids:
            return True

        check_ids = {user_id}
        if "@" in user_id:
            check_ids.add(user_id.split("@")[0])

        # WhatsApp: resolve phone/LID aliases from bridge session mapping files
        if source.platform == Platform.WHATSAPP:
            normalized_allowed_ids = set()
            for allowed_id in allowed_ids:
                normalized_allowed_ids.update(_expand_whatsapp_auth_aliases(allowed_id))
            if normalized_allowed_ids:
                allowed_ids = normalized_allowed_ids

            check_ids.update(_expand_whatsapp_auth_aliases(user_id))
            normalized_user_id = _normalize_whatsapp_identifier(user_id)
            if normalized_user_id:
                check_ids.add(normalized_user_id)

        return bool(check_ids & allowed_ids)

    def _get_unauthorized_dm_behavior(self, platform: Optional[Platform]) -> str:
        """Return how unauthorized DMs should be handled for a platform.

        Resolution order:
        1. Explicit per-platform ``unauthorized_dm_behavior`` in config — always wins.
        2. Explicit global ``unauthorized_dm_behavior`` in config — wins when no per-platform.
        3. When an allowlist (``PLATFORM_ALLOWED_USERS``,
           ``PLATFORM_GROUP_ALLOWED_USERS`` / ``PLATFORM_GROUP_ALLOWED_CHATS``,
           or ``GATEWAY_ALLOWED_USERS``) is configured, default to ``"ignore"`` —
           the allowlist signals that the owner has deliberately restricted
           access; spamming unknown contacts with pairing codes is both noisy
           and a potential info-leak. (#9337)
        4. No allowlist and no explicit config → ``"pair"`` (open-gateway default).
        """
        config = getattr(self, "config", None)

        # Check for an explicit per-platform override first.
        if config and hasattr(config, "get_unauthorized_dm_behavior") and platform:
            platform_cfg = config.platforms.get(platform) if hasattr(config, "platforms") else None
            if platform_cfg and "unauthorized_dm_behavior" in getattr(platform_cfg, "extra", {}):
                # Operator explicitly configured behavior for this platform — respect it.
                return config.get_unauthorized_dm_behavior(platform)

        # Check for an explicit global config override.
        if config and hasattr(config, "unauthorized_dm_behavior"):
            if config.unauthorized_dm_behavior != "pair":  # non-default → explicit override
                return config.unauthorized_dm_behavior

        # No explicit override. Fall back to allowlist-aware default:
        # if any allowlist is configured for this platform, silently drop
        # unauthorized messages instead of sending pairing codes.
        if platform:
            platform_env_map = {
                Platform.TELEGRAM: "TELEGRAM_ALLOWED_USERS",
                Platform.DISCORD: "DISCORD_ALLOWED_USERS",
                Platform.WHATSAPP: "WHATSAPP_ALLOWED_USERS",
                Platform.SLACK: "SLACK_ALLOWED_USERS",
                Platform.SIGNAL: "SIGNAL_ALLOWED_USERS",
                Platform.EMAIL: "EMAIL_ALLOWED_USERS",
                Platform.SMS: "SMS_ALLOWED_USERS",
                Platform.MATTERMOST: "MATTERMOST_ALLOWED_USERS",
                Platform.MATRIX: "MATRIX_ALLOWED_USERS",
                Platform.DINGTALK: "DINGTALK_ALLOWED_USERS",
                Platform.FEISHU: "FEISHU_ALLOWED_USERS",
                Platform.WECOM: "WECOM_ALLOWED_USERS",
                Platform.WECOM_CALLBACK: "WECOM_CALLBACK_ALLOWED_USERS",
                Platform.WEIXIN: "WEIXIN_ALLOWED_USERS",
                Platform.BLUEBUBBLES: "BLUEBUBBLES_ALLOWED_USERS",
                Platform.QQBOT: "QQ_ALLOWED_USERS",
            }
            platform_group_env_map = {
                Platform.TELEGRAM: (
                    "TELEGRAM_GROUP_ALLOWED_USERS",
                    "TELEGRAM_GROUP_ALLOWED_CHATS",
                ),
                Platform.QQBOT: ("QQ_GROUP_ALLOWED_USERS",),
            }
            if _auth_env(platform_env_map.get(platform, "")):
                return "ignore"
            for env_key in platform_group_env_map.get(platform, ()):
                if _auth_env(env_key):
                    return "ignore"

        if _auth_env("GATEWAY_ALLOWED_USERS"):
            return "ignore"

        return "pair"
