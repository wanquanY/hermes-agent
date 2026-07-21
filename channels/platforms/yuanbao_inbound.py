"""Yuanbao inbound middleware pipeline."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import urllib.parse
from abc import ABC, abstractmethod
from dataclasses import dataclass, field as dc_field
from typing import Any, Callable, Dict, List, Optional, Tuple

from agent.secret_scope import get_profile_env
import httpx

from channels.platforms.base import (
    MessageEvent,
    MessageType,
    cache_document_from_bytes,
    cache_image_from_bytes,
)
from channels.platforms.yuanbao_auth import SignManager
from channels.platforms.yuanbao_constants import (
    OBSERVED_MEDIA_BACKFILL_LOOKBACK,
    OBSERVED_MEDIA_BACKFILL_MAX_RESOLVE_PER_TURN,
    _RESOLVABLE_MEDIA_KINDS,
    _YB_RES_REF_RE,
)
from channels.platforms.yuanbao_media import download_url as media_download_url, guess_mime_type
from channels.platforms.yuanbao_proto import decode_inbound_push
from channels.session_identity import build_session_key

logger = logging.getLogger(__name__)

@dataclass
class InboundContext:
    """Mutable context flowing through the inbound middleware pipeline.

    Each middleware reads/writes fields on this context.  The pipeline
    engine passes it to every middleware in registration order.
    """

    adapter: Any  # YuanbaoAdapter (forward-ref avoids circular import)
    raw_frames: list = dc_field(default_factory=list)  # Raw bytes frames (debounce-aggregated)

    # Populated by DecodeMiddleware
    push: Optional[dict] = None
    decoded_via: str = ""  # "json" | "protobuf"

    # Extracted from push by FieldExtractMiddleware
    from_account: str = ""
    group_code: str = ""
    group_name: str = ""
    sender_nickname: str = ""
    msg_body: list = dc_field(default_factory=list)
    msg_id: str = ""
    cloud_custom_data: str = ""

    # Derived by ChatRoutingMiddleware
    chat_id: str = ""
    chat_type: str = ""  # "dm" | "group"
    chat_name: str = ""

    # Populated by ContentExtractMiddleware
    raw_text: str = ""
    media_refs: list = dc_field(default_factory=list)

    # Owner command detection
    owner_command: Optional[str] = None

    # Source built by BuildSourceMiddleware
    source: Optional[Any] = None  # SessionSource

    # Populated by ClassifyMessageTypeMiddleware
    msg_type: Optional[Any] = None  # MessageType

    # Populated by QuoteContextMiddleware
    reply_to_message_id: Optional[str] = None
    reply_to_text: Optional[str] = None
    quote_media_refs: list = dc_field(default_factory=list)  # List of (rid, kind, filename)

    # Populated by MediaResolveMiddleware
    media_urls: list = dc_field(default_factory=list)
    media_types: list = dc_field(default_factory=list)

    # Populated by ExtractContentMiddleware
    link_urls: list = dc_field(default_factory=list)

    # Populated by GroupAttributionMiddleware
    channel_prompt: Optional[str] = None


class InboundMiddleware(ABC):
    """Abstract base class for all inbound pipeline middlewares.

    Subclasses must:
      - Set ``name`` as a class-level attribute (used for pipeline registration
        and dynamic insertion/removal).
      - Implement ``async handle(ctx, next_fn)`` containing the middleware logic.

    Convention:
      - Call ``await next_fn()`` to pass control to the next middleware.
      - Return without calling ``next_fn`` to **stop** the pipeline.
    """

    name: str = ""  # Override in each subclass

    @abstractmethod
    async def handle(self, ctx: InboundContext, next_fn: Callable) -> None:
        """Process *ctx* and optionally call *next_fn* to continue the pipeline."""

    async def __call__(self, ctx: InboundContext, next_fn: Callable) -> None:
        """Allow middleware instances to be called directly (duck-typing compat)."""
        return await self.handle(ctx, next_fn)

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__} name={self.name!r}>"


class InboundPipeline:
    """Onion-model middleware pipeline engine for inbound message processing.

    Inspired by OpenClaw's MessagePipeline (extensions/yuanbao/src/business/
    pipeline/engine.ts).  Supports named middlewares, conditional guards
    (``when``), and ``use_before`` / ``use_after`` / ``remove`` for dynamic
    composition.

    Accepts both ``InboundMiddleware`` instances (OOP style) and plain
    ``async def(ctx, next_fn)`` callables (functional style) for flexibility.
    """

    def __init__(self) -> None:
        self._middlewares: list = []  # list of (name, handler, when_fn | None)

    # -- Internal helpers --------------------------------------------------

    @staticmethod
    def _normalize(name_or_mw, handler=None):
        """Normalize (name, handler) or (InboundMiddleware,) into (name, callable)."""
        if isinstance(name_or_mw, InboundMiddleware):
            return name_or_mw.name, name_or_mw
        # Functional style: name is a str, handler is a callable
        return name_or_mw, handler

    # -- Registration API --------------------------------------------------

    def use(self, name_or_mw, handler=None, when=None) -> "InboundPipeline":
        """Append a middleware to the end of the pipeline.

        Accepts either:
          - ``pipeline.use(SomeMiddleware())``  — OOP style
          - ``pipeline.use("name", some_fn)``   — functional style
        """
        name, h = self._normalize(name_or_mw, handler)
        self._middlewares.append((name, h, when))
        return self

    def use_before(self, target: str, name_or_mw, handler=None, when=None) -> "InboundPipeline":
        """Insert a middleware before *target* (by name).  Appends if not found."""
        name, h = self._normalize(name_or_mw, handler)
        idx = next((i for i, (n, _, _) in enumerate(self._middlewares) if n == target), None)
        entry = (name, h, when)
        if idx is None:
            self._middlewares.append(entry)
        else:
            self._middlewares.insert(idx, entry)
        return self

    def use_after(self, target: str, name_or_mw, handler=None, when=None) -> "InboundPipeline":
        """Insert a middleware after *target* (by name).  Appends if not found."""
        name, h = self._normalize(name_or_mw, handler)
        idx = next((i for i, (n, _, _) in enumerate(self._middlewares) if n == target), None)
        entry = (name, h, when)
        if idx is None:
            self._middlewares.append(entry)
        else:
            self._middlewares.insert(idx + 1, entry)
        return self

    def remove(self, name: str) -> "InboundPipeline":
        """Remove a middleware by name."""
        self._middlewares = [(n, h, w) for n, h, w in self._middlewares if n != name]
        return self

    @property
    def middleware_names(self) -> list:
        """Return ordered list of registered middleware names (for testing)."""
        return [n for n, _, _ in self._middlewares]

    # -- Execution ---------------------------------------------------------

    async def execute(self, ctx: InboundContext) -> None:
        """Run all middlewares in order.  Each middleware receives ``(ctx, next_fn)``."""
        chain = self._middlewares
        index = 0

        async def next_fn() -> None:
            nonlocal index
            while index < len(chain):
                name, handler, when_fn = chain[index]
                index += 1
                # Conditional guard: skip when returns False
                if when_fn is not None and not when_fn(ctx):
                    continue
                try:
                    await handler(ctx, next_fn)
                except Exception:
                    logger.error("[InboundPipeline] middleware [%s] error", name, exc_info=True)
                    raise
                return
            # End of chain — nothing more to do

        await next_fn()
class DecodeMiddleware(InboundMiddleware):
    """Decode raw inbound frames from JSON or Protobuf into ctx.push.

    Encapsulates JSON push parsing (aligned with TS decodeFromContent)
    and Protobuf decoding via ``decode_inbound_push``.
    """

    name = "decode"

    # -- JSON push parsing -------------------------------------------------

    @staticmethod
    def convert_json_msg_body(raw_body: list) -> list:
        """Normalize raw JSON msg_body array to [{"msg_type": str, "msg_content": dict}].

        Compatible with both PascalCase (MsgType/MsgContent) and
        snake_case (msg_type/msg_content) naming.
        """
        result = []
        for item in raw_body or []:
            if not isinstance(item, dict):
                continue
            msg_type = item.get("msg_type") or item.get("MsgType", "")
            msg_content = item.get("msg_content") or item.get("MsgContent", {})
            if isinstance(msg_content, str):
                try:
                    msg_content = json.loads(msg_content)
                except Exception:
                    msg_content = {"text": msg_content}
            result.append({"msg_type": msg_type, "msg_content": msg_content or {}})
        return result

    @staticmethod
    def parse_json_push(raw_json: dict) -> dict | None:
        """Convert JSON-format push to a dict with the same structure as
        ``decode_inbound_push``.

        Supports standard callback format (callback_command + from_account +
        msg_body) and legacy format fields (GroupId, MsgSeq, MsgKey, MsgBody,
        etc.).
        """
        if not raw_json:
            return None

        # Tencent IM callback format uses PascalCase (From_Account, To_Account, MsgBody).
        # Internal format uses snake_case (from_account, to_account, msg_body).
        # Support both.
        from_account = (
            raw_json.get("from_account", "")
            or raw_json.get("From_Account", "")
        )
        group_code = (
            raw_json.get("group_code", "")
            or raw_json.get("GroupId", "")
            or raw_json.get("group_id", "")
        )
        msg_body_raw = (
            raw_json.get("msg_body", [])
            or raw_json.get("MsgBody", [])
        )
        msg_body = DecodeMiddleware.convert_json_msg_body(msg_body_raw)

        # Recall callbacks may have neither from_account nor msg_body.
        if not from_account and not msg_body and not raw_json.get("callback_command"):
            return None

        return {
            "callback_command": raw_json.get("callback_command", ""),
            "from_account": from_account,
            "to_account": raw_json.get("to_account", "") or raw_json.get("To_Account", ""),
            "sender_nickname": raw_json.get("sender_nickname", "") or raw_json.get("nick_name", ""),
            "group_code": group_code,
            "group_name": raw_json.get("group_name", ""),
            "msg_seq": raw_json.get("msg_seq", 0) or raw_json.get("MsgSeq", 0),
            "msg_id": raw_json.get("msg_id", "") or raw_json.get("msg_key", "") or raw_json.get("MsgKey", ""),
            "msg_body": msg_body,
            "cloud_custom_data": raw_json.get("cloud_custom_data", "") or raw_json.get("CloudCustomData", ""),
            "bot_owner_id": raw_json.get("bot_owner_id", "") or raw_json.get("botOwnerId", ""),
            "recall_msg_seq_list": raw_json.get("recall_msg_seq_list") or None,
            "trace_id": (raw_json.get("log_ext") or {}).get("trace_id", "") if isinstance(raw_json.get("log_ext"), dict) else "",
        }

    # -- Pipeline handler --------------------------------------------------

    def _decode_single(self, adapter, data: bytes) -> tuple:
        """Decode a single raw frame into (push_dict, decoded_via) or (None, '')."""
        try:
            conn_json = json.loads(data.decode("utf-8"))
        except Exception:
            conn_json = None

        if isinstance(conn_json, dict):
            push = self.parse_json_push(conn_json)
            if push:
                return push, "json"
        else:
            try:
                push = decode_inbound_push(data)
            except Exception:
                push = None
            if push:
                return push, "protobuf"

        return None, ""

    async def handle(self, ctx: InboundContext, next_fn) -> None:
        data_list = ctx.raw_frames
        if not data_list:
            return  # Stop pipeline — nothing to decode

        merged_push = None
        decoded_via = ""

        for data in data_list:
            push, via = self._decode_single(ctx.adapter, data)
            if not push:
                logger.info(
                "[%s] Push decoded but no valid message. raw hex(first64)=%s",
                    ctx.adapter.name, data.hex()[:128] if data else "(empty)",
                )
                continue

            if merged_push is None:
                # First valid push becomes the base
                merged_push = push
                decoded_via = via
                logger.info(
                "[%s] Frame decoded (via=%s): len=%d",
                    ctx.adapter.name, via, len(data),
                )
            else:
                # Subsequent pushes: merge msg_body into the base with a
                extra_body = push.get("msg_body", [])
                if extra_body:
                    _sep = {"msg_type": "TIMTextElem", "msg_content": {"text": "\n"}}
                    merged_push["msg_body"] = merged_push.get("msg_body", []) + [_sep] + extra_body
                    logger.info(
                        "[%s] Merged %d extra msg_body elements from aggregated push",
                        ctx.adapter.name, len(extra_body),
                    )

        if not merged_push:
            return  # Stop pipeline

        ctx.push = merged_push
        ctx.decoded_via = decoded_via

        logger.info(
            "[%s] Push decoded (via=%s): from=%s group=%s msg_id=%s msg_types=%s",
            ctx.adapter.name, ctx.decoded_via,
            ctx.push.get("from_account", ""),
            ctx.push.get("group_code", ""),
            ctx.push.get("msg_id", ""),
            [e.get("msg_type", "") for e in ctx.push.get("msg_body", [])],
        )
        logger.debug("[%s] Push payload: %s", ctx.adapter.name, ctx.push)

        await next_fn()


class ExtractFieldsMiddleware(InboundMiddleware):
    """Extract common fields from ctx.push into ctx attributes."""

    name = "extract-fields"

    async def handle(self, ctx: InboundContext, next_fn) -> None:
        push = ctx.push
        ctx.from_account = push.get("from_account", "")
        ctx.group_code = push.get("group_code", "")
        ctx.group_name = push.get("group_name", "")
        ctx.sender_nickname = push.get("sender_nickname", "")
        ctx.msg_body = push.get("msg_body", [])
        ctx.msg_id = push.get("msg_id", "")
        ctx.cloud_custom_data = push.get("cloud_custom_data", "")
        await next_fn()


class DedupMiddleware(InboundMiddleware):
    """Inbound message deduplication."""

    name = "dedup"

    async def handle(self, ctx: InboundContext, next_fn) -> None:
        if ctx.msg_id and ctx.adapter._dedup.is_duplicate(ctx.msg_id):
            logger.debug("[%s] Duplicate message ignored: msg_id=%s", ctx.adapter.name, ctx.msg_id)
            return  # Stop pipeline
        await next_fn()


class RecallGuardMiddleware(InboundMiddleware):
    """Intercept Group.CallbackAfterRecallMsg / C2C.CallbackAfterMsgWithDraw.

    Branch A: message in transcript (observed, not yet consumed) → redact content
    Branch B: message not in transcript → append system note
    Branch C: message currently being processed → silent interrupt + delayed redact
    """

    name = "recall_guard"

    _RECALL_COMMANDS = frozenset({
        "Group.CallbackAfterRecallMsg",
        "C2C.CallbackAfterMsgWithDraw",
    })
    _REDACTED = "[This message was recalled/withdrawn by the sender; original content removed]"

    async def handle(self, ctx: InboundContext, next_fn) -> None:
        cmd = (ctx.push or {}).get("callback_command", "")
        if cmd not in self._RECALL_COMMANDS:
            await next_fn()
            return
        self._handle_recall(ctx, cmd)

    @staticmethod
    def _build_source(adapter, group_code: str, from_account: str):
        return adapter.build_source(
            chat_id=(f"group:{group_code}" if group_code else f"direct:{from_account}"),
            chat_type="group" if group_code else "dm",
            user_id=from_account or None,
            thread_id="main" if group_code else None,
        )

    def _handle_recall(self, ctx: InboundContext, cmd: str) -> None:
        adapter = ctx.adapter
        push = ctx.push or {}

        if cmd == "Group.CallbackAfterRecallMsg":
            seq_list = push.get("recall_msg_seq_list") or []
        else:
            mid = push.get("msg_id") or ""
            seq = push.get("msg_seq")
            seq_list = [{"msg_id": mid, "msg_seq": seq}] if (mid or seq) else []

        if not seq_list:
            logger.debug("[%s] Recall callback with empty seq_list, skipping", adapter.name)
            return

        group_code = (push.get("group_code") or "").strip()
        from_account = (push.get("from_account") or "").strip()

        for seq_entry in seq_list:
            recalled_id = seq_entry.get("msg_id") or str(seq_entry.get("msg_seq") or "")
            if not recalled_id:
                continue

            matched_sk = self._find_processing_session(adapter, recalled_id)
            if matched_sk is not None:
                self._interrupt_for_recall(adapter, matched_sk, recalled_id, group_code, from_account)
            else:
                recalled_content = adapter._msg_content_cache.get(recalled_id)
                self._patch_transcript(adapter, recalled_id, group_code, from_account, recalled_content)

    # -- Branch C: interrupt currently-processing message ---------------

    @staticmethod
    def _find_processing_session(adapter, recalled_id: str) -> Optional[str]:
        for sk, mid in adapter._processing_msg_ids.items():
            if mid == recalled_id and sk in adapter._active_sessions:
                return sk
        return None

    @classmethod
    def _interrupt_for_recall(cls, adapter, session_key: str, recalled_id: str,
                              group_code: str, from_account: str) -> None:
        where = f"group {group_code}" if group_code else f"direct chat with {from_account}"
        recall_text = (
            f"[CRITICAL — MESSAGE RECALLED] The user message that triggered "
            f"your current task (message_id=\"{recalled_id}\") in {where} has "
            f"been recalled/withdrawn by the sender. "
            f"IGNORE any prior system note asking you to finish processing "
            f"tool results — the original request is void. "
            f"Do NOT continue the task, do NOT call more tools, do NOT "
            f"reference the recalled content. "
            f"Reply only with a brief acknowledgment such as "
            f"\"The message has been recalled.\" in the "
            f"language the user was using."
        )

        synth_event = MessageEvent(
            text=recall_text,
            message_type=MessageType.TEXT,
            source=cls._build_source(adapter, group_code, from_account),
            internal=True,
        )
        # Set pending + signal directly (bypass handle_message to avoid busy-ack).
        # May overwrite a user message pending in the same ~200ms window — acceptable.
        adapter._pending_messages[session_key] = synth_event
        active_event = adapter._active_sessions.get(session_key)
        if active_event is not None:
            active_event.set()

        logger.info("[%s] Recall interrupt: msg_id=%s session=%s", adapter.name, recalled_id, session_key[:30])

        # The interrupted turn will persist the recalled content *after* our
        # interrupt — schedule a delayed redaction to clean it up.
        recalled_text = adapter._processing_msg_texts.get(session_key, "")
        if recalled_text:
            cls._schedule_content_redact(adapter, session_key, recalled_text, group_code, from_account)

    @classmethod
    def _schedule_content_redact(cls, adapter, session_key: str, recalled_text: str,
                                 group_code: str, from_account: str) -> None:
        async def _redact() -> None:
            store = getattr(adapter, "_session_store", None)
            if not store:
                return
            try:
                sid = store.get_or_create_session(
                    cls._build_source(adapter, group_code, from_account),
                ).session_id
            except Exception:
                return
            # Poll until the recalled content appears in transcript — the
            # interrupted turn hasn't finished writing yet when scheduled.
            for _ in range(30):
                await asyncio.sleep(0.5)
                try:
                    transcript = store.load_transcript(sid)
                except Exception:
                    continue
                for entry in transcript:
                    if entry.get("role") == "user" and entry.get("content") == recalled_text:
                        entry["content"] = cls._REDACTED
                        try:
                            store.rewrite_transcript(sid, transcript)
                            logger.info("[%s] Recall redact: session %s", adapter.name, session_key[:30])
                        except Exception as exc:
                            logger.warning("[%s] Recall redact failed: %s", adapter.name, exc)
                        return
            logger.debug("[%s] Recall redact: content not found after polling, session %s", adapter.name, session_key[:30])

        task = asyncio.create_task(_redact())
        adapter._background_tasks.add(task)
        task.add_done_callback(adapter._background_tasks.discard)

    # -- Branch A/B: patch transcript (session idle) --------------------

    @classmethod
    def _patch_transcript(cls, adapter, recalled_id: str, group_code: str,
                          from_account: str, recalled_content: Optional[str] = None) -> None:
        store = getattr(adapter, "_session_store", None)
        if not store:
            return
        try:
            sid = store.get_or_create_session(cls._build_source(adapter, group_code, from_account)).session_id
        except Exception as exc:
            logger.warning("[%s] Recall: failed to resolve session: %s", adapter.name, exc)
            return

        # Load transcript from canonical store (state.db).  Since PR #29278
        # added a ``platform_message_id`` column to the messages table and
        # ``append_to_transcript`` wires the incoming dict's ``message_id``
        # into it, ``load_transcript`` returns rows with ``message_id`` set
        # for any message that was observed with one — Branch A1 (exact id
        # match) is the canonical path again.
        try:
            transcript = store.load_transcript(sid)
        except Exception as exc:
            logger.warning("[%s] Recall: failed to load transcript: %s", adapter.name, exc)
            return

        # Branch A1: exact platform message_id match. Authoritative when the
        # row was persisted with a platform_message_id (observed group
        # messages and any inbound message whose adapter carried a msg_id).
        target = None
        branch_label = ""
        for entry in transcript:
            if entry.get("message_id") == recalled_id:
                target = entry
                branch_label = "branch A1: id match"
                break
        # Branch A2: content-match fallback for messages that lack an exact
        # platform id on the row — e.g. agent-processed @bot messages
        # (run.py doesn't carry msg_id through) or older rows persisted
        # before the platform_message_id column existed.
        if target is None and recalled_content:
            for entry in transcript:
                if entry.get("role") == "user" and entry.get("content") == recalled_content:
                    target = entry
                    branch_label = "branch A2: content match"
                    break
        if target is not None:
            target["content"] = cls._REDACTED
            try:
                store.rewrite_transcript(sid, transcript)
                logger.info("[%s] Recall: redacted msg_id=%s (%s)", adapter.name, recalled_id, branch_label)
            except Exception as exc:
                logger.warning("[%s] Recall: rewrite_transcript failed: %s", adapter.name, exc)
            return

        # Branch B: not found in transcript → append system note
        store.append_to_transcript(sid, {
            "role": "system",
            "content": f'[recall] message_id="{recalled_id}" has been recalled; do not quote or reference it.',
            "timestamp": datetime.now(tz=timezone.utc).isoformat(),
        })
        logger.info("[%s] Recall: system note for msg_id=%s (branch B)", adapter.name, recalled_id)


class SkipSelfMiddleware(InboundMiddleware):
    """Filter out bot's own messages."""

    name = "skip-self"

    @staticmethod
    def _is_self_reference(from_account: str, bot_id: Optional[str]) -> bool:
        """Detect whether the message is from the bot itself."""
        if not from_account or not bot_id:
            return False
        return from_account == bot_id

    async def handle(self, ctx: InboundContext, next_fn) -> None:
        if self._is_self_reference(ctx.from_account, ctx.adapter._bot_id):
            logger.debug("[%s] Ignoring self-sent message from %s", ctx.adapter.name, ctx.from_account)
            return  # Stop pipeline
        await next_fn()


