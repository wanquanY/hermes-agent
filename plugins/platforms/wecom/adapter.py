"""WeCom platform plugin registration."""

from channels.builtin_platform_plugins import register_builtin_platforms


def __getattr__(name: str):
    """Expose compatibility symbols without importing optional deps at load."""
    if name in {"WeComAdapter", "check_wecom_requirements"}:
        from channels.platforms import wecom

        return getattr(wecom, name)
    if name in {"WecomCallbackAdapter", "check_wecom_callback_requirements"}:
        from channels.platforms import wecom_callback

        return getattr(wecom_callback, name)
    raise AttributeError(name)


def register(ctx) -> None:
    register_builtin_platforms(ctx, ("wecom", "wecom_callback"))


__all__ = [
    "WeComAdapter",
    "WecomCallbackAdapter",
    "check_wecom_callback_requirements",
    "check_wecom_requirements",
    "register",
]
