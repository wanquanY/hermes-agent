"""Codex API runtime — App Server and Responses-API streaming paths.

Extracted from :class:`AIAgent` to keep the agent loop file focused.
Each function takes the parent ``AIAgent`` as its first argument
(``agent``).  AIAgent keeps thin forwarder methods for backward
compatibility.

* ``run_codex_app_server_turn`` — drives one turn through the
  ``codex_app_server`` subprocess client (used when a Codex CLI install
  is the active provider).
* ``run_codex_stream`` — streams a Codex Responses API call (the
  ``codex_responses`` api_mode).
* ``run_codex_create_stream_fallback`` — recovery path when the
  Responses ``stream=True`` initial create fails.
"""

from __future__ import annotations

import json
import logging
import os
import time
from types import SimpleNamespace
from typing import Any, Dict, List

logger = logging.getLogger(__name__)


def _codex_thread_map_path(agent: Any) -> str | None:
    """Return the path to the codex thread map file for this agent.

    The map is a JSON dict {hermes_session_id → codex_thread_id} stored inside
    the per-employee CODEX_HOME so it stays with the codex install and moves
    with it when the user migrates the employee.
    """
    codex_home = getattr(agent, "codex_home", None)
    if not codex_home:
        return None
    try:
        os.makedirs(codex_home, exist_ok=True)
    except Exception:
        return None
    return os.path.join(codex_home, "hermes_thread_map.json")


def _load_codex_thread_id_for_session(agent: Any) -> str | None:
    """Read the previously-committed codex thread id for this hermes session.

    Returns None on first-turn or when the map is missing/corrupt — caller
    then falls back to thread/start. The lookup key is the AIAgent's
    session_id (hermes conversation id), so different conversations for the
    same employee resume independent codex threads (matching the mental model
    of one codex thread per conversation).
    """
    path = _codex_thread_map_path(agent)
    if not path or not os.path.isfile(path):
        return None
    session_id = str(getattr(agent, "session_id", "") or "").strip()
    if not session_id:
        return None
    try:
        with open(path, "r", encoding="utf-8") as fp:
            data = json.load(fp) or {}
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    value = data.get(session_id)
    return str(value).strip() if value else None


def _save_codex_thread_id_for_session(agent: Any, thread_id: str) -> None:
    """Persist the codex thread id so the next turn can thread/resume it."""
    if not thread_id:
        return
    path = _codex_thread_map_path(agent)
    if not path:
        return
    session_id = str(getattr(agent, "session_id", "") or "").strip()
    if not session_id:
        return
    try:
        data: Dict[str, Any] = {}
        if os.path.isfile(path):
            try:
                with open(path, "r", encoding="utf-8") as fp:
                    loaded = json.load(fp)
                if isinstance(loaded, dict):
                    data = loaded
            except Exception:
                data = {}
        if data.get(session_id) == thread_id:
            return
        data[session_id] = thread_id
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as fp:
            json.dump(data, fp, ensure_ascii=False)
        os.replace(tmp, path)
    except Exception:
        logger.debug("codex thread-id persist failed", exc_info=True)


def _codex_tool_summary(item: Dict[str, Any]) -> str:
    """Best-effort short label for a codex tool item shown on the UI card."""
    item_type = str(item.get("type") or "")
    if item_type == "commandExecution":
        cmd = item.get("command")
        if isinstance(cmd, list) and cmd:
            return " ".join(str(x) for x in cmd)[:160]
        if isinstance(cmd, str):
            return cmd[:160]
        return "shell"
    if item_type == "fileChange":
        changes = item.get("changes") or []
        if isinstance(changes, list) and changes:
            paths = []
            for change in changes[:4]:
                if isinstance(change, dict):
                    p = change.get("path") or change.get("filename")
                    if p:
                        paths.append(str(p))
            if paths:
                return "patch " + ", ".join(paths)
        return "patch"
    if item_type == "mcpToolCall":
        return f"mcp:{item.get('name') or item.get('toolName') or ''}"
    if item_type == "dynamicToolCall":
        return f"tool:{item.get('name') or ''}"
    return item_type or "tool"


def _codex_tool_name(item: Dict[str, Any]) -> str:
    item_type = str(item.get("type") or "")
    if item_type == "commandExecution":
        return "shell"
    if item_type == "fileChange":
        return "apply_patch"
    if item_type == "mcpToolCall":
        return f"mcp:{item.get('name') or item.get('toolName') or 'mcp'}"
    if item_type == "dynamicToolCall":
        return str(item.get("name") or "tool")
    return item_type or "tool"


