"""
Base platform adapter interface.

All platform adapters (Telegram, Discord, WhatsApp, Weixin, and more) inherit from this
and implement the required methods.
"""

import asyncio
import inspect
import ipaddress
import logging
import os
import random
import re
import socket as _socket
import subprocess
import sys
import uuid
from abc import ABC, abstractmethod
from urllib.parse import urlsplit

from utils import normalize_proxy_url

logger = logging.getLogger(__name__)

# Audio file extensions Hermes recognizes for native audio delivery.
# Kept in sync with tools/send_message_tool.py and cron/scheduler.py via
# should_send_media_as_audio() below.
_AUDIO_EXTS = frozenset({'.ogg', '.opus', '.mp3', '.wav', '.m4a', '.flac'})
# Telegram's Bot API sendAudio only accepts MP3 / M4A. Other audio
# formats either need to go through sendVoice (Opus/OGG) or must be
# delivered as a regular document.
_TELEGRAM_AUDIO_ATTACHMENT_EXTS = frozenset({'.mp3', '.m4a'})
_TELEGRAM_VOICE_EXTS = frozenset({'.ogg', '.opus'})

MEDIA_TAG_CLEANUP_RE = re.compile(
    r'''[`"']?MEDIA:\s*(?P<path>`[^`\n]+`|"[^"\n]+"|'[^'\n]+'|(?:~/|/)\S+(?:[^\S\n]+\S+)*?\.(?:png|jpe?g|gif|webp|mp4|mov|avi|mkv|webm|ogg|opus|mp3|wav|m4a|flac|epub|pdf|zip|rar|7z|docx?|xlsx?|pptx?|txt|csv|apk|ipa)(?=[\s`"',;:)\]}]|$))[`"']?'''
)


from channels.platforms.base_text import (
    _custom_unit_to_cp,
    _platform_name,
    _prefix_within_utf16_limit,
    _reply_anchor_for_event,
    _thread_metadata_for_source,
    should_send_media_as_audio,
    utf16_len,
)

from channels.platforms.base_network import (
    is_host_excluded_by_no_proxy,
    is_network_accessible,
    proxy_kwargs_for_aiohttp,
    proxy_kwargs_for_bot,
    resolve_proxy_url,
    should_bypass_proxy,
)
from channels.platforms.base_media_cache import (
    DEFAULT_INBOUND_MEDIA_MAX_BYTES,
    AUDIO_CACHE_DIR,
    DOCUMENT_CACHE_DIR,
    IMAGE_CACHE_DIR,
    MEDIA_DELIVERY_SAFE_ROOTS,
    SUPPORTED_DOCUMENT_TYPES,
    SUPPORTED_IMAGE_DOCUMENT_TYPES,
    SUPPORTED_VIDEO_TYPES,
    VIDEO_CACHE_DIR,
    CachedMedia,
    _ssrf_redirect_guard,
    cache_audio_from_bytes,
    cache_audio_from_url,
    cache_document_from_bytes,
    cache_image_from_bytes,
    cache_image_from_url,
    cache_media_bytes,
    cache_video_from_bytes,
    cleanup_document_cache,
    cleanup_image_cache,
    get_audio_cache_dir,
    get_document_cache_dir,
    get_image_cache_dir,
    get_inbound_media_max_bytes,
    get_video_cache_dir,
    safe_url_for_log,
    validate_inbound_media_size,
    validate_media_delivery_path,
)

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Any, Callable, Awaitable, Tuple, Union
from enum import Enum

from pathlib import Path as _Path
sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))

from channels.config import Platform, PlatformConfig
from channels.session_identity import SessionSource, build_session_key
from hermes_constants import get_hermes_dir, get_hermes_home


GATEWAY_SECRET_CAPTURE_UNSUPPORTED_MESSAGE = (
    "Secure secret entry is not supported over messaging. "
    "Load this skill in the local CLI to be prompted, or add the key to ~/.hermes/.env manually."
)


from channels.platforms.base_models import (
    EphemeralReply,
    MessageEvent,
    MessageHandler,
    MessageType,
    ProcessingOutcome,
    SendResult,
    classify_send_error,
    coerce_plaintext_gateway_command,
    merge_pending_message_event,
    resolve_channel_prompt,
    resolve_channel_skills,
)

from channels.platforms.base_delivery import BaseDeliveryMixin

