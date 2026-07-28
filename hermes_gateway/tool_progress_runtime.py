"""Tool-progress delivery runtime for gateway agent turns."""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import queue
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from agent.async_utils import safe_schedule_threadsafe
from channels.platforms.base import BasePlatformAdapter
from hermes_cli.config import cfg_get
from hermes_constants import get_hermes_home
from hermes_gateway.config import Platform
from hermes_gateway.display_config import resolve_display_setting
from utils import is_truthy_value

logger = logging.getLogger(__name__)


class ToolProgressRuntime:
    """Owns per-turn tool-progress callbacks, delivery, and cleanup."""

    LONG_TOOL_THRESHOLD_S = 30.0
    PROGRESS_EDIT_INTERVAL = 1.5

    def __init__(
        self,
        *,
        runner,
        user_config: dict[str, Any],
        platform_key: str,
        source,
        event_message_id: str | None,
        run_still_current: Callable[[], bool],
        agent_provider: Callable[[], Any],
        load_gateway_config: Callable[[], dict[str, Any]],
        hermes_home: Path | None = None,
    ) -> None:
        self._runner = runner
        self._user_config = user_config
        self._platform_key = platform_key
        self._source = source
        self._event_message_id = event_message_id
        self._run_still_current = run_still_current
        self._agent_provider = agent_provider
        self._load_gateway_config = load_gateway_config
        self._hermes_home = hermes_home or get_hermes_home()

        display_config = user_config.get("display", {})
        if not isinstance(display_config, dict):
            display_config = {}
        self._display_config = display_config

        resolved = resolve_display_setting(user_config, platform_key, "tool_progress")
        env_value = os.getenv("HERMES_TOOL_PROGRESS_MODE")
        platform_cfg = (display_config.get("platforms") or {}).get(platform_key) or {}
        legacy_overrides = display_config.get("tool_progress_overrides") or {}
        configured = (
            "tool_progress" in display_config
            or (isinstance(platform_cfg, dict) and "tool_progress" in platform_cfg)
            or (isinstance(legacy_overrides, dict) and platform_key in legacy_overrides)
        )
        self.mode = env_value if env_value and not configured else (resolved or env_value or "all")
        self.enabled = self.mode != "off" and source.platform != Platform.WEBHOOK

        self.live_status_mode = str(
            resolve_display_setting(user_config, platform_key, "live_status", "full")
            or "full"
        ).strip().lower()
        live_status_adapter = runner.adapters.get(source.platform)
        self.live_status_adapter = (
            live_status_adapter
            if self.live_status_mode != "off"
            and getattr(live_status_adapter, "supports_status_text", False)
            else None
        )
        self.callback_enabled = self.enabled or self.live_status_adapter is not None

        self.queue: queue.Queue | None = queue.Queue() if self.enabled else None
        self._last_tool: str | None = None
        self._last_progress_msg: str | None = None
        self._repeat_count = 0
        self._long_tool_hint_fired = False

        cleanup = bool(resolve_display_setting(user_config, platform_key, "cleanup_progress"))
        adapter = runner.adapters.get(source.platform) if cleanup else None
        if adapter is not None and type(adapter).delete_message is BasePlatformAdapter.delete_message:
            cleanup = False
            adapter = None
        self.cleanup_enabled = cleanup
        self.cleanup_adapter = adapter
        self.cleanup_message_ids: list[str] = []

        if source.platform == Platform.SLACK:
            self.thread_id = source.thread_id or event_message_id
        else:
            self.thread_id = source.thread_id
        self.metadata = (
            runner._thread_metadata_for_source(source, event_message_id)
            if self.thread_id == source.thread_id
            else {"thread_id": self.thread_id}
        ) if self.thread_id else None
        self.reply_to = (
            event_message_id
            if source.platform in (Platform.FEISHU, Platform.MATTERMOST)
            and source.thread_id
            and event_message_id
            else None
        )

    @property
    def status_thread_metadata(self) -> dict[str, Any] | None:
        if (
            self._source.platform == Platform.FEISHU
            and self._source.thread_id
            and self._event_message_id
        ):
            return {
                "thread_id": self.thread_id,
                "reply_to_message_id": self._event_message_id,
            }
        return self.metadata if self.thread_id else None

    def track_message_result(self, result: Any) -> None:
        if (
            self.cleanup_enabled
            and getattr(result, "success", False)
            and getattr(result, "message_id", None)
        ):
            self.cleanup_message_ids.append(str(result.message_id))

    def callback(
        self,
        event_type: str,
        tool_name: str | None = None,
        preview: str | None = None,
        args: dict | None = None,
        **kwargs,
    ) -> None:
        """Callback invoked by agent on tool lifecycle events."""
        self._update_live_status(event_type, tool_name, args)
        if not self.queue or not self._run_still_current():
            return

        if event_type == "tool.completed" and not self._long_tool_hint_fired:
            self._maybe_enqueue_long_tool_hint(kwargs)
            return

        if event_type != "tool.started":
            return

        agent = self._safe_current_agent()
        if agent is not None and getattr(agent, "is_interrupted", False):
            return

        if self.mode == "new" and tool_name == self._last_tool:
            return
        self._last_tool = tool_name

        msg = self._format_progress_message(tool_name, preview, args)
        if msg == self._last_progress_msg:
            self._repeat_count += 1
            self.queue.put(("__dedup__", msg, self._repeat_count))
            return

        self._last_progress_msg = msg
        self._repeat_count = 0
        self.queue.put(msg)

    def clear_live_status(self) -> None:
        """Clear this turn's live status without clobbering a newer turn."""
        if self.live_status_adapter is None or not self._run_still_current():
            return
        self.live_status_adapter.set_status_text(self._source.chat_id, None)

    def _update_live_status(
        self,
        event_type: str,
        tool_name: str | None,
        args: dict | None,
    ) -> None:
        adapter = self.live_status_adapter
        if adapter is None or tool_name == "_thinking" or not self._run_still_current():
            return
        try:
            if event_type == "tool.started" and tool_name:
                from agent.display import build_status_phrase

                phrase_args = args if self.live_status_mode == "full" else None
                adapter.set_status_text(
                    self._source.chat_id,
                    build_status_phrase(tool_name, phrase_args),
                )
            elif event_type == "tool.completed":
                adapter.set_status_text(self._source.chat_id, None)
        except Exception as exc:
            logger.debug("live status update failed: %s", exc)

    def reset_current_bubble(self) -> None:
        if self.queue is not None:
            self.queue.put(("__reset__",))

    async def send_messages(self) -> None:
        if not self.queue:
            return

        adapter = self._runner.adapters.get(self._source.platform)
        if not adapter:
            return

        if type(adapter).edit_message is BasePlatformAdapter.edit_message:
            self._drain_queue()
            return

        sender = _ProgressSender(
            adapter=adapter,
            source=self._source,
            progress_queue=self.queue,
            metadata=self.metadata,
            reply_to=self.reply_to,
            run_still_current=self._run_still_current,
            agent_provider=self._safe_current_agent,
            track_result=self.track_message_result,
            reset_dedup=self._reset_dedup,
        )
        await sender.run()

    def register_cleanup_callback(
        self,
        *,
        response: dict[str, Any] | Any,
        session_key: str | None,
        run_generation: int | None,
        loop: asyncio.AbstractEventLoop,
    ) -> None:
        if (
            not self.cleanup_enabled
            or self.cleanup_adapter is None
            or not self.cleanup_message_ids
            or not session_key
            or not isinstance(response, dict)
            or response.get("failed")
            or not hasattr(self.cleanup_adapter, "register_post_delivery_callback")
        ):
            return

        ids_snapshot = list(self.cleanup_message_ids)
        chat_id = self._source.chat_id
        adapter = self.cleanup_adapter

        def cleanup_temp_bubbles() -> None:
            async def delete_all() -> None:
                for message_id in ids_snapshot:
                    try:
                        await adapter.delete_message(chat_id, message_id)
                    except Exception as exc:
                        logger.debug(
                            "Temp bubble cleanup delete failed for %s: %s",
                            message_id,
                            exc,
                        )

            safe_schedule_threadsafe(
                delete_all(),
                loop,
                logger=logger,
                log_message="Temp bubble cleanup scheduling error",
            )

        try:
            adapter.register_post_delivery_callback(
                session_key,
                cleanup_temp_bubbles,
                generation=run_generation,
            )
        except Exception as exc:
            logger.debug("Post-delivery cleanup registration failed: %s", exc)

    def _maybe_enqueue_long_tool_hint(self, kwargs: dict[str, Any]) -> None:
        try:
            duration = kwargs.get("duration") or 0
            if duration < self.LONG_TOOL_THRESHOLD_S or self.mode != "all":
                return
            from agent.onboarding import (
                TOOL_PROGRESS_FLAG,
                is_seen,
                mark_seen,
                tool_progress_hint_gateway,
            )

            config = self._load_gateway_config()
            gate_on = is_truthy_value(
                cfg_get(config, "display", "tool_progress_command"),
                default=False,
            )
            if gate_on and not is_seen(config, TOOL_PROGRESS_FLAG):
                self._long_tool_hint_fired = True
                self.queue.put(tool_progress_hint_gateway())
                mark_seen(self._hermes_home / "config.yaml", TOOL_PROGRESS_FLAG)
        except Exception as exc:
            logger.debug("tool-progress onboarding hint failed: %s", exc)

    def _format_progress_message(
        self,
        tool_name: str | None,
        preview: str | None,
        args: dict | None,
    ) -> str:
        from agent.display import get_tool_emoji, get_tool_preview_max_len

        emoji = get_tool_emoji(tool_name, default="⚙️")
        if self.mode == "verbose":
            if args:
                preview_len = get_tool_preview_max_len()
                args_str = json.dumps(args, ensure_ascii=False, default=str)
                if preview_len > 0 and len(args_str) > preview_len:
                    args_str = args_str[:preview_len - 3] + "..."
                return f"{emoji} {tool_name}({list(args.keys())})\n{args_str}"
            if preview:
                return f"{emoji} {tool_name}: \"{preview}\""
            return f"{emoji} {tool_name}..."

        if preview:
            preview_len = get_tool_preview_max_len()
            cap = preview_len if preview_len > 0 else 40
            if len(preview) > cap:
                preview = preview[:cap - 3] + "..."
            return f"{emoji} {tool_name}: \"{preview}\""
        return f"{emoji} {tool_name}..."

    def _safe_current_agent(self) -> Any:
        try:
            return self._agent_provider()
        except Exception as exc:
            logger.debug("tool-progress agent lookup failed: %s", exc)
            return None

    def _reset_dedup(self) -> None:
        self._last_progress_msg = None
        self._repeat_count = 0

    def _drain_queue(self) -> None:
        if not self.queue:
            return
        while not self.queue.empty():
            try:
                self.queue.get_nowait()
            except queue.Empty:
                break


