"""Dynamic model-facing schema for delegate_task."""

from __future__ import annotations

from tools import delegate_tool as _runtime

_DEFAULT_MAX_CONCURRENT_CHILDREN = _runtime._DEFAULT_MAX_CONCURRENT_CHILDREN
MAX_DEPTH = _runtime.MAX_DEPTH
_SUBAGENT_NAME_DESCRIPTION = _runtime._SUBAGENT_NAME_DESCRIPTION
_TOOLSET_LIST_STR = _runtime._TOOLSET_LIST_STR
_get_max_concurrent_children = _runtime._get_max_concurrent_children
_get_max_spawn_depth = _runtime._get_max_spawn_depth
_get_orchestrator_enabled = _runtime._get_orchestrator_enabled

def _build_top_level_description() -> str:
    """Compose the delegate_task tool description with current runtime limits.

    The model needs to know its actual ceilings (not the framework defaults),
    otherwise it self-caps at "default 3" / "default 2" even when the user has
    raised delegation.max_concurrent_children / max_spawn_depth. Called both
    at module import (to seed DELEGATE_TASK_SCHEMA) and on every
    get_definitions() call via dynamic_schema_overrides.
    """
    try:
        max_children = _get_max_concurrent_children()
    except Exception:
        max_children = _DEFAULT_MAX_CONCURRENT_CHILDREN
    try:
        max_depth = _get_max_spawn_depth()
    except Exception:
        max_depth = MAX_DEPTH
    try:
        orchestrator_on = _get_orchestrator_enabled()
    except Exception:
        orchestrator_on = True

    if max_depth >= 2 and orchestrator_on:
        nesting_clause = (
            f"Nested delegation IS enabled for this user "
            f"(max_spawn_depth={max_depth}): pass role='orchestrator' on a "
            f"child to let it spawn its own workers, up to {max_depth - 1} "
            f"additional level(s) deep."
        )
    elif max_depth >= 2 and not orchestrator_on:
        nesting_clause = (
            f"Nested delegation is DISABLED on this install "
            f"(delegation.orchestrator_enabled=false), even though "
            f"max_spawn_depth={max_depth}. role='orchestrator' is silently "
            f"forced to 'leaf'."
        )
    else:
        nesting_clause = (
            f"Nested delegation is OFF for this user "
            f"(max_spawn_depth={max_depth}): every child is a leaf and "
            f"cannot delegate further. Raise delegation.max_spawn_depth in "
            f"config.yaml to enable nesting."
        )

    return (
        "Spawn one or more subagents to work on tasks in isolated contexts. "
        "Each subagent gets its own conversation, terminal session, and toolset. "
        "Only the final summary is returned -- intermediate tool results "
        "never enter your context window.\n\n"
        "TWO MODES (one of 'goal' or 'tasks' is required):\n"
        "1. Single task: provide 'goal' (+ optional name, context, toolsets)\n"
        f"2. Batch (parallel): provide 'tasks' array with up to {max_children} "
        f"items concurrently for this user (configured via "
        f"delegation.max_concurrent_children in config.yaml). "
        f"All run in parallel and results are returned together. {nesting_clause}\n\n"
        "WHEN TO USE delegate_task:\n"
        "- Reasoning-heavy subtasks (debugging, code review, research synthesis)\n"
        "- Tasks that would flood your context with intermediate data\n"
        "- Parallel independent workstreams (research A and B simultaneously)\n\n"
        "WHEN NOT TO USE (use these instead):\n"
        "- Mechanical multi-step work with no reasoning needed -> use execute_code\n"
        "- Single tool call -> just call the tool directly\n"
        "- Tasks needing user interaction -> subagents cannot use clarify\n"
        "- Durable long-running work that must outlive the current turn -> "
        "use cronjob (action='create') or terminal(background=True, "
        "notify_on_complete=True) instead. delegate_task runs SYNCHRONOUSLY "
        "inside the parent turn: if the parent is interrupted (user sends a "
        "new message, /stop, /new) the child is cancelled with status="
        "'interrupted' and its work is discarded. Children cannot continue "
        "in the background.\n\n"
        "IMPORTANT:\n"
        "- Subagents have NO memory of your conversation. Pass all relevant "
        "info (file paths, error messages, constraints) via the 'context' field.\n"
        "- Give every subagent a concise human-readable 'name' when possible "
        "(for example '目录巡检员', '时间校准员', '计算核对员'). The name is "
        "shown in the UI and should be a short role label, not the full task.\n"
        "- If the user is writing in a non-English language, or asked for "
        "output in a specific language / tone / style, say so in 'context' "
        "(e.g. \"respond in Chinese\", \"return output in Japanese\"). "
        "Otherwise subagents default to English and their summaries will "
        "contaminate your final reply with the wrong language.\n"
        "- Subagent summaries are SELF-REPORTS, not verified facts. A subagent "
        "that claims \"uploaded successfully\" or \"file written\" may be wrong. "
        "For operations with external side-effects (HTTP POST/PUT, remote "
        "writes, file creation at shared paths, publishing), require the "
        "subagent to return a verifiable handle (URL, ID, absolute path, HTTP "
        "status) and verify it yourself — fetch the URL, stat the file, read "
        "back the content — before telling the user the operation succeeded.\n"
        "- Leaf subagents (role='leaf', the default) CANNOT call: "
        "delegate_task, clarify, memory, send_message, execute_code.\n"
        "- Orchestrator subagents (role='orchestrator') retain "
        "delegate_task so they can spawn their own workers, but still "
        "cannot use clarify, memory, send_message, or execute_code. "
        f"Orchestrators are bounded by max_spawn_depth={max_depth} for this "
        f"user and can be disabled globally via "
        "delegation.orchestrator_enabled=false.\n"
        "- Each subagent gets its own terminal session (separate working directory and state).\n"
        "- Results are always returned as an array, one entry per task."
    )


