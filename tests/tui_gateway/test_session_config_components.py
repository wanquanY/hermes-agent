"""Component boundaries used by TUI session configuration."""

from __future__ import annotations

import contextlib
import threading
from types import SimpleNamespace

from tui_gateway import server as _server  # noqa: F401 - initialize composition root first.
from tui_gateway.core import session_config
from tui_gateway.methods import prompt_respond


def test_persisted_runtime_reads_session_component(monkeypatch):
    class _Sessions:
        def get(self, session_id):
            assert session_id == "session-1"
            return {
                "model": "model-1",
                "model_config": '{"provider":"custom","runtime_executor":"codex"}',
            }

    db = SimpleNamespace(sessions=_Sessions())
    monkeypatch.setattr(session_config, "_db_for_stable_session", lambda _key: db)

    assert session_config._persisted_session_runtime("session-1") == (
        "model-1",
        "custom",
    )
    assert session_config._persisted_session_codex_runtime("session-1") == {
        "runtime_executor": "codex"
    }


def test_ensure_session_row_uses_session_component(monkeypatch):
    created: list[tuple[tuple, dict]] = []

    class _Sessions:
        def create(self, *args, **kwargs):
            created.append((args, kwargs))

    db = SimpleNamespace(sessions=_Sessions())
    monkeypatch.setattr(session_config, "_get_db", lambda: db)
    monkeypatch.setattr(session_config, "_resolve_model", lambda: "model-1")
    monkeypatch.setattr(
        session_config._server,
        "_session_source",
        lambda _session: "tui",
    )

    session_config._ensure_session_db_row({"session_key": "session-1"})

    assert created == [
        (
            ("session-1",),
            {
                "source": "tui",
                "model": "model-1",
                "model_config": None,
                "cwd": None,
            },
        )
    ]


def test_set_session_cwd_uses_session_component(monkeypatch, tmp_path):
    updated: list[tuple[str, str]] = []

    class _Sessions:
        def update_cwd(self, session_id, cwd):
            updated.append((session_id, cwd))

    @contextlib.contextmanager
    def _session_db(_session):
        yield SimpleNamespace(sessions=_Sessions())

    monkeypatch.setattr(session_config, "_session_db", _session_db)
    monkeypatch.setattr(session_config, "_register_session_cwd", lambda _session: None)
    monkeypatch.setattr("tools.terminal_tool.cleanup_vm", lambda _session_id: None)
    session = {"session_key": "session-1"}

    result = session_config._set_session_cwd(session, str(tmp_path))

    assert result == str(tmp_path)
    assert updated == [("session-1", str(tmp_path))]


def test_inprocess_interaction_registers_owner_identity_before_emit(monkeypatch):
    registered: list[dict] = []
    registry = SimpleNamespace(register=lambda **kwargs: registered.append(kwargs))
    monkeypatch.setattr(prompt_respond, "_pending_registry", lambda: registry)
    monkeypatch.setattr(session_config, "_sessions_lock", threading.RLock())
    monkeypatch.setattr(session_config, "_sessions", {
        "runtime-1": {
            "session_key": "conversation-1",
            "active_run_id": "run-1",
            "active_turn_id": "turn-1",
            "runtime_scope_key": "profile:agent-1",
        },
    })

    result = session_config._register_pending_interaction(
        "clarify.request",
        "runtime-1",
        {"question": "Choose", "choices": ["A", "B"]},
        "request-1",
    )

    assert result is registry
    assert registered == [{
        "request_id": "request-1",
        "kind": "clarify",
        "conversation_id": "runtime-1",
        "session_key": "conversation-1",
        "scope_key": "profile:agent-1",
        "request_payload": {
            "question": "Choose",
            "choices": ["A", "B"],
            "run_id": "run-1",
            "turn_id": "turn-1",
            "participant_id": "agent",
            "activity_id": "chat:conversation-1",
            "activity_kind": "chat",
            "runtime_scope_key": "profile:agent-1",
        },
    }]


def test_inprocess_interaction_timeout_publishes_expired_lifecycle(monkeypatch):
    expired: list[str] = []
    registry = SimpleNamespace(mark_expired=lambda request_id: expired.append(request_id))
    emitted: list[tuple[str, str, dict]] = []
    monkeypatch.setattr(
        session_config,
        "_register_pending_interaction",
        lambda _event, _sid, _payload, _request_id: registry,
    )
    monkeypatch.setattr(
        session_config,
        "_emit",
        lambda event, sid, payload: emitted.append((event, sid, dict(payload))),
    )
    monkeypatch.setattr(session_config, "_project_block_state", lambda *_args, **_kwargs: None)

    assert session_config._block("sudo.request", "runtime-1", {}, timeout=0) == ""
    assert emitted[0][0:2] == ("sudo.request", "runtime-1")
    assert expired == [emitted[0][2]["request_id"]]


def test_terminal_read_block_does_not_project_human_approval_state(monkeypatch):
    projected: list[bool] = []
    emitted: list[tuple[str, str, dict]] = []
    monkeypatch.setattr(
        session_config,
        "_register_pending_interaction",
        lambda _event, _sid, _payload, _request_id: None,
    )
    monkeypatch.setattr(
        session_config,
        "_emit",
        lambda event, sid, payload: emitted.append((event, sid, dict(payload))),
    )
    monkeypatch.setattr(
        session_config,
        "_project_block_state",
        lambda _sid, *, present: projected.append(present),
    )

    assert session_config._block("terminal.read.request", "runtime-1", {}, timeout=0) == ""
    assert emitted[0][0:2] == ("terminal.read.request", "runtime-1")
    assert projected == []
