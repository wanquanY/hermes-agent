"""Gateway stream consumer send and fallback helpers."""
from __future__ import annotations

import asyncio
import logging
import re
from typing import Any, Callable, Optional

from channels.platforms.base import BasePlatformAdapter as _BasePlatformAdapter
from channels.platforms.base import MEDIA_TAG_CLEANUP_RE
from channels.platforms.base import _custom_unit_to_cp

logger = logging.getLogger("hermes_gateway.stream_consumer")


class StreamConsumerDeliveryMixin:
    _MEDIA_RE = MEDIA_TAG_CLEANUP_RE

    @staticmethod
    def _clean_for_display(text: str) -> str:
        """Strip MEDIA: directives and internal markers from text before display.

        The streaming path delivers raw text chunks that may include
        ``MEDIA:<path>`` tags and ``[[audio_as_voice]]`` directives meant for
        the platform adapter's post-processing.  The actual media files are
        delivered separately via ``GatewayMediaDeliveryService`` after the
        stream finishes — we just need to hide the raw directives from the
        user.
        """
        if "MEDIA:" not in text and "[[audio_as_voice]]" not in text:
            return text
        cleaned = text.replace("[[audio_as_voice]]", "")
        cleaned = StreamConsumerDeliveryMixin._MEDIA_RE.sub("", cleaned)
        # Collapse excessive blank lines left behind by removed tags
        cleaned = re.sub(r'\n{3,}', '\n\n', cleaned)
        # Strip trailing whitespace/newlines but preserve leading content
        return cleaned.rstrip()

    async def _send_new_chunk(
        self,
        text: str,
        reply_to_id: Optional[str],
        *,
        final: bool = False,
    ) -> Optional[str]:
        """Send a new message chunk, optionally threaded to a previous message.

        Returns the message_id so callers can thread subsequent chunks.
        """
        text = self._clean_for_display(text)
        if not text.strip():
            return reply_to_id
        try:
            result = await self.adapter.send(
                chat_id=self.chat_id,
                content=text,
                reply_to=reply_to_id,
                metadata=self._metadata_for_send(final=final, expect_edits=True),
            )
            if result.success and result.message_id:
                self._message_id = str(result.message_id)
                self._track_preview_ids_from_result(result)
                self._already_sent = True
                self._last_sent_text = text
                # Fresh content bubble — close off any stale tool bubble
                # above so the next tool starts a new bubble below.
                self._notify_new_message()
                return str(result.message_id)
            else:
                self._edit_supported = False
                return reply_to_id
        except Exception as e:
            logger.error("Stream send chunk error: %s", e)
            return reply_to_id

    def _visible_prefix(self) -> str:
        """Return the visible text already shown in the streamed message."""
        prefix = self._last_sent_text or ""
        if self.cfg.cursor and prefix.endswith(self.cfg.cursor):
            prefix = prefix[:-len(self.cfg.cursor)]
        return self._clean_for_display(prefix)

    def _continuation_text(self, final_text: str) -> str:
        """Return only the part of final_text the user has not already seen."""
        prefix = self._fallback_prefix or self._visible_prefix()
        if prefix and final_text.startswith(prefix):
            return final_text[len(prefix):].lstrip()
        return final_text

    @staticmethod
    def _split_text_chunks(
        text: str, limit: int,
        len_fn: "Callable[[str], int]" = len,
    ) -> list[str]:
        """Split text into reasonably sized chunks for fallback sends."""
        if len_fn(text) <= limit:
            return [text]
        chunks: list[str] = []
        remaining = text
        while len_fn(remaining) > limit:
            _cp_budget = _custom_unit_to_cp(remaining, limit, len_fn)
            split_at = remaining.rfind("\n", 0, _cp_budget)
            if split_at < limit // 2:
                split_at = limit
            chunks.append(remaining[:split_at])
            remaining = remaining[split_at:].lstrip("\n")
        if remaining:
            chunks.append(remaining)
        return chunks

    async def _send_fallback_final(self, text: str) -> None:
        """Send the final continuation after streaming edits stop working.

        Retries each chunk once on flood-control failures with a short delay.
        """
        final_text = self._clean_for_display(text)
        visible_prefix = self._visible_prefix()
        fallback_requested = self._fallback_final_send
        if (
            fallback_requested
            or self._fallback_preserve_partial_messages
            or not self._message_id
            or not visible_prefix
            or final_text == visible_prefix
        ):
            continuation = self._continuation_text(final_text)
        else:
            # A normal fallback preview is stale: re-send the complete final
            # response, then delete the preview once the fresh message lands.
            # Only partial-overflow fallback keeps the visible prefix and sends
            # just the missing tail.
            continuation = final_text
        self._fallback_final_send = False
        if not continuation.strip():
            # Nothing new to send — the visible partial already matches final text.
            # BUT: if final_text itself has meaningful content (e.g. a timeout
            # message after a long tool call), the prefix-based continuation
            # calculation may wrongly conclude "already shown" because the
            # streamed prefix was from a *previous* segment (before the tool
            # boundary).  In that case, send the full final_text as-is (#10807).
            if final_text.strip() and final_text != visible_prefix:
                continuation = final_text
            else:
                # Defence-in-depth for #7183: the last edit may still show the
                # cursor character because fallback mode was entered after an
                # edit failure left it stuck.  Try one final edit to strip it
                # so the message doesn't freeze with a visible ▉.  Best-effort
                # — if this edit also fails (flood control still active),
                # _try_strip_cursor has already been called on fallback entry
                # and the adaptive-backoff retries will have had their shot.
                if (
                    self._message_id
                    and self._last_sent_text
                    and self.cfg.cursor
                    and self._last_sent_text.endswith(self.cfg.cursor)
                ):
                    clean_text = self._last_sent_text[:-len(self.cfg.cursor)]
                    try:
                        result = await self._edit_message(
                            message_id=self._message_id,
                            content=clean_text,
                        )
                        if result.success:
                            self._last_sent_text = clean_text
                    except Exception:
                        pass
                self._already_sent = True
                self._final_response_sent = True
                self._final_content_delivered = True
                return

        raw_limit = getattr(self.adapter, "MAX_MESSAGE_LENGTH", 4096)
        _len_fn: "Callable[[str], int]" = (
            self.adapter.message_len_fn
            if isinstance(self.adapter, _BasePlatformAdapter)
            else len
        )
        safe_limit = max(500, raw_limit - 100)
        chunks = self._split_text_chunks(continuation, safe_limit, len_fn=_len_fn)

        stale_message_id = self._message_id  # partial message to clean up
        last_message_id: Optional[str] = None
        last_successful_chunk = ""
        sent_any_chunk = False
        for chunk in chunks:
            # Try sending with one retry on flood-control errors.
            result = None
            for attempt in range(2):
                result = await self.adapter.send(
                    chat_id=self.chat_id,
                    content=chunk,
                    metadata=self._metadata_for_send(final=True),
                )
                if result.success:
                    break
                if attempt == 0 and self._is_flood_error(result):
                    logger.debug(
                        "Flood control on fallback send, retrying in 3s"
                    )
                    await asyncio.sleep(3.0)
                else:
                    break  # non-flood error or second attempt failed

            if not result or not result.success:
                if sent_any_chunk:
                    # Some continuation text already reached the user, but not
                    # the full response. Do NOT set _final_response_sent — the
                    # base gateway final-send path should still deliver the
                    # complete response so the user gets the full answer.
                    # Suppress only _already_sent to avoid a duplicate send
                    # of the same partial content.
                    self._already_sent = True
                    self._message_id = last_message_id
                    self._last_sent_text = last_successful_chunk
                    self._fallback_prefix = ""
                    return
                # No fallback chunk reached the user — allow the normal gateway
                # final-send path to try one more time.
                self._already_sent = False
                self._message_id = None
                self._last_sent_text = ""
                self._fallback_prefix = ""
                return
            sent_any_chunk = True
            last_successful_chunk = chunk
            last_message_id = result.message_id or last_message_id
            # Each fallback chunk is a fresh platform message — notify
            # so any stale tool-progress bubble gets closed off.
            self._notify_new_message()

        # Remove the frozen partial message so the user only sees the
        # complete fallback response.  ONLY safe when the fallback re-sent
        # the FULL final text (continuation == final_text).  When the
        # prefix-based dedup above sent only the missing TAIL, the partial
        # message IS the head of the answer — deleting it leaves the user
        # with only the last part of the response (the "Gemini sent only
        # the second half" symptom).  Best-effort — if the platform doesn't
        # implement ``delete_message``, the delete fails (flood control still
        # active, bot lacks permission, message too old to delete), the
        # partial remains but at least the full answer was delivered.
        if (
            stale_message_id
            and stale_message_id != last_message_id
            and not self._fallback_preserve_partial_messages
            and continuation == final_text
        ):
            delete_fn = getattr(self.adapter, "delete_message", None)
            if delete_fn is not None:
                try:
                    await delete_fn(self.chat_id, stale_message_id)
                except Exception as e:
                    logger.debug(
                        "Fallback partial cleanup failed (%s): %s",
                        stale_message_id, e,
                    )

        self._message_id = last_message_id
        self._already_sent = True
        self._final_response_sent = True
        self._final_content_delivered = True
        self._last_sent_text = chunks[-1]
        self._fallback_prefix = ""
        self._fallback_preserve_partial_messages = False

    def _is_flood_error(self, result) -> bool:
        """Check if a SendResult failure is due to flood control / rate limiting."""
        err = getattr(result, "error", "") or ""
        err_lower = err.lower()
        return "flood" in err_lower or "retry after" in err_lower or "rate" in err_lower

    def _resolve_draft_streaming(self) -> bool:
        """Decide whether this run should use native draft streaming.

        Honors ``cfg.transport``:
          * ``"edit"``  → never use drafts (legacy progressive-edit path).
          * ``"draft"`` → require draft support; gracefully fall back to edit
            when the adapter declines.  Logs the downgrade at debug.
          * ``"auto"``  → use drafts when the adapter supports them for this
            chat type; otherwise edit.

        Adapter eligibility is checked via
        :meth:`BasePlatformAdapter.supports_draft_streaming`, which considers
        the chat type (e.g. Telegram drafts are DM-only) and platform-version
        gates (e.g. python-telegram-bot 22.6+).
        """
        transport = (self.cfg.transport or "edit").lower()
        if transport == "edit":
            return False
        # "off" is filtered upstream by the gateway; treat as edit defensively.
        if transport == "off":
            return False
        # Test adapters are MagicMocks that don't subclass BasePlatformAdapter;
        # default them to edit so existing test behaviour is preserved.
        if not isinstance(self.adapter, _BasePlatformAdapter):
            return False
        try:
            supported = self.adapter.supports_draft_streaming(
                chat_type=self.cfg.chat_type or None,
                metadata=self.metadata,
            )
        except Exception:
            logger.debug("supports_draft_streaming probe raised", exc_info=True)
            supported = False
        if not supported:
            if transport == "draft":
                logger.debug(
                    "Draft streaming requested but unsupported (chat=%s, type=%r) — "
                    "falling back to edit",
                    self.chat_id, self.cfg.chat_type,
                )
            return False
        return True

    async def _send_draft_frame(self, text: str) -> bool:
        """Emit a single animated draft frame for the current accumulated text.

        Returns True when the frame landed.  On any failure, permanently
        disables drafts for the remainder of this run so subsequent frames
        flow through the edit-based path (which can adapt with flood-control
        backoff, etc.).  Drafts have no message_id and clear naturally on
        the client when the response finalizes via a regular sendMessage.
        """
        if self._draft_id is None:
            # Defensive: should never happen — _use_draft_streaming gate is
            # set in tandem with _draft_id in run().  Disable to be safe.
            self._use_draft_streaming = False
            return False
        try:
            result = await self.adapter.send_draft(
                chat_id=self.chat_id,
                draft_id=self._draft_id,
                content=text,
                metadata=self.metadata,
            )
        except Exception as e:
            logger.debug(
                "send_draft raised, disabling draft transport for this run: %s", e,
            )
            self._draft_failures += 1
            self._use_draft_streaming = False
            return False
        if not getattr(result, "success", False):
            logger.debug(
                "send_draft returned success=False, disabling draft transport: %s",
                getattr(result, "error", "unknown"),
            )
            self._draft_failures += 1
            self._use_draft_streaming = False
            return False
        # Frame delivered.  Track text for parity with edit-based no-op skip.
        self._last_sent_text = text
        return True

    async def _flush_segment_tail_on_edit_failure(self) -> None:
        """Deliver un-sent tail content before a segment-break reset.

        When an edit fails (flood control, transport error) and a tool
        boundary arrives before the next retry, ``_accumulated`` holds text
        that was generated but never shown to the user. Without this flush,
        the segment reset would discard that tail and leave a frozen cursor
        in the partial message.

        Sends the tail that sits after the last successfully-delivered
        prefix as a new message, and best-effort strips the stuck cursor
        from the previous partial message.
        """
        if not self._fallback_final_send:
            await self._try_strip_cursor()
        visible = self._fallback_prefix or self._visible_prefix()
        tail = self._accumulated
        if visible and tail.startswith(visible):
            tail = tail[len(visible):].lstrip()
        tail = self._clean_for_display(tail)
        if not tail.strip():
            return
        try:
            result = await self.adapter.send(
                chat_id=self.chat_id,
                content=tail,
                metadata=self.metadata,
            )
            if result.success:
                self._already_sent = True
        except Exception as e:
            logger.error("Segment-break tail flush error: %s", e)

    async def _try_strip_cursor(self) -> None:
        """Best-effort edit to remove the cursor from the last visible message.

        Called when entering fallback mode so the user doesn't see a stuck
        cursor (▉) in the partial message.
        """
        if not self._message_id or self._message_id == "__no_edit__":
            return
        prefix = self._visible_prefix()
        if not prefix or not prefix.strip():
            return
        try:
            await self._edit_message(
                message_id=self._message_id,
                content=prefix,
            )
            self._last_sent_text = prefix
        except Exception:
            pass  # best-effort — don't let this block the fallback path
