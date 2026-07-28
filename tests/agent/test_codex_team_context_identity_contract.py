from __future__ import annotations

from agent.codex_runtime import _format_team_context_preface


def test_codex_team_context_preface_does_not_include_hermes_member_identity_contract():
    preface = _format_team_context_preface(
        [
            {
                "role": "assistant",
                "content": "我是小多(Dovie)。",
                "participant_id": "leader:conv-1",
            }
        ],
        participants={
            "leader:conv-1": {
                "participant_id": "leader:conv-1",
                "role": "leader",
                "display_name": "小多",
            }
        },
        omitted=False,
    )

    assert "小多 (Leader): 我是小多(Dovie)。" in preface
    assert "你绝不能冒充任何其他参与者" not in preface
    assert "你的回复不要以 `[任何名字]` 前缀开头" not in preface
    assert "历史中带 `[某某]` 前缀、以 user 角色出现的消息" not in preface
