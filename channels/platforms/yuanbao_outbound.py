"""Yuanbao outbound delivery managers."""

from __future__ import annotations

import asyncio
import collections
import json
import logging
import os
import re
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Callable, ClassVar, Dict, List, Optional, Tuple

from channels.platforms.base import SendResult
from channels.platforms.yuanbao_constants import (
    DEFAULT_SEND_TIMEOUT,
    REPLY_HEARTBEAT_INTERVAL_S,
    REPLY_HEARTBEAT_TIMEOUT_S,
    SLOW_RESPONSE_MESSAGE,
    SLOW_RESPONSE_TIMEOUT_S,
    _INDICATOR_RE,
)
from channels.platforms.yuanbao_markdown import MarkdownProcessor
from channels.platforms.yuanbao_media import (
    build_file_msg_body,
    build_image_msg_body,
    download_url as media_download_url,
    get_cos_credentials,
    guess_mime_type,
    md5_hex,
    upload_to_cos,
)
from channels.platforms.yuanbao_proto import (
    WS_HEARTBEAT_FINISH,
    WS_HEARTBEAT_RUNNING,
    decode_get_group_member_list_rsp,
    decode_query_group_info_rsp,
    encode_get_group_member_list,
    encode_query_group_info,
    encode_send_c2c_message,
    encode_send_group_heartbeat,
    encode_send_group_message,
    encode_send_private_heartbeat,
    next_seq_no,
)

logger = logging.getLogger(__name__)

class MediaSendHandler(ABC):
    """Abstract base class for media send strategies.

    Subclasses implement:
      - acquire_file(): how to obtain file bytes (download URL / read local)
      - build_msg_body(): how to build TIMxxxElem from upload result

    The shared flow (check ws → cancel notifier → validate → COS upload
    → lock → dispatch) is handled by the base handle() template method.
    """

    @abstractmethod
    async def acquire_file(
        self, adapter: "YuanbaoAdapter", **kwargs: Any,
    ) -> Tuple[bytes, str, str]:
        """Return (file_bytes, filename, content_type).

        Raises:
            ValueError: when file cannot be acquired (not found, empty, etc.)
        """

    @abstractmethod
    def build_msg_body(self, upload_result: dict, **kwargs: Any) -> list:
        """Build platform-specific MsgBody list from COS upload result."""

    def needs_cos_upload(self) -> bool:
        """Override to return False for non-COS media (e.g. sticker)."""
        return True

    async def handle(
        self,
        adapter: "YuanbaoAdapter",
        chat_id: str,
        reply_to: Optional[str] = None,
        caption: Optional[str] = None,
        **kwargs: Any,
    ) -> "SendResult":
        """Template method: shared media send flow."""
        conn = adapter._connection
        sender = adapter._outbound.sender

        if conn.ws is None:
            return SendResult(success=False, error="Not connected", retryable=True)

        adapter._outbound.cancel_slow_notifier(chat_id)

        try:
            # 1. Acquire file bytes
            file_bytes, filename, content_type = await self.acquire_file(
                adapter, **kwargs,
            )

            # 2. Validate (only for handlers that upload to COS; stickers use
            # TIMFaceElem and legitimately carry no file bytes, so skipping
            # validate_media here avoids a spurious "Empty file: sticker").
            if self.needs_cos_upload():
                validation_err = MessageSender.validate_media(
                    file_bytes, filename, adapter.MEDIA_MAX_SIZE_MB,
                )
                if validation_err:
                    return SendResult(success=False, error=validation_err)

            if self.needs_cos_upload():
                file_uuid = md5_hex(file_bytes)

                # 3. Get COS upload credentials
                token_data = await adapter._get_cached_token()
                token: str = token_data.get("token", "")
                bot_id: str = (
                    token_data.get("bot_id", "") or adapter._bot_id or ""
                )

                credentials = await get_cos_credentials(
                    app_key=adapter._app_key,
                    api_domain=adapter._api_domain,
                    token=token,
                    filename=filename,
                    bot_id=bot_id,
                    route_env=adapter._route_env,
                )

                # 4. Upload to COS
                upload_result = await upload_to_cos(
                    file_bytes=file_bytes,
                    filename=filename,
                    content_type=content_type,
                    credentials=credentials,
                    bucket=credentials["bucketName"],
                    region=credentials["region"],
                )

                # 5. Build MsgBody
                # Remove keys already passed explicitly to avoid "multiple values" TypeError
                fwd_kwargs = {
                    k: v for k, v in kwargs.items()
                    if k not in {"file_uuid", "filename", "content_type"}
                }
                msg_body = self.build_msg_body(
                    upload_result,
                    file_uuid=file_uuid,
                    filename=filename,
                    content_type=content_type,
                    **fwd_kwargs,
                )
            else:
                # Non-COS media (e.g. sticker): build MsgBody directly
                msg_body = self.build_msg_body({}, **kwargs)

            # 6. Append caption if provided
            if caption:
                msg_body.append(
                    {"msg_type": "TIMTextElem", "msg_content": {"text": caption}},
                )

            # 7. Lock + dispatch
            gc = kwargs.get("group_code", "")
            return await sender.dispatch_msg_body(chat_id, msg_body, reply_to, group_code=gc)

        except ValueError as ve:
            return SendResult(success=False, error=str(ve))
        except Exception as exc:
            handler_name = type(self).__name__
            logger.error(
                "[%s] %s.handle() failed: %s",
                adapter.name, handler_name, exc, exc_info=True,
            )
            return SendResult(success=False, error=str(exc))


