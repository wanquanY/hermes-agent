"""Platform-aware operational notice delivery."""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


class GatewayPlatformNoticeService:
    def __init__(self, runner):
        self._runner = runner

    async def deliver_platform_notice(self, source, content: str) -> None:
        """Deliver a setup/operational notice using platform-specific privacy rules."""
        runner = self._runner
        adapter = runner.adapters.get(source.platform)
        if not adapter:
            return

        config = getattr(runner, "config", None)
        notice_delivery = "public"
        if config and hasattr(config, "get_notice_delivery"):
            notice_delivery = config.get_notice_delivery(source.platform)

        metadata = runner._thread_metadata_for_source(source)
        if notice_delivery == "private" and getattr(source, "user_id", None):
            try:
                result = await adapter.send_private_notice(
                    source.chat_id,
                    source.user_id,
                    content,
                    metadata=metadata,
                )
                if getattr(result, "success", False):
                    return
            except Exception:
                logger.debug(
                    "[%s] send_private_notice failed, falling back to public",
                    getattr(source, "platform", "?"),
                    exc_info=True,
                )

        await adapter.send(source.chat_id, content, metadata=metadata)


def platform_notice_for(runner) -> GatewayPlatformNoticeService:
    service = getattr(runner, "platform_notice", None)
    if isinstance(service, GatewayPlatformNoticeService):
        return service
    service = GatewayPlatformNoticeService(runner)
    runner.platform_notice = service
    return service
