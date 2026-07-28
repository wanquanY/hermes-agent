"""Discord Gateway task and WebSocket liveness ownership."""

from __future__ import annotations

import asyncio
import logging
import math
import time
from contextlib import suppress
from typing import Any, Optional

from agent.secret_scope import get_profile_env

logger = logging.getLogger(__name__)


def _consume_background_task_result(task: asyncio.Future[Any]) -> None:
    with suppress(asyncio.CancelledError, Exception):
        task.result()


def _abort_discord_websocket_transport(websocket: Any) -> bool:
    """Abort the active aiohttp transport after a bounded close times out."""
    socket = getattr(websocket, "socket", None)
    response = getattr(socket, "_response", None)
    connection = getattr(socket, "_conn", None)
    if connection is None:
        connection = getattr(response, "connection", None)
    protocol = getattr(connection, "protocol", None)
    writer = getattr(socket, "_writer", None)
    transport = getattr(writer, "transport", None)
    if transport is None:
        transport = getattr(protocol, "transport", None)
    abort = getattr(transport, "abort", None)
    if not callable(abort):
        return False
    abort()
    return True


def discord_ready_timeout_seconds() -> float:
    raw = get_profile_env(
        "HERMES_GATEWAY_PLATFORM_CONNECT_TIMEOUT", ""
    ).strip()
    if raw:
        try:
            return max(0.0, float(raw))
        except ValueError:
            logger.warning(
                "Ignoring invalid HERMES_GATEWAY_PLATFORM_CONNECT_TIMEOUT=%r",
                raw,
            )
    return 30.0


