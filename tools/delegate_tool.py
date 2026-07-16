#!/usr/bin/env python3
"""
Delegate Tool -- Subagent Architecture

Spawns isolated child agents with bounded tools and terminal sessions. Supports
single or batched execution in explicit synchronous or asynchronous modes.

The parent's context only sees the delegation call and the summary result,
never the child's intermediate tool calls or reasoning.
"""

import enum
import json
import logging

logger = logging.getLogger(__name__)
import os
import threading
import time
from concurrent.futures import (
    ThreadPoolExecutor,
    TimeoutError as FuturesTimeoutError,
)
from typing import Any, Callable, Dict, List, Optional

from toolsets import TOOLSETS

from tools import file_state
from tools.subagent_identity import (
    SUBAGENT_NAME_DESCRIPTION as _SUBAGENT_NAME_DESCRIPTION,
    clean_subagent_name as _clean_subagent_name,
    humanize_subagent_name as _humanize_subagent_name,
    resolve_task_agent_name as _resolve_task_agent_name,
)
from tools.delegate_tool_access import (
    resolve_child_tool_access as _resolve_child_tool_access,
    strip_blocked_toolsets as _strip_blocked_toolsets,
)
from tools.delegation_credentials import (
    _resolve_child_credential_pool,
    _resolve_delegation_credentials,
)
from tools.delegation_builder import build_child_agent as _build_child_agent
from tools.delegation_dovie import (
    _prepare_child_dovie_attribution,
    _run_child_conversation_with_dovie_attribution,
)
from tools.delegation_runner import (
    dump_subagent_timeout_diagnostic as _dump_subagent_timeout_diagnostic,
    run_single_child as _run_single_child,
)
from tools.delegation_result_utils import (
    _extract_output_tail,
    _looks_like_error_output,
    _stringify_tool_content,
)
from tools.delegation_summary import (
    DEFAULT_MAX_SUMMARY_CHARS as _DEFAULT_MAX_SUMMARY_CHARS,
    MIN_SUMMARY_CHARS as _MIN_SUMMARY_CHARS,
    apply_summary_budget as _apply_delegation_summary_budget,
    parent_summary_char_budget as _parent_delegation_summary_char_budget,
)
from tools.terminal_tool import set_approval_callback as _set_subagent_approval_cb
from utils import is_truthy_value


# Tools that children must never have access to
DELEGATE_BLOCKED_TOOLS = frozenset(
    [
        "delegate_task",  # no recursive delegation
        "clarify",  # no user interaction
        "memory",  # no writes to shared MEMORY.md
        "send_message",  # no cross-platform side effects
        "execute_code",  # children should reason step-by-step, not write scripts
    ]
)

def _trace_subagent_stream_producer(
    parent_agent: Any,
    *,
    event_type: str,
    subagent_id: str | None,
    delegate_call_id: str,
    task_index: int,
    offset: int,
    text: str,
) -> None:
    if not is_truthy_value(os.environ.get("DOVIE_STREAM_TRACE")):
        return
    logger.info(
        "[dovie-subagent-stream-source] stage=producer event_type=%s "
        "session_id=%s run_id=%s turn_id=%s subagent_id=%s delegate_call_id=%s "
        "task_index=%s offset=%s text_len=%s utf16_len=%s",
        event_type,
        str(getattr(parent_agent, "session_id", "") or ""),
        str(getattr(parent_agent, "_hermes_active_run_id", "") or ""),
        str(getattr(parent_agent, "_hermes_active_turn_id", "") or ""),
        str(subagent_id or ""),
        delegate_call_id,
        task_index,
        offset,
        len(text),
        len(text.encode("utf-16-le")) // 2,
    )


def _trace_subagent_event_producer(
    parent_agent: Any,
    *,
    event_type: str,
    subagent_id: str | None,
    delegate_call_id: str,
    task_index: int,
    source_index: int,
    payload: Dict[str, Any],
) -> None:
    if not is_truthy_value(os.environ.get("DOVIE_STREAM_TRACE")):
        return

    def _json_bytes(value: Any) -> int:
        try:
            return len(
                json.dumps(value, ensure_ascii=False, default=str).encode("utf-8")
            )
        except Exception:
            return -1

    logger.info(
        "[dovie-subagent-event-source] stage=producer event_type=%s "
        "session_id=%s run_id=%s turn_id=%s subagent_id=%s delegate_call_id=%s "
        "task_index=%s source_index=%s tool_id=%s status=%s tool_count=%s "
        "preview_bytes=%s args_bytes=%s result_bytes=%s context_bytes=%s "
        "dispatch_message_bytes=%s payload_bytes=%s",
        event_type,
        str(getattr(parent_agent, "session_id", "") or ""),
        str(getattr(parent_agent, "_hermes_active_run_id", "") or ""),
        str(getattr(parent_agent, "_hermes_active_turn_id", "") or ""),
        str(subagent_id or ""),
        delegate_call_id,
        task_index,
        source_index,
        str(payload.get("tool_id") or ""),
        str(payload.get("status") or ""),
        payload.get("tool_count"),
        _json_bytes(payload.get("preview")),
        _json_bytes(payload.get("args")),
        _json_bytes(payload.get("result")),
        _json_bytes(payload.get("context")),
        _json_bytes(payload.get("dispatch_message")),
        _json_bytes(payload),
    )


def _subagent_auto_deny(command: str, description: str, **kwargs) -> str:
    """Auto-deny dangerous commands in subagent threads (safe default).

    Returns 'deny' so the subagent sees a refusal it can recover from, and
    never calls input() (which would deadlock the parent TUI).
    """
    logger.warning(
        "Subagent auto-denied dangerous command: %s (%s). "
        "Set delegation.subagent_auto_approve: true to allow.",
        command, description,
    )
    return "deny"


def _subagent_auto_approve(command: str, description: str, **kwargs) -> str:
    """Auto-approve dangerous commands in subagent threads (opt-in YOLO).

    Only installed when delegation.subagent_auto_approve=true. Returns 'once'
    so the subagent proceeds without blocking the parent UI.
    """
    logger.warning(
        "Subagent auto-approved dangerous command: %s (%s)",
        command, description,
    )
    return "once"


def _get_subagent_approval_callback():
    """Return the callback to install into subagent worker threads.

    Config key: delegation.subagent_auto_approve (bool, default False).
    Reads via the same _load_config() path as the rest of delegate_task so
    priority is config.yaml > (no env override for this knob) > default.
    """
    cfg = _load_config()
    val = cfg.get("subagent_auto_approve", False)
    if is_truthy_value(val):
        return _subagent_auto_approve
    return _subagent_auto_deny

# Build a description fragment listing toolsets available for subagents.
# Excludes toolsets where ALL tools are blocked, composite/platform toolsets
# (hermes-* prefixed), and scenario toolsets.
#
# NOTE: "delegation" is in this exclusion set so the subagent-facing
# capability hint string (_TOOLSET_LIST_STR) doesn't advertise it as a
# toolset to request explicitly — the correct mechanism for nested
# delegation is role='orchestrator', which re-adds "delegation" in
# _build_child_agent regardless of this exclusion.
_EXCLUDED_TOOLSET_NAMES = frozenset({"debugging", "safe", "delegation", "moa", "rl"})
_SUBAGENT_TOOLSETS = sorted(
    name
    for name, defn in TOOLSETS.items()
    if name not in _EXCLUDED_TOOLSET_NAMES
    and not name.startswith("hermes-")
    and not all(t in DELEGATE_BLOCKED_TOOLS for t in defn.get("tools", []))
)
_TOOLSET_LIST_STR = ", ".join(f"'{n}'" for n in _SUBAGENT_TOOLSETS)

_DEFAULT_MAX_CONCURRENT_CHILDREN = 3
MAX_DEPTH = 1  # flat by default: parent (0) -> child (1); grandchild rejected unless max_spawn_depth raised.
# Configurable depth cap consulted by _get_max_spawn_depth; MAX_DEPTH
# stays as the default fallback and is still the symbol tests import.
_MIN_SPAWN_DEPTH = 1
_MAX_SPAWN_DEPTH_CAP = 3


# ---------------------------------------------------------------------------
# Runtime state: pause flag + active subagent registry
#
# Consumed by the TUI observability layer (overlay/control surface) and the
# gateway RPCs `delegation.pause`, `delegation.status`, `subagent.interrupt`.
# Kept module-level so they span every delegate_task invocation in the
# process, including nested orchestrator -> worker chains.
# ---------------------------------------------------------------------------