class ImageUrlHandler(MediaSendHandler):
    """Strategy: send image from a URL (download → COS → TIMImageElem)."""

    async def acquire_file(self, adapter, **kwargs):
        image_url: str = kwargs["image_url"]
        logger.info("[%s] ImageUrlHandler: downloading %s", adapter.name, image_url)
        file_bytes, content_type = await media_download_url(
            image_url, max_size_mb=adapter.MEDIA_MAX_SIZE_MB,
        )
        if not content_type or content_type == "application/octet-stream":
            path_part = image_url.split("?")[0]
            content_type = guess_mime_type(path_part) or "image/jpeg"
        filename = os.path.basename(image_url.split("?")[0]) or "image.jpg"
        return file_bytes, filename, content_type

    def build_msg_body(self, upload_result, **kwargs):
        return build_image_msg_body(
            url=upload_result["url"],
            uuid=kwargs["file_uuid"],
            filename=kwargs["filename"],
            size=upload_result["size"],
            width=upload_result.get("width", 0),
            height=upload_result.get("height", 0),
            mime_type=kwargs["content_type"],
        )


class ImageFileHandler(MediaSendHandler):
    """Strategy: send image from a local file path (read → COS → TIMImageElem)."""

    async def acquire_file(self, adapter, **kwargs):
        image_path: str = kwargs["image_path"]
        if not os.path.isfile(image_path):
            raise ValueError(f"File not found: {image_path}")
        logger.info("[%s] ImageFileHandler: reading %s", adapter.name, image_path)
        with open(image_path, "rb") as f:
            file_bytes = f.read()
        filename = os.path.basename(image_path) or "image.jpg"
        content_type = guess_mime_type(filename) or "image/jpeg"
        return file_bytes, filename, content_type

    def build_msg_body(self, upload_result, **kwargs):
        return build_image_msg_body(
            url=upload_result["url"],
            uuid=kwargs["file_uuid"],
            filename=kwargs["filename"],
            size=upload_result["size"],
            width=upload_result.get("width", 0),
            height=upload_result.get("height", 0),
            mime_type=kwargs["content_type"],
        )


class FileUrlHandler(MediaSendHandler):
    """Strategy: send file from a URL (download → COS → TIMFileElem)."""

    async def acquire_file(self, adapter, **kwargs):
        file_url: str = kwargs["file_url"]
        logger.info("[%s] FileUrlHandler: downloading %s", adapter.name, file_url)
        file_bytes, content_type = await media_download_url(
            file_url, max_size_mb=adapter.MEDIA_MAX_SIZE_MB,
        )
        filename = kwargs.get("filename")
        if not filename:
            path_part = file_url.split("?")[0]
            filename = os.path.basename(path_part) or "file"
        if not content_type or content_type == "application/octet-stream":
            content_type = guess_mime_type(filename) or "application/octet-stream"
        return file_bytes, filename, content_type

    def build_msg_body(self, upload_result, **kwargs):
        return build_file_msg_body(
            url=upload_result["url"],
            filename=kwargs["filename"],
            uuid=kwargs["file_uuid"],
            size=upload_result["size"],
        )


class DocumentHandler(MediaSendHandler):
    """Strategy: send local file/document (read → COS → TIMFileElem)."""

    async def acquire_file(self, adapter, **kwargs):
        file_path: str = kwargs["file_path"]
        if not os.path.isfile(file_path):
            raise ValueError(f"File not found: {file_path}")
        logger.info("[%s] DocumentHandler: reading %s", adapter.name, file_path)
        with open(file_path, "rb") as f:
            file_bytes = f.read()
        filename = kwargs.get("filename") or os.path.basename(file_path) or "document"
        content_type = guess_mime_type(filename) or "application/octet-stream"
        return file_bytes, filename, content_type

    def build_msg_body(self, upload_result, **kwargs):
        return build_file_msg_body(
            url=upload_result["url"],
            filename=kwargs["filename"],
            uuid=kwargs["file_uuid"],
            size=upload_result["size"],
        )


class StickerHandler(MediaSendHandler):
    """Strategy: send sticker/emoji (TIMFaceElem, no COS upload needed)."""

    def needs_cos_upload(self) -> bool:
        return False

    async def acquire_file(self, adapter, **kwargs):
        # Sticker does not need file bytes; return dummy values
        return b"", "sticker", "application/octet-stream"

    def build_msg_body(self, upload_result, **kwargs):
        from channels.platforms.yuanbao_sticker import (
            get_sticker_by_name,
            get_random_sticker,
            build_face_msg_body,
            build_sticker_msg_body,
        )
        sticker_name = kwargs.get("sticker_name")
        face_index = kwargs.get("face_index")

        if sticker_name is not None:
            sticker = get_sticker_by_name(sticker_name)
            if sticker is None:
                raise ValueError(f"Sticker not found: {sticker_name!r}")
            return build_sticker_msg_body(sticker)
        elif face_index is not None:
            return build_face_msg_body(face_index=face_index)
        else:
            sticker = get_random_sticker()
            return build_sticker_msg_body(sticker)

