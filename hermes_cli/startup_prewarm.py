"""Non-blocking import warm-up for the interactive CLI."""

from __future__ import annotations

import logging
import os
import threading

logger = logging.getLogger(__name__)

_start_lock = threading.Lock()
_started = False


def _prewarm_agent_runtime() -> None:
    try:
        import run_agent  # noqa: F401
        import openai  # noqa: F401
    except Exception:
        logger.debug("agent runtime pre-import failed", exc_info=True)


def prewarm_agent_runtime_async() -> bool:
    """Start the import warm-up once, unless startup is explicitly deferred."""
    global _started

    if os.environ.get("HERMES_DEFER_AGENT_STARTUP") == "1":
        return False

    with _start_lock:
        if _started:
            return False
        _started = True
        threading.Thread(
            target=_prewarm_agent_runtime,
            name="agent-runtime-prewarm",
            daemon=True,
        ).start()
    return True


def _reset_startup_prewarm_for_tests() -> None:
    global _started

    with _start_lock:
        _started = False
