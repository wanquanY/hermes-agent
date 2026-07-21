"""homeassistant platform plugin registration."""

from channels.builtin_platform_plugins import register_builtin_platform
from channels.platforms.homeassistant import HomeAssistantAdapter, check_ha_requirements


def register(ctx) -> None:
    register_builtin_platform(ctx, "homeassistant")


__all__ = ["HomeAssistantAdapter", "check_ha_requirements", "register"]
