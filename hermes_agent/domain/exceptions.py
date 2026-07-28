"""Domain-level exceptions."""

from __future__ import annotations


class StorageBusyError(RuntimeError):
    """Raised when SQLite storage remains busy after bounded retries."""


class SeqAllocatorBusy(StorageBusyError):
    """Raised when canonical sequence allocation cannot acquire SQLite write access."""