def _forward_codex_tool_event(agent: Any, note: Dict[str, Any]) -> None:
    """Bridge codex tool item events into hermes tool callbacks so the UI card strip renders."""
    method = str(note.get("method", "") or "")
    if method not in ("item/started", "item/completed"):
        return
    params = note.get("params") or {}
    item = params.get("item") or {}
    item_type = str(item.get("type") or "")
    if item_type not in ("commandExecution", "fileChange", "mcpToolCall", "dynamicToolCall"):
        return
    item_id = str(item.get("id") or "")
    if not item_id:
        return
    tool_name = _codex_tool_name(item)
    if method == "item/started":
        cb = getattr(agent, "tool_start_callback", None)
        if callable(cb):
            try:
                cb(item_id, tool_name, {"summary": _codex_tool_summary(item)})
            except Exception:
                logger.debug("codex tool_start forward raised", exc_info=True)
        return
    if method == "item/completed":
        cb = getattr(agent, "tool_complete_callback", None)
        if callable(cb):
            output = ""
            if item_type == "commandExecution":
                output = str(item.get("aggregatedOutput") or "")[:2000]
            elif item_type == "fileChange":
                output = _codex_tool_summary(item)
            elif item_type in ("mcpToolCall", "dynamicToolCall"):
                r = item.get("result") or item.get("output") or ""
                output = str(r)[:2000] if not isinstance(r, str) else r[:2000]
            try:
                cb(item_id, tool_name, {"summary": _codex_tool_summary(item)}, output)
            except Exception:
                logger.debug("codex tool_complete forward raised", exc_info=True)


def _coerce_usage_int(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return max(value, 0)
    if isinstance(value, float):
        return max(int(value), 0)
    if isinstance(value, str):
        try:
            return max(int(value), 0)
        except ValueError:
            return 0
    return 0


def _record_codex_app_server_usage(agent, turn) -> dict[str, Any]:
    """Translate Codex app-server token usage into Hermes accounting.

    Codex app-server reports usage via thread/tokenUsage/updated as:
    inputTokens, cachedInputTokens, outputTokens, reasoningOutputTokens,
    totalTokens.

    Hermes' canonical prompt bucket includes uncached input + cached input.
    The Codex app-server protocol does not currently expose cache-write tokens,
    so that bucket remains zero on this runtime.

    Even when Codex omits usage for a turn, Hermes should still count that turn
    as one API call for session/status accounting.
    """
    agent.session_api_calls += 1

    usage = getattr(turn, "token_usage_last", None)
    if not isinstance(usage, dict) or not usage:
        if agent._session_db and agent.session_id:
            try:
                if not agent._session_db_created:
                    agent._ensure_db_session()
                agent._session_db.update_token_counts(
                    agent.session_id,
                    model=agent.model,
                    api_call_count=1,
                )
            except Exception as exc:
                logger.debug(
                    "Codex app-server api-call persistence failed (session=%s): %s",
                    agent.session_id, exc,
                )
        return {}

    from agent.usage_pricing import CanonicalUsage, estimate_usage_cost

    input_tokens = _coerce_usage_int(usage.get("inputTokens"))
    cache_read_tokens = _coerce_usage_int(usage.get("cachedInputTokens"))
    output_tokens = _coerce_usage_int(usage.get("outputTokens"))
    reasoning_tokens = _coerce_usage_int(usage.get("reasoningOutputTokens"))
    reported_total = _coerce_usage_int(usage.get("totalTokens"))

    canonical_usage = CanonicalUsage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cache_read_tokens,
        cache_write_tokens=0,
        reasoning_tokens=reasoning_tokens,
        raw_usage=usage,
    )
    prompt_tokens = canonical_usage.prompt_tokens
    completion_tokens = canonical_usage.output_tokens
    total_tokens = reported_total or canonical_usage.total_tokens
    usage_dict = {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens,
        "input_tokens": canonical_usage.input_tokens,
        "output_tokens": canonical_usage.output_tokens,
        "cache_read_tokens": canonical_usage.cache_read_tokens,
        "cache_write_tokens": canonical_usage.cache_write_tokens,
        "reasoning_tokens": canonical_usage.reasoning_tokens,
    }

    compressor = getattr(agent, "context_compressor", None)
    if compressor is not None:
        try:
            compressor.update_from_response(usage_dict)
            context_window = getattr(turn, "model_context_window", None)
            if isinstance(context_window, int) and context_window > 0:
                compressor.context_length = context_window
        except Exception:
            logger.debug("codex app-server usage update failed", exc_info=True)

    agent.session_prompt_tokens += prompt_tokens
    agent.session_completion_tokens += completion_tokens
    agent.session_total_tokens += total_tokens
    agent.session_input_tokens += canonical_usage.input_tokens
    agent.session_output_tokens += canonical_usage.output_tokens
    agent.session_cache_read_tokens += canonical_usage.cache_read_tokens
    agent.session_cache_write_tokens += canonical_usage.cache_write_tokens
    agent.session_reasoning_tokens += canonical_usage.reasoning_tokens

    cost_result = estimate_usage_cost(
        agent.model,
        canonical_usage,
        provider=agent.provider,
        base_url=agent.base_url,
        api_key=getattr(agent, "api_key", ""),
    )
    if cost_result.amount_usd is not None:
        agent.session_estimated_cost_usd += float(cost_result.amount_usd)
    agent.session_cost_status = cost_result.status
    agent.session_cost_source = cost_result.source

    if agent._session_db and agent.session_id:
        try:
            if not agent._session_db_created:
                agent._ensure_db_session()
            agent._session_db.update_token_counts(
                agent.session_id,
                input_tokens=canonical_usage.input_tokens,
                output_tokens=canonical_usage.output_tokens,
                cache_read_tokens=canonical_usage.cache_read_tokens,
                cache_write_tokens=canonical_usage.cache_write_tokens,
                reasoning_tokens=canonical_usage.reasoning_tokens,
                estimated_cost_usd=float(cost_result.amount_usd)
                if cost_result.amount_usd is not None else None,
                cost_status=cost_result.status,
                cost_source=cost_result.source,
                billing_provider=agent.provider,
                billing_base_url=agent.base_url,
                billing_mode="subscription_included"
                if cost_result.status == "included" else None,
                model=agent.model,
                api_call_count=1,
            )
        except Exception as exc:
            logger.debug(
                "Codex app-server token persistence failed (session=%s, tokens=%d): %s",
                agent.session_id, total_tokens, exc,
            )

    return {
        **usage_dict,
        "last_prompt_tokens": prompt_tokens,
        "estimated_cost_usd": float(cost_result.amount_usd)
        if cost_result.amount_usd is not None else None,
        "cost_status": cost_result.status,
        "cost_source": cost_result.source,
    }


