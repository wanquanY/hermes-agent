from __future__ import annotations

import asyncio
import dataclasses
import html as _html
import logging
import os
import re
from datetime import datetime, timezone
from typing import Any, Dict, Optional

try:
    from telegram import Message, Update
    from telegram.ext import ContextTypes
    from telegram.constants import ChatType, ParseMode
except ImportError:  # pragma: no cover - optional platform dependency
    Message = Any
    Update = Any
    class _MockContextTypes:
        DEFAULT_TYPE = Any
    ContextTypes = _MockContextTypes
    ChatType = None
    ParseMode = None

from channels.platforms.base import (
    MessageEvent,
    MessageType,
    ProcessingOutcome,
    SUPPORTED_DOCUMENT_TYPES,
    SUPPORTED_IMAGE_DOCUMENT_TYPES,
    SUPPORTED_VIDEO_TYPES,
)
from channels.platforms.telegram import (
    MAX_COMMANDS_PER_SCOPE,
    _TELEGRAM_IMAGE_EXTENSIONS,
    _TELEGRAM_IMAGE_MIME_TO_EXT,
    _escape_mdv2,
    _rich_normalize_linebreaks,
    _strip_mdv2,
    _wrap_markdown_tables,
)

logger = logging.getLogger(__name__)


def _telegram_public_attr(name: str, fallback: Any = None) -> Any:
    import sys

    public_module = sys.modules.get("channels.platforms.telegram")
    if public_module is None:
        return fallback
    return getattr(public_module, name, fallback)


