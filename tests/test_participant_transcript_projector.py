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
            {
                "role": "assistant",
                "content": "我来协调",
                "participant_id": "leader:conv",
            },
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
    assert [message["content"] for message in projected] == [
        "开始",
        "[assistant | Leader | leader:conv]\n我来协调",
        "我来研究",
        "[assistant | Bob | member:b]\n我来测试",
    ]
    assert all("name" not in message for message in projected)


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
            {
                "role": "assistant",
                "content": "Bob finished",
                "participant_id": "member:b",
            },
        ],
        viewing_participant_id="member:a",
        participants=PARTICIPANTS,
    )

    assert len(projected) == 1
    assert projected[0]["role"] == "user"
    assert projected[0]["content"] == "[assistant | Bob | member:b]\nBob finished"
    assert "name" not in projected[0]
    assert "tool_calls" not in projected[0]


def test_projection_neutralizes_unknown_assistant_instead_of_claiming_it() -> None:
    projected = project_participant_transcript(
        [{"role": "assistant", "content": "I am the Leader"}],
        viewing_participant_id="member:a",
        participants=PARTICIPANTS,
    )

    assert projected[0]["role"] == "user"
    assert projected[0]["content"] == (
        "[assistant | Unknown Participant | unknown]\nI am the Leader"
    )
    assert "name" not in projected[0]
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


def test_v2_named_foreign_user_is_reprojected_with_prefix_and_without_name() -> None:
    projected = project_participant_transcript(
        [
            {
                "role": "user",
                "content": "滴～ 小马，我在呢！有什么需要帮忙的？",
                "name": "team_leader_cf87ae1b02",
                "participant_id": "leader:conv",
                "metadata": {
                    "speaker_projection_version": "participant-structured-v2",
                    "speaker_participant_id": "leader:conv",
                    "speaker_display_name": "Leader",
                    "speaker_original_role": "assistant",
                    "speaker_projected_role": "user",
                },
            }
        ],
        viewing_participant_id="member:a",
        participants=PARTICIPANTS,
    )

    assert projected[0]["role"] == "user"
    assert projected[0]["content"] == (
        "[assistant | Leader | leader:conv]\n滴～ 小马，我在呢！有什么需要帮忙的？"
    )
    assert "name" not in projected[0]
    assert (
        projected[0]["metadata"]["speaker_projection_version"]
        == "participant-perspective-v3"
    )


def test_unstamped_user_is_attributed_to_canonical_user() -> None:
    projected = project_participant_transcript(
        [{"role": "user", "content": "hello"}],
        viewing_participant_id="member:a",
        participants=PARTICIPANTS,
    )

    assert projected[0]["role"] == "user"
    assert projected[0]["content"] == "hello"
    assert "name" not in projected[0]
    assert projected[0]["metadata"]["speaker_participant_id"] == "user"


def test_projection_strips_legacy_speaker_envelopes_without_reencoding_them() -> None:
    projected = project_participant_transcript(
        [
            {
                "role": "assistant",
                "content": "[assistant | You | member:a]\n我的历史回复",
                "participant_id": "member:a",
            },
            {
                "role": "assistant",
                "content": "[assistant | Bob | member:b]\nBob 的历史回复",
                "participant_id": "member:b",
            },
            {
                "role": "user",
                "content": "[user | Yang | user]\n继续",
                "participant_id": "user",
            },
        ],
        viewing_participant_id="member:a",
        participants=PARTICIPANTS,
    )

    assert [message["content"] for message in projected] == [
        "我的历史回复",
        "[assistant | Bob | member:b]\nBob 的历史回复",
        "继续",
    ]
    assert projected[0]["role"] == "assistant"
    assert projected[1]["role"] == "user"
    assert "name" not in projected[1]
    assert "[assistant |" not in projected[0]["content"]
    assert all("[user |" not in message["content"] for message in projected)


def test_projection_strips_repeated_model_echo_of_legacy_envelope() -> None:
    projected = project_participant_transcript(
        [
            {
                "role": "assistant",
                "content": (
                    "[assistant | You | leader:conv]\n"
                    "[assistant | You | leader:conv]\n"
                    "任务已经取消。"
                ),
                "participant_id": "leader:conv",
            }
        ],
        viewing_participant_id="leader:conv",
        participants=PARTICIPANTS,
    )

    assert projected == [
        {
            "role": "assistant",
            "content": "任务已经取消。",
            "participant_id": "leader:conv",
            "metadata": {
                "speaker_projection_version": "participant-perspective-v3",
                "speaker_participant_id": "leader:conv",
                "speaker_display_name": "Leader",
                "speaker_original_role": "assistant",
                "speaker_projected_role": "assistant",
            },
        }
    ]


def test_foreign_member_prefix_distinguishes_it_from_the_real_user() -> None:
    participant_id = "member:8f52b4a1-1862-4a03-97d6-a09250c600e3"
    projected = project_participant_transcript(
        [
            {
                "role": "assistant",
                "content": "我来整理产品需求。",
                "participant_id": participant_id,
            }
        ],
        viewing_participant_id="leader:conv",
        participants=[
            {
                "participant_id": participant_id,
                "display_name": "产品经理",
                "role": "member",
            }
        ],
    )

    assert projected[0]["role"] == "user"
    assert projected[0]["content"] == (
        f"[assistant | 产品经理 | {participant_id}]\n我来整理产品需求。"
    )
    assert "name" not in projected[0]
    assert projected[0]["metadata"]["speaker_participant_id"] == participant_id


def test_duplicate_display_names_remain_distinct_by_participant_envelope() -> None:
    participants = [
        {"participant_id": "member:a", "display_name": "Reviewer", "role": "member"},
        {"participant_id": "member:b", "display_name": "Reviewer", "role": "member"},
    ]
    projected = project_participant_transcript(
        [
            {"role": "assistant", "content": "A", "participant_id": "member:a"},
            {"role": "assistant", "content": "B", "participant_id": "member:b"},
        ],
        viewing_participant_id="leader:conv",
        participants=participants,
    )

    assert projected[0]["content"] == "[assistant | Reviewer | member:a]\nA"
    assert projected[1]["content"] == "[assistant | Reviewer | member:b]\nB"
    assert all("name" not in message for message in projected)


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
