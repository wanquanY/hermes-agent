"""Gateway message command dispatch before agent execution."""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from hermes_gateway.conversation_editing_commands import conversation_editing_for
from hermes_gateway.debug_command import debug_command_for
from hermes_gateway.fast_command import fast_command_for
from hermes_gateway.footer_command import footer_command_for
from hermes_gateway.goal_commands import goal_command_for
from hermes_gateway.model_command import model_command_for
from hermes_gateway.personality_command import personality_command_for
from hermes_gateway.platform_command import platform_command_for
from hermes_gateway.reasoning_command import reasoning_command_for
from hermes_gateway.reload_mcp_command import reload_mcp_command_for
from hermes_gateway.reload_skills_command import reload_skills_command_for
from hermes_gateway.restart_lifecycle import restart_lifecycle_for
from hermes_gateway.rollback_command import rollback_command_for
from hermes_gateway.runtime_status_command import runtime_status_command_for
from hermes_gateway.session_navigation_commands import session_navigation_for
from hermes_gateway.title_command import title_command_for
from hermes_gateway.update_lifecycle import update_lifecycle_for
from hermes_gateway.usage_command import usage_command_for
from hermes_gateway.verbose_command import verbose_command_for
from hermes_gateway.voice_runtime import voice_runtime_for
from hermes_gateway.yolo_command import yolo_command_for
from hermes_gateway.skill_hint import check_unavailable_skill as _check_unavailable_skill_for_repo

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class MessageCommandResult:
    handled: bool
    response: Any = None


