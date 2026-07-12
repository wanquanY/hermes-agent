from hermes_agent.domain.participant_transcript_projector import (
    project_participant_transcript,
)


PARTICIPANTS = [
    {"participant_id": "user", "display_name": "Yang", "role": "user"},
    {"participant_id": "leader:conv", "display_name": "Leader", "role": "leader"},
    {"participant_id": "member:a", "display_name": "Alice", "role": "member"},
    {"participant_id": "member:b", "display_name": "Bob", "role": "member"},
]


def test_projection_attributes_other_assistants_as_user_context() -> None:
    projected = project_participant_transcript(
        [
            {"role": "user", "content": "开始", "participant_id": "user"},
            {"role": "assistant", "content": "我来协调", "participant_id": "leader:conv"},
            {"role": "assistant", "content": "我来研究", "participant_id": "member:a"},
            {"role": "assistant", "content": "我来测试", "participant_id": "member:b"},
        ],
        viewing_participant_id="member:a",
        participants=PARTICIPANTS,
    )

    assert [message["role"] for message in projected] == [
        "user",
        "user",
        "assistant",
        "user",
    ]
    assert projected[0]["content"].startswith("[user | Yang | user]")
    assert projected[1]["content"].startswith("[assistant | Leader | leader:conv]")
    assert projected[2]["content"].startswith("[assistant | You | member:a]")
    assert projected[3]["content"].startswith("[assistant | Bob | member:b]")


def test_projection_drops_other_actor_tool_sequence_without_claiming_actions() -> None:
    projected = project_participant_transcript(
        [
            {
                "role": "assistant",
                "content": None,
                "participant_id": "member:b",
                "tool_calls": [{"id": "call-b", "function": {"name": "terminal"}}],
            },
            {"role": "tool", "tool_call_id": "call-b", "content": "secret result"},
            {"role": "assistant", "content": "Bob finished", "participant_id": "member:b"},
        ],
        viewing_participant_id="member:a",
        participants=PARTICIPANTS,
    )

    assert len(projected) == 1
    assert projected[0]["role"] == "user"
    assert projected[0]["content"].startswith("[assistant | Bob | member:b]")
    assert "tool_calls" not in projected[0]


def test_projection_neutralizes_unknown_assistant_instead_of_claiming_it() -> None:
    projected = project_participant_transcript(
        [{"role": "assistant", "content": "I am the Leader"}],
        viewing_participant_id="member:a",
        participants=PARTICIPANTS,
    )

    assert projected[0]["role"] == "user"
    assert projected[0]["content"].startswith(
        "[assistant | Unknown Participant | unknown]"
    )
    assert projected[0]["metadata"]["speaker_projected_role"] == "user"


def test_projection_is_idempotent_for_same_viewer() -> None:
    once = project_participant_transcript(
        [{"role": "assistant", "content": "hello", "participant_id": "member:a"}],
        viewing_participant_id="member:a",
        participants=PARTICIPANTS,
    )
    twice = project_participant_transcript(
        once,
        viewing_participant_id="member:a",
        participants=PARTICIPANTS,
    )

    assert twice == once


def test_projection_is_idempotent_for_foreign_assistant() -> None:
    once = project_participant_transcript(
        [{"role": "assistant", "content": "hello", "participant_id": "member:b"}],
        viewing_participant_id="member:a",
        participants=PARTICIPANTS,
    )
    twice = project_participant_transcript(
        once,
        viewing_participant_id="member:a",
        participants=PARTICIPANTS,
    )

    assert once[0]["role"] == "user"
    assert twice == once


def test_unstamped_user_is_attributed_to_canonical_user() -> None:
    projected = project_participant_transcript(
        [{"role": "user", "content": "hello"}],
        viewing_participant_id="member:a",
        participants=PARTICIPANTS,
    )

    assert projected[0]["role"] == "user"
    assert projected[0]["content"].startswith("[user | Yang | user]")
    assert projected[0]["metadata"]["speaker_participant_id"] == "user"


def test_persisted_system_rows_are_not_replayed_as_participant_history() -> None:
    projected = project_participant_transcript(
        [
            {"role": "system", "content": "legacy member identity contract"},
            {"role": "user", "content": "hello"},
        ],
        viewing_participant_id="member:a",
        participants=PARTICIPANTS,
    )

    assert len(projected) == 1
    assert projected[0]["role"] == "user"
    assert "legacy member identity contract" not in projected[0]["content"]
