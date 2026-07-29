"""TUI interim assistant callback configuration and event contracts."""

from unittest.mock import patch


def test_load_interim_assistant_messages_defaults_true():
    from tui_gateway.server import _load_interim_assistant_messages

    with patch("tui_gateway.server._load_cfg", return_value={}):
        assert _load_interim_assistant_messages() is True


def test_load_interim_assistant_messages_supports_boolish_values():
    from tui_gateway.server import _load_interim_assistant_messages

    with patch(
        "tui_gateway.server._load_cfg",
        return_value={"display": {"interim_assistant_messages": "off"}},
    ):
        assert _load_interim_assistant_messages() is False
    with patch(
        "tui_gateway.server._load_cfg",
        return_value={"display": {"interim_assistant_messages": "on"}},
    ):
        assert _load_interim_assistant_messages() is True


def test_agent_callbacks_emit_interim_message_when_enabled():
    from tui_gateway.server import _agent_cbs

    emitted: list[tuple] = []

    with (
        patch("tui_gateway.server._load_cfg", return_value={}),
        patch(
            "tui_gateway.server._emit",
            side_effect=lambda event, sid, payload=None: emitted.append(
                (event, sid, payload)
            ),
        ),
    ):
        callbacks = _agent_cbs("session-1")
        callbacks["interim_assistant_callback"](
            "commentary",
            already_streamed=True,
        )

    assert emitted == [
        (
            "message.interim",
            "session-1",
            {"text": "commentary", "already_streamed": True},
        )
    ]


def test_agent_callbacks_omit_interim_message_when_disabled():
    from tui_gateway.server import _agent_cbs

    with patch(
        "tui_gateway.server._load_cfg",
        return_value={"display": {"interim_assistant_messages": False}},
    ):
        assert "interim_assistant_callback" not in _agent_cbs("session-1")


def test_agent_callbacks_publish_internal_interim_visibility():
    from tui_gateway.server import _agent_cbs

    emitted: list[tuple] = []

    with (
        patch("tui_gateway.server._load_cfg", return_value={}),
        patch(
            "tui_gateway.server._emit",
            side_effect=lambda event, sid, payload=None: emitted.append(
                (event, sid, payload)
            ),
        ),
    ):
        callbacks = _agent_cbs("session-1")
        callbacks["interim_assistant_callback"](
            "commentary",
            already_streamed=True,
            transcript_visibility="internal",
            synthetic_kind="repeated_interim_commentary",
        )

    assert emitted == [
        (
            "message.interim",
            "session-1",
            {
                "text": "commentary",
                "already_streamed": True,
                "transcript_visibility": "internal",
                "synthetic_kind": "repeated_interim_commentary",
            },
        )
    ]


def test_interim_callback_preserves_prompt_segment_identity_for_retraction():
    from tui_gateway.services.interim_message_events import (
        create_interim_assistant_callback,
    )

    emitted: list[tuple] = []
    callback = create_interim_assistant_callback(
        emit=lambda event, sid, payload: emitted.append((event, sid, payload)),
        session_id="session-1",
        identity_payload=lambda _already_streamed: {
            "client_message_id": "turn-1:assistant-segment:2",
            "message_seq_in_run": 3,
        },
    )

    callback(
        "repeated commentary",
        already_streamed=True,
        transcript_visibility="internal",
        synthetic_kind="repeated_interim_commentary",
    )

    assert emitted == [
        (
            "message.interim",
            "session-1",
            {
                "text": "repeated commentary",
                "already_streamed": True,
                "client_message_id": "turn-1:assistant-segment:2",
                "message_seq_in_run": 3,
                "transcript_visibility": "internal",
                "synthetic_kind": "repeated_interim_commentary",
            },
        )
    ]
