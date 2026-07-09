"""Gateway /title command ownership."""

from __future__ import annotations

from agent.i18n import t
from channels.platforms.base import MessageEvent


class GatewayTitleCommandMixin:
    async def _handle_title_command(self, event: MessageEvent) -> str:
        """Set or show the current session title."""

        source = event.source
        session_entry = self.session_store.get_or_create_session(source)
        session_id = session_entry.session_id

        if not self._session_db:
            return self._format_session_db_unavailable()

        existing_title = self._session_db.get_session_title(session_id)
        if existing_title is None:
            try:
                self._session_db.create_session(
                    session_id=session_id,
                    source=source.platform.value if source.platform else "unknown",
                    user_id=source.user_id,
                )
            except Exception:
                pass

        title_arg = event.get_command_args().strip()
        if title_arg:
            try:
                sanitized = self._session_db.sanitize_title(title_arg)
            except ValueError as e:
                return t("gateway.shared.warn_passthrough", error=e)
            if not sanitized:
                return t("gateway.title.empty_after_clean")
            try:
                if self._session_db.set_session_title(session_id, sanitized):
                    return t("gateway.title.set_to", title=sanitized)
                return t("gateway.title.not_found")
            except ValueError as e:
                return t("gateway.shared.warn_passthrough", error=e)

        title = self._session_db.get_session_title(session_id)
        if title:
            return t("gateway.title.current_with_title", session_id=session_id, title=title)
        return t("gateway.title.current_no_title", session_id=session_id)
