"""Feishu inbound batching, admission, bot identity, and dedup state."""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import time
from typing import Any, Dict, List, Optional

try:
    from lark_oapi.core import AccessTokenType, HttpMethod
    from lark_oapi.core.model import BaseRequest
except ImportError:
    AccessTokenType = None  # type: ignore[assignment]
    HttpMethod = None  # type: ignore[assignment]
    BaseRequest = None  # type: ignore[assignment]

from channels.platforms.base import MessageEvent
from channels.platforms.feishu_message import (
    FeishuMentionRef,
    RejectReason,
    _FeishuBotIdentity,
    _is_bot_sender,
    _sender_identity,
    normalize_feishu_message,
)
from utils import atomic_json_write

logger = logging.getLogger(__name__)

_FEISHU_DEDUP_TTL_SECONDS = 6 * 60 * 60


def _feishu_public_attr(name: str, fallback: Any = None) -> Any:
    module = sys.modules.get("channels.platforms.feishu")
    return getattr(module, name, fallback) if module is not None else fallback


class FeishuInboundStateMixin:
    def _text_batch_key(self, event: MessageEvent) -> str:
        """Return the session-scoped key used for Feishu text aggregation."""
        from channels.session_identity import build_session_key

        return build_session_key(
            event.source,
            group_sessions_per_user=self.config.extra.get("group_sessions_per_user", True),
            thread_sessions_per_user=self.config.extra.get("thread_sessions_per_user", False),
        )

    @staticmethod
    def _text_batch_is_compatible(existing: MessageEvent, incoming: MessageEvent) -> bool:
        """Only merge text events when reply/thread context is identical."""
        return (
            existing.reply_to_message_id == incoming.reply_to_message_id
            and existing.reply_to_text == incoming.reply_to_text
            and existing.source.thread_id == incoming.source.thread_id
        )

    async def _enqueue_text_event(self, event: MessageEvent) -> None:
        """Debounce rapid Feishu text bursts into a single MessageEvent."""
        key = self._text_batch_key(event)
        chunk_len = len(event.text or "")
        existing = self._pending_text_batches.get(key)
        if existing is None:
            event._last_chunk_len = chunk_len  # type: ignore[attr-defined]
            self._pending_text_batches[key] = event
            self._pending_text_batch_counts[key] = 1
            self._schedule_text_batch_flush(key)
            return

        if not self._text_batch_is_compatible(existing, event):
            await self._flush_text_batch_now(key)
            self._pending_text_batches[key] = event
            self._pending_text_batch_counts[key] = 1
            self._schedule_text_batch_flush(key)
            return

        existing_count = self._pending_text_batch_counts.get(key, 1)
        next_count = existing_count + 1
        appended_text = event.text or ""
        next_text = f"{existing.text}\n{appended_text}" if existing.text and appended_text else (existing.text or appended_text)
        if next_count > self._text_batch_max_messages or len(next_text) > self._text_batch_max_chars:
            await self._flush_text_batch_now(key)
            self._pending_text_batches[key] = event
            self._pending_text_batch_counts[key] = 1
            self._schedule_text_batch_flush(key)
            return

        existing.text = next_text
        existing._last_chunk_len = chunk_len  # type: ignore[attr-defined]
        existing.timestamp = event.timestamp
        if event.message_id:
            existing.message_id = event.message_id
        self._pending_text_batch_counts[key] = next_count
        self._schedule_text_batch_flush(key)

    def _schedule_text_batch_flush(self, key: str) -> None:
        """Reset the debounce timer for a pending Feishu text batch."""
        self._reschedule_batch_task(
            self._pending_text_batch_tasks,
            key,
            self._flush_text_batch,
        )

    @staticmethod
    def _reschedule_batch_task(
        task_map: Dict[str, asyncio.Task],
        key: str,
        flush_fn: Any,
    ) -> None:
        prior_task = task_map.get(key)
        if prior_task and not prior_task.done():
            prior_task.cancel()
        task_map[key] = asyncio.create_task(flush_fn(key))

    async def _flush_text_batch(self, key: str) -> None:
        """Flush a pending text batch after the quiet period.

        Uses a longer delay when the latest chunk is near Feishu's ~4096-char
        split point, since a continuation chunk is almost certain.
        """
        current_task = asyncio.current_task()
        try:
            # Adaptive delay: if the latest chunk is near the split threshold,
            # a continuation is almost certain — wait longer.
            pending = self._pending_text_batches.get(key)
            last_len = getattr(pending, "_last_chunk_len", 0) if pending else 0
            if last_len >= self._SPLIT_THRESHOLD:
                delay = self._text_batch_split_delay_seconds
            else:
                delay = self._text_batch_delay_seconds
            await asyncio.sleep(delay)
            await self._flush_text_batch_now(key)
        finally:
            if self._pending_text_batch_tasks.get(key) is current_task:
                self._pending_text_batch_tasks.pop(key, None)

    async def _flush_text_batch_now(self, key: str) -> None:
        """Dispatch the current text batch immediately."""
        event = self._pending_text_batches.pop(key, None)
        self._pending_text_batch_counts.pop(key, None)
        if not event:
            return
        logger.info(
            "[Feishu] Flushing text batch %s (%d chars)",
            key,
            len(event.text or ""),
        )
        await self._handle_message_with_guards(event)

    # =========================================================================
    # Message content extraction and resource download
    # =========================================================================

    # =========================================================================
    # Inbound admission
    # =========================================================================

    def _admit(self, sender: Any, message: Any) -> Optional[RejectReason]:
        sender_ids = _sender_identity(sender)
        self_ids = frozenset(v for v in (self._bot_open_id, self._bot_user_id) if v)
        is_bot = _is_bot_sender(sender)
        is_group = getattr(message, "chat_type", "p2p") != "p2p"
        chat_id = getattr(message, "chat_id", "") or ""
        require_mention = is_group and self._require_mention_for(chat_id)

        # Defensive only — Feishu doesn't echo our outbound back as inbound,
        # and open_id is always populated on both sides.
        if self_ids and sender_ids & self_ids:
            return "self_echo"

        if is_bot:
            mode = self._allow_bots
            if mode != "mentions" and mode != "all":
                return "bots_disabled"
            # Defensive: pre-hydration or malformed payloads.
            if not self_ids or not sender_ids:
                return "self_ids_unknown"
            # Step 4 covers mention enforcement for groups when require_mention
            # is on; check here only on paths step 4 won't reach.
            if mode == "mentions" and not require_mention and not self._mentions_self(message):
                return "bot_not_mentioned"

        if not is_group:
            return None

        if not self._allow_group_message(
            getattr(sender, "sender_id", None), chat_id, is_bot=is_bot,
        ):
            return "group_policy_rejected"
        if require_mention and not self._mentions_self(message):
            return "group_policy_rejected"
        return None

    def _require_mention_for(self, chat_id: str) -> bool:
        rule = self._group_rules.get(chat_id) if chat_id else None
        if rule and rule.require_mention is not None:
            return rule.require_mention
        return self._require_mention

    # --- Group policy ---------------------------------------------------------

    def _allow_group_message(
        self,
        sender_id: Any,
        chat_id: str = "",
        *,
        is_bot: bool = False,
    ) -> bool:
        """Per-group policy gate for non-DM traffic."""
        sender_open_id = getattr(sender_id, "open_id", None)
        sender_user_id = getattr(sender_id, "user_id", None)
        sender_ids = {sender_open_id, sender_user_id} - {None}

        if sender_ids and self._admins and (sender_ids & self._admins):
            return True

        rule = self._group_rules.get(chat_id) if chat_id else None
        if rule:
            policy = rule.policy
            allowlist = rule.allowlist
            blacklist = rule.blacklist
        else:
            policy = self._default_group_policy or self._group_policy
            allowlist = self._allowed_group_users
            blacklist = set()

        # Channel locks apply to everyone; allowlist/blacklist only gate humans
        # (bots were already cleared upstream by FEISHU_ALLOW_BOTS).
        if policy == "disabled":
            return False
        if policy == "open":
            return True
        if policy == "admin_only":
            return False
        if is_bot:
            return True

        if policy == "allowlist":
            return bool(sender_ids and (sender_ids & allowlist))
        if policy == "blacklist":
            return bool(sender_ids and not (sender_ids & blacklist))

        return bool(sender_ids and (sender_ids & self._allowed_group_users))

    # --- Mention detection ----------------------------------------------------

    def _mentions_self(self, message: Any) -> bool:
        # @_all is Feishu's @everyone placeholder.
        raw_content = getattr(message, "content", "") or ""
        if "@_all" in raw_content:
            return True
        mentions = getattr(message, "mentions", None) or []
        if mentions and self._message_mentions_bot(mentions):
            return True
        normalized = normalize_feishu_message(
            message_type=getattr(message, "message_type", "") or "",
            raw_content=raw_content,
            mentions=getattr(message, "mentions", None),
            bot=self._bot_identity(),
        )
        return self._post_mentions_bot(normalized.mentions)

    def _message_mentions_bot(self, mentions: List[Any]) -> bool:
        # IDs trump names: when both sides have open_id (or both user_id),
        # match requires equal IDs. Name fallback only when either side
        # lacks an ID.
        for mention in mentions:
            mention_id = getattr(mention, "id", None)
            mention_open_id = (getattr(mention_id, "open_id", None) or "").strip()
            mention_user_id = (getattr(mention_id, "user_id", None) or "").strip()
            mention_name = (getattr(mention, "name", None) or "").strip()

            if mention_open_id and self._bot_open_id:
                if mention_open_id == self._bot_open_id:
                    return True
                continue  # IDs differ — not the bot; skip name fallback.
            if mention_user_id and self._bot_user_id:
                if mention_user_id == self._bot_user_id:
                    return True
                continue
            if self._bot_name and mention_name == self._bot_name:
                return True

        return False

    def _post_mentions_bot(self, mentions: List[FeishuMentionRef]) -> bool:
        return any(m.is_self for m in mentions)

    def _bot_identity(self) -> _FeishuBotIdentity:
        return _FeishuBotIdentity(
            open_id=self._bot_open_id,
            user_id=self._bot_user_id,
            name=self._bot_name,
        )

    async def _hydrate_bot_identity(self) -> None:
        """Best-effort discovery of bot identity for precise group mention gating
        and self-sent bot event filtering.

        Populates ``_bot_open_id`` and ``_bot_name`` from /open-apis/bot/v3/info
        (no extra scopes required beyond the tenant access token). The probe
        always runs when a client is available so stale env vars from app/bot
        migrations do not break group @mention gating. Falls back to the
        application info endpoint for ``_bot_name`` only when the first probe
        doesn't return it. If the probe fails, env-provided values are preserved.
        """
        if not self._client:
            return

        # Primary probe: /open-apis/bot/v3/info — returns bot_name + open_id, no
        # extra scopes required. This is the same endpoint the onboarding wizard
        # uses via probe_bot().
        try:
            base_request = _feishu_public_attr("BaseRequest", BaseRequest)
            http_method = _feishu_public_attr("HttpMethod", HttpMethod)
            access_token_type = _feishu_public_attr("AccessTokenType", AccessTokenType)
            req = (
                base_request.builder()
                .http_method(http_method.GET)
                .uri("/open-apis/bot/v3/info")
                .token_types({access_token_type.TENANT})
                .build()
            )
            resp = await asyncio.to_thread(self._client.request, req)
            content = getattr(getattr(resp, "raw", None), "content", None)
            if content:
                payload = json.loads(content)
                _parse_bot_response = _feishu_public_attr("_parse_bot_response")
                parsed = _parse_bot_response(payload) or {}
                open_id = (parsed.get("bot_open_id") or "").strip()
                bot_name = (parsed.get("bot_name") or "").strip()
                if open_id:
                    if self._bot_open_id and self._bot_open_id != open_id:
                        logger.warning(
                            "[Feishu] FEISHU_BOT_OPEN_ID is stale; using /bot/v3/info open_id for group @mention gating."
                        )
                    self._bot_open_id = open_id
                if bot_name:
                    if self._bot_name and self._bot_name != bot_name:
                        logger.info(
                            "[Feishu] FEISHU_BOT_NAME differs from /bot/v3/info; using hydrated bot name for group @mention gating."
                        )
                    self._bot_name = bot_name
        except Exception:
            logger.debug(
                "[Feishu] /bot/v3/info probe failed during hydration",
                exc_info=True,
            )

        # Fallback probe for _bot_name only: application info endpoint. Needs
        # admin:app.info:readonly or application:application:self_manage scope,
        # so it's best-effort.
        if self._bot_name:
            return
        try:
            request = self._build_get_application_request(app_id=self._app_id, lang="en_us")
            response = await asyncio.to_thread(self._client.application.v6.application.get, request)
            if not response or not response.success():
                code = getattr(response, "code", None)
                if code == 99991672:
                    logger.warning(
                        "[Feishu] Unable to hydrate bot name from application info. "
                        "Grant admin:app.info:readonly or application:application:self_manage "
                        "so group @mention gating can resolve the bot name precisely."
                    )
                return
            app = getattr(getattr(response, "data", None), "app", None)
            app_name = (getattr(app, "app_name", None) or "").strip()
            if app_name and not self._bot_name:
                self._bot_name = app_name
        except Exception:
            logger.debug("[Feishu] Failed to hydrate bot name from application info", exc_info=True)

    # =========================================================================
    # Deduplication — seen message ID cache (persistent)
    # =========================================================================

    def _load_seen_message_ids(self) -> None:
        try:
            payload = json.loads(self._dedup_state_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (OSError, json.JSONDecodeError):
            logger.warning("[Feishu] Failed to load persisted dedup state from %s", self._dedup_state_path, exc_info=True)
            return
        seen_data = payload.get("message_ids", {}) if isinstance(payload, dict) else {}
        now = time.time()
        ttl = _FEISHU_DEDUP_TTL_SECONDS
        # Backward-compat: old format stored a plain list of IDs (no timestamps).
        if isinstance(seen_data, list):
            entries: Dict[str, float] = {str(item).strip(): 0.0 for item in seen_data if str(item).strip()}
        elif isinstance(seen_data, dict):
            entries = {}
            for key, value in seen_data.items():
                if not isinstance(key, str) or not key.strip():
                    continue
                try:
                    entries[key] = float(value)
                except (TypeError, ValueError):
                    continue
        else:
            return
        # Filter out TTL-expired entries (entries saved with ts=0.0 are treated as immortal
        # for one migration cycle to avoid nuking old data on first upgrade).
        valid: Dict[str, float] = {
            msg_id: ts for msg_id, ts in entries.items()
            if ts == 0.0 or ttl <= 0 or now - ts < ttl
        }
        # Apply size cap; keep the most recently seen IDs.
        sorted_ids = sorted(valid, key=lambda k: valid[k], reverse=True)[:self._dedup_cache_size]
        self._seen_message_order = list(reversed(sorted_ids))
        self._seen_message_ids = {k: valid[k] for k in sorted_ids}

    def _persist_seen_message_ids(self) -> None:
        try:
            self._dedup_state_path.parent.mkdir(parents=True, exist_ok=True)
            recent = self._seen_message_order[-self._dedup_cache_size:]
            # Save as {msg_id: timestamp} so TTL filtering works across restarts.
            payload = {"message_ids": {k: self._seen_message_ids[k] for k in recent if k in self._seen_message_ids}}
            atomic_json_write(self._dedup_state_path, payload, indent=None)
        except OSError:
            logger.warning("[Feishu] Failed to persist dedup state to %s", self._dedup_state_path, exc_info=True)

    def _is_duplicate(self, message_id: str) -> bool:
        now = time.time()
        ttl = _FEISHU_DEDUP_TTL_SECONDS
        with self._dedup_lock:
            seen_at = self._seen_message_ids.get(message_id)
            if seen_at is not None and (ttl <= 0 or now - seen_at < ttl):
                return True
            # Record with current wall-clock timestamp so TTL works across restarts.
            self._seen_message_ids[message_id] = now
            self._seen_message_order.append(message_id)
            while len(self._seen_message_order) > self._dedup_cache_size:
                stale = self._seen_message_order.pop(0)
                self._seen_message_ids.pop(stale, None)
            self._persist_seen_message_ids()
            return False

    # =========================================================================
    # Outbound payload construction and send pipeline
    # =========================================================================

    # =========================================================================
    # Connection internals — websocket / webhook setup
    # =========================================================================
