"""Token-free, surface-neutral detection of user reactions."""

from __future__ import annotations

import re


VIBE = "vibe"

_VIBE_RE = re.compile(
    "|".join(
        (
            r"\bgood\s*bot\b",
            r"\bi\s*(?:love|luv)\s*(?:you|u|ya)\b",
            r"\b(?:love|luv)\s*(?:you|u|ya)\b",
            r"\bily(?:sm)?\b",
            r"\bthank\s*(?:you|u)\b",
            r"\b(?:thanks|thx|tysm|ty)\b",
            r"<3+",
            r"[\u2764\u2665"
            r"\U0001F970\U0001F60D\U0001F618"
            r"\U0001F495\U0001F496\U0001F497\U0001F49E"
            r"\U0001F49B\U0001F49C\U0001F49A\U0001F499"
            r"\U0001F493\U0001F498\U0001F49D\U0001FA77]",
        )
    ),
    re.IGNORECASE,
)


def detect_reaction(text: str | None) -> str | None:
    """Return a stable reaction kind for affection/gratitude, if any."""
    return VIBE if text and _VIBE_RE.search(text) else None
