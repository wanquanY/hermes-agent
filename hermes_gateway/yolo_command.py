"""Gateway /yolo session approval-bypass command ownership."""

from __future__ import annotations

from typing import Union

from agent.i18n import t
from channels.platforms.base import MessageEvent
from channels.platforms.base_models import EphemeralReply


class GatewayYoloCommandService:
    def __init__(self, runner):
        self._runner = runner

    async def handle_yolo_command(self, event: MessageEvent) -> Union[str, EphemeralReply]:
        """Handle /yolo — toggle dangerous command approval bypass for this session only."""
        from tools.approval import (
            disable_session_yolo,
            enable_session_yolo,
            is_session_yolo_enabled,
        )

        session_key = self._runner._session_key_for_source(event.source)
        current = is_session_yolo_enabled(session_key)
        if current:
            disable_session_yolo(session_key)
            return EphemeralReply(t("gateway.yolo.disabled"))
        else:
            enable_session_yolo(session_key)
            return EphemeralReply(t("gateway.yolo.enabled"))


def yolo_command_for(runner) -> GatewayYoloCommandService:
    service = getattr(runner, "yolo_command", None)
    if isinstance(service, GatewayYoloCommandService):
        return service
    service = GatewayYoloCommandService(runner)
    runner.yolo_command = service
    return service
