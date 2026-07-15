import threading
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture()
def server_state(monkeypatch, tmp_path: Path):
    from tui_gateway import server
    from tui_gateway.services.session_store import SessionStoreResult

    root = tmp_path / "root"
    profile = tmp_path / "profiles" / "default"
    root.mkdir()
    profile.mkdir(parents=True)

    calls: list[dict[str, Path]] = []

    def fake_get_session_db_for_home(**kwargs):
        calls.append(
            {
                "active_home": kwargs["active_home"],
                "default_home": kwargs["default_home"],
            }
        )
        db = SimpleNamespace(db_path=kwargs["active_home"] / "state.db")
        return SessionStoreResult(db=db, default_db=db, default_error=None)

    monkeypatch.setattr(server, "_hermes_home", root)
    monkeypatch.setattr(server, "_db", None)
    monkeypatch.setattr(server, "_db_error", None)
    monkeypatch.setattr(server, "_db_by_home", {})
    monkeypatch.setattr(server, "_db_error_by_home", {})
    monkeypatch.setattr(server, "_get_session_db_for_home", fake_get_session_db_for_home)
    monkeypatch.delenv("DOVIE_HERMES_CONTROL_HOME", raising=False)
    monkeypatch.setattr(server, "_active_hermes_home", lambda: str(profile))

    return server, root, profile, calls


def test_get_control_plane_db_uses_root_by_default(server_state) -> None:
    server, root, _profile, calls = server_state

    db = server._get_control_plane_db()

    assert db.db_path == root.resolve() / "state.db"
    assert calls == [
        {
            "active_home": root.resolve(),
            "default_home": root.resolve(),
        }
    ]


def test_get_control_plane_db_honors_DOVIE_HERMES_CONTROL_HOME_env_for_testing(
    monkeypatch,
    server_state,
    tmp_path: Path,
) -> None:
    server, _root, _profile, calls = server_state
    override = tmp_path / "control-override"
    override.mkdir()
    created: list[Path] = []

    def fake_open_cli_session_store(db_path: Path):
        created.append(Path(db_path))
        return SimpleNamespace(db_path=Path(db_path))

    monkeypatch.setattr(server, "_open_cli_session_store", fake_open_cli_session_store)
    monkeypatch.setenv("DOVIE_HERMES_CONTROL_HOME", str(override))

    db = server._get_control_plane_db()

    assert db.db_path == override.resolve() / "state.db"
    assert created == [override.resolve() / "state.db"]
    assert calls == []


def test_get_control_plane_db_never_routes_to_profile_db(server_state) -> None:
    server, root, profile, calls = server_state

    db = server._get_control_plane_db(use_active_profile=True)

    assert db.db_path == root.resolve() / "state.db"
    assert db.db_path != profile.resolve() / "state.db"
    assert calls[0]["active_home"] == root.resolve()


def test_get_control_plane_db_initializes_singleton_once_under_concurrency(
    monkeypatch,
    server_state,
) -> None:
    from tui_gateway.services.session_store import SessionStoreResult

    server, root, _profile, _calls = server_state
    factory_started = threading.Event()
    allow_factory_return = threading.Event()
    created: list[SimpleNamespace] = []
    results: list[SimpleNamespace] = []

    def blocking_get_session_db_for_home(**kwargs):
        cached = kwargs["default_db"]
        if cached is not None:
            return SessionStoreResult(
                db=cached,
                default_db=cached,
                default_error=kwargs["default_error"],
            )
        db = SimpleNamespace(db_path=root.resolve() / "state.db")
        created.append(db)
        factory_started.set()
        assert allow_factory_return.wait(timeout=1.0)
        return SessionStoreResult(db=db, default_db=db, default_error=None)

    monkeypatch.setattr(
        server,
        "_get_session_db_for_home",
        blocking_get_session_db_for_home,
    )

    first = threading.Thread(target=lambda: results.append(server._get_control_plane_db()))
    second = threading.Thread(target=lambda: results.append(server._get_control_plane_db()))
    first.start()
    assert factory_started.wait(timeout=1.0)
    second.start()
    assert len(created) == 1
    allow_factory_return.set()
    first.join(timeout=1.0)
    second.join(timeout=1.0)

    assert len(created) == 1
    assert results == [created[0], created[0]]