_spawn_pause_lock = threading.Lock()
_spawn_paused: bool = False

_active_subagents_lock = threading.Lock()
# subagent_id -> mutable record tracking the live child agent.  Stays only
# for the lifetime of the run; _run_single_child is the owner.
_active_subagents: Dict[str, Dict[str, Any]] = {}


def set_spawn_paused(paused: bool) -> bool:
    """Globally block/unblock new delegate_task spawns.

    Active children keep running; only NEW calls to delegate_task fail fast
    with a "spawning paused" error until unblocked.  Returns the new state.
    """
    global _spawn_paused
    with _spawn_pause_lock:
        _spawn_paused = bool(paused)
        return _spawn_paused


def is_spawn_paused() -> bool:
    with _spawn_pause_lock:
        return _spawn_paused


def _register_subagent(record: Dict[str, Any]) -> None:
    sid = record.get("subagent_id")
    if not sid:
        return
    with _active_subagents_lock:
        _active_subagents[sid] = record


def _unregister_subagent(subagent_id: str) -> None:
    with _active_subagents_lock:
        _active_subagents.pop(subagent_id, None)


def interrupt_subagent(subagent_id: str) -> bool:
    """Request that a single running subagent stop at its next iteration boundary.

    Does not hard-kill the worker thread (Python can't); sets the child's
    interrupt flag which propagates to in-flight tools and recurses into
    grandchildren via AIAgent.interrupt().  Returns True if a matching
    subagent was found.
    """
    with _active_subagents_lock:
        record = _active_subagents.get(subagent_id)
    if not record:
        return False
    agent = record.get("agent")
    if agent is None:
        return False
    try:
        agent.interrupt(f"Interrupted via TUI ({subagent_id})")
    except Exception as exc:
        logger.debug("interrupt_subagent(%s) failed: %s", subagent_id, exc)
        return False
    return True


def list_active_subagents() -> List[Dict[str, Any]]:
    """Snapshot of the currently running subagent tree.

    Each record: {subagent_id, parent_id, depth, goal, agent_name, model,
    started_at, tool_count, status}.  Safe to call from any thread — returns a copy.
    """
    with _active_subagents_lock:
        return [
            {k: v for k, v in r.items() if k != "agent"}
            for r in _active_subagents.values()
        ]


def _normalize_role(r: Optional[str]) -> str:
    """Normalise a caller-provided role to 'leaf' or 'orchestrator'.

    None/empty -> 'leaf'.  Unknown strings coerce to 'leaf' with a
    warning log (matches the silent-degrade pattern of
    _get_orchestrator_enabled).  _build_child_agent adds a second
    degrade layer for depth/kill-switch bounds.
    """
    if r is None or not r:
        return "leaf"
    r_norm = str(r).strip().lower()
    if r_norm in {"leaf", "orchestrator"}:
        return r_norm
    logger.warning("Unknown delegate_task role=%r, coercing to 'leaf'", r)
    return "leaf"


def _get_max_concurrent_children() -> int:
    """Read delegation.max_concurrent_children from config, falling back to
    DELEGATION_MAX_CONCURRENT_CHILDREN env var, then the default (3).

    Users can raise this as high as they want; only the floor (1) is enforced.

    Uses the same ``_load_config()`` path that the rest of ``delegate_task``
    uses, keeping config priority consistent (config.yaml > env > default).
    """
    cfg = _load_config()
    val = cfg.get("max_concurrent_children")
    if val is not None:
        try:
            result = max(1, int(val))
            if result > 10:
                logger.warning(
                    "delegation.max_concurrent_children=%d: each child consumes API tokens "
                    "independently. High values multiply cost linearly.",
                    result,
                )
            return result
        except (TypeError, ValueError):
            logger.warning(
                "delegation.max_concurrent_children=%r is not a valid integer; "
                "using default %d",
                val,
                _DEFAULT_MAX_CONCURRENT_CHILDREN,
            )
            return _DEFAULT_MAX_CONCURRENT_CHILDREN
    env_val = os.getenv("DELEGATION_MAX_CONCURRENT_CHILDREN")
    if env_val:
        try:
            return max(1, int(env_val))
        except (TypeError, ValueError):
            return _DEFAULT_MAX_CONCURRENT_CHILDREN
    return _DEFAULT_MAX_CONCURRENT_CHILDREN


_DEFAULT_MAX_ASYNC_CHILDREN = 3


def _get_max_async_children() -> int:
    """Read delegation.max_async_children from config (floor 1, no ceiling).

    Caps how many background (``background=true``) subagents can run at once.
    When at capacity, a new async dispatch is REJECTED (not queued) so a
    runaway model can't pile up unbounded background work. Separate from
    max_concurrent_children, which bounds a single synchronous batch.
    """
    cfg = _load_config()
    val = cfg.get("max_async_children")
    if val is not None:
        try:
            return max(1, int(val))
        except (TypeError, ValueError):
            logger.warning(
                "delegation.max_async_children=%r is not a valid integer; "
                "using default %d",
                val, _DEFAULT_MAX_ASYNC_CHILDREN,
            )
            return _DEFAULT_MAX_ASYNC_CHILDREN
    env_val = os.getenv("DELEGATION_MAX_ASYNC_CHILDREN")
    if env_val:
        try:
            return max(1, int(env_val))
        except (TypeError, ValueError):
            return _DEFAULT_MAX_ASYNC_CHILDREN
    return _DEFAULT_MAX_ASYNC_CHILDREN


def _get_max_summary_chars() -> int:
    value = _load_config().get("max_summary_chars", _DEFAULT_MAX_SUMMARY_CHARS)
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return _DEFAULT_MAX_SUMMARY_CHARS


def _parent_summary_char_budget(parent_agent, summary_count: int) -> Optional[int]:
    """Return a per-summary budget from the parent's remaining input window."""
    return _parent_delegation_summary_char_budget(parent_agent, summary_count)


def _apply_summary_budget(
    results: List[Dict[str, Any]],
    parent_agent,
    *,
    total_summary_count: int | None = None,
) -> None:
    """Bound fan-in context while preserving full summaries in secure storage."""
    _apply_delegation_summary_budget(
        results,
        parent_agent,
        static_cap=_get_max_summary_chars(),
        total_summary_count=total_summary_count,
    )


def _get_child_timeout() -> Optional[float]:
    """Read delegation.child_timeout_seconds from config.

    Returns the number of seconds a single child agent is allowed to stay
    inactive before being considered stuck. Default: 600 s (10 minutes).
    """
    cfg = _load_config()
    val = cfg.get("child_timeout_seconds")
    if val is not None:
        try:
            return max(30.0, float(val))
        except (TypeError, ValueError):
            logger.warning(
                "delegation.child_timeout_seconds=%r is not a valid number; "
                "using default %d",
                val,
                DEFAULT_CHILD_TIMEOUT,
            )
    env_val = os.getenv("DELEGATION_CHILD_TIMEOUT_SECONDS")
    if env_val:
        try:
            return max(30.0, float(env_val))
        except (TypeError, ValueError):
            pass
    return float(DEFAULT_CHILD_TIMEOUT)


_CHILD_ACTIVITY_TOKEN_FIELDS = (
    "last_activity_ts",
    "last_activity_desc",
    "current_tool",
    "api_call_count",
    "budget_used",
    "budget_max",
)


def _get_child_activity_token(child: Any) -> Optional[tuple]:
    """Return a comparable child activity token, or None if unavailable."""
    if child is None:
        return None
    try:
        summary = child.get_activity_summary()
    except Exception:
        return None
    if not isinstance(summary, dict):
        return None
    return tuple(
        (field, repr(summary.get(field)))
        for field in _CHILD_ACTIVITY_TOKEN_FIELDS
    )


