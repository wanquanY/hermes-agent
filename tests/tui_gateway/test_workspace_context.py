import pytest

from tui_gateway.services.workspace import normalize_session_cwd, workspace_from_params


def test_normalize_session_cwd_rejects_missing_path(tmp_path):
    with pytest.raises(ValueError, match="cwd does not exist"):
        normalize_session_cwd(tmp_path / "missing")


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
