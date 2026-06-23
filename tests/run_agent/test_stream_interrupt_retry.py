"""Tests that /stop interrupts streaming retry loops immediately.

When the agent is interrupted during a streaming API call, the outer poll
loop closes the HTTP connection.  The inner `_call()` thread sees a
connection error and enters its retry loop.  Before this fix, the retry
loop would open a FRESH connection without checking `_interrupt_requested`,
making /stop take multiple retry cycles × read-timeout to actually stop
(510+ seconds observed on slow ollama-cloud providers).

The fix adds an `_interrupt_requested` check at the top of the retry loop
so the agent exits immediately instead of retrying.
"""
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest


def _make_agent(**kwargs):
    """Create a minimal AIAgent for streaming tests."""
    from run_agent import AIAgent

    defaults = dict(
        api_key="test-key",
        base_url="https://example.com/v1",
        model="test/model",
        quiet_mode=True,
        skip_context_files=True,
        skip_memory=True,
    )
    defaults.update(kwargs)
    agent = AIAgent(**defaults)
    agent.api_mode = "chat_completions"
    return agent


class TestStreamInterruptBeforeRetry:
    """Verify _interrupt_requested is checked before each streaming retry."""

    @pytest.mark.filterwarnings(
        "ignore::pytest.PytestUnhandledThreadExceptionWarning"
    )
    @patch("run_agent.AIAgent._create_request_openai_client")
    @patch("run_agent.AIAgent._close_request_openai_client")
    def test_interrupt_prevents_stream_retry(self, mock_close, mock_create):
        """When _interrupt_requested is set during a transient stream error,
        the retry loop must NOT retry — it should raise InterruptedError
        immediately instead of opening a fresh connection."""
        import httpx

        attempt_count = [0]

        def fail_once_then_interrupt(*args, **kwargs):
            attempt_count[0] += 1
            if attempt_count[0] == 1:
                # First attempt: simulate normal failure, then set interrupt
                # (as if /stop arrived while the retry loop processes the error)
                agent._interrupt_requested = True
                raise httpx.ConnectError("connection reset by /stop")
            # Should never reach here — the interrupt check should fire first
            raise httpx.ConnectError("unexpected retry — interrupt not checked!")

        mock_client = MagicMock()
        mock_client.chat.completions.create.side_effect = fail_once_then_interrupt
        mock_create.return_value = mock_client

        agent = _make_agent()
        agent._interrupt_requested = False

        with pytest.raises(InterruptedError, match="interrupted"):
            agent._interruptible_streaming_api_call({})

        # Only 1 attempt should have been made — the interrupt should prevent retry
        assert attempt_count[0] == 1, (
            f"Expected 1 attempt but got {attempt_count[0]}. "
            "The retry loop retried despite _interrupt_requested being set."
        )

    @pytest.mark.filterwarnings(
        "ignore::pytest.PytestUnhandledThreadExceptionWarning"
    )
    @patch("run_agent.AIAgent._create_request_openai_client")
    @patch("run_agent.AIAgent._close_request_openai_client")
    def test_interrupt_before_first_attempt(self, mock_close, mock_create):
        """If _interrupt_requested is already set when the streaming call
        starts, it should exit immediately without making any API call."""
        mock_client = MagicMock()
        mock_create.return_value = mock_client

        agent = _make_agent()
        agent._interrupt_requested = True  # Pre-set before call

        with pytest.raises(InterruptedError, match="interrupted"):
            agent._interruptible_streaming_api_call({})

        # No API call should have been made at all
        assert mock_client.chat.completions.create.call_count == 0

    @patch("run_agent.AIAgent._create_request_openai_client")
    @patch("run_agent.AIAgent._close_request_openai_client")
    def test_normal_retry_still_works_without_interrupt(self, mock_close, mock_create):
        """Without an interrupt, transient errors should still retry normally."""
        import httpx

        attempts = [0]

        def fail_twice_then_succeed(*args, **kwargs):
            attempts[0] += 1
            if attempts[0] <= 2:
                raise httpx.ConnectError("transient failure")
            # Third attempt succeeds
            chunks = [
                SimpleNamespace(
                    choices=[
                        SimpleNamespace(
                            index=0,
                            delta=SimpleNamespace(
                                content="ok",
                                tool_calls=None,
                                reasoning_content=None,
                                reasoning=None,
                            ),
                            finish_reason=None,
                        )
                    ],
                    model="test/model",
                    usage=None,
                ),
                SimpleNamespace(
                    choices=[
                        SimpleNamespace(
                            index=0,
                            delta=SimpleNamespace(
                                content=None,
                                tool_calls=None,
                                reasoning_content=None,
                                reasoning=None,
                            ),
                            finish_reason="stop",
                        )
                    ],
                    model="test/model",
                    usage=None,
                ),
            ]
            stream = MagicMock()
            stream.__iter__ = MagicMock(return_value=iter(chunks))
            stream.response = MagicMock()
            stream.response.headers = {}
            return stream

        mock_client = MagicMock()
        mock_client.chat.completions.create.side_effect = fail_twice_then_succeed
        mock_create.return_value = mock_client

        agent = _make_agent()
        agent._interrupt_requested = False

        # Should succeed on the third attempt
        result = agent._interruptible_streaming_api_call({})
        assert result is not None
        assert attempts[0] == 3


