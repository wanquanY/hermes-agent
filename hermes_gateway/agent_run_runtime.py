"""Agent execution runtime delegated from GatewayRunner."""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from agent.async_utils import safe_schedule_threadsafe
from hermes_constants import get_hermes_home
from hermes_gateway.agent_cache import agent_cache_for
from hermes_gateway.agent_execution_monitor import agent_execution_monitor_for
from hermes_gateway.agent_input_preparation import agent_input_preparation_for
from hermes_gateway.agent_interaction_callbacks import agent_interaction_callbacks_for
from hermes_gateway.agent_pending_followup_runtime import (
    PendingFollowupContext,
    agent_pending_followup_for,
)
from hermes_gateway.agent_result_finalizer import agent_result_finalizer_for
from hermes_gateway.agent_run_supervision import agent_run_supervisor_for
from hermes_gateway.agent_streaming_runtime import agent_streaming_for
from hermes_gateway.agent_turn_completion import (
    AgentTurnCleanupContext,
    agent_final_delivery_for,
    agent_turn_cleanup_for,
)
from hermes_gateway.bootstrap import (
    float_env as _float_env,
    reload_runtime_env_preserving_config_authority,
)
from hermes_gateway.config import Platform
from hermes_gateway.display_config import resolve_display_setting
from hermes_gateway.fast_command import fast_command_for
from hermes_gateway.freshness import (
    auto_continue_freshness_window as _auto_continue_freshness_window,
    is_fresh_gateway_interruption as _is_fresh_gateway_interruption,
    last_transcript_timestamp as _last_transcript_timestamp,
)
from hermes_gateway.gateway_runtime_config import runtime_config_for
from hermes_gateway.media import collect_history_media_paths as _collect_history_media_paths
from hermes_gateway.model_command import model_command_for
from hermes_gateway.output_policy import (
    prepare_gateway_status_message as _prepare_gateway_status_message,
    redact_gateway_user_facing_secrets as _redact_gateway_user_facing_secrets,
)
from hermes_gateway.proxy_mode import proxy_mode_for
from hermes_gateway.replay import build_replay_entry as _build_replay_entry
from hermes_gateway.session import SessionSource
from hermes_gateway.session_runtime_state import session_runtime_state_for
from hermes_gateway.tool_progress_runtime import tool_progress_for
from utils import is_truthy_value

logger = logging.getLogger(__name__)
_hermes_home = get_hermes_home()


def _load_gateway_config() -> dict:
    runner_module = _gateway_runner_module()
    patched_loader = getattr(runner_module, "_load_gateway_config", None) if runner_module else None
    if callable(patched_loader):
        return patched_loader()
    from hermes_agent.gateway.runtime_config import load_gateway_runtime_config

    return load_gateway_runtime_config(_active_hermes_home())


def _resolve_gateway_model(config: dict | None = None) -> str:
    from hermes_agent.gateway.runtime_config import resolve_gateway_model

    return resolve_gateway_model(config)


def _platform_config_key(platform: Platform) -> str:
    return "cli" if platform == Platform.LOCAL else platform.value


def _redact_approval_command(text: str) -> str:
    from hermes_agent.gateway.runtime_config import redact_approval_command

    return redact_approval_command(text)


def _gateway_runner_module():
    return sys.modules.get("hermes_gateway.runner")


def _active_hermes_home():
    runner_module = _gateway_runner_module()
    return getattr(runner_module, "_hermes_home", _hermes_home) if runner_module else _hermes_home


def _runtime_config_for(runner):
    runner_module = _gateway_runner_module()
    patched_factory = getattr(runner_module, "runtime_config_for", None) if runner_module else None
    if callable(patched_factory) and patched_factory is not runtime_config_for:
        return patched_factory(runner)
    return runtime_config_for(runner)


