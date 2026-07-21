"""Request-client transport ownership regression tests."""

from __future__ import annotations

import threading
from types import SimpleNamespace
from unittest.mock import MagicMock

import httpx
import pytest

from agent.chat_completion_helpers import interruptible_api_call


@pytest.mark.parametrize("kind", ["openai", "anthropic_messages"])
def test_interrupt_aborts_on_poll_thread_and_closes_on_owner_thread(kind: str):
    """A stranger thread may abort sockets, but only the worker may close."""
    agent = MagicMock()
    agent.api_mode = (
        "chat_completions" if kind == "openai" else "anthropic_messages"
    )
    agent._interrupt_requested = False
    agent._compute_non_stream_stale_timeout.return_value = 10.0

    request_client = MagicMock()
    shared_anthropic_client = MagicMock()
    agent._anthropic_client = shared_anthropic_client
    worker_started = threading.Event()
    socket_aborted = threading.Event()
    owner_closed = threading.Event()
    thread_ids: dict[str, int] = {}

    def blocking_create(*_args, **_kwargs):
        thread_ids["owner"] = threading.get_ident()
        agent._interrupt_requested = True
        worker_started.set()
        assert socket_aborted.wait(timeout=3.0)
        raise httpx.RemoteProtocolError("socket aborted by polling thread")

    def abort(_client, *, reason):
        assert _client is request_client
        assert reason == "interrupt_abort"
        thread_ids["abort"] = threading.get_ident()
        socket_aborted.set()

    def close(_client, *, reason):
        assert _client is request_client
        assert reason == "request_complete"
        thread_ids["close"] = threading.get_ident()
        owner_closed.set()

    if kind == "openai":
        request_client.chat.completions.create.side_effect = blocking_create
        agent._create_request_openai_client.return_value = request_client
        agent._abort_request_openai_client.side_effect = abort
        agent._close_request_openai_client.side_effect = close
    else:
        agent._create_request_anthropic_client.return_value = request_client
        agent._anthropic_messages_create.side_effect = blocking_create
        agent._abort_request_anthropic_client.side_effect = abort
        agent._close_request_anthropic_client.side_effect = close

    poll_thread_id = threading.get_ident()
    with pytest.raises(InterruptedError):
        interruptible_api_call(agent, {"model": "test", "messages": []})

    assert worker_started.is_set()
    assert owner_closed.wait(timeout=3.0)
    assert thread_ids["abort"] == poll_thread_id
    assert thread_ids["close"] == thread_ids["owner"]
    assert thread_ids["close"] != poll_thread_id
    shared_anthropic_client.close.assert_not_called()
    agent._rebuild_anthropic_client.assert_not_called()


@pytest.mark.parametrize(
    "method_name",
    ["_abort_request_openai_client", "_abort_request_anthropic_client"],
)
def test_agent_abort_only_shuts_down_sockets(method_name: str):
    """The cross-thread abort primitive must never invoke SDK ``close``."""
    from run_agent import AIAgent

    client = SimpleNamespace(close=MagicMock())
    agent = AIAgent.__new__(AIAgent)
    agent.provider = "test-provider"
    agent.model = "test-model"
    agent._client_log_context = lambda: "provider=test-provider"
    agent._force_close_tcp_sockets = MagicMock(return_value=2)

    getattr(agent, method_name)(client, reason="interrupt_abort")

    agent._force_close_tcp_sockets.assert_called_once_with(client)
    client.close.assert_not_called()
