from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Dict, Optional, Tuple

try:
    import discord
except ImportError:  # pragma: no cover - optional platform dependency
    discord = None

from channels.config import Platform
from channels.platforms.base import MessageEvent, MessageType

logger = logging.getLogger(__name__)


def _discord_public_attr(name: str, fallback: Any = None) -> Any:
    import sys

    public_module = sys.modules.get("channels.platforms.discord")
    if public_module is None:
        return fallback
    return getattr(public_module, name, fallback)


class DiscordSlashCommandMixin:
    def _is_allowed_user(
        self,
        user_id: str,
        author=None,
        *,
        guild=None,
        is_dm: bool = False,
    ) -> bool:
        """Check if user is allowed via DISCORD_ALLOWED_USERS or DISCORD_ALLOWED_ROLES.
    
        Uses OR semantics: if the user matches EITHER allowlist, they're allowed.
        If both allowlists are empty, everyone is allowed (backwards compatible).
    
        Role checks are **scoped to the guild the message originated from**.
        For DMs (no guild context), role-based auth is disabled by default and
        only user-ID allowlist applies. Set ``discord.dm_role_auth_guild``
        in config.yaml to a specific guild ID to opt-in: role membership in
        that one guild will authorize DMs. This prevents cross-guild
        privilege escalation where a user with the configured role in any
        shared public server could DM the bot and pass the allowlist.
    
        Args:
            user_id: Author ID as a string.
            author: Optional Member/User object for in-guild role lookup.
            guild: The guild the message arrived in (None for DMs).
            is_dm: True if the message came from a DM channel.
        """
        # ``getattr`` fallbacks here guard against test fixtures that build
        # an adapter via ``object.__new__(DiscordAdapter)`` and skip __init__
        # (see AGENTS.md pitfall #17 — same pattern as the gateway runner).
        allowed_users = getattr(self, "_allowed_user_ids", set())
        allowed_roles = getattr(self, "_allowed_role_ids", set())
        has_users = bool(allowed_users)
        has_roles = bool(allowed_roles)
        if not has_users and not has_roles:
            return True
        # Check user ID allowlist (works for both DMs and guild messages)
        if has_users and user_id in allowed_users:
            return True
        # Role allowlist is only consulted when configured.
        if not has_roles:
            return False
    
        # DM path: roles require explicit opt-in via
        # ``discord.dm_role_auth_guild`` in config.yaml. Without this, a
        # user with the configured role in ANY mutual guild could DM the
        # bot and bypass the allowlist (cross-guild leakage).
        if is_dm or guild is None:
            dm_guild_id = _discord_public_attr("_read_dm_role_auth_guild")()
            if dm_guild_id is None:
                return False
            if self._client is None:
                return False
            dm_guild = self._client.get_guild(dm_guild_id)
            if dm_guild is None:
                return False
            try:
                uid_int = int(user_id)
            except (TypeError, ValueError):
                return False
            m = dm_guild.get_member(uid_int)
            if m is None:
                return False
            m_roles = getattr(m, "roles", None) or []
            return any(getattr(r, "id", None) in allowed_roles for r in m_roles)
    
        # Guild path: role check is scoped to THIS guild only.
        # 1) Prefer the direct Member object passed in (correct guild by construction).
        direct_roles = getattr(author, "roles", None) if author is not None else None
        author_guild = getattr(author, "guild", None)
        if direct_roles and (author_guild is None or author_guild.id == guild.id):
            if any(getattr(r, "id", None) in allowed_roles for r in direct_roles):
                return True
        # 2) Fallback: resolve the Member in the message's guild only — NEVER
        #    scan other mutual guilds (that is the cross-guild bypass bug).
        try:
            uid_int = int(user_id)
        except (TypeError, ValueError):
            return False
        m = guild.get_member(uid_int)
        if m is None:
            return False
        m_roles = getattr(m, "roles", None) or []
        return any(getattr(r, "id", None) in allowed_roles for r in m_roles)
    
    def _evaluate_slash_authorization(
        self, interaction: "discord.Interaction",
    ) -> Tuple[bool, Optional[str]]:
        """Evaluate slash authorization without producing any response.
    
        Returns ``(allowed, reason)``. ``reason`` is populated only when
        ``allowed`` is False. This is the shared core used by both the
        responding wrapper (``_check_slash_authorization``) and side-effect-
        free callers like the ``/skill`` autocomplete callback, which must
        return an empty list for unauthorized users instead of leaking an
        ephemeral rejection per-keystroke.
    
        Fail-closed semantics for malformed payloads: when an allowlist is
        configured but the interaction is missing the data needed to
        evaluate it (no channel id with channel policy active, no user
        with user/role policy active), the gate REJECTS rather than
        falling through. Without these guards a guild interaction that
        happens to deserialize without a channel id would silently bypass
        ``DISCORD_ALLOWED_CHANNELS`` and a payload missing ``user`` would
        raise ``AttributeError`` in the user check below, surfacing as
        an opaque interaction failure rather than a clean rejection.
        """
        chan_obj = getattr(interaction, "channel", None)
        in_dm = isinstance(chan_obj, discord.DMChannel) if chan_obj is not None else False
    
        # ── Channel scope (mirrors on_message lines 3374-3388) ──
        # DMs aren't channel-gated — DMs follow on_message's DM lockdown
        # path which has its own user-allowlist enforcement.
        if not in_dm:
            chan_id_raw = getattr(interaction, "channel_id", None) or getattr(
                chan_obj, "id", None,
            )
            channel_ids: set = set()
            if chan_id_raw is not None:
                channel_ids.add(str(chan_id_raw))
                # Mirror on_message: also test the parent channel for threads
                # so per-channel allow/deny lists work consistently.
                if isinstance(chan_obj, discord.Thread):
                    parent_id = self._get_parent_channel_id(chan_obj)
                    if parent_id:
                        channel_ids.add(str(parent_id))
    
            allowed_raw = os.getenv("DISCORD_ALLOWED_CHANNELS", "")
            if allowed_raw:
                allowed = {c.strip() for c in allowed_raw.split(",") if c.strip()}
                if "*" not in allowed:
                    if not channel_ids:
                        # Channel policy is configured but the interaction
                        # has no resolvable channel id. Fail closed.
                        return (
                            False,
                            "channel id missing with DISCORD_ALLOWED_CHANNELS configured",
                        )
                    if not (channel_ids & allowed):
                        return (False, "channel not in DISCORD_ALLOWED_CHANNELS")
    
            # Ignored beats allowed: even when a thread's parent channel
            # is on the allowlist, an explicit DISCORD_IGNORED_CHANNELS
            # entry on the thread or its parent rejects the interaction.
            ignored_raw = os.getenv("DISCORD_IGNORED_CHANNELS", "")
            if ignored_raw and channel_ids:
                ignored = {c.strip() for c in ignored_raw.split(",") if c.strip()}
                if "*" in ignored or (channel_ids & ignored):
                    return (False, "channel in DISCORD_IGNORED_CHANNELS")
    
        # ── User / role allowlist (mirrors on_message line 681) ──
        user = getattr(interaction, "user", None)
        allowed_users = getattr(self, "_allowed_user_ids", set()) or set()
        allowed_roles = getattr(self, "_allowed_role_ids", set()) or set()
        if user is None or getattr(user, "id", None) is None:
            # No identifiable user. With any user/role allowlist
            # configured, fail closed rather than raise AttributeError
            # on ``interaction.user.id`` below. With no allowlist this
            # is the existing "no allowlist = everyone" backwards-compat.
            if allowed_users or allowed_roles:
                return (False, "missing interaction.user with allowlist configured")
            return (True, None)
    
        user_id = str(user.id)
        # Pass guild + is_dm so role check is scoped to the originating
        # guild and cross-guild DM bypass (#12136) can't land via the
        # slash surface either.
        interaction_guild = getattr(interaction, "guild", None)
        if not self._is_allowed_user(
            user_id,
            author=user,
            guild=interaction_guild,
            is_dm=in_dm,
        ):
            return (
                False,
                "user not in DISCORD_ALLOWED_USERS / DISCORD_ALLOWED_ROLES",
            )
    
        return (True, None)
    
    async def _check_slash_authorization(
        self, interaction: "discord.Interaction", command_text: str,
    ) -> bool:
        """Mirror on_message's user/role/channel gates onto a slash invocation.
    
        Returns True to proceed. Returns False *after* sending an ephemeral
        rejection, logging a warning, and scheduling a cross-platform admin
        alert — the caller must stop on False (the interaction has already
        been responded to).
        """
        allowed, reason = self._evaluate_slash_authorization(interaction)
        if allowed:
            return True
        return await self._reject_slash(
            interaction, command_text, reason=reason or "unauthorized",
        )
    
    async def _reject_slash(
        self, interaction: "discord.Interaction", command_text: str, *, reason: str,
    ) -> bool:
        """Send ephemeral reject + log warning + schedule admin alert. Returns False.
    
        Tolerates a missing ``interaction.user`` -- the fail-closed branch
        in ``_evaluate_slash_authorization`` deliberately routes here for
        malformed payloads (no user) when an allowlist is configured, and
        ``str(interaction.user.id)`` would raise AttributeError before the
        ephemeral rejection could be sent.
        """
        user = getattr(interaction, "user", None)
        if user is not None:
            user_id = str(getattr(user, "id", "?"))
            user_name = getattr(user, "name", "?")
        else:
            user_id = "?"
            user_name = "?"
        chan_id = getattr(interaction, "channel_id", None) or getattr(
            getattr(interaction, "channel", None), "id", None,
        )
        guild_id = getattr(interaction, "guild_id", None)
    
        logger.warning(
            "[Discord] Unauthorized slash attempt: user=%s id=%s channel=%s "
            "guild=%s cmd=%r reason=%r",
            user_name, user_id, chan_id, guild_id, command_text, reason,
        )
    
        try:
            await interaction.response.send_message(
                "You're not authorized to use this command.",
                ephemeral=True,
            )
        except Exception as e:
            # Interaction may already be responded to (e.g. caller deferred
            # before the auth check, or Discord retried). Best-effort only.
            logger.debug("[Discord] Could not send unauthorized ephemeral: %s", e)
    
        # Fire-and-forget: don't block the interaction handler on Telegram I/O.
        try:
            asyncio.create_task(self._notify_unauthorized_slash(
                user_name, user_id, chan_id, guild_id, command_text, reason,
            ))
        except Exception as e:
            logger.debug("[Discord] Could not schedule admin notify task: %s", e)
    
        return False
    
    async def _notify_unauthorized_slash(
        self, user_name: str, user_id: str, chan_id, guild_id,
        command_text: str, reason: str,
    ) -> None:
        """Best-effort cross-platform alert to the gateway operator.
    
        Tries TELEGRAM first (most operators set TELEGRAM_HOME_CHANNEL),
        then SLACK. Silently no-ops if no other platform is configured
        with a home channel.
    
        A soft send failure -- adapter.send() returning a result with
        ``success=False`` rather than raising -- continues the fallback
        chain. Treating a SendResult(success=False) as delivered would
        mean a Telegram outage that the adapter politely surfaces (e.g.
        rate-limit, auth failure) silently swallows the alert without
        attempting Slack. Hard exceptions still take the same path via
        the except branch below.
        """
        runner = getattr(self, "gateway_runner", None)
        if not runner:
            return
        for target in (Platform.TELEGRAM, Platform.SLACK):
            try:
                adapter = runner.adapters.get(target)
                if not adapter:
                    continue
                home = runner.config.get_home_channel(target)
                if not home or not getattr(home, "chat_id", None):
                    continue
                msg = (
                    "⚠️ Unauthorized Discord slash attempt\n"
                    f"User: {user_name} ({user_id})\n"
                    f"Channel: {chan_id} (guild {guild_id})\n"
                    f"Command: {command_text}\n"
                    f"Reason: {reason}"
                )
                result = await adapter.send(str(home.chat_id), msg)
                # Only return on confirmed delivery. SendResult(success=False)
                # -> continue to the next platform.
                if getattr(result, "success", None) is False:
                    logger.debug(
                        "[Discord] Admin notify via %s returned success=False"
                        " (error=%r); falling through",
                        target, getattr(result, "error", None),
                    )
                    continue
                return
            except Exception as e:
                logger.debug("[Discord] Admin notify via %s failed: %s", target, e)
    
    async def _run_simple_slash(
        self,
        interaction: discord.Interaction,
        command_text: str,
        followup_msg: str | None = None,
    ) -> None:
        """Common handler for simple slash commands that dispatch a command string.
    
        Defers the interaction (shows "thinking..."), dispatches the command,
        then cleans up the deferred response.  If *followup_msg* is provided
        the "thinking..." indicator is replaced with that text; otherwise it
        is deleted so the channel isn't cluttered.
        """
        # Log the invoker so ghost-command reports can be triaged.  Discord
        # native slash invocations are always user-initiated (no bot can fire
        # them), but mobile autocomplete / keyboard shortcuts / other users
        # in the same channel are easy to miss in post-mortems.
        try:
            _user = interaction.user
            _chan_id = getattr(interaction.channel, "id", None) or getattr(interaction, "channel_id", None)
            logger.info(
                "[Discord] slash '%s' invoked by user=%s id=%s channel=%s guild=%s",
                command_text,
                getattr(_user, "name", "?"),
                getattr(_user, "id", "?"),
                _chan_id,
                getattr(interaction, "guild_id", None),
            )
        except Exception:
            pass  # logging must never block command dispatch
    
        # Auth gate — must run before defer() so an ephemeral rejection can
        # be delivered on the still-unresponded interaction.
        if not await self._check_slash_authorization(interaction, command_text):
            return
    
        await interaction.response.defer(ephemeral=True)
        event = self._build_slash_event(interaction, command_text)
        await self.handle_message(event)
        try:
            if followup_msg:
                await interaction.edit_original_response(content=followup_msg)
            else:
                await interaction.delete_original_response()
        except Exception as e:
            logger.debug("Discord interaction cleanup failed: %s", e)
    
    def _register_slash_commands(self) -> None:
        """Register Discord slash commands on the command tree."""
        if not self._client:
            return
    
        tree = self._client.tree
    
        @tree.command(name="new", description="Start a new conversation")
        async def slash_new(interaction: discord.Interaction):
            await self._run_simple_slash(interaction, "/reset", "New conversation started~")
    
        @tree.command(name="reset", description="Reset your Hermes session")
        async def slash_reset(interaction: discord.Interaction):
            await self._run_simple_slash(interaction, "/reset", "Session reset~")
    
        @tree.command(name="model", description="Show or change the model")
        @discord.app_commands.describe(name="Model name (e.g. anthropic/claude-sonnet-4). Leave empty to see current.")
        async def slash_model(interaction: discord.Interaction, name: str = ""):
            await self._run_simple_slash(interaction, f"/model {name}".strip())
    
        @tree.command(name="reasoning", description="Show or change reasoning effort")
        @discord.app_commands.describe(effort="Reasoning effort: none, minimal, low, medium, high, xhigh, max, or ultra.")
        async def slash_reasoning(interaction: discord.Interaction, effort: str = ""):
            await self._run_simple_slash(interaction, f"/reasoning {effort}".strip())
    
        @tree.command(name="personality", description="Set a personality")
        @discord.app_commands.describe(name="Personality name. Leave empty to list available.")
        async def slash_personality(interaction: discord.Interaction, name: str = ""):
            await self._run_simple_slash(interaction, f"/personality {name}".strip())
    
        @tree.command(name="retry", description="Retry your last message")
        async def slash_retry(interaction: discord.Interaction):
            await self._run_simple_slash(interaction, "/retry", "Retrying~")
    
        @tree.command(name="undo", description="Remove the last exchange")
        async def slash_undo(interaction: discord.Interaction):
            await self._run_simple_slash(interaction, "/undo")
    
        @tree.command(name="status", description="Show Hermes session status")
        async def slash_status(interaction: discord.Interaction):
            await self._run_simple_slash(interaction, "/status", "Status sent~")
    
        @tree.command(name="sethome", description="Set this chat as the home channel")
        async def slash_sethome(interaction: discord.Interaction):
            await self._run_simple_slash(interaction, "/sethome")
    
        @tree.command(name="stop", description="Stop the running Hermes agent")
        async def slash_stop(interaction: discord.Interaction):
            await self._run_simple_slash(interaction, "/stop", "Stop requested~")
    
        @tree.command(name="steer", description="Inject a message after the next tool call (no interrupt)")
        @discord.app_commands.describe(prompt="Text to inject into the agent's next tool result")
        async def slash_steer(interaction: discord.Interaction, prompt: str):
            await self._run_simple_slash(interaction, f"/steer {prompt}".strip())
    
        @tree.command(name="compress", description="Compress conversation context")
        async def slash_compress(interaction: discord.Interaction):
            await self._run_simple_slash(interaction, "/compress")
    
        @tree.command(name="title", description="Set or show the session title")
        @discord.app_commands.describe(name="Session title. Leave empty to show current.")
        async def slash_title(interaction: discord.Interaction, name: str = ""):
            await self._run_simple_slash(interaction, f"/title {name}".strip())
    
        @tree.command(name="resume", description="Resume a previously-named session")
        @discord.app_commands.describe(name="Session name to resume. Leave empty to list sessions.")
        async def slash_resume(interaction: discord.Interaction, name: str = ""):
            await self._run_simple_slash(interaction, f"/resume {name}".strip())
    
        @tree.command(name="usage", description="Show token usage for this session")
        async def slash_usage(interaction: discord.Interaction):
            await self._run_simple_slash(interaction, "/usage")
    
        @tree.command(name="help", description="Show available commands")
        async def slash_help(interaction: discord.Interaction):
            await self._run_simple_slash(interaction, "/help")
    
        @tree.command(name="insights", description="Show usage insights and analytics")
        @discord.app_commands.describe(days="Number of days to analyze (default: 7)")
        async def slash_insights(interaction: discord.Interaction, days: int = 7):
            await self._run_simple_slash(interaction, f"/insights {days}")
    
        @tree.command(name="reload-mcp", description="Reload MCP servers from config")
        async def slash_reload_mcp(interaction: discord.Interaction):
            await self._run_simple_slash(interaction, "/reload-mcp")
    
        @tree.command(name="reload-skills", description="Re-scan ~/.hermes/skills/ for new or removed skills")
        async def slash_reload_skills(interaction: discord.Interaction):
            await self._run_simple_slash(interaction, "/reload-skills")
    
        @tree.command(name="voice", description="Toggle voice reply mode")
        @discord.app_commands.describe(mode="Voice mode: join, channel, leave, on, tts, off, or status")
        @discord.app_commands.choices(mode=[
            # `join` and `channel` both route to _handle_voice_channel_join in
            # gateway/run.py — expose both in the slash UI so autocomplete
            # matches what the docs advertise and what the runner accepts when
            # the command is typed as plain text.
            discord.app_commands.Choice(name="join — join your voice channel", value="join"),
            discord.app_commands.Choice(name="channel — join your voice channel (alias)", value="channel"),
            discord.app_commands.Choice(name="leave — leave voice channel", value="leave"),
            discord.app_commands.Choice(name="on — voice reply to voice messages", value="on"),
            discord.app_commands.Choice(name="tts — voice reply to all messages", value="tts"),
            discord.app_commands.Choice(name="off — text only", value="off"),
            discord.app_commands.Choice(name="status — show current mode", value="status"),
        ])
        async def slash_voice(interaction: discord.Interaction, mode: str = ""):
            await self._run_simple_slash(interaction, f"/voice {mode}".strip())
    
        @tree.command(name="update", description="Update Hermes Agent to the latest version")
        async def slash_update(interaction: discord.Interaction):
            await self._run_simple_slash(interaction, "/update", "Update initiated~")
    
        @tree.command(name="restart", description="Gracefully restart the Hermes gateway")
        async def slash_restart(interaction: discord.Interaction):
            await self._run_simple_slash(interaction, "/restart", "Restart requested~")
    
        @tree.command(name="approve", description="Approve a pending dangerous command")
        @discord.app_commands.describe(scope="Optional: 'all', 'session', 'always', 'all session', 'all always'")
        async def slash_approve(interaction: discord.Interaction, scope: str = ""):
            await self._run_simple_slash(interaction, f"/approve {scope}".strip())
    
        @tree.command(name="deny", description="Deny a pending dangerous command")
        @discord.app_commands.describe(scope="Optional: 'all' to deny all pending commands")
        async def slash_deny(interaction: discord.Interaction, scope: str = ""):
            await self._run_simple_slash(interaction, f"/deny {scope}".strip())
    
        @tree.command(name="thread", description="Create a new thread and start a Hermes session in it")
        @discord.app_commands.describe(
            name="Thread name",
            message="Optional first message to send to Hermes in the thread",
            auto_archive_duration="Auto-archive in minutes (60, 1440, 4320, 10080)",
        )
        async def slash_thread(
            interaction: discord.Interaction,
            name: str,
            message: str = "",
            auto_archive_duration: int = 1440,
        ):
            # defer() is performed inside the handler *after* the auth gate
            # so a rejected invoker can receive an ephemeral rejection.
            await self._handle_thread_create_slash(interaction, name, message, auto_archive_duration)
    
        @tree.command(name="queue", description="Queue a prompt for the next turn (doesn't interrupt)")
        @discord.app_commands.describe(prompt="The prompt to queue")
        async def slash_queue(interaction: discord.Interaction, prompt: str):
            await self._run_simple_slash(interaction, f"/queue {prompt}", "Queued for the next turn.")
    
        @tree.command(name="background", description="Run a prompt in the background")
        @discord.app_commands.describe(prompt="The prompt to run in the background")
        async def slash_background(interaction: discord.Interaction, prompt: str):
            await self._run_simple_slash(interaction, f"/background {prompt}", "Background task started~")
    
        # ── Auto-register any gateway-available commands not yet on the tree ──
        # This ensures new commands added to COMMAND_REGISTRY in
        # hermes_cli/commands.py automatically appear as Discord slash
        # commands without needing a manual entry here.
        def _build_auto_slash_command(_name: str, _description: str, _args_hint: str = ""):
            """Build a discord.app_commands.Command that proxies to _run_simple_slash."""
            discord_name = _name.lower()[:32]
            desc = (_description or f"Run /{_name}")[:100]
            has_args = bool(_args_hint)
    
            if has_args:
                def _make_args_handler(__name: str, __hint: str):
                    @discord.app_commands.describe(args=f"Arguments: {__hint}"[:100])
                    async def _handler(interaction: discord.Interaction, args: str = ""):
                        await self._run_simple_slash(
                            interaction, f"/{__name} {args}".strip()
                        )
                    _handler.__name__ = f"auto_slash_{__name.replace('-', '_')}"
                    return _handler
    
                handler = _make_args_handler(_name, _args_hint)
            else:
                def _make_simple_handler(__name: str):
                    async def _handler(interaction: discord.Interaction):
                        await self._run_simple_slash(interaction, f"/{__name}")
                    _handler.__name__ = f"auto_slash_{__name.replace('-', '_')}"
                    return _handler
    
                handler = _make_simple_handler(_name)
    
            return discord.app_commands.Command(
                name=discord_name,
                description=desc,
                callback=handler,
            )
    
        already_registered: set[str] = set()
        try:
            from hermes_cli.commands import COMMAND_REGISTRY, _is_gateway_available, _resolve_config_gates
    
            try:
                already_registered = {cmd.name for cmd in tree.get_commands()}
            except Exception:
                pass
    
            config_overrides = _resolve_config_gates()
    
            for cmd_def in COMMAND_REGISTRY:
                if not _is_gateway_available(cmd_def, config_overrides):
                    continue
                # Discord command names: lowercase, hyphens OK, max 32 chars.
                discord_name = cmd_def.name.lower()[:32]
                if discord_name in already_registered:
                    continue
                auto_cmd = _build_auto_slash_command(
                    cmd_def.name,
                    cmd_def.description,
                    cmd_def.args_hint,
                )
                try:
                    tree.add_command(auto_cmd)
                    already_registered.add(discord_name)
                except Exception:
                    # Silently skip commands that fail registration (e.g.
                    # name conflict with a subcommand group).
                    pass
    
            logger.debug(
                "Discord auto-registered %d commands from COMMAND_REGISTRY",
                len(already_registered),
            )
        except Exception as e:
            logger.warning("Discord auto-register from COMMAND_REGISTRY failed: %s", e)
    
        # ── Plugin-registered slash commands ──
        # Plugins register via PluginContext.register_command(); we mirror
        # those into Discord's native slash picker so users get the same
        # autocomplete UX as for built-in commands. No per-platform plugin
        # API needed — plugin commands are platform-agnostic.
        try:
            from hermes_cli.commands import _iter_plugin_command_entries
    
            for plugin_name, plugin_desc, plugin_args_hint in _iter_plugin_command_entries():
                discord_name = plugin_name.lower()[:32]
                if discord_name in already_registered:
                    continue
                auto_cmd = _build_auto_slash_command(
                    plugin_name,
                    plugin_desc,
                    plugin_args_hint,
                )
                try:
                    tree.add_command(auto_cmd)
                    already_registered.add(discord_name)
                except Exception:
                    # Silently skip commands that fail registration (e.g.
                    # name conflict with a subcommand group).
                    pass
        except Exception as e:
            logger.warning(
                "Discord auto-register from plugin commands failed: %s", e
            )
    
        # Register skills under a single /skill command group with category
        # subcommand groups.  This uses 1 top-level slot instead of N,
        # supporting up to 25 categories × 25 skills = 625 skills.
        self._register_skill_group(tree)
    
        # Optional defense-in-depth: hide every slash command from non-admin
        # guild members in Discord's slash picker. Server-side authorization
        # (``_check_slash_authorization``) is the actual gate; this is purely
        # UX so users don't see commands they can't invoke. Off by default
        # to preserve the slash UX for deployments that intentionally allow
        # everyone in the guild.
        if os.getenv("DISCORD_HIDE_SLASH_COMMANDS", "false").strip().lower() in {
            "true", "1", "yes", "on",
        }:
            self._apply_owner_only_visibility(tree)
    
    def _apply_owner_only_visibility(self, tree) -> None:
        """Set default_member_permissions=0 on every registered slash command.
    
        Discord interprets ``Permissions(0)`` as "requires no permissions",
        which paradoxically means the command is hidden from every guild
        member except those with the Administrator permission. Server admins
        can re-grant per user/role via Server Settings → Integrations →
        <bot> → Permissions.
    
        Authoritative gate is ``_check_slash_authorization`` on every
        invocation, which catches stale clients, role grants made by
        mistake, and direct API calls bypassing Discord's UI hide.
        """
        try:
            no_perms = discord.Permissions(0)
        except Exception as e:
            logger.warning(
                "[Discord] _apply_owner_only_visibility: cannot build Permissions(0): %s",
                e,
            )
            return
        applied = 0
        for cmd in tree.get_commands():
            try:
                cmd.default_permissions = no_perms
                applied += 1
            except Exception as e:
                logger.debug(
                    "[Discord] Could not set default_permissions on %r: %s",
                    getattr(cmd, "name", "?"), e,
                )
        logger.info(
            "[Discord] Hid %d slash command(s) from non-admin guild members "
            "(opt-in defense in depth via DISCORD_HIDE_SLASH_COMMANDS).",
            applied,
        )
    
    def _register_skill_group(self, tree) -> None:
        """Register a single ``/skill`` command with autocomplete on the name.
    
        Discord enforces an ~8000-byte per-command payload limit. The older
        nested layout (``/skill <category> <name>``) registered one giant
        command whose serialized payload grew linearly with the skill
        catalog — with the default ~75 skills the payload was ~14 KB and
        ``tree.sync()`` rejected the entire slash-command batch (issues
        #11321, #10259, #11385, #10261, #10214).
    
        Autocomplete options are fetched dynamically by Discord when the
        user types — they do NOT count against the per-command registration
        budget. So we register ONE flat ``/skill`` command with
        ``name: str`` (autocompleted) and ``args: str = ""``. This scales
        to thousands of skills with no size math, no splitting, and no
        hidden skills. The slash picker also becomes more discoverable —
        Discord live-filters by the user's typed prefix against both the
        skill name and its description.
    
        The entries list and lookup dict are stored on ``self`` rather
        than captured in closure variables so :meth:`refresh_skill_group`
        can repopulate them when the user runs ``/reload-skills`` without
        needing to touch the Discord slash-command tree or trigger a
        ``tree.sync()`` call.
        """
        try:
            existing_names = set()
            try:
                existing_names = {cmd.name for cmd in tree.get_commands()}
            except Exception:
                pass
    
            # Populate the instance-level entries/lookup so the
            # autocomplete + handler callbacks below always read the
            # freshest state. refresh_skill_group() re-runs the same
            # collector and mutates these two attributes in place.
            self._skill_entries: list[tuple[str, str, str]] = []
            self._skill_lookup: dict[str, tuple[str, str]] = {}
            self._skill_group_reserved_names: set[str] = set(existing_names)
            self._refresh_skill_catalog_state()
    
            if not self._skill_entries:
                return
    
            async def _autocomplete_name(
                interaction: "discord.Interaction", current: str,
            ) -> list:
                """Filter skills by the user's typed prefix.
    
                Matches both the skill name and its description so
                "/skill pdf" surfaces skills whose description mentions
                PDFs even if the name doesn't. Discord caps this list at
                25 entries per query.
    
                Authorization: a quiet pre-check evaluates the slash
                allowlists and returns ``[]`` for unauthorized users so
                the installed skill catalog is not leaked to anyone who
                can see the command in the picker. Returning a generic
                empty list here is intentional — sending a per-keystroke
                ephemeral rejection would produce a barrage of error
                popups during typing.
    
                Reads ``self._skill_entries`` so a ``/reload-skills`` run
                since process start shows up on the very next keystroke.
                """
                try:
                    allowed, _reason = self._evaluate_slash_authorization(interaction)
                except Exception:
                    # Defensive: never raise from autocomplete. Fail
                    # closed by returning an empty suggestion list.
                    return []
                if not allowed:
                    return []
                q = (current or "").strip().lower()
                choices: list = []
                for name, desc, _key in self._skill_entries:
                    if not q or q in name.lower() or (desc and q in desc.lower()):
                        if desc:
                            label = f"{name} — {desc}"
                        else:
                            label = name
                        # Discord's Choice.name is capped at 100 chars.
                        if len(label) > 100:
                            label = label[:97] + "..."
                        choices.append(
                            discord.app_commands.Choice(name=label, value=name)
                        )
                        if len(choices) >= 25:
                            break
                return choices
    
            @discord.app_commands.describe(
                name="Which skill to run",
                args="Optional arguments for the skill",
            )
            @discord.app_commands.autocomplete(name=_autocomplete_name)
            async def _skill_handler(
                interaction: "discord.Interaction", name: str, args: str = "",
            ):
                # Authorize BEFORE any skill lookup so that known and
                # unknown skill names produce identical rejections for
                # unauthorized users (no probing the installed catalog
                # via "Unknown skill: <name>" responses).
                if not await self._check_slash_authorization(interaction, "/skill"):
                    return
                entry = self._skill_lookup.get(name)
                if not entry:
                    await interaction.response.send_message(
                        f"Unknown skill: `{name}`. Start typing for "
                        f"autocomplete suggestions.",
                        ephemeral=True,
                    )
                    return
                _desc, cmd_key = entry
                await self._run_simple_slash(
                    interaction, f"{cmd_key} {args}".strip()
                )
    
            cmd = discord.app_commands.Command(
                name="skill",
                description="Run a Hermes skill",
                callback=_skill_handler,
            )
            tree.add_command(cmd)
    
            logger.info(
                "[%s] Registered /skill command with %d skill(s) via autocomplete",
                self.name, len(self._skill_entries),
            )
            if self._skill_group_hidden_count:
                logger.info(
                    "[%s] %d skill(s) filtered out of /skill (name clamp / reserved)",
                    self.name, self._skill_group_hidden_count,
                )
        except Exception as exc:
            logger.warning("[%s] Failed to register /skill command: %s", self.name, exc)
    
    def _refresh_skill_catalog_state(self) -> None:
        """Re-scan disk for skills and repopulate ``self._skill_entries``.
    
        Called once from :meth:`_register_skill_group` at startup and
        again from :meth:`refresh_skill_group` whenever the user runs
        ``/reload-skills``. No Discord API calls are made — autocomplete
        and the handler both read from these instance attributes
        directly, so an in-place mutation is sufficient.
        """
        from hermes_cli.commands import discord_skill_commands_by_category
    
        reserved = getattr(self, "_skill_group_reserved_names", set())
        categories, uncategorized, hidden = discord_skill_commands_by_category(
            reserved_names=set(reserved),
        )
        entries: list[tuple[str, str, str]] = list(uncategorized)
        for cat_skills in categories.values():
            entries.extend(cat_skills)
        # Stable alphabetical order so the autocomplete suggestion
        # list is predictable across restarts.
        entries.sort(key=lambda t: t[0])
    
        self._skill_entries = entries
        self._skill_lookup = {n: (d, k) for n, d, k in entries}
        self._skill_group_hidden_count = hidden
    
    def refresh_skill_group(self) -> tuple[int, int]:
        """Rescan skills and update the live ``/skill`` autocomplete state.
    
        Invoked by the gateway reload-skills command runtime
        after :func:`agent.skill_commands.reload_skills` has refreshed
        the in-process skill-command registry. Without this call, the
        ``/skill`` autocomplete dropdown keeps showing the list captured
        at process start — new skills stay invisible and deleted skills
        return an "Unknown skill" error when clicked.
    
        Because autocomplete options are fetched dynamically by Discord,
        we only need to mutate the entries/lookup attributes read by the
        callbacks — no ``tree.sync()`` is required.
    
        Returns ``(new_count, hidden_count)``.
        """
        try:
            self._refresh_skill_catalog_state()
        except Exception as exc:
            logger.warning(
                "[%s] Failed to refresh /skill autocomplete after reload: %s",
                self.name, exc,
            )
            return (len(getattr(self, "_skill_entries", [])), 0)
        logger.info(
            "[%s] Refreshed /skill autocomplete: %d skill(s) available (%d filtered)",
            self.name,
            len(self._skill_entries),
            self._skill_group_hidden_count,
        )
        return (len(self._skill_entries), self._skill_group_hidden_count)
    
    def _build_slash_event(self, interaction: discord.Interaction, text: str) -> MessageEvent:
        """Build a MessageEvent from a Discord slash command interaction."""
        is_dm = isinstance(interaction.channel, discord.DMChannel)
        is_thread = isinstance(interaction.channel, discord.Thread)
        thread_id = None
    
        if is_dm:
            chat_type = "dm"
        elif is_thread:
            chat_type = "thread"
            thread_id = str(interaction.channel_id)
        else:
            chat_type = "group"
    
        chat_name = ""
        if not is_dm and hasattr(interaction.channel, "name"):
            chat_name = interaction.channel.name
            if hasattr(interaction.channel, "guild") and interaction.channel.guild:
                chat_name = f"{interaction.channel.guild.name} / #{chat_name}"
    
        # Get channel topic (if available).
        # For forum threads, inherit the parent forum's topic.
        chat_topic = self._get_effective_topic(interaction.channel, is_thread=is_thread)
    
        source = self.build_source(
            chat_id=str(interaction.channel_id),
            chat_name=chat_name,
            chat_type=chat_type,
            user_id=str(interaction.user.id),
            user_name=interaction.user.display_name,
            thread_id=thread_id,
            chat_topic=chat_topic,
        )
    
        msg_type = MessageType.COMMAND if text.startswith("/") else MessageType.TEXT
        channel_id = str(interaction.channel_id)
        parent_id = str(getattr(getattr(interaction, "channel", None), "parent_id", "") or "")
        return MessageEvent(
            text=text,
            message_type=msg_type,
            source=source,
            raw_message=interaction,
            channel_prompt=self._resolve_channel_prompt(channel_id, parent_id or None),
        )
    
    async def _handle_thread_create_slash(
        self,
        interaction: discord.Interaction,
        name: str,
        message: str = "",
        auto_archive_duration: int = 1440,
    ) -> None:
        """Create a Discord thread from a slash command and start a session in it."""
        if not await self._check_slash_authorization(interaction, "/thread"):
            return
        await interaction.response.defer(ephemeral=True)
        result = await self._create_thread(
            interaction,
            name=name,
            message=message,
            auto_archive_duration=auto_archive_duration,
        )
    
        if not result.get("success"):
            error = result.get("error", "unknown error")
            await interaction.followup.send(f"Failed to create thread: {error}", ephemeral=True)
            return
    
        thread_id = result.get("thread_id")
        thread_name = result.get("thread_name") or name
    
        # Tell the user where the thread is
        link = f"<#{thread_id}>" if thread_id else f"**{thread_name}**"
        await interaction.followup.send(f"Created thread {link}", ephemeral=True)
    
        # Track thread participation so follow-ups don't require @mention
        if thread_id:
            self._threads.mark(thread_id)
    
        # If a message was provided, kick off a new Hermes session in the thread
        starter = (message or "").strip()
        if starter and thread_id:
            await self._dispatch_thread_session(interaction, thread_id, thread_name, starter)
    
    async def _dispatch_thread_session(
        self,
        interaction: discord.Interaction,
        thread_id: str,
        thread_name: str,
        text: str,
    ) -> None:
        """Build a MessageEvent pointing at a thread and send it through handle_message."""
        guild_name = ""
        if hasattr(interaction, "guild") and interaction.guild:
            guild_name = interaction.guild.name
    
        chat_name = f"{guild_name} / {thread_name}" if guild_name else thread_name
    
        # Inherit forum topic when the thread was created inside a forum channel.
        _chan = getattr(interaction, "channel", None)
        chat_topic = self._get_effective_topic(_chan, is_thread=True) if _chan else None
    
        source = self.build_source(
            chat_id=thread_id,
            chat_name=chat_name,
            chat_type="thread",
            user_id=str(interaction.user.id),
            user_name=interaction.user.display_name,
            thread_id=thread_id,
            chat_topic=chat_topic,
        )
    
        _parent_channel = self._thread_parent_channel(getattr(interaction, "channel", None))
        _parent_id = str(getattr(_parent_channel, "id", "") or "")
        _skills = self._resolve_channel_skills(thread_id, _parent_id or None)
        _channel_prompt = self._resolve_channel_prompt(thread_id, _parent_id or None)
        event = MessageEvent(
            text=text,
            message_type=MessageType.TEXT,
            source=source,
            raw_message=interaction,
            auto_skill=_skills,
            channel_prompt=_channel_prompt,
        )
        await self.handle_message(event)