def _wait_for_child_result_with_idle_timeout(
    child_future: Any,
    *,
    child: Any,
    idle_timeout_seconds: Optional[float],
    poll_interval: Optional[float] = None,
) -> Any:
    """Wait for a child result and time out only after child inactivity.

    ``delegation.child_timeout_seconds`` used to be applied as a wall-clock
    future timeout, which killed legitimate long-running subagents even while
    they were actively streaming or executing tools. The child already exposes
    activity via ``get_activity_summary()``; this watcher resets its idle timer
    whenever that activity token changes and only raises after continuous
    inactivity for the configured duration.
    """
    if idle_timeout_seconds is None:
        return child_future.result()

    idle_timeout = float(idle_timeout_seconds)
    if idle_timeout <= 0:
        return child_future.result()

    poll = max(0.05, float(poll_interval or _CHILD_IDLE_TIMEOUT_POLL_INTERVAL))
    last_activity_at = time.monotonic()
    last_token = _get_child_activity_token(child)

    while True:
        now = time.monotonic()
        remaining_idle = idle_timeout - (now - last_activity_at)
        if remaining_idle <= 0:
            if child_future.done():
                return child_future.result()
            raise FuturesTimeoutError(
                f"child idle for {idle_timeout:g}s without activity"
            )

        try:
            return child_future.result(timeout=min(poll, remaining_idle))
        except FuturesTimeoutError:
            current_token = _get_child_activity_token(child)
            if current_token is not None and current_token != last_token:
                last_token = current_token
                last_activity_at = time.monotonic()


def _get_max_spawn_depth() -> int:
    """Read delegation.max_spawn_depth from config, clamped to [1, 3].

    depth 0 = parent agent.  max_spawn_depth = N means agents at depths
    0..N-1 can spawn; depth N is the leaf floor.  Default 1 is flat:
    parent spawns children (depth 1), depth-1 children cannot spawn
    (blocked by this guard AND, for leaf children, by the delegation
    toolset strip in _strip_blocked_tools).

    Raise to 2 or 3 to unlock nested orchestration. role="orchestrator"
    removes the toolset strip for depth-1 children when
    max_spawn_depth >= 2, enabling them to spawn their own workers.
    """
    cfg = _load_config()
    val = cfg.get("max_spawn_depth")
    if val is None:
        return MAX_DEPTH
    try:
        ival = int(val)
    except (TypeError, ValueError):
        logger.warning(
            "delegation.max_spawn_depth=%r is not a valid integer; " "using default %d",
            val,
            MAX_DEPTH,
        )
        return MAX_DEPTH
    clamped = max(_MIN_SPAWN_DEPTH, min(_MAX_SPAWN_DEPTH_CAP, ival))
    if clamped != ival:
        logger.warning(
            "delegation.max_spawn_depth=%d out of range [%d, %d]; " "clamping to %d",
            ival,
            _MIN_SPAWN_DEPTH,
            _MAX_SPAWN_DEPTH_CAP,
            clamped,
        )
    return clamped


def _get_orchestrator_enabled() -> bool:
    """Global kill switch for the orchestrator role.

    When False, role="orchestrator" is silently forced to "leaf" in
    _build_child_agent and the delegation toolset is stripped as before.
    Lets an operator disable the feature without a code revert.
    """
    cfg = _load_config()
    val = cfg.get("orchestrator_enabled", True)
    if isinstance(val, bool):
        return val
    # Accept "true"/"false" strings from YAML that doesn't auto-coerce.
    if isinstance(val, str):
        return val.strip().lower() in {"true", "1", "yes", "on"}
    return True


def _get_inherit_mcp_toolsets() -> bool:
    """Whether narrowed child toolsets should keep the parent's MCP toolsets."""
    cfg = _load_config()
    return is_truthy_value(cfg.get("inherit_mcp_toolsets"), default=True)


DEFAULT_MAX_ITERATIONS = 50
DEFAULT_CHILD_TIMEOUT = 600  # idle seconds before a child agent is considered stuck
_CHILD_IDLE_TIMEOUT_POLL_INTERVAL = 1.0
_HEARTBEAT_INTERVAL = 30  # seconds between parent activity heartbeats during delegation
# Stale-heartbeat thresholds. A child with no API-call progress is either:
#   - idle between turns (no current_tool) — no visible child activity
#   - inside a tool (current_tool set) — probably running a legitimately long
#     operation (terminal command, web fetch, large file read)
# The child idle timeout is the authoritative stuck detector. These heartbeat
# ceilings only prevent endless parent heartbeats if the idle watcher fails.
_HEARTBEAT_STALE_CYCLES_IDLE = 15  # 15 * 30s = 450s idle between turns → stale
_HEARTBEAT_STALE_CYCLES_IN_TOOL = 40  # 40 * 30s = 1200s stuck on same tool → stale
DEFAULT_TOOLSETS = ["terminal", "file", "web"]


class DelegateEvent(str, enum.Enum):
    """Formal event types emitted during delegation progress.

    _build_child_progress_callback normalises incoming legacy strings
    (``tool.started``, ``_thinking``, …) to these enum values via
    ``_LEGACY_EVENT_MAP``.  External consumers (gateway SSE, ACP adapter,
    CLI) still receive the legacy strings during the deprecation window.

    TASK_SPAWNED / TASK_COMPLETED / TASK_FAILED are reserved for
    future orchestrator lifecycle events and are not currently emitted.
    """

    TASK_SPAWNED = "delegate.task_spawned"
    TASK_PROGRESS = "delegate.task_progress"
    TASK_COMPLETED = "delegate.task_completed"
    TASK_FAILED = "delegate.task_failed"
    TASK_THINKING = "delegate.task_thinking"
    TASK_TOOL_STARTED = "delegate.tool_started"
    TASK_TOOL_COMPLETED = "delegate.tool_completed"


# Legacy event strings → DelegateEvent mapping.
# Incoming child-agent events use the old names; the callback normalises them.
_LEGACY_EVENT_MAP: Dict[str, DelegateEvent] = {
    "_thinking": DelegateEvent.TASK_THINKING,
    "reasoning.available": DelegateEvent.TASK_THINKING,
    "tool.started": DelegateEvent.TASK_TOOL_STARTED,
    "tool.completed": DelegateEvent.TASK_TOOL_COMPLETED,
    "subagent_progress": DelegateEvent.TASK_PROGRESS,
}


def check_delegate_requirements() -> bool:
    """Delegation has no external requirements -- always available."""
    return True


def _build_child_system_prompt(
    goal: str,
    context: Optional[str] = None,
    *,
    workspace_path: Optional[str] = None,
    parent_system_prompt: Optional[str] = None,
    role: str = "leaf",
    max_spawn_depth: int = 2,
    child_depth: int = 1,
) -> str:
    """Build a focused system prompt for a child agent.

    When role='orchestrator', appends a delegation-capability block
    modeled on OpenClaw's buildSubagentSystemPrompt (canSpawn branch at
    inspiration/openclaw/src/agents/subagent-system-prompt.ts:63-95).
    The depth note is literal truth (grounded in the passed config) so
    the LLM doesn't confabulate nesting capabilities that don't exist.
    """
    parts = []
    parent_prompt = str(parent_system_prompt or "").strip()
    if parent_prompt:
        parts.extend(
            [
                "Inherit the parent agent's active runtime instructions:",
                parent_prompt,
                "",
            ]
        )

    parts.extend([
        "You are a focused subagent working on a specific delegated task.",
        "",
        f"YOUR TASK:\n{goal}",
    ])
    if context and context.strip():
        parts.append(f"\nCONTEXT:\n{context}")
    if workspace_path and str(workspace_path).strip():
        parts.append(
            "\nWORKSPACE PATH:\n"
            f"{workspace_path}\n"
            "Use this exact path for local repository/workdir operations unless the task explicitly says otherwise."
        )
    parts.append(
        "\nComplete this task using the tools available to you. "
        "When finished, provide a clear, concise summary of:\n"
        "- What you did\n"
        "- What you found or accomplished\n"
        "- Any files you created or modified\n"
        "- Any issues encountered\n\n"
        "Important workspace rule: Never assume a repository lives at /workspace/... or any other container-style path unless the task/context explicitly gives that path. "
        "If no exact local path is provided, discover it first before issuing git/workdir-specific commands.\n\n"
        "Be thorough but concise -- your response is returned to the "
        "parent agent as a summary."
    )
    if role == "orchestrator":
        child_note = (
            "Your own children MUST be leaves (cannot delegate further) "
            "because they would be at the depth floor — you cannot pass "
            "role='orchestrator' to your own delegate_task calls."
            if child_depth + 1 >= max_spawn_depth
            else "Your own children can themselves be orchestrators or leaves, "
            "depending on the `role` you pass to delegate_task. Default is "
            "'leaf'; pass role='orchestrator' explicitly when a child "
            "needs to further decompose its work."
        )
        parts.append(
            "\n## Subagent Spawning (Orchestrator Role)\n"
            "You have access to the `delegate_task` tool and CAN spawn "
            "your own subagents to parallelize independent work.\n\n"
            "WHEN to delegate:\n"
            "- The goal decomposes into 2+ independent subtasks that can "
            "run in parallel (e.g. research A and B simultaneously).\n"
            "- A subtask is reasoning-heavy and would flood your context "
            "with intermediate data.\n\n"
            "WHEN NOT to delegate:\n"
            "- Single-step mechanical work — do it directly.\n"
            "- Trivial tasks you can execute in one or two tool calls.\n"
            "- Re-delegating your entire assigned goal to one worker "
            "(that's just pass-through with no value added).\n\n"
            "Coordinate your workers' results and synthesize them before "
            "reporting back to your parent. You are responsible for the "
            "final summary, not your workers.\n\n"
            f"NOTE: You are at depth {child_depth}. The delegation tree "
            f"is capped at max_spawn_depth={max_spawn_depth}. {child_note}"
        )
    return "\n".join(parts)