def run_codex_app_server_turn(
    agent,
    *,
    user_message: str,
    original_user_message: Any,
    messages: List[Dict[str, Any]],
    effective_task_id: str,
    should_review_memory: bool = False,
) -> Dict[str, Any]:
    """Codex app-server runtime path. Hands the entire turn to a `codex
    app-server` subprocess and projects its events back into Hermes'
    messages list so memory/skill review keep working.

    Called from run_conversation() when agent.api_mode == "codex_app_server".
    Returns the same dict shape as the chat_completions path.
    """
    import time as _perf_time
    _perf_turn_begin = _perf_time.monotonic()
    import logging as _perf_log
    _perf_log.getLogger().warning(
        "[codex-perf][turn] BEGIN sid=%s codex_session_exists=%s stream_cb=%s",
        getattr(agent, "session_id", "?"),
        hasattr(agent, "_codex_session") and agent._codex_session is not None,
        callable(getattr(agent, "_stream_callback", None)),
    )
    _perf_log.getLogger().warning(
        "[codex-perf][turn] worker_env proxy: HTTP_PROXY=%r HTTPS_PROXY=%r NO_PROXY=%r",
        os.environ.get("HTTP_PROXY") or os.environ.get("http_proxy"),
        os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy"),
        os.environ.get("NO_PROXY") or os.environ.get("no_proxy"),
    )
    from agent.transports.codex_app_server_session import CodexAppServerSession

    # Lazy session: one CodexAppServerSession per AIAgent instance.
    # Spawned on first turn, reused across turns, closed at AIAgent
    # shutdown (see _cleanup hook).
    if not hasattr(agent, "_codex_session") or agent._codex_session is None:
        from agent.runtime_cwd import resolve_agent_cwd

        cwd = getattr(agent, "session_cwd", None) or str(resolve_agent_cwd())
        # Approval callback: defer to Hermes' standard prompt flow if a
        # CLI thread has installed one. Gateway / cron contexts get the
        # codex-side fail-closed default.
        try:
            from tools.terminal_tool import _get_approval_callback
            approval_callback = _get_approval_callback()
        except Exception:
            approval_callback = None
        prior_thread_id = _load_codex_thread_id_for_session(agent)

        # Stream forwarder: pipe codex's agentMessage/delta events into the
        # standard hermes stream callback so the UI sees text arrive
        # token-by-token instead of waiting for the whole turn to land.
        # Without this the projector only emits messages on item/completed
        # and the run reads as "运行中" until codex finishes.
        #
        # ``agent._stream_callback`` is what tui_gateway.prompt wires the
        # per-turn ``_stream`` closure onto via run_conversation(stream_callback=...);
        # it's set *before* our branch runs (conversation_loop line ~611)
        # so we can capture the live reference at session-construct time.

        def _stream_cb() -> Any:
            cb = getattr(agent, "_stream_callback", None)
            if callable(cb):
                return cb
            cb = getattr(agent, "stream_delta_callback", None)
            return cb if callable(cb) else None

        # Turn-local counter: per-turn number of agentMessage items we've
        # forwarded so far. Codex resets its turn state via turn/started so
        # we key on that. Used to inject a "\n\n" separator between multi-
        # part answers so the delta stream and projector's final_text (which
        # joins segments with the same separator) stay in sync — otherwise
        # the reconciliation pass sees a length mismatch and re-emits the
        # full text as a duplicate delta.
        _msg_state: Dict[str, int] = {"agent_message_started": 0}

        def _forward_codex_stream(note: Dict[str, Any]) -> None:
            method = str(note.get("method", "") or "")
            params = note.get("params") or {}

            if method == "turn/started":
                _msg_state["agent_message_started"] = 0

            # 1) Per-token streaming: pipe agentMessage delta text into hermes.
            if method == "item/agentMessage/delta":
                delta = params.get("delta")
                if not isinstance(delta, str) or not delta:
                    return
                cb = _stream_cb()
                if cb is None:
                    return
                try:
                    cb(delta)
                except Exception:
                    logger.debug("codex stream forwarder raised", exc_info=True)
                return

            # 2) Separator between multi-agentMessage turns. Fires on the
            #    second (and later) agentMessage/started so it never runs on
            #    single-segment turns (which was the "duplicate reply" bug).
            if method == "item/started":
                item = params.get("item") or {}
                if str(item.get("type") or "") == "agentMessage":
                    _msg_state["agent_message_started"] += 1
                    if _msg_state["agent_message_started"] > 1:
                        cb = _stream_cb()
                        if cb is not None:
                            try:
                                cb("\n\n")
                            except Exception:
                                logger.debug(
                                    "codex segment-separator forward raised",
                                    exc_info=True,
                                )

            # NOTE: earlier revisions called stream_callback(None) here to
            # close the UI segment on every item/completed(agentMessage).
            # That broke single-segment turns: hermes's
            # ``final-response-reconciliation`` sees delta_normalizer reset,
            # believes stream produced nothing, and re-emits the full text as
            # a fresh delta — the frontend then renders the reply twice AND
            # the turn state machine reads as still-running. Segment merging
            # for multi-part turns is now handled projector-side by
            # concatenating final_text across items so the reconciliation
            # invariant (raw_text prefix/equals stream_text) holds naturally.

            # Surface codex-side tools (shell commands, file patches, mcp
            #    tool calls) as hermes tool events so the UI's tool card
            #    strip populates the same way it does for chat_completions
            #    tool calls. Without this the frontend shows "no tools ran"
            #    even though codex just executed several shell commands and
            #    wrote files.
            _forward_codex_tool_event(agent, note)

        agent._codex_stream_forward = _forward_codex_stream

        agent._codex_session = CodexAppServerSession(
            cwd=cwd,
            codex_home=getattr(agent, "codex_home", None),
            extra_env=getattr(agent, "codex_extra_env", None),
            approval_callback=approval_callback,
            prior_thread_id=prior_thread_id,
            on_event=_forward_codex_stream,
        )

    # NOTE: the user message is ALREADY appended to messages by the
    # standard run_conversation() flow (line ~11823) before the early
    # return reaches us. Do NOT append again — that would duplicate.

    _perf_before_run = _perf_time.monotonic()
    _perf_log.getLogger().warning(
        "[codex-perf][turn] pre-session-setup dt=%.3fs, calling run_turn now",
        _perf_before_run - _perf_turn_begin,
    )
    try:
        turn = agent._codex_session.run_turn(user_input=user_message)
        _perf_log.getLogger().warning(
            "[codex-perf][turn] run_turn RETURNED dt=%.3fs (from run_turn start)",
            _perf_time.monotonic() - _perf_before_run,
        )
    except Exception as exc:
        logger.exception("codex app-server turn failed")
        # Crash → unconditionally drop the session so the next turn
        # respawns from scratch instead of reusing a dead client.
        try:
            agent._codex_session.close()
        except Exception:
            pass
        agent._codex_session = None
        return {
            "final_response": (
                f"Codex app-server turn failed: {exc}. "
                f"Fall back to default runtime with `/codex-runtime auto`."
            ),
            "messages": messages,
            "api_calls": 0,
            "completed": False,
            "partial": True,
            "error": str(exc),
        }

    # If the turn signalled the underlying client is wedged (deadline
    # blown, post-tool watchdog tripped, OAuth refresh died, subprocess
    # exited), retire the session so the next turn respawns codex
    # rather than riding the broken process. Mirrors openclaw beta.8's
    # "retire timed-out app-server clients" fix.
    if getattr(turn, "should_retire", False):
        logger.warning(
            "codex app-server session retired (turn error: %s)",
            turn.error,
        )
        try:
            agent._codex_session.close()
        except Exception:
            pass
        agent._codex_session = None

    # Splice projected messages into the conversation. The projector emits
    # standard {role, content, tool_calls, tool_call_id} entries, which
    # is exactly what curator.py / sessions DB expect.
    if turn.projected_messages:
        messages.extend(turn.projected_messages)

    # Persist the updated transcript to the sessions/messages tables. The
    # chat_completions loop calls _persist_session on every iteration; we
    # bypass that loop entirely, so without this write the assistant reply
    # never reaches the messages table. The UI history reader falls back
    # to messages after message.delta rows get pruned at terminal, so a
    # missing write here surfaces as an empty assistant turn on reload.
    #
    # Pass the messages buffer as-is: it's a ``TurnMessageBuffer`` whose
    # ``_hermes_persist_from_index`` marks where the current turn starts
    # inside the history+turn concatenation. ``_flush_messages_to_session_db``
    # reads that boundary to append ONLY this turn's new user/assistant rows.
    # A ``list(messages)`` copy would drop the attribute — flush would then
    # fall back to "no explicit boundary", start_idx=0, and re-insert every
    # historical row on every codex turn (surface: sidebar shows N copies
    # of every user message stacked before the current assistant reply).
    try:
        agent._persist_session(messages)
    except Exception:
        logger.debug("codex app-server _persist_session raised", exc_info=True)

    # Persist the codex thread id so the next turn can thread/resume it and
    # keep the conversation memory alive on the codex side (otherwise codex
    # sees a fresh thread every turn and has no idea what the user said
    # before). Skipped when the session was retired above (turn errored) —
    # a broken thread should not be resumed on the next turn.
    if turn.thread_id and getattr(agent, "_codex_session", None) is not None:
        try:
            _save_codex_thread_id_for_session(agent, str(turn.thread_id))
        except Exception:
            logger.debug("codex app-server thread-id persist raised", exc_info=True)

    # Counter ticks for the agent-improvement loop.
    # _turns_since_memory and _user_turn_count are ALREADY incremented
    # in the run_conversation() pre-loop block (lines ~11793-11817) so we
    # do NOT touch them here — that would double-count.
    # Only _iters_since_skill needs explicit increment, since the
    # chat_completions loop bumps it per tool iteration (line ~12110)
    # and that loop is bypassed on this path.
    agent._iters_since_skill = (
        getattr(agent, "_iters_since_skill", 0) + turn.tool_iterations
    )
    usage_result = _record_codex_app_server_usage(agent, turn)
    api_calls = 1

    # Now check the skill nudge AFTER iters were incremented — same
    # pattern the chat_completions path uses (line ~15432).
    should_review_skills = False
    if (
        agent._skill_nudge_interval > 0
        and agent._iters_since_skill >= agent._skill_nudge_interval
        and "skill_manage" in agent.valid_tool_names
    ):
        should_review_skills = True
        agent._iters_since_skill = 0

    # External memory provider sync (mirrors line ~15439). Skipped on
    # interrupt/error to avoid feeding partial transcripts to memory.
    if not turn.interrupted and turn.error is None:
        try:
            agent._sync_external_memory_for_turn(
                original_user_message=original_user_message,
                final_response=turn.final_text,
                interrupted=False,
            )
        except Exception:
            logger.debug("external memory sync raised", exc_info=True)

    # Background review fork — same cadence + signature as the default
    # path (line ~15449). Only fires when a trigger actually tripped AND
    # we have a real final response.
    if (
        turn.final_text
        and not turn.interrupted
        and (should_review_memory or should_review_skills)
    ):
        try:
            agent._spawn_background_review(
                messages_snapshot=list(messages),
                review_memory=should_review_memory,
                review_skills=should_review_skills,
            )
        except Exception:
            logger.debug("background review spawn raised", exc_info=True)

    import logging as _dbg_log_mod
    _dbg_log_mod.getLogger().warning(
        "[codex-flow][run_conversation] RETURN final_text_len=%s final_text_preview=%r projected_msgs=%s completed=%s error=%r",
        len(turn.final_text or ""),
        (turn.final_text or "")[:120],
        len(turn.projected_messages or []),
        not turn.interrupted and turn.error is None,
        turn.error,
    )
    return {
        "final_response": turn.final_text,
        "messages": messages,
        "api_calls": api_calls,
        "completed": not turn.interrupted and turn.error is None,
        "partial": turn.interrupted or turn.error is not None,
        "error": turn.error,
        "codex_thread_id": turn.thread_id,
        "codex_turn_id": turn.turn_id,
        **usage_result,
    }




