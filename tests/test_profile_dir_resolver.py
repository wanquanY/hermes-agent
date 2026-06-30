from pathlib import Path

from hermes_profile_dir import (
    is_legacy_default_agent_dir,
    resolve_default_agent_dir,
)


def test_returns_profiles_default_when_exists(tmp_path: Path) -> None:
    default_dir = tmp_path / "profiles" / "default"
    default_dir.mkdir(parents=True)

    assert resolve_default_agent_dir(tmp_path) == default_dir
    assert default_dir.is_dir()


def test_returns_profiles_agent_when_legacy_only(tmp_path: Path) -> None:
    legacy_dir = tmp_path / "profiles" / "agent"
    legacy_dir.mkdir(parents=True)

    assert resolve_default_agent_dir(tmp_path) == legacy_dir
    assert legacy_dir.is_dir()
    assert not (tmp_path / "profiles" / "default").exists()


def test_creates_profiles_default_when_neither_exists(tmp_path: Path) -> None:
    resolved = resolve_default_agent_dir(tmp_path)

    assert resolved == tmp_path / "profiles" / "default"
    assert resolved.is_dir()


def test_is_legacy_default_agent_dir_true_when_only_agent(tmp_path: Path) -> None:
    (tmp_path / "profiles" / "agent").mkdir(parents=True)

    assert is_legacy_default_agent_dir(tmp_path) is True


def test_is_legacy_default_agent_dir_false_when_default_exists(tmp_path: Path) -> None:
    (tmp_path / "profiles" / "agent").mkdir(parents=True)
    (tmp_path / "profiles" / "default").mkdir(parents=True)

    assert is_legacy_default_agent_dir(tmp_path) is False


def test_resolver_does_not_mutate_when_both_dirs_exist(tmp_path: Path) -> None:
    legacy_dir = tmp_path / "profiles" / "agent"
    default_dir = tmp_path / "profiles" / "default"
    legacy_dir.mkdir(parents=True)
    default_dir.mkdir(parents=True)

    assert resolve_default_agent_dir(tmp_path) == default_dir
    assert legacy_dir.is_dir()
    assert default_dir.is_dir()


def test_default_runtime_scope_uses_resolved_agent_home(monkeypatch, tmp_path: Path) -> None:
    from tui_gateway.services import runtime_proxy

    monkeypatch.setattr(runtime_proxy, "get_hermes_home", lambda: tmp_path)

    scope = runtime_proxy.runtime_scope_from_params(
        {
            "agentProfileId": "agent-default",
            "runtimeScopeKey": "profile:agent-default",
        }
    )

    assert Path(scope.hermes_home) == tmp_path / "profiles" / "default"
    assert Path(scope.hermes_home).is_dir()
