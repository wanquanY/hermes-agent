"""Feishu inbound content extraction, media caching, and sender profile helpers."""

from __future__ import annotations

import asyncio
import json
import logging
import mimetypes
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

try:
    from lark_oapi.core import AccessTokenType, HttpMethod
    from lark_oapi.core.model import BaseRequest
except ImportError:
    AccessTokenType = None  # type: ignore[assignment]
    HttpMethod = None  # type: ignore[assignment]
    BaseRequest = None  # type: ignore[assignment]

from channels.platforms.base import (
    MessageType,
    SUPPORTED_DOCUMENT_TYPES,
    cache_audio_from_bytes,
    cache_document_from_bytes,
    cache_image_from_url,
    cache_image_from_bytes,
)
from channels.platforms.feishu_message import (
    FeishuMentionRef,
    FeishuNormalizedMessage,
    normalize_feishu_message,
)

logger = logging.getLogger(__name__)

_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}
_AUDIO_EXTENSIONS = {".ogg", ".mp3", ".wav", ".m4a", ".aac", ".flac", ".opus", ".webm"}
_VIDEO_EXTENSIONS = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".m4v", ".3gp"}
_DOCUMENT_MIME_TO_EXT = {mime: ext for ext, mime in SUPPORTED_DOCUMENT_TYPES.items()}
_MAX_TEXT_INJECT_BYTES = 100 * 1024
_FEISHU_SENDER_NAME_TTL_SECONDS = 10 * 60


def _feishu_public_attr(name: str, fallback: Any = None) -> Any:
    module = sys.modules.get("channels.platforms.feishu")
    return getattr(module, name, fallback) if module is not None else fallback


