from __future__ import annotations

import importlib
from types import SimpleNamespace


approval_methods = importlib.import_module("tui_gateway.methods.prompt_respond")


class SessionQueries:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail

    def get(self, session_id: str):
        if self.fail:
            raise RuntimeError("state unavailable")
        return {"id": session_id} if session_id == "stored-session" else None


def _install_state(monkeypatch, *, fail: bool = False) -> None:
    state = SimpleNamespace(sessions=SessionQueries(fail=fail))
    monkeypatch.setattr(approval_methods, "_sessions", {})
    monkeypatch.setattr(
        approval_methods,
        "_db_for_stable_session",
        lambda _session_id: state,
    )


def test_approval_session_resolvers_use_session_component(monkeypatch):
    _install_state(monkeypatch)
    params = {"conversation_session_id": "stored-session"}

    assert approval_methods.resolve_approval_session_key(params) == "stored-session"
    assert approval_methods._approval_session_key(params, "request-1") == (
        "stored-session",
        None,
    )


def test_approval_session_resolvers_preserve_error_contract(monkeypatch):
    _install_state(monkeypatch, fail=True)
    params = {"session_id": "stored-session"}

    assert approval_methods.resolve_approval_session_key(params) == ""
    session_key, error = approval_methods._approval_session_key(params, "request-1")
    assert session_key == ""
    assert error["error"] == {"code": 5004, "message": "state unavailable"}


def test_approval_session_resolver_prefers_live_stable_key(monkeypatch):
    monkeypatch.setattr(
        approval_methods,
        "_sessions",
        {"runtime-1": {"session_key": "stored-session"}},
    )
    monkeypatch.setattr(
        approval_methods,
        "_db_for_stable_session",
        lambda _session_id: None,
    )

    params = {"session_id": "runtime-1"}
    assert approval_methods.resolve_approval_session_key(params) == "stored-session"