class ChatRoutingMiddleware(InboundMiddleware):
    """Determine chat_id, chat_type, chat_name from push fields."""

    name = "chat-routing"

    async def handle(self, ctx: InboundContext, next_fn) -> None:
        if ctx.group_code:
            ctx.chat_id = f"group:{ctx.group_code}"
            ctx.chat_type = "group"
            ctx.chat_name = ctx.group_name or ctx.group_code
        else:
            ctx.chat_id = f"direct:{ctx.from_account}"
            ctx.chat_type = "dm"
            ctx.chat_name = ctx.sender_nickname or ctx.from_account
        await next_fn()


class AccessPolicy:
    """Platform-level DM / Group access control policy.

    Encapsulates the allow/deny logic so that both inbound middleware
    and outbound ``send_dm`` can share the same rules without reaching
    into adapter internals.
    """

    def __init__(
        self,
        dm_policy: str,
        dm_allow_from: list[str],
        group_policy: str,
        group_allow_from: list[str],
    ) -> None:
        self._dm_policy = dm_policy
        self._dm_allow_from = dm_allow_from
        self._group_policy = group_policy
        self._group_allow_from = group_allow_from

    def is_dm_allowed(self, sender_id: str) -> bool:
        """Platform-level DM inbound filter (open / allowlist / disabled)."""
        if self._dm_policy == "disabled":
            return False
        if self._dm_policy == "allowlist":
            return sender_id.strip() in self._dm_allow_from
        return True

    def is_group_allowed(self, group_code: str) -> bool:
        """Platform-level group chat inbound filter (open / allowlist / disabled)."""
        if self._group_policy == "disabled":
            return False
        if self._group_policy == "allowlist":
            return group_code.strip() in self._group_allow_from
        return True

    @property
    def dm_policy(self) -> str:
        return self._dm_policy

    @property
    def group_policy(self) -> str:
        return self._group_policy


