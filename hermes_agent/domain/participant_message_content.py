"""Canonical participant-message content rules.

Participant identity is metadata in the canonical transcript and must never be
persisted in its natural-language message body. Participant-specific provider
views may add a transient ownership envelope for foreign assistant utterances;
that projection must not flow back into storage. This module contains the
narrow compatibility parser used to clean historical persisted envelopes and
to make transient projection idempotent.
"""

from __future__ import annotations

import re
from collections.abc import Iterable


LEGACY_SPEAKER_ENVELOPE_VERSION = "participant-speaker-v1"

_LEGACY_SPEAKER_ENVELOPE_RE = re.compile(
    r"\A\[(?P<role>user|assistant)\s*\|\s*"
    r"(?P<ownership>[^\]|\r\n]+?)\s*\|\s*"
    r"(?P<participant_id>[^\]|\r\n]+?)\]"
    r"[ \t]*(?:\r?\n)?"
)


def strip_legacy_speaker_envelopes(
    content: str,
    *,
    participant_id: str = "",
    known_participant_ids: Iterable[str] = (),
    trusted_legacy_projection: bool = False,
) -> tuple[str, int]:
    """Strip validated legacy speaker envelopes from the start of ``content``.

    The parser is deliberately strict and start-anchored.  An envelope is
    removed only when its participant identity is known for the conversation,
    agrees with the canonical message participant, or is explicitly trusted by
    legacy projection metadata.  Repeated prefixes are removed because models
    can echo a polluted history prefix on later turns.
    """

    canonical_participant = str(participant_id or "").strip()
    allowed_identities = {
        str(value or "").strip()
        for value in known_participant_ids
        if str(value or "").strip()
    }
    if canonical_participant:
        allowed_identities.add(canonical_participant)
    allowed_identities.update({"user", "unknown"})

    cleaned = content
    removed = 0
    while cleaned:
        match = _LEGACY_SPEAKER_ENVELOPE_RE.match(cleaned)
        if match is None:
            break
        envelope_participant = match.group("participant_id").strip()
        if (
            not trusted_legacy_projection
            and envelope_participant not in allowed_identities
        ):
            break
        cleaned = cleaned[match.end() :]
        removed += 1
    return cleaned, removed


__all__ = [
    "LEGACY_SPEAKER_ENVELOPE_VERSION",
    "strip_legacy_speaker_envelopes",
]
