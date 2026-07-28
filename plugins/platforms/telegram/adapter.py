"""telegram platform plugin registration."""

from channels.builtin_platform_plugins import register_builtin_platform
from channels.platforms.telegram import TelegramAdapter, check_telegram_requirements


def register(ctx) -> None:
    register_builtin_platform(ctx, "telegram")


__all__ = ["TelegramAdapter", "check_telegram_requirements", "register"]
