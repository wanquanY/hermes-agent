from __future__ import annotations

import os
from typing import Any


def _enabled() -> bool:
    raw = str(os.environ.get("HERMES_DOXIE_DIAGNOSTICS") or "").strip().lower()
    return raw in {"1", "true", "yes", "on", "debug", "trace"}


def emit_doxie_diagnostic(prefix: str, fields: dict[str, Any]) -> None:
    """Emit DoXie runtime diagnostics without taking Python logging locks."""

    if not _enabled():
        return
    try:
        body = " ".join(f"{key}={value!r}" for key, value in fields.items())
        os.write(2, f"{prefix} {body}\n".encode("utf-8", errors="replace"))
    except Exception:
        pass
