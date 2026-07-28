"""Shared grammar contract for conversation-scoped runtime commands."""

from hermes_cli.session_scope import parse_session_scoped_args


def test_scope_flags_can_appear_anywhere():
    parsed = parse_session_scoped_args("--session high")
    assert parsed.value == "high"
    assert parsed.explicit_session is True
    assert parsed.persist_global is False

    parsed = parse_session_scoped_args("fast --global")
    assert parsed.value == "fast"
    assert parsed.persist_global is True


def test_unicode_scope_dashes_are_normalized():
    for dash in ("—", "–", "−"):
        parsed = parse_session_scoped_args(f"high {dash}global")
        assert parsed.value == "high"
        assert parsed.persist_global is True


def test_global_wins_when_both_scopes_are_present():
    parsed = parse_session_scoped_args("--session normal --global")
    assert parsed.value == "normal"
    assert parsed.explicit_session is True
    assert parsed.persist_global is True