class GroupQueryService:
    """Encapsulates all group query operations (both low-level WS calls and
    higher-level AI-tool-facing wrappers).

    Responsibilities:
      - Low-level WS encode/decode for group info and member list queries
      - Chat-id parsing, error wrapping and result filtering for AI tools
      - Member cache population on the adapter
    """

    def __init__(self, adapter: "YuanbaoAdapter") -> None:
        self._adapter = adapter

    # ------------------------------------------------------------------
    # Low-level WS query methods
    # ------------------------------------------------------------------

    async def query_group_info_raw(self, group_code: str) -> Optional[dict]:
        """Query group info via WS (group name, owner, member count, etc.).

        Returns:
            Decoded dict or None on failure.
        """
        adapter = self._adapter
        if adapter._connection.ws is None:
            return None
        encoded = encode_query_group_info(group_code)
        from channels.platforms.yuanbao_proto import decode_conn_msg as _decode
        decoded = _decode(encoded)
        req_id = decoded["head"]["msg_id"]
        try:
            response = await adapter._connection.send_biz_request(encoded, req_id=req_id)
            head = response.get("head", {})
            status = head.get("status", 0)
            if status != 0:
                logger.warning("[%s] query_group_info failed: status=%d", adapter.name, status)
                return None
            biz_data = response.get("data", b"") or response.get("body", b"")
            if biz_data and isinstance(biz_data, bytes):
                return decode_query_group_info_rsp(biz_data)
            return {"group_code": group_code}
        except asyncio.TimeoutError:
            logger.warning("[%s] query_group_info timeout: group=%s", adapter.name, group_code)
            return None
        except Exception as exc:
            logger.warning("[%s] query_group_info failed: %s", adapter.name, exc)
            return None

    async def get_group_member_list_raw(
        self, group_code: str, offset: int = 0, limit: int = 200
    ) -> Optional[dict]:
        """Query group member list via WS.

        Returns:
            Decoded dict or None on failure.  Also populates adapter._member_cache.
        """
        adapter = self._adapter
        if adapter._connection.ws is None:
            return None
        encoded = encode_get_group_member_list(group_code, offset=offset, limit=limit)
        from channels.platforms.yuanbao_proto import decode_conn_msg as _decode
        decoded = _decode(encoded)
        req_id = decoded["head"]["msg_id"]
        try:
            response = await adapter._connection.send_biz_request(encoded, req_id=req_id)
            head = response.get("head", {})
            status = head.get("status", 0)
            if status != 0:
                logger.warning("[%s] get_group_member_list failed: status=%d", adapter.name, status)
                return None
            biz_data = response.get("data", b"") or response.get("body", b"")
            if biz_data and isinstance(biz_data, bytes):
                result = decode_get_group_member_list_rsp(biz_data)
            else:
                result = {"members": [], "next_offset": 0, "is_complete": True}
            if result and result.get("members"):
                adapter._member_cache[group_code] = (time.time(), result["members"])
            return result
        except asyncio.TimeoutError:
            logger.warning("[%s] get_group_member_list timeout: group=%s", adapter.name, group_code)
            return None
        except Exception as exc:
            logger.warning("[%s] get_group_member_list failed: %s", adapter.name, exc)
            return None

    # ------------------------------------------------------------------
    # AI-tool-facing wrappers (chat_id parsing + filtering)
    # ------------------------------------------------------------------

    async def query_group_info(self, chat_id: str) -> dict:
        """AI tool: Query current group info.

        No parameters needed (group_code extracted from session context).
        Returns group name, owner, member count, etc.
        """
        if not chat_id.startswith("group:"):
            return {"error": "This command is only available in group chats"}
        group_code = chat_id[len("group:"):]
        result = await self.query_group_info_raw(group_code)
        if result is None:
            return {"error": "Failed to query group info"}
        return result

    async def query_session_members(
        self,
        chat_id: str,
        action: str = "list_all",
        name: Optional[str] = None,
    ) -> dict:
        """AI tool: Query group member list.

        Args:
            chat_id: Chat ID (extracted from session context)
            action: 'find' (search by name) | 'list_bots' (list bots) | 'list_all' (list all)
            name: Search keyword when action='find'

        Returns:
            {"members": [...], "total": int, "mentionHint": str}
        """
        if not chat_id.startswith("group:"):
            return {"error": "This command is only available in group chats"}
        group_code = chat_id[len("group:"):]
        result = await self.get_group_member_list_raw(group_code)
        if result is None:
            return {"error": "Failed to query group members"}

        members = result.get("members", [])

        if action == "find" and name:
            query = name.lower()
            members = [
                m for m in members
                if query in (m.get("nickname", "") or "").lower()
                or query in (m.get("name_card", "") or "").lower()
                or query in (m.get("user_id", "") or "").lower()
            ]
        elif action == "list_bots":
            members = [m for m in members if "bot" in (m.get("nickname", "") or "").lower()]

        # Construct mentionHint
        mention_hint = ""
        if members and len(members) <= 10:
            names = [m.get("name_card") or m.get("nickname") or m.get("user_id", "") for m in members]
            mention_hint = "Mention with @name: " + ", ".join(names)

        return {
            "members": members[:50],  # Limit return count
            "total": len(members),
            "mentionHint": mention_hint,
        }


