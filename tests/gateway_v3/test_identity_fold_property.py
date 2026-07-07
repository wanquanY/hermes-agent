"""Property-based coverage of the session_id identity fold (spec §5.1 / §J1).

Uses the stdlib ``random`` module (no hypothesis dependency) to fuzz the
alias fold on both request (`normalize_params`) and response
(`fold_response_aliases`) sides.

The invariants proved for every random scenario:

* After request-side fold, only ``session_id`` remains — every alias key is
  dropped.
* When an explicit ``session_id`` is present, it wins even if any alias
  carries a conflicting value (spec §5.1 precedence rule).
* When ``session_id`` is absent, precedence is ``stored_session_id``
  before ``stable_session_id`` before ``runtime_session_id``.
* camelCase alias forms fold identically after snake_case normalization.
* Response-side fold is idempotent (running twice = running once).
* Nested dict / list traversal folds every level.
* Values that are not dict/list are passed through unchanged.
"""

from __future__ import annotations

import random
import string

from hermes_agent.gateway.pipeline import (
    _SESSION_ID_ALIASES,
    _fold_session_id_aliases,
    fold_response_aliases,
    normalize_params,
)


_ROUNDS = 32


def _rand_sid(rng: random.Random) -> str:
    return "s-" + "".join(
        rng.choice(string.ascii_lowercase + string.digits) for _ in range(6)
    )


# ---------------------------------------------------------------------------
# Request-side fold
# ---------------------------------------------------------------------------


def test_fold_removes_every_alias_after_normalization():
    rng = random.Random(42)
    for _ in range(_ROUNDS):
        payload: dict[str, object] = {}
        for alias in _SESSION_ID_ALIASES:
            if rng.random() < 0.5:
                payload[alias] = _rand_sid(rng)
        # Sometimes an explicit session_id, sometimes not.
        if rng.random() < 0.4:
            payload["session_id"] = _rand_sid(rng)
        payload["params_size"] = rng.randint(0, 10)

        normalized = normalize_params(payload)

        # None of the alias forms may survive at this level.
        for alias in _SESSION_ID_ALIASES:
            assert alias not in normalized, f"alias {alias!r} leaked past fold: {normalized}"


def test_explicit_session_id_wins_over_alias():
    rng = random.Random(43)
    for _ in range(_ROUNDS):
        canonical = _rand_sid(rng)
        payload = {
            "session_id": canonical,
            "stored_session_id": _rand_sid(rng),
            "stable_session_id": _rand_sid(rng),
            "runtime_session_id": _rand_sid(rng),
        }
        normalized = normalize_params(payload)
        assert normalized["session_id"] == canonical


def test_alias_precedence_matches_vocabulary_order():
    rng = random.Random(44)
    for _ in range(_ROUNDS):
        # Randomly present a subset of aliases; the first present in vocab
        # order must win.
        aliases_present: dict[str, str] = {}
        for alias in _SESSION_ID_ALIASES:
            if rng.random() < 0.5:
                aliases_present[alias] = _rand_sid(rng)
        if not aliases_present:
            continue
        first_present = next(a for a in _SESSION_ID_ALIASES if a in aliases_present)
        expected = aliases_present[first_present]
        normalized = normalize_params(dict(aliases_present))
        assert normalized["session_id"] == expected, (
            f"vocab order violated: aliases={aliases_present!r}, got={normalized!r}"
        )


def test_camelcase_alias_folds_after_snake_case_normalization():
    """`storedSessionId` on the wire must fold to `session_id` too."""
    rng = random.Random(45)
    for _ in range(_ROUNDS):
        canonical = _rand_sid(rng)
        # Camel form on the wire.
        payload = {"storedSessionId": canonical, "params_size": 1}
        normalized = normalize_params(payload)
        assert normalized["session_id"] == canonical
        assert "stored_session_id" not in normalized
        assert "storedSessionId" not in normalized


# ---------------------------------------------------------------------------
# Response-side fold
# ---------------------------------------------------------------------------


def test_response_fold_is_idempotent():
    rng = random.Random(46)
    for _ in range(_ROUNDS):
        result = {
            "session_id": _rand_sid(rng),
            "stored_session_id": _rand_sid(rng),
            "children": [
                {"stable_session_id": _rand_sid(rng)},
                {"runtime_session_id": _rand_sid(rng), "extra": "data"},
                42,
                "hello",
            ],
        }
        once = fold_response_aliases(dict(result))
        twice = fold_response_aliases(dict(once))
        # Comparable via json-style equality: dicts, ints and strs.
        assert twice == once


def test_response_fold_walks_nested_lists_and_dicts():
    rng = random.Random(47)
    for _ in range(_ROUNDS):
        canonical = _rand_sid(rng)
        payload = {
            "outer": {
                "children": [
                    {"stored_session_id": canonical, "role": "leader"},
                    {"stable_session_id": _rand_sid(rng)},
                ]
            }
        }
        folded = fold_response_aliases(payload)
        first = folded["outer"]["children"][0]
        second = folded["outer"]["children"][1]
        assert first["session_id"] == canonical
        assert "stored_session_id" not in first
        assert "session_id" in second
        assert "stable_session_id" not in second


def test_response_fold_passthrough_for_scalar_types():
    """Non-dict / non-list values must not be touched."""
    rng = random.Random(48)
    samples = [None, 0, 3.14, "hello", True, False]
    for _ in range(_ROUNDS):
        v = rng.choice(samples)
        assert fold_response_aliases(v) == v


# ---------------------------------------------------------------------------
# Round-trip request→response symmetry
# ---------------------------------------------------------------------------


def test_request_and_response_fold_agree_on_canonical_output():
    """The wire boundary must present ``session_id`` and only
    ``session_id`` — regardless of which side did the folding.
    """
    rng = random.Random(49)
    for _ in range(_ROUNDS):
        canonical = _rand_sid(rng)
        wire_shape = {"stored_session_id": canonical, "payload": {"n": 1}}
        req_folded = normalize_params(wire_shape)
        resp_folded = fold_response_aliases(dict(wire_shape))
        assert req_folded["session_id"] == canonical
        assert resp_folded["session_id"] == canonical
        for alias in _SESSION_ID_ALIASES:
            assert alias not in req_folded
            assert alias not in resp_folded
