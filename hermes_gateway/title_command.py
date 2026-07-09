"""Gateway /title command ownership."""

from __future__ import annotations

import logging

from agent.i18n import t
from channels.platforms.base import MessageEvent

logger = logging.getLogger(__name__)


class GatewayTitleCommandService:
    def __init__(self, runner):
        self._runner = runner

    async def handle_title_command(self, event: MessageEvent) -> str:
        """Set or show the current session title."""

        source = event.source
        session_entry = self._runner.session_store.get_or_create_session(source)
        session_id = session_entry.session_id

        session_db = getattr(self._runner, "_session_db", None)
        if not session_db:
            return self._runner._format_session_db_unavailable()

        existing_title = session_db.get_session_title(session_id)
        if existing_title is None:
            try:
                session_db.create_session(
                    session_id=session_id,
                    source=source.platform.value if source.platform else "unknown",
                    user_id=source.user_id,
                )
            except Exception as exc:
                logger.debug("Could not create session row before title command: %s", exc)

        title_arg = event.get_command_args().strip()
        if title_arg:
            try:
                sanitized = session_db.sanitize_title(title_arg)
            except ValueError as e:
                return t("gateway.shared.warn_passthrough", error=e)
            if not sanitized:
                return t("gateway.title.empty_after_clean")
            try:
                if session_db.set_session_title(session_id, sanitized):
                    return t("gateway.title.set_to", title=sanitized)
                return t("gateway.title.not_found")
            except ValueError as e:
                return t("gateway.shared.warn_passthrough", error=e)

        title = session_db.get_session_title(session_id)
        if title:
            return t("gateway.title.current_with_title", session_id=session_id, title=title)
        return t("gateway.title.current_no_title", session_id=session_id)


def title_command_for(runner) -> GatewayTitleCommandService:
    service = getattr(runner, "title_command", None)
    if isinstance(service, GatewayTitleCommandService):
        return service
    service = GatewayTitleCommandService(runner)
    runner.title_command = service
    return service