def _resolve_workspace_hint(parent_agent) -> Optional[str]:
    """Best-effort local workspace hint for child prompts.

    We only inject a path when we have a concrete absolute directory. This avoids
    teaching subagents a fake container path while still helping them avoid
    guessing `/workspace/...` for local repo tasks.
    """
    candidates = [
        os.getenv("TERMINAL_CWD"),
        getattr(
            getattr(parent_agent, "_subdirectory_hints", None), "working_dir", None
        ),
        getattr(parent_agent, "terminal_cwd", None),
        getattr(parent_agent, "cwd", None),
    ]
    for candidate in candidates:
        if not candidate:
            continue
        try:
            text = os.path.abspath(os.path.expanduser(str(candidate)))
        except Exception:
            continue
        if os.path.isabs(text) and os.path.isdir(text):
            return text
    return None


def _strip_blocked_tools(toolsets: List[str]) -> List[str]:
    """Remove toolsets that contain only blocked tools."""
    return _strip_blocked_toolsets(toolsets)


def _build_child_progress_callback(
    task_index: int,
    goal: str,
    parent_agent,
    task_count: int = 1,
    *,
    subagent_id: Optional[str] = None,
    parent_id: Optional[str] = None,
    depth: Optional[int] = None,
    model: Optional[str] = None,
    toolsets: Optional[List[str]] = None,
    role: Optional[str] = None,
    context: Optional[str] = None,
    delegate_call_id: Optional[str] = None,
    agent_name: Optional[str] = None,
) -> Optional[callable]:
    """Build a callback that relays child agent tool calls to the parent display.

    Two display paths:
      CLI:     prints tree-view lines above the parent's delegation spinner
      Gateway: batches tool names and relays to parent's progress callback

    Routing ids are threaded into every event. Immutable task descriptors are
    carried only by spawn/start; repeating a large task context on every tool
    lifecycle event makes transport and persistence cost grow with tool count.

    Returns None if no display mechanism is available, in which case the
    child agent runs with no progress callback (identical to current behavior).
    """
    if getattr(parent_agent, "_delegate_child_progress_suppressed", False) is True:
        return None

    spinner = getattr(parent_agent, "_delegate_spinner", None)
    parent_cb = getattr(parent_agent, "tool_progress_callback", None)

    if not spinner and not parent_cb:
        return None  # No display → no callback → zero behavior change

    # Show 1-indexed prefix only in batch mode (multiple tasks)
    prefix = f"[{task_index + 1}] " if task_count > 1 else ""
    goal_label = (goal or "").strip()
    context_text = (context or "").strip()
    dispatch_message = (
        f"{goal_label}\n\n{context_text}".strip()
        if context_text and goal_label
        else goal_label or context_text
    )
    normalized_delegate_call_id = str(delegate_call_id or "").strip()
    normalized_agent_name = _clean_subagent_name(agent_name)
    raw_delegation_tool_name = getattr(parent_agent, "_delegate_child_output_tool_name", "")
    delegation_tool_name = (
        raw_delegation_tool_name.strip()
        if isinstance(raw_delegation_tool_name, str)
        else ""
    )

    # Gateway: batch tool names, flush periodically
    _BATCH_SIZE = 5
    _batch: List[str] = []
    _tool_count = [0]  # per-subagent running counter (list for closure mutation)
    _source_event_count = [0]
    _pending_tools: List[Dict[str, Any]] = []

    def _identity_kwargs(*, include_descriptor: bool = False) -> Dict[str, Any]:
        kw: Dict[str, Any] = {
            "task_index": task_index,
            "task_count": task_count,
        }
        if subagent_id is not None:
            kw["subagent_id"] = subagent_id
        if parent_id is not None:
            kw["parent_id"] = parent_id
        if normalized_delegate_call_id:
            kw["delegate_call_id"] = normalized_delegate_call_id
            kw["tool_call_id"] = normalized_delegate_call_id
        if delegation_tool_name:
            kw["delegation_tool_name"] = delegation_tool_name
        if include_descriptor:
            kw["goal"] = goal_label
            if depth is not None:
                kw["depth"] = depth
            if model is not None:
                kw["model"] = model
            if toolsets is not None:
                kw["toolsets"] = list(toolsets)
            if role:
                kw["role"] = str(role)
            if normalized_agent_name:
                kw["agent_name"] = normalized_agent_name
            if context_text:
                kw["context"] = context_text
            if dispatch_message:
                kw["dispatch_message"] = dispatch_message
        kw["tool_count"] = _tool_count[0]
        return kw

    def _relay(
        event_type: str, tool_name: str = None, preview: str = None, args=None, **kwargs
    ):
        if not parent_cb:
            return
        payload = _identity_kwargs(
            include_descriptor=event_type in {
                "subagent.spawn_requested",
                "subagent.start",
            },
        )
        payload.update(kwargs)  # caller overrides (e.g. status, duration_seconds)
        diagnostic_payload = {
            **payload,
            "tool_name": tool_name,
            "preview": preview,
            "args": args,
        }
        _source_event_count[0] += 1
        _trace_subagent_event_producer(
            parent_agent,
            event_type=event_type,
            subagent_id=subagent_id,
            delegate_call_id=normalized_delegate_call_id,
            task_index=task_index,
            source_index=_source_event_count[0],
            payload=diagnostic_payload,
        )
        try:
            parent_cb(event_type, tool_name, preview, args, **payload)
        except Exception as e:
            logger.debug("Parent callback failed: %s", e)

    def _callback(
        event_type, tool_name: str = None, preview: str = None, args=None, **kwargs
    ):
        # Lifecycle events emitted by the orchestrator itself — handled
        # before enum normalisation since they are not part of DelegateEvent.
        if event_type == "subagent.spawn_requested":
            _relay(
                "subagent.spawn_requested",
                preview=preview or goal_label or "",
                **kwargs,
            )
            return

        if event_type == "subagent.start":
            if spinner and goal_label:
                short = (
                    (goal_label[:55] + "...") if len(goal_label) > 55 else goal_label
                )
                try:
                    spinner.print_above(f" {prefix}├─ 🔀 {short}")
                except Exception as e:
                    logger.debug("Spinner print_above failed: %s", e)
            _relay("subagent.start", preview=preview or goal_label or "", **kwargs)
            return

        if event_type == "subagent.complete":
            _relay("subagent.complete", preview=preview, **kwargs)
            return

        # Normalise legacy strings, new-style "delegate.*" strings, and
        # DelegateEvent enum values all to a single DelegateEvent.  The
        # original implementation only accepted the five legacy strings;
        # enum-typed callers were silently dropped.
        if isinstance(event_type, DelegateEvent):
            event = event_type
        else:
            event = _LEGACY_EVENT_MAP.get(event_type)
            if event is None:
                try:
                    event = DelegateEvent(event_type)
                except (ValueError, TypeError):
                    return  # Unknown event — ignore

        if event == DelegateEvent.TASK_THINKING:
            text = preview or tool_name or ""
            if spinner:
                short = (text[:55] + "...") if len(text) > 55 else text
                try:
                    spinner.print_above(f' {prefix}├─ 💭 "{short}"')
                except Exception as e:
                    logger.debug("Spinner print_above failed: %s", e)
            _relay("subagent.thinking", preview=text)
            return

        if event == DelegateEvent.TASK_TOOL_COMPLETED:
            pending_index = next(
                (
                    idx
                    for idx, item in enumerate(_pending_tools)
                    if not tool_name or item.get("name") == tool_name
                ),
                0 if _pending_tools else -1,
            )
            pending = _pending_tools.pop(pending_index) if pending_index >= 0 else {}
            completed_name = tool_name or pending.get("name") or ""
            completed_args = args if args is not None else pending.get("args")
            completed_preview = preview if preview is not None else pending.get("preview")
            is_error = bool(kwargs.get("is_error"))
            _relay(
                "subagent.tool",
                completed_name,
                completed_preview,
                completed_args,
                tool_id=pending.get("tool_id") or kwargs.get("tool_id"),
                status="failed" if is_error else "completed",
                duration_seconds=kwargs.get("duration_seconds") or kwargs.get("duration"),
                result=kwargs.get("result"),
            )
            return

        if event == DelegateEvent.TASK_PROGRESS:
            # Pre-batched progress summary relayed from a nested
            # orchestrator's grandchild (upstream emits as
            # parent_cb("subagent_progress", summary_string) where the
            # summary lands in the tool_name positional slot).  Treat as
            # a pass-through: render distinctly (not via the tool-start
            # emoji lookup, which would mistake the summary string for a
            # tool name) and relay upward without re-batching.
            summary_text = tool_name or preview or ""
            if spinner and summary_text:
                try:
                    spinner.print_above(f" {prefix}├─ 🔀 {summary_text}")
                except Exception as e:
                    logger.debug("Spinner print_above failed: %s", e)
            if parent_cb:
                try:
                    parent_cb("subagent_progress", f"{prefix}{summary_text}")
                except Exception as e:
                    logger.debug("Parent callback relay failed: %s", e)
            return

        # TASK_TOOL_STARTED — display and batch for parent relay
        _tool_count[0] += 1
        tool_id = (
            f"subagent-tool:{subagent_id or task_index}:{_tool_count[0]}:{tool_name or 'tool'}"
        )
        _pending_tools.append({
            "tool_id": tool_id,
            "name": tool_name or "",
            "preview": preview,
            "args": args,
        })
        if subagent_id is not None:
            with _active_subagents_lock:
                rec = _active_subagents.get(subagent_id)
                if rec is not None:
                    rec["tool_count"] = _tool_count[0]
                    rec["last_tool"] = tool_name or ""
        if spinner:
            short = (
                (preview[:35] + "...")
                if preview and len(preview) > 35
                else (preview or "")
            )
            from agent.display import get_tool_emoji

            emoji = get_tool_emoji(tool_name or "")
            line = f" {prefix}├─ {emoji} {tool_name}"
            if short:
                line += f'  "{short}"'
            try:
                spinner.print_above(line)
            except Exception as e:
                logger.debug("Spinner print_above failed: %s", e)

        if parent_cb:
            _relay("subagent.tool", tool_name, preview, args, tool_id=tool_id, status="running")
            _batch.append(tool_name or "")
            if len(_batch) >= _BATCH_SIZE:
                summary = ", ".join(_batch)
                _relay("subagent.progress", preview=f"🔀 {prefix}{summary}")
                _batch.clear()

    def _flush():
        """Flush remaining batched tool names to gateway on completion."""
        if parent_cb and _batch:
            summary = ", ".join(_batch)
            _relay("subagent.progress", preview=f"🔀 {prefix}{summary}")
            _batch.clear()

    _callback._flush = _flush
    return _callback