class AccessGuardMiddleware(InboundMiddleware):
    """Platform-level DM/Group access control filter."""

    name = "access-guard"

    async def handle(self, ctx: InboundContext, next_fn) -> None:
        adapter = ctx.adapter
        policy: AccessPolicy = adapter._access_policy
        if ctx.chat_type == "dm":
            if not policy.is_dm_allowed(ctx.from_account):
                logger.debug(
                    "[%s] DM from %s blocked by dm_policy=%s",
                    adapter.name, ctx.from_account, policy.dm_policy,
                )
                return  # Stop pipeline
        elif ctx.chat_type == "group":
            if not policy.is_group_allowed(ctx.group_code):
                logger.debug(
                    "[%s] Group %s blocked by group_policy=%s",
                    adapter.name, ctx.group_code, policy.group_policy,
                )
                return  # Stop pipeline
        await next_fn()


class AutoSetHomeMiddleware(InboundMiddleware):
    """Auto-designate the first inbound conversation as Yuanbao home channel.

    Triggers when no home channel is configured, or when an existing group-chat
    home is superseded by the first DM (direct > group upgrade).
    Silent: persists the current profile's home channel, no user-facing message.
    """

    name = "auto-sethome"

    async def handle(self, ctx: InboundContext, next_fn) -> None:
        adapter = ctx.adapter
        if not adapter._auto_sethome_done:
            configured_home = adapter.config.home_channel
            _cur_home = (
                configured_home.chat_id
                if configured_home is not None
                else get_profile_env("YUANBAO_HOME_CHANNEL", "")
            )
            _should_set = (
                not _cur_home
                or (_cur_home.startswith("group:") and ctx.chat_type == "dm")
            )
            if ctx.chat_type == "dm":
                adapter._auto_sethome_done = True  # DM seen — no further upgrades needed
            if _should_set:
                try:
                    from channels.config import HomeChannel, Platform
                    from hermes_cli.config import save_env_value

                    save_env_value("YUANBAO_HOME_CHANNEL", str(ctx.chat_id))
                    adapter.config.home_channel = HomeChannel(
                        platform=Platform.YUANBAO,
                        chat_id=str(ctx.chat_id),
                        name=ctx.chat_name or str(ctx.chat_id),
                    )
                    logger.info(
                        "[%s] Auto-sethome: designated %s (%s) as Yuanbao home channel",
                        adapter.name, ctx.chat_id, ctx.chat_name,
                    )
                    # Silent auto-sethome: no user-facing message, only log
                except Exception as e:
                    logger.warning("[%s] Auto-sethome failed: %s", adapter.name, e)
        await next_fn()


