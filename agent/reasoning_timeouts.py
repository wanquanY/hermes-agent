"""Stale-timeout policy for models with long reasoning phases.

Reasoning models can remain silent for several minutes before their first
content token.  This module owns the model-family policy used by both the
streaming and non-streaming watchdogs so transport code does not grow its own
inconsistent substring checks.

The returned value is a default floor, not a hard override.  Callers must
continue to honor an explicit provider/model timeout chosen by the user.
"""

from __future__ import annotations

import re
from typing import Optional


_REASONING_STALE_TIMEOUT_FLOORS: tuple[tuple[str, int], ...] = (
    ("nemotron-3-ultra", 600),
    ("nemotron-3-super", 600),
    ("nemotron-3-nano", 300),
    ("deepseek-r1", 600),
    ("deepseek-reasoner", 600),
    ("deepseek-v4-flash", 600),
    ("deepseek-v4-pro", 600),
    ("qwq-32b", 300),
    ("qwen3", 180),
    ("o1-preview", 600),
    ("o1-mini", 600),
    ("o1-pro", 600),
    ("o1", 600),
    ("o3-mini", 300),
    ("o3-pro", 600),
    ("o3", 600),
    ("o4-mini", 300),
    ("claude-opus-4", 240),
    ("claude-sonnet-5", 180),
    ("claude-sonnet-4.5", 180),
    ("claude-sonnet-4.6", 180),
    ("grok-4-fast-reasoning", 300),
    ("grok-4.20-reasoning", 300),
    ("grok-4.5", 300),
    ("grok-4-fast-non-reasoning", 180),
)

_PATTERN_CACHE: dict[str, re.Pattern[str]] = {}


def _model_pattern(slug: str) -> re.Pattern[str]:
    pattern = _PATTERN_CACHE.get(slug)
    if pattern is None:
        pattern = re.compile(r"^" + re.escape(slug) + r"(?:$|[-._])")
        _PATTERN_CACHE[slug] = pattern
    return pattern


def get_reasoning_stale_timeout_floor(model: object) -> Optional[float]:
    """Return the default stale-timeout floor for a reasoning model.

    Aggregator namespaces are stripped and matching is anchored to the start
    of the remaining model slug.  This prevents names such as
    ``llama-4-70b-o1-preview`` from being mistaken for OpenAI's ``o1`` family.
    More-specific slugs win when families share a prefix.
    """
    if not isinstance(model, str) or not model.strip():
        return None
    name = model.strip().lower().rsplit("/", 1)[-1]
    for slug, floor in sorted(
        _REASONING_STALE_TIMEOUT_FLOORS,
        key=lambda item: len(item[0]),
        reverse=True,
    ):
        if _model_pattern(slug).search(name):
            return float(floor)
    return None


__all__ = ["get_reasoning_stale_timeout_floor"]
