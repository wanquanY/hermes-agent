"""matrix platform plugin registration."""

from channels.builtin_platform_plugins import register_builtin_platform
from channels.platforms.matrix import (
    MatrixAdapter,
    _apply_yaml_config,
    check_matrix_requirements,
)
from channels.platforms.matrix_support import DEFAULT_MAX_MESSAGE_LENGTH


def register(ctx) -> None:
    register_builtin_platform(ctx, "matrix")


__all__ = [
    "DEFAULT_MAX_MESSAGE_LENGTH",
    "MatrixAdapter",
    "_apply_yaml_config",
    "check_matrix_requirements",
    "register",
]