class ExtractContentMiddleware(InboundMiddleware):
    """Extract raw text and media refs from msg_body."""

    name = "extract-content"

    _CARD_CONTENT_MAX_LENGTH = 1000

    @staticmethod
    def _format_shared_link(custom: dict) -> str:
        """Format elem_type 1010 (share card) into bracket-placeholder text."""
        title = custom.get("title", "")
        link = custom.get("link", "")
        header = f"[share_card: {title} | {link}]" if link else f"[share_card: {title}]"
        lines = [header]
        max_len = ExtractContentMiddleware._CARD_CONTENT_MAX_LENGTH
        for field in ("card_content", "wechat_des"):
            val = custom.get(field)
            if val and isinstance(val, str):
                preview = val[:max_len] + "...(truncated)" if len(val) > max_len else val
                lines.append(f"Preview: {preview}")
                break
        if link:
            lines.append("[visit link for full content]")
        return "\n".join(lines)

    @staticmethod
    def _format_link_understanding(custom: dict) -> Optional[str]:
        """Format elem_type 1007 (link understanding card) into bracket-placeholder text."""
        content = custom.get("content")
        if not content:
            return None
        try:
            parsed = json.loads(content)
            link = parsed.get("link") if isinstance(parsed, dict) else None
        except (json.JSONDecodeError, TypeError):
            link = None
        if not link or not isinstance(link, str):
            return None
        return f"[link: {link} | visit link for full content]"

    @staticmethod
    def _parse_resource_id(url: str) -> str:
        """Extract resourceId from Yuanbao resource URL query parameters.

        Args:
            url: Resource URL (e.g., https://...?resourceId=abc123)

        Returns:
            Resource ID string, or empty string if not found
        """
        if not url:
            return ""
        try:
            query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
            ids = query.get("resourceId") or query.get("resourceid") or []
            return str(ids[0]).strip() if ids else ""
        except Exception:
            return ""

    @classmethod
    def _extract_text(cls, msg_body: list) -> str:
        """Extract plain text content from MsgBody.

        - TIMTextElem      -> text field
        - TIMImageElem     -> "[image]"
        - TIMFileElem      -> "[file: {filename}]"
        - TIMSoundElem     -> "[voice]"
        - TIMVideoFileElem -> "[video]"
        - TIMFaceElem      -> "[emoji: {name}]" or "[emoji]"
        - TIMCustomElem    -> try to extract data field, otherwise "[custom message]"
        - Multiple elems joined with spaces
        """
        parts: list[str] = []
        for elem in msg_body:
            elem_type: str = elem.get("msg_type", "")
            content: dict = elem.get("msg_content", {})

            if elem_type == "TIMTextElem":
                text = content.get("text", "")
                if text:
                    parts.append(text)
            elif elem_type == "TIMImageElem":
                # Extract resourceId from image_info_array URL
                image_info_array = content.get("image_info_array")
                if not isinstance(image_info_array, list):
                    image_info_array = []
                image_info = None
                # Prefer medium image (index 1), fallback to index 0
                if len(image_info_array) > 1 and isinstance(image_info_array[1], dict):
                    image_info = image_info_array[1]
                elif len(image_info_array) > 0 and isinstance(image_info_array[0], dict):
                    image_info = image_info_array[0]
                image_url = str((image_info or {}).get("url") or "").strip()
                rid = cls._parse_resource_id(image_url)
                parts.append(f"[image|ybres:{rid}]" if rid else "[image]")
            elif elem_type == "TIMFileElem":
                filename = content.get("file_name", content.get("fileName", content.get("filename", "")))
                file_url = str(content.get("url") or "").strip()
                rid = cls._parse_resource_id(file_url)
                if rid:
                    parts.append(f"[file:{filename}|ybres:{rid}]" if filename else f"[file|ybres:{rid}]")
                else:
                    parts.append(f"[file: {filename}]" if filename else "[file]")
            elif elem_type == "TIMSoundElem":
                sound_url = str(content.get("url") or "").strip()
                rid = cls._parse_resource_id(sound_url)
                parts.append(f"[voice|ybres:{rid}]" if rid else "[voice]")
            elif elem_type == "TIMVideoFileElem":
                video_url = str(content.get("url") or "").strip()
                rid = cls._parse_resource_id(video_url)
                parts.append(f"[video|ybres:{rid}]" if rid else "[video]")
            elif elem_type == "TIMCustomElem":
                data_val = content.get("data", "")
                if data_val:
                    try:
                        custom = json.loads(data_val)
                        if not isinstance(custom, dict):
                            parts.append("[unsupported message type]")
                            continue
                        ctype = custom.get("elem_type")
                        if ctype == 1002:
                            parts.append(custom.get("text", "[mention]"))
                        elif ctype == 1010:
                            parts.append(cls._format_shared_link(custom))
                        elif ctype == 1007:
                            text = cls._format_link_understanding(custom)
                            if text:
                                parts.append(text)
                            else:
                                parts.append("[unsupported message type]")
                        else:
                            parts.append("[unsupported message type]")
                    except (json.JSONDecodeError, TypeError):
                        parts.append(data_val)
                else:
                    parts.append("[unsupported message type]")
            elif elem_type == "TIMFaceElem":
                # Sticker/emoji: extract name from data JSON
                raw_data = content.get("data", "")
                face_name = ""
                if raw_data:
                    try:
                        face_data = json.loads(raw_data)
                        face_name = (face_data.get("name") or "").strip()
                    except (json.JSONDecodeError, TypeError, AttributeError):
                        pass
                parts.append(f"[emoji: {face_name}]" if face_name else "[emoji]")
            elif elem_type:
                # Unknown element type — include type as placeholder
                parts.append(f"[{elem_type}]")

        return " ".join(parts) if parts else ""

    @staticmethod
    def _rewrite_slash_command(text: str) -> str:
        """Normalize input text: strip whitespace and convert full-width slash
        (Chinese input method) to ASCII slash so commands are recognized correctly.
        """
        text = text.strip()
        if text.startswith('\uff0f'):  # Full-width slash
            text = '/' + text[1:]
        return text

    @staticmethod
    def _extract_inbound_media_refs(msg_body: list) -> List[Dict[str, str]]:
        """Extract inbound image/file references from TIM msg_body.

        Return example:
          [{"kind": "image", "url": "https://..."}, {"kind": "file", "url": "...", "name": "a.pdf"}]
        """
        refs: List[Dict[str, str]] = []
        for elem in msg_body or []:
            if not isinstance(elem, dict):
                continue
            msg_type = elem.get("msg_type", "")
            content = elem.get("msg_content", {}) or {}
            if not isinstance(content, dict):
                continue

            if msg_type == "TIMImageElem":
                # Prefer medium image (index 1), fallback to index 0.
                image_info_array = content.get("image_info_array")
                if not isinstance(image_info_array, list):
                    image_info_array = []
                image_info = None
                if len(image_info_array) > 1 and isinstance(image_info_array[1], dict):
                    image_info = image_info_array[1]
                elif len(image_info_array) > 0 and isinstance(image_info_array[0], dict):
                    image_info = image_info_array[0]
                image_url = str((image_info or {}).get("url") or "").strip()
                if image_url:
                    refs.append({"kind": "image", "url": image_url})
                continue

            if msg_type == "TIMFileElem":
                file_url = str(content.get("url") or "").strip()
                file_name = (
                    str(content.get("file_name") or "").strip()
                    or str(content.get("fileName") or "").strip()
                    or str(content.get("filename") or "").strip()
                )
                if file_url:
                    ref: Dict[str, str] = {"kind": "file", "url": file_url}
                    if file_name:
                        ref["name"] = file_name
                    refs.append(ref)
        return refs

    @staticmethod
    def _extract_link_urls(msg_body: list) -> list:
        """Extract link URLs from share-card (1010) and link-understanding (1007) custom elems."""
        urls: list[str] = []
        for elem in msg_body or []:
            if not isinstance(elem, dict) or elem.get("msg_type") != "TIMCustomElem":
                continue
            data_str = (elem.get("msg_content") or {}).get("data", "")
            if not data_str:
                continue
            try:
                custom = json.loads(data_str)
            except (json.JSONDecodeError, TypeError):
                continue
            if not isinstance(custom, dict):
                continue
            ctype = custom.get("elem_type")
            if ctype == 1010:
                link = custom.get("link")
                if link and isinstance(link, str):
                    urls.append(link)
            elif ctype == 1007:
                content = custom.get("content")
                if content:
                    try:
                        parsed = json.loads(content)
                        link = parsed.get("link") if isinstance(parsed, dict) else None
                        if link and isinstance(link, str):
                            urls.append(link)
                    except (json.JSONDecodeError, TypeError):
                        pass
        return urls

    async def handle(self, ctx: InboundContext, next_fn) -> None:
        ctx.raw_text = self._rewrite_slash_command(self._extract_text(ctx.msg_body))
        ctx.media_refs = self._extract_inbound_media_refs(ctx.msg_body)
        ctx.link_urls = self._extract_link_urls(ctx.msg_body)
        await next_fn()

