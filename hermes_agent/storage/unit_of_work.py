"""Shared SQLite transaction boundary for repository-backed state services."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from types import TracebackType
from typing import Protocol, TypeVar


T = TypeVar("T")


class LockLike(Protocol):
    def __enter__(self) -> object: ...

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool | None: ...


class SqliteUnitOfWork:
    def __init__(self, conn: sqlite3.Connection, lock: LockLike) -> None:
        self._conn = conn
        self._lock = lock

    def execute(self, operation: Callable[[sqlite3.Connection], T]) -> T:
        with self._lock:
            owns_transaction = not self._conn.in_transaction
            if owns_transaction:
                self._conn.execute("BEGIN IMMEDIATE")
            try:
                result = operation(self._conn)
                if owns_transaction:
                    self._conn.execute("COMMIT")
                return result
            except BaseException:
                if owns_transaction and self._conn.in_transaction:
                    self._conn.execute("ROLLBACK")
                raise


__all__ = ["SqliteUnitOfWork"]
