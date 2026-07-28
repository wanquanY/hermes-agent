"""Regression tests for streaming partial-line painting."""

import re

import pytest


def _strip_ansi(value: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*m", "", value)


@pytest.fixture
def cli_stub(monkeypatch):
    import cli as cli_module
    from cli import HermesCLI

    instance = HermesCLI.__new__(HermesCLI)
    instance.show_reasoning = False
    instance.final_response_markdown = "raw"
    instance.show_timestamps = False
    instance._reset_stream_state()

    emitted: list[str] = []
    monkeypatch.setattr(cli_module, "_cprint", emitted.append)
    monkeypatch.setattr(cli_module, "_terminal_width_for_streaming", lambda: 74)
    return instance, emitted


def test_long_paragraph_paints_before_first_newline(cli_stub):
    instance, emitted = cli_stub
    text = (
        "This opening paragraph previously remained invisible until a newline "
        "arrived, even though content tokens were already streaming. "
    ) * 4

    for index in range(0, len(text), 12):
        instance._stream_delta(text[index:index + 12])

    assert len(emitted) > 3


def test_force_flush_preserves_all_content(cli_stub):
    instance, emitted = cli_stub
    words = [f"word{index}" for index in range(120)]
    text = " ".join(words)

    for index in range(0, len(text), 7):
        instance._stream_delta(text[index:index + 7])
    instance._flush_stream()

    rendered = " ".join(_strip_ansi("\n".join(emitted)).split())
    for word in words:
        assert word in rendered


def test_short_partial_remains_buffered(cli_stub):
    instance, emitted = cli_stub
    instance._stream_delta("short line, no newline")

    assert "short line" not in _strip_ansi("\n".join(emitted))
    assert instance._stream_buf == "short line, no newline"


def test_table_partial_is_not_force_flushed(cli_stub):
    instance, emitted = cli_stub
    instance._stream_delta("| " + " | ".join(f"cell{i}" for i in range(20)) + " |")

    assert "cell19" not in _strip_ansi("\n".join(emitted))


def test_unbreakable_run_hard_wraps_without_loss(cli_stub):
    instance, emitted = cli_stub
    instance._stream_delta("x" * 300)
    instance._flush_stream()

    assert _strip_ansi("\n".join(emitted)).count("x") == 300