class AgentRunRuntime:
    def __init__(self, runner) -> None:
        self._runner = runner

    async def run(
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
    ) -> Dict[str, Any]:
        """
        Run the agent with the given message and context.
        
        Returns the full result dict from run_conversation, including:
          - "final_response": str (the text to send back)
          - "messages": list (full conversation including tool calls)
          - "api_calls": int
          - "completed": bool
        
        This is run in a thread pool to not block the event loop.
        Supports interruption via new messages.
        """
        runner = self._runner
        # ---- Proxy mode: delegate to remote API server ----
        proxy_mode = proxy_mode_for(runner)
        if proxy_mode.get_proxy_url():
            return await proxy_mode.run_agent_via_proxy(
                message=message,
                context_prompt=context_prompt,
                history=history,
                source=source,
                session_id=session_id,
                session_key=session_key,
                run_generation=run_generation,
                event_message_id=event_message_id,
            )

        from run_agent import AIAgent

        def _run_still_current() -> bool:
            if run_generation is None or not session_key:
                return True
            return session_runtime_state_for(runner).is_session_run_current(session_key, run_generation)
        
        user_config = _load_gateway_config()
        platform_key = _platform_config_key(source.platform)

        from hermes_cli.tools_config import _get_platform_tools
        enabled_toolsets = sorted(_get_platform_tools(user_config, platform_key))
        agent_cfg_local = user_config.get("agent") or {}
        disabled_toolsets = agent_cfg_local.get("disabled_toolsets") or None

        display_config = user_config.get("display", {})
        if not isinstance(display_config, dict):
            display_config = {}

        # Apply tool preview length config (0 = no limit)
        try:
            from agent.display import set_tool_preview_max_len
            _tpl = resolve_display_setting(user_config, platform_key, "tool_preview_length", 0)
            set_tool_preview_max_len(int(_tpl) if _tpl else 0)
        except Exception as exc:
            logger.debug("tool preview length config skipped: %s", exc)

        # Tool progress mode — resolved per-platform with env var fallback
        _resolved_tp = resolve_display_setting(user_config, platform_key, "tool_progress")
        _env_tp = os.getenv("HERMES_TOOL_PROGRESS_MODE")
        _display_cfg = display_config if isinstance(display_config, dict) else {}
        _platforms_cfg = _display_cfg.get("platforms") or {}
        _platform_cfg = _platforms_cfg.get(platform_key) or {}
        _legacy_tp_overrides = _display_cfg.get("tool_progress_overrides") or {}
        _tool_progress_configured = (
            "tool_progress" in _display_cfg
            or (
                isinstance(_platform_cfg, dict)
                and "tool_progress" in _platform_cfg
            )
            or (
                isinstance(_legacy_tp_overrides, dict)
                and platform_key in _legacy_tp_overrides
            )
        )
        progress_mode = (
            _env_tp
            if _env_tp and not _tool_progress_configured
            else (_resolved_tp or _env_tp or "all")
        )
        # Disable tool progress for webhooks - they don't support message editing,
        # so each progress line would be sent as a separate message.
        from hermes_gateway.config import Platform
        tool_progress_enabled = progress_mode != "off" and source.platform != Platform.WEBHOOK
        # Natural assistant status messages are intentionally independent from
        # tool progress and token streaming. Users can keep tool_progress quiet
        # in chat platforms while opting into concise mid-turn updates.
        interim_assistant_messages_enabled = (
            source.platform != Platform.WEBHOOK
            and is_truthy_value(
                display_config.get("interim_assistant_messages"),
                default=True,
            )
        )

        # We need to share the agent instance for interrupt support
        agent_holder = [None]  # Mutable container for the agent instance
        result_holder = [None]  # Mutable container for the result
        tools_holder = [None]   # Mutable container for the tool definitions
        stream_consumer_holder = [None]  # Mutable container for stream consumer

        tool_progress = tool_progress_for(
            runner,
            user_config=user_config,
            platform_key=platform_key,
            source=source,
            event_message_id=event_message_id,
            run_still_current=_run_still_current,
            agent_provider=lambda: agent_holder[0],
            load_gateway_config=_load_gateway_config,
            hermes_home=_active_hermes_home(),
        )
        tool_progress_enabled = tool_progress.enabled
        
        # Bridge sync step_callback → async hooks.emit for agent:step events
        _loop_for_step = asyncio.get_running_loop()
        _hooks_ref = runner.hooks

        def _step_callback_sync(iteration: int, prev_tools: list) -> None:
            if not _run_still_current():
                return
            # prev_tools may be list[str] or list[dict] with "name"/"result"
            # keys.  Normalise to keep "tool_names" backward-compatible for
            # user-authored hooks that do ', '.join(tool_names)'.
            _names: list[str] = []
            for _t in (prev_tools or []):
                if isinstance(_t, dict):
                    _names.append(_t.get("name") or "")
                else:
                    _names.append(str(_t))
            safe_schedule_threadsafe(
                _hooks_ref.emit("agent:step", {
                    "platform": source.platform.value if source.platform else "",
                    "user_id": source.user_id,
                    "session_id": session_id,
                    "iteration": iteration,
                    "tool_names": _names,
                    "tools": prev_tools,
                }),
                _loop_for_step,
                logger=logger,
                log_message="agent:step hook scheduling error",
            )

        # Bridge sync status_callback → async adapter.send for context pressure
        _status_adapter = runner.adapters.get(source.platform)
        _status_chat_id = source.chat_id
        _status_thread_metadata = tool_progress.status_thread_metadata

        def _status_callback_sync(event_type: str, message: str) -> None:
            if not _status_adapter or not _run_still_current():
                return
            prepared_message = _prepare_gateway_status_message(
                source.platform,
                event_type,
                message,
            )
            if prepared_message is None:
                logger.debug(
                    "status_callback suppressed for %s/%s: %s",
                    source.platform.value if source.platform else "unknown",
                    event_type,
                    _redact_gateway_user_facing_secrets(str(message or ""))[:160],
                )
                return
            _fut = safe_schedule_threadsafe(
                _status_adapter.send(
                    _status_chat_id,
                    prepared_message,
                    metadata=_status_thread_metadata,
                ),
                _loop_for_step,
                logger=logger,
                log_message=f"status_callback ({event_type}) scheduling error",
            )
            if _fut is None:
                return
            if tool_progress.cleanup_enabled:
                def _track_status_id(fut) -> None:
                    try:
                        res = fut.result()
                    except Exception as exc:
                        logger.debug("status message result tracking failed: %s", exc)
                        return
                    tool_progress.track_message_result(res)
                _fut.add_done_callback(_track_status_id)

        def run_sync():
            # The conditional re-assignment of `message` further below
            # (prepending model-switch notes) makes Python treat it as a
            # local variable in the entire function.  `nonlocal` lets us
            # read *and* reassign the outer `_run_agent` parameter without
            # triggering an UnboundLocalError on the earlier read at
            # `_resolve_turn_agent_config(message, …)`.
            nonlocal message

            # session_key is now set via contextvars in _set_session_env()
            # (concurrency-safe). Keep os.environ as fallback for CLI/cron.
            os.environ["HERMES_SESSION_KEY"] = session_key or ""

            # Read from env var or use default (same as CLI)
            max_iterations = int(os.getenv("HERMES_MAX_ITERATIONS", "90"))
            
            # Map platform enum to the platform hint key the agent understands.
            # Platform.LOCAL ("local") maps to "cli"; others pass through as-is.
            platform_key = "cli" if source.platform == Platform.LOCAL else source.platform.value
            
            # Combine platform context, per-channel context, and the user-configured
            # ephemeral system prompt.
            combined_ephemeral = context_prompt or ""
            event_channel_prompt = (channel_prompt or "").strip()
            if event_channel_prompt:
                combined_ephemeral = (combined_ephemeral + "\n\n" + event_channel_prompt).strip()
            if runner._ephemeral_system_prompt:
                combined_ephemeral = (combined_ephemeral + "\n\n" + runner._ephemeral_system_prompt).strip()

            # Re-read .env and config for fresh credentials (gateway is long-lived,
            # keys may change without restart). Keep config.yaml authoritative for
            # runtime budget settings bridged into env vars.
            reload_runtime_env_preserving_config_authority(
                _active_hermes_home(),
                project_env=Path(__file__).resolve().parents[1] / ".env",
            )

            try:
                model, runtime_kwargs = _runtime_config_for(runner).resolve_session_agent_runtime(
                    source=source,
                    session_key=session_key,
                    user_config=user_config,
                )
                logger.debug(
                    "run_agent resolved: model=%s provider=%s session=%s",
                    model, runtime_kwargs.get("provider"), session_key or "",
                )
            except Exception as exc:
                return {
                    "final_response": f"⚠️ Provider authentication failed: {exc}",
                    "messages": [],
                    "api_calls": 0,
                    "tools": [],
                }

            pr = runner._provider_routing
            reasoning_config = _runtime_config_for(runner).resolve_session_reasoning_config(
                source=source,
                session_key=session_key,
            )
            runner._reasoning_config = reasoning_config
            runner._service_tier = fast_command_for(runner).load_service_tier()
            stream_runtime = agent_streaming_for(
                runner,
                source=source,
                user_config=user_config,
                platform_key=platform_key,
                status_adapter=_status_adapter,
                status_chat_id=_status_chat_id,
                status_thread_metadata=_status_thread_metadata,
                event_message_id=event_message_id,
                loop=_loop_for_step,
                run_still_current=_run_still_current,
                on_new_content_message=(
                    tool_progress.reset_current_bubble
                    if tool_progress.queue is not None
                    else None
                ),
                stream_consumer_holder=stream_consumer_holder,
                interim_messages_enabled=interim_assistant_messages_enabled,
            )
            stream_runtime.configure()

            turn_route = _runtime_config_for(runner).resolve_turn_agent_config(message, model, runtime_kwargs)

            # Check agent cache — reuse the AIAgent from the previous message
            # in this session to preserve the frozen system prompt and tool
            # schemas for prompt cache hits.
            agent_cache = agent_cache_for(runner)
            _sig = agent_cache.agent_config_signature(
                turn_route["model"],
                turn_route["runtime"],
                enabled_toolsets,
                combined_ephemeral,
                cache_keys=agent_cache.extract_cache_busting_config(user_config),
            )
            agent = None
            _cache_lock = getattr(runner, "_agent_cache_lock", None)
            _cache = getattr(runner, "_agent_cache", None)
            if _cache_lock and _cache is not None:
                with _cache_lock:
                    cached = _cache.get(session_key)
                    if cached and cached[1] == _sig:
                        agent = cached[0]
                        # Refresh LRU order so the cap enforcement evicts
                        # truly-oldest entries, not the one we just used.
                        if hasattr(_cache, "move_to_end"):
                            try:
                                _cache.move_to_end(session_key)
                            except KeyError:
                                logger.debug(
                                    "agent cache LRU refresh missed session %s",
                                    session_key,
                                )
                        agent_cache.init_cached_agent_for_turn(agent, _interrupt_depth)
                        logger.debug("Reusing cached agent for session %s", session_key)

            if agent is None:
                # Config changed or first message — create fresh agent
                agent = AIAgent(
                    model=turn_route["model"],
                    **turn_route["runtime"],
                    max_iterations=max_iterations,
                    quiet_mode=True,
                    verbose_logging=False,
                    enabled_toolsets=enabled_toolsets,
                    disabled_toolsets=disabled_toolsets,
                    ephemeral_system_prompt=combined_ephemeral or None,
                    prefill_messages=runner._prefill_messages or None,
                    reasoning_config=reasoning_config,
                    service_tier=runner._service_tier,
                    request_overrides=turn_route.get("request_overrides"),
                    providers_allowed=pr.get("only"),
                    providers_ignored=pr.get("ignore"),
                    providers_order=pr.get("order"),
                    provider_sort=pr.get("sort"),
                    provider_require_parameters=pr.get("require_parameters", False),
                    provider_data_collection=pr.get("data_collection"),
                    session_id=session_id,
                    platform=platform_key,
                    user_id=source.user_id,
                    user_name=source.user_name,
                    chat_id=source.chat_id,
                    chat_name=source.chat_name,
                    chat_type=source.chat_type,
                    thread_id=source.thread_id,
                    gateway_session_key=session_key,
                    session_db=runner._session_db,
                    fallback_model=runner._fallback_model,
                )
                if _cache_lock and _cache is not None:
                    with _cache_lock:
                        _cache[session_key] = (agent, _sig)
                        agent_cache.enforce_agent_cache_cap()
                logger.debug("Created new agent for session %s (sig=%s)", session_key, _sig)

            # Per-message state — callbacks and reasoning config change every
            # turn and must not be baked into the cached agent constructor.
            agent.tool_progress_callback = tool_progress.callback if tool_progress_enabled else None
            agent.step_callback = _step_callback_sync if _hooks_ref.loaded_hooks else None
            agent.stream_delta_callback = stream_runtime.stream_delta_callback
            agent.interim_assistant_callback = (
                stream_runtime.interim_callback
                if interim_assistant_messages_enabled
                else None
            )
            agent.status_callback = _status_callback_sync
            agent.reasoning_config = reasoning_config
            agent.service_tier = runner._service_tier
            agent.request_overrides = turn_route.get("request_overrides") or {}
            interaction_callbacks = agent_interaction_callbacks_for(
                status_adapter=_status_adapter,
                status_chat_id=_status_chat_id,
                status_thread_metadata=_status_thread_metadata,
                session_key=session_key,
                run_generation=run_generation,
                loop=_loop_for_step,
                run_still_current=_run_still_current,
                redact_approval_command=_redact_approval_command,
            )
            interaction_callbacks.install_on(agent)

            # Store agent reference for interrupt support
            agent_holder[0] = agent
            # Capture the full tool definitions for transcript logging
            tools_holder[0] = agent.tools if hasattr(agent, 'tools') else None
            prepared_input = agent_input_preparation_for(
                runner=runner,
                session_key=session_key,
                history=history,
                build_replay_entry=_build_replay_entry,
                collect_history_media_paths=_collect_history_media_paths,
                last_transcript_timestamp=_last_transcript_timestamp,
                is_fresh_gateway_interruption=_is_fresh_gateway_interruption,
                auto_continue_freshness_window=_auto_continue_freshness_window,
                consume_native_image_paths=runner._consume_pending_native_image_paths,
            ).prepare(message)
            agent_history = prepared_input.agent_history
            _history_media_paths = prepared_input.history_media_paths

            _approval_session_token = interaction_callbacks.activate_approval()
            try:
                result = agent.run_conversation(
                    prepared_input.message,
                    conversation_history=agent_history,
                    task_id=session_id,
                )
            finally:
                interaction_callbacks.deactivate_approval(_approval_session_token)
            result_holder[0] = result

            # Signal the stream consumer that the agent is done
            if stream_consumer_holder[0] is not None:
                stream_consumer_holder[0].finish()
            return agent_result_finalizer_for(
                session_store=getattr(runner, "session_store", None),
                session_db=runner._session_db,
                source=source,
                session_id=session_id,
                session_key=session_key,
                tools=tools_holder[0],
                history_offset=len(agent_history),
                history_media_paths=_history_media_paths,
            ).finalize(result, agent_holder[0])
        
        # Start progress message sender if enabled
        progress_task = None
        if tool_progress_enabled:
            progress_task = asyncio.create_task(tool_progress.send_messages())

        # Start stream consumer task — polls for consumer creation since it
        # happens inside run_sync (thread pool) after the agent is constructed.
        stream_task = None
        _interrupt_detected = asyncio.Event()  # shared with backup check
        _NOTIFY_INTERVAL_RAW = _float_env("HERMES_AGENT_NOTIFY_INTERVAL", 180)
        _NOTIFY_INTERVAL = _NOTIFY_INTERVAL_RAW if _NOTIFY_INTERVAL_RAW > 0 else None
        _executor_task_ref = [None]
        run_supervisor = agent_run_supervisor_for(
            runner,
            source=source,
            session_key=session_key,
            run_generation=run_generation,
            agent_holder=agent_holder,
            stream_consumer_holder=stream_consumer_holder,
            status_thread_metadata=_status_thread_metadata,
            track_message_result=tool_progress.track_message_result,
            should_emit_long_running_notification=runner._should_emit_long_running_notification,
            notify_interval=_NOTIFY_INTERVAL,
        )
        stream_task = run_supervisor.start_stream_consumer()
        tracking_task = run_supervisor.start_agent_tracking()
        interrupt_monitor = run_supervisor.start_interrupt_monitor(_interrupt_detected)
        _notify_task = run_supervisor.start_long_running_notifications(lambda: _executor_task_ref[0])

        try:
            # Run in thread pool to not block.  Use an *inactivity*-based
            # timeout instead of a wall-clock limit: the agent can run for
            # hours if it's actively calling tools / receiving stream tokens,
            # but a hung API call or stuck tool with no activity for the
            # configured duration is caught and killed.  (#4815)
            #
            # Config: agent.gateway_timeout in config.yaml, or
            # HERMES_AGENT_TIMEOUT env var (env var takes precedence).
            # Default 1800s (30 min inactivity).  0 = unlimited.
            _agent_timeout_raw = _float_env("HERMES_AGENT_TIMEOUT", 1800)
            _agent_timeout = _agent_timeout_raw if _agent_timeout_raw > 0 else None
            _agent_warning_raw = _float_env("HERMES_AGENT_TIMEOUT_WARNING", 900)
            _agent_warning = _agent_warning_raw if _agent_warning_raw > 0 else None
            _executor_task = asyncio.ensure_future(
                runner._run_in_executor_with_context(run_sync)
            )
            _executor_task_ref[0] = _executor_task
            response = await agent_execution_monitor_for(
                runner=runner,
                source=source,
                session_key=session_key,
                agent_holder=agent_holder,
                result_holder=result_holder,
                tools_holder=tools_holder,
                interrupt_detected=_interrupt_detected,
                interrupt_monitor=interrupt_monitor,
                status_thread_metadata=_status_thread_metadata,
                agent_timeout=_agent_timeout,
                agent_warning=_agent_warning,
            ).wait(_executor_task)

            # Track fallback model state: if the agent switched to a
            # fallback model during this run, persist it so /model shows
            # the actually-active model instead of the config default.
            # Skip eviction when the run failed — evicting a failed agent
            # forces MCP reinit on the next message for no benefit (the
            # same error will recur).  This was the root cause of #7130:
            # a bad model ID triggered fallback → eviction → recreation →
            # MCP reinit → same 400 → loop, burning 91% CPU for hours.
            _agent = agent_holder[0]
            _result_for_fb = result_holder[0]
            _run_failed = _result_for_fb.get("failed") if _result_for_fb else False
            if _agent is not None and hasattr(_agent, 'model') and not _run_failed:
                _cfg_model = _resolve_gateway_model()
                if _agent.model != _cfg_model and not model_command_for(runner).is_intentional_model_switch(session_key, _agent.model):
                    # Fallback activated on a successful run — evict cached
                    # agent so the next message retries the primary model.
                    agent_cache_for(runner).evict_cached_agent(session_key)

            followup_result = await agent_pending_followup_for(runner).process(
                response=response,
                result=result_holder[0],
                context=PendingFollowupContext(
                    message=message,
                    context_prompt=context_prompt,
                    history=history,
                    source=source,
                    session_id=session_id,
                    session_key=session_key,
                    run_generation=run_generation,
                    interrupt_depth=_interrupt_depth,
                    status_thread_metadata=_status_thread_metadata,
                    stream_consumer=stream_consumer_holder[0],
                    stream_task=stream_task,
                ),
            )
            if followup_result is not None:
                return followup_result
        finally:
            await agent_turn_cleanup_for(runner).cleanup(
                AgentTurnCleanupContext(
                    progress_task=progress_task,
                    stream_task=stream_task,
                    stream_consumer=stream_consumer_holder[0],
                    interrupt_monitor=interrupt_monitor,
                    tracking_task=tracking_task,
                    notify_task=_notify_task,
                    session_key=session_key,
                    run_generation=run_generation,
                )
            )

        return agent_final_delivery_for().mark_stream_delivery_and_register_cleanup(
            response=response,
            stream_consumer=stream_consumer_holder[0],
            session_key=session_key,
            run_generation=run_generation,
            tool_progress=tool_progress,
        )


def agent_run_runtime_for(runner) -> AgentRunRuntime:
    return AgentRunRuntime(runner)
