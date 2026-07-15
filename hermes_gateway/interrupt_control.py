"""Internal interruption reason classification."""

from __future__ import annotations

from typing import Optional


INTERRUPT_REASON_STOP = "Stop requested"
INTERRUPT_REASON_RESET = "Session reset requested"
INTERRUPT_REASON_TIMEOUT = "Execution timed out (inactivity)"
INTERRUPT_REASON_SSE_DISCONNECT = "SSE client disconnected"
INTERRUPT_REASON_GATEWAY_SHUTDOWN = "Gateway shutting down"
INTERRUPT_REASON_GATEWAY_RESTART = "Gateway restarting"

CONTROL_INTERRUPT_MESSAGES = frozenset(
    {
        INTERRUPT_REASON_STOP.lower(),
        INTERRUPT_REASON_RESET.lower(),
        INTERRUPT_REASON_TIMEOUT.lower(),
        INTERRUPT_REASON_SSE_DISCONNECT.lower(),
        INTERRUPT_REASON_GATEWAY_SHUTDOWN.lower(),
        INTERRUPT_REASON_GATEWAY_RESTART.lower(),
    }
)


def is_control_interrupt_message(message: Optional[str]) -> bool:
    """Return True when an interrupt message is internal control flow."""
    if not message:
        return False
    normalized = " ".join(str(message).strip().split()).lower()
    return normalized in CONTROL_INTERRUPT_MESSAGES
