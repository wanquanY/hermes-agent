from __future__ import annotations

from dovie_extension.display_transcript import (
    sanitize_assistant_display_text,
    sanitize_display_text,
    sanitize_session_list_item,
    sanitize_transcript_messages,
)


CRON_HINT = (
    "[IMPORTANT: You are running as a scheduled cron job. "
    "DELIVERY: Your final response will be automatically delivered "
    "to the user — do NOT use send_message or try to deliver "
    "the output yourself. Just produce your report/output as your "
    "final response and the system handles the rest. "
    "SILENT: If there is genuinely nothing new to report, respond "
    "with exactly \"[SILENT]\" (nothing else) to suppress delivery. "
    "Never combine [SILENT] with content — either report your "
    "findings normally, or say [SILENT] and nothing more.]\n\n"
)


def test_dovie_display_text_strips_cron_delivery_guidance_only():
    assert sanitize_display_text(CRON_HINT + "请写一首短诗") == "请写一首短诗"
    assert sanitize_display_text("请写一首短诗") == "请写一首短诗"


def test_dovie_assistant_display_text_strips_structural_boundary_blank_lines():
    assert sanitize_assistant_display_text("\n\n已读取支付协议。\n\n") == "已读取支付协议。"
    assert sanitize_assistant_display_text("  保留正文缩进\n  第二行") == "  保留正文缩进\n  第二行"


def test_dovie_transcript_sanitizer_preserves_raw_message_shape():
    messages = sanitize_transcript_messages(
        [
            {
                "role": "user",
                "text": CRON_HINT + "请写一首短诗",
                "message_id": "m1",
                "metadata": {"turn_id": "turn-1"},
            },
            {"role": "assistant", "text": "好的"},
        ]
    )

    assert messages == [
        {
            "role": "user",
            "text": "请写一首短诗",
            "message_id": "m1",
            "metadata": {"turn_id": "turn-1"},
        },
        {"role": "assistant", "text": "好的", "metadata": {"turn_id": "turn-1"}},
    ]


def test_dovie_transcript_sanitizer_strips_assistant_boundary_blank_lines():
    messages = sanitize_transcript_messages(
        [
            {"role": "assistant", "text": "\n\n已读取支付协议全文。\n\n"},
            {"role": "tool", "name": "read_file", "context": "读取文件"},
            {"role": "assistant", "text": "\n\n继续审查产品设计。"},
        ]
    )

    assert messages == [
        {"role": "assistant", "text": "已读取支付协议全文。"},
        {"role": "tool", "name": "read_file", "context": "读取文件"},
        {"role": "assistant", "text": "继续审查产品设计。"},
    ]


def test_dovie_session_list_sanitizer_uses_clean_preview_as_title_fallback():
    item = sanitize_session_list_item(
        {
            "id": "cron-session",
            "title": CRON_HINT,
            "preview": CRON_HINT + "创建自动化任务",
            "source": "cron",
        }
    )

    assert item["title"] == "创建自动化任务"
    assert item["preview"] == "创建自动化任务"
    assert item["source"] == "cron"


def test_session_messages_returns_dovie_sanitized_cron_prompt(monkeypatch):
    from tui_gateway import server
    from tui_gateway.methods import session as session_methods

    class _DB:
        def get_session(self, _session_id):
            return {"id": "cron-session"}

        def get_session_by_title(self, _title):
            return None

        def get_messages_page_as_conversation(self, _session_id, **_kwargs):
            return {
                "messages": [
                    {"role": "user", "content": CRON_HINT + "请写一首短诗"},
                    {"role": "assistant", "content": "好的"},
                ],
                "pageInfo": {"hasMoreBefore": False, "hasMoreAfter": False},
            }

    monkeypatch.setattr(session_methods, "_get_db", lambda: _DB())

    response = server.handle_request(
        {
            "id": "messages",
            "method": "session.messages",
            "params": {"session_id": "cron-session"},
        }
    )

    assert "error" not in response
    assert response["result"]["messages"] == [
        {"role": "user", "text": "请写一首短诗"},
        {"role": "assistant", "text": "好的"},
    ]


