"""Gateway agent-turn orchestration runtime."""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import math
import os
import sys
import time

from hermes_agent.gateway.runtime_config import (
    load_gateway_runtime_config,
    resolve_runtime_agent_kwargs,
)
from hermes_agent.composition.async_sqlite import run_sqlite_io
from hermes_constants import get_hermes_home
from hermes_gateway.agent_cache import agent_cache_for
from hermes_gateway.agent_turn_context import agent_turn_context_for
from hermes_gateway.agent_turn_hygiene import agent_turn_hygiene_for
from hermes_gateway.agent_turn_persistence import agent_turn_persistence_for
from hermes_gateway.bootstrap import home_target_env_var
from hermes_gateway.config import Platform
from hermes_gateway.gateway_runtime_config import runtime_config_for
from hermes_gateway.media_delivery import media_delivery_for
from hermes_gateway.output_policy import sanitize_gateway_final_response
from hermes_gateway.platform_notice import platform_notice_for
from hermes_gateway.process_notifications import (
    drain_gateway_watch_events,
    format_gateway_process_notification,
)
from hermes_gateway.process_watcher import process_watcher_for
from hermes_gateway.response_normalization import normalize_empty_agent_response
from hermes_gateway.resume_pending import should_clear_resume_pending_after_turn
from hermes_gateway.response_filters import is_intentional_silence_agent_result
from hermes_gateway.session_context import build_session_context
from hermes_gateway.session_navigation_commands import session_navigation_for
from hermes_gateway.session_runtime_state import session_runtime_state_for
from hermes_gateway.session_turn_lease import session_turn_lease_for
from hermes_gateway.voice_runtime import voice_runtime_for

logger = logging.getLogger(__name__)


def _load_gateway_config() -> dict:
    return load_gateway_runtime_config(_active_hermes_home())


def _active_hermes_home():
    runner_module = sys.modules.get("hermes_gateway.runner")
    if runner_module is not None and hasattr(runner_module, "_hermes_home"):
        return getattr(runner_module, "_hermes_home")
    return get_hermes_home()


def _resolve_runtime_agent_kwargs() -> dict:
    runner_module = sys.modules.get("hermes_gateway.runner")
    patched = getattr(runner_module, "_resolve_runtime_agent_kwargs", None) if runner_module is not None else None
    if callable(patched):
        return dict(patched())
    return resolve_runtime_agent_kwargs(_active_hermes_home())


def _platform_config_key(platform: Platform) -> str:
    return "cli" if platform == Platform.LOCAL else platform.value


