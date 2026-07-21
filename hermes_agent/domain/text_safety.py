"""Unicode normalization at persistence boundaries."""

from __future__ import annotations

import re
from typing import Any

_LONE_SURROGATE_RE = re.compile(r"[\ud800-\udfff]")


def scrub_lone_surrogates(value: Any) -> Any:
    """Return a structurally equivalent value safe for UTF-8/SQLite binds."""
    if isinstance(value, str):
        return _LONE_SURROGATE_RE.sub("\ufffd", value)
    if isinstance(value, list):
        return [scrub_lone_surrogates(item) for item in value]
    if isinstance(value, tuple):
        return tuple(scrub_lone_surrogates(item) for item in value)
    if isinstance(value, dict):
        return {
            scrub_lone_surrogates(key): scrub_lone_surrogates(item)
            for key, item in value.items()
        }
    return value


__all__ = ["scrub_lone_surrogates"]
