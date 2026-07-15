"""Regression tests for #15165 (CLI sibling site) — CLI exit cleanup must
forward the agent's conversation transcript to ``shutdown_memory_provider``
so memory providers' ``on_session_end`` hooks see the real messages.

Before the fix, ``_run_cleanup`` called
``shutdown_memory_provider(getattr(agent, 'conversation_history', None) or [])``.
``AIAgent`` has no ``conversation_history`` attribute — so the ``or []``
branch always fired and providers got an empty list on CLI exit. This
mirrors the gateway bug fixed in the same commit (hermes_gateway/runner.py uses
``_session_messages``, which IS set on ``AIAgent``).

The fix reads ``_session_messages`` (same attribute the gateway path uses)
with an ``isinstance(..., list)`` guard so MagicMock-based agents in
other tests keep their existing no-arg behaviour.
"""

from __future__ import annotations

import threading
from unittest.mock import MagicMock, patch

from agent.turn_message_buffer import TurnMessageBuffer


@patch("hermes_cli.plugins.invoke_hook")
def test_cleanup_forwards_session_messages(mock_invoke_hook):
    """_run_cleanup forwards a populated ``_session_messages`` list."""
    import cli as cli_mod

    transcript = [
        {"role": "user", "content": "remember my dog is named Biscuit"},
        {"role": "assistant", "content": "Got it — Biscuit."},
    ]

    agent = MagicMock()
    agent.session_id = "cli-session-id"
    agent._session_messages = transcript

    cli_mod._active_agent_ref = agent
    cli_mod._cleanup_done = False
    try:
        cli_mod._run_cleanup()
    finally:
        cli_mod._active_agent_ref = None
        cli_mod._cleanup_done = False

    agent.shutdown_memory_provider.assert_called_once_with(transcript)


@patch("hermes_cli.plugins.invoke_hook")
def test_cleanup_empty_list_still_forwarded(mock_invoke_hook):
    """An agent that initialised but ran no turns has an empty list.
    Forwarding it (rather than falling through) matches the gateway-side
    behaviour and is explicit to providers."""
    import cli as cli_mod

    agent = MagicMock()
    agent.session_id = "cli-session-id"
    agent._session_messages = []

    cli_mod._active_agent_ref = agent
    cli_mod._cleanup_done = False
    try:
        cli_mod._run_cleanup()
    finally:
        cli_mod._active_agent_ref = None
        cli_mod._cleanup_done = False

    agent.shutdown_memory_provider.assert_called_once_with([])


@patch("hermes_cli.plugins.invoke_hook")
def test_cleanup_non_list_attribute_falls_back_to_no_arg(mock_invoke_hook):
    """A MagicMock agent auto-synthesises ``_session_messages`` as a
    nested MagicMock. ``isinstance(mock, list)`` is False, so we fall
    back to the no-arg path rather than passing a garbage value to
    providers expecting ``List[Dict]``.  This keeps existing CLI test
    suites that use bare ``MagicMock()`` agents green."""
    import cli as cli_mod

    agent = MagicMock()
    agent.session_id = "cli-session-id"
    # No explicit _session_messages — MagicMock synthesises one on access.

    cli_mod._active_agent_ref = agent
    cli_mod._cleanup_done = False
    try:
        cli_mod._run_cleanup()
    finally:
        cli_mod._active_agent_ref = None
        cli_mod._cleanup_done = False

    agent.shutdown_memory_provider.assert_called_once_with()


@patch("hermes_cli.plugins.invoke_hook")
def test_cleanup_provider_exception_is_swallowed(mock_invoke_hook):
    """A raising ``shutdown_memory_provider`` must not crash CLI exit."""
    import cli as cli_mod

    agent = MagicMock()
    agent.session_id = "cli-session-id"
    agent._session_messages = [{"role": "user", "content": "x"}]
    agent.shutdown_memory_provider.side_effect = RuntimeError("boom")

    cli_mod._active_agent_ref = agent
    cli_mod._cleanup_done = False
    try:
        cli_mod._run_cleanup()  # must not raise
    finally:
        cli_mod._active_agent_ref = None
        cli_mod._cleanup_done = False

    agent.shutdown_memory_provider.assert_called_once()


def test_close_snapshot_persists_pending_cli_input_once():
    """Accepted input is durable even when close wins before worker start."""
    import cli as cli_mod

    pending = {"role": "user", "content": "accepted input"}

    class Agent:
        def __init__(self):
            self._session_db = object()
            self._session_messages = [
                {"role": "assistant", "content": "previous"}
            ]
            self._pending_cli_user_message = pending
            self._session_persist_lock = threading.RLock()
            self._cached_system_prompt = "prompt"
            self.persisted = []
            self.ensure_calls = 0

        def _ensure_db_session(self):
            self.ensure_calls += 1

        def _persist_session(self, messages, conversation_history=None):
            self.persisted.append(
                (list(messages), messages.persist_from_index, conversation_history)
            )

    agent = Agent()

    assert cli_mod._persist_active_agent_snapshot(agent) is True

    persisted, boundary, history = agent.persisted[0]
    assert agent.ensure_calls == 1
    assert persisted == [
        {"role": "assistant", "content": "previous"},
        pending,
    ]
    assert persisted.count(pending) == 1
    assert boundary == 1
    assert history == agent._session_messages


def test_close_snapshot_fallback_flushes_pending_already_in_cli_history():
    """The history fallback keeps the pending row on the writable side."""
    import cli as cli_mod

    pending = {"role": "user", "content": "accepted input"}
    history = [{"role": "assistant", "content": "previous"}, pending]

    class Agent:
        def __init__(self):
            self._session_db = object()
            self._pending_cli_user_message = pending
            self._session_persist_lock = threading.RLock()
            self._cached_system_prompt = "prompt"
            self.persisted = None

        def _ensure_db_session(self):
            pass

        def _persist_session(self, messages, conversation_history=None):
            assert isinstance(messages, TurnMessageBuffer)
            self.persisted = (list(messages), messages.persist_from_index)

    agent = Agent()

    assert cli_mod._persist_active_agent_snapshot(agent, history) is True
    assert agent.persisted == (history, 1)
