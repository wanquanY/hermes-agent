"""Slack inbound routing, approvals, thread context, and downloads."""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from typing import Any, Dict, List, Optional, Tuple

from channels.config import Platform
from agent.secret_scope import get_profile_env
from channels.platforms.base import (
    MessageEvent,
    MessageType,
    SendResult,
    SUPPORTED_DOCUMENT_TYPES,
    cache_document_from_bytes,
    resolve_channel_prompt,
    resolve_channel_skills,
)
from channels.platforms.slack_blocks import (
    _extract_text_from_slack_blocks,
    _serialize_slack_blocks_for_agent,
)
from channels.platforms.slack_support import _ThreadContextCache, _slash_user_id

logger = logging.getLogger(__name__)


class SlackInboundMixin:
    @staticmethod
    def _scoped_env(name: str) -> str:
        """Read the current profile without a process-global fallback."""
        return str(get_profile_env(name, "") or "").strip()

    def _is_interactive_user_authorized(
        self,
        user_id: str,
        *,
        channel_id: str = "",
        user_name: str = "",
        team_id: str = "",
    ) -> bool:
        normalized_user_id = str(user_id or "").strip()
        if not normalized_user_id:
            return False

        runner = getattr(getattr(self, "_message_handler", None), "__self__", None)
        auth_fn = getattr(runner, "_is_user_authorized", None)
        if callable(auth_fn):
            try:
                from channels.session_identity import SessionSource

                return bool(
                    auth_fn(
                        SessionSource(
                            platform=Platform.SLACK,
                            chat_id=str(channel_id or normalized_user_id),
                            chat_type=(
                                "dm" if str(channel_id).startswith("D") else "group"
                            ),
                            user_id=normalized_user_id,
                            user_name=user_name or None,
                            scope_id=team_id or None,
                        )
                    )
                )
            except Exception:
                logger.debug(
                    "[Slack] Falling back to scoped allowlist auth for %s",
                    normalized_user_id,
                    exc_info=True,
                )

        allowed_ids: set[str] = set()
        configured = getattr(self.config, "extra", {}).get("allow_from", [])
        if isinstance(configured, str):
            configured = configured.split(",")
        allowed_ids.update(
            str(part).strip() for part in configured if str(part).strip()
        )
        for name in ("SLACK_ALLOWED_USERS", "GATEWAY_ALLOWED_USERS"):
            raw = self._scoped_env(name)
            allowed_ids.update(part.strip() for part in raw.split(",") if part.strip())
        if allowed_ids:
            return "*" in allowed_ids or normalized_user_id in allowed_ids
        return any(
            self._scoped_env(name).lower() in {"true", "1", "yes"}
            for name in ("SLACK_ALLOW_ALL_USERS", "GATEWAY_ALLOW_ALL_USERS")
        )

    def _assistant_thread_key(self, channel_id: str, thread_ts: str) -> Optional[Tuple[str, str]]:
        """Return a stable cache key for Slack assistant thread metadata."""
        if not channel_id or not thread_ts:
            return None
        return (str(channel_id), str(thread_ts))

    def _extract_assistant_thread_metadata(self, event: dict) -> Dict[str, str]:
        """Extract Slack Assistant thread identity data from an event payload."""
        assistant_thread = event.get("assistant_thread") or {}
        context = assistant_thread.get("context") or event.get("context") or {}

        channel_id = (
            assistant_thread.get("channel_id")
            or event.get("channel")
            or context.get("channel_id")
            or ""
        )
        thread_ts = (
            assistant_thread.get("thread_ts")
            or event.get("thread_ts")
            or event.get("message_ts")
            or ""
        )
        user_id = (
            assistant_thread.get("user_id")
            or event.get("user")
            or context.get("user_id")
            or ""
        )
        team_id = (
            event.get("team")
            or event.get("team_id")
            or assistant_thread.get("team_id")
            or ""
        )
        context_channel_id = context.get("channel_id") or ""

        return {
            "channel_id": str(channel_id) if channel_id else "",
            "thread_ts": str(thread_ts) if thread_ts else "",
            "user_id": str(user_id) if user_id else "",
            "team_id": str(team_id) if team_id else "",
            "context_channel_id": str(context_channel_id) if context_channel_id else "",
        }

    def _cache_assistant_thread_metadata(self, metadata: Dict[str, str]) -> None:
        """Remember assistant thread identity data for later message events."""
        channel_id = metadata.get("channel_id", "")
        thread_ts = metadata.get("thread_ts", "")
        key = self._assistant_thread_key(channel_id, thread_ts)
        if not key:
            return

        existing = self._assistant_threads.get(key, {})
        merged = dict(existing)
        merged.update({k: v for k, v in metadata.items() if v})
        self._assistant_threads[key] = merged

        # Evict oldest entries when the cache exceeds the limit
        if len(self._assistant_threads) > self._ASSISTANT_THREADS_MAX:
            excess = len(self._assistant_threads) - self._ASSISTANT_THREADS_MAX // 2
            for old_key in list(self._assistant_threads)[:excess]:
                del self._assistant_threads[old_key]

        team_id = merged.get("team_id", "")
        if team_id and channel_id:
            self._channel_team[channel_id] = team_id

    def _lookup_assistant_thread_metadata(
        self,
        event: dict,
        channel_id: str = "",
        thread_ts: str = "",
    ) -> Dict[str, str]:
        """Load cached assistant-thread metadata that matches the current event."""
        metadata = self._extract_assistant_thread_metadata(event)
        if channel_id and not metadata.get("channel_id"):
            metadata["channel_id"] = channel_id
        if thread_ts and not metadata.get("thread_ts"):
            metadata["thread_ts"] = thread_ts

        key = self._assistant_thread_key(
            metadata.get("channel_id", ""),
            metadata.get("thread_ts", ""),
        )
        cached = self._assistant_threads.get(key, {}) if key else {}
        if cached:
            merged = dict(cached)
            merged.update({k: v for k, v in metadata.items() if v})
            return merged
        return metadata

    def _seed_assistant_thread_session(self, metadata: Dict[str, str]) -> None:
        """Prime the session store so assistant threads get stable user scoping."""
        session_store = getattr(self, "_session_store", None)
        if not session_store:
            return

        channel_id = metadata.get("channel_id", "")
        thread_ts = metadata.get("thread_ts", "")
        user_id = metadata.get("user_id", "")
        if not channel_id or not thread_ts or not user_id:
            return

        source = self.build_source(
            chat_id=channel_id,
            chat_name=channel_id,
            chat_type="dm",
            user_id=user_id,
            thread_id=thread_ts,
            chat_topic=metadata.get("context_channel_id") or None,
        )

        try:
            session_store.get_or_create_session(source)
        except Exception:
            logger.debug(
                "[Slack] Failed to seed assistant thread session for %s/%s",
                channel_id,
                thread_ts,
                exc_info=True,
            )

    async def _handle_assistant_thread_lifecycle_event(self, event: dict) -> None:
        """Handle Slack Assistant lifecycle events that carry user/thread identity."""
        metadata = self._extract_assistant_thread_metadata(event)
        self._cache_assistant_thread_metadata(metadata)
        self._seed_assistant_thread_session(metadata)

    async def _handle_slack_message(self, event: dict) -> None:
        """Handle an incoming Slack message event."""
        # Dedup: Slack Socket Mode can redeliver events after reconnects (#4777)
        event_ts = event.get("ts", "")
        if event_ts and self._dedup.is_duplicate(event_ts):
            return

        # Bot message filtering (SLACK_ALLOW_BOTS / config allow_bots):
        #   "none"     — ignore all bot messages (default, backward-compatible)
        #   "mentions" — accept bot messages only when they @mention us
        #   "all"      — accept all bot messages (except our own)
        if event.get("bot_id") or event.get("subtype") == "bot_message":
            allow_bots = self.config.extra.get("allow_bots", "")
            if not allow_bots:
                allow_bots = get_profile_env("SLACK_ALLOW_BOTS", "none")
            allow_bots = str(allow_bots).lower().strip()
            if allow_bots == "none":
                return
            elif allow_bots == "mentions":
                text_check = event.get("text", "")
                if self._bot_user_id and f"<@{self._bot_user_id}>" not in text_check:
                    return
            # "all" falls through to process the message
            # Always ignore our own messages to prevent echo loops
            msg_user = event.get("user", "")
            if msg_user and self._bot_user_id and msg_user == self._bot_user_id:
                return

        # Ignore message edits and deletions
        subtype = event.get("subtype")
        if subtype in {"message_changed", "message_deleted"}:
            return

        original_text = event.get("text", "")

        # Slack blocks native slash commands inside threads ("/queue is not
        # supported in threads. Sorry!").  As a workaround, recognise a
        # leading ``!`` as an alternate command prefix and rewrite it to
        # ``/`` so the rest of the pipeline (MessageType.COMMAND tagging,
        # gateway dispatcher) handles it like a normal slash command.  Only
        # rewrite when the first token resolves to a known gateway command
        # so casual messages like "!nice work" pass through unchanged.
        if original_text.startswith("!"):
            try:
                from hermes_cli.commands import is_gateway_known_command
                first_token = original_text[1:].split(maxsplit=1)[0]
                # Strip "@suffix" the same way get_command() does, so
                # forms like ``!stop@hermes`` still resolve.
                cmd_name = first_token.split("@", 1)[0].lower()
                if cmd_name and "/" not in cmd_name and is_gateway_known_command(cmd_name):
                    original_text = "/" + original_text[1:]
            except Exception:  # pragma: no cover - defensive
                pass

        text = original_text

        # Extract quoted/forwarded content from Slack blocks.
        # Slack's modern composer embeds forwarded messages in the ``blocks``
        # array as ``rich_text_quote`` elements, which are NOT reflected in
        # the plain ``text`` field.  Merge block text so the agent sees the
        # full message content.
        blocks = event.get("blocks")
        if blocks:
            blocks_text = _extract_text_from_slack_blocks(blocks)
            if blocks_text:
                # Only append if the blocks contain text not already present
                # in the plain text field (avoids duplication).
                stripped_blocks = blocks_text.strip()
                if stripped_blocks and stripped_blocks not in text.strip():
                    logger.debug(
                        "Slack: extracted additional text from blocks "
                        "(likely quoted/forwarded content): %s",
                        stripped_blocks[:300],
                    )
                    text = (text.strip() + "\n" + stripped_blocks).strip()

            blocks_payload = _serialize_slack_blocks_for_agent(blocks)
            if blocks_payload:
                text = (text.strip() + "\n\n" + blocks_payload).strip()

        # Extract link unfurls / rich attachments (e.g. Notion previews).
        # Slack places unfurled link previews in the ``attachments`` array with
        # fields like title, title_link/from_url, text, footer, and fallback.
        # Without reading these, the agent never sees shared link previews.
        slack_attachments = event.get("attachments") or []
        if slack_attachments:
            att_parts: list[str] = []
            for att in slack_attachments:
                att_title = att.get("title", "")
                att_url = att.get("title_link", "") or att.get("from_url", "")
                att_text = att.get("text", "")
                att_footer = att.get("footer", "")
                att_fallback = att.get("fallback", "")

                # Skip message-type attachments (e.g. Slack bot messages with
                # is_msg_unfurl) to avoid echoing our own content.
                if att.get("is_msg_unfurl"):
                    continue

                # Build a readable representation.
                if att_title and att_url:
                    header = f"📎 [{att_title}]({att_url})"
                elif att_title:
                    header = f"📎 {att_title}"
                elif att_url:
                    header = f"📎 {att_url}"
                else:
                    header = None

                # Prefer preview text, fall back to fallback description.
                body = att_text or att_fallback or ""
                if body:
                    body = body.strip()
                    if len(body) > 500:
                        body = body[:497] + "..."

                if header and body:
                    section = f"{header}\n   {body}"
                elif header:
                    section = header
                elif body:
                    section = f"📎 {body}"
                else:
                    continue

                # Deduplicate only when the fully rendered section is already
                # present. The shared URL often already appears in the user's
                # message text, and skipping on URL/title alone would hide the
                # preview body we actually want the agent to see.
                if section in text:
                    continue

                if att_footer:
                    section = f"{section}\n   _{att_footer}_"

                att_parts.append(section)

            if att_parts:
                attachment_text = "\n\n".join(att_parts)
                text = (text.strip() + "\n\n" + attachment_text).strip()
                logger.debug(
                    "Slack: appended %d link unfurl(s) to message text",
                    len(att_parts),
                )

        channel_id = event.get("channel", "")
        ts = event.get("ts", "")
        assistant_meta = self._lookup_assistant_thread_metadata(
            event,
            channel_id=channel_id,
            thread_ts=event.get("thread_ts", ""),
        )
        user_id = event.get("user") or assistant_meta.get("user_id", "")
        if not channel_id:
            channel_id = assistant_meta.get("channel_id", "")
        team_id = (
            event.get("team")
            or event.get("team_id")
            or assistant_meta.get("team_id", "")
        )

        # Track which workspace owns this channel
        if team_id and channel_id:
            self._channel_team[channel_id] = team_id

        # Determine if this is a DM or channel message
        channel_type = event.get("channel_type", "")
        if not channel_type and channel_id.startswith("D"):
            channel_type = "im"
        is_dm = channel_type in {"im", "mpim"}  # Both 1:1 and group DMs

        # Build thread_ts for session keying.
        # In channels: fall back to ts so each top-level @mention starts a
        #   new thread/session (the bot always replies in a thread).
        # In DMs: fall back to ts so each top-level DM reply thread gets
        #   its own session key (matching channel behavior). Set
        #   dm_top_level_threads_as_sessions: false in config to revert to
        #   legacy single-session-per-DM-channel behavior.
        if is_dm:
            thread_ts = event.get("thread_ts") or assistant_meta.get("thread_ts")
            if not thread_ts and self._dm_top_level_threads_as_sessions():
                thread_ts = ts
        else:
            thread_ts = event.get("thread_ts") or ts  # ts fallback for channels

        # In channels, respond if:
        #   0. Channel is in free_response_channels, OR require_mention is
        #      disabled — always process regardless of mention.
        #   1. The bot is @mentioned in this message, OR
        #   2. The message is a reply in a thread the bot started/participated in, OR
        #   3. The message is in a thread where the bot was previously @mentioned, OR
        #   4. There's an existing session for this thread (survives restarts)
        bot_uid = self._team_bot_user_ids.get(team_id, self._bot_user_id)
        routing_text = original_text or ""
        is_mentioned = bot_uid and f"<@{bot_uid}>" in routing_text
        event_thread_ts = event.get("thread_ts")
        is_thread_reply = bool(event_thread_ts and event_thread_ts != ts)

        if not is_dm and bot_uid:
            # Check allowed channels — if set, only respond in these channels (whitelist)
            allowed_channels = self._slack_allowed_channels()
            if allowed_channels and channel_id not in allowed_channels:
                logger.debug("[Slack] Ignoring message in non-allowed channel: %s", channel_id)
                return

            if channel_id in self._slack_free_response_channels():
                pass  # Free-response channel — always process
            elif not self._slack_require_mention():
                pass  # Mention requirement disabled globally for Slack
            elif self._slack_strict_mention() and not is_mentioned:
                return  # Strict mode: ignore until @-mentioned again
            elif not is_mentioned:
                reply_to_bot_thread = (
                    is_thread_reply and event_thread_ts in self._bot_message_ts
                )
                in_mentioned_thread = (
                    event_thread_ts is not None
                    and event_thread_ts in self._mentioned_threads
                )
                has_session = (
                    is_thread_reply
                    and self._has_active_session_for_thread(
                        channel_id=channel_id,
                        thread_ts=event_thread_ts,
                        user_id=user_id,
                    )
                )
                if not reply_to_bot_thread and not in_mentioned_thread and not has_session:
                    return

        if is_mentioned:
            # Strip the bot mention from the text
            text = text.replace(f"<@{bot_uid}>", "").strip()
            # Register this thread so all future messages auto-trigger the bot.
            # Skipped in strict mode: strict_mention=true bots must be
            # re-mentioned every turn, so remembering the thread would
            # defeat the feature (and re-enable agent-to-agent ack loops).
            if event_thread_ts and not self._slack_strict_mention():
                self._mentioned_threads.add(event_thread_ts)
                if len(self._mentioned_threads) > self._MENTIONED_THREADS_MAX:
                    to_remove = list(self._mentioned_threads)[:self._MENTIONED_THREADS_MAX // 2]
                    for t in to_remove:
                        self._mentioned_threads.discard(t)

        # When entering a thread for the first time (no existing session),
        # fetch thread context so the agent understands the conversation.
        if is_thread_reply and not self._has_active_session_for_thread(
            channel_id=channel_id,
            thread_ts=event_thread_ts,
            user_id=user_id,
        ):
            thread_context = await self._fetch_thread_context(
                channel_id=channel_id,
                thread_ts=event_thread_ts,
                current_ts=ts,
                team_id=team_id,
            )
            if thread_context:
                text = thread_context + text

        # Determine message type
        msg_type = MessageType.TEXT
        if (original_text or "").startswith("/"):
            msg_type = MessageType.COMMAND

        # Handle file attachments
        media_urls = []
        media_types = []
        attachment_notices: List[str] = []
        files = event.get("files", [])
        for f in files:
            # Slack Connect channels return stub file objects with
            # file_access="check_file_info" and no URL fields. We must
            # call files.info to retrieve the full object (including url_private_download)
            # before we can download it.
            # https://docs.slack.dev/reference/objects/file-object/#slack_connect_files
            if f.get("file_access") == "check_file_info":
                file_id = f.get("id")
                if not file_id:
                    continue
                try:
                    info_resp = await self._get_client(channel_id).files_info(file=file_id)
                    if info_resp.get("ok"):
                        f = info_resp["file"]
                    else:
                        detail = self._describe_slack_api_error(info_resp, file_obj=f)
                        if detail:
                            attachment_notices.append(detail)
                            logger.warning("[Slack] %s", detail)
                        else:
                            logger.warning(
                                "[Slack] files.info failed for %s: %s",
                                file_id, info_resp.get("error"),
                            )
                        continue
                except Exception as e:
                    response = getattr(e, "response", None)
                    detail = self._describe_slack_api_error(response, file_obj=f)
                    if detail:
                        attachment_notices.append(detail)
                        logger.warning("[Slack] %s", detail)
                    else:
                        logger.warning("[Slack] files.info error for %s: %s", file_id, e, exc_info=True)
                    continue

            mimetype = f.get("mimetype", "unknown")
            url = f.get("url_private_download") or f.get("url_private", "")
            if mimetype.startswith("image/") and url:
                try:
                    ext = "." + mimetype.split("/")[-1].split(";")[0]
                    if ext not in {".jpg", ".jpeg", ".png", ".gif", ".webp"}:
                        ext = ".jpg"
                    # Slack private URLs require the bot token as auth header
                    cached = await self._download_slack_file(url, ext, team_id=team_id)
                    media_urls.append(cached)
                    media_types.append(mimetype)
                except Exception as e:  # pragma: no cover - defensive logging
                    detail = self._describe_slack_download_failure(e, file_obj=f)
                    if detail:
                        attachment_notices.append(detail)
                        logger.warning("[Slack] %s", detail)
                    else:
                        logger.warning("[Slack] Failed to cache image from %s: %s", url, e, exc_info=True)
            elif mimetype.startswith("audio/") and url:
                try:
                    ext = "." + mimetype.split("/")[-1].split(";")[0]
                    if ext not in {".ogg", ".mp3", ".wav", ".webm", ".m4a"}:
                        ext = ".ogg"
                    cached = await self._download_slack_file(url, ext, audio=True, team_id=team_id)
                    media_urls.append(cached)
                    media_types.append(mimetype)
                except Exception as e:  # pragma: no cover - defensive logging
                    detail = self._describe_slack_download_failure(e, file_obj=f)
                    if detail:
                        attachment_notices.append(detail)
                        logger.warning("[Slack] %s", detail)
                    else:
                        logger.warning("[Slack] Failed to cache audio from %s: %s", url, e, exc_info=True)
            elif url:
                # Try to handle as a document attachment
                try:
                    original_filename = f.get("name", "")
                    ext = ""
                    if original_filename:
                        _, ext = os.path.splitext(original_filename)
                        ext = ext.lower()

                    # Fallback: reverse-lookup from MIME type
                    if not ext and mimetype:
                        mime_to_ext = {v: k for k, v in SUPPORTED_DOCUMENT_TYPES.items()}
                        ext = mime_to_ext.get(mimetype, "")

                    if ext not in SUPPORTED_DOCUMENT_TYPES:
                        continue  # Skip unsupported file types silently

                    # Check file size (Slack limit: 20 MB for bots)
                    file_size = f.get("size", 0)
                    MAX_DOC_BYTES = 20 * 1024 * 1024
                    if not file_size or file_size > MAX_DOC_BYTES:
                        logger.warning("[Slack] Document too large or unknown size: %s", file_size)
                        continue

                    # Download and cache
                    raw_bytes = await self._download_slack_file_bytes(url, team_id=team_id)
                    cached_path = cache_document_from_bytes(
                        raw_bytes, original_filename or f"document{ext}"
                    )
                    doc_mime = SUPPORTED_DOCUMENT_TYPES[ext]
                    media_urls.append(cached_path)
                    media_types.append(doc_mime)
                    logger.debug("[Slack] Cached user document: %s", cached_path)

                    # Inject small text-ish files directly into the prompt so
                    # snippets like JSON/YAML/configs are actually visible to the agent.
                    MAX_TEXT_INJECT_BYTES = 100 * 1024
                    TEXT_INJECT_EXTENSIONS = {
                        ".md", ".txt", ".csv", ".log", ".json", ".xml",
                        ".yaml", ".yml", ".toml", ".ini", ".cfg",
                    }
                    if ext in TEXT_INJECT_EXTENSIONS and len(raw_bytes) <= MAX_TEXT_INJECT_BYTES:
                        try:
                            text_content = raw_bytes.decode("utf-8")
                            display_name = original_filename or f"document{ext}"
                            display_name = re.sub(r'[^\w.\- ]', '_', display_name)
                            injection = f"[Content of {display_name}]:\n{text_content}"
                            if text:
                                text = f"{injection}\n\n{text}"
                            else:
                                text = injection
                        except UnicodeDecodeError:
                            pass  # Binary content, skip injection

                except Exception as e:  # pragma: no cover - defensive logging
                    detail = self._describe_slack_download_failure(e, file_obj=f)
                    if detail:
                        attachment_notices.append(detail)
                        logger.warning("[Slack] %s", detail)
                    else:
                        logger.warning("[Slack] Failed to cache document from %s: %s", url, e, exc_info=True)

        if attachment_notices:
            notice_block = "[Slack attachment notice]\n" + "\n".join(f"- {n}" for n in attachment_notices)
            text = f"{notice_block}\n\n{text}" if text else notice_block

        if msg_type != MessageType.COMMAND and media_types:
            if any(m.startswith("image/") for m in media_types):
                msg_type = MessageType.PHOTO
            elif any(m.startswith("audio/") for m in media_types):
                msg_type = MessageType.VOICE
            else:
                msg_type = MessageType.DOCUMENT

        # Resolve user display name (cached after first lookup)
        user_name = await self._resolve_user_name(user_id, chat_id=channel_id)

        # Build source
        source = self.build_source(
            chat_id=channel_id,
            chat_name=channel_id,  # Will be resolved later if needed
            chat_type="dm" if is_dm else "group",
            user_id=user_id,
            user_name=user_name,
            thread_id=thread_ts,
        )

        # Per-channel ephemeral prompt
        from channels.platforms.base import resolve_channel_prompt, resolve_channel_skills
        _channel_prompt = resolve_channel_prompt(
            self.config.extra, channel_id, None,
        )
        _auto_skill = resolve_channel_skills(
            self.config.extra, channel_id, None,
        )

        # Extract reply context if this message is a thread reply.
        # Mirrors the Telegram/Discord implementations so that the gateway runtime
        # can inject a `[Replying to: "..."]` prefix when the parent is not
        # already in the session history. Uses the thread-context cache when
        # available to avoid redundant conversations.replies calls.
        reply_to_text = None
        if thread_ts and thread_ts != ts:
            try:
                reply_to_text = await self._fetch_thread_parent_text(
                    channel_id=channel_id,
                    thread_ts=thread_ts,
                    team_id=team_id,
                ) or None
            except Exception:  # pragma: no cover - defensive
                reply_to_text = None

        msg_event = MessageEvent(
            text=text,
            message_type=msg_type,
            source=source,
            raw_message=event,
            message_id=ts,
            media_urls=media_urls,
            media_types=media_types,
            reply_to_message_id=thread_ts if thread_ts != ts else None,
            channel_prompt=_channel_prompt,
            reply_to_text=reply_to_text,
            auto_skill=_auto_skill,
        )

        # Only react when bot is directly addressed (DM or @mention).
        # In listen-all channels (require_mention=false), reacting to every
        # casual message would be noisy.
        _should_react = (is_dm or is_mentioned) and self._reactions_enabled()
        if _should_react:
            self._reacting_message_ids.add(ts)

        await self.handle_message(msg_event)

    # ----- Approval button support (Block Kit) -----

    async def send_exec_approval(
        self, chat_id: str, command: str, session_key: str,
        description: str = "dangerous command",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Send a Block Kit approval prompt with interactive buttons.

        The buttons call ``resolve_gateway_approval()`` to unblock the waiting
        agent thread — same mechanism as the text ``/approve`` flow.
        """
        if not self._app:
            return SendResult(success=False, error="Not connected")

        try:
            cmd_preview = command[:2900] + "..." if len(command) > 2900 else command
            thread_ts = self._resolve_thread_ts(None, metadata)

            blocks = [
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": (
                            f":warning: *Command Approval Required*\n"
                            f"```{cmd_preview}```\n"
                            f"Reason: {description}"
                        ),
                    },
                },
                {
                    "type": "actions",
                    "elements": [
                        {
                            "type": "button",
                            "text": {"type": "plain_text", "text": "Allow Once"},
                            "style": "primary",
                            "action_id": "hermes_approve_once",
                            "value": session_key,
                        },
                        {
                            "type": "button",
                            "text": {"type": "plain_text", "text": "Allow Session"},
                            "action_id": "hermes_approve_session",
                            "value": session_key,
                        },
                        {
                            "type": "button",
                            "text": {"type": "plain_text", "text": "Always Allow"},
                            "action_id": "hermes_approve_always",
                            "value": session_key,
                        },
                        {
                            "type": "button",
                            "text": {"type": "plain_text", "text": "Deny"},
                            "style": "danger",
                            "action_id": "hermes_deny",
                            "value": session_key,
                        },
                    ],
                },
            ]

            kwargs: Dict[str, Any] = {
                "channel": chat_id,
                "text": f"⚠️ Command approval required: {cmd_preview[:100]}",
                "blocks": blocks,
            }
            if thread_ts:
                kwargs["thread_ts"] = thread_ts

            result = await self._get_client(chat_id).chat_postMessage(**kwargs)
            msg_ts = result.get("ts", "")
            if msg_ts:
                self._approval_resolved[msg_ts] = False

            return SendResult(success=True, message_id=msg_ts, raw_response=result)
        except Exception as e:
            logger.error("[Slack] send_exec_approval failed: %s", e, exc_info=True)
            return SendResult(success=False, error=str(e))

    async def send_slash_confirm(
        self, chat_id: str, title: str, message: str, session_key: str,
        confirm_id: str, metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Send a Block Kit three-option slash-command confirmation prompt."""
        if not self._app:
            return SendResult(success=False, error="Not connected")

        try:
            body = message[:2900] + "..." if len(message) > 2900 else message
            thread_ts = self._resolve_thread_ts(None, metadata)
            # Encode session_key and confirm_id into the button value so the
            # callback handler can resolve without extra bookkeeping.
            value = f"{session_key}|{confirm_id}"

            blocks = [
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"*{title or 'Confirm'}*\n\n{body}",
                    },
                },
                {
                    "type": "actions",
                    "elements": [
                        {
                            "type": "button",
                            "text": {"type": "plain_text", "text": "Approve Once"},
                            "style": "primary",
                            "action_id": "hermes_confirm_once",
                            "value": value,
                        },
                        {
                            "type": "button",
                            "text": {"type": "plain_text", "text": "Always Approve"},
                            "action_id": "hermes_confirm_always",
                            "value": value,
                        },
                        {
                            "type": "button",
                            "text": {"type": "plain_text", "text": "Cancel"},
                            "style": "danger",
                            "action_id": "hermes_confirm_cancel",
                            "value": value,
                        },
                    ],
                },
            ]

            kwargs: Dict[str, Any] = {
                "channel": chat_id,
                "text": f"{title or 'Confirm'}: {body[:100]}",
                "blocks": blocks,
            }
            if thread_ts:
                kwargs["thread_ts"] = thread_ts

            result = await self._get_client(chat_id).chat_postMessage(**kwargs)
            return SendResult(success=True, message_id=result.get("ts", ""), raw_response=result)
        except Exception as e:
            logger.error("[Slack] send_slash_confirm failed: %s", e, exc_info=True)
            return SendResult(success=False, error=str(e))

    async def _handle_slash_confirm_action(self, ack, body, action) -> None:
        """Handle a slash-confirm button click from Block Kit."""
        await ack()

        action_id = action.get("action_id", "")
        value = action.get("value", "")
        message = body.get("message", {})
        msg_ts = message.get("ts", "")
        channel_id = body.get("channel", {}).get("id", "")
        user_name = body.get("user", {}).get("name", "unknown")
        user_id = body.get("user", {}).get("id", "")

        team_id = str(body.get("team", {}).get("id", "") or body.get("team_id", ""))
        if not self._is_interactive_user_authorized(
            user_id,
            channel_id=channel_id,
            user_name=user_name,
            team_id=team_id,
        ):
            logger.warning(
                "[Slack] Unauthorized slash-confirm click by %s (%s) — ignoring",
                user_name,
                user_id,
            )
            return

        # Parse session_key|confirm_id back out
        if "|" not in value:
            logger.warning("[Slack] Malformed slash-confirm value: %s", value)
            return
        session_key, confirm_id = value.split("|", 1)

        choice_map = {
            "hermes_confirm_once": "once",
            "hermes_confirm_always": "always",
            "hermes_confirm_cancel": "cancel",
        }
        choice = choice_map.get(action_id, "cancel")

        label_map = {
            "once": f"✅ Approved once by {user_name}",
            "always": f"🔒 Always approved by {user_name}",
            "cancel": f"❌ Cancelled by {user_name}",
        }
        decision_text = label_map.get(choice, f"Resolved by {user_name}")

        # Pull original prompt body out of the section block so we can show
        # the decision inline without losing context.
        original_text = ""
        for block in message.get("blocks", []):
            if block.get("type") == "section":
                original_text = block.get("text", {}).get("text", "")
                break

        updated_blocks = [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": original_text or "Confirmation prompt",
                },
            },
            {
                "type": "context",
                "elements": [
                    {"type": "mrkdwn", "text": decision_text},
                ],
            },
        ]

        try:
            await self._get_client(channel_id).chat_update(
                channel=channel_id,
                ts=msg_ts,
                text=decision_text,
                blocks=updated_blocks,
            )
        except Exception as e:
            logger.warning("[Slack] Failed to update slash-confirm message: %s", e)

        # Resolve via the module-level primitive and post any follow-up.
        try:
            from tools import slash_confirm as _slash_confirm_mod
            result_text = await _slash_confirm_mod.resolve(session_key, confirm_id, choice)
            if result_text:
                post_kwargs: Dict[str, Any] = {
                    "channel": channel_id,
                    "text": result_text,
                }
                # Inherit the thread so the reply stays in the same place.
                thread_ts = message.get("thread_ts") or msg_ts
                if thread_ts:
                    post_kwargs["thread_ts"] = thread_ts
                await self._get_client(channel_id).chat_postMessage(**post_kwargs)
            logger.info(
                "Slack button resolved slash-confirm for session %s (choice=%s, user=%s)",
                session_key, choice, user_name,
            )
        except Exception as exc:
            logger.error("Failed to resolve slash-confirm from Slack button: %s", exc, exc_info=True)

    async def _handle_approval_action(self, ack, body, action) -> None:
        """Handle an approval button click from Block Kit."""
        await ack()

        action_id = action.get("action_id", "")
        session_key = action.get("value", "")
        message = body.get("message", {})
        msg_ts = message.get("ts", "")
        channel_id = body.get("channel", {}).get("id", "")
        user_name = body.get("user", {}).get("name", "unknown")
        user_id = body.get("user", {}).get("id", "")

        # Only authorized users may click approval buttons.  Button clicks
        # bypass the normal message auth flow in gateway/run.py, so we must
        # check here as well.
        team_id = str(body.get("team", {}).get("id", "") or body.get("team_id", ""))
        if not self._is_interactive_user_authorized(
            user_id,
            channel_id=channel_id,
            user_name=user_name,
            team_id=team_id,
        ):
            logger.warning(
                "[Slack] Unauthorized approval click by %s (%s) — ignoring",
                user_name,
                user_id,
            )
            return

        # Map action_id to approval choice
        choice_map = {
            "hermes_approve_once": "once",
            "hermes_approve_session": "session",
            "hermes_approve_always": "always",
            "hermes_deny": "deny",
        }
        choice = choice_map.get(action_id, "deny")

        # Prevent double-clicks — atomic pop; first caller gets False, others get True (default)
        if self._approval_resolved.pop(msg_ts, True):
            return

        # Update the message to show the decision and remove buttons
        label_map = {
            "once": f"✅ Approved once by {user_name}",
            "session": f"✅ Approved for session by {user_name}",
            "always": f"✅ Approved permanently by {user_name}",
            "deny": f"❌ Denied by {user_name}",
        }
        decision_text = label_map.get(choice, f"Resolved by {user_name}")

        # Get original text from the section block
        original_text = ""
        for block in message.get("blocks", []):
            if block.get("type") == "section":
                original_text = block.get("text", {}).get("text", "")
                break

        updated_blocks = [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": original_text or "Command approval request",
                },
            },
            {
                "type": "context",
                "elements": [
                    {"type": "mrkdwn", "text": decision_text},
                ],
            },
        ]

        try:
            await self._get_client(channel_id).chat_update(
                channel=channel_id,
                ts=msg_ts,
                text=decision_text,
                blocks=updated_blocks,
            )
        except Exception as e:
            logger.warning("[Slack] Failed to update approval message: %s", e)

        # Resolve the approval — this unblocks the agent thread
        try:
            from tools.approval import resolve_gateway_approval
            count = resolve_gateway_approval(session_key, choice)
            logger.info(
                "Slack button resolved %d approval(s) for session %s (choice=%s, user=%s)",
                count, session_key, choice, user_name,
            )
        except Exception as exc:
            logger.error("Failed to resolve gateway approval from Slack button: %s", exc)

        # (approval state already consumed by atomic pop above)

    # ----- Thread context fetching -----

    async def _fetch_thread_context(
        self, channel_id: str, thread_ts: str, current_ts: str,
        team_id: str = "", limit: int = 30,
    ) -> str:
        """Fetch recent thread messages to provide context when the bot is
        mentioned mid-thread for the first time.

        This method is only called when there is NO active session for the
        thread (guarded at the call site by _has_active_session_for_thread).
        That guard ensures thread messages are prepended only on the very
        first turn — after that the session history already holds them, so
        there is no duplication across subsequent turns.

        Results are cached for _THREAD_CACHE_TTL seconds per thread to avoid
        hammering conversations.replies (Tier 3, ~50 req/min).

        Returns a formatted string with prior thread history, or empty string
        on failure or if the thread has no prior messages.
        """
        cache_key = f"{channel_id}:{thread_ts}:{team_id}"
        now = time.monotonic()
        cached = self._thread_context_cache.get(cache_key)
        if cached and (now - cached.fetched_at) < self._THREAD_CACHE_TTL:
            return cached.content

        try:
            client = self._get_client(channel_id)

            # Retry with exponential backoff for Tier-3 rate limits (429).
            result = None
            for attempt in range(3):
                try:
                    result = await client.conversations_replies(
                        channel=channel_id,
                        ts=thread_ts,
                        limit=limit + 1,  # +1 because it includes the current message
                        inclusive=True,
                    )
                    break
                except Exception as exc:
                    # Check for rate-limit error from slack_sdk
                    err_str = str(exc).lower()
                    is_rate_limit = (
                        "ratelimited" in err_str
                        or "429" in err_str
                        or "rate_limited" in err_str
                    )
                    if is_rate_limit and attempt < 2:
                        retry_after = 1.0 * (2 ** attempt)  # 1s, 2s
                        logger.warning(
                            "[Slack] conversations.replies rate limited; retrying in %.1fs (attempt %d/3)",
                            retry_after, attempt + 1,
                        )
                        await asyncio.sleep(retry_after)
                        continue
                    raise

            if result is None:
                return ""

            messages = result.get("messages", [])
            if not messages:
                return ""

            bot_uid = self._team_bot_user_ids.get(team_id, self._bot_user_id)
            context_parts = []
            parent_text = ""
            for msg in messages:
                msg_ts = msg.get("ts", "")
                # Exclude the current triggering message — it will be delivered
                # as the user message itself, so including it here would duplicate it.
                if msg_ts == current_ts:
                    continue

                is_parent = msg_ts == thread_ts
                is_bot = bool(msg.get("bot_id")) or msg.get("subtype") == "bot_message"
                msg_user = msg.get("user", "")

                # Identify "our own" bot for this workspace (multi-workspace safe).
                msg_team = msg.get("team") or team_id
                self_bot_uid = (
                    self._team_bot_user_ids.get(msg_team)
                    if msg_team
                    else None
                ) or self._bot_user_id

                # Exclude only our own prior bot replies (circular context).
                # Keep:
                #   - the thread parent even if it was posted by a bot
                #     (e.g. a cron job summary we are now replying to);
                #   - other bots' child messages (useful third-party context).
                if (
                    is_bot
                    and not is_parent
                    and self_bot_uid
                    and msg_user == self_bot_uid
                ):
                    continue

                msg_text = msg.get("text", "").strip()
                if not msg_text:
                    continue

                # Strip bot mentions from context messages
                if bot_uid:
                    msg_text = msg_text.replace(f"<@{bot_uid}>", "").strip()

                prefix = "[thread parent] " if is_parent else ""
                display_user = msg_user or "unknown"
                # Prefer the bot's own name when the message is a bot post.
                if is_bot and not display_user:
                    display_user = msg.get("username") or "bot"
                name = await self._resolve_user_name(display_user, chat_id=channel_id)
                context_parts.append(f"{prefix}{name}: {msg_text}")
                if is_parent:
                    parent_text = msg_text

            content = ""
            if context_parts:
                content = (
                    "[Thread context — prior messages in this thread (not yet in conversation history):]\n"
                    + "\n".join(context_parts)
                    + "\n[End of thread context]\n\n"
                )

            self._thread_context_cache[cache_key] = _ThreadContextCache(
                content=content,
                fetched_at=now,
                message_count=len(context_parts),
                parent_text=parent_text,
            )
            return content

        except Exception as e:
            logger.warning("[Slack] Failed to fetch thread context: %s", e)
            return ""

    async def _fetch_thread_parent_text(
        self, channel_id: str, thread_ts: str, team_id: str = "",
    ) -> str:
        """Return the raw text of the thread parent message (for reply_to_text).

        Uses the same per-thread cache as :meth:`_fetch_thread_context` to avoid
        hitting ``conversations.replies`` twice. Falls back to a cheap single-
        message fetch (``limit=1, inclusive=True``) when the cache is cold.

        Returns empty string on any failure — callers should treat an empty
        return as "no parent context to inject".
        """
        cache_key = f"{channel_id}:{thread_ts}:{team_id}"
        now = time.monotonic()
        cached = self._thread_context_cache.get(cache_key)
        if cached and (now - cached.fetched_at) < self._THREAD_CACHE_TTL:
            return cached.parent_text

        try:
            client = self._get_client(channel_id)
            result = await client.conversations_replies(
                channel=channel_id,
                ts=thread_ts,
                limit=1,
                inclusive=True,
            )
            messages = result.get("messages", []) if result else []
            if not messages:
                return ""
            parent = messages[0]
            if parent.get("ts", "") != thread_ts:
                return ""
            bot_uid = self._team_bot_user_ids.get(team_id, self._bot_user_id)
            text = (parent.get("text") or "").strip()
            if bot_uid:
                text = text.replace(f"<@{bot_uid}>", "").strip()
            return text
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("[Slack] Failed to fetch thread parent text: %s", exc)
            return ""

    async def _handle_slash_command(self, command: dict) -> None:
        """Handle Slack slash commands.

        Every gateway command in COMMAND_REGISTRY is registered as a native
        Slack slash (``/btw``, ``/stop``, ``/model``, etc.), matching the
        Discord and Telegram model. The slash name itself is the command;
        any text after it is the argument list.

        The legacy ``/hermes <subcommand> [args]`` form is preserved for
        backward compatibility with older workspace manifests and for users
        who want a single entry point for free-form questions (``/hermes
        what's the weather`` — non-slash text is treated as a regular
        message).
        """
        slash_name = (command.get("command") or "").lstrip("/").strip()
        text = command.get("text", "").strip()
        user_id = command.get("user_id", "")
        channel_id = command.get("channel_id", "")
        team_id = command.get("team_id", "")

        # Track which workspace owns this channel
        if team_id and channel_id:
            self._channel_team[channel_id] = team_id

        if slash_name in {"hermes", ""}:
            # Legacy /hermes <subcommand> [args] routing + free-form questions.
            # Empty slash_name falls into this branch for backward compat
            # with any caller that didn't populate command["command"].
            from hermes_cli.commands import slack_subcommand_map
            subcommand_map = slack_subcommand_map()
            subcommand_map["compact"] = "/compress"
            # Guard against whitespace-only text where ``text`` is truthy but
            # ``text.split()`` returns ``[]`` (e.g. user sends ``/hermes   ``).
            parts = text.split() if text else []
            first_word = parts[0] if parts else ""
            if first_word in subcommand_map:
                rest = text[len(first_word):].strip()
                text = f"{subcommand_map[first_word]} {rest}".strip() if rest else subcommand_map[first_word]
            elif text:
                pass  # Treat as a regular question
            else:
                text = "/help"
        else:
            # Native slash — /<slash_name> [args].  Route directly through the
            # gateway command dispatcher by prepending the slash.
            text = f"/{slash_name} {text}".strip()

        # Slack slash commands can originate from DMs or shared channels.
        # Preserve DM semantics only for DM channel IDs; shared channels must
        # keep group semantics so different users do not collide into one
        # session key.
        is_dm = str(channel_id).startswith("D")
        source = self.build_source(
            chat_id=channel_id,
            chat_type="dm" if is_dm else "group",
            user_id=user_id,
        )

        event = MessageEvent(
            text=text,
            message_type=MessageType.COMMAND if text.startswith("/") else MessageType.TEXT,
            source=source,
            raw_message=command,
        )

        # Stash the Slack response_url so the first reply for this
        # channel+user can be routed ephemerally (replaces the initial
        # "Running /cmd…" ack shown by handle_hermes_command).
        # Only stash for COMMAND events (text starts with "/") — free-form
        # questions via "/hermes <question>" must produce public replies so
        # the whole channel can see the agent's answer.
        response_url = command.get("response_url", "")
        if response_url and user_id and channel_id and text.startswith("/"):
            self._slash_command_contexts[(channel_id, user_id)] = {
                "response_url": response_url,
                "ts": time.monotonic(),
            }

        # Set the ContextVar so send() can match the correct stashed
        # response_url even when multiple users slash concurrently.
        _slash_user_id_token = _slash_user_id.set(user_id or None)
        try:
            await self.handle_message(event)
        finally:
            _slash_user_id.reset(_slash_user_id_token)

    def _has_active_session_for_thread(
        self,
        channel_id: str,
        thread_ts: str,
        user_id: str,
    ) -> bool:
        """Check if there's an active session for a thread.

        Used to determine if thread replies without @mentions should be
        processed (they should if there's an active session).

        Uses ``build_session_key()`` as the single source of truth for key
        construction — avoids the bug where manual key building didn't
        respect ``thread_sessions_per_user`` and ``group_sessions_per_user``
        settings correctly.
        """
        session_store = getattr(self, "_session_store", None)
        if not session_store:
            return False

        try:
            from channels.session_identity import SessionSource, build_session_key

            source = SessionSource(
                platform=Platform.SLACK,
                chat_id=channel_id,
                chat_type="group",
                user_id=user_id,
                thread_id=thread_ts,
            )

            # Read session isolation settings from the store's config
            store_cfg = getattr(session_store, "config", None)
            gspu = getattr(store_cfg, "group_sessions_per_user", True) if store_cfg else True
            tspu = getattr(store_cfg, "thread_sessions_per_user", False) if store_cfg else False

            session_key = build_session_key(
                source,
                group_sessions_per_user=gspu,
                thread_sessions_per_user=tspu,
            )

            session_store._ensure_loaded()
            return session_key in session_store._entries
        except Exception:
            return False

    async def _download_slack_file(self, url: str, ext: str, audio: bool = False, team_id: str = "") -> str:
        """Download a Slack file using the bot token for auth, with retry."""
        import httpx

        bot_token = self._team_clients[team_id].token if team_id and team_id in self._team_clients else self.config.token

        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
            for attempt in range(3):
                try:
                    response = await client.get(
                        url,
                        headers={"Authorization": f"Bearer {bot_token}"},
                    )
                    response.raise_for_status()

                    # Slack may return an HTML sign-in/redirect page
                    # instead of actual media bytes (e.g. expired token,
                    # restricted file access).  Detect this early so we
                    # don't cache bogus data and confuse downstream tools.
                    ct = response.headers.get("content-type", "")
                    if "text/html" in ct:
                        raise ValueError(
                            "Slack returned HTML instead of media "
                            f"(content-type: {ct}); "
                            "check bot token scopes and file permissions"
                        )

                    if audio:
                        from channels.platforms.base import cache_audio_from_bytes
                        return cache_audio_from_bytes(response.content, ext)
                    else:
                        from channels.platforms.base import cache_image_from_bytes
                        return cache_image_from_bytes(response.content, ext)
                except (httpx.TimeoutException, httpx.HTTPStatusError) as exc:
                    if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code < 429:
                        raise
                    if attempt < 2:
                        logger.debug("Slack file download retry %d/2 for %s: %s",
                                     attempt + 1, url[:80], exc)
                        await asyncio.sleep(1.5 * (attempt + 1))
                        continue
                    raise

    async def _download_slack_file_bytes(self, url: str, team_id: str = "") -> bytes:
        """Download a Slack file and return raw bytes, with retry."""
        import httpx

        bot_token = self._team_clients[team_id].token if team_id and team_id in self._team_clients else self.config.token

        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
            for attempt in range(3):
                try:
                    response = await client.get(
                        url,
                        headers={"Authorization": f"Bearer {bot_token}"},
                    )
                    response.raise_for_status()
                    ct = response.headers.get("content-type", "")
                    if "text/html" in ct:
                        raise ValueError(
                            "Slack returned HTML instead of file bytes "
                            f"(content-type: {ct}); "
                            "check bot token scopes and file permissions"
                        )
                    return response.content
                except (httpx.TimeoutException, httpx.HTTPStatusError, ValueError) as exc:
                    if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code < 429:
                        raise
                    if isinstance(exc, ValueError):
                        raise
                    if attempt < 2:
                        logger.debug("Slack file download retry %d/2 for %s: %s",
                                     attempt + 1, url[:80], exc)
                        await asyncio.sleep(1.5 * (attempt + 1))
                        continue
                    raise

    # ── Channel mention gating ─────────────────────────────────────────────

    def _slack_require_mention(self) -> bool:
        """Return whether channel messages require an explicit bot mention.

        Uses explicit-false parsing (like Discord/Matrix) rather than
        truthy parsing, since the safe default is True (gating on).
        Unrecognised or empty values keep gating enabled.
        """
        configured = self.config.extra.get("require_mention")
        if configured is not None:
            if isinstance(configured, str):
                return configured.lower() not in {"false", "0", "no", "off"}
            return bool(configured)
        return get_profile_env("SLACK_REQUIRE_MENTION", "true").lower() not in {
            "false",
            "0",
            "no",
            "off",
        }

    def _slack_strict_mention(self) -> bool:
        """When true, channel threads require an explicit @-mention on every
        message. Disables all auto-triggers (mentioned-thread memory,
        bot-message follow-up, session-presence). Defaults to False.
        """
        configured = self.config.extra.get("strict_mention")
        if configured is not None:
            if isinstance(configured, str):
                return configured.lower() in {"true", "1", "yes", "on"}
            return bool(configured)
        return get_profile_env("SLACK_STRICT_MENTION", "false").lower() in {
            "true",
            "1",
            "yes",
            "on",
        }

    def _slack_free_response_channels(self) -> set:
        """Return channel IDs where no @mention is required."""
        raw = self.config.extra.get("free_response_channels")
        if raw is None:
            raw = get_profile_env("SLACK_FREE_RESPONSE_CHANNELS", "")
        if isinstance(raw, list):
            return {str(part).strip() for part in raw if str(part).strip()}
        # Coerce non-list scalars (str/int/float) to str before splitting.
        # A bare numeric YAML value (`free_response_channels: 1234567890`) is
        # loaded as int and was previously falling through the isinstance(str)
        # branch to return an empty set.  str() here accepts whatever scalar
        # the YAML loader hands us without changing existing string/CSV
        # semantics.
        s = str(raw).strip() if raw is not None else ""
        if s:
            return {part.strip() for part in s.split(",") if part.strip()}
        return set()

    def _slack_allowed_channels(self) -> set:
        """Return the whitelist of channel IDs the bot will respond in.

        When non-empty, messages from channels NOT in this set are silently
        ignored — even if the bot is @mentioned.  DMs are never filtered.
        Empty set means no restriction (fully backward compatible).
        """
        raw = self.config.extra.get("allowed_channels")
        if raw is None:
            raw = get_profile_env("SLACK_ALLOWED_CHANNELS", "")
        if isinstance(raw, list):
            return {str(part).strip() for part in raw if str(part).strip()}
        if isinstance(raw, str) and raw.strip():
            return {part.strip() for part in raw.split(",") if part.strip()}
        return set()
