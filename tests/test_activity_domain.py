from __future__ import annotations

import pytest

from hermes_team_mission.domain.activity import (
    Activity,
    ActivityCommand,
    is_legal_transition,
)


def test_activity_requires_activity_id() -> None:
    with pytest.raises(ValueError, match="activity_id is required"):
        Activity(activity_id="", kind="chat", conversation_id="conv-1")


def test_activity_rejects_unknown_kind() -> None:
    with pytest.raises(ValueError, match="kind must be one of"):
        Activity(activity_id="act-1", kind="unknown", conversation_id="conv-1")


def test_activity_requires_conversation_id() -> None:
    with pytest.raises(ValueError, match="conversation_id is required"):
        Activity(activity_id="act-1", kind="chat", conversation_id=" ")


def test_activity_command_requires_command_id() -> None:
    with pytest.raises(ValueError, match="command_id is required"):
        ActivityCommand(command_id="", activity_id="act-1", kind="create")


def test_activity_command_rejects_unknown_kind() -> None:
    with pytest.raises(ValueError, match="kind must be one of"):
        ActivityCommand(command_id="cmd-1", activity_id="act-1", kind="pause")


def test_activity_command_rejects_unknown_state() -> None:
    with pytest.raises(ValueError, match="state must be one of"):
        ActivityCommand(
            command_id="cmd-1",
            activity_id="act-1",
            kind="create",
            state="queued",
        )


def test_is_legal_transition_satisfies_state_machine() -> None:
    assert is_legal_transition("accepted", "dispatched")
    assert is_legal_transition("accepted", "satisfied")
    assert is_legal_transition("accepted", "failed")
    assert is_legal_transition("dispatched", "satisfied")
    assert is_legal_transition("dispatched", "failed")
    assert not is_legal_transition("accepted", "accepted")
    assert not is_legal_transition("dispatched", "accepted")
    assert not is_legal_transition("satisfied", "failed")
    assert not is_legal_transition("failed", "dispatched")
    assert not is_legal_transition("missing", "failed")


def test_with_state_legal_transition_succeeds() -> None:
    command = ActivityCommand(command_id="cmd-1", activity_id="act-1", kind="start")

    updated = command.with_state("dispatched")

    assert updated is not command
    assert updated.command_id == "cmd-1"
    assert updated.state == "dispatched"
    assert updated.state_changed_at > command.state_changed_at


def test_with_state_illegal_transition_raises() -> None:
    command = ActivityCommand(
        command_id="cmd-1",
        activity_id="act-1",
        kind="start",
        state="satisfied",
    )

    with pytest.raises(ValueError, match="illegal command state transition"):
        command.with_state("failed")


def test_with_state_preserves_unset_fields() -> None:
    command = ActivityCommand(
        command_id="cmd-1",
        activity_id="act-1",
        kind="complete",
        payload={"ok": True},
        metadata={"source": "test"},
    )

    updated = command.with_state("satisfied")

    assert updated.payload == {"ok": True}
    assert updated.metadata == {"source": "test"}
    assert updated.error_reason == ""
    assert updated.result_event_id is None
