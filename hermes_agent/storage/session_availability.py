"""User-facing session-store availability messages."""

from __future__ import annotations

_WAL_INCOMPAT_MARKERS = (
    "locking protocol",
    "network filesystem",
    "nfs",
    "smb",
    "fuse",
)


def format_session_store_unavailable(
    cause: str | None = None,
    *,
    prefix: str = "Session database not available",
) -> str:
    normalized = str(cause or "").strip()
    if not normalized:
        return f"{prefix}."
    hint = ""
    if any(marker in normalized.lower() for marker in _WAL_INCOMPAT_MARKERS):
        hint = " (state.db may be on NFS/SMB/FUSE - see https://www.sqlite.org/wal.html)"
    return f"{prefix}: {normalized}{hint}."


__all__ = ["format_session_store_unavailable"]
