"""discord platform plugin registration."""

from channels.builtin_platform_plugins import register_builtin_platform
from channels.platforms.discord import (
    DiscordAdapter,
    check_discord_requirements,
    discord,
)


def register(ctx) -> None:
    register_builtin_platform(ctx, "discord")


__all__ = ["DiscordAdapter", "check_discord_requirements", "discord", "register"]
