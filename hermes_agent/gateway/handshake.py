"""Handshake payload (spec §11, Phase H)."""

from __future__ import annotations

from typing import Any

from tui_gateway.services.contract_capabilities import (
    CONTRACT_VERSION,
    timeline_contract_ready_payload,
)


HANDSHAKE_FRAME_TYPE = "handshake"


def build_handshake_frame() -> dict[str, Any]:
    """First-frame handshake per spec §11.1. Layout is dictated by the frontend
    ``BackendCapabilities`` type — see docs §11.1 for the frozen schema.
    """
    payload = timeline_contract_ready_payload()
    return {
        "type": HANDSHAKE_FRAME_TYPE,
        "contractVersion": payload["contractVersion"],
        "capabilities": payload["capabilities"],
        "deprecations": payload["deprecations"],
    }


__all__ = ["build_handshake_frame", "HANDSHAKE_FRAME_TYPE", "CONTRACT_VERSION"]