class HeartbeatManager:
    """Manages reply heartbeat (RUNNING / FINISH) lifecycle.

    Responsibilities:
      - Periodic RUNNING heartbeat sender (every 2s)
      - Auto-FINISH after 30s inactivity
      - Explicit stop with optional FINISH signal
    """

    def __init__(self, adapter: "YuanbaoAdapter") -> None:
        self._adapter = adapter
        self._reply_heartbeat_tasks: Dict[str, asyncio.Task] = {}
        self._reply_hb_last_active: Dict[str, float] = {}

    async def send_heartbeat_once(self, chat_id: str, heartbeat_val: int) -> None:
        """Send a single heartbeat (RUNNING or FINISH), best effort."""
        adapter = self._adapter
        conn = adapter._connection
        if conn.ws is None or not adapter._bot_id:
            return
        try:
            if chat_id.startswith("group:"):
                group_code = chat_id[len("group:"):]
                encoded = encode_send_group_heartbeat(
                    from_account=adapter._bot_id,
                    group_code=group_code,
                    heartbeat=heartbeat_val,
                )
            else:
                to_account = chat_id.removeprefix("direct:")
                encoded = encode_send_private_heartbeat(
                    from_account=adapter._bot_id,
                    to_account=to_account,
                    heartbeat=heartbeat_val,
                )
            await conn.ws.send(encoded)
            status_name = "RUNNING" if heartbeat_val == WS_HEARTBEAT_RUNNING else "FINISH"
            logger.debug(
                "[%s] Reply heartbeat %s sent: chat=%s",
                adapter.name, status_name, chat_id,
            )
        except Exception as exc:
            logger.debug("[%s] send_heartbeat_once failed: %s", adapter.name, exc)

    async def start(self, chat_id: str) -> None:
        """Start or renew the Reply Heartbeat periodic sender (RUNNING, every 2s)."""
        adapter = self._adapter
        conn = adapter._connection
        if conn.ws is None or not adapter._bot_id:
            return

        existing = self._reply_heartbeat_tasks.get(chat_id)
        if existing and not existing.done():
            self._reply_hb_last_active[chat_id] = time.time()
            return

        self._reply_hb_last_active[chat_id] = time.time()

        task = asyncio.create_task(
            self._worker(chat_id),
            name=f"yuanbao-reply-hb-{chat_id}",
        )
        self._reply_heartbeat_tasks[chat_id] = task

    async def _worker(self, chat_id: str) -> None:
        """Background coroutine: send RUNNING heartbeat every 2s.
        30s without renewal -> send FINISH and exit.
        """
        try:
            await self.send_heartbeat_once(chat_id, WS_HEARTBEAT_RUNNING)

            while True:
                await asyncio.sleep(REPLY_HEARTBEAT_INTERVAL_S)

                last_active = self._reply_hb_last_active.get(chat_id, 0)
                if time.time() - last_active > REPLY_HEARTBEAT_TIMEOUT_S:
                    break

                conn = self._adapter._connection
                if conn.ws is None:
                    break

                await self.send_heartbeat_once(chat_id, WS_HEARTBEAT_RUNNING)

        except asyncio.CancelledError:
            cancelled = True
        except Exception:
            cancelled = False
        else:
            cancelled = False
        finally:
            if not cancelled:
                try:
                    await self.send_heartbeat_once(chat_id, WS_HEARTBEAT_FINISH)
                except Exception:
                    pass
            self._reply_heartbeat_tasks.pop(chat_id, None)
            self._reply_hb_last_active.pop(chat_id, None)

    async def stop(self, chat_id: str, send_finish: bool = True) -> None:
        """Stop Reply Heartbeat and optionally send FINISH."""
        task = self._reply_heartbeat_tasks.pop(chat_id, None)
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        if send_finish:
            try:
                await self.send_heartbeat_once(chat_id, WS_HEARTBEAT_FINISH)
            except Exception:
                pass

    async def close(self) -> None:
        """Cancel all reply heartbeat tasks."""
        for task in list(self._reply_heartbeat_tasks.values()):
            if not task.done():
                task.cancel()
        self._reply_heartbeat_tasks.clear()
        self._reply_hb_last_active.clear()