def _build_child_output_delta_callback(
    task_index: int,
    goal: str,
    parent_agent,
    task_count: int = 1,
    *,
    subagent_id: Optional[str] = None,
    parent_id: Optional[str] = None,
    depth: Optional[int] = None,
    model: Optional[str] = None,
    toolsets: Optional[List[str]] = None,
    role: Optional[str] = None,
    context: Optional[str] = None,
    delegate_call_id: Optional[str] = None,
    agent_name: Optional[str] = None,
) -> Optional[callable]:
    """Build a callback that relays child assistant text deltas to the parent.

    This is intentionally separate from ``_build_child_progress_callback``.
    Gateway/Dovie sessions need live child answer streaming in the side panel,
    while still keeping that text out of the parent's main assistant response.
    """
    if getattr(parent_agent, "_delegate_child_output_delta_enabled", True) is False:
        return None

    parent_cb = getattr(parent_agent, "tool_progress_callback", None)
    if not parent_cb:
        return None

    normalized_delegate_call_id = str(delegate_call_id or "").strip()
    raw_output_tool_name = getattr(parent_agent, "_delegate_child_output_tool_name", "")
    tool_name = raw_output_tool_name.strip() if isinstance(raw_output_tool_name, str) else ""

    def _identity_kwargs() -> Dict[str, Any]:
        kw: Dict[str, Any] = {
            "task_index": task_index,
            "task_count": task_count,
            "tool_count": 0,
        }
        if subagent_id is not None:
            kw["subagent_id"] = subagent_id
        if parent_id is not None:
            kw["parent_id"] = parent_id
        if depth is not None:
            kw["depth"] = depth
        if model is not None:
            kw["model"] = model
        if toolsets is not None:
            kw["toolsets"] = list(toolsets)
        if role:
            kw["role"] = str(role)
        if normalized_delegate_call_id:
            kw["delegate_call_id"] = normalized_delegate_call_id
            kw["tool_call_id"] = normalized_delegate_call_id
        return kw

    stream_offset = 0

    def _callback(text: Optional[str]) -> None:
        nonlocal stream_offset
        if text is None:
            return
        delta = str(text)
        if not delta:
            return
        try:
            _trace_subagent_stream_producer(
                parent_agent,
                event_type="subagent.output_delta",
                subagent_id=subagent_id,
                delegate_call_id=normalized_delegate_call_id,
                task_index=task_index,
                offset=stream_offset,
                text=delta,
            )
            parent_cb(
                "subagent.output_delta",
                tool_name or None,
                delta,
                None,
                **_identity_kwargs(),
                mode="append",
                delta=delta,
                offset=stream_offset,
            )
            stream_offset += len(delta.encode("utf-16-le")) // 2
        except Exception as exc:
            logger.debug("Parent output-delta callback failed: %s", exc)

    return _callback


def _build_child_reasoning_delta_callback(
    task_index: int,
    goal: str,
    parent_agent,
    task_count: int = 1,
    *,
    subagent_id: Optional[str] = None,
    parent_id: Optional[str] = None,
    depth: Optional[int] = None,
    model: Optional[str] = None,
    toolsets: Optional[List[str]] = None,
    role: Optional[str] = None,
    delegate_call_id: Optional[str] = None,
) -> Optional[callable]:
    """Relay provider reasoning deltas from a child agent to the parent UI.

    This preserves the same streaming reasoning contract as the main agent
    while keeping child reasoning out of the parent's model context.
    """
    if getattr(parent_agent, "_delegate_child_reasoning_delta_enabled", True) is False:
        return None

    parent_cb = getattr(parent_agent, "tool_progress_callback", None)
    if not parent_cb:
        return None

    normalized_delegate_call_id = str(delegate_call_id or "").strip()
    raw_output_tool_name = getattr(parent_agent, "_delegate_child_output_tool_name", "")
    tool_name = raw_output_tool_name.strip() if isinstance(raw_output_tool_name, str) else ""

    def _identity_kwargs() -> Dict[str, Any]:
        kw: Dict[str, Any] = {
            "task_index": task_index,
            "task_count": task_count,
            "tool_count": 0,
            "source": "provider_reasoning",
        }
        if subagent_id is not None:
            kw["subagent_id"] = subagent_id
        if parent_id is not None:
            kw["parent_id"] = parent_id
        if depth is not None:
            kw["depth"] = depth
        if model is not None:
            kw["model"] = model
        if toolsets is not None:
            kw["toolsets"] = list(toolsets)
        if role:
            kw["role"] = str(role)
        if normalized_delegate_call_id:
            kw["delegate_call_id"] = normalized_delegate_call_id
            kw["tool_call_id"] = normalized_delegate_call_id
        return kw

    stream_offset = 0

    def _callback(text: Optional[str]) -> None:
        nonlocal stream_offset
        if text is None:
            return
        delta = str(text)
        if not delta:
            return
        try:
            _trace_subagent_stream_producer(
                parent_agent,
                event_type="subagent.reasoning_delta",
                subagent_id=subagent_id,
                delegate_call_id=normalized_delegate_call_id,
                task_index=task_index,
                offset=stream_offset,
                text=delta,
            )
            parent_cb(
                "subagent.reasoning_delta",
                tool_name or None,
                delta,
                None,
                **_identity_kwargs(),
                mode="append",
                delta=delta,
                offset=stream_offset,
            )
            stream_offset += len(delta.encode("utf-16-le")) // 2
        except Exception as exc:
            logger.debug("Parent reasoning-delta callback failed: %s", exc)

    return _callback


