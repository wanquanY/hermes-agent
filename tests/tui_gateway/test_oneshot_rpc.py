from types import SimpleNamespace
from unittest.mock import patch

from tui_gateway import server


def test_oneshot_rpc_inherits_live_runtime_without_mutating_history():
    sid = "oneshot-session"
    agent = SimpleNamespace(
        _current_main_runtime=lambda: {
            "provider": "custom",
            "model": "model-x",
            "api_key": "secret",
        }
    )
    history = [{"role": "user", "content": "existing"}]
    with server._sessions_lock:
        server._sessions[sid] = {"agent": agent, "history": history}
    try:
        with patch("agent.oneshot.run_oneshot", return_value="feat: concise") as run:
            response = server.handle_request(
                {
                    "id": "oneshot",
                    "method": "llm.oneshot",
                    "params": {
                        "session_id": sid,
                        "template": "commit_message",
                        "variables": {"diff": "+line"},
                    },
                }
            )
    finally:
        with server._sessions_lock:
            server._sessions.pop(sid, None)

    assert response["result"] == {"text": "feat: concise"}
    assert history == [{"role": "user", "content": "existing"}]
    assert run.call_args.kwargs["main_runtime"] == {
        "provider": "custom",
        "model": "model-x",
        "api_key": "secret",
    }


def test_oneshot_rpc_rejects_empty_prompt():
    response = server.handle_request(
        {"id": "oneshot-empty", "method": "llm.oneshot", "params": {}}
    )
    assert response["error"]["code"] == 4030
