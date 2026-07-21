"""mattermost platform plugin registration."""

from channels.builtin_platform_plugins import register_builtin_platform
from channels.platforms.mattermost import MattermostAdapter, check_mattermost_requirements


def register(ctx) -> None:
    register_builtin_platform(ctx, "mattermost")


__all__ = ["MattermostAdapter", "check_mattermost_requirements", "register"]
