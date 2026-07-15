from __future__ import annotations

import logging
import os
from typing import Any


logger = logging.getLogger("hermes.dovie_diagnostics")


def _diagnostic_trace_enabled() -> bool:
    return str(os.environ.get("DOVIE_STREAM_TRACE") or "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def emit_dovie_diagnostic(prefix: str, fields: dict[str, Any]) -> None:
    log = logger.info if _diagnostic_trace_enabled() else logger.debug
    log("%s %s", str(prefix or "").strip(), fields or {})


def emit_dovie_runtime_diagnostic(
    channel: str,
    stage: str,
    fields: dict[str, Any] | None = None,
) -> None:
    log = logger.info if _diagnostic_trace_enabled() else logger.debug
    log(
        "%s %s %s",
        str(channel or "").strip(),
        str(stage or "").strip(),
        fields or {},
    )
