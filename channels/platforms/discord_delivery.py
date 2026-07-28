from __future__ import annotations

import asyncio
import logging
import os
import re
from typing import Any, Dict, List, Optional, Tuple

from agent.secret_scope import get_profile_env
try:
    import discord
except ImportError:  # pragma: no cover - optional platform dependency
    discord = None

from channels.platforms.base import MessageEvent, ProcessingOutcome, SendResult
from tools.url_safety import is_safe_url

logger = logging.getLogger(__name__)


class DiscordDeliveryMixin:

    async def _add_reaction(self, message: Any, emoji: str) -> bool:
        """Add an emoji reaction to a Discord message."""
        if not message or not hasattr(message, "add_reaction"):
            return False
        try:
            await message.add_reaction(emoji)
            return True
        except Exception as e:
            logger.debug("[%s] add_reaction failed (%s): %s", self.name, emoji, e)
            return False
    
    async def _remove_reaction(self, message: Any, emoji: str) -> bool:
        """Remove the bot's own emoji reaction from a Discord message."""
        if not message or not hasattr(message, "remove_reaction") or not self._client or not self._client.user:
            return False
        try:
            await message.remove_reaction(emoji, self._client.user)
            return True
        except Exception as e:
            logger.debug("[%s] remove_reaction failed (%s): %s", self.name, emoji, e)
            return False
    
    def _reactions_enabled(self) -> bool:
        """Check if message reactions are enabled via config/env."""
        configured = self.config.extra.get("reactions")
        raw = (
            str(configured)
            if configured is not None
            else get_profile_env("DISCORD_REACTIONS", "true")
        )
        return raw.lower() not in {"false", "0", "no"}
    
    async def on_processing_start(self, event: MessageEvent) -> None:
        """Add an in-progress reaction and record durable handling state."""
        message = event.raw_message
        acked = False
        if self._reactions_enabled() and hasattr(message, "add_reaction"):
            acked = await self._add_reaction(message, "👀")
        await asyncio.to_thread(
            self._record_discord_processing_start,
            event,
            emoji_ack=acked,
        )
    
    async def on_processing_complete(self, event: MessageEvent, outcome: ProcessingOutcome) -> None:
        """Record completion and swap the in-progress reaction."""
        await asyncio.to_thread(
            self._record_discord_processing_complete,
            event,
            outcome,
        )
        if not self._reactions_enabled():
            return
        message = event.raw_message
        if hasattr(message, "add_reaction"):
            await self._remove_reaction(message, "👀")
            if outcome == ProcessingOutcome.SUCCESS:
                await self._add_reaction(message, "✅")
            elif outcome == ProcessingOutcome.FAILURE:
                await self._add_reaction(message, "❌")
    
    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None
    ) -> SendResult:
        """Send a message to a Discord channel or thread.
    
        When metadata contains a thread_id, the message is sent to that
        thread instead of the parent channel identified by chat_id.
    
        Forum channels (type 15) reject direct messages — a thread post is
        created automatically.
        """
        if not self._client:
            return SendResult(success=False, error="Not connected")
    
        try:
            # Determine target channel: thread_id in metadata takes precedence.
            thread_id = None
            if metadata and metadata.get("thread_id"):
                thread_id = metadata["thread_id"]
    
            if thread_id:
                # Fetch the thread directly — threads are addressed by their own ID.
                channel = self._client.get_channel(int(thread_id))
                if not channel:
                    channel = await self._client.fetch_channel(int(thread_id))
                if not channel:
                    return SendResult(success=False, error=f"Thread {thread_id} not found")
            else:
                # Get the parent channel
                channel = self._client.get_channel(int(chat_id))
                if not channel:
                    channel = await self._client.fetch_channel(int(chat_id))
                if not channel:
                    return SendResult(success=False, error=f"Channel {chat_id} not found")
    
            # Forum channels reject channel.send() — create a thread post instead.
            if self._is_forum_parent(channel):
                result = await self._send_to_forum(channel, content)
                await asyncio.to_thread(
                    self._record_discord_response,
                    reply_to=reply_to,
                    result=result,
                    content=content,
                    final=bool(metadata and metadata.get("notify")),
                )
                return result
    
            # Format and split message if needed
            formatted = self.format_message(content)
            chunks = self.truncate_message(formatted, self.MAX_MESSAGE_LENGTH)
    
            message_ids = []
            reference = None
    
            if reply_to and self._reply_to_mode != "off":
                try:
                    ref_msg = await channel.fetch_message(int(reply_to))
                    if hasattr(ref_msg, "to_reference"):
                        reference = ref_msg.to_reference(fail_if_not_exists=False)
                    else:
                        reference = ref_msg
                except Exception as e:
                    logger.debug("Could not fetch reply-to message: %s", e)
    
            for i, chunk in enumerate(chunks):
                if self._reply_to_mode == "all":
                    chunk_reference = reference
                else:  # "first" (default) or "off"
                    chunk_reference = reference if i == 0 else None
                try:
                    msg = await channel.send(
                        content=chunk,
                        reference=chunk_reference,
                    )
                except Exception as e:
                    err_text = str(e)
                    if (
                        chunk_reference is not None
                        and (
                            (
                                "error code: 50035" in err_text
                                and "Cannot reply to a system message" in err_text
                            )
                            or "error code: 10008" in err_text
                        )
                    ):
                        logger.warning(
                            "[%s] Reply target %s rejected the reply reference; retrying send without reply reference",
                            self.name,
                            reply_to,
                        )
                        reference = None
                        msg = await channel.send(
                            content=chunk,
                            reference=None,
                        )
                    else:
                        raise
                message_ids.append(str(msg.id))
    
            # Track the last message we sent in this channel for history
            # backfill — avoids a full channel.history() scan on hot paths.
            if message_ids:
                _target_id = thread_id or chat_id
                self._last_self_message_id[_target_id] = message_ids[-1]
    
            result = SendResult(
                success=True,
                message_id=message_ids[0] if message_ids else None,
                raw_response={"message_ids": message_ids}
            )
            await asyncio.to_thread(
                self._record_discord_response,
                reply_to=reply_to,
                result=result,
                content=content,
                final=bool(metadata and metadata.get("notify")),
            )
            return result
    
        except Exception as e:  # pragma: no cover - defensive logging
            logger.error("[%s] Failed to send Discord message: %s", self.name, e, exc_info=True)
            result = SendResult(success=False, error=str(e))
            await asyncio.to_thread(
                self._record_discord_response,
                reply_to=reply_to,
                result=result,
                content=content,
                final=bool(metadata and metadata.get("notify")),
            )
            return result
    
    async def _send_to_forum(self, forum_channel: Any, content: str) -> SendResult:
        """Create a thread post in a forum channel with the message as starter content.
    
        Forum channels (type 15) don't support direct messages.  Instead we
        POST to /channels/{forum_id}/threads with a thread name derived from
        the first line of the message.  Any follow-up chunk failures are
        reported in ``raw_response['warnings']`` so the caller can surface
        partial-send issues.
        """
        from tools.send_message_tool import _derive_forum_thread_name
    
        formatted = self.format_message(content)
        chunks = self.truncate_message(formatted, self.MAX_MESSAGE_LENGTH)
    
        thread_name = _derive_forum_thread_name(content)
    
        starter_content = chunks[0] if chunks else thread_name
    
        try:
            thread = await forum_channel.create_thread(
                name=thread_name,
                content=starter_content,
            )
        except Exception as e:
            logger.error("[%s] Failed to create forum thread in %s: %s", self.name, forum_channel.id, e)
            return SendResult(success=False, error=f"Forum thread creation failed: {e}")
    
        thread_channel = thread if hasattr(thread, "send") else getattr(thread, "thread", None)
        thread_id = str(getattr(thread_channel, "id", getattr(thread, "id", "")))
        starter_msg = getattr(thread, "message", None)
        message_id = str(getattr(starter_msg, "id", thread_id)) if starter_msg else thread_id
    
        # Send remaining chunks into the newly created thread.  Track any
        # per-chunk failures so the caller sees partial-send outcomes.
        message_ids = [message_id]
        warnings: list[str] = []
        for chunk in chunks[1:]:
            try:
                msg = await thread_channel.send(content=chunk)
                message_ids.append(str(msg.id))
            except Exception as e:
                warning = f"Failed to send follow-up chunk to forum thread {thread_id}: {e}"
                logger.warning("[%s] %s", self.name, warning)
                warnings.append(warning)
    
        raw_response: Dict[str, Any] = {"message_ids": message_ids, "thread_id": thread_id}
        if warnings:
            raw_response["warnings"] = warnings
    
        return SendResult(
            success=True,
            message_id=message_ids[0],
            raw_response=raw_response,
        )
    
    async def _forum_post_file(
        self,
        forum_channel: Any,
        *,
        thread_name: Optional[str] = None,
        content: str = "",
        file: Any = None,
        files: Optional[list] = None,
    ) -> SendResult:
        """Create a forum thread whose starter message carries file attachments.
    
        Used by the send_voice / send_image_file / send_document paths when
        the target channel is a forum (type 15).  ``create_thread`` on a
        ForumChannel accepts the same file/files/content kwargs as
        ``channel.send``, creating the thread and starter message atomically.
        """
        from tools.send_message_tool import _derive_forum_thread_name
    
        if not thread_name:
            # Prefer the text content, fall back to the first attached
            # filename, fall back to the generic default.
            hint = content or ""
            if not hint.strip():
                if file is not None:
                    hint = getattr(file, "filename", "") or ""
                elif files:
                    hint = getattr(files[0], "filename", "") or ""
            thread_name = _derive_forum_thread_name(hint) if hint.strip() else "New Post"
    
        kwargs: Dict[str, Any] = {"name": thread_name}
        if content:
            kwargs["content"] = content
        if file is not None:
            kwargs["file"] = file
        if files:
            kwargs["files"] = files
    
        try:
            thread = await forum_channel.create_thread(**kwargs)
        except Exception as e:
            logger.error(
                "[%s] Failed to create forum thread with file in %s: %s",
                self.name,
                getattr(forum_channel, "id", "?"),
                e,
            )
            return SendResult(success=False, error=f"Forum thread creation failed: {e}")
    
        thread_channel = thread if hasattr(thread, "send") else getattr(thread, "thread", None)
        thread_id = str(getattr(thread_channel, "id", getattr(thread, "id", "")))
        starter_msg = getattr(thread, "message", None)
        message_id = str(getattr(starter_msg, "id", thread_id)) if starter_msg else thread_id
    
        return SendResult(
            success=True,
            message_id=message_id,
            raw_response={"thread_id": thread_id},
        )
    
    async def edit_message(
        self,
        chat_id: str,
        message_id: str,
        content: str,
        *,
        finalize: bool = False,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Edit a Discord message without losing over-limit final content.

        Streaming previews stay on the original message and saturate at one
        Discord-sized chunk.  Final edits split across continuations and
        return the last visible message id so the shared stream consumer can
        preserve a single linear response.
        """
        if not self._client:
            return SendResult(success=False, error="Not connected")
        try:
            channel = self._client.get_channel(int(chat_id))
            if not channel:
                channel = await self._client.fetch_channel(int(chat_id))
            msg = await channel.fetch_message(int(message_id))
            formatted = self.format_message(content)

            preview_key = (str(chat_id), str(message_id))
            saturated_preview = False
            if finalize:
                self._last_overflow_preview.pop(preview_key, None)

            if len(formatted) > self.MAX_MESSAGE_LENGTH:
                if finalize:
                    result = await self._edit_overflow_split(
                        channel,
                        msg,
                        message_id,
                        content,
                    )
                    await self._record_final_discord_edit(
                        result=result,
                        metadata=metadata,
                        content=content,
                    )
                    return result
                formatted = self.truncate_message(
                    formatted,
                    self.MAX_MESSAGE_LENGTH,
                )[0]
                saturated_preview = True
                if self._last_overflow_preview.get(preview_key) == formatted:
                    return SendResult(success=True, message_id=message_id)
            elif not finalize:
                self._last_overflow_preview.pop(preview_key, None)

            try:
                await msg.edit(content=formatted)
                if saturated_preview:
                    self._last_overflow_preview[preview_key] = formatted
            except Exception as edit_error:
                if not self._is_length_overflow_error(edit_error):
                    raise
                if finalize:
                    result = await self._edit_overflow_split(
                        channel,
                        msg,
                        message_id,
                        content,
                    )
                    await self._record_final_discord_edit(
                        result=result,
                        metadata=metadata,
                        content=content,
                    )
                    return result
                truncated = self.truncate_message(
                    formatted,
                    self.MAX_MESSAGE_LENGTH,
                )[0]
                if self._last_overflow_preview.get(preview_key) == truncated:
                    return SendResult(success=True, message_id=message_id)
                await msg.edit(content=truncated)
                self._last_overflow_preview[preview_key] = truncated

            result = SendResult(success=True, message_id=message_id)
            if finalize:
                await self._record_final_discord_edit(
                    result=result,
                    metadata=metadata,
                    content=content,
                )
            return result
        except Exception as e:  # pragma: no cover - defensive logging
            logger.error(
                "[%s] Failed to edit Discord message %s: %s",
                self.name,
                message_id,
                e,
            )
            return SendResult(success=False, error=str(e))

    async def _record_final_discord_edit(
        self,
        *,
        result: SendResult,
        metadata: Optional[Dict[str, Any]],
        content: str,
    ) -> None:
        """Persist final-delivery state without blocking the event loop."""
        raw_response = result.raw_response
        complete = not (
            isinstance(raw_response, dict)
            and raw_response.get("partial_overflow")
        )
        await asyncio.to_thread(
            self._record_discord_response,
            reply_to=(metadata or {}).get("reply_to_message_id"),
            result=result,
            content=content,
            final=complete,
        )

    @staticmethod
    def _is_length_overflow_error(error: Exception) -> bool:
        """Return whether Discord rejected text specifically for length."""
        text = str(error).lower()
        return "error code: 50035" in text and (
            "2000 or fewer" in text or "fewer in length" in text
        )

    async def _edit_overflow_split(
        self,
        channel: Any,
        message: Any,
        message_id: str,
        content: str,
    ) -> SendResult:
        """Deliver an oversized final edit across the original and replies."""
        formatted = self.format_message(content)
        chunks = self.truncate_message(formatted, self.MAX_MESSAGE_LENGTH)
        if len(chunks) <= 1:
            await message.edit(content=chunks[0] if chunks else formatted)
            return SendResult(success=True, message_id=message_id)

        try:
            await message.edit(content=chunks[0])
        except Exception as error:
            logger.error(
                "[%s] Overflow split first-chunk edit failed: %s",
                self.name,
                error,
            )
            return SendResult(success=False, error=str(error))

        continuation_ids: list[str] = []
        delivered_chunks = [chunks[0]]
        previous_message = message
        for chunk in chunks[1:]:
            reference = None
            if hasattr(previous_message, "to_reference"):
                try:
                    reference = previous_message.to_reference(
                        fail_if_not_exists=False,
                    )
                except Exception:
                    reference = None
            try:
                sent = await channel.send(content=chunk, reference=reference)
            except Exception as anchored_error:
                logger.warning(
                    "[%s] Overflow continuation rejected its reply anchor "
                    "(%s); retrying without one",
                    self.name,
                    anchored_error,
                )
                try:
                    sent = await channel.send(content=chunk, reference=None)
                except Exception as retry_error:
                    delivered_prefix = "".join(
                        re.sub(r" \(\d+/\d+\)$", "", delivered)
                        for delivered in delivered_chunks
                    )
                    last_id = (
                        continuation_ids[-1]
                        if continuation_ids
                        else message_id
                    )
                    logger.warning(
                        "[%s] Overflow split stopped at %d/%d chunks: %s",
                        self.name,
                        len(delivered_chunks),
                        len(chunks),
                        retry_error,
                    )
                    return SendResult(
                        success=False,
                        message_id=last_id,
                        error="overflow_continuation_failed",
                        retryable=True,
                        raw_response={
                            "partial_overflow": True,
                            "delivered_chunks": len(delivered_chunks),
                            "total_chunks": len(chunks),
                            "last_message_id": last_id,
                            "delivered_prefix": delivered_prefix,
                            "continuation_message_ids": tuple(
                                continuation_ids
                            ),
                        },
                        continuation_message_ids=tuple(continuation_ids),
                    )
            continuation_ids.append(str(sent.id))
            delivered_chunks.append(chunk)
            previous_message = sent

        last_id = continuation_ids[-1]
        self._last_self_message_id[str(channel.id)] = last_id
        return SendResult(
            success=True,
            message_id=last_id,
            continuation_message_ids=tuple(continuation_ids),
        )
    
    async def _send_file_attachment(
        self,
        chat_id: str,
        file_path: str,
        caption: Optional[str] = None,
        file_name: Optional[str] = None,
    ) -> SendResult:
        """Send a local file as a Discord attachment.
    
        Forum channels (type 15) get a new thread whose starter message
        carries the file — they reject direct POST /messages.
        """
        if not self._client:
            return SendResult(success=False, error="Not connected")
    
        channel = self._client.get_channel(int(chat_id))
        if not channel:
            channel = await self._client.fetch_channel(int(chat_id))
        if not channel:
            return SendResult(success=False, error=f"Channel {chat_id} not found")
    
        filename = file_name or os.path.basename(file_path)
        with open(file_path, "rb") as fh:
            file = discord.File(fh, filename=filename)
            if self._is_forum_parent(channel):
                return await self._forum_post_file(
                    channel,
                    content=(caption or "").strip(),
                    file=file,
                )
            msg = await channel.send(content=caption if caption else None, file=file)
        return SendResult(success=True, message_id=str(msg.id))
    
    async def send_multiple_images(
        self,
        chat_id: str,
        images: List[Tuple[str, str]],
        metadata: Optional[Dict[str, Any]] = None,
        human_delay: float = 0.0,
    ) -> None:
        """Send a batch of images as a single Discord message with multiple attachments.
    
        Discord permits up to 10 file attachments per message. Batches are
        chunked accordingly. URL images are downloaded into memory and
        uploaded as inline attachments (same pattern as ``send_image`` so
        they render inline, not as bare links). Local files are opened
        directly. On per-chunk failure the remaining images in that chunk
        fall back to the base per-image loop.
        """
        if not self._client:
            return
        if not images:
            return
    
        try:
            import discord as _discord_mod
            import io as _io
            from urllib.parse import unquote as _unquote
        except Exception:  # pragma: no cover
            await super().send_multiple_images(chat_id, images, metadata, human_delay)
            return
    
        try:
            channel = self._client.get_channel(int(chat_id))
            if not channel:
                channel = await self._client.fetch_channel(int(chat_id))
            if not channel:
                logger.warning("[%s] Channel %s not found for multi-image send", self.name, chat_id)
                return
        except Exception as e:
            logger.warning("[%s] Failed to resolve channel for multi-image send: %s", self.name, e)
            await super().send_multiple_images(chat_id, images, metadata, human_delay)
            return
    
        CHUNK = 10
        chunks = [images[i:i + CHUNK] for i in range(0, len(images), CHUNK)]
    
        for chunk_idx, chunk in enumerate(chunks):
            if human_delay > 0 and chunk_idx > 0:
                await asyncio.sleep(human_delay)
    
            files: List[Any] = []
            captions: List[str] = []
            aiohttp_session = None
            try:
                for image_url, alt_text in chunk:
                    if alt_text:
                        captions.append(alt_text)
                    if image_url.startswith("file://"):
                        local_path = _unquote(image_url[7:])
                        if not os.path.exists(local_path):
                            logger.warning("[%s] Skipping missing image: %s", self.name, local_path)
                            continue
                        files.append(_discord_mod.File(local_path, filename=os.path.basename(local_path)))
                    else:
                        if not is_safe_url(image_url):
                            logger.warning("[%s] Blocked unsafe image URL in batch", self.name)
                            continue
                        # Download to BytesIO so it renders inline
                        try:
                            import aiohttp as _aiohttp
                            from channels.platforms.base import resolve_proxy_url, proxy_kwargs_for_aiohttp
                            _proxy = resolve_proxy_url(platform_env_var="DISCORD_PROXY")
                            _sess_kw, _req_kw = proxy_kwargs_for_aiohttp(_proxy)
                            if aiohttp_session is None:
                                aiohttp_session = _aiohttp.ClientSession(**_sess_kw)
                            async with aiohttp_session.get(
                                image_url, timeout=_aiohttp.ClientTimeout(total=30), **_req_kw,
                            ) as resp:
                                if resp.status != 200:
                                    logger.warning(
                                        "[%s] Failed to download image (HTTP %d) in batch: %s",
                                        self.name, resp.status, image_url[:80],
                                    )
                                    continue
                                data = await resp.read()
                                ct = resp.headers.get("content-type", "image/png")
                                ext = "png"
                                if "jpeg" in ct or "jpg" in ct:
                                    ext = "jpg"
                                elif "gif" in ct:
                                    ext = "gif"
                                elif "webp" in ct:
                                    ext = "webp"
                                files.append(_discord_mod.File(_io.BytesIO(data), filename=f"image_{len(files)}.{ext}"))
                        except Exception as dl_err:
                            logger.warning("[%s] Download failed for %s: %s", self.name, image_url[:80], dl_err)
                            continue
    
                if not files:
                    continue
    
                # Use the first caption if any (Discord only has one message body for the group)
                content = captions[0] if captions else None
                logger.info(
                    "[%s] Sending %d image(s) as single Discord message (chunk %d/%d)",
                    self.name, len(files), chunk_idx + 1, len(chunks),
                )
    
                if self._is_forum_parent(channel):
                    await self._forum_post_file(
                        channel,
                        content=(content or "").strip(),
                        files=files,
                    )
                else:
                    await channel.send(content=content, files=files)
            except Exception as e:
                logger.warning(
                    "[%s] Multi-image Discord send failed (chunk %d/%d), falling back to per-image: %s",
                    self.name, chunk_idx + 1, len(chunks), e,
                    exc_info=True,
                )
                await super().send_multiple_images(chat_id, chunk, metadata, human_delay=human_delay)
            finally:
                if aiohttp_session is not None:
                    try:
                        await aiohttp_session.close()
                    except Exception:
                        pass
    
    async def play_tts(
        self,
        chat_id: str,
        audio_path: str,
        **kwargs,
    ) -> SendResult:
        """Play auto-TTS audio.
    
        When the bot is in a voice channel for this chat's guild, play
        directly in the VC instead of sending as a file attachment.
        """
        for gid, text_ch_id in self._voice_text_channels.items():
            if str(text_ch_id) == str(chat_id) and self.is_in_voice_channel(gid):
                logger.info("[%s] Playing TTS in voice channel (guild=%d)", self.name, gid)
                success = await self.play_in_voice_channel(gid, audio_path)
                return SendResult(success=success)
        return await self.send_voice(chat_id=chat_id, audio_path=audio_path, **kwargs)
    
    async def send_voice(
        self,
        chat_id: str,
        audio_path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        **kwargs,
    ) -> SendResult:
        """Send audio as a Discord file attachment."""
        try:
            import io
    
            channel = self._client.get_channel(int(chat_id))
            if not channel:
                channel = await self._client.fetch_channel(int(chat_id))
            if not channel:
                return SendResult(success=False, error=f"Channel {chat_id} not found")
    
            if not os.path.exists(audio_path):
                return SendResult(success=False, error=f"Audio file not found: {audio_path}")
    
            filename = os.path.basename(audio_path)
    
            with open(audio_path, "rb") as f:
                file_data = f.read()
    
            # Forum channels (type 15) reject direct POST /messages — the
            # native voice flag path also targets /messages so it would fail
            # too.  Create a thread post with the audio as the starter
            # attachment instead.
            if self._is_forum_parent(channel):
                forum_file = discord.File(io.BytesIO(file_data), filename=filename)
                return await self._forum_post_file(
                    channel,
                    content=(caption or "").strip(),
                    file=forum_file,
                )
    
            # Try sending as a native voice message via raw API (flags=8192).
            try:
                import base64
    
                duration_secs = 5.0
                try:
                    from mutagen.oggopus import OggOpus
                    info = OggOpus(audio_path)
                    duration_secs = info.info.length
                except Exception:
                    duration_secs = max(1.0, len(file_data) / 2000.0)
    
                waveform_bytes = bytes([128] * 256)
                waveform_b64 = base64.b64encode(waveform_bytes).decode()
    
                import json as _json
                payload = _json.dumps({
                    "flags": 8192,
                    "attachments": [{
                        "id": "0",
                        "filename": "voice-message.ogg",
                        "duration_secs": round(duration_secs, 2),
                        "waveform": waveform_b64,
                    }],
                })
                form = [
                    {"name": "payload_json", "value": payload},
                    {
                        "name": "files[0]",
                        "value": file_data,
                        "filename": "voice-message.ogg",
                        "content_type": "audio/ogg",
                    },
                ]
                msg_data = await self._client.http.request(
                    discord.http.Route("POST", "/channels/{channel_id}/messages", channel_id=channel.id),
                    form=form,
                )
                return SendResult(success=True, message_id=str(msg_data["id"]))
            except Exception as voice_err:
                logger.debug("Voice message flag failed, falling back to file: %s", voice_err)
                file = discord.File(io.BytesIO(file_data), filename=filename)
                msg = await channel.send(file=file)
                return SendResult(success=True, message_id=str(msg.id))
        except Exception as e:  # pragma: no cover - defensive logging
            logger.error("[%s] Failed to send audio, falling back to base adapter: %s", self.name, e, exc_info=True)
            return await super().send_voice(chat_id, audio_path, caption, reply_to, metadata=metadata)
    
    async def send_image_file(
        self,
        chat_id: str,
        image_path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Send a local image file natively as a Discord file attachment."""
        try:
            return await self._send_file_attachment(chat_id, image_path, caption)
        except FileNotFoundError:
            return SendResult(success=False, error=f"Image file not found: {image_path}")
        except Exception as e:  # pragma: no cover - defensive logging
            logger.error("[%s] Failed to send local image, falling back to base adapter: %s", self.name, e, exc_info=True)
            return await super().send_image_file(chat_id, image_path, caption, reply_to, metadata=metadata)
    
    async def send_image(
        self,
        chat_id: str,
        image_url: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Send an image natively as a Discord file attachment."""
        if not self._client:
            return SendResult(success=False, error="Not connected")
    
        if not is_safe_url(image_url):
            logger.warning("[%s] Blocked unsafe image URL during Discord send_image", self.name)
            return await super().send_image(chat_id, image_url, caption, reply_to, metadata=metadata)
    
        try:
            import aiohttp
    
            channel = self._client.get_channel(int(chat_id))
            if not channel:
                channel = await self._client.fetch_channel(int(chat_id))
            if not channel:
                return SendResult(success=False, error=f"Channel {chat_id} not found")
    
            # Download the image and send as a Discord file attachment
            # (Discord renders attachments inline, unlike plain URLs)
            from channels.platforms.base import resolve_proxy_url, proxy_kwargs_for_aiohttp
            _proxy = resolve_proxy_url(platform_env_var="DISCORD_PROXY")
            _sess_kw, _req_kw = proxy_kwargs_for_aiohttp(_proxy)
            async with aiohttp.ClientSession(**_sess_kw) as session:
                async with session.get(image_url, timeout=aiohttp.ClientTimeout(total=30), **_req_kw) as resp:
                    if resp.status != 200:
                        raise Exception(f"Failed to download image: HTTP {resp.status}")
    
                    image_data = await resp.read()
    
                    # Determine filename from URL or content type
                    content_type = resp.headers.get("content-type", "image/png")
                    ext = "png"
                    if "jpeg" in content_type or "jpg" in content_type:
                        ext = "jpg"
                    elif "gif" in content_type:
                        ext = "gif"
                    elif "webp" in content_type:
                        ext = "webp"
    
                    import io
                    file = discord.File(io.BytesIO(image_data), filename=f"image.{ext}")
    
                    if self._is_forum_parent(channel):
                        return await self._forum_post_file(
                            channel,
                            content=(caption or "").strip(),
                            file=file,
                        )
    
                    msg = await channel.send(
                        content=caption if caption else None,
                        file=file,
                    )
                    return SendResult(success=True, message_id=str(msg.id))
    
        except ImportError:
            logger.warning(
                "[%s] aiohttp not installed, falling back to URL. Run: pip install aiohttp",
                self.name,
                exc_info=True,
            )
            return await super().send_image(chat_id, image_url, caption, reply_to)
        except Exception as e:  # pragma: no cover - defensive logging
            logger.error(
                "[%s] Failed to send image attachment, falling back to URL: %s",
                self.name,
                e,
                exc_info=True,
            )
            return await super().send_image(chat_id, image_url, caption, reply_to)
    
    async def send_animation(
        self,
        chat_id: str,
        animation_url: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Send an animated GIF natively as a Discord file attachment."""
        if not self._client:
            return SendResult(success=False, error="Not connected")
    
        if not is_safe_url(animation_url):
            logger.warning("[%s] Blocked unsafe animation URL during Discord send_animation", self.name)
            return await super().send_animation(chat_id, animation_url, caption, reply_to, metadata=metadata)
    
        try:
            import aiohttp
    
            channel = self._client.get_channel(int(chat_id))
            if not channel:
                channel = await self._client.fetch_channel(int(chat_id))
            if not channel:
                return SendResult(success=False, error=f"Channel {chat_id} not found")
    
            # Download the GIF and send as a Discord file attachment
            # (Discord renders .gif attachments as auto-playing animations inline)
            from channels.platforms.base import resolve_proxy_url, proxy_kwargs_for_aiohttp
            _proxy = resolve_proxy_url(platform_env_var="DISCORD_PROXY")
            _sess_kw, _req_kw = proxy_kwargs_for_aiohttp(_proxy)
            async with aiohttp.ClientSession(**_sess_kw) as session:
                async with session.get(animation_url, timeout=aiohttp.ClientTimeout(total=30), **_req_kw) as resp:
                    if resp.status != 200:
                        raise Exception(f"Failed to download animation: HTTP {resp.status}")
    
                    animation_data = await resp.read()
    
                    import io
                    file = discord.File(io.BytesIO(animation_data), filename="animation.gif")
    
                    if self._is_forum_parent(channel):
                        return await self._forum_post_file(
                            channel,
                            content=(caption or "").strip(),
                            file=file,
                        )
    
                    msg = await channel.send(
                        content=caption if caption else None,
                        file=file,
                    )
                    return SendResult(success=True, message_id=str(msg.id))
    
        except ImportError:
            logger.warning(
                "[%s] aiohttp not installed, falling back to URL. Run: pip install aiohttp",
                self.name,
                exc_info=True,
            )
            return await super().send_animation(chat_id, animation_url, caption, reply_to, metadata=metadata)
        except Exception as e:  # pragma: no cover - defensive logging
            logger.error(
                "[%s] Failed to send animation attachment, falling back to URL: %s",
                self.name,
                e,
                exc_info=True,
            )
            return await super().send_animation(chat_id, animation_url, caption, reply_to, metadata=metadata)
    
    async def send_video(
        self,
        chat_id: str,
        video_path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Send a local video file natively as a Discord attachment."""
        try:
            return await self._send_file_attachment(chat_id, video_path, caption)
        except FileNotFoundError:
            return SendResult(success=False, error=f"Video file not found: {video_path}")
        except Exception as e:  # pragma: no cover - defensive logging
            logger.error("[%s] Failed to send local video, falling back to base adapter: %s", self.name, e, exc_info=True)
            return await super().send_video(chat_id, video_path, caption, reply_to, metadata=metadata)
    
    async def send_document(
        self,
        chat_id: str,
        file_path: str,
        caption: Optional[str] = None,
        file_name: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Send an arbitrary file natively as a Discord attachment."""
        try:
            return await self._send_file_attachment(chat_id, file_path, caption, file_name=file_name)
        except FileNotFoundError:
            return SendResult(success=False, error=f"File not found: {file_path}")
        except Exception as e:  # pragma: no cover - defensive logging
            logger.error("[%s] Failed to send document, falling back to base adapter: %s", self.name, e, exc_info=True)
            return await super().send_document(chat_id, file_path, caption, file_name, reply_to, metadata=metadata)