def _recover_tasks_from_json_string(
    tasks: Any,
) -> tuple[Optional[List[Dict[str, Any]]], Optional[str]]:
    if not isinstance(tasks, str):
        return None, None
    raw = tasks.strip()
    if not raw:
        return None, "Provide either 'goal' (single task) or 'tasks' (batch)."
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        return None, (
            "tasks must be a JSON array of task objects; received a string "
            f"that could not be parsed as JSON ({exc.msg})."
        )
    if not isinstance(parsed, list):
        return None, (
            f"tasks must be a JSON array of task objects; parsed "
            f"{type(parsed).__name__} instead."
        )
    return parsed, None


def _child_terminal_entry(
    *,
    task_index: int,
    child: Any,
    status: str,
    error: str,
) -> Dict[str, Any]:
    return {
        "task_index": task_index,
        "agent_name": _clean_subagent_name(
            getattr(child, "_subagent_name", None),
        )
        or None,
        "status": status,
        "summary": None,
        "error": error,
        "api_calls": 0,
        "duration_seconds": 0,
        "_child_role": getattr(child, "_delegate_role", None),
    }


def _detach_async_children(parent_agent: Any, children: list[tuple]) -> None:
    """Detach async children from parent-turn cancellation ownership."""
    if not hasattr(parent_agent, "_active_children"):
        return
    lock = getattr(parent_agent, "_active_children_lock", None)
    for _index, _task, child in children:
        try:
            if lock:
                with lock:
                    parent_agent._active_children.remove(child)
            else:
                parent_agent._active_children.remove(child)
        except ValueError:
            pass


def _interrupt_prebuilt_children(children: list[tuple], reason: str) -> None:
    for _index, _task, child in children:
        try:
            if hasattr(child, "interrupt"):
                child.interrupt(reason)
            elif hasattr(child, "_interrupt_requested"):
                child._interrupt_requested = True
        except Exception:
            logger.debug("subagent interrupt failed", exc_info=True)


def _interrupt_prebuilt_child(
    children: list[tuple],
    task_index: int,
    reason: str,
) -> None:
    for index, _task, child in children:
        if index != task_index:
            continue
        try:
            if hasattr(child, "interrupt"):
                child.interrupt(reason)
            elif hasattr(child, "_interrupt_requested"):
                child._interrupt_requested = True
        except Exception:
            logger.debug("subagent child interrupt failed", exc_info=True)
        return


def _execute_prebuilt_children(
    *,
    children: list[tuple],
    task_list: list[Dict[str, Any]],
    parent_agent: Any,
    max_children: int,
    async_mode: bool,
    on_child_result: Optional[Callable[[Dict[str, Any]], None]] = None,
) -> List[Dict[str, Any]]:
    """Run already-built children through one sync/async-neutral kernel."""
    from concurrent.futures import FIRST_COMPLETED
    from concurrent.futures import wait as wait_futures

    n_tasks = len(children)
    results: List[Dict[str, Any]] = []
    labels = [str(task.get("goal") or "")[:40] for task in task_list]
    spinner_ref = getattr(parent_agent, "_delegate_spinner", None)

    def accept(entry: Dict[str, Any]) -> None:
        _apply_summary_budget(
            [entry],
            parent_agent,
            total_summary_count=n_tasks,
        )
        if on_child_result is not None:
            on_child_result(entry)
        results.append(entry)

    if n_tasks == 1:
        index, task, child = children[0]
        try:
            entry = _run_single_child(index, task["goal"], child, parent_agent)
        except Exception as exc:
            entry = _child_terminal_entry(
                task_index=index,
                child=child,
                status="error",
                error=str(exc) or type(exc).__name__,
            )
        accept(entry)
        return results

    if async_mode:
        from hermes_agent.application.subagent_execution_service import (
            DaemonThreadPoolExecutor,
        )

        executor_class = DaemonThreadPoolExecutor
    else:
        executor_class = ThreadPoolExecutor
    executor = executor_class(
        max_workers=max_children,
        thread_name_prefix="subagent-fanout",
    )
    futures = {
        executor.submit(
            _run_single_child,
            task_index=index,
            goal=task["goal"],
            child=child,
            parent_agent=parent_agent,
        ): index
        for index, task, child in children
    }
    child_by_index = {index: child for index, _task, child in children}
    pending = set(futures)
    interrupted = False
    completed_count = 0
    try:
        while pending:
            if (
                not async_mode
                and getattr(parent_agent, "_interrupt_requested", False) is True
            ):
                interrupted = True
                for future in pending:
                    index = futures[future]
                    if future.done():
                        try:
                            entry = future.result()
                        except Exception as exc:
                            entry = _child_terminal_entry(
                                task_index=index,
                                child=child_by_index.get(index),
                                status="error",
                                error=str(exc) or type(exc).__name__,
                            )
                    else:
                        entry = _child_terminal_entry(
                            task_index=index,
                            child=child_by_index.get(index),
                            status="interrupted",
                            error="Parent turn interrupted before child completion",
                        )
                    accept(entry)
                break

            done, pending = wait_futures(
                pending,
                timeout=0.5,
                return_when=FIRST_COMPLETED,
            )
            for future in done:
                index = futures[future]
                try:
                    entry = future.result()
                except Exception as exc:
                    entry = _child_terminal_entry(
                        task_index=index,
                        child=child_by_index.get(index),
                        status="error",
                        error=str(exc) or type(exc).__name__,
                    )
                accept(entry)
                completed_count += 1
                label = labels[index] if index < len(labels) else f"Task {index}"
                status = entry.get("status", "?")
                icon = "✓" if status == "completed" else "✗"
                line = (
                    f"{icon} [{index + 1}/{n_tasks}] {label}  "
                    f"({entry.get('duration_seconds', 0)}s)"
                )
                if spinner_ref:
                    try:
                        spinner_ref.print_above(line)
                    except Exception:
                        print(f"  {line}")
                else:
                    print(f"  {line}")
                remaining = n_tasks - completed_count
                if spinner_ref and remaining > 0:
                    try:
                        spinner_ref.update_text(
                            f"🔀 {remaining} task{'s' if remaining != 1 else ''} remaining"
                        )
                    except Exception:
                        logger.debug("spinner update failed", exc_info=True)
    finally:
        executor.shutdown(wait=not interrupted, cancel_futures=interrupted)
    results.sort(key=lambda entry: entry["task_index"])
    return results


def _finalize_delegation_results(
    *,
    results: List[Dict[str, Any]],
    task_list: list[Dict[str, Any]],
    children: list[tuple],
    parent_agent: Any,
    overall_start: float,
) -> Dict[str, Any]:
    """Apply parent-side accounting exactly once for either execution mode."""
    lock = getattr(parent_agent, "_delegation_result_lock", None)
    if lock is None:
        lock = threading.RLock()
        parent_agent._delegation_result_lock = lock
    with lock:
        manager = getattr(parent_agent, "_memory_manager", None)
        if manager:
            for entry in results:
                try:
                    index = entry["task_index"]
                    manager.on_delegation(
                        task=task_list[index]["goal"] if index < len(task_list) else "",
                        result=entry.get("summary", "") or "",
                        child_session_id=(
                            getattr(children[index][2], "session_id", "")
                            if index < len(children)
                            else ""
                        ),
                    )
                except Exception:
                    logger.debug("delegation memory callback failed", exc_info=True)

        try:
            from hermes_cli.plugins import invoke_hook as invoke_hook
        except Exception:
            invoke_hook = None
        child_cost_total = 0.0
        for entry in results:
            child_role = entry.pop("_child_role", None)
            try:
                child_cost_total += float(entry.pop("_child_cost_usd", 0.0) or 0.0)
            except (TypeError, ValueError):
                pass
            if invoke_hook is not None:
                try:
                    invoke_hook(
                        "subagent_stop",
                        parent_session_id=getattr(parent_agent, "session_id", None),
                        child_role=child_role,
                        child_summary=entry.get("summary"),
                        child_status=entry.get("status"),
                        duration_ms=int((entry.get("duration_seconds") or 0) * 1000),
                    )
                except Exception:
                    logger.debug("subagent_stop hook failed", exc_info=True)
        if child_cost_total > 0:
            try:
                current = float(
                    getattr(parent_agent, "session_estimated_cost_usd", 0.0) or 0.0
                )
                parent_agent.session_estimated_cost_usd = current + child_cost_total
                if getattr(parent_agent, "session_cost_source", "none") in {None, "", "none"}:
                    parent_agent.session_cost_source = "subagent"
                if getattr(parent_agent, "session_cost_status", "unknown") in {
                    None,
                    "",
                    "unknown",
                }:
                    parent_agent.session_cost_status = "estimated"
            except Exception:
                logger.debug("subagent cost rollup failed", exc_info=True)
    return {
        "results": results,
        "total_duration_seconds": round(time.monotonic() - overall_start, 2),
    }


