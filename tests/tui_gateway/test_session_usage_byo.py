from types import SimpleNamespace
from unittest.mock import patch

from tui_gateway import server
from tui_gateway.methods import session as session_methods


def _usage_payload():
    return {
        "model": "gpt-5.5",
        "calls": 1,
        "input": 10,
        "output": 5,
        "cache_read": 2,
        "cache_write": 0,
        "reasoning": 3,
        "image": 0,
        "total": 20,
        "cost_usd": 0.0,
    }


def _session_usage_entry(sid: str, agent: SimpleNamespace) -> dict:
    with server._sessions_lock:
        server._sessions[sid] = {
            "session_key": f"stored-{sid}",
            "agent": agent,
        }
    try:
        with patch.object(session_methods, "_get_usage", return_value=_usage_payload()):
            resp = server.handle_request({
                "id": f"usage-{sid}",
                "method": "session.usage",
                "params": {"session_id": sid},
            })
    finally:
        with server._sessions_lock:
            server._sessions.pop(sid, None)

    assert "error" not in resp
    return resp["result"]["sessions"][0]["usage"]["modelUsage"][0]


def test_session_usage_marks_byo_codex_model_usage_entry():
    agent = SimpleNamespace(
        api_mode="codex_app_server",
        codex_account_mode="byo",
        provider="openai-codex",
        model="gpt-5.5",
    )

    entry = _session_usage_entry("sid-byo-codex-usage", agent)

    assert entry["provider"] == "openai-codex"
    assert entry["model"] == "gpt-5.5"
    assert entry["byo"] is True


def test_session_usage_does_not_mark_platform_codex_model_usage_entry():
    agent = SimpleNamespace(
        api_mode="codex_app_server",
        codex_account_mode="platform",
        codex_extra_env={"DOXIE_PLATFORM_API_KEY": "rt-token"},
        provider="openai-codex",
        model="glm-5.2",
    )

    entry = _session_usage_entry("sid-platform-codex-usage", agent)

    assert entry["provider"] == "openai-codex"
    assert entry.get("byo") in (None, False)
