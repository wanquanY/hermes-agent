from __future__ import annotations

import threading
from types import SimpleNamespace

from agent.stream_single_writer import claim_stream_writer, stream_writer_is_current
from agent.stream_writer_fence import StreamWriterAgentMixin


class _Agent(StreamWriterAgentMixin):
    pass


def test_new_claim_supersedes_an_older_thread_writer() -> None:
    agent = _Agent()
    claimed = threading.Event()
    replacement_claimed = threading.Event()
    result: dict[str, bool] = {}

    def older_writer() -> None:
        token = agent._claim_stream_writer()
        claimed.set()
        replacement_claimed.wait(timeout=2)
        result["current"] = agent._stream_writer_is_current(token)
        result["superseded"] = agent._stream_writer_superseded()

    thread = threading.Thread(target=older_writer)
    thread.start()
    assert claimed.wait(timeout=2)
    agent._claim_stream_writer()
    replacement_claimed.set()
    thread.join(timeout=2)

    assert result == {"current": False, "superseded": True}


def test_best_effort_helpers_do_not_require_agent_methods() -> None:
    agent = SimpleNamespace()

    token = claim_stream_writer(agent)

    assert token == 0
    assert stream_writer_is_current(agent, token) is True
