"""Default delivery/media helpers for BasePlatformAdapter."""

from __future__ import annotations

import asyncio
import logging
import os
import re
from abc import abstractmethod
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from channels.platforms.base_media_cache import safe_url_for_log, validate_media_delivery_path
from channels.platforms.base_models import EphemeralReply, SendResult

logger = logging.getLogger(__name__)

_AUDIO_EXTS = frozenset({'.ogg', '.opus', '.mp3', '.wav', '.m4a', '.flac'})
_TELEGRAM_AUDIO_ATTACHMENT_EXTS = frozenset({'.mp3', '.m4a'})
_TELEGRAM_VOICE_EXTS = frozenset({'.ogg', '.opus'})

MEDIA_TAG_CLEANUP_RE = re.compile('[`"\']?MEDIA:\\s*(?P<path>`[^`\\n]+`|"[^"\\n]+"|\'[^\'\\n]+\'|(?:~/|/)\\S+(?:[^\\S\\n]+\\S+)*?\\.(?:png|jpe?g|gif|webp|mp4|mov|avi|mkv|webm|ogg|opus|mp3|wav|m4a|flac|epub|pdf|zip|rar|7z|docx?|xlsx?|pptx?|txt|csv|apk|ipa)(?=[\\s`"\',;:)\\]}]|$))[`"\']?')


def _platform_name(platform) -> str:
    value = getattr(platform, "value", platform)
    return str(value or "").lower()


def should_send_media_as_audio(platform, ext: str, is_voice: bool = False) -> bool:
    normalized_ext = (ext or "").lower()
    if normalized_ext not in _AUDIO_EXTS:
        return False
    if _platform_name(platform) == "telegram":
        if normalized_ext in _TELEGRAM_VOICE_EXTS:
            return is_voice
        return normalized_ext in _TELEGRAM_AUDIO_ATTACHMENT_EXTS
    return True


