import os
from types import SimpleNamespace

from hermes_agent.repositories.session_repo import SessionRepoImpl, SessionSpec
from hermes_agent.storage.session_repository_db import connect_session_repository_db
from tui_gateway import server
from tui_gateway.methods import session as session_methods


def test_read_only_profile_data_methods_do_not_take_env_lock(monkeypatch, tmp_path):
    """History hydration needs profile-local DB access without blocking active runs."""

    class _ExplodingEnvLock:
        def acquire(self):
            raise AssertionError("read-only session.messages must not take profile env lock")

        def release(self):
            raise AssertionError("read-only session.messages must not release profile env lock")

    class _DB:
        def __init__(self):
            self._conn = connect_session_repository_db(tmp_path / "profile-state.db")
            SessionRepoImpl(self._conn).create(
                SessionSpec(session_id="stored-session", source="tui")
            )
            self._conn.execute(
                """
                CREATE TABLE messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT,
                    participant_id TEXT NOT NULL DEFAULT '',
                    tool_call_id TEXT,
                    tool_calls TEXT,
                    tool_name TEXT,
                    timestamp REAL NOT NULL,
                    token_count INTEGER,
                    finish_reason TEXT,
                    reasoning TEXT,
                    reasoning_content TEXT,
                    reasoning_details TEXT,
                    codex_reasoning_items TEXT,
                    codex_message_items TEXT,
                    platform_message_id TEXT,
                    conversation_message_id TEXT NOT NULL DEFAULT '',
                    metadata_json TEXT,
                    active INTEGER NOT NULL DEFAULT 1
                )
                """
            )
            self._conn.execute(
                "INSERT INTO messages (session_id, role, content, timestamp) VALUES (?, ?, ?, ?)",
                ("stored-session", "user", "hello", 1.0),
            )

    monkeypatch.setattr(server, "_profile_env_lock", _ExplodingEnvLock())
    monkeypatch.setattr(session_methods, "_get_db", lambda: _DB())

    profile_home = tmp_path / "profile-home"
    resp = server.handle_request(
        {
            "id": "messages",
            "method": "session.messages",
            "params": {
                "session_id": "stored-session",
                "includeRunEvents": True,
                "dovie_profile": {
                    "hermesHomePath": str(profile_home),
                    "env": {"DOVIE_TEST_PROFILE_ENV": "must-not-leak"},
                },
            },
        }
    )

    assert "error" not in resp
    assert resp["result"]["messages"] == [
        {"role": "user", "text": "hello", "message_id": "1", "timestamp": 1.0}
    ]
    assert os.environ.get("DOVIE_TEST_PROFILE_ENV") is None


def test_profile_context_extracts_codex_mode_and_extra_env_from_contract_fields(tmp_path):
    profile_home = tmp_path / "profile-home"
    codex_home = tmp_path / "codex-home"

    ctx = server._profile_context_for_params({
        "agentProfileId": "agent-codex",
        "runtimeExecutor": "codex",
        "codexHome": str(codex_home),
        "codexAccountMode": "byo",
        "codex_extra_env": {
            "CODEX_TRACE": 1,
            "DROP_ME": None,
            42: True,
        },
        "dovie_profile": {
            "hermesHomePath": str(profile_home),
        },
    })

    assert ctx is not None
    assert ctx["runtime_executor"] == "codex"
    assert ctx["codex_home"] == str(codex_home)
    assert ctx["codex_account_mode"] == "byo"
    assert ctx["codex_extra_env"] == {
        "CODEX_TRACE": "1",
        "42": "True",
    }


def test_profile_context_extracts_nested_codex_camel_and_snake_fields(tmp_path):
    codex_home = tmp_path / "nested-codex-home"

    ctx = server._profile_context_for_params({
        "dovieProfile": {
            "id": "agent-nested-codex",
            "runtime_executor": "codex",
            "codex_home": str(codex_home),
            "codexAccountMode": "platform",
            "codexExtraEnv": {"DOXIE_PLATFORM_API_KEY": "rt-token"},
        },
    })

    assert ctx is not None
    assert ctx["id"] == "agent-nested-codex"
    assert ctx["runtime_executor"] == "codex"
    assert ctx["codex_home"] == str(codex_home)
    assert ctx["codex_account_mode"] == "platform"
    assert ctx["codex_extra_env"] == {"DOXIE_PLATFORM_API_KEY": "rt-token"}


def test_control_plane_db_selection_uses_process_home_for_active_and_default(monkeypatch, tmp_path):
    """Profile context must not move control-plane DB reads out of process home."""

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
        "active_home": str(process_home.resolve()),
        "default_home": str(process_home.resolve()),
    }
