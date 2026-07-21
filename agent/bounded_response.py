"""Bounded reads for diagnostic bodies on streaming HTTP error responses."""

from __future__ import annotations

import logging
import threading
from typing import Optional

import httpx


logger = logging.getLogger(__name__)

DEFAULT_ERROR_BODY_MAX_BYTES = 64 * 1024
DEFAULT_ERROR_BODY_TIMEOUT_S = 10.0


def _safe_close(response: httpx.Response) -> None:
    try:
        response.close()
    except Exception:
        pass


def read_streaming_error_body(
    response: httpx.Response,
    *,
    max_bytes: int = DEFAULT_ERROR_BODY_MAX_BYTES,
    timeout_s: float = DEFAULT_ERROR_BODY_TIMEOUT_S,
) -> str:
    """Return a bounded UTF-8 diagnostic body and never mask the HTTP error.

    A daemon reader is intentional: ``httpx.Response.iter_bytes`` may block
    inside a socket read, so a deadline checked only between chunks is not a
    wall-clock bound. Closing the response on timeout interrupts transports
    that support cancellation while the caller immediately gets partial data.
    """
    byte_limit = max(0, int(max_bytes))
    deadline = max(0.0, float(timeout_s))
    chunks: list[bytes] = []
    state = {"truncated": False}
    done = threading.Event()

    def _drain() -> None:
        total = 0
        try:
            for chunk in response.iter_bytes():
                if not chunk:
                    continue
                remaining = byte_limit - total
                if remaining <= 0:
                    state["truncated"] = True
                    break
                if len(chunk) > remaining:
                    chunks.append(chunk[:remaining])
                    state["truncated"] = True
                    break
                chunks.append(chunk)
                total += len(chunk)
        except Exception as exc:
            logger.debug("bounded error-body read failed: %s", exc)
        finally:
            done.set()

    worker = threading.Thread(
        target=_drain,
        name="bounded-error-body-read",
        daemon=True,
    )
    worker.start()
    finished = done.wait(timeout=deadline)
    if not finished:
        logger.debug(
            "bounded error-body read timed out after %.1fs (%d bytes)",
            deadline,
            sum(len(chunk) for chunk in chunks),
        )
    _safe_close(response)
    if state["truncated"]:
        logger.debug("bounded error-body read capped at %d bytes", byte_limit)
    return b"".join(chunks).decode("utf-8", errors="replace")


def read_error_body_or_default(
    response: httpx.Response,
    *,
    max_bytes: int = DEFAULT_ERROR_BODY_MAX_BYTES,
    timeout_s: float = DEFAULT_ERROR_BODY_TIMEOUT_S,
) -> Optional[str]:
    """Return the bounded diagnostic body, or ``None`` when it is empty."""
    text = read_streaming_error_body(
        response,
        max_bytes=max_bytes,
        timeout_s=timeout_s,
    )
    return text or None
