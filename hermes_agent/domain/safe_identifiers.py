"""Validation and filesystem encoding for externally influenced identifiers."""

from __future__ import annotations

import hashlib
import re

_EXTERNAL_SESSION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,255}$")
_PORTABLE_FILENAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_UNSAFE_FILENAME_RUN = re.compile(r"[^A-Za-z0-9._-]+")


def validate_external_session_id(value: object) -> str:
    """Return a safe API session ID or raise ``ValueError``.

    Domain session IDs created internally may use richer syntax. This strict
    validator is for untrusted API creation/continuation boundaries only.
    """
    stable = str(value or "").strip()
    if not _EXTERNAL_SESSION_ID.fullmatch(stable) or stable in {".", ".."}:
        raise ValueError(
            "session ID must be 1-256 portable characters: letters, digits, '.', '_' or '-'"
        )
    return stable


def validate_path_component(value: object, *, label: str = "identifier") -> str:
    """Return one portable path component or raise ``ValueError``."""
    stable = str(value or "").strip()
    if not _PORTABLE_FILENAME.fullmatch(stable) or stable in {".", ".."}:
        raise ValueError(f"invalid {label}")
    return stable


def safe_filename_component(value: object, *, fallback: str = "unknown") -> str:
    """Encode an identifier as one stable, collision-resistant path segment."""
    stable = str(value or "").strip()
    if _PORTABLE_FILENAME.fullmatch(stable) and stable not in {".", ".."}:
        return stable
    digest = hashlib.sha256(stable.encode("utf-8", errors="surrogatepass")).hexdigest()[:16]
    slug = _UNSAFE_FILENAME_RUN.sub("_", stable).strip("._-")[:72]
    return f"{slug or fallback}-{digest}"


__all__ = ["safe_filename_component", "validate_external_session_id", "validate_path_component"]
