"""Gateway session hygiene owner for agent turns."""

from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)


class GatewayAgentTurnHygieneService:
    def __init__(self, runner):
        self._runner = runner

    async def compress_if_needed(
        self,
        *,
        history,
        session_entry,
        source,
        session_key: str,
        event,
        quick_key: str,
        gateway_run,
        load_gateway_config,
        runtime_config_for,
        agent_cache_for,
    ):
        runner = self._runner
        # -----------------------------------------------------------------
        # Session hygiene: auto-compress pathologically large transcripts
        #
        # Long-lived gateway sessions can accumulate enough history that
        # every new message rehydrates an oversized transcript, causing
        # repeated truncation/context failures.  Detect this early and
        # compress proactively — before the agent even starts.  (#628)
        #
        # Token source priority:
        # 1. Actual API-reported prompt_tokens from the last turn
        #    (stored in session_entry.last_prompt_tokens)
        # 2. Rough char-based estimate (str(msg)//4). Overestimates
        #    by 30-50% on code/JSON-heavy sessions, but that just
        #    means hygiene fires a bit early — safe and harmless.
        # -----------------------------------------------------------------
        if history and len(history) >= 4:
            from agent.model_metadata import (
                estimate_messages_tokens_rough,
                get_model_context_length,
            )

            # Read model + compression config from config.yaml.
            # NOTE: hygiene threshold is intentionally HIGHER than the agent's
            # own compressor (0.85 vs 0.50).  Hygiene is a safety net for
            # sessions that grew too large between turns — it fires pre-agent
            # to prevent API failures.  The agent's own compressor handles
            # normal context management during its tool loop with accurate
            # real token counts.  Having hygiene at 0.50 caused premature
            # compression on every turn in long gateway sessions.
            _hyg_model = "anthropic/claude-sonnet-4.6"
            _hyg_threshold_pct = 0.85
            _hyg_compression_enabled = True
            _hyg_hard_msg_limit = 400
            _hyg_config_context_length = None
            _hyg_provider = None
            _hyg_base_url = None
            _hyg_api_key = None
            _hyg_data = {}
            try:
                _hyg_data = load_gateway_config()
                if _hyg_data:
                    # Resolve model name (same logic as run_sync)
                    _model_cfg = _hyg_data.get("model", {})
                    if isinstance(_model_cfg, str):
                        _hyg_model = _model_cfg
                    elif isinstance(_model_cfg, dict):
                        _hyg_model = _model_cfg.get("default") or _model_cfg.get("model") or _hyg_model
                        # Read explicit context_length override from model config
                        # (same as run_agent.py lines 995-1005)
                        _raw_ctx = _model_cfg.get("context_length")
                        if _raw_ctx is not None:
                            try:
                                _hyg_config_context_length = int(_raw_ctx)
                            except (TypeError, ValueError):
                                pass
                        # Read provider for accurate context detection
                        _hyg_provider = _model_cfg.get("provider") or None
                        _hyg_base_url = _model_cfg.get("base_url") or None

                    # Read compression settings — only use enabled flag.
                    # The threshold is intentionally separate from the agent's
                    # compression.threshold (hygiene runs higher).
                    _comp_cfg = _hyg_data.get("compression", {})
                    if isinstance(_comp_cfg, dict):
                        _hyg_compression_enabled = str(
                            _comp_cfg.get("enabled", True)
                        ).lower() in {"true", "1", "yes"}
                        _raw_hard_limit = _comp_cfg.get("hygiene_hard_message_limit")
                        if _raw_hard_limit is not None:
                            try:
                                _parsed = int(_raw_hard_limit)
                                if _parsed > 0:
                                    _hyg_hard_msg_limit = _parsed
                            except (TypeError, ValueError):
                                pass

                try:
                    _hyg_model, _hyg_runtime = runtime_config_for(runner).resolve_session_agent_runtime(
                        source=source,
                        session_key=session_key,
                        user_config=_hyg_data if isinstance(_hyg_data, dict) else None,
                    )
                    _hyg_provider = _hyg_runtime.get("provider") or _hyg_provider
                    _hyg_base_url = _hyg_runtime.get("base_url") or _hyg_base_url
                    _hyg_api_key = _hyg_runtime.get("api_key") or _hyg_api_key
                except Exception:
                    try:
                        _hyg_runtime = dict(gateway_run._resolve_runtime_agent_kwargs())
                        _hyg_model = _hyg_runtime.get("model") or _hyg_model
                        _hyg_provider = _hyg_runtime.get("provider") or _hyg_provider
                        _hyg_base_url = _hyg_runtime.get("base_url") or _hyg_base_url
                        _hyg_api_key = _hyg_runtime.get("api_key") or _hyg_api_key
                    except Exception:
                        pass

                # Check custom_providers per-model context_length
                # (same fallback as run_agent.py lines 1171-1189).
                # Must run after runtime resolution so _hyg_base_url is set.
                if _hyg_config_context_length is None and _hyg_base_url:
                    try:
                        try:
                            from hermes_cli.config import get_compatible_custom_providers as _gw_gcp
                            _hyg_custom_providers = _gw_gcp(_hyg_data)
                        except Exception:
                            _hyg_custom_providers = _hyg_data.get("custom_providers")
                            if not isinstance(_hyg_custom_providers, list):
                                _hyg_custom_providers = []
                        for _cp in _hyg_custom_providers:
                            if not isinstance(_cp, dict):
                                continue
                            _cp_url = (_cp.get("base_url") or "").rstrip("/")
                            if _cp_url and _cp_url == _hyg_base_url.rstrip("/"):
                                _cp_models = _cp.get("models", {})
                                if isinstance(_cp_models, dict):
                                    _cp_model_cfg = _cp_models.get(_hyg_model, {})
                                    if isinstance(_cp_model_cfg, dict):
                                        _cp_ctx = _cp_model_cfg.get("context_length")
                                        if _cp_ctx is not None:
                                            _hyg_config_context_length = int(_cp_ctx)
                                break
                    except (TypeError, ValueError):
                        pass
            except Exception:
                pass

            if _hyg_compression_enabled:
                _hyg_context_length = get_model_context_length(
                    _hyg_model,
                    base_url=_hyg_base_url or "",
                    api_key=_hyg_api_key or "",
                    config_context_length=_hyg_config_context_length,
                    provider=_hyg_provider or "",
                )
                _compress_token_threshold = int(
                    _hyg_context_length * _hyg_threshold_pct
                )
                _warn_token_threshold = int(_hyg_context_length * 0.95)

                _msg_count = len(history)

                # Prefer actual API-reported tokens from the last turn
                # (stored in session entry) over the rough char-based estimate.
                _stored_tokens = session_entry.last_prompt_tokens
                if _stored_tokens > 0:
                    _approx_tokens = _stored_tokens
                    _token_source = "actual"
                else:
                    _approx_tokens = estimate_messages_tokens_rough(history)
                    _token_source = "estimated"
                    # Note: rough estimates overestimate by 30-50% for code/JSON-heavy
                    # sessions, but that just means hygiene fires a bit early — which
                    # is safe and harmless.  The 85% threshold already provides ample
                    # headroom (agent's own compressor runs at 50%).  A previous 1.4x
                    # multiplier tried to compensate by inflating the threshold, but
                    # 85% * 1.4 = 119% of context — which exceeds the model's limit
                    # and prevented hygiene from ever firing for ~200K models (GLM-5).

                # Hard safety valve: force compression if message count is
                # extreme, regardless of token estimates.  This breaks the
                # death spiral where API disconnects prevent token data
                # collection, which prevents compression, which causes more
                # disconnects.  400 messages is well above normal sessions
                # but catches runaway growth before it becomes unrecoverable.
                # Threshold is configurable via
                # compression.hygiene_hard_message_limit.
                # (#2153)
                _HARD_MSG_LIMIT = _hyg_hard_msg_limit
                _needs_compress = (
                    _approx_tokens >= _compress_token_threshold
                    or _msg_count >= _HARD_MSG_LIMIT
                )

                if _needs_compress:
                    logger.info(
                        "Session hygiene: %s messages, ~%s tokens (%s) — auto-compressing "
                        "(threshold: %s%% of %s = %s tokens)",
                        _msg_count, f"{_approx_tokens:,}", _token_source,
                        int(_hyg_threshold_pct * 100),
                        f"{_hyg_context_length:,}",
                        f"{_compress_token_threshold:,}",
                    )

                    _hyg_meta = runner._thread_metadata_for_source(source, runner._reply_anchor_for_event(event))

                    try:
                        from run_agent import AIAgent

                        try:
                            _hyg_model, _hyg_runtime = runtime_config_for(runner).resolve_session_agent_runtime(
                                source=source,
                                session_key=session_key,
                                user_config=_hyg_data if isinstance(_hyg_data, dict) else None,
                            )
                        except Exception:
                            _hyg_runtime = dict(gateway_run._resolve_runtime_agent_kwargs())
                            _hyg_model = _hyg_runtime.get("model") or _hyg_model
                        if _hyg_runtime.get("api_key"):
                            _hyg_msgs = [
                                {"role": m.get("role"), "content": m.get("content")}
                                for m in history
                                if m.get("role") in {"user", "assistant"}
                                and m.get("content")
                            ]

                            if len(_hyg_msgs) >= 4:
                                _hyg_agent = AIAgent(
                                    **_hyg_runtime,
                                    model=_hyg_model,
                                    max_iterations=4,
                                    quiet_mode=True,
                                    skip_memory=True,
                                    enabled_toolsets=["memory"],
                                    session_id=session_entry.session_id,
                                )
                                try:
                                    # The hygiene agent rotates the session
                                    # forward to a continuation id that becomes
                                    # the gateway session's live row. It must
                                    # never finalize on close() (today it has no
                                    # session_db so close() no-ops, but this
                                    # guards a future where one is wired in).
                                    _hyg_agent._end_session_on_close = False
                                    _hyg_agent._print_fn = lambda *a, **kw: None

                                    loop = asyncio.get_running_loop()
                                    _compressed, _ = await loop.run_in_executor(
                                        None,
                                        lambda: _hyg_agent._compress_context(
                                            _hyg_msgs, "",
                                            approx_tokens=_approx_tokens,
                                        ),
                                    )

                                    # _compress_context ends the old session and creates
                                    # a new session_id.  Write compressed messages into
                                    # the NEW session so the old transcript stays intact
                                    # and searchable via session_search.
                                    _hyg_new_sid = _hyg_agent.session_id
                                    if _hyg_new_sid != session_entry.session_id:
                                        session_entry.session_id = _hyg_new_sid
                                        runner.session_store._save()

                                    runner.session_store.rewrite_transcript(
                                        session_entry.session_id, _compressed
                                    )
                                    # Reset stored token count — transcript was rewritten
                                    session_entry.last_prompt_tokens = 0
                                    history = _compressed
                                    _new_count = len(_compressed)
                                    _new_tokens = estimate_messages_tokens_rough(
                                        _compressed
                                    )

                                    logger.info(
                                        "Session hygiene: compressed %s → %s msgs, "
                                        "~%s → ~%s tokens",
                                        _msg_count, _new_count,
                                        f"{_approx_tokens:,}", f"{_new_tokens:,}",
                                    )

                                    if _new_tokens >= _warn_token_threshold:
                                        logger.warning(
                                            "Session hygiene: still ~%s tokens after "
                                            "compression",
                                            f"{_new_tokens:,}",
                                        )

                                    # If summary generation failed, the
                                    # compressor aborts entirely and returns
                                    # messages unchanged — nothing is dropped.
                                    # Surface a visible warning to the gateway
                                    # user — agent.log alone is invisible on
                                    # TG/Discord/etc. — so they know the chat
                                    # is "frozen" at the current size and can
                                    # /compress to retry or /reset to start
                                    # fresh.
                                    _comp = getattr(_hyg_agent, "context_compressor", None)
                                    if _comp is not None and getattr(_comp, "_last_compress_aborted", False):
                                        _err = getattr(_comp, "_last_summary_error", None) or "unknown error"
                                        _warn_msg = (
                                            "⚠️ Context compression aborted "
                                            f"({_err}). No messages were dropped — "
                                            "conversation is unchanged. Run /compress "
                                            "to retry, /reset for a clean session, or "
                                            "check your auxiliary.compression model "
                                            "configuration."
                                        )
                                        try:
                                            _adapter = runner.adapters.get(source.platform)
                                            if _adapter and source.chat_id:
                                                await _adapter.send(source.chat_id, _warn_msg, metadata=_hyg_meta)
                                        except Exception as _werr:
                                            logger.warning(
                                                "Failed to deliver compression-failure warning to user: %s",
                                                _werr,
                                            )
                                    # Separately: if the user's CONFIGURED aux
                                    # model failed and we recovered by falling
                                    # back to the main model, tell them — a
                                    # misconfigured auxiliary.compression.model
                                    # is something only they can fix, and
                                    # silent recovery would hide it.
                                    elif _comp is not None and getattr(_comp, "_last_aux_model_failure_model", None):
                                        _aux_model = getattr(_comp, "_last_aux_model_failure_model", "")
                                        _aux_err = getattr(_comp, "_last_aux_model_failure_error", None) or "unknown error"
                                        _aux_msg = (
                                            f"ℹ️ Configured compression model `{_aux_model}` "
                                            f"failed ({_aux_err}). Recovered using your main "
                                            "model — context is intact — but you may want to "
                                            "check `auxiliary.compression.model` in config.yaml."
                                        )
                                        try:
                                            _adapter = runner.adapters.get(source.platform)
                                            if _adapter and source.chat_id:
                                                await _adapter.send(source.chat_id, _aux_msg, metadata=_hyg_meta)
                                        except Exception as _werr:
                                            logger.warning(
                                                "Failed to deliver aux-model-fallback notice to user: %s",
                                                _werr,
                                            )
                                finally:
                                    # Evict the cached agent so the next turn
                                    # rebuilds its system prompt from current
                                    # SOUL.md, memory, and skills.
                                    agent_cache_for(runner).evict_cached_agent(session_key)
                                    runner._cleanup_agent_resources(_hyg_agent)

                    except Exception as e:
                        logger.warning(
                            "Session hygiene auto-compress failed: %s", e
                        )


        return history


def agent_turn_hygiene_for(runner) -> GatewayAgentTurnHygieneService:
    service = getattr(runner, "agent_turn_hygiene", None)
    if isinstance(service, GatewayAgentTurnHygieneService):
        return service
    service = GatewayAgentTurnHygieneService(runner)
    runner.agent_turn_hygiene = service
    return service
