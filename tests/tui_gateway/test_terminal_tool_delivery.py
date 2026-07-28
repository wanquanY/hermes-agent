from __future__ import annotations

from pathlib import Path

from hermes_agent.composition.cli_session_store import open_cli_session_store
from tui_gateway.services import run_control


def test_terminal_publish_delivers_tool_cleanup_before_run_terminal(
    tmp_path: Path,
) -> None:
    db = open_cli_session_store(tmp_path / "terminal-tool-delivery.db")
    delivered: list[dict] = []

    class CapturingTransport:
        def write(self, obj):
            delivered.append(obj)
            return True

    session_id = "conversation-terminal-tool-delivery"
    common = {
        "session_id": session_id,
        "conversation_session_id": session_id,
        "execution_session_id": "exec-delivery",
        "runtime_scope_key": "profile:agent-default",
        "run_id": "run-terminal-tool-delivery",
        "turn_id": "turn-terminal-tool-delivery",
    }
    subscription_id, _ = run_control.subscribe_session_with_id(
        conversation_session_id=session_id,
        transport=CapturingTransport(),
        db=db,
    )
    try:
        run_control.publish_recorded_event(
            {
                **common,
                "type": "tool.generating",
                "payload": {"tool_id": "call-write", "name": "write_file"},
            },
            db=db,
        )
        run_control.publish_recorded_event(
            {
                **common,
                "type": "message.complete",
                "payload": {
                    "status": "failed",
                    "message": "provider stream failed",
                },
            },
            db=db,
        )
    finally:
        run_control.unsubscribe_session(subscription_id=subscription_id)
        db.close()

    events = [
        item["params"]
        for item in delivered
        if item.get("method") == "event"
    ]
    assert [event["type"] for event in events] == [
        "tool.generating",
        "tool.complete",
        "message.complete",
    ]
    assert events[1]["payload"]["status"] == "failed"
    assert events[1]["payload"]["error_code"] == "parent_run_terminal"
    assert [event["seq"] for event in events] == [1, 2, 3]
