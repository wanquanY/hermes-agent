import re

import pytest

from hermes_agent.domain.safe_identifiers import (
    safe_filename_component,
    validate_external_session_id,
    validate_path_component,
)


def test_external_session_id_accepts_portable_contract():
    assert validate_external_session_id("api-session_1.2") == "api-session_1.2"


@pytest.mark.parametrize("value", ("../escape", "/absolute", "..\\windows", ".", "..", "a:b", "a/b"))
def test_external_session_id_rejects_path_and_platform_unsafe_values(value):
    with pytest.raises(ValueError):
        validate_external_session_id(value)


def test_filename_encoding_is_single_component_stable_and_collision_resistant():
    first = safe_filename_component("../tenant/a")
    second = safe_filename_component("../tenant/b")
    assert first != second
    assert safe_filename_component("safe-id") == "safe-id"
    for encoded in (first, second):
        assert re.fullmatch(r"[A-Za-z0-9._-]+", encoded)
        assert "/" not in encoded and "\\" not in encoded and ".." not in encoded


def test_snapshot_component_rejects_traversal():
    with pytest.raises(ValueError):
        validate_path_component("../snapshot", label="snapshot ID")

