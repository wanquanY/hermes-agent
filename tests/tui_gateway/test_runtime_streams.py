from tui_gateway.services import runtime_streams


def setup_function():
    runtime_streams.reset_for_tests()


def _event(text: str, *, seq: int, offset: int, event_type: str = "message.delta"):
    return {
        "type": event_type,
        "conversation_session_id": "session-1",
        "run_id": "run-1",
        "turn_id": "turn-1",
        "runtime_scope_key": "profile:default",
        "seq": seq,
        "payload": {
            "mode": "append",
            "delta": text,
            "text": text,
            "offset": offset,
            "client_message_id": "client-1",
        },
    }


def test_reconnect_snapshot_reconstructs_utf16_offset_stream():
    runtime_streams.observe(_event("A😀", seq=10, offset=0))
    runtime_streams.observe(_event("中", seq=11, offset=3))

    snapshots = runtime_streams.replay_snapshots("session-1")

    assert len(snapshots) == 1
    assert snapshots[0]["transient"] is True
    assert "seq" not in snapshots[0]
    assert snapshots[0]["runtime_source_seq"] == 11
    assert snapshots[0]["payload"]["mode"] == "snapshot"
    assert snapshots[0]["payload"]["text"] == "A😀中"


def test_checkpoint_is_incremental_append_and_marks_lane_clean():
    runtime_streams.observe(_event("你", seq=1, offset=0))
    runtime_streams.observe(_event("好", seq=2, offset=1))

    pending = runtime_streams.pending_checkpoints("session-1", run_id="run-1")

    assert len(pending) == 1
    checkpoint = pending[0]
    assert checkpoint.frame.get("seq") is None
    assert checkpoint.frame["payload"]["stream_checkpoint"] is True
    assert checkpoint.frame["payload"]["mode"] == "append"
    assert checkpoint.frame["payload"]["offset"] == 0
    assert checkpoint.frame["payload"]["text"] == "你好"
    runtime_streams.mark_checkpointed([checkpoint])
    assert runtime_streams.pending_checkpoints("session-1", run_id="run-1") == []

    runtime_streams.observe(_event("😀", seq=3, offset=2))
    second = runtime_streams.pending_checkpoints("session-1", run_id="run-1")[0]
    assert second.frame["payload"]["mode"] == "append"
    assert second.frame["payload"]["offset"] == 2
    assert second.frame["payload"]["text"] == "😀"


def test_observe_assigns_utf16_offsets_to_offsetless_live_fragments():
    first = _event("A😀", seq=10, offset=0)
    first["payload"].pop("offset")
    first["payload"].pop("mode")
    second = _event("中", seq=11, offset=0)
    second["payload"].pop("offset")
    second["payload"].pop("mode")

    runtime_streams.observe(first)
    runtime_streams.observe(second)

    assert first["payload"]["offset"] == 0
    assert first["payload"]["mode"] == "append"
    assert second["payload"]["offset"] == 3
    assert second["payload"]["mode"] == "append"
    snapshot = runtime_streams.replay_snapshots("session-1")[0]
    assert snapshot["payload"]["text"] == "A😀中"
