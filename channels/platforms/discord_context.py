from __future__ import annotations

import asyncio
import logging
import os
import re
from typing import Any, Dict, Optional

try:
    import discord
    DISCORD_AVAILABLE = True
except ImportError:  # pragma: no cover - optional platform dependency
    discord = None
    DISCORD_AVAILABLE = False

from channels.platforms.base import SendResult

logger = logging.getLogger(__name__)

VALID_THREAD_AUTO_ARCHIVE_MINUTES = {60, 1440, 4320, 10080}


def _discord_public_attr(name: str, fallback: Any = None) -> Any:
    import sys

    public_module = sys.modules.get("channels.platforms.discord")
    if public_module is None:
        return fallback
    return getattr(public_module, name, fallback)


class DiscordContextMixin:
    async def send_typing(self, chat_id: str, metadata=None) -> None:
        """Start a persistent typing indicator for a channel.
    
        Discord's TYPING_START gateway event is unreliable in DMs for bots.
        Instead, start a background loop that hits the typing endpoint every
        12 seconds (typing indicator lasts ~10s).  The loop is cancelled when
        stop_typing() is called (after the response is sent).
    
        Rate-limit handling: if a 429 is encountered, the loop logs a
        warning, sleeps for the ``retry_after`` duration (or a sensible
        default), and continues — it does NOT die on a single rate-limit
        hit.  Only CancelledError (from stop_typing) stops the loop.
        """
        if not self._client:
            return
        # Don't start a duplicate loop
        if chat_id in self._typing_tasks:
            return
    
        async def _typing_loop() -> None:
            try:
                while True:
                    try:
                        route = discord.http.Route(
                            "POST", "/channels/{channel_id}/typing",
                            channel_id=chat_id,
                        )
                        await self._client.http.request(route)
                    except asyncio.CancelledError:
                        return
                    except Exception as e:
                        # Don't die on 429 — backoff and continue
                        retry_after = self._extract_discord_retry_after(e)
                        if retry_after is not None:
                            logger.warning(
                                "Typing indicator rate-limited for %s; retrying in %.1fs",
                                chat_id, retry_after,
                            )
                        else:
                            logger.debug(
                                "Discord typing indicator failed for %s: %s",
                                chat_id, e,
                            )
                            return
                        await asyncio.sleep(retry_after)
                        continue
                    await asyncio.sleep(12)
            except asyncio.CancelledError:
                pass
            finally:
                self._typing_tasks.pop(chat_id, None)
    
        self._typing_tasks[chat_id] = asyncio.create_task(_typing_loop())
    
    async def stop_typing(self, chat_id: str) -> None:
        """Stop the persistent typing indicator for a channel."""
        task = self._typing_tasks.pop(chat_id, None)
        if task:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
    
    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        """Get information about a Discord channel."""
        if not self._client:
            return {"name": "Unknown", "type": "dm"}
    
        try:
            channel = self._client.get_channel(int(chat_id))
            if not channel:
                channel = await self._client.fetch_channel(int(chat_id))
    
            if not channel:
                return {"name": str(chat_id), "type": "dm"}
    
            # Determine channel type
            if isinstance(channel, discord.DMChannel):
                chat_type = "dm"
                name = channel.recipient.name if channel.recipient else str(chat_id)
            elif isinstance(channel, discord.Thread):
                chat_type = "thread"
                name = channel.name
            elif isinstance(channel, discord.TextChannel):
                chat_type = "channel"
                name = f"#{channel.name}"
                if channel.guild:
                    name = f"{channel.guild.name} / {name}"
            else:
                chat_type = "channel"
                name = getattr(channel, "name", str(chat_id))
    
            return {
                "name": name,
                "type": chat_type,
                "guild_id": str(channel.guild.id) if hasattr(channel, "guild") and channel.guild else None,
                "guild_name": channel.guild.name if hasattr(channel, "guild") and channel.guild else None,
            }
        except Exception as e:  # pragma: no cover - defensive logging
            logger.error("[%s] Failed to get chat info for %s: %s", self.name, chat_id, e, exc_info=True)
            return {"name": str(chat_id), "type": "dm", "error": str(e)}
    
    async def _resolve_allowed_usernames(self) -> None:
        """
        Resolve non-numeric entries in DISCORD_ALLOWED_USERS to Discord user IDs.
    
        Users can specify usernames (e.g. "teknium") or display names instead of
        raw numeric IDs.  After resolution, the env var and internal set are updated
        so authorization checks work with IDs only.
        """
        if not self._allowed_user_ids or not self._client:
            return
    
        numeric_ids = set()
        to_resolve = set()
    
        for entry in self._allowed_user_ids:
            if entry.isdigit():
                numeric_ids.add(entry)
            else:
                to_resolve.add(entry.lower())
    
        if not to_resolve:
            return
    
        print(f"[{self.name}] Resolving {len(to_resolve)} username(s): {', '.join(to_resolve)}")
        resolved_count = 0
    
        for guild in self._client.guilds:
            # Fetch full member list (requires members intent)
            try:
                members = guild.members
                if len(members) < guild.member_count:
                    members = [m async for m in guild.fetch_members(limit=None)]
            except Exception as e:
                logger.warning("Failed to fetch members for guild %s: %s", guild.name, e)
                continue
    
            for member in members:
                name_lower = member.name.lower()
                display_lower = member.display_name.lower()
                global_lower = (member.global_name or "").lower()
    
                matched = name_lower in to_resolve or display_lower in to_resolve or global_lower in to_resolve
                if matched:
                    uid = str(member.id)
                    numeric_ids.add(uid)
                    resolved_count += 1
                    matched_name = name_lower if name_lower in to_resolve else (
                        display_lower if display_lower in to_resolve else global_lower
                    )
                    to_resolve.discard(matched_name)
                    print(f"[{self.name}] Resolved '{matched_name}' -> {uid} ({member.name}#{member.discriminator})")
    
            if not to_resolve:
                break
    
        if to_resolve:
            print(f"[{self.name}] Could not resolve usernames: {', '.join(to_resolve)}")
    
        # Update internal set and env var so gateway auth checks use IDs
        self._allowed_user_ids = numeric_ids
        os.environ["DISCORD_ALLOWED_USERS"] = ",".join(sorted(numeric_ids))
        if resolved_count:
            print(f"[{self.name}] Updated DISCORD_ALLOWED_USERS with {resolved_count} resolved ID(s)")
    
    def format_message(self, content: str) -> str:
        """
        Format message for Discord.
    
        Discord uses its own markdown variant.
        """
        # Discord markdown is fairly standard, no special escaping needed
        return content
    
    def _resolve_channel_skills(self, channel_id: str, parent_id: str | None = None) -> list[str] | None:
        """Look up auto-skill bindings for a Discord channel/forum thread.
    
        Config format (in platform extra):
            channel_skill_bindings:
              - id: "123456"
                skills: ["skill-a", "skill-b"]
        Also checks parent_id so forum threads inherit the forum's bindings.
        """
        from channels.platforms.base import resolve_channel_skills
        return resolve_channel_skills(self.config.extra, channel_id, parent_id)
    
    def _resolve_channel_prompt(self, channel_id: str, parent_id: str | None = None) -> str | None:
        """Resolve a Discord per-channel prompt, preferring the exact channel over its parent."""
        from channels.platforms.base import resolve_channel_prompt
        return resolve_channel_prompt(self.config.extra, channel_id, parent_id)
    
    def _discord_require_mention(self) -> bool:
        """Return whether Discord channel messages require a bot mention."""
        configured = self.config.extra.get("require_mention")
        if configured is not None:
            if isinstance(configured, str):
                return configured.lower() not in {"false", "0", "no", "off"}
            return bool(configured)
        return os.getenv("DISCORD_REQUIRE_MENTION", "true").lower() not in {"false", "0", "no", "off"}
    
    def _discord_allow_any_attachment(self) -> bool:
        """Return whether Discord attachments bypass the SUPPORTED_DOCUMENT_TYPES allowlist.
    
        When True, any uploaded file is cached to disk and surfaced to the
        agent as a local path so it can be inspected via terminal / read_file
        / ffprobe / etc. Default False preserves the historical behaviour of
        dropping unsupported types with a warning log.
        """
        configured = self.config.extra.get("allow_any_attachment")
        if configured is not None:
            if isinstance(configured, str):
                return configured.lower() not in {"false", "0", "no", "off", ""}
            return bool(configured)
        return os.getenv("DISCORD_ALLOW_ANY_ATTACHMENT", "false").lower() in {"true", "1", "yes", "on"}
    
    def _discord_max_attachment_bytes(self) -> int:
        """Return the per-attachment byte cap. 0 means unlimited.
    
        The whole attachment is held in memory while being written to the
        cache, so unlimited carries a real memory cost. Default 32 MiB
        matches the historical hardcoded value.
        """
        configured = self.config.extra.get("max_attachment_bytes")
        if configured is None:
            configured = os.getenv("DISCORD_MAX_ATTACHMENT_BYTES")
        if configured is None or configured == "":
            return 32 * 1024 * 1024
        try:
            value = int(configured)
        except (TypeError, ValueError):
            logger.warning(
                "[Discord] Invalid max_attachment_bytes value %r, falling back to 32 MiB",
                configured,
            )
            return 32 * 1024 * 1024
        return max(0, value)
    
    @staticmethod
    def _is_discord_voice_message_attachment(att: Any) -> bool:
        """Return True when a Discord audio attachment is a native voice note."""
        marker = getattr(att, "is_voice_message", None)
        if marker is not None:
            if callable(marker):
                try:
                    return bool(marker())
                except Exception as exc:
                    logger.debug("[Discord] is_voice_message() failed for attachment: %s", exc)
                    return False
            return bool(marker)
    
        return (
            getattr(att, "duration", None) is not None
            and getattr(att, "waveform", None) is not None
        )
    
    def _discord_free_response_channels(self) -> set:
        """Return Discord channel IDs where no bot mention is required.
    
        A single ``"*"`` entry (either from a list or a comma-separated
        string) is preserved in the returned set so callers can short-circuit
        on wildcard membership, consistent with ``allowed_channels``.
        """
        raw = self.config.extra.get("free_response_channels")
        if raw is None:
            raw = os.getenv("DISCORD_FREE_RESPONSE_CHANNELS", "")
        if isinstance(raw, list):
            return {str(part).strip() for part in raw if str(part).strip()}
        # Coerce non-list scalars (str/int/float) to str before splitting.
        # YAML parses a bare numeric value such as
        # `free_response_channels: 1491973769726791812` as int, which was
        # previously falling through the isinstance(str) branch and silently
        # returning an empty set.  str() here accepts whatever scalar the YAML
        # loader hands us without changing existing string/CSV semantics.
        s = str(raw).strip() if raw is not None else ""
        if s:
            return {part.strip() for part in s.split(",") if part.strip()}
        return set()
    
    def _discord_thread_require_mention(self) -> bool:
        """Return whether thread participation requires @mention to follow up.
    
        When ``False`` (default), once the bot has participated in a thread it
        keeps responding to every message in that thread without needing to be
        mentioned again — useful for one-on-one conversations.
    
        When ``True``, the @mention requirement is enforced inside threads as
        well.  Set this when multiple bots share a thread and you want each
        one to only fire on explicit @mention, avoiding bot-to-bot loops or
        unwanted cross-replies.
        """
        configured = self.config.extra.get("thread_require_mention")
        if configured is not None:
            if isinstance(configured, str):
                return configured.lower() not in {"false", "0", "no", "off"}
            return bool(configured)
        return os.getenv("DISCORD_THREAD_REQUIRE_MENTION", "false").lower() in {"true", "1", "yes", "on"}
    
    def _discord_history_backfill(self) -> bool:
        """Return whether history backfill is enabled for shared sessions."""
        configured = self.config.extra.get("history_backfill")
        if configured is not None:
            if isinstance(configured, str):
                return configured.lower() not in {"false", "0", "no", "off"}
            return bool(configured)
        return os.getenv("DISCORD_HISTORY_BACKFILL", "true").lower() in {"true", "1", "yes"}
    
    def _discord_history_backfill_limit(self) -> int:
        """Return the max number of messages to scan backwards for context.
    
        In practice the scan usually stops much earlier — at the bot's own
        last message in the channel (the natural partition point).  This
        limit is a safety cap for cold starts and long gaps where no prior
        bot message exists in recent history.
        """
        configured = self.config.extra.get("history_backfill_limit")
        if configured is not None:
            try:
                return int(configured)
            except (ValueError, TypeError):
                pass
        raw = os.getenv("DISCORD_HISTORY_BACKFILL_LIMIT", "50")
        try:
            return int(raw)
        except (ValueError, TypeError):
            return 50
    
    async def _fetch_channel_context(
        self,
        channel: Any,
        before: "DiscordMessage",
    ) -> str:
        """Fetch recent channel messages for conversational context.
    
        Scans backwards from *before* and collects messages until it hits
        a message sent by this bot (the natural partition point between
        bot turns) or reaches ``history_backfill_limit``.
    
        Returns a formatted block like::
    
            [Recent channel messages]
            [Alice] some message
            [Bob [bot]] another message
    
        Returns an empty string if no context is available.
        """
        limit = self._discord_history_backfill_limit()
        if limit <= 0:
            return ""
    
        # Determine which bot messages to include in context
        allow_bots_raw = os.getenv("DISCORD_ALLOW_BOTS", "none").lower().strip()
        include_other_bots = allow_bots_raw != "none"
    
        # Use the in-memory cache to narrow the fetch window on hot paths.
        # If we know our last message ID in this channel, pass it as `after`
        # to avoid scanning the full limit.  Falls back to scanning on cache
        # miss (cold start / restart).
        # Guard: only use the cache when it's chronologically before the
        # trigger — Discord snowflake IDs are monotonically increasing, so
        # a simple int comparison suffices.
        channel_id = str(getattr(channel, "id", ""))
        _cached_id = self._last_self_message_id.get(channel_id)
        _after_obj = None
        try:
            if _cached_id and int(_cached_id) < int(before.id):
                _after_obj = discord.Object(id=int(_cached_id))
        except (ValueError, TypeError):
            pass  # Malformed cache entry — fall back to cold-start scan
    
        try:
            collected = []
            # IMPORTANT: pass oldest_first=False explicitly.  discord.py 2.x
            # silently flips the default to True when `after=` is supplied,
            # which would select the *earliest* N messages after our last
            # response instead of the *latest* N before the trigger.  In
            # high-traffic windows that returns stale tool traces and drops
            # the actual final answer.  See the regression test
            # `test_fetch_channel_context_cache_uses_latest_window_when_after_set`.
            async for msg in channel.history(
                limit=limit,
                before=before,
                after=_after_obj,
                oldest_first=False,
            ):
                # Stop at our own message — this is the partition point.
                # Everything before this is already in the session transcript.
                # (Redundant when _after_obj is set, but needed for cold start.)
                if msg.author == self._client.user:
                    break
    
                # Skip system messages (pins, joins, thread renames, etc.)
                if msg.type not in {discord.MessageType.default, discord.MessageType.reply}:
                    continue
    
                # Respect DISCORD_ALLOW_BOTS for other bots.
                # For history context, "mentions" is treated as "all" — we are
                # deciding what context to show, not whether to respond.
                if getattr(msg.author, "bot", False) and not include_other_bots:
                    continue
    
                content = getattr(msg, "clean_content", msg.content) or ""
                if not content and msg.attachments:
                    content = "(attachment)"
                if not content:
                    continue
    
                name = msg.author.display_name
                if getattr(msg.author, "bot", False):
                    name = f"{name} [bot]"
                collected.append(f"[{name}] {content}")
    
            if not collected:
                return ""
    
            # channel.history returns newest-first (oldest_first=False); reverse for chronological order
            collected.reverse()
            return "[Recent channel messages]\n" + "\n".join(collected)
    
        except discord.Forbidden:
            logger.debug("[%s] Missing permissions to fetch channel history", self.name)
            return ""
        except Exception as e:
            logger.warning("[%s] Failed to fetch channel history: %s", self.name, e)
            return ""
    
    def _thread_parent_channel(self, channel: Any) -> Any:
        """Return the parent text channel when invoked from a thread."""
        return getattr(channel, "parent", None) or channel
    
    async def _resolve_interaction_channel(self, interaction: discord.Interaction) -> Optional[Any]:
        """Return the interaction channel, fetching it if the payload is partial."""
        channel = getattr(interaction, "channel", None)
        if channel is not None:
            return channel
        if not self._client:
            return None
        channel_id = getattr(interaction, "channel_id", None)
        if channel_id is None:
            return None
        channel = self._client.get_channel(int(channel_id))
        if channel is not None:
            return channel
        try:
            return await self._client.fetch_channel(int(channel_id))
        except Exception:
            return None
    
    async def _create_thread(
        self,
        interaction: discord.Interaction,
        *,
        name: str,
        message: str = "",
        auto_archive_duration: int = 1440,
    ) -> Dict[str, Any]:
        """Create a thread in the current Discord channel.
    
        Tries ``parent_channel.create_thread()`` first.  If Discord rejects
        that (e.g. permission issues), falls back to sending a seed message
        and creating the thread from it.
        """
        name = (name or "").strip()
        if not name:
            return {"error": "Thread name is required."}
    
        if auto_archive_duration not in VALID_THREAD_AUTO_ARCHIVE_MINUTES:
            allowed = ", ".join(str(v) for v in sorted(VALID_THREAD_AUTO_ARCHIVE_MINUTES))
            return {"error": f"auto_archive_duration must be one of: {allowed}."}
    
        channel = await self._resolve_interaction_channel(interaction)
        if channel is None:
            return {"error": "Could not resolve the current Discord channel."}
        if isinstance(channel, discord.DMChannel):
            return {"error": "Discord threads can only be created inside server text channels, not DMs."}
    
        parent_channel = self._thread_parent_channel(channel)
        if parent_channel is None:
            return {"error": "Could not determine a parent text channel for the new thread."}
    
        display_name = getattr(getattr(interaction, "user", None), "display_name", None) or "unknown user"
        reason = f"Requested by {display_name} via /thread"
        starter_message = (message or "").strip()
    
        try:
            thread = await parent_channel.create_thread(
                name=name,
                auto_archive_duration=auto_archive_duration,
                reason=reason,
            )
            if starter_message:
                await thread.send(starter_message)
            return {
                "success": True,
                "thread_id": str(thread.id),
                "thread_name": getattr(thread, "name", None) or name,
            }
        except Exception as direct_error:
            try:
                seed_content = starter_message or f"\U0001f9f5 Thread created by Hermes: **{name}**"
                seed_msg = await parent_channel.send(seed_content)
                thread = await seed_msg.create_thread(
                    name=name,
                    auto_archive_duration=auto_archive_duration,
                    reason=reason,
                )
                return {
                    "success": True,
                    "thread_id": str(thread.id),
                    "thread_name": getattr(thread, "name", None) or name,
                }
            except Exception as fallback_error:
                return {
                    "error": (
                        "Discord rejected direct thread creation and the fallback also failed. "
                        f"Direct error: {direct_error}. Fallback error: {fallback_error}"
                    )
                }
    
    async def _auto_create_thread(self, message: 'DiscordMessage') -> Optional[Any]:
        """Create a thread from a user message for auto-threading.
    
        Returns the created thread object, or ``None`` on failure.
        """
        # Build a short thread name from the message. Strip Discord mention
        # syntax (users / roles / channels) so thread titles don't end up
        # showing raw <@id>, <@&id>, or <#id> markers — the ID isn't
        # meaningful to humans glancing at the thread list (#6336).
        content = (message.content or "").strip()
        # <@123>, <@!123>, <@&123>, <#123> — collapse to empty; normalize spaces.
        content = re.sub(r"<@[!&]?\d+>", "", content)
        content = re.sub(r"<#\d+>", "", content)
        content = re.sub(r"\s+", " ", content).strip()
        thread_name = content[:80] if content else "Hermes"
        if len(content) > 80:
            thread_name = thread_name[:77] + "..."
    
        try:
            thread = await message.create_thread(name=thread_name, auto_archive_duration=1440)
            return thread
        except Exception as direct_error:
            display_name = getattr(getattr(message, "author", None), "display_name", None) or "unknown user"
            reason = f"Auto-threaded from mention by {display_name}"
            try:
                seed_msg = await message.channel.send(f"\U0001f9f5 Thread created by Hermes: **{thread_name}**")
                thread = await seed_msg.create_thread(
                    name=thread_name,
                    auto_archive_duration=1440,
                    reason=reason,
                )
                return thread
            except Exception as fallback_error:
                logger.warning(
                    "[%s] Auto-thread creation failed. Direct error: %s. Fallback error: %s",
                    self.name,
                    direct_error,
                    fallback_error,
                )
                return None
    
    async def create_handoff_thread(
        self,
        parent_chat_id: str,
        name: str,
    ) -> Optional[str]:
        """Create a Discord thread under a text channel for a handoff.
    
        Falls back to a seed-message + ``message.create_thread`` path if
        ``parent.create_thread`` is rejected (some channel types or
        permission setups). Returns the new thread id as a string, or
        ``None`` on failure or when the parent isn't a text channel
        (DMs, voice channels, threads themselves can't host threads).
        """
        if not self._client or not _discord_public_attr("DISCORD_AVAILABLE", DISCORD_AVAILABLE):
            return None
    
        try:
            parent_id = int(parent_chat_id)
        except (TypeError, ValueError):
            return None
    
        try:
            parent = self._client.get_channel(parent_id)
            if parent is None:
                parent = await self._client.fetch_channel(parent_id)
        except Exception as exc:
            logger.warning(
                "[%s] Handoff thread: cannot resolve parent %s: %s",
                self.name, parent_chat_id, exc,
            )
            return None
    
        # DMs, voice channels, and existing threads can't host child threads.
        if isinstance(parent, getattr(discord, "DMChannel", ())):
            logger.info(
                "[%s] Handoff thread: parent %s is a DM; threads not supported here",
                self.name, parent_chat_id,
            )
            return None
    
        thread_name = (name or "handoff").strip()[:80] or "handoff"
        reason = "Hermes session handoff"
    
        # First try: create a thread directly on the channel.
        try:
            create = getattr(parent, "create_thread", None)
            if create is not None:
                thread = await create(
                    name=thread_name,
                    auto_archive_duration=1440,
                    reason=reason,
                )
                return str(thread.id)
        except Exception as direct_error:
            logger.debug(
                "[%s] Handoff thread: direct create failed (%s); trying seed-message fallback",
                self.name, direct_error,
            )
    
        # Fallback: post a seed message and create the thread from it.
        try:
            send = getattr(parent, "send", None)
            if send is None:
                return None
            seed_msg = await send(f"\U0001f9f5 Hermes handoff: **{thread_name}**")
            thread = await seed_msg.create_thread(
                name=thread_name,
                auto_archive_duration=1440,
                reason=reason,
            )
            return str(thread.id)
        except Exception as fallback_error:
            logger.warning(
                "[%s] Handoff thread: both create paths failed for parent %s: %s",
                self.name, parent_chat_id, fallback_error,
            )
            return None
    
    async def send_exec_approval(
        self, chat_id: str, command: str, session_key: str,
        description: str = "dangerous command",
        metadata: Optional[dict] = None,
    ) -> SendResult:
        """
        Send a button-based exec approval prompt for a dangerous command.
    
        The buttons call ``resolve_gateway_approval()`` to unblock the waiting
        agent thread — this replaces the text-based ``/approve`` flow on Discord.
        """
        if not self._client or not _discord_public_attr("DISCORD_AVAILABLE", DISCORD_AVAILABLE):
            return SendResult(success=False, error="Not connected")
    
        try:
            # Resolve channel — use thread_id from metadata if present
            target_id = chat_id
            if metadata and metadata.get("thread_id"):
                target_id = metadata["thread_id"]
    
            channel = self._client.get_channel(int(target_id))
            if not channel:
                channel = await self._client.fetch_channel(int(target_id))
    
            # Discord embed description limit is 4096; show full command up to that
            max_desc = 4088
            cmd_display = command if len(command) <= max_desc else command[: max_desc - 3] + "..."
            embed = discord.Embed(
                title="⚠️ Command Approval Required",
                description=f"```\n{cmd_display}\n```",
                color=discord.Color.orange(),
            )
            embed.add_field(name="Reason", value=description, inline=False)
    
            view = _discord_public_attr("ExecApprovalView")(
                session_key=session_key,
                allowed_user_ids=self._allowed_user_ids,
                allowed_role_ids=self._allowed_role_ids,
            )
    
            msg = await channel.send(embed=embed, view=view)
            return SendResult(success=True, message_id=str(msg.id))
    
        except Exception as e:
            return SendResult(success=False, error=str(e))
    
    async def send_slash_confirm(
        self, chat_id: str, title: str, message: str, session_key: str,
        confirm_id: str, metadata: Optional[dict] = None,
    ) -> SendResult:
        """Send a three-button slash-command confirmation prompt."""
        if not self._client or not _discord_public_attr("DISCORD_AVAILABLE", DISCORD_AVAILABLE):
            return SendResult(success=False, error="Not connected")
    
        try:
            target_id = chat_id
            if metadata and metadata.get("thread_id"):
                target_id = metadata["thread_id"]
    
            channel = self._client.get_channel(int(target_id))
            if not channel:
                channel = await self._client.fetch_channel(int(target_id))
    
            # Embed description limit is 4096; message usually fits easily.
            max_desc = 4088
            body = message if len(message) <= max_desc else message[: max_desc - 3] + "..."
            embed = discord.Embed(
                title=title or "Confirm",
                description=body,
                color=discord.Color.orange(),
            )
    
            view = _discord_public_attr("SlashConfirmView")(
                session_key=session_key,
                confirm_id=confirm_id,
                allowed_user_ids=self._allowed_user_ids,
                allowed_role_ids=self._allowed_role_ids,
            )
    
            msg = await channel.send(embed=embed, view=view)
            return SendResult(success=True, message_id=str(msg.id))
        except Exception as e:
            return SendResult(success=False, error=str(e))
    
    async def send_clarify(
        self,
        chat_id: str,
        question: str,
        choices: Optional[list],
        clarify_id: str,
        session_key: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Render a clarify prompt with one Discord button per choice.
    
        Multi-choice mode (``choices`` non-empty): renders a button per option
        plus a final "✏️ Other (type answer)" button. Picking "Other" flips
        the clarify entry into text-capture mode so the next user message in
        the session becomes the response. Numeric clicks resolve immediately
        via ``resolve_gateway_clarify(clarify_id, choice_text)``.
    
        Open-ended mode (``choices`` empty/None): renders the question as
        plain embed text — no buttons. The gateway's text-intercept captures
        the next message in this session and resolves the clarify.
        """
        if not self._client or not _discord_public_attr("DISCORD_AVAILABLE", DISCORD_AVAILABLE):
            return SendResult(success=False, error="Not connected")
    
        try:
            target_id = chat_id
            if metadata and metadata.get("thread_id"):
                target_id = metadata["thread_id"]
    
            channel = self._client.get_channel(int(target_id))
            if not channel:
                channel = await self._client.fetch_channel(int(target_id))
    
            # Discord embed description limit is 4096; trim conservatively.
            max_desc = 4088
            body = str(question or "").strip()
            if len(body) > max_desc:
                body = body[: max_desc - 3] + "..."
    
            embed = discord.Embed(
                title="❓ Hermes needs your input",
                description=body,
                color=discord.Color.orange(),
            )
    
            clean_choices = [
                str(c).strip() for c in (choices or []) if c is not None and str(c).strip()
            ]
            # Discord allows up to 5 buttons per row, 5 rows per view = 25.
            # We reserve one slot for the "Other" button, so cap at 24 choices.
            clean_choices = clean_choices[:24]
    
            if clean_choices:
                embed.add_field(
                    name="Choices",
                    value="Pick one below, or click ✏️ Other to type a custom answer.",
                    inline=False,
                )
                view = _discord_public_attr("ClarifyChoiceView")(
                    choices=clean_choices,
                    clarify_id=clarify_id,
                    allowed_user_ids=self._allowed_user_ids,
                    allowed_role_ids=self._allowed_role_ids,
                )
            else:
                embed.add_field(
                    name="Reply",
                    value="Reply in this channel with your answer.",
                    inline=False,
                )
                view = None
    
            msg = await channel.send(embed=embed, view=view) if view else await channel.send(embed=embed)
            return SendResult(success=True, message_id=str(msg.id))
        except Exception as e:
            logger.warning("[%s] send_clarify failed: %s", self.name, e)
            return SendResult(success=False, error=str(e))
    
    async def send_update_prompt(
        self, chat_id: str, prompt: str, default: str = "",
        session_key: str = "",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Send an interactive button-based update prompt (Yes / No).
    
        Used by the gateway ``/update`` watcher when ``hermes update --gateway``
        needs user input (stash restore, config migration).
        """
        if not self._client or not _discord_public_attr("DISCORD_AVAILABLE", DISCORD_AVAILABLE):
            return SendResult(success=False, error="Not connected")
        try:
            target_id = metadata.get("thread_id") if metadata and metadata.get("thread_id") else chat_id
            channel = self._client.get_channel(int(target_id))
            if not channel:
                channel = await self._client.fetch_channel(int(target_id))
    
            default_hint = f" (default: {default})" if default else ""
            embed = discord.Embed(
                title="⚕ Update Needs Your Input",
                description=f"{prompt}{default_hint}",
                color=discord.Color.gold(),
            )
            view = _discord_public_attr("UpdatePromptView")(
                session_key=session_key,
                allowed_user_ids=self._allowed_user_ids,
                allowed_role_ids=self._allowed_role_ids,
            )
            msg = await channel.send(embed=embed, view=view)
            return SendResult(success=True, message_id=str(msg.id))
        except Exception as e:
            return SendResult(success=False, error=str(e))
    
    async def send_model_picker(
        self,
        chat_id: str,
        providers: list,
        current_model: str,
        current_provider: str,
        session_key: str,
        on_model_selected,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Send an interactive select-menu model picker.
    
        Two-step drill-down: provider dropdown → model dropdown.
        Uses Discord embeds + Select menus via ``ModelPickerView``.
        """
        if not self._client or not _discord_public_attr("DISCORD_AVAILABLE", DISCORD_AVAILABLE):
            return SendResult(success=False, error="Not connected")
    
        try:
            # Resolve target channel (use thread_id if present)
            target_id = chat_id
            if metadata and metadata.get("thread_id"):
                target_id = metadata["thread_id"]
    
            channel = self._client.get_channel(int(target_id))
            if not channel:
                channel = await self._client.fetch_channel(int(target_id))
    
            try:
                from hermes_cli.providers import get_label
                provider_label = get_label(current_provider)
            except Exception:
                provider_label = current_provider
    
            embed = discord.Embed(
                title="⚙ Model Configuration",
                description=(
                    f"Current model: `{current_model or 'unknown'}`\n"
                    f"Provider: {provider_label}\n\n"
                    f"Select a provider:"
                ),
                color=discord.Color.blue(),
            )
    
            view = _discord_public_attr("ModelPickerView")(
                providers=providers,
                current_model=current_model,
                current_provider=current_provider,
                session_key=session_key,
                on_model_selected=on_model_selected,
                allowed_user_ids=self._allowed_user_ids,
                allowed_role_ids=self._allowed_role_ids,
            )
    
            msg = await channel.send(embed=embed, view=view)
            return SendResult(success=True, message_id=str(msg.id))
    
        except Exception as e:
            logger.warning("[%s] send_model_picker failed: %s", self.name, e)
            return SendResult(success=False, error=str(e))
    
    def _get_parent_channel_id(self, channel: Any) -> Optional[str]:
        """Return the parent channel ID for a Discord thread-like channel, if present."""
        parent = getattr(channel, "parent", None)
        if parent is not None and getattr(parent, "id", None) is not None:
            return str(parent.id)
        parent_id = getattr(channel, "parent_id", None)
        if parent_id is not None:
            return str(parent_id)
        return None
    
    def _is_forum_parent(self, channel: Any) -> bool:
        """Best-effort check for whether a Discord channel is a forum channel."""
        if channel is None:
            return False
        forum_cls = getattr(discord, "ForumChannel", None)
        if forum_cls and isinstance(channel, forum_cls):
            return True
        channel_type = getattr(channel, "type", None)
        if channel_type is not None:
            type_value = getattr(channel_type, "value", channel_type)
            if type_value == 15:
                return True
        return False
    
    def _get_effective_topic(self, channel: Any, is_thread: bool = False) -> Optional[str]:
        """Return the channel topic, falling back to the parent forum's topic for forum threads."""
        topic = getattr(channel, "topic", None)
        if not topic and is_thread:
            parent = getattr(channel, "parent", None)
            if parent and self._is_forum_parent(parent):
                topic = getattr(parent, "topic", None)
        return topic
    
    def _format_thread_chat_name(self, thread: Any) -> str:
        """Build a readable chat name for thread-like Discord channels, including forum context when available."""
        thread_name = getattr(thread, "name", None) or str(getattr(thread, "id", "thread"))
        parent = getattr(thread, "parent", None)
        guild = getattr(thread, "guild", None) or getattr(parent, "guild", None)
        guild_name = getattr(guild, "name", None)
        parent_name = getattr(parent, "name", None)
    
        if self._is_forum_parent(parent) and guild_name and parent_name:
            return f"{guild_name} / {parent_name} / {thread_name}"
        if parent_name and guild_name:
            return f"{guild_name} / #{parent_name} / {thread_name}"
        if parent_name:
            return f"{parent_name} / {thread_name}"
        return thread_name
