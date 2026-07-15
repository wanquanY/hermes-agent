"""Dovie/Hermes timeline contract capability advertisement.

Structure matches frontend ``BackendCapabilities`` (v3.1 §8.2) verbatim:

    {
        "contractVersion": "3.1",
        "capabilities": {
            "cursor":      { "afterSeq", "afterId", "beforeSeq", "beforeId" },
            "history":     { "canonical" },
            "toolEvents":  { "canonical" },
        },
        "deprecations": ["runtimeSourceSeq"?]   # populated by Phase M
    }

Internal backend milestones such as ``interaction.persistent`` and
``runStateMachine.singleEntrypoint`` are intentionally omitted — they are
internal architecture guarantees that the renderer cannot route on.

See ``docs/hermes_agent_architecture_v3.md`` §11.1 (v3.0.2 handshake schema).
"""

from __future__ import annotations

from typing import Any

CONTRACT_VERSION = "3.1"


def _cursor_capabilities() -> dict[str, bool]:
    """4-arm cursor coverage.

    - ``afterSeq``  / ``beforeSeq``:  hydrate transparent cursor by canonical seq
    - ``afterId``   / ``beforeId``:   hydrate transparent cursor by row/message id
    """
    return {
        "afterSeq": True,
        "afterId": True,
        "beforeSeq": True,
        "beforeId": True,
    }


def _tool_events_capabilities() -> dict[str, bool]:
    return {"canonical": True}


def _history_capabilities() -> dict[str, bool]:
    """Historical transcript hydration is not yet fully backed by run_events.

    ``history.canonical`` may only be true once ``run_events`` can reconstruct
    the complete visible transcript, including user messages, assistant text,
    reasoning, tools, seq, and historical timestamps. Current Phase A storage
    still requires ``messages`` for the visible transcript, so advertising a
    canonical history contract would make clients drop valid history rows.
    """

    return {"canonical": False}


def _deprecations() -> list[str]:
    """Contract-level deprecation flags.

    Phase M drops ``run_events.runtime_source_seq`` — advertise ``"runtimeSourceSeq"``
    here at that point so the frontend ``capabilities.deprecations.includes(...)``
    branch can retire its fallback path (frontend Phase H3 lands first).
    """
    return []


def timeline_contract_capabilities() -> dict[str, Any]:
    """Return the cross-repo timeline contract capabilities."""

    return {
        "contractVersion": CONTRACT_VERSION,
        "capabilities": {
            "cursor": _cursor_capabilities(),
            "history": _history_capabilities(),
            "toolEvents": _tool_events_capabilities(),
        },
        "deprecations": _deprecations(),
    }


def timeline_contract_ready_payload() -> dict[str, Any]:
    """Handshake first-frame payload (spec §11.1)."""

    contract = timeline_contract_capabilities()
    return {
        "contractVersion": contract["contractVersion"],
        "capabilities": contract["capabilities"],
        "deprecations": contract["deprecations"],
    }
