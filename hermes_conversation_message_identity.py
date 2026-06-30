from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any


def _text(value: Any) -> str:
    return str(value or "").strip()


@dataclass(frozen=True)
class AssistantMessageIdentity:
    session_id: str
    run_id: str
    message_seq_in_run: str


def assistant_conversation_message_id_for(identity: AssistantMessageIdentity) -> str:
    raw = f"{identity.session_id}\0{identity.run_id}\0{identity.message_seq_in_run}"
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]
    return f"msg_{digest}"


def user_conversation_message_id_for(
    *,
    session_id: str,
    turn_id: str,
    run_id: str = "",
    client_message_id: str = "",
) -> str:
    """Stable id for a user-submission row in a visible conversation."""
    stable_session_id = _text(session_id)
    stable_turn_id = _text(turn_id)
    stable_run_id = _text(run_id)
    stable_client_message_id = _text(client_message_id)
    identity = stable_turn_id or stable_client_message_id or stable_run_id
    if not stable_session_id or not identity:
        raise ValueError("session_id and one user message identity are required")
    raw = f"{stable_session_id}\0user\0{identity}"
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]
    return f"msg_{digest}"