async def wait_for_ready_or_bot_exit(
    ready_event: asyncio.Event,
    bot_task: asyncio.Task,
    timeout: Optional[float],
) -> None:
    """Wait for Discord readiness while surfacing early Bot.start failures."""
    ready_task = asyncio.create_task(ready_event.wait())
    try:
        done, _pending = await asyncio.wait(
            {ready_task, bot_task},
            timeout=timeout,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if not done:
            raise asyncio.TimeoutError
        if bot_task in done:
            error = bot_task.exception()
            if error is not None:
                raise error
            if not ready_task.done():
                raise RuntimeError("Discord bot task exited before ready")
        await ready_task
    finally:
        if not ready_task.done():
            ready_task.cancel()
            with suppress(asyncio.CancelledError):
                await ready_task


class DiscordLivenessMixin:
    """Own Gateway health sampling and retryable-fatal escalation."""

    def _init_discord_liveness(self) -> None:
        self._liveness_interval_seconds = self._finite_positive_config_float(
            "websocket_liveness_interval_seconds",
            15.0,
            env_key="HERMES_DISCORD_LIVENESS_INTERVAL_SECONDS",
        )
        self._liveness_failure_threshold = self._config_int(
            "websocket_liveness_failure_threshold",
            2,
            env_key="HERMES_DISCORD_LIVENESS_FAILURE_THRESHOLD",
        )
        self._heartbeat_ack_max_age_seconds = self._finite_positive_config_float(
            "websocket_heartbeat_ack_max_age_seconds",
            60.0,
        )
        self._max_latency_seconds = self._finite_positive_config_float(
            "websocket_max_latency_seconds",
            30.0,
        )
        self._liveness_task: Optional[asyncio.Task] = None
        self._liveness_notification_task: Optional[asyncio.Task] = None
        self._disconnecting = False

    def _config_value(
        self,
        key: str,
        default: Any,
        *,
        env_key: Optional[str] = None,
    ) -> Any:
        extra = self.config.extra if isinstance(self.config.extra, dict) else {}
        value = extra.get(key)
        if value is None and env_key:
            value = get_profile_env(env_key)
        return default if value is None or value == "" else value

    def _finite_positive_config_float(
        self,
        key: str,
        default: float,
        *,
        env_key: Optional[str] = None,
    ) -> float:
        try:
            value = float(self._config_value(key, default, env_key=env_key))
        except (TypeError, ValueError):
            return 0.0
        return value if math.isfinite(value) and value > 0 else 0.0

    def _config_int(
        self,
        key: str,
        default: int,
        *,
        env_key: Optional[str] = None,
    ) -> int:
        value = self._config_value(key, default, env_key=env_key)
        if isinstance(value, bool):
            return 0
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return 0
        return parsed if parsed > 0 else 0

    def _handle_bot_task_done(self, task: asyncio.Task) -> None:
        if self._disconnecting:
            with suppress(asyncio.CancelledError, Exception):
                task.exception()
            return
        if self._bot_task is not None and task is not self._bot_task:
            with suppress(asyncio.CancelledError, Exception):
                task.exception()
            return
        if not self._running:
            with suppress(asyncio.CancelledError, Exception):
                task.exception()
            return
        try:
            error = task.exception()
        except asyncio.CancelledError:
            return
        except Exception as task_error:  # pragma: no cover - defensive
            error = task_error
        message = (
            "Discord gateway task exited without an exception"
            if error is None
            else f"Discord gateway task exited: {error}"
        )
        logger.error("[%s] %s", self.name, message)
        self._set_fatal_error("discord_gateway_task_exited", message, retryable=True)
        notification = asyncio.create_task(self._notify_fatal_error())
        notification.add_done_callback(_consume_background_task_result)

    async def _cancel_bot_task(self) -> None:
        task = self._bot_task
        if task and not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await task
        self._bot_task = None

    def _start_liveness_probe(self) -> None:
        if (
            self._liveness_interval_seconds <= 0
            or self._liveness_failure_threshold <= 0
            or self._heartbeat_ack_max_age_seconds <= 0
            or self._max_latency_seconds <= 0
        ):
            return
        if self._liveness_task and not self._liveness_task.done():
            return
        self._liveness_task = asyncio.create_task(self._liveness_loop())

    def _read_websocket_health(self, client: Any) -> tuple[bool, str]:
        try:
            if not client.is_ready():
                return False, "not_ready"
        except Exception:
            return False, "not_ready"
        try:
            if client.is_closed():
                return False, "client_closed"
        except Exception:
            return False, "client_closed"
        websocket = getattr(client, "ws", None)
        try:
            socket_open = bool(
                websocket is not None and getattr(websocket, "open", False)
            )
        except Exception:
            return False, "socket_state_unavailable"
        if not socket_open:
            return False, "socket_closed"
        keep_alive = getattr(websocket, "_keep_alive", None)
        last_ack = getattr(keep_alive, "_last_ack", None)
        if not isinstance(last_ack, (int, float)):
            return False, "ack_unavailable"
        ack_age = time.perf_counter() - last_ack
        if (
            not math.isfinite(ack_age)
            or ack_age > self._heartbeat_ack_max_age_seconds
        ):
            return False, "ack_stale"
        latency = getattr(client, "latency", None)
        if not isinstance(latency, (int, float)) or not math.isfinite(latency):
            return False, "latency_non_finite"
        if latency > self._max_latency_seconds:
            return False, "latency_exceeded"
        return True, "healthy"

    async def _liveness_loop(self) -> None:
        failures = 0
        while self._running:
            try:
                await asyncio.sleep(self._liveness_interval_seconds)
            except asyncio.CancelledError:
                return
            client = self._client
            if not self._running or client is None or self._disconnecting:
                return
            try:
                healthy, reason = self._read_websocket_health(client)
            except Exception:
                healthy, reason = False, "health_check_error"
            if healthy:
                failures = 0
                continue
            failures += 1
            logger.warning(
                "[%s] Discord Gateway WebSocket unhealthy (%s, %d/%d)",
                self.name,
                reason,
                failures,
                self._liveness_failure_threshold,
            )
            if failures < self._liveness_failure_threshold:
                continue
            self._disconnecting = True
            message = f"Discord Gateway WebSocket health check failed: {reason}"
            logger.error("[%s] %s; forcing reconnect", self.name, message)
            self._set_fatal_error(
                "discord_websocket_health_stale",
                message,
                retryable=True,
            )
            self._liveness_notification_task = asyncio.create_task(
                self._notify_liveness_fatal_error(client)
            )
            return

    async def _notify_liveness_fatal_error(self, client: Any) -> None:
        failed_websocket = getattr(client, "ws", None)
        try:
            close_task = asyncio.create_task(client.close())
            try:
                done, _pending = await asyncio.wait({close_task}, timeout=1.0)
                if close_task not in done:
                    raise asyncio.TimeoutError
                await close_task
            except asyncio.TimeoutError:
                logger.warning("[%s] Timed out closing unhealthy Discord client", self.name)
                close_task.cancel()
                close_task.add_done_callback(_consume_background_task_result)
                closing_task = getattr(client, "_closing_task", None)
                if isinstance(closing_task, asyncio.Task):
                    closing_task.cancel()
                    closing_task.add_done_callback(_consume_background_task_result)
                    client._closing_task = None
                if _abort_discord_websocket_transport(failed_websocket):
                    logger.warning(
                        "[%s] Aborted unresponsive Discord WebSocket transport",
                        self.name,
                    )
            except Exception as close_error:
                logger.debug(
                    "[%s] Error closing unhealthy Discord client: %s",
                    self.name,
                    close_error,
                )
            if self._liveness_notification_task is asyncio.current_task():
                self._liveness_notification_task = None
            await self._notify_fatal_error()
        except Exception as notify_error:
            logger.debug(
                "[%s] Fatal-error handler raised: %s",
                self.name,
                notify_error,
            )

    async def _cancel_liveness_task(self) -> None:
        current = asyncio.current_task()
        for task_name in ("_liveness_task", "_liveness_notification_task"):
            task = getattr(self, task_name, None)
            if task is None or task is current:
                continue
            if not task.done():
                task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await task
            setattr(self, task_name, None)