class SlowResponseNotifier:
    """Manages delayed 'please wait' notifications for slow agent responses.

    Starts a timer per chat_id; if the agent hasn't replied within
    SLOW_RESPONSE_TIMEOUT_S seconds, sends a courtesy message.
    """

    def __init__(self, adapter: "YuanbaoAdapter", sender: "MessageSender") -> None:
        self._adapter = adapter
        self._sender = sender
        self._tasks: Dict[str, asyncio.Task] = {}

    async def start(self, chat_id: str) -> None:
        """Start a delayed task that notifies the user when the agent is slow."""
        self.cancel(chat_id)
        task = asyncio.create_task(
            self._notifier(chat_id),
            name=f"yuanbao-slow-resp-{chat_id}",
        )
        self._tasks[chat_id] = task

    async def _notifier(self, chat_id: str) -> None:
        """Wait SLOW_RESPONSE_TIMEOUT_S, then push a 'please wait' message."""
        try:
            await asyncio.sleep(SLOW_RESPONSE_TIMEOUT_S)
            logger.info(
                "[%s] Agent response exceeded %ds for %s, sending wait notice",
                self._adapter.name, int(SLOW_RESPONSE_TIMEOUT_S), chat_id,
            )
            await self._sender.send_text_chunk(chat_id, SLOW_RESPONSE_MESSAGE)
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.debug("[%s] Slow-response notifier failed: %s", self._adapter.name, exc)

    def cancel(self, chat_id: str) -> None:
        """Cancel the pending slow-response notifier for *chat_id*, if any."""
        task = self._tasks.pop(chat_id, None)
        if task and not task.done():
            task.cancel()

    async def close(self) -> None:
        """Cancel all slow-response tasks."""
        for task in list(self._tasks.values()):
            if not task.done():
                task.cancel()
        self._tasks.clear()


