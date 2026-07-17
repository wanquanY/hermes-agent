from __future__ import annotations

import os
from pathlib import Path
import stat
import time

from tools.context_spill import SpillPolicy, spill_if_oversized


def _policy(tmp_path: Path, **overrides) -> SpillPolicy:
    values = {
        "max_chars": 40,
        "preview_head": 10,
        "preview_tail": 10,
        "directory": tmp_path / "spill",
        "max_files": 3,
        "max_age_seconds": 3600,
    }
    values.update(overrides)
    return SpillPolicy(**values)


def test_small_context_is_unchanged_and_does_not_touch_disk(tmp_path: Path) -> None:
    result = spill_if_oversized(
        "small",
        session_id="session-1",
        source="hook",
        filename_prefix="context",
        policy=_policy(tmp_path),
    )
    assert result.preview == "small"
    assert result.spilled is False
    assert not (tmp_path / "spill").exists()


def test_oversized_context_is_private_lossless_and_path_safe(tmp_path: Path) -> None:
    raw = "HEAD" + "x" * 100 + "TAIL"
    result = spill_if_oversized(
        raw,
        session_id="../../escape/session",
        source="plugin hook",
        filename_prefix="hook",
        policy=_policy(tmp_path),
    )
    assert result.spilled is True
    assert "HEAD" in result.preview and "TAIL" in result.preview
    assert result.full_path
    path = Path(result.full_path)
    assert path.read_text(encoding="utf-8") == raw
    assert path.is_relative_to(tmp_path / "spill")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_write_failure_is_bounded_and_never_returns_raw_payload(tmp_path: Path) -> None:
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("block", encoding="utf-8")
    raw = "secret-" * 200
    result = spill_if_oversized(
        raw,
        session_id="session",
        source="hook",
        filename_prefix="hook",
        policy=_policy(tmp_path, directory=blocker),
    )
    assert result.spilled is True
    assert result.full_path is None
    assert result.preview != raw
    assert len(result.preview) < len(raw)
    assert "raw content was withheld" in result.preview


def test_prunes_old_and_excess_files_without_following_symlinks(tmp_path: Path) -> None:
    policy = _policy(tmp_path, max_files=2, max_age_seconds=1)
    session_dir = policy.directory / "session"
    session_dir.mkdir(parents=True)
    old = session_dir / "old.txt"
    old.write_text("old", encoding="utf-8")
    os.utime(old, (time.time() - 10, time.time() - 10))
    outside = tmp_path / "outside.txt"
    outside.write_text("keep", encoding="utf-8")
    (session_dir / "linked.txt").symlink_to(outside)

    for index in range(4):
        spill_if_oversized(
            f"{index}-" + "x" * 100,
            session_id="session",
            source="hook",
            filename_prefix="hook",
            policy=policy,
        )

    regular = [path for path in session_dir.glob("*.txt") if not path.is_symlink()]
    assert len(regular) <= 2
    assert not old.exists()
    assert outside.read_text(encoding="utf-8") == "keep"
