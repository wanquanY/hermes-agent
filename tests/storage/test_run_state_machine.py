from __future__ import annotations

from hermes_agent.domain.run_state_machine import error_for_status
from hermes_agent.domain.run_state_machine import prefer_terminal_run_status
from hermes_agent.domain.run_state_machine import resolve_explicit_run_status
from hermes_agent.domain.run_state_machine import resolve_run_status_transition
from hermes_agent.domain.run_state_machine import terminal_status_from_event


def test_terminal_status_from_event_normalizes_runtime_payloads() -> None:
    assert terminal_status_from_event("error", {}) == "failed"
    assert terminal_status_from_event("session.recalled", {}) == "interrupted"
    assert terminal_status_from_event("message.complete", {"status": "canceled"}) == "cancelled"
    assert terminal_status_from_event("message.complete", {"status": "failed"}) == "failed"
    assert terminal_status_from_event("message.complete", {"status": "complete"}) == "completed"
    assert terminal_status_from_event("message.delta", {}) is None


def test_terminal_status_preference_keeps_completed_over_failed_duplicates() -> None:
    assert prefer_terminal_run_status("completed", "failed") == "completed"
    assert prefer_terminal_run_status("failed", "completed") == "completed"
    assert prefer_terminal_run_status("interrupted", "cancelled") == "interrupted"


def test_event_transition_preserves_terminal_run_on_nonterminal_late_frame() -> None:
    transition = resolve_run_status_transition(
        event_type="tool.progress",
        existing_status="completed",
        terminal_status=None,
        has_existing_run=True,
    )

    assert transition.should_track is True
    assert transition.status == "completed"
    assert transition.opens_active is True


def test_event_transition_opens_running_for_lifecycle_events() -> None:
    transition = resolve_run_status_transition(
        event_type="message.start",
        existing_status="",
        terminal_status=None,
        has_existing_run=False,
    )

    assert transition.should_track is True
    assert transition.status == "running"
    assert transition.opens_active is True


def test_event_transition_ignores_control_only_untracked_events() -> None:
    transition = resolve_run_status_transition(
        event_type="session.info",
        existing_status="",
        terminal_status=None,
        has_existing_run=False,
    )

    assert transition.should_track is False
    assert transition.status == ""
    assert transition.opens_active is False


def test_explicit_status_update_does_not_reopen_terminal_run() -> None:
    assert resolve_explicit_run_status(existing_status="completed", incoming_status="running") == "completed"
    assert resolve_explicit_run_status(existing_status="failed", incoming_status="completed") == "completed"
    assert resolve_explicit_run_status(existing_status="running", incoming_status="failed") == "failed"


def test_error_for_status_clears_successful_terminal_error() -> None:
    assert error_for_status(status="completed", payload={"message": "ignored"}, existing_error="old") == ""
    assert error_for_status(status="failed", payload={"message": "new"}, existing_error="old") == "new"
    assert error_for_status(status="running", payload={}, existing_error="old") == "old"
    assert (
        error_for_status(
            status="failed",
            payload={"message": "runtime"},
            existing_error="old",
            duplicate_active_error="duplicate",
        )
        == "duplicate"
    )
