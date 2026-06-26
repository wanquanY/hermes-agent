import json

import pytest

from hermes_team_mission.domain.run_context import RunContext


def _payload(**overrides):
    payload = {
        "conversation_session_id": "team-session-1",
        "participant_id": "member:builder",
        "activity_id": "member_chat",
        "activity_kind": "member_chat",
        "execution_scope_key": "member-chat:conversation-1:builder",
        "control_home": "/tmp/hermes-control",
        "execution_home": "/tmp/hermes-execution",
    }
    payload.update(overrides)
    return payload


def test_run_context_construct_and_serialise():
    context = RunContext(**_payload())

    payload = context.to_payload()
    restored = RunContext.from_payload(payload)

    assert payload == _payload()
    assert restored == context


def test_run_context_required_fields_raise_when_empty():
    for field_name in _payload():
        payload = _payload(**{field_name: ""})
        with pytest.raises(ValueError, match=field_name):
            RunContext(**payload)


def test_run_context_activity_kind_validates():
    with pytest.raises(ValueError, match="activity_kind"):
        RunContext(**_payload(activity_kind="foo"))

    for activity_kind in ("chat", "member_chat", "mission"):
        context = RunContext(**_payload(activity_kind=activity_kind))
        assert context.activity_kind == activity_kind


def test_run_context_from_payload_accepts_json_string():
    payload = _payload(activity_kind="mission", activity_id="mission-1")

    context = RunContext.from_payload(json.dumps(payload))

    assert context.to_payload() == payload


def test_run_context_from_payload_missing_fields_raise():
    payload = _payload()
    payload.pop("participant_id")

    with pytest.raises(ValueError, match="participant_id"):
        RunContext.from_payload(payload)
