"""Gateway per-turn session context environment binding."""

from __future__ import annotations

from channels.session_context import clear_session_vars, set_session_vars


class GatewaySessionEnvRuntime:
    def __init__(self, runner):
        self._runner = runner

    def set_session_env(self, context) -> list:
        runner = self._runner
        adapters = getattr(runner, "adapters", None) or {}
        adapter = adapters.get(context.source.platform)
        async_delivery = getattr(adapter, "supports_async_delivery", True)
        return set_session_vars(
            platform=context.source.platform.value,
            chat_id=context.source.chat_id,
            chat_name=context.source.chat_name or "",
            thread_id=str(context.source.thread_id) if context.source.thread_id else "",
            user_id=str(context.source.user_id) if context.source.user_id else "",
            user_name=str(context.source.user_name) if context.source.user_name else "",
            session_key=context.session_key,
            message_id=str(context.source.message_id) if context.source.message_id else "",
            async_delivery=async_delivery,
        )

    @staticmethod
    def clear_session_env(tokens: list) -> None:
        clear_session_vars(tokens)


def session_env_runtime_for(runner) -> GatewaySessionEnvRuntime:
    service = getattr(runner, "session_env_runtime", None)
    if isinstance(service, GatewaySessionEnvRuntime):
        return service
    service = GatewaySessionEnvRuntime(runner)
    runner.session_env_runtime = service
    return service
