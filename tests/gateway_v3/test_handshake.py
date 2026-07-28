"""Phase G — handshake frame (spec §11)."""

from __future__ import annotations

from hermes_agent.gateway import HANDSHAKE_FRAME_TYPE, build_handshake_frame


def test_handshake_frame_shape():
    frame = build_handshake_frame()
    assert frame["type"] == HANDSHAKE_FRAME_TYPE
    assert frame["contractVersion"] == "3.1"
    assert set(frame["capabilities"].keys()) == {"cursor", "history", "toolEvents"}
    assert frame["deprecations"] == []


def test_handshake_frame_cursor_full_4_arm():
    frame = build_handshake_frame()
    cursor = frame["capabilities"]["cursor"]
    assert set(cursor.keys()) == {"afterSeq", "afterId", "beforeSeq", "beforeId"}
    assert all(v is True for v in cursor.values())


def test_handshake_frame_no_internal_leakage():
    frame = build_handshake_frame()
    caps = frame["capabilities"]
    for banned in (
        "interaction.persistent",
        "runStateMachine.singleEntrypoint",
        "since",
        "runtimeSourceSeq.deprecated",
    ):
        assert banned not in caps
        assert banned not in frame
