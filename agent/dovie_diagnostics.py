from __future__ import annotations

import logging
from typing import Any


logger = logging.getLogger("hermes.dovie_diagnostics")


def emit_dovie_diagnostic(prefix: str, fields: dict[str, Any]) -> None:
    logger.debug("%s %s", str(prefix or "").strip(), fields or {})


def emit_dovie_runtime_diagnostic(
    channel: str,
    stage: str,
    fields: dict[str, Any] | None = None,
) -> None:
    logger.debug(
        "%s %s %s",
        str(channel or "").strip(),
        str(stage or "").strip(),
        fields or {},
    )
