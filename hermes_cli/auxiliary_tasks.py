"""Canonical namespace for Hermes-owned auxiliary LLM tasks.

The configurable task catalog is shared by the CLI model picker, dashboard,
and plugin registration boundary.  Runtime-only task names remain reserved so
plugins cannot silently change the behavior of built-in one-shot or MoA calls.
"""

from __future__ import annotations


# (task_key, display_name, short_description)
CONFIGURABLE_AUXILIARY_TASKS: tuple[tuple[str, str, str], ...] = (
    ("vision", "Vision", "image/screenshot analysis"),
    ("compression", "Compression", "context summarization"),
    ("web_extract", "Web extract", "web page summarization"),
    ("approval", "Approval", "smart command approval"),
    ("mcp", "MCP", "MCP tool reasoning"),
    ("memory_query_rewrite", "Memory query rewrite", "memory retrieval queries"),
    ("tts_audio_tags", "TTS audio tags", "Gemini TTS tag insertion"),
    ("skills_hub", "Skills hub", "skills search/install"),
    ("triage_specifier", "Triage specifier", "kanban spec fleshing"),
    ("kanban_decomposer", "Kanban decomposer", "task decomposition"),
    ("profile_describer", "Profile describer", "auto profile descriptions"),
    ("goal_judge", "Goal judge", "goal completion and contract review"),
    ("curator", "Curator", "skill-usage review pass"),
    ("background_review", "Background review", "memory/skill learning review"),
    ("monitor", "Monitor", "automation urgency classification"),
)

CONFIGURABLE_AUXILIARY_TASK_KEYS = tuple(
    task_key for task_key, _display_name, _description in CONFIGURABLE_AUXILIARY_TASKS
)

# These are Hermes-owned call-routing identities, but intentionally have no
# user-facing auxiliary configuration slot.  ``title_generation`` is also the
# default identity for generic one-shot helpers; the MoA identities participate
# in separate aggregation accounting and routing.
RUNTIME_ONLY_AUXILIARY_TASK_KEYS = frozenset(
    {
        "title_generation",
        "moa_reference",
        "moa_aggregator",
    }
)

RESERVED_AUXILIARY_TASK_KEYS = (
    frozenset(CONFIGURABLE_AUXILIARY_TASK_KEYS)
    | RUNTIME_ONLY_AUXILIARY_TASK_KEYS
)
