from __future__ import annotations

from doxie_extension.display_transcript import (
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


def test_doxie_display_text_strips_cron_delivery_guidance_only():
    assert sanitize_display_text(CRON_HINT + "请写一首短诗") == "请写一首短诗"
    assert sanitize_display_text("请写一首短诗") == "请写一首短诗"


def test_doxie_transcript_sanitizer_preserves_raw_message_shape():
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
        {"role": "assistant", "text": "好的"},
    ]


def test_doxie_session_list_sanitizer_uses_clean_preview_as_title_fallback():
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


def test_session_messages_returns_doxie_sanitized_cron_prompt(monkeypatch):
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


def test_session_list_returns_doxie_sanitized_title_and_preview(monkeypatch):
    from tui_gateway import server
    from tui_gateway.methods import session as session_methods

    class _DB:
        def list_sessions_rich(self, **_kwargs):
            return [
                {
                    "id": "cron-session",
                    "title": CRON_HINT,
                    "preview": CRON_HINT + "创建自动化任务",
                    "started_at": 1,
                    "last_active": 2,
                    "message_count": 2,
                    "source": "cron",
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