def _responses_null_output_iterable_error(exc: BaseException) -> bool:
    """True when the OpenAI SDK trips over terminal response.output=None."""
    text = str(exc)
    return isinstance(exc, TypeError) and "NoneType" in text and "not iterable" in text


def _codex_backfilled_response(output_items: list, text_parts: list, *, has_tool_calls: bool, model: str = None):
    """Build a minimal Responses-like object from events already streamed."""
    if output_items:
        return SimpleNamespace(
            output=list(output_items),
            usage=None,
            status="completed",
            model=model,
        )
    if text_parts and not has_tool_calls:
        assembled = "".join(text_parts)
        return SimpleNamespace(
            output=[SimpleNamespace(
                type="message",
                role="assistant",
                status="completed",
                content=[SimpleNamespace(type="output_text", text=assembled)],
            )],
            usage=None,
            status="completed",
            model=model,
        )
    return None


def run_codex_stream(agent, api_kwargs: dict, client: Any = None, on_first_delta: callable = None):
    """Execute one streaming Responses API request and return the final response."""
    import httpx as _httpx

    active_client = client or agent._ensure_primary_openai_client(reason="codex_stream_direct")
    max_stream_retries = 1
    has_tool_calls = False
    first_delta_fired = False
    # Accumulate streamed text so we can recover if get_final_response()
    # returns empty output (e.g. chatgpt.com backend-api sends
    # response.incomplete instead of response.completed).
    agent._codex_streamed_text_parts: list = []
    for attempt in range(max_stream_retries + 1):
        if agent._interrupt_requested:
            raise InterruptedError("Agent interrupted before Codex stream retry")
        collected_output_items: list = []
        try:
            with active_client.responses.stream(**api_kwargs) as stream:
                for event in stream:
                    # Mark stream activity for the TTFB watchdog in
                    # interruptible_api_call. The Codex backend can accept the
                    # connection but never emit a single event; this timestamp
                    # staying None tells the watchdog no bytes are flowing.
                    agent._codex_stream_last_event_ts = time.time()
                    agent._touch_activity("receiving stream response")
                    if agent._interrupt_requested:
                        break
                    event_type = getattr(event, "type", "")
                    # Fire callbacks on text content deltas (suppress during tool calls)
                    if "output_text.delta" in event_type or event_type == "response.output_text.delta":
                        delta_text = getattr(event, "delta", "")
                        if delta_text:
                            agent._codex_streamed_text_parts.append(delta_text)
                        if delta_text and not has_tool_calls:
                            if not first_delta_fired:
                                first_delta_fired = True
                                if on_first_delta:
                                    try:
                                        on_first_delta()
                                    except Exception:
                                        pass
                            agent._fire_stream_delta(delta_text)
                    # Track tool calls to suppress text streaming
                    elif "function_call" in event_type:
                        has_tool_calls = True
                    # Fire reasoning callbacks
                    elif "reasoning" in event_type and "delta" in event_type:
                        reasoning_text = getattr(event, "delta", "")
                        if reasoning_text:
                            agent._fire_reasoning_delta(reasoning_text)
                    # Collect completed output items — some backends
                    # (chatgpt.com/backend-api/codex) stream valid items
                    # via response.output_item.done but the SDK's
                    # get_final_response() returns an empty output list.
                    elif event_type == "response.output_item.done":
                        done_item = getattr(event, "item", None)
                        if done_item is not None:
                            collected_output_items.append(done_item)
                    # Log non-completed terminal events for diagnostics
                    elif event_type in {"response.incomplete", "response.failed"}:
                        resp_obj = getattr(event, "response", None)
                        status = getattr(resp_obj, "status", None) if resp_obj else None
                        incomplete_details = getattr(resp_obj, "incomplete_details", None) if resp_obj else None
                        logger.warning(
                            "Codex Responses stream received terminal event %s "
                            "(status=%s, incomplete_details=%s, streamed_chars=%d). %s",
                            event_type, status, incomplete_details,
                            sum(len(p) for p in agent._codex_streamed_text_parts),
                            agent._client_log_context(),
                        )
                final_response = stream.get_final_response()
                # PATCH: ChatGPT Codex backend streams valid output items
                # but get_final_response() can return an empty output list.
                # Backfill from collected items or synthesize from deltas.
                _out = getattr(final_response, "output", None)
                if _out is None or (isinstance(_out, list) and not _out):
                    recovered = _codex_backfilled_response(
                        collected_output_items,
                        agent._codex_streamed_text_parts,
                        has_tool_calls=has_tool_calls,
                        model=api_kwargs.get("model"),
                    )
                    if recovered is not None:
                        final_response.output = recovered.output
                        logger.debug(
                            "Codex stream: backfilled missing output from stream events "
                            "(items=%d, text_parts=%d)",
                            len(collected_output_items),
                            len(agent._codex_streamed_text_parts),
                        )
                return final_response
        except (_httpx.RemoteProtocolError, _httpx.ReadTimeout, _httpx.ConnectError, ConnectionError) as exc:
            if attempt < max_stream_retries:
                logger.debug(
                    "Codex Responses stream transport failed (attempt %s/%s); retrying. %s error=%s",
                    attempt + 1,
                    max_stream_retries + 1,
                    agent._client_log_context(),
                    exc,
                )
                continue
            logger.debug(
                "Codex Responses stream transport failed; falling back to create(stream=True). %s error=%s",
                agent._client_log_context(),
                exc,
            )
            return agent._run_codex_create_stream_fallback(api_kwargs, client=active_client)
        except TypeError as exc:
            if _responses_null_output_iterable_error(exc):
                recovered = _codex_backfilled_response(
                    collected_output_items,
                    agent._codex_streamed_text_parts,
                    has_tool_calls=has_tool_calls,
                    model=api_kwargs.get("model"),
                )
                if recovered is not None:
                    logger.debug(
                        "Codex Responses stream parser hit response.output=None; "
                        "recovered from streamed events (items=%d, text_parts=%d). %s",
                        len(collected_output_items),
                        len(agent._codex_streamed_text_parts),
                        agent._client_log_context(),
                    )
                    return recovered
                logger.debug(
                    "Codex Responses stream parser hit response.output=None without "
                    "recoverable events; falling back to create(stream=True). %s",
                    agent._client_log_context(),
                )
                return agent._run_codex_create_stream_fallback(api_kwargs, client=active_client)
            raise
        except RuntimeError as exc:
            err_text = str(exc)
            missing_completed = "response.completed" in err_text
            # The OpenAI SDK's Responses streaming state machine raises
            # ``RuntimeError("Expected to have received `response.created`
            # before `<event-type>`")`` when the first SSE event from the
            # server is anything other than ``response.created`` — and it
            # discards the event's payload before we can read it.  Three
            # real-world backends emit a different first frame:
            #
            #   * xAI on grok-4.x OAuth — sends ``error`` (issues
            #     reported around the May 2026 SuperGrok rollout when
            #     multi-turn conversations replay encrypted reasoning
            #     content the OAuth tier rejects)
            #   * codex-lb relays — send ``codex.rate_limits`` (#14634)
            #   * custom Responses relays — send ``response.in_progress``
            #     (#8133)
            #
            # In all three cases the underlying byte stream is still
            # readable: a non-stream ``responses.create(stream=True)``
            # fallback succeeds and surfaces the real provider error as
            # a normal exception with body+status_code attached, which
            # ``_summarize_api_error`` can then translate into a useful
            # user-facing line.  Treat ``response.created`` prelude
            # errors the same way we already treat ``response.completed``
            # postlude errors.
            prelude_error = (
                "Expected to have received `response.created`" in err_text
                or "Expected to have received \"response.created\"" in err_text
            )
            if (missing_completed or prelude_error) and attempt < max_stream_retries:
                logger.debug(
                    "Responses stream %s (attempt %s/%s); retrying. %s",
                    "prelude rejected" if prelude_error else "closed before completion",
                    attempt + 1,
                    max_stream_retries + 1,
                    agent._client_log_context(),
                )
                continue
            if missing_completed or prelude_error:
                logger.debug(
                    "Responses stream %s; falling back to create(stream=True). %s err=%s",
                    "rejected before response.created" if prelude_error else "did not emit response.completed",
                    agent._client_log_context(),
                    err_text,
                )
                return agent._run_codex_create_stream_fallback(api_kwargs, client=active_client)
            raise



