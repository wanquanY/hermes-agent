"""Agent-level single-writer fence for provider streaming attempts."""

from __future__ import annotations

import logging
import threading

logger = logging.getLogger(__name__)


class StreamWriterAgentMixin:
    """Fence late deltas after a retry or replacement stream takes ownership."""

    def _ensure_stream_writer_state(self) -> None:
        if getattr(self, "_stream_writer_lock", None) is None:
            self._stream_writer_lock = threading.Lock()
        if not hasattr(self, "_stream_writer_token"):
            self._stream_writer_token = 0
        if getattr(self, "_stream_writer_tls", None) is None:
            self._stream_writer_tls = threading.local()
        if not hasattr(self, "_stream_writer_dropped"):
            self._stream_writer_dropped = 0

    def _claim_stream_writer(self) -> int:
        """Make the calling stream the only current writer and return its token."""
        self._ensure_stream_writer_state()
        with self._stream_writer_lock:
            self._stream_writer_token += 1
            token = self._stream_writer_token
        self._stream_writer_tls.token = token
        return token

    def _stream_writer_is_current(self, token: int) -> bool:
        return token == getattr(self, "_stream_writer_token", token)

    def _stream_writer_superseded(self) -> bool:
        local = getattr(self, "_stream_writer_tls", None)
        token = getattr(local, "token", None) if local is not None else None
        if token is None:
            return False
        return token != getattr(self, "_stream_writer_token", token)

    def _note_dropped_stream_writer(self, where: str) -> None:
        try:
            count = int(getattr(self, "_stream_writer_dropped", 0)) + 1
        except (TypeError, ValueError):
            count = 1
        self._stream_writer_dropped = count
        if count == 1 or count & (count - 1) == 0:
            logger.warning(
                "Dropped delta from superseded stream writer at %s "
                "(discarded=%d)",
                where,
                count,
            )


__all__ = ["StreamWriterAgentMixin"]
