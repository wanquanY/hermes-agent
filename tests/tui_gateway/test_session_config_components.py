"""Component boundaries used by TUI session configuration."""

from __future__ import annotations

import contextlib
from types import SimpleNamespace

from tui_gateway import server as _server  # noqa: F401 - initialize composition root first.
from tui_gateway.core import session_config


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
