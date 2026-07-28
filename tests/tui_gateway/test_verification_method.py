from __future__ import annotations

from hermes_agent.composition.cli_session_store import open_cli_session_store


def test_verification_status_projects_application_aggregate(monkeypatch, tmp_path) -> None:
    from tui_gateway.methods import verification

    root = tmp_path / "project"
    root.mkdir()
    (root / "package.json").write_text(
        '{"scripts":{"test":"vitest run"}}', encoding="utf-8"
    )
    (root / "pnpm-lock.yaml").write_text("lockfileVersion: '9.0'", encoding="utf-8")
    store = open_cli_session_store(tmp_path / "state.db")
    store.verification.mark_edited("conversation-1", root, ["src/app.ts"])
    monkeypatch.setattr(verification, "_get_db", lambda: store)

    response = verification.verification_status(
        "request-1",
        {"session_id": "conversation-1", "cwd": str(root)},
    )

    assert response["id"] == "request-1"
    result = response["result"]["verification"]
    assert result["scope_id"] == "conversation-1"
    assert result["workspace_root"] == str(root)
    assert result["status"] == "unverified"
    assert result["edit_generation"] == 1
    assert result["changed_paths"] == ["src/app.ts"]
    store.close()


def test_verification_status_without_workspace_is_not_applicable(
    monkeypatch, tmp_path
) -> None:
    from tui_gateway.methods import verification

    store = open_cli_session_store(tmp_path / "state.db")
    monkeypatch.setattr(verification, "_get_db", lambda: store)
    monkeypatch.setattr(verification, "_live_workspace", lambda _scope: "")
    monkeypatch.setattr(verification, "_stored_workspace", lambda _db, _scope: "")

    response = verification.verification_status(
        "request-1", {"session_id": "conversation-1"}
    )

    result = response["result"]["verification"]
    assert result["status"] == "not_applicable"
    assert result["workspace_root"] == ""
    store.close()


def test_project_facts_projects_the_shared_detector(tmp_path) -> None:
    from tui_gateway.methods import verification

    root = tmp_path / "project"
    root.mkdir()
    (root / "package.json").write_text(
        '{"scripts":{"test":"vitest run"}}', encoding="utf-8"
    )
    (root / "pnpm-lock.yaml").write_text("lockfileVersion: '9.0'", encoding="utf-8")

    response = verification.project_facts("request-1", {"cwd": str(root)})

    assert response["id"] == "request-1"
    assert response["result"]["facts"] == {
        "root": str(root),
        "manifests": ["package.json"],
        "packageManagers": ["pnpm"],
        "verifyCommands": ["pnpm run test"],
        "contextFiles": [],
    }


def test_project_facts_returns_null_outside_a_project(tmp_path) -> None:
    from tui_gateway.methods import verification

    response = verification.project_facts("request-1", {"cwd": str(tmp_path)})

    assert response["result"] == {"facts": None}
