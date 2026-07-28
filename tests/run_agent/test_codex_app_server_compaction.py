from __future__ import annotations

from types import SimpleNamespace

from agent.codex_compaction import compact_codex_app_server_context
from agent.codex_runtime import _record_codex_app_server_compaction
from agent.transports.codex_app_server_session import TurnResult


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


class _Session:
    def __init__(self, result: TurnResult) -> None:
        self.result = result
        self.calls = 0
        self.closed = False

    def compact_thread(self) -> TurnResult:
        self.calls += 1
        return self.result

    def close(self) -> None:
        self.closed = True


def test_compaction_coordinator_preserves_transcript_and_commits_once(monkeypatch):
    session = _Session(
        TurnResult(
            compacted=True,
            thread_id="thread-1",
            turn_id="compact-1",
            token_usage_last={"totalTokens": 10},
        )
    )
    events: list[str] = []
    monkeypatch.setattr(
        "agent.codex_runtime._record_codex_app_server_compaction",
        lambda *_args, **_kwargs: events.append("boundary") or True,
    )
    monkeypatch.setattr(
        "agent.codex_runtime._record_codex_app_server_usage",
        lambda *_args, **_kwargs: events.append("usage") or {},
    )
    agent = SimpleNamespace(
        _codex_session=session,
        _cached_system_prompt="prompt",
        session_id="session-1",
        _emit_warning=lambda _message: None,
    )
    messages = [{"role": "user", "content": "keep me"}]

    returned, prompt = compact_codex_app_server_context(
        agent, messages, "system", approx_tokens=50_000
    )

    assert returned is messages
    assert prompt == "prompt"
    assert session.calls == 1
    assert events == ["boundary", "usage"]


def test_compaction_coordinator_does_not_commit_interrupted_result(monkeypatch):
    session = _Session(
        TurnResult(
            compacted=True,
            interrupted=True,
            error="compact turn interrupted",
        )
    )
    committed: list[str] = []
    monkeypatch.setattr(
        "agent.codex_runtime._record_codex_app_server_compaction",
        lambda *_args, **_kwargs: committed.append("boundary"),
    )
    warnings: list[str] = []
    agent = SimpleNamespace(
        _codex_session=session,
        _cached_system_prompt="prompt",
        session_id="session-1",
        _emit_warning=warnings.append,
    )

    compact_codex_app_server_context(agent, [], "system")

    assert committed == []
    assert warnings and "interrupted" in warnings[0]
