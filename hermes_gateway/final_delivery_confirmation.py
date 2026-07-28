"""Canonical proof that a gateway final response reached the user."""

from __future__ import annotations

from typing import Any


def stream_confirmed_final_delivery(
    consumer: Any,
    final_text: str,
    *,
    previewed: bool = False,
) -> bool:
    """Return whether the exact final response has confirmed delivery.

    ``response_previewed`` alone is not proof: an interim callback may have
    delivered unrelated commentary before a compression/session split. When
    previewing is the only signal, ask the stream consumer whether that exact
    visible text was delivered.
    """
    if consumer is None:
        return False
    if getattr(consumer, "final_response_sent", False):
        return True
    if getattr(consumer, "final_content_delivered", False):
        return True
    if not previewed:
        return False
    has_delivered_text = getattr(consumer, "has_delivered_text", None)
    if not callable(has_delivered_text):
        return False
    try:
        return bool(has_delivered_text(final_text))
    except Exception:
        return False