class TestStickyOuterInterruptSurvivesClearInterrupt:
    """Regression test for the production race where conversation_loop's
    ``clear_interrupt()`` lands between the outer polling thread's
    interrupt detection and the inner worker thread's retry-loop check.

    Production timeline observed in the agent.log when the bug fires:

      T+0      ``agent.interrupt()`` sets ``_interrupt_requested = True``
      T+0.84   outer polling thread (0.3s tick): closes the request
               client (``stream_interrupt_abort``) and raises
               ``InterruptedError`` out of the helper
      T+0.84   conversation_loop catches → marks interrupted → drops
               through to ``clear_interrupt()`` at end of the turn,
               resetting ``_interrupt_requested = False``
      T+0.85   inner Thread (daemon, orphaned) catches the
               connection-closed error, classifies it as
               ``_is_conn_err = True``, hits the retry block,
               ``continue`` → top of loop
      T+0.85   inner Thread checks ``_interrupt_requested`` — reads
               False (just cleared) → fires a fresh
               ``chat_completion_stream_request`` that completes
               normally over the next ~19 seconds, but the run
               lifecycle never sees it (no ``runs`` row, no
               ``terminal-event-persisted``, FE has no run_id to
               cancel against on the second /stop click).

    The fix is a sticky local flag on the ``result`` closure dict —
    set once by the outer thread, never cleared — that the inner
    retry check reads alongside ``_interrupt_requested``.
    """

    @pytest.mark.filterwarnings(
        "ignore::pytest.PytestUnhandledThreadExceptionWarning"
    )
    @patch("run_agent.AIAgent._create_request_openai_client")
    @patch("run_agent.AIAgent._close_request_openai_client")
    def test_sticky_outer_interrupt_blocks_retry_even_after_clear(self, mock_close, mock_create):
        """The bug repro: the inner mock raises a transient connection error
        while ``_interrupt_requested`` is briefly True for the polling
        thread to observe — then immediately reset to False (simulating
        ``conversation_loop.clear_interrupt()`` racing the inner retry
        check). With the sticky flag fix, the inner thread sees the
        ``_outer_interrupted`` mark and raises ``InterruptedError`` even
        though ``_interrupt_requested`` reads False at check time. Before
        the fix, a SECOND mock call would happen — exactly the orphan
        retry that produces "agent kept responding to a recalled message"
        in production."""
        import time as _time

        import httpx

        attempt_count = [0]

        # Outer holder so the closure can flip the agent's interrupt state.
        # We can't capture the agent inside the side_effect before it's
        # constructed, so go through a mutable cell.
        agent_holder = {}

        def fail_with_outer_interrupt_then_clear(*args, **kwargs):
            attempt_count[0] += 1
            if attempt_count[0] == 1:
                # Set the interrupt flag and wait long enough for the
                # outer polling tick (0.3s) to observe it and set the
                # sticky ``_outer_interrupted`` mark on the result dict.
                agent_holder["agent"]._interrupt_requested = True
                _time.sleep(0.5)
                # Now simulate ``conversation_loop.clear_interrupt()``
                # winning the race. From this point on, the live flag
                # reads False — only the sticky flag should keep the
                # retry from firing.
                agent_holder["agent"]._interrupt_requested = False
                raise httpx.ConnectError("connection reset by /stop")
            # If we ever land here, the fix failed: the retry happened
            # despite the outer interrupt.
            raise AssertionError(
                f"Inner retry fired on attempt {attempt_count[0]} — "
                "_outer_interrupted sticky flag did not block the retry "
                "(the production orphan-stream bug)."
            )

        mock_client = MagicMock()
        mock_client.chat.completions.create.side_effect = fail_with_outer_interrupt_then_clear
        mock_create.return_value = mock_client

        agent = _make_agent()
        agent_holder["agent"] = agent
        agent._interrupt_requested = False

        with pytest.raises(InterruptedError, match="interrupted"):
            agent._interruptible_streaming_api_call({})

        assert attempt_count[0] == 1, (
            f"Expected exactly 1 attempt but got {attempt_count[0]}. "
            "The sticky _outer_interrupted flag did not survive the "
            "_interrupt_requested clear() — the bug regressed."
        )