class PlaceholderFilterMiddleware(InboundMiddleware):
    """Skip pure placeholder messages (e.g. '[image]' with no media)."""

    name = "placeholder-filter"

    SKIPPABLE_PLACEHOLDERS: frozenset = frozenset({
        "[image]", "[图片]", "[file]", "[文件]",
        "[video]", "[视频]", "[voice]", "[语音]",
    })

    @classmethod
    def is_skippable_placeholder(cls, text: str, media_count: int = 0) -> bool:
        """Detect whether the message is a pure placeholder (should be skipped)."""
        if media_count > 0:
            return False
        stripped = text.strip()
        return stripped in cls.SKIPPABLE_PLACEHOLDERS

    async def handle(self, ctx: InboundContext, next_fn) -> None:
        if self.is_skippable_placeholder(ctx.raw_text, len(ctx.media_refs)):
            logger.debug("[%s] Skipping placeholder message: %r", ctx.adapter.name, ctx.raw_text)
            return  # Stop pipeline
        await next_fn()


class OwnerCommandMiddleware(InboundMiddleware):
    """Detect bot-owner slash commands in group chat.

    Identifies in-group allowlisted slash commands and determines sender identity.
    Owner commands skip @Bot detection; non-owner attempts are rejected.
    """

    name = "owner-command"

    # Slash command allowlist that bot owner can execute in group without @Bot
    ALLOWLIST: frozenset = frozenset({
        "/new", "/reset", "/retry", "/undo", "/stop",
        "/approve", "/deny", "/background", "/bg",
        "/btw", "/queue", "/q",
    })

    @staticmethod
    def _rewrite_slash_command(text: str) -> str:
        """Normalize full-width slash to ASCII slash and strip whitespace."""
        text = text.strip()
        if text.startswith('\uff0f'):  # Full-width slash
            text = '/' + text[1:]
        return text

    @classmethod
    def _detect_owner_command(
        cls,
        *,
        push: dict,
        msg_body: list,
        chat_type: str,
        from_account: str,
    ) -> Tuple[Optional[str], Optional[str], bool]:
        """Identify allowlisted slash commands and determine sender identity.

        Returns (cmd, cmd_line, is_owner):
          - (None, None, False): Not an allowlisted command
          - (cmd, cmd_line, True): Owner match
          - (cmd, cmd_line, False): Allowlisted command but sender is not owner
        """
        if chat_type != "group" or not cls.ALLOWLIST:
            return None, None, False

        # Extract TIMTextElem: only do command recognition with exactly one text segment
        text_elems = [
            e for e in (msg_body or [])
            if e.get("msg_type") == "TIMTextElem"
        ]
        if len(text_elems) != 1:
            return None, None, False

        text = (text_elems[0].get("msg_content") or {}).get("text", "")
        cmd_line = cls._rewrite_slash_command(text)
        if not cmd_line.startswith("/"):
            return None, None, False
        cmd = cmd_line.split(maxsplit=1)[0].lower()
        if cmd not in cls.ALLOWLIST:
            return None, None, False

        # Sender identity check: bot owner <-> push.from_account == push.bot_owner_id.
        # The allowlisted commands (/approve, /deny, /stop, /reset, ...) are
        # privileged — leaking them to non-owners lets any group member approve
        # a dangerous tool call, kill the owner's task, or wipe session state.
        owner_id = str((push or {}).get("bot_owner_id") or "").strip()
        is_owner = bool(owner_id) and owner_id == from_account
        return cmd, cmd_line, is_owner

    async def handle(self, ctx: InboundContext, next_fn) -> None:
        adapter = ctx.adapter
        matched_cmd, cmd_line, is_owner = self._detect_owner_command(
            push=ctx.push,
            msg_body=ctx.msg_body,
            chat_type=ctx.chat_type,
            from_account=ctx.from_account,
        )
        if matched_cmd and not is_owner:
            # Non-owner tried an owner-only command — reject and stop
            logger.info(
                "[%s] Reject non-owner slash command: chat=%s from=%s cmd=%s",
                adapter.name, ctx.chat_id, ctx.from_account, matched_cmd,
            )
            adapter._track_task(asyncio.create_task(
                adapter.send(ctx.chat_id, f"⚠️ {matched_cmd} is only available to the creator in private chat mode"),
                name=f"yuanbao-owner-cmd-denial-{matched_cmd}",
            ))
            return  # Stop pipeline

        if matched_cmd and is_owner and cmd_line:
            logger.info(
                "[%s] Bot owner slash command: chat=%s from=%s cmd=%s",
                adapter.name, ctx.chat_id, ctx.from_account, matched_cmd,
            )
            ctx.owner_command = matched_cmd
            ctx.raw_text = cmd_line  # Override with clean command text
        await next_fn()


class BuildSourceMiddleware(InboundMiddleware):
    """Build SessionSource from context fields."""

    name = "build-source"

    async def handle(self, ctx: InboundContext, next_fn) -> None:
        adapter = ctx.adapter
        ctx.source = adapter.build_source(
            chat_id=ctx.chat_id,
            chat_type=ctx.chat_type,
            chat_name=ctx.chat_name,
            user_id=ctx.from_account or None,
            user_name=ctx.sender_nickname or ctx.from_account,
            thread_id="main" if ctx.chat_type == "group" else None,
        )
        await next_fn()


