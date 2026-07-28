"""Phase G — response-side identity alias fold (spec §5.1 symmetric channel).

Handlers may emit legacy ``stored_session_id`` / ``stable_session_id`` /
``runtime_session_id`` keys because they compose data from legacy sources
(hermes_state_branch.py, event dicts, frame payloads, ...). The dispatch
pipeline strips those aliases and promotes them to the canonical
``session_id`` before returning the wire response. The frontend never sees
the legacy name.
"""

from __future__ import annotations

from typing import Any

from hermes_agent.gateway import (
    AllowAllResolver,
    MethodRegistry,
    dispatch,
    fold_response_aliases,
    requires_permission,
)
from hermes_agent.gateway.pipeline import DispatchContext


def test_fold_response_promotes_stored_session_id_when_canonical_missing():
    got = fold_response_aliases({"stored_session_id": "s-1", "other": 42})
    assert got == {"session_id": "s-1", "other": 42}


def test_fold_response_prefers_canonical_over_alias():
    got = fold_response_aliases(
        {"session_id": "canonical", "stored_session_id": "legacy"}
    )
    assert got == {"session_id": "canonical"}


def test_fold_response_alias_precedence_stored_over_stable_over_runtime():
    got = fold_response_aliases(
        {
            "runtime_session_id": "from-runtime",
            "stable_session_id": "from-stable",
            "stored_session_id": "from-stored",
        }
    )
    assert got == {"session_id": "from-stored"}


def test_fold_response_empty_alias_does_not_pollute_output():
    got = fold_response_aliases({"stored_session_id": "", "value": 1})
    assert got == {"value": 1}


def test_fold_response_recurses_into_nested_dicts():
    got = fold_response_aliases(
        {"inner": {"storedSessionId": "s-1"}}  # note: this key is already snake or camel
    )
    # camelCase not touched here — the request side normalizes those; this
    # helper focuses on snake_case alias vocabulary.
    assert got == {"inner": {"storedSessionId": "s-1"}}

    got_snake = fold_response_aliases({"inner": {"stored_session_id": "s-1"}})
    assert got_snake == {"inner": {"session_id": "s-1"}}


def test_fold_response_recurses_into_lists():
    got = fold_response_aliases(
        {
            "sessions": [
                {"stored_session_id": "a"},
                {"session_id": "b", "runtime_session_id": "override"},
            ]
        }
    )
    assert got == {
        "sessions": [
            {"session_id": "a"},
            {"session_id": "b"},
        ]
    }


def test_fold_response_preserves_non_container_values():
    assert fold_response_aliases(None) is None
    assert fold_response_aliases("string") == "string"
    assert fold_response_aliases(42) == 42
    assert fold_response_aliases(True) is True


# ---------------------------------------------------------------------------
# End-to-end dispatch: handler leaks legacy alias → wire response is clean
# ---------------------------------------------------------------------------


@requires_permission("test.read", read_only=True)
def _legacy_leaky_handler(params: dict[str, Any], ctx: DispatchContext) -> dict[str, Any]:
    """Simulates a legacy code path (e.g. hermes_state_branch.py) that still
    emits ``stored_session_id`` in its return value.
    """
    return {
        "stored_session_id": "s-1",
        "parent_session_id": "",
        "title": "Legacy",
    }


def test_dispatch_scrubs_legacy_alias_from_wire_response():
    reg = MethodRegistry()
    reg.register("legacy.leaky", _legacy_leaky_handler)
    resp = dispatch(
        reg,
        {"id": "req", "method": "legacy.leaky", "params": {}},
        resolver=AllowAllResolver(),
    )
    result = resp["result"]
    assert result["session_id"] == "s-1"
    assert "stored_session_id" not in result
    assert result["parent_session_id"] == ""
    assert result["title"] == "Legacy"


@requires_permission("test.read", read_only=True)
def _handler_emits_nested_alias_list(
    params: dict[str, Any], ctx: DispatchContext
) -> dict[str, Any]:
    return {
        "sessions": [
            {"stored_session_id": "s1", "title": "one"},
            {"stored_session_id": "s2", "title": "two"},
        ]
    }


def test_dispatch_scrubs_alias_from_nested_list_items():
    reg = MethodRegistry()
    reg.register("legacy.list", _handler_emits_nested_alias_list)
    resp = dispatch(
        reg,
        {"id": "req", "method": "legacy.list", "params": {}},
        resolver=AllowAllResolver(),
    )
    result = resp["result"]
    for entry in result["sessions"]:
        assert "stored_session_id" not in entry
        assert "session_id" in entry


@requires_permission("test.read", read_only=True)
def _handler_returns_only_canonical(
    params: dict[str, Any], ctx: DispatchContext
) -> dict[str, Any]:
    return {"session_id": "canonical-1", "answer": 42}


def test_dispatch_does_not_touch_canonical_only_result():
    reg = MethodRegistry()
    reg.register("clean.method", _handler_returns_only_canonical)
    resp = dispatch(
        reg,
        {"id": "req", "method": "clean.method", "params": {}},
        resolver=AllowAllResolver(),
    )
    assert resp["result"] == {"session_id": "canonical-1", "answer": 42}
