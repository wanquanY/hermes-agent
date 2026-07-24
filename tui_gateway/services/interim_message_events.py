"""Gateway event adapter for mid-turn assistant commentary."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any


def create_interim_assistant_callback(
    *,
    emit: Callable[[str, str, dict[str, Any]], Any],
    session_id: str,
    identity_payload: Callable[[], Mapping[str, Any]] | None = None,
) -> Callable[..., Any]:
    """Create the one canonical ``message.interim`` event publisher."""

    def callback(
        commentary: str,
        *,
        already_streamed: bool = False,
        transcript_visibility: str = "",
        synthetic_kind: str = "",
    ) -> Any:
        visibility = str(transcript_visibility or "").strip()
        kind = str(synthetic_kind or "").strip()
        return emit(
            "message.interim",
            session_id,
            {
                "text": str(commentary),
                "already_streamed": bool(already_streamed),
                **(dict(identity_payload()) if identity_payload is not None else {}),
                **({"transcript_visibility": visibility} if visibility else {}),
                **({"synthetic_kind": kind} if kind else {}),
            },
        )

    return callback


__all__ = ["create_interim_assistant_callback"]
