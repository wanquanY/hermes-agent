"""Gateway profile and home-channel commands."""

from __future__ import annotations

from agent.i18n import t
from channels.platforms.base import MessageEvent
from hermes_gateway.bootstrap import home_target_env_var, home_thread_env_var
from hermes_gateway.config import HomeChannel, PlatformConfig


class GatewayProfileHomeCommandMixin:
    async def _handle_profile_command(self, event: MessageEvent) -> str:
        """Show active profile name and home directory."""

        from hermes_constants import display_hermes_home
        from hermes_cli.profiles import get_active_profile_name

        display = display_hermes_home()
        profile_name = get_active_profile_name()

        lines = [
            t("gateway.profile.header", profile=profile_name),
            t("gateway.profile.home", home=display),
        ]
        return "\n".join(lines)

    async def _handle_set_home_command(self, event: MessageEvent) -> str:
        """Set the current chat/thread as the platform home target."""

        source = event.source
        platform_name = source.platform.value if source.platform else "unknown"
        chat_id = source.chat_id
        chat_name = source.chat_name or chat_id
        thread_id = source.thread_id

        try:
            from hermes_cli.config import save_env_value
            save_env_value(home_target_env_var(platform_name), str(chat_id))
            save_env_value(home_thread_env_var(platform_name), str(thread_id or ""))
        except Exception as e:
            return t("gateway.set_home.save_failed", error=e)

        if source.platform:
            platform_config = self.config.platforms.setdefault(
                source.platform,
                PlatformConfig(enabled=True),
            )
            platform_config.home_channel = HomeChannel(
                platform=source.platform,
                chat_id=str(chat_id),
                name=chat_name,
                thread_id=str(thread_id) if thread_id else None,
            )

        return t("gateway.set_home.success", name=chat_name, chat_id=chat_id)
