from types import SimpleNamespace
from threading import RLock


def test_terminal_events_route_to_owning_gateway_session(monkeypatch):
    from tools.process_registry import process_registry
    from tui_gateway.services.agent_terminal_bridge import wire_agent_terminal_events

    monkeypatch.setattr(process_registry, "on_output", None)
    monkeypatch.setattr(process_registry, "on_close", None)
    emitted = []
    sessions = {
        "conversation-a": {"session_key": "runtime-owner-a"},
        "conversation-b": {"session_key": "runtime-owner-b"},
    }
    wire_agent_terminal_events(
        enabled=True,
        sessions=sessions,
        sessions_lock=RLock(),
        emit=lambda event, sid, payload: emitted.append((event, sid, payload)),
    )

    process = SimpleNamespace(id="proc_1", session_key="runtime-owner-b")
    process_registry.on_output(process, "tick\n")
    process_registry.on_close(process, process.id)

    assert emitted == [
        ("agent.terminal.output", "conversation-b", {"process_id": "proc_1", "chunk": "tick\n"}),
        ("terminal.close", "conversation-b", {"process_id": "proc_1"}),
    ]


def test_terminal_close_without_process_is_not_broadcast(monkeypatch):
    from tools.process_registry import process_registry
    from tui_gateway.services.agent_terminal_bridge import wire_agent_terminal_events

    monkeypatch.setattr(process_registry, "on_output", None)
    monkeypatch.setattr(process_registry, "on_close", None)
    emitted = []
    wire_agent_terminal_events(
        enabled=True,
        sessions={"conversation-a": {"session_key": "runtime-owner-a"}},
        sessions_lock=RLock(),
        emit=lambda event, sid, payload: emitted.append((event, sid, payload)),
    )

    process_registry.on_close(None, "proc_pruned")

    assert emitted == []


def test_idle_conversation_terminal_events_use_bound_desktop_transport(monkeypatch):
    from tools.process_registry import process_registry
    from tui_gateway.services.agent_terminal_bridge import (
        bind_desktop_terminal_owner,
        wire_agent_terminal_events,
    )

    monkeypatch.setattr(process_registry, "on_output", None)
    monkeypatch.setattr(process_registry, "on_close", None)
    frames = []
    transport = SimpleNamespace(write=lambda frame: frames.append(frame) or True)
    bind_desktop_terminal_owner(
        "persisted-conversation",
        transport=transport,
        runtime_scope_key="profile:agent-1",
    )
    wire_agent_terminal_events(
        enabled=True,
        sessions={},
        sessions_lock=RLock(),
        emit=lambda *_args: None,
    )

    process = SimpleNamespace(id="proc_idle", session_key="persisted-conversation")
    process_registry.on_output(process, "ready\n")

    assert frames == [{
        "jsonrpc": "2.0",
        "method": "event",
        "params": {
            "type": "agent.terminal.output",
            "event_domain": "terminal",
            "session_id": "persisted-conversation",
            "conversation_session_id": "persisted-conversation",
            "execution_session_id": "persisted-conversation",
            "runtime_scope_key": "profile:agent-1",
            "transient": True,
            "payload": {"process_id": "proc_idle", "chunk": "ready\n"},
        },
    }]


def test_terminal_events_are_not_wired_without_renderer_capability(monkeypatch):
    from tools.process_registry import process_registry
    from tui_gateway.services.agent_terminal_bridge import wire_agent_terminal_events

    monkeypatch.setattr(process_registry, "on_output", None)
    monkeypatch.setattr(process_registry, "on_close", None)

    wire_agent_terminal_events(
        enabled=False,
        sessions={},
        sessions_lock=RLock(),
        emit=lambda *_args: None,
    )

    assert process_registry.on_output is None
    assert process_registry.on_close is None