class GroupAtGuardMiddleware(InboundMiddleware):
    """In group chat, observe non-@bot messages; only reply on @Bot.

    Owner commands skip @Bot detection (owner doesn't need to @Bot).
    """

    name = "group-at-guard"

    @staticmethod
    def _is_at_bot(msg_body: list, bot_id: Optional[str]) -> bool:
        """Detect whether the message @Bot.

        AT element format: TIMCustomElem, msg_content.data is a JSON string:
            {"elem_type": 1002, "text": "@xxx", "user_id": "<botId>"}
        Considered @Bot when elem_type == 1002 and user_id == bot_id.
        """
        if not bot_id:
            return False
        for elem in msg_body:
            if elem.get("msg_type") != "TIMCustomElem":
                continue
            data_str = elem.get("msg_content", {}).get("data", "")
            if not data_str:
                continue
            try:
                custom = json.loads(data_str)
            except (json.JSONDecodeError, TypeError):
                continue
            if custom.get("elem_type") == 1002 and custom.get("user_id") == bot_id:
                return True
        return False

    @staticmethod
    def _extract_bot_mention_text(msg_body: list, bot_id: Optional[str]) -> str:
        """Extract the display text used to @-mention this bot (e.g. ``@yuanbao-bot``)."""
        if not bot_id:
            return ""
        for elem in msg_body:
            if elem.get("msg_type") != "TIMCustomElem":
                continue
            data_str = elem.get("msg_content", {}).get("data", "")
            if not data_str:
                continue
            try:
                custom = json.loads(data_str)
            except (json.JSONDecodeError, TypeError):
                continue
            if custom.get("elem_type") == 1002 and custom.get("user_id") == bot_id:
                mention_text = str(custom.get("text") or "").strip()
                if mention_text:
                    return mention_text
        return ""

    @staticmethod
    def _build_group_channel_prompt(msg_body: list, bot_id: Optional[str]) -> str:
        """Build a per-turn group-chat prompt that highlights which message to respond to."""
        bid = str(bot_id or "unknown")
        bot_mention = GroupAtGuardMiddleware._extract_bot_mention_text(msg_body, bot_id) or "unknown"
        return (
            "You are handling a Yuanbao group chat message.\n"
            f"- Your identity: user_id={bid}, @-mention name in this group={bot_mention}\n"
            "- Lines in history prefixed with `[nickname|user_id]` are observed group context "
            "and are not necessarily addressed to you.\n"
            "- Treat only the current new message as a request explicitly directed at you, "
            "and answer it directly."
        )

    @staticmethod
    def _observe_group_message(
        adapter, source, sender_display: str, text: str,
        *, msg_id: Optional[str] = None,
    ) -> None:
        """Write a group message into the session transcript without triggering the agent.

        This allows the model to see the full group conversation when it is
        eventually invoked via @bot.  Messages are stored with ``role: "user"``
        in the format ``[nickname|user_id]\\n<content>`` so the model
        can distinguish participants and their user ids.
        """
        store = getattr(adapter, "_session_store", None)
        if not store:
            return
        try:
            session_entry = store.get_or_create_session(source)
            user_id = source.user_id or "unknown"
            attributed = f"[{sender_display}|{user_id}]\n{text}"
            entry: dict = {
                "role": "user",
                "content": attributed,
                "timestamp": datetime.now(tz=timezone.utc).isoformat(),
                "observed": True,
            }
            if msg_id:
                entry["message_id"] = msg_id
            store.append_to_transcript(
                session_entry.session_id,
                entry,
            )
        except Exception as exc:
            logger.warning("[%s] Failed to observe group message: %s", adapter.name, exc)

    async def handle(self, ctx: InboundContext, next_fn) -> None:
        adapter = ctx.adapter
        if ctx.chat_type == "group" and not ctx.owner_command and not self._is_at_bot(ctx.msg_body, adapter._bot_id):
            self._observe_group_message(
                adapter, ctx.source, ctx.sender_nickname or ctx.from_account, ctx.raw_text,
                msg_id=ctx.msg_id or None,
            )
            logger.info(
                "[%s] Group message observed (no @bot): chat=%s from=%s",
                adapter.name, ctx.chat_id, ctx.from_account,
            )
            return  # Stop pipeline — message observed but not dispatched
        await next_fn()


class GroupAttributionMiddleware(InboundMiddleware):
    """Tag group @bot messages with [nickname|user_id] attribution and channel_prompt.

    For group messages that pass the @bot guard (i.e. the bot is mentioned),
    this middleware:
      - Builds a per-turn channel_prompt so the model knows its identity and
        the attribution scheme.
      - Rewrites ctx.raw_text to ``[nickname|user_id]\\n<content>`` to match
        the observed-history format.
      - Suppresses the runner's default ``[user_name]`` shared-thread prefix
        by clearing ``source.user_name``.
    """

    name = "group-attribution"

    async def handle(self, ctx: InboundContext, next_fn) -> None:
        if ctx.chat_type == "group" and not ctx.owner_command:
            adapter = ctx.adapter
            ctx.channel_prompt = GroupAtGuardMiddleware._build_group_channel_prompt(
                ctx.msg_body, adapter._bot_id,
            )
            user_id_label = ctx.from_account or "unknown"
            nickname_label = ctx.sender_nickname or ctx.from_account or "unknown"
            ctx.raw_text = f"[{nickname_label}|{user_id_label}]\n{ctx.raw_text}"
            # Suppress runner's default ``[user_name]`` shared-thread prefix so
            # the text the model sees matches the observed-history format.
            if ctx.source is not None:
                ctx.source = dataclasses.replace(ctx.source, user_name=None)
        await next_fn()


class ClassifyMessageTypeMiddleware(InboundMiddleware):
    """Determine MessageType from text content and msg_body elements."""

    name = "classify-msg-type"

    @staticmethod
    def _classify(text: str, msg_body: list) -> MessageType:
        """Classify message type based on text and msg_body."""
        if text.startswith("/"):
            return MessageType.COMMAND
        for elem in msg_body:
            etype = elem.get("msg_type", "")
            if etype == "TIMImageElem":
                return MessageType.PHOTO
            if etype == "TIMSoundElem":
                return MessageType.VOICE
            if etype == "TIMVideoFileElem":
                return MessageType.VIDEO
            if etype == "TIMFileElem":
                return MessageType.DOCUMENT
        return MessageType.TEXT

    async def handle(self, ctx: InboundContext, next_fn) -> None:
        ctx.msg_type = self._classify(ctx.raw_text, ctx.msg_body)
        await next_fn()


class QuoteContextMiddleware(InboundMiddleware):
    """Extract quote/reply context from cloud_custom_data."""

    name = "quote-context"

    @staticmethod
    def _extract_quote_context(cloud_custom_data: str) -> Tuple[Optional[str], Optional[str], list]:
        """Extract quote context, mapping to MessageEvent.reply_to_*.

        Returns:
          (reply_to_message_id, reply_to_text, quote_media_refs)
          where quote_media_refs is a list of (rid, kind, filename) tuples
        """
        if not cloud_custom_data:
            return None, None, []
        try:
            parsed = json.loads(cloud_custom_data)
        except (json.JSONDecodeError, TypeError):
            return None, None, []

        quote = parsed.get("quote") if isinstance(parsed, dict) else None
        if not isinstance(quote, dict):
            return None, None, []

        # type=2 corresponds to image reference; desc may be empty, provide a placeholder.
        quote_type = int(quote.get("type") or 0)
        desc = str(quote.get("desc") or "").strip()
        if quote_type == 2 and not desc:
            desc = "[image]"
        if not desc:
            return None, None, []

        quote_id = str(quote.get("id") or "").strip() or None
        sender = str(quote.get("sender_nickname") or quote.get("sender_id") or "").strip()
        quote_text = f"{sender}: {desc}" if sender else desc

        # Extract media references from desc using _YB_RES_REF_RE regex
        media_refs: list = []
        for m in _YB_RES_REF_RE.finditer(desc):
            head = m.group(1)  # "image" | "file:<name>" | "voice" | "video"
            rid = m.group(2)
            kind, _, filename = head.partition(":")
            kind = kind.strip()
            media_refs.append((rid, kind, filename.strip()))

        return quote_id, quote_text, media_refs

    async def handle(self, ctx: InboundContext, next_fn) -> None:
        ctx.reply_to_message_id, ctx.reply_to_text, ctx.quote_media_refs = self._extract_quote_context(ctx.cloud_custom_data)

        await next_fn()