class MessageSender:
    """Core message sending dispatcher for YuanbaoAdapter.

    Responsibilities:
      - Per-chat-id lock management (serial send ordering)
      - Text chunk sending with retry
      - C2C / Group message encoding and dispatch
      - Media send helpers (image, file, sticker, document)
      - Direct send helper (text + media, used by send_message tool)
    """

    IMAGE_EXTS: ClassVar[frozenset] = frozenset({".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"})
    CHAT_DICT_MAX_SIZE: ClassVar[int] = 1000  # Max distinct chat IDs in _chat_locks

    def __init__(self, adapter: "YuanbaoAdapter") -> None:
        self._adapter = adapter
        self._chat_locks: collections.OrderedDict[str, asyncio.Lock] = collections.OrderedDict()

        # Optional hooks injected by OutboundManager for coordination
        self._on_send_start: Optional[Callable[[str], Any]] = None   # cancel slow-notifier
        self._on_send_finish: Optional[Callable[[str], Any]] = None  # send FINISH heartbeat

        # Media send handlers (strategy pattern)
        self._media_handlers: Dict[str, MediaSendHandler] = {
            "image_url": ImageUrlHandler(),
            "image_file": ImageFileHandler(),
            "file_url": FileUrlHandler(),
            "document": DocumentHandler(),
            "sticker": StickerHandler(),
        }

    # -- Media handler registry ---------------------------------------------

    def register_handler(self, name: str, handler: MediaSendHandler) -> None:
        """Register (or replace) a named media send handler."""
        self._media_handlers[name] = handler

    # -- Chat lock ---------------------------------------------------------

    def get_chat_lock(self, chat_id: str) -> asyncio.Lock:
        """Return (or create) a per-chat-id lock with safe LRU eviction."""
        if chat_id in self._chat_locks:
            self._chat_locks.move_to_end(chat_id)
            return self._chat_locks[chat_id]
        if len(self._chat_locks) >= self.CHAT_DICT_MAX_SIZE:
            evicted = False
            for key in list(self._chat_locks):
                if not self._chat_locks[key].locked():
                    self._chat_locks.pop(key)
                    evicted = True
                    break
            if not evicted:
                self._chat_locks.pop(next(iter(self._chat_locks)))
        self._chat_locks[chat_id] = asyncio.Lock()
        return self._chat_locks[chat_id]

    # -- Text send ---------------------------------------------------------

    async def send_text(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        group_code: str = "",
    ) -> "SendResult":
        """Send text message with auto-chunking and per-chat-id ordering guarantee."""
        adapter = self._adapter
        conn = adapter._connection
        if conn.ws is None:
            return SendResult(success=False, error="Not connected", retryable=True)

        if self._on_send_start:
            self._on_send_start(chat_id)

        lock = self.get_chat_lock(chat_id)
        async with lock:
            content_to_send = self.strip_cron_wrapper(content)
            chunks = self.truncate_message(content_to_send, adapter.MAX_TEXT_CHUNK)
            logger.info(
                "[%s] truncate_message: input=%d chars, max=%d, output=%d chunk(s) sizes=%s",
                adapter.name, len(content_to_send), adapter.MAX_TEXT_CHUNK,
                len(chunks), [len(c) for c in chunks],
            )
            for i, chunk in enumerate(chunks):
                r_to = reply_to if i == 0 else None
                result = await self.send_text_chunk(chat_id, chunk, r_to, group_code=group_code)
                if not result.success:
                    return result

        # Notify outbound coordinator that send is complete (e.g. FINISH heartbeat)
        if self._on_send_finish:
            try:
                await self._on_send_finish(chat_id)
            except Exception:
                pass
        return SendResult(success=True)

    async def send_media(
        self,
        chat_id: str,
        handler_name: str,
        reply_to: Optional[str] = None,
        caption: Optional[str] = None,
        **kwargs: Any,
    ) -> "SendResult":
        """Dispatch media send to the named handler strategy."""
        handler = self._media_handlers.get(handler_name)
        if handler is None:
            return SendResult(
                success=False,
                error=f"Unknown media handler: {handler_name!r}",
            )
        return await handler.handle(
            self._adapter, chat_id,
            reply_to=reply_to, caption=caption, **kwargs,
        )

    # -- Direct send (text + media, used by send_message tool) -------------

    async def send_direct(
        self,
        chat_id: str,
        message: str,
        media_files: Optional[List[Tuple[str, bool]]] = None,
    ) -> Dict[str, Any]:
        """Send text + media via Yuanbao (used by the ``send_message`` tool).

        Unlike Weixin which creates a fresh adapter per call, Yuanbao reuses
        the running gateway adapter (persistent WebSocket).  Logic mirrors
        send_weixin_direct: send text first, then iterate media_files by
        extension.
        """
        adapter = self._adapter
        last_result: Optional["SendResult"] = None

        # 1. Send text
        if message.strip():
            last_result = await adapter.send(chat_id, message)
            if not last_result.success:
                return {"error": f"Yuanbao send failed: {last_result.error}"}

        # 2. Iterate media_files, dispatch by file extension
        for media_path, _is_voice in media_files or []:
            ext = Path(media_path).suffix.lower()
            if ext in self.IMAGE_EXTS:
                last_result = await adapter.send_image_file(chat_id, media_path)
            else:
                last_result = await adapter.send_document(chat_id, media_path)

            if not last_result.success:
                return {"error": f"Yuanbao media send failed: {last_result.error}"}

        if last_result is None:
            return {"error": "No deliverable text or media remained after processing"}

        return {
            "success": True,
            "platform": "yuanbao",
            "chat_id": chat_id,
            "message_id": last_result.message_id if last_result else None,
        }

    async def dispatch_msg_body(
        self,
        chat_id: str,
        msg_body: list,
        reply_to: Optional[str] = None,
        group_code: str = "",
    ) -> "SendResult":
        """Lock + dispatch an arbitrary MsgBody to C2C or group."""
        lock = self.get_chat_lock(chat_id)
        async with lock:
            if chat_id.startswith("group:"):
                grp = chat_id[len("group:"):]
                result = await self.send_group_msg_body(grp, msg_body, reply_to)
            else:
                to_account = chat_id.removeprefix("direct:")
                result = await self.send_c2c_msg_body(to_account, msg_body, group_code=group_code)

        if result.get("success"):
            return SendResult(success=True, message_id=result.get("msg_key"))
        return SendResult(success=False, error=result.get("error", "Unknown error"))

    async def send_text_chunk(
        self,
        chat_id: str,
        text: str,
        reply_to: Optional[str] = None,
        retry: int = 3,
        group_code: str = "",
    ) -> "SendResult":
        """Send a single text chunk with retry (exponential backoff: 1s, 2s, 4s)."""
        adapter = self._adapter
        last_error: str = "Unknown error"
        for attempt in range(retry):
            try:
                if chat_id.startswith("group:"):
                    grp = chat_id[len("group:"):]
                    raw = await self.send_group_message(grp, text, reply_to)
                else:
                    to_account = chat_id.removeprefix("direct:")
                    raw = await self.send_c2c_message(to_account, text, group_code=group_code)

                if raw.get("success"):
                    return SendResult(success=True, message_id=raw.get("msg_key"))

                last_error = raw.get("error", "Unknown error")
                logger.warning(
                    "[%s] send_text_chunk attempt %d/%d failed: %s",
                    adapter.name, attempt + 1, retry, last_error,
                )
            except Exception as exc:
                last_error = str(exc)
                logger.warning(
                    "[%s] send_text_chunk attempt %d/%d exception: %s",
                    adapter.name, attempt + 1, retry, last_error,
                )

            if attempt < retry - 1:
                await asyncio.sleep(2 ** attempt)

        logger.error(
            "[%s] send_text_chunk max retries (%d) exceeded. Last error: %s",
            adapter.name, retry, last_error,
        )
        return SendResult(success=False, error=f"Max retries exceeded: {last_error}")

    # -- C2C / Group message -----------------------------------------------

    async def send_c2c_message(self, to_account: str, text: str, group_code: str = "") -> dict:
        """Send C2C text message, return {success: bool, msg_key: str}."""
        msg_body = [{"msg_type": "TIMTextElem", "msg_content": {"text": text}}]
        return await self.send_c2c_msg_body(to_account, msg_body, group_code=group_code)

    async def send_group_message(
        self,
        group_code: str,
        text: str,
        reply_to: Optional[str] = None,
    ) -> dict:
        """Send group text message, auto-converting @nickname to TIMCustomElem."""
        msg_body = self._build_msg_body_with_mentions(text, group_code)
        return await self.send_group_msg_body(group_code, msg_body, reply_to)

    # @mention pattern: (whitespace or start) + @ + nickname + (whitespace or end)
    _AT_USER_RE = re.compile(r'(?:(?<=\s)|(?<=^))@(\S+?)(?=\s|$)', re.MULTILINE)

    def _build_msg_body_with_mentions(self, text: str, group_code: str) -> list:
        """Parse @nickname patterns and build mixed TIMTextElem + TIMCustomElem msg_body."""
        cached = self._adapter._member_cache.get(group_code)
        if cached:
            ts, member_list = cached
            members = member_list if (time.time() - ts < self._adapter.MEMBER_CACHE_TTL_S) else []
        else:
            members = []
        if not members:
            return [{"msg_type": "TIMTextElem", "msg_content": {"text": text}}]

        nickname_to_uid = {}
        for m in members:
            nick = m.get("nickname") or m.get("nick_name") or ""
            uid = m.get("user_id") or ""
            if nick and uid:
                nickname_to_uid[nick.lower()] = (nick, uid)

        msg_body: list = []
        last_idx = 0
        for match in self._AT_USER_RE.finditer(text):
            start = match.start()
            if start > last_idx:
                seg = text[last_idx:start].strip()
                if seg:
                    msg_body.append({"msg_type": "TIMTextElem", "msg_content": {"text": seg}})

            nickname = match.group(1)
            entry = nickname_to_uid.get(nickname.lower())
            if entry:
                real_nick, uid = entry
                msg_body.append({
                    "msg_type": "TIMCustomElem",
                    "msg_content": {
                        "data": json.dumps({"elem_type": 1002, "text": f"@{real_nick}", "user_id": uid}),
                    },
                })
            else:
                msg_body.append({"msg_type": "TIMTextElem", "msg_content": {"text": f"@{nickname}"}})

            last_idx = match.end()

        if last_idx < len(text):
            tail = text[last_idx:].strip()
            if tail:
                msg_body.append({"msg_type": "TIMTextElem", "msg_content": {"text": tail}})

        if not msg_body:
            msg_body.append({"msg_type": "TIMTextElem", "msg_content": {"text": text}})

        return msg_body

    async def send_c2c_msg_body(self, to_account: str, msg_body: list, group_code: str = "") -> dict:
        """Send C2C message with arbitrary MsgBody."""
        adapter = self._adapter
        req_id = f"c2c_{next_seq_no()}"
        encoded = encode_send_c2c_message(
            to_account=to_account,
            msg_body=msg_body,
            from_account=adapter._bot_id or "",
            msg_id=req_id,
            group_code=group_code,
        )
        return await self._dispatch_encoded(adapter, encoded, req_id)

    async def send_group_msg_body(
        self,
        group_code: str,
        msg_body: list,
        reply_to: Optional[str] = None,
    ) -> dict:
        """Send group message with arbitrary MsgBody."""
        adapter = self._adapter
        req_id = f"grp_{next_seq_no()}"
        encoded = encode_send_group_message(
            group_code=group_code,
            msg_body=msg_body,
            from_account=adapter._bot_id or "",
            msg_id=req_id,
            ref_msg_id=reply_to or "",
        )
        return await self._dispatch_encoded(adapter, encoded, req_id)

    # -- Common dispatch helper --------------------------------------------

    @staticmethod
    async def _dispatch_encoded(
        adapter: "YuanbaoAdapter", encoded: bytes, req_id: str,
    ) -> dict:
        """Send pre-encoded bytes via WS and return a normalised result dict."""
        try:
            response = await adapter._connection.send_biz_request(encoded, req_id=req_id)
            return {"success": True, "msg_key": response.get("msg_id", "")}
        except asyncio.TimeoutError:
            return {"success": False, "error": f"Request timeout after {DEFAULT_SEND_TIMEOUT}s"}
        except Exception as exc:
            return {"success": False, "error": str(exc)}

    # -- Media validation ---------------------------------------------------

    @staticmethod
    def validate_media(
        file_bytes: Optional[bytes], filename: str, max_size_mb: int = 20
    ) -> Optional[str]:
        """Media pre-validation: check file validity before sending/uploading.

        Returns:
            Error description (str) if validation fails, otherwise None.
        """
        if file_bytes is None or len(file_bytes) == 0:
            return f"Empty file: {filename}"
        max_bytes = max_size_mb * 1024 * 1024
        if len(file_bytes) > max_bytes:
            size_mb = len(file_bytes) / 1024 / 1024
            return f"File too large: {filename} ({size_mb:.1f}MB > {max_size_mb}MB)"
        return None

    # -- Text truncation (table-aware) --------------------------------------

    @staticmethod
    def truncate_message(
        content: str,
        max_length: int = 4000,
        len_fn: Optional[Callable[[str], int]] = None,
    ) -> List[str]:
        """
        Split a long message into chunks with table-awareness.

        Delegates core splitting to ``MarkdownProcessor.chunk_markdown_text``
        and strips page indicators like ``(1/3)`` from the output.

        Falls back to ``BasePlatformAdapter.truncate_message`` for non-table
        content and for overall text that fits in a single chunk.
        """
        _len = len_fn or len
        if _len(content) <= max_length:
            return [content]

        # Delegate to MarkdownProcessor for table/fence-aware chunking
        chunks = MarkdownProcessor.chunk_markdown_text(
            content, max_length, len_fn=len_fn,
        )

        # Strip page indicators like (1/3) that BasePlatformAdapter may add
        chunks = [_INDICATOR_RE.sub('', c) for c in chunks]

        return chunks if chunks else [content]

    # -- Cron wrapper stripping ---------------------------------------------

    @staticmethod
    def strip_cron_wrapper(content: str) -> str:
        """Strip scheduler cron header/footer wrapper for cleaner Yuanbao output."""
        if not content.startswith("Cronjob Response: "):
            return content

        divider = "\n-------------\n\n"
        footer_prefix = '\n\nTo stop or manage this job, send me a new message (e.g. "stop reminder '
        divider_pos = content.find(divider)
        footer_pos = content.rfind(footer_prefix)
        if divider_pos < 0 or footer_pos < 0 or footer_pos <= divider_pos:
            return content

        header = content[:divider_pos]
        if "\n(job_id: " not in header:
            return content

        body_start = divider_pos + len(divider)
        body = content[body_start:footer_pos].strip()
        return body or content

    # -- Cleanup on disconnect ---------------------------------------------

    async def close(self) -> None:
        """Release chat locks (no-op for now; placeholder for future cleanup)."""
        self._chat_locks.clear()


