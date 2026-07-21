"""Best-effort accessors for the agent-level stream-writer fence."""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def claim_stream_writer(agent: Any) -> int:
    """Claim the sink, degrading to an unfenced token for duck-typed agents."""
    claim = getattr(agent, "_claim_stream_writer", None)
    if callable(claim):
        try:
            return int(claim())
        except Exception:
            logger.debug("Stream-writer claim failed; continuing unfenced", exc_info=True)
    return 0


def stream_writer_is_current(agent: Any, token: int) -> bool:
    """Only reject a writer when the agent can prove it was superseded."""
    if not token:
        return True
    check = getattr(agent, "_stream_writer_is_current", None)
    if callable(check):
        try:
            return bool(check(token))
        except Exception:
            logger.debug("Stream-writer check failed; treating writer as current", exc_info=True)
    return True


__all__ = ["claim_stream_writer", "stream_writer_is_current"]
