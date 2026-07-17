from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from hermes_agent.application.verification_service import VerificationService
from hermes_agent.composition.session_repository_db import connect_session_repository_db
from hermes_agent.repositories.verification_repo import VerificationRepository
from hermes_agent.storage.sqlite_connection_lock import lock_for_connection
from hermes_agent.storage.unit_of_work import SqliteUnitOfWork


def _service(conn: sqlite3.Connection, *, clock=lambda: 1000.0, **kwargs) -> VerificationService:
    return VerificationService(
        VerificationRepository(conn),
        SqliteUnitOfWork(conn, lock_for_connection(conn)),
        clock=clock,
        **kwargs,
    )


def _workspace(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    root.mkdir(parents=True)
    (root / "package.json").write_text(
        '{"scripts":{"test":"vitest run","lint":"eslint .","build":"vite build"}}',
        encoding="utf-8",
    )
    (root / "pnpm-lock.yaml").write_text("lockfileVersion: '9.0'", encoding="utf-8")
    return root


def test_edit_failure_pass_and_stale_generation(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    conn = connect_session_repository_db(tmp_path / "state.db")
    service = _service(conn)

    assert service.status("conversation-1", root).status == "not_required"
    edited = service.mark_edited("conversation-1", root, [root / "src/app.ts"])
    assert edited is not None
    assert edited.status == "unverified"
    assert edited.edit_generation == 1
    assert edited.changed_paths == ("src/app.ts",)

    failed = service.record_terminal(
        "conversation-1",
        command="corepack pnpm test -- src/app.test.ts",
        cwd=root,
        exit_code=1,
        output="one failed",
    )
    assert failed is not None
    assert failed.status == "failed"
    assert failed.evidence is not None
    assert failed.evidence.scope == "targeted"
    assert failed.last_verified_generation == -1

    passed = service.record_terminal(
        "conversation-1",
        command="pnpm test -- src/app.test.ts",
        cwd=root,
        exit_code=0,
        output="one passed",
    )
    assert passed is not None
    assert passed.status == "passed"
    assert passed.evidence is not None
    assert passed.evidence.kind == "test"
    assert passed.last_verified_generation == 1

    stale = service.mark_edited("conversation-1", root, ["src/next.ts"])
    assert stale is not None
    assert stale.status == "unverified"
    assert stale.edit_generation == 2
    assert stale.last_verified_generation == 1
    conn.close()


def test_full_scope_alias_and_unrelated_commands(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    conn = connect_session_repository_db(tmp_path / "state.db")
    service = _service(conn)
    service.mark_edited("conversation-1", root, ["src/app.ts"])

    assert service.record_terminal(
        "conversation-1", command="git status", cwd=root, exit_code=0
    ) is None
    result = service.record_terminal(
        "conversation-1",
        command="corepack pnpm run lint -- --quiet",
        cwd=root,
        exit_code=0,
    )
    assert result is not None
    assert result.status == "passed"
    assert result.evidence is not None
    assert result.evidence.kind == "lint"
    assert result.evidence.scope == "full"
    conn.close()


def test_requirement_is_bounded_and_typed(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    conn = connect_session_repository_db(tmp_path / "state.db")
    service = _service(conn)
    service.mark_edited("conversation-1", root, ["src/app.ts"])

    requirement = service.completion_requirement(
        "conversation-1", root, attempt=0, max_attempts=1
    )
    assert requirement is not None
    assert requirement.attempt == 1
    assert "runtime policy context, not a user message" in requirement.prompt()
    assert service.completion_requirement(
        "conversation-1", root, attempt=1, max_attempts=1
    ) is None
    conn.close()


def test_document_only_and_no_contract_are_not_applicable(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    conn = connect_session_repository_db(tmp_path / "state.db")
    service = _service(conn)
    document = service.mark_edited("conversation-1", root, ["README.md"])
    assert document is not None
    assert document.status == "not_applicable"
    assert service.completion_requirement("conversation-1", root, attempt=0) is None

    prose = service.mark_edited(
        "conversation-prose", root, ["LICENSE", "docs/notes.asciidoc"]
    )
    assert prose is not None
    assert prose.status == "not_applicable"

    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "AGENTS.md").write_text("instructions", encoding="utf-8")
    no_contract = service.mark_edited("conversation-2", plain, ["src/app.py"])
    assert no_contract is not None
    assert no_contract.status == "not_applicable"
    conn.close()


def test_python_verification_runner_aliases_are_recognized(tmp_path: Path) -> None:
    root = tmp_path / "python-project"
    root.mkdir()
    (root / "pyproject.toml").write_text(
        "[tool.pytest.ini_options]\n", encoding="utf-8"
    )
    conn = connect_session_repository_db(tmp_path / "state.db")
    service = _service(conn)

    for index, runner in enumerate(("uv", "poetry", "pipenv"), start=1):
        scope = f"conversation-{index}"
        service.mark_edited(scope, root, ["src/app.py"])
        result = service.record_terminal(
            scope,
            command=f"{runner} run pytest tests/test_app.py",
            cwd=root,
            exit_code=0,
        )
        assert result is not None
        assert result.status == "passed"
        assert result.evidence is not None
        assert result.evidence.scope == "targeted"
    conn.close()


def test_multi_workspace_and_restart_are_isolated(tmp_path: Path) -> None:
    first = _workspace(tmp_path / "one")
    second = _workspace(tmp_path / "two")
    db_path = tmp_path / "state.db"
    conn = connect_session_repository_db(db_path)
    service = _service(conn)
    service.mark_edited("conversation-1", first, ["src/one.ts"])
    service.mark_edited("conversation-1", second, ["src/two.ts"])
    service.record_terminal(
        "conversation-1", command="pnpm test", cwd=first, exit_code=0
    )
    assert service.status("conversation-1", first).status == "passed"
    assert service.status("conversation-1", second).status == "unverified"
    conn.close()

    reopened = connect_session_repository_db(db_path)
    restored = _service(reopened).status("conversation-1", first)
    assert restored is not None
    assert restored.status == "passed"
    assert restored.changed_paths == ("src/one.ts",)
    reopened.close()


def test_retention_keeps_latest_evidence(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    ticks = iter((1000.0, 1001.0, 1002.0, 1003.0))
    conn = connect_session_repository_db(tmp_path / "state.db")
    service = _service(conn, clock=lambda: next(ticks), retain_count=2)
    service.mark_edited("conversation-1", root, ["src/app.ts"])
    for code in (1, 1, 0):
        service.record_terminal(
            "conversation-1", command="pnpm test", cwd=root, exit_code=code
        )
    count = conn.execute(
        "SELECT COUNT(*) FROM verification_evidence WHERE scope_id = ?",
        ("conversation-1",),
    ).fetchone()[0]
    assert count == 2
    assert service.status("conversation-1", root).status == "passed"
    conn.close()


def test_concurrent_writers_preserve_every_generation(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    db_path = tmp_path / "state.db"
    connect_session_repository_db(db_path).close()

    def write(index: int) -> None:
        conn = connect_session_repository_db(db_path)
        try:
            _service(conn).mark_edited(
                "conversation-1", root, [f"src/file-{index}.ts"]
            )
        finally:
            conn.close()

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(write, range(20)))

    conn = connect_session_repository_db(db_path)
    state = _service(conn).status("conversation-1", root)
    assert state is not None
    assert state.edit_generation == 20
    assert len(state.changed_paths) == 20
    conn.close()