def run_codex_create_stream_fallback(agent, api_kwargs: dict, client: Any = None):
    """Fallback path for stream completion edge cases on Codex-style Responses backends."""
    active_client = client or agent._ensure_primary_openai_client(reason="codex_create_stream_fallback")
    fallback_kwargs = dict(api_kwargs)
    fallback_kwargs["stream"] = True
    fallback_kwargs = agent._get_transport().preflight_kwargs(fallback_kwargs, allow_stream=True)
    stream_or_response = active_client.responses.create(**fallback_kwargs)

    # Compatibility shim for mocks or providers that still return a concrete response.
    if hasattr(stream_or_response, "output"):
        return stream_or_response
    if not hasattr(stream_or_response, "__iter__"):
        return stream_or_response

    terminal_response = None
    collected_output_items: list = []
    collected_text_deltas: list = []
    has_tool_calls = False
    try:
        for event in stream_or_response:
            agent._touch_activity("receiving stream response")
            event_type = getattr(event, "type", None)
            if not event_type and isinstance(event, dict):
                event_type = event.get("type")

            # ``error`` SSE frames carry the provider's real failure
            # reason (subscription / quota / model-not-available /
            # rejected-reasoning-replay) but never appear in the
            # ``{completed, incomplete, failed}`` terminal set, so the
            # raw loop below would silently consume them and end with
            # "did not emit a terminal response".  xAI in particular
            # emits ``type=error`` as the FIRST frame for OAuth
            # accounts whose Grok subscription is missing/exhausted —
            # the SDK's stream helper raises ``RuntimeError(Expected
            # to have received response.created before error)`` which
            # the caller catches and routes here, expecting this
            # fallback to surface the message.  Synthesize an
            # APIError-shaped exception so ``_summarize_api_error``
            # and the credential-pool entitlement detector see the
            # real text instead of a generic RuntimeError.
            if event_type == "error":
                err_message = getattr(event, "message", None)
                if not err_message and isinstance(event, dict):
                    err_message = event.get("message")
                err_code = getattr(event, "code", None)
                if not err_code and isinstance(event, dict):
                    err_code = event.get("code")
                err_param = getattr(event, "param", None)
                if not err_param and isinstance(event, dict):
                    err_param = event.get("param")
                err_message = (err_message or "stream emitted error event").strip()
                from run_agent import _StreamErrorEvent
                raise _StreamErrorEvent(err_message, code=err_code, param=err_param)

            # Collect output items and text deltas for backfill
            if event_type == "response.output_item.done":
                done_item = getattr(event, "item", None)
                if done_item is None and isinstance(event, dict):
                    done_item = event.get("item")
                if done_item is not None:
                    collected_output_items.append(done_item)
            elif event_type in {"response.output_text.delta",}:
                delta = getattr(event, "delta", "")
                if not delta and isinstance(event, dict):
                    delta = event.get("delta", "")
                if delta:
                    collected_text_deltas.append(delta)
            elif event_type and "function_call" in event_type:
                has_tool_calls = True

            if event_type not in {"response.completed", "response.incomplete", "response.failed"}:
                continue

            terminal_response = getattr(event, "response", None)
            if terminal_response is None and isinstance(event, dict):
                terminal_response = event.get("response")
            if terminal_response is not None:
                # Backfill empty output from collected stream events
                _out = getattr(terminal_response, "output", None)
                if _out is None or (isinstance(_out, list) and not _out):
                    recovered = _codex_backfilled_response(
                        collected_output_items,
                        collected_text_deltas,
                        has_tool_calls=has_tool_calls,
                        model=fallback_kwargs.get("model"),
                    )
                    if recovered is not None:
                        terminal_response.output = recovered.output
                        logger.debug(
                            "Codex fallback stream: backfilled missing output "
                            "(items=%d, text_parts=%d)",
                            len(collected_output_items),
                            len(collected_text_deltas),
                        )
                return terminal_response
    finally:
        close_fn = getattr(stream_or_response, "close", None)
        if callable(close_fn):
            try:
                close_fn()
            except Exception:
                pass

    if terminal_response is not None:
        return terminal_response
    raise RuntimeError("Responses create(stream=True) fallback did not emit a terminal response.")



__all__ = [
    "run_codex_app_server_turn",
    "run_codex_stream",
    "run_codex_create_stream_fallback",
]
