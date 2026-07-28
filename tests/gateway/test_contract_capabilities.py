"""Phase H — handshake capability schema (spec §11.1) vs frontend BackendCapabilities."""

from __future__ import annotations

import pytest

from tui_gateway.services.contract_capabilities import (
    CONTRACT_VERSION,
    timeline_contract_capabilities,
    timeline_contract_ready_payload,
)


def test_contract_version_is_3_1():
    """Frontend v3.1 pins the same string."""
    assert CONTRACT_VERSION == "3.1"


def test_capabilities_structure_matches_frontend_type():
    """BackendCapabilities has exactly: contractVersion, capabilities.cursor,
    capabilities.history, capabilities.toolEvents, deprecations. Any extra top-level key is a spec drift.
    """
    contract = timeline_contract_capabilities()

    assert set(contract.keys()) == {"contractVersion", "capabilities", "deprecations"}
    assert isinstance(contract["contractVersion"], str)
    assert isinstance(contract["capabilities"], dict)
    assert isinstance(contract["deprecations"], list)

    caps = contract["capabilities"]
    assert set(caps.keys()) == {"cursor", "history", "toolEvents"}, (
        "internal backend milestones (interaction.persistent / "
        "runStateMachine.singleEntrypoint / since) must NOT leak into handshake"
    )


def test_cursor_capabilities_full_4_arm():
    """spec §11.1 — cursor block covers afterSeq / afterId / beforeSeq / beforeId."""
    contract = timeline_contract_capabilities()
    cursor = contract["capabilities"]["cursor"]

    assert set(cursor.keys()) == {"afterSeq", "afterId", "beforeSeq", "beforeId"}
    assert all(isinstance(v, bool) for v in cursor.values())
    # Backend claims full 4-arm cursor support at v3.1.
    assert cursor == {
        "afterSeq": True,
        "afterId": True,
        "beforeSeq": True,
        "beforeId": True,
    }


def test_tool_events_canonical_true():
    """spec §11.1 — toolEvents.canonical=True (DEBT-3 already resolved backend-side)."""
    contract = timeline_contract_capabilities()
    tool_events = contract["capabilities"]["toolEvents"]

    assert set(tool_events.keys()) == {"canonical"}
    assert tool_events["canonical"] is True


def test_history_canonical_false_until_run_events_cover_visible_transcript():
    """run_events cannot advertise canonical history before covering the visible transcript."""
    contract = timeline_contract_capabilities()
    history = contract["capabilities"]["history"]

    assert set(history.keys()) == {"canonical"}
    assert history["canonical"] is False


def test_deprecations_is_string_list_not_flat_boolean():
    """spec §11.1 (v3.0.2 fix) — deprecations is a list[str], NOT flat
    ``runtimeSourceSeq.deprecated: bool``. Frontend v3.1 §8.2 expects
    ``readonly deprecations: readonly string[]`` and uses
    ``capabilities.deprecations.includes("runtimeSourceSeq")`` for gating.
    """
    contract = timeline_contract_capabilities()

    assert isinstance(contract["deprecations"], list)
    # Element type check (empty for now; Phase M appends "runtimeSourceSeq").
    for entry in contract["deprecations"]:
        assert isinstance(entry, str)


def test_no_internal_milestone_leakage():
    """These fields belong to the backend internal state map, not the wire contract."""
    contract = timeline_contract_capabilities()
    caps = contract["capabilities"]

    forbidden_top_keys = {
        "interaction.persistent",
        "runStateMachine.singleEntrypoint",
        "since",
        # legacy flat boolean form (pre-v3.0.2)
        "runtimeSourceSeq.deprecated",
        "sessionHistory.deprecated",
    }
    for key in forbidden_top_keys:
        assert key not in caps, f"{key!r} must not leak into handshake capabilities"
        assert key not in contract, f"{key!r} must not leak into handshake top level"


def test_ready_payload_mirrors_contract():
    """timeline_contract_ready_payload is the first-frame payload projection."""
    payload = timeline_contract_ready_payload()
    contract = timeline_contract_capabilities()

    assert payload["contractVersion"] == contract["contractVersion"]
    assert payload["capabilities"] == contract["capabilities"]
    assert payload["deprecations"] == contract["deprecations"]
