from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

from hermes_state.migrations.merge_profile_dbs import merge_profile_dbs


def _connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY,
            source TEXT NOT NULL,
            started_at REAL NOT NULL
        );
        CREATE TABLE session_index (
            session_id TEXT PRIMARY KEY,
            title TEXT NOT NULL DEFAULT '',
            updated_at REAL NOT NULL DEFAULT 0
        );
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT,
            timestamp REAL NOT NULL
        );
        CREATE TABLE runs (
            run_id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            status TEXT NOT NULL,
            started_at REAL NOT NULL,
            updated_at REAL NOT NULL
        );
        CREATE TABLE no_key_rows (
            value TEXT
        );
        """
    )
    conn.commit()
    return conn


def _profile_db(root: Path, area: str, slug: str) -> Path:
    return root / area / slug / "state.db"


def _tui_gateway_db(root: Path, area: str, slug: str) -> Path:
    return root / area / slug / "tui-gateway" / "state.db"


def _seed_session(db_path: Path, session_id: str) -> None:
    conn = _connect(db_path)
    conn.execute(
        "INSERT INTO sessions (id, source, started_at) VALUES (?, 'cli', 1)",
        (session_id,),
    )
    conn.execute(
        "INSERT INTO session_index (session_id, title, updated_at) VALUES (?, ?, 1)",
        (session_id, f"title {session_id}"),
    )
    conn.execute(
        "INSERT INTO messages (session_id, role, content, timestamp) "
        "VALUES (?, 'user', ?, 1)",
        (session_id, f"hello {session_id}"),
    )
    conn.execute(
        "INSERT INTO runs (run_id, session_id, status, started_at, updated_at) "
        "VALUES (?, ?, 'completed', 1, 1)",
        (f"run-{session_id}", session_id),
    )
    conn.commit()
    conn.close()


def _session_ids(db_path: Path) -> list[str]:
    conn = sqlite3.connect(db_path)
    try:
        return [
            row[0]
            for row in conn.execute("SELECT id FROM sessions ORDER BY id").fetchall()
        ]
    finally:
        conn.close()


def _count_rows(db_path: Path, table_name: str) -> int:
    conn = sqlite3.connect(db_path)
    try:
        return int(conn.execute(f"SELECT COUNT(*) FROM {table_name}").fetchone()[0])
    finally:
        conn.close()


def test_merge_three_profile_dbs_each_with_sessions_inserts_all(tmp_path: Path):
    root_db = tmp_path / "state.db"
    _connect(root_db).close()
    _seed_session(_profile_db(tmp_path, "profiles", "alpha"), "s-alpha")
    _seed_session(_profile_db(tmp_path, "profiles", "beta"), "s-beta")
    _seed_session(_profile_db(tmp_path, "drafts", "gamma"), "s-gamma")

    result = merge_profile_dbs(tmp_path, backup=False)

    assert result.total_profile_dbs == 3
    assert result.errors == []
    assert _session_ids(root_db) == ["s-alpha", "s-beta", "s-gamma"]
    assert result.merged_rows_per_table["sessions"] == 3
    assert result.merged_rows_per_table["session_index"] == 3
    assert result.merged_rows_per_table["runs"] == 3


def test_discovery_includes_tui_gateway_subdir(tmp_path: Path):
    root_db = tmp_path / "state.db"
    _connect(root_db).close()
    _seed_session(_tui_gateway_db(tmp_path, "profiles", "alpha"), "s-alpha")
    _seed_session(_tui_gateway_db(tmp_path, "drafts", "beta"), "s-beta")

    result = merge_profile_dbs(tmp_path, backup=False)

    assert result.total_profile_dbs == 2
    assert result.errors == []
    assert _session_ids(root_db) == ["s-alpha", "s-beta"]


def test_merge_idempotent_second_run_no_duplicates(tmp_path: Path):
    root_db = tmp_path / "state.db"
    _connect(root_db).close()
    _seed_session(_profile_db(tmp_path, "profiles", "alpha"), "s-alpha")

    first = merge_profile_dbs(tmp_path, backup=False)
    second = merge_profile_dbs(tmp_path, backup=False)

    assert first.merged_rows_per_table["sessions"] == 1
    assert second.merged_rows_per_table == {}
    assert _count_rows(root_db, "sessions") == 1
    assert _count_rows(root_db, "messages") == 1


def test_conflicts_are_reported_per_table(tmp_path: Path):
    root_db = tmp_path / "state.db"
    root = _connect(root_db)
    root.execute(
        "INSERT INTO sessions (id, source, started_at) VALUES ('same', 'root', 1)"
    )
    root.commit()
    root.close()
    _seed_session(_profile_db(tmp_path, "profiles", "alpha"), "same")

    result = merge_profile_dbs(tmp_path, backup=False)

    conn = sqlite3.connect(root_db)
    try:
        row = conn.execute("SELECT source FROM sessions WHERE id = 'same'").fetchone()
    finally:
        conn.close()
    assert row[0] == "root"
    assert "sessions" not in result.merged_rows_per_table
    assert result.conflicts == [
        {
            "db": str(_profile_db(tmp_path, "profiles", "alpha")),
            "table": "sessions",
            "count": 1,
            "keys": [{"id": "same"}],
        }
    ]
    assert result.to_json_dict()["conflicts"] == result.conflicts


def test_successful_merge_renames_source_db(tmp_path: Path):
    root_db = tmp_path / "state.db"
    _connect(root_db).close()
    profile = _profile_db(tmp_path, "profiles", "alpha")
    _seed_session(profile, "s-alpha")
    profile.with_name("state.db-wal").write_bytes(b"wal")
    profile.with_name("state.db-shm").write_bytes(b"shm")

    result = merge_profile_dbs(tmp_path, backup=True)

    backup_paths = [Path(path) for path in result.backups]
    assert not profile.exists()
    assert profile.with_name("state.db.migrated-to-root.bak").exists()
    assert any(
        path.name.startswith("state.db.merged-to-root.bak.")
        for path in backup_paths
    )
    assert any(
        path.name.startswith("state.db-wal.merged-to-root.bak.")
        for path in backup_paths
    )
    assert any(
        path.name.startswith("state.db-shm.merged-to-root.bak.")
        for path in backup_paths
    )
    assert all(path.exists() for path in backup_paths)


def test_failed_merge_leaves_source_in_place(tmp_path: Path):
    root_db = tmp_path / "state.db"
    _connect(root_db).close()
    _seed_session(_profile_db(tmp_path, "profiles", "good"), "s-good")
    bad = _profile_db(tmp_path, "profiles", "bad")
    bad.parent.mkdir(parents=True)
    bad.write_bytes(b"not sqlite")

    result = merge_profile_dbs(tmp_path, backup=False)

    assert _session_ids(root_db) == ["s-good"]
    assert str(bad) in result.skipped_dbs
    assert str(bad) in result.failed_dbs
    assert bad.exists()
    assert not bad.with_name("state.db.migrated-to-root.bak").exists()
    assert not _profile_db(tmp_path, "profiles", "good").exists()
    assert result.errors
    assert "bad/state.db" in result.errors[0]


def test_result_failed_dbs_list_is_complete(tmp_path: Path):
    root_db = tmp_path / "state.db"
    _connect(root_db).close()
    bad_profile = _profile_db(tmp_path, "profiles", "bad")
    bad_draft = _profile_db(tmp_path, "drafts", "bad")
    for bad in (bad_profile, bad_draft):
        bad.parent.mkdir(parents=True)
        bad.write_bytes(b"not sqlite")

    result = merge_profile_dbs(tmp_path, backup=False)

    assert result.skipped_dbs == [str(bad_draft), str(bad_profile)]
    assert result.failed_dbs == result.skipped_dbs
    assert len(result.errors) == 2
    assert result.to_json_dict()["failed_dbs"] == result.failed_dbs


def test_merge_handles_empty_profile_dir(tmp_path: Path):
    (tmp_path / "profiles").mkdir()

    result = merge_profile_dbs(tmp_path, backup=True)

    assert result.total_profile_dbs == 0
    assert result.merged_rows_per_table == {}
    assert result.backups == []
    assert result.errors == []
    assert result.failed_dbs == []


def test_dry_run_does_not_modify_root_or_backup(tmp_path: Path):
    root_db = tmp_path / "state.db"
    _connect(root_db).close()
    profile = _profile_db(tmp_path, "profiles", "alpha")
    _seed_session(profile, "s-alpha")

    result = merge_profile_dbs(tmp_path, backup=True, dry_run=True)

    assert result.merged_rows_per_table["sessions"] == 1
    assert result.backups == []
    assert _session_ids(root_db) == []
    assert not list(profile.parent.glob("state.db.merged-to-root.bak.*"))
    assert not list(profile.parent.glob("state.db.migrated-to-root.bak"))


def test_cli_outputs_valid_json_to_stdout(tmp_path: Path):
    root_db = tmp_path / "state.db"
    _connect(root_db).close()
    _seed_session(_profile_db(tmp_path, "profiles", "alpha"), "s-alpha")

    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "hermes_state.migrations.merge_profile_dbs",
            "--root",
            str(tmp_path),
            "--dry-run",
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    payload = json.loads(completed.stdout)
    assert payload["status"] == "ok"
    assert payload["total_dbs"] == 1
    assert payload["merged_rows"]["sessions"] == 1
    assert payload["backups"] == []
    assert payload["failed_dbs"] == []
    assert payload["conflicts"] == []
    assert completed.stderr == ""