class BaseDeliveryMixin:
    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None
    ) -> SendResult:
        """
        Send a message to a chat.
        
        Args:
            chat_id: The chat/channel ID to send to
            content: Message content (may be markdown)
            reply_to: Optional message ID to reply to
            metadata: Additional platform-specific options
        
        Returns:
            SendResult with success status and message ID
        """
        pass

    # Default: the adapter treats ``finalize=True`` on edit_message as a
    # no-op and is happy to have the stream consumer skip redundant final
    # edits.  Subclasses that *require* an explicit finalize call to close
    # out the message lifecycle (e.g. rich card / AI assistant surfaces
    # such as DingTalk AI Cards) override this to True (class attribute or
    # property) so the stream consumer knows not to short-circuit.
    REQUIRES_EDIT_FINALIZE: bool = False

    async def create_handoff_thread(
        self,
        parent_chat_id: str,
        name: str,
    ) -> Optional[str]:
        """Create a fresh thread under ``parent_chat_id`` for a session handoff.

        Used by the gateway's handoff watcher when transferring a CLI
        session to a thread-capable platform — the new thread isolates the
        handed-off conversation from any pre-existing chat in the home
        channel and gives users a clean per-handoff scrollback.

        Returns the new thread/topic id (as a string) on success, or
        ``None`` if the platform doesn't support threading or the
        attempt failed (permissions, topics-mode off, etc.). When ``None``
        is returned the watcher falls back to using ``parent_chat_id``
        directly.

        Default implementation returns ``None`` — adapters that support
        threads override this. See:
          - Telegram: forum topics in groups, DM topics with bot API 9.4+
          - Discord:  text-channel threads (1440-min auto-archive)
          - Slack:    seed-message thread anchoring
        """
        return None


    async def edit_message(
        self,
        chat_id: str,
        message_id: str,
        content: str,
        *,
        finalize: bool = False,
    ) -> SendResult:
        """
        Edit a previously sent message. Optional — platforms that don't
        support editing return success=False and callers fall back to
        sending a new message.

        ``finalize`` signals that this is the last edit in a streaming
        sequence.  Most platforms (Telegram, Slack, Discord, Matrix,
        etc.) treat it as a no-op because their edit APIs have no notion
        of message lifecycle state — an edit is an edit.  Platforms that
        render streaming updates with a distinct "in progress" state and
        require explicit closure (e.g. rich card / AI assistant surfaces
        such as DingTalk AI Cards) use it to finalize the message and
        transition the UI out of the streaming indicator — those should
        also set ``REQUIRES_EDIT_FINALIZE = True`` so callers route a
        final edit through even when content is unchanged.  Callers
        should set ``finalize=True`` on the final edit of a streamed
        response (typically when ``got_done`` fires in the stream
        consumer) and leave it ``False`` on intermediate edits.
        """
        return SendResult(success=False, error="Not supported")

    async def delete_message(
        self,
        chat_id: str,
        message_id: str,
    ) -> bool:
        """
        Delete a previously sent message.  Optional — platforms that don't
        support deletion return ``False`` and callers fall back to leaving
        the message in place.

        Used by the stream consumer's fresh-final cleanup path (see
        openclaw/openclaw#72038) to remove long-lived preview messages
        after sending the completed reply as a fresh message so the
        platform's visible timestamp reflects completion time.

        Returns ``True`` on successful deletion, ``False`` otherwise.
        Subclasses should override for platforms with a deletion API
        (e.g. Telegram ``deleteMessage``).
        """
        return False

    def _get_ephemeral_system_ttl_default(self) -> int:
        """Read ``display.ephemeral_system_ttl`` from config.

        Returns the TTL in seconds to use when an :class:`EphemeralReply`
        does not specify one explicitly.  ``0`` (the default) disables
        auto-deletion.  Non-fatal if config is unreadable.
        """
        try:
            from hermes_cli.config import load_config as _load_config
        except Exception:
            return 0
        try:
            cfg = _load_config()
        except Exception:
            return 0
        display = cfg.get("display", {}) if isinstance(cfg, dict) else {}
        if not isinstance(display, dict):
            return 0
        raw = display.get("ephemeral_system_ttl", 0)
        try:
            return int(raw)
        except (TypeError, ValueError):
            return 0

    def _schedule_ephemeral_delete(
        self,
        chat_id: str,
        message_id: str,
        ttl_seconds: int,
    ) -> None:
        """Spawn a detached task that deletes ``message_id`` after ``ttl_seconds``.

        Best-effort — failures (gateway restart, permission denied, message
        too old for Telegram's 48h window) are swallowed at debug level.
        Does not block the caller.
        """

        async def _run_delete() -> None:
            try:
                await asyncio.sleep(max(1, int(ttl_seconds)))
                await self.delete_message(chat_id=chat_id, message_id=message_id)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.debug(
                    "[%s] Ephemeral delete failed for %s/%s: %s",
                    self.name, chat_id, message_id, e,
                )

        coro = _run_delete()
        try:
            asyncio.create_task(coro)
        except RuntimeError:
            # No running loop (e.g. unit tests that never reach the async
            # path).  Close the coroutine cleanly so Python doesn't warn
            # about it never being awaited, then drop silently.
            coro.close()

    async def send_slash_confirm(
        self,
        chat_id: str,
        title: str,
        message: str,
        session_key: str,
        confirm_id: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Send a three-option slash-command confirmation prompt.

        Used by the gateway's generic slash-confirm primitive (see
        ``GatewayRunner._request_slash_confirm``) for commands that have a
        non-destructive but expensive side effect the user should explicitly
        acknowledge — the current caller is ``/reload-mcp``, which
        invalidates the provider prompt cache.

        Platforms with inline-button support (Telegram, Discord, Slack,
        Matrix, Feishu) should override this to render three buttons:
        Approve Once / Always Approve / Cancel.  Button callbacks MUST be
        routed back through the gateway by calling
        ``GatewayRunner._resolve_slash_confirm(confirm_id, choice)`` where
        ``choice`` is ``"once"`` / ``"always"`` / ``"cancel"``.

        Platforms without button UIs leave this as the default and fall
        through to the gateway's text fallback (which sends ``message`` as
        plain text and intercepts the next ``/approve`` / ``/always`` /
        ``/cancel`` reply).

        ``confirm_id`` is a short string generated by the gateway; the
        adapter stores it alongside any platform-specific state needed to
        route the callback (e.g. Telegram's ``_approval_state`` dict).
        """
        return SendResult(success=False, error="Not supported")

    async def send_clarify(
        self,
        chat_id: str,
        question: str,
        choices: Optional[list],
        clarify_id: str,
        session_key: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Send a clarify prompt to the user.

        Two render modes:

          * **Multiple choice** (``choices`` is a non-empty list) — adapters
            that override this should render inline buttons (one per choice
            plus a final "Other" / free-text option).  Button callbacks
            MUST resolve via
            ``tools.clarify_gateway.resolve_gateway_clarify(clarify_id, response)``
            with the chosen string.  Picking the "Other" button calls
            ``mark_awaiting_text(clarify_id)`` so the next message in the
            session is captured as the response.

          * **Open-ended** (``choices`` is None or empty) — render the
            question as a plain text message; the next user message in the
            session is captured by the gateway's text-intercept and
            resolves the clarify automatically (see
            ``GatewayRunner._maybe_intercept_clarify_text``).

        The default implementation falls back to a numbered text list,
        which works on every platform — the user replies with a number
        ("2") or with the literal choice text, and the gateway intercepts
        and resolves.  For the text fallback path, the default calls
        ``mark_awaiting_text()`` so that the gateway text-intercept
        (:meth:`GatewayRunner._maybe_intercept_clarify_text`) catches the
        user's reply instead of timing out.
        Adapters with native button UIs (Telegram, Discord) SHOULD
        override this for a richer UX.
        """
        if choices:
            lines = [f"❓ {question}", ""]
            for i, choice in enumerate(choices, start=1):
                lines.append(f"  {i}. {choice}")
            lines.append("")
            lines.append("Reply with the number, the option text, or your own answer.")
            text = "\n".join(lines)
            # Text fallback: enable text-capture so the gateway intercept
            # picks up the user's typed reply (e.g. "2" or choice text).
            from tools.clarify_gateway import mark_awaiting_text
            mark_awaiting_text(clarify_id)
        else:
            text = f"❓ {question}"
        return await self.send(
            chat_id=chat_id,
            content=text,
            metadata=metadata,
        )

    async def send_private_notice(
        self,
        chat_id: str,
        user_id: Optional[str],
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Send a notice privately when the platform supports it.

        The default implementation falls back to a normal send so callers can
        use one code path across platforms.
        """
        return await self.send(
            chat_id=chat_id,
            content=content,
            reply_to=reply_to,
            metadata=metadata,
        )

    async def send_typing(self, chat_id: str, metadata=None) -> None:
        """
        Send a typing indicator.
        
        Override in subclasses if the platform supports it.
        metadata: optional dict with platform-specific context (e.g. thread_id for Slack).
        """
        pass

    async def stop_typing(self, chat_id: str) -> None:
        """Stop a persistent typing indicator (if the platform uses one).

        Override in subclasses that start background typing loops.
        Default is a no-op for platforms with one-shot typing indicators.
        """
        pass

    async def send_multiple_images(
        self,
        chat_id: str,
        images: List[Tuple[str, str]],
        metadata: Optional[Dict[str, Any]] = None,
        human_delay: float = 0.0,
    ) -> None:
        """Send a batch of images.

        Accepts ``http(s)://``, ``file://`` URIs in the first tuple
        element.

        Default implementation sends each item individually,
        routing animated GIFs through ``send_animation`` and local
        files through ``send_image_file``.

        Override in subclasses to bundle into a single native API call
        (e.g. Signal's multi-attachment RPC)
        """
        from urllib.parse import unquote as _unquote

        for image_url, alt_text in images:
            if human_delay > 0:
                await asyncio.sleep(human_delay)
            try:
                logger.info(
                    "[%s] Sending image: %s (alt=%s)",
                    self.name,
                    safe_url_for_log(image_url),
                    alt_text[:30] if alt_text else "",
                )
                if image_url.startswith("file://"):
                    img_result = await self.send_image_file(
                        chat_id=chat_id,
                        image_path=_unquote(image_url[7:]),
                        caption=alt_text if alt_text else None,
                        metadata=metadata,
                    )
                elif self._is_animation_url(image_url):
                    img_result = await self.send_animation(
                        chat_id=chat_id,
                        animation_url=image_url,
                        caption=alt_text if alt_text else None,
                        metadata=metadata,
                    )
                else:
                    img_result = await self.send_image(
                        chat_id=chat_id,
                        image_url=image_url,
                        caption=alt_text if alt_text else None,
                        metadata=metadata,
                    )
                if not img_result.success:
                    logger.error("[%s] Failed to send image: %s", self.name, img_result.error)
            except Exception as img_err:
                logger.error("[%s] Error sending image: %s", self.name, img_err, exc_info=True)

    async def send_image(
        self,
        chat_id: str,
        image_url: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """
        Send an image natively via the platform API.
        
        Override in subclasses to send images as proper attachments
        instead of plain-text URLs. Default falls back to sending the
        URL as a text message.
        """
        # Fallback: send URL as text (subclasses override for native images)
        text = f"{caption}\n{image_url}" if caption else image_url
        return await self.send(chat_id=chat_id, content=text, reply_to=reply_to, metadata=metadata)

    async def send_animation(
        self,
        chat_id: str,
        animation_url: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """
        Send an animated GIF natively via the platform API.
        
        Override in subclasses to send GIFs as proper animations
        (e.g., Telegram send_animation) so they auto-play inline.
        Default falls back to send_image.
        """
        return await self.send_image(chat_id=chat_id, image_url=animation_url, caption=caption, reply_to=reply_to, metadata=metadata)

    @staticmethod
    def _is_animation_url(url: str) -> bool:
        """Check if a URL points to an animated GIF (vs a static image)."""
        lower = url.lower().split('?')[0]  # Strip query params
        return lower.endswith('.gif')

    @staticmethod
    def extract_images(content: str) -> Tuple[List[Tuple[str, str]], str]:
        """
        Extract image URLs from markdown and HTML image tags in a response.
        
        Finds patterns like:
        - ![alt text](https://example.com/image.png)
        - <img src="https://example.com/image.png">
        - <img src="https://example.com/image.png"></img>
        
        Args:
            content: The response text to scan.
        
        Returns:
            Tuple of (list of (url, alt_text) pairs, cleaned content with image tags removed).
        """
        images = []
        cleaned = content
        
        # Match markdown images: ![alt](url)
        md_pattern = r'!\[([^\]]*)\]\((https?://[^\s\)]+)\)'
        for match in re.finditer(md_pattern, content):
            alt_text = match.group(1)
            url = match.group(2)
            # Only extract URLs that look like actual images
            if any(url.lower().endswith(ext) or ext in url.lower() for ext in
                   ['.png', '.jpg', '.jpeg', '.gif', '.webp', 'fal.media', 'fal-cdn', 'replicate.delivery']):
                images.append((url, alt_text))
        
        # Match HTML img tags: <img src="url"> or <img src="url"></img> or <img src="url"/>
        html_pattern = r'<img\s+src=["\']?(https?://[^\s"\'<>]+)["\']?\s*/?>\s*(?:</img>)?'
        for match in re.finditer(html_pattern, content):
            url = match.group(1)
            images.append((url, ""))
        
        # Remove only the matched image tags from content (not all markdown images)
        if images:
            extracted_urls = {url for url, _ in images}
            def _remove_if_extracted(match):
                url = match.group(2) if match.lastindex >= 2 else match.group(1)
                return '' if url in extracted_urls else match.group(0)
            cleaned = re.sub(md_pattern, _remove_if_extracted, cleaned)
            cleaned = re.sub(html_pattern, _remove_if_extracted, cleaned)
            # Clean up leftover blank lines
            cleaned = re.sub(r'\n{3,}', '\n\n', cleaned).strip()
        
        return images, cleaned

    async def send_voice(
        self,
        chat_id: str,
        audio_path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        **kwargs,
    ) -> SendResult:
        """
        Send an audio file as a native voice message via the platform API.
        
        Override in subclasses to send audio as voice bubbles (Telegram)
        or file attachments (Discord). Default falls back to sending the
        file path as text.
        """
        text = f"🔊 Audio: {audio_path}"
        if caption:
            text = f"{caption}\n{text}"
        return await self.send(chat_id=chat_id, content=text, reply_to=reply_to, metadata=metadata)

    def prepare_tts_text(self, text: str) -> str:
        """Prepare text for TTS. Override to filter tool output, code, etc.

        Default strips markdown formatting and truncates to 4000 chars.
        """
        return re.sub(r'[*_`#\[\]()]', '', text)[:4000].strip()

    async def play_tts(
        self,
        chat_id: str,
        audio_path: str,
        **kwargs,
    ) -> SendResult:
        """
        Play auto-TTS audio for voice replies.

        Override in subclasses for invisible playback (e.g. Web UI).
        Default falls back to send_voice (shows audio player).
        """
        return await self.send_voice(chat_id=chat_id, audio_path=audio_path, **kwargs)

    async def send_video(
        self,
        chat_id: str,
        video_path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        **kwargs,
    ) -> SendResult:
        """
        Send a video natively via the platform API.

        Override in subclasses to send videos as inline playable media.
        Default falls back to sending the file path as text.
        """
        text = f"🎬 Video: {video_path}"
        if caption:
            text = f"{caption}\n{text}"
        return await self.send(chat_id=chat_id, content=text, reply_to=reply_to, metadata=metadata)

    async def send_document(
        self,
        chat_id: str,
        file_path: str,
        caption: Optional[str] = None,
        file_name: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        **kwargs,
    ) -> SendResult:
        """
        Send a document/file natively via the platform API.

        Override in subclasses to send files as downloadable attachments.
        Default falls back to sending the file path as text.
        """
        text = f"📎 File: {file_path}"
        if caption:
            text = f"{caption}\n{text}"
        return await self.send(chat_id=chat_id, content=text, reply_to=reply_to, metadata=metadata)

    async def send_image_file(
        self,
        chat_id: str,
        image_path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        **kwargs,
    ) -> SendResult:
        """
        Send a local image file natively via the platform API.

        Unlike send_image() which takes a URL, this takes a local file path.
        Override in subclasses for native photo attachments.
        Default falls back to sending the file path as text.
        """
        text = f"🖼️ Image: {image_path}"
        if caption:
            text = f"{caption}\n{text}"
        return await self.send(chat_id=chat_id, content=text, reply_to=reply_to, metadata=metadata)

    @staticmethod
    def validate_media_delivery_path(path: str) -> Optional[str]:
        """Return a resolved path if it is safe for native attachment upload."""
        return validate_media_delivery_path(path)

    @staticmethod
    def filter_media_delivery_paths(media_files) -> List[Tuple[str, bool]]:
        """Drop unsafe MEDIA paths and normalize accepted paths."""
        safe_media: List[Tuple[str, bool]] = []
        for media_path, is_voice in media_files or []:
            safe_path = validate_media_delivery_path(str(media_path))
            if safe_path:
                safe_media.append((safe_path, bool(is_voice)))
            else:
                logger.warning("Skipping unsafe MEDIA directive path outside allowed roots")
        return safe_media

    @staticmethod
    def filter_local_delivery_paths(file_paths) -> List[str]:
        """Drop unsafe bare local file paths and normalize accepted paths."""
        safe_paths: List[str] = []
        for file_path in file_paths or []:
            safe_path = validate_media_delivery_path(str(file_path))
            if safe_path:
                safe_paths.append(safe_path)
            else:
                logger.warning("Skipping unsafe local file path outside allowed roots")
        return safe_paths

    @staticmethod
    def extract_media(content: str) -> Tuple[List[Tuple[str, bool]], str]:
        """
        Extract MEDIA:<path> tags and [[audio_as_voice]] directives from response text.

        The TTS tool returns responses like:
            [[audio_as_voice]]
            MEDIA:/path/to/audio.ogg

        Skills that produce large/lossless images (e.g. info-graph, where a
        rendered JPG is 1-2 MB but Telegram's sendPhoto recompresses to
        ~200 KB at 1280px) can use ``[[as_document]]`` to request unmodified
        delivery via sendDocument instead of sendPhoto/sendMediaGroup. The
        directive is detected at the dispatch sites (which have access to the
        original response); this method just strips it so it never leaks into
        user-visible text. Per-file granularity is intentionally not exposed —
        when an agent emits ``[[as_document]]`` once, every image path in the
        same response is delivered as a document, mirroring the all-or-nothing
        scope of ``[[audio_as_voice]]``.

        Args:
            content: The response text to scan.

        Returns:
            Tuple of (list of (path, is_voice) pairs, cleaned content with tags removed).
        """
        media = []
        cleaned = content

        # Check for [[audio_as_voice]] directive
        has_voice_tag = "[[audio_as_voice]]" in content
        cleaned = cleaned.replace("[[audio_as_voice]]", "")
        # Strip [[as_document]] directive — callers inspect the original
        # ``content`` for it (so they can still react to it); here we just
        # keep it out of the user-visible cleaned text.
        cleaned = cleaned.replace("[[as_document]]", "")
        
        # Extract MEDIA:<path> tags, allowing optional whitespace after the colon
        # and quoted/backticked paths for LLM-formatted outputs.
        media_pattern = MEDIA_TAG_CLEANUP_RE
        for match in media_pattern.finditer(content):
            path = match.group("path").strip()
            if len(path) >= 2 and path[0] == path[-1] and path[0] in "`\"'":
                path = path[1:-1].strip()
            path = path.lstrip("`\"'").rstrip("`\"',.;:)}]")
            if path:
                media.append((os.path.expanduser(path), has_voice_tag))

        # Remove MEDIA tags from content (including surrounding quote/backtick wrappers)
        if media:
            cleaned = media_pattern.sub('', cleaned)
            cleaned = re.sub(r'\n{3,}', '\n\n', cleaned).strip()
        
        return media, cleaned

    @staticmethod
    def extract_local_files(content: str) -> Tuple[List[str], str]:
        """
        Detect bare local file paths in response text for native delivery.

        Matches absolute paths (/...) and tilde paths (~/) ending in common
        image, video, audio, or document extensions.  Validates each
        candidate with ``os.path.isfile()`` to avoid false positives from
        URLs or non-existent paths.

        The extension list is broader than just images/video so the agent
        can produce arbitrary artifacts (charts, PDFs, spreadsheets, code
        archives, CSVs) and have them ship to the user as native uploads
        without needing an explicit ``MEDIA:`` tag.  Image / video
        extensions still embed inline where the platform supports it;
        document extensions route through ``send_document``.  The dispatch
        partition lives in ``gateway/run.py``.

        Paths inside fenced code blocks (``` ... ```) and inline code
        (`...`) are ignored so that code samples are never mutilated.

        Returns:
            Tuple of (list of expanded file paths, cleaned text with the
            raw path strings removed).
        """
        _LOCAL_MEDIA_EXTS = (
            # Images (embed inline)
            '.png', '.jpg', '.jpeg', '.gif', '.webp', '.bmp', '.tiff', '.svg',
            # Video (embed inline where supported)
            '.mp4', '.mov', '.avi', '.mkv', '.webm',
            # Audio (delivered as voice/audio where supported)
            '.mp3', '.wav', '.ogg', '.m4a', '.flac',
            # Documents (uploaded as file attachments)
            '.pdf', '.docx', '.doc', '.odt', '.rtf', '.txt', '.md',
            # Spreadsheets / data
            '.xlsx', '.xls', '.ods', '.csv', '.tsv', '.json', '.xml', '.yaml', '.yml',
            # Presentations
            '.pptx', '.ppt', '.odp', '.key',
            # Archives
            '.zip', '.tar', '.gz', '.tgz', '.bz2', '.xz', '.7z', '.rar',
            # Web / rendered output
            '.html', '.htm',
        )
        ext_part = '|'.join(e.lstrip('.') for e in _LOCAL_MEDIA_EXTS)

        # (?<![/:\w.]) prevents matching inside URLs (e.g. https://…/img.png)
        #             and relative paths (./foo.png)
        # (?:~/|/)    anchors to absolute or home-relative paths
        path_re = re.compile(
            r'(?<![/:\w.])(?:~/|/)(?:[\w.\-]+/)*[\w.\-]+\.(?:' + ext_part + r')\b',
            re.IGNORECASE,
        )

        # Build spans covered by fenced code blocks and inline code
        code_spans: list = []
        for m in re.finditer(r'```[^\n]*\n.*?```', content, re.DOTALL):
            code_spans.append((m.start(), m.end()))
        for m in re.finditer(r'`[^`\n]+`', content):
            code_spans.append((m.start(), m.end()))

        def _in_code(pos: int) -> bool:
            return any(s <= pos < e for s, e in code_spans)

        found: list = []  # (raw_match_text, expanded_path)
        for match in path_re.finditer(content):
            if _in_code(match.start()):
                continue
            raw = match.group(0)
            expanded = os.path.expanduser(raw)
            if os.path.isfile(expanded):
                found.append((raw, expanded))

        # Deduplicate by expanded path, preserving discovery order
        seen: set = set()
        unique: list = []
        for raw, expanded in found:
            if expanded not in seen:
                seen.add(expanded)
                unique.append((raw, expanded))

        paths = [expanded for _, expanded in unique]

        cleaned = content
        if unique:
            for raw, _exp in unique:
                cleaned = cleaned.replace(raw, '')
            cleaned = re.sub(r'\n{3,}', '\n\n', cleaned).strip()

        return paths, cleaned