def delegate_task(
    goal: Optional[str] = None,
    name: Optional[str] = None,
    context: Optional[str] = None,
    toolsets: Optional[List[str]] = None,
    tasks: Optional[List[Dict[str, Any]]] = None,
    max_iterations: Optional[int] = None,
    acp_command: Optional[str] = None,
    acp_args: Optional[List[str]] = None,
    role: Optional[str] = None,
    execution_mode: Optional[str] = None,
    background: Optional[bool] = None,
    parent_agent=None,
    delegate_call_id: Optional[str] = None,
) -> str:
    """
    Spawn one or more child agents to handle delegated tasks.

    Supports two modes:
      - Single: provide goal (+ optional name, context, toolsets, role)
      - Batch:  provide tasks array [{goal, name, context, toolsets, role}, ...]

    The 'role' parameter controls whether a child can further delegate:
    'leaf' (default) cannot; 'orchestrator' retains the delegation
    toolset and can spawn its own workers, bounded by
    delegation.max_spawn_depth.  Per-task role beats the top-level one.

    ``execution_mode`` is the authoritative sync/async choice.  The legacy
    ``background`` boolean remains a compatibility adapter and must agree
    when both are supplied.

    Sync returns a JSON results array.  Async returns a persistent Activity
    handle immediately; completion remains an internal Activity/Run fact and
    is never injected into the conversation as a synthetic user message.
    """
    if parent_agent is None:
        return tool_error("delegate_task requires a parent agent context.")

    from hermes_agent.application.subagent_execution_service import (
        ExecutionMode,
        ExecutionModeError,
        PersistenceRequiredError,
        SubagentExecutionService,
        SubagentTaskSpec,
        resolve_execution_mode,
        subagent_execution_runtime,
    )

    try:
        resolved_mode = resolve_execution_mode(
            execution_mode=execution_mode,
            background=background,
        )
    except ExecutionModeError as exc:
        return tool_error(str(exc))

    # Operator-controlled kill switch — lets the TUI freeze new fan-out
    # when a runaway tree is detected, without interrupting already-running
    # children.  Cleared via the matching `delegation.pause` RPC.
    if is_spawn_paused():
        return tool_error(
            "Delegation spawning is paused. Clear the pause via the TUI "
            "(`p` in /agents) or the `delegation.pause` RPC before retrying."
        )

    # Normalise the top-level role once; per-task overrides re-normalise.
    top_role = _normalize_role(role)
    normalized_delegate_call_id = str(
        delegate_call_id or getattr(parent_agent, "_current_tool_call_id", "") or ""
    ).strip()

    # Depth limit — configurable via delegation.max_spawn_depth,
    # default 2 for parity with the original MAX_DEPTH constant.
    depth = getattr(parent_agent, "_delegate_depth", 0)
    if resolved_mode is ExecutionMode.ASYNC and depth > 0:
        return tool_error(
            "Nested async delegation is not allowed. Orchestrator subagents "
            "must use execution_mode='sync' so one bounded parent lifecycle "
            "owns every descendant."
        )
    max_spawn = _get_max_spawn_depth()
    if depth >= max_spawn:
        return json.dumps(
            {
                "error": (
                    f"Delegation depth limit reached (depth={depth}, "
                    f"max_spawn_depth={max_spawn}). Raise "
                    f"delegation.max_spawn_depth in config.yaml if deeper "
                    f"nesting is required (cap: {_MAX_SPAWN_DEPTH_CAP})."
                )
            }
        )

    # Load config
    cfg = _load_config()
    default_max_iter = cfg.get("max_iterations", DEFAULT_MAX_ITERATIONS)
    # Model-supplied max_iterations is ignored — the config value is authoritative
    # so users get predictable budgets. The kwarg is retained for internal callers
    # and tests; a model-emitted value here would only shrink the budget and
    # surprise the user mid-run. Log and drop it if one slips through from a
    # cached tool schema or a stale provider.
    if max_iterations is not None and max_iterations != default_max_iter:
        logger.debug(
            "delegate_task: ignoring caller-supplied max_iterations=%s; "
            "using delegation.max_iterations=%s from config",
            max_iterations, default_max_iter,
        )
    effective_max_iter = default_max_iter

    # Resolve delegation credentials (provider:model pair).
    # When delegation.provider is configured, this resolves the full credential
    # bundle (base_url, api_key, api_mode) via the same runtime provider system
    # used by CLI/gateway startup.  When unconfigured, returns None values so
    # children inherit from the parent.
    try:
        creds = _resolve_delegation_credentials(cfg, parent_agent)
    except ValueError as exc:
        return tool_error(str(exc))

    # Normalize to task list
    max_children = _get_max_concurrent_children()
    recovered_tasks, tasks_error = _recover_tasks_from_json_string(tasks)
    if tasks_error:
        return tool_error(tasks_error)
    if recovered_tasks is not None:
        tasks = recovered_tasks

    if tasks and isinstance(tasks, list):
        if len(tasks) > max_children:
            return tool_error(
                f"Too many tasks: {len(tasks)} provided, but "
                f"max_concurrent_children is {max_children}. "
                f"Either reduce the task count, split into multiple "
                f"delegate_task calls, or increase "
                f"delegation.max_concurrent_children in config.yaml."
            )
        task_list = tasks
    elif goal and isinstance(goal, str) and goal.strip():
        task_list = [
            {"goal": goal, "name": name, "context": context, "toolsets": toolsets, "role": top_role}
        ]
    else:
        return tool_error("Provide either 'goal' (single task) or 'tasks' (batch).")

    if not task_list:
        return tool_error("No tasks provided.")

    # Validate each task has a goal
    for i, task in enumerate(task_list):
        if not isinstance(task, dict):
            return tool_error(
                f"Task {i} must be an object, got {type(task).__name__}."
            )
        if not task.get("goal", "").strip():
            return tool_error(f"Task {i} is missing a 'goal'.")

    overall_start = time.monotonic()

    n_tasks = len(task_list)
    # Save parent tool names BEFORE any child construction mutates the legacy
    # model_tools global.  The agent's exact loaded surface is authoritative;
    # consulting an already-loaded compatibility module is only a fallback.
    # Deliberately do not cold-import model_tools on the async return path.
    import sys

    _parent_valid_tools = getattr(parent_agent, "valid_tool_names", None)
    if isinstance(_parent_valid_tools, (set, frozenset, list, tuple)):
        _parent_tool_names = [str(name) for name in _parent_valid_tools]
    else:
        _loaded_model_tools = sys.modules.get("model_tools")
        _parent_tool_names = list(
            getattr(_loaded_model_tools, "_last_resolved_tool_names", ()) or ()
        )

    # Build all child agents on the main thread (thread-safe construction)
    # Wrapped in try/finally so the global is always restored even if a
    # child build raises (otherwise _last_resolved_tool_names stays corrupted).
    children = []
    try:
        for i, t in enumerate(task_list):
            task_acp_args = t.get("acp_args") if "acp_args" in t else None
            # Per-task role beats top-level; normalise again so unknown
            # per-task values warn and degrade to leaf uniformly.
            effective_role = _normalize_role(t.get("role") or top_role)
            task_agent_name = _resolve_task_agent_name(
                t,
                fallback_goal=goal,
                fallback_context=context,
                fallback_toolsets=toolsets,
                task_index=i,
            )
            child = _build_child_agent(
                task_index=i,
                goal=t["goal"],
                context=t.get("context"),
                toolsets=t.get("toolsets") or toolsets,
                model=creds["model"],
                max_iterations=effective_max_iter,
                task_count=n_tasks,
                parent_agent=parent_agent,
                override_provider=creds["provider"],
                override_base_url=creds["base_url"],
                override_api_key=creds["api_key"],
                override_api_mode=creds["api_mode"],
                override_acp_command=t.get("acp_command")
                or acp_command
                or creds.get("command"),
                override_acp_args=(
                    task_acp_args
                    if task_acp_args is not None
                    else (acp_args if acp_args is not None else creds.get("args"))
                ),
                role=effective_role,
                delegate_call_id=normalized_delegate_call_id,
                agent_name=task_agent_name,
            )
            # Override with correct parent tool names (before child construction mutated global)
            child._delegate_saved_tool_names = _parent_tool_names
            children.append((i, t, child))
    finally:
        # Authoritative restore for legacy consumers after all children build.
        # A real child construction imports model_tools through AIAgent; mocked
        # lightweight async tests need not import that heavyweight module.
        _loaded_model_tools = sys.modules.get("model_tools")
        if _loaded_model_tools is not None:
            _loaded_model_tools._last_resolved_tool_names = _parent_tool_names

    from hermes_team_mission.domain.run_context import RunContext

    parent_context = None
    if hasattr(parent_agent, "_active_run_context"):
        try:
            parent_context = parent_agent._active_run_context()
        except Exception:
            logger.debug("parent run-context lookup failed", exc_info=True)
    if not isinstance(parent_context, RunContext):
        parent_context = getattr(parent_agent, "run_context", None) or getattr(
            parent_agent,
            "_run_context",
            None,
        )
    if not isinstance(parent_context, RunContext):
        parent_context = None
    conversation_session_id = str(
        getattr(parent_context, "conversation_session_id", "")
        or getattr(parent_agent, "gateway_session_key", "")
        or getattr(parent_agent, "session_id", "")
    ).strip()
    parent_activity_id = str(
        getattr(parent_context, "activity_id", "") or ""
    ).strip()
    execution_scope_key = str(
        getattr(parent_context, "execution_scope_key", "")
        or getattr(parent_agent, "runtime_scope_key", "")
        or conversation_session_id
    ).strip()
    service = SubagentExecutionService(
        state_store=getattr(parent_agent, "_session_db", None),
        conversation_session_id=conversation_session_id,
        parent_activity_id=parent_activity_id,
        execution_scope_key=execution_scope_key,
        participant_id=str(getattr(parent_context, "participant_id", "") or ""),
        profile_id=str(getattr(parent_context, "profile_id", "") or ""),
    )
    specs = [
        SubagentTaskSpec(
            task_index=index,
            goal=task["goal"],
            child_session_id=str(getattr(child, "session_id", "") or ""),
            subagent_id=str(getattr(child, "_subagent_id", "") or ""),
            role=str(getattr(child, "_delegate_role", "leaf") or "leaf"),
            model=str(getattr(child, "model", "") or ""),
            toolsets=tuple(getattr(child, "_subagent_toolsets", ()) or ()),
        )
        for index, task, child in children
    ]
    try:
        plan = service.create_plan(specs, mode=resolved_mode)
    except (PersistenceRequiredError, ValueError) as exc:
        _interrupt_prebuilt_children(children, str(exc))
        for _index, _task, child in children:
            try:
                child.close()
            except Exception:
                logger.debug("rejected subagent close failed", exc_info=True)
        return tool_error(str(exc))

    if parent_context is not None:
        try:
            from dataclasses import replace as dataclass_replace

            for execution, (_index, _task, child) in zip(plan.children, children):
                child_context = dataclass_replace(
                    parent_context,
                    activity_id=execution.activity_id,
                    activity_kind="agent_dispatch",
                    execution_scope_key=plan.execution_scope_key,
                    execution_session_id=execution.child_session_id,
                    node_id=str(getattr(child, "_subagent_id", "") or ""),
                )
                child.run_context = child_context
                child._run_context = child_context
        except Exception:
            logger.exception("subagent RunContext propagation failed")
            service.cancel(plan, reason="subagent RunContext propagation failed")
            _interrupt_prebuilt_children(children, "RunContext propagation failed")
            return tool_error("Subagent execution identity could not be established.")

    def on_child_result(entry: Dict[str, Any]) -> None:
        service.complete_child(
            plan,
            task_index=int(entry.get("task_index", 0)),
            result=entry,
        )

    def execute_and_finalize(*, async_mode: bool) -> Dict[str, Any]:
        results = _execute_prebuilt_children(
            children=children,
            task_list=task_list,
            parent_agent=parent_agent,
            max_children=max_children,
            async_mode=async_mode,
            on_child_result=on_child_result,
        )
        return _finalize_delegation_results(
            results=results,
            task_list=task_list,
            children=children,
            parent_agent=parent_agent,
            overall_start=overall_start,
        )

    if resolved_mode is ExecutionMode.SYNC:
        service.start(plan)
        combined = execute_and_finalize(async_mode=False)
        service.complete(plan, combined)
        return json.dumps(combined, ensure_ascii=False)

    _detach_async_children(parent_agent, children)

    def async_runner() -> Dict[str, Any]:
        return execute_and_finalize(async_mode=True)

    dispatch = subagent_execution_runtime.submit(
        plan=plan,
        service=service,
        runner=async_runner,
        interrupt_fn=lambda: _interrupt_prebuilt_children(
            children,
            "Async delegation cancelled",
        ),
        max_workers=_get_max_async_children(),
        interrupt_child_fn=lambda task_index: _interrupt_prebuilt_child(
            children,
            task_index,
            "Async child delegation cancelled",
        ),
    )
    if dispatch.get("status") != "running":
        service.cancel(plan, reason=str(dispatch.get("error") or "schedule rejected"))
        _interrupt_prebuilt_children(children, "Async delegation schedule rejected")
        for _index, _task, child in children:
            try:
                child.close()
            except Exception:
                logger.debug("rejected async child close failed", exc_info=True)
        return tool_error(
            str(dispatch.get("error") or "Async delegation could not be scheduled.")
        )
    dispatch["goals"] = [task["goal"] for task in task_list]
    dispatch["note"] = (
        "Subagent execution continues independently. Observe or cancel it by "
        "Activity; completion is a typed internal event, not a conversation message."
    )
    return json.dumps(dispatch, ensure_ascii=False)