class MediaResolveMiddleware(InboundMiddleware):
    """Resolve inbound media references to downloadable URLs."""

    name = "media-resolve"

    @staticmethod
    def _guess_image_ext_from_url(url: str) -> str:
        """Guess image extension from URL path."""
        path = urllib.parse.urlparse(url).path
        ext = os.path.splitext(path)[1].lower()
        if ext in {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".heic", ".tiff"}:
            return ext
        return ".jpg"

    @staticmethod
    async def _fetch_resource_url(adapter, resource_id: str) -> str:
        """Low-level helper: exchange a ``resourceId`` for a direct download URL.

        Handles token retrieval, the ``/api/resource/v1/download`` API call,
        and a single 401-retry with token force-refresh.  Raises on failure.
        """
        resource_id = resource_id.strip()
        if not resource_id:
            raise RuntimeError("missing resource_id")

        token_data = await adapter._get_cached_token()
        token = str(token_data.get("token") or "").strip()
        source = str(token_data.get("source") or "web").strip() or "web"
        bot_id = str(token_data.get("bot_id") or adapter._bot_id or adapter._app_key).strip()
        if not token or not bot_id:
            raise RuntimeError("missing token or bot_id for resource download")

        api_url = f"{adapter._api_domain}/api/resource/v1/download"
        headers = {
            "Content-Type": "application/json",
            "X-ID": bot_id,
            "X-Token": token,
            "X-Source": source,
        }

        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
            for attempt in range(2):
                resp = await client.get(api_url, params={"resourceId": resource_id}, headers=headers)
                if resp.status_code == 401 and attempt == 0:
                    # Force refresh token once on expiry and retry
                    token_data = await SignManager.force_refresh(
                        adapter._app_key, adapter._app_secret, adapter._api_domain,
                    )
                    token = str(token_data.get("token") or "").strip()
                    source = str(token_data.get("source") or source or "web").strip() or "web"
                    bot_id = str(token_data.get("bot_id") or adapter._bot_id or adapter._app_key).strip()
                    if not token or not bot_id:
                        break
                    headers["X-ID"] = bot_id
                    headers["X-Token"] = token
                    headers["X-Source"] = source
                    continue

                resp.raise_for_status()
                payload = resp.json()
                code = payload.get("code")
                if code not in {None, 0}:
                    raise RuntimeError(
                        f"resource/v1/download failed: code={code}, msg={payload.get('msg', '')}"
                    )
                data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
                real_url = str((data or {}).get("url") or (data or {}).get("realUrl") or "").strip()
                if real_url:
                    return real_url
                raise RuntimeError("resource/v1/download missing url/realUrl")

        raise RuntimeError("resource/v1/download did not return a URL")

    @staticmethod
    async def _resolve_download_url(adapter, url: str) -> str:
        """Resolve Yuanbao resource placeholder to a directly fetchable real URL.

        Common URL patterns:
          https://hunyuan.tencent.com/api/resource/download?resourceId=...
        Direct GET returns 401; need business API:
          GET /api/resource/v1/download?resourceId=...
        """
        try:
            parsed = urllib.parse.urlparse(url)
        except Exception:
            return url

        query = urllib.parse.parse_qs(parsed.query)
        resource_ids = query.get("resourceId") or query.get("resourceid") or []
        resource_id = str(resource_ids[0]).strip() if resource_ids else ""
        if not resource_id:
            return url

        try:
            return await MediaResolveMiddleware._fetch_resource_url(adapter, resource_id)
        except Exception:
            return url

    @classmethod
    async def _download_and_cache(
        cls, adapter, *, fetch_url: str, kind: str,
        file_name: Optional[str] = None, log_tag: str = "",
    ) -> Optional[Tuple[str, str]]:
        """Download a Yuanbao resource and cache locally. Returns ``(local_path, mime)`` or ``None``."""
        try:
            file_bytes, content_type = await media_download_url(
                fetch_url, max_size_mb=adapter.MEDIA_MAX_SIZE_MB,
            )
        except Exception as exc:
            logger.warning(
                "[%s] inbound media download failed: kind=%s %s err=%s",
                adapter.name, kind, log_tag, exc,
            )
            return None

        if kind == "image":
            ext = cls._guess_image_ext_from_url(fetch_url)
            try:
                local_path = cache_image_from_bytes(file_bytes, ext=ext)
            except ValueError as exc:
                logger.warning(
                    "[%s] inbound image cache rejected: %s err=%s",
                    adapter.name, log_tag, exc,
                )
                return None
            mime = guess_mime_type(f"image{ext}")
            if not mime.startswith("image/"):
                mime = content_type if content_type.startswith("image/") else "image/jpeg"
            return local_path, mime

        # kind == "file"
        if not file_name:
            parsed = urllib.parse.urlparse(fetch_url)
            file_name = os.path.basename(parsed.path) or "file"
        try:
            local_path = cache_document_from_bytes(file_bytes, file_name)
        except Exception as exc:
            logger.warning(
                "[%s] inbound file cache failed: %s err=%s",
                adapter.name, log_tag, exc,
            )
            return None
        mime = guess_mime_type(file_name) or content_type or "application/octet-stream"
        return local_path, mime

    @classmethod
    async def _resolve_by_resource_id(cls, adapter, resource_id: str) -> str:
        """Exchange a Yuanbao ``resourceId`` for a short-lived direct download URL. Raises on failure."""
        return await cls._fetch_resource_url(adapter, resource_id)

    @classmethod
    async def _resolve_media_urls(
        cls, adapter, media_refs: List[Dict[str, str]]
    ) -> Tuple[List[str], List[str]]:
        """Resolve inbound media refs: download to local cache, return (local_paths, mime_types).

        Yuanbao COS hostnames resolve to private IPs, tripping the SSRF guard
        in vision_tools. We download ourselves and return local cache paths.
        """
        media_urls: List[str] = []
        media_types: List[str] = []

        for ref in media_refs:
            kind = str(ref.get("kind") or "").strip().lower()
            url = str(ref.get("url") or "").strip()
            if kind not in _RESOLVABLE_MEDIA_KINDS or not url:
                continue

            try:
                fetch_url = await cls._resolve_download_url(adapter, url)
            except Exception as exc:
                logger.warning(
                    "[%s] inbound media resolve failed: kind=%s url=%s err=%s",
                    adapter.name, kind, url, exc,
                )
                continue

            cached = await cls._download_and_cache(
                adapter,
                fetch_url=fetch_url,
                kind=kind,
                file_name=str(ref.get("name") or "").strip() or None,
                log_tag=f"placeholder_url={url[:80]}",
            )
            if cached is None:
                continue
            local_path, mime = cached
            media_urls.append(local_path)
            media_types.append(mime)

        return media_urls, media_types

    @classmethod
    async def _collect_observed_media(
        cls, adapter, source,
    ) -> Tuple[List[str], List[str]]:
        """Resolve recent observed image/file anchors from transcript into ``(local_paths, mimes)``."""
        store = getattr(adapter, "_session_store", None)
        if not store:
            return [], []
        try:
            session_entry = store.get_or_create_session(source)
            history = store.load_transcript(session_entry.session_id)
        except Exception as exc:
            logger.warning(
                "[%s] Observed-media hydration setup failed: %s",
                adapter.name, exc,
            )
            return [], []
        if not history:
            return [], []

        start = max(0, len(history) - OBSERVED_MEDIA_BACKFILL_LOOKBACK)
        order: List[Tuple[str, str, str]] = []  # (rid, kind, filename)
        seen: set = set()
        for msg in history[start:]:
            content = msg.get("content")
            if not isinstance(content, str) or "|ybres:" not in content:
                continue
            for m in _YB_RES_REF_RE.finditer(content):
                head = m.group(1)  # "image" | "file:<name>" | "voice" | "video"
                rid = m.group(2)
                kind, _, filename = head.partition(":")
                kind = kind.strip()
                if kind not in _RESOLVABLE_MEDIA_KINDS:
                    continue
                if rid in seen:
                    continue
                seen.add(rid)
                order.append((rid, kind, filename.strip()))
                if len(order) >= OBSERVED_MEDIA_BACKFILL_MAX_RESOLVE_PER_TURN:
                    break
            if len(order) >= OBSERVED_MEDIA_BACKFILL_MAX_RESOLVE_PER_TURN:
                break

        if not order:
            return [], []

        media_paths: List[str] = []
        mimes: List[str] = []
        for rid, kind, filename in order:
            try:
                fresh_url = await cls._resolve_by_resource_id(adapter, rid)
            except Exception as exc:
                logger.warning(
                    "[%s] observed-media resolve failed: rid=%s kind=%s err=%s",
                    adapter.name, rid, kind, exc,
                )
                continue
            cached = await cls._download_and_cache(
                adapter,
                fetch_url=fresh_url,
                kind=kind,
                file_name=filename or None,
                log_tag=f"rid={rid}",
            )
            if cached is None:
                continue
            path, mime = cached
            media_paths.append(path)
            mimes.append(mime)
        return media_paths, mimes

    async def handle(self, ctx: InboundContext, next_fn) -> None:
        adapter = ctx.adapter
        ctx.media_urls, ctx.media_types = await self._resolve_media_urls(adapter, ctx.media_refs)
        # Re-check placeholder after media resolution
        if PlaceholderFilterMiddleware.is_skippable_placeholder(ctx.raw_text, len(ctx.media_urls)):
            logger.debug("[%s] Skip placeholder after media download: %r", adapter.name, ctx.raw_text)
            return  # Stop pipeline
        await next_fn()


