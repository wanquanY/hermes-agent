from agent.codex_responses_adapter import (
    _chat_messages_to_responses_input,
    _preflight_codex_api_kwargs,
)
from agent.responses_route_policy import (
    ResponsesRouteKind,
    resolve_responses_route_policy,
)


def _assistant_history(item_id: str) -> list[dict]:
    return [
        {
            "role": "assistant",
            "content": "pong",
            "codex_message_items": [
                {
                    "type": "message",
                    "role": "assistant",
                    "status": "in_progress",
                    "phase": "commentary",
                    "id": item_id,
                    "content": [{"type": "output_text", "text": "pong"}],
                }
            ],
        }
    ]


def test_copilot_route_never_replays_connection_scoped_message_id():
    policy = resolve_responses_route_policy(
        {"base_url": "https://api.githubcopilot.com", "provider": "copilot"}
    )

    items = _chat_messages_to_responses_input(
        _assistant_history("msg_short_but_connection_scoped"),
        route_policy=policy,
    )

    assert policy.kind is ResponsesRouteKind.GITHUB_COPILOT
    assert items == [
        {
            "type": "message",
            "role": "assistant",
            "status": "in_progress",
            "phase": "commentary",
            "content": [{"type": "output_text", "text": "pong"}],
        }
    ]


def test_codex_route_keeps_safe_id_and_drops_oversized_id():
    policy = resolve_responses_route_policy({"is_codex_backend": True})

    safe = _chat_messages_to_responses_input(
        _assistant_history("msg_safe"), route_policy=policy
    )
    oversized = _chat_messages_to_responses_input(
        _assistant_history("m" * 65), route_policy=policy
    )

    assert safe[0]["id"] == "msg_safe"
    assert "id" not in oversized[0]


def test_final_preflight_removes_id_reintroduced_after_build():
    policy = resolve_responses_route_policy({"is_github_responses": True})
    request = {
        "model": "gpt-5.5",
        "instructions": "answer",
        "store": False,
        "input": [
            {
                "type": "message",
                "role": "assistant",
                "status": "completed",
                "phase": "final_answer",
                "id": "middleware-reintroduced-id",
                "content": [{"type": "output_text", "text": "done"}],
            }
        ],
    }

    normalized = _preflight_codex_api_kwargs(request, route_policy=policy)

    assert "id" not in normalized["input"][0]
    assert normalized["input"][0]["phase"] == "final_answer"


def test_route_policy_filters_foreign_reasoning_but_keeps_legacy_item():
    policy = resolve_responses_route_policy({"is_github_responses": True})
    history = [
        {
            "role": "assistant",
            "content": "",
            "codex_reasoning_items": [
                {
                    "type": "reasoning",
                    "encrypted_content": "foreign",
                    "_issuer_kind": "codex_backend",
                },
                {"type": "reasoning", "encrypted_content": "legacy"},
            ],
        }
    ]

    items = _chat_messages_to_responses_input(history, route_policy=policy)

    assert [item.get("encrypted_content") for item in items if item.get("type") == "reasoning"] == ["legacy"]