def _load_config() -> dict:
    """Load delegation config from CLI_CONFIG or persistent config.

    Checks the runtime config (cli.py CLI_CONFIG) first, then falls back
    to the persistent config (hermes_cli/config.py load_config()) so that
    ``delegation.model`` / ``delegation.provider`` are picked up regardless
    of the entry point (CLI, gateway, cron).
    """
    try:
        from cli import CLI_CONFIG

        cfg = CLI_CONFIG.get("delegation") or {}
        if cfg:
            return cfg
    except Exception:
        pass
    try:
        from hermes_cli.config import load_config

        full = load_config()
        return full.get("delegation") or {}
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# OpenAI Function-Calling Schema
# ---------------------------------------------------------------------------


from tools.delegation_schema import (
    DELEGATE_TASK_SCHEMA,
    _build_dynamic_schema_overrides,
    _build_role_param_description,
    _build_tasks_param_description,
    _build_top_level_description,
)


# --- Registry ---
from tools.registry import registry, tool_error

registry.register(
    name="delegate_task",
    toolset="delegation",
    schema=DELEGATE_TASK_SCHEMA,
    handler=lambda args, **kw: delegate_task(
        goal=args.get("goal"),
        name=args.get("name"),
        context=args.get("context"),
        toolsets=args.get("toolsets"),
        tasks=args.get("tasks"),
        max_iterations=args.get("max_iterations"),
        acp_command=args.get("acp_command"),
        acp_args=args.get("acp_args"),
        role=args.get("role"),
        execution_mode=args.get("execution_mode"),
        background=args.get("background"),
        parent_agent=kw.get("parent_agent"),
        delegate_call_id=kw.get("tool_call_id") or kw.get("delegate_call_id"),
    ),
    check_fn=check_delegate_requirements,
    emoji="🔀",
    dynamic_schema_overrides=_build_dynamic_schema_overrides,
)
