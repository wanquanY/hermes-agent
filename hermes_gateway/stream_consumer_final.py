"""Gateway stream consumer finalization helpers."""
from __future__ import annotations

import logging
import time
from typing import Any, Optional

from channels.platforms.base import BasePlatformAdapter as _BasePlatformAdapter

logger = logging.getLogger("hermes_gateway.stream_consumer")


class StreamConsumerFinalMixin:

    async def _send_commentary(self, text: str) -> bool:
        """Send a completed interim assistant commentary message."""
        text = self._clean_for_display(text)
        if not text.strip():
            return False
        try:
            result = await self.adapter.send(
                chat_id=self.chat_id,
                content=text,
                metadata=self.metadata,
            )
            # Note: do NOT set _already_sent = True here.
            # Commentary messages are interim status updates (e.g. "Using browser
            # tool..."), not the final response. Setting already_sent would cause
            # the final response to be incorrectly suppressed when there are
            # multiple tool calls. See: https://github.com/NousResearch/hermes-agent/issues/10454
            if result.success:
                # Commentary counts as fresh content — close off any
                # stale tool bubble above it so the next tool starts a
                # new bubble below.
                self._notify_new_message()
            return result.success
        except Exception as e:
            logger.error("Commentary send error: %s", e)
            return False

    def _should_send_fresh_final(self) -> bool:
        """Return True when a long-lived preview should be replaced with a
        fresh final message instead of an edit.

        Conditions:
        - Fresh-final is enabled (``fresh_final_after_seconds > 0``).
        - We have a real preview message id (not the ``__no_edit__`` sentinel
          and not ``None``).
        - The preview has been visible for at least the configured threshold.

        Ported from openclaw/openclaw#72038.
        """
        threshold = getattr(self.cfg, "fresh_final_after_seconds", 0.0) or 0.0
        if threshold <= 0:
            return False
        if not self._message_id or self._message_id == "__no_edit__":
            return False
        if self._message_created_ts is None:
            return False
        age = time.monotonic() - self._message_created_ts
        return age >= threshold

    def _raw_message_limit(self) -> int:
        """Per-message length budget (in the adapter's ``message_len_fn`` units)
        before the consumer splits an overflowing reply.

        Adapters with a richer send/draft path (e.g. Telegram rich messages)
        can raise this above ``MAX_MESSAGE_LENGTH`` via
        ``streaming_overflow_limit`` so a reply that fits one rich message isn't
        fragmented at the legacy edit limit.  Falls back to
        ``MAX_MESSAGE_LENGTH`` (4096 default) for everyone else.
        """
        base = getattr(self.adapter, "MAX_MESSAGE_LENGTH", 4096)
        # isinstance gate: MagicMock adapters return mock objects (truthy, not
        # ints) for arbitrary attribute access — keep them on the base limit.
        if isinstance(self.adapter, _BasePlatformAdapter):
            try:
                cap = self.adapter.streaming_overflow_limit()
            except Exception as e:
                logger.debug("streaming_overflow_limit check failed: %s", e)
                cap = None
            if isinstance(cap, int) and cap > base:
                return cap
        return base

    def _track_preview_id(self, message_id: Optional[str]) -> None:
        """Record a real preview message id for fresh-final cleanup."""
        if message_id and message_id != "__no_edit__":
            self._preview_message_ids.add(str(message_id))

    def _track_preview_ids_from_result(self, result: Any) -> None:
        """Record every message id a send/edit result exposes: the primary id
        plus any continuation ids from an oversized split
        (``continuation_message_ids`` or ``raw_response['message_ids']``)."""
        self._track_preview_id(getattr(result, "message_id", None))
        for mid in (getattr(result, "continuation_message_ids", None) or ()):
            self._track_preview_id(mid)
        raw = getattr(result, "raw_response", None) or {}
        if isinstance(raw, dict):
            for mid in (raw.get("message_ids") or ()):
                self._track_preview_id(mid)

    def _adapter_prefers_fresh_final(self, text: str) -> bool:
        """Return True when the adapter would rather finalize a streamed reply
        by sending a fresh message and deleting the preview than by editing the
        preview in place — e.g. Telegram, whose ``sendRichMessage`` send path
        currently renders richer markdown than Hermes' MarkdownV2 edit path.

        Returns False when there is no real preview to replace (no message id,
        or the ``__no_edit__`` sentinel), when the adapter doesn't expose the
        hook, or on any error (the consumer then keeps the edit-in-place path).
        """
        if not self._message_id or self._message_id == "__no_edit__":
            return False
        fn = getattr(self.adapter, "prefers_fresh_final_streaming", None)
        if fn is None:
            return False
        try:
            try:
                result = fn(text, metadata=self.metadata)
            except TypeError:
                # Adapter / test double whose hook doesn't accept the metadata
                # keyword — fall back to the positional-only form.
                result = fn(text)
        except Exception as e:
            logger.debug("prefers_fresh_final_streaming check failed: %s", e)
            return False
        # ``is True`` (not ``bool(...)``) so a MagicMock adapter's auto-child
        # method — truthy by default in tests — does not wrongly enable the
        # fresh-final path.  Mirrors the REQUIRES_EDIT_FINALIZE gate in __init__.
        return result is True

    async def _try_fresh_final(self, text: str, *, is_turn_final: bool = True) -> bool:
        """Send ``text`` as a brand-new message (best-effort delete the old
        preview) so the platform's visible timestamp reflects completion
        time.  Returns True on successful delivery, False on any failure so
        the caller falls back to the normal edit path.

        ``is_turn_final`` is False when finalizing an interim segment at a tool
        boundary (a preamble) rather than the turn-final answer; the
        final-delivery flag is then left unset so the gateway still delivers the
        real answer from the next API call (#29346).

        Ported from openclaw/openclaw#72038.
        """
        # Every preview message the user has seen for this response: the
        # current one plus any continuation fragments tracked while streaming
        # (an oversized reply split across the platform's edit limit).  All of
        # them are replaced by the single fresh message below.
        stale_ids = set(self._preview_message_ids)
        if self._message_id and self._message_id != "__no_edit__":
            stale_ids.add(self._message_id)
        try:
            result = await self.adapter.send(
                chat_id=self.chat_id,
                content=text,
                metadata=self._metadata_for_send(final=True),
            )
        except Exception as e:
            logger.debug("Fresh-final send failed, falling back to edit: %s", e)
            return False
        if not getattr(result, "success", False):
            return False
        # Adopt the new message id as the current message so subsequent
        # callers (e.g. overflow split loops, finalize retries) see a
        # consistent state.
        new_message_id = getattr(result, "message_id", None)
        # Successful fresh send — try to delete the stale preview(s) so the
        # user doesn't see the old edit-stuck message(s) underneath.  Cleanup
        # is best-effort; platforms that don't implement ``delete_message``
        # just leave the preview behind (still an acceptable outcome — the
        # visible final timestamp is the important part).  Never delete the
        # message we just sent.
        delete_fn = getattr(self.adapter, "delete_message", None)
        if delete_fn is not None:
            for stale_id in stale_ids:
                if not stale_id or stale_id == "__no_edit__" or stale_id == new_message_id:
                    continue
                try:
                    await delete_fn(self.chat_id, stale_id)
                except Exception as e:
                    logger.debug(
                        "Fresh-final preview cleanup failed (%s): %s",
                        stale_id, e,
                    )
        self._preview_message_ids = set()
        if new_message_id:
            self._message_id = new_message_id
            self._message_created_ts = time.monotonic()
        else:
            # Send succeeded but platform didn't return an id — treat the
            # delivery as final-only and fall back to "__no_edit__" so we
            # don't try to edit something we can't address.
            self._message_id = "__no_edit__"
            self._message_created_ts = None
        self._already_sent = True
        self._last_sent_text = text
        if is_turn_final:
            self._final_response_sent = True
        return True

    async def _suppress_silence_marker(self) -> None:
        """Best-effort retract previews for an intentional-silence turn."""

        stale_ids = set(self._preview_message_ids)
        if self._message_id and self._message_id != "__no_edit__":
            stale_ids.add(self._message_id)
        delete_fn = getattr(self.adapter, "delete_message", None)
        if delete_fn is not None:
            for stale_id in stale_ids:
                if not stale_id or stale_id == "__no_edit__":
                    continue
                try:
                    await delete_fn(self.chat_id, stale_id)
                except Exception as exc:
                    logger.debug(
                        "Silence-marker preview cleanup failed (%s): %s",
                        stale_id,
                        exc,
                    )
        self._preview_message_ids = set()
        self._message_id = None
        self._accumulated = ""
        self._last_sent_text = ""
        self._already_sent = False
        self._final_response_sent = False
        self._final_content_delivered = False
        logger.info(
            "Suppressed streamed intentional-silence marker (chat=%s)",
            self.chat_id,
        )

    async def _send_or_edit(
        self, text: str, *, finalize: bool = False, is_turn_final: bool = True,
    ) -> bool:
        """Send or edit the streaming message.

        Returns True if the text was successfully delivered (sent or edited),
        False otherwise.  Callers like the overflow split loop use this to
        decide whether to advance past the delivered chunk.

        ``finalize`` is True when this is the last edit in a streaming
        sequence.
        """
        # Strip MEDIA: directives so they don't appear as visible text.
        # Media files are delivered as native attachments after the stream
        # finishes (via _deliver_media_from_response in gateway/run.py).
        text = self._clean_for_display(text)
        # A bare streaming cursor is not meaningful user-visible content and
        # can render as a stray tofu/white-box message on some clients.
        visible_without_cursor = text
        if self.cfg.cursor:
            visible_without_cursor = visible_without_cursor.replace(self.cfg.cursor, "")
        _visible_stripped = visible_without_cursor.strip()
        if not _visible_stripped:
            return True  # cursor-only / whitespace-only update
        if not text.strip():
            return True  # nothing to send is "success"
        # Guard: do not create a brand-new standalone message when the only
        # visible content is a handful of characters alongside the streaming
        # cursor.  During rapid tool-calling the model often emits 1-2 tokens
        # before switching to tool calls; the resulting "X ▉" message risks
        # leaving the cursor permanently visible if the follow-up edit (to
        # strip the cursor on segment break) is rate-limited by the platform.
        # This was reported on Telegram, Matrix, and other clients where the
        # ▉ block character renders as a visible white box ("tofu").
        # Existing messages (edits) are unaffected — only first sends gated.
        _MIN_NEW_MSG_CHARS = 4
        if (self._message_id is None
                and self.cfg.cursor
                and self.cfg.cursor in text
                and len(_visible_stripped) < _MIN_NEW_MSG_CHARS):
            return True  # too short for a standalone message — accumulate more

        # Native draft streaming: route mid-stream frames through send_draft.
        # The final answer is delivered via the regular sendMessage path
        # below — drafts have no message_id so we can't finalize them
        # in-place; the regular sendMessage clears the draft naturally on
        # the client and gives the user a real message in their history.
        # Skip when:
        #   * finalize=True (this is the final answer; needs to be a real message)
        #   * an edit path is already established (message_id is set, e.g. after
        #     a tool-boundary segment break where the prior text was finalized
        #     as a real sendMessage and the next text segment continues editing
        #     that one — staying on edit-based for that segment is correct).
        if (
            self._use_draft_streaming
            and not finalize
            and self._message_id is None
        ):
            # No-op skip: identical to the last frame we sent.
            if text == self._last_sent_text:
                return True
            ok = await self._send_draft_frame(text)
            if ok:
                # Drafts mark "we put something on screen" but DO NOT set
                # _already_sent — that flag gates the gateway's fallback
                # final-send path and we still need that to fire so the
                # user gets a real message (drafts have no message_id).
                return True
            # Failure already disabled drafts for this run; fall through to
            # the regular edit/send path below.
        self._last_edit_overflowed = False
        try:
            if self._message_id is not None:
                if self._edit_supported:
                    # Skip if text is identical to what we last sent.
                    # Exception: adapters that require an explicit finalize
                    # call (REQUIRES_EDIT_FINALIZE) must still receive the
                    # finalize=True edit even when content is unchanged, so
                    # their streaming UI can transition out of the in-
                    # progress state.  Everyone else short-circuits.
                    if text == self._last_sent_text and not (
                        finalize and self._adapter_requires_finalize
                    ):
                        return True
                    # Fresh-final for long-lived previews: when finalizing
                    # the last edit in a streaming sequence, if the
                    # original preview has been visible for at least
                    # ``fresh_final_after_seconds``, send the completed
                    # reply as a fresh message so the platform's visible
                    # timestamp reflects completion time instead of the
                    # preview creation time.  Best-effort cleanup of the
                    # old preview follows.  Ported from
                    # openclaw/openclaw#72038.  Gated by config so the
                    # legacy edit-in-place path stays the default.
                    #
                    # Adapters can also opt in regardless of the time threshold
                    # via prefers_fresh_final_streaming (e.g. Telegram, whose
                    # send path renders richer markdown than its edit path):
                    # finalizing through edit would visibly downgrade a rich
                    # preview, so re-deliver as a fresh message + delete the
                    # preview instead.
                    #
                    # When the adapter exposes prefers_fresh_final_streaming
                    # and explicitly returns False, the time-based threshold
                    # must NOT override that decision.  On Telegram the
                    # fresh-final path sends a Rich Message (sendRichMessage)
                    # that overlaps with the legacy MarkdownV2 preview already
                    # visible from streaming — both remain on screen because
                    # the old message is only best-effort deleted.  Adapters
                    # without the hook still get the time-based fresh-final.
                    # (#47048)
                    # Check the *class* for the hook so MagicMock adapters
                    # (which auto-create attributes on access) are not
                    # falsely detected as having it.  Also check instance
                    # __dict__ for test doubles that explicitly assign the
                    # attribute (e.g. adapter.prefers_fresh_final_streaming
                    # = MagicMock(return_value=False)).
                    _has_prefers_hook = (
                        hasattr(type(self.adapter),
                                "prefers_fresh_final_streaming")
                        or "prefers_fresh_final_streaming"
                            in getattr(self.adapter, "__dict__", {})
                    )
                    _prefers_fresh = self._adapter_prefers_fresh_final(text)
                    if (
                        finalize
                        and (
                            _prefers_fresh
                            or (
                                not _has_prefers_hook
                                and self._should_send_fresh_final()
                            )
                        )
                        and await self._try_fresh_final(
                            text, is_turn_final=is_turn_final,
                        )
                    ):
                        return True
                    # Edit existing message
                    result = await self._edit_message(
                        message_id=self._message_id,
                        content=text,
                        finalize=finalize,
                    )
                    if result.success:
                        self._already_sent = True
                        # Record any continuation fragments an oversized edit
                        # split off, so fresh-final can clean them all up.
                        self._track_preview_ids_from_result(result)
                        # Adapter may have split-and-delivered an oversized
                        # edit across the original message + N continuations.
                        # When that happens, ``message_id`` is the LAST visible
                        # continuation and ``_last_sent_text`` no longer reflects
                        # the on-screen content (the new message only holds the
                        # final chunk's text), so subsequent edits must target
                        # the new id and skip-if-same comparisons must reset.
                        # Fire on_new_message so tool-progress bubbles linearize
                        # below the new continuation, not the original.
                        # ``getattr`` with default keeps backwards compat with
                        # SimpleNamespace mocks in tests that pre-date the field.
                        _continuation_ids = getattr(result, "continuation_message_ids", ()) or ()
                        if (
                            _continuation_ids
                            and result.message_id
                            and result.message_id != self._message_id
                        ):
                            self._last_edit_overflowed = True
                            self._message_id = str(result.message_id)
                            self._message_created_ts = time.monotonic()
                            self._last_sent_text = ""
                            self._notify_new_message()
                        else:
                            self._last_sent_text = text
                        # Successful edit — reset flood strike counter
                        self._flood_strikes = 0
                        return True
                    else:
                        if (
                            finalize
                            and is_turn_final
                            and self.cfg.cursor
                            and self._last_sent_text.endswith(self.cfg.cursor)
                            and self._visible_prefix() == text
                        ):
                            # The final clean-up edit failed, but the complete
                            # answer is already visible from the last streaming
                            # frame (usually with only the cursor still stuck on
                            # screen).  Mark the content delivered so the
                            # gateway suppresses its normal full final send;
                            # otherwise users see the same long answer twice
                            # when Telegram/Discord rate-limit this cosmetic
                            # final edit (#36965, #25349).
                            self._final_content_delivered = True
                        raw_response = getattr(result, "raw_response", None)
                        if isinstance(raw_response, dict) and raw_response.get("partial_overflow"):
                            # Telegram edited/sent one or more overflow chunks,
                            # but not the complete response.  Preserve the
                            # visible prefix so the got_done fallback sends the
                            # missing tail instead of marking a clipped topic
                            # reply as final delivery.
                            self._message_id = str(
                                raw_response.get("last_message_id")
                                or result.message_id
                                or self._message_id
                            )
                            delivered_prefix = raw_response.get("delivered_prefix")
                            if isinstance(delivered_prefix, str) and delivered_prefix:
                                self._last_sent_text = delivered_prefix
                                self._fallback_prefix = delivered_prefix
                                self._fallback_preserve_partial_messages = text.startswith(
                                    delivered_prefix
                                )
                            else:
                                self._fallback_prefix = self._visible_prefix()
                                self._fallback_preserve_partial_messages = False
                            self._fallback_final_send = True
                            self._edit_supported = False
                            self._already_sent = True
                            if getattr(result, "continuation_message_ids", ()):
                                self._notify_new_message()
                            return False

                        # Edit failed.  If this looks like flood control / rate
                        # limiting, use adaptive backoff: double the edit interval
                        # and retry on the next cycle.  Only permanently disable
                        # edits after _MAX_FLOOD_STRIKES consecutive failures.
                        if self._is_flood_error(result):
                            self._flood_strikes += 1
                            self._current_edit_interval = min(
                                self._current_edit_interval * 2, 10.0,
                            )
                            logger.debug(
                                "Flood control on edit (strike %d/%d), "
                                "backoff interval → %.1fs",
                                self._flood_strikes,
                                self._MAX_FLOOD_STRIKES,
                                self._current_edit_interval,
                            )
                            if self._flood_strikes < self._MAX_FLOOD_STRIKES:
                                # Don't disable edits yet — just slow down.
                                # Update _last_edit_time so the next edit
                                # respects the new interval.
                                self._last_edit_time = time.monotonic()
                                return False

                        # Non-flood error OR flood strikes exhausted: enter
                        # fallback mode — send only the missing tail once the
                        # final response is available.
                        logger.debug(
                            "Edit failed (strikes=%d), entering fallback mode",
                            self._flood_strikes,
                        )
                        self._fallback_prefix = self._visible_prefix()
                        self._fallback_final_send = True
                        self._edit_supported = False
                        self._already_sent = True
                        # Best-effort: strip the cursor from the last visible
                        # message so the user doesn't see a stuck ▉.
                        await self._try_strip_cursor()
                        return False
                else:
                    # Editing not supported — skip intermediate updates.
                    # The final response will be sent by the fallback path.
                    return False
            else:
                # First message — send new, threaded to the original user message
                # so it lands in the correct topic/thread.
                result = await self.adapter.send(
                    chat_id=self.chat_id,
                    content=text,
                    reply_to=self._initial_reply_to_id,
                    metadata=self._metadata_for_send(
                        final=finalize,
                        expect_edits=True,
                    ),
                )
                if result.success:
                    if result.message_id:
                        self._message_id = result.message_id
                        # Track when the preview first became visible to
                        # the user so fresh-final logic can detect stale
                        # preview timestamps on long-running responses.
                        self._message_created_ts = time.monotonic()
                        # Record this (and any continuation fragments from an
                        # oversized first send) for fresh-final cleanup.
                        self._track_preview_ids_from_result(result)
                    else:
                        self._edit_supported = False
                    self._already_sent = True
                    self._last_sent_text = text
                    if not result.message_id:
                        self._fallback_prefix = self._visible_prefix()
                        self._fallback_final_send = True
                        # Sentinel prevents re-entering the first-send path on
                        # every delta/tool boundary when platforms accept a
                        # message but do not return an editable message id.
                        self._message_id = "__no_edit__"
                    # Notify the gateway that a fresh content bubble was
                    # created so any accumulated tool-progress bubble above
                    # gets closed off — the next tool fires into a new
                    # bubble below, preserving chronological order.
                    self._notify_new_message()
                    return True
                else:
                    # Initial send failed — disable streaming for this session
                    self._edit_supported = False
                    return False
        except Exception as e:
            logger.error("Stream send/edit error: %s", e)
            return False