class BasePlatformAdapter(BaseDeliveryMixin, ABC):
    """
    Base class for platform adapters.
    
    Subclasses implement platform-specific logic for:
    - Connecting and authenticating
    - Receiving messages
    - Sending messages/responses
    - Handling media
    """

    # Whether this platform renders triple-backtick fenced code blocks (i.e.
    # ``format_message`` translates/preserves markdown fences into a real code
    # block).  Capability flag for markdown-aware presentation choices.
    # Default False (plain-text platforms); markdown-rendering adapters set True.
    # Tool-progress uses this to render a terminal command as a bare fenced code
    # block (no language tag — Slack mrkdwn would print the tag as a literal
    # first code line).  Plain-text platforms fall back to the short truncated
    # preview (see gateway/run.py progress_callback).
    supports_code_blocks: bool = False

    # Whether this adapter can deliver an ASYNC notification back to the agent
    # AFTER a turn ends — i.e. wake a fresh turn to surface a background
    # process completion (terminal notify_on_complete / watch_patterns) or a
    # detached subagent result (delegate_task background=True).
    #
    # True for adapters that hold a persistent outbound channel (Telegram,
    # Discord, Slack, ... — they have a real ``send()`` and the gateway runs
    # the watcher/drain loops). False for stateless request/response adapters
    # (the API server): every route closes its channel when the turn ends, so
    # there is nowhere to push a later completion. The gateway propagates this
    # into the ``HERMES_SESSION_ASYNC_DELIVERY`` contextvar at session-bind
    # time; tools read it via ``async_delivery_supported()`` and refuse to make
    # a delivery promise they can't keep. A new stateless adapter only needs to
    # set this to False to stay correct-by-default.
    supports_async_delivery: bool = True

    # The command prefix users can always TYPE on this platform to reach
    # Hermes commands.  Default "/" (most platforms deliver "/approve" etc.
    # as plain message text).  Platforms where typing a leading "/" is
    # intercepted or restricted by the client (Slack blocks native slash
    # commands inside threads; Matrix clients reserve "/" for client-local
    # commands) ship a "!" alias rewrite in their adapter and set this to
    # "!" so user-facing instruction text ("Reply `!approve` ...") tells
    # users the form that actually works everywhere.  Capability flag —
    # shared prompt builders read it via getattr(adapter,
    # "typed_command_prefix", "/"); no per-platform branching at call sites.
    typed_command_prefix: str = "/"

    def __init__(self, config: PlatformConfig, platform: Platform):
        self.config = config
        self.platform = platform
        self._message_handler: Optional[MessageHandler] = None
        self._running = False
        self._fatal_error_code: Optional[str] = None
        self._fatal_error_message: Optional[str] = None
        self._fatal_error_retryable = True
        self._fatal_error_handler: Optional[Callable[["BasePlatformAdapter"], Awaitable[None] | None]] = None
        
        # Track active message handlers per session for interrupt support.
        # _active_sessions stores the per-session interrupt Event; _session_tasks
        # maps session → the specific Task currently processing it so that
        # session-terminating commands (/stop, /new, /reset) can cancel the
        # right task and release the adapter-level guard deterministically.
        # Without the owner-task map, an old task's finally block could delete
        # a newer task's guard, leaving stale busy state.
        self._active_sessions: Dict[str, asyncio.Event] = {}
        self._pending_messages: Dict[str, MessageEvent] = {}
        self._session_tasks: Dict[str, asyncio.Task] = {}
        # Background message-processing tasks spawned by handle_message().
        # Gateway shutdown cancels these so an old gateway instance doesn't keep
        # working on a task after --replace or manual restarts.
        self._background_tasks: set[asyncio.Task] = set()
        # One-shot callbacks to fire after the main response is delivered.
        # Keyed by session_key. Values are either a bare callback (legacy) or
        # a ``(generation, callback)`` tuple so GatewayRunner can make deferred
        # deliveries generation-aware and avoid stale runs clearing callbacks
        # registered by a fresher run for the same session.
        self._post_delivery_callbacks: Dict[str, Any] = {}
        self._expected_cancelled_tasks: set[asyncio.Task] = set()
        self._busy_session_handler: Optional[Callable[[MessageEvent, str], Awaitable[bool]]] = None
        # Auto-TTS on voice input: ``_auto_tts_default`` is the global default
        # (``voice.auto_tts`` in config.yaml, pushed by GatewayRunner on connect).
        # Per-chat overrides live in two sets populated from ``_voice_mode``:
        #   - ``_auto_tts_enabled_chats``: chat explicitly opted in via ``/voice on``
        #     or ``/voice tts`` (mode is ``voice_only`` or ``all``). Fires even when
        #     the global default is False.
        #   - ``_auto_tts_disabled_chats``: chat explicitly opted out via
        #     ``/voice off`` (mode is ``off``). Suppresses auto-TTS even when the
        #     global default is True.
        # The gate in _process_message() is:
        #   fire if chat in _auto_tts_enabled_chats
        #     OR (_auto_tts_default and chat not in _auto_tts_disabled_chats)
        self._auto_tts_default: bool = False
        self._auto_tts_enabled_chats: set = set()
        self._auto_tts_disabled_chats: set = set()
        # Chats where typing indicator is paused (e.g. during approval waits).
        # _keep_typing skips send_typing when the chat_id is in this set.
        self._typing_paused: set = set()

    @property
    def message_len_fn(self) -> Callable[[str], int]:
        """Return the length function for measuring message size on this platform.

        Override in adapters whose platform counts characters differently from
        Python ``len`` (e.g. Telegram counts UTF-16 code units).
        """
        return len

    def supports_draft_streaming(
        self,
        chat_type: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """Whether this adapter supports native streaming-draft updates.

        Telegram Bot API 9.5 introduced ``sendMessageDraft``, which renders an
        animated streaming preview as the bot calls it repeatedly with the
        same ``draft_id`` and growing text.  Adapters that implement
        ``send_draft`` should return True here for the chat types where the
        platform supports it (Telegram restricts drafts to private DMs).

        Default implementation returns False.  Stream consumers fall back to
        the edit-based path (``send`` + ``edit_message``) when this returns
        False or when ``send_draft`` raises.
        """
        return False

    async def send_draft(
        self,
        chat_id: str,
        draft_id: int,
        content: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Send or update an animated streaming-draft preview.

        Reuse the same ``draft_id`` (any non-zero int) across consecutive
        calls within a single response so the platform animates the preview
        rather than re-creating it.  Different responses must use different
        ``draft_id`` values within the same chat to avoid animating over a
        prior bubble.

        Drafts have no message_id and cannot be edited, replied to, or
        deleted via normal message APIs.  When the response finishes, the
        caller delivers the final answer as a regular ``send`` and the
        draft preview clears naturally on the client.

        Default implementation raises NotImplementedError; adapters that
        also return True from :meth:`supports_draft_streaming` must override.
        """
        raise NotImplementedError(
            f"{type(self).__name__} does not implement send_draft"
        )

    @property
    def has_fatal_error(self) -> bool:
        return self._fatal_error_message is not None

    @property
    def fatal_error_message(self) -> Optional[str]:
        return self._fatal_error_message

    @property
    def fatal_error_code(self) -> Optional[str]:
        return self._fatal_error_code

    @property
    def fatal_error_retryable(self) -> bool:
        return self._fatal_error_retryable

    def _should_auto_tts_for_chat(self, chat_id: str) -> bool:
        """Whether auto-TTS on voice input should fire for ``chat_id``.

        Decision layers (Issue #16007):
          1. Explicit ``/voice on`` or ``/voice tts`` → always fire (even if
             ``voice.auto_tts`` is False).
          2. Explicit ``/voice off`` → never fire.
          3. Fall back to the global ``voice.auto_tts`` config default.
        """
        if chat_id in self._auto_tts_enabled_chats:
            return True
        if chat_id in self._auto_tts_disabled_chats:
            return False
        return bool(self._auto_tts_default)

    def set_fatal_error_handler(self, handler: Callable[["BasePlatformAdapter"], Awaitable[None] | None]) -> None:
        self._fatal_error_handler = handler

    def _mark_connected(self) -> None:
        self._running = True
        self._fatal_error_code = None
        self._fatal_error_message = None
        self._fatal_error_retryable = True
        self._write_runtime_status_safe("connected", platform_state="connected", error_code=None, error_message=None)

    def _mark_disconnected(self) -> None:
        self._running = False
        if self.has_fatal_error:
            return
        self._write_runtime_status_safe("disconnected", platform_state="disconnected", error_code=None, error_message=None)

    def _set_fatal_error(self, code: str, message: str, *, retryable: bool) -> None:
        self._running = False
        self._fatal_error_code = code
        self._fatal_error_message = message
        self._fatal_error_retryable = retryable
        self._write_runtime_status_safe("fatal", platform_state="fatal", error_code=code, error_message=message)

    def _write_runtime_status_safe(self, context: str, **kwargs) -> None:
        """Write runtime status; log first failure per context at warning, rest at debug.

        Status writes can fail on permissions, ENOSPC, missing status dir, etc.
        A persistently failing status dir used to be silent (``except: pass``).
        Logging every failure would spam the log on reconnect loops, so this
        surfaces the first failure per (platform, context) at warning level and
        downgrades subsequent failures to debug.
        """
        try:
            from channels.runtime_status import write_runtime_status
            write_runtime_status(platform=self.platform.value, **kwargs)
        except Exception as exc:
            # Use getattr so object.__new__(...) test harnesses that skip __init__
            # don't blow up on attribute access.
            logged = getattr(self, "_status_write_logged", None)
            if logged is None:
                logged = set()
                try:
                    self._status_write_logged = logged
                except Exception:
                    pass
            key = (self.platform.value, context)
            if key not in logged:
                logger.warning(
                    "Failed to write runtime status (%s) for %s: %s (further failures at debug level)",
                    context, self.platform.value, exc,
                )
                logged.add(key)
            else:
                logger.debug("Failed to write runtime status (%s) for %s: %s", context, self.platform.value, exc)

    async def _notify_fatal_error(self) -> None:
        handler = self._fatal_error_handler
        if not handler:
            return
        result = handler(self)
        if asyncio.iscoroutine(result):
            await result

    def _acquire_platform_lock(self, scope: str, identity: str, resource_desc: str) -> bool:
        """Acquire a scoped lock for this adapter. Returns True on success."""
        from channels.runtime_status import acquire_scoped_lock
        self._platform_lock_scope = scope
        self._platform_lock_identity = identity
        acquired, existing = acquire_scoped_lock(
            scope, identity, metadata={'platform': self.platform.value}
        )
        if acquired:
            return True
        owner_pid = existing.get('pid') if isinstance(existing, dict) else None
        message = (
            f'{resource_desc} already in use'
            + (f' (PID {owner_pid})' if owner_pid else '')
            + '. Stop the other gateway first.'
        )
        logger.error('[%s] %s', self.name, message)
        self._set_fatal_error(f'{scope}_lock', message, retryable=False)
        return False

    def _release_platform_lock(self) -> None:
        """Release the scoped lock acquired by _acquire_platform_lock."""
        identity = getattr(self, '_platform_lock_identity', None)
        if not identity:
            return
        from channels.runtime_status import release_scoped_lock
        release_scoped_lock(self._platform_lock_scope, identity)
        self._platform_lock_identity = None

    @property
    def name(self) -> str:
        """Human-readable name for this adapter."""
        return self.platform.value.title()
    
    @property
    def is_connected(self) -> bool:
        """Check if adapter is currently connected."""
        return self._running
    
    def set_message_handler(self, handler: MessageHandler) -> None:
        """
        Set the handler for incoming messages.
        
        The handler receives a MessageEvent and should return
        an optional response string.
        """
        self._message_handler = handler

    def set_busy_session_handler(self, handler: Optional[Callable[[MessageEvent, str], Awaitable[bool]]]) -> None:
        """Set an optional handler for messages arriving during active sessions."""
        self._busy_session_handler = handler
    
    def set_session_store(self, session_store: Any) -> None:
        """
        Set the session store for checking active sessions.
        
        Used by adapters that need to check if a thread/conversation
        has an active session before processing messages (e.g., Slack
        thread replies without explicit mentions).
        """
        self._session_store = session_store
    
    @abstractmethod
    async def connect(self) -> bool:
        """
        Connect to the platform and start receiving messages.
        
        Returns True if connection was successful.
        """
        pass
    
    @abstractmethod
    async def disconnect(self) -> None:
        """Disconnect from the platform."""
        pass
    
    async def _keep_typing(
        self,
        chat_id: str,
        interval: float = 2.0,
        metadata=None,
        stop_event: asyncio.Event | None = None,
    ) -> None:
        """
        Continuously send typing indicator until cancelled.
        
        Telegram/Discord typing status expires after ~5 seconds, so we refresh every 2
        to recover quickly after progress messages interrupt it.
        
        Skips send_typing when the chat is in ``_typing_paused`` (e.g. while
        the agent is waiting for dangerous-command approval).  This is critical
        for Slack's Assistant API where ``assistant_threads_setStatus`` disables
        the compose box — pausing lets the user type ``/approve`` or ``/deny``.

        Each ``send_typing`` call is bounded by a ~1.5s timeout so a slow
        network round-trip can't stall the refresh cadence.  Telegram- and
        Discord-side typing expire after ~5s; if any individual send_typing
        takes longer than the refresh interval, the bubble would die and
        stay dead until that call returns.  Abandoning the slow call lets
        the next tick fire a fresh send_typing on schedule — as long as
        one of them succeeds within the 5s platform-side window, the bubble
        stays visible across provider stalls / upstream API timeouts.
        """
        # Bound each send_typing round-trip so the refresh cadence isn't
        # gated on network health.  Must stay below ``interval`` so a slow
        # call gets abandoned before the next scheduled tick.
        _send_typing_timeout = max(0.25, min(1.5, interval - 0.25))
        try:
            while True:
                if stop_event is not None and stop_event.is_set():
                    return
                if chat_id not in self._typing_paused:
                    try:
                        await asyncio.wait_for(
                            self.send_typing(chat_id, metadata=metadata),
                            timeout=_send_typing_timeout,
                        )
                    except asyncio.TimeoutError:
                        # Slow network — abandon this tick, keep the loop
                        # on schedule so the next send_typing fires fresh.
                        pass
                    except asyncio.CancelledError:
                        raise
                    except Exception as typing_err:
                        logger.debug(
                            "[%s] send_typing error (non-fatal): %s",
                            self.name, typing_err,
                        )
                if stop_event is None:
                    await asyncio.sleep(interval)
                    continue
                loop = asyncio.get_running_loop()
                deadline = loop.time() + interval
                while not stop_event.is_set():
                    remaining = deadline - loop.time()
                    if remaining <= 0:
                        break
                    # Poll instead of wait_for(stop_event.wait()).  Cancelling
                    # wait_for while it owns the inner Event.wait task can leave
                    # shutdown paths stuck awaiting the typing task on Python
                    # 3.11/pytest-asyncio; sleep cancellation is immediate.
                    await asyncio.sleep(min(0.25, remaining))
                if stop_event.is_set():
                    return
        except asyncio.CancelledError:
            pass  # Normal cancellation when handler completes
        finally:
            # Ensure the underlying platform typing loop is stopped.
            # _keep_typing may have called send_typing() after an outer
            # stop_typing() cleared the task dict, recreating the loop.
            # Cancelling _keep_typing alone won't clean that up.
            if hasattr(self, "stop_typing"):
                try:
                    await self.stop_typing(chat_id)
                except Exception:
                    pass
            self._typing_paused.discard(chat_id)

    def pause_typing_for_chat(self, chat_id: str) -> None:
        """Pause typing indicator for a chat (e.g. during approval waits).

        Thread-safe (CPython GIL) — can be called from the sync agent thread
        while ``_keep_typing`` runs on the async event loop.
        """
        self._typing_paused.add(chat_id)

    def resume_typing_for_chat(self, chat_id: str) -> None:
        """Resume typing indicator for a chat after approval resolves."""
        self._typing_paused.discard(chat_id)

    async def interrupt_session_activity(self, session_key: str, chat_id: str) -> None:
        """Signal the active session loop to stop and clear typing immediately."""
        if session_key:
            interrupt_event = self._active_sessions.get(session_key)
            if interrupt_event is not None:
                interrupt_event.set()
        try:
            await self.stop_typing(chat_id)
        except Exception:
            pass

    def register_post_delivery_callback(
        self,
        session_key: str,
        callback: Callable,
        *,
        generation: int | None = None,
    ) -> None:
        """Register a deferred callback to fire after the main response.

        ``generation`` lets callers tie the callback to a specific gateway run
        generation so stale runs cannot clear callbacks owned by a fresher run.

        If a callback for the same ``session_key`` (and generation, when set)
        is already registered, the new callback is chained — both fire, in
        registration order, with per-callback exception isolation. This lets
        independent features (background-review release + temporary-bubble
        cleanup) coexist without clobbering each other. Stale-generation
        callers never overwrite a fresher generation's slot.
        """
        if not session_key or not callable(callback):
            return

        existing = self._post_delivery_callbacks.get(session_key)
        if existing is not None:
            if isinstance(existing, tuple) and len(existing) == 2:
                existing_gen, existing_cb = existing
            else:
                existing_gen, existing_cb = None, existing
            # Stale-generation registrations never overwrite a fresher slot.
            if (
                existing_gen is not None
                and generation is not None
                and int(generation) < int(existing_gen)
            ):
                return
            # Same-or-newer generation: chain with the existing callback so
            # both fire in registration order.
            if callable(existing_cb) and (
                existing_gen is None
                or generation is None
                or int(existing_gen) == int(generation)
            ):
                _prev = existing_cb
                _new = callback

                def _chained() -> None:
                    try:
                        _prev()
                    except Exception:
                        logger.debug("Post-delivery callback failed", exc_info=True)
                    try:
                        _new()
                    except Exception:
                        logger.debug("Post-delivery callback failed", exc_info=True)

                callback = _chained

        if generation is None:
            self._post_delivery_callbacks[session_key] = callback
        else:
            self._post_delivery_callbacks[session_key] = (int(generation), callback)

    def pop_post_delivery_callback(
        self,
        session_key: str,
        *,
        generation: int | None = None,
    ) -> Callable | None:
        """Pop a deferred callback, optionally requiring generation ownership."""
        if not session_key:
            return None
        entry = self._post_delivery_callbacks.get(session_key)
        if entry is None:
            return None
        if isinstance(entry, tuple) and len(entry) == 2:
            entry_generation, callback = entry
            if generation is not None and int(entry_generation) != int(generation):
                return None
            self._post_delivery_callbacks.pop(session_key, None)
            return callback if callable(callback) else None
        if generation is not None:
            return None
        self._post_delivery_callbacks.pop(session_key, None)
        return entry if callable(entry) else None

    # ── Processing lifecycle hooks ──────────────────────────────────────────
    # Subclasses override these to react to message processing events
    # (e.g. Discord adds 👀/✅/❌ reactions).

    async def on_processing_start(self, event: MessageEvent) -> None:
        """Hook called when background processing begins."""

    async def on_processing_complete(self, event: MessageEvent, outcome: ProcessingOutcome) -> None:
        """Hook called when background processing completes."""

    async def _run_processing_hook(self, hook_name: str, *args: Any, **kwargs: Any) -> None:
        """Run a lifecycle hook without letting failures break message flow."""
        hook = getattr(self, hook_name, None)
        if not callable(hook):
            return
        try:
            await hook(*args, **kwargs)
        except Exception as e:
            logger.warning("[%s] %s hook failed: %s", self.name, hook_name, e)

    @staticmethod
    def _is_retryable_error(error: Optional[str]) -> bool:
        """Return True if the error string looks like a transient network failure."""
        if not error:
            return False
        lowered = error.lower()
        return any(pat in lowered for pat in _RETRYABLE_ERROR_PATTERNS)

    @staticmethod
    def _is_timeout_error(error: Optional[str]) -> bool:
        """Return True if the error string indicates a read/write timeout.

        Timeout errors are NOT retryable and should NOT trigger plain-text
        fallback — the request may have already been delivered.
        """
        if not error:
            return False
        lowered = error.lower()
        return "timed out" in lowered or "readtimeout" in lowered or "writetimeout" in lowered

    def _unwrap_ephemeral(self, response: Any) -> Tuple[Optional[str], int]:
        """Unwrap a handler response into (text, ttl_seconds).

        Accepts a plain string, ``None``, or an :class:`EphemeralReply`.
        Returns ``(text, ttl)`` where ``ttl > 0`` means the caller should
        schedule a deletion via :meth:`_schedule_ephemeral_delete` after
        the send succeeds.  ``ttl`` is forced to 0 when the adapter
        doesn't override :meth:`delete_message` so non-supporting
        platforms silently degrade to normal sends.
        """
        if isinstance(response, EphemeralReply):
            ttl = response.ttl_seconds
            if ttl is None:
                try:
                    ttl = int(self._get_ephemeral_system_ttl_default())
                except Exception:
                    ttl = 0
            if ttl and ttl > 0 and type(self).delete_message is BasePlatformAdapter.delete_message:
                ttl = 0
            return response.text, int(ttl or 0)
        return response, 0

    async def _send_with_retry(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Any = None,
        max_retries: int = 2,
        base_delay: float = 2.0,
    ) -> "SendResult":
        """
        Send a message with automatic retry for transient network errors.

        On permanent failures (e.g. formatting / permission errors) falls back
        to a plain-text version before giving up. If all attempts fail due to
        network errors, sends the user a brief delivery-failure notice so they
        know to retry rather than waiting indefinitely.
        """

        result = await self.send(
            chat_id=chat_id,
            content=content,
            reply_to=reply_to,
            metadata=metadata,
        )

        if result.success:
            return result

        error_str = result.error or ""
        is_network = result.retryable or self._is_retryable_error(error_str)

        # Timeout errors are not safe to retry (message may have been
        # delivered) and not formatting errors — return the failure as-is.
        if not is_network and self._is_timeout_error(error_str):
            return result

        if is_network:
            # Retry with exponential backoff for transient errors
            for attempt in range(1, max_retries + 1):
                delay = base_delay * (2 ** (attempt - 1)) + random.uniform(0, 1)
                logger.warning(
                    "[%s] Send failed (attempt %d/%d, retrying in %.1fs): %s",
                    self.name, attempt, max_retries, delay, error_str,
                )
                await asyncio.sleep(delay)
                result = await self.send(
                    chat_id=chat_id,
                    content=content,
                    reply_to=reply_to,
                    metadata=metadata,
                )
                if result.success:
                    logger.info("[%s] Send succeeded on retry %d", self.name, attempt)
                    return result
                error_str = result.error or ""
                if not (result.retryable or self._is_retryable_error(error_str)):
                    break  # error switched to non-transient — fall through to plain-text fallback
            else:
                # All retries exhausted (loop completed without break) — notify user
                logger.error("[%s] Failed to deliver response after %d retries: %s", self.name, max_retries, error_str)
                notice = (
                    "\u26a0\ufe0f Message delivery failed after multiple attempts. "
                    "Please try again \u2014 your request was processed but the response could not be sent."
                )
                try:
                    await self.send(chat_id=chat_id, content=notice, reply_to=reply_to, metadata=metadata)
                except Exception as notify_err:
                    logger.debug("[%s] Could not send delivery-failure notice: %s", self.name, notify_err)
                return result

        # Non-network / post-retry formatting failure: try plain text as fallback
        logger.warning("[%s] Send failed: %s — trying plain-text fallback", self.name, error_str)
        fallback_result = await self.send(
            chat_id=chat_id,
            content=f"(Response formatting failed, plain text:)\n\n{content[:3500]}",
            reply_to=reply_to,
            metadata=metadata,
        )
        if not fallback_result.success:
            logger.error("[%s] Fallback send also failed: %s", self.name, fallback_result.error)
        return fallback_result

    @staticmethod
    def _merge_caption(existing_text: Optional[str], new_text: str) -> str:
        """Merge a new caption into existing text, avoiding duplicates.

        Uses line-by-line exact match (not substring) to prevent false positives
        where a shorter caption is silently dropped because it appears as a
        substring of a longer one (e.g. "Meeting" inside "Meeting agenda").
        Whitespace is normalised for comparison.
        """
        if not existing_text:
            return new_text
        existing_captions = [c.strip() for c in existing_text.split("\n\n")]
        if new_text.strip() not in existing_captions:
            return f"{existing_text}\n\n{new_text}".strip()
        return existing_text

    # ------------------------------------------------------------------
    # Session task + guard ownership helpers
    # ------------------------------------------------------------------
    # These were introduced together with the _session_tasks owner map to
    # make session lifecycle reconciliation deterministic across (a) the
    # normal completion path, (b) /stop/ /new/ /reset bypass commands,
    # and (c) stale-lock self-heal on the next inbound message.

    def _release_session_guard(
        self,
        session_key: str,
        *,
        guard: Optional[asyncio.Event] = None,
    ) -> None:
        """Release the adapter-level guard for a session.

        When ``guard`` is provided, only release the entry if it still points
        at that exact Event.  This lets reset-like commands swap in a temporary
        guard while the old processing task unwinds, without having the old
        task's cleanup accidentally clear the replacement guard.
        """
        current_guard = self._active_sessions.get(session_key)
        if current_guard is None:
            return
        if guard is not None and current_guard is not guard:
            return
        del self._active_sessions[session_key]

    def _session_task_is_stale(self, session_key: str) -> bool:
        """Return True if the owner task for ``session_key`` is done/cancelled.

        A lock is "stale" when the adapter still has ``_active_sessions[key]``
        AND a known owner task in ``_session_tasks`` that has already exited.
        When there is no owner task at all, that usually means the guard was
        installed by some path other than handle_message() (tests sometimes
        install guards directly) — don't treat that as stale.  The on-entry
        self-heal only needs to handle the production split-brain case where
        an owner task was recorded, then exited without clearing its guard.
        """
        task = self._session_tasks.get(session_key)
        if task is None:
            return False
        done = getattr(task, "done", None)
        return bool(done and done())

    def _heal_stale_session_lock(self, session_key: str) -> bool:
        """Clear a stale session lock if the owner task is already gone.

        Returns True if a stale lock was healed.  Returns False if there is
        no lock, or the owner task is still alive (the normal busy case).

        This is the on-entry safety net sidbin's issue #11016 analysis calls
        for: without it, a split-brain — adapter still thinks the session is
        active, but nothing is actually processing — traps the chat in
        infinite "Interrupting current task..." until the gateway is
        restarted.
        """
        if session_key not in self._active_sessions:
            return False
        if not self._session_task_is_stale(session_key):
            return False
        logger.warning(
            "[%s] Healing stale session lock for %s (owner task is done/absent)",
            self.name,
            session_key,
        )
        self._active_sessions.pop(session_key, None)
        self._pending_messages.pop(session_key, None)
        self._session_tasks.pop(session_key, None)
        return True

    def _start_session_processing(
        self,
        event: MessageEvent,
        session_key: str,
        *,
        interrupt_event: Optional[asyncio.Event] = None,
    ) -> bool:
        """Spawn a background processing task under the given session guard.

        Returns True on success.  If the runtime stubs ``create_task`` with a
        non-Task sentinel (some tests do this), the guard is rolled back and
        False is returned so the caller isn't left holding a half-installed
        session lock.
        """
        guard = interrupt_event or asyncio.Event()
        self._active_sessions[session_key] = guard

        task = asyncio.create_task(self._process_message_background(event, session_key))
        self._session_tasks[session_key] = task
        try:
            self._background_tasks.add(task)
        except TypeError:
            # Tests stub create_task() with lightweight sentinels that are not
            # hashable and do not support lifecycle callbacks.
            self._session_tasks.pop(session_key, None)
            self._release_session_guard(session_key, guard=guard)
            return False
        if hasattr(task, "add_done_callback"):
            task.add_done_callback(self._background_tasks.discard)
            task.add_done_callback(self._expected_cancelled_tasks.discard)
        return True

    async def cancel_session_processing(
        self,
        session_key: str,
        *,
        release_guard: bool = True,
        discard_pending: bool = True,
    ) -> None:
        """Cancel in-flight processing for a single session.

        ``release_guard=False`` keeps the adapter-level session guard in place
        so reset-like commands can finish atomically before follow-up messages
        are allowed to start a fresh background task.

        Bounded by a 5s timeout so a wedged finally block in the cancelled
        task (typing-task cleanup, on_processing_complete hook, etc.) can't
        stall the calling dispatch coroutine — particularly under pytest-
        asyncio where the event loop's cancellation-propagation semantics
        differ subtly from a bare ``asyncio.run`` harness.
        """
        task = self._session_tasks.pop(session_key, None)
        if task is not None and not task.done():
            logger.debug(
                "[%s] Cancelling active processing for session %s",
                self.name,
                session_key,
            )
            self._expected_cancelled_tasks.add(task)
            task.cancel()
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=5.0)
            except asyncio.CancelledError:
                pass
            except asyncio.TimeoutError:
                logger.warning(
                    "[%s] Cancelled task for %s did not exit within 5s; "
                    "unblocking dispatch and letting the task unwind in the background",
                    self.name, session_key,
                )
            except Exception:
                logger.debug(
                    "[%s] Session cancellation raised while unwinding %s",
                    self.name,
                    session_key,
                    exc_info=True,
                )
        if discard_pending:
            self._pending_messages.pop(session_key, None)
        if release_guard:
            self._release_session_guard(session_key)

    async def _drain_pending_after_session_command(
        self,
        session_key: str,
        command_guard: asyncio.Event,
    ) -> None:
        """Resume the latest queued follow-up once a session command completes.

        Called at the tail of /stop, /new, and /reset dispatch.  Releases the
        command-scoped guard, then — if a follow-up message landed while the
        command was running — spawns a fresh processing task for it.
        """
        pending_event = self._pending_messages.pop(session_key, None)
        self._release_session_guard(session_key, guard=command_guard)
        if pending_event is None:
            return
        self._start_session_processing(pending_event, session_key)

    async def _dispatch_active_session_command(
        self,
        event: MessageEvent,
        session_key: str,
        cmd: str,
    ) -> None:
        """Dispatch a reset-like bypass command while preserving guard ordering.

        /stop, /new, and /reset must:
          1. Keep the session guard installed while the runner processes the
             command (so a racing follow-up message stays queued, not
             dispatched as a second parallel run).
          2. Cancel the old in-flight adapter task only AFTER the runner has
             finished handling the command (so the runner sees consistent
             state and its response is sent in order).
          3. Release the command-scoped guard and drain the latest queued
             follow-up exactly once, after 1 and 2 complete.
        """
        logger.debug(
            "[%s] Command '/%s' bypassing active-session guard for %s",
            self.name,
            cmd,
            session_key,
        )

        current_guard = self._active_sessions.get(session_key)
        command_guard = asyncio.Event()
        self._active_sessions[session_key] = command_guard
        thread_meta = _thread_metadata_for_source(event.source, _reply_anchor_for_event(event))

        try:
            response = await self._message_handler(event)
            _text, _eph_ttl = self._unwrap_ephemeral(response)
            # Send the response BEFORE cancelling the old task so the send
            # cannot be affected by task-cancellation side effects (race
            # condition fix — issue #18912).  Previously the send happened
            # after cancel_session_processing, which could silently drop the
            # "/new" confirmation when an agent was actively running.
            if _text:
                logger.info(
                    "[%s] Sending command '/%s' response (%d chars) to %s",
                    self.name,
                    cmd,
                    len(_text),
                    event.source.chat_id,
                )
                _r = await self._send_with_retry(
                    chat_id=event.source.chat_id,
                    content=_text,
                    reply_to=_reply_anchor_for_event(event),
                    metadata=thread_meta,
                )
                if _eph_ttl > 0 and _r.success and _r.message_id:
                    self._schedule_ephemeral_delete(
                        chat_id=event.source.chat_id,
                        message_id=_r.message_id,
                        ttl_seconds=_eph_ttl,
                    )
            # Old adapter task (if any) is cancelled AFTER the response has
            # been sent — keeps ordering deterministic and avoids the race.
            await self.cancel_session_processing(
                session_key,
                release_guard=False,
                discard_pending=False,
            )
        except Exception:
            # On failure, restore the original guard if one still exists so
            # we don't leave the session in a half-reset state.
            if self._active_sessions.get(session_key) is command_guard:
                if session_key in self._session_tasks and current_guard is not None:
                    self._active_sessions[session_key] = current_guard
                else:
                    self._release_session_guard(session_key, guard=command_guard)
            raise

        await self._drain_pending_after_session_command(session_key, command_guard)

    async def handle_message(self, event: MessageEvent) -> None:
        """
        Process an incoming message.
        
        This method returns quickly by spawning background tasks.
        This allows new messages to be processed even while an agent is running,
        enabling interruption support.
        """
        if not self._message_handler:
            return

        coerce_plaintext_gateway_command(event)
        
        session_key = build_session_key(
            event.source,
            group_sessions_per_user=self.config.extra.get("group_sessions_per_user", True),
            thread_sessions_per_user=self.config.extra.get("thread_sessions_per_user", False),
        )

        # On-entry self-heal: if the adapter still has an _active_sessions
        # entry for this key but the owner task has already exited (done or
        # cancelled), the lock is stale.  Clear it and fall through to
        # normal dispatch so the user isn't trapped behind a dead guard —
        # this is the split-brain tail described in issue #11016.
        if session_key in self._active_sessions:
            self._heal_stale_session_lock(session_key)

        # Check if there's already an active handler for this session
        if session_key in self._active_sessions:
            # Certain commands must bypass the active-session guard and be
            # dispatched directly to the gateway runner.  Without this, they
            # are queued as pending messages and either:
            #   - leak into the conversation as user text (/stop, /new), or
            #   - deadlock (/approve, /deny — agent is blocked on Event.wait)
            #
            # Dispatch inline: call the message handler directly and send the
            # response.  Do NOT use _process_message_background — it manages
            # session lifecycle and its cleanup races with the running task
            # (see PR #4926).
            cmd = event.get_command()
            from hermes_cli.commands import should_bypass_active_session

            if should_bypass_active_session(cmd):
                # /stop, /new, /reset must cancel the in-flight adapter task
                # and preserve ordering of queued follow-ups.  Route those
                # through the dedicated handoff path that serializes
                # cancellation + runner response + pending drain.
                if cmd in {"stop", "new", "reset"}:
                    try:
                        await self._dispatch_active_session_command(event, session_key, cmd)
                    except Exception as e:
                        logger.error(
                            "[%s] Command '/%s' dispatch failed: %s",
                            self.name, cmd, e, exc_info=True,
                        )
                    return

                # Other bypass commands (/approve, /deny, /status,
                # /background, /restart) just need direct dispatch — they
                # don't cancel the running task.
                logger.debug(
                    "[%s] Command '/%s' bypassing active-session guard for %s",
                    self.name, cmd, session_key,
                )
                try:
                    _thread_meta = _thread_metadata_for_source(event.source, _reply_anchor_for_event(event))
                    response = await self._message_handler(event)
                    _text, _eph_ttl = self._unwrap_ephemeral(response)
                    if _text:
                        _r = await self._send_with_retry(
                            chat_id=event.source.chat_id,
                            content=_text,
                            reply_to=_reply_anchor_for_event(event),
                            metadata=_thread_meta,
                        )
                        if _eph_ttl > 0 and _r.success and _r.message_id:
                            self._schedule_ephemeral_delete(
                                chat_id=event.source.chat_id,
                                message_id=_r.message_id,
                                ttl_seconds=_eph_ttl,
                            )
                except Exception as e:
                    logger.error("[%s] Command '/%s' dispatch failed: %s", self.name, cmd, e, exc_info=True)
                return

            # Clarify text-capture bypass: if the agent is blocked on a
            # clarify_tool call awaiting a free-form text response (open-
            # ended clarify, or user picked "Other"), the next non-command
            # message in this session MUST reach the runner so the
            # clarify-intercept can resolve it and unblock the agent.
            #
            # Without this bypass: the message gets queued in
            # _pending_messages AND triggers an interrupt, killing the
            # agent run mid-clarify and discarding the user's answer.
            # Same shape as the /approve deadlock fix (PR #4926) — both
            # cases are "agent thread blocked on Event.wait, message must
            # reach the resolver before being treated as a new turn."
            if not cmd:
                try:
                    from tools import clarify_gateway as _clarify_mod
                    _has_text_clarify = (
                        _clarify_mod.get_pending_for_session(session_key) is not None
                    )
                except Exception:
                    _has_text_clarify = False

                if _has_text_clarify:
                    logger.debug(
                        "[%s] Routing message to clarify text-intercept for %s",
                        self.name, session_key,
                    )
                    try:
                        _thread_meta = _thread_metadata_for_source(
                            event.source, _reply_anchor_for_event(event)
                        )
                        response = await self._message_handler(event)
                        _text, _eph_ttl = self._unwrap_ephemeral(response)
                        if _text:
                            _r = await self._send_with_retry(
                                chat_id=event.source.chat_id,
                                content=_text,
                                reply_to=_reply_anchor_for_event(event),
                                metadata=_thread_meta,
                            )
                            if _eph_ttl > 0 and _r.success and _r.message_id:
                                self._schedule_ephemeral_delete(
                                    chat_id=event.source.chat_id,
                                    message_id=_r.message_id,
                                    ttl_seconds=_eph_ttl,
                                )
                    except Exception as e:
                        logger.error(
                            "[%s] Clarify text-intercept dispatch failed: %s",
                            self.name, e, exc_info=True,
                        )
                    return

            if self._busy_session_handler is not None:
                try:
                    if await self._busy_session_handler(event, session_key):
                        return
                except Exception as e:
                    logger.error("[%s] Busy-session handler failed: %s", self.name, e, exc_info=True)

            # Special case: photo bursts/albums frequently arrive as multiple near-
            # simultaneous messages. Queue them without interrupting the active run,
            # then process them immediately after the current task finishes.
            if event.message_type == MessageType.PHOTO:
                logger.debug("[%s] Queuing photo follow-up for session %s without interrupt", self.name, session_key)
                merge_pending_message_event(self._pending_messages, session_key, event)
                return  # Don't interrupt now - will run after current task completes

            # Default behavior for non-photo follow-ups: interrupt the running agent.
            #
            # Use merge_text=True so rapid TEXT follow-ups (#4469) accumulate
            # into the single pending slot instead of clobbering each other.
            # Without merging, three rapid messages "A", "B", "C" land like:
            #   _pending_messages[k] = A  (interrupts)
            #   _pending_messages[k] = B  (replaces A before consumer reads)
            #   _pending_messages[k] = C  (replaces B)
            # ...and only "C" reaches the next turn.  merge_pending_message_event
            # already does the right thing for photo/media bursts; the
            # ``merge_text=True`` flag extends that to plain TEXT events.
            # Same shape as the Telegram bursty-grace path in gateway/run.py.
            logger.debug("[%s] New message while session %s is active — triggering interrupt", self.name, session_key)
            merge_pending_message_event(
                self._pending_messages,
                session_key,
                event,
                merge_text=True,
            )
            # Signal the interrupt (the processing task checks this)
            self._active_sessions[session_key].set()
            return  # Don't process now - will be handled after current task finishes
        
        # Mark session as active BEFORE spawning background task to close
        # the race window where a second message arriving before the task
        # starts would also pass the _active_sessions check and spawn a
        # duplicate task.  (grammY sequentialize / aiogram EventIsolation
        # pattern — set the guard synchronously, not inside the task.)
        # _start_session_processing installs the guard AND the owner-task
        # mapping atomically so stale-lock detection works.
        self._start_session_processing(event, session_key)
    
    @staticmethod
    def _get_human_delay() -> float:
        """
        Return a random delay in seconds for human-like response pacing.

        Reads from env vars:
          HERMES_HUMAN_DELAY_MODE: "off" (default) | "natural" | "custom"
          HERMES_HUMAN_DELAY_MIN_MS: minimum delay in ms (default 800, custom mode)
          HERMES_HUMAN_DELAY_MAX_MS: maximum delay in ms (default 2500, custom mode)
        """
        mode = os.getenv("HERMES_HUMAN_DELAY_MODE", "off").lower()
        if mode == "off":
            return 0.0
        if mode == "natural":
            min_ms, max_ms = 800, 2500
            return random.uniform(min_ms / 1000.0, max_ms / 1000.0)
        # custom mode — tolerate malformed env vars instead of crashing.
        try:
            min_ms = int(os.getenv("HERMES_HUMAN_DELAY_MIN_MS", "800"))
        except (TypeError, ValueError):
            min_ms = 800
        try:
            max_ms = int(os.getenv("HERMES_HUMAN_DELAY_MAX_MS", "2500"))
        except (TypeError, ValueError):
            max_ms = 2500
        return random.uniform(min_ms / 1000.0, max_ms / 1000.0)

    async def _process_message_background(self, event: MessageEvent, session_key: str) -> None:
        """Background task that actually processes the message."""
        # Track delivery outcomes for the processing-complete hook
        delivery_attempted = False
        delivery_succeeded = False

        def _record_delivery(result):
            nonlocal delivery_attempted, delivery_succeeded
            if result is None:
                return
            delivery_attempted = True
            if getattr(result, "success", False):
                delivery_succeeded = True

        # Reuse the interrupt event set by handle_message() (which marks
        # the session active before spawning this task to prevent races).
        # Fall back to a new Event only if the entry was removed externally.
        interrupt_event = self._active_sessions.get(session_key) or asyncio.Event()
        self._active_sessions[session_key] = interrupt_event
        
        # Start continuous typing indicator (refreshes every 2 seconds)
        _thread_metadata = _thread_metadata_for_source(event.source, _reply_anchor_for_event(event))
        _keep_typing_kwargs = {"metadata": _thread_metadata}
        try:
            _keep_typing_sig = inspect.signature(self._keep_typing)
        except (TypeError, ValueError):
            _keep_typing_sig = None
        if _keep_typing_sig is None or "stop_event" in _keep_typing_sig.parameters:
            _keep_typing_kwargs["stop_event"] = interrupt_event
        typing_task = asyncio.create_task(
            self._keep_typing(
                event.source.chat_id,
                **_keep_typing_kwargs,
            )
        )

        async def _stop_typing_task() -> None:
            typing_task.cancel()
            try:
                await asyncio.wait_for(asyncio.shield(typing_task), timeout=0.5)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                # Cancellation cleanup must not block adapter shutdown.  The
                # typing task is already cancelled; if the parent task is also
                # cancelling, let this message-processing task unwind now.
                pass
        
        try:
            await self._run_processing_hook("on_processing_start", event)

            # Call the handler (this can take a while with tool calls)
            response = await self._message_handler(event)

            # Slash-command handlers may return an EphemeralReply sentinel to
            # request that their reply message auto-delete after a TTL (used
            # for system notices like "✨ New session started!" that the user
            # doesn't need to keep in the thread).  Unwrap here so all the
            # downstream extract_media / text-processing logic sees a plain
            # string, and remember the TTL + platform capability so the
            # post-send block can schedule the deletion.
            response, _ephemeral_ttl = self._unwrap_ephemeral(response)

            # Send response if any.  A None/empty response is normal when
            # streaming already delivered the text (already_sent=True) or
            # when the message was queued behind an active agent.  Log at
            # DEBUG to avoid noisy warnings for expected behavior.
            #
            # Suppress stale response when the session was interrupted by a
            # new message that hasn't been consumed yet.  The pending message
            # is processed by the pending-message handler below (#8221/#2483).
            if (
                response
                and interrupt_event.is_set()
                and session_key in self._pending_messages
            ):
                logger.info(
                    "[%s] Suppressing stale response for interrupted session %s",
                    self.name,
                    session_key,
                )
                response = None
            if not response:
                logger.debug("[%s] Handler returned empty/None response for %s", self.name, event.source.chat_id)
            if response:
                # Capture [[as_document]] before extract_media strips it, so the
                # dispatch partition below can route image-extension files
                # through send_document instead of send_multiple_images. Used
                # by skills that produce large/lossless images (e.g. info-graph)
                # where Telegram's sendPhoto recompression destroys legibility.
                force_document_attachments = "[[as_document]]" in response

                # Extract MEDIA:<path> tags (from TTS tool) before other processing
                media_files, response = self.extract_media(response)
                media_files = self.filter_media_delivery_paths(media_files)

                # Extract image URLs and send them as native platform attachments
                images, text_content = self.extract_images(response)
                # Strip any remaining internal directives from message body (fixes #1561)
                text_content = text_content.replace("[[audio_as_voice]]", "").strip()
                text_content = text_content.replace("[[as_document]]", "").strip()
                text_content = re.sub(r"MEDIA:\s*\S+", "", text_content).strip()
                if images:
                    logger.info("[%s] extract_images found %d image(s) in response (%d chars)", self.name, len(images), len(response))

                # Auto-detect bare local file paths for native media delivery
                # (helps small models that don't use MEDIA: syntax)
                local_files, text_content = self.extract_local_files(text_content)
                local_files = self.filter_local_delivery_paths(local_files)
                if local_files:
                    logger.info("[%s] extract_local_files found %d file(s) in response", self.name, len(local_files))
                
                # Auto-TTS: if voice message, generate audio FIRST (before sending text)
                # Gated via ``_should_auto_tts_for_chat``: fires when the chat has
                # an explicit ``/voice on|tts`` opt-in OR when ``voice.auto_tts`` is
                # True globally and no ``/voice off`` has been issued.
                _tts_path = None
                if (self._should_auto_tts_for_chat(event.source.chat_id)
                        and event.message_type == MessageType.VOICE
                        and text_content
                        and not media_files):
                    try:
                        from tools.tts_tool import text_to_speech_tool, check_tts_requirements
                        if check_tts_requirements():
                            import json as _json
                            speech_text = self.prepare_tts_text(text_content)
                            if not speech_text:
                                raise ValueError("Empty text after markdown cleanup")
                            tts_result_str = await asyncio.to_thread(
                                text_to_speech_tool, text=speech_text
                            )
                            tts_data = _json.loads(tts_result_str)
                            _tts_path = tts_data.get("file_path")
                    except Exception as tts_err:
                        logger.warning("[%s] Auto-TTS failed: %s", self.name, tts_err)

                # Play TTS audio before text (voice-first experience)
                _tts_caption_delivered = False
                if _tts_path and Path(_tts_path).exists():
                    try:
                        telegram_tts_caption = None
                        if (
                            self.platform == Platform.TELEGRAM
                            and text_content
                            and text_content[:1024] == text_content
                        ):
                            telegram_tts_caption = text_content
                        tts_result = await self.play_tts(
                            chat_id=event.source.chat_id,
                            audio_path=_tts_path,
                            caption=telegram_tts_caption,
                            metadata=_thread_metadata,
                        )
                        _tts_caption_delivered = bool(
                            telegram_tts_caption and getattr(tts_result, "success", False)
                        )
                    finally:
                        try:
                            os.remove(_tts_path)
                        except OSError:
                            pass

                # Send the text portion
                if text_content and not _tts_caption_delivered:
                    logger.info("[%s] Sending response (%d chars) to %s", self.name, len(text_content), event.source.chat_id)
                    _reply_anchor = _reply_anchor_for_event(event)
                    # Mark final response messages for notification delivery.
                    # Platform adapters that support per-message notification
                    # control (e.g. Telegram's disable_notification) use this
                    # flag to override silent-mode and ensure the final
                    # response triggers a push notification.
                    # Clone to avoid mutating the metadata shared with the
                    # typing-indicator task (which must remain unmarked).
                    if _thread_metadata is not None:
                        _thread_metadata = dict(_thread_metadata)
                        _thread_metadata["notify"] = True
                    else:
                        _thread_metadata = {"notify": True}
                    result = await self._send_with_retry(
                        chat_id=event.source.chat_id,
                        content=text_content,
                        reply_to=_reply_anchor,
                        metadata=_thread_metadata,
                    )
                    _record_delivery(result)

                    # Schedule auto-deletion of system-notice replies.
                    # Detached so the handler returns immediately; errors
                    # (permission denied, message too old) are swallowed.
                    if (
                        _ephemeral_ttl
                        and _ephemeral_ttl > 0
                        and result.success
                        and result.message_id
                    ):
                        self._schedule_ephemeral_delete(
                            chat_id=event.source.chat_id,
                            message_id=result.message_id,
                            ttl_seconds=_ephemeral_ttl,
                        )

                # Human-like pacing delay between text and media
                human_delay = self._get_human_delay()

                # Send extracted images as native attachments
                if images:
                    logger.info("[%s] Extracted %d image(s) to send as attachments", self.name, len(images))
                    try:
                        await self.send_multiple_images(
                            chat_id=event.source.chat_id,
                            images=images,
                            metadata=_thread_metadata,
                            human_delay=human_delay,
                        )
                    except Exception as batch_err:
                        logger.warning("[%s] Error batching images: %s", self.name, batch_err, exc_info=True)


                # Send extracted media files — route by file type
                _VIDEO_EXTS = {'.mp4', '.mov', '.avi', '.mkv', '.webm', '.3gp'}
                _IMAGE_EXTS = {'.jpg', '.jpeg', '.png', '.webp', '.gif'}

                # Partition images out of media_files + local_files so they
                # can be sent as a single batch (Signal RPC). When
                # ``[[as_document]]`` was set on the original response, image
                # files skip the photo path and route to send_document below
                # so they're delivered with original bytes (no Telegram
                # sendPhoto recompression).
                from urllib.parse import quote as _quote
                _image_paths: list = []
                _non_image_media: list = []
                for media_path, is_voice in media_files:
                    _ext = Path(media_path).suffix.lower()
                    if (_ext in _IMAGE_EXTS
                            and not is_voice
                            and not force_document_attachments):
                        _image_paths.append(media_path)
                    else:
                        _non_image_media.append((media_path, is_voice))
                _non_image_local: list = []
                for file_path in local_files:
                    if (Path(file_path).suffix.lower() in _IMAGE_EXTS
                            and not force_document_attachments):
                        _image_paths.append(file_path)
                    else:
                        _non_image_local.append(file_path)

                if _image_paths:
                    try:
                        _batch = [(f"file://{_quote(p)}", "") for p in _image_paths]
                        await self.send_multiple_images(
                            chat_id=event.source.chat_id,
                            images=_batch,
                            metadata=_thread_metadata,
                            human_delay=human_delay,
                        )
                    except Exception as batch_err:
                        logger.warning("[%s] Error batching images: %s", self.name, batch_err, exc_info=True)

                for media_path, is_voice in _non_image_media:
                    if human_delay > 0:
                        await asyncio.sleep(human_delay)
                    try:
                        ext = Path(media_path).suffix.lower()
                        if should_send_media_as_audio(self.platform, ext, is_voice=is_voice):
                            media_result = await self.send_voice(
                                chat_id=event.source.chat_id,
                                audio_path=media_path,
                                metadata=_thread_metadata,
                            )
                        elif ext in _VIDEO_EXTS:
                            media_result = await self.send_video(
                                chat_id=event.source.chat_id,
                                video_path=media_path,
                                metadata=_thread_metadata,
                            )
                        else:
                            media_result = await self.send_document(
                                chat_id=event.source.chat_id,
                                file_path=media_path,
                                metadata=_thread_metadata,
                            )

                        if not media_result.success:
                            logger.warning("[%s] Failed to send media (%s): %s", self.name, ext, media_result.error)
                    except Exception as media_err:
                        logger.warning("[%s] Error sending media: %s", self.name, media_err)

                # Send auto-detected local non-image files as native attachments
                for file_path in _non_image_local:
                    if human_delay > 0:
                        await asyncio.sleep(human_delay)
                    try:
                        ext = Path(file_path).suffix.lower()
                        if ext in _VIDEO_EXTS:
                            await self.send_video(
                                chat_id=event.source.chat_id,
                                video_path=file_path,
                                metadata=_thread_metadata,
                            )
                        else:
                            await self.send_document(
                                chat_id=event.source.chat_id,
                                file_path=file_path,
                                metadata=_thread_metadata,
                            )
                    except Exception as file_err:
                        logger.error("[%s] Error sending local file %s: %s", self.name, file_path, file_err)

            # Determine overall success for the processing hook
            processing_ok = delivery_succeeded if delivery_attempted else not bool(response)
            await self._run_processing_hook(
                "on_processing_complete",
                event,
                ProcessingOutcome.SUCCESS if processing_ok else ProcessingOutcome.FAILURE,
            )

            # Check if there's a pending message that was queued during our processing
            if session_key in self._pending_messages:
                pending_event = self._pending_messages.pop(session_key)
                logger.debug("[%s] Processing queued message from interrupt", self.name)
                # Keep the _active_sessions entry live across the turn chain
                # and only CLEAR the interrupt Event — do NOT delete the entry.
                # If we deleted here, a concurrent inbound message arriving
                # during the awaits below would pass the Level-1 guard, spawn
                # its own _process_message_background, and run simultaneously
                # with the recursive drain below.  Two agents on one
                # session_key = duplicate responses, duplicate tool calls.
                # Clearing the Event keeps the guard live so follow-ups take
                # the busy-handler path (queue + interrupt) as intended.
                _active = self._active_sessions.get(session_key)
                if _active is not None:
                    _active.clear()
                await _stop_typing_task()
                # Spawn a fresh task for the pending message instead of
                # recursing.  Issue #17758: `await
                # self._process_message_background(...)` here grew the
                # call stack one frame per chained follow-up, and under
                # sustained pending-queue activity the C stack would
                # exhaust at ~2000 frames and SIGSEGV the process.
                # Mirror the late-arrival drain pattern below: hand off
                # to a new task and return so this frame can unwind.
                drain_task = asyncio.create_task(
                    self._process_message_background(pending_event, session_key)
                )
                # Hand ownership of the session to the drain task so
                # stale-lock detection keeps working while it runs.
                self._session_tasks[session_key] = drain_task
                try:
                    self._background_tasks.add(drain_task)
                    drain_task.add_done_callback(self._background_tasks.discard)
                except TypeError:
                    # Tests stub create_task() with non-hashable sentinels; tolerate.
                    pass
                return  # Drain task owns the session now.
                
        except asyncio.CancelledError:
            current_task = asyncio.current_task()
            outcome = ProcessingOutcome.CANCELLED
            if current_task is None or current_task not in self._expected_cancelled_tasks:
                outcome = ProcessingOutcome.FAILURE
            await self._run_processing_hook("on_processing_complete", event, outcome)
            raise
        except Exception as e:
            await self._run_processing_hook("on_processing_complete", event, ProcessingOutcome.FAILURE)
            logger.error("[%s] Error handling message: %s", self.name, e, exc_info=True)
            # Send the error to the user so they aren't left with radio silence
            try:
                error_type = type(e).__name__
                error_detail = str(e)[:300] if str(e) else "no details available"
                _thread_metadata = _thread_metadata_for_source(event.source, _reply_anchor_for_event(event))
                await self.send(
                    chat_id=event.source.chat_id,
                    content=(
                        f"Sorry, I encountered an error ({error_type}).\n"
                        f"{error_detail}\n"
                        "Try again or use /reset to start a fresh session."
                    ),
                    metadata=_thread_metadata,
                )
            except Exception:
                pass  # Last resort — don't let error reporting crash the handler
        finally:
            # Fire any one-shot post-delivery callback registered for this
            # session (e.g. deferred background-review notifications).
            #
            # Snapshot the callback generation HERE (after the agent has run),
            # not at the top of this task.  _hermes_run_generation is set on
            # the interrupt event by GatewayRunner._bind_adapter_run_generation
            # during _handle_message_with_agent — which happens DURING the
            # self._message_handler(event) await above.  Snapshotting earlier
            # always captured None, which bypassed the generation-ownership
            # check in pop_post_delivery_callback and let stale runs fire a
            # fresher run's callbacks.
            _callback_generation = getattr(
                interrupt_event,
                "_hermes_run_generation",
                None,
            )
            if hasattr(self, "pop_post_delivery_callback"):
                _post_cb = self.pop_post_delivery_callback(
                    session_key,
                    generation=_callback_generation,
                )
            else:
                _post_cb = getattr(self, "_post_delivery_callbacks", {}).pop(session_key, None)
            if callable(_post_cb):
                try:
                    _post_result = _post_cb()
                    if inspect.isawaitable(_post_result):
                        await _post_result
                except Exception:
                    pass
            # Stop typing indicator
            await _stop_typing_task()
            # Also cancel any platform-level persistent typing tasks (e.g. Discord)
            # that may have been recreated by _keep_typing after the last stop_typing()
            try:
                if hasattr(self, "stop_typing"):
                    await self.stop_typing(event.source.chat_id)
            except Exception:
                pass
            # Late-arrival drain: a message may have arrived during the
            # cleanup awaits above (typing_task cancel, stop_typing).  Such
            # messages passed the Level-1 guard (entry still live, Event
            # possibly set) and landed in _pending_messages via the
            # busy-handler path.  Without this block, we would delete the
            # active-session entry and the queued message would be silently
            # dropped (user never gets a reply).
            late_pending = self._pending_messages.pop(session_key, None)
            if late_pending is not None:
                current_task = asyncio.current_task()
                existing_task = self._session_tasks.get(session_key)
                if (
                    existing_task is not None
                    and existing_task is not current_task
                ):
                    # The in-band drain (or an earlier late-arrival drain)
                    # already spawned a follow-up task that owns this
                    # session.  Re-queue the late-arrival event so that
                    # task picks it up — avoids spawning two concurrent
                    # _process_message_background tasks for the same key
                    # (#17758 follow-up: prevents the create_task path
                    # from racing with itself across the in-band/finally
                    # boundary).
                    self._pending_messages[session_key] = late_pending
                else:
                    logger.debug(
                        "[%s] Late-arrival pending message during cleanup — spawning drain task",
                        self.name,
                    )
                    _active = self._active_sessions.get(session_key)
                    if _active is not None:
                        _active.clear()
                    drain_task = asyncio.create_task(
                        self._process_message_background(late_pending, session_key)
                    )
                    # Hand ownership of the session to the drain task so stale-lock
                    # detection keeps working while it runs.
                    self._session_tasks[session_key] = drain_task
                    try:
                        self._background_tasks.add(drain_task)
                        drain_task.add_done_callback(self._background_tasks.discard)
                    except TypeError:
                        # Tests stub create_task() with non-hashable sentinels; tolerate.
                        pass
                # Leave _active_sessions[session_key] populated — the drain
                # task's own lifecycle will clean it up.
            else:
                # Clean up session tracking.  Guard-match both deletes so a
                # reset-like command that already swapped in its own
                # command_guard (and cancelled us) can't be accidentally
                # cleared by our unwind.  The command owns the session now.
                #
                # The owner-check also covers the in-band drain handoff
                # above: when we spawned a drain_task and transferred
                # ownership via ``_session_tasks[session_key] = drain_task``,
                # ``_session_tasks.get(session_key) is current_task`` is
                # False, so we leave _active_sessions populated.  Without
                # this guard, the drain task picks up the same
                # interrupt_event in its own _process_message_background
                # entry, _release_session_guard's guard-match succeeds,
                # and we'd delete the entry while the drain task is still
                # running — letting a concurrent inbound message pass
                # the Level-1 guard and spawn a second handler for the
                # same session.
                current_task = asyncio.current_task()
                if current_task is not None and self._session_tasks.get(session_key) is current_task:
                    del self._session_tasks[session_key]
                    self._release_session_guard(session_key, guard=interrupt_event)
    
    async def cancel_background_tasks(self) -> None:
        """Cancel any in-flight background message-processing tasks.

        Used during gateway shutdown/replacement so active sessions from the old
        process do not keep running after adapters are being torn down.

        Each cancelled task is awaited with a 5s bound so a wedged finally
        (typing-task cleanup, on_processing_complete hook) can't stall the
        whole shutdown path.  Stragglers are released from our tracking and
        allowed to finish unwinding on their own.
        """
        # Loop until no new tasks appear.  Without this, a message
        # arriving during the `await asyncio.gather` below would spawn
        # a fresh _process_message_background task (added to
        # self._background_tasks at line ~1668 via handle_message),
        # and the _background_tasks.clear() at the end of this method
        # would drop the reference — the task runs untracked against a
        # disconnecting adapter, logs send-failures, and may linger
        # until it completes on its own.  Retrying the drain until the
        # task set stabilizes closes the window.
        MAX_DRAIN_ROUNDS = 5
        for _ in range(MAX_DRAIN_ROUNDS):
            tasks = [task for task in self._background_tasks if not task.done()]
            if not tasks:
                break
            for task in tasks:
                self._expected_cancelled_tasks.add(task)
                task.cancel()
            try:
                await asyncio.wait_for(
                    asyncio.gather(
                        *(asyncio.shield(t) for t in tasks),
                        return_exceptions=True,
                    ),
                    timeout=5.0,
                )
            except asyncio.TimeoutError:
                logger.warning(
                    "[%s] %d background task(s) did not exit within 5s; "
                    "releasing tracking and letting them unwind in the background",
                    self.name, len([t for t in tasks if not t.done()]),
                )
                break
            # Loop: late-arrival tasks spawned during the gather above
            # will be in self._background_tasks now.  Re-check.
        self._background_tasks.clear()
        self._expected_cancelled_tasks.clear()
        self._session_tasks.clear()
        self._pending_messages.clear()
        self._active_sessions.clear()

    def has_pending_interrupt(self, session_key: str) -> bool:
        """Check if there's a pending interrupt for a session."""
        return session_key in self._active_sessions and self._active_sessions[session_key].is_set()
    
    def get_pending_message(self, session_key: str) -> Optional[MessageEvent]:
        """Get and clear any pending message for a session."""
        return self._pending_messages.pop(session_key, None)
    
    def build_source(
        self,
        chat_id: str,
        chat_name: Optional[str] = None,
        chat_type: str = "dm",
        user_id: Optional[str] = None,
        user_name: Optional[str] = None,
        thread_id: Optional[str] = None,
        chat_topic: Optional[str] = None,
        user_id_alt: Optional[str] = None,
        chat_id_alt: Optional[str] = None,
        is_bot: bool = False,
        guild_id: Optional[str] = None,
        parent_chat_id: Optional[str] = None,
        message_id: Optional[str] = None,
    ) -> SessionSource:
        """Helper to build a SessionSource for this platform."""
        # Normalize empty topic to None
        if chat_topic is not None and not chat_topic.strip():
            chat_topic = None
        return SessionSource(
            platform=self.platform,
            chat_id=str(chat_id),
            chat_name=chat_name,
            chat_type=chat_type,
            user_id=str(user_id) if user_id else None,
            user_name=user_name,
            thread_id=str(thread_id) if thread_id else None,
            chat_topic=chat_topic.strip() if chat_topic else None,
            user_id_alt=user_id_alt,
            chat_id_alt=chat_id_alt,
            is_bot=is_bot,
            guild_id=str(guild_id) if guild_id else None,
            parent_chat_id=str(parent_chat_id) if parent_chat_id else None,
            message_id=str(message_id) if message_id else None,
        )
    
    @abstractmethod
    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        """
        Get information about a chat/channel.
        
        Returns dict with at least:
        - name: Chat name
        - type: "dm", "group", "channel"
        """
        pass
    
    def format_message(self, content: str) -> str:
        """
        Format a message for this platform.
        
        Override in subclasses to handle platform-specific formatting
        (e.g., Telegram MarkdownV2, Discord markdown).
        
        Default implementation returns content as-is.
        """
        return content
    
    @staticmethod
    def truncate_message(
        content: str,
        max_length: int = 4096,
        len_fn: Optional["Callable[[str], int]"] = None,
    ) -> List[str]:
        """
        Split a long message into chunks, preserving code block boundaries.

        When a split falls inside a triple-backtick code block, the fence is
        closed at the end of the current chunk and reopened (with the original
        language tag) at the start of the next chunk.  Multi-chunk responses
        receive indicators like ``(1/3)``.

        Args:
            content: The full message content
            max_length: Maximum length per chunk (platform-specific)
            len_fn: Optional length function for measuring string length.
                     Defaults to ``len`` (Unicode code-points).  Pass
                     ``utf16_len`` for platforms that measure message
                     length in UTF-16 code units (e.g. Telegram).

        Returns:
            List of message chunks
        """
        _len = len_fn or len
        if _len(content) <= max_length:
            return [content]

        INDICATOR_RESERVE = 10   # room for " (XX/XX)"
        FENCE_CLOSE = "\n```"

        chunks: List[str] = []
        remaining = content
        # When the previous chunk ended mid-code-block, this holds the
        # language tag (possibly "") so we can reopen the fence.
        carry_lang: Optional[str] = None

        while remaining:
            # If we're continuing a code block from the previous chunk,
            # prepend a new opening fence with the same language tag.
            prefix = f"```{carry_lang}\n" if carry_lang is not None else ""

            # How much body text we can fit after accounting for the prefix,
            # a potential closing fence, and the chunk indicator.
            headroom = max_length - INDICATOR_RESERVE - _len(prefix) - _len(FENCE_CLOSE)
            if headroom < 1:
                headroom = max_length // 2

            # Everything remaining fits in one final chunk
            if _len(prefix) + _len(remaining) <= max_length - INDICATOR_RESERVE:
                chunks.append(prefix + remaining)
                break

            # Find a natural split point (prefer newlines, then spaces).
            # When _len != len (e.g. utf16_len for Telegram), headroom is
            # measured in the custom unit.  We need codepoint-based slice
            # positions that stay within the custom-unit budget.
            #
            # _safe_slice_pos() maps a custom-unit budget to the largest
            # codepoint offset whose custom length ≤ budget.
            if _len is not len:
                # Map headroom (custom units) → codepoint slice length
                _cp_limit = _custom_unit_to_cp(remaining, headroom, _len)
            else:
                _cp_limit = headroom
            region = remaining[:_cp_limit]
            split_at = region.rfind("\n")
            if split_at < _cp_limit // 2:
                split_at = region.rfind(" ")
            if split_at < 1:
                split_at = _cp_limit

            # Avoid splitting inside an inline code span (`...`).
            # If the text before split_at has an odd number of unescaped
            # backticks, the split falls inside inline code — the resulting
            # chunk would have an unpaired backtick and any special characters
            # (like parentheses) inside the broken span would be unescaped,
            # causing MarkdownV2 parse errors on Telegram.
            candidate = remaining[:split_at]
            backtick_count = candidate.count("`") - candidate.count("\\`")
            if backtick_count % 2 == 1:
                # Find the last unescaped backtick and split before it
                last_bt = candidate.rfind("`")
                while last_bt > 0 and candidate[last_bt - 1] == "\\":
                    last_bt = candidate.rfind("`", 0, last_bt)
                if last_bt > 0:
                    # Try to find a space or newline just before the backtick
                    safe_split = candidate.rfind(" ", 0, last_bt)
                    nl_split = candidate.rfind("\n", 0, last_bt)
                    safe_split = max(safe_split, nl_split)
                    if safe_split > _cp_limit // 4:
                        split_at = safe_split

            chunk_body = remaining[:split_at]
            remaining = remaining[split_at:].lstrip()

            full_chunk = prefix + chunk_body

            # Walk only the chunk_body (not the prefix we prepended) to
            # determine whether we end inside an open code block.
            in_code = carry_lang is not None
            lang = carry_lang or ""
            for line in chunk_body.split("\n"):
                stripped = line.strip()
                if stripped.startswith("```"):
                    if in_code:
                        in_code = False
                        lang = ""
                    else:
                        in_code = True
                        tag = stripped[3:].strip()
                        lang = tag.split()[0] if tag else ""

            if in_code:
                # Close the orphaned fence so the chunk is valid on its own
                full_chunk += FENCE_CLOSE
                carry_lang = lang
            else:
                carry_lang = None

            chunks.append(full_chunk)

        # Append chunk indicators when the response spans multiple messages
        if len(chunks) > 1:
            total = len(chunks)
            chunks = [
                f"{chunk} ({i + 1}/{total})" for i, chunk in enumerate(chunks)
            ]

        return chunks
