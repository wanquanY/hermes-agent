from __future__ import annotations

import sqlite3

from hermes_agent.storage.sqlite_connection_lock import lock_for_connection
from hermes_agent.storage.unit_of_work import SqliteUnitOfWork


def test_unit_of_work_read_does_not_start_write_transaction(tmp_path):
    conn = sqlite3.connect(tmp_path / "state.db", isolation_level=None)
    unit_of_work = SqliteUnitOfWork(conn, lock_for_connection(conn))
    try:
        conn.execute("CREATE TABLE sample (value INTEGER NOT NULL)")
        conn.execute("INSERT INTO sample (value) VALUES (7)")

        transaction_state: list[bool] = []

        def read_value(read_conn: sqlite3.Connection) -> int:
            transaction_state.append(read_conn.in_transaction)
            return int(read_conn.execute("SELECT value FROM sample").fetchone()[0])

        assert unit_of_work.read(read_value) == 7
        assert transaction_state == [False]
        assert conn.in_transaction is False
    finally:
        conn.close()
