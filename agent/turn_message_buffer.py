"""Working message buffer with an explicit history/current-turn boundary."""

from __future__ import annotations

from typing import Any, Iterable


class TurnMessageBuffer(list):
    """List used by the agent loop while preserving where loaded history ends.

    The conversation loop needs a single list for model context because providers
    expect prior messages and current-turn messages in order. Persistence has a
    different contract: only messages appended during the current turn are new
    transcript rows. The boundary is part of this buffer so DB flush never has to
    infer it from an optional ``conversation_history`` argument.
    """

    _hermes_persist_from_index: int

    def __init__(self, messages: Iterable[dict[str, Any]] | None = None, *, persist_from_index: int | None = None) -> None:
        super().__init__(messages or [])
        if persist_from_index is None:
            persist_from_index = len(self)
        self._hermes_persist_from_index = self._clamp_index(persist_from_index)

    @classmethod
    def from_history(cls, history: Iterable[dict[str, Any]] | None) -> "TurnMessageBuffer":
        return cls(history, persist_from_index=None)

    @classmethod
    def full_snapshot(cls, messages: Iterable[dict[str, Any]] | None) -> "TurnMessageBuffer":
        return cls(messages, persist_from_index=0)

    @property
    def persist_from_index(self) -> int:
        return self._clamp_index(self._hermes_persist_from_index)

    def mark_full_snapshot(self) -> None:
        self._hermes_persist_from_index = 0

    def _clamp_index(self, value: int | None) -> int:
        try:
            index = int(value if value is not None else len(self))
        except (TypeError, ValueError):
            index = len(self)
        return max(0, min(index, len(self)))


def message_persist_boundary(messages: Any) -> int | None:
    """Return the explicit persistence boundary carried by a turn buffer."""

    if isinstance(messages, TurnMessageBuffer):
        return messages.persist_from_index
    value = getattr(messages, "_hermes_persist_from_index", None)
    if value is None:
        return None
    try:
        return max(0, min(int(value), len(messages)))
    except (TypeError, ValueError):
        return None
