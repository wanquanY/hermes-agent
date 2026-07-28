"""
Yuanbao platform adapter.

Connects to the Yuanbao WebSocket gateway, handles authentication (AUTH_BIND),
heartbeat, reconnection, message receive (T05) and send (T06).

Configuration in config.yaml (or via env vars):
    platforms:
      yuanbao:
        extra:
          app_id: "..."              # or YUANBAO_APP_ID
          app_secret: "..."          # or YUANBAO_APP_SECRET
          bot_id: "..."              # or YUANBAO_BOT_ID  (optional, returned by sign-token)
          ws_url: "wss://..."        # or YUANBAO_WS_URL
          api_domain: "https://..."  # or YUANBAO_API_DOMAIN
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import Any, ClassVar, Dict, Optional, Tuple

from agent.secret_scope import get_profile_env
import sys

import httpx

try:
    import websockets
    import websockets.exceptions
    WEBSOCKETS_AVAILABLE = True
except ImportError:
    WEBSOCKETS_AVAILABLE = False
    websockets = None  # type: ignore[assignment]

from channels.config import Platform, PlatformConfig
from channels.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    SendResult,
)
from channels.platforms.helpers import MessageDeduplicator
from channels.platforms.yuanbao_media import (
    download_url as media_download_url,
    get_cos_credentials,
    upload_to_cos,
    build_image_msg_body,
    build_file_msg_body,
    guess_mime_type,
    md5_hex,
)
from channels.platforms.yuanbao_proto import (
    CMD_TYPE,
    HERMES_INSTANCE_ID,
    decode_conn_msg,
    decode_inbound_push,
    decode_query_group_info_rsp,
    decode_get_group_member_list_rsp,
    encode_auth_bind,
    encode_ping,
    encode_push_ack,
    next_seq_no,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Version / platform constants (used in AUTH_BIND and sign-token headers)
# ---------------------------------------------------------------------------
try:
    from hermes_cli import __version__ as _HERMES_VERSION
except ImportError:
    _HERMES_VERSION = "0.0.0"

_APP_VERSION = _HERMES_VERSION
_BOT_VERSION = _HERMES_VERSION
_YUANBAO_INSTANCE_ID = str(HERMES_INSTANCE_ID)  # single source: yuanbao_proto.HERMES_INSTANCE_ID
_OPERATION_SYSTEM = sys.platform

from channels.platforms.yuanbao_auth import SignManager
from channels.platforms.yuanbao_constants import (
    AUTH_FAILED_CODES,
    AUTH_RETRYABLE_CODES,
    AUTH_TIMEOUT_SECONDS,
    CONNECT_TIMEOUT_SECONDS,
    DEFAULT_API_DOMAIN,
    DEFAULT_SEND_TIMEOUT,
    DEFAULT_WS_GATEWAY_URL,
    HEARTBEAT_INTERVAL_SECONDS,
    HEARTBEAT_TIMEOUT_THRESHOLD,
    MAX_RECONNECT_ATTEMPTS,
    NO_RECONNECT_CLOSE_CODES,
    REPLY_REF_TTL_S,
)
from channels.platforms.yuanbao_inbound import (
    AccessGuardMiddleware,
    AccessPolicy,
    AutoSetHomeMiddleware,
    BuildSourceMiddleware,
    ChatRoutingMiddleware,
    ClassifyMessageTypeMiddleware,
    DecodeMiddleware,
    DedupMiddleware,
    DispatchMiddleware,
    ExtractContentMiddleware,
    ExtractFieldsMiddleware,
    GroupAtGuardMiddleware,
    GroupAttributionMiddleware,
    InboundContext,
    InboundMiddleware,
    InboundPipeline,
    InboundPipelineBuilder,
    MediaResolveMiddleware,
    OwnerCommandMiddleware,
    PlaceholderFilterMiddleware,
    QuoteContextMiddleware,
    RecallGuardMiddleware,
    SkipSelfMiddleware,
)
from channels.platforms.yuanbao_markdown import MarkdownProcessor
from channels.platforms.yuanbao_outbound import (
    DocumentHandler,
    FileUrlHandler,
    GroupQueryService,
    HeartbeatManager,
    ImageFileHandler,
    ImageUrlHandler,
    MediaSendHandler,
    MessageSender,
    OutboundManager,
    SlowResponseNotifier,
    StickerHandler,
)

class ConnectionManager:
    """Manages the WebSocket connection lifecycle for YuanbaoAdapter.

    Responsibilities:
      - Opening and closing the WebSocket
      - AUTH_BIND handshake
      - Heartbeat (ping/pong) loop
      - Receive loop (frame dispatch)
      - Reconnect with exponential backoff
    """

    def __init__(self, adapter: "YuanbaoAdapter") -> None:
        self._adapter = adapter
        self._ws = None  # websockets connection
        self._connect_id: Optional[str] = None
        self._heartbeat_task: Optional[asyncio.Task] = None
        self._recv_task: Optional[asyncio.Task] = None
        self._pending_acks: Dict[str, asyncio.Future] = {}
        self._pending_pong: Optional[asyncio.Future] = None
        self._consecutive_hb_timeouts: int = 0
        self._reconnect_attempts: int = 0
        self._reconnecting: bool = False
        # Debounce buffer for aggregating multi-part inbound messages
        self._inbound_buffer: Dict[str, list] = {}  # key -> [raw_data_frames, ...]
        self._inbound_timers: Dict[str, asyncio.TimerHandle] = {}  # key -> timer

    # -- Properties --------------------------------------------------------

    @property
    def ws(self):
        return self._ws

    @property
    def connect_id(self) -> Optional[str]:
        return self._connect_id

    @property
    def reconnect_attempts(self) -> int:
        return self._reconnect_attempts

    @property
    def is_connected(self) -> bool:
        if self._ws is None:
            return False
        open_attr = getattr(self._ws, "open", None)
        if open_attr is True:
            return True
        if callable(open_attr):
            try:
                return bool(open_attr())
            except Exception:
                return False
        return False

    # -- Open / Close ------------------------------------------------------

    async def open(self) -> bool:
        """Open WebSocket connection: sign-token → WS connect → AUTH_BIND → start loops.

        Returns True on success, False on failure.
        """
        adapter = self._adapter

        if not WEBSOCKETS_AVAILABLE:
            msg = "Yuanbao startup failed: 'websockets' package not installed"
            adapter._set_fatal_error("yuanbao_missing_dependency", msg, retryable=True)
            logger.warning("[%s] %s. Run: pip install websockets", adapter.name, msg)
            return False

        if not adapter._app_key or not adapter._app_secret:
            msg = (
                "Yuanbao startup failed: "
                "YUANBAO_APP_ID and YUANBAO_APP_SECRET are required"
            )
            adapter._set_fatal_error("yuanbao_missing_credentials", msg, retryable=False)
            logger.error("[%s] %s", adapter.name, msg)
            return False

        # Idempotency guard
        if self._ws is not None:
            try:
                open_attr = getattr(self._ws, "open", None)
                if open_attr is True or (callable(open_attr) and open_attr()):
                    logger.debug("[%s] Already connected, skipping connect()", adapter.name)
                    return True
            except Exception:
                pass

        # Acquire platform-scoped lock to prevent duplicate connections
        if not adapter._acquire_platform_lock(
            'yuanbao-app-key', adapter._app_key, 'Yuanbao app key'
        ):
            return False

        try:
            # Step 1: Get sign token
            logger.info("[%s] Fetching sign token from %s", adapter.name, adapter._api_domain)
            token_data = await SignManager.get_token(
                adapter._app_key, adapter._app_secret, adapter._api_domain,
                route_env=adapter._route_env,
            )

            # Update bot_id if returned by sign-token API
            if token_data.get("bot_id"):
                adapter._bot_id = str(token_data["bot_id"])

            # Step 2: Open WebSocket connection (disable built-in ping/pong)
            logger.info("[%s] Connecting to %s", adapter.name, adapter._ws_url)
            self._ws = await asyncio.wait_for(
                websockets.connect(  # type: ignore[attr-defined]
                    adapter._ws_url,
                    ping_interval=None,
                    ping_timeout=None,
                    close_timeout=5,
                ),
                timeout=CONNECT_TIMEOUT_SECONDS,
            )

            # Step 3: Authenticate (AUTH_BIND + wait for BIND_ACK)
            authed = await self._authenticate(token_data)
            if not authed:
                await self._cleanup_ws()
                return False

            # Step 4: Start background tasks
            self._reconnect_attempts = 0
            adapter._mark_connected()
            adapter._loop = asyncio.get_running_loop()
            self._heartbeat_task = asyncio.create_task(
                self._heartbeat_loop(), name=f"yuanbao-heartbeat-{self._connect_id}"
            )
            self._recv_task = asyncio.create_task(
                self._receive_loop(), name=f"yuanbao-recv-{self._connect_id}"
            )
            logger.info(
                "[%s] Connected. connectId=%s botId=%s",
                adapter.name, self._connect_id, adapter._bot_id,
            )

            YuanbaoAdapter.set_active(adapter)

            return True

        except asyncio.TimeoutError:
            logger.error("[%s] Connection timed out", adapter.name)
            await self._cleanup_ws()
            adapter._release_platform_lock()
            return False
        except Exception as exc:
            logger.error("[%s] connect() failed: %s", adapter.name, exc, exc_info=True)
            await self._cleanup_ws()
            adapter._release_platform_lock()
            return False

    async def close(self) -> None:
        """Cancel background tasks, fail pending futures, and close the WebSocket."""

        if self._heartbeat_task:
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except asyncio.CancelledError:
                pass
            self._heartbeat_task = None

        if self._recv_task:
            self._recv_task.cancel()
            try:
                await self._recv_task
            except asyncio.CancelledError:
                pass
            self._recv_task = None

        # Fail any pending ACK futures
        disc_exc = RuntimeError("YuanbaoAdapter disconnected")
        for fut in self._pending_acks.values():
            if not fut.done():
                fut.set_exception(disc_exc)
        self._pending_acks.clear()

        # Clear refresh locks to avoid stale locks from a previous event loop
        SignManager.clear_locks()

        await self._cleanup_ws()

    # -- Authentication ----------------------------------------------------

    async def _authenticate(self, token_data: dict) -> bool:
        """Send AUTH_BIND and read frames until BIND_ACK is received.

        Returns True on success, False on failure/timeout.
        """
        adapter = self._adapter
        if self._ws is None:
            return False

        token = token_data.get("token", "")
        uid = adapter._bot_id or token_data.get("bot_id", "")
        source = token_data.get("source") or "bot"
        route_env = adapter._route_env or token_data.get("route_env", "") or ""

        msg_id = str(uuid.uuid4())

        auth_bytes = encode_auth_bind(
            biz_id="ybBot",
            uid=uid,
            source=source,
            token=token,
            msg_id=msg_id,
            app_version=_APP_VERSION,
            operation_system=_OPERATION_SYSTEM,
            bot_version=_BOT_VERSION,
            route_env=route_env,
        )
        await self._ws.send(auth_bytes)
        logger.debug("[%s] AUTH_BIND sent (msg_id=%s uid=%s)", adapter.name, msg_id, uid)

        try:
            _loop = asyncio.get_running_loop()
            deadline = _loop.time() + AUTH_TIMEOUT_SECONDS
            while True:
                remaining = deadline - _loop.time()
                if remaining <= 0:
                    logger.error("[%s] AUTH_BIND timeout waiting for BIND_ACK", adapter.name)
                    return False

                raw = await asyncio.wait_for(self._ws.recv(), timeout=remaining)
                if not isinstance(raw, (bytes, bytearray)):
                    continue

                try:
                    msg = decode_conn_msg(bytes(raw))
                except Exception:
                    continue

                head = msg.get("head", {})
                cmd_type = head.get("cmd_type", -1)
                cmd = head.get("cmd", "")

                if cmd_type == CMD_TYPE["Response"] and cmd == "auth-bind":
                    connect_id = self._extract_connect_id(msg)
                    if connect_id:
                        self._connect_id = connect_id
                        logger.info("[%s] BIND_ACK received: connectId=%s", adapter.name, connect_id)
                        return True
                    else:
                        logger.error("[%s] BIND_ACK missing connectId", adapter.name)
                        return False

        except asyncio.TimeoutError:
            logger.error("[%s] AUTH_BIND timeout", adapter.name)
            return False
        except Exception as exc:
            logger.error("[%s] AUTH_BIND error: %s", adapter.name, exc, exc_info=True)
            return False

    def _extract_connect_id(self, decoded_msg: dict) -> Optional[str]:
        """Extract connectId from decoded BIND_ACK message."""
        data: bytes = decoded_msg.get("data", b"")
        if not data:
            return None
        try:
            fdict = _fields_to_dict(_parse_fields(data))
            code = _get_varint(fdict, 1)
            if code != 0:
                message = _get_string(fdict, 2)
                logger.error(
                    "[%s] AuthBindRsp error: code=%d message=%r",
                    self._adapter.name, code, message,
                )
                return None
            connect_id = _get_string(fdict, 3)
            return connect_id if connect_id else None
        except Exception as exc:
            logger.warning("[%s] Failed to extract connectId: %s", self._adapter.name, exc)
            return None

    # -- Heartbeat ---------------------------------------------------------

    async def _heartbeat_loop(self) -> None:
        """Send HEARTBEAT (ping) every 30s; trigger reconnect after threshold misses."""
        adapter = self._adapter
        try:
            while adapter._running:
                await asyncio.sleep(HEARTBEAT_INTERVAL_SECONDS)
                if self._ws is None:
                    continue
                try:
                    msg_id = str(uuid.uuid4())
                    ping_bytes = encode_ping(msg_id)
                    loop = asyncio.get_running_loop()
                    pong_future: asyncio.Future = loop.create_future()
                    self._pending_pong = pong_future
                    self._pending_acks[msg_id] = pong_future
                    await self._ws.send(ping_bytes)
                    logger.debug("[%s] PING sent (msg_id=%s)", adapter.name, msg_id)
                    try:
                        await asyncio.wait_for(pong_future, timeout=10.0)
                        self._consecutive_hb_timeouts = 0
                    except asyncio.TimeoutError:
                        self._pending_acks.pop(msg_id, None)
                        self._consecutive_hb_timeouts += 1
                        logger.warning(
                            "[%s] PONG timeout (%d/%d)",
                            adapter.name, self._consecutive_hb_timeouts, HEARTBEAT_TIMEOUT_THRESHOLD,
                        )
                        if self._consecutive_hb_timeouts >= HEARTBEAT_TIMEOUT_THRESHOLD:
                            logger.warning("[%s] Heartbeat threshold exceeded, triggering reconnect", adapter.name)
                            self.schedule_reconnect()
                            return
                    finally:
                        self._pending_acks.pop(msg_id, None)
                        self._pending_pong = None
                except Exception as exc:
                    logger.debug("[%s] Heartbeat send failed: %s", adapter.name, exc)
        except asyncio.CancelledError:
            pass

    # -- Receive loop ------------------------------------------------------

    async def _receive_loop(self) -> None:
        """Read WS frames and dispatch by cmd_type."""
        adapter = self._adapter
        try:
            async for raw in self._ws:  # type: ignore[union-attr]
                if not isinstance(raw, (bytes, bytearray)):
                    continue
                await self._handle_frame(bytes(raw))
        except asyncio.CancelledError:
            pass
        except websockets.exceptions.ConnectionClosed as close_exc:  # type: ignore[union-attr]
            close_code = getattr(close_exc, 'code', None)
            logger.warning(
                "[%s] WebSocket connection closed: code=%s reason=%s",
                adapter.name, close_code, getattr(close_exc, 'reason', ''),
            )
            if close_code and close_code in NO_RECONNECT_CLOSE_CODES:
                logger.error(
                    "[%s] Close code %d is non-recoverable, NOT reconnecting",
                    adapter.name, close_code,
                )
                adapter._mark_disconnected()
            else:
                self.schedule_reconnect()
        except Exception as exc:
            logger.warning("[%s] receive_loop exited: %s", adapter.name, exc)
            self.schedule_reconnect()

    async def _handle_frame(self, raw: bytes) -> None:
        """Handle a single WebSocket frame."""
        adapter = self._adapter
        try:
            msg = decode_conn_msg(raw)
        except Exception as exc:
            logger.debug("[%s] Failed to decode frame: %s", adapter.name, exc)
            return

        head = msg.get("head", {})
        cmd_type = head.get("cmd_type", -1)
        cmd = head.get("cmd", "")
        msg_id = head.get("msg_id", "")
        need_ack = head.get("need_ack", False)
        data: bytes = msg.get("data", b"")

        # HEARTBEAT_ACK
        if cmd_type == CMD_TYPE["Response"] and cmd == "ping":
            logger.debug("[%s] HEARTBEAT_ACK received (msg_id=%s)", adapter.name, msg_id)
            if self._pending_pong is not None and not self._pending_pong.done():
                self._pending_pong.set_result(True)
            elif msg_id and msg_id in self._pending_acks:
                fut = self._pending_acks.pop(msg_id)
                if not fut.done():
                    fut.set_result(True)
            return

        # Fire-and-forget heartbeat ACKs — server always responds but callers don't
        # wait on these; silently discard to avoid "Unmatched Response" noise.
        if cmd_type == CMD_TYPE["Response"] and cmd in {
            "send_group_heartbeat",
            "send_private_heartbeat",
        }:
            logger.debug("[%s] Heartbeat ACK received: cmd=%s msg_id=%s", adapter.name, cmd, msg_id)
            return

        # Response to an outbound RPC call
        if cmd_type == CMD_TYPE["Response"]:
            if msg_id and msg_id in self._pending_acks:
                fut = self._pending_acks.pop(msg_id)
                if not fut.done():
                    result = {"head": head}
                    if data:
                        result["data"] = data
                    fut.set_result(result)
            else:
                logger.debug(
                    "[%s] Unmatched Response: cmd=%s msg_id=%s",
                    adapter.name, cmd, msg_id,
                )
            return

        # Server-initiated Push
        if cmd_type == CMD_TYPE["Push"]:
            logger.info("[%s] Push received: cmd=%s msg_id=%s data_len=%d", adapter.name, cmd, msg_id, len(data))
            if need_ack and self._ws is not None:
                try:
                    ack_bytes = encode_push_ack(head)
                    await self._ws.send(ack_bytes)
                except Exception as ack_exc:
                    logger.debug("[%s] Failed to send PushAck: %s", adapter.name, ack_exc)

            if msg_id and msg_id in self._pending_acks:
                fut = self._pending_acks.pop(msg_id)
                if not fut.done():
                    try:
                        decoded = decode_inbound_push(data) if data else {"head": head}
                        fut.set_result(decoded)
                    except Exception as exc:
                        fut.set_exception(exc)
                return

            # Genuine inbound message — dispatch to AI
            if data:
                logger.info(
                    "[%s] WS received inbound push, decoding and dispatching: cmd=%s, data_len=%d",
                    adapter.name, cmd, len(data),
                )
                self._push_to_inbound(data)
            return

        logger.debug(
            "[%s] Ignoring frame: cmd_type=%d cmd=%s msg_id=%s",
            adapter.name, cmd_type, cmd, msg_id,
        )

    # -- Inbound dispatch ---------------------------------------------------

    _DEBOUNCE_WINDOW: float = 1.5  # seconds to wait for companion messages

    def _extract_sender_key(self, raw_data: bytes) -> str:
        """Lightweight decode to extract sender key for debounce grouping.

        Returns 'from_account:group_code' or a fallback unique key.
        """
        try:
            parsed = json.loads(raw_data.decode("utf-8"))
            if isinstance(parsed, dict):
                from_account = (
                    parsed.get("from_account", "")
                    or parsed.get("From_Account", "")
                )
                group_code = (
                    parsed.get("group_code", "")
                    or parsed.get("GroupId", "")
                    or parsed.get("group_id", "")
                )
                if from_account:
                    return f"{from_account}:{group_code}"
        except Exception:
            pass
        # Protobuf: try decode_inbound_push for sender info
        try:
            push = decode_inbound_push(raw_data)
            if push:
                return f"{push.get('from_account', '')}:{push.get('group_code', '')}"
        except Exception:
            pass
        # Fallback: unique key (no aggregation)
        return f"__unknown_{id(raw_data)}"

    def _push_to_inbound(self, raw_data: bytes) -> None:
        """Debounced inbound dispatch.

        Buffers raw frames from the same sender within a short time window,
        then dispatches all buffered data as a single aggregated pipeline
        execution.  This merges multi-part messages (e.g. image + text sent
        as separate WS pushes) into one pipeline run.
        """
        key = self._extract_sender_key(raw_data)

        # Cancel existing timer for this key (reset debounce window)
        existing_timer = self._inbound_timers.pop(key, None)
        if existing_timer:
            existing_timer.cancel()

        # Append to buffer
        if key not in self._inbound_buffer:
            self._inbound_buffer[key] = []
        self._inbound_buffer[key].append(raw_data)

        logger.debug(
            "[%s] Debounce: buffered frame for key=%s, count=%d",
            self._adapter.name, key, len(self._inbound_buffer[key]),
        )

        # Schedule flush after debounce window
        loop = asyncio.get_running_loop()
        timer = loop.call_later(
            self._DEBOUNCE_WINDOW,
            self._flush_inbound_buffer,
            key,
        )
        self._inbound_timers[key] = timer

    def _flush_inbound_buffer(self, key: str) -> None:
        """Flush the debounce buffer for a given key — execute the pipeline."""
        self._inbound_timers.pop(key, None)
        data_list = self._inbound_buffer.pop(key, [])
        if not data_list:
            return

        adapter = self._adapter
        logger.info(
            "[%s] Debounce flush: key=%s, aggregated %d frames",
            adapter.name, key, len(data_list),
        )

        ctx = InboundContext(adapter=adapter, raw_frames=data_list)

        adapter._track_task(asyncio.create_task(
            adapter._inbound_pipeline.execute(ctx),
            name=f"yuanbao-pipeline-{key}",
        ))

    # -- Send business request ---------------------------------------------

    async def send_biz_request(
        self,
        encoded_conn_msg: bytes,
        req_id: str,
        timeout: float = DEFAULT_SEND_TIMEOUT,
    ) -> dict:
        """Send a business-layer request and wait for the response.

        1. Register a Future in pending_acks[req_id]
        2. Send encoded_conn_msg (bytes) to WS
        3. asyncio.wait_for(future, timeout)
        4. Clean up pending_acks on timeout/exception
        """
        if self._ws is None:
            raise RuntimeError("Not connected")

        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        self._pending_acks[req_id] = future
        try:
            await self._ws.send(encoded_conn_msg)
            result = await asyncio.wait_for(asyncio.shield(future), timeout=timeout)
            return result
        except asyncio.TimeoutError:
            raise
        except Exception:
            raise
        finally:
            self._pending_acks.pop(req_id, None)

    # -- Reconnect ---------------------------------------------------------

    def schedule_reconnect(self) -> None:
        """Schedule a reconnect only if running and not already reconnecting."""
        if self._adapter._running and not self._reconnecting:
            asyncio.create_task(self._reconnect_with_backoff())

    async def _reconnect_with_backoff(self) -> bool:
        """Reconnect with exponential backoff (1s, 2s, 4s, … up to 60s)."""
        if self._reconnecting:
            logger.debug("[%s] Reconnect already in progress, skipping", self._adapter.name)
            return False
        self._reconnecting = True
        try:
            return await self._do_reconnect()
        finally:
            self._reconnecting = False

    async def _do_reconnect(self) -> bool:
        """Internal reconnect loop, called under the _reconnecting guard."""
        adapter = self._adapter
        for attempt in range(MAX_RECONNECT_ATTEMPTS):
            self._reconnect_attempts = attempt + 1
            wait = min(2 ** attempt, 60)
            logger.info(
                "[%s] Reconnect attempt %d/%d in %ds",
                adapter.name, attempt + 1, MAX_RECONNECT_ATTEMPTS, wait,
            )
            await asyncio.sleep(wait)

            await self._cleanup_ws()

            try:
                token_data = await SignManager.force_refresh(
                    adapter._app_key, adapter._app_secret, adapter._api_domain,
                    route_env=adapter._route_env,
                )
                if token_data.get("bot_id"):
                    adapter._bot_id = str(token_data["bot_id"])

                self._ws = await asyncio.wait_for(
                    websockets.connect(  # type: ignore[attr-defined]
                        adapter._ws_url,
                        ping_interval=None,
                        ping_timeout=None,
                        close_timeout=5,
                    ),
                    timeout=CONNECT_TIMEOUT_SECONDS,
                )

                authed = await self._authenticate(token_data)
                if not authed:
                    logger.warning("[%s] Re-auth failed on attempt %d", adapter.name, attempt + 1)
                    await self._cleanup_ws()
                    continue

                self._reconnect_attempts = 0
                self._consecutive_hb_timeouts = 0
                adapter._mark_connected()

                if self._heartbeat_task and not self._heartbeat_task.done():
                    self._heartbeat_task.cancel()
                self._heartbeat_task = asyncio.create_task(
                    self._heartbeat_loop(),
                    name=f"yuanbao-heartbeat-{self._connect_id}",
                )

                if self._recv_task and not self._recv_task.done():
                    self._recv_task.cancel()
                self._recv_task = asyncio.create_task(
                    self._receive_loop(),
                    name=f"yuanbao-recv-{self._connect_id}",
                )

                logger.info(
                    "[%s] Reconnected on attempt %d. connectId=%s",
                    adapter.name, attempt + 1, self._connect_id,
                )
                return True

            except asyncio.TimeoutError:
                logger.warning("[%s] Reconnect attempt %d timed out", adapter.name, attempt + 1)
            except Exception as exc:
                logger.warning(
                    "[%s] Reconnect attempt %d failed: %s", adapter.name, attempt + 1, exc
                )

        logger.error(
            "[%s] Giving up after %d reconnect attempts", adapter.name, MAX_RECONNECT_ATTEMPTS
        )
        adapter._mark_disconnected()
        return False

    async def _cleanup_ws(self) -> None:
        """Close and clear the WebSocket connection."""
        ws = self._ws
        self._ws = None
        if ws is not None:
            try:
                await ws.close()
            except Exception:
                pass

class YuanbaoAdapter(BasePlatformAdapter):
    """Yuanbao AI Bot adapter backed by a persistent WebSocket connection."""

    PLATFORM = Platform.YUANBAO
    MAX_TEXT_CHUNK: int = 4000  # Yuanbao single message character limit
    MEDIA_MAX_SIZE_MB: int = 50  # Max media file size in MB for upload validation
    REPLY_REF_MAX_ENTRIES: ClassVar[int] = 500  # Max capacity of reference dedup dict

    # -- Active instance registry (class-level singleton) -------------------

    _active_instance: ClassVar[Optional["YuanbaoAdapter"]] = None

    @classmethod
    def get_active(cls) -> Optional["YuanbaoAdapter"]:
        """Return the currently connected YuanbaoAdapter, or None."""
        return cls._active_instance

    @classmethod
    def set_active(cls, adapter: Optional["YuanbaoAdapter"]) -> None:
        """Register (or clear) the active adapter instance."""
        cls._active_instance = adapter

    def __init__(self, config: PlatformConfig, **kwargs: Any) -> None:
        super().__init__(config, Platform.YUANBAO)

        # Credentials / endpoints from config.extra (populated by config.py from env/yaml)
        _extra = config.extra or {}
        self._app_key: str = (_extra.get("app_id") or "").strip()
        self._app_secret: str = (_extra.get("app_secret") or "").strip()
        self._bot_id: Optional[str] = _extra.get("bot_id") or None
        self._ws_url: str = (_extra.get("ws_url") or DEFAULT_WS_GATEWAY_URL).strip()
        self._api_domain: str = (_extra.get("api_domain") or DEFAULT_API_DOMAIN).rstrip("/")
        self._route_env: str = (_extra.get("route_env") or "").strip()

        # Core managers (UML composition)
        self._connection: ConnectionManager = ConnectionManager(self)
        self._outbound: OutboundManager = OutboundManager(self)

        # Inbound dispatch tasks — tracked so disconnect() can cancel them
        self._inbound_tasks: set[asyncio.Task] = set()

        # Set of background tasks — prevent GC from collecting fire-and-forget tasks
        self._background_tasks: set[asyncio.Task] = set()

        # Member cache: group_code -> (updated_ts, [{"user_id":..., "nickname":..., ...}, ...])
        # Populated by get_group_member_list(), used by @mention resolution.
        # Entries older than MEMBER_CACHE_TTL_S are treated as stale.
        self._member_cache: Dict[str, Tuple[float, list]] = {}
        self.MEMBER_CACHE_TTL_S: float = 300.0  # 5 minutes

        # Inbound message deduplication (WS reconnect / network jitter)
        self._dedup = MessageDeduplicator(ttl_seconds=300)

        # Group chat sequential dispatch queue (session_key → asyncio.Queue).
        self._group_queues: Dict[str, asyncio.Queue] = {}

        # Recall support: track which msg_id is being processed per session_key
        # so RecallGuardMiddleware can detect "currently processing" messages.
        self._processing_msg_ids: Dict[str, str] = {}
        self._processing_msg_texts: Dict[str, str] = {}
        # Bounded cache of msg_id → attributed content for recent messages.
        # Used by _patch_transcript as content-match fallback when transcript
        # entries lack a message_id field (agent-processed @bot messages).
        self._msg_content_cache: Dict[str, str] = {}

        # Reply-to dedup: inbound_msg_id -> expire_ts
        # ------------------------------------------------------------------
        # Access control policy (DM / Group)
        # ------------------------------------------------------------------
        dm_policy: str = (
            _extra.get("dm_policy")
            or get_profile_env("YUANBAO_DM_POLICY", "open")
        ).strip().lower()

        _dm_allow_from_raw: str = (
            _extra.get("dm_allow_from")
            or get_profile_env("YUANBAO_DM_ALLOW_FROM", "")
        )
        dm_allow_from: list[str] = [x.strip() for x in _dm_allow_from_raw.split(",") if x.strip()]

        group_policy: str = (
            _extra.get("group_policy")
            or get_profile_env("YUANBAO_GROUP_POLICY", "open")
        ).strip().lower()

        _group_allow_from_raw: str = (
            _extra.get("group_allow_from")
            or get_profile_env("YUANBAO_GROUP_ALLOW_FROM", "")
        )
        group_allow_from: list[str] = [x.strip() for x in _group_allow_from_raw.split(",") if x.strip()]

        self._access_policy = AccessPolicy(
            dm_policy=dm_policy,
            dm_allow_from=dm_allow_from,
            group_policy=group_policy,
            group_allow_from=group_allow_from,
        )

        # Group query service (AI tool backing)
        self._group_query = GroupQueryService(self)

        # Inbound message processing pipeline (middleware pattern)
        self._inbound_pipeline: InboundPipeline = InboundPipelineBuilder.build()

        # ------------------------------------------------------------------
        # Auto-sethome: first user to message the bot becomes the owner.
        # If no home channel is configured, the first conversation will be
        # automatically set as the home channel.  When the existing home
        # channel is a group chat (group:xxx), it stays eligible for
        # upgrade — the first DM will override it with direct:xxx.
        # ------------------------------------------------------------------
        _existing_home = get_profile_env("YUANBAO_HOME_CHANNEL") or (
            config.home_channel.chat_id if config.home_channel else ""
        )
        self._auto_sethome_done: bool = bool(_existing_home) and not _existing_home.startswith("group:")

    # ------------------------------------------------------------------
    # Task tracking helper
    # ------------------------------------------------------------------

    def _track_task(self, task: asyncio.Task) -> asyncio.Task:
        """Register a fire-and-forget task so it won't be GC'd prematurely."""
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)
        return task

    # ------------------------------------------------------------------
    # Abstract method implementations
    # ------------------------------------------------------------------

    async def connect(self) -> bool:
        """Connect to Yuanbao WS gateway and authenticate.

        Delegates to ConnectionManager.open().
        """
        return await self._connection.open()

    async def disconnect(self) -> None:
        """Cancel background tasks and close the WebSocket connection."""
        if YuanbaoAdapter._active_instance is self:
            YuanbaoAdapter.set_active(None)

        self._running = False
        self._mark_disconnected()
        self._release_platform_lock()

        # Delegate to managers
        await self._connection.close()
        await self._outbound.close()

        # Cancel all in-flight inbound dispatch tasks
        for task in list(self._inbound_tasks):
            if not task.done():
                task.cancel()
        self._inbound_tasks.clear()

        self._group_queues.clear()

        logger.info("[%s] Disconnected", self.name)

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        group_code: str = "",
    ) -> SendResult:
        """Send text message with auto-chunking. Delegates to OutboundManager."""
        return await self._outbound.send_text(chat_id, content, reply_to, group_code=group_code)

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        """Return basic chat metadata derived from the chat_id prefix.

        chat_id conventions:
          "group:<group_code>"  → group chat
          "direct:<account>"   → C2C / direct message (default)

        TODO (T06): fetch real chat name/member-count from Yuanbao API.
        """
        if chat_id.startswith("group:"):
            return {"name": chat_id, "type": "group"}
        return {"name": chat_id, "type": "dm"}

    async def send_typing(self, chat_id: str, metadata: Optional[dict] = None) -> None:
        """Send "typing" status heartbeat (RUNNING). Delegates to OutboundManager."""
        try:
            await self._outbound.start_typing(chat_id)
        except Exception:
            pass

    async def stop_typing(self, chat_id: str) -> None:
        """Stop the RUNNING heartbeat loop without sending FINISH immediately.

        FINISH is sent by send() after actual message delivery to ensure correct ordering:
        RUNNING... -> message arrives -> FINISH.
        """
        try:
            await self._outbound.stop_typing(chat_id, send_finish=False)
        except Exception:
            pass

    async def _process_message_background(self, event, session_key: str) -> None:
        """Wrap base class processing with a slow-response notifier."""
        chat_id = event.source.chat_id
        await self._outbound.start_slow_notifier(chat_id)
        try:
            await super()._process_message_background(event, session_key)
        finally:
            self._outbound.cancel_slow_notifier(chat_id)

    # ------------------------------------------------------------------
    # Group query (delegate to GroupQueryService)
    # ------------------------------------------------------------------

    async def query_group_info(self, group_code: str) -> Optional[dict]:
        """Query group info (delegates to GroupQueryService)."""
        return await self._group_query.query_group_info_raw(group_code)

    async def get_group_member_list(
        self, group_code: str, offset: int = 0, limit: int = 200
    ) -> Optional[dict]:
        """Query group member list (delegates to GroupQueryService)."""
        return await self._group_query.get_group_member_list_raw(group_code, offset=offset, limit=limit)

    # ------------------------------------------------------------------
    # DM active private chat + access control
    # ------------------------------------------------------------------

    DM_MAX_CHARS = 10000  # DM text limit

    async def send_dm(self, user_id: str, text: str, group_code: str = "") -> SendResult:
        """
        Actively send C2C private chat message.

        Args:
            user_id: Target user ID
            text: Message text (limit 10000 characters)
            group_code: Source group code (for group-originated DM context)

        Returns:
            SendResult
        """
        if not self._access_policy.is_dm_allowed(user_id):
            return SendResult(success=False, error="DM access denied for this user")
        if len(text) > self.DM_MAX_CHARS:
            text = text[:self.DM_MAX_CHARS] + "\n...(truncated)"
        chat_id = f"direct:{user_id}"
        return await self.send(chat_id, text, group_code=group_code)

    # ------------------------------------------------------------------
    # Media send methods
    # ------------------------------------------------------------------

    async def send_image(
        self,
        chat_id: str,
        image_url: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[dict] = None,
        **kwargs: Any,
    ) -> SendResult:
        """Send image message (URL). Delegates to OutboundManager via ImageUrlHandler."""
        return await self._outbound.send_media(
            chat_id, "image_url",
            reply_to=reply_to, caption=caption, image_url=image_url,
            **kwargs,
        )

    async def send_image_file(
        self,
        chat_id: str,
        image_path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[dict] = None,
        **kwargs: Any,
    ) -> SendResult:
        """Send local image file. Delegates to OutboundManager via ImageFileHandler."""
        return await self._outbound.send_media(
            chat_id, "image_file",
            reply_to=reply_to, caption=caption, image_path=image_path,
            **kwargs,
        )

    async def send_file(
        self,
        chat_id: str,
        file_url: str,
        filename: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[dict] = None,
        **kwargs: Any,
    ) -> SendResult:
        """Send file message (URL). Delegates to OutboundManager via FileUrlHandler."""
        return await self._outbound.send_media(
            chat_id, "file_url",
            reply_to=reply_to, file_url=file_url, filename=filename,
            **kwargs,
        )

    async def send_sticker(
        self,
        chat_id: str,
        sticker_name: Optional[str] = None,
        face_index: Optional[int] = None,
        reply_to: Optional[str] = None,
        **kwargs: Any,
    ) -> SendResult:
        """Send sticker/emoji. Delegates to OutboundManager via StickerHandler."""
        return await self._outbound.send_media(
            chat_id, "sticker",
            reply_to=reply_to,
            sticker_name=sticker_name, face_index=face_index,
            **kwargs,
        )

    async def send_document(
        self,
        chat_id: str,
        file_path: str,
        filename: Optional[str] = None,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[dict] = None,
        **kwargs: Any,
    ) -> SendResult:
        """Send local file (document). Delegates to OutboundManager via DocumentHandler."""
        return await self._outbound.send_media(
            chat_id, "document",
            reply_to=reply_to, caption=caption,
            file_path=file_path, filename=filename,
            **kwargs,
        )

    async def _get_cached_token(self) -> dict:
        """Get the current valid sign token (using module-level cache)."""
        return await SignManager.get_token(
            self._app_key, self._app_secret, self._api_domain,
            route_env=self._route_env,
        )

    def get_status(self) -> dict:
        """Return a snapshot of the current connection status."""
        conn = self._connection
        return {
            "connected": conn.is_connected,
            "bot_id": self._bot_id,
            "connect_id": conn.connect_id,
            "reconnect_attempts": conn.reconnect_attempts,
            "ws_url": self._ws_url,
        }


# ---------------------------------------------------------------------------
# Module-level thin delegates (preserve import compatibility for external callers)
# ---------------------------------------------------------------------------


def get_active_adapter() -> Optional["YuanbaoAdapter"]:
    """Delegate to ``YuanbaoAdapter.get_active()``."""
    return YuanbaoAdapter.get_active()


async def send_yuanbao_direct(
    adapter: "YuanbaoAdapter",
    chat_id: str,
    message: str,
    media_files: Optional[List[Tuple[str, bool]]] = None,
) -> Dict[str, Any]:
    """Delegate to ``OutboundManager.send_direct``."""
    return await adapter._outbound.send_direct(chat_id, message, media_files)
