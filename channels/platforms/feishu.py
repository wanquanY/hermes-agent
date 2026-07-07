"""
Feishu/Lark platform adapter.

Supports:
- WebSocket long connection and Webhook transport
- Direct-message and group @mention-gated text receive/send
- Inbound image/file/audio/media caching
- Gateway allowlist integration via FEISHU_ALLOWED_USERS
- Persistent dedup state across restarts
- Per-chat serial message processing (matches openclaw createChatQueue)
- Processing status reactions: Typing while working, removed on success,
  swapped for CrossMark on failure
- Reaction events routed as synthetic text events (matches openclaw)
- Interactive card button-click events routed as synthetic COMMAND events
- Webhook anomaly tracking (matches openclaw createWebhookAnomalyTracker)
- Verification token validation as second auth layer (matches openclaw)

Feishu identity model
---------------------
Feishu uses three user-ID tiers (official docs:
https://open.feishu.cn/document/home/user-identity-introduction/introduction):

  open_id  (ou_xxx)  — **App-scoped**.  The same person gets a different
                        open_id under each Feishu app.  Always available in
                        event payloads without extra permissions.
  user_id  (u_xxx)   — **Tenant-scoped**.  Stable within a company but
                        requires the ``contact:user.employee_id:readonly``
                        scope.  May not be present.
  union_id (on_xxx)  — **Developer-scoped**.  Same across all apps owned by
                        one developer/ISV.  Best cross-app stable ID.

For bots specifically:

  app_id              — The application's canonical credential identifier.
  bot open_id         — Returned by ``/bot/v3/info``.  This is the bot's own
                        open_id *within its app context* and is what Feishu
                        puts in ``mentions[].id.open_id`` when someone
                        @-mentions the bot.  Used for mention gating only.

In single-bot mode (what Hermes currently supports), open_id works as a
de-facto unique user identifier since there is only one app context.

SessionSource.user_id uses ``open_id`` first because QR onboarding stores the
owner allowlist as an open_id.  ``user_id_alt`` carries ``union_id`` when
available, then tenant ``user_id`` as a secondary stable identity for session
partitioning and diagnostics.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import itertools
import json
import logging
import mimetypes
import os
import re
import threading
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Literal, Optional, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

# aiohttp/websockets are independent optional deps — import outside lark_oapi
# so they remain available for tests and webhook mode even if lark_oapi is missing.
try:
    import aiohttp
    from aiohttp import web
except ImportError:
    aiohttp = None  # type: ignore[assignment]
    web = None  # type: ignore[assignment]

try:
    import websockets
except ImportError:
    websockets = None  # type: ignore[assignment]

try:
    import lark_oapi as lark
    from lark_oapi.api.application.v6 import GetApplicationRequest
    from lark_oapi.api.im.v1 import (
        CreateFileRequest,
        CreateFileRequestBody,
        CreateImageRequest,
        CreateImageRequestBody,
        CreateMessageRequest,
        CreateMessageRequestBody,
        GetChatRequest,
        GetMessageRequest,
        GetMessageResourceRequest,
        P2ImMessageMessageReadV1,
        ReplyMessageRequest,
        ReplyMessageRequestBody,
        UpdateMessageRequest,
        UpdateMessageRequestBody,
    )
    from lark_oapi.core import AccessTokenType, HttpMethod
    from lark_oapi.core.const import FEISHU_DOMAIN, LARK_DOMAIN
    from lark_oapi.core.model import BaseRequest
    from lark_oapi.event.callback.model.p2_card_action_trigger import (
        CallBackCard,
        P2CardActionTriggerResponse,
    )
    from lark_oapi.event.dispatcher_handler import EventDispatcherHandler
    from lark_oapi.ws import Client as FeishuWSClient

    FEISHU_AVAILABLE = True
except ImportError:
    FEISHU_AVAILABLE = False
    lark = None  # type: ignore[assignment]
    CallBackCard = None  # type: ignore[assignment]
    P2CardActionTriggerResponse = None  # type: ignore[assignment]
    EventDispatcherHandler = None  # type: ignore[assignment]
    FeishuWSClient = None  # type: ignore[assignment]
    FEISHU_DOMAIN = None  # type: ignore[assignment]
    LARK_DOMAIN = None  # type: ignore[assignment]

FEISHU_WEBSOCKET_AVAILABLE = websockets is not None
FEISHU_WEBHOOK_AVAILABLE = aiohttp is not None

from channels.config import Platform, PlatformConfig
from channels.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    ProcessingOutcome,
    SendResult,
    SUPPORTED_DOCUMENT_TYPES,
    cache_document_from_bytes,
    cache_image_from_url,
    cache_audio_from_bytes,
    cache_image_from_bytes,
)
from channels.runtime_status import acquire_scoped_lock, release_scoped_lock
from hermes_constants import get_hermes_home
from utils import atomic_json_write

logger = logging.getLogger(__name__)

from channels.platforms.feishu_message import (
    FALLBACK_ATTACHMENT_TEXT,
    FALLBACK_FORWARD_TEXT,
    FALLBACK_IMAGE_TEXT,
    FALLBACK_INTERACTIVE_TEXT,
    FALLBACK_POST_TEXT,
    FALLBACK_SHARE_CHAT_TEXT,
    FeishuAdapterSettings,
    FeishuBatchState,
    FeishuGroupRule,
    FeishuMentionRef,
    FeishuNormalizedMessage,
    FeishuPostMediaRef,
    FeishuPostParseResult,
    RejectReason,
    _FeishuBotIdentity,
    _build_markdown_post_payload,
    _build_mention_hint,
    _build_mentions_map,
    _coerce_int,
    _coerce_required_int,
    _is_bot_sender,
    _normalize_feishu_text,
    _sender_identity,
    _strip_markdown_to_plain_text,
    _strip_edge_self_mentions,
    _to_boolean,
    normalize_feishu_message,
    parse_feishu_post_payload,
)
from channels.platforms.feishu_content import FeishuContentMixin
from channels.platforms.feishu_inbound_state import FeishuInboundStateMixin
from channels.platforms.feishu_outbound import FeishuOutboundMixin
from channels.platforms.feishu_webhook import FeishuWebhookMixin

# ---------------------------------------------------------------------------
# Regex patterns
# ---------------------------------------------------------------------------

_MARKDOWN_HINT_RE = re.compile(
    r"(^#{1,6}\s)|(^\s*[-*]\s)|(^\s*\d+\.\s)|(^\s*---+\s*$)|(```)|(`[^`\n]+`)|(\*\*[^*\n].+?\*\*)|(~~[^~\n].+?~~)|(<u>.+?</u>)|(\*[^*\n]+\*)|(\[[^\]]+\]\([^)]+\))|(^>\s)",
    re.MULTILINE,
)
# Detect markdown tables: a line starting with | followed by a separator line.
# Feishu post-type 'md' elements do not render tables, so we force text mode.
_MARKDOWN_TABLE_RE = re.compile(r"^\|.*\|\n\|[-|: ]+\|", re.MULTILINE)
_MARKDOWN_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
_MARKDOWN_FENCE_OPEN_RE = re.compile(r"^```([^\n`]*)\s*$")
_MARKDOWN_FENCE_CLOSE_RE = re.compile(r"^```\s*$")
_MENTION_RE = re.compile(r"@_user_\d+")
_MULTISPACE_RE = re.compile(r"[ \t]{2,}")
_POST_CONTENT_INVALID_RE = re.compile(r"content format of the post type is incorrect", re.IGNORECASE)
# ---------------------------------------------------------------------------
# Media type sets and upload constants
# ---------------------------------------------------------------------------

_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}
_AUDIO_EXTENSIONS = {".ogg", ".mp3", ".wav", ".m4a", ".aac", ".flac", ".opus", ".webm"}
_VIDEO_EXTENSIONS = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".m4v", ".3gp"}
_DOCUMENT_MIME_TO_EXT = {mime: ext for ext, mime in SUPPORTED_DOCUMENT_TYPES.items()}
_FEISHU_IMAGE_UPLOAD_TYPE = "message"
_FEISHU_FILE_UPLOAD_TYPE = "stream"
_FEISHU_OPUS_UPLOAD_EXTENSIONS = {".ogg", ".opus"}
_FEISHU_MEDIA_UPLOAD_EXTENSIONS = {".mp4", ".mov", ".avi", ".m4v"}
_FEISHU_DOC_UPLOAD_TYPES = {
    ".pdf": "pdf",
    ".doc": "doc",
    ".docx": "doc",
    ".xls": "xls",
    ".xlsx": "xls",
    ".ppt": "ppt",
    ".pptx": "ppt",
}
# ---------------------------------------------------------------------------
# Connection, retry and batching tuning
# ---------------------------------------------------------------------------

_MAX_TEXT_INJECT_BYTES = 100 * 1024
_FEISHU_CONNECT_ATTEMPTS = 3
_FEISHU_SEND_ATTEMPTS = 3
_FEISHU_APP_LOCK_SCOPE = "feishu-app-id"
_DEFAULT_TEXT_BATCH_DELAY_SECONDS = 0.6
_DEFAULT_TEXT_BATCH_MAX_MESSAGES = 8
_DEFAULT_TEXT_BATCH_MAX_CHARS = 4000
_DEFAULT_MEDIA_BATCH_DELAY_SECONDS = 0.8
_DEFAULT_DEDUP_CACHE_SIZE = 2048
_DEFAULT_WEBHOOK_HOST = "127.0.0.1"
_DEFAULT_WEBHOOK_PORT = 8765
_DEFAULT_WEBHOOK_PATH = "/feishu/webhook"
# ---------------------------------------------------------------------------
# TTL, rate-limit and webhook security constants
# ---------------------------------------------------------------------------

_FEISHU_DEDUP_TTL_SECONDS = 24 * 60 * 60          # 24 hours — matches openclaw
_FEISHU_SENDER_NAME_TTL_SECONDS = 10 * 60          # 10 minutes sender-name cache
_FEISHU_WEBHOOK_MAX_BODY_BYTES = 1 * 1024 * 1024   # 1 MB body limit
_FEISHU_WEBHOOK_RATE_WINDOW_SECONDS = 60            # sliding window for rate limiter
_FEISHU_WEBHOOK_RATE_LIMIT_MAX = 120               # max requests per window per IP — matches openclaw
_FEISHU_WEBHOOK_RATE_MAX_KEYS = 4096               # max tracked keys (prevents unbounded growth)
_FEISHU_WEBHOOK_BODY_TIMEOUT_SECONDS = 30          # max seconds to read request body
_FEISHU_WEBHOOK_ANOMALY_THRESHOLD = 25             # consecutive error responses before WARNING log
_FEISHU_WEBHOOK_ANOMALY_TTL_SECONDS = 6 * 60 * 60  # anomaly tracker TTL (6 hours) — matches openclaw
_FEISHU_CARD_ACTION_DEDUP_TTL_SECONDS = 15 * 60    # card action token dedup window (15 min)

_APPROVAL_CHOICE_MAP: Dict[str, str] = {
    "approve_once": "once",
    "approve_session": "session",
    "approve_always": "always",
    "deny": "deny",
}
_APPROVAL_LABEL_MAP: Dict[str, str] = {
    "once": "Approved once",
    "session": "Approved for session",
    "always": "Approved permanently",
    "deny": "Denied",
}
_FEISHU_BOT_MSG_TRACK_SIZE = 512                   # LRU size for tracking sent message IDs
_FEISHU_REPLY_FALLBACK_CODES = frozenset({230011, 231003})  # reply target withdrawn/missing → create fallback

# Feishu reactions render as prominent badges, unlike Discord/Telegram's
# small footer emoji — a success badge on every message would add noise, so
# we only mark start (Typing) and failure (CrossMark); the reply itself is
# the success signal.
_FEISHU_REACTION_IN_PROGRESS = "Typing"
_FEISHU_REACTION_FAILURE = "CrossMark"
# Bound on the (message_id → reaction_id) handle cache. Happy-path entries
# drain on completion; the cap is a safeguard against unbounded growth from
# delete-failures, not a capacity plan.
_FEISHU_PROCESSING_REACTION_CACHE_SIZE = 1024

# QR onboarding constants
_ONBOARD_ACCOUNTS_URLS = {
    "feishu": "https://accounts.feishu.cn",
    "lark": "https://accounts.larksuite.com",
}
_ONBOARD_OPEN_URLS = {
    "feishu": "https://open.feishu.cn",
    "lark": "https://open.larksuite.com",
}
_REGISTRATION_PATH = "/oauth/v1/app/registration"
_ONBOARD_REQUEST_TIMEOUT_S = 10

# ---------------------------------------------------------------------------
# Fallback display strings
# ---------------------------------------------------------------------------

def _run_official_feishu_ws_client(ws_client: Any, adapter: Any) -> None:
    """Run the official Lark WS client in its own thread-local event loop."""
    import lark_oapi.ws.client as ws_client_module

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    ws_client_module.loop = loop
    adapter._ws_thread_loop = loop

    original_connect = ws_client_module.websockets.connect
    original_configure = getattr(ws_client, "_configure", None)

    def _apply_runtime_ws_overrides() -> None:
        try:
            setattr(ws_client, "_reconnect_nonce", adapter._ws_reconnect_nonce)
            setattr(ws_client, "_reconnect_interval", adapter._ws_reconnect_interval)
            if adapter._ws_ping_interval is not None:
                setattr(ws_client, "_ping_interval", adapter._ws_ping_interval)
        except Exception:
            logger.debug("[Feishu] Failed to apply websocket runtime overrides", exc_info=True)

    def _connect_with_overrides(*args: Any, **kwargs: Any) -> Any:
        if adapter._ws_ping_interval is not None and "ping_interval" not in kwargs:
            kwargs["ping_interval"] = adapter._ws_ping_interval
        if adapter._ws_ping_timeout is not None and "ping_timeout" not in kwargs:
            kwargs["ping_timeout"] = adapter._ws_ping_timeout
        return original_connect(*args, **kwargs)

    def _configure_with_overrides(conf: Any) -> Any:
        if original_configure is None:
            raise RuntimeError("Feishu _configure_with_overrides called but original_configure is None")
        result = original_configure(conf)
        _apply_runtime_ws_overrides()
        return result

    ws_client_module.websockets.connect = _connect_with_overrides
    if original_configure is not None:
        setattr(ws_client, "_configure", _configure_with_overrides)
    _apply_runtime_ws_overrides()
    try:
        ws_client.start()
    except Exception:
        pass
    finally:
        ws_client_module.websockets.connect = original_connect
        if original_configure is not None:
            setattr(ws_client, "_configure", original_configure)
        pending = [t for t in asyncio.all_tasks(loop) if not t.done()]
        for task in pending:
            task.cancel()
        if pending:
            loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        try:
            loop.stop()
        except Exception:
            pass
        try:
            loop.close()
        except Exception:
            pass
        adapter._ws_thread_loop = None


def check_feishu_requirements() -> bool:
    """Check if Feishu/Lark dependencies are available.

    Lazy-installs the full Feishu dependency set on first call. This must run
    even when lark-oapi itself is already importable because websocket mode can
    require optional transitive packages such as python-socks when the host has
    a SOCKS proxy configured.
    """
    def _import():
        import lark_oapi as lark
        from lark_oapi.api.application.v6 import GetApplicationRequest
        from lark_oapi.api.im.v1 import (
            CreateFileRequest, CreateFileRequestBody,
            CreateImageRequest, CreateImageRequestBody,
            CreateMessageRequest, CreateMessageRequestBody,
            GetChatRequest, GetMessageRequest, GetMessageResourceRequest,
            P2ImMessageMessageReadV1,
            ReplyMessageRequest, ReplyMessageRequestBody,
            UpdateMessageRequest, UpdateMessageRequestBody,
        )
        from lark_oapi.core import AccessTokenType, HttpMethod
        from lark_oapi.core.const import FEISHU_DOMAIN, LARK_DOMAIN
        from lark_oapi.core.model import BaseRequest
        from lark_oapi.event.callback.model.p2_card_action_trigger import (
            CallBackCard, P2CardActionTriggerResponse,
        )
        from lark_oapi.event.dispatcher_handler import EventDispatcherHandler
        from lark_oapi.ws import Client as FeishuWSClient
        return {
            "lark": lark,
            "GetApplicationRequest": GetApplicationRequest,
            "CreateFileRequest": CreateFileRequest,
            "CreateFileRequestBody": CreateFileRequestBody,
            "CreateImageRequest": CreateImageRequest,
            "CreateImageRequestBody": CreateImageRequestBody,
            "CreateMessageRequest": CreateMessageRequest,
            "CreateMessageRequestBody": CreateMessageRequestBody,
            "GetChatRequest": GetChatRequest,
            "GetMessageRequest": GetMessageRequest,
            "GetMessageResourceRequest": GetMessageResourceRequest,
            "P2ImMessageMessageReadV1": P2ImMessageMessageReadV1,
            "ReplyMessageRequest": ReplyMessageRequest,
            "ReplyMessageRequestBody": ReplyMessageRequestBody,
            "UpdateMessageRequest": UpdateMessageRequest,
            "UpdateMessageRequestBody": UpdateMessageRequestBody,
            "AccessTokenType": AccessTokenType,
            "HttpMethod": HttpMethod,
            "FEISHU_DOMAIN": FEISHU_DOMAIN,
            "LARK_DOMAIN": LARK_DOMAIN,
            "BaseRequest": BaseRequest,
            "CallBackCard": CallBackCard,
            "P2CardActionTriggerResponse": P2CardActionTriggerResponse,
            "EventDispatcherHandler": EventDispatcherHandler,
            "FeishuWSClient": FeishuWSClient,
            "FEISHU_AVAILABLE": True,
        }

    from tools.lazy_deps import ensure_and_bind
    return ensure_and_bind("platform.feishu", _import, globals(), prompt=False)


class FeishuAdapter(
    FeishuOutboundMixin,
    FeishuContentMixin,
    FeishuWebhookMixin,
    FeishuInboundStateMixin,
    BasePlatformAdapter,
):
    """Feishu/Lark bot adapter."""

    MAX_MESSAGE_LENGTH = 8000
    # Threshold for detecting Feishu client-side message splits.
    # When a chunk is near the ~4096-char practical limit, a continuation
    # is almost certain.
    _SPLIT_THRESHOLD = 4000

    # =========================================================================
    # Lifecycle — init / settings / connect / disconnect
    # =========================================================================

    def __init__(self, config: PlatformConfig):
        super().__init__(config, Platform.FEISHU)

        self._settings = self._load_settings(config.extra or {})
        self._apply_settings(self._settings)
        self._client: Optional[Any] = None
        self._ws_client: Optional[Any] = None
        self._ws_future: Optional[asyncio.Future] = None
        self._ws_thread_loop: Optional[asyncio.AbstractEventLoop] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._webhook_runner: Optional[Any] = None
        self._webhook_site: Optional[Any] = None
        self._event_handler: Optional[Any] = None
        self._seen_message_ids: Dict[str, float] = {}  # message_id → seen_at (time.time())
        self._seen_message_order: List[str] = []
        self._dedup_state_path = get_hermes_home() / "feishu_seen_message_ids.json"
        self._dedup_lock = threading.Lock()
        self._sender_name_cache: Dict[str, tuple[str, float]] = {}  # sender_id → (name, expire_at)
        self._webhook_rate_counts: Dict[str, tuple[int, float]] = {}  # rate_key → (count, window_start)
        self._webhook_anomaly_counts: Dict[str, tuple[int, str, float]] = {}  # ip → (count, last_status, first_seen)
        self._card_action_tokens: Dict[str, float] = {}  # token → first_seen_time
        # Inbound events that arrived before the adapter loop was ready
        # (e.g. during startup/restart or network-flap reconnect). A single
        # drainer thread replays them as soon as the loop becomes available.
        self._pending_inbound_events: List[Any] = []
        self._pending_inbound_lock = threading.Lock()
        self._pending_drain_scheduled = False
        self._pending_inbound_max_depth = 1000  # cap queue; drop oldest beyond
        self._chat_locks: Dict[str, asyncio.Lock] = {}  # chat_id → lock (per-chat serial processing)
        self._sent_message_ids_to_chat: Dict[str, str] = {}  # message_id → chat_id (for reaction routing)
        self._sent_message_id_order: List[str] = []  # LRU order for _sent_message_ids_to_chat
        self._chat_info_cache: Dict[str, Dict[str, Any]] = {}
        self._message_text_cache: Dict[str, Optional[str]] = {}
        self._app_lock_identity: Optional[str] = None
        self._text_batch_state = FeishuBatchState()
        self._pending_text_batches = self._text_batch_state.events
        self._pending_text_batch_tasks = self._text_batch_state.tasks
        self._pending_text_batch_counts = self._text_batch_state.counts
        self._media_batch_state = FeishuBatchState()
        self._pending_media_batches = self._media_batch_state.events
        self._pending_media_batch_tasks = self._media_batch_state.tasks
        # Exec approval button state (approval_id → {session_key, message_id, chat_id})
        self._approval_state: Dict[int, Dict[str, str]] = {}
        self._approval_counter = itertools.count(1)
        # Update prompt button state (prompt_id → {session_key, message_id, chat_id})
        self._update_prompt_state: Dict[int, Dict[str, str]] = {}
        self._update_prompt_counter = itertools.count(1)
        # Feishu reaction deletion requires the opaque reaction_id returned
        # by create, so we cache it per message_id.
        self._pending_processing_reactions: "OrderedDict[str, str]" = OrderedDict()
        self._load_seen_message_ids()

    @staticmethod
    def _load_settings(extra: Dict[str, Any]) -> FeishuAdapterSettings:
        # Parse per-group rules from config
        raw_group_rules = extra.get("group_rules", {})
        group_rules: Dict[str, FeishuGroupRule] = {}
        if isinstance(raw_group_rules, dict):
            for chat_id, rule_cfg in raw_group_rules.items():
                if not isinstance(rule_cfg, dict):
                    continue
                # Only override when the key is explicitly set — missing vs false
                # must not collapse.
                per_chat_require_mention: Optional[bool] = None
                if "require_mention" in rule_cfg:
                    per_chat_require_mention = _to_boolean(rule_cfg.get("require_mention"))
                group_rules[str(chat_id)] = FeishuGroupRule(
                    policy=str(rule_cfg.get("policy", "open")).strip().lower(),
                    allowlist={str(u).strip() for u in rule_cfg.get("allowlist", []) if str(u).strip()},
                    blacklist={str(u).strip() for u in rule_cfg.get("blacklist", []) if str(u).strip()},
                    require_mention=per_chat_require_mention,
                )

        # Bot-level admins
        raw_admins = extra.get("admins", [])
        admins = frozenset(str(u).strip() for u in raw_admins if str(u).strip())

        # Default group policy (for groups not in group_rules)
        default_group_policy = str(extra.get("default_group_policy", "")).strip().lower()

        # Env-only so adapter and gateway auth bypass share one source; yaml
        # feishu.allow_bots is bridged to this env var at config load.
        allow_bots = os.getenv("FEISHU_ALLOW_BOTS", "none").strip().lower()
        if allow_bots not in {"none", "mentions", "all"}:
            logger.warning(
                "[Feishu] Unknown allow_bots=%r, falling back to 'none'. Valid: none, mentions, all.",
                allow_bots,
            )
            allow_bots = "none"

        return FeishuAdapterSettings(
            app_id=str(extra.get("app_id") or os.getenv("FEISHU_APP_ID", "")).strip(),
            app_secret=str(extra.get("app_secret") or os.getenv("FEISHU_APP_SECRET", "")).strip(),
            domain_name=str(extra.get("domain") or os.getenv("FEISHU_DOMAIN", "feishu")).strip().lower(),
            connection_mode=str(
                extra.get("connection_mode") or os.getenv("FEISHU_CONNECTION_MODE", "websocket")
            ).strip().lower(),
            encrypt_key=os.getenv("FEISHU_ENCRYPT_KEY", "").strip(),
            verification_token=os.getenv("FEISHU_VERIFICATION_TOKEN", "").strip(),
            group_policy=os.getenv("FEISHU_GROUP_POLICY", "allowlist").strip().lower(),
            allowed_group_users=frozenset(
                item.strip()
                for item in os.getenv("FEISHU_ALLOWED_USERS", "").split(",")
                if item.strip()
            ),
            bot_open_id=os.getenv("FEISHU_BOT_OPEN_ID", "").strip(),
            bot_user_id=os.getenv("FEISHU_BOT_USER_ID", "").strip(),
            bot_name=os.getenv("FEISHU_BOT_NAME", "").strip(),
            dedup_cache_size=max(
                32,
                int(os.getenv("HERMES_FEISHU_DEDUP_CACHE_SIZE", str(_DEFAULT_DEDUP_CACHE_SIZE))),
            ),
            text_batch_delay_seconds=float(
                os.getenv("HERMES_FEISHU_TEXT_BATCH_DELAY_SECONDS", str(_DEFAULT_TEXT_BATCH_DELAY_SECONDS))
            ),
            text_batch_split_delay_seconds=float(
                os.getenv("HERMES_FEISHU_TEXT_BATCH_SPLIT_DELAY_SECONDS", "2.0")
            ),
            text_batch_max_messages=max(
                1,
                int(os.getenv("HERMES_FEISHU_TEXT_BATCH_MAX_MESSAGES", str(_DEFAULT_TEXT_BATCH_MAX_MESSAGES))),
            ),
            text_batch_max_chars=max(
                1,
                int(os.getenv("HERMES_FEISHU_TEXT_BATCH_MAX_CHARS", str(_DEFAULT_TEXT_BATCH_MAX_CHARS))),
            ),
            media_batch_delay_seconds=float(
                os.getenv("HERMES_FEISHU_MEDIA_BATCH_DELAY_SECONDS", str(_DEFAULT_MEDIA_BATCH_DELAY_SECONDS))
            ),
            webhook_host=str(
                extra.get("webhook_host") or os.getenv("FEISHU_WEBHOOK_HOST", _DEFAULT_WEBHOOK_HOST)
            ).strip(),
            webhook_port=int(
                extra.get("webhook_port") or os.getenv("FEISHU_WEBHOOK_PORT", str(_DEFAULT_WEBHOOK_PORT))
            ),
            webhook_path=(
                str(extra.get("webhook_path") or os.getenv("FEISHU_WEBHOOK_PATH", _DEFAULT_WEBHOOK_PATH)).strip()
                or _DEFAULT_WEBHOOK_PATH
            ),
            ws_reconnect_nonce=_coerce_required_int(extra.get("ws_reconnect_nonce"), default=30, min_value=0),
            ws_reconnect_interval=_coerce_required_int(extra.get("ws_reconnect_interval"), default=120, min_value=1),
            ws_ping_interval=_coerce_int(extra.get("ws_ping_interval"), default=None, min_value=1),
            ws_ping_timeout=_coerce_int(extra.get("ws_ping_timeout"), default=None, min_value=1),
            admins=admins,
            default_group_policy=default_group_policy,
            group_rules=group_rules,
            allow_bots=allow_bots,
            require_mention=_to_boolean(
                extra.get("require_mention", os.getenv("FEISHU_REQUIRE_MENTION", "true"))
            ),
        )

    def _apply_settings(self, settings: FeishuAdapterSettings) -> None:
        self._app_id = settings.app_id
        self._app_secret = settings.app_secret
        self._domain_name = settings.domain_name
        self._connection_mode = settings.connection_mode
        self._encrypt_key = settings.encrypt_key
        self._verification_token = settings.verification_token
        self._group_policy = settings.group_policy
        self._allowed_group_users = set(settings.allowed_group_users)
        self._admins = set(settings.admins)
        self._default_group_policy = settings.default_group_policy or settings.group_policy
        self._group_rules = settings.group_rules
        self._bot_open_id = settings.bot_open_id
        self._bot_user_id = settings.bot_user_id
        self._bot_name = settings.bot_name
        self._dedup_cache_size = settings.dedup_cache_size
        self._text_batch_delay_seconds = settings.text_batch_delay_seconds
        self._text_batch_split_delay_seconds = settings.text_batch_split_delay_seconds
        self._text_batch_max_messages = settings.text_batch_max_messages
        self._text_batch_max_chars = settings.text_batch_max_chars
        self._media_batch_delay_seconds = settings.media_batch_delay_seconds
        self._webhook_host = settings.webhook_host
        self._webhook_port = settings.webhook_port
        self._webhook_path = settings.webhook_path
        self._ws_reconnect_nonce = settings.ws_reconnect_nonce
        self._ws_reconnect_interval = settings.ws_reconnect_interval
        self._ws_ping_interval = settings.ws_ping_interval
        self._ws_ping_timeout = settings.ws_ping_timeout
        self._allow_bots = settings.allow_bots
        self._require_mention = settings.require_mention

    def _build_event_handler(self) -> Any:
        if EventDispatcherHandler is None:
            return None
        return (
            EventDispatcherHandler.builder(
                self._encrypt_key,
                self._verification_token,
            )
            .register_p2_im_message_message_read_v1(self._on_message_read_event)
            .register_p2_im_message_receive_v1(self._on_message_event)
            .register_p2_im_message_reaction_created_v1(
                lambda data: self._on_reaction_event("im.message.reaction.created_v1", data)
            )
            .register_p2_im_message_reaction_deleted_v1(
                lambda data: self._on_reaction_event("im.message.reaction.deleted_v1", data)
            )
            .register_p2_card_action_trigger(self._on_card_action_trigger)
            .register_p2_im_chat_member_bot_added_v1(self._on_bot_added_to_chat)
            .register_p2_im_chat_member_bot_deleted_v1(self._on_bot_removed_from_chat)
            .register_p2_im_chat_access_event_bot_p2p_chat_entered_v1(self._on_p2p_chat_entered)
            .register_p2_im_message_recalled_v1(self._on_message_recalled)
            .register_p2_customized_event(
                "drive.notice.comment_add_v1",
                self._on_drive_comment_event,
            )
            .build()
        )

    async def connect(self) -> bool:
        """Connect to Feishu/Lark."""
        if not FEISHU_AVAILABLE:
            logger.error("[Feishu] lark-oapi not installed")
            return False
        if not self._app_id or not self._app_secret:
            logger.error("[Feishu] FEISHU_APP_ID or FEISHU_APP_SECRET not set")
            return False
        if self._connection_mode not in {"websocket", "webhook"}:
            logger.error(
                "[Feishu] Unsupported FEISHU_CONNECTION_MODE=%s. Supported modes: websocket, webhook.",
                self._connection_mode,
            )
            return False

        try:
            self._app_lock_identity = self._app_id
            acquired, existing = acquire_scoped_lock(
                _FEISHU_APP_LOCK_SCOPE,
                self._app_lock_identity,
                metadata={"platform": self.platform.value},
            )
            if not acquired:
                owner_pid = existing.get("pid") if isinstance(existing, dict) else None
                message = (
                    "Another local Hermes gateway is already using this Feishu app_id"
                    + (f" (PID {owner_pid})." if owner_pid else ".")
                    + " Stop the other gateway before starting a second Feishu websocket client."
                )
                logger.error("[Feishu] %s", message)
                self._set_fatal_error("feishu_app_lock", message, retryable=False)
                return False

            self._loop = asyncio.get_running_loop()
            await self._connect_with_retry()
            self._mark_connected()
            logger.info("[Feishu] Connected in %s mode (%s)", self._connection_mode, self._domain_name)
            return True
        except Exception as exc:
            await self._release_app_lock()
            message = f"Feishu startup failed: {exc}"
            self._set_fatal_error("feishu_connect_error", message, retryable=True)
            logger.error("[Feishu] Failed to connect: %s", exc, exc_info=True)
            return False

    async def disconnect(self) -> None:
        """Disconnect from Feishu/Lark."""
        self._running = False
        await self._cancel_pending_tasks(self._pending_text_batch_tasks)
        await self._cancel_pending_tasks(self._pending_media_batch_tasks)
        self._reset_batch_buffers()
        self._disable_websocket_auto_reconnect()
        await self._stop_webhook_server()

        ws_thread_loop = self._ws_thread_loop
        if ws_thread_loop is not None and not ws_thread_loop.is_closed():
            logger.debug("[Feishu] Cancelling websocket thread tasks and stopping loop")

            def cancel_all_tasks() -> None:
                tasks = [t for t in asyncio.all_tasks(ws_thread_loop) if not t.done()]
                logger.debug("[Feishu] Found %d pending tasks in websocket thread", len(tasks))
                for task in tasks:
                    task.cancel()
                ws_thread_loop.call_later(0.1, ws_thread_loop.stop)

            ws_thread_loop.call_soon_threadsafe(cancel_all_tasks)

        ws_future = self._ws_future
        if ws_future is not None:
            try:
                logger.debug("[Feishu] Waiting for websocket thread to exit (timeout=10s)")
                await asyncio.wait_for(asyncio.shield(ws_future), timeout=10.0)
                logger.debug("[Feishu] Websocket thread exited cleanly")
            except asyncio.TimeoutError:
                logger.warning("[Feishu] Websocket thread did not exit within 10s - may be stuck")
            except asyncio.CancelledError:
                logger.debug("[Feishu] Websocket thread cancelled during disconnect")
            except Exception as exc:
                logger.debug("[Feishu] Websocket thread exited with error: %s", exc, exc_info=True)

        self._ws_future = None
        self._ws_thread_loop = None
        self._loop = None
        self._event_handler = None
        self._persist_seen_message_ids()
        await self._release_app_lock()

        self._mark_disconnected()
        logger.info("[Feishu] Disconnected")

    async def _cancel_pending_tasks(self, tasks: Dict[str, asyncio.Task]) -> None:
        pending = [task for task in tasks.values() if task and not task.done()]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        tasks.clear()

    def _reset_batch_buffers(self) -> None:
        self._pending_text_batches.clear()
        self._pending_text_batch_counts.clear()
        self._pending_media_batches.clear()

    def _disable_websocket_auto_reconnect(self) -> None:
        if self._ws_client is None:
            return
        try:
            setattr(self._ws_client, "_auto_reconnect", False)
        except Exception:
            pass
        finally:
            self._ws_client = None

    async def _stop_webhook_server(self) -> None:
        if self._webhook_runner is None:
            return
        try:
            await self._webhook_runner.cleanup()
        finally:
            self._webhook_runner = None
            self._webhook_site = None

    # =========================================================================
    # Outbound — send / edit / send_image / send_voice / …
    # =========================================================================

    # =========================================================================
    # Inbound event handlers
    # =========================================================================

    def _on_message_event(self, data: Any) -> None:
        """Normalize Feishu inbound events into MessageEvent.

        Called by the lark_oapi SDK's event dispatcher on a background thread.
        If the adapter loop is not currently accepting callbacks (brief window
        during startup/restart or network-flap reconnect), the event is queued
        for replay instead of dropped.
        """
        loop = self._loop
        if not self._loop_accepts_callbacks(loop):
            start_drainer = self._enqueue_pending_inbound_event(data)
            if start_drainer:
                threading.Thread(
                    target=self._drain_pending_inbound_events,
                    name="feishu-pending-inbound-drainer",
                    daemon=True,
                ).start()
            return
        self._submit_on_loop(loop, self._handle_message_event_data(data))

    def _enqueue_pending_inbound_event(self, data: Any) -> bool:
        """Append an event to the pending-inbound queue.

        Returns True if the caller should spawn a drainer thread (no drainer
        currently scheduled), False if a drainer is already running and will
        pick up the new event on its next pass.
        """
        with self._pending_inbound_lock:
            if len(self._pending_inbound_events) >= self._pending_inbound_max_depth:
                # Queue full — drop the oldest to make room. This happens only
                # if the loop stays unavailable for an extended period AND the
                # WS keeps firing callbacks. Still better than silent drops.
                dropped = self._pending_inbound_events.pop(0)
                try:
                    event = getattr(dropped, "event", None)
                    message = getattr(event, "message", None)
                    message_id = str(getattr(message, "message_id", "") or "unknown")
                except Exception:
                    message_id = "unknown"
                logger.error(
                    "[Feishu] Pending-inbound queue full (%d); dropped oldest event %s",
                    self._pending_inbound_max_depth,
                    message_id,
                )
            self._pending_inbound_events.append(data)
            depth = len(self._pending_inbound_events)
            should_start = not self._pending_drain_scheduled
            if should_start:
                self._pending_drain_scheduled = True
        logger.warning(
            "[Feishu] Queued inbound event for replay (loop not ready, queue depth=%d)",
            depth,
        )
        return should_start

    def _drain_pending_inbound_events(self) -> None:
        """Replay queued inbound events once the adapter loop is ready.

        Runs in a dedicated daemon thread. Polls ``_running`` and
        ``_loop_accepts_callbacks`` until events can be dispatched or the
        adapter shuts down. A single drainer handles the entire queue;
        concurrent ``_on_message_event`` calls just append.
        """
        poll_interval = 0.25
        max_wait_seconds = 120.0  # safety cap: drop queue after 2 minutes
        waited = 0.0
        try:
            while True:
                if not getattr(self, "_running", True):
                    # Adapter shutting down — drop queued events rather than
                    # holding them against a closed loop.
                    with self._pending_inbound_lock:
                        dropped = len(self._pending_inbound_events)
                        self._pending_inbound_events.clear()
                    if dropped:
                        logger.warning(
                            "[Feishu] Dropped %d queued inbound event(s) during shutdown",
                            dropped,
                        )
                    return
                loop = self._loop
                if self._loop_accepts_callbacks(loop):
                    with self._pending_inbound_lock:
                        batch = self._pending_inbound_events[:]
                        self._pending_inbound_events.clear()
                    if not batch:
                        # Queue emptied between check and grab; done.
                        with self._pending_inbound_lock:
                            if not self._pending_inbound_events:
                                return
                        continue
                    dispatched = 0
                    requeue: List[Any] = []
                    for event in batch:
                        if self._submit_on_loop(
                            loop, self._handle_message_event_data(event)
                        ):
                            dispatched += 1
                        else:
                            # Loop closed/unavailable — requeue and poll again.
                            requeue.append(event)
                    if requeue:
                        with self._pending_inbound_lock:
                            self._pending_inbound_events[:0] = requeue
                    if dispatched:
                        logger.info(
                            "[Feishu] Replayed %d queued inbound event(s)",
                            dispatched,
                        )
                    if not requeue:
                        # Successfully drained; check if more arrived while
                        # we were dispatching and exit if not.
                        with self._pending_inbound_lock:
                            if not self._pending_inbound_events:
                                return
                    # More events queued or requeue pending — loop again.
                    continue
                if waited >= max_wait_seconds:
                    with self._pending_inbound_lock:
                        dropped = len(self._pending_inbound_events)
                        self._pending_inbound_events.clear()
                    logger.error(
                        "[Feishu] Adapter loop unavailable for %.0fs; "
                        "dropped %d queued inbound event(s)",
                        max_wait_seconds,
                        dropped,
                    )
                    return
                time.sleep(poll_interval)
                waited += poll_interval
        finally:
            with self._pending_inbound_lock:
                self._pending_drain_scheduled = False

    async def _handle_message_event_data(self, data: Any) -> None:
        """Shared inbound message handling for websocket and webhook transports."""
        event = getattr(data, "event", None)
        message = getattr(event, "message", None)
        sender = getattr(event, "sender", None)
        if not message or not sender or not getattr(sender, "sender_id", None):
            logger.debug("[Feishu] Dropping malformed inbound event: missing message/sender")
            return

        message_id = getattr(message, "message_id", None)
        if not message_id or self._is_duplicate(message_id):
            logger.debug("[Feishu] Dropping duplicate/missing message_id: %s", message_id)
            return

        reason = self._admit(sender, message)
        if reason is not None:
            logger.debug("[Feishu] dropping inbound event: %s", reason)
            return

        chat_type = getattr(message, "chat_type", "p2p")
        await self._process_inbound_message(
            data=data,
            message=message,
            sender_id=getattr(sender, "sender_id", None),
            chat_type=chat_type,
            message_id=message_id,
            is_bot=_is_bot_sender(sender),
        )

    def _on_message_read_event(self, data: P2ImMessageMessageReadV1) -> None:
        """Ignore read-receipt events that Hermes does not act on."""
        event = getattr(data, "event", None)
        message = getattr(event, "message", None)
        message_id = getattr(message, "message_id", None) or ""
        logger.debug("[Feishu] Ignoring message_read event: %s", message_id)

    def _on_bot_added_to_chat(self, data: Any) -> None:
        """Handle bot being added to a group chat."""
        event = getattr(data, "event", None)
        chat_id = str(getattr(event, "chat_id", "") or "")
        logger.info("[Feishu] Bot added to chat: %s", chat_id)
        self._chat_info_cache.pop(chat_id, None)

    def _on_bot_removed_from_chat(self, data: Any) -> None:
        """Handle bot being removed from a group chat."""
        event = getattr(data, "event", None)
        chat_id = str(getattr(event, "chat_id", "") or "")
        logger.info("[Feishu] Bot removed from chat: %s", chat_id)
        self._chat_info_cache.pop(chat_id, None)

    def _on_p2p_chat_entered(self, data: Any) -> None:
        logger.debug("[Feishu] User entered P2P chat with bot")

    def _on_message_recalled(self, data: Any) -> None:
        logger.debug("[Feishu] Message recalled by user")

    def _on_drive_comment_event(self, data: Any) -> None:
        """Handle drive document comment notification (drive.notice.comment_add_v1).

        Delegates to :mod:`channels.platforms.feishu_comment` for parsing,
        logging, and reaction.  Scheduling follows the same
        ``run_coroutine_threadsafe`` pattern used by ``_on_message_event``.
        """
        from channels.platforms.feishu_comment import handle_drive_comment_event

        loop = self._loop
        if not self._loop_accepts_callbacks(loop):
            logger.warning("[Feishu] Dropping drive comment event before adapter loop is ready")
            return
        self._submit_on_loop(
            loop,
            handle_drive_comment_event(self._client, data, self_open_id=self._bot_open_id),
        )

    def _on_reaction_event(self, event_type: str, data: Any) -> None:
        """Route user reactions on bot messages as synthetic text events."""
        event = getattr(data, "event", None)
        message_id = str(getattr(event, "message_id", "") or "")
        operator_type = str(getattr(event, "operator_type", "") or "")
        reaction_type_obj = getattr(event, "reaction_type", None)
        emoji_type = str(getattr(reaction_type_obj, "emoji_type", "") or "")
        action = "added" if "created" in event_type else "removed"
        logger.debug(
            "[Feishu] Reaction %s on message %s (operator_type=%s, emoji=%s)",
            action,
            message_id,
            operator_type,
            emoji_type,
        )
        # Drop bot/app-origin reactions to break the feedback loop from our
        # own lifecycle reactions. A human reacting with the same emoji (e.g.
        # clicking Typing on a bot message) is still routed through.
        loop = self._loop
        if (
            operator_type in {"bot", "app"}
            or not message_id
            or loop is None
            or bool(getattr(loop, "is_closed", lambda: False)())
        ):
            return
        self._submit_on_loop(loop, self._handle_reaction_event(event_type, data))

    def _on_card_action_trigger(self, data: Any) -> Any:
        """Handle card-action callback from the Feishu SDK (synchronous).

        For approval actions: parses the event once, returns the resolved card
        inline (the only reliable way to sync all clients), and schedules a
        lightweight async method to actually unblock the agent.

        For other card actions: delegates to ``_handle_card_action_event``.
        """
        loop = self._loop
        if not self._loop_accepts_callbacks(loop):
            logger.warning("[Feishu] Dropping card action before adapter loop is ready")
            return P2CardActionTriggerResponse() if P2CardActionTriggerResponse else None

        event = getattr(data, "event", None)
        action = getattr(event, "action", None)
        action_value = getattr(action, "value", {}) or {}
        hermes_action = action_value.get("hermes_action") if isinstance(action_value, dict) else None
        update_prompt_action = (
            action_value.get("hermes_update_prompt_action")
            if isinstance(action_value, dict) else None
        )

        if hermes_action:
            return self._handle_approval_card_action(event=event, action_value=action_value, loop=loop)
        if update_prompt_action:
            return self._handle_update_prompt_card_action(
                event=event,
                action_value=action_value,
                loop=loop,
            )

        self._submit_on_loop(loop, self._handle_card_action_event(data))
        if P2CardActionTriggerResponse is None:
            return None
        return P2CardActionTriggerResponse()

    @staticmethod
    def _loop_accepts_callbacks(loop: Any) -> bool:
        """Return True when the adapter loop can accept thread-safe submissions."""
        return loop is not None and not bool(getattr(loop, "is_closed", lambda: False)())

    def _submit_on_loop(self, loop: Any, coro: Any) -> bool:
        """Schedule background work on the adapter loop with shared failure logging."""
        from agent.async_utils import safe_schedule_threadsafe
        future = safe_schedule_threadsafe(
            coro, loop,
            logger=logger,
            log_message="[Feishu] Failed to schedule background callback work",
            log_level=logging.WARNING,
        )
        if future is None:
            return False
        future.add_done_callback(self._log_background_failure)
        return True

    def _is_interactive_operator_authorized(self, open_id: str) -> bool:
        """Return whether this card-action operator may answer gated prompts."""
        normalized = str(open_id or "").strip()
        if not normalized:
            return False
        allowed_ids = set(self._admins) | set(self._allowed_group_users)
        if not allowed_ids:
            return True
        return "*" in allowed_ids or normalized in allowed_ids

    def _handle_approval_card_action(self, *, event: Any, action_value: Dict[str, Any], loop: Any) -> Any:
        """Schedule approval resolution and build the synchronous callback response."""
        approval_id = action_value.get("approval_id")
        if approval_id is None:
            logger.debug("[Feishu] Card action missing approval_id, ignoring")
            return P2CardActionTriggerResponse() if P2CardActionTriggerResponse else None
        choice = _APPROVAL_CHOICE_MAP.get(action_value.get("hermes_action"), "deny")

        operator = getattr(event, "operator", None)
        open_id = str(getattr(operator, "open_id", "") or "")
        user_name = self._get_cached_sender_name(open_id) or open_id

        if not self._submit_on_loop(loop, self._resolve_approval(approval_id, choice, user_name)):
            return P2CardActionTriggerResponse() if P2CardActionTriggerResponse else None

        if P2CardActionTriggerResponse is None:
            return None
        response = P2CardActionTriggerResponse()
        if CallBackCard is not None:
            card = CallBackCard()
            card.type = "raw"
            card.data = self._build_resolved_approval_card(choice=choice, user_name=user_name)
            response.card = card
        return response

    def _handle_update_prompt_card_action(self, *, event: Any, action_value: Dict[str, Any], loop: Any) -> Any:
        """Schedule update prompt resolution and build the synchronous callback response."""
        prompt_id = action_value.get("update_prompt_id")
        if prompt_id is None:
            logger.debug("[Feishu] Card action missing update_prompt_id, ignoring")
            return P2CardActionTriggerResponse() if P2CardActionTriggerResponse else None
        if prompt_id not in self._update_prompt_state:
            logger.debug("[Feishu] Update prompt %s already resolved or unknown", prompt_id)
            return P2CardActionTriggerResponse() if P2CardActionTriggerResponse else None

        answer = str(action_value.get("hermes_update_prompt_action", "") or "").strip().lower()
        if answer not in {"y", "n"}:
            logger.debug("[Feishu] Card action has invalid update prompt answer=%r", answer)
            return P2CardActionTriggerResponse() if P2CardActionTriggerResponse else None

        operator = getattr(event, "operator", None)
        open_id = str(getattr(operator, "open_id", "") or "")
        if not self._is_interactive_operator_authorized(open_id):
            logger.warning("[Feishu] Unauthorized update prompt click by %s", open_id or "<unknown>")
            return P2CardActionTriggerResponse() if P2CardActionTriggerResponse else None

        user_name = self._get_cached_sender_name(open_id) or open_id
        if not self._submit_on_loop(loop, self._resolve_update_prompt(prompt_id, answer, user_name)):
            return P2CardActionTriggerResponse() if P2CardActionTriggerResponse else None

        if P2CardActionTriggerResponse is None:
            return None
        response = P2CardActionTriggerResponse()
        if CallBackCard is not None:
            card = CallBackCard()
            card.type = "raw"
            card.data = self._build_resolved_update_prompt_card(answer=answer, user_name=user_name)
            response.card = card
        return response

    async def _resolve_approval(self, approval_id: Any, choice: str, user_name: str) -> None:
        """Pop approval state and unblock the waiting agent thread."""
        state = self._approval_state.pop(approval_id, None)
        if not state:
            logger.debug("[Feishu] Approval %s already resolved or unknown", approval_id)
            return
        try:
            from tools.approval import resolve_gateway_approval
            count = resolve_gateway_approval(state["session_key"], choice)
            logger.info(
                "Feishu button resolved %d approval(s) for session %s (choice=%s, user=%s)",
                count, state["session_key"], choice, user_name,
            )
        except Exception as exc:
            logger.error("Failed to resolve gateway approval from Feishu button: %s", exc)

    async def _resolve_update_prompt(self, prompt_id: Any, answer: str, user_name: str) -> None:
        """Persist an update prompt answer for the detached update process."""
        state = self._update_prompt_state.pop(prompt_id, None)
        if not state:
            logger.debug("[Feishu] Update prompt %s already resolved or unknown", prompt_id)
            return
        try:
            self._write_update_prompt_response(answer)
            logger.info(
                "Feishu update prompt resolved for session %s (answer=%s, user=%s)",
                state["session_key"], answer, user_name,
            )
        except Exception as exc:
            logger.error("Failed to resolve Feishu update prompt: %s", exc)

    async def _handle_reaction_event(self, event_type: str, data: Any) -> None:
        """Fetch the reacted-to message; if it was sent by this bot, emit a synthetic text event."""
        if not self._client:
            return
        event = getattr(data, "event", None)
        message_id = str(getattr(event, "message_id", "") or "")
        if not message_id:
            return

        # Fetch the target message to verify it was sent by us and to obtain chat context.
        try:
            request = self._build_get_message_request(message_id)
            response = await asyncio.to_thread(self._client.im.v1.message.get, request)
            if not response or not getattr(response, "success", lambda: False)():
                return
            items = getattr(getattr(response, "data", None), "items", None) or []
            msg = items[0] if items else None
            if not msg:
                return
            # GET im/v1/messages returns sender.id=app_id for bot messages —
            # peer bots and us share sender_type="app" but differ on app_id.
            sender = getattr(msg, "sender", None)
            if str(getattr(sender, "id", "") or "") != self._app_id:
                return  # only route reactions on this bot's own messages
            chat_id = str(getattr(msg, "chat_id", "") or "")
            chat_type_raw = str(getattr(msg, "chat_type", "p2p") or "p2p")
            if not chat_id:
                return
        except Exception:
            logger.debug("[Feishu] Failed to fetch message for reaction routing", exc_info=True)
            return

        user_id_obj = getattr(event, "user_id", None)
        reaction_type_obj = getattr(event, "reaction_type", None)
        emoji_type = str(getattr(reaction_type_obj, "emoji_type", "") or "UNKNOWN")
        action = "added" if "created" in event_type else "removed"
        synthetic_text = f"reaction:{action}:{emoji_type}"

        sender_profile = await self._resolve_sender_profile(user_id_obj)
        chat_info = await self.get_chat_info(chat_id)
        source = self.build_source(
            chat_id=chat_id,
            chat_name=chat_info.get("name") or chat_id or "Feishu Chat",
            chat_type=self._resolve_source_chat_type(chat_info=chat_info, event_chat_type=chat_type_raw),
            user_id=sender_profile["user_id"],
            user_name=sender_profile["user_name"],
            thread_id=None,
            user_id_alt=sender_profile["user_id_alt"],
        )
        synthetic_event = MessageEvent(
            text=synthetic_text,
            message_type=MessageType.TEXT,
            source=source,
            raw_message=data,
            message_id=message_id,
            timestamp=datetime.now(),
        )
        logger.info("[Feishu] Routing reaction %s:%s on bot message %s as synthetic event", action, emoji_type, message_id)
        await self._handle_message_with_guards(synthetic_event)

    def _is_card_action_duplicate(self, token: str) -> bool:
        """Return True if this card action token was already processed within the dedup window."""
        now = time.time()
        # Prune expired tokens lazily each call.
        expired = [t for t, ts in self._card_action_tokens.items() if now - ts > _FEISHU_CARD_ACTION_DEDUP_TTL_SECONDS]
        for t in expired:
            del self._card_action_tokens[t]
        if token in self._card_action_tokens:
            return True
        self._card_action_tokens[token] = now
        return False

    async def _handle_card_action_event(self, data: Any) -> None:
        """Route Feishu interactive card button clicks as synthetic COMMAND events."""
        event = getattr(data, "event", None)
        token = str(getattr(event, "token", "") or "")
        if token and self._is_card_action_duplicate(token):
            logger.debug("[Feishu] Dropping duplicate card action token: %s", token)
            return

        context = getattr(event, "context", None)
        chat_id = str(getattr(context, "open_chat_id", "") or "")
        operator = getattr(event, "operator", None)
        open_id = str(getattr(operator, "open_id", "") or "")
        if not chat_id or not open_id:
            logger.debug("[Feishu] Card action missing chat_id or operator open_id, dropping")
            return

        action = getattr(event, "action", None)
        action_tag = str(getattr(action, "tag", "") or "button")
        action_value = getattr(action, "value", {}) or {}

        synthetic_text = f"/card {action_tag}"
        if action_value:
            try:
                synthetic_text += f" {json.dumps(action_value, ensure_ascii=False)}"
            except Exception:
                pass

        sender_id = SimpleNamespace(open_id=open_id, user_id=None, union_id=None)
        sender_profile = await self._resolve_sender_profile(sender_id)
        chat_info = await self.get_chat_info(chat_id)
        source = self.build_source(
            chat_id=chat_id,
            chat_name=chat_info.get("name") or chat_id or "Feishu Chat",
            chat_type=self._resolve_source_chat_type(chat_info=chat_info, event_chat_type="group"),
            user_id=sender_profile["user_id"],
            user_name=sender_profile["user_name"],
            thread_id=None,
            user_id_alt=sender_profile["user_id_alt"],
        )
        synthetic_event = MessageEvent(
            text=synthetic_text,
            message_type=MessageType.COMMAND,
            source=source,
            raw_message=data,
            message_id=token or str(uuid.uuid4()),
            timestamp=datetime.now(),
        )
        logger.info("[Feishu] Routing card action %r from %s in %s as synthetic command", action_tag, open_id, chat_id)
        await self._handle_message_with_guards(synthetic_event)

    # =========================================================================
    # Per-chat serialization and typing indicator
    # =========================================================================

    def _get_chat_lock(self, chat_id: str) -> asyncio.Lock:
        """Return (creating if needed) the per-chat asyncio.Lock for serial message processing."""
        lock = self._chat_locks.get(chat_id)
        if lock is None:
            lock = asyncio.Lock()
            self._chat_locks[chat_id] = lock
        return lock

    async def _handle_message_with_guards(self, event: MessageEvent) -> None:
        """Dispatch a single event through the agent pipeline with per-chat serialization
        before handing the event off to the agent.

        Per-chat lock ensures messages in the same chat are processed one at a
        time (matches openclaw's createChatQueue serial queue behaviour).
        """
        chat_id = getattr(event.source, "chat_id", "") or "" if event.source else ""
        chat_lock = self._get_chat_lock(chat_id)
        async with chat_lock:
            await self.handle_message(event)

    # =========================================================================
    # Processing status reactions
    # =========================================================================

    def _reactions_enabled(self) -> bool:
        return os.getenv("FEISHU_REACTIONS", "true").strip().lower() not in {"false", "0", "no"}

    async def _add_reaction(self, message_id: str, emoji_type: str) -> Optional[str]:
        """Return the reaction_id on success, else None. The id is needed later for deletion."""
        if not self._client or not message_id or not emoji_type:
            return None
        try:
            from lark_oapi.api.im.v1 import (
                CreateMessageReactionRequest,
                CreateMessageReactionRequestBody,
            )
            body = (
                CreateMessageReactionRequestBody.builder()
                .reaction_type({"emoji_type": emoji_type})
                .build()
            )
            request = (
                CreateMessageReactionRequest.builder()
                .message_id(message_id)
                .request_body(body)
                .build()
            )
            response = await asyncio.to_thread(self._client.im.v1.message_reaction.create, request)
            if response and getattr(response, "success", lambda: False)():
                data = getattr(response, "data", None)
                return getattr(data, "reaction_id", None)
            logger.debug(
                "[Feishu] Add reaction %s on %s rejected: code=%s msg=%s",
                emoji_type,
                message_id,
                getattr(response, "code", None),
                getattr(response, "msg", None),
            )
        except Exception:
            logger.warning(
                "[Feishu] Add reaction %s on %s raised",
                emoji_type,
                message_id,
                exc_info=True,
            )
        return None

    async def _remove_reaction(self, message_id: str, reaction_id: str) -> bool:
        if not self._client or not message_id or not reaction_id:
            return False
        try:
            from lark_oapi.api.im.v1 import DeleteMessageReactionRequest
            request = (
                DeleteMessageReactionRequest.builder()
                .message_id(message_id)
                .reaction_id(reaction_id)
                .build()
            )
            response = await asyncio.to_thread(self._client.im.v1.message_reaction.delete, request)
            if response and getattr(response, "success", lambda: False)():
                return True
            logger.debug(
                "[Feishu] Remove reaction %s on %s rejected: code=%s msg=%s",
                reaction_id,
                message_id,
                getattr(response, "code", None),
                getattr(response, "msg", None),
            )
        except Exception:
            logger.warning(
                "[Feishu] Remove reaction %s on %s raised",
                reaction_id,
                message_id,
                exc_info=True,
            )
        return False

    def _remember_processing_reaction(self, message_id: str, reaction_id: str) -> None:
        cache = self._pending_processing_reactions
        cache[message_id] = reaction_id
        cache.move_to_end(message_id)
        while len(cache) > _FEISHU_PROCESSING_REACTION_CACHE_SIZE:
            cache.popitem(last=False)

    def _pop_processing_reaction(self, message_id: str) -> Optional[str]:
        return self._pending_processing_reactions.pop(message_id, None)

    async def on_processing_start(self, event: MessageEvent) -> None:
        if not self._reactions_enabled():
            return
        message_id = event.message_id
        if not message_id or message_id in self._pending_processing_reactions:
            return
        reaction_id = await self._add_reaction(message_id, _FEISHU_REACTION_IN_PROGRESS)
        if reaction_id:
            self._remember_processing_reaction(message_id, reaction_id)

    async def on_processing_complete(
        self, event: MessageEvent, outcome: ProcessingOutcome
    ) -> None:
        if not self._reactions_enabled():
            return
        message_id = event.message_id
        if not message_id:
            return

        start_reaction_id = self._pending_processing_reactions.get(message_id)
        if start_reaction_id:
            if not await self._remove_reaction(message_id, start_reaction_id):
                # Don't stack a second badge on top of a Typing we couldn't
                # remove — UI would read as both "working" and "done/failed"
                # simultaneously. Keep the handle so LRU eventually evicts it.
                return
            self._pop_processing_reaction(message_id)

        if outcome is ProcessingOutcome.FAILURE:
            await self._add_reaction(message_id, _FEISHU_REACTION_FAILURE)

    # =========================================================================
    # Webhook server and security
    # =========================================================================

    async def _process_inbound_message(
        self,
        *,
        data: Any,
        message: Any,
        sender_id: Any,
        chat_type: str,
        message_id: str,
        is_bot: bool = False,
    ) -> None:
        text, inbound_type, media_urls, media_types, mentions = await self._extract_message_content(message)

        if inbound_type == MessageType.TEXT:
            text = _strip_edge_self_mentions(text, mentions)
            if text.startswith("/"):
                inbound_type = MessageType.COMMAND

        # Guard runs post-strip so a pure "@Bot" message (stripped to "") is dropped.
        if inbound_type == MessageType.TEXT and not text and not media_urls:
            logger.debug("[Feishu] Ignoring empty text message id=%s", message_id)
            return

        if inbound_type != MessageType.COMMAND:
            hint = _build_mention_hint(mentions)
            if hint:
                text = f"{hint}\n\n{text}" if text else hint

        thread_id = getattr(message, "thread_id", None) or getattr(message, "root_id", None) or None
        reply_to_message_id = (
            getattr(message, "parent_id", None)
            or getattr(message, "upper_message_id", None)
            or getattr(message, "root_id", None)
            or None
        )
        reply_to_text = await self._fetch_message_text(reply_to_message_id) if reply_to_message_id else None

        sender_primary = (
            getattr(sender_id, "open_id", None)
            or getattr(sender_id, "user_id", None)
            or getattr(sender_id, "union_id", None)
            or "<unknown>"
        )
        logger.info(
            "[Feishu] Inbound %s message received: id=%s type=%s chat_id=%s sender=%s:%s text=%r media=%d",
            "dm" if chat_type == "p2p" else "group",
            message_id,
            inbound_type.value,
            getattr(message, "chat_id", "") or "",
            "bot" if is_bot else "user",
            sender_primary,
            text[:120],
            len(media_urls),
        )

        chat_id = getattr(message, "chat_id", "") or ""
        chat_info = await self.get_chat_info(chat_id)
        sender_profile = await self._resolve_sender_profile(sender_id, is_bot=is_bot)
        source = self.build_source(
            chat_id=chat_id,
            chat_name=chat_info.get("name") or chat_id or "Feishu Chat",
            chat_type=self._resolve_source_chat_type(chat_info=chat_info, event_chat_type=chat_type),
            user_id=sender_profile["user_id"],
            user_name=sender_profile["user_name"],
            thread_id=thread_id,
            user_id_alt=sender_profile["user_id_alt"],
            is_bot=is_bot,
        )
        normalized = MessageEvent(
            text=text,
            message_type=inbound_type,
            source=source,
            raw_message=data,
            message_id=message_id,
            media_urls=media_urls,
            media_types=media_types,
            reply_to_message_id=reply_to_message_id,
            reply_to_text=reply_to_text,
            timestamp=datetime.now(),
        )
        await self._dispatch_inbound_event(normalized)

    async def _dispatch_inbound_event(self, event: MessageEvent) -> None:
        """Apply Feishu-specific burst protection before entering the base adapter."""
        if event.message_type == MessageType.TEXT and not event.is_command():
            await self._enqueue_text_event(event)
            return
        if self._should_batch_media_event(event):
            await self._enqueue_media_event(event)
            return
        await self._handle_message_with_guards(event)

    # =========================================================================
    # Media batching
    # =========================================================================

    def _should_batch_media_event(self, event: MessageEvent) -> bool:
        return bool(
            event.media_urls
            and event.message_type in {MessageType.PHOTO, MessageType.VIDEO, MessageType.DOCUMENT, MessageType.AUDIO}
        )

    def _media_batch_key(self, event: MessageEvent) -> str:
        from channels.session_identity import build_session_key

        session_key = build_session_key(
            event.source,
            group_sessions_per_user=self.config.extra.get("group_sessions_per_user", True),
            thread_sessions_per_user=self.config.extra.get("thread_sessions_per_user", False),
        )
        return f"{session_key}:media:{event.message_type.value}"

    @staticmethod
    def _media_batch_is_compatible(existing: MessageEvent, incoming: MessageEvent) -> bool:
        return (
            existing.message_type == incoming.message_type
            and existing.reply_to_message_id == incoming.reply_to_message_id
            and existing.reply_to_text == incoming.reply_to_text
            and existing.source.thread_id == incoming.source.thread_id
        )

    async def _enqueue_media_event(self, event: MessageEvent) -> None:
        key = self._media_batch_key(event)
        existing = self._pending_media_batches.get(key)
        if existing is None:
            self._pending_media_batches[key] = event
            self._schedule_media_batch_flush(key)
            return
        if not self._media_batch_is_compatible(existing, event):
            await self._flush_media_batch_now(key)
            self._pending_media_batches[key] = event
            self._schedule_media_batch_flush(key)
            return
        existing.media_urls.extend(event.media_urls)
        existing.media_types.extend(event.media_types)
        if event.text:
            existing.text = self._merge_caption(existing.text, event.text)
        existing.timestamp = event.timestamp
        if event.message_id:
            existing.message_id = event.message_id
        self._schedule_media_batch_flush(key)

    def _schedule_media_batch_flush(self, key: str) -> None:
        self._reschedule_batch_task(
            self._pending_media_batch_tasks,
            key,
            self._flush_media_batch,
        )

    async def _flush_media_batch(self, key: str) -> None:
        current_task = asyncio.current_task()
        try:
            await asyncio.sleep(self._media_batch_delay_seconds)
            await self._flush_media_batch_now(key)
        finally:
            if self._pending_media_batch_tasks.get(key) is current_task:
                self._pending_media_batch_tasks.pop(key, None)

    async def _flush_media_batch_now(self, key: str) -> None:
        event = self._pending_media_batches.pop(key, None)
        if not event:
            return
        logger.info(
            "[Feishu] Flushing media batch %s with %d attachment(s)",
            key,
            len(event.media_urls),
        )
        await self._handle_message_with_guards(event)

    # =========================================================================
    # Text batching
    # =========================================================================

    async def _connect_with_retry(self) -> None:
        for attempt in range(_FEISHU_CONNECT_ATTEMPTS):
            try:
                if self._connection_mode == "websocket":
                    await self._connect_websocket()
                else:
                    await self._connect_webhook()
                return
            except Exception as exc:
                self._running = False
                self._disable_websocket_auto_reconnect()
                self._ws_future = None
                await self._stop_webhook_server()
                if attempt >= _FEISHU_CONNECT_ATTEMPTS - 1:
                    raise
                wait_seconds = 2 ** attempt
                logger.warning(
                    "[Feishu] Connect attempt %d/%d failed; retrying in %ds: %s",
                    attempt + 1,
                    _FEISHU_CONNECT_ATTEMPTS,
                    wait_seconds,
                    exc,
                )
                await asyncio.sleep(wait_seconds)

    async def _connect_websocket(self) -> None:
        if not FEISHU_WEBSOCKET_AVAILABLE:
            raise RuntimeError("websockets not installed; websocket mode unavailable")
        domain = FEISHU_DOMAIN if self._domain_name != "lark" else LARK_DOMAIN
        self._client = self._build_lark_client(domain)
        self._event_handler = self._build_event_handler()
        if self._event_handler is None:
            raise RuntimeError("failed to build Feishu event handler")
        loop = self._loop
        if loop is None or loop.is_closed():
            raise RuntimeError("adapter loop is not ready")
        await self._hydrate_bot_identity()
        self._ws_client = FeishuWSClient(
            app_id=self._app_id,
            app_secret=self._app_secret,
            log_level=lark.LogLevel.INFO,
            event_handler=self._event_handler,
            domain=domain,
        )
        self._ws_future = loop.run_in_executor(
            None,
            _run_official_feishu_ws_client,
            self._ws_client,
            self,
        )

    async def _connect_webhook(self) -> None:
        if not FEISHU_WEBHOOK_AVAILABLE:
            raise RuntimeError("aiohttp not installed; webhook mode unavailable")
        domain = FEISHU_DOMAIN if self._domain_name != "lark" else LARK_DOMAIN
        self._client = self._build_lark_client(domain)
        self._event_handler = self._build_event_handler()
        if self._event_handler is None:
            raise RuntimeError("failed to build Feishu event handler")
        await self._hydrate_bot_identity()
        app = web.Application()
        app.router.add_post(self._webhook_path, self._handle_webhook_request)
        self._webhook_runner = web.AppRunner(app)
        await self._webhook_runner.setup()
        self._webhook_site = web.TCPSite(self._webhook_runner, self._webhook_host, self._webhook_port)
        await self._webhook_site.start()

    def _build_lark_client(self, domain: Any) -> Any:
        return (
            lark.Client.builder()
            .app_id(self._app_id)
            .app_secret(self._app_secret)
            .domain(domain)
            .log_level(lark.LogLevel.WARNING)
            .build()
        )

    async def _release_app_lock(self) -> None:
        if not self._app_lock_identity:
            return
        try:
            release_scoped_lock(_FEISHU_APP_LOCK_SCOPE, self._app_lock_identity)
        except Exception as exc:
            logger.warning("[Feishu] Failed to release app lock: %s", exc, exc_info=True)
        finally:
            self._app_lock_identity = None

    # =========================================================================
    # Lark API request builders
    # =========================================================================

# =============================================================================
# QR scan-to-create onboarding
#
# Device-code flow: user scans a QR code with Feishu/Lark mobile app and the
# platform creates a fully configured bot application automatically.
# Called by `hermes gateway setup` via _setup_feishu() in hermes_cli/gateway.py.
# =============================================================================

try:
    import qrcode as _qrcode_mod
except (ImportError, TypeError):
    _qrcode_mod = None  # type: ignore[assignment]


def _accounts_base_url(domain: str) -> str:
    return _ONBOARD_ACCOUNTS_URLS.get(domain, _ONBOARD_ACCOUNTS_URLS["feishu"])


def _onboard_open_base_url(domain: str) -> str:
    return _ONBOARD_OPEN_URLS.get(domain, _ONBOARD_OPEN_URLS["feishu"])


def _post_registration(base_url: str, body: Dict[str, str]) -> dict:
    url = f"{base_url}{_REGISTRATION_PATH}"
    data = urlencode(body).encode("utf-8")
    req = Request(url, data=data, headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urlopen(req, timeout=_ONBOARD_REQUEST_TIMEOUT_S) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except HTTPError as exc:
        body_bytes = exc.read()
        if body_bytes:
            try:
                return json.loads(body_bytes.decode("utf-8"))
            except (ValueError, json.JSONDecodeError):
                raise exc from None
        raise


def _init_registration(domain: str = "feishu") -> None:
    base_url = _accounts_base_url(domain)
    res = _post_registration(base_url, {"action": "init"})
    methods = res.get("supported_auth_methods") or []
    if "client_secret" not in methods:
        raise RuntimeError(
            f"Feishu / Lark registration environment does not support client_secret auth. "
            f"Supported: {methods}"
        )


def _begin_registration(domain: str = "feishu") -> dict:
    base_url = _accounts_base_url(domain)
    res = _post_registration(base_url, {
        "action": "begin",
        "archetype": "PersonalAgent",
        "auth_method": "client_secret",
        "request_user_info": "open_id",
    })
    device_code = res.get("device_code")
    if not device_code:
        raise RuntimeError("Feishu / Lark registration did not return a device_code")
    qr_url = res.get("verification_uri_complete", "")
    qr_url += "&from=hermes&tp=hermes" if "?" in qr_url else "?from=hermes&tp=hermes"
    return {
        "device_code": device_code,
        "qr_url": qr_url,
        "user_code": res.get("user_code", ""),
        "interval": res.get("interval") or 5,
        "expire_in": res.get("expire_in") or 600,
    }


def _poll_registration(
    *,
    device_code: str,
    interval: int,
    expire_in: int,
    domain: str = "feishu",
) -> Optional[dict]:
    deadline = time.monotonic() + expire_in
    current_domain = domain
    domain_switched = False
    poll_count = 0

    while time.monotonic() < deadline:
        base_url = _accounts_base_url(current_domain)
        try:
            res = _post_registration(base_url, {
                "action": "poll",
                "device_code": device_code,
                "tp": "ob_app",
            })
        except (URLError, OSError, json.JSONDecodeError):
            time.sleep(interval)
            continue

        poll_count += 1
        if poll_count == 1:
            print("  Fetching configuration results...", end="", flush=True)
        elif poll_count % 6 == 0:
            print(".", end="", flush=True)

        user_info = res.get("user_info") or {}
        tenant_brand = user_info.get("tenant_brand")
        if tenant_brand == "lark" and not domain_switched:
            current_domain = "lark"
            domain_switched = True

        if res.get("client_id") and res.get("client_secret"):
            if poll_count > 0:
                print()
            return {
                "app_id": res["client_id"],
                "app_secret": res["client_secret"],
                "domain": current_domain,
                "open_id": user_info.get("open_id"),
            }

        error = res.get("error", "")
        if error in {"access_denied", "expired_token"}:
            if poll_count > 0:
                print()
            logger.warning("[Feishu onboard] Registration %s", error)
            return None

        time.sleep(interval)

    if poll_count > 0:
        print()
    logger.warning("[Feishu onboard] Poll timed out after %ds", expire_in)
    return None


def _render_qr(url: str) -> bool:
    if _qrcode_mod is None:
        return False
    try:
        qr = _qrcode_mod.QRCode()
        qr.add_data(url)
        qr.make(fit=True)
        qr.print_ascii(invert=True)
        return True
    except Exception:
        return False


def probe_bot(app_id: str, app_secret: str, domain: str) -> Optional[dict]:
    if FEISHU_AVAILABLE:
        return _probe_bot_sdk(app_id, app_secret, domain)
    return _probe_bot_http(app_id, app_secret, domain)


def _build_onboard_client(app_id: str, app_secret: str, domain: str) -> Any:
    sdk_domain = LARK_DOMAIN if domain == "lark" else FEISHU_DOMAIN
    return (
        lark.Client.builder()
        .app_id(app_id)
        .app_secret(app_secret)
        .domain(sdk_domain)
        .log_level(lark.LogLevel.WARNING)
        .build()
    )


def _parse_bot_response(data: dict) -> Optional[dict]:
    if data.get("code") != 0:
        return None
    bot = data.get("bot") or data.get("data", {}).get("bot") or {}
    return {
        "bot_name": bot.get("app_name") or bot.get("bot_name"),
        "bot_open_id": bot.get("open_id"),
    }


def _probe_bot_sdk(app_id: str, app_secret: str, domain: str) -> Optional[dict]:
    try:
        client = _build_onboard_client(app_id, app_secret, domain)
        req = (
            BaseRequest.builder()
            .http_method(HttpMethod.GET)
            .uri("/open-apis/bot/v3/info")
            .token_types({AccessTokenType.TENANT})
            .build()
        )
        resp = client.request(req)
        content = getattr(getattr(resp, "raw", None), "content", None)
        if content is None:
            return None
        return _parse_bot_response(json.loads(content))
    except Exception as exc:
        logger.debug("[Feishu onboard] SDK probe failed: %s", exc)
        return None


def _probe_bot_http(app_id: str, app_secret: str, domain: str) -> Optional[dict]:
    base_url = _onboard_open_base_url(domain)
    try:
        token_data = json.dumps({"app_id": app_id, "app_secret": app_secret}).encode("utf-8")
        token_req = Request(
            f"{base_url}/open-apis/auth/v3/tenant_access_token/internal",
            data=token_data,
            headers={"Content-Type": "application/json"},
        )
        with urlopen(token_req, timeout=_ONBOARD_REQUEST_TIMEOUT_S) as resp:
            token_res = json.loads(resp.read().decode("utf-8"))

        access_token = token_res.get("tenant_access_token")
        if not access_token:
            return None

        bot_req = Request(
            f"{base_url}/open-apis/bot/v3/info",
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
            },
        )
        with urlopen(bot_req, timeout=_ONBOARD_REQUEST_TIMEOUT_S) as resp:
            bot_res = json.loads(resp.read().decode("utf-8"))

        return _parse_bot_response(bot_res)
    except (URLError, OSError, KeyError, json.JSONDecodeError) as exc:
        logger.debug("[Feishu onboard] HTTP probe failed: %s", exc)
        return None


def qr_register(
    *,
    initial_domain: str = "feishu",
    timeout_seconds: int = 600,
) -> Optional[dict]:
    try:
        return _qr_register_inner(initial_domain=initial_domain, timeout_seconds=timeout_seconds)
    except (RuntimeError, URLError, OSError, json.JSONDecodeError) as exc:
        logger.warning("[Feishu onboard] Registration failed: %s", exc)
        return None


def _qr_register_inner(
    *,
    initial_domain: str,
    timeout_seconds: int,
) -> Optional[dict]:
    print("  Connecting to Feishu / Lark...", end="", flush=True)
    _init_registration(initial_domain)
    begin = _begin_registration(initial_domain)
    print(" done.")

    print()
    qr_url = begin["qr_url"]
    if _render_qr(qr_url):
        print(f"\n  Scan the QR code above, or open this URL directly:\n  {qr_url}")
    else:
        print(f"  Open this URL in Feishu / Lark on your phone:\n\n  {qr_url}\n")
        print("  Tip: pip install qrcode  to display a scannable QR code here next time")
    print()

    result = _poll_registration(
        device_code=begin["device_code"],
        interval=begin["interval"],
        expire_in=min(begin["expire_in"], timeout_seconds),
        domain=initial_domain,
    )
    if not result:
        return None

    bot_info = probe_bot(result["app_id"], result["app_secret"], result["domain"])
    result["bot_name"] = bot_info.get("bot_name") if bot_info else None
    result["bot_open_id"] = bot_info.get("bot_open_id") if bot_info else None
    return result
