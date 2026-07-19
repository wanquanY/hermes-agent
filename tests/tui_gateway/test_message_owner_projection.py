from __future__ import annotations

import pytest

from tui_gateway.services.message_owner_projection import (
    MessageOwnerResolutionError,
    project_render_message_owners,
)


def test_direct_projection_recovers_legacy_user_and_agent_owners() -> None:
    messages = project_render_message_owners(
        [
            {"id": "message-1", "role": "user", "text": "question"},
            {"id": "message-2", "role": "assistant", "text": "answer"},
        ],
        run_events=[],
        participants=[
            {"participant_id": "user", "role": "user"},
            {"participant_id": "agent:profile-1", "role": "agent"},
        ],
        allow_single_execution_participant=True,
    )

    assert [message["participant_id"] for message in messages] == [
        "user",
        "agent:profile-1",
    ]


def test_team_projection_uses_correlated_run_owner() -> None:
    messages = project_render_message_owners(
        [
            {
                "id": "message-1",
                "role": "assistant",
                "text": "answer",
                "metadata": {"run_id": "run-1"},
            },
        ],
        run_events=[
            {
                "type": "message.complete",
                "run_id": "run-1",
                "participant_id": "member:writer",
            },
        ],
        participants=[
            {"participant_id": "leader:team-1", "role": "leader"},
            {"participant_id": "member:writer", "role": "member"},
        ],
        allow_single_execution_participant=False,
    )

    assert messages[0]["participant_id"] == "member:writer"


def test_conflicting_run_owners_do_not_claim_a_render_ready_message() -> None:
    with pytest.raises(MessageOwnerResolutionError, match="owner identity unavailable"):
        project_render_message_owners(
            [
                {
                    "id": "message-1",
                    "role": "assistant",
                    "text": "answer",
                    "metadata": {"run_id": "run-1"},
                },
            ],
            run_events=[
                {"run_id": "run-1", "participant_id": "member:writer"},
                {"run_id": "run-1", "participant_id": "member:reviewer"},
            ],
            participants=[
                {"participant_id": "member:writer", "role": "member"},
                {"participant_id": "member:reviewer", "role": "member"},
            ],
            allow_single_execution_participant=False,
        )
