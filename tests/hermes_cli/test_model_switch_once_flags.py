from hermes_cli.model_switch import (
    parse_model_flags,
    parse_model_flags_detailed,
    resolve_persist_behavior,
)


def test_parse_model_flags_detailed_supports_once_and_unicode_dash():
    parsed = parse_model_flags_detailed(
        "sonnet --provider anthropic —once"
    )

    assert parsed.model_input == "sonnet"
    assert parsed.explicit_provider == "anthropic"
    assert parsed.is_global is False
    assert parsed.force_refresh is False
    assert parsed.is_session is False
    assert parsed.is_once is True


def test_parse_model_flags_legacy_wrapper_strips_once():
    parsed = parse_model_flags("sonnet --once")

    assert parsed == ("sonnet", "", False, False, False)


def test_once_always_disables_persistence(monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {"model": {"persist_switch_by_default": True}},
    )

    assert resolve_persist_behavior(False, False, is_once=True) is False
    assert resolve_persist_behavior(True, False, is_once=True) is False
