"""slack platform plugin registration."""

from channels.builtin_platform_plugins import register_builtin_platform
from channels.platforms.slack import SlackAdapter, check_slack_requirements


def register(ctx) -> None:
    register_builtin_platform(ctx, "slack")


__all__ = ["SlackAdapter", "check_slack_requirements", "register"]
