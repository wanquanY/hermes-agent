"""Shared parsing for session-scoped runtime slash commands.

Model, reasoning, and fast-mode commands all follow one persistence contract:
the current conversation is the default owner, while ``--global`` explicitly
writes durable config. Surface-specific services still own state changes;
this module only normalizes the command grammar.
"""

from __future__ import annotations

from dataclasses import dataclass
import logging

logger = logging.getLogger(__name__)

_UNICODE_DASHES = ("—", "–", "−")


@dataclass(frozen=True)
class SessionScopedArgs:
    value: str
    persist_global: bool
    explicit_session: bool


def parse_session_scoped_args(raw_args: str) -> SessionScopedArgs:
    """Strip scope flags and return their normalized intent.

    Flags may appear in any position and Unicode dash variants are accepted.
    If both scopes are supplied, ``--global`` wins; this preserves historical
    command behavior while making the durable side effect explicit.
    """
    normalized = str(raw_args or "")
    for dash in _UNICODE_DASHES:
        normalized = normalized.replace(dash, "--")
    value_parts: list[str] = []
    persist_global = False
    explicit_session = False
    for token in normalized.split():
        lowered = token.lower()
        if lowered == "--global":
            persist_global = True
        elif lowered == "--session":
            explicit_session = True
        else:
            value_parts.append(token)
    return SessionScopedArgs(
        value=" ".join(value_parts).strip(),
        persist_global=persist_global,
        explicit_session=explicit_session,
    )


def parse_service_tier(raw: object) -> str | None:
    """Normalize persisted fast-mode values to the runtime service tier."""
    value = str(raw or "").strip().lower()
    if not value or value in {"normal", "default", "standard", "off", "none"}:
        return None
    if value in {"fast", "priority", "on"}:
        return "priority"
    logger.warning("Unknown service_tier '%s', ignoring", raw)
    return None
