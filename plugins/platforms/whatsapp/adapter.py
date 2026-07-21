"""whatsapp platform plugin registration."""

from channels.builtin_platform_plugins import register_builtin_platform
from channels.platforms.whatsapp import WhatsAppAdapter, check_whatsapp_requirements


def register(ctx) -> None:
    register_builtin_platform(ctx, "whatsapp")


__all__ = ["WhatsAppAdapter", "check_whatsapp_requirements", "register"]
