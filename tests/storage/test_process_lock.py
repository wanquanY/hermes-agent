from __future__ import annotations

import multiprocessing
from pathlib import Path

import pytest

from hermes_agent.storage.process_lock import (
    ProcessLockError,
    exclusive_process_lock,
)


def _try_lock(path: str, result: "multiprocessing.Queue[bool]") -> None:
    with exclusive_process_lock(Path(path), blocking=False) as lease:
        result.put(lease.acquired)


def test_nonblocking_lock_reports_cross_process_contention(tmp_path):
    path = tmp_path / "store.lock"
    context = multiprocessing.get_context("spawn")
    result = context.Queue()

    with exclusive_process_lock(path) as held:
        assert held.acquired is True
        process = context.Process(target=_try_lock, args=(str(path), result))
        process.start()
        process.join(timeout=5)

    assert process.exitcode == 0
    assert result.get(timeout=1) is False
    with exclusive_process_lock(path, blocking=False) as reacquired:
        assert reacquired.acquired is True


def test_lock_open_error_fails_closed(tmp_path):
    parent_file = tmp_path / "not-a-directory"
    parent_file.write_text("x")

    with pytest.raises(ProcessLockError):
        with exclusive_process_lock(parent_file / "store.lock"):
            raise AssertionError("body must not execute")
