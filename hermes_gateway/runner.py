"""Concrete Hermes gateway runner."""

from __future__ import annotations

import hermes_bootstrap  # noqa: F401 - configure UTF-8 stdio before runtime imports

import asyncio
import dataclasses
import logging
import os
from collections import OrderedDict
from contextvars import copy_context
from pathlib import Path
from typing import Any, Dict, List, Optional

from agent.i18n import t
from channels.platforms.base import MessageType
from channels.platforms.base import BasePlatformAdapter, MessageEvent
from channels.platforms.base_text import _reply_anchor_for_event
from channels.session_identity import SessionContext, SessionSource, build_session_key
from hermes_agent.gateway.runtime_config import (
    load_gateway_runtime_config,
    redact_approval_command as _redact_approval_command,
    resolve_gateway_model as _resolve_gateway_model,
    resolve_runtime_agent_kwargs,
)
from hermes_agent.storage.session_availability import format_session_store_unavailable
from hermes_gateway.approval_commands import GatewayApprovalCommandMixin
from hermes_gateway.agent_cache import AGENT_PENDING_SENTINEL as _AGENT_PENDING_SENTINEL
from hermes_gateway.agent_run_runtime import agent_run_runtime_for
from hermes_gateway.agent_run_supervision import agent_run_supervisor_for
from hermes_gateway.agent_turn_runtime import agent_turn_runtime_for
from hermes_gateway.background_tasks import GatewayBackgroundTaskMixin
from hermes_gateway.bundles_command import GatewayBundlesCommandMixin
from hermes_gateway.codex_runtime_command import GatewayCodexRuntimeCommandMixin
from hermes_gateway.command_listing import GatewayCommandListingMixin
from hermes_gateway.compress_command import GatewayCompressCommandMixin
from hermes_gateway.config_env_bridge import bridge_gateway_config_env
from hermes_gateway.config import GatewayConfig, Platform
from hermes_gateway.gateway_media_warnings import media_warnings_for
from hermes_gateway.gateway_runtime_config import runtime_config_for
from hermes_gateway.inbound_media import GatewayInboundMediaMixin
from hermes_gateway.inbound_message_preparation import GatewayInboundMessagePreparationMixin
from hermes_gateway.insights_command import GatewayInsightsCommandMixin
from hermes_gateway.kanban_watchers import GatewayKanbanWatcherMixin
from hermes_gateway.message_runtime import message_runtime_for
from hermes_gateway.platform_authorization import GatewayPlatformAuthorizationMixin
from hermes_gateway.platform_runtime import platform_runtime_for
from hermes_gateway.profile_home_commands import GatewayProfileHomeCommandMixin
from hermes_gateway.reset_command import GatewayResetCommandMixin
from hermes_gateway.restart import DEFAULT_GATEWAY_RESTART_DRAIN_TIMEOUT
from hermes_gateway.runner_initialization import initialize_gateway_runner_state
from hermes_gateway.session_env_runtime import session_env_runtime_for
from hermes_gateway.session_recovery_runtime import GatewaySessionRecoveryRuntimeMixin
from hermes_gateway.session_runtime_state import session_runtime_state_for
from hermes_gateway.shutdown_runtime import GatewayShutdownRuntimeMixin
from hermes_gateway.teams_pipeline_gateway_runtime import teams_pipeline_runtime_for
from hermes_gateway.update_restart import GatewayUpdateRestartMixin
from hermes_gateway.process_notifications import (
    drain_gateway_watch_events as _drain_gateway_watch_events,
    format_gateway_process_notification as _format_gateway_process_notification,
)
from hermes_gateway.response_normalization import normalize_empty_agent_response as _normalize_empty_agent_response
from hermes_gateway.runner_ref import gateway_runner_ref as _gateway_runner_ref
from hermes_gateway.skill_hint import check_unavailable_skill as _check_unavailable_skill_for_repo

logger = logging.getLogger(__name__)
_hermes_home = Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes").expanduser()
_env_path = _hermes_home / ".env"

try:
    from dotenv import load_dotenv
except ImportError:
    def load_dotenv(*_args, **_kwargs):
        return False

