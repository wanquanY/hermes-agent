"""Configuration adapter for bounded ``pre_llm_call`` hook context."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from tools.context_spill import SpillPolicy, spill_if_oversized as _spill

DEFAULT_MAX_CHARS = 10_000
DEFAULT_PREVIEW_HEAD = 500
DEFAULT_PREVIEW_TAIL = 500
DEFAULT_MAX_FILES = 100
DEFAULT_MAX_AGE_HOURS = 168


def _integer(value: Any, default: int, *, minimum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed >= minimum else default


def get_spill_config() -> dict[str, Any]:
    section: dict[str, Any] = {}
    try:
        from hermes_cli.config import load_config

        config = load_config() or {}
        hooks = config.get("hooks") if isinstance(config, dict) else None
        candidate = hooks.get("output_spill") if isinstance(hooks, dict) else None
        if isinstance(candidate, dict):
            section = candidate
    except Exception:
        pass
    directory = section.get("directory")
    return {
        "enabled": section.get("enabled", True) is not False,
        "max_chars": _integer(section.get("max_chars"), DEFAULT_MAX_CHARS, minimum=1),
        "preview_head": _integer(section.get("preview_head"), DEFAULT_PREVIEW_HEAD, minimum=0),
        "preview_tail": _integer(section.get("preview_tail"), DEFAULT_PREVIEW_TAIL, minimum=0),
        "max_files": _integer(section.get("max_files"), DEFAULT_MAX_FILES, minimum=1),
        "max_age_hours": _integer(
            section.get("max_age_hours"), DEFAULT_MAX_AGE_HOURS, minimum=1,
        ),
        "directory": directory if isinstance(directory, str) and directory.strip() else None,
    }


def spill_if_oversized(
    text: object,
    *,
    session_id: object = "",
    source: str = "plugin hook",
    config: dict[str, Any] | None = None,
) -> str:
    cfg = dict(config or get_spill_config())
    value = str(text or "")
    if not cfg.get("enabled", True):
        return value
    directory = cfg.get("directory")
    if directory:
        base = Path(str(directory)).expanduser()
    else:
        try:
            from hermes_constants import get_hermes_home

            base = Path(get_hermes_home()) / "hook_outputs"
        except Exception:
            base = Path.home() / ".hermes" / "hook_outputs"
    policy = SpillPolicy(
        max_chars=_integer(cfg.get("max_chars"), DEFAULT_MAX_CHARS, minimum=1),
        preview_head=_integer(cfg.get("preview_head"), DEFAULT_PREVIEW_HEAD, minimum=0),
        preview_tail=_integer(cfg.get("preview_tail"), DEFAULT_PREVIEW_TAIL, minimum=0),
        directory=base,
        max_files=_integer(cfg.get("max_files"), DEFAULT_MAX_FILES, minimum=1),
        max_age_seconds=(
            _integer(cfg.get("max_age_hours"), DEFAULT_MAX_AGE_HOURS, minimum=1) * 3600
        ),
    )
    return _spill(
        value,
        session_id=session_id,
        source=source,
        filename_prefix="hook-context",
        policy=policy,
    ).preview


__all__ = ["get_spill_config", "spill_if_oversized"]
