"""Background memory/skill review — fork the agent to evaluate the turn.

After every turn, ``AIAgent.run_conversation`` may call
:func:`spawn_background_review` to fire off a daemon thread that replays
the conversation snapshot in a forked :class:`AIAgent` and asks itself
"should any skill/memory be saved or updated?".  Writes go straight to
the memory + skill stores.  Main conversation and prompt cache are never
touched.

By default the fork inherits the parent's live runtime and warm prompt cache.
``auxiliary.background_review`` may route it to a different model; routed
reviews get a bounded history digest and never inherit model-specific prompt
or reasoning state.  The fork runs with a memory/skill-only whitelist and is
isolated from foreground transcript/session persistence.

See the ``hermes-agent-dev`` skill (``references/self-improvement-loop.md``)
for invariants and PR review criteria.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, List, Optional

from agent.thread_scoped_output import thread_scoped_silence

logger = logging.getLogger(__name__)


def _parent_review_runtime(agent: Any) -> Dict[str, Any]:
    runtime = agent._current_main_runtime()
    api_mode = runtime.get("api_mode") or None
    if api_mode == "codex_app_server":
        api_mode = "codex_responses"
    return {
        "provider": agent.provider,
        "model": agent.model,
        "api_key": runtime.get("api_key") or None,
        "base_url": runtime.get("base_url") or None,
        "api_mode": api_mode,
        "credential_pool": getattr(agent, "_credential_pool", None),
        "request_overrides": dict(getattr(agent, "request_overrides", {}) or {}),
        "max_tokens": getattr(agent, "max_tokens", None),
        "command": getattr(agent, "acp_command", None),
        "args": list(getattr(agent, "acp_args", []) or []),
        "routed": False,
    }


def _resolve_review_runtime(agent: Any) -> Dict[str, Any]:
    """Select the background-review runtime without changing the default path.

    ``auto`` and same provider/model retain the parent's warm-cache runtime.
    Only an explicit different provider+model is a routed cold-cache review.
    Any config or credential failure falls back to the parent so review remains
    a best-effort sidecar and cannot fail the foreground turn.
    """
    parent = _parent_review_runtime(agent)
    try:
        from hermes_cli.config import load_config

        config = load_config()
        auxiliary = config.get("auxiliary") if isinstance(config, dict) else {}
        auxiliary = auxiliary if isinstance(auxiliary, dict) else {}
        task = auxiliary.get("background_review")
        task = task if isinstance(task, dict) else {}
        provider = str(task.get("provider") or "").strip()
        model = str(task.get("model") or "").strip()
        base_url = str(task.get("base_url") or "").strip() or None
        api_key = str(task.get("api_key") or "").strip() or None
        if not provider or provider == "auto" or not model:
            return parent
        if provider == str(agent.provider or "") and model == str(agent.model or ""):
            return parent

        from hermes_cli.runtime_provider import resolve_runtime_provider

        resolved = resolve_runtime_provider(
            requested=provider,
            target_model=model,
            explicit_api_key=api_key,
            explicit_base_url=base_url,
        )
        if not isinstance(resolved, dict) or not resolved:
            raise RuntimeError(f"unable to resolve auxiliary provider {provider!r}")
        request_overrides = dict(resolved.get("request_overrides") or {})
        extra_body = task.get("extra_body")
        if isinstance(extra_body, dict):
            merged_extra_body = dict(request_overrides.get("extra_body") or {})
            merged_extra_body.update(extra_body)
            if merged_extra_body:
                request_overrides["extra_body"] = merged_extra_body
        return {
            "provider": resolved.get("provider") or provider,
            "model": resolved.get("model") or model,
            "api_key": resolved.get("api_key"),
            "base_url": resolved.get("base_url"),
            "api_mode": resolved.get("api_mode"),
            "credential_pool": resolved.get("credential_pool"),
            "request_overrides": request_overrides,
            "max_tokens": resolved.get("max_output_tokens"),
            "command": resolved.get("command"),
            "args": list(resolved.get("args") or []),
            "routed": True,
        }
    except Exception as exc:
        logger.debug(
            "background-review auxiliary routing failed (%s); using main model",
            exc,
        )
        return parent


def _message_text(message: Dict[str, Any]) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        return " ".join(
            str(block.get("text") or "")
            for block in content
            if isinstance(block, dict)
        ).strip()
    return ""


def _digest_history(messages_snapshot: List[Dict], tail: int = 24) -> List[Dict]:
    """Bound cold-written context only for a different-model review fork."""
    messages = list(messages_snapshot or [])
    tail = max(1, int(tail or 24))
    keep = messages[-tail:]
    while keep and isinstance(keep[0], dict) and keep[0].get("role") == "tool":
        tail += 1
        if len(messages) <= tail:
            keep = messages
            while (
                keep
                and isinstance(keep[0], dict)
                and keep[0].get("role") == "tool"
            ):
                keep = keep[1:]
            return keep
        keep = messages[-tail:]
    if len(messages) <= tail:
        return messages

    lines: List[str] = []
    for message in messages[: -len(keep)]:
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or "")
        text = _message_text(message).replace("\n", " ")
        if role == "user" and text:
            lines.append(f"USER: {text[:300]}")
        elif role == "assistant":
            tool_calls = message.get("tool_calls") or []
            names = [
                str((call.get("function") or {}).get("name") or "?")
                for call in tool_calls
                if isinstance(call, dict)
            ]
            if names:
                lines.append(f"ASSISTANT[tools: {', '.join(names)}]")
            if text:
                lines.append(f"ASSISTANT: {text[:200]}")
    digest = {
        "role": "user",
        "content": (
            "[Earlier conversation digest. Older turns were summarized to bound "
            "the routed background review's cold-write cost; recent turns follow "
            "verbatim.]\n" + "\n".join(lines)
        ),
    }
    return [digest, *keep]


# Review-prompt strings — used by ``spawn_background_review_thread`` to build
# the user-message that the forked review agent receives.  AIAgent exposes
# them as class attributes (``_MEMORY_REVIEW_PROMPT`` etc.) for back-compat;
# the actual text lives here so future edits are one-place.
_MEMORY_REVIEW_PROMPT = (
    "Review the conversation above and consider saving to memory if appropriate.\n\n"
    "Focus on:\n"
    "1. Has the user revealed things about themselves — their persona, desires, "
    "preferences, or personal details worth remembering?\n"
    "2. Has the user expressed expectations about how you should behave, their work "
    "style, or ways they want you to operate?\n\n"
    "If something stands out, save it using the memory tool. "
    "If nothing is worth saving, just say 'Nothing to save.' and stop."
)

_SKILL_REVIEW_PROMPT = (
    "Review the conversation above and update the skill library. Be "
    "ACTIVE — most sessions produce at least one skill update, even if "
    "small. A pass that does nothing is a missed learning opportunity, "
    "not a neutral outcome.\n\n"
    "Target shape of the library: CLASS-LEVEL skills, each with a rich "
    "SKILL.md and a `references/` directory for session-specific detail. "
    "Not a long flat list of narrow one-session-one-skill entries. This "
    "shapes HOW you update, not WHETHER you update.\n\n"
    "Signals to look for (any one of these warrants action):\n"
    "  • User corrected your style, tone, format, legibility, or "
    "verbosity. Frustration signals like 'stop doing X', 'this is too "
    "verbose', 'don't format like this', 'why are you explaining', "
    "'just give me the answer', 'you always do Y and I hate it', or an "
    "explicit 'remember this' are FIRST-CLASS skill signals, not just "
    "memory signals. Update the relevant skill(s) to embed the "
    "preference so the next session starts already knowing.\n"
    "  • User corrected your workflow, approach, or sequence of steps. "
    "Encode the correction as a pitfall or explicit step in the skill "
    "that governs that class of task.\n"
    "  • Non-trivial technique, fix, workaround, debugging path, or "
    "tool-usage pattern emerged that a future session would benefit "
    "from. Capture it.\n"
    "  • A skill that got loaded or consulted this session turned out "
    "to be wrong, missing a step, or outdated. Patch it NOW.\n\n"
    "Preference order — prefer the earliest action that fits, but do "
    "pick one when a signal above fired:\n"
    "  1. UPDATE A CURRENTLY-LOADED SKILL. Look back through the "
    "conversation for skills the user loaded via /skill-name or you "
    "read via skill_view. If any of them covers the territory of the "
    "new learning, PATCH that one first. It is the skill that was in "
    "play, so it's the right one to extend.\n"
    "  2. UPDATE AN EXISTING UMBRELLA (via skills_list + skill_view). "
    "If no loaded skill fits but an existing class-level skill does, "
    "patch it. Add a subsection, a pitfall, or broaden a trigger.\n"
    "  3. ADD A SUPPORT FILE under an existing umbrella. Skills can be "
    "packaged with three kinds of support files — use the right "
    "directory per kind:\n"
    "     • `references/<topic>.md` — session-specific detail (error "
    "transcripts, reproduction recipes, provider quirks) AND "
    "condensed knowledge banks: quoted research, API docs, external "
    "authoritative excerpts, or domain notes you found while working "
    "on the problem. Write it concise and for the value of the task, "
    "not as a full mirror of upstream docs.\n"
    "     • `templates/<name>.<ext>` — starter files meant to be "
    "copied and modified (boilerplate configs, scaffolding, a "
    "known-good example the agent can `reproduce with modifications`).\n"
    "     • `scripts/<name>.<ext>` — statically re-runnable actions "
    "the skill can invoke directly (verification scripts, fixture "
    "generators, deterministic probes, anything the agent should run "
    "rather than hand-type each time).\n"
    "     Add support files via skill_manage action=write_file with "
    "file_path starting 'references/', 'templates/', or 'scripts/'. "
    "The umbrella's SKILL.md should gain a one-line pointer to any "
    "new support file so future agents know it exists.\n"
    "  4. CREATE A NEW CLASS-LEVEL UMBRELLA SKILL when no existing "
    "skill covers the class. The name MUST be at the class level. "
    "The name MUST NOT be a specific PR number, error string, feature "
    "codename, library-alone name, or 'fix-X / debug-Y / audit-Z-today' "
    "session artifact. If the proposed name only makes sense for "
    "today's task, it's wrong — fall back to (1), (2), or (3).\n\n"
    "User-preference embedding (important): when the user expressed a "
    "style/format/workflow preference, the update belongs in the "
    "SKILL.md body, not just in memory. Memory captures 'who the user "
    "is and what the current situation and state of your operations "
    "are'; skills capture 'how to do this class of task for this "
    "user'. When they complain about how you handled a task, the "
    "skill that governs that task needs to carry the lesson.\n\n"
    "If you notice two existing skills that overlap, note it in your "
    "reply — the background curator handles consolidation at scale.\n\n"
    "Protected skills (DO NOT edit these):\n"
    "  • Bundled skills (shipped with Hermes, e.g. 'hermes-agent').\n"
    "  • Hub-installed skills (installed via 'hermes skills install').\n"
    "  • Pinned skills (marked via 'hermes curator pin'). Pinned means "
    "the user has opted out of all autonomous maintenance; do not edit, "
    "delete, archive, or consolidate them.\n"
    "If the only skills that need updating are protected, say\n"
    "'Nothing to save.' and stop.\n\n"
    "Do NOT capture (these become persistent self-imposed constraints "
    "that bite you later when the environment changes):\n"
    "  • Environment-dependent failures: missing binaries, fresh-install "
    "errors, post-migration path mismatches, 'command not found', "
    "unconfigured credentials, uninstalled packages. The user can fix "
    "these — they are not durable rules.\n"
    "  • Negative claims about tools or features ('browser tools do not "
    "work', 'X tool is broken', 'cannot use Y from execute_code'). These "
    "harden into refusals the agent cites against itself for months "
    "after the actual problem was fixed.\n"
    "  • Session-specific transient errors that resolved before the "
    "conversation ended. If retrying worked, the lesson is the retry "
    "pattern, not the original failure.\n"
    "  • One-off task narratives. A user asking 'summarize today's "
    "market' or 'analyze this PR' is not a class of work that warrants "
    "a skill.\n\n"
    "If a tool failed because of setup state, capture the FIX (install "
    "command, config step, env var to set) under an existing setup or "
    "troubleshooting skill — never 'this tool does not work' as a "
    "standalone constraint.\n\n"
    "'Nothing to save.' is a real option but should NOT be the "
    "default. If the session ran smoothly with no corrections and "
    "produced no new technique, just say 'Nothing to save.' and stop. "
    "Otherwise, act."
)

_COMBINED_REVIEW_PROMPT = (
    "Review the conversation above and update two things:\n\n"
    "**Memory**: who the user is. Did the user reveal persona, "
    "desires, preferences, personal details, or expectations about "
    "how you should behave? Save facts about the user and durable "
    "preferences with the memory tool.\n\n"
    "**Skills**: how to do this class of task. Be ACTIVE — most "
    "sessions produce at least one skill update. A pass that does "
    "nothing is a missed learning opportunity, not a neutral outcome.\n\n"
    "Target shape of the skill library: CLASS-LEVEL skills with a rich "
    "SKILL.md and a `references/` directory for session-specific detail. "
    "Not a long flat list of narrow one-session-one-skill entries.\n\n"
    "Signals that warrant a skill update (any one is enough):\n"
    "  • User corrected your style, tone, format, legibility, "
    "verbosity, or approach. Frustration is a FIRST-CLASS skill "
    "signal, not just a memory signal. 'stop doing X', 'don't format "
    "like this', 'I hate when you Y' — embed the lesson in the skill "
    "that governs that task so the next session starts fixed.\n"
    "  • Non-trivial technique, fix, workaround, or debugging path "
    "emerged.\n"
    "  • A skill that was loaded or consulted turned out wrong, "
    "missing, or outdated — patch it now.\n\n"
    "Preference order for skills — pick the earliest that fits:\n"
    "  1. UPDATE A CURRENTLY-LOADED SKILL. Check what skills were "
    "loaded via /skill-name or skill_view in the conversation. If one "
    "of them covers the learning, PATCH it first. It was in play; "
    "it's the right place.\n"
    "  2. UPDATE AN EXISTING UMBRELLA (skills_list + skill_view to "
    "find the right one). Patch it.\n"
    "  3. ADD A SUPPORT FILE under an existing umbrella via "
    "skill_manage action=write_file. Three kinds: "
    "`references/<topic>.md` for session-specific detail OR condensed "
    "knowledge banks (quoted research, API docs excerpts, domain "
    "notes) written concise and task-focused; `templates/<name>.<ext>` "
    "for starter files meant to be copied and modified; "
    "`scripts/<name>.<ext>` for statically re-runnable actions "
    "(verification, fixture generators, probes). Add a one-line "
    "pointer in SKILL.md so future agents find them.\n"
    "  4. CREATE A NEW CLASS-LEVEL UMBRELLA when nothing exists. "
    "Name at the class level — NOT a PR number, error string, "
    "codename, library-alone name, or 'fix-X / debug-Y' session "
    "artifact. If the name only fits today's task, fall back to (1), "
    "(2), or (3).\n\n"
    "User-preference embedding: when the user complains about how "
    "you handled a task, update the skill that governs that task — "
    "memory alone isn't enough. Memory says 'who the user is and "
    "what the current situation and state of your operations are'; "
    "skills say 'how to do this class of task for this user'. Both "
    "should carry user-preference lessons when relevant.\n\n"
    "If you notice overlapping existing skills, mention it — the "
    "background curator handles consolidation.\n\n"
    "Protected skills (DO NOT edit these):\n"
    "  • Bundled skills (shipped with Hermes, e.g. 'hermes-agent').\n"
    "  • Hub-installed skills (installed via 'hermes skills install').\n"
    "  • Pinned skills (marked via 'hermes curator pin'). Pinned means "
    "the user has opted out of all autonomous maintenance; do not edit, "
    "delete, archive, or consolidate them.\n"
    "If the only skills that need updating are protected, say\n"
    "'Nothing to save.' and stop.\n\n"
    "Do NOT capture as skills (these become persistent self-imposed "
    "constraints that bite you later when the environment changes):\n"
    "  • Environment-dependent failures: missing binaries, fresh-install "
    "errors, post-migration path mismatches, 'command not found', "
    "unconfigured credentials, uninstalled packages. The user can fix "
    "these — they are not durable rules.\n"
    "  • Negative claims about tools or features ('browser tools do not "
    "work', 'X tool is broken', 'cannot use Y from execute_code'). These "
    "harden into refusals the agent cites against itself for months "
    "after the actual problem was fixed.\n"
    "  • Session-specific transient errors that resolved before the "
    "conversation ended. If retrying worked, the lesson is the retry "
    "pattern, not the original failure.\n"
    "  • One-off task narratives. A user asking 'summarize today's "
    "market' or 'analyze this PR' is not a class of work that warrants "
    "a skill.\n\n"
    "If a tool failed because of setup state, capture the FIX (install "
    "command, config step, env var to set) under an existing setup or "
    "troubleshooting skill — never 'this tool does not work' as a "
    "standalone constraint.\n\n"
    "Act on whichever of the two dimensions has real signal. If "
    "genuinely nothing stands out on either, say 'Nothing to save.' "
    "and stop — but don't reach for that conclusion as a default."
)



def summarize_background_review_actions(
    review_messages: List[Dict],
    prior_snapshot: List[Dict],
    notification_mode: str = "on",
) -> List[str]:
    """Build the human-facing action summary for a background review pass.

    Walks the review agent's session messages and collects "successful tool
    action" descriptions to surface to the user (e.g. "Memory updated").
    Tool messages already present in ``prior_snapshot`` are skipped so we
    don't re-surface stale results from the prior conversation that the
    review agent inherited via ``conversation_history`` (issue #14944).

    Matching is by ``tool_call_id`` when available, with a content-equality
    fallback for tool messages that lack one.
    """
    mode = str(notification_mode or "on").strip().lower()
    if mode == "off":
        return []
    verbose = mode == "verbose"

    existing_tool_call_ids = set()
    existing_tool_contents = set()
    for prior in prior_snapshot or []:
        if not isinstance(prior, dict) or prior.get("role") != "tool":
            continue
        tcid = prior.get("tool_call_id")
        if tcid:
            existing_tool_call_ids.add(tcid)
        else:
            content = prior.get("content")
            if isinstance(content, str):
                existing_tool_contents.add(content)

    # Map review-agent tool results back to the calls that produced them.  The
    # result JSON only says "Entry added"; the call arguments contain action,
    # target, and content previews.  Restricting to notify_tools also prevents
    # helper tools from surfacing as memory work just because they succeeded.
    notify_tools = {"memory", "skill_manage"}
    all_tool_call_ids: set = set()
    call_details: dict = {}
    for msg in review_messages or []:
        if not isinstance(msg, dict) or msg.get("role") != "assistant":
            continue
        for tc in msg.get("tool_calls", []) or []:
            if not isinstance(tc, dict):
                continue
            fn = tc.get("function", {}) or {}
            fn_name = fn.get("name", "")
            tcid = tc.get("id")
            if tcid:
                all_tool_call_ids.add(tcid)
            if fn_name not in notify_tools:
                continue
            try:
                args = json.loads(fn.get("arguments", "{}"))
            except (json.JSONDecodeError, TypeError):
                args = {}
            if tcid:
                call_details[tcid] = {
                    "tool": fn_name,
                    "action": args.get("action", "?"),
                    "target": args.get("target", "memory"),
                    "content": args.get("content", ""),
                    "old_text": args.get("old_text", ""),
                    "operations": args.get("operations") or [],
                    "name": args.get("name", ""),
                    "old_string": args.get("old_string", ""),
                    "new_string": args.get("new_string", ""),
                }

    actions: List[str] = []
    for msg in review_messages or []:
        if not isinstance(msg, dict) or msg.get("role") != "tool":
            continue
        tcid = msg.get("tool_call_id")
        if tcid and tcid in existing_tool_call_ids:
            continue
        if not tcid:
            content_str = msg.get("content")
            if isinstance(content_str, str) and content_str in existing_tool_contents:
                continue
        try:
            data = json.loads(msg.get("content", "{}"))
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(data, dict) or not data.get("success"):
            continue
        detail = call_details.get(tcid, {})
        if tcid in all_tool_call_ids and tcid not in call_details:
            continue
        message = str(data.get("message") or "")
        message_lower = message.lower()
        is_skill = detail.get("tool") == "skill_manage"
        target = str(data.get("target") or detail.get("target") or "memory")
        label = (
            "Memory"
            if target == "memory"
            else "User profile"
            if target == "user"
            else target
        )
        recognized = is_skill or any(
            token in message_lower
            for token in ("created", "updated", "added", "removed", "replaced", "applied", "patched")
        )
        if not recognized:
            continue

        if not verbose:
            if "created" in message_lower or "updated" in message_lower:
                actions.append(message)
            else:
                actions.append(f"{label} updated")
            continue

        action = str(detail.get("action") or "")
        content = str(detail.get("content") or "")
        old_text = str(detail.get("old_text") or "")
        skill_name = str(detail.get("name") or "")
        operations = detail.get("operations") or []
        max_preview = 120
        if is_skill:
            change = data.get("_change") if isinstance(data.get("_change"), dict) else {}
            old_string = str(change.get("old") or detail.get("old_string") or "")
            new_string = str(change.get("new") or detail.get("new_string") or "")
            description = str(change.get("description") or "")
            if action == "patch" and (old_string or new_string):
                old_preview = old_string[:80].replace("\n", " ") + ("…" if len(old_string) > 80 else "")
                new_preview = new_string[:80].replace("\n", " ") + ("…" if len(new_string) > 80 else "")
                actions.append(
                    f"📝 Skill '{skill_name}' patched: \"{old_preview}\" → \"{new_preview}\""
                )
            elif action in {"create", "edit"} and description:
                verb = "created" if action == "create" else "rewritten"
                actions.append(f"📝 Skill '{skill_name}' {verb}: {description}")
            else:
                actions.append(f"📝 {message}" if message else f"Skill {action}")
            continue

        verbose_actions: List[str] = []
        for operation in operations:
            operation = operation if isinstance(operation, dict) else {}
            op_action = str(operation.get("action") or "")
            op_content = str(operation.get("content") or "")
            op_old = str(operation.get("old_text") or "")
            if op_action in {"add", "replace"} and op_content:
                preview = op_content[:max_preview] + ("…" if len(op_content) > max_preview else "")
                icon = "➕" if op_action == "add" else "✏️"
                verbose_actions.append(f"{label} {icon} {preview}")
            elif op_action == "remove" and op_old:
                preview = op_old[:60] + ("…" if len(op_old) > 60 else "")
                verbose_actions.append(f"{label} ➖ {preview}")
        if verbose_actions:
            actions.extend(verbose_actions)
        elif action in {"add", "replace"} and content:
            preview = content[:max_preview] + ("…" if len(content) > max_preview else "")
            icon = "➕" if action == "add" else "✏️"
            actions.append(f"{label} {icon} {preview}")
        elif action == "remove" and old_text:
            preview = old_text[:60] + ("…" if len(old_text) > 60 else "")
            actions.append(f"{label} ➖ {preview}")
        else:
            actions.append(f"{label} updated")
    return actions


def build_memory_write_metadata(
    agent: Any,
    *,
    write_origin: Optional[str] = None,
    execution_context: Optional[str] = None,
    task_id: Optional[str] = None,
    tool_call_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Build provenance metadata for external memory-provider mirrors."""
    metadata: Dict[str, Any] = {
        "write_origin": write_origin or getattr(agent, "_memory_write_origin", "assistant_tool"),
        "execution_context": (
            execution_context
            or getattr(agent, "_memory_write_context", "foreground")
        ),
        "session_id": agent.session_id or "",
        "parent_session_id": agent._parent_session_id or "",
        "platform": agent.platform or os.environ.get("HERMES_SESSION_SOURCE", "cli"),
        "tool_name": "memory",
    }
    if task_id:
        metadata["task_id"] = task_id
    if tool_call_id:
        metadata["tool_call_id"] = tool_call_id
    return {k: v for k, v in metadata.items() if v not in {None, ""}}


def _run_review_in_thread(
    agent: Any,
    messages_snapshot: List[Dict],
    prompt: str,
) -> None:
    """Worker function executed in the background-review daemon thread.

    Spawns a forked ``AIAgent`` inheriting the parent's runtime, runs the
    review prompt, and surfaces a compact action summary back to the user
    via ``agent._safe_print`` and ``agent.background_review_callback``.
    """
    # Local import to avoid a hard circular dep at module load.
    from run_agent import AIAgent
    from tools.terminal_tool import set_approval_callback as _set_approval_callback

    # Install a non-interactive approval callback on this worker
    # thread so any dangerous-command guard the review agent trips
    # resolves to "deny" instead of falling back to input() -- which
    # deadlocks against the parent's prompt_toolkit TUI (#15216).
    # Same pattern as _subagent_auto_deny in tools/delegate_tool.py.
    def _bg_review_auto_deny(command, description, **kwargs):
        logger.warning(
            "Background review auto-denied dangerous command: %s (%s)",
            command, description,
        )
        return "deny"
    try:
        _set_approval_callback(_bg_review_auto_deny)
    except Exception:
        pass

    review_agent = None
    review_messages: List[Dict] = []
    try:
        # ``redirect_stdout`` mutates process-wide state and can swallow output
        # from the foreground TUI.  Route only this worker thread to devnull.
        with thread_scoped_silence():
            review_runtime = _resolve_review_runtime(agent)
            routed = bool(review_runtime.get("routed"))
            fork_kwargs: Dict[str, Any] = {}
            max_tokens = review_runtime.get("max_tokens")
            if isinstance(max_tokens, int) and max_tokens > 0:
                fork_kwargs["max_tokens"] = max_tokens
            command = review_runtime.get("command")
            if command:
                fork_kwargs["acp_command"] = command
                fork_kwargs["acp_args"] = list(review_runtime.get("args") or [])
            if not routed:
                reasoning_config = getattr(agent, "reasoning_config", None)
                if isinstance(reasoning_config, dict):
                    fork_kwargs["reasoning_config"] = dict(reasoning_config)

            # skip_memory=True prevents the fork from ingesting its harness
            # prompt into external memory providers. Built-in profile memory is
            # rebound below so explicit memory writes still reach the profile.
            review_agent = AIAgent(
                model=review_runtime.get("model") or agent.model,
                max_iterations=16,
                quiet_mode=True,
                platform=agent.platform,
                provider=review_runtime.get("provider") or agent.provider,
                api_mode=review_runtime.get("api_mode") or None,
                base_url=review_runtime.get("base_url") or None,
                api_key=review_runtime.get("api_key") or None,
                credential_pool=review_runtime.get("credential_pool"),
                request_overrides=dict(
                    review_runtime.get("request_overrides") or {}
                ),
                parent_session_id=agent.session_id,
                enabled_toolsets=getattr(agent, "enabled_toolsets", None),
                disabled_toolsets=getattr(agent, "disabled_toolsets", None),
                skip_memory=True,
                **fork_kwargs,
            )
            review_agent._memory_write_origin = "background_review"
            review_agent._memory_write_context = "background_review"
            # The review fork pins the parent's cached system prompt and keeps
            # ``tools[]`` byte-identical to the parent so its outbound request
            # hits the same provider cache prefix (see the toolset-parity note
            # above). The between-turns MCP refresh in build_turn_context would
            # add late-connecting MCP tools to this fork and break that parity,
            # so opt the review fork out of it.
            review_agent._skip_mcp_refresh = True
            review_agent._memory_store = agent._memory_store
            review_agent._memory_enabled = agent._memory_enabled
            review_agent._user_profile_enabled = agent._user_profile_enabled
            review_agent._memory_nudge_interval = 0
            review_agent._skill_nudge_interval = 0
            # The fork is a sidecar, not a second owner of the foreground
            # session.  It may write learning artifacts, but never transcript,
            # session DB, JSONL, compression lineage, or lifecycle state.
            review_agent._persist_disabled = True
            review_agent._session_db = None
            review_agent._session_json_enabled = False
            # Suppress all status/warning emits from the fork so the
            # user only sees the final successful-action summary.
            # Without this, mid-review "Iteration budget exhausted",
            # rate-limit retries, compression warnings, and other
            # lifecycle messages bubble up through _emit_status ->
            # _vprint and leak past the stdout redirect (they go via
            # _print_fn/status_callback, which bypass sys.stdout).
            review_agent.suppress_status_output = True
            # Prompt and reasoning parity are valid only for the same model.
            # A routed auxiliary model must build its own compatible prompt.
            if not routed:
                review_agent._cached_system_prompt = agent._cached_system_prompt
                review_agent.session_start = agent.session_start
            review_agent.session_id = agent.session_id
            # The fork shares the parent's live session_id (pinned above for
            # prefix-cache parity). It is single-lifecycle and calls close()
            # right after this run_conversation(); without opting out, close()
            # would finalize the parent's still-active session row mid
            # conversation (the review fires every ~10 turns). Leave session
            # finalization to the real owner (CLI close / gateway reset / cron).
            review_agent._end_session_on_close = False
            # Never let the review fork compress. It shares the parent's
            # session_id, so if it won a compression race it would rotate the
            # parent into a NEW child that the gateway never adopts (the fork
            # is single-lifecycle and dies right after this run_conversation).
            # The foreground turn would then start from the stale parent and
            # compress it again, leaving the same parent with two sibling
            # children (issue #38727). Review also needs full context to
            # produce a good memory/skill summary — compressing would strip
            # detail. Both compression triggers in conversation_loop.py gate on
            # agent.compression_enabled, so this short-circuits both paths.
            review_agent.compression_enabled = False

            from model_tools import get_tool_definitions
            from hermes_cli.plugins import (
                set_thread_tool_whitelist,
                clear_thread_tool_whitelist,
            )

            review_toolsets = ["skills"]
            if review_agent._memory_enabled or review_agent._user_profile_enabled:
                review_toolsets.insert(0, "memory")
            review_whitelist = {
                t["function"]["name"]
                for t in get_tool_definitions(
                    enabled_toolsets=review_toolsets,
                    quiet_mode=True,
                )
            }
            set_thread_tool_whitelist(
                review_whitelist,
                deny_msg_fmt=(
                    "Background review denied non-whitelisted tool: "
                    "{tool_name}. Only memory/skill tools are allowed."
                ),
            )
            try:
                from tools.skill_manager_tool import (
                    _reset_background_review_read_marks,
                )

                _reset_background_review_read_marks()
            except Exception:
                logger.debug(
                    "background-review read-mark reset failed",
                    exc_info=True,
                )
            try:
                review_agent.run_conversation(
                    user_message=(
                        prompt
                        + "\n\nYou can only call memory and skill "
                        "management tools. Other tools will be denied "
                        "at runtime — do not attempt them."
                    ),
                    conversation_history=(
                        _digest_history(messages_snapshot)
                        if routed
                        else messages_snapshot
                    ),
                )
            finally:
                clear_thread_tool_whitelist()

            # Snapshot before shutdown: providers and close hooks may clear
            # transient messages in future implementations.
            review_messages = list(getattr(review_agent, "_session_messages", []))
            try:
                review_agent.shutdown_memory_provider()
            except Exception:
                pass
            try:
                review_agent.close()
            except Exception:
                pass
            review_agent = None

        # Scan the review agent's messages for successful tool actions
        # and surface a compact summary to the user. Tool messages
        # already present in messages_snapshot must be skipped, since
        # the review agent inherits that history and would otherwise
        # re-surface stale "created"/"updated" messages from the prior
        # conversation as if they just happened (issue #14944).
        try:
            actions = summarize_background_review_actions(
                review_messages,
                messages_snapshot,
                notification_mode=getattr(agent, "_memory_notifications", "on"),
            )
        except Exception:
            logger.warning(
                "Background review produced an unreadable action summary",
                exc_info=True,
            )
            actions = []

        if actions:
            summary = " · ".join(dict.fromkeys(actions))
            agent._safe_print(
                f"  💾 Self-improvement review: {summary}"
            )
            _bg_cb = agent.background_review_callback
            if _bg_cb:
                try:
                    _bg_cb(
                        f"💾 Self-improvement review: {summary}"
                    )
                except Exception:
                    pass

    except Exception as e:
        logger.warning("Background memory/skill review failed: %s", e)
        agent._emit_auxiliary_failure("background review", e)
    finally:
        # Safety-net cleanup for the exception path, still scoped only to this
        # worker thread.
        if review_agent is not None:
            try:
                with thread_scoped_silence():
                    try:
                        review_agent.shutdown_memory_provider()
                    except Exception:
                        pass
                    try:
                        review_agent.close()
                    except Exception:
                        pass
            except Exception:
                pass
        # Clear the approval callback on this bg-review thread so a
        # recycled thread-id doesn't inherit a stale reference.
        try:
            _set_approval_callback(None)
        except Exception:
            pass


def spawn_background_review_thread(
    agent: Any,
    messages_snapshot: List[Dict],
    review_memory: bool = False,
    review_skills: bool = False,
):
    """Build the review thread target and prompt for a background review.

    Returns a ``(target, prompt)`` tuple.  The caller (``AIAgent._spawn_background_review``)
    owns the actual ``threading.Thread`` construction so test-level patches
    of ``run_agent.threading.Thread`` keep working.
    """
    # Pick the right prompt based on which triggers fired.  Allow per-agent
    # override (the prompts moved to module-level constants but old code paths
    # that set agent._MEMORY_REVIEW_PROMPT etc. directly keep working).
    if review_memory and review_skills:
        prompt = getattr(agent, "_COMBINED_REVIEW_PROMPT", _COMBINED_REVIEW_PROMPT)
    elif review_memory:
        prompt = getattr(agent, "_MEMORY_REVIEW_PROMPT", _MEMORY_REVIEW_PROMPT)
    else:
        prompt = getattr(agent, "_SKILL_REVIEW_PROMPT", _SKILL_REVIEW_PROMPT)

    def _target() -> None:
        _run_review_in_thread(agent, messages_snapshot, prompt)

    return _target, prompt


__all__ = [
    "_MEMORY_REVIEW_PROMPT",
    "_SKILL_REVIEW_PROMPT",
    "_COMBINED_REVIEW_PROMPT",
    "spawn_background_review_thread",
    "summarize_background_review_actions",
    "build_memory_write_metadata",
]