from hermes_cli.env_loader import load_hermes_dotenv

load_hermes_dotenv(hermes_home=_hermes_home, project_env=Path(__file__).resolve().parents[1] / ".env")
bridge_gateway_config_env(_hermes_home)


def _load_gateway_config() -> dict:
    return load_gateway_runtime_config(_hermes_home)


def _resolve_runtime_agent_kwargs() -> dict:
    return resolve_runtime_agent_kwargs(_hermes_home)


def _check_unavailable_skill(command_name: str) -> str | None:
    repo_root = Path(__file__).resolve().parent.parent
    return _check_unavailable_skill_for_repo(command_name, repo_root=repo_root)

class GatewayRunner(
    GatewayApprovalCommandMixin,
    GatewayBackgroundTaskMixin,
    GatewayBundlesCommandMixin,
    GatewayCodexRuntimeCommandMixin,
    GatewayCommandListingMixin,
    GatewayCompressCommandMixin,
    GatewayPlatformAuthorizationMixin,
    GatewayProfileHomeCommandMixin,
    GatewayInboundMediaMixin,
    GatewayInboundMessagePreparationMixin,
    GatewayKanbanWatcherMixin,
    GatewayInsightsCommandMixin,
    GatewayResetCommandMixin,
    GatewaySessionRecoveryRuntimeMixin,
    GatewayShutdownRuntimeMixin,
    GatewayUpdateRestartMixin,
):
    """
    Main gateway controller.

    Manages the lifecycle of all platform adapters and routes
    messages to/from the agent.
    """

    # Class-level defaults so partial construction in tests doesn't
    # blow up on attribute access.
    _running_agents_ts: Dict[str, float] = {}
    _busy_input_mode: str = "interrupt"
    _restart_drain_timeout: float = DEFAULT_GATEWAY_RESTART_DRAIN_TIMEOUT
    _exit_code: Optional[int] = None
    _draining: bool = False
    _restart_requested: bool = False
    _restart_task_started: bool = False
    _restart_detached: bool = False
    _restart_via_service: bool = False
    _stop_task: Optional[asyncio.Task] = None
    _session_model_overrides: Dict[str, Dict[str, str]] = {}
    _pending_one_turn_model_restores: Dict[str, Dict[str, Any]] = {}
    _session_reasoning_overrides: Dict[str, Dict[str, Any]] = {}

    def __init__(self, config: Optional[GatewayConfig] = None):
        initialize_gateway_runner_state(self, config)


    def _wire_teams_pipeline_runtime(self) -> None:
        teams_pipeline_runtime_for(self).wire_runtime()


    def _warn_if_docker_media_delivery_is_risky(self) -> None:
        media_warnings_for(self).warn_if_docker_media_delivery_is_risky()



    # -- Setup skill availability ----------------------------------------

    def _has_setup_skill(self) -> bool:
        """Check if the hermes-agent-setup skill is installed."""
        try:
            from tools.skill_manager_tool import _find_skill
            return _find_skill("hermes-agent-setup") is not None
        except Exception:
            return False

    # -- Voice mode persistence ------------------------------------------








    async def _safe_adapter_disconnect(self, adapter, platform) -> None:
        await platform_runtime_for(self).safe_adapter_disconnect(adapter, platform)

    def _adapter_disconnect_timeout_secs(self) -> float:
        return platform_runtime_for(self).adapter_disconnect_timeout_secs()

    def _platform_connect_timeout_secs(self) -> float:
        return platform_runtime_for(self).platform_connect_timeout_secs()

    async def _connect_adapter_with_timeout(
        self,
        adapter,
        platform,
        *,
        is_reconnect: bool = False,
    ) -> bool:
        return await platform_runtime_for(self).connect_adapter_with_timeout(
            adapter,
            platform,
            is_reconnect=is_reconnect,
        )

    @property
    def should_exit_cleanly(self) -> bool:
        return self._exit_cleanly

    @property
    def should_exit_with_failure(self) -> bool:
        return self._exit_with_failure

    @property
    def exit_reason(self) -> Optional[str]:
        return self._exit_reason

    @property
    def exit_code(self) -> Optional[int]:
        return self._exit_code

    def _session_key_for_source(self, source: SessionSource) -> str:
        """Resolve the current session key for a source, honoring gateway config when available."""
        if hasattr(self, "session_store") and self.session_store is not None:
            try:
                session_key = self.session_store._generate_session_key(source)
                if isinstance(session_key, str) and session_key:
                    return session_key
            except Exception:
                logger.debug("Failed to resolve session key from session store", exc_info=True)
        config = getattr(self, "config", None)
        return build_session_key(
            source,
            group_sessions_per_user=getattr(config, "group_sessions_per_user", True),
            thread_sessions_per_user=getattr(config, "thread_sessions_per_user", False),
        )

    def _format_session_db_unavailable(self) -> str:
        return format_session_store_unavailable(
            getattr(self, "_session_db_error", None),
            prefix=t("gateway.shared.session_db_unavailable_prefix"),
        )


    # Telegram's General (pinned top) topic in forum-enabled private chats.
    # Bot API behavior varies: some clients omit message_thread_id for
    # General, others send "1". Treat both as "root" for lobby/lane purposes.













    def _request_clean_exit(self, reason: str) -> None:
        self._exit_cleanly = True
        self._exit_reason = reason
        self._shutdown_event.set()

    def _running_agent_count(self) -> int:
        return len(self._running_agents)

    def _status_action_label(self) -> str:
        return "restart" if self._restart_requested else "shutdown"

    def _status_action_gerund(self) -> str:
        return "restarting" if self._restart_requested else "shutting down"


    # -------- /queue FIFO helpers --------------------------------------
    # /queue must produce one full agent turn per invocation, in FIFO
    # order, with no merging.  The adapter's _pending_messages dict is a
    # single "next-up" slot (shared with photo-burst follow-ups), so we
    # use it for the head of the queue and an overflow list for the
    # tail.  Enqueue puts new items in the slot when free, otherwise in
    # the overflow.  Promotion (called after each run's drain) moves the
    # next overflow item into the slot so the following recursion picks
    # it up.  Clearing happens on /new and /reset via
    # _handle_reset_command.







    # ------------------------------------------------------------------
    # Per-platform circuit breaker (pause/resume) — used by the reconnect
    # watcher when a retryable failure recurs past a threshold, and by the
    # /platform pause|resume slash command for manual control.
    # ------------------------------------------------------------------


    def _snapshot_running_agents(self) -> Dict[str, Any]:
        return {
            session_key: agent
            for session_key, agent in self._running_agents.items()
            if agent is not _AGENT_PENDING_SENTINEL
        }




    def _interrupt_running_agents(self, reason: str) -> None:
        for session_key, agent in list(self._running_agents.items()):
            if agent is _AGENT_PENDING_SENTINEL:
                continue
            try:
                agent.interrupt(reason)
                logger.debug("Interrupted running agent for session %s during shutdown", session_key)
            except Exception as e:
                logger.debug("Failed interrupting agent during shutdown: %s", e)



    def _should_emit_long_running_notification(
        self,
        session_key: Optional[str],
        agent: Any,
        executor_task: Optional[Any],
    ) -> bool:
        """Only emit the heartbeat while this task still owns the live run.

        Guards against a stale ``running: delegate_task`` heartbeat outliving the
        run that started it: stop once the executor finishes, the agent is gone,
        or the session key has been rebound to a different live agent (e.g. the
        user sent ``/new`` and a fresh agent took the slot mid-run, #12029).
        """
        if agent is None:
            return False
        if executor_task is not None and executor_task.done():
            return False
        if session_key and self._running_agents.get(session_key) is not agent:
            return False
        return True







    def request_restart(self, *, detached: bool = False, via_service: bool = False) -> bool:
        if self._restart_task_started:
            return False
        self._restart_requested = True
        self._restart_detached = detached
        self._restart_via_service = via_service
        self._restart_task_started = True

        async def _run_restart() -> None:
            await asyncio.sleep(0.05)
            await self.stop(restart=True, detached_restart=detached, service_restart=via_service)

        task = asyncio.create_task(_run_restart())
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)
        return True

    # Drain-timeout reasons set by _stop_impl() when a still-running turn is
    # force-interrupted; "restart_interrupted" is set by
    # SessionStore.suspend_recently_active() on crash recovery (no
    # .clean_shutdown marker).  All three mean "the agent was mid-turn and
    # we killed it" — eligible for startup auto-resume.


    async def start(self) -> bool:
        from hermes_gateway.runner_start import start_gateway_runner

        return await start_gateway_runner(self)




    def _active_profile_name(self) -> str:
        """Return the profile name this gateway represents."""
        try:
            from hermes_cli.profiles import get_active_profile_name
            return get_active_profile_name() or "default"
        except Exception:
            return "default"


    def _kanban_advance(
        self, sub: dict, cursor: int, board: Optional[str] = None,
    ) -> None:
        """Sync helper: advance a subscription's cursor. Runs in to_thread.

        ``board`` scopes the DB connection to the board that owns this
        subscription. Unsub cursors in one board can't touch another's.
        """
        from hermes_cli import kanban_db as _kb
        conn = _kb.connect(board=board)
        try:
            _kb.advance_notify_cursor(
                conn,
                task_id=sub["task_id"],
                platform=sub["platform"],
                chat_id=sub["chat_id"],
                thread_id=sub.get("thread_id") or "",
                new_cursor=cursor,
            )
        finally:
            conn.close()

    def _kanban_unsub(self, sub: dict, board: Optional[str] = None) -> None:
        from hermes_cli import kanban_db as _kb
        conn = _kb.connect(board=board)
        try:
            _kb.remove_notify_sub(
                conn,
                task_id=sub["task_id"],
                platform=sub["platform"],
                chat_id=sub["chat_id"],
                thread_id=sub.get("thread_id") or "",
            )
        finally:
            conn.close()

    def _kanban_rewind(
        self,
        sub: dict,
        claimed_cursor: int,
        old_cursor: int,
        board: Optional[str] = None,
    ) -> None:
        """Sync helper: undo a claimed notification cursor after send failure."""
        from hermes_cli import kanban_db as _kb
        conn = _kb.connect(board=board)
        try:
            _kb.rewind_notify_cursor(
                conn,
                task_id=sub["task_id"],
                platform=sub["platform"],
                chat_id=sub["chat_id"],
                thread_id=sub.get("thread_id") or "",
                claimed_cursor=claimed_cursor,
                old_cursor=old_cursor,
            )
        finally:
            conn.close()




    async def stop(
        self,
        *,
        restart: bool = False,
        detached_restart: bool = False,
        service_restart: bool = False,
    ) -> None:
        from hermes_gateway.runner_stop import stop_gateway_runner

        await stop_gateway_runner(
            self,
            restart=restart,
            detached_restart=detached_restart,
            service_restart=service_restart,
        )

    async def wait_for_shutdown(self) -> None:
        """Wait for shutdown signal."""
        await self._shutdown_event.wait()

    def _create_adapter(
        self,
        platform: Platform,
        config: Any,
    ) -> Optional[BasePlatformAdapter]:
        return platform_runtime_for(self).create_adapter(platform, config)



    async def _handle_message(self, event: MessageEvent) -> Optional[str]:
        return await message_runtime_for(self).handle_message(event)

    def _consume_pending_native_image_paths(self, session_key: str) -> List[str]:
        pending_native = getattr(self, "_pending_native_image_paths_by_session", None)
        if not pending_native:
            return []
        return list(pending_native.pop(session_key, []) or [])

    def _cache_session_source(self, session_key: str, source) -> None:
        if not session_key or source is None:
            return
        cached_sources = getattr(self, "_session_sources", None)
        if cached_sources is None:
            cached_sources = OrderedDict()
            self._session_sources = cached_sources
        try:
            cached_sources[session_key] = dataclasses.replace(source)
        except Exception:
            logger.debug("Failed to cache live session source for %s", session_key, exc_info=True)
            return
        # LRU: mark as most-recently-used and trim to max size.
        try:
            cached_sources.move_to_end(session_key)
            max_size = getattr(self, "_session_sources_max", 512)
            while len(cached_sources) > max_size:
                cached_sources.popitem(last=False)
        except Exception:
            logger.debug("Failed to maintain session-source LRU cache", exc_info=True)

    def _get_cached_session_source(self, session_key: str):
        if not session_key:
            return None
        cached_sources = getattr(self, "_session_sources", None)
        if not cached_sources:
            return None
        source = cached_sources.get(session_key)
        if source is not None:
            try:
                cached_sources.move_to_end(session_key)
            except Exception:
                logger.debug("Failed to refresh cached session-source recency for %s", session_key, exc_info=True)
        return source

    async def _handle_message_with_agent(self, event, source, _quick_key: str, run_generation: int):
        """Inner handler that runs under the _running_agents sentinel guard."""
        return await agent_turn_runtime_for(self).handle(event, source, _quick_key, run_generation)

    def _slash_confirmation_runtime(self):
        """Build slash-confirm dependencies without owning the runtime logic."""
        from channels.slash_commands.confirmation import (
            SlashConfirmationRuntime,
            counter_id_factory,
        )

        counter = getattr(self, "_slash_confirm_counter", None)
        if counter is None:
            import itertools as _itertools

            counter = _itertools.count(1)
            self._slash_confirm_counter = counter
        return SlashConfirmationRuntime(
            adapters=self.adapters,
            session_key_for_source=self._session_key_for_source,
            read_user_config=self._read_user_config,
            save_config_value=self._save_config_value,
            thread_metadata_for_source=self._thread_metadata_for_source,
            reply_anchor_for_event=self._reply_anchor_for_event,
            confirm_id_factory=counter_id_factory(counter),
        )

    @staticmethod
    def _save_config_value(key_path: str, value: Any) -> Any:
        from cli import save_config_value

        return save_config_value(key_path, value)

    def _read_user_config(self) -> Dict[str, Any]:
        """Read the user's raw config.yaml (cached) for gate lookups.

        Used by slash-confirm gates that must reflect on-disk state changes
        (e.g. a prior "Always Approve" click) without a gateway restart.
        """
        try:
            from hermes_cli.config import load_config
            cfg = load_config()
            return cfg if isinstance(cfg, dict) else {}
        except Exception:
            return {}

    def _thread_metadata_for_source(
        self,
        source,
        reply_to_message_id: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Build the metadata dict platforms need for thread-aware replies."""
        thread_id = getattr(source, "thread_id", None)
        if thread_id is None:
            return None
        metadata: Dict[str, Any] = {"thread_id": thread_id}
        if (
            getattr(source, "platform", None) == Platform.TELEGRAM
            and getattr(source, "chat_type", None) == "dm"
        ):
            metadata["telegram_dm_topic_reply_fallback"] = True
            # Telegram DM topic lanes need direct_messages_topic_id in metadata
            # so synthetic/queued messages (goal continuations, status notices)
            # route to the correct topic even when reply anchor is unavailable.
            tid = str(thread_id)
            if tid and tid not in {"", "1"}:
                metadata["direct_messages_topic_id"] = tid
            anchor = reply_to_message_id or getattr(source, "message_id", None)
            if anchor is not None:
                metadata["telegram_reply_to_message_id"] = str(anchor)
        return metadata

    @staticmethod
    def _reply_anchor_for_event(event: MessageEvent) -> Optional[str]:
        """Return the platform-specific reply anchor for GatewayRunner sends."""
        return _reply_anchor_for_event(event)


    # ------------------------------------------------------------------
    # /approve & /deny — explicit dangerous-command approval
    # ------------------------------------------------------------------

    _APPROVAL_TIMEOUT_SECONDS = 300  # 5 minutes



    # Platforms where /update is allowed.  ACP, API server, and webhooks are
    # programmatic interfaces that should not trigger system updates.
    _UPDATE_ALLOWED_PLATFORMS = frozenset({
        Platform.TELEGRAM, Platform.DISCORD, Platform.SLACK, Platform.WHATSAPP,
        Platform.SIGNAL, Platform.MATTERMOST, Platform.MATRIX,
        Platform.HOMEASSISTANT, Platform.EMAIL, Platform.SMS, Platform.DINGTALK,
        Platform.FEISHU, Platform.WECOM, Platform.WECOM_CALLBACK, Platform.WEIXIN, Platform.BLUEBUBBLES, Platform.QQBOT, Platform.LOCAL,
    })








    def _set_session_env(self, context: SessionContext) -> list:
        return session_env_runtime_for(self).set_session_env(context)

    def _clear_session_env(self, tokens: list) -> None:
        session_env_runtime_for(self).clear_session_env(tokens)

    async def _run_in_executor_with_context(self, func, *args):
        """Run blocking work in the thread pool while preserving session contextvars."""
        loop = asyncio.get_running_loop()
        ctx = copy_context()
        return await loop.run_in_executor(None, ctx.run, func, *args)

    def _decide_image_input_mode(self) -> str:
        """Resolve the image-input routing for the currently active model.

        Returns ``"native"`` (attach pixels on the user turn) or ``"text"``
        (pre-analyze with vision_analyze and prepend the description). See
        agent/image_routing.py for the full decision table.

        The active provider/model are read from config.yaml so the decision
        tracks ``/model`` switches automatically on the next message.
        """
        try:
            from agent.image_routing import decide_image_input_mode
            from agent.auxiliary_client import _read_main_model, _read_main_provider
            from hermes_cli.config import load_config

            cfg = load_config()
            provider = _read_main_provider()
            model = _read_main_model()
            return decide_image_input_mode(provider, model, cfg)
        except Exception as exc:
            logger.debug("image_routing: decision failed, falling back to text — %s", exc)
            return "text"

    _MAX_INTERRUPT_DEPTH = 3  # Cap recursive interrupt handling (#816)









    async def _interrupt_and_clear_session(
        self,
        session_key: str,
        source: SessionSource,
        *,
        interrupt_reason: str,
        invalidation_reason: str,
        release_running_state: bool = True,
    ) -> None:
        """Interrupt the current run and clear queued session state consistently."""
        if not session_key:
            return
        running_agent = self._running_agents.get(session_key)
        if running_agent and running_agent is not _AGENT_PENDING_SENTINEL:
            running_agent.interrupt(interrupt_reason)
        session_runtime_state_for(self).invalidate_session_run_generation(session_key, reason=invalidation_reason)
        adapter = self.adapters.get(source.platform)
        if adapter and hasattr(adapter, "interrupt_session_activity"):
            await adapter.interrupt_session_activity(session_key, source.chat_id)
        if adapter and hasattr(adapter, "get_pending_message"):
            adapter.get_pending_message(session_key)  # consume and discard
        self._pending_messages.pop(session_key, None)
        if release_running_state:
            session_runtime_state_for(self).release_running_agent_state(session_key)

    # ------------------------------------------------------------------
    # Proxy mode: forward messages to a remote Hermes API server
    # ------------------------------------------------------------------


    # ------------------------------------------------------------------

    async def _run_agent(
        self,
        message: str,
        context_prompt: str,
        history: List[Dict[str, Any]],
        source: SessionSource,
        session_id: str,
        session_key: str = None,
        run_generation: Optional[int] = None,
        _interrupt_depth: int = 0,
        event_message_id: Optional[str] = None,
        channel_prompt: Optional[str] = None,
        turn_context_notes: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        return await agent_run_runtime_for(self).run(
            message=message,
            context_prompt=context_prompt,
            history=history,
            source=source,
            session_id=session_id,
            session_key=session_key,
            run_generation=run_generation,
            _interrupt_depth=_interrupt_depth,
            event_message_id=event_message_id,
            channel_prompt=channel_prompt,
            turn_context_notes=turn_context_notes,
        )


async def start_gateway(config: Optional[GatewayConfig] = None, replace: bool = False, verbosity: Optional[int] = 0) -> bool:
    """Start the gateway process with the concrete GatewayRunner implementation."""
    from hermes_constants import get_hermes_home
    from hermes_gateway.gateway_process import start_gateway_process

    return await start_gateway_process(
        runner_cls=GatewayRunner,
        config=config,
        replace=replace,
        verbosity=verbosity,
        hermes_home=get_hermes_home(),
    )