class DispatchMiddleware(InboundMiddleware):
    """Build MessageEvent and dispatch to AI handler."""

    name = "dispatch"

    async def handle(self, ctx: InboundContext, next_fn) -> None:
        adapter = ctx.adapter

        _sk = build_session_key(
            ctx.source,
            group_sessions_per_user=adapter.config.extra.get("group_sessions_per_user", True),
            thread_sessions_per_user=adapter.config.extra.get("thread_sessions_per_user", False),
        )

        async def _dispatch_inbound_event() -> None:
            media_urls = list(ctx.media_urls)
            media_types = list(ctx.media_types)

            # If user quoted a message (reply_to_message_id is set), resolve only
            # quote_media_refs to avoid injecting unrelated history media.
            # Otherwise, backfill observed media from recent transcript history.
            if ctx.reply_to_message_id is not None:
                # Fallback: if desc didn't contain ybres refs, look up transcript
                if not ctx.quote_media_refs:
                    try:
                        store = getattr(adapter, "_session_store", None)
                        if store:
                            session_entry = store.get_or_create_session(ctx.source)
                            history = store.load_transcript(session_entry.session_id)
                            for msg in reversed(history or []):
                                mid = msg.get("message_id", "")
                                if mid and mid == ctx.reply_to_message_id:
                                    _content = msg.get("content", "")
                                    if isinstance(_content, str) and "|ybres:" in _content:
                                        for m in _YB_RES_REF_RE.finditer(_content):
                                            head = m.group(1)
                                            rid = m.group(2)
                                            kind, _, filename = head.partition(":")
                                            kind = kind.strip()
                                            if kind in _RESOLVABLE_MEDIA_KINDS:
                                                ctx.quote_media_refs.append((rid, kind, filename.strip()))
                                    break
                    except Exception as exc:
                        logger.warning(
                            "[%s] quote transcript lookup failed: %s",
                            adapter.name, exc,
                        )
                # User quoted a message — resolve only media from the quote
                for rid, kind, filename in ctx.quote_media_refs:
                    if kind not in _RESOLVABLE_MEDIA_KINDS:
                        continue
                    try:
                        fresh_url = await MediaResolveMiddleware._resolve_by_resource_id(adapter, rid)
                    except Exception as exc:
                        logger.warning(
                            "[%s] quote media resolve failed: rid=%s kind=%s err=%s",
                            adapter.name, rid, kind, exc,
                        )
                        continue
                    cached = await MediaResolveMiddleware._download_and_cache(
                        adapter,
                        fetch_url=fresh_url,
                        kind=kind,
                        file_name=filename or None,
                        log_tag=f"quote rid={rid}",
                    )
                    if cached is None:
                        continue
                    path, mime = cached
                    # Avoid duplicates
                    if path not in media_urls:
                        media_urls.append(path)
                        media_types.append(mime)
            else:
                # No quote — backfill observed media from recent transcript history
                extra_img_urls: List[str] = []
                extra_img_mimes: List[str] = []
                try:
                    extra_img_urls, extra_img_mimes = await MediaResolveMiddleware._collect_observed_media(
                        adapter, ctx.source,
                    )
                except Exception as exc:
                    logger.warning(
                        "[%s] observed-image hydration raised, continuing anyway: %s",
                        adapter.name, exc,
                    )
                if extra_img_urls:
                    current = set(media_urls)
                    for u, m in zip(extra_img_urls, extra_img_mimes):
                        if u in current:
                            continue
                        media_urls.append(u)
                        media_types.append(m)
                        current.add(u)

            # Replace [kind|ybres:xxx] anchors with local cache paths so
            # the transcript records usable paths for the model.
            _patched_event_text = ctx.raw_text
            for u, m in zip(media_urls, media_types):
                if not u.startswith("/"):
                    continue
                anchor_match = _YB_RES_REF_RE.search(_patched_event_text)
                if not anchor_match:
                    continue
                head = anchor_match.group(1)
                kind, _, filename = head.partition(":")
                kind = kind.strip()
                if kind == "image" and m.startswith("image/"):
                    replacement = f"[image: {u}]"
                elif kind == "file":
                    label = filename.strip() or os.path.basename(u)
                    replacement = f"[file: {label} → {u}]"
                else:
                    continue
                _patched_event_text = (
                    _patched_event_text[:anchor_match.start()]
                    + replacement
                    + _patched_event_text[anchor_match.end():]
                )

            event = MessageEvent(
                text=_patched_event_text,
                message_type=(
                    MessageType.DOCUMENT
                    if any(mt.startswith(("application/", "text/")) for mt in media_types)
                    else ctx.msg_type
                ),
                source=ctx.source,
                message_id=ctx.msg_id or None,
                raw_message=ctx.push,
                media_urls=media_urls,
                media_types=media_types,
                reply_to_message_id=ctx.reply_to_message_id,
                reply_to_text=ctx.reply_to_text,
                channel_prompt=ctx.channel_prompt,
            )
            if _sk and ctx.msg_id:
                adapter._processing_msg_ids[_sk] = ctx.msg_id
                adapter._processing_msg_texts[_sk] = ctx.raw_text or ""
            if ctx.msg_id and ctx.raw_text:
                cache = adapter._msg_content_cache
                cache[ctx.msg_id] = ctx.raw_text
                if len(cache) > 200:
                    for k in list(cache)[:len(cache) - 200]:
                        del cache[k]
            await adapter.handle_message(event)

        if ctx.chat_type == "group":
            is_new = _sk not in adapter._group_queues
            queue = adapter._group_queues.setdefault(_sk, asyncio.Queue())
            queue.put_nowait(_dispatch_inbound_event)
            logger.info(
                "[%s] Group message enqueued (qsize=%d) for %s",
                adapter.name, queue.qsize(), (_sk or "")[:50],
            )
            if is_new:
                consumer = asyncio.create_task(
                    self._consume_group_queue(adapter, _sk),
                    name=f"yuanbao-group-consumer-{(_sk or '')[:30]}",
                )
                adapter._inbound_tasks.add(consumer)
                consumer.add_done_callback(adapter._inbound_tasks.discard)
        else:
            task = asyncio.create_task(
                _dispatch_inbound_event(),
                name=f"yuanbao-inbound-{ctx.msg_id or 'unknown'}",
            )
            adapter._inbound_tasks.add(task)
            task.add_done_callback(adapter._inbound_tasks.discard)

        await next_fn()

    @staticmethod
    async def _consume_group_queue(adapter: "YuanbaoAdapter", session_key: str) -> None:
        """Drain the group queue one dispatch at a time, waiting for each to finish."""
        _IDLE_TIMEOUT = 2.0
        queue = adapter._group_queues.get(session_key)
        if not queue:
            return
        try:
            while True:
                try:
                    dispatch_fn = await asyncio.wait_for(queue.get(), timeout=_IDLE_TIMEOUT)
                except asyncio.TimeoutError:
                    break
                logger.debug(
                    "[%s] Group queue: dispatching for %s (remaining=%d)",
                    adapter.name, (session_key or "")[:50], queue.qsize(),
                )
                try:
                    await dispatch_fn()
                    while session_key in adapter._active_sessions:
                        await asyncio.sleep(0.1)
                except Exception:
                    logger.exception("[%s] Group queue consumer error", adapter.name)
        finally:
            adapter._group_queues.pop(session_key, None)


class InboundPipelineBuilder:
    """Factory for building InboundPipeline instances.

    Separates pipeline assembly (business knowledge) from the pipeline engine
    (InboundPipeline) so the engine stays generic and reusable.
    """

    # Default middleware sequence for Yuanbao inbound message processing.
    _DEFAULT_MIDDLEWARES: list[type] = [
        DecodeMiddleware,
        ExtractFieldsMiddleware,
        RecallGuardMiddleware,
        DedupMiddleware,
        SkipSelfMiddleware,
        ChatRoutingMiddleware,
        AccessGuardMiddleware,
        AutoSetHomeMiddleware,
        ExtractContentMiddleware,
        PlaceholderFilterMiddleware,
        OwnerCommandMiddleware,
        BuildSourceMiddleware,
        GroupAtGuardMiddleware,
        GroupAttributionMiddleware,
        ClassifyMessageTypeMiddleware,
        QuoteContextMiddleware,
        MediaResolveMiddleware,
        DispatchMiddleware,
    ]

    @classmethod
    def build(cls) -> InboundPipeline:
        """Build the default inbound message processing pipeline."""
        pipeline = InboundPipeline()
        for mw_cls in cls._DEFAULT_MIDDLEWARES:
            pipeline.use(mw_cls())
        return pipeline
