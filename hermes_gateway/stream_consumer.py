"""Gateway streaming consumer — bridges sync agent callbacks to async platform delivery.

The agent fires stream_delta_callback(text) synchronously from its worker thread.
GatewayStreamConsumer:
  1. Receives deltas via on_delta() (thread-safe, sync)
  2. Queues them to an asyncio task via queue.Queue
  3. The async run() task buffers, rate-limits, and progressively edits
     a single message on the target platform

Design: Uses the edit transport (send initial message, then editMessageText).
This is universally supported across Telegram, Discord, and Slack.

Credit: jobless0x (#774, #1312), OutThisLife (#798), clicksingh (#697).
"""

from __future__ import annotations

import asyncio
import logging
import queue
import re
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional

from channels.platforms.base import BasePlatformAdapter as _BasePlatformAdapter
from channels.platforms.base import _custom_unit_to_cp
from channels.platforms.base import MEDIA_TAG_CLEANUP_RE
from hermes_gateway.config import (
    DEFAULT_STREAMING_EDIT_INTERVAL as _DEFAULT_STREAMING_EDIT_INTERVAL,
    DEFAULT_STREAMING_BUFFER_THRESHOLD as _DEFAULT_STREAMING_BUFFER_THRESHOLD,
    DEFAULT_STREAMING_CURSOR as _DEFAULT_STREAMING_CURSOR,
)
from hermes_gateway.response_filters import (
    is_intentional_silence_response as _is_intentional_silence_response,
    is_partial_silence_marker as _is_partial_silence_marker,
)
from hermes_gateway.stream_consumer_delivery import StreamConsumerDeliveryMixin
from hermes_gateway.stream_consumer_final import StreamConsumerFinalMixin
from hermes_gateway.stream_consumer_lifecycle import StreamConsumerLifecycleMixin

logger = logging.getLogger("hermes_gateway.stream_consumer")

# Sentinel to signal the stream is complete
_DONE = object()

# Sentinel to signal a tool boundary — finalize current message and start a
# new one so that subsequent text appears below tool progress messages.
_NEW_SEGMENT = object()

# Queue marker for a completed assistant commentary message emitted between
# API/tool iterations (for example: "I'll inspect the repo first.").
_COMMENTARY = object()


@dataclass
class StreamConsumerConfig:
    """Runtime config for a single stream consumer instance."""
    edit_interval: float = _DEFAULT_STREAMING_EDIT_INTERVAL
    buffer_threshold: int = _DEFAULT_STREAMING_BUFFER_THRESHOLD
    cursor: str = _DEFAULT_STREAMING_CURSOR
    buffer_only: bool = False
    # When >0, the final edit for a streamed response is delivered as a
    # fresh message if the original preview has been visible for at least
    # this many seconds.  This makes the platform's visible timestamp
    # reflect completion time instead of first-token time for long-running
    # responses (e.g. reasoning models that stream slowly).  Ported from
    # openclaw/openclaw#72038.  Default 0 = always edit in place (legacy
    # behavior).  The gateway enables this selectively per-platform.
    fresh_final_after_seconds: float = 0.0
    # Streaming transport selection:
    #   "auto"  — prefer native draft streaming (e.g. Telegram sendMessageDraft)
    #             when the adapter + chat supports it; fall back to edit.
    #   "draft" — explicitly request native draft streaming; fall back to
    #             edit when unsupported.
    #   "edit"  — progressive editMessageText (legacy/default behavior).
    #   "off"   — handled by the gateway before the consumer is even built.
    transport: str = "edit"
    # Hint for the consumer about the originating chat type (e.g. "dm",
    # "group", "supergroup", "forum").  Used to gate native draft streaming,
    # which is platform-specific (Telegram drafts are DM-only).
    chat_type: str = ""


