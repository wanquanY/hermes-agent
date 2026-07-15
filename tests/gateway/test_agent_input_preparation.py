from __future__ import annotations

import time
from datetime import datetime, timedelta
from types import SimpleNamespace

from hermes_gateway.agent_input_preparation import AgentInputPreparation


class _Store:
    def __init__(self, entry):
        self._entry = entry

    def get_entry(self, _session_key):
        return self._entry


def _preparation(*, history, entry):
    runner = SimpleNamespace(
        session_store=_Store(entry),
        _pending_model_notes={},
        _pending_skills_reload_notes={},
    )

    def is_fresh(value, *, window_secs):
        if isinstance(value, datetime):
            timestamp = value.timestamp()
        else:
            timestamp = float(value or 0)
        return time.time() - timestamp <= window_secs

    return AgentInputPreparation(
        runner=runner,
        session_key="scope-1",
        history=history,
        build_replay_entry=lambda role, content, metadata: {"role": role, "content": content},
        collect_history_media_paths=lambda _history: set(),
        last_transcript_timestamp=lambda rows: rows[-1].get("timestamp") if rows else 0,
        is_fresh_gateway_interruption=is_fresh,
        auto_continue_freshness_window=lambda: 1800,
        consume_native_image_paths=lambda _session_key: [],
    )


def test_fresh_persistent_resume_marker_wins_over_stale_transcript():
    entry = SimpleNamespace(
        resume_pending=True,
        resume_reason="restart_timeout",
        last_resume_marked_at=datetime.now(),
    )
    preparation = _preparation(
        history=[{"role": "assistant", "content": "old", "timestamp": time.time() - 7200}],
        entry=entry,
    )

    result = preparation.prepare("continue")

    assert "gateway restart" in result.message
    assert result.message.endswith("continue")


def test_blank_stale_auto_resume_never_reaches_model_empty():
    entry = SimpleNamespace(
        resume_pending=True,
        resume_reason="restart_timeout",
        last_resume_marked_at=datetime.now() - timedelta(hours=2),
    )
    preparation = _preparation(
        history=[{"role": "assistant", "content": "old", "timestamp": time.time() - 7200}],
        entry=entry,
    )

    result = preparation.prepare("")

    assert result.message.strip()
    assert "previous turn" in result.message


def test_ordinary_blank_input_is_not_rewritten():
    preparation = _preparation(history=[], entry=None)

    result = preparation.prepare("")

    assert result.message == ""
