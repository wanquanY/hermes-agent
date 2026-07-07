from __future__ import annotations

import threading
from types import SimpleNamespace
from typing import Any, Callable

import pytest

from agent.chat_completion_helpers import (
    interruptible_api_call,
    interruptible_streaming_api_call,
)
from channels.session_context import clear_session_vars, get_session_env, set_session_vars


@pytest.fixture(autouse=True)
def _clear_dovie_context(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("HERMES_DOVIE_PRODUCT_CONTEXT", raising=False)
    tokens = set_session_vars(dovie_product_context="")
    clear_session_vars(tokens)
    yield
    tokens = set_session_vars(dovie_product_context="")
    clear_session_vars(tokens)


class _FakeClient:
    def __init__(self, create: Callable[..., Any]) -> None:
        self.chat = SimpleNamespace(
            completions=SimpleNamespace(create=create),
        )


class _FakeAgent:
    def __init__(self, create: Callable[..., Any]) -> None:
        self.api_mode = "chat_completions"
        self.provider = "openai"
        self.model = "fake-model"
        self.base_url = "https://api.example.test/v1"
        self.stream_delta_callback = None
        self.reasoning_callback = None
        self.tool_gen_callback = None
        self._stream_callback = None
        self._interrupt_requested = False
        self._current_streamed_assistant_text = ""
        self._client = _FakeClient(create)

    def _create_request_openai_client(self, **_kwargs: Any) -> _FakeClient:
        return self._client

    def _close_request_openai_client(self, _client: _FakeClient, *, reason: str) -> None:
        return None

    def _compute_non_stream_stale_timeout(self, _api_payload: Any) -> float:
        return 5.0

    def _touch_activity(self, _desc: str) -> None:
        return None

    def _emit_status(self, _message: str) -> None:
        return None

    def _has_stream_consumers(self) -> bool:
        return False

    def _stream_diag_init(self) -> dict[str, Any]:
        return {}

    def _stream_diag_capture_response(self, _diag: dict[str, Any], _response: Any) -> None:
        return None

    def _capture_rate_limits(self, _response: Any) -> None:
        return None

    def _check_openrouter_cache_status(self, _response: Any) -> None:
        return None

    def _fire_stream_delta(self, _text: str) -> None:
        return None

    def _fire_reasoning_delta(self, _text: str) -> None:
        return None

    def _fire_tool_gen_started(self, _tool_name: str) -> None:
        return None

    def _is_provider_stream_parse_error(self, _error: BaseException) -> bool:
        return False

    def _emit_stream_drop(self, **_kwargs: Any) -> None:
        return None

    def _log_stream_retry(self, **_kwargs: Any) -> None:
        return None

    def _replace_primary_openai_client(self, *, reason: str) -> bool:
        return True


def _set_dovie_context(value: str) -> list:
    return set_session_vars(dovie_product_context=value)


def _stream_chunk(content: str, *, finish_reason: str | None = None) -> SimpleNamespace:
    delta = SimpleNamespace(
        content=content,
        tool_calls=None,
        reasoning_content=None,
        reasoning=None,
    )
    choice = SimpleNamespace(delta=delta, finish_reason=finish_reason)
    return SimpleNamespace(choices=[choice], model="fake-model", usage=None)


def test_non_stream_chat_completion_thread_reads_process_dovie_context() -> None:
    seen: dict[str, str] = {}

    def create(**_kwargs: Any) -> SimpleNamespace:
        seen["context"] = get_session_env("HERMES_DOVIE_PRODUCT_CONTEXT", "")
        assert seen["context"] == "sentinel-non-stream"
        return SimpleNamespace(id="response-non-stream")

    agent = _FakeAgent(create)
    tokens = _set_dovie_context("sentinel-non-stream")
    try:
        response = interruptible_api_call(agent, {"model": "fake-model", "messages": []})
    finally:
        clear_session_vars(tokens)

    assert response.id == "response-non-stream"
    assert seen == {"context": "sentinel-non-stream"}


def test_stream_chat_completion_thread_reads_process_dovie_context() -> None:
    seen: dict[str, str] = {}

    def create(**_kwargs: Any) -> list[SimpleNamespace]:
        seen["context"] = get_session_env("HERMES_DOVIE_PRODUCT_CONTEXT", "")
        assert seen["context"] == "sentinel-stream"
        return [_stream_chunk("ok", finish_reason="stop")]

    agent = _FakeAgent(create)
    tokens = _set_dovie_context("sentinel-stream")
    try:
        response = interruptible_streaming_api_call(
            agent,
            {"model": "fake-model", "messages": []},
        )
    finally:
        clear_session_vars(tokens)

    assert response.choices[0].message.content == "ok"
    assert seen == {"context": "sentinel-stream"}


def test_nested_chat_completion_thread_reads_process_dovie_context() -> None:
    seen: dict[str, str] = {}

    def nested_read() -> None:
        def read() -> None:
            seen["context"] = get_session_env("HERMES_DOVIE_PRODUCT_CONTEXT", "")

        thread = threading.Thread(target=read, daemon=True)
        thread.start()
        thread.join(timeout=2.0)
        assert not thread.is_alive()

    def create(**_kwargs: Any) -> SimpleNamespace:
        nested_read()
        assert seen["context"] == "sentinel-nested"
        return SimpleNamespace(id="response-nested")

    agent = _FakeAgent(create)
    tokens = _set_dovie_context("sentinel-nested")
    try:
        response = interruptible_api_call(agent, {"model": "fake-model", "messages": []})
    finally:
        clear_session_vars(tokens)

    assert response.id == "response-nested"
    assert seen == {"context": "sentinel-nested"}