class GatewayAgentTurnRuntime:
    def __init__(self, runner):
        self._runner = runner

    async def handle(self, event, source, _quick_key: str, run_generation: int):
        """Inner handler that runs under the _running_agents sentinel guard."""
        runner = self._runner
        _msg_start_time = time.time()
        _platform_name = source.platform.value if hasattr(source.platform, "value") else str(source.platform)
        _msg_preview = (event.text or "")[:80].replace("\n", " ")
        logger.info(
            "inbound message: platform=%s user=%s chat=%s msg=%r",
            _platform_name, source.user_name or source.user_id or "unknown",
            source.chat_id or "unknown", _msg_preview,
        )

        # Get or create session
        # Topic-mode DMs: rewrite a stale/foreign thread_id to the user's
        # last-active topic so a cross-topic Reply or stripped plain reply
        # doesn't fragment the conversation across sessions.
        recovered = await run_sqlite_io(
            session_navigation_for(runner).recover_telegram_topic_thread_id,
            source,
        )
        if recovered is not None:
            logger.info(
                "telegram topic recovery: chat=%s user=%s %r -> %s",
                source.chat_id, source.user_id, source.thread_id, recovered,
            )
            source = dataclasses.replace(source, thread_id=recovered)
            try:
                event.source = source
            except Exception:
                logger.debug("Suppressed recoverable gateway exception", exc_info=True)

        session_entry = await run_sqlite_io(
            runner.session_store.get_or_create_session,
            source,
        )
        session_key = session_entry.session_key
        runner._cache_session_source(session_key, source)
        if await run_sqlite_io(
            session_navigation_for(runner).is_telegram_topic_lane,
            source,
        ):
            try:
                binding = (
                    await run_sqlite_io(
                        runner._session_db.telegram_topics.get_telegram_topic_binding,
                        chat_id=str(source.chat_id),
                        thread_id=str(source.thread_id),
                    )
                    if runner._session_db
                    else None
                )
            except Exception:
                logger.debug("Failed to read Telegram topic binding", exc_info=True)
                binding = None
            if binding:
                bound_session_id = str(binding.get("session_id") or "")
                if bound_session_id and bound_session_id != session_entry.session_id:
                    # Route the override through SessionStore so the session_key
                    # → session_id mapping is persisted to disk and the previous
                    # lane session is ended cleanly. Mutating session_entry in
                    # place here created a split-brain state where the JSON
                    # index pointed at one id but code downstream used another.
                    switched = await run_sqlite_io(
                        runner.session_store.switch_session,
                        session_key,
                        bound_session_id,
                    )
                    if switched is not None:
                        session_entry = switched
            else:
                try:
                    await run_sqlite_io(
                        session_navigation_for(runner).record_telegram_topic_binding,
                        source,
                        session_entry,
                    )
                except Exception:
                    logger.debug("Failed to record Telegram topic binding", exc_info=True)
        if getattr(session_entry, "was_auto_reset", False):
            session_runtime_state_for(runner).clear_conversation_scope(
                session_key,
                reason="auto_reset",
            )
            # The cache is keyed by routing key and otherwise carries the old
            # compressor/memory instances into the new durable conversation.
            agent_cache_for(runner).evict_cached_agent(session_key)
        
        # Emit session:start for new or auto-reset sessions
        _is_new_session = (
            session_entry.created_at == session_entry.updated_at
            or getattr(session_entry, "was_auto_reset", False)
            or getattr(session_entry, "is_fresh_reset", False)
        )
        # Consume the is_fresh_reset flag immediately so it doesn't leak
        # onto subsequent messages in the same session (issue #6508).
        if getattr(session_entry, "is_fresh_reset", False):
            session_entry.is_fresh_reset = False
        if _is_new_session:
            await runner.hooks.emit("session:start", {
                "platform": source.platform.value if source.platform else "",
                "user_id": source.user_id,
                "session_id": session_entry.session_id,
                "session_key": session_key,
            })
        
        # Build session context
        context = build_session_context(source, runner.config, session_entry)
        
        # Set session context variables for tools (task-local, concurrency-safe)
        _session_env_tokens = runner._set_session_env(context)
        
        # Read privacy.redact_pii from config (re-read per message)
        _redact_pii = False
        try:
            _pcfg = _load_gateway_config()
            _redact_pii = bool((_pcfg.get("privacy") or {}).get("redact_pii", False))
        except Exception:
            logger.debug("Suppressed recoverable gateway exception", exc_info=True)

        turn_context_notes: list[str] = []
        context_prompt = agent_turn_context_for(runner).pinned_context_prompt(
            context=context,
            redact_pii=_redact_pii,
            session_key=session_key,
        )
        
        # If the previous session expired and was auto-reset, prepend a notice
        # so the agent knows this is a fresh conversation (not an intentional /reset).
        if getattr(session_entry, 'was_auto_reset', False):
            reset_reason = getattr(session_entry, 'auto_reset_reason', None) or 'idle'
            if reset_reason == "suspended":
                context_note = "[System note: The user's previous session was stopped and suspended. This is a fresh conversation with no prior context.]"
            elif reset_reason == "daily":
                context_note = "[System note: The user's session was automatically reset by the daily schedule. This is a fresh conversation with no prior context.]"
            else:
                context_note = "[System note: The user's previous session expired due to inactivity. This is a fresh conversation with no prior context.]"
            turn_context_notes.append(context_note)

            # Send a user-facing notification explaining the reset, unless:
            # - notifications are disabled in config
            # - the platform is excluded (e.g. api_server, webhook)
            # - the expired session had no activity (nothing was cleared)
            try:
                policy = runner.session_store.config.get_reset_policy(
                    platform=source.platform,
                    session_type=getattr(source, 'chat_type', 'dm'),
                )
                platform_name = source.platform.value if source.platform else ""
                had_activity = getattr(session_entry, 'reset_had_activity', False)
                # Suspended sessions always notify (they were explicitly stopped
                # or crashed mid-operation) — skip the policy check.
                should_notify = reset_reason == "suspended" or (
                    policy.notify
                    and had_activity
                    and platform_name not in policy.notify_exclude_platforms
                )
                if should_notify:
                    adapter = runner.adapters.get(source.platform)
                    if adapter:
                        if reset_reason == "suspended":
                            reason_text = "previous session was stopped or interrupted"
                        elif reset_reason == "daily":
                            reason_text = f"daily schedule at {policy.at_hour}:00"
                        else:
                            hours = policy.idle_minutes // 60
                            mins = policy.idle_minutes % 60
                            duration = f"{hours}h" if not mins else f"{hours}h {mins}m" if hours else f"{mins}m"
                            reason_text = f"inactive for {duration}"
                        notice = (
                            f"◐ Session automatically reset ({reason_text}). "
                            f"Conversation history cleared.\n"
                            f"Use /resume to browse and restore a previous session.\n"
                            f"Adjust reset timing in config.yaml under session_reset."
                        )
                        try:
                            session_info = runner._format_session_info()
                            if session_info:
                                notice = f"{notice}\n\n{session_info}"
                        except Exception:
                            logger.debug("Suppressed recoverable gateway exception", exc_info=True)
                        await adapter.send(
                            source.chat_id, notice,
                            metadata=runner._thread_metadata_for_source(source),
                        )
            except Exception as e:
                logger.debug("Auto-reset notification failed (non-fatal): %s", e)

            session_entry.was_auto_reset = False
            session_entry.auto_reset_reason = None

        # Auto-load skill(s) for topic/channel bindings (Telegram DM Topics,
        # Discord channel_skill_bindings).  Supports a single name or ordered list.
        # Only inject on NEW sessions — ongoing conversations already have the
        # skill content in their conversation history from the first message.
        _auto = getattr(event, "auto_skill", None)
        if _is_new_session and _auto:
            _skill_names = [_auto] if isinstance(_auto, str) else list(_auto)
            try:
                from agent.skill_commands import _load_skill_payload, _build_skill_message
                _combined_parts: list[str] = []
                _loaded_names: list[str] = []
                for _sname in _skill_names:
                    _loaded = _load_skill_payload(_sname, task_id=_quick_key)
                    if _loaded:
                        _loaded_skill, _skill_dir, _display_name = _loaded
                        _note = (
                            f'[IMPORTANT: The "{_display_name}" skill is auto-loaded. '
                            f"Follow its instructions for this session.]"
                        )
                        _part = _build_skill_message(_loaded_skill, _skill_dir, _note)
                        if _part:
                            _combined_parts.append(_part)
                            _loaded_names.append(_sname)
                    else:
                        logger.warning("[Gateway] Auto-skill '%s' not found", _sname)
                if _combined_parts:
                    # Append the user's original text after all skill payloads
                    _combined_parts.append(event.text)
                    event.text = "\n\n".join(_combined_parts)
                    logger.info(
                        "[Gateway] Auto-loaded skill(s) %s for session %s",
                        _loaded_names, session_key,
                    )
            except Exception as e:
                logger.warning("[Gateway] Failed to auto-load skill(s) %s: %s", _skill_names, e)

        # Busy guards are keyed by routing key, while transcript ownership is
        # keyed by the resolved session_id. Session navigation can map multiple
        # routing keys to one durable conversation, so serialize the entire
        # load/run/persist region at that true ownership boundary.
        await session_turn_lease_for(runner).acquire(
            session_entry.session_id,
            routing_key=_quick_key,
            generation=run_generation,
        )

        # Load conversation history from transcript
        history = await run_sqlite_io(
            runner.session_store.load_transcript,
            session_entry.session_id,
        )
        
        history = await agent_turn_hygiene_for(runner).compress_if_needed(
            history=history,
            session_entry=session_entry,
            source=source,
            session_key=session_key,
            event=event,
            quick_key=_quick_key,
            run_generation=run_generation,
            load_gateway_config=_load_gateway_config,
            runtime_config_for=runtime_config_for,
            agent_cache_for=agent_cache_for,
            resolve_runtime_agent_kwargs=_resolve_runtime_agent_kwargs,
        )

        turn_context_notes.extend(
            await agent_turn_context_for(runner).collect_turn_notes(
            history=history,
            source=source,
            event=event,
            session_key=session_key,
            home_target_env_var=home_target_env_var,
            platform_notice_for=platform_notice_for,
            voice_runtime_for=voice_runtime_for,
            )
        )

        # -----------------------------------------------------------------
        # Auto-analyze images sent by the user
        #
        # If the user attached image(s), we run the vision tool eagerly so
        # the conversation model always receives a text description.  The
        # local file path is also included so the model can re-examine the
        # image later with a more targeted question via vision_analyze.
        #
        # We filter to image paths only (by media_type) so that non-image
        # attachments (documents, audio, etc.) are not sent to the vision
        # tool even when they appear in the same message.
        # -----------------------------------------------------------------
        message_text = await runner._prepare_inbound_message_text(
            event=event,
            source=source,
            history=history,
        )
        if message_text is None:
            return

        # Bind this gateway run generation to the adapter's active-session
        # event so deferred post-delivery callbacks can be released by the
        # same run that registered them.
        session_runtime_state_for(runner).bind_adapter_run_generation(
            runner.adapters.get(source.platform),
            session_key,
            run_generation,
        )

        try:
            # Emit agent:start hook
            hook_ctx = {
                "platform": source.platform.value if source.platform else "",
                "user_id": source.user_id,
                "chat_id": source.chat_id or "",
                "session_id": session_entry.session_id,
                "message": message_text[:500],
            }
            await runner.hooks.emit("agent:start", hook_ctx)

            # Run the agent
            agent_result = await runner._run_agent(
                message=message_text,
                context_prompt=context_prompt,
                history=history,
                source=source,
                session_id=session_entry.session_id,
                session_key=session_key,
                run_generation=run_generation,
                event_message_id=runner._reply_anchor_for_event(event),
                channel_prompt=event.channel_prompt,
                turn_context_notes=turn_context_notes,
            )

            # Stop persistent typing indicator now that the agent is done
            try:
                _typing_adapter = runner.adapters.get(source.platform)
                if _typing_adapter and hasattr(_typing_adapter, "stop_typing"):
                    await _typing_adapter.stop_typing(source.chat_id)
            except Exception:
                logger.debug("Suppressed recoverable gateway exception", exc_info=True)

            if not session_runtime_state_for(runner).is_session_run_current(_quick_key, run_generation):
                logger.info(
                    "Discarding stale agent result for %s — generation %d is no longer current",
                    _quick_key or "?",
                    run_generation,
                )
                _stale_adapter = runner.adapters.get(source.platform)
                if getattr(type(_stale_adapter), "pop_post_delivery_callback", None) is not None:
                    _stale_adapter.pop_post_delivery_callback(
                        _quick_key,
                        generation=run_generation,
                    )
                elif _stale_adapter and hasattr(_stale_adapter, "_post_delivery_callbacks"):
                    _stale_adapter._post_delivery_callbacks.pop(_quick_key, None)
                return None

            response = agent_result.get("final_response") or ""
            _intentional_silence = is_intentional_silence_agent_result(
                agent_result,
                response,
            )

            # Convert the agent's internal "(empty)" sentinel into a
            # user-friendly message.  "(empty)" means the model failed to
            # produce visible content after exhausting all retries (nudge,
            # prefill, empty-retry, fallback).  Sending the raw sentinel
            # looks like a bug; a short explanation is more helpful.
            if response == "(empty)":
                response = (
                    "⚠️ The model returned no response after processing tool "
                    "results. This can happen with some models — try again or "
                    "rephrase your question."
                )
            agent_messages = agent_result.get("messages", [])
            _response_time = time.time() - _msg_start_time
            _api_calls = agent_result.get("api_calls", 0)
            _resp_len = len(response)
            logger.info(
                "response ready: platform=%s chat=%s time=%.1fs api_calls=%d response=%d chars",
                _platform_name, source.chat_id or "unknown",
                _response_time, _api_calls, _resp_len,
            )

            # Successful turn — clear any stuck-loop counter for this session.
            # This ensures the counter only accumulates across CONSECUTIVE
            # restarts where the session was active (never completed).
            #
            # Also clear the resume_pending flag (set by drain-timeout
            # shutdown) — the turn ran to completion, so recovery
            # succeeded and subsequent messages should no longer receive
            # the restart-interruption system note.
            if session_key and should_clear_resume_pending_after_turn(agent_result):
                runner._clear_restart_failure_count(session_key)
                try:
                    await run_sqlite_io(
                        runner.session_store.clear_resume_pending,
                        session_key,
                    )
                except Exception as _e:
                    logger.debug(
                        "clear_resume_pending failed for %s: %s",
                        session_key, _e,
                    )

            # Normalize empty responses: surface errors, partial failures, and
            # the case where agent did work but returned no text. Fix for #18765.
            if not _intentional_silence:
                response = normalize_empty_agent_response(
                    agent_result, response, history_len=len(history),
                )
                response = sanitize_gateway_final_response(source.platform, response)

            # If the agent's session_id changed during compression, update
            # session_entry so transcript writes below go to the right session.
            if agent_result.get("session_id") and agent_result["session_id"] != session_entry.session_id:
                rotated_session_id = str(agent_result["session_id"])
                updated = await run_sqlite_io(
                    runner.session_store.update_entry_session_id,
                    session_entry.session_key,
                    rotated_session_id,
                )
                if updated:
                    session_turn_lease_for(runner).rebind(
                        _quick_key,
                        run_generation,
                        rotated_session_id,
                    )
                    try:
                        await run_sqlite_io(
                            session_navigation_for(runner).record_telegram_topic_binding,
                            source,
                            session_entry,
                        )
                    except Exception:
                        logger.debug(
                            "Failed to synchronize topic binding after agent session rotation",
                            exc_info=True,
                        )

            # Prepend reasoning/thinking if display is enabled (per-platform)
            try:
                from hermes_gateway.display_config import resolve_display_setting as _rds
                _show_reasoning_effective = _rds(
                    _load_gateway_config(),
                    _platform_config_key(source.platform),
                    "show_reasoning",
                    getattr(runner, "_show_reasoning", False),
                )
            except Exception:
                _show_reasoning_effective = getattr(runner, "_show_reasoning", False)
            if _show_reasoning_effective and response and not _intentional_silence:
                last_reasoning = agent_result.get("last_reasoning")
                if last_reasoning:
                    # Collapse long reasoning to keep messages readable
                    lines = last_reasoning.strip().splitlines()
                    if len(lines) > 15:
                        display_reasoning = "\n".join(lines[:15])
                        display_reasoning += f"\n_... ({len(lines) - 15} more lines)_"
                    else:
                        display_reasoning = last_reasoning.strip()
                    response = f"💭 **Reasoning:**\n```\n{display_reasoning}\n```\n\n{response}"

            # Runtime-metadata footer — only on the FINAL message of the turn.
            # Off by default (display.runtime_footer.enabled=false).  When
            # streaming already delivered the body, we can't mutate the sent
            # text, so we fire a separate trailing send below.
            _footer_line = ""
            try:
                from hermes_gateway.runtime_footer import build_footer_line as _bfl
                _footer_line = _bfl(
                    user_config=_load_gateway_config(),
                    platform_key=_platform_config_key(source.platform),
                    model=agent_result.get("model"),
                    context_tokens=agent_result.get("last_prompt_tokens", 0) or 0,
                    context_length=agent_result.get("context_length") or None,
                    cwd=os.environ.get("TERMINAL_CWD", ""),
                )
            except Exception as _footer_err:
                logger.debug("runtime_footer build failed: %s", _footer_err)
                _footer_line = ""
            if (
                _footer_line
                and response
                and not _intentional_silence
                and not agent_result.get("already_sent")
            ):
                response = f"{response}\n\n{_footer_line}"

            # Emit agent:end hook
            await runner.hooks.emit("agent:end", {
                **hook_ctx,
                "response": (response or "")[:500],
            })
            
            # Check for pending process watchers (check_interval on background processes)
            try:
                from tools.process_registry import process_registry
                while process_registry.pending_watchers:
                    watcher = process_registry.pending_watchers.pop(0)
                    asyncio.create_task(process_watcher_for(runner).run_process_watcher(watcher))
            except Exception as e:
                logger.error("Process watcher setup error: %s", e)

            # Drain watch pattern notifications that arrived during the agent run.
            # Watch events and completions share the same queue; process
            # completions are already handled by the per-process watcher task
            # above, so we only inject watch-type events here.
            #
            # Subagent completion is persisted as typed Activity/Run state and
            # never enters this process-notification queue.
            try:
                from tools.process_registry import process_registry as _pr
                _watch_events = drain_gateway_watch_events(_pr.completion_queue)
                for evt in _watch_events:
                    synth_text = format_gateway_process_notification(evt)
                    if synth_text:
                        try:
                            await process_watcher_for(runner).inject_watch_notification(synth_text, evt)
                        except Exception as e2:
                            logger.error("Watch notification injection error: %s", e2)
            except Exception as e:
                logger.debug("Watch queue drain error: %s", e)

            response = await agent_turn_persistence_for(runner).persist(
                event=event,
                source=source,
                session_entry=session_entry,
                session_key=session_key,
                history=history,
                message_text=message_text,
                response=response,
                agent_result=agent_result,
                agent_messages=agent_messages,
            )
            if _intentional_silence:
                # Persist the control marker above so the model retains its
                # own decision, then erase only the outbound representation.
                response = ""
                _footer_line = ""
                agent_result.pop("already_sent", None)

            # Auto voice reply: send TTS audio before the text response
            _already_sent = bool(agent_result.get("already_sent"))
            if voice_runtime_for(runner).should_send_voice_reply(
                event,
                response,
                agent_messages,
                already_sent=_already_sent,
            ):
                await voice_runtime_for(runner).send_voice_reply(event, response)

            # If streaming already delivered the response, extract and
            # deliver any MEDIA: files before returning None.  Streaming
            # sends raw text chunks that include MEDIA: tags — the normal
            # post-processing in _process_message_background is skipped
            # when already_sent is True, so media files would never be
            # delivered without this.
            #
            # Never skip when the agent failed — the error message is new
            # content the user hasn't seen (streaming only sent earlier
            # partial output before the failure).  Without this guard,
            # users see the agent "stop responding without explanation."
            if agent_result.get("already_sent") and not agent_result.get("failed"):
                if response:
                    _media_adapter = runner.adapters.get(source.platform)
                    if _media_adapter:
                        await media_delivery_for(runner).deliver_media_from_response(
                            response, event, _media_adapter,
                        )
                # Streaming already delivered the body text, but the footer was
                # intentionally held back (see the `not already_sent` gate above).
                # Send it now as a small trailing message so Telegram/Discord/etc.
                # still surface the runtime metadata on the final reply.
                if _footer_line:
                    try:
                        _foot_adapter = runner.adapters.get(source.platform)
                        if _foot_adapter:
                            await _foot_adapter.send(
                                source.chat_id,
                                _footer_line,
                                metadata=runner._thread_metadata_for_source(source, runner._reply_anchor_for_event(event)),
                            )
                    except Exception as _e:
                        logger.debug("trailing footer send failed: %s", _e)
                return None

            return response
            
        except Exception as e:
            # Stop typing indicator on error too
            try:
                _err_adapter = runner.adapters.get(source.platform)
                if _err_adapter and hasattr(_err_adapter, "stop_typing"):
                    await _err_adapter.stop_typing(source.chat_id)
            except Exception:
                logger.debug("Suppressed recoverable gateway exception", exc_info=True)
            logger.exception("Agent error in session %s", session_key)
            error_type = type(e).__name__
            error_detail = str(e)[:300] if str(e) else "no details available"
            status_hint = ""
            status_code = getattr(e, "status_code", None)
            _hist_len = len(history) if 'history' in locals() else 0
            if status_code == 401:
                status_hint = " Check your API key or run `claude /login` to refresh OAuth credentials."
            elif status_code == 402:
                status_hint = " Your API balance or quota is exhausted. Check your provider dashboard."
            elif status_code == 429:
                # Check if this is a plan usage limit (resets on a schedule) vs a transient rate limit
                _err_body = getattr(e, "response", None)
                _err_json = {}
                try:
                    if _err_body is not None:
                        _err_json = _err_body.json().get("error", {})
                        if not isinstance(_err_json, dict):
                            _err_json = {}
                except Exception:
                    logger.debug("Suppressed recoverable gateway exception", exc_info=True)
                if _err_json.get("type") == "usage_limit_reached":
                    _resets_in = _err_json.get("resets_in_seconds")
                    if _resets_in and _resets_in > 0:
                        import math
                        _hours = math.ceil(_resets_in / 3600)
                        status_hint = f" Your plan's usage limit has been reached. It resets in ~{_hours}h."
                    else:
                        status_hint = " Your plan's usage limit has been reached. Please wait until it resets."
                else:
                    status_hint = " You are being rate-limited. Please wait a moment and try again."
            elif status_code == 529:
                status_hint = " The API is temporarily overloaded. Please try again shortly."
            elif status_code in {400, 500}:
                # 400 with a large session is context overflow.
                # 500 with a large session often means the payload is too large
                # for the API to process — treat it the same way.
                if _hist_len > 50:
                    return (
                        "⚠️ Session too large for the model's context window.\n"
                        "Use /compact to compress the conversation, or "
                        "/reset to start fresh."
                    )
                elif status_code == 400:
                    status_hint = " The request was rejected by the API."
            return (
                f"Sorry, I encountered an error ({error_type}).\n"
                f"{error_detail}\n"
                f"{status_hint}"
                "Try again or use /reset to start a fresh session."
            )
        finally:
            # Restore session context variables to their pre-handler state
            runner._clear_session_env(_session_env_tokens)


def agent_turn_runtime_for(runner) -> GatewayAgentTurnRuntime:
    service = getattr(runner, "agent_turn_runtime", None)
    if isinstance(service, GatewayAgentTurnRuntime):
        return service
    service = GatewayAgentTurnRuntime(runner)
    runner.agent_turn_runtime = service
    return service
