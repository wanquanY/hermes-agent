"""dingtalk platform plugin registration."""

from channels.builtin_platform_plugins import register_builtin_platform
from channels.platforms.dingtalk import DingTalkAdapter, check_dingtalk_requirements


def register(ctx) -> None:
    register_builtin_platform(ctx, "dingtalk")


__all__ = ["DingTalkAdapter", "check_dingtalk_requirements", "register"]