class GatewayStreamConsumer(
    StreamConsumerLifecycleMixin,
    StreamConsumerFinalMixin,
    StreamConsumerDeliveryMixin,
):
    """Async consumer that progressively edits a platform message with streamed tokens.

    Usage::

        consumer = GatewayStreamConsumer(adapter, chat_id, config, metadata=metadata)
        # Pass consumer.on_delta as stream_delta_callback to AIAgent
        agent = AIAgent(..., stream_delta_callback=consumer.on_delta)
        # Start the consumer as an asyncio task
        task = asyncio.create_task(consumer.run())
        # ... run agent in thread pool ...
        consumer.finish()  # signal completion
        await task         # wait for final edit
    """

    # After this many consecutive flood-control failures, permanently disable
    # progressive edits for the remainder of the stream.
    _MAX_FLOOD_STRIKES = 3

    # Reasoning/thinking tags that models emit inline in content.
    # Must stay in sync with cli.py _OPEN_TAGS/_CLOSE_TAGS and
    # run_agent.py _strip_think_blocks() tag variants.
    _OPEN_THINK_TAGS = (
        "<REASONING_SCRATCHPAD>", "<think>", "<reasoning>",
        "<THINKING>", "<thinking>", "<thought>",
    )
    _CLOSE_THINK_TAGS = (
        "</REASONING_SCRATCHPAD>", "</think>", "</reasoning>",
        "</THINKING>", "</thinking>", "</thought>",
    )

    # Class-wide monotonic counter for native-streaming draft ids.  Telegram
    # animates a draft when the same draft_id is reused across consecutive
    # calls in the same chat, so we need a fresh non-zero id per response.
    _draft_id_counter: int = 0

    def __init__(
        self,
        adapter: Any,
        chat_id: str,
        config: Optional[StreamConsumerConfig] = None,
        metadata: Optional[dict] = None,
        on_new_message: Optional[callable] = None,
        on_before_finalize: Optional[Callable[[], Any]] = None,
        initial_reply_to_id: Optional[str] = None,
    ):
        self.adapter = adapter
        self.chat_id = chat_id
        self.cfg = config or StreamConsumerConfig()
        self.metadata = metadata
        # Fired whenever a fresh content bubble is created on the platform
        # (first-send of a new message, commentary, overflow chunk, or
        # fallback continuation). The gateway uses this to linearize the
        # tool-progress bubble: when content resumes after a tool batch,
        # the next tool.started should open a NEW progress bubble below
        # the content, not edit the old bubble above it.
        # Called with no arguments. Exceptions are swallowed.
        self._on_new_message = on_new_message
        # Fired once when the stream transitions into its finalization path.
        # Gateway callers use this to pause typing refreshes before a slow
        # final rich-text edit (Telegram MarkdownV2 finalize, etc.).
        self._on_before_finalize = on_before_finalize
        self._initial_reply_to_id = initial_reply_to_id
        self._queue: queue.Queue = queue.Queue()
        self._accumulated = ""
        self._message_id: Optional[str] = None
        # Wall-clock timestamp (time.monotonic) when ``_message_id`` was
        # first assigned from a successful first-send.  Used by the
        # fresh-final logic to detect long-lived previews whose edit
        # timestamps would be stale by completion time.  Ported from
        # openclaw/openclaw#72038.
        self._message_created_ts: Optional[float] = None
        # Every real preview message id the consumer has put on screen during
        # this response (first send + any continuation messages from oversized
        # edits/sends).  The fresh-final path deletes all of them when it
        # re-delivers the completed answer as a single (rich) message, so a
        # reply that was split across the platform's edit limit while streaming
        # doesn't leave stale fragments above the final message.
        self._preview_message_ids: "set[str]" = set()
        self._already_sent = False
        self._edit_supported = True  # Disabled when progressive edits are no longer usable
        self._last_edit_time = 0.0
        self._last_sent_text = ""   # Track last-sent text to skip redundant edits
        # True when the most recent _send_or_edit split-and-delivered across
        # continuation messages (the adapter adopted a new message id).
        self._last_edit_overflowed = False
        self._fallback_final_send = False
        self._fallback_prefix = ""
        # True when fallback is sending only the missing tail after a partial
        # Telegram overflow delivery.  In that case the already-visible prefix
        # is intentional content, not a stale preview to delete.
        self._fallback_preserve_partial_messages = False
        self._flood_strikes = 0         # Consecutive flood-control edit failures
        self._current_edit_interval = self.cfg.edit_interval  # Adaptive backoff
        self._final_response_sent = False
        # Set when the final response content was sent to the user via
        # streaming, even if the final edit (cursor removal etc.)
        # subsequently failed.
        self._final_content_delivered = False
        self._delivered_commentary_texts: list[str] = []
        # Cache adapter lifecycle capability: only platforms that need an
        # explicit finalize call (e.g. DingTalk AI Cards) force us to make
        # a redundant final edit.  Everyone else keeps the fast path.
        # Use ``is True`` (not ``bool(...)``) so MagicMock attribute access
        # in tests doesn't incorrectly enable this path.
        self._adapter_requires_finalize: bool = (
            getattr(adapter, "REQUIRES_EDIT_FINALIZE", False) is True
        )

        # Think-block filter state (mirrors CLI's _stream_delta tag suppression)
        self._in_think_block = False
        self._think_buffer = ""

        # Native draft-streaming state.  Resolved at the start of run() based
        # on cfg.transport, cfg.chat_type, and the adapter's
        # supports_draft_streaming() probe.  When True, the consumer emits
        # animated draft frames via adapter.send_draft instead of progressive
        # edits via adapter.edit_message.  The final answer still goes
        # through the normal first-send path so the user gets a real message
        # in their chat history (drafts have no message_id).
        self._use_draft_streaming = False
        self._draft_id: Optional[int] = None
        # Cumulative draft-frame failure count for this consumer.  After the
        # first failure we permanently disable drafts for the remainder of
        # this response and route through edit-based for graceful degradation.
        self._draft_failures = 0
        self._before_finalize_notified = False

    @property
    def already_sent(self) -> bool:
        """True if at least one message was sent or edited during the run."""
        return self._already_sent

    @property
    def final_response_sent(self) -> bool:
        """True when the stream consumer delivered the final assistant reply."""
        return self._final_response_sent

    @property
    def message_id(self) -> str | None:
        """The Discord/chat message ID of the last-sent or edited message."""
        return self._message_id

    @property
    def final_content_delivered(self) -> bool:
        """True when the final response content reached the user, even if
        the subsequent cosmetic edit (cursor removal) failed."""
        return self._final_content_delivered

    def has_delivered_text(self, text: str) -> bool:
        """Return True when the exact visible text was already delivered."""
        target = self._clean_for_display(text or "").strip()
        if not target:
            return False
        visible_prefix = self._visible_prefix().strip()
        if visible_prefix == target:
            return True
        return any(sent.strip() == target for sent in self._delivered_commentary_texts)

    def on_segment_break(self) -> None:
        """Finalize the current stream segment and start a fresh message."""
        self._queue.put(_NEW_SEGMENT)

    def on_commentary(self, text: str) -> None:
        """Queue a completed interim assistant commentary message."""
        if text:
            self._queue.put((_COMMENTARY, text))

    def _notify_new_message(self) -> None:
        """Fire the on_new_message callback, swallowing any errors."""
        cb = self._on_new_message
        if cb is None:
            return
        try:
            cb()
        except Exception:
            logger.debug("on_new_message callback error", exc_info=True)

    def _reset_segment_state(self, *, preserve_no_edit: bool = False) -> None:
        if preserve_no_edit and self._message_id == "__no_edit__":
            return
        self._message_id = None
        self._message_created_ts = None
        self._accumulated = ""
        self._last_sent_text = ""
        self._fallback_final_send = False
        self._fallback_prefix = ""
        self._fallback_preserve_partial_messages = False
        # #29346: a tool/segment boundary means what we delivered was an interim
        # preamble, not the final answer — clear the flags so a premature setter
        # can't fool the gateway. Safe: got_done returns before any reset, and
        # run.py reads these only after the consumer task exits.
        self._final_response_sent = False
        self._final_content_delivered = False
        # Native draft streaming: bump the draft_id so the next text segment
        # animates as a fresh preview below the tool-progress bubbles, not
        # over the prior segment's already-finalized draft.  This is how
        # we avoid the "inter-tool-call text leak" failure mode openclaw
        # documented in their issue #32535 — each text block becomes its
        # own visible message via the finalize, then a new draft animates
        # for the next one.
        if self._use_draft_streaming:
            type(self)._draft_id_counter += 1
            self._draft_id = type(self)._draft_id_counter

    def on_delta(self, text: str) -> None:
        """Thread-safe callback — called from the agent's worker thread.

        When *text* is ``None``, signals a tool boundary: the current message
        is finalized and subsequent text will be sent as a new message so it
        appears below any tool-progress messages the gateway sent in between.
        """
        if text:
            self._queue.put(text)
        elif text is None:
            self.on_segment_break()

    def finish(self) -> None:
        """Signal that the stream is complete."""
        self._queue.put(_DONE)

    # ── Think-block filtering ────────────────────────────────────────
    # Models like MiniMax emit inline <think>...</think> blocks in their
    # content.  The CLI's _stream_delta suppresses these via a state
    # machine; we do the same here so gateway users never see raw
    # reasoning tags.  The agent also strips them from the final
    # response (run_agent.py _strip_think_blocks), but the stream
    # consumer sends intermediate edits before that stripping happens.

    def _filter_and_accumulate(self, text: str) -> None:
        """Add a text delta to the accumulated buffer, suppressing think blocks.

        Uses a state machine that tracks whether we are inside a
        reasoning/thinking block.  Text inside such blocks is silently
        discarded.  Partial tags at buffer boundaries are held back in
        ``_think_buffer`` until enough characters arrive to decide.
        """
        buf = self._think_buffer + text
        self._think_buffer = ""

        while buf:
            if self._in_think_block:
                # Look for the earliest closing tag
                best_idx = -1
                best_len = 0
                for tag in self._CLOSE_THINK_TAGS:
                    idx = buf.find(tag)
                    if idx != -1 and (best_idx == -1 or idx < best_idx):
                        best_idx = idx
                        best_len = len(tag)

                if best_len:
                    # Found closing tag — discard block, process remainder
                    self._in_think_block = False
                    buf = buf[best_idx + best_len:]
                else:
                    # No closing tag yet — hold tail that could be a
                    # partial closing tag prefix, discard the rest.
                    max_tag = max(len(t) for t in self._CLOSE_THINK_TAGS)
                    self._think_buffer = buf[-max_tag:] if len(buf) > max_tag else buf
                    return
            else:
                # Look for earliest opening tag at a block boundary
                # (start of text / preceded by newline + optional whitespace).
                # This prevents false positives when models *mention* tags
                # in prose (e.g. "the <think> tag is used for…").
                best_idx = -1
                best_len = 0
                for tag in self._OPEN_THINK_TAGS:
                    search_start = 0
                    while True:
                        idx = buf.find(tag, search_start)
                        if idx == -1:
                            break
                        # Block-boundary check (mirrors cli.py logic)
                        if idx == 0:
                            is_boundary = (
                                not self._accumulated
                                or self._accumulated.endswith("\n")
                            )
                        else:
                            preceding = buf[:idx]
                            last_nl = preceding.rfind("\n")
                            if last_nl == -1:
                                is_boundary = (
                                    (not self._accumulated
                                     or self._accumulated.endswith("\n"))
                                    and preceding.strip() == ""
                                )
                            else:
                                is_boundary = preceding[last_nl + 1:].strip() == ""

                        if is_boundary and (best_idx == -1 or idx < best_idx):
                            best_idx = idx
                            best_len = len(tag)
                            break  # first boundary hit for this tag is enough
                        search_start = idx + 1

                if best_len:
                    # Emit text before the tag, enter think block
                    self._accumulated += buf[:best_idx]
                    self._in_think_block = True
                    buf = buf[best_idx + best_len:]
                else:
                    # No opening tag — check for a partial tag at the tail
                    held_back = 0
                    for tag in self._OPEN_THINK_TAGS:
                        for i in range(1, len(tag)):
                            if buf.endswith(tag[:i]) and i > held_back:
                                held_back = i
                    if held_back:
                        self._accumulated += buf[:-held_back]
                        self._think_buffer = buf[-held_back:]
                    else:
                        self._accumulated += buf
                    return

    def _flush_think_buffer(self) -> None:
        """Flush any held-back partial-tag buffer into accumulated text.

        Called when the stream ends (got_done) so that partial text that
        was held back waiting for a possible opening tag is not lost.
        """
        if self._think_buffer and not self._in_think_block:
            self._accumulated += self._think_buffer
            self._think_buffer = ""

    async def run(self) -> None:
        """Async task that drains the queue and edits the platform message."""
        # Platform message length limit — leave room for cursor + formatting.
        # Use the adapter's length function (e.g. utf16_len for Telegram) so
        # overflow detection matches what the platform actually enforces.
        # Gate on isinstance(BasePlatformAdapter) so test MagicMocks (whose
        # auto-attributes return mock objects, not callables) fall back to len.
        _len_fn: "Callable[[str], int]" = (
            self.adapter.message_len_fn
            if isinstance(self.adapter, _BasePlatformAdapter)
            else len
        )
        # Rich-capable adapters (Telegram rich messages) raise this above the
        # legacy per-message limit so a reply that fits one rich send/draft
        # isn't fragmented at 4096 while streaming.  See _raw_message_limit.
        _raw_limit = self._raw_message_limit()
        _safe_limit = max(500, _raw_limit - _len_fn(self.cfg.cursor) - 100)

        # Resolve native draft streaming once per run.  When enabled the
        # consumer routes mid-stream frames through adapter.send_draft and
        # leaves _message_id=None so the existing got_done path delivers the
        # final answer as a regular sendMessage (drafts have no message_id
        # to edit).
        self._use_draft_streaming = self._resolve_draft_streaming()
        if self._use_draft_streaming:
            type(self)._draft_id_counter += 1
            self._draft_id = type(self)._draft_id_counter
            logger.debug(
                "Stream consumer using native-draft transport (chat=%s draft_id=%s)",
                self.chat_id, self._draft_id,
            )

        try:
            while True:
                # Drain all available items from the queue
                got_done = False
                got_segment_break = False
                commentary_text = None
                while True:
                    try:
                        item = self._queue.get_nowait()
                        if item is _DONE:
                            got_done = True
                            break
                        if item is _NEW_SEGMENT:
                            got_segment_break = True
                            break
                        if isinstance(item, tuple) and len(item) == 2 and item[0] is _COMMENTARY:
                            commentary_text = item[1]
                            break
                        self._filter_and_accumulate(item)
                    except queue.Empty:
                        break

                # Flush any held-back partial-tag buffer on stream end
                # so trailing text that was waiting for a potential open
                # tag is not lost.
                if got_done:
                    self._flush_think_buffer()
                    if _is_intentional_silence_response(
                        self._clean_for_display(self._accumulated)
                    ):
                        await self._suppress_silence_marker()
                        return

                # Decide whether to flush an edit
                now = time.monotonic()
                elapsed = now - self._last_edit_time
                should_edit = (
                    got_done
                    or got_segment_break
                    or commentary_text is not None
                )
                if not self.cfg.buffer_only:
                    should_edit = should_edit or (
                        (elapsed >= self._current_edit_interval
                            and self._accumulated)
                        # buffer_threshold is intentionally codepoint-based:
                        # it's a debounce heuristic ("send updates roughly
                        # every N visible characters"), not a platform-limit
                        # check. _len_fn is reserved for overflow detection.
                        or len(self._accumulated) >= self.cfg.buffer_threshold
                    )

                current_update_visible = False
                if (
                    should_edit
                    and not got_done
                    and not got_segment_break
                    and commentary_text is None
                    and _is_partial_silence_marker(
                        self._clean_for_display(self._accumulated)
                    )
                ):
                    should_edit = False
                if should_edit and self._accumulated:
                    # Split overflow: if accumulated text exceeds the platform
                    # limit, split into properly sized chunks.
                    if (
                        _len_fn(self._accumulated) > _safe_limit
                        and self._message_id is None
                    ):
                        # No existing message to edit (first message or after a
                        # segment break).  Use truncate_message — the same
                        # helper the non-streaming path uses — to split with
                        # proper word/code-fence boundaries and chunk
                        # indicators like "(1/2)".
                        chunks = self.adapter.truncate_message(
                            self._accumulated, _safe_limit, len_fn=_len_fn,
                        )
                        chunks_delivered = False
                        reply_to = self._message_id or self._initial_reply_to_id
                        for chunk in chunks:
                            new_id = await self._send_new_chunk(
                                chunk,
                                reply_to,
                                final=got_done,
                            )
                            if new_id is not None and new_id != reply_to:
                                chunks_delivered = True
                        self._accumulated = ""
                        self._last_sent_text = ""
                        self._last_edit_time = time.monotonic()
                        if got_done:
                            # Only claim final delivery if THESE chunks actually
                            # landed.  ``_already_sent`` may be True from prior
                            # tool-progress edits or fallback-mode promotion (#10748)
                            # — that doesn't mean the final answer reached the user.
                            self._final_response_sent = chunks_delivered
                            if chunks_delivered:
                                self._final_content_delivered = True
                            return
                        if got_segment_break:
                            self._message_id = None
                            self._fallback_final_send = False
                            self._fallback_prefix = ""
                        continue

                    # Existing message: edit it with the first chunk, then
                    # start a new message for the overflow remainder.
                    while (
                        _len_fn(self._accumulated) > _safe_limit
                        and self._message_id is not None
                        and self._edit_supported
                    ):
                        _cp_budget = _custom_unit_to_cp(
                            self._accumulated, _safe_limit, _len_fn,
                        )
                        split_at = self._accumulated.rfind("\n", 0, _cp_budget)
                        if split_at < _safe_limit // 2:
                            split_at = _safe_limit
                        chunk = self._accumulated[:split_at]
                        # finalize=True so the adapter applies platform-specific
                        # rich-text markup (e.g. Telegram MarkdownV2). This
                        # sealed chunk will never be edited again — _message_id
                        # is reset to None right below — so it must receive its
                        # final formatting pass now, or early split messages
                        # render raw markdown while only the last chunk renders.
                        # is_turn_final=False: this is the first of several split
                        # messages, NOT the turn-final answer, so the fresh-final
                        # path (opt-in fresh_final_after_seconds) must not mark
                        # the turn delivered on it (#29346 semantics).
                        ok = await self._send_or_edit(
                            chunk, finalize=True, is_turn_final=False,
                        )
                        if self._fallback_final_send or not ok:
                            # Edit failed (or backed off due to flood control)
                            # while attempting to split an oversized message.
                            # Keep the full accumulated text intact so the
                            # fallback final-send path can deliver the remaining
                            # continuation without dropping content.
                            break
                        self._accumulated = self._accumulated[split_at:].lstrip("\n")
                        self._message_id = None
                        self._last_sent_text = ""

                    display_text = self._accumulated
                    if not got_done and not got_segment_break and commentary_text is None:
                        display_text += self.cfg.cursor

                    # Segment break: finalize the current message so platforms
                    # that need explicit closure (e.g. DingTalk AI Cards) don't
                    # leave the previous segment stuck in a loading state when
                    # the next segment (tool progress, next chunk) creates a
                    # new message below it.  got_done has its own finalize
                    # path below so we don't finalize here for it.
                    current_update_visible = await self._send_or_edit(
                        display_text,
                        finalize=(got_done or got_segment_break),
                        # A segment-break finalize closes a preamble, not the
                        # turn-final answer — only got_done marks delivered (#29346).
                        is_turn_final=got_done,
                    )
                    self._last_edit_time = time.monotonic()

                if got_done:
                    if self._accumulated or self._message_id is not None or self._already_sent:
                        await self._notify_before_finalize()
                    # Final edit without cursor. If progressive editing failed
                    # mid-stream, send a single continuation/fallback message
                    # here instead of letting the base gateway path send the
                    # full response again.
                    if self._accumulated:
                        if self._fallback_final_send:
                            await self._send_fallback_final(self._accumulated)
                        elif self._final_response_sent:
                            # A finalize=True tick above already delivered the
                            # final answer via the adapter's fresh-final path
                            # (_try_fresh_final sent a fresh rich message and
                            # deleted the preview).  Running a second finalize
                            # edit here would duplicate the message / re-delete,
                            # so just record delivery and stop.
                            self._final_content_delivered = True
                        elif (
                            current_update_visible
                            and (
                                not self._adapter_requires_finalize
                                or self._last_edit_overflowed
                            )
                        ):
                            # Mid-stream edit above already delivered the
                            # final accumulated content.  Skip the redundant
                            # final edit for adapters that don't need an
                            # explicit finalize signal, and for any adapter
                            # when that edit split-and-delivered across
                            # continuations: the split edit carried
                            # finalize=True itself, and re-finalizing with
                            # the full text would overflow-split again into
                            # the adopted continuation, duplicating chunks
                            # on screen.
                            self._final_response_sent = True
                            self._final_content_delivered = True
                        elif self._message_id:
                            # Either the mid-stream edit didn't run (no
                            # visible update this tick) OR the adapter needs
                            # explicit finalize=True to close the stream.
                            self._final_response_sent = await self._send_or_edit(
                                self._accumulated, finalize=True,
                            )
                            if self._final_response_sent:
                                self._final_content_delivered = True
                            elif self._fallback_final_send:
                                # The final edit attempt itself may be the one
                                # that exhausts flood-control strikes and
                                # promotes the consumer into fallback mode.  Do
                                # not return to the gateway with a full-response
                                # fallback still pending; send only the unsent
                                # tail here so the normal gateway send path does
                                # not duplicate the visible prefix.
                                await self._send_fallback_final(self._accumulated)
                        elif not self._already_sent:
                            self._final_response_sent = await self._send_or_edit(self._accumulated)
                            if self._final_response_sent:
                                self._final_content_delivered = True
                    return

                if commentary_text is not None:
                    self._reset_segment_state()
                    await self._send_commentary(commentary_text)
                    self._last_edit_time = time.monotonic()
                    self._reset_segment_state()

                # Tool boundary: reset message state so the next text chunk
                # creates a fresh message below any tool-progress messages.
                #
                # Exception: when _message_id is "__no_edit__" the platform
                # never returned a real message ID (e.g. Signal, webhook with
                # github_comment delivery).  Resetting to None would re-enter
                # the "first send" path on every tool boundary and post one
                # platform message per tool call — that is what caused 155
                # comments under a single PR.  Instead, preserve the sentinel
                # so the full continuation is delivered once via
                # _send_fallback_final.
                # (When editing fails mid-stream due to flood control the id is
                # a real string like "msg_1", not "__no_edit__", so that case
                # still resets and creates a fresh segment as intended.)
                if got_segment_break:
                    # If the segment-break edit failed to deliver the
                    # accumulated content (flood control that has not yet
                    # promoted to fallback mode, or fallback mode itself),
                    # _accumulated still holds pre-boundary text the user
                    # never saw. Flush that tail as a continuation message
                    # before the reset below wipes _accumulated — otherwise
                    # text generated before the tool boundary is silently
                    # dropped (issue #8124).
                    if (
                        self._accumulated
                        and not current_update_visible
                        and self._message_id
                        and self._message_id != "__no_edit__"
                    ):
                        await self._flush_segment_tail_on_edit_failure()
                    self._reset_segment_state(preserve_no_edit=True)

                await asyncio.sleep(0.05)  # Small yield to not busy-loop

        except asyncio.CancelledError:
            # Best-effort final edit on cancellation.  finalize=True so
            # REQUIRES_EDIT_FINALIZE platforms (Telegram) apply final
            # formatting — a plain edit here would leave the entire reply
            # rendered as a raw streaming preview while the success flags
            # below suppress the gateway's formatted re-send.
            # is_turn_final=False keeps _try_fresh_final from setting
            # _final_response_sent itself; this handler owns the flags.
            _best_effort_ok = False
            if self._accumulated and self._message_id:
                try:
                    _best_effort_ok = bool(
                        await self._send_or_edit(
                            self._accumulated, finalize=True, is_turn_final=False,
                        )
                    )
                except Exception:
                    logger.debug("Suppressed recoverable gateway exception", exc_info=True)
            # Only confirm final delivery if the best-effort send above
            # actually succeeded OR if the final response was already
            # confirmed before we were cancelled.  Previously this
            # promoted any partial send (already_sent=True) to
            # final_response_sent — which suppressed the gateway's
            # fallback send even when only intermediate text (e.g.
            # "Let me search…") had been delivered, not the real answer.
            if _best_effort_ok and not self._final_response_sent:
                self._final_response_sent = True
                self._final_content_delivered = True
        except Exception as e:
            logger.error("Stream consumer error: %s", e)
