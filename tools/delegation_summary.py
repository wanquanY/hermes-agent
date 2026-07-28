"""Bound delegated child summaries before they re-enter parent context."""

from __future__ import annotations

from pathlib import Path
import json
import logging
from typing import Any

from tools.context_spill import SpillPolicy, spill_if_oversized

logger = logging.getLogger(__name__)

DEFAULT_MAX_SUMMARY_CHARS = 24_000
SUMMARY_HEADROOM_FRACTION = 0.5
MIN_SUMMARY_CHARS = 2_000


def parent_summary_char_budget(
    parent_agent: Any,
    summary_count: int,
) -> int | None:
    """Return a per-summary budget from the parent's remaining input window."""
    try:
        compressor = getattr(parent_agent, "context_compressor", None)
        context_length = getattr(compressor, "context_length", None)
        if not isinstance(context_length, int) or context_length <= 0:
            return None
        used_tokens = getattr(parent_agent, "session_prompt_tokens", 0)
        if not isinstance(used_tokens, (int, float)) or used_tokens < 0:
            used_tokens = 0
        reserved_tokens = getattr(compressor, "max_tokens", 0)
        if not isinstance(reserved_tokens, (int, float)) or reserved_tokens < 0:
            reserved_tokens = 0
        headroom = context_length - int(used_tokens) - int(reserved_tokens)
        if headroom <= 0:
            return MIN_SUMMARY_CHARS
        batch_tokens = int(headroom * SUMMARY_HEADROOM_FRACTION)
        per_summary_tokens = batch_tokens // max(1, int(summary_count))
        return max(MIN_SUMMARY_CHARS, per_summary_tokens * 4)
    except Exception:
        logger.debug("delegation summary headroom calculation failed", exc_info=True)
        return None


def apply_summary_budget(
    results: list[dict[str, Any]],
    parent_agent: Any,
    *,
    static_cap: int,
    total_summary_count: int | None = None,
) -> None:
    """Apply an idempotent headroom cap and retain full text in private storage."""
    summaries = [
        entry
        for entry in results
        if isinstance(entry, dict)
        and not entry.get("summary_truncated")
        and isinstance(entry.get("summary"), str)
        and entry["summary"]
    ]
    if not summaries:
        return
    dynamic_cap = parent_summary_char_budget(
        parent_agent,
        total_summary_count or len(summaries),
    )
    candidates = [cap for cap in (static_cap, dynamic_cap) if cap and cap > 0]
    if not candidates:
        return
    cap = min(candidates)
    directory, directory_error = _resolve_directory()
    session_id = getattr(parent_agent, "session_id", "")

    for entry in summaries:
        original = entry["summary"]
        if len(original) <= cap:
            continue
        source = f"subagent {entry.get('task_index', '?')} summary"
        if directory is None:
            preview = _bounded_unavailable_preview(
                original,
                source=source,
                cap=cap,
                error=directory_error,
            )
            full_path = None
        else:
            policy = SpillPolicy(
                max_chars=cap,
                preview_head=max(1, int(cap * 0.75)),
                preview_tail=max(0, cap - int(cap * 0.75)),
                directory=directory,
                max_files=max(100, len(summaries) * 4),
                max_age_seconds=7 * 24 * 60 * 60,
            )
            spilled = spill_if_oversized(
                original,
                session_id=session_id,
                source=source,
                filename_prefix=(
                    f"subagent-summary-{entry.get('task_index', 'unknown')}"
                ),
                policy=policy,
            )
            preview = spilled.preview
            full_path = spilled.full_path

        entry["summary"] = preview
        entry["summary_truncated"] = True
        entry["summary_original_chars"] = len(original)
        if full_path:
            entry["summary_full_path"] = full_path
            entry["summary"] += (
                "\nRead the complete summary with read_file using path="
                f"{json.dumps(full_path)}."
            )


def _resolve_directory() -> tuple[Path | None, str]:
    try:
        from hermes_constants import get_hermes_dir

        return get_hermes_dir("cache/delegation", "delegation_cache"), ""
    except Exception as exc:
        logger.warning("delegation summary spill directory unavailable", exc_info=True)
        return None, f"{type(exc).__name__}: {exc}"


def _bounded_unavailable_preview(
    text: str,
    *,
    source: str,
    cap: int,
    error: str,
) -> str:
    """Fail bounded when even the spill directory cannot be resolved."""
    head_size = max(1, int(cap * 0.75))
    tail_size = max(0, cap - head_size)
    head = text[:head_size]
    tail = text[-tail_size:] if tail_size else ""
    lines = [
        f"[{source} truncated: {len(text):,} chars; full content unavailable]",
        "--- head ---",
        head,
    ]
    if tail:
        lines.extend(("--- tail ---", tail))
    lines.append(
        "[spill storage unavailable; oversized raw content was withheld: "
        f"{error[:240]}]"
    )
    return "\n".join(lines)


__all__ = [
    "DEFAULT_MAX_SUMMARY_CHARS",
    "MIN_SUMMARY_CHARS",
    "apply_summary_budget",
    "parent_summary_char_budget",
]
