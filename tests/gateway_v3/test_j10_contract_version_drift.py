"""spec §J10 — Contract version negotiation.

Three sources of truth for the wire contract version must agree:

1. ``tui_gateway.services.contract_capabilities.CONTRACT_VERSION`` —
   the module constant the frontend imports.
2. ``timeline_contract_capabilities()`` payload dict — what the handshake
   builder actually reads.
3. ``build_handshake_frame()`` output — what actually goes on the wire.

If any two disagree the frontend enters a broken state (spec §11.2 says
the frontend gates on ``contractVersion`` and refuses to hydrate
history). This test locks the version at the current v3.0.2/v3.0.3 value
of ``"3.1"`` and cross-checks all three sources.
"""

from __future__ import annotations

from hermes_agent.gateway import build_handshake_frame
from tui_gateway.services.contract_capabilities import (
    CONTRACT_VERSION,
    timeline_contract_capabilities,
    timeline_contract_ready_payload,
)


# The v3.0.2/v3.0.3 wire contract version. Bumping this requires:
#   - frontend BackendCapabilities version bump
#   - spec §11.1 update
#   - migration path documented for old clients
EXPECTED_CONTRACT_VERSION = "3.1"


def test_j10_contract_version_constant_matches_expected():
    assert CONTRACT_VERSION == EXPECTED_CONTRACT_VERSION


def test_j10_timeline_contract_capabilities_uses_the_same_version():
    payload = timeline_contract_capabilities()
    assert payload["contractVersion"] == EXPECTED_CONTRACT_VERSION


def test_j10_timeline_contract_ready_payload_matches():
    payload = timeline_contract_ready_payload()
    assert payload["contractVersion"] == EXPECTED_CONTRACT_VERSION


def test_j10_built_handshake_frame_matches():
    frame = build_handshake_frame()
    assert frame["contractVersion"] == EXPECTED_CONTRACT_VERSION


def test_j10_capabilities_schema_locked_to_frontend_v3_1():
    """spec §11.1 nested capabilities structure — flat drift would break
    ``BackendCapabilities.cursor.afterSeq`` style property access on the
    frontend.
    """
    frame = build_handshake_frame()
    caps = frame["capabilities"]

    # Three top-level buckets exactly (no new bucket without frontend
    # coordination).
    assert set(caps.keys()) == {"cursor", "history", "toolEvents"}

    # Cursor: exactly the 4 arms of spec §11.1.
    assert set(caps["cursor"].keys()) == {"afterSeq", "afterId", "beforeSeq", "beforeId"}
    # All four arms must be true — landing target for v3.0.2.
    assert all(v is True for v in caps["cursor"].values())

    # History: canonical is the only key.
    assert set(caps["history"].keys()) == {"canonical"}

    # ToolEvents: canonical is the only key.
    assert set(caps["toolEvents"].keys()) == {"canonical"}


def test_j10_deprecations_field_is_present_as_list():
    """Frontend v3.1 §5 requires ``deprecations: string[]`` (may be empty).
    Missing key or wrong type = deprecation branches don't render.
    """
    frame = build_handshake_frame()
    assert "deprecations" in frame
    assert isinstance(frame["deprecations"], list)
    for item in frame["deprecations"]:
        assert isinstance(item, str), (
            f"deprecations entry is not str: {item!r} ({type(item).__name__})"
        )


def test_j10_no_internal_milestones_leak_to_wire():
    """spec §11.1 explicitly forbids these internal milestones on the wire —
    the frontend cannot route on them, and their presence would violate
    the boundary between architecture and contract.
    """
    frame = build_handshake_frame()
    caps = frame["capabilities"]
    banned = {
        "interaction.persistent",
        "runStateMachine.singleEntrypoint",
        "since",
        "runtimeSourceSeq.deprecated",
        "internal",
    }
    for key in banned:
        assert key not in caps, f"internal milestone {key!r} leaked into wire"
        assert key not in frame, f"internal milestone {key!r} leaked at top level"


def test_j10_handshake_type_field_is_stable():
    """Frame type is a wire-level dispatch key; must be exactly
    ``handshake`` (spec §11.1).
    """
    frame = build_handshake_frame()
    assert frame.get("type") == "handshake"
