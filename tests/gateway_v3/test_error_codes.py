"""Phase G — ErrorCode enum + err() signature guard (spec §J9)."""

from __future__ import annotations

import pytest

from hermes_agent.gateway import ErrorCode, err


def test_err_requires_error_code_enum():
    """spec §J9 — the code argument must be ErrorCode, not a magic string."""
    with pytest.raises(TypeError, match="ErrorCode"):
        err("req-1", "4001", "bad")  # type: ignore[arg-type]


def test_err_signature_shape():
    resp = err("req-1", ErrorCode.INVALID_PARAMS, "missing session_id", field="session_id")
    assert resp["id"] == "req-1"
    assert resp["error"]["code"] == ErrorCode.INVALID_PARAMS.value
    assert resp["error"]["message"] == "missing session_id"
    assert resp["error"]["details"] == {"field": "session_id"}


def test_error_code_ranges():
    """Range convention lets ops route by first digit."""
    assert ErrorCode.UNKNOWN_METHOD.value.startswith("4")
    assert ErrorCode.SEQ_ALLOCATOR_BUSY.value.startswith("5")
    assert ErrorCode.CONTRACT_VERSION_MISMATCH.value.startswith("6")


def test_error_codes_are_unique_strings():
    values = [c.value for c in ErrorCode]
    assert len(set(values)) == len(values), "ErrorCode enum has duplicate values"


def test_common_codes_present():
    """Spot-check that codes for spec-referenced flows exist."""
    assert ErrorCode.SEQ_ALLOCATOR_BUSY.value == "5002"
    assert ErrorCode.PERMISSION_DENIED.value == "4003"
    assert ErrorCode.UNKNOWN_METHOD.value == "4001"
    assert ErrorCode.CONTRACT_VERSION_MISMATCH.value == "6001"
