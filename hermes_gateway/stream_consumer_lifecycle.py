"""Metadata and finalization lifecycle for gateway stream consumers."""

from __future__ import annotations

import inspect
import logging
from typing import Any

logger = logging.getLogger("hermes_gateway.stream_consumer")


class StreamConsumerLifecycleMixin:
    """Own adapter metadata negotiation and the one-shot finalize hook."""

    def _metadata_for_send(
        self,
        *,
        final: bool = False,
        expect_edits: bool = False,
    ) -> dict | None:
        """Return per-send metadata for stream-created messages.

        Mattermost uses ``notify`` to distinguish final content from previews.
        ``expect_edits`` keeps Telegram previews on its editable send path.
        """
        meta = dict(self.metadata) if self.metadata else {}
        if self._initial_reply_to_id:
            meta["reply_to_message_id"] = self._initial_reply_to_id
        if expect_edits:
            meta["expect_edits"] = True
        if final:
            meta["notify"] = True
        return meta or None

    async def _notify_before_finalize(self) -> None:
        """Run the pre-finalize hook exactly once, isolating hook errors."""
        if self._before_finalize_notified:
            return
        self._before_finalize_notified = True
        if self._on_before_finalize is None:
            return
        try:
            result = self._on_before_finalize()
            if inspect.isawaitable(result):
                await result
        except Exception:
            logger.debug("Pre-finalize stream hook failed", exc_info=True)

    async def _edit_message(
        self,
        *,
        message_id: str,
        content: str,
        finalize: bool = False,
    ) -> Any:
        """Edit via the adapter, passing routing metadata when supported."""
        kwargs: dict[str, Any] = {
            "chat_id": self.chat_id,
            "message_id": message_id,
            "content": content,
            "finalize": finalize,
        }
        edit_metadata = self._metadata_for_send(final=finalize)
        if edit_metadata:
            try:
                params = inspect.signature(self.adapter.edit_message).parameters
                accepts_metadata = "metadata" in params or any(
                    param.kind is inspect.Parameter.VAR_KEYWORD
                    for param in params.values()
                )
                if accepts_metadata:
                    kwargs["metadata"] = edit_metadata
            except (TypeError, ValueError):
                logger.debug(
                    "Could not inspect stream adapter edit signature",
                    exc_info=True,
                )
        return await self.adapter.edit_message(**kwargs)


__all__ = ["StreamConsumerLifecycleMixin"]