def _build_tasks_param_description() -> str:
    """Compose the 'tasks' parameter description with current concurrency limit."""
    try:
        max_children = _get_max_concurrent_children()
    except Exception:
        max_children = _DEFAULT_MAX_CONCURRENT_CHILDREN
    return (
        f"Batch mode: tasks to run in parallel (up to {max_children} for this "
        f"user, set via delegation.max_concurrent_children). Each gets "
        "its own subagent with isolated context and terminal session. "
        "When provided, top-level goal/name/context/toolsets are ignored."
    )


def _build_role_param_description() -> str:
    """Compose the 'role' parameter description with current spawn-depth limit."""
    try:
        max_depth = _get_max_spawn_depth()
    except Exception:
        max_depth = MAX_DEPTH
    try:
        orchestrator_on = _get_orchestrator_enabled()
    except Exception:
        orchestrator_on = True

    if max_depth >= 2 and orchestrator_on:
        nesting_note = (
            f"Nesting IS enabled for this user (max_spawn_depth={max_depth}): "
            f"orchestrator children can themselves delegate up to {max_depth - 1} "
            "more level(s) deep."
        )
    elif max_depth >= 2 and not orchestrator_on:
        nesting_note = (
            "Nesting is currently disabled "
            "(delegation.orchestrator_enabled=false); 'orchestrator' is "
            "silently forced to 'leaf'."
        )
    else:
        nesting_note = (
            f"Nesting is OFF for this user (max_spawn_depth={max_depth}); "
            "'orchestrator' is silently forced to 'leaf'. Raise "
            "delegation.max_spawn_depth in config.yaml to enable."
        )

    return (
        "Role of the child agent. 'leaf' (default) = focused "
        "worker, cannot delegate further. 'orchestrator' = can "
        f"use delegate_task to spawn its own workers. {nesting_note}"
    )


def _build_dynamic_schema_overrides() -> dict:
    """Return per-call schema overrides reflecting current config.

    Plugged into ToolEntry.dynamic_schema_overrides so every
    get_definitions() pass rewrites the description fields to the user's
    actual limits.
    """
    overrides_params = {
        **DELEGATE_TASK_SCHEMA["parameters"],
    }
    # Deep-copy properties so we don't mutate the static schema dict.
    overrides_params["properties"] = {
        k: dict(v) for k, v in DELEGATE_TASK_SCHEMA["parameters"]["properties"].items()
    }
    overrides_params["properties"]["tasks"]["description"] = _build_tasks_param_description()
    overrides_params["properties"]["role"]["description"] = _build_role_param_description()
    return {
        "description": _build_top_level_description(),
        "parameters": overrides_params,
    }


