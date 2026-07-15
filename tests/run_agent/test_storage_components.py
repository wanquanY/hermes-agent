"""AIAgent persistence checks use explicit storage components."""

from __future__ import annotations

from types import SimpleNamespace

from run_agent import AIAgent


def test_session_db_prefix_match_reads_messages_component():
    class _Messages:
        def list(self, session_id):
            assert session_id == "session-1"
            return [{"role": "user", "content": "hello"}]

    agent = AIAgent.__new__(AIAgent)
    agent._session_db = SimpleNamespace(messages=_Messages())

    assert agent._session_db_matches_message_prefix(
        "session-1",
        [{"role": "user", "content": "hello"}],
        1,
    )
