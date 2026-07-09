"""Gateway platform lifecycle and reconnect runtime."""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
from typing import Any, Optional

from channels.platforms.base import BasePlatformAdapter
from hermes_agent.gateway.runtime_config import load_gateway_runtime_config
from hermes_constants import get_hermes_home
from hermes_gateway.busy_session_runtime import busy_session_runtime_for
from hermes_gateway.platform_adapter_factory import create_platform_adapter
from hermes_gateway.runtime_status_writer import runtime_status_for

logger = logging.getLogger(__name__)
_PLATFORM_CONNECT_TIMEOUT_SECS_DEFAULT = 30.0
_ADAPTER_DISCONNECT_TIMEOUT_SECS_DEFAULT = 5.0


class GatewayPlatformRuntimeService:
    def __init__(self, runner):
        self._runner = runner

    def create_adapter(self, platform, config: Any) -> Optional[BasePlatformAdapter]:
        runner = self._runner
        return create_platform_adapter(
            platform,
            config,
            group_sessions_per_user=runner.config.group_sessions_per_user,
            thread_sessions_per_user=getattr(runner.config, "thread_sessions_per_user", False),
            gateway_runner=runner,
            load_user_config=_load_gateway_config,
        )

    async def connect_adapter_with_timeout(self, adapter, platform) -> bool:
        timeout = self.platform_connect_timeout_secs()
        if timeout <= 0:
            return await adapter.connect()
        try:
            return await asyncio.wait_for(adapter.connect(), timeout=timeout)
        except asyncio.TimeoutError as exc:
            raise TimeoutError(
                f"{platform.value} connect timed out after {timeout:g}s"
            ) from exc

    async def safe_adapter_disconnect(self, adapter, platform) -> None:
        timeout = self.adapter_disconnect_timeout_secs()
        try:
            if timeout <= 0:
                await adapter.disconnect()
            else:
                await asyncio.wait_for(adapter.disconnect(), timeout=timeout)
        except asyncio.TimeoutError:
            logger.warning(
                "Timed out after %.1fs while disconnecting %s adapter; continuing shutdown",
                timeout,
                platform.value if platform is not None else "adapter",
            )
        except Exception as exc:
            logger.debug(
                "Defensive %s disconnect after failed connect raised: %s",
                platform.value if platform is not None else "adapter",
                exc,
            )

    @staticmethod
    def adapter_disconnect_timeout_secs() -> float:
        raw = os.getenv("HERMES_GATEWAY_ADAPTER_DISCONNECT_TIMEOUT", "").strip()
        if raw:
            try:
                timeout = float(raw)
            except ValueError:
                logger.warning(
                    "Ignoring invalid HERMES_GATEWAY_ADAPTER_DISCONNECT_TIMEOUT=%r",
                    raw,
                )
            else:
                return max(0.0, timeout)
        return _ADAPTER_DISCONNECT_TIMEOUT_SECS_DEFAULT

    @staticmethod
    def platform_connect_timeout_secs() -> float:
        raw = os.getenv("HERMES_GATEWAY_PLATFORM_CONNECT_TIMEOUT", "").strip()
        if raw:
            try:
                timeout = float(raw)
            except ValueError:
                logger.warning(
                    "Ignoring invalid HERMES_GATEWAY_PLATFORM_CONNECT_TIMEOUT=%r",
                    raw,
                )
            else:
                return max(0.0, timeout)
        return _PLATFORM_CONNECT_TIMEOUT_SECS_DEFAULT

    async def handle_adapter_fatal_error(self, adapter: BasePlatformAdapter) -> None:
        """React to an adapter failure after startup.

        If the error is retryable (e.g. network blip, DNS failure), queue the
        platform for background reconnection instead of giving up permanently.
        """
        runner = self._runner
        logger.error(
            "Fatal %s adapter error (%s): %s",
            adapter.platform.value,
            adapter.fatal_error_code or "unknown",
            adapter.fatal_error_message or "unknown error",
        )
        runtime_status_for(runner).update_platform_runtime_status(
            adapter.platform.value,
            platform_state="retrying" if adapter.fatal_error_retryable else "fatal",
            error_code=adapter.fatal_error_code,
            error_message=adapter.fatal_error_message,
        )

        existing = runner.adapters.get(adapter.platform)
        if existing is adapter:
            try:
                await adapter.disconnect()
            finally:
                runner.adapters.pop(adapter.platform, None)
                runner.delivery_router.adapters = runner.adapters

        # Queue retryable failures for background reconnection
        if adapter.fatal_error_retryable:
            platform_config = runner.config.platforms.get(adapter.platform)
            if platform_config and adapter.platform not in runner._failed_platforms:
                runner._failed_platforms[adapter.platform] = {
                    "config": platform_config,
                    "attempts": 0,
                    "next_retry": time.monotonic() + 30,
                }
                logger.info(
                    "%s queued for background reconnection",
                    adapter.platform.value,
                )

        if not runner.adapters and not runner._failed_platforms:
            runner._exit_reason = adapter.fatal_error_message or "All messaging adapters disconnected"
            if adapter.fatal_error_retryable:
                runner._exit_with_failure = True
                logger.error("No connected messaging platforms remain. Shutting down gateway for service restart.")
            else:
                logger.error("No connected messaging platforms remain. Shutting down gateway cleanly.")
            await runner.stop()
        elif not runner.adapters and runner._failed_platforms:
            # All platforms are down and queued for background reconnection.
            # Keep the gateway alive so cron jobs still run and the reconnect
            # watcher can recover platforms when the underlying problem clears.
            logger.warning(
                "No connected messaging platforms remain, but %d platform(s) "
                "queued for reconnection — gateway staying alive, watcher will "
                "retry in background.",
                len(runner._failed_platforms),
            )

    def pause_failed_platform(self, platform, *, reason: str = "") -> None:
        """Mark a queued platform as paused."""
        runner = self._runner
        info = getattr(runner, "_failed_platforms", {}).get(platform)
        if info is None:
            return
        if info.get("paused"):
            return
        info["paused"] = True
        info["pause_reason"] = reason or "auto-paused after repeated failures"
        # Push next_retry far enough out that even if "paused" is missed
        # by a stale code path, the watcher won't fire on it.
        info["next_retry"] = float("inf")
        try:
            runtime_status_for(runner).update_platform_runtime_status(
                platform.value,
                platform_state="paused",
                error_code=None,
                error_message=info["pause_reason"],
            )
        except Exception:
            pass
        logger.warning(
            "%s paused after %d consecutive failures (%s) — "
            "fix the underlying issue then run `/platform resume %s` "
            "to retry, or `hermes gateway restart` to restart the gateway.",
            platform.value, info.get("attempts", 0), info["pause_reason"], platform.value,
        )

    def resume_paused_platform(self, platform) -> bool:
        """Unpause a platform and schedule an immediate retry."""
        runner = self._runner
        info = getattr(runner, "_failed_platforms", {}).get(platform)
        if info is None:
            return False
        if not info.get("paused"):
            return False
        info["paused"] = False
        info.pop("pause_reason", None)
        info["attempts"] = 0
        info["next_retry"] = time.monotonic()
        try:
            runtime_status_for(runner).update_platform_runtime_status(
                platform.value,
                platform_state="retrying",
                error_code=None,
                error_message=None,
            )
        except Exception:
            pass
        logger.info("%s resumed — retrying on next watcher tick", platform.value)
        return True

    async def platform_reconnect_watcher(self) -> None:
        """Background task that periodically retries connecting failed platforms."""
        backoff_cap = 300
        pause_after_failures = 10

        await asyncio.sleep(10)
        runner = self._runner
        while runner._running:
            if not runner._failed_platforms:
                for _ in range(30):
                    if not runner._running:
                        return
                    await asyncio.sleep(1)
                continue

            now = time.monotonic()
            for platform in list(runner._failed_platforms.keys()):
                if not runner._running:
                    return
                info = runner._failed_platforms[platform]
                if info.get("paused"):
                    continue
                if now < info["next_retry"]:
                    continue

                platform_config = info["config"]
                attempt = info["attempts"] + 1
                logger.info("Reconnecting %s (attempt %d)...", platform.value, attempt)

                try:
                    adapter = runner._create_adapter(platform, platform_config)
                    if not adapter:
                        logger.warning(
                            "Reconnect %s: adapter creation returned None, removing from retry queue",
                            platform.value,
                        )
                        del runner._failed_platforms[platform]
                        continue

                    adapter.set_message_handler(runner._handle_message)
                    adapter.set_fatal_error_handler(self.handle_adapter_fatal_error)
                    adapter.set_session_store(runner.session_store)
                    adapter.set_busy_session_handler(busy_session_runtime_for(runner).handle_active_session_busy_message)

                    success = await runner._connect_adapter_with_timeout(adapter, platform)
                    if success:
                        runner.adapters[platform] = adapter
                        from hermes_gateway.voice_runtime import voice_runtime_for

                        voice_runtime_for(runner).sync_voice_mode_state_to_adapter(adapter)
                        runner.delivery_router.adapters = runner.adapters
                        del runner._failed_platforms[platform]
                        runtime_status_for(runner).update_platform_runtime_status(
                            platform.value,
                            platform_state="connected",
                            error_code=None,
                            error_message=None,
                        )
                        logger.info("✓ %s reconnected successfully", platform.value)

                        try:
                            from hermes_gateway.channel_directory import build_channel_directory

                            await build_channel_directory(runner.adapters)
                        except Exception:
                            pass
                    elif adapter.has_fatal_error and not adapter.fatal_error_retryable:
                        runtime_status_for(runner).update_platform_runtime_status(
                            platform.value,
                            platform_state="fatal",
                            error_code=adapter.fatal_error_code,
                            error_message=adapter.fatal_error_message,
                        )
                        logger.warning(
                            "Reconnect %s: non-retryable error (%s), removing from retry queue",
                            platform.value, adapter.fatal_error_message,
                        )
                        del runner._failed_platforms[platform]
                    else:
                        runtime_status_for(runner).update_platform_runtime_status(
                            platform.value,
                            platform_state="retrying",
                            error_code=adapter.fatal_error_code,
                            error_message=adapter.fatal_error_message or "failed to reconnect",
                        )
                        backoff = min(30 * (2 ** (attempt - 1)), backoff_cap)
                        info["attempts"] = attempt
                        info["next_retry"] = time.monotonic() + backoff
                        logger.info(
                            "Reconnect %s failed, next retry in %ds",
                            platform.value, backoff,
                        )
                        if attempt >= pause_after_failures:
                            self.pause_failed_platform(
                                platform,
                                reason=adapter.fatal_error_message or "failed to reconnect",
                            )
                except Exception as exc:
                    runtime_status_for(runner).update_platform_runtime_status(
                        platform.value,
                        platform_state="retrying",
                        error_code=None,
                        error_message=str(exc),
                    )
                    backoff = min(30 * (2 ** (attempt - 1)), backoff_cap)
                    info["attempts"] = attempt
                    info["next_retry"] = time.monotonic() + backoff
                    logger.warning(
                        "Reconnect %s error: %s, next retry in %ds",
                        platform.value, exc, backoff,
                    )
                    if attempt >= pause_after_failures:
                        self.pause_failed_platform(platform, reason=str(exc))

            for _ in range(10):
                if not runner._running:
                    return
                await asyncio.sleep(1)


def platform_runtime_for(runner) -> GatewayPlatformRuntimeService:
    service = getattr(runner, "platform_runtime", None)
    if isinstance(service, GatewayPlatformRuntimeService):
        return service
    service = GatewayPlatformRuntimeService(runner)
    runner.platform_runtime = service
    return service


def _load_gateway_config() -> dict:
    runner_module = sys.modules.get("hermes_gateway.runner")
    patched = getattr(runner_module, "_load_gateway_config", None) if runner_module is not None else None
    if callable(patched):
        return patched()
    return load_gateway_runtime_config(get_hermes_home())
