from __future__ import annotations

import asyncio
import logging
import os
import re
from typing import Any

try:
    import discord
except ImportError:  # pragma: no cover - optional platform dependency
    discord = None

from channels.platforms.base import MessageEvent, MessageType

logger = logging.getLogger(__name__)


def _discord_public_attr(name: str, fallback: Any = None) -> Any:
    import sys

    public_module = sys.modules.get("channels.platforms.discord")
    if public_module is None:
        return fallback
    return getattr(public_module, name, fallback)


class DiscordInboundMixin:
    async def _read_attachment_bytes(
        self,
        att,
        *,
        media_type: str = "media",
    ) -> Optional[bytes]:
        """Read an attachment via discord.py's authenticated bot session.
    
        Returns the raw bytes on success, or ``None`` if ``att`` doesn't
        expose a callable ``read()`` or the read itself fails. Callers
        should treat ``None`` as a signal to fall back to the URL-based
        downloaders.
    
        Oversized attachments (per ``gateway.max_inbound_media_bytes``) raise
        ``ValueError`` BEFORE the bytes are pulled into memory when Discord
        reports the size up front, so a hostile upload can't OOM the gateway.
        """
        attachment_size = getattr(att, "size", None)
        if attachment_size:
            _discord_public_attr("validate_inbound_media_size")( int(attachment_size), media_type=media_type)
    
        reader = getattr(att, "read", None)
        if reader is None or not callable(reader):
            return None
        try:
            raw_bytes = await reader()
        except Exception as e:
            logger.warning(
                "[Discord] Authenticated attachment read failed for %s: %s",
                getattr(att, "filename", None) or getattr(att, "url", "<unknown>"),
                e,
            )
            return None
        _discord_public_attr("validate_inbound_media_size")( len(raw_bytes), media_type=media_type)
        return raw_bytes
    
    async def _cache_discord_image(self, att, ext: str) -> str:
        """Cache a Discord image attachment to local disk.
    
        Primary path: ``att.read()`` + ``cache_image_from_bytes``
        (authenticated, no SSRF gate).
    
        Fallback: ``cache_image_from_url`` (plain httpx, SSRF-gated).
        """
        raw_bytes = await self._read_attachment_bytes(att, media_type="image")
        if raw_bytes is not None:
            try:
                return _discord_public_attr("cache_image_from_bytes")( raw_bytes, ext=ext)
            except Exception as e:
                logger.debug(
                    "[Discord] cache_image_from_bytes rejected att.read() data; falling back to URL: %s",
                    e,
                )
        return await _discord_public_attr("cache_image_from_url")( att.url, ext=ext)
    
    async def _cache_discord_audio(self, att, ext: str) -> str:
        """Cache a Discord audio attachment to local disk.
    
        Primary path: ``att.read()`` + ``cache_audio_from_bytes``
        (authenticated, no SSRF gate).
    
        Fallback: ``cache_audio_from_url`` (plain httpx, SSRF-gated).
        """
        raw_bytes = await self._read_attachment_bytes(att, media_type="audio")
        if raw_bytes is not None:
            try:
                return _discord_public_attr("cache_audio_from_bytes")( raw_bytes, ext=ext)
            except Exception as e:
                logger.debug(
                    "[Discord] cache_audio_from_bytes failed; falling back to URL: %s",
                    e,
                )
        return await _discord_public_attr("cache_audio_from_url")( att.url, ext=ext)
    
    async def _cache_discord_document(self, att, ext: str) -> bytes:
        """Download a Discord document attachment and return the raw bytes.
    
        Primary path: ``att.read()`` (authenticated, no SSRF gate).
    
        Fallback: SSRF-gated ``aiohttp`` download. This closes the gap
        where the old document path made raw ``aiohttp.ClientSession``
        requests with no safety check (#11345). The caller is responsible
        for passing the returned bytes to ``cache_document_from_bytes``
        (and, where applicable, for injecting text content).
        """
        raw_bytes = await self._read_attachment_bytes(att, media_type="document")
        if raw_bytes is not None:
            return raw_bytes
    
        # Fallback: SSRF-gated URL download.
        if not _discord_public_attr("is_safe_url")( att.url):
            raise ValueError(
                f"Blocked unsafe attachment URL (SSRF protection): {att.url}"
            )
        import aiohttp
        from channels.platforms.base import resolve_proxy_url, proxy_kwargs_for_aiohttp
        _proxy = resolve_proxy_url(platform_env_var="DISCORD_PROXY")
        _sess_kw, _req_kw = proxy_kwargs_for_aiohttp(_proxy)
        async with aiohttp.ClientSession(**_sess_kw) as session:
            async with session.get(
                att.url,
                timeout=aiohttp.ClientTimeout(total=30),
                **_req_kw,
            ) as resp:
                if resp.status != 200:
                    raise Exception(f"HTTP {resp.status}")
                return await resp.read()
    
    async def _handle_message(self, message: DiscordMessage) -> None:
        """Handle incoming Discord messages."""
        # In server channels (not DMs), require the bot to be @mentioned
        # UNLESS the channel is in the free-response list or the message is
        # in a thread where the bot has already participated.
        #
        # Config (all settable via discord.* in config.yaml or DISCORD_* env vars):
        #   discord.require_mention: Require @mention in server channels (default: true)
        #   discord.free_response_channels: Channel IDs where bot responds without mention
        #   discord.ignored_channels: Channel IDs where bot NEVER responds (even when mentioned)
        #   discord.allowed_channels: If set, bot ONLY responds in these channels (whitelist)
        #   discord.no_thread_channels: Channel IDs where bot responds directly without creating thread
        #   discord.auto_thread: Auto-create thread on @mention in channels (default: true)
    
        thread_id = None
        parent_channel_id = None
        is_thread = isinstance(message.channel, discord.Thread)
        if is_thread:
            thread_id = str(message.channel.id)
            parent_channel_id = self._get_parent_channel_id(message.channel)
    
        is_voice_linked_channel = False
    
        # Save mention-stripped text before auto-threading since create_thread()
        # can clobber message.content, breaking /command detection in channels.
        raw_content = message.content.strip()
        normalized_content = raw_content
        mention_prefix = False
    
        snapshot_attachments = []
        if hasattr(message, "message_snapshots") and message.message_snapshots:
            snapshot_text_parts = []
            for snap in message.message_snapshots:
                if getattr(snap, "content", None):
                    snapshot_text_parts.append(snap.content.strip())
                snapshot_attachments.extend(getattr(snap, "attachments", []) or [])
            if snapshot_text_parts and not raw_content:
                raw_content = "\n".join(snapshot_text_parts)
                normalized_content = raw_content
        if self._client.user and self._client.user in message.mentions:
            mention_prefix = True
            normalized_content = normalized_content.replace(f"<@{self._client.user.id}>", "").strip()
            normalized_content = normalized_content.replace(f"<@!{self._client.user.id}>", "").strip()
            message.content = normalized_content
        if not isinstance(message.channel, discord.DMChannel):
            channel_ids = {str(message.channel.id)}
            if parent_channel_id:
                channel_ids.add(parent_channel_id)
    
            # Check allowed channels - if set, only respond in these channels
            allowed_channels_raw = os.getenv("DISCORD_ALLOWED_CHANNELS", "")
            if allowed_channels_raw:
                allowed_channels = {ch.strip() for ch in allowed_channels_raw.split(",") if ch.strip()}
                if "*" not in allowed_channels and not (channel_ids & allowed_channels):
                    logger.debug("[%s] Ignoring message in non-allowed channel: %s", self.name, channel_ids)
                    return
    
            # Check ignored channels - never respond even when mentioned
            ignored_channels_raw = os.getenv("DISCORD_IGNORED_CHANNELS", "")
            ignored_channels = {ch.strip() for ch in ignored_channels_raw.split(",") if ch.strip()}
            if "*" in ignored_channels or (channel_ids & ignored_channels):
                logger.debug("[%s] Ignoring message in ignored channel: %s", self.name, channel_ids)
                return
    
            free_channels = self._discord_free_response_channels()
            if parent_channel_id:
                channel_ids.add(parent_channel_id)
    
            require_mention = self._discord_require_mention()
            # Voice-linked text channels act as free-response while voice is active.
            # Only the exact bound channel gets the exemption, not sibling threads.
            voice_linked_ids = {str(ch_id) for ch_id in self._voice_text_channels.values()}
            current_channel_id = str(message.channel.id)
            is_voice_linked_channel = current_channel_id in voice_linked_ids
            is_free_channel = (
                "*" in free_channels
                or bool(channel_ids & free_channels)
                or is_voice_linked_channel
            )
    
            # Skip the mention check if the message is in a thread where
            # the bot has previously participated (auto-created or replied in)
            # — UNLESS thread_require_mention is enabled, in which case threads
            # are gated the same as channels.  Useful when multiple bots share
            # a thread.
            in_bot_thread = (
                is_thread
                and thread_id in self._threads
                and not self._discord_thread_require_mention()
            )
    
            if require_mention and not is_free_channel and not in_bot_thread:
                if self._client.user not in message.mentions and not mention_prefix:
                    return
        # Auto-thread: when enabled, automatically create a thread for every
        # @mention in a text channel so each conversation is isolated (like Slack).
        # Messages already inside threads or DMs are unaffected.
        # no_thread_channels: channels where bot responds directly without thread.
        auto_threaded_channel = None
        if not is_thread and not isinstance(message.channel, discord.DMChannel):
            no_thread_channels_raw = os.getenv("DISCORD_NO_THREAD_CHANNELS", "")
            no_thread_channels = {ch.strip() for ch in no_thread_channels_raw.split(",") if ch.strip()}
            skip_thread = bool(channel_ids & no_thread_channels) or is_free_channel
            auto_thread = os.getenv("DISCORD_AUTO_THREAD", "true").lower() in {"true", "1", "yes"}
            is_reply_message = getattr(message, "type", None) == discord.MessageType.reply
            if auto_thread and not skip_thread and not is_voice_linked_channel and not is_reply_message:
                thread = await self._auto_create_thread(message)
                if thread:
                    parent_channel_id = str(message.channel.id)
                    is_thread = True
                    thread_id = str(thread.id)
                    auto_threaded_channel = thread
                    self._threads.mark(thread_id)
    
        all_attachments = list(message.attachments) + snapshot_attachments
    
        # Determine message type
        msg_type = MessageType.TEXT
        if normalized_content.startswith("/"):
            msg_type = MessageType.COMMAND
        elif all_attachments:
            _allow_any = self._discord_allow_any_attachment()
            # Check attachment types
            for att in all_attachments:
                if att.content_type:
                    if att.content_type.startswith("image/"):
                        msg_type = MessageType.PHOTO
                    elif att.content_type.startswith("video/"):
                        msg_type = MessageType.VIDEO
                    elif att.content_type.startswith("audio/"):
                        if self._is_discord_voice_message_attachment(att):
                            msg_type = MessageType.VOICE
                        else:
                            msg_type = MessageType.AUDIO
                    else:
                        doc_ext = ""
                        if att.filename:
                            _, doc_ext = os.path.splitext(att.filename)
                            doc_ext = doc_ext.lower()
                        if doc_ext in _discord_public_attr("SUPPORTED_DOCUMENT_TYPES") or _allow_any:
                            msg_type = MessageType.DOCUMENT
                    break
                elif _allow_any:
                    # No content_type at all (rare — discord usually fills it
                    # in). Treat as a document so downstream pipelines surface
                    # the path to the agent.
                    msg_type = MessageType.DOCUMENT
                    break
    
        # When auto-threading kicked in, route responses to the new thread
        effective_channel = auto_threaded_channel or message.channel
    
        # Determine chat type
        if isinstance(message.channel, discord.DMChannel):
            chat_type = "dm"
            chat_name = message.author.name
        elif is_thread:
            chat_type = "thread"
            chat_name = self._format_thread_chat_name(effective_channel)
        else:
            chat_type = "group"
            chat_name = getattr(message.channel, "name", str(message.channel.id))
            if hasattr(message.channel, "guild") and message.channel.guild:
                chat_name = f"{message.channel.guild.name} / #{chat_name}"
    
        # Get channel topic (if available - TextChannels have topics, DMs/threads don't).
        # For threads whose parent is a forum channel, inherit the parent's topic
        # so forum descriptions (e.g. project instructions) appear in the session context.
        chat_topic = self._get_effective_topic(message.channel, is_thread=is_thread)
    
        # Build source
        guild = getattr(message, "guild", None)
        source = self.build_source(
            chat_id=str(effective_channel.id),
            chat_name=chat_name,
            chat_type=chat_type,
            user_id=str(message.author.id),
            user_name=message.author.display_name,
            thread_id=thread_id,
            chat_topic=chat_topic,
            is_bot=getattr(message.author, "bot", False),
            guild_id=str(guild.id) if guild else None,
            parent_chat_id=parent_channel_id,
            message_id=str(message.id),
        )
    
        # Build media URLs -- download image attachments to local cache so the
        # vision tool can access them reliably (Discord CDN URLs can expire).
        media_urls = []
        media_types = []
        pending_text_injection: Optional[str] = None
        for att in all_attachments:
            content_type = att.content_type or "unknown"
            if content_type.startswith("image/"):
                try:
                    # Determine extension from content type (image/png -> .png)
                    ext = "." + content_type.split("/")[-1].split(";")[0]
                    if ext not in {".jpg", ".jpeg", ".png", ".gif", ".webp"}:
                        ext = ".jpg"
                    cached_path = await self._cache_discord_image(att, ext)
                    media_urls.append(cached_path)
                    media_types.append(content_type)
                    print(f"[Discord] Cached user image: {cached_path}", flush=True)
                except Exception as e:
                    print(f"[Discord] Failed to cache image attachment: {e}", flush=True)
                    # Fall back to the CDN URL if caching fails
                    media_urls.append(att.url)
                    media_types.append(content_type)
            elif content_type.startswith("audio/"):
                try:
                    ext = "." + content_type.split("/")[-1].split(";")[0]
                    if ext not in {".ogg", ".mp3", ".wav", ".webm", ".m4a"}:
                        ext = ".ogg"
                    cached_path = await self._cache_discord_audio(att, ext)
                    media_urls.append(cached_path)
                    media_types.append(content_type)
                    print(f"[Discord] Cached user audio: {cached_path}", flush=True)
                except Exception as e:
                    print(f"[Discord] Failed to cache audio attachment: {e}", flush=True)
                    media_urls.append(att.url)
                    media_types.append(content_type)
            else:
                # Document attachments: download, cache, and optionally inject text
                ext = ""
                if att.filename:
                    _, ext = os.path.splitext(att.filename)
                    ext = ext.lower()
                if not ext and content_type:
                    mime_to_ext = {v: k for k, v in _discord_public_attr("SUPPORTED_DOCUMENT_TYPES").items()}
                    ext = mime_to_ext.get(content_type, "")
                allow_any_attachment = self._discord_allow_any_attachment()
                in_allowlist = ext in _discord_public_attr("SUPPORTED_DOCUMENT_TYPES")
                if not in_allowlist and not allow_any_attachment:
                    logger.warning(
                        "[Discord] Unsupported document type '%s' (%s), skipping",
                        ext or "unknown", content_type,
                    )
                else:
                    max_doc_bytes = self._discord_max_attachment_bytes()
                    if max_doc_bytes and att.size and att.size > max_doc_bytes:
                        logger.warning(
                            "[Discord] Document too large (%s bytes > cap %s), skipping: %s",
                            att.size, max_doc_bytes, att.filename,
                        )
                    else:
                        try:
                            raw_bytes = await self._cache_discord_document(att, ext)
                            cached_path = _discord_public_attr("cache_document_from_bytes")( 
                                raw_bytes, att.filename or f"document{ext or '.bin'}"
                            )
                            if in_allowlist:
                                doc_mime = _discord_public_attr("SUPPORTED_DOCUMENT_TYPES")[ext]
                            else:
                                # allow_any_attachment path: untyped file. Use the
                                # source content_type if discord gave us one,
                                # otherwise fall back to octet-stream so the agent
                                # knows it's binary and reaches for terminal tools.
                                doc_mime = (
                                    content_type
                                    if content_type and content_type != "unknown"
                                    else "application/octet-stream"
                                )
                            media_urls.append(cached_path)
                            media_types.append(doc_mime)
                            logger.info(
                                "[Discord] Cached user %s: %s",
                                "document" if in_allowlist else "attachment",
                                cached_path,
                            )
                            # Inject text content for plain-text documents (capped at 100 KB)
                            MAX_TEXT_INJECT_BYTES = 100 * 1024
                            if in_allowlist and ext in {".md", ".txt", ".log"} and len(raw_bytes) <= MAX_TEXT_INJECT_BYTES:
                                try:
                                    text_content = raw_bytes.decode("utf-8")
                                    display_name = att.filename or f"document{ext}"
                                    display_name = re.sub(r'[^\w.\- ]', '_', display_name)
                                    injection = f"[Content of {display_name}]:\n{text_content}"
                                    if pending_text_injection:
                                        pending_text_injection = f"{pending_text_injection}\n\n{injection}"
                                    else:
                                        pending_text_injection = injection
                                except UnicodeDecodeError:
                                    pass
                            # NOTE: for the allow_any_attachment path we deliberately
                            # do NOT inject a path string here. ``gateway/run.py``
                            # already detects DOCUMENT-typed events with
                            # ``application/octet-stream`` MIME and emits a context
                            # note with the sandbox-translated cache path via
                            # ``to_agent_visible_cache_path()`` (important for
                            # Docker/Modal terminal backends).
                        except Exception as e:
                            logger.warning(
                                "[Discord] Failed to cache document %s: %s",
                                att.filename, e, exc_info=True,
                            )
    
        # Use normalized_content (saved before auto-threading) instead of message.content,
        # to detect /slash commands in channel messages.
        event_text = normalized_content
        if pending_text_injection:
            event_text = f"{pending_text_injection}\n\n{event_text}" if event_text else pending_text_injection
    
        # ── History backfill ─────────────────────────────────────────
        # When require_mention is active, the bot only processes messages
        # that @mention it.  Messages in the channel between bot turns are
        # invisible to the session transcript.  To recover that context,
        # fetch recent channel history and prepend it to the user message.
        #
        # The fetch window is: everything after the bot's last message in
        # the channel up to (but not including) the current trigger.  On
        # cold start (no prior bot message found), fetch the last N messages
        # and stop at the first self-message encountered.
        #
        # Threads naturally scope to thread-only history (channel.history()
        # on a thread returns only that thread's messages).  DMs are skipped
        # because every DM message triggers the bot — there's no mention gap
        # to fill; the session transcript already has everything.
        #
        # Per-user sessions also benefit: Alice's session is missing the
        # other-channel-participants' context, and her own messages from
        # before she mentioned the bot.  Backfill fills that gap.
        #
        # Messages that arrive while the bot is processing (between trigger
        # and response) are not captured — this is an accepted simplification
        # to keep the partition rule clean.
        _channel_context = None
        _is_dm = isinstance(message.channel, discord.DMChannel)
        if not _is_dm:
            _needed_mention = (
                require_mention
                and not is_free_channel
                and not in_bot_thread
            )
            _backfill_enabled = self._discord_history_backfill()
            if _needed_mention and _backfill_enabled:
                _backfill_text = await self._fetch_channel_context(
                    message.channel, before=message,
                )
                if _backfill_text:
                    _channel_context = _backfill_text
    
        # Defense-in-depth: prevent empty user messages from entering session
        # (can happen when user sends @mention-only with no other text).
        # When channel_context is present, a bare mention means "catch me up"
        # — the context IS the message, so skip the placeholder.
        if (not event_text or not event_text.strip()) and not _channel_context:
            event_text = "(The user sent a message with no text content)"
    
        _chan = message.channel
        _parent_id = str(getattr(_chan, "parent_id", "") or "")
        _chan_id = str(getattr(_chan, "id", ""))
        _skills = self._resolve_channel_skills(_chan_id, _parent_id or None)
        _channel_prompt = self._resolve_channel_prompt(_chan_id, _parent_id or None)
    
        reply_to_id = None
        reply_to_text = None
        if message.reference:
            reply_to_id = str(message.reference.message_id)
            if message.reference.resolved:
                reply_to_text = getattr(message.reference.resolved, "content", None) or None
    
        event = MessageEvent(
            text=event_text,
            message_type=msg_type,
            source=source,
            raw_message=message,
            message_id=str(message.id),
            media_urls=media_urls,
            media_types=media_types,
            reply_to_message_id=reply_to_id,
            reply_to_text=reply_to_text,
            timestamp=message.created_at,
            auto_skill=_skills,
            channel_prompt=_channel_prompt,
            channel_context=_channel_context,
        )
    
        # Track thread participation so the bot won't require @mention for
        # follow-up messages in threads it has already engaged in.
        if thread_id:
            self._threads.mark(thread_id)
    
        # Only batch plain text messages — commands, media, etc. dispatch
        # immediately since they won't be split by the Discord client.
        if msg_type == MessageType.TEXT and self._text_batch_delay_seconds > 0:
            self._enqueue_text_event(event)
        else:
            await self.handle_message(event)
    
    def _text_batch_key(self, event: MessageEvent) -> str:
        """Session-scoped key for text message batching."""
        from channels.session_identity import build_session_key
        return build_session_key(
            event.source,
            group_sessions_per_user=self.config.extra.get("group_sessions_per_user", True),
            thread_sessions_per_user=self.config.extra.get("thread_sessions_per_user", False),
        )
    
    def _enqueue_text_event(self, event: MessageEvent) -> None:
        """Buffer a text event and reset the flush timer.
    
        When Discord splits a long user message at 2000 chars, the chunks
        arrive within a few hundred milliseconds.  This merges them into
        a single event before dispatching.
        """
        key = self._text_batch_key(event)
        existing = self._pending_text_batches.get(key)
        chunk_len = len(event.text or "")
        if existing is None:
            event._last_chunk_len = chunk_len  # type: ignore[attr-defined]
            self._pending_text_batches[key] = event
        else:
            if event.text:
                existing.text = f"{existing.text}\n{event.text}" if existing.text else event.text
            existing._last_chunk_len = chunk_len  # type: ignore[attr-defined]
            if event.media_urls:
                existing.media_urls.extend(event.media_urls)
                existing.media_types.extend(event.media_types)
    
        prior_task = self._pending_text_batch_tasks.get(key)
        if prior_task and not prior_task.done():
            prior_task.cancel()
        self._pending_text_batch_tasks[key] = asyncio.create_task(
            self._flush_text_batch(key)
        )
    
    async def _flush_text_batch(self, key: str) -> None:
        """Wait for the quiet period then dispatch the aggregated text.
    
        Uses a longer delay when the latest chunk is near Discord's 2000-char
        split point, since a continuation chunk is almost certain.
        """
        current_task = asyncio.current_task()
        try:
            pending = self._pending_text_batches.get(key)
            last_len = getattr(pending, "_last_chunk_len", 0) if pending else 0
            if last_len >= self._SPLIT_THRESHOLD:
                delay = self._text_batch_split_delay_seconds
            else:
                delay = self._text_batch_delay_seconds
            await asyncio.sleep(delay)
            event = self._pending_text_batches.pop(key, None)
            if not event:
                return
            logger.info(
                "[Discord] Flushing text batch %s (%d chars)",
                key, len(event.text or ""),
            )
            # Shield the downstream dispatch so that a subsequent chunk
            # arriving while handle_message is mid-flight cannot cancel
            # the running agent turn.  _enqueue_text_event always cancels
            # the prior flush task when a new chunk lands; without this
            # shield, CancelledError would propagate from our task down
            # into handle_message → the agent's streaming request,
            # aborting the response the user was waiting on.  The new
            # chunk is handled by the fresh flush task regardless.
            await asyncio.shield(self.handle_message(event))
        except asyncio.CancelledError:
            # Only reached if cancel landed before the pop — the shielded
            # handle_message is unaffected either way.  Let the task exit
            # cleanly so the finally block cleans up.
            pass
        finally:
            if self._pending_text_batch_tasks.get(key) is current_task:
                self._pending_text_batch_tasks.pop(key, None)
