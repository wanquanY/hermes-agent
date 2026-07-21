"""A TUI /new boundary must release every conversation runtime pin."""

import threading
from types import SimpleNamespace
from unittest.mock import patch

import tui_gateway.server as server
import tui_gateway.core.agent_session as agent_session


def test_reset_session_agent_rebuilds_from_durable_defaults() -> None:
    old_agent = SimpleNamespace(
        model="session-model",
        reasoning_config={"enabled": True, "effort": "ultra"},
        service_tier="priority",
    )
    new_agent = SimpleNamespace(model="configured-model")
    session = {
        "agent": old_agent,
        "session_key": "conversation-1",
        "model_override": {"model": "session-model"},
        "create_reasoning_override": {"enabled": True, "effort": "ultra"},
        "create_service_tier_override": "priority",
        "one_turn_model_restore": {"model": "configured-model"},
        "attached_images": [],
        "edit_snapshots": {},
        "history": [{"role": "user", "content": "old"}],
        "history_lock": threading.Lock(),
        "history_version": 0,
    }

    with (
        patch.object(server, "_set_session_context", return_value=()),
        patch.object(server, "_clear_session_context"),
        patch.object(agent_session, "_make_agent", return_value=new_agent) as make_agent,
        patch.object(server, "_emit"),
        patch.object(server, "_restart_slash_worker"),
        patch.object(server, "_session_info", return_value={"model": "configured-model"}),
        patch.object(server, "_load_show_reasoning", return_value=False),
        patch.object(server, "_load_tool_progress_mode", return_value="all"),
    ):
        server._reset_session_agent("sid", session)

    make_agent.assert_called_once_with(
        "sid",
        "conversation-1",
        session_id="conversation-1",
    )
    for key in (
        "model_override",
        "create_reasoning_override",
        "create_service_tier_override",
        "one_turn_model_restore",
    ):
        assert key not in session
    assert session["agent"] is new_agent
    assert session["history"] == []
