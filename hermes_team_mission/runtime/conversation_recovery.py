from __future__ import annotations

from typing import Any

from tui_gateway.services import run_control


def _text(value: Any) -> str:
    return str(value or "").strip()


def recover_conversation_active_run(
    db: Any,
    conversation: dict | None,
    *,
    current_gateway_instance_id: str = "",
) -> dict[str, Any]:
    """Read canonical run state after one-time storage migrations complete."""
    if not isinstance(conversation, dict):
        return {}
    conversation_session_id = _text(
        conversation.get("conversation_session_id")
        or conversation.get("conversationSessionId")
    )
    if not conversation_session_id:
        return {}
    return run_control.session_status(
        conversation_session_id,
        db=db,
        current_gateway_instance_id=current_gateway_instance_id,
    )
