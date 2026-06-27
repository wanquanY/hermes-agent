"""Stable Team Mission identity helpers."""

from __future__ import annotations

from typing import Any


def _text(value: Any) -> str:
    return str(value or "").strip()


def canonical_node_id(mission_id: str, node_id: str) -> str:
    """Return the conversation-wide identity for a mission graph node.

    ``node_id`` remains mission-local for graph lookups. This helper is for
    cross-mission projections where two missions in one conversation may reuse
    the same local node id.

    CR-P3.3: graph identity only; for speaker use participant_id.
    """

    mission_id = _text(mission_id)
    node_id = _text(node_id)
    if not node_id:
        return ""
    if mission_id and (
        node_id.startswith(f"{mission_id}:")
        or node_id.startswith(f"team-mission:{mission_id}:")
    ):
        return node_id
    return f"{mission_id}:{node_id}" if mission_id else node_id


__all__ = ["canonical_node_id"]