def test_session_messages_strips_assistant_structural_boundary_blank_lines(monkeypatch):
    from tui_gateway import server
    from tui_gateway.methods import session as session_methods

    class _DB:
        def get_session(self, _session_id):
            return {"id": "spacing-session"}

        def get_session_by_title(self, _title):
            return None

        def get_messages_page_as_conversation(self, _session_id, **_kwargs):
            return {
                "messages": [
                    {
                        "role": "assistant",
                        "content": "\n\n已读取支付协议全文。\n\n",
                        "reasoning_content": "\n\n分析协议结构。\n\n",
                    },
                    {"role": "tool", "tool_name": "read_file", "content": ""},
                    {"role": "assistant", "content": "\n\n继续读取产品文档。"},
                ],
                "pageInfo": {"hasMoreBefore": False, "hasMoreAfter": False},
            }

    monkeypatch.setattr(session_methods, "_get_db", lambda: _DB())

    response = server.handle_request(
        {
            "id": "messages",
            "method": "session.messages",
            "params": {"session_id": "spacing-session"},
        }
    )

    assert "error" not in response
    assert response["result"]["messages"] == [
        {
            "role": "assistant",
            "text": "已读取支付协议全文。",
            "reasoning": "分析协议结构。",
        },
        {"role": "tool", "name": "read_file", "context": ""},
        {"role": "assistant", "text": "继续读取产品文档。"},
    ]


def test_session_list_returns_dovie_sanitized_title_and_preview(monkeypatch):
    from tui_gateway import server
    from tui_gateway.methods import session as session_methods

    class _DB:
        def list_sessions_rich(self, **_kwargs):
            return [
                {
                    "id": "visible-session",
                    "title": CRON_HINT,
                    "preview": CRON_HINT + "创建自动化任务",
                    "started_at": 1,
                    "last_active": 2,
                    "message_count": 2,
                    "source": "tui",
                }
            ]

    monkeypatch.setattr(session_methods, "_get_db", lambda: _DB())
    monkeypatch.setattr(session_methods, "_live_sessions_by_stored_key", lambda: {})

    response = server.handle_request(
        {
            "id": "sessions",
            "method": "session.list",
            "params": {},
        }
    )

    assert "error" not in response
    assert response["result"]["sessions"][0]["title"] == "创建自动化任务"
    assert response["result"]["sessions"][0]["preview"] == "创建自动化任务"


def test_history_to_messages_preserves_storage_message_id():
    # Regression: get_messages_page_as_conversation exposes the sqlite row id as
    # "message_id" (not "id"). history_to_messages must pass it through so the
    # desktop can attach persisted artifact links by their message_id anchor
    # after a restart (real-device loss: session 20260703_011656_2c8a39).
    from tui_gateway.services.transcript_messages import history_to_messages

    messages = history_to_messages(
        [
            {
                "role": "user",
                "content": "重新测试，再随便创建几个文件",
                "message_id": "8099",
                "timestamp": 1783012699.89,
                "metadata": {"run_id": "run-1", "turn_id": "turn-1"},
            },
            {
                "role": "assistant",
                "content": "测试完成，全部正常",
                "message_id": "8113",
                "timestamp": 1783012699.91,
                "metadata": {"run_id": "run-1", "turn_id": "turn-1"},
            },
            {
                "role": "tool",
                "content": "ok",
                "tool_call_id": "call-1",
                "tool_name": "write_file",
                "message_id": "8111",
            },
            # Legacy JSONL transcript shape keeps working via the "id" fallback.
            {"role": "assistant", "content": "旧格式", "id": 42},
        ]
    )

    assert [m.get("message_id") for m in messages] == ["8099", "8113", "8111", "42"]
