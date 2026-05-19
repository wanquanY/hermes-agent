import os

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
                "doxie_profile": {
                    "hermesHomePath": str(profile_home),
                    "env": {"DOXIE_TEST_PROFILE_ENV": "must-not-leak"},
                },
            },
        }
    )

    assert "error" not in resp
    assert resp["result"]["messages"] == [{"role": "user", "text": "hello"}]
    assert seen["home"] == str(profile_home.resolve())
    assert os.environ.get("DOXIE_TEST_PROFILE_ENV") is None
