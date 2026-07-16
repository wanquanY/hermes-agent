from __future__ import annotations

from types import SimpleNamespace

from agent.codex_runtime import _record_codex_app_server_compaction


class _Compressor:
    def __init__(self) -> None:
        self.compression_count = 0
        self.boundaries: list[bool] = []

    def record_completed_compaction(self, *, used_fallback: bool = False) -> None:
        self.boundaries.append(used_fallback)


def test_codex_native_compaction_uses_shared_completed_boundary_seam():
    compressor = _Compressor()
    events: list[tuple[str, dict]] = []
    agent = SimpleNamespace(
        context_compressor=compressor,
        session_id="session-1",
        platform="tui",
        event_callback=lambda name, payload: events.append((name, payload)),
    )
    turn = SimpleNamespace(
        compacted=True,
        thread_id="thread-1",
        turn_id="turn-1",
    )

    assert _record_codex_app_server_compaction(agent, turn) is True
    assert compressor.compression_count == 1
    assert compressor.boundaries == [False]
    assert events == [
        (
            "session:compress",
            {
                "platform": "tui",
                "session_id": "session-1",
                "old_session_id": "",
                "in_place": False,
                "compression_count": 1,
                "runtime": "codex_app_server",
                "thread_id": "thread-1",
                "turn_id": "turn-1",
            },
        )
    ]


def test_codex_turn_without_compaction_does_not_touch_compressor():
    compressor = _Compressor()
    agent = SimpleNamespace(context_compressor=compressor)
    turn = SimpleNamespace(compacted=False)

    assert _record_codex_app_server_compaction(agent, turn) is False
    assert compressor.compression_count == 0
    assert compressor.boundaries == []
