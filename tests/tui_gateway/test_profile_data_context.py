import os
from types import SimpleNamespace

from tui_gateway import server
from tui_gateway.methods import session as session_methods


def test_read_only_profile_data_methods_do_not_take_env_lock(monkeypatch, tmp_path):
    """History hydration needs profile-local DB access without blocking active runs."""

    class _ExplodingEnvLock:
        def acquire(self):
            raise AssertionError("read-only session.messages must not take profile env lock")

        def release(self):
            raise AssertionError("read-only session.messages must not release profile env lock")

    seen: dict[str, str] = {}

    class _DB:
        def get_session(self, _sid):
            return {"id": "stored-session"}

        def get_session_by_title(self, _title):
            return None

        def get_messages_page_as_conversation(self, _sid, **_kwargs):
            from hermes_constants import get_hermes_home

            seen["home"] = str(get_hermes_home())
            return {
                "messages": [{"role": "user", "content": "hello"}],
                "pageInfo": {"hasMoreBefore": False, "hasMoreAfter": False},
            }

    monkeypatch.setattr(server, "_profile_env_lock", _ExplodingEnvLock())
    monkeypatch.setattr(session_methods, "_get_db", lambda: _DB())

    profile_home = tmp_path / "profile-home"
    resp = server.handle_request(
        {
            "id": "messages",
            "method": "session.messages",
            "params": {
                "session_id": "stored-session",
                "dovie_profile": {
                    "hermesHomePath": str(profile_home),
                    "env": {"DOVIE_TEST_PROFILE_ENV": "must-not-leak"},
                },
            },
        }
    )

    assert "error" not in resp
    assert resp["result"]["messages"] == [{"role": "user", "text": "hello"}]
    assert seen["home"] == str(profile_home.resolve())
    assert os.environ.get("DOVIE_TEST_PROFILE_ENV") is None


def test_profile_db_selection_uses_process_home_as_default(monkeypatch, tmp_path):
    """A request-scoped profile home must not redefine the process default DB."""

    process_home = tmp_path / "process-home"
    profile_home = tmp_path / "profile-home"
    seen: dict[str, str] = {}

    def fake_get_session_db_for_home(**kwargs):
        seen["active_home"] = str(kwargs["active_home"])
        seen["default_home"] = str(kwargs["default_home"])
        return SimpleNamespace(
            db=object(),
            default_db=kwargs["default_db"],
            default_error=kwargs["default_error"],
        )

    monkeypatch.setattr(server, "_hermes_home", process_home)
    monkeypatch.setattr(server, "_db", object())
    monkeypatch.setattr(server, "_db_error", None)
    monkeypatch.setattr(server, "_get_session_db_for_home", fake_get_session_db_for_home)

    token = server._enter_profile_context(
        server._profile_context_for_params({
            "dovie_profile": {
                "id": "agent-default",
                "hermesHomePath": str(profile_home),
            },
        })
    )
    try:
        server._get_db()
    finally:
        server._leave_profile_context(token)

    assert seen == {
        "active_home": str(profile_home.resolve()),
        "default_home": str(process_home.resolve()),
    }
