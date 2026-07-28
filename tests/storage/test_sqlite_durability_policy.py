from __future__ import annotations

import os
import signal
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

from hermes_agent.storage.sqlite_wal import configure_sqlite_connection
from hermes_cli.kanban_db import connect as connect_kanban


REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def _pragma_int(conn: sqlite3.Connection, name: str) -> int:
    row = conn.execute(f"PRAGMA {name}").fetchone()
    assert row is not None
    return int(row[0])


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS durability policy")
def test_macos_wal_policy_survives_close_and_reopen(tmp_path) -> None:
    path = tmp_path / "durable.db"
    conn = sqlite3.connect(path, isolation_level=None)
    try:
        assert configure_sqlite_connection(conn, db_label=str(path)) == "wal"
        conn.execute("CREATE TABLE payloads (id INTEGER PRIMARY KEY, value TEXT NOT NULL)")
        conn.execute("INSERT INTO payloads(value) VALUES ('committed')")
        assert _pragma_int(conn, "synchronous") == 2
        assert _pragma_int(conn, "checkpoint_fullfsync") == 1
        conn.execute("PRAGMA wal_checkpoint(FULL)")
    finally:
        conn.close()

    reopened = sqlite3.connect(path, isolation_level=None)
    try:
        assert configure_sqlite_connection(reopened, db_label=str(path)) == "wal"
        assert _pragma_int(reopened, "synchronous") == 2
        assert _pragma_int(reopened, "checkpoint_fullfsync") == 1
        assert reopened.execute("SELECT value FROM payloads").fetchone()[0] == "committed"
        assert reopened.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        reopened.close()


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS durability policy")
def test_kanban_connection_cannot_downgrade_synchronous_full(tmp_path) -> None:
    conn = connect_kanban(tmp_path / "kanban.db")
    try:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        assert _pragma_int(conn, "synchronous") == 2
        assert _pragma_int(conn, "checkpoint_fullfsync") == 1
        assert _pragma_int(conn, "foreign_keys") == 1
        assert _pragma_int(conn, "busy_timeout") == 30_000
    finally:
        conn.close()


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS durability policy")
def test_forced_process_termination_reopens_with_integrity(tmp_path) -> None:
    """A SIGKILL may lose the active statement, never prior commits or the DB."""

    path = tmp_path / "forced-termination.db"
    ready = tmp_path / "writer-ready"
    script = """
import sqlite3
import sys
import time
from pathlib import Path

from hermes_agent.storage.sqlite_wal import configure_sqlite_connection

db_path = Path(sys.argv[1])
ready_path = Path(sys.argv[2])
conn = sqlite3.connect(db_path, isolation_level=None)
configure_sqlite_connection(conn, db_label=str(db_path))
conn.execute("CREATE TABLE committed_rows (id INTEGER PRIMARY KEY, value TEXT NOT NULL)")
conn.execute("INSERT INTO committed_rows(value) VALUES ('ready')")
ready_path.write_text("ready", encoding="utf-8")
counter = 0
while True:
    conn.execute("INSERT INTO committed_rows(value) VALUES (?)", (f"row-{counter}",))
    counter += 1
    time.sleep(0.001)
"""
    process = subprocess.Popen(
        [sys.executable, "-c", script, str(path), str(ready)],
        cwd=REPO_ROOT,
    )
    deadline = time.monotonic() + 10.0
    while not ready.exists() and process.poll() is None and time.monotonic() < deadline:
        time.sleep(0.01)
    assert ready.exists(), "writer did not commit its ready row"

    os.kill(process.pid, signal.SIGKILL)
    assert process.wait(timeout=5.0) == -signal.SIGKILL

    reopened = sqlite3.connect(path, isolation_level=None)
    try:
        configure_sqlite_connection(reopened, db_label=str(path))
        assert reopened.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert reopened.execute("SELECT COUNT(*) FROM committed_rows").fetchone()[0] >= 1
        assert _pragma_int(reopened, "synchronous") == 2
        assert _pragma_int(reopened, "checkpoint_fullfsync") == 1
    finally:
        reopened.close()


def test_non_macos_policy_does_not_force_full(monkeypatch, tmp_path) -> None:
    from hermes_agent.storage import sqlite_wal

    monkeypatch.setattr(sqlite_wal.sys, "platform", "linux")
    conn = sqlite3.connect(tmp_path / "linux.db", isolation_level=None)
    try:
        configure_sqlite_connection(conn)
        assert _pragma_int(conn, "checkpoint_fullfsync") == 0
    finally:
        conn.close()


def test_durability_pragmas_have_one_storage_owner() -> None:
    owner = Path("hermes_agent/storage/sqlite_wal.py")
    offenders: list[str] = []
    durability_tokens = (
        "PRAGMA journal_mode",
        "PRAGMA synchronous",
        "PRAGMA checkpoint_fullfsync",
    )
    excluded_roots = {".venv", "docs", "tests", "__pycache__"}
    for path in REPO_ROOT.rglob("*.py"):
        relative = path.relative_to(REPO_ROOT)
        if relative == owner or excluded_roots.intersection(relative.parts):
            continue
        source = path.read_text(encoding="utf-8")
        if any(token in source for token in durability_tokens):
            offenders.append(relative.as_posix())

    assert not offenders, (
        "SQLite durability pragmas must be owned by sqlite_wal.py; found in: "
        + ", ".join(sorted(offenders))
    )
