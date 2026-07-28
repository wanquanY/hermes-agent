from __future__ import annotations

import pytest

from hermes_agent.domain.run_identity import (
    CrossWiredRunError,
    RunIdentity,
    ensure_run_identity_compatible,
)


def test_identity_normalizes_scope_to_session():
    identity = RunIdentity.create(run_id="run-1", session_id="session-1")

    assert identity.runtime_scope_key == "session-1"


def test_empty_worker_and_profile_are_claimable_once():
    persisted = RunIdentity.create(run_id="run-1", session_id="session-1")
    incoming = RunIdentity.create(
        run_id="run-1",
        session_id="session-1",
        worker_id="worker-1",
        agent_profile_id="profile-1",
        require_worker=True,
    )

    claimed = persisted.claimed_with(incoming)

    assert claimed.worker_id == "worker-1"
    assert claimed.agent_profile_id == "profile-1"


@pytest.mark.parametrize(
    ("field", "change"),
    [
        ("session_id", {"session_id": "session-2"}),
        ("worker_id", {"worker_id": "worker-2"}),
        ("runtime_scope_key", {"runtime_scope_key": "scope-2"}),
        ("agent_profile_id", {"agent_profile_id": "profile-2"}),
    ],
)
def test_claimed_identity_rejects_each_cross_wire_dimension(field, change):
    values = {
        "run_id": "run-1",
        "session_id": "session-1",
        "worker_id": "worker-1",
        "runtime_scope_key": "scope-1",
        "agent_profile_id": "profile-1",
    }
    existing = RunIdentity.create(**values)
    incoming = RunIdentity.create(**{**values, **change})

    with pytest.raises(CrossWiredRunError) as raised:
        ensure_run_identity_compatible(existing, incoming)

    assert raised.value.mismatch_fields == (field,)
    assert raised.value.details["run_id"] == "run-1"
