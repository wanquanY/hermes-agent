"""Phase G / D5 — dispatch pre-hook collapses session_id aliases (spec §5.1)."""

from __future__ import annotations

from hermes_agent.gateway import normalize_params


def test_stored_session_id_folds_to_session_id():
    got = normalize_params({"storedSessionId": "s-1"})
    assert got == {"session_id": "s-1"}
    assert "stored_session_id" not in got


def test_stable_session_id_folds_to_session_id():
    got = normalize_params({"stable_session_id": "s-2"})
    assert got == {"session_id": "s-2"}


def test_runtime_session_id_folds_to_session_id():
    got = normalize_params({"runtimeSessionId": "s-3"})
    assert got == {"session_id": "s-3"}


def test_explicit_session_id_wins_over_alias():
    """The alias is legacy; if a caller already provides session_id,
    the alias must not overwrite it (spec §5.1 precedence).
    """
    got = normalize_params({"session_id": "canonical", "storedSessionId": "legacy"})
    assert got["session_id"] == "canonical"
    assert "stored_session_id" not in got


def test_alias_precedence_stored_wins_over_stable():
    got = normalize_params(
        {"stableSessionId": "from-stable", "storedSessionId": "from-stored"}
    )
    assert got["session_id"] == "from-stored"


def test_empty_alias_does_not_populate_session_id():
    got = normalize_params({"storedSessionId": "", "stableSessionId": None})
    assert "session_id" not in got


def test_alias_folded_at_nested_levels():
    got = normalize_params(
        {
            "outer": {"storedSessionId": "nested-1"},
            "list": [
                {"stableSessionId": "list-a"},
                {"session_id": "list-b", "runtimeSessionId": "loses"},
            ],
        }
    )
    assert got == {
        "outer": {"session_id": "nested-1"},
        "list": [
            {"session_id": "list-a"},
            {"session_id": "list-b"},
        ],
    }


def test_normalize_params_still_snake_cases_other_keys():
    got = normalize_params({"runInfo": {"turnId": "t1"}, "storedSessionId": "s1"})
    assert got == {"session_id": "s1", "run_info": {"turn_id": "t1"}}


def test_normalize_none_and_non_dict_pass_through():
    assert normalize_params(None) == {}
    assert normalize_params("not-a-dict") == "not-a-dict"
