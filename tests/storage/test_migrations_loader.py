from __future__ import annotations

import sqlite3
import textwrap

import pytest

from hermes_agent.storage.migrations import MigrationLoadError
from hermes_agent.storage.migrations import MigrationRunner
from hermes_agent.storage.migrations import load_migrations


def _write_migration(tmp_path, name: str, body: str) -> None:
    (tmp_path / name).write_text(textwrap.dedent(body).lstrip(), encoding="utf-8")


def test_loader_scans_directory_in_filename_order(tmp_path):
    _write_migration(
        tmp_path,
        "0002_second.py",
        """
        version = 2
        description = "second"

        def apply(cursor):
            cursor.execute("INSERT INTO applied VALUES (2)")
        """,
    )
    _write_migration(
        tmp_path,
        "0001_first.py",
        """
        version = 1
        description = "first"

        def apply(cursor):
            cursor.execute("INSERT INTO applied VALUES (1)")
        """,
    )

    records = load_migrations(tmp_path)

    assert [record.version for record in records] == [1, 2]
    assert [record.description for record in records] == ["first", "second"]


def test_loader_rejects_duplicate_versions(tmp_path):
    _write_migration(
        tmp_path,
        "0001_first.py",
        """
        version = 1
        description = "first"

        def apply(cursor):
            pass
        """,
    )
    _write_migration(
        tmp_path,
        "0001_duplicate.py",
        """
        version = 1
        description = "duplicate"

        def apply(cursor):
            pass
        """,
    )

    with pytest.raises(MigrationLoadError, match="duplicate migration version 1"):
        load_migrations(tmp_path)


def test_runner_stops_on_apply_error_without_bumping_version(tmp_path):
    _write_migration(
        tmp_path,
        "0001_baseline.py",
        """
        version = 1
        description = "baseline"

        def apply(cursor):
            cursor.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)")
        """,
    )
    _write_migration(
        tmp_path,
        "0002_breaks.py",
        """
        version = 2
        description = "breaks"

        def apply(cursor):
            raise RuntimeError("boom")
        """,
    )

    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE schema_version (version INTEGER NOT NULL)")
    conn.execute("INSERT INTO schema_version (version) VALUES (1)")

    with pytest.raises(RuntimeError, match="boom"):
        MigrationRunner(conn.cursor(), migrations_dir=tmp_path).run_all()

    assert conn.execute("SELECT version FROM schema_version").fetchone()[0] == 1