class GatewayMessageCommandService:
    def __init__(self, runner):
        self._runner = runner

    async def dispatch(self, event, source, session_key: str) -> MessageCommandResult:
        runner = self._runner
        # Check for commands
        command = event.get_command()

        from hermes_cli.commands import (
            GATEWAY_KNOWN_COMMANDS,
            is_gateway_known_command,
            resolve_command as _resolve_cmd,
        )

        # Resolve aliases to canonical name so dispatch and hook names
        # don't depend on the exact alias the user typed.
        _cmd_def = _resolve_cmd(command) if command else None
        canonical = _cmd_def.name if _cmd_def else command

        # Expand alias quick commands before built-in dispatch so targets like
        # /model openai/gpt-5.5 --provider openrouter reach the /model handler.
        # Preserve built-in precedence; aliases only need early handling when
        # the typed command is not already known.
        if command and _cmd_def is None:
            if isinstance(runner.config, dict):
                quick_commands = runner.config.get("quick_commands", {}) or {}
            else:
                quick_commands = getattr(runner.config, "quick_commands", {}) or {}
            if isinstance(quick_commands, dict) and command in quick_commands:
                qcmd = quick_commands[command]
                if qcmd.get("type") == "alias":
                    target = qcmd.get("target", "").strip()
                    if target:
                        target = target if target.startswith("/") else f"/{target}"
                        target_command = target.lstrip("/")
                        user_args = event.get_command_args().strip()
                        event.text = f"{target} {user_args}".strip()
                        command = target_command.split()[0] if target_command else target_command
                        _cmd_def = _resolve_cmd(command) if command else None
                        canonical = _cmd_def.name if _cmd_def else command

        # Per-platform slash command access control. Only kicks in when the
        # operator has set ``allow_admin_from`` for the source's scope (DM
        # vs group). When unset → backward-compat: every allowed user can
        # run every command. When set → non-admins can run only commands in
        # ``user_allowed_commands`` (plus the always-allowed floor: /help,
        # /whoami). Plain chat is unaffected — only slash commands gate.
        if command and canonical and is_gateway_known_command(canonical):
            from channels.slash_commands import check_slash_access

            _denied = check_slash_access(
                gateway_config=runner.config,
                source=source,
                canonical_cmd=canonical,
            )
            if _denied is not None:
                return _denied

        # Fire the ``command:<canonical>`` hook for any recognized slash
        # command — built-in OR plugin-registered. Handlers can return a
        # dict with ``{"decision": "deny" | "handled" | "rewrite", ...}``
        # to intercept dispatch before core handling runs. This replaces
        # the previous fire-and-forget emit(): return values are now
        # honored, but handlers that return nothing behave exactly as
        # before (telemetry-style hooks keep working).
        if command and is_gateway_known_command(canonical):
            raw_args = event.get_command_args().strip()
            hook_ctx = {
                "platform": source.platform.value if source.platform else "",
                "user_id": source.user_id,
                "command": canonical,
                "raw_command": command,
                "args": raw_args,
                "raw_args": raw_args,
            }
            try:
                hook_results = await runner.hooks.emit_collect(
                    f"command:{canonical}", hook_ctx
                )
            except Exception as _hook_err:
                logger.debug(
                    "command:%s hook dispatch failed (non-fatal): %s",
                    canonical, _hook_err,
                )
                hook_results = []

            for hook_result in hook_results:
                if not isinstance(hook_result, dict):
                    continue
                decision = str(hook_result.get("decision", "")).strip().lower()
                if not decision or decision == "allow":
                    continue
                if decision == "deny":
                    message = hook_result.get("message")
                    if isinstance(message, str) and message:
                        return message
                    return f"Command `/{command}` was blocked by a hook."
                if decision == "handled":
                    message = hook_result.get("message")
                    return message if isinstance(message, str) and message else None
                if decision == "rewrite":
                    new_command = str(
                        hook_result.get("command_name", "")
                    ).strip().lstrip("/")
                    if not new_command:
                        continue
                    new_args = str(hook_result.get("raw_args", "")).strip()
                    event.text = f"/{new_command} {new_args}".strip()
                    command = event.get_command()
                    _cmd_def = _resolve_cmd(command) if command else None
                    canonical = _cmd_def.name if _cmd_def else command
                    break

        if canonical == "new":
            if session_navigation_for(runner).is_telegram_topic_root_lobby(source):
                return session_navigation_for(runner).telegram_topic_root_new_message()
            async def _do_reset():
                return await runner._handle_reset_command(event)
            from channels.slash_commands import maybe_confirm_destructive_slash

            return await maybe_confirm_destructive_slash(
                runtime=runner._slash_confirmation_runtime(),
                event=event,
                command="new",
                title="/new",
                detail=(
                    "This starts a fresh session and discards the current "
                    "conversation history."
                ),
                execute=_do_reset,
            )

        if canonical == "topic":
            return await session_navigation_for(runner).handle_topic_command(event)
        
        if canonical == "help":
            return await runner._handle_help_command(event)

        if canonical == "commands":
            return await runner._handle_commands_command(event)
        
        if canonical == "profile":
            return await runner._handle_profile_command(event)

        if canonical == "whoami":
            from channels.slash_commands import handle_whoami_command

            return await handle_whoami_command(gateway_config=runner.config, event=event)

        if canonical == "status":
            return await runtime_status_command_for(runner).handle_status_command(event)

        if canonical == "agents":
            return await runtime_status_command_for(runner).handle_agents_command(event)

        if canonical == "platform":
            return await platform_command_for(runner).handle_platform_command(event)

        if canonical == "restart":
            return await restart_lifecycle_for(runner).handle_restart_command(event)
        
        if canonical == "stop":
            return await runtime_status_command_for(runner).handle_stop_command(event)
        
        if canonical == "reasoning":
            return await reasoning_command_for(runner).handle_reasoning_command(event)

        if canonical == "fast":
            return await fast_command_for(runner).handle_fast_command(event)

        if canonical == "verbose":
            return await verbose_command_for(runner).handle_verbose_command(event)

        if canonical == "footer":
            return await footer_command_for(runner).handle_footer_command(event)

        if canonical == "yolo":
            return await yolo_command_for(runner).handle_yolo_command(event)

        if canonical == "model":
            return await model_command_for(runner).handle_model_command(event)

        if canonical == "codex-runtime":
            return await runner._handle_codex_runtime_command(event)

        if canonical == "personality":
            return await personality_command_for(runner).handle_personality_command(event)

        if canonical == "kanban":
            from channels.slash_commands import handle_kanban_command

            return await handle_kanban_command(
                event=event,
                notifier_profile=getattr(runner, "_kanban_notifier_profile", None),
                active_profile_name=runner._active_profile_name,
            )

        if canonical == "suggestions":
            return await conversation_editing_for(runner).handle_suggestions_command(event)

        if canonical == "retry":
            return await conversation_editing_for(runner).handle_retry_command(event)
        
        if canonical == "undo":
            async def _do_undo():
                return await conversation_editing_for(runner).handle_undo_command(event)
            from channels.slash_commands import maybe_confirm_destructive_slash

            return await maybe_confirm_destructive_slash(
                runtime=runner._slash_confirmation_runtime(),
                event=event,
                command="undo",
                title="/undo",
                detail="This removes the last user/assistant exchange from history.",
                execute=_do_undo,
            )
        
        if canonical == "sethome":
            return await runner._handle_set_home_command(event)

        if canonical == "compress":
            return await runner._handle_compress_command(event)

        if canonical == "usage":
            return await usage_command_for(runner).handle_usage_command(event)

        if canonical == "insights":
            return await runner._handle_insights_command(event)

        if canonical == "reload-mcp":
            return await reload_mcp_command_for(runner).handle_reload_mcp_command(event)

        if canonical == "reload-skills":
            return await reload_skills_command_for(runner).handle_reload_skills_command(event)

        if canonical == "bundles":
            return await runner._handle_bundles_command(event)

        if canonical == "approve":
            return await runner._handle_approve_command(event)

        if canonical == "deny":
            return await runner._handle_deny_command(event)

        if canonical == "update":
            return await update_lifecycle_for(runner).handle_update_command(event)

        if canonical == "debug":
            return await debug_command_for(runner).handle_debug_command(event)

        if canonical == "title":
            return await title_command_for(runner).handle_title_command(event)

        if canonical == "resume":
            return await session_navigation_for(runner).handle_resume_command(event)

        if canonical == "branch":
            return await session_navigation_for(runner).handle_branch_command(event)

        if canonical == "rollback":
            return await rollback_command_for(runner).handle_rollback_command(event)

        if canonical == "background":
            return await runner._handle_background_command(event)

        if canonical == "steer":
            # No active agent — /steer has no tool call to inject into.
            # Strip the prefix so downstream treats it as a normal user
            # message. If the payload is empty, surface the usage hint.
            steer_payload = event.get_command_args().strip()
            if not steer_payload:
                return "Usage: /steer <prompt>  (no agent is running; sending as a normal message)"
            try:
                event.text = steer_payload
            except Exception:
                pass
            # Do NOT return — fall through to _handle_message_with_agent
            # at the end of this function so the rewritten text is sent
            # to the agent as a regular user turn.

        if canonical == "goal":
            return await goal_command_for(runner).handle_goal_command(event)

        if canonical == "subgoal":
            return await goal_command_for(runner).handle_subgoal_command(event)

        if canonical == "voice":
            return await voice_runtime_for(runner).handle_voice_command(event)

        if runner._draining:
            return f"⏳ Gateway is {runner._status_action_gerund()} and is not accepting new work right now."

        # User-defined quick commands (bypass agent loop, no LLM call)
        if command:
            if isinstance(runner.config, dict):
                quick_commands = runner.config.get("quick_commands", {}) or {}
            else:
                quick_commands = getattr(runner.config, "quick_commands", {}) or {}
            if not isinstance(quick_commands, dict):
                quick_commands = {}
            if command in quick_commands:
                qcmd = quick_commands[command]
                if qcmd.get("type") == "exec":
                    exec_cmd = qcmd.get("command", "")
                    if exec_cmd:
                        try:
                            # Sanitize env to prevent credential leakage —
                            # quick commands run in the gateway process which
                            # has all API keys in os.environ.
                            from tools.environments.local import _sanitize_subprocess_env
                            sanitized_env = _sanitize_subprocess_env(os.environ.copy())
                            proc = await asyncio.create_subprocess_shell(
                                exec_cmd,
                                stdout=asyncio.subprocess.PIPE,
                                stderr=asyncio.subprocess.PIPE,
                                env=sanitized_env,
                            )
                            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
                            output = (stdout or stderr).decode().strip()
                            # Redact any remaining sensitive patterns in output
                            if output:
                                from agent.redact import redact_sensitive_text
                                output = redact_sensitive_text(output)
                            return output if output else "Command returned no output."
                        except asyncio.TimeoutError:
                            return "Quick command timed out (30s)."
                        except Exception as e:
                            return f"Quick command error: {e}"
                    else:
                        return f"Quick command '/{command}' has no command defined."
                elif qcmd.get("type") == "alias":
                    target = qcmd.get("target", "").strip()
                    if target:
                        target = target if target.startswith("/") else f"/{target}"
                        target_command = target.lstrip("/")
                        user_args = event.get_command_args().strip()
                        event.text = f"{target} {user_args}".strip()
                        command = target_command.split()[0] if target_command else target_command
                        # Fall through to normal command dispatch below
                    else:
                        return f"Quick command '/{command}' has no target defined."
                else:
                    return f"Quick command '/{command}' has unsupported type (supported: 'exec', 'alias')."

        # Plugin-registered slash commands
        if command:
            try:
                from hermes_cli.plugins import get_plugin_command_handler
                # Normalize underscores to hyphens so Telegram's underscored
                # autocomplete form matches plugin commands registered with
                # hyphens. See hermes_cli/commands.py:_build_telegram_menu.
                plugin_handler = get_plugin_command_handler(command.replace("_", "-"))
                if plugin_handler:
                    user_args = event.get_command_args().strip()
                    result = plugin_handler(user_args)
                    if asyncio.iscoroutine(result):
                        result = await result
                    return str(result) if result else None
            except Exception as e:
                logger.debug("Plugin command dispatch failed (non-fatal): %s", e)

        # Skill slash commands: /skill-name loads the skill and sends to agent.
        # resolve_skill_command_key() handles the Telegram underscore/hyphen
        # round-trip so /claude_code from Telegram autocomplete still resolves
        # to the claude-code skill.
        if command:
            # Skill bundles take precedence over individual skill commands —
            # /<bundle> loads multiple skills at once. Mirrors CLI dispatch.
            _bundle_handled = False
            try:
                from agent.skill_bundles import (
                    build_bundle_invocation_message,
                    resolve_bundle_command_key,
                )
                bundle_key = resolve_bundle_command_key(command)
                if bundle_key is not None:
                    user_instruction = event.get_command_args().strip()
                    bundle_result = build_bundle_invocation_message(
                        bundle_key, user_instruction, task_id=_quick_key
                    )
                    if bundle_result:
                        msg, _loaded, missing = bundle_result
                        event.text = msg
                        _bundle_handled = True
                        if missing:
                            logger.info(
                                "Bundle %s skipped missing skills: %s",
                                bundle_key, ", ".join(missing),
                            )
                        # Fall through to normal message processing with bundle content
            except Exception as exc:
                logger.debug("Bundle dispatch failed (non-fatal): %s", exc)

        if command and not locals().get("_bundle_handled", False):
            try:
                from agent.skill_commands import (
                    get_skill_commands,
                    build_skill_invocation_message,
                    resolve_skill_command_key,
                )
                skill_cmds = get_skill_commands()
                cmd_key = resolve_skill_command_key(command)
                if cmd_key is not None:
                    # Check per-platform disabled status before executing.
                    # get_skill_commands() only applies the *global* disabled
                    # list at scan time; per-platform overrides need checking
                    # here because the cache is process-global across platforms.
                    _skill_name = skill_cmds[cmd_key].get("name", "")
                    _plat = source.platform.value if source.platform else None
                    if _plat and _skill_name:
                        from agent.skill_utils import get_disabled_skill_names as _get_plat_disabled
                        if _skill_name in _get_plat_disabled(platform=_plat):
                            return (
                                f"The **{_skill_name}** skill is disabled for {_plat}.\n"
                                f"Enable it with: `hermes skills config`"
                            )
                    user_instruction = event.get_command_args().strip()
                    msg = build_skill_invocation_message(
                        cmd_key, user_instruction, task_id=_quick_key
                    )
                    if msg:
                        event.text = msg
                        # Fall through to normal message processing with skill content
                else:
                    # Not an active skill — check if it's a known-but-disabled or
                    # uninstalled skill and give actionable guidance.
                    _unavail_msg = _check_unavailable_skill(command)
                    if _unavail_msg:
                        return _unavail_msg
                    # Genuinely unrecognized /command: not a built-in, not a
                    # plugin, not a skill, not a known-inactive skill. Warn
                    # the user instead of silently forwarding it to the LLM
                    # as free text (which leads to silent-failure behavior
                    # like the model inventing a delegate_task call).
                    # Normalize to hyphenated form before checking known
                    # built-ins (command may be an alias target set by the
                    # quick-command block above, so _cmd_def can be stale).
                    if command.replace("_", "-") not in GATEWAY_KNOWN_COMMANDS:
                        logger.warning(
                            "Unrecognized slash command /%s from %s — "
                            "replying with unknown-command notice",
                            command,
                            source.platform.value if source.platform else "?",
                        )
                        return (
                            f"Unknown command `/{command}`. "
                            f"Type /commands to see what's available, "
                            f"or resend without the leading slash to send "
                            f"as a regular message."
                        )
            except Exception as e:
                logger.debug("Skill command check failed (non-fatal): %s", e)
        
        # Pending exec approvals are handled by /approve and /deny commands above.
        # No bare text matching — "yes" in normal conversation must not trigger
        # execution of a dangerous command.

        if session_navigation_for(runner).is_telegram_topic_root_lobby(source):
            # Debounce the lobby reminder so a user who forgets about
            # topic mode and fires ten prompts doesn't get ten copies.
            if session_navigation_for(runner).should_send_telegram_lobby_reminder(source):
                return session_navigation_for(runner).telegram_topic_root_lobby_message()
            return None


        return MessageCommandResult(handled=False)


def _check_unavailable_skill(command_name: str) -> str | None:
    repo_root = Path(__file__).resolve().parent.parent
    return _check_unavailable_skill_for_repo(command_name, repo_root=repo_root)


def message_command_for(runner) -> GatewayMessageCommandService:
    service = getattr(runner, "message_command", None)
    if isinstance(service, GatewayMessageCommandService):
        return service
    service = GatewayMessageCommandService(runner)
    runner.message_command = service
    return service
