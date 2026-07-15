"""Streaming and interim-commentary callbacks for gateway agent turns."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any

from agent.async_utils import safe_schedule_threadsafe
from hermes_gateway.config import Platform, StreamingConfig
from hermes_gateway.display_config import resolve_display_setting

logger = logging.getLogger(__name__)


class AgentStreamingRuntime:
    def __init__(
        self,
        *,
        runner,
        source,
        user_config: dict[str, Any],
        platform_key: str,
        status_adapter,
        status_chat_id: str,
        status_thread_metadata: dict[str, Any] | None,
        event_message_id: str | None,
        loop: asyncio.AbstractEventLoop,
        run_still_current: Callable[[], bool],
        on_new_content_message: Callable[[], None] | None,
        stream_consumer_holder: list[Any],
        interim_messages_enabled: bool,
    ) -> None:
        self._runner = runner
        self._source = source
        self._user_config = user_config
        self._platform_key = platform_key
        self._status_adapter = status_adapter
        self._status_chat_id = status_chat_id
        self._status_thread_metadata = status_thread_metadata
        self._event_message_id = event_message_id
        self._loop = loop
        self._run_still_current = run_still_current
        self._on_new_content_message = on_new_content_message
        self._stream_consumer_holder = stream_consumer_holder
        self._interim_messages_enabled = interim_messages_enabled
        self._stream_consumer = None
        self.stream_delta_callback = None

    def configure(self) -> None:
        streaming_config = getattr(getattr(self._runner, "config", None), "streaming", None)
        if streaming_config is None:
            streaming_config = StreamingConfig()

        platform_streaming = resolve_display_setting(
            self._user_config,
            self._platform_key,
            "streaming",
        )
        streaming_enabled = (
            streaming_config.enabled and streaming_config.transport != "off"
            if platform_streaming is None
            else bool(platform_streaming)
        )
        if not (streaming_enabled or self._interim_messages_enabled):
            return

        try:
            from hermes_gateway.stream_consumer import (
                GatewayStreamConsumer,
                StreamConsumerConfig,
            )

            adapter = self._runner.adapters.get(self._source.platform)
            if not adapter:
                return
            if not getattr(adapter, "SUPPORTS_MESSAGE_EDITING", True):
                raise RuntimeError("skip streaming for non-editable platform")

            effective_cursor = streaming_config.cursor
            buffer_only = False
            if self._source.platform == Platform.MATRIX:
                effective_cursor = ""
                buffer_only = True
            fresh_final_secs = (
                float(getattr(streaming_config, "fresh_final_after_seconds", 0.0) or 0.0)
                if self._source.platform == Platform.TELEGRAM
                else 0.0
            )
            consumer_config = StreamConsumerConfig(
                edit_interval=streaming_config.edit_interval,
                buffer_threshold=streaming_config.buffer_threshold,
                cursor=effective_cursor,
                buffer_only=buffer_only,
                fresh_final_after_seconds=fresh_final_secs,
                transport=streaming_config.transport or "edit",
                chat_type=getattr(self._source, "chat_type", "") or "",
            )
            self._stream_consumer = GatewayStreamConsumer(
                adapter=adapter,
                chat_id=self._source.chat_id,
                config=consumer_config,
                metadata=self._status_thread_metadata,
                on_new_message=self._on_new_content_message,
                initial_reply_to_id=self._event_message_id,
            )
            if streaming_enabled:
                self.stream_delta_callback = self._on_stream_delta
            self._stream_consumer_holder[0] = self._stream_consumer
        except Exception as exc:
            logger.debug("Could not set up stream consumer: %s", exc)

    def interim_callback(self, text: str, *, already_streamed: bool = False) -> None:
        if not self._run_still_current():
            return
        if self._stream_consumer is not None:
            if already_streamed:
                self._stream_consumer.on_segment_break()
            else:
                self._stream_consumer.on_commentary(text)
            return
        if already_streamed or not self._status_adapter or not str(text or "").strip():
            return
        safe_schedule_threadsafe(
            self._status_adapter.send(
                self._status_chat_id,
                text,
                metadata=self._status_thread_metadata,
            ),
            self._loop,
            logger=logger,
            log_message="interim_assistant_callback scheduling error",
        )

    def _on_stream_delta(self, text: str) -> None:
        if self._run_still_current() and self._stream_consumer is not None:
            self._stream_consumer.on_delta(text)


def agent_streaming_for(runner, **kwargs) -> AgentStreamingRuntime:
    return AgentStreamingRuntime(runner=runner, **kwargs)
