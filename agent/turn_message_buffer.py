"""Working message buffer with an explicit history/current-turn boundary."""

from __future__ import annotations

import copy
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
    _hermes_current_input_message: dict[str, Any] | None

    def __init__(self, messages: Iterable[dict[str, Any]] | None = None, *, persist_from_index: int | None = None) -> None:
        super().__init__(messages or [])
        if persist_from_index is None:
            persist_from_index = len(self)
        self._hermes_persist_from_index = self._clamp_index(persist_from_index)
        self._hermes_current_input_message = None

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

    @property
    def current_input_message(self) -> dict[str, Any] | None:
        return self._hermes_current_input_message

    @property
    def current_input_index(self) -> int | None:
        current = self._hermes_current_input_message
        if current is None:
            return None
        for index, message in enumerate(self):
            if message is current:
                return index
        return None

    def bind_persisted_current_input(
        self,
        conversation_message_id: str,
    ) -> dict[str, Any] | None:
        """Bind a canonical user row that was persisted before execution.

        Team-conversation submissions are written to the shared transcript
        before their worker starts. Hydration therefore already contains the
        current input. Binding that row lets the inference loop reuse it as the
        current turn instead of appending a second copy of the same text.

        The lookup is deliberately identity-based and restricted to ``user``
        rows. Participant projection may decorate content and metadata, but it
        preserves ``conversation_message_id`` as the stable event identity.
        """
        target = str(conversation_message_id or "").strip()
        if not target:
            return None
        for message in reversed(self):
            if not isinstance(message, dict) or message.get("role") != "user":
                continue
            metadata = (
                message.get("metadata")
                if isinstance(message.get("metadata"), dict)
                else {}
            )
            candidate = str(
                message.get("conversation_message_id")
                or message.get("conversationMessageId")
                or metadata.get("conversation_message_id")
                or metadata.get("conversationMessageId")
                or ""
            ).strip()
            if candidate == target:
                self._hermes_current_input_message = message
                return message
        return None

    def append_current_input(
        self,
        content: Any,
        *,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Append and bind a runtime-owned current input exactly once."""
        message: dict[str, Any] = {"role": "user", "content": content}
        if metadata:
            message["metadata"] = dict(metadata)
        self.append(message)
        self._hermes_current_input_message = message
        return message

    def append_existing_current_input(
        self,
        message: dict[str, Any],
        *,
        api_content: Any,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Reuse a CLI-staged user dict across the close/worker handoff."""
        if not isinstance(message, dict) or message.get("role") != "user":
            raise ValueError("current input must be a user message dict")
        message["content"] = api_content
        if metadata:
            existing = message.get("metadata")
            merged = dict(existing) if isinstance(existing, dict) else {}
            merged.update(metadata)
            message["metadata"] = merged
        self.append(message)
        self._hermes_current_input_message = message
        return message

    def bind_current_input_message(
        self,
        message: dict[str, Any],
    ) -> None:
        """Bind an existing row after a whole-buffer rewrite."""
        if not any(candidate is message for candidate in self):
            raise ValueError("current input message must belong to this buffer")
        self._hermes_current_input_message = message

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


def reanchor_current_input(
    messages: list[Any],
    *,
    conversation_message_id: str = "",
    content_candidates: tuple[Any, ...] = (),
    restore_message: dict[str, Any] | None = None,
) -> tuple[int, dict[str, Any] | None]:
    """Locate or restore the canonical current input after a list rewrite.

    An arbitrary latest user row is not a valid fallback: it may be a
    user-role compression summary or runtime scaffold, and treating it as the
    current input lets API-only content overlay overwrite that history.
    """
    stable_message_id = str(conversation_message_id or "").strip()
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        metadata = (
            message.get("metadata")
            if isinstance(message.get("metadata"), dict)
            else {}
        )
        candidate_id = str(
            message.get("conversation_message_id")
            or metadata.get("conversation_message_id")
            or ""
        ).strip()
        if stable_message_id and candidate_id == stable_message_id:
            if isinstance(messages, TurnMessageBuffer):
                messages.bind_current_input_message(message)
            return index, message
        if any(message.get("content") == candidate for candidate in content_candidates):
            if isinstance(messages, TurnMessageBuffer):
                messages.bind_current_input_message(message)
            return index, message
    if isinstance(restore_message, dict) and restore_message.get("role") == "user":
        restored = copy.deepcopy(restore_message)
        restored.pop("_db_persisted", None)
        messages.append(restored)
        if isinstance(messages, TurnMessageBuffer):
            messages.bind_current_input_message(restored)
        return len(messages) - 1, restored
    return -1, None
