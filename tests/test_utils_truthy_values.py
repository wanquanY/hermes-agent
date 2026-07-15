"""Tests for shared environment-value helpers."""

from utils import env_float
from utils import env_int
from utils import env_var_enabled
from utils import is_truthy_value


def test_is_truthy_value_accepts_common_truthy_strings():
    assert is_truthy_value("true") is True
    assert is_truthy_value(" YES ") is True
    assert is_truthy_value("on") is True
    assert is_truthy_value("1") is True


def test_is_truthy_value_respects_default_for_none():
    assert is_truthy_value(None, default=True) is True
    assert is_truthy_value(None, default=False) is False


def test_is_truthy_value_rejects_falsey_strings():
    assert is_truthy_value("false") is False
    assert is_truthy_value("0") is False
    assert is_truthy_value("off") is False


def test_env_var_enabled_uses_shared_truthy_rules(monkeypatch):
    monkeypatch.setenv("HERMES_TEST_BOOL", "YeS")
    assert env_var_enabled("HERMES_TEST_BOOL") is True

    monkeypatch.setenv("HERMES_TEST_BOOL", "no")
    assert env_var_enabled("HERMES_TEST_BOOL") is False


def test_numeric_env_helpers_use_fallback_for_missing_or_invalid_values(monkeypatch):
    monkeypatch.delenv("HERMES_TEST_NUMBER", raising=False)
    assert env_int("HERMES_TEST_NUMBER", 7) == 7
    assert env_float("HERMES_TEST_NUMBER", 1.5) == 1.5

    monkeypatch.setenv("HERMES_TEST_NUMBER", "bad")
    assert env_int("HERMES_TEST_NUMBER", 7) == 7
    assert env_float("HERMES_TEST_NUMBER", 1.5) == 1.5

    monkeypatch.setenv("HERMES_TEST_NUMBER", "42")
    assert env_int("HERMES_TEST_NUMBER", 7) == 42
    assert env_float("HERMES_TEST_NUMBER", 1.5) == 42.0