DELEGATE_TASK_SCHEMA = {
    "name": "delegate_task",
    # NOTE: description / tasks.description / role.description are placeholder
    # values. The real text is generated per get_definitions() call by
    # _build_dynamic_schema_overrides() (registered via
    # dynamic_schema_overrides below) so the model sees the user's actual
    # delegation.max_concurrent_children / max_spawn_depth, not the framework
    # defaults. Building these lazily (instead of at module import) also
    # avoids forcing cli.CLI_CONFIG to load before the test conftest can
    # redirect HERMES_HOME.
    "description": (
        "Spawn one or more subagents in isolated contexts. "
        "Description is rebuilt at every get_definitions() call to reflect "
        "the user's current delegation limits."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "goal": {
                "type": "string",
                "description": (
                    "What the subagent should accomplish. Be specific and "
                    "self-contained -- the subagent knows nothing about your "
                    "conversation history."
                ),
            },
            "name": {
                "type": "string",
                "description": _SUBAGENT_NAME_DESCRIPTION,
            },
            "context": {
                "type": "string",
                "description": (
                    "Background information the subagent needs: file paths, "
                    "error messages, project structure, constraints. The more "
                    "specific you are, the better the subagent performs."
                ),
            },
            "toolsets": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Toolsets to enable for this subagent. "
                    "Default: inherits your enabled toolsets. "
                    f"Available toolsets: {_TOOLSET_LIST_STR}. "
                    "Common patterns: ['terminal', 'file'] for code work, "
                    "['web'] for research, ['browser'] for web interaction, "
                    "['terminal', 'file', 'web'] for full-stack tasks."
                ),
            },
            "tasks": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "goal": {"type": "string", "description": "Task goal"},
                        "name": {
                            "type": "string",
                            "description": _SUBAGENT_NAME_DESCRIPTION,
                        },
                        "context": {
                            "type": "string",
                            "description": "Task-specific context",
                        },
                        "toolsets": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": f"Toolsets for this specific task. Available: {_TOOLSET_LIST_STR}. Use 'web' for network access, 'terminal' for shell, 'browser' for web interaction.",
                        },
                        "acp_command": {
                            "type": "string",
                            "description": (
                                "Per-task ACP command override (e.g. 'copilot'). "
                                "Overrides the top-level acp_command for this task only. "
                                "Do NOT set unless the user explicitly told you an ACP CLI is installed."
                            ),
                        },
                        "acp_args": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Per-task ACP args override. Leave empty unless acp_command is set.",
                        },
                        "role": {
                            "type": "string",
                            "enum": ["leaf", "orchestrator"],
                            "description": "Per-task role override. See top-level 'role' for semantics.",
                        },
                    },
                    "required": ["goal"],
                },
                # No maxItems — the runtime limit is configurable via
                # delegation.max_concurrent_children (default 3) and
                # enforced with a clear error in delegate_task().
                "description": "(rebuilt at get_definitions() time)",
            },
            "role": {
                "type": "string",
                "enum": ["leaf", "orchestrator"],
                "description": "(rebuilt at get_definitions() time)",
            },
            "execution_mode": {
                "type": "string",
                "enum": ["sync", "async"],
                "description": (
                    "Execution lifecycle for this delegation. 'sync' waits and "
                    "returns results in the current tool call. 'async' persists "
                    "Activity/Run state and returns an Activity handle immediately."
                ),
            },
            "background": {
                "type": "boolean",
                "description": (
                    "Deprecated compatibility alias for execution_mode: true "
                    "means 'async' and false means 'sync'. If both fields are "
                    "provided they must agree. Async batches are supported and "
                    "return one parent Activity plus ordered child Activities."
                ),
            },
            "acp_command": {
                "type": "string",
                "description": (
                    "Override ACP command for child agents (e.g. 'copilot'). "
                    "When set, children use ACP subprocess transport instead of inheriting "
                    "the parent's transport. Requires an ACP-compatible CLI "
                    "(currently GitHub Copilot CLI via 'copilot --acp --stdio'). "
                    "See agent/copilot_acp_client.py for the implementation. "
                    "IMPORTANT: Do NOT set this unless the user has explicitly told you "
                    "a specific ACP-compatible CLI is installed and configured. "
                    "Leave empty to use the parent's default transport (Hermes subagents)."
                ),
            },
            "acp_args": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Arguments for the ACP command (default: ['--acp', '--stdio']). "
                    "Only used when acp_command is set. "
                    "Leave empty unless acp_command is explicitly provided."
                ),
            },
        },
        "required": [],
    },
}
