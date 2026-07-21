"""feishu platform plugin registration."""

from channels.builtin_platform_plugins import register_builtin_platform
from channels.platforms.feishu import FeishuAdapter, check_feishu_requirements


def register(ctx) -> None:
    register_builtin_platform(ctx, "feishu")


__all__ = ["FeishuAdapter", "check_feishu_requirements", "register"]
