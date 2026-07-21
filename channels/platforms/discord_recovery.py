"""Durable Discord missed-message reconciliation."""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import os
import re
import time
from contextlib import suppress
from typing import Any, Optional

from agent.secret_scope import get_profile_env
try:
    import discord
except ImportError:  # pragma: no cover - optional dependency
    discord = None

from channels.platforms.base import MessageEvent, ProcessingOutcome, SendResult
from channels.platforms.discord_recovery_store import DiscordRecoveryStore
from hermes_constants import get_hermes_home

logger = logging.getLogger(__name__)


class DiscordRecoveryMixin:
    """Own scan policy, recovery admission, and durable completion state."""

    def _init_discord_recovery(self) -> None:
        self._missed_message_backfill_task: Optional[asyncio.Task] = None
        self._discord_recovery_store = DiscordRecoveryStore(get_hermes_home())

    def _missed_message_backfill_enabled(self) -> bool:
        configured = self.config.extra.get("missed_message_backfill")
        if isinstance(configured, dict) and "enabled" in configured:
            value = configured["enabled"]
            if isinstance(value, str):
                return value.strip().lower() in {"true", "1", "yes", "on"}
            return bool(value)
        return get_profile_env(
            "DISCORD_MISSED_MESSAGE_BACKFILL",
            "false",
        ).strip().lower() in {"true", "1", "yes", "on"}

    def _missed_message_backfill_channels(self) -> set[str]:
        configured = self.config.extra.get("missed_message_backfill")
        if isinstance(configured, dict) and "channels" in configured:
            raw = configured.get("channels")
            if isinstance(raw, list):
                return {str(item).strip() for item in raw if str(item).strip()}
            raw = str(raw or "")
            if raw.strip():
                return {item.strip() for item in raw.split(",") if item.strip()}
        raw = get_profile_env(
            "DISCORD_MISSED_MESSAGE_BACKFILL_CHANNELS", ""
        )
        if not raw.strip():
            allowed = self._discord_allowed_channel_entries()
            return allowed | self._discord_free_response_channels()
        return {item.strip() for item in raw.split(",") if item.strip()}

    def _missed_message_backfill_window_seconds(self) -> float:
        configured = self.config.extra.get("missed_message_backfill")
        raw = (
            configured.get("window_seconds", 21600)
            if isinstance(configured, dict)
            else get_profile_env(
                "DISCORD_MISSED_MESSAGE_BACKFILL_WINDOW_SECONDS",
                "21600",
            )
        )
        try:
            value = float(raw)
        except (TypeError, ValueError):
            value = 21600.0
        return max(60.0, value)

    def _missed_message_backfill_limit(self) -> int:
        configured = self.config.extra.get("missed_message_backfill")
        raw = (
            configured.get("limit", 100)
            if isinstance(configured, dict)
            else get_profile_env(
                "DISCORD_MISSED_MESSAGE_BACKFILL_LIMIT", "100"
            )
        )
        try:
            value = int(raw)
        except (TypeError, ValueError):
            value = 100
        return max(1, min(value, 500))

    def _missed_message_backfill_max_dispatches(self) -> int:
        configured = self.config.extra.get("missed_message_backfill")
        raw = (
            configured.get("max_dispatches", 10)
            if isinstance(configured, dict)
            else get_profile_env(
                "DISCORD_MISSED_MESSAGE_BACKFILL_MAX_DISPATCHES",
                "10",
            )
        )
        try:
            value = int(raw)
        except (TypeError, ValueError):
            value = 10
        return max(1, min(value, 100))

    def _ensure_missed_message_backfill_task(self) -> asyncio.Task:
        task = self._missed_message_backfill_task
        if task is not None and not task.done():
            return task
        task = asyncio.create_task(self._run_missed_message_backfill())
        self._missed_message_backfill_task = task
        runner = getattr(self, "gateway_runner", None)
        if runner is not None and getattr(
            runner,
            "_startup_restore_in_progress",
            False,
        ):
            tasks = getattr(runner, "_startup_restore_tasks", None)
            if tasks is None:
                tasks = []
                runner._startup_restore_tasks = tasks
            tasks.append(task)
        return task

    async def _cancel_missed_message_backfill_task(self) -> None:
        task = self._missed_message_backfill_task
        if task and not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        self._missed_message_backfill_task = None

    async def _run_missed_message_backfill(self) -> None:
        if not self._client:
            return
        channels = self._missed_message_backfill_channels()
        ledger_ok = await self._with_discord_recovery_db_async(
            lambda connection: connection.execute("SELECT 1").fetchone()
            is not None,
            False,
        )
        if not ledger_ok:
            logger.error(
                "[%s] Missed-message recovery aborted: durable ledger unavailable",
                self.name,
            )
            return
        scan_id = await asyncio.to_thread(
            self._record_recovery_scan_start,
            channels,
        )
        if not channels:
            logger.info(
                "[%s] Missed-message backfill enabled but no channels configured",
                self.name,
            )
            await asyncio.to_thread(
                self._record_recovery_scan_complete,
                scan_id,
                status="skipped",
                scanned=0,
                missed=0,
                dispatched=0,
            )
            return
        max_dispatches = self._missed_message_backfill_max_dispatches()
        dispatched = scanned = missed = 0
        try:
            async for message in self._iter_missed_message_backfill_candidates(
                channels
            ):
                scanned += 1
                message_id = str(getattr(message, "id", ""))
                await asyncio.to_thread(
                    self._record_discord_message_seen,
                    message,
                    status="discovered",
                )
                if self._dedup.contains(message_id):
                    continue
                if not await self._should_backfill_discord_message(message):
                    continue
                missed += 1
                logger.info(
                    "[%s] Backfilling missed Discord message %s in channel %s",
                    self.name,
                    getattr(message, "id", "unknown"),
                    getattr(getattr(message, "channel", None), "id", "unknown"),
                )
                await asyncio.to_thread(
                    self._record_recovery_attempt,
                    message,
                    status="queued",
                )
                try:
                    admitted = await self._dispatch_recovered_message(message)
                    if admitted:
                        dispatched += 1
                except asyncio.CancelledError:
                    self._dedup.discard(message_id)
                    await asyncio.to_thread(
                        self._record_recovery_attempt,
                        message,
                        status="cancelled",
                    )
                    raise
                except Exception as error:
                    self._dedup.discard(message_id)
                    await asyncio.to_thread(
                        self._record_recovery_attempt,
                        message,
                        status="failed",
                        error=str(error),
                    )
                    raise
                if dispatched >= max_dispatches:
                    break
            await asyncio.to_thread(
                self._record_recovery_scan_complete,
                scan_id,
                status="success",
                scanned=scanned,
                missed=missed,
                dispatched=dispatched,
            )
            logger.info(
                "[%s] Missed-message backfill complete: scanned=%d missed=%d dispatched=%d",
                self.name,
                scanned,
                missed,
                dispatched,
            )
        except asyncio.CancelledError:
            await asyncio.to_thread(
                self._record_recovery_scan_complete,
                scan_id,
                status="cancelled",
                scanned=scanned,
                missed=missed,
                dispatched=dispatched,
            )
            raise
        except Exception as error:  # pragma: no cover - defensive logging
            await asyncio.to_thread(
                self._record_recovery_scan_complete,
                scan_id,
                status="failed",
                scanned=scanned,
                missed=missed,
                dispatched=dispatched,
                error=str(error),
            )
            logger.warning(
                "[%s] Missed-message backfill failed: %s",
                self.name,
                error,
            )

    async def _dispatch_recovered_message(self, message: Any) -> bool:
        if not isinstance(message.channel, discord.DMChannel):
            parent_id = self._get_parent_channel_id(message.channel)
            channel_keys = self._discord_channel_keys(message, parent_id)
            free_channels = self._discord_free_response_channels()
            in_bot_thread = (
                isinstance(message.channel, discord.Thread)
                and str(message.channel.id) in self._threads
                and not self._discord_thread_require_mention()
            )
            if (
                self._discord_require_mention()
                and "*" not in free_channels
                and not (channel_keys & free_channels)
                and not in_bot_thread
                and not self._self_is_explicitly_mentioned(message)
            ):
                return False
        admitted, role_authorized = self._discord_message_admission(
            message,
            claim=False,
        )
        if not admitted:
            return False
        return await self._handle_message(
            message,
            role_authorized=role_authorized,
            recovered=True,
        )

    async def _iter_missed_message_backfill_candidates(
        self,
        channel_ids: set[str],
    ):
        if not self._client:
            return
        after = dt.datetime.now(dt.timezone.utc) - dt.timedelta(
            seconds=self._missed_message_backfill_window_seconds()
        )
        limit = self._missed_message_backfill_limit()
        seen: set[str] = set()
        candidate_channels = []
        if "*" in channel_ids:
            for guild in getattr(self._client, "guilds", []) or []:
                candidate_channels.extend(
                    getattr(guild, "text_channels", []) or []
                )
        else:
            for channel_id in sorted(channel_ids):
                try:
                    channel = self._client.get_channel(int(channel_id))
                except Exception:
                    channel = None
                if channel is None:
                    try:
                        channel = await self._client.fetch_channel(int(channel_id))
                    except Exception as error:
                        logger.debug(
                            "[%s] Cannot fetch backfill channel %s: %s",
                            self.name,
                            channel_id,
                            error,
                        )
                        continue
                candidate_channels.append(channel)
        iterators = [
            self._iter_channel_and_thread_messages(
                channel,
                limit=limit,
                after=after,
                seen_channels=seen,
            ).__aiter__()
            for channel in candidate_channels
        ]
        yielded = 0
        while iterators and yielded < limit:
            next_round = []
            for iterator in iterators:
                try:
                    item = await iterator.__anext__()
                except StopAsyncIteration:
                    continue
                yield item
                yielded += 1
                next_round.append(iterator)
                if yielded >= limit:
                    return
            iterators = next_round

    async def _iter_channel_and_thread_messages(
        self,
        channel: Any,
        *,
        limit: int,
        after: Any,
        seen_channels: set[str],
    ):
        channel_key = str(getattr(channel, "id", ""))
        if not channel_key or channel_key in seen_channels:
            return
        seen_channels.add(channel_key)
        cursor = self._discord_recovery_cursor(channel_key)
        if cursor:
            with suppress(ValueError, TypeError):
                after = discord.Object(id=int(cursor))
        history = getattr(channel, "history", None)
        if callable(history):
            try:
                messages = []
                async for message in history(
                    limit=limit,
                    after=after,
                    oldest_first=False,
                ):
                    messages.append(message)
                for message in reversed(messages):
                    yield message
            except Exception as error:
                logger.debug(
                    "[%s] Cannot read history for %s: %s",
                    self.name,
                    channel_key,
                    error,
                )
        child_threads = list(getattr(channel, "threads", []) or [])
        archived_threads = getattr(channel, "archived_threads", None)
        if callable(archived_threads):
            try:
                async for thread in archived_threads(limit=limit):
                    child_threads.append(thread)
            except Exception as error:
                logger.debug(
                    "[%s] Cannot list archived threads for %s: %s",
                    self.name,
                    channel_key,
                    error,
                )
        for thread in child_threads:
            thread_key = str(getattr(thread, "id", ""))
            if not thread_key or thread_key in seen_channels:
                continue
            async for message in self._iter_channel_and_thread_messages(
                thread,
                limit=limit,
                after=after,
                seen_channels=seen_channels,
            ):
                yield message

    def _discord_recovery_cursor(self, channel_id: str) -> Optional[str]:
        if not channel_id:
            return None

        def _operation(connection):
            row = connection.execute(
                "SELECT last_message_id FROM discord_recovery_cursors "
                "WHERE channel_id=?",
                (channel_id,),
            ).fetchone()
            return str(row[0]) if row else None

        return self._with_discord_recovery_db(_operation)

    def _advance_discord_recovery_cursor(
        self,
        channel_id: str,
        message_id: str,
    ) -> None:
        if not channel_id or not message_id:
            return
        now = self._utc_now_iso()

        def _operation(connection):
            connection.execute(
                """
                INSERT INTO discord_recovery_cursors
                    (channel_id, last_message_id, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(channel_id) DO UPDATE SET
                    last_message_id=excluded.last_message_id,
                    updated_at=excluded.updated_at
                """,
                (channel_id, message_id, now),
            )

        self._with_discord_recovery_db(_operation)

    async def _should_backfill_discord_message(self, message: Any) -> bool:
        if not self._client or not getattr(self._client, "user", None):
            return False
        if getattr(getattr(message, "author", None), "id", None) == getattr(
            self._client.user,
            "id",
            None,
        ):
            return False
        message_id = str(getattr(message, "id", ""))
        complete = await asyncio.to_thread(
            self._discord_message_is_persistently_complete,
            message_id,
        )
        if complete:
            return False
        claimed = await asyncio.to_thread(
            self._discord_message_has_active_claim,
            message_id,
        )
        if claimed:
            return False
        return not await self._message_has_non_down_bot_response(message)

    @staticmethod
    def _is_down_notice_content(content: str) -> bool:
        text = (content or "").lower()
        subject = r"(?:hermes|the agent|agent|the gateway|gateway|bmo)"
        state = r"(?:is|was|appears to be|is currently|was currently)"
        condition = r"(?:down|offline|unavailable|not running)"
        return re.search(
            rf"\b{subject}\s+{state}\s+{condition}\b",
            text,
        ) is not None

    async def _message_has_non_down_bot_response(self, message: Any) -> bool:
        bot_user = getattr(self._client, "user", None) if self._client else None
        bot_id = getattr(bot_user, "id", None)
        if bot_id is None:
            return False

        async def _scan_history(channel: Any) -> bool:
            history = getattr(channel, "history", None)
            if not callable(history):
                return False
            try:
                async for candidate in history(
                    limit=25,
                    after=getattr(message, "created_at", None),
                    oldest_first=True,
                ):
                    if getattr(getattr(candidate, "author", None), "id", None) != bot_id:
                        continue
                    if self._is_down_notice_content(
                        getattr(candidate, "content", "")
                    ):
                        continue
                    reference = getattr(candidate, "reference", None)
                    if str(getattr(reference, "message_id", "") or "") == str(
                        getattr(message, "id", "")
                    ):
                        return True
            except Exception:
                return False
            return False

        if await _scan_history(getattr(message, "channel", None)):
            return True
        thread = getattr(message, "thread", None)
        return thread is not None and await _scan_history(thread)

    def _discord_recovery_db_path(self):
        return self._discord_recovery_store.path()

    def _with_discord_recovery_db(self, operation, default=None):
        return self._discord_recovery_store.call(operation, default)

    async def _with_discord_recovery_db_async(self, operation, default=None):
        return await asyncio.to_thread(
            self._discord_recovery_store.call,
            operation,
            default,
        )

    @staticmethod
    def _utc_now_iso() -> str:
        return dt.datetime.now(dt.timezone.utc).isoformat()

    @staticmethod
    def _message_channel_ids(
        message: Any,
    ) -> tuple[str, Optional[str], Optional[str]]:
        channel = getattr(message, "channel", None)
        channel_id = str(getattr(channel, "id", "") or "")
        parent_id = str(getattr(channel, "parent_id", "") or "") or None
        return channel_id, channel_id if parent_id else None, parent_id

    def _record_discord_message_seen(self, message: Any, *, status: str) -> None:
        if not self._missed_message_backfill_enabled():
            return
        message_id = str(getattr(message, "id", "") or "")
        if not message_id:
            return
        channel_id, thread_id, parent_id = self._message_channel_ids(message)
        author_id = str(
            getattr(getattr(message, "author", None), "id", "") or ""
        )
        created_at = getattr(message, "created_at", None)
        created_text = (
            created_at.isoformat() if hasattr(created_at, "isoformat") else None
        )
        now = self._utc_now_iso()

        def _operation(connection):
            existing = connection.execute(
                "SELECT status FROM discord_messages WHERE message_id=?",
                (message_id,),
            ).fetchone()
            final_status = (
                existing[0]
                if existing and existing[0] == "responded"
                else status
            )
            connection.execute(
                """
                INSERT INTO discord_messages
                    (message_id, channel_id, thread_id, parent_channel_id,
                     author_id, created_at, status, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(message_id) DO UPDATE SET
                    channel_id=excluded.channel_id,
                    thread_id=excluded.thread_id,
                    parent_channel_id=excluded.parent_channel_id,
                    author_id=excluded.author_id,
                    created_at=COALESCE(
                        discord_messages.created_at,
                        excluded.created_at
                    ),
                    status=?,
                    updated_at=excluded.updated_at
                """,
                (
                    message_id,
                    channel_id,
                    thread_id,
                    parent_id,
                    author_id,
                    created_text,
                    final_status,
                    now,
                    final_status,
                ),
            )

        self._with_discord_recovery_db(_operation)

    def _record_recovery_attempt(
        self,
        message: Any,
        *,
        status: str,
        error: Optional[str] = None,
    ) -> None:
        if not self._missed_message_backfill_enabled():
            return
        self._record_discord_message_seen(message, status=status)
        message_id = str(getattr(message, "id", "") or "")
        if not message_id:
            return
        now = self._utc_now_iso()

        def _operation(connection):
            connection.execute(
                """
                UPDATE discord_messages
                   SET status=?, attempts=attempts+1, last_attempt_at=?,
                       last_error=?, updated_at=?
                 WHERE message_id=?
                """,
                (status, now, error, now, message_id),
            )

        self._with_discord_recovery_db(_operation)

    def _record_discord_processing_start(
        self,
        event: MessageEvent,
        *,
        emoji_ack: bool,
    ) -> None:
        if not self._missed_message_backfill_enabled():
            return
        message = event.raw_message
        self._record_discord_message_seen(message, status="processing")
        message_id = str(
            getattr(message, "id", "") or getattr(event, "message_id", "") or ""
        )
        if not message_id:
            return
        now = self._utc_now_iso()

        def _operation(connection):
            connection.execute(
                "UPDATE discord_messages SET status='processing', emoji_ack=?, "
                "updated_at=? WHERE message_id=?",
                (1 if emoji_ack else 0, now, message_id),
            )

        self._with_discord_recovery_db(_operation)

    def _record_discord_processing_complete(
        self,
        event: MessageEvent,
        outcome: ProcessingOutcome,
    ) -> None:
        if not self._missed_message_backfill_enabled():
            return
        message_id = str(
            getattr(getattr(event, "raw_message", None), "id", "")
            or getattr(event, "message_id", "")
            or ""
        )
        if not message_id:
            return
        status = (
            "processed"
            if outcome == ProcessingOutcome.SUCCESS
            else "cancelled"
            if outcome == ProcessingOutcome.CANCELLED
            else "failed"
        )
        now = self._utc_now_iso()

        def _operation(connection):
            connection.execute(
                "UPDATE discord_messages SET status=CASE WHEN status='responded' "
                "THEN status ELSE ? END, updated_at=? WHERE message_id=?",
                (status, now, message_id),
            )

        self._with_discord_recovery_db(_operation)

    def _record_discord_response(
        self,
        *,
        reply_to: Optional[str],
        result: SendResult,
        content: str,
        final: bool,
    ) -> None:
        del content
        if not self._missed_message_backfill_enabled() or not reply_to:
            return
        now = self._utc_now_iso()
        completed = bool(final and result.success)
        status = "responded" if completed else "failed"

        def _operation(connection):
            connection.execute(
                """
                INSERT INTO discord_messages
                    (message_id, status, replied, outage_response,
                     response_message_id, updated_at)
                VALUES (?, ?, ?, 0, ?, ?)
                ON CONFLICT(message_id) DO UPDATE SET
                    status=CASE WHEN ? THEN 'responded'
                        ELSE discord_messages.status END,
                    replied=CASE WHEN ? THEN 1
                        ELSE discord_messages.replied END,
                    outage_response=CASE WHEN ? THEN 0
                        ELSE discord_messages.outage_response END,
                    response_message_id=COALESCE(?, response_message_id),
                    updated_at=?
                """,
                (
                    reply_to,
                    status,
                    1 if completed else 0,
                    result.message_id,
                    now,
                    1 if completed else 0,
                    1 if completed else 0,
                    1 if completed else 0,
                    result.message_id,
                    now,
                ),
            )

        self._with_discord_recovery_db(_operation)
        if not completed:
            return

        def _channel_for_message(connection):
            row = connection.execute(
                "SELECT COALESCE(thread_id, channel_id) FROM discord_messages "
                "WHERE message_id=?",
                (reply_to,),
            ).fetchone()
            return str(row[0]) if row and row[0] else None

        channel_id = self._with_discord_recovery_db(_channel_for_message)
        if channel_id:
            self._advance_discord_recovery_cursor(channel_id, reply_to)

    def _discord_message_is_persistently_complete(self, message_id: str) -> bool:
        if not message_id:
            return False

        def _operation(connection):
            row = connection.execute(
                "SELECT status, replied, outage_response FROM discord_messages "
                "WHERE message_id=?",
                (message_id,),
            ).fetchone()
            if not row:
                return False
            status, replied, outage = row
            return status == "responded" and bool(replied) and not bool(outage)

        return bool(self._with_discord_recovery_db(_operation, default=False))

    def _discord_message_has_active_claim(self, message_id: str) -> bool:
        if not message_id:
            return False
        cutoff = (
            dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=10)
        ).isoformat()

        def _operation(connection):
            row = connection.execute(
                "SELECT status, updated_at FROM discord_messages "
                "WHERE message_id=?",
                (message_id,),
            ).fetchone()
            return bool(
                row
                and row[0] in {"queued", "processing"}
                and row[1] >= cutoff
            )

        return bool(self._with_discord_recovery_db(_operation, default=True))

    def _record_recovery_scan_start(self, channels: set[str]) -> str:
        scan_id = f"{int(time.time() * 1000)}-{os.getpid()}"
        now = self._utc_now_iso()

        def _operation(connection):
            connection.execute(
                "INSERT OR REPLACE INTO discord_recovery_scans "
                "(scan_id, started_at, status, channels, window_seconds, "
                "limit_count) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    scan_id,
                    now,
                    "running",
                    json.dumps(sorted(channels)),
                    self._missed_message_backfill_window_seconds(),
                    self._missed_message_backfill_limit(),
                ),
            )

        self._with_discord_recovery_db(_operation)
        return scan_id

    def _record_recovery_scan_complete(
        self,
        scan_id: str,
        *,
        status: str,
        scanned: int,
        missed: int,
        dispatched: int,
        error: Optional[str] = None,
    ) -> None:
        now = self._utc_now_iso()

        def _operation(connection):
            connection.execute(
                "UPDATE discord_recovery_scans SET completed_at=?, status=?, "
                "scanned=?, missed=?, dispatched=?, error=? WHERE scan_id=?",
                (
                    now,
                    status,
                    scanned,
                    missed,
                    dispatched,
                    error,
                    scan_id,
                ),
            )

        self._with_discord_recovery_db(_operation)
