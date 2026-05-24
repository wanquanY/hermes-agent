import pytest

from tui_gateway.services.session_info import session_info
from tui_gateway.services.workspace import normalize_session_cwd, workspace_from_params


def test_normalize_session_cwd_rejects_missing_path(tmp_path):
    with pytest.raises(ValueError, match="cwd does not exist"):
        normalize_session_cwd(tmp_path / "missing")


def test_normalize_session_cwd_prefers_doxie_workspace_root(tmp_path, monkeypatch):
    workspace_root = tmp_path / "doxie-workspace"
    terminal_cwd = tmp_path / "terminal-cwd"
    process_cwd = tmp_path / "hermes-agent"
    workspace_root.mkdir()
    terminal_cwd.mkdir()
    process_cwd.mkdir()

    monkeypatch.setenv("DOXIE_WORKSPACE_ROOT", str(workspace_root))
    monkeypatch.setenv("TERMINAL_CWD", str(terminal_cwd))
    monkeypatch.chdir(process_cwd)

    assert normalize_session_cwd() == str(workspace_root)


def test_normalize_session_cwd_rejects_missing_doxie_workspace_root(tmp_path, monkeypatch):
    process_cwd = tmp_path / "hermes-agent"
    process_cwd.mkdir()

    monkeypatch.delenv("DOXIE_WORKSPACE_ROOT", raising=False)
    monkeypatch.delenv("TERMINAL_CWD", raising=False)
    monkeypatch.setenv("DOXIE_PROCESS_ROLE", "hermes-worker")
    monkeypatch.chdir(process_cwd)

    with pytest.raises(ValueError, match="Doxie workspace root is not configured"):
        normalize_session_cwd()


def test_workspace_from_params_defaults_to_session_cwd(tmp_path):
    workspace = workspace_from_params({}, str(tmp_path))

    assert workspace["id"].startswith("local:")
    assert workspace["name"] == tmp_path.name
    assert workspace["path"] == str(tmp_path)
    assert workspace["kind"] == "local"


def test_workspace_from_params_preserves_doxie_metadata(tmp_path):
    workspace = workspace_from_params(
        {
            "workspace": {
                "id": "workspace-local",
                "name": "Project",
                "path": str(tmp_path),
                "kind": "local",
            }
        },
        str(tmp_path),
    )

    assert workspace["id"] == "workspace-local"
    assert workspace["name"] == "Project"
    assert workspace["path"] == str(tmp_path)
    assert workspace["kind"] == "local"


def test_workspace_from_params_rejects_cwd_outside_workspace(tmp_path):
    workspace_root = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace_root.mkdir()
    outside.mkdir()

    with pytest.raises(ValueError, match="cwd must be inside workspace path"):
        workspace_from_params(
            {"workspace": {"path": str(workspace_root)}},
            str(outside),
        )


def test_session_info_uses_agent_session_cwd_without_process_cwd(tmp_path, monkeypatch):
    workspace_root = tmp_path / "workspace"
    process_cwd = tmp_path / "hermes-agent"
    workspace_root.mkdir()
    process_cwd.mkdir()
    monkeypatch.chdir(process_cwd)

    agent = type("Agent", (), {"model": "test-model", "session_cwd": str(workspace_root)})()

    assert session_info(agent)["cwd"] == str(workspace_root)