class _ProgressSender:
    def __init__(
        self,
        *,
        adapter,
        source,
        progress_queue: queue.Queue,
        metadata: dict[str, Any] | None,
        reply_to: str | None,
        run_still_current: Callable[[], bool],
        agent_provider: Callable[[], Any],
        track_result: Callable[[Any], None],
        reset_dedup: Callable[[], None],
    ) -> None:
        self.adapter = adapter
        self.source = source
        self.queue = progress_queue
        self.metadata = metadata
        self.reply_to = reply_to
        self.run_still_current = run_still_current
        self.agent_provider = agent_provider
        self.track_result = track_result
        self.reset_dedup = reset_dedup

        self.progress_lines: list[Any] = []
        self.progress_msg_id: str | None = None
        self.can_edit = True
        self.last_edit_ts = 0.0

        self.progress_len_fn = (
            adapter.message_len_fn
            if isinstance(adapter, BasePlatformAdapter)
            else len
        )
        try:
            raw_limit = int(getattr(adapter, "MAX_MESSAGE_LENGTH", 4000) or 4000)
        except Exception:
            raw_limit = 4000
        self.text_limit = max(1, raw_limit - (64 if raw_limit > 128 else 0))
        self.edit_accepts_metadata = self._edit_accepts_metadata()

    async def run(self) -> None:
        while True:
            try:
                if not self.run_still_current():
                    self._drain_queue()
                    return

                raw = self.queue.get_nowait()
                if self._is_interrupted():
                    await asyncio.sleep(0)
                    continue

                should_continue = await self._handle_raw(raw)
                if should_continue:
                    continue

                if await self._roll_overflow_if_needed():
                    self.last_edit_ts = time.monotonic()
                    await self._send_typing_after_pause()
                    continue

                remaining = ToolProgressRuntime.PROGRESS_EDIT_INTERVAL - (
                    time.monotonic() - self.last_edit_ts
                )
                if remaining > 0:
                    await asyncio.sleep(remaining)
                    continue

                if not self.run_still_current():
                    return

                await self._flush_current_message()
                self.last_edit_ts = time.monotonic()
                await self._send_typing_after_pause()

            except queue.Empty:
                await asyncio.sleep(0.3)
            except asyncio.CancelledError:
                await self._drain_on_cancel()
                return
            except Exception as exc:
                logger.error("Progress message error: %s", exc)
                await asyncio.sleep(1)

    async def _handle_raw(self, raw: Any) -> bool:
        if isinstance(raw, tuple) and len(raw) == 3 and raw[0] == "__dedup__":
            _, base_msg, count = raw
            if self.progress_lines:
                self.progress_lines[-1] = f"{base_msg} (×{count + 1})"
            return False

        if isinstance(raw, tuple) and len(raw) >= 1 and raw[0] == "__reset__":
            self.progress_msg_id = None
            self.progress_lines = []
            self.reset_dedup()
            return True

        self.progress_lines.append(raw)
        return False

    async def _flush_current_message(self) -> None:
        if self.can_edit and self.progress_msg_id is not None:
            full_text = self._progress_text(self.progress_lines)
            result = await self._edit_progress_message(self.progress_msg_id, full_text)
            if result.success:
                return
            error = (getattr(result, "error", "") or "").lower()
            if getattr(result, "retryable", False):
                logger.debug("[%s] Transient edit failure; keeping edits enabled", self.adapter.name)
                return
            if "flood" in error or "retry after" in error:
                logger.info("[%s] Progress edit flood control, backing off", self.adapter.name)
                self.last_edit_ts = time.monotonic()
            else:
                self.can_edit = False
            flood_result = await self._send_progress_text(str(self.progress_lines[-1]))
            self.track_result(flood_result)
            return

        text = self._progress_text(self.progress_lines) if self.can_edit else str(self.progress_lines[-1])
        result = await self._send_progress_text(text)
        if result.success and result.message_id:
            self.progress_msg_id = result.message_id
            self.track_result(result)

    async def _drain_on_cancel(self) -> None:
        while not self.queue.empty():
            try:
                raw = self.queue.get_nowait()
                if isinstance(raw, tuple) and len(raw) == 3 and raw[0] == "__dedup__":
                    _, base_msg, count = raw
                    if self.progress_lines:
                        self.progress_lines[-1] = f"{base_msg} (×{count + 1})"
                        await self._roll_overflow_if_needed()
                elif isinstance(raw, tuple) and len(raw) >= 1 and raw[0] == "__reset__":
                    await self._roll_overflow_if_needed()
                    if self.can_edit and self.progress_lines and self.progress_msg_id:
                        pending_text = self._progress_text(self.progress_lines)
                        try:
                            await self._edit_progress_message(self.progress_msg_id, pending_text)
                        except Exception as exc:
                            logger.debug("Final progress edit before reset failed: %s", exc)
                    self.progress_msg_id = None
                    self.progress_lines = []
                    self.reset_dedup()
                else:
                    self.progress_lines.append(raw)
                    await self._roll_overflow_if_needed()
            except queue.Empty:
                break
            except Exception as exc:
                logger.debug("Progress queue cancel-drain stopped early: %s", exc)
                break

        if self.can_edit and self.progress_lines and self.progress_msg_id:
            await self._roll_overflow_if_needed()
        if self.can_edit and self.progress_lines and self.progress_msg_id:
            full_text = self._progress_text(self.progress_lines)
            try:
                await self._edit_progress_message(self.progress_msg_id, full_text)
            except Exception as exc:
                logger.debug("Final progress edit on cancel failed: %s", exc)

    async def _roll_overflow_if_needed(self) -> bool:
        if not self.progress_lines or not self.can_edit:
            return False
        groups = self._split_progress_groups(self.progress_lines)
        if len(groups) <= 1:
            return False

        first_text = self._progress_text(groups[0])
        if self.progress_msg_id is not None:
            result = await self._edit_progress_message(self.progress_msg_id, first_text)
            if not result.success:
                self.can_edit = False
                return False
        else:
            result = await self._send_progress_text(first_text)
            if result.success and result.message_id:
                self.progress_msg_id = result.message_id
                self.track_result(result)

        for group in groups[1:]:
            result = await self._send_progress_text(self._progress_text(group))
            if result.success and result.message_id:
                self.progress_msg_id = result.message_id
                self.track_result(result)

        self.progress_lines = groups[-1]
        return True

    async def _edit_progress_message(self, message_id: str, content: str):
        kwargs = {
            "chat_id": self.source.chat_id,
            "message_id": message_id,
            "content": content,
        }
        if self.edit_accepts_metadata:
            kwargs["metadata"] = self.metadata
        return await self.adapter.edit_message(**kwargs)

    async def _send_progress_text(self, text: str):
        return await self.adapter.send(
            chat_id=self.source.chat_id,
            content=text,
            reply_to=self.reply_to,
            metadata=self.metadata,
        )

    async def _send_typing_after_pause(self) -> None:
        await asyncio.sleep(0.3)
        if self.run_still_current():
            await self.adapter.send_typing(self.source.chat_id, metadata=self.metadata)

    def _split_progress_groups(self, lines: list[Any]) -> list[list[Any]]:
        groups: list[list[Any]] = []
        current: list[Any] = []
        for line in lines:
            candidate = current + [line]
            if current and self.progress_len_fn(self._progress_text(candidate)) > self.text_limit:
                groups.append(current)
                current = [line]
            else:
                current = candidate
        if current:
            groups.append(current)
        return groups

    def _progress_text(self, lines: list[Any]) -> str:
        return "\n".join(str(line) for line in lines)

    def _edit_accepts_metadata(self) -> bool:
        if not self.metadata:
            return False
        try:
            params = inspect.signature(self.adapter.edit_message).parameters
        except (TypeError, ValueError):
            return False
        return "metadata" in params or any(
            param.kind is inspect.Parameter.VAR_KEYWORD
            for param in params.values()
        )

    def _is_interrupted(self) -> bool:
        try:
            agent = self.agent_provider()
            return agent is not None and getattr(agent, "is_interrupted", False)
        except Exception as exc:
            logger.debug("Progress interrupt check failed: %s", exc)
            return False

    def _drain_queue(self) -> None:
        while not self.queue.empty():
            try:
                self.queue.get_nowait()
            except queue.Empty:
                break


def tool_progress_for(runner, **kwargs) -> ToolProgressRuntime:
    return ToolProgressRuntime(runner=runner, **kwargs)
