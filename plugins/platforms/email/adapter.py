"""email platform plugin registration."""

from channels.builtin_platform_plugins import register_builtin_platform
from channels.platforms.email import EmailAdapter, check_email_requirements


def register(ctx) -> None:
    register_builtin_platform(ctx, "email")


__all__ = ["EmailAdapter", "check_email_requirements", "register"]
