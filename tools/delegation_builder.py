"""Construction of isolated delegated child agents."""

from __future__ import annotations

import logging
from typing import List, Optional

logger = logging.getLogger(__name__)

def build_child_agent(
    task_index: int,
    goal: str,
    context: Optional[str],
    toolsets: Optional[List[str]],
    model: Optional[str],
    max_iterations: int,
    task_count: int,
    parent_agent,
    # Credential overrides from delegation config (provider:model resolution)
    override_provider: Optional[str] = None,
    override_base_url: Optional[str] = None,
    override_api_key: Optional[str] = None,
    override_api_mode: Optional[str] = None,
    # ACP transport overrides — lets a non-ACP parent spawn ACP child agents
    override_acp_command: Optional[str] = None,
    override_acp_args: Optional[List[str]] = None,
    # Per-call role controlling whether the child can further delegate.
    # 'leaf' (default) cannot; 'orchestrator' retains the delegation
    # toolset subject to depth/kill-switch bounds applied below.
    role: str = "leaf",
    delegate_call_id: Optional[str] = None,
    agent_name: Optional[str] = None,
):
    """
    Build a child AIAgent on the main thread (thread-safe construction).
    Returns the constructed child agent without running it.

    When override_* params are set (from delegation config), the child uses
    those credentials instead of inheriting from the parent.  This enables
    routing subagents to a different provider:model pair (e.g. cheap/fast
    model on OpenRouter while the parent runs on Nous Portal).
    """
    from tools import delegate_tool as delegate_tool

    DEFAULT_TOOLSETS = delegate_tool.DEFAULT_TOOLSETS
    DELEGATE_BLOCKED_TOOLS = delegate_tool.DELEGATE_BLOCKED_TOOLS
    _build_child_output_delta_callback = (
        delegate_tool._build_child_output_delta_callback
    )
    _build_child_progress_callback = delegate_tool._build_child_progress_callback
    _build_child_reasoning_delta_callback = (
        delegate_tool._build_child_reasoning_delta_callback
    )
    _build_child_system_prompt = delegate_tool._build_child_system_prompt
    _clean_subagent_name = delegate_tool._clean_subagent_name
    _get_inherit_mcp_toolsets = delegate_tool._get_inherit_mcp_toolsets
    _get_max_spawn_depth = delegate_tool._get_max_spawn_depth
    _get_orchestrator_enabled = delegate_tool._get_orchestrator_enabled
    _humanize_subagent_name = delegate_tool._humanize_subagent_name
    _load_config = delegate_tool._load_config
    _prepare_child_dovie_attribution = delegate_tool._prepare_child_dovie_attribution
    _resolve_child_credential_pool = delegate_tool._resolve_child_credential_pool
    _resolve_child_tool_access = delegate_tool._resolve_child_tool_access
    _resolve_workspace_hint = delegate_tool._resolve_workspace_hint

    from run_agent import AIAgent
    import uuid as _uuid

    # ── Role resolution ─────────────────────────────────────────────────
    # Honor the caller's role only when BOTH the kill switch and the
    # child's depth allow it.  This is the single point where role
    # degrades to 'leaf' — keeps the rule predictable.  Callers pass
    # the normalised role (_normalize_role ran in delegate_task) so
    # we only deal with 'leaf' or 'orchestrator' here.
    child_depth = getattr(parent_agent, "_delegate_depth", 0) + 1
    max_spawn = _get_max_spawn_depth()
    orchestrator_ok = _get_orchestrator_enabled() and child_depth < max_spawn
    effective_role = role if (role == "orchestrator" and orchestrator_ok) else "leaf"

    # ── Subagent identity (stable across events, 0-indexed for TUI) ─────
    # subagent_id is generated here so the progress callback, the
    # spawn_requested event, and the _active_subagents registry all share
    # one key.  parent_id is non-None when THIS parent is itself a subagent
    # (nested orchestrator -> worker chain).
    subagent_id = f"sa-{task_index}-{_uuid.uuid4().hex[:8]}"
    parent_subagent_id = getattr(parent_agent, "_subagent_id", None)
    tui_depth = max(0, child_depth - 1)  # 0 = first-level child for the UI
    normalized_delegate_call_id = str(delegate_call_id or "").strip()
    display_agent_name = _clean_subagent_name(agent_name) or _humanize_subagent_name(
        goal,
        context,
        toolsets,
        task_index,
    )

    delegation_cfg = _load_config()

    blocked_tools = (
        frozenset(["delegate_task"])
        if getattr(parent_agent, "_delegate_inherits_parent_tools", False) is True
        else DELEGATE_BLOCKED_TOOLS
    )
    child_toolsets, child_tool_names = _resolve_child_tool_access(
        parent_agent,
        toolsets,
        role=effective_role,
        blocked_tools=blocked_tools,
        default_toolsets=DEFAULT_TOOLSETS,
        inherit_mcp_toolsets=_get_inherit_mcp_toolsets(),
    )
    if effective_role == "orchestrator" and "delegate_task" not in child_tool_names:
        # Role elevation may never widen beyond the parent's real tool surface.
        # If the parent cannot delegate, downgrade the child and make its
        # prompt/tool contract agree instead of granting a hidden capability.
        effective_role = "leaf"
        child_toolsets, child_tool_names = _resolve_child_tool_access(
            parent_agent,
            toolsets,
            role=effective_role,
            blocked_tools=blocked_tools,
            default_toolsets=DEFAULT_TOOLSETS,
            inherit_mcp_toolsets=_get_inherit_mcp_toolsets(),
        )

    workspace_hint = _resolve_workspace_hint(parent_agent)
    parent_ephemeral_prompt = getattr(parent_agent, "ephemeral_system_prompt", None)
    if not isinstance(parent_ephemeral_prompt, str):
        parent_ephemeral_prompt = None
    child_prompt = _build_child_system_prompt(
        goal,
        context,
        workspace_path=workspace_hint,
        parent_system_prompt=parent_ephemeral_prompt,
        role=effective_role,
        max_spawn_depth=max_spawn,
        child_depth=child_depth,
    )
    # Extract parent's API key so subagents inherit auth (e.g. Nous Portal).
    parent_api_key = getattr(parent_agent, "api_key", None)
    if (not parent_api_key) and hasattr(parent_agent, "_client_kwargs"):
        parent_api_key = parent_agent._client_kwargs.get("api_key")

    # Resolve the child's effective model early so it can ride on every event.
    effective_model_for_cb = model or getattr(parent_agent, "model", None)

    # Build progress callback to relay tool calls to parent display.
    # Identity kwargs thread the subagent_id through every emitted event so the
    # TUI can reconstruct the spawn tree and route per-branch controls.
    child_progress_cb = _build_child_progress_callback(
        task_index,
        goal,
        parent_agent,
        task_count,
        subagent_id=subagent_id,
        parent_id=parent_subagent_id,
        depth=tui_depth,
        model=effective_model_for_cb,
        toolsets=child_toolsets,
        role=effective_role,
        context=context,
        delegate_call_id=delegate_call_id,
        agent_name=display_agent_name,
    )
    child_output_delta_cb = _build_child_output_delta_callback(
        task_index,
        goal,
        parent_agent,
        task_count,
        subagent_id=subagent_id,
        parent_id=parent_subagent_id,
        depth=tui_depth,
        model=effective_model_for_cb,
        toolsets=child_toolsets,
        role=effective_role,
        context=context,
        delegate_call_id=delegate_call_id,
        agent_name=display_agent_name,
    )
    child_reasoning_delta_cb = _build_child_reasoning_delta_callback(
        task_index,
        goal,
        parent_agent,
        task_count,
        subagent_id=subagent_id,
        parent_id=parent_subagent_id,
        depth=tui_depth,
        model=effective_model_for_cb,
        toolsets=child_toolsets,
        role=effective_role,
        delegate_call_id=delegate_call_id,
    )

    # Each subagent gets its own iteration budget capped at max_iterations
    # (configurable via delegation.max_iterations, default 50).  This means
    # total iterations across parent + subagents can exceed the parent's
    # max_iterations.  The user controls the per-subagent cap in config.yaml.

    # Quiet-mode thinking_callback is a local activity spinner, not provider
    # reasoning. Routing it through child_progress_cb makes Dovie render
    # ordinary subagent runtime status as a model "thinking" block.
    child_thinking_cb = None

    # Resolve effective credentials: config override > parent inherit
    effective_model = model or parent_agent.model
    effective_provider = override_provider or getattr(parent_agent, "provider", None)
    effective_base_url = override_base_url or parent_agent.base_url
    effective_api_key = override_api_key or parent_api_key
    # Bug #20558 / PR #20563: api_mode must NOT be inherited when the child uses a
    # different provider than the parent — each provider has its own API surface
    # (e.g. MiniMax uses anthropic_messages, DeepSeek uses chat_completions).
    # Inheriting the parent's mode causes 404 errors when the child routes to the
    # wrong endpoint.  Derive the mode from the target provider when it differs.
    _parent_provider = getattr(parent_agent, "provider", None) or ""
    if override_api_mode is not None:
        effective_api_mode = override_api_mode
    elif effective_provider != _parent_provider:
        effective_api_mode = None  # force re-derivation from provider's defaults
    else:
        effective_api_mode = getattr(parent_agent, "api_mode", None)
    effective_acp_command = override_acp_command or getattr(
        parent_agent, "acp_command", None
    )
    effective_acp_args = list(
        override_acp_args
        if override_acp_args is not None
        else (getattr(parent_agent, "acp_args", []) or [])
    )

    # When override_provider is set (e.g. delegation.provider: minimax-cn),
    # the subagent must use direct API calls — not the parent's ACP transport.
    # Inheriting acp_command unconditionally causes run_agent.py to initialize
    # CopilotACPClient, bypassing override credentials entirely (issue #16816).
    if override_provider and not override_acp_command:
        effective_acp_command = None
        effective_acp_args = []

    if override_acp_command:
        # If explicitly forcing an ACP transport override, the provider MUST be copilot-acp
        # so run_agent.py initializes the CopilotACPClient.
        effective_provider = "copilot-acp"
        effective_api_mode = "chat_completions"

    # Resolve reasoning config: delegation override > parent inherit
    parent_reasoning = getattr(parent_agent, "reasoning_config", None)
    child_reasoning = parent_reasoning
    try:
        delegation_effort = str(delegation_cfg.get("reasoning_effort") or "").strip()
        if delegation_effort:
            from hermes_constants import parse_reasoning_effort

            parsed = parse_reasoning_effort(delegation_effort)
            if parsed is not None:
                child_reasoning = parsed
            else:
                logger.warning(
                    "Unknown delegation.reasoning_effort '%s', inheriting parent level",
                    delegation_effort,
                )
    except Exception as exc:
        logger.debug("Could not load delegation reasoning_effort: %s", exc)

    # Inherit the parent's fallback provider chain so subagents can recover
    # from rate-limits and credential exhaustion exactly like the top-level
    # agent does.  _fallback_chain is a list accepted by AIAgent's
    # fallback_model parameter (which handles both list and dict forms).
    parent_fallback = getattr(parent_agent, "_fallback_chain", None) or None

    # Inherit the parent's OpenRouter provider-preference filters by default
    # (so subagents routed to the same provider honour the same routing
    # constraints).  BUT: when `delegation.provider` is set the user is
    # explicitly asking the child to run on a different provider, and
    # parent-level OpenRouter filters (e.g. `only=["Anthropic"]`) would
    # silently force the child back onto the parent's provider. Clear the
    # filters in that case so the delegated provider is honoured.
    child_providers_allowed = getattr(parent_agent, "providers_allowed", None)
    child_providers_ignored = getattr(parent_agent, "providers_ignored", None)
    child_providers_order = getattr(parent_agent, "providers_order", None)
    child_provider_sort = getattr(parent_agent, "provider_sort", None)
    child_openrouter_min_coding_score = getattr(parent_agent, "openrouter_min_coding_score", None)
    if override_provider:
        child_providers_allowed = None
        child_providers_ignored = None
        child_providers_order = None
        child_provider_sort = None
        # Note: openrouter_min_coding_score is model-gated (only emitted on
        # openrouter/pareto-code), so we keep it inherited even when the
        # provider is overridden — it's a no-op on any other model.

    child_session_db = (
        None
        if getattr(parent_agent, "_delegate_child_transient_session", False) is True
        else getattr(parent_agent, "_session_db", None)
    )
    child_parent_session_id = None if child_session_db is None else getattr(parent_agent, "session_id", None)
    parent_skip_context_files = getattr(parent_agent, "skip_context_files", False)
    if not isinstance(parent_skip_context_files, bool):
        parent_skip_context_files = False

    child = AIAgent(
        base_url=effective_base_url,
        api_key=effective_api_key,
        model=effective_model,
        provider=effective_provider,
        api_mode=effective_api_mode,
        acp_command=effective_acp_command,
        acp_args=effective_acp_args,
        max_iterations=max_iterations,
        max_tokens=getattr(parent_agent, "max_tokens", None),
        reasoning_config=child_reasoning,
        prefill_messages=getattr(parent_agent, "prefill_messages", None),
        fallback_model=parent_fallback,
        enabled_toolsets=child_toolsets,
        enabled_tools=child_tool_names,
        quiet_mode=True,
        ephemeral_system_prompt=child_prompt,
        log_prefix=f"[subagent-{task_index}]",
        platform=parent_agent.platform,
        skip_context_files=parent_skip_context_files,
        skip_memory=True,
        clarify_callback=None,
        thinking_callback=child_thinking_cb,
        reasoning_callback=child_reasoning_delta_cb,
        session_db=child_session_db,
        parent_session_id=child_parent_session_id,
        session_kind="execution",
        conversation_kind="internal",
        providers_allowed=child_providers_allowed,
        providers_ignored=child_providers_ignored,
        providers_order=child_providers_order,
        provider_sort=child_provider_sort,
        openrouter_min_coding_score=child_openrouter_min_coding_score,
        tool_progress_callback=child_progress_cb,
        stream_delta_callback=child_output_delta_cb,
        iteration_budget=None,  # fresh budget per subagent
    )
    child._print_fn = getattr(parent_agent, "_print_fn", None)
    # Set delegation depth so children can't spawn grandchildren
    child._delegate_depth = child_depth
    # Stash the post-degrade role for introspection (leaf if the
    # kill switch or depth bounded the caller's requested role).
    child._delegate_role = effective_role
    # Stash subagent identity for nested-delegation event propagation and
    # for _run_single_child / interrupt_subagent to look up by id.
    child._subagent_id = subagent_id
    child._parent_subagent_id = parent_subagent_id
    child._subagent_goal = goal
    child._subagent_name = display_agent_name
    child._subagent_task_index = task_index
    child._subagent_task_count = task_count
    child._subagent_delegate_call_id = normalized_delegate_call_id
    child._subagent_toolsets = list(child_toolsets)
    child._subagent_tools = list(child_tool_names)
    child._subagent_tui_depth = tui_depth
    _prepare_child_dovie_attribution(child)
    if getattr(parent_agent, "_delegate_child_transient_session", False) is True:
        child._session_persistence_disabled = True
        child._session_db = None

    # Share a credential pool with the child when possible so subagents can
    # rotate credentials on rate limits instead of getting pinned to one key.
    child_pool = _resolve_child_credential_pool(
        effective_provider, parent_agent, effective_base_url
    )
    if child_pool is not None:
        child._credential_pool = child_pool

    # Register child for interrupt propagation
    if hasattr(parent_agent, "_active_children"):
        lock = getattr(parent_agent, "_active_children_lock", None)
        if lock:
            with lock:
                parent_agent._active_children.append(child)
        else:
            parent_agent._active_children.append(child)

    # Announce the spawn immediately — the child may sit in a queue
    # for seconds if max_concurrent_children is saturated, so the TUI
    # wants a node in the tree before run starts.
    if child_progress_cb:
        try:
            child_progress_cb("subagent.spawn_requested", preview=goal)
        except Exception as exc:
            logger.debug("spawn_requested relay failed: %s", exc)

    return child
