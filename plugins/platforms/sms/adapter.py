"""sms platform plugin registration."""

from channels.builtin_platform_plugins import register_builtin_platform
from channels.platforms.sms import SmsAdapter, check_sms_requirements


def register(ctx) -> None:
    register_builtin_platform(ctx, "sms")


__all__ = ["SmsAdapter", "check_sms_requirements", "register"]
