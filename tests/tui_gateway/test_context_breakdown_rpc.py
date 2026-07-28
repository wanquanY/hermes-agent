import threading
from types import SimpleNamespace
from unittest.mock import patch

from tui_gateway import server


def test_session_context_breakdown_projects_live_agent_history():
    sid = "context-breakdown-session"
    agent = SimpleNamespace(
        model="openai/gpt-5.4",
        tools=[{"type": "function", "function": {"name": "terminal"}}],
        _memory_store=None,
        _memory_enabled=True,
        _user_profile_enabled=True,
        context_compressor=SimpleNamespace(context_length=100_000, last_prompt_tokens=25_000),
    )
    with server._sessions_lock:
        server._sessions[sid] = {
            "agent": agent,
            "history": [{"role": "user", "content": "hello"}],
            "history_lock": threading.Lock(),
            "session_key": sid,
        }
    try:
        with patch(
            "agent.system_prompt.build_system_prompt_parts",
            return_value={"stable": "system", "context": "rules", "volatile": "now"},
        ):
            response = server.handle_request(
                {
                    "id": "context-breakdown",
                    "method": "session.context_breakdown",
                    "params": {"session_id": sid},
                }
            )
    finally:
        with server._sessions_lock:
            server._sessions.pop(sid, None)

    assert "error" not in response
    assert response["result"]["context_used"] == 25_000
    assert response["result"]["context_percent"] == 25
    assert {item["id"] for item in response["result"]["categories"]} >= {
        "system_prompt",
        "rules",
        "conversation",
    }
