"""Secure, bounded spill storage for model-visible context.

The full payload is an audit artifact; :attr:`SpillResult.preview` is the only
value that may enter a model prompt.  I/O failures therefore fail bounded and
never fall back to the original oversized text.
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
import os
from pathlib import Path
import re
import stat
import time
import uuid

logger = logging.getLogger(__name__)

_SAFE_SEGMENT_RE = re.compile(r"[^A-Za-z0-9._-]+")


@dataclass(frozen=True)
class SpillPolicy:
    max_chars: int
    preview_head: int
    preview_tail: int
    directory: Path
    max_files: int = 100
    max_age_seconds: float = 7 * 24 * 60 * 60


@dataclass(frozen=True)
class SpillResult:
    preview: str
    full_path: str | None
    original_chars: int
    spilled: bool


def safe_path_segment(value: object, *, fallback: str = "no-session") -> str:
    """Remove all path semantics from an untrusted identifier."""
    text = str(value or "").strip()
    text = _SAFE_SEGMENT_RE.sub("_", text).strip("._-")
    return (text[:120] or fallback)


def spill_if_oversized(
    value: object,
    *,
    session_id: object,
    source: str,
    filename_prefix: str,
    policy: SpillPolicy,
) -> SpillResult:
    """Return the original small text or a bounded preview of a spilled value."""
    text = str(value or "")
    if not text or len(text) <= max(1, int(policy.max_chars)):
        return SpillResult(text, None, len(text), False)

    safe_source = str(source or "context").strip()[:120] or "context"
    session_dir = policy.directory / safe_path_segment(session_id)
    full_path: str | None = None
    write_error = ""
    try:
        _ensure_private_directory(policy.directory)
        _ensure_private_directory(session_dir)
        full_path = _write_private_file(
            session_dir,
            prefix=safe_path_segment(filename_prefix, fallback="context"),
            text=text,
        )
        _prune_session_dir(session_dir, policy=policy)
    except Exception as exc:  # fail bounded: never return raw oversized text
        write_error = f"{type(exc).__name__}: {exc}"
        logger.warning("%s spill failed: %s", safe_source, write_error)

    preview = _build_preview(
        text,
        source=safe_source,
        head=max(0, int(policy.preview_head)),
        tail=max(0, int(policy.preview_tail)),
        full_path=full_path,
        write_error=write_error,
    )
    return SpillResult(preview, full_path, len(text), True)


def _ensure_private_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    mode = path.lstat().st_mode
    if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
        raise OSError(f"spill directory is not a real directory: {path}")
    try:
        path.chmod(0o700)
    except OSError:
        pass


def _write_private_file(directory: Path, *, prefix: str, text: str) -> str:
    path = directory / f"{prefix}-{time.time_ns()}-{uuid.uuid4().hex[:12]}.txt"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
    except Exception:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    return str(path.resolve())


def _prune_session_dir(directory: Path, *, policy: SpillPolicy) -> None:
    now = time.time()
    files: list[tuple[float, Path]] = []
    for path in directory.glob("*.txt"):
        try:
            info = path.lstat()
        except OSError:
            continue
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            continue
        if policy.max_age_seconds > 0 and now - info.st_mtime > policy.max_age_seconds:
            try:
                path.unlink()
            except OSError:
                pass
            continue
        files.append((info.st_mtime, path))
    files.sort(key=lambda item: (item[0], item[1].name), reverse=True)
    for _mtime, path in files[max(1, int(policy.max_files)) :]:
        try:
            path.unlink()
        except OSError:
            pass


def _build_preview(
    text: str,
    *,
    source: str,
    head: int,
    tail: int,
    full_path: str | None,
    write_error: str,
) -> str:
    head_text = text[:head] if head else ""
    tail_text = text[-tail:] if tail and len(text) > head else ""
    location = f"full content saved to {full_path}" if full_path else "full content unavailable"
    lines = [f"[{source} truncated: {len(text):,} chars; {location}]", "--- head ---"]
    if head_text:
        lines.append(head_text)
    if tail_text:
        lines.extend(("--- tail ---", tail_text))
    if write_error:
        lines.append("[spill write failed; oversized raw content was withheld]")
    return "\n".join(lines)


__all__ = ["SpillPolicy", "SpillResult", "safe_path_segment", "spill_if_oversized"]