class TelegramInboundMixin:
    def format_message(self, content: str) -> str:
        """
        Convert standard markdown to Telegram MarkdownV2 format.
    
        Protected regions (code blocks, inline code) are extracted first so
        their contents are never modified.  Standard markdown constructs
        (headers, bold, italic, links) are translated to MarkdownV2 syntax,
        and all remaining special characters are escaped.
        """
        if not content:
            return content
    
        placeholders: dict = {}
        counter = [0]
    
        def _ph(value: str) -> str:
            """Stash *value* behind a placeholder token that survives escaping."""
            key = f"\x00PH{counter[0]}\x00"
            counter[0] += 1
            placeholders[key] = value
            return key
    
        text = content
    
        # 0) Rewrite GFM-style pipe tables into Telegram-friendly row groups
        #    before the normal MarkdownV2 conversions run.
        text = _wrap_markdown_tables(text)
    
        # 1) Protect fenced code blocks (``` ... ```)
        #    Per MarkdownV2 spec, \ and ` inside pre/code must be escaped.
        def _protect_fenced(m):
            raw = m.group(0)
            # Split off opening ``` (with optional language) and closing ```
            open_end = raw.index('\n') + 1 if '\n' in raw[3:] else 3
            opening = raw[:open_end]
            body_and_close = raw[open_end:]
            body = body_and_close[:-3]
            body = body.replace('\\', '\\\\').replace('`', '\\`')
            return _ph(opening + body + '```')
    
        text = re.sub(
            r'(```(?:[^\n]*\n)?[\s\S]*?```)',
            _protect_fenced,
            text,
        )
    
        # 2) Protect inline code (`...`)
        #    Escape \ inside inline code per MarkdownV2 spec.
        text = re.sub(
            r'(`[^`]+`)',
            lambda m: _ph(m.group(0).replace('\\', '\\\\')),
            text,
        )
    
        # 3) Convert markdown links – escape the display text; inside the URL
        #    only ')' and '\' need escaping per the MarkdownV2 spec.
        def _convert_link(m):
            display = _escape_mdv2(m.group(1))
            url = m.group(2).replace('\\', '\\\\').replace(')', '\\)')
            return _ph(f'[{display}]({url})')
    
        text = re.sub(r'\[([^\]]+)\]\(([^()]*(?:\([^()]*\)[^()]*)*)\)', _convert_link, text)
    
        # 4) Convert markdown headers (## Title) → bold *Title*
        def _convert_header(m):
            inner = m.group(1).strip()
            # Strip redundant bold markers that may appear inside a header
            inner = re.sub(r'\*\*(.+?)\*\*', r'\1', inner)
            return _ph(f'*{_escape_mdv2(inner)}*')
    
        text = re.sub(
            r'^#{1,6}\s+(.+)$', _convert_header, text, flags=re.MULTILINE
        )
    
        # 5) Convert bold: **text** → *text* (MarkdownV2 bold)
        text = re.sub(
            r'\*\*(.+?)\*\*',
            lambda m: _ph(f'*{_escape_mdv2(m.group(1))}*'),
            text,
        )
    
        # 6) Convert italic: *text* (single asterisk) → _text_ (MarkdownV2 italic)
        #    [^*\n]+ prevents matching across newlines (which would corrupt
        #    bullet lists using * markers and multi-line content).
        text = re.sub(
            r'\*([^*\n]+)\*',
            lambda m: _ph(f'_{_escape_mdv2(m.group(1))}_'),
            text,
        )
    
        # 7) Convert strikethrough: ~~text~~ → ~text~ (MarkdownV2)
        text = re.sub(
            r'~~(.+?)~~',
            lambda m: _ph(f'~{_escape_mdv2(m.group(1))}~'),
            text,
        )
    
        # 8) Convert spoiler: ||text|| → ||text|| (protect from | escaping)
        text = re.sub(
            r'\|\|(.+?)\|\|',
            lambda m: _ph(f'||{_escape_mdv2(m.group(1))}||'),
            text,
        )
    
        # 9) Convert blockquotes: > at line start → protect > from escaping
        #    Handle both regular blockquotes (> text) and expandable blockquotes
        #    (Telegram MarkdownV2: **> for expandable start, || to end the quote)
        def _convert_blockquote(m):
            prefix = m.group(1)  # >, >>, >>>, **>, or **>> etc.
            content = m.group(2)
            # Check if content ends with || (expandable blockquote end marker)
            # In this case, preserve the trailing || unescaped for Telegram
            if prefix.startswith('**') and content.endswith('||'):
                return _ph(f'{prefix} {_escape_mdv2(content[:-2])}||')
            return _ph(f'{prefix} {_escape_mdv2(content)}')
    
        text = re.sub(
            r'^((?:\*\*)?>{1,3}) (.+)$',
            _convert_blockquote,
            text,
            flags=re.MULTILINE,
        )
    
        # 10) Escape remaining special characters in plain text
        text = _escape_mdv2(text)
    
        # 11) Restore placeholders in reverse insertion order so that
        #    nested references (a placeholder inside another) resolve correctly.
        for key in reversed(list(placeholders.keys())):
            text = text.replace(key, placeholders[key])
    
        # 12) Safety net: escape unescaped ( ) { } that slipped through
        #     placeholder processing.  Split the text into code/non-code
        #     segments so we never touch content inside ``` or ` spans.
        _code_split = re.split(r'(```[\s\S]*?```|`[^`]+`)', text)
        _safe_parts = []
        for _idx, _seg in enumerate(_code_split):
            if _idx % 2 == 1:
                # Inside code span/block — leave untouched
                _safe_parts.append(_seg)
            else:
                # Outside code — escape bare ( ) { }
                def _esc_bare(m, _seg=_seg):
                    s = m.start()
                    ch = m.group(0)
                    # Already escaped
                    if s > 0 and _seg[s - 1] == '\\':
                        return ch
                    # ( that opens a MarkdownV2 link [text](url)
                    if ch == '(' and s > 0 and _seg[s - 1] == ']':
                        return ch
                    # ) that closes a link URL
                    if ch == ')':
                        before = _seg[:s]
                        if '](http' in before or '](' in before:
                            # Check depth
                            depth = 0
                            for j in range(s - 1, max(s - 2000, -1), -1):
                                if _seg[j] == '(':
                                    depth -= 1
                                    if depth < 0:
                                        if j > 0 and _seg[j - 1] == ']':
                                            return ch
                                        break
                                elif _seg[j] == ')':
                                    depth += 1
                    return '\\' + ch
                _safe_parts.append(re.sub(r'[(){}]', _esc_bare, _seg))
        text = ''.join(_safe_parts)
    
        return text
    
    def _telegram_require_mention(self) -> bool:
        """Return whether group chats should require an explicit bot trigger."""
        configured = self.config.extra.get("require_mention")
        if configured is not None:
            if isinstance(configured, str):
                return configured.lower() in {"true", "1", "yes", "on"}
            return bool(configured)
        return os.getenv("TELEGRAM_REQUIRE_MENTION", "false").lower() in {"true", "1", "yes", "on"}
    
    def _telegram_observe_unmentioned_group_messages(self) -> bool:
        """Return whether skipped unmentioned group messages are stored as context.
    
        When enabled with ``require_mention``, Telegram matches the Yuanbao /
        OpenClaw-style group UX: observe ordinary group chatter in the session
        transcript, but only dispatch the agent when the bot is explicitly
        addressed.
        """
        configured = self.config.extra.get("observe_unmentioned_group_messages")
        if configured is None:
            configured = self.config.extra.get("ingest_unmentioned_group_messages")
        if configured is not None:
            if isinstance(configured, str):
                return configured.lower() in {"true", "1", "yes", "on"}
            return bool(configured)
        return os.getenv("TELEGRAM_OBSERVE_UNMENTIONED_GROUP_MESSAGES", "false").lower() in {"true", "1", "yes", "on"}
    
    def _telegram_guest_mode(self) -> bool:
        """Return whether non-allowlisted groups may trigger via direct @mention."""
        configured = self.config.extra.get("guest_mode")
        if configured is not None:
            if isinstance(configured, str):
                return configured.lower() in {"true", "1", "yes", "on"}
            return bool(configured)
        return os.getenv("TELEGRAM_GUEST_MODE", "false").lower() in {"true", "1", "yes", "on"}
    
    def _telegram_exclusive_bot_mentions(self) -> bool:
        """Return whether explicit @...bot mentions exclusively route group messages."""
        configured = self.config.extra.get("exclusive_bot_mentions")
        if configured is not None:
            if isinstance(configured, str):
                return configured.lower() in {"true", "1", "yes", "on"}
            return bool(configured)
        return os.getenv("TELEGRAM_EXCLUSIVE_BOT_MENTIONS", "true").lower() in {"true", "1", "yes", "on"}
    
    def _telegram_free_response_chats(self) -> set[str]:
        raw = self.config.extra.get("free_response_chats")
        if raw is None:
            raw = os.getenv("TELEGRAM_FREE_RESPONSE_CHATS", "")
        if isinstance(raw, list):
            return {str(part).strip() for part in raw if str(part).strip()}
        return {part.strip() for part in str(raw).split(",") if part.strip()}
    
    def _telegram_allowed_chats(self) -> set[str]:
        """Return the whitelist of group/supergroup chat IDs the bot will respond in.
    
        When non-empty, group messages from chats NOT in this set are
        silently ignored unless ``guest_mode`` is enabled and the bot is
        explicitly @mentioned.  DMs are never filtered.
        Empty set means no restriction (fully backward compatible).
        """
        raw = self.config.extra.get("allowed_chats")
        if raw is None:
            raw = os.getenv("TELEGRAM_ALLOWED_CHATS", "")
        if isinstance(raw, list):
            return {str(part).strip() for part in raw if str(part).strip()}
        return {part.strip() for part in str(raw).split(",") if part.strip()}
    
    def _telegram_group_allowed_chats(self) -> set[str]:
        """Return Telegram chats authorized at group scope."""
        raw = self.config.extra.get("group_allowed_chats")
        if raw is None:
            raw = os.getenv("TELEGRAM_GROUP_ALLOWED_CHATS", "")
        if isinstance(raw, list):
            return {str(part).strip() for part in raw if str(part).strip()}
        return {part.strip() for part in str(raw).split(",") if part.strip()}
    
    def _telegram_observe_allowed_chats(self) -> set[str]:
        """Chats where observed group context may use a shared source.
    
        ``group_allowed_chats`` is the gateway authorization allowlist for
        user-less group sources.  ``allowed_chats`` remains an optional response
        gate; when set, observed context must satisfy both lists.
        """
        group_allowed = self._telegram_group_allowed_chats()
        if not group_allowed:
            return set()
        response_allowed = self._telegram_allowed_chats()
        if response_allowed:
            return group_allowed & response_allowed
        return group_allowed
    
    def _telegram_allowed_topics(self) -> set[str]:
        """Return the whitelist of Telegram forum topic IDs this bot handles.
    
        When non-empty, group/supergroup messages from other topics are
        silently ignored. DMs are never filtered by topic. Telegram may omit
        ``message_thread_id`` for the forum General topic, so ``None`` is
        treated as topic ``1`` for matching purposes.
        """
        raw = self.config.extra.get("allowed_topics")
        if raw is None:
            raw = os.getenv("TELEGRAM_ALLOWED_TOPICS", "")
        if isinstance(raw, list):
            return {str(part).strip() for part in raw if str(part).strip()}
        return {part.strip() for part in str(raw).split(",") if part.strip()}
    
    def _telegram_ignored_threads(self) -> set[int]:
        raw = self.config.extra.get("ignored_threads")
        if raw is None:
            raw = os.getenv("TELEGRAM_IGNORED_THREADS", "")
    
        if isinstance(raw, list):
            values = raw
        else:
            values = str(raw).split(",")
    
        ignored: set[int] = set()
        for value in values:
            text = str(value).strip()
            if not text:
                continue
            try:
                ignored.add(int(text))
            except (TypeError, ValueError):
                logger.warning("[%s] Ignoring invalid Telegram thread id: %r", self.name, value)
        return ignored
    
    def _compile_mention_patterns(self) -> List[re.Pattern]:
        """Compile optional regex wake-word patterns for group triggers."""
        patterns = self.config.extra.get("mention_patterns")
        if patterns is None:
            raw = os.getenv("TELEGRAM_MENTION_PATTERNS", "").strip()
            if raw:
                try:
                    loaded = json.loads(raw)
                except Exception:
                    loaded = [part.strip() for part in raw.splitlines() if part.strip()]
                    if not loaded:
                        loaded = [part.strip() for part in raw.split(",") if part.strip()]
                patterns = loaded
    
        if patterns is None:
            return []
        if isinstance(patterns, str):
            patterns = [patterns]
        if not isinstance(patterns, list):
            logger.warning(
                "[%s] telegram mention_patterns must be a list or string; got %s",
                self.name,
                type(patterns).__name__,
            )
            return []
    
        compiled: List[re.Pattern] = []
        for pattern in patterns:
            if not isinstance(pattern, str) or not pattern.strip():
                continue
            try:
                compiled.append(re.compile(pattern, re.IGNORECASE))
            except re.error as exc:
                logger.warning("[%s] Invalid Telegram mention pattern %r: %s", self.name, pattern, exc)
        if compiled:
            logger.info("[%s] Loaded %d Telegram mention pattern(s)", self.name, len(compiled))
        return compiled
    
    def _is_group_chat(self, message: Message) -> bool:
        chat = getattr(message, "chat", None)
        if not chat:
            return False
        chat_type = str(getattr(chat, "type", "")).split(".")[-1].lower()
        return chat_type in {"group", "supergroup"}
    
    def _is_reply_to_bot(self, message: Message) -> bool:
        if not self._bot or not getattr(message, "reply_to_message", None):
            return False
        reply_user = getattr(message.reply_to_message, "from_user", None)
        return bool(reply_user and getattr(reply_user, "id", None) == getattr(self._bot, "id", None))
    
    @staticmethod
    def _extract_bot_mention_usernames(message: Message) -> set[str]:
        """Extract explicit Telegram bot usernames mentioned in text/captions.
    
        Telegram bot usernames are 5-32 characters and must end in "bot".
        Entity mentions are authoritative. The raw-text fallback is intentionally narrow so
        entity-less mobile/client variants still work without treating email
        addresses or arbitrary substrings as bot mentions.
        """
        mentioned_bot_usernames: set[str] = set()
    
        def _iter_sources():
            yield getattr(message, "text", None) or "", getattr(message, "entities", None) or []
            yield getattr(message, "caption", None) or "", getattr(message, "caption_entities", None) or []
    
        for source_text, entities in _iter_sources():
            for entity in entities:
                entity_type = str(getattr(entity, "type", "")).split(".")[-1].lower()
                if entity_type not in {"mention", "bot_command"}:
                    continue
                offset = int(getattr(entity, "offset", -1))
                length = int(getattr(entity, "length", 0))
                if offset < 0 or length <= 0:
                    continue
    
                entity_text = source_text[offset:offset + length].strip()
                if entity_type == "mention":
                    handle = entity_text.lstrip("@").lower()
                    if re.fullmatch(r"[a-z0-9_]{2,29}bot", handle, re.IGNORECASE):
                        mentioned_bot_usernames.add(handle)
                    continue
    
                # Telegram emits /cmd@botname as one bot_command entity, not as
                # a separate mention entity. Treat that suffix as an explicit
                # bot address for exclusive multi-bot routing even when the
                # group has require_mention/free-response disabled.
                at_index = entity_text.find("@")
                if at_index < 0:
                    continue
                command_target = entity_text[at_index + 1:].strip().lower()
                if re.fullmatch(r"[a-z0-9_]{2,29}bot", command_target, re.IGNORECASE):
                    mentioned_bot_usernames.add(command_target)
    
        # Entity-less fallback for older/client-specific updates. If Telegram
        # supplied entities for a source, trust them and do not regex-rescue
        # malformed/URL/code spans that the server did not mark as mentions.
        for raw_text, entities in _iter_sources():
            if not raw_text or entities:
                continue
            for match in re.finditer(r"(?i)(?<![A-Za-z0-9_`/])@([A-Za-z0-9_]{2,29}bot)\b", raw_text):
                mentioned_bot_usernames.add(match.group(1).lower())
    
        return mentioned_bot_usernames
    
    def _message_mentions_bot(self, message: Message) -> bool:
        if not self._bot:
            return False
    
        bot_username = (getattr(self._bot, "username", None) or "").lstrip("@").lower()
        bot_id = getattr(self._bot, "id", None)
        expected = f"@{bot_username}" if bot_username else None
    
        def _iter_sources():
            yield getattr(message, "text", None) or "", getattr(message, "entities", None) or []
            yield getattr(message, "caption", None) or "", getattr(message, "caption_entities", None) or []
    
        # Telegram parses mentions server-side and emits MessageEntity objects
        # (type=mention for @username, type=text_mention for @FirstName targeting
        # a user without a public username). Those entities are authoritative:
        # raw substring matches like "foo@hermes_bot.example" are not mentions
        # (bug #12545). Entities also correctly handle @handles inside URLs, code
        # blocks, and quoted text, where a regex scan would over-match.
        for source_text, entities in _iter_sources():
            for entity in entities:
                entity_type = str(getattr(entity, "type", "")).split(".")[-1].lower()
                if entity_type == "mention" and expected:
                    offset = int(getattr(entity, "offset", -1))
                    length = int(getattr(entity, "length", 0))
                    if offset < 0 or length <= 0:
                        continue
                    if source_text[offset:offset + length].strip().lower() == expected:
                        return True
                elif entity_type == "text_mention":
                    user = getattr(entity, "user", None)
                    if user and getattr(user, "id", None) == bot_id:
                        return True
                elif entity_type == "bot_command" and expected:
                    # Telegram's official group-disambiguation form for slash
                    # commands (``/cmd@botname``) is emitted as a single
                    # ``bot_command`` entity covering the whole span — there
                    # is no accompanying ``mention`` entity. Treat it as a
                    # direct address to this bot when the ``@botname`` suffix
                    # matches. This is the form Telegram's own command menu
                    # autocomplete produces in groups, so dropping it at the
                    # mention gate would break /new, /reset, /help, ... for
                    # every group that has ``require_mention`` enabled (#15415).
                    offset = int(getattr(entity, "offset", -1))
                    length = int(getattr(entity, "length", 0))
                    if offset < 0 or length <= 0:
                        continue
                    command_text = source_text[offset:offset + length]
                    at_index = command_text.find("@")
                    if at_index < 0:
                        continue
                    if command_text[at_index:].strip().lower() == expected:
                        return True
        if bot_username and re.fullmatch(r"[a-z0-9_]{2,29}bot", bot_username, re.IGNORECASE):
            return bot_username in self._extract_bot_mention_usernames(message)
        return False
    
    def _explicit_bot_mentions_exclude_self(self, message: Message) -> bool:
        """Return True when explicit bot handles target other bots, not this one.
    
        Telegram groups can contain several Hermes bot profiles. A message like
        ``@bot3 hi @bot4`` must not wake ``@bot1`` through reply/wake-word
        fallbacks. Treat explicit bot-handle mentions as an exclusive routing
        hint: if at least one @...bot username is present and none matches this
        adapter's own bot username, this adapter should ignore the message.
    
        MessageEntity values are preferred, but some Telegram clients expose
        selected bot handles as plain text in group messages. The raw-text
        fallback is intentionally limited to usernames ending in "bot", which
        Telegram requires for bot accounts.
        """
        if not self._bot:
            return False
    
        bot_username = (getattr(self._bot, "username", None) or "").lstrip("@").lower()
        if not bot_username:
            return False
    
        mentioned_bot_usernames = self._extract_bot_mention_usernames(message)
        return bool(mentioned_bot_usernames) and bot_username not in mentioned_bot_usernames
    
    def _message_matches_mention_patterns(self, message: Message) -> bool:
        if not self._mention_patterns:
            return False
        for candidate in (getattr(message, "text", None), getattr(message, "caption", None)):
            if not candidate:
                continue
            for pattern in self._mention_patterns:
                if pattern.search(candidate):
                    return True
        return False
    
    def _is_guest_mention(self, message: Message) -> bool:
        """Return True for the narrow guest-mode bypass: explicit bot mention.
    
        The caller (:meth:`_should_process_message`) has already verified
        the message is a group chat, so that check is not repeated here.
        """
        return self._telegram_guest_mode() and self._message_mentions_bot(message)
    
    def _clean_bot_trigger_text(self, text: Optional[str]) -> Optional[str]:
        if not text or not self._bot or not getattr(self._bot, "username", None):
            return text
        username = re.escape(self._bot.username)
        cleaned = re.sub(rf"(?i)@{username}\b[,:\-]*\s*", "", text).strip()
        return cleaned or text
    
    def _should_observe_unmentioned_group_message(self, message: Message) -> bool:
        """Return True when a group message should be stored but not dispatched."""
        if not self._telegram_observe_unmentioned_group_messages():
            return False
        if not self._is_group_chat(message):
            return False
    
        thread_id = getattr(message, "message_thread_id", None)
        allowed_topics = self._telegram_allowed_topics()
        if allowed_topics:
            topic_id = str(thread_id) if thread_id is not None else self._GENERAL_TOPIC_THREAD_ID
            if topic_id not in allowed_topics:
                return False
    
        if thread_id is not None:
            try:
                if int(thread_id) in self._telegram_ignored_threads():
                    return False
            except (TypeError, ValueError):
                return False
    
        chat_id_str = str(getattr(getattr(message, "chat", None), "id", ""))
        if self._telegram_exclusive_bot_mentions() and self._explicit_bot_mentions_exclude_self(message):
            return False
    
        allowed = self._telegram_observe_allowed_chats()
        # Observed context is shared at chat/topic scope so a later trigger from
        # another user can see it.  Require an explicit chat allowlist; that
        # keeps shared observed history limited to operator-approved groups and
        # lets gateway authorization pass even after the shared session source
        # drops the per-sender user_id.
        if not allowed or chat_id_str not in allowed:
            return False
    
        # Only observe messages skipped by the require_mention gate.  If the
        # message would be processed normally, let the dispatcher handle it;
        # if require_mention is disabled, every group message is a request.
        if chat_id_str in self._telegram_free_response_chats():
            return False
        if not self._telegram_require_mention():
            return False
        if self._is_reply_to_bot(message):
            return False
        if self._message_mentions_bot(message):
            return False
        if self._message_matches_mention_patterns(message):
            return False
        return True
    
    def _telegram_group_observe_shared_source(self, source):
        """Return a chat/topic-scoped source for observed Telegram group context."""
        return dataclasses.replace(source, user_id=None, user_name=None, user_id_alt=None)
    
    def _telegram_group_observe_attributed_text(self, event: MessageEvent) -> str:
        user_id = event.source.user_id or "unknown"
        sender = event.source.user_name or user_id
        return f"[{sender}|{user_id}]\n{event.text or ''}"
    
    def _telegram_group_observe_channel_prompt(self) -> str:
        username = getattr(getattr(self, "_bot", None), "username", None) or "unknown"
        bot_id = getattr(getattr(self, "_bot", None), "id", None) or "unknown"
        return (
            "You are handling a Telegram group chat message.\n"
            f"- Your identity: user_id={bot_id}, @-mention name in this group=@{username}\n"
            "- observed Telegram group context may be provided in a separate context-only block "
            "before the current message; it is not necessarily addressed to you.\n"
            "- Treat only the current new message as a request explicitly directed at you, "
            "and use observed context only when the current message asks for it."
        )
    
    def _apply_telegram_group_observe_attribution(self, event: MessageEvent) -> MessageEvent:
        """Align triggered group turns with observed-history attribution."""
        if not self._telegram_observe_unmentioned_group_messages():
            return event
        raw_message = getattr(event, "raw_message", None)
        if not raw_message or not self._is_group_chat(raw_message):
            return event
        chat_id_str = str(getattr(getattr(raw_message, "chat", None), "id", ""))
        allowed = self._telegram_observe_allowed_chats()
        if not allowed or chat_id_str not in allowed:
            return event
        shared_source = self._telegram_group_observe_shared_source(event.source)
        observe_prompt = self._telegram_group_observe_channel_prompt()
        channel_prompt = f"{event.channel_prompt}\n\n{observe_prompt}" if event.channel_prompt else observe_prompt
        if event.message_type == MessageType.COMMAND:
            return dataclasses.replace(
                event,
                source=shared_source,
                channel_prompt=channel_prompt,
            )
        return dataclasses.replace(
            event,
            text=self._telegram_group_observe_attributed_text(event),
            source=shared_source,
            channel_prompt=channel_prompt,
        )
    
    def _media_message_type(self, msg: Message) -> MessageType:
        """Classify a Telegram media message into a MessageType."""
        if msg.sticker:
            return MessageType.STICKER
        if msg.photo:
            return MessageType.PHOTO
        if msg.video:
            return MessageType.VIDEO
        if msg.audio:
            return MessageType.AUDIO
        if msg.voice:
            return MessageType.VOICE
        return MessageType.DOCUMENT
    
    async def _cache_observed_media(self, msg: Message, event: MessageEvent) -> None:
        """Cache an unmentioned group attachment and annotate the observed text.
    
        Passive group traffic, so downloads are bounded by the same
        ``_max_doc_bytes`` limit as the addressed document path. Oversized or
        unsupported attachments are noted in the transcript without downloading.
        """
        from channels.platforms.base import cache_media_bytes
    
        source, filename, mime, kind = self._observed_media_source(msg)
        if source is None:
            return
    
        max_bytes = getattr(self, "_max_doc_bytes", 20 * 1024 * 1024)
        file_size = getattr(source, "file_size", None)
        try:
            size = int(file_size or 0)
        except (TypeError, ValueError):
            size = 0
        if not (0 < size <= max_bytes):
            limit_mb = max_bytes // (1024 * 1024)
            event.text = self._append_observed_note(
                event.text,
                f"[Observed Telegram attachment too large or unverifiable. Maximum: {limit_mb} MB.]",
            )
            logger.info("[Telegram] Observed group attachment skipped (size=%s)", file_size)
            return
    
        try:
            file_obj = await source.get_file()
            data = bytes(await file_obj.download_as_bytearray())
            if not filename:
                filename = os.path.basename(getattr(file_obj, "file_path", "") or "")
            cached = cache_media_bytes(data, filename=filename, mime_type=mime, default_kind=kind)
        except Exception as exc:
            logger.warning("[Telegram] Failed to cache observed group media: %s", exc, exc_info=True)
            return
    
        if cached is None:
            event.text = self._append_observed_note(
                event.text, "[Observed Telegram attachment: unsupported type, not cached.]"
            )
            return
    
        event.media_urls = [cached.path]
        event.media_types = [cached.media_type]
        if cached.kind == "image":
            event.message_type = MessageType.PHOTO
        elif cached.kind == "video":
            event.message_type = MessageType.VIDEO
        event.text = self._append_observed_note(event.text, cached.context_note())
        logger.info("[Telegram] Cached observed group %s at %s", cached.kind, cached.path)
    
    async def _cache_replied_media(self, msg: Any, event: MessageEvent) -> None:
        """Cache media from the message this turn replies to, if any."""
        from channels.platforms.base import cache_media_bytes
    
        reply_msg = getattr(msg, "reply_to_message", None)
        if reply_msg is None:
            return
        source, filename, mime, kind = self._observed_media_source(reply_msg)
        if source is None:
            return
    
        max_bytes = getattr(self, "_max_doc_bytes", 20 * 1024 * 1024)
        file_size = getattr(source, "file_size", None)
        try:
            size = int(file_size or 0)
        except (TypeError, ValueError):
            size = 0
        if not (0 < size <= max_bytes):
            return
    
        try:
            file_obj = await source.get_file()
            data = bytes(await file_obj.download_as_bytearray())
            if not filename:
                filename = os.path.basename(getattr(file_obj, "file_path", "") or "")
            cached = cache_media_bytes(data, filename=filename, mime_type=mime, default_kind=kind)
        except Exception as exc:
            logger.warning("[Telegram] Failed to cache replied-to media: %s", exc, exc_info=True)
            return
    
        if cached is None:
            return
    
        event.media_urls.append(cached.path)
        event.media_types.append(cached.media_type)
        if len(event.media_urls) == 1:
            if cached.kind == "image":
                event.message_type = MessageType.PHOTO
            elif cached.kind == "video":
                event.message_type = MessageType.VIDEO
        event.text = self._append_observed_note(
            event.text,
            f"[Replied-to {cached.kind} '{cached.display_name}' saved at: {cached.path}]",
        )
        logger.info("[Telegram] Cached replied-to %s at %s", cached.kind, cached.path)
    
    def _observed_media_source(self, msg: Message):
        """Return (telegram_file_source, filename, mime, default_kind) or Nones."""
        if msg.photo:
            return msg.photo[-1], "", "", "image"
        if msg.video:
            return msg.video, "", "video/mp4", "video"
        if msg.voice:
            return msg.voice, "voice.ogg", "audio/ogg", "audio"
        if msg.audio:
            return msg.audio, getattr(msg.audio, "file_name", "") or "", "", "audio"
        if msg.document:
            doc = msg.document
            return doc, doc.file_name or "", (doc.mime_type or "").lower(), None
        return None, "", "", None
    
    @staticmethod
    def _append_observed_note(existing: Optional[str], note: str) -> str:
        if not note:
            return existing or ""
        if not existing:
            return note
        return f"{existing}\n\n{note}"
    
    def _observe_unmentioned_group_message(
        self,
        message: Message,
        msg_type: MessageType,
        update_id: Optional[int] = None,
        event: Optional[MessageEvent] = None,
    ) -> None:
        """Append skipped group chatter to the target session without dispatching."""
        store = getattr(self, "_session_store", None)
        if not store:
            return
        try:
            event = event or self._build_message_event(message, msg_type, update_id=update_id)
            shared_source = self._telegram_group_observe_shared_source(event.source)
            session_entry = store.get_or_create_session(shared_source)
            entry = {
                "role": "user",
                "content": self._telegram_group_observe_attributed_text(event),
                "timestamp": datetime.now(tz=timezone.utc).isoformat(),
                "observed": True,
            }
            if event.message_id:
                entry["message_id"] = str(event.message_id)
            store.append_to_transcript(session_entry.session_id, entry)
            adapter_name = getattr(self, "name", "telegram")
            logger.info(
                "[%s] Telegram group message observed (no bot trigger): chat=%s from=%s",
                adapter_name,
                getattr(getattr(message, "chat", None), "id", "unknown"),
                event.source.user_id or "unknown",
            )
        except Exception as exc:
            adapter_name = getattr(self, "name", "telegram")
            logger.warning("[%s] Failed to observe Telegram group message: %s", adapter_name, exc)
    
    def _should_process_message(self, message: Message, *, is_command: bool = False) -> bool:
        """Apply Telegram group trigger rules.
    
        DMs remain unrestricted. Group/supergroup messages are accepted when:
        - the chat passes the ``allowed_chats`` whitelist (when set), or
          ``guest_mode`` is enabled and the bot is explicitly mentioned
        - the chat is explicitly allowlisted in ``free_response_chats``
        - ``require_mention`` is disabled
        - the message replies to the bot
        - the bot is @mentioned
        - the text/caption matches a configured regex wake-word pattern
    
        When ``allowed_chats`` is non-empty, it remains a hard gate except for
        the narrow ``guest_mode`` bypass: group/supergroup messages that
        explicitly @mention this bot. Replies and regex wake words do not bypass
        ``allowed_chats``. When ``require_mention`` is enabled, slash commands are not given
        special treatment — they must pass the same mention/reply checks
        as any other group message.  Users can still trigger commands via
        the Telegram bot menu (``/command@botname``) or by explicitly
        mentioning the bot (``@botname /command``), both of which are
        recognised as mentions by :meth:`_message_mentions_bot`.
        """
        if not self._is_group_chat(message):
            return True
    
        thread_id = getattr(message, "message_thread_id", None)
        allowed_topics = self._telegram_allowed_topics()
        if allowed_topics:
            topic_id = str(thread_id) if thread_id is not None else self._GENERAL_TOPIC_THREAD_ID
            if topic_id not in allowed_topics:
                return False
    
        # Check ignored_threads first — applies to both groups and DM topics
        if thread_id is not None:
            try:
                if int(thread_id) in self._telegram_ignored_threads():
                    return False
            except (TypeError, ValueError):
                logger.warning("[%s] Ignoring non-numeric Telegram message_thread_id: %r", self.name, thread_id)
    
        if not self._is_group_chat(message):
            # Root DM (non-topic): ignore if ignore_root_dm is configured
            if thread_id is None and self.config.extra.get("ignore_root_dm", False):
                chat_id = str(getattr(getattr(message, "chat", None), "id", ""))
                if not is_command and chat_id in self._dm_topic_chat_ids:
                    return False
            return True
    
        chat_id_str = str(getattr(getattr(message, "chat", None), "id", ""))
    
        if self._telegram_exclusive_bot_mentions() and self._explicit_bot_mentions_exclude_self(message):
            return False
    
        # Resolve guest-mode mention bypass once so _message_mentions_bot
        # is not called redundantly in the normal flow below.
        guest_mention = self._is_guest_mention(message)
    
        # allowed_chats check (whitelist). When set, group messages from chats
        # outside the whitelist are ignored unless guest_mode permits this
        # exact message as an explicit direct mention. DMs are excluded above.
        allowed = self._telegram_allowed_chats()
        if allowed and chat_id_str not in allowed:
            return guest_mention
    
        if guest_mention:
            return True
        if chat_id_str in self._telegram_free_response_chats():
            return True
        if not self._telegram_require_mention():
            return True
        if self._is_reply_to_bot(message):
            return True
        # When guest_mode is True, _is_guest_mention already called
        # _message_mentions_bot above — skip the redundant second call.
        if not self._telegram_guest_mode() and self._message_mentions_bot(message):
            return True
        return self._message_matches_mention_patterns(message)
    
    async def _ensure_forum_commands(self, message) -> None:
        """Lazy-register bot commands for forum supergroups.
    
        Forum topics don't inherit AllGroupChats scope — Telegram resolves
        via BotCommandScopeChat(chat_id).  Register on first message so the
        command menu works in topic views.
        """
        async with self._forum_lock:
            try:
                chat = getattr(message, "chat", None)
                if not chat or not getattr(chat, "is_forum", False):
                    return
                chat_id = int(chat.id)
                if chat_id in self._forum_command_registered:
                    return
                from telegram import BotCommand, BotCommandScopeChat
                from hermes_cli.commands import telegram_menu_commands
                menu_commands, _ = telegram_menu_commands(max_commands=MAX_COMMANDS_PER_SCOPE)
                bot_commands = [BotCommand(name, desc) for name, desc in menu_commands]
                await self._bot.set_my_commands(bot_commands, scope=BotCommandScopeChat(chat_id=chat_id))
                self._forum_command_registered.add(chat_id)
                logger.info("[%s] Lazy-registered %d commands for forum chat %s", self.name, len(bot_commands), chat_id)
            except Exception as e:
                logger.warning("[%s] Forum command lazy-registration failed: %s", self.name, e)
    
    def _effective_update_message(self, update: Update) -> Optional[Message]:
        """Return the message-like payload for normal messages and channel posts.
    
        Telegram exposes channel broadcasts as ``update.channel_post`` rather
        than ``update.message``.  MessageHandler filters can still dispatch
        those updates, so handlers must use ``effective_message`` to avoid
        consuming channel posts without ever building a gateway event.
        """
        return getattr(update, "effective_message", None) or getattr(update, "message", None)
    
    async def _handle_text_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle incoming text messages.
    
        Telegram clients split long messages into multiple updates.  Buffer
        rapid successive text messages from the same user/chat and aggregate
        them into a single MessageEvent before dispatching.
        """
        msg = self._effective_update_message(update)
        if not msg or not msg.text:
            return
        if not self._should_process_message(msg):
            if self._should_observe_unmentioned_group_message(msg):
                self._observe_unmentioned_group_message(msg, MessageType.TEXT, update_id=update.update_id)
            return
        await self._ensure_forum_commands(update.message)
    
        event = self._build_message_event(msg, MessageType.TEXT, update_id=update.update_id)
        event.text = self._clean_bot_trigger_text(event.text)
        await self._cache_replied_media(msg, event)
        event = self._apply_telegram_group_observe_attribution(event)
        self._enqueue_text_event(event)
    
    async def _handle_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle incoming command messages."""
        msg = self._effective_update_message(update)
        if not msg or not msg.text:
            return
        if not self._should_process_message(msg, is_command=True):
            return
        await self._ensure_forum_commands(msg)
    
        event = self._build_message_event(msg, MessageType.COMMAND, update_id=update.update_id)
        event.text = self._clean_bot_trigger_text(event.text)
        await self._cache_replied_media(msg, event)
        event = self._apply_telegram_group_observe_attribution(event)
        await self.handle_message(event)
    
    async def _handle_location_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle incoming location/venue pin messages."""
        msg = self._effective_update_message(update)
        if not msg:
            return
        if not self._should_process_message(msg):
            if self._should_observe_unmentioned_group_message(msg):
                self._observe_unmentioned_group_message(msg, MessageType.LOCATION, update_id=update.update_id)
            return
    
        venue = getattr(msg, "venue", None)
        location = getattr(venue, "location", None) if venue else getattr(msg, "location", None)
    
        if not location:
            return
    
        lat = getattr(location, "latitude", None)
        lon = getattr(location, "longitude", None)
        if lat is None or lon is None:
            return
    
        # Build a text message with coordinates and context
        parts = ["[The user shared a location pin.]"]
        if venue:
            title = getattr(venue, "title", None)
            address = getattr(venue, "address", None)
            if title:
                parts.append(f"Venue: {title}")
            if address:
                parts.append(f"Address: {address}")
        parts.append(f"latitude: {lat}")
        parts.append(f"longitude: {lon}")
        parts.append(f"Map: https://www.google.com/maps/search/?api=1&query={lat},{lon}")
        parts.append("Ask what they'd like to find nearby (restaurants, cafes, etc.) and any preferences.")
    
        event = self._build_message_event(msg, MessageType.LOCATION, update_id=update.update_id)
        event.text = "\n".join(parts)
        event = self._apply_telegram_group_observe_attribution(event)
        await self.handle_message(event)
    
    def _apply_topic_recovery(self, event: MessageEvent) -> None:
        """Hook installed by gateway runtime to recover Telegram topic lanes."""
        return None
    
    def _text_batch_key(self, event: MessageEvent) -> str:
        """Session-scoped key for text message batching.
    
        Applies the installed topic-recovery hook first so DM-topic batches
        coalesce on (and dispatch to) the recovered lane rather than the
        raw inbound ``message_thread_id`` Telegram may have attached.
        """
        from channels.session_identity import build_session_key
        self._apply_topic_recovery(event)
        return build_session_key(
            event.source,
            group_sessions_per_user=self.config.extra.get("group_sessions_per_user", True),
            thread_sessions_per_user=self.config.extra.get("thread_sessions_per_user", False),
        )
    
    def _enqueue_text_event(self, event: MessageEvent) -> None:
        """Buffer a text event and reset the flush timer.
    
        When Telegram splits a long user message into multiple updates,
        they arrive within a few hundred milliseconds.  This method
        concatenates them and waits for a short quiet period before
        dispatching the combined message.
        """
        key = self._text_batch_key(event)
        existing = self._pending_text_batches.get(key)
        chunk_len = len(event.text or "")
        if existing is None:
            event._last_chunk_len = chunk_len  # type: ignore[attr-defined]
            self._pending_text_batches[key] = event
        else:
            # Append text from the follow-up chunk
            if event.text:
                existing.text = f"{existing.text}\n{event.text}" if existing.text else event.text
            existing._last_chunk_len = chunk_len  # type: ignore[attr-defined]
            # Merge any media that might be attached
            if event.media_urls:
                existing.media_urls.extend(event.media_urls)
                existing.media_types.extend(event.media_types)
    
        # Cancel any pending flush and restart the timer
        prior_task = self._pending_text_batch_tasks.get(key)
        if prior_task and not prior_task.done():
            prior_task.cancel()
        self._pending_text_batch_tasks[key] = asyncio.create_task(
            self._flush_text_batch(key)
        )
    
    async def _flush_text_batch(self, key: str) -> None:
        """Wait for the quiet period then dispatch the aggregated text.
    
        Uses a longer delay when the latest chunk is near Telegram's 4096-char
        split point, since a continuation chunk is almost certain.
        """
        current_task = asyncio.current_task()
        try:
            # Adaptive delay tiers:
            #  - last chunk ≥ _SPLIT_THRESHOLD: a continuation is almost
            #    certain → wait the longer split delay.
            #  - total accumulated text ≤ _TEXT_BATCH_FAST_LEN (~320 cp):
            #    short message → cap delay at _TEXT_BATCH_FAST_DELAY_S
            #    so the agent sees the text near-instantly.
            #  - total ≤ _TEXT_BATCH_SHORT_LEN (~1024 cp):
            #    medium → cap at _TEXT_BATCH_SHORT_DELAY_S.
            #  - otherwise: use the configured cap.
            # Tiers compose with operator overrides via the env-var-driven
            # ``_text_batch_delay_seconds`` (e.g. an operator who sets the
            # cap below 0.18s gets that lower number on every tier).
            pending = self._pending_text_batches.get(key)
            last_len = getattr(pending, "_last_chunk_len", 0) if pending else 0
            total_len = len(getattr(pending, "text", "") or "") if pending else 0
            if last_len >= self._SPLIT_THRESHOLD:
                delay = self._text_batch_split_delay_seconds
            elif total_len <= self._TEXT_BATCH_FAST_LEN:
                delay = min(self._text_batch_delay_seconds, self._TEXT_BATCH_FAST_DELAY_S)
            elif total_len <= self._TEXT_BATCH_SHORT_LEN:
                delay = min(self._text_batch_delay_seconds, self._TEXT_BATCH_SHORT_DELAY_S)
            else:
                delay = self._text_batch_delay_seconds
            await asyncio.sleep(delay)
            event = self._pending_text_batches.pop(key, None)
            if not event:
                return
            logger.info(
                "[Telegram] Flushing text batch %s (%d chars)",
                key, len(event.text or ""),
            )
            await self.handle_message(event)
        finally:
            if self._pending_text_batch_tasks.get(key) is current_task:
                self._pending_text_batch_tasks.pop(key, None)
    
    def _photo_batch_key(self, event: MessageEvent, msg: Message) -> str:
        """Return a batching key for Telegram photos/albums."""
        from channels.session_identity import build_session_key
        session_key = build_session_key(
            event.source,
            group_sessions_per_user=self.config.extra.get("group_sessions_per_user", True),
            thread_sessions_per_user=self.config.extra.get("thread_sessions_per_user", False),
        )
        media_group_id = getattr(msg, "media_group_id", None)
        if media_group_id:
            return f"{session_key}:album:{media_group_id}"
        return f"{session_key}:photo-burst"
    
    async def _flush_photo_batch(self, batch_key: str) -> None:
        """Send a buffered photo burst/album as a single MessageEvent."""
        current_task = asyncio.current_task()
        try:
            await asyncio.sleep(self._media_batch_delay_seconds)
            event = self._pending_photo_batches.pop(batch_key, None)
            if not event:
                return
            logger.info("[Telegram] Flushing photo batch %s with %d image(s)", batch_key, len(event.media_urls))
            await self.handle_message(event)
        finally:
            if self._pending_photo_batch_tasks.get(batch_key) is current_task:
                self._pending_photo_batch_tasks.pop(batch_key, None)
    
    def _enqueue_photo_event(self, batch_key: str, event: MessageEvent) -> None:
        """Merge photo events into a pending batch and schedule flush."""
        existing = self._pending_photo_batches.get(batch_key)
        if existing is None:
            self._pending_photo_batches[batch_key] = event
        else:
            existing.media_urls.extend(event.media_urls)
            existing.media_types.extend(event.media_types)
            if event.text:
                existing.text = self._merge_caption(existing.text, event.text)
    
        prior_task = self._pending_photo_batch_tasks.get(batch_key)
        if prior_task and not prior_task.done():
            prior_task.cancel()
    
        self._pending_photo_batch_tasks[batch_key] = asyncio.create_task(self._flush_photo_batch(batch_key))
    
    async def _handle_media_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle incoming media messages, downloading images to local cache."""
        if not update.message:
            return
        if not self._should_process_message(update.message):
            if self._should_observe_unmentioned_group_message(update.message):
                _m = update.message
                _observe_type = self._media_message_type(_m)
                _event = self._build_message_event(_m, _observe_type, update_id=update.update_id)
                if _m.caption:
                    _event.text = self._clean_bot_trigger_text(_m.caption)
                await self._cache_observed_media(_m, _event)
                self._observe_unmentioned_group_message(
                    _m, _event.message_type, update_id=update.update_id, event=_event
                )
            return
    
        msg = update.message
    
        msg_type = self._media_message_type(msg)
    
        event = self._build_message_event(msg, msg_type, update_id=update.update_id)
        
        # Add caption as text
        if msg.caption:
            event.text = self._clean_bot_trigger_text(msg.caption)
        
        # Handle stickers: describe via vision tool with caching
        if msg.sticker:
            await self._handle_sticker(msg, event)
            event = self._apply_telegram_group_observe_attribution(event)
            await self.handle_message(event)
            return
    
        # Apply observe attribution after caption is set; sticker is handled above
        # because _handle_sticker overwrites event.text with its vision description.
        event = self._apply_telegram_group_observe_attribution(event)
    
        # Download photo to local image cache so the vision tool can access it
        # even after Telegram's ephemeral file URLs expire (~1 hour).
        if msg.photo:
            try:
                # msg.photo is a list of PhotoSize sorted by size; take the largest
                photo = msg.photo[-1]
                file_obj = await photo.get_file()
                # Download the image bytes directly into memory
                image_bytes = await file_obj.download_as_bytearray()
                # Determine extension from the file path if available
                ext = ".jpg"
                if file_obj.file_path:
                    for candidate in [".png", ".webp", ".gif", ".jpeg", ".jpg"]:
                        if file_obj.file_path.lower().endswith(candidate):
                            ext = candidate
                            break
                # Save to local cache (for vision tool access)
                cached_path = _telegram_public_attr("cache_image_from_bytes")( bytes(image_bytes), ext=ext)
                event.media_urls = [cached_path]
                event.media_types = [f"image/{ext.lstrip('.')}" ]
                logger.info("[Telegram] Cached user photo at %s", cached_path)
                media_group_id = getattr(msg, "media_group_id", None)
                if media_group_id:
                    await self._queue_media_group_event(str(media_group_id), event)
                else:
                    batch_key = self._photo_batch_key(event, msg)
                    self._enqueue_photo_event(batch_key, event)
                return
    
            except Exception as e:
                logger.warning("[Telegram] Failed to cache photo: %s", e, exc_info=True)
    
        # Download voice/audio messages to cache for STT transcription
        if msg.voice:
            try:
                allowed, note = self._telegram_media_size_allowed(msg.voice, "voice message")
                if not allowed:
                    event.text = self._append_observed_note(event.text, note or "")
                    logger.info("[Telegram] Skipped oversized user voice (size=%s)", getattr(msg.voice, "file_size", None))
                    await self.handle_message(event)
                    return
                file_obj = await msg.voice.get_file()
                audio_bytes = await file_obj.download_as_bytearray()
                cached_path = _telegram_public_attr("cache_audio_from_bytes")( bytes(audio_bytes), ext=".ogg")
                event.media_urls = [cached_path]
                event.media_types = ["audio/ogg"]
                logger.info("[Telegram] Cached user voice at %s", cached_path)
            except Exception as e:
                logger.warning("[Telegram] Failed to cache voice: %s", e, exc_info=True)
        elif msg.audio:
            try:
                allowed, note = self._telegram_media_size_allowed(msg.audio, "audio file")
                if not allowed:
                    event.text = self._append_observed_note(event.text, note or "")
                    logger.info("[Telegram] Skipped oversized user audio (size=%s)", getattr(msg.audio, "file_size", None))
                    await self.handle_message(event)
                    return
                file_obj = await msg.audio.get_file()
                audio_bytes = await file_obj.download_as_bytearray()
                cached_path = _telegram_public_attr("cache_audio_from_bytes")( bytes(audio_bytes), ext=".mp3")
                event.media_urls = [cached_path]
                event.media_types = ["audio/mp3"]
                logger.info("[Telegram] Cached user audio at %s", cached_path)
            except Exception as e:
                logger.warning("[Telegram] Failed to cache audio: %s", e, exc_info=True)
    
        elif msg.video:
            try:
                file_obj = await msg.video.get_file()
                video_bytes = await file_obj.download_as_bytearray()
                ext = ".mp4"
                if getattr(file_obj, "file_path", None):
                    for candidate in SUPPORTED_VIDEO_TYPES:
                        if file_obj.file_path.lower().endswith(candidate):
                            ext = candidate
                            break
                cached_path = _telegram_public_attr("cache_video_from_bytes")( bytes(video_bytes), ext=ext)
                event.media_urls = [cached_path]
                event.media_types = [SUPPORTED_VIDEO_TYPES.get(ext, "video/mp4")]
                logger.info("[Telegram] Cached user video at %s", cached_path)
            except Exception as e:
                logger.warning("[Telegram] Failed to cache video: %s", e, exc_info=True)
    
        # Download document files to cache for agent processing
        elif msg.document:
            doc = msg.document
            try:
                # Determine file extension
                ext = ""
                original_filename = doc.file_name or ""
                if original_filename:
                    _, ext = os.path.splitext(original_filename)
                    ext = ext.lower()
    
                # Normalize mime_type for robust comparisons (some clients send
                # uppercase like "IMAGE/PNG").
                doc_mime = (doc.mime_type or "").lower()
    
                # If no extension from filename, reverse-lookup from MIME type
                if not ext and doc_mime:
                    ext = _TELEGRAM_IMAGE_MIME_TO_EXT.get(doc_mime, "")
                    if not ext:
                        mime_to_ext = {v: k for k, v in SUPPORTED_DOCUMENT_TYPES.items()}
                        ext = mime_to_ext.get(doc_mime, "")
    
                # Check file size early so image documents cannot bypass the
                # document size limit by taking the image path.
                if not doc.file_size or doc.file_size > self._max_doc_bytes:
                    limit_mb = self._max_doc_bytes // (1024 * 1024)
                    event.text = (
                        "The document is too large or its size could not be verified. "
                        f"Maximum: {limit_mb} MB."
                    )
                    logger.info("[Telegram] Document too large: %s bytes", doc.file_size)
                    await self.handle_message(event)
                    return
    
                # Telegram may deliver screenshots/photos as documents. If the
                # payload is actually an image, route it through the image cache
                # and batching path instead of rejecting it as a document.
                if ext in _TELEGRAM_IMAGE_EXTENSIONS or doc_mime.startswith("image/"):
                    file_obj = await doc.get_file()
                    image_bytes = await file_obj.download_as_bytearray()
                    image_ext = ext if ext in _TELEGRAM_IMAGE_EXTENSIONS else _TELEGRAM_IMAGE_MIME_TO_EXT.get(doc_mime, ".jpg")
                    try:
                        cached_path = _telegram_public_attr("cache_image_from_bytes")( bytes(image_bytes), ext=image_ext)
                    except ValueError as e:
                        logger.warning("[Telegram] Failed to cache image document: %s", e, exc_info=True)
                        event.text = (
                            f"Image document '{original_filename or doc_mime or ext or 'unknown'}' "
                            "could not be read as an image."
                        )
                        await self.handle_message(event)
                        return
    
                    event.message_type = MessageType.PHOTO
                    event.media_urls = [cached_path]
                    event.media_types = [doc_mime if doc_mime.startswith("image/") else _TELEGRAM_IMAGE_EXT_TO_MIME.get(image_ext, "image/jpeg")]
                    logger.info("[Telegram] Cached user image-document at %s", cached_path)
    
                    media_group_id = getattr(msg, "media_group_id", None)
                    if media_group_id:
                        await self._queue_media_group_event(str(media_group_id), event)
                    else:
                        batch_key = self._photo_batch_key(event, msg)
                        self._enqueue_photo_event(batch_key, event)
                    return
    
                if not ext and doc.mime_type:
                    video_mime_to_ext = {v: k for k, v in SUPPORTED_VIDEO_TYPES.items()}
                    ext = video_mime_to_ext.get(doc.mime_type, "")
    
                if not ext and doc.mime_type:
                    # SUPPORTED_IMAGE_DOCUMENT_TYPES has duplicate values (.jpg + .jpeg
                    # both map to image/jpeg); keep the first ext we encounter.
                    image_mime_to_ext: dict[str, str] = {}
                    for _ext, _mime in SUPPORTED_IMAGE_DOCUMENT_TYPES.items():
                        image_mime_to_ext.setdefault(_mime, _ext)
                    ext = image_mime_to_ext.get(doc.mime_type, "")
    
                if ext in SUPPORTED_VIDEO_TYPES:
                    file_obj = await doc.get_file()
                    video_bytes = await file_obj.download_as_bytearray()
                    cached_path = _telegram_public_attr("cache_video_from_bytes")( bytes(video_bytes), ext=ext)
                    event.media_urls = [cached_path]
                    event.media_types = [SUPPORTED_VIDEO_TYPES[ext]]
                    event.message_type = MessageType.VIDEO
                    logger.info("[Telegram] Cached user video document at %s", cached_path)
                    await self.handle_message(event)
                    return
    
                # NOTE: image-document handling is performed earlier in this
                # function (ext in _TELEGRAM_IMAGE_EXTENSIONS or image/* mime),
                # which returns before reaching here.  Any subsequent
                # ext-in-SUPPORTED_IMAGE_DOCUMENT_TYPES branch would be dead
                # code — the extension sets are identical.
    
                # Check if supported
                if ext not in SUPPORTED_DOCUMENT_TYPES:
                    supported_list = ", ".join(sorted(SUPPORTED_DOCUMENT_TYPES.keys()))
                    event.text = (
                        f"Unsupported document type '{ext or 'unknown'}'. "
                        f"Supported types: {supported_list}"
                    )
                    logger.info("[Telegram] Unsupported document type: %s", ext or "unknown")
                    await self.handle_message(event)
                    return
    
                # Download and cache
                file_obj = await doc.get_file()
                doc_bytes = await file_obj.download_as_bytearray()
                raw_bytes = bytes(doc_bytes)
                cached_path = _telegram_public_attr("cache_document_from_bytes")( raw_bytes, original_filename or f"document{ext}")
                mime_type = SUPPORTED_DOCUMENT_TYPES[ext]
                event.media_urls = [cached_path]
                event.media_types = [mime_type]
                logger.info("[Telegram] Cached user document at %s", cached_path)
    
                # For text files, inject content into event.text (capped at 100 KB)
                MAX_TEXT_INJECT_BYTES = 100 * 1024
                if ext in {".md", ".txt"} and len(raw_bytes) <= MAX_TEXT_INJECT_BYTES:
                    try:
                        text_content = raw_bytes.decode("utf-8")
                        display_name = original_filename or f"document{ext}"
                        display_name = re.sub(r'[^\w.\- ]', '_', display_name)
                        injection = f"[Content of {display_name}]:\n{text_content}"
                        if event.text:
                            event.text = f"{injection}\n\n{event.text}"
                        else:
                            event.text = injection
                    except UnicodeDecodeError:
                        logger.warning(
                            "[Telegram] Could not decode text file as UTF-8, skipping content injection",
                            exc_info=True,
                        )
    
            except Exception as e:
                logger.warning("[Telegram] Failed to cache document: %s", e, exc_info=True)
    
        media_group_id = getattr(msg, "media_group_id", None)
        if media_group_id:
            await self._queue_media_group_event(str(media_group_id), event)
            return
    
        await self.handle_message(event)
    
    async def _queue_media_group_event(self, media_group_id: str, event: MessageEvent) -> None:
        """Buffer Telegram media-group items so albums arrive as one logical event.
    
        Telegram delivers albums as multiple updates with a shared media_group_id.
        If we forward each item immediately, the gateway thinks the second image is a
        new user message and interrupts the first. We debounce briefly and merge the
        attachments into a single MessageEvent.
        """
        existing = self._media_group_events.get(media_group_id)
        if existing is None:
            self._media_group_events[media_group_id] = event
        else:
            existing.media_urls.extend(event.media_urls)
            existing.media_types.extend(event.media_types)
            if event.text:
                existing.text = self._merge_caption(existing.text, event.text)
    
        prior_task = self._media_group_tasks.get(media_group_id)
        if prior_task:
            prior_task.cancel()
    
        self._media_group_tasks[media_group_id] = asyncio.create_task(
            self._flush_media_group_event(media_group_id)
        )
    
    async def _flush_media_group_event(self, media_group_id: str) -> None:
        try:
            await asyncio.sleep(self.MEDIA_GROUP_WAIT_SECONDS)
            event = self._media_group_events.pop(media_group_id, None)
            if event is not None:
                await self.handle_message(event)
        except asyncio.CancelledError:
            return
        finally:
            self._media_group_tasks.pop(media_group_id, None)
    
    async def _handle_sticker(self, msg: Message, event: "MessageEvent") -> None:
        """
        Describe a Telegram sticker via vision analysis, with caching.
    
        For static stickers (WEBP), we download, analyze with vision, and cache
        the description by file_unique_id. For animated/video stickers, we inject
        a placeholder noting the emoji.
        """
        from channels.sticker_cache import (
            get_cached_description,
            cache_sticker_description,
            build_sticker_injection,
            build_animated_sticker_injection,
            STICKER_VISION_PROMPT,
        )
    
        sticker = msg.sticker
        emoji = sticker.emoji or ""
        set_name = sticker.set_name or ""
    
        # Animated and video stickers can't be analyzed as static images
        if sticker.is_animated or sticker.is_video:
            event.text = build_animated_sticker_injection(emoji)
            return
    
        # Check the cache first
        cached = get_cached_description(sticker.file_unique_id)
        if cached:
            event.text = build_sticker_injection(
                cached["description"], cached.get("emoji", emoji), cached.get("set_name", set_name)
            )
            logger.info("[Telegram] Sticker cache hit: %s", sticker.file_unique_id)
            return
    
        # Cache miss -- download and analyze
        try:
            file_obj = await sticker.get_file()
            image_bytes = await file_obj.download_as_bytearray()
            cached_path = _telegram_public_attr("cache_image_from_bytes")( bytes(image_bytes), ext=".webp")
            logger.info("[Telegram] Analyzing sticker at %s", cached_path)
    
            from tools.vision_tools import vision_analyze_tool
            result_json = await vision_analyze_tool(
                image_url=cached_path,
                user_prompt=STICKER_VISION_PROMPT,
            )
            result = json.loads(result_json)
    
            if result.get("success"):
                description = result.get("analysis", "a sticker")
                cache_sticker_description(sticker.file_unique_id, description, emoji, set_name)
                event.text = build_sticker_injection(description, emoji, set_name)
            else:
                # Vision failed -- use emoji as fallback
                event.text = build_sticker_injection(
                    f"a sticker with emoji {emoji}" if emoji else "a sticker",
                    emoji, set_name,
                )
        except Exception as e:
            logger.warning("[Telegram] Sticker analysis error: %s", e, exc_info=True)
            event.text = build_sticker_injection(
                f"a sticker with emoji {emoji}" if emoji else "a sticker",
                emoji, set_name,
            )
    
    def _reload_dm_topics_from_config(self) -> None:
        """Re-read dm_topics from config.yaml and load any new thread_ids into cache.
    
        This allows topics created externally (e.g. by the agent via API) to be
        recognized without a gateway restart.
        """
        try:
            from hermes_constants import get_hermes_home
            config_path = get_hermes_home() / "config.yaml"
            if not config_path.exists():
                return
    
            import yaml as _yaml
            with open(config_path, "r", encoding="utf-8") as f:
                config = _yaml.safe_load(f) or {}
    
            dm_topics = (
                config.get("platforms", {})
                .get("telegram", {})
                .get("extra", {})
                .get("dm_topics", [])
            )
            if not dm_topics:
                # Clear both config and precomputed set when all topics are removed
                self._dm_topics_config = []
                self._dm_topic_chat_ids = set()
                return
    
            # Update in-memory config and cache any new thread_ids
            self._dm_topics_config = dm_topics
            # Rebuild the chat_id set for O(1) root-DM ignore lookup
            self._dm_topic_chat_ids = {
                str(chat_entry["chat_id"]) for chat_entry in dm_topics if "chat_id" in chat_entry
            }
            for chat_entry in dm_topics:
                cid = chat_entry.get("chat_id")
                if not cid:
                    continue
                for t in chat_entry.get("topics", []):
                    tid = t.get("thread_id")
                    name = t.get("name")
                    if tid and name:
                        cache_key = f"{cid}:{name}"
                        if cache_key not in self._dm_topics:
                            self._dm_topics[cache_key] = int(tid)
                            logger.info(
                                "[%s] Hot-loaded DM topic from config: %s -> thread_id=%s",
                                self.name, cache_key, tid,
                            )
        except Exception as e:
            logger.debug("[%s] Failed to reload dm_topics from config: %s", self.name, e)
    
    def _get_dm_topic_info(self, chat_id: str, thread_id: Optional[str]) -> Optional[Dict[str, Any]]:
        """Look up DM topic config by chat_id and thread_id.
    
        Returns the topic config dict (name, skill, etc.) if this thread_id
        matches a known DM topic, or None.
        """
        if not thread_id:
            return None
    
        thread_id_int = int(thread_id)
    
        # Check cached topics first (created by us or loaded at startup)
        for key, cached_tid in self._dm_topics.items():
            if cached_tid == thread_id_int and key.startswith(f"{chat_id}:"):
                topic_name = key.split(":", 1)[1]
                # Find the full config for this topic
                for chat_entry in self._dm_topics_config:
                    if str(chat_entry.get("chat_id")) == chat_id:
                        for t in chat_entry.get("topics", []):
                            if t.get("name") == topic_name:
                                return t
                return {"name": topic_name}
    
        # Not in cache — hot-reload config in case topics were added externally
        self._reload_dm_topics_from_config()
    
        # Check cache again after reload
        for key, cached_tid in self._dm_topics.items():
            if cached_tid == thread_id_int and key.startswith(f"{chat_id}:"):
                topic_name = key.split(":", 1)[1]
                for chat_entry in self._dm_topics_config:
                    if str(chat_entry.get("chat_id")) == chat_id:
                        for t in chat_entry.get("topics", []):
                            if t.get("name") == topic_name:
                                return t
                return {"name": topic_name}
    
        return None
    
    def _cache_dm_topic_from_message(self, chat_id: str, thread_id: str, topic_name: str) -> None:
        """Cache a thread_id -> topic_name mapping discovered from an incoming message."""
        cache_key = f"{chat_id}:{topic_name}"
        if cache_key not in self._dm_topics:
            self._dm_topics[cache_key] = int(thread_id)
            logger.info(
                "[%s] Cached DM topic from message: %s -> thread_id=%s",
                self.name, cache_key, thread_id,
            )
    
    @classmethod
    def _flatten_rich_inline_text(cls, value: Any) -> str:
        """Best-effort plaintext flattener for Bot API rich-message inline nodes."""
        if value is None:
            return ""
        if isinstance(value, str):
            return value
        if isinstance(value, list):
            return "".join(cls._flatten_rich_inline_text(item) for item in value)
        if isinstance(value, dict):
            text = value.get("text")
            if text is not None:
                return cls._flatten_rich_inline_text(text)
            children = value.get("children")
            if children is not None:
                return cls._flatten_rich_inline_text(children)
        return ""
    
    @classmethod
    def _flatten_rich_blocks(cls, blocks: Any) -> str:
        """Best-effort plaintext flattener for Bot API rich-message blocks."""
        if not isinstance(blocks, list):
            return ""
    
        lines: List[str] = []
        for block in blocks:
            if not isinstance(block, dict):
                continue
    
            block_type = block.get("type")
            if block_type == "list":
                for item in block.get("items", []):
                    if not isinstance(item, dict):
                        continue
                    item_text = cls._flatten_rich_blocks(item.get("blocks"))
                    if not item_text:
                        continue
                    label = item.get("label")
                    item_lines = item_text.splitlines()
                    if not item_lines:
                        continue
                    first_line = item_lines[0]
                    if label:
                        first_line = f"{label} {first_line}".strip()
                    lines.append(first_line)
                    lines.extend(item_lines[1:])
                continue
    
            text = cls._flatten_rich_inline_text(block.get("text"))
            if text:
                lines.extend(text.splitlines())
    
        return "\n".join(line.rstrip() for line in lines if line)
    
    @classmethod
    def _extract_rich_reply_text(cls, reply_to_message: Any) -> Optional[str]:
        """Return plaintext echoed by Telegram's rich_message reply payload."""
        try:
            api_kwargs = getattr(reply_to_message, "api_kwargs", None)
            getter = getattr(api_kwargs, "get", None)
            if not callable(getter):
                return None
            rich_message = getter("rich_message")
            rich_getter = getattr(rich_message, "get", None)
            if not callable(rich_getter):
                return None
            text = cls._flatten_rich_blocks(rich_getter("blocks")).strip()
            return text or None
        except Exception:
            return None
    
    def _build_message_event(
        self,
        message: Message,
        msg_type: MessageType,
        update_id: Optional[int] = None,
    ) -> MessageEvent:
        """Build a MessageEvent from a Telegram message.
    
        ``update_id`` is the ``Update.update_id`` from PTB; passing it through
        lets ``/restart`` record the triggering offset so the new gateway
        process can advance past it (prevents ``/restart`` being re-delivered
        when PTB's graceful-shutdown ACK fails).
        """
        chat = message.chat
        user = message.from_user
        
        # Determine chat type.  Normalize through ``str`` so tests/mocks and
        # python-telegram-bot enum values both work (``ChatType.CHANNEL`` is
        # string-like, but mocks often provide plain strings).
        telegram_chat_type = str(getattr(chat, "type", "")).split(".")[-1].lower()
        chat_type = "dm"
        if telegram_chat_type in {"group", "supergroup"}:
            chat_type = "group"
        elif telegram_chat_type == "channel":
            chat_type = "channel"
    
        # Resolve Telegram topic name and skill binding.
        # Only preserve message_thread_id when Telegram marks the message as
        # a real topic/forum message. Telegram can also populate
        # message_thread_id for ordinary reply UI anchors; treating those as
        # durable session threads fragments workflows such as CAPTCHA/login
        # handoffs where the user later replies "done" in the same group.
        # Private chats have the same pitfall: only real DM topic messages
        # (is_topic_message=True) should keep the thread id, otherwise sends
        # can hit Telegram's 'Message thread not found' error (#3206).
        thread_id_raw = message.message_thread_id
        is_topic_message = bool(getattr(message, "is_topic_message", False))
        is_forum_group = getattr(chat, "is_forum", False) is True
        thread_id_str = None
        if thread_id_raw is not None:
            if chat_type == "group" and (is_topic_message or is_forum_group):
                thread_id_str = str(thread_id_raw)
            elif chat_type == "dm" and is_topic_message:
                thread_id_str = str(thread_id_raw)
        # For forum groups without an explicit topic, default to the
        # General-topic id so the gateway routes back to the General topic
        # rather than dropping into the bot's main channel (#22423).
        if chat_type == "group" and thread_id_str is None and is_forum_group:
            thread_id_str = self._GENERAL_TOPIC_THREAD_ID
        chat_topic = None
        topic_skill = None
    
        if chat_type == "dm" and thread_id_str:
            topic_info = self._get_dm_topic_info(str(chat.id), thread_id_str)
            if topic_info:
                chat_topic = topic_info.get("name")
                topic_skill = topic_info.get("skill")
    
            # Also check forum_topic_created service message for topic discovery
            if hasattr(message, "forum_topic_created") and message.forum_topic_created:
                created_name = message.forum_topic_created.name
                if created_name:
                    self._cache_dm_topic_from_message(str(chat.id), thread_id_str, created_name)
                    if not chat_topic:
                        chat_topic = created_name
    
        elif chat_type == "group" and thread_id_str:
            # Group/supergroup forum topic skill binding via config.extra['group_topics']
            group_topics_config: list = self.config.extra.get("group_topics", [])
            for chat_entry in group_topics_config:
                if str(chat_entry.get("chat_id", "")) == str(chat.id):
                    for topic in chat_entry.get("topics", []):
                        tid = topic.get("thread_id")
                        if tid is not None and str(tid) == thread_id_str:
                            chat_topic = topic.get("name")
                            topic_skill = topic.get("skill")
                            break
                    break
    
        # Build source
        source = self.build_source(
            chat_id=str(chat.id),
            chat_name=chat.title or (chat.full_name if hasattr(chat, "full_name") else None),
            chat_type=chat_type,
            user_id=(
                str(user.id)
                if user
                else (str(chat.id) if chat_type in {"dm", "channel"} else None)
            ),
            user_name=(
                user.full_name
                if user
                else (
                    chat.full_name
                    if hasattr(chat, "full_name") and chat_type == "dm"
                    else (chat.title if chat_type == "channel" else None)
                )
            ),
            thread_id=thread_id_str,
            chat_topic=chat_topic,
            message_id=str(message.message_id),
        )
        
        # Extract reply context if this message is a reply.
        # Prefer Telegram's native partial quote (message.quote, TextQuote)
        # so a user replying to a single selected substring of a prior
        # multi-section message doesn't get the whole replied-to message
        # injected into the agent's context — which can cause the agent
        # to act on unrelated actionable-looking text the user didn't
        # quote (#22619). Fall back to the full replied-to message text
        # / caption when no native quote is present.
        reply_to_id = None
        reply_to_text = None
        if message.reply_to_message:
            reply_to_id = str(message.reply_to_message.message_id)
            quote = getattr(message, "quote", None)
            quote_text = getattr(quote, "text", None) if quote is not None else None
            if quote_text:
                reply_to_text = quote_text
            else:
                reply_to_text = (
                    message.reply_to_message.text
                    or message.reply_to_message.caption
                    or None
                )
                if not reply_to_text:
                    # Prefer Telegram's native rich-message echo when present;
                    # keep the local send-time index only as a fallback for
                    # older/unrecoverable reply payloads.
                    reply_to_text = self._extract_rich_reply_text(message.reply_to_message)
                if not reply_to_text:
                    try:
                        from channels import rich_sent_store
                        reply_to_text = rich_sent_store.lookup(
                            str(chat.id), reply_to_id
                        )
                    except Exception:
                        reply_to_text = None
    
        # Per-channel/topic ephemeral prompt
        from channels.platforms.base import resolve_channel_prompt
        _chat_id_str = str(chat.id)
        _channel_prompt = resolve_channel_prompt(
            self.config.extra,
            thread_id_str or _chat_id_str,
            _chat_id_str if thread_id_str else None,
        )
    
        return MessageEvent(
            text=message.text or "",
            message_type=msg_type,
            source=source,
            raw_message=message,
            message_id=str(message.message_id),
            platform_update_id=update_id,
            reply_to_message_id=reply_to_id,
            reply_to_text=reply_to_text,
            auto_skill=topic_skill,
            channel_prompt=_channel_prompt,
            timestamp=message.date,
        )
    
    def _reactions_enabled(self) -> bool:
        """Check if message reactions are enabled via config/env."""
        return os.getenv("TELEGRAM_REACTIONS", "false").lower() not in {"false", "0", "no"}
    
    async def _set_reaction(self, chat_id: str, message_id: str, emoji: str) -> bool:
        """Set a single emoji reaction on a Telegram message."""
        if not self._bot:
            return False
        try:
            await self._bot.set_message_reaction(
                chat_id=int(chat_id),
                message_id=int(message_id),
                reaction=emoji,
            )
            return True
        except Exception as e:
            logger.debug("[%s] set_message_reaction failed (%s): %s", self.name, emoji, e)
            return False
    
    async def _clear_reactions(self, chat_id: str, message_id: str) -> bool:
        """Clear all reactions from a Telegram message.
    
        Calling ``set_message_reaction`` with ``reaction=None`` (or an empty
        sequence) is the documented Bot API way to remove all bot-set
        reactions on a message — equivalent to Bot API 10.0's
        ``deleteMessageReaction`` but supported in PTB 22.6 already.
        """
        if not self._bot:
            return False
        try:
            await self._bot.set_message_reaction(
                chat_id=int(chat_id),
                message_id=int(message_id),
                reaction=None,
            )
            return True
        except Exception as e:
            logger.debug("[%s] clear reactions failed: %s", self.name, e)
            return False
    
    async def on_processing_start(self, event: MessageEvent) -> None:
        """Add an in-progress reaction when message processing begins."""
        if not self._reactions_enabled():
            return
        chat_id = getattr(event.source, "chat_id", None)
        message_id = getattr(event, "message_id", None)
        if chat_id and message_id:
            await self._set_reaction(chat_id, message_id, "\U0001f440")
    
    async def on_processing_complete(self, event: MessageEvent, outcome: ProcessingOutcome) -> None:
        """Swap the in-progress reaction for a final success/failure reaction.
    
        Unlike Discord (additive reactions), Telegram's set_message_reaction
        replaces all existing reactions in one call — no remove step needed.
    
        On CANCELLED outcomes (e.g. the user runs ``/stop``, or a session is
        interrupted mid-flight), we explicitly clear the 👀 in-progress
        reaction so it doesn't linger on the user's message indefinitely.
        Without this clear, the only way to remove the 👀 was to wait for
        another agent run to swap it to 👍/👎 — which never happens if the
        cancellation was the last activity in the chat.
        """
        if not self._reactions_enabled():
            return
        chat_id = getattr(event.source, "chat_id", None)
        message_id = getattr(event, "message_id", None)
        if not (chat_id and message_id):
            return
        if outcome == ProcessingOutcome.CANCELLED:
            await self._clear_reactions(chat_id, message_id)
        else:
            await self._set_reaction(
                chat_id,
                message_id,
                "\U0001f44d" if outcome == ProcessingOutcome.SUCCESS else "\U0001f44e",
            )