class FeishuContentMixin:
    async def _extract_message_content(
        self, message: Any
    ) -> tuple[str, MessageType, List[str], List[str], List[FeishuMentionRef]]:
        raw_content = getattr(message, "content", "") or ""
        raw_type = getattr(message, "message_type", "") or ""
        message_id = str(getattr(message, "message_id", "") or "")
        logger.info("[Feishu] Received raw message type=%s message_id=%s", raw_type, message_id)

        normalized = normalize_feishu_message(
            message_type=raw_type,
            raw_content=raw_content,
            mentions=getattr(message, "mentions", None),
            bot=self._bot_identity(),
        )
        media_urls, media_types = await self._download_feishu_message_resources(
            message_id=message_id,
            normalized=normalized,
        )
        inbound_type = self._resolve_normalized_message_type(normalized, media_types)
        text = normalized.text_content

        if (
            inbound_type in {MessageType.DOCUMENT, MessageType.AUDIO, MessageType.VIDEO, MessageType.PHOTO}
            and len(media_urls) == 1
            and normalized.preferred_message_type in {"document", "audio"}
        ):
            injected = await self._maybe_extract_text_document(media_urls[0], media_types[0])
            if injected:
                text = injected

        return text, inbound_type, media_urls, media_types, list(normalized.mentions)

    async def _download_feishu_message_resources(
        self,
        *,
        message_id: str,
        normalized: FeishuNormalizedMessage,
    ) -> tuple[List[str], List[str]]:
        media_urls: List[str] = []
        media_types: List[str] = []

        for image_key in normalized.image_keys:
            cached_path, media_type = await self._download_feishu_image(
                message_id=message_id,
                image_key=image_key,
            )
            if cached_path:
                media_urls.append(cached_path)
                media_types.append(media_type)

        for media_ref in normalized.media_refs:
            cached_path, media_type = await self._download_feishu_message_resource(
                message_id=message_id,
                file_key=media_ref.file_key,
                resource_type=media_ref.resource_type,
                fallback_filename=media_ref.file_name,
            )
            if cached_path:
                media_urls.append(cached_path)
                media_types.append(media_type)

        return media_urls, media_types

    @staticmethod
    def _resolve_media_message_type(media_type: str, *, default: MessageType) -> MessageType:
        normalized = (media_type or "").lower()
        if normalized.startswith("image/"):
            return MessageType.PHOTO
        if normalized.startswith("audio/"):
            return MessageType.AUDIO
        if normalized.startswith("video/"):
            return MessageType.VIDEO
        return default

    def _resolve_normalized_message_type(
        self,
        normalized: FeishuNormalizedMessage,
        media_types: List[str],
    ) -> MessageType:
        preferred = normalized.preferred_message_type
        if preferred == "photo":
            return self._resolve_media_message_type(media_types[0] if media_types else "", default=MessageType.PHOTO)
        if preferred == "audio":
            return self._resolve_media_message_type(media_types[0] if media_types else "", default=MessageType.AUDIO)
        if preferred == "document":
            return self._resolve_media_message_type(media_types[0] if media_types else "", default=MessageType.DOCUMENT)
        return MessageType.TEXT

    async def _maybe_extract_text_document(self, cached_path: str, media_type: str) -> str:
        if not cached_path or not media_type.startswith("text/"):
            return ""
        try:
            if os.path.getsize(cached_path) > _MAX_TEXT_INJECT_BYTES:
                return ""
            ext = Path(cached_path).suffix.lower()
            if ext not in {".txt", ".md"} and media_type not in {"text/plain", "text/markdown"}:
                return ""
            content = Path(cached_path).read_text(encoding="utf-8")
            display_name = self._display_name_from_cached_path(cached_path)
            return f"[Content of {display_name}]:\n{content}"
        except (OSError, UnicodeDecodeError):
            logger.warning("[Feishu] Failed to inject text document content from %s", cached_path, exc_info=True)
            return ""

    async def _download_feishu_image(self, *, message_id: str, image_key: str) -> tuple[str, str]:
        if not self._client or not message_id:
            return "", ""
        try:
            request = self._build_message_resource_request(
                message_id=message_id,
                file_key=image_key,
                resource_type="image",
            )
            response = await asyncio.to_thread(self._client.im.v1.message_resource.get, request)
            if not response or not response.success():
                logger.warning(
                    "[Feishu] Failed to download image %s: %s %s",
                    image_key,
                    getattr(response, "code", "unknown"),
                    getattr(response, "msg", "request failed"),
                )
                return "", ""
            raw_bytes = self._read_binary_response(response)
            if not raw_bytes:
                return "", ""
            content_type = self._get_response_header(response, "Content-Type")
            filename = getattr(response, "file_name", None) or f"{image_key}.jpg"
            ext = self._guess_extension(filename, content_type, ".jpg", allowed=_IMAGE_EXTENSIONS)
            cached_path = cache_image_from_bytes(raw_bytes, ext=ext)
            media_type = self._normalize_media_type(content_type, default=self._default_image_media_type(ext))
            return cached_path, media_type
        except Exception:
            logger.warning("[Feishu] Failed to cache image resource %s", image_key, exc_info=True)
            return "", ""

    async def _download_feishu_message_resource(
        self,
        *,
        message_id: str,
        file_key: str,
        resource_type: str,
        fallback_filename: str,
    ) -> tuple[str, str]:
        if not self._client or not message_id:
            return "", ""

        request_types = [resource_type]
        if resource_type in {"audio", "media"}:
            request_types.append("file")

        for request_type in request_types:
            try:
                request = self._build_message_resource_request(
                    message_id=message_id,
                    file_key=file_key,
                    resource_type=request_type,
                )
                response = await asyncio.to_thread(self._client.im.v1.message_resource.get, request)
                if not response or not response.success():
                    logger.debug(
                        "[Feishu] Resource download failed for %s/%s via type=%s: %s %s",
                        message_id,
                        file_key,
                        request_type,
                        getattr(response, "code", "unknown"),
                        getattr(response, "msg", "request failed"),
                    )
                    continue

                raw_bytes = self._read_binary_response(response)
                if not raw_bytes:
                    continue
                content_type = self._get_response_header(response, "Content-Type")
                response_filename = getattr(response, "file_name", None) or ""
                filename = response_filename or fallback_filename or f"{request_type}_{file_key}"
                media_type = self._normalize_media_type(
                    content_type,
                    default=self._guess_media_type_from_filename(filename),
                )

                if media_type.startswith("image/"):
                    ext = self._guess_extension(filename, content_type, ".jpg", allowed=_IMAGE_EXTENSIONS)
                    cached_path = cache_image_from_bytes(raw_bytes, ext=ext)
                    logger.info("[Feishu] Cached message image resource at %s", cached_path)
                    return cached_path, media_type or self._default_image_media_type(ext)

                if request_type == "audio" or media_type.startswith("audio/"):
                    ext = self._guess_extension(filename, content_type, ".ogg", allowed=_AUDIO_EXTENSIONS)
                    cached_path = cache_audio_from_bytes(raw_bytes, ext=ext)
                    logger.info("[Feishu] Cached message audio resource at %s", cached_path)
                    return cached_path, (media_type or f"audio/{ext.lstrip('.') or 'ogg'}")

                if media_type.startswith("video/"):
                    if not Path(filename).suffix:
                        filename = f"{filename}.mp4"
                    cached_path = cache_document_from_bytes(raw_bytes, filename)
                    logger.info("[Feishu] Cached message video resource at %s", cached_path)
                    return cached_path, media_type

                if not Path(filename).suffix and media_type in _DOCUMENT_MIME_TO_EXT:
                    filename = f"{filename}{_DOCUMENT_MIME_TO_EXT[media_type]}"
                cached_path = cache_document_from_bytes(raw_bytes, filename)
                logger.info("[Feishu] Cached message document resource at %s", cached_path)
                return cached_path, (media_type or self._guess_document_media_type(filename))
            except Exception:
                logger.warning(
                    "[Feishu] Failed to cache message resource %s/%s",
                    message_id,
                    file_key,
                    exc_info=True,
                )
        return "", ""

    # =========================================================================
    # Static helpers — extension / media-type guessing
    # =========================================================================

    @staticmethod
    def _read_binary_response(response: Any) -> bytes:
        file_obj = getattr(response, "file", None)
        if file_obj is None:
            return b""
        if hasattr(file_obj, "getvalue"):
            return bytes(file_obj.getvalue())
        return bytes(file_obj.read())

    @staticmethod
    def _get_response_header(response: Any, name: str) -> str:
        raw = getattr(response, "raw", None)
        headers = getattr(raw, "headers", {}) or {}
        return str(headers.get(name, headers.get(name.lower(), "")) or "").split(";", 1)[0].strip().lower()

    @staticmethod
    def _guess_extension(filename: str, content_type: str, default: str, *, allowed: set[str]) -> str:
        ext = Path(filename or "").suffix.lower()
        if ext in allowed:
            return ext
        guessed = mimetypes.guess_extension((content_type or "").split(";", 1)[0].strip().lower() or "")
        if guessed in allowed:
            return guessed
        return default

    @staticmethod
    def _normalize_media_type(content_type: str, *, default: str) -> str:
        normalized = (content_type or "").split(";", 1)[0].strip().lower()
        return normalized or default

    @staticmethod
    def _guess_document_media_type(filename: str) -> str:
        ext = Path(filename or "").suffix.lower()
        return SUPPORTED_DOCUMENT_TYPES.get(ext, mimetypes.guess_type(filename or "")[0] or "application/octet-stream")

    @staticmethod
    def _display_name_from_cached_path(path: str) -> str:
        basename = os.path.basename(path)
        parts = basename.split("_", 2)
        display_name = parts[2] if len(parts) >= 3 else basename
        return re.sub(r"[^\w.\- ]", "_", display_name)

    @staticmethod
    def _guess_media_type_from_filename(filename: str) -> str:
        guessed = (mimetypes.guess_type(filename or "")[0] or "").lower()
        if guessed:
            return guessed
        ext = Path(filename or "").suffix.lower()
        if ext in _VIDEO_EXTENSIONS:
            return f"video/{ext.lstrip('.')}"
        if ext in _AUDIO_EXTENSIONS:
            return f"audio/{ext.lstrip('.')}"
        if ext in _IMAGE_EXTENSIONS:
            return FeishuAdapter._default_image_media_type(ext)
        return ""

    @staticmethod
    def _map_chat_type(raw_chat_type: str) -> str:
        normalized = (raw_chat_type or "").strip().lower()
        if normalized == "p2p":
            return "dm"
        if "topic" in normalized or "thread" in normalized or "forum" in normalized:
            return "forum"
        if normalized == "group":
            return "group"
        return "dm"

    @staticmethod
    def _resolve_source_chat_type(*, chat_info: Dict[str, Any], event_chat_type: str) -> str:
        resolved = str(chat_info.get("type") or "").strip().lower()
        if resolved in {"group", "forum"}:
            return resolved
        if event_chat_type == "p2p":
            return "dm"
        return "group"

    async def _resolve_sender_profile(
        self,
        sender_id: Any,
        *,
        is_bot: bool = False,
    ) -> Dict[str, Optional[str]]:
        """Map Feishu's three-tier user IDs onto Hermes' SessionSource fields.

        Preference order for the primary ``user_id`` field:
          1. user_id for bot senders when Feishu provides it, because bot name
             lookup still needs open_id but the sender profile should preserve
             the tenant-scoped identity.
          2. open_id for human senders, which is app-scoped, always available,
             and what QR onboarding saves.
          3. user_id fallback when open_id is unavailable.

        ``user_id_alt`` carries the union_id (developer-scoped, stable across
        all apps by the same developer) when present, otherwise the tenant
        user_id.  Session-key generation prefers user_id_alt when present, so
        participant isolation can still use a stable non-app-scoped ID without
        breaking open_id-based allowlists.
        """
        open_id = getattr(sender_id, "open_id", None) or None
        user_id = getattr(sender_id, "user_id", None) or None
        union_id = getattr(sender_id, "union_id", None) or None
        primary_id = (user_id or open_id) if is_bot else (open_id or user_id)
        alternate_id = union_id or (
            user_id if user_id and user_id != primary_id else open_id if open_id != primary_id else None
        )
        # bot/v3/bots/basic_batch only accepts open_id.
        name_lookup_id = open_id if is_bot else (primary_id or alternate_id)
        display_name = await self._resolve_sender_name_from_api(
            name_lookup_id, is_bot=is_bot,
        )
        return {
            "user_id": primary_id,
            "user_name": display_name,
            "user_id_alt": alternate_id,
        }

    def _get_cached_sender_name(self, sender_id: Optional[str]) -> Optional[str]:
        """Return a cached sender name only while its TTL is still valid."""
        if not sender_id:
            return None
        cached = self._sender_name_cache.get(sender_id)
        if cached is None:
            return None
        name, expire_at = cached
        if time.time() < expire_at:
            return name
        self._sender_name_cache.pop(sender_id, None)
        return None

    async def _resolve_sender_name_from_api(
        self,
        sender_id: Optional[str],
        *,
        is_bot: bool = False,
    ) -> Optional[str]:
        """Bots divert to bot/basic_batch — contact API doesn't return bot names.
        Failures are silent so the pipeline never blocks on name resolution.
        """
        if not sender_id or not self._client:
            return None
        trimmed = sender_id.strip()
        if not trimmed:
            return None
        now = time.time()
        cached_name = self._get_cached_sender_name(trimmed)
        if cached_name is not None:
            return cached_name or None  # "" cached means "known nameless"
        if is_bot:
            names = await self._fetch_bot_names([trimmed])
            if names is None:
                return None
            expire_at = now + _FEISHU_SENDER_NAME_TTL_SECONDS
            for oid, name in names.items():
                self._sender_name_cache[oid] = (name, expire_at)
            hit = self._sender_name_cache.get(trimmed)
            return (hit[0] or None) if hit else None
        try:
            from lark_oapi.api.contact.v3 import GetUserRequest  # lazy import
            if trimmed.startswith("ou_"):
                id_type = "open_id"
            elif trimmed.startswith("on_"):
                id_type = "union_id"
            else:
                id_type = "user_id"
            request = GetUserRequest.builder().user_id(trimmed).user_id_type(id_type).build()
            response = await asyncio.to_thread(self._client.contact.v3.user.get, request)
            if not response or not response.success():
                return None
            user = getattr(getattr(response, "data", None), "user", None)
            name = (
                getattr(user, "name", None)
                or getattr(user, "display_name", None)
                or getattr(user, "nickname", None)
                or getattr(user, "en_name", None)
            )
            if name and isinstance(name, str):
                name = name.strip()
                if name:
                    self._sender_name_cache[trimmed] = (name, now + _FEISHU_SENDER_NAME_TTL_SECONDS)
                    return name
        except Exception:
            logger.debug("[Feishu] Failed to resolve sender name for %s", sender_id, exc_info=True)
        return None

    async def _fetch_bot_names(self, bot_ids: List[str]) -> Optional[Dict[str, str]]:
        if not self._client or not bot_ids:
            return None
        try:
            req = (
                BaseRequest.builder()
                .http_method(HttpMethod.GET)
                .uri("/open-apis/bot/v3/bots/basic_batch")
                .queries([("bot_ids", oid) for oid in bot_ids])
                .token_types({AccessTokenType.TENANT})
                .build()
            )
            resp = await asyncio.to_thread(self._client.request, req)
            content = getattr(getattr(resp, "raw", None), "content", None)
            if not content:
                return None
            payload = json.loads(content)
            if payload.get("code") != 0:
                return None
            bots = (payload.get("data") or {}).get("bots") or {}
            return {
                oid: str(info.get("name") or "").strip()
                for oid, info in bots.items()
                if oid
            }
        except Exception:
            logger.debug("[Feishu] Failed to fetch bot names for %s", bot_ids, exc_info=True)
            return None

    async def _fetch_message_text(self, message_id: str) -> Optional[str]:
        if not self._client or not message_id:
            return None
        if message_id in self._message_text_cache:
            return self._message_text_cache[message_id]
        try:
            request = self._build_get_message_request(message_id)
            response = await asyncio.to_thread(self._client.im.v1.message.get, request)
            if not response or getattr(response, "success", lambda: False)() is False:
                code = getattr(response, "code", "unknown")
                msg = getattr(response, "msg", "message lookup failed")
                logger.warning("[Feishu] Failed to fetch parent message %s: [%s] %s", message_id, code, msg)
                return None
            items = getattr(getattr(response, "data", None), "items", None) or []
            parent = items[0] if items else None
            body = getattr(parent, "body", None)
            msg_type = getattr(parent, "msg_type", "") or ""
            raw_content = getattr(body, "content", "") or ""
            parent_mentions = getattr(parent, "mentions", None) if parent else None
            text = self._extract_text_from_raw_content(
                msg_type=msg_type,
                raw_content=raw_content,
                mentions=parent_mentions,
            )
            self._message_text_cache[message_id] = text
            return text
        except Exception:
            logger.warning("[Feishu] Failed to fetch parent message %s", message_id, exc_info=True)
            return None

    def _extract_text_from_raw_content(
        self,
        *,
        msg_type: str,
        raw_content: str,
        mentions: Optional[Sequence[Any]] = None,
    ) -> Optional[str]:
        normalized = normalize_feishu_message(
            message_type=msg_type,
            raw_content=raw_content,
            mentions=mentions,
            bot=self._bot_identity(),
        )
        if normalized.text_content:
            return normalized.text_content
        placeholder = normalized.metadata.get("placeholder_text") if isinstance(normalized.metadata, dict) else None
        return str(placeholder).strip() or None

    @staticmethod
    def _default_image_media_type(ext: str) -> str:
        normalized_ext = (ext or "").lower()
        if normalized_ext in {".jpg", ".jpeg"}:
            return "image/jpeg"
        return f"image/{normalized_ext.lstrip('.') or 'jpeg'}"

    @staticmethod
    def _log_background_failure(future: Any) -> None:
        try:
            future.result()
        except Exception:
            logger.exception("[Feishu] Background inbound processing failed")

    async def _download_remote_image(self, image_url: str) -> str:
        ext = self._guess_remote_extension(image_url, default=".jpg")
        cache_fn = _feishu_public_attr("cache_image_from_url", cache_image_from_url)
        return await cache_fn(image_url, ext=ext)

    async def _download_remote_document(
        self,
        file_url: str,
        *,
        default_ext: str,
        preferred_name: str,
    ) -> tuple[str, str]:
        from tools.url_safety import is_safe_url
        if not is_safe_url(file_url):
            raise ValueError(f"Blocked unsafe URL (SSRF protection): {file_url[:80]}")

        import httpx

        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
            response = await client.get(
                file_url,
                headers={
                    "User-Agent": "Mozilla/5.0 (compatible; HermesAgent/1.0)",
                    "Accept": "*/*",
                },
            )
            response.raise_for_status()
            # Snapshot Content-Type and body while the client context is
            # still active so pooled connections fully release on exit.
            # See #18451.
            content_type_hdr = str(response.headers.get("Content-Type", ""))
            body = response.content
        filename = self._derive_remote_filename(
            file_url,
            content_type=content_type_hdr,
            default_name=preferred_name,
            default_ext=default_ext,
        )
        cache_fn = _feishu_public_attr("cache_document_from_bytes", cache_document_from_bytes)
        cached_path = cache_fn(body, filename)
        return cached_path, filename

    @staticmethod
    def _guess_remote_extension(url: str, *, default: str) -> str:
        ext = Path((url or "").split("?", 1)[0]).suffix.lower()
        return ext if ext in (_IMAGE_EXTENSIONS | _AUDIO_EXTENSIONS | _VIDEO_EXTENSIONS | set(SUPPORTED_DOCUMENT_TYPES)) else default

    @staticmethod
    def _derive_remote_filename(file_url: str, *, content_type: str, default_name: str, default_ext: str) -> str:
        candidate = Path((file_url or "").split("?", 1)[0]).name or default_name
        ext = Path(candidate).suffix.lower()
        if not ext:
            guessed = mimetypes.guess_extension((content_type or "").split(";", 1)[0].strip().lower() or "") or default_ext
            candidate = f"{candidate}{guessed}"
        return candidate
