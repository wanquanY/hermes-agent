"""Gateway media-delivery environment warnings."""

from __future__ import annotations

import json
import logging
import os
import re

from hermes_gateway.config import Platform

logger = logging.getLogger(__name__)

_DOCKER_VOLUME_SPEC_RE = re.compile(r"^(?P<host>.+):(?P<container>/[^:]+?)(?::(?P<options>[^:]+))?$")
_DOCKER_MEDIA_OUTPUT_CONTAINER_PATHS = {"/output", "/outputs"}


class GatewayMediaWarningService:
    def __init__(self, runner):
        self._runner = runner

    def warn_if_docker_media_delivery_is_risky(self) -> None:
        if os.getenv("TERMINAL_ENV", "").strip().lower() != "docker":
            return

        connected = self._runner.config.get_connected_platforms()
        messaging_platforms = [
            platform
            for platform in connected
            if platform not in {Platform.LOCAL, Platform.API_SERVER, Platform.WEBHOOK}
        ]
        if not messaging_platforms:
            return

        if self._has_explicit_output_mount(os.getenv("TERMINAL_DOCKER_VOLUMES", "").strip()):
            return

        logger.warning(
            "Docker backend is enabled for the messaging gateway but no explicit host-visible "
            "output mount (for example '/home/user/.hermes/cache/documents:/output') is configured. "
            "This is fine if the model already emits host-visible paths, but MEDIA file delivery can fail "
            "for container-local paths like '/workspace/...' or '/output/...'."
        )

    @staticmethod
    def _has_explicit_output_mount(raw_volumes: str) -> bool:
        if not raw_volumes:
            return False
        try:
            parsed = json.loads(raw_volumes)
        except Exception:
            logger.debug("Could not parse TERMINAL_DOCKER_VOLUMES for gateway media warning", exc_info=True)
            return False
        if not isinstance(parsed, list):
            return False

        for spec in (str(value) for value in parsed if isinstance(value, str)):
            match = _DOCKER_VOLUME_SPEC_RE.match(spec)
            if match and match.group("container") in _DOCKER_MEDIA_OUTPUT_CONTAINER_PATHS:
                return True
        return False


def media_warnings_for(runner) -> GatewayMediaWarningService:
    service = getattr(runner, "media_warning_service", None)
    if isinstance(service, GatewayMediaWarningService):
        return service
    service = GatewayMediaWarningService(runner)
    runner.media_warning_service = service
    return service
