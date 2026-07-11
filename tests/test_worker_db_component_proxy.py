from __future__ import annotations

from hermes_agent.orchestration.worker_db_proxy import (
    WORKER_DB_COMPONENT_NAMES,
    WorkerDBProxy,
)


class _UnusedWriter:
    def write_json(self, _payload) -> None:
        raise AssertionError("component contract test must not write to IPC")


def test_worker_db_proxy_exposes_every_cli_store_component(monkeypatch) -> None:
    proxy = WorkerDBProxy(_UnusedWriter())
    calls: list[tuple[str, tuple[object, ...], dict[str, object], str]] = []

    def capture(method, args, kwargs, *, conversation_session_id):
        calls.append((method, args, kwargs, conversation_session_id))
        return method

    monkeypatch.setattr(proxy, "_call", capture)

    for component in WORKER_DB_COMPONENT_NAMES:
        result = getattr(proxy, component).probe("value", enabled=True)
        assert result == f"{component}.probe"

    assert {method for method, *_rest in calls} == {
        f"{component}.probe" for component in WORKER_DB_COMPONENT_NAMES
    }
    assert all(args == ("value",) for _method, args, _kwargs, _scope in calls)
    assert all(kwargs == {"enabled": True} for _method, _args, kwargs, _scope in calls)
    assert all(scope == "" for _method, _args, _kwargs, scope in calls)


def test_scoped_worker_db_component_call_preserves_conversation_identity(
    monkeypatch,
) -> None:
    proxy = WorkerDBProxy(_UnusedWriter())
    calls: list[tuple[str, str]] = []

    def capture(method, _args, _kwargs, *, conversation_session_id):
        calls.append((method, conversation_session_id))
        return "ok"

    monkeypatch.setattr(proxy, "_call", capture)

    result = proxy.scoped("conversation-session-1").runs.append_event({"seq": 1})

    assert result == "ok"
    assert calls == [("runs.append_event", "conversation-session-1")]