class OutboundManager:
    """Outbound coordinator that orchestrates sending, heartbeat and slow-response.

    Composes:
      - MessageSender   — core text/media sending
      - HeartbeatManager — reply heartbeat (RUNNING / FINISH) lifecycle
      - SlowResponseNotifier — delayed 'please wait' notifications

    YuanbaoAdapter holds a single ``_outbound: OutboundManager`` and delegates
    all outbound operations through it.
    """

    # Expose class-level constants from MessageSender for backward compatibility
    CHAT_DICT_MAX_SIZE: ClassVar[int] = MessageSender.CHAT_DICT_MAX_SIZE

    def __init__(self, adapter: "YuanbaoAdapter") -> None:
        self._adapter = adapter
        self.sender: MessageSender = MessageSender(adapter)
        self.heartbeat: HeartbeatManager = HeartbeatManager(adapter)
        self.slow_notifier: SlowResponseNotifier = SlowResponseNotifier(adapter, self.sender)

        # Wire coordination hooks into MessageSender
        self.sender._on_send_start = self._handle_send_start
        self.sender._on_send_finish = self._handle_send_finish

    # -- Coordination hooks ------------------------------------------------

    def _handle_send_start(self, chat_id: str) -> None:
        """Called by MessageSender before sending: cancel slow-response notifier."""
        self.slow_notifier.cancel(chat_id)

    async def _handle_send_finish(self, chat_id: str) -> None:
        """Called by MessageSender after sending: send FINISH heartbeat."""
        await self.heartbeat.send_heartbeat_once(chat_id, WS_HEARTBEAT_FINISH)

    # -- Delegated public API (used by YuanbaoAdapter) ---------------------

    async def send_text(
        self, chat_id: str, content: str, reply_to: Optional[str] = None,
        group_code: str = "",
    ) -> "SendResult":
        """Send text message with auto-chunking."""
        return await self.sender.send_text(chat_id, content, reply_to, group_code=group_code)

    async def send_media(
        self, chat_id: str, handler_name: str, **kwargs: Any,
    ) -> "SendResult":
        """Dispatch media send to the named handler strategy."""
        return await self.sender.send_media(chat_id, handler_name, **kwargs)

    async def send_direct(
        self, chat_id: str, message: str,
        media_files: Optional[List[Tuple[str, bool]]] = None,
    ) -> Dict[str, Any]:
        """Send text + media (used by send_message tool)."""
        return await self.sender.send_direct(chat_id, message, media_files)

    async def start_typing(self, chat_id: str) -> None:
        """Start reply heartbeat (RUNNING)."""
        await self.heartbeat.start(chat_id)

    async def stop_typing(self, chat_id: str, send_finish: bool = False) -> None:
        """Stop reply heartbeat."""
        await self.heartbeat.stop(chat_id, send_finish=send_finish)

    async def start_slow_notifier(self, chat_id: str) -> None:
        """Start slow-response notifier."""
        await self.slow_notifier.start(chat_id)

    def cancel_slow_notifier(self, chat_id: str) -> None:
        """Cancel slow-response notifier."""
        self.slow_notifier.cancel(chat_id)

    def get_chat_lock(self, chat_id: str) -> asyncio.Lock:
        """Proxy to MessageSender.get_chat_lock for backward compatibility."""
        return self.sender.get_chat_lock(chat_id)

    @property
    def _chat_locks(self) -> collections.OrderedDict:
        """Proxy to MessageSender._chat_locks for backward compatibility."""
        return self.sender._chat_locks

    @staticmethod
    def validate_media(
        file_bytes: Optional[bytes], filename: str, max_size_mb: int = 20,
    ) -> Optional[str]:
        """Proxy to MessageSender.validate_media."""
        return MessageSender.validate_media(file_bytes, filename, max_size_mb)

    async def close(self) -> None:
        """Shut down all sub-managers."""
        await self.sender.close()
        await self.heartbeat.close()
        await self.slow_notifier.close()


