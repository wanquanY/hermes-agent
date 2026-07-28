"""Tests for the removed auto-title compatibility facade."""

from unittest.mock import MagicMock

from agent.title_generator import (
    auto_title_session,
    generate_title,
    maybe_auto_title,
)


def test_generate_title_returns_none_without_side_effects():
    captured = []

    def _failure_callback(task, exc):
        captured.append((task, exc))

    assert generate_title(
        "first user message",
        "assistant response",
        failure_callback=_failure_callback,
        main_runtime={"model": "unused"},
    ) is None
    assert captured == []


def test_auto_title_session_does_not_read_or_write_session_db():
    db = MagicMock()
    seen = []

    auto_title_session(
        db,
        "sess-1",
        "first user message",
        "assistant response",
        title_callback=seen.append,
    )

    db.get_session_title.assert_not_called()
    db.set_session_title.assert_not_called()
    assert seen == []


def test_maybe_auto_title_does_not_start_worker_or_write_title():
    db = MagicMock()
    seen = []

    maybe_auto_title(
        db,
        "sess-1",
        "first user message",
        "assistant response",
        [{"role": "user", "content": "first user message"}],
        title_callback=seen.append,
    )

    db.get_session_title.assert_not_called()
    db.set_session_title.assert_not_called()
    assert seen == []
