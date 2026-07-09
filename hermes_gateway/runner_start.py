"""Gateway runner startup owner."""

from __future__ import annotations

import asyncio
import logging
import os
import time

from hermes_constants import get_hermes_home
from hermes_gateway.bootstrap import restart_notification_pending as _restart_notification_pending_for_home
from hermes_gateway.busy_session_runtime import busy_session_runtime_for
from hermes_gateway.config import Platform
from hermes_gateway.platform_runtime import platform_runtime_for
from hermes_gateway.process_watcher import process_watcher_for
from hermes_gateway.runtime_status_writer import runtime_status_for
from hermes_gateway.session_expiry_runtime import session_expiry_runtime_for
from hermes_gateway.session_handoff_runtime import session_handoff_runtime_for

logger = logging.getLogger(__name__)
_hermes_home = get_hermes_home()


async def start_gateway_runner(runner) -> bool:
    self = runner
    """
    Start the gateway and all configured platform adapters.

    Returns True if at least one adapter connected successfully.
    """
    logger.info("Starting Hermes Gateway...")
    try:
        self._gateway_loop = asyncio.get_running_loop()
    except RuntimeError:
        self._gateway_loop = None
    logger.info("Session storage: %s", self.config.sessions_dir)

    # Sanity-check that systemd's TimeoutStopSec covers our drain
    # window.  When the user upgraded hermes-agent without re-running
    # ``hermes setup``, their unit file may still encode the old
    # default — in which case SIGKILL hits mid-drain and looks like
    # a phantom kill in the journal.  Best-effort, never raises.
    try:
        from hermes_gateway.shutdown_forensics import check_systemd_timing_alignment
        _alignment = check_systemd_timing_alignment(self._restart_drain_timeout)
        if _alignment is not None and _alignment.get("mismatch"):
            logger.warning(
                "Stale systemd unit detected: %s has TimeoutStopSec=%.0fs but "
                "drain_timeout=%.0fs (expected >=%.0fs). systemd may SIGKILL the "
                "gateway mid-drain. Run `hermes gateway service install --replace` "
                "to regenerate the unit, or shorten agent.restart_drain_timeout.",
                _alignment.get("unit", "(unknown)"),
                _alignment["timeout_stop_sec"],
                _alignment["drain_timeout"],
                _alignment["expected_min"],
            )
    except Exception as _e:
        logger.debug("check_systemd_timing_alignment failed: %s", _e)
    # Log the resolved max_iterations budget so operators can verify the
    # config.yaml → env bridge did the right thing at a glance (instead
    # of silently running at a stale .env value for weeks).
    try:
        _effective_max_iter = int(os.getenv("HERMES_MAX_ITERATIONS", "90"))
        logger.info(
            "Agent budget: max_iterations=%d (agent.max_turns from config.yaml, "
            "or HERMES_MAX_ITERATIONS from .env, or default 90)",
            _effective_max_iter,
        )
    except Exception:
        logger.debug("Suppressed recoverable gateway exception", exc_info=True)
    # Redaction status: ON by default (#17691). Surface a prominent
    # warning if an operator has explicitly opted out so they don't
    # forget the downgrade is active — the redactor snapshots its
    # state at import time, so this log line is the source of truth
    # for this process's lifetime.
    try:
        _redact_raw = os.getenv("HERMES_REDACT_SECRETS", "true")
        _redact_on = _redact_raw.lower() in {"1", "true", "yes", "on"}
        if _redact_on:
            logger.info(
                "Secret redaction: ENABLED (tool output, logs, and chat "
                "responses are scrubbed before delivery)"
            )
        else:
            logger.warning(
                "Secret redaction: DISABLED (HERMES_REDACT_SECRETS=%s). "
                "API keys and tokens may appear verbatim in chat output, "
                "session JSONs, and logs. Set security.redact_secrets: true "
                "in config.yaml to re-enable.",
                _redact_raw,
            )
    except Exception:
        logger.debug("Suppressed recoverable gateway exception", exc_info=True)
    try:
        from hermes_cli.profiles import get_active_profile_name
        _profile = get_active_profile_name()
        if _profile and _profile != "default":
            logger.info("Active profile: %s", _profile)
    except Exception:
        logger.debug("Suppressed recoverable gateway exception", exc_info=True)
    try:
        from channels.runtime_status import write_runtime_status
        write_runtime_status(gateway_state="starting", exit_reason=None)
    except Exception:
        logger.debug("Suppressed recoverable gateway exception", exc_info=True)

    # Log any active supply-chain security advisories. Operators see this
    # in gateway.log and `hermes status` surfaces it; we do NOT block
    # startup or surface it inline to user messages, since the gateway
    # operator is the one who can act on it (uninstall the package,
    # rotate credentials).  See hermes_cli/security_advisories.py.
    try:
        from hermes_cli.security_advisories import (
            detect_compromised,
            gateway_log_message,
        )
        _adv_hits = detect_compromised()
        _adv_msg = gateway_log_message(_adv_hits)
        if _adv_msg:
            logger.warning("%s", _adv_msg)
            logger.warning(
                "Run `hermes doctor` on the gateway host for full "
                "remediation steps."
            )
    except Exception:
        logger.debug(
            "security advisory check failed at gateway startup",
            exc_info=True,
        )

    # Warn if no user allowlists are configured and open access is not opted in
    _builtin_allowed_vars = (
        "TELEGRAM_ALLOWED_USERS", "DISCORD_ALLOWED_USERS",
        "WHATSAPP_ALLOWED_USERS", "SLACK_ALLOWED_USERS",
        "SIGNAL_ALLOWED_USERS", "SIGNAL_GROUP_ALLOWED_USERS",
        "TELEGRAM_GROUP_ALLOWED_USERS",
        "TELEGRAM_GROUP_ALLOWED_CHATS",
        "EMAIL_ALLOWED_USERS",
        "SMS_ALLOWED_USERS", "MATTERMOST_ALLOWED_USERS",
        "MATRIX_ALLOWED_USERS", "DINGTALK_ALLOWED_USERS",
        "FEISHU_ALLOWED_USERS",
        "WECOM_ALLOWED_USERS",
        "WECOM_CALLBACK_ALLOWED_USERS",
        "WEIXIN_ALLOWED_USERS",
        "BLUEBUBBLES_ALLOWED_USERS",
        "QQ_ALLOWED_USERS",
        "YUANBAO_ALLOWED_USERS",
        "GATEWAY_ALLOWED_USERS",
    )
    _builtin_allow_all_vars = (
        "TELEGRAM_ALLOW_ALL_USERS", "DISCORD_ALLOW_ALL_USERS",
        "WHATSAPP_ALLOW_ALL_USERS", "SLACK_ALLOW_ALL_USERS",
        "SIGNAL_ALLOW_ALL_USERS", "EMAIL_ALLOW_ALL_USERS",
        "SMS_ALLOW_ALL_USERS", "MATTERMOST_ALLOW_ALL_USERS",
        "MATRIX_ALLOW_ALL_USERS", "DINGTALK_ALLOW_ALL_USERS",
        "FEISHU_ALLOW_ALL_USERS",
        "WECOM_ALLOW_ALL_USERS",
        "WECOM_CALLBACK_ALLOW_ALL_USERS",
        "WEIXIN_ALLOW_ALL_USERS",
        "BLUEBUBBLES_ALLOW_ALL_USERS",
        "QQ_ALLOW_ALL_USERS",
        "YUANBAO_ALLOW_ALL_USERS",
    )
    # Also pick up plugin-registered platforms — each entry can declare
    # its own allowed_users_env / allow_all_env, so the warning stays
    # accurate as plugins like IRC come online.
    _plugin_allowed_vars: tuple = ()
    _plugin_allow_all_vars: tuple = ()
    try:
        from channels.platform_registry import platform_registry
        _plugin_allowed_vars = tuple(
            e.allowed_users_env for e in platform_registry.plugin_entries()
            if e.allowed_users_env
        )
        _plugin_allow_all_vars = tuple(
            e.allow_all_env for e in platform_registry.plugin_entries()
            if e.allow_all_env
        )
    except Exception:
        logger.debug("Suppressed recoverable gateway exception", exc_info=True)
    _any_allowlist = any(
        os.getenv(v) for v in _builtin_allowed_vars + _plugin_allowed_vars
    )
    _allow_all = os.getenv("GATEWAY_ALLOW_ALL_USERS", "").lower() in {"true", "1", "yes"} or any(
        os.getenv(v, "").lower() in {"true", "1", "yes"}
        for v in _builtin_allow_all_vars + _plugin_allow_all_vars
    )
    if not _any_allowlist and not _allow_all:
        logger.warning(
            "No user allowlists configured. All unauthorized users will be denied. "
            "Set GATEWAY_ALLOW_ALL_USERS=true in ~/.hermes/.env to allow open access, "
            "or configure platform allowlists (e.g., TELEGRAM_ALLOWED_USERS=your_id)."
        )

    # Discover Python plugins before shell hooks so plugin block
    # decisions take precedence in tie cases.  The CLI startup path
    # does this via an explicit call in hermes_cli/main.py; the
    # gateway lazily imports run_agent inside per-request handlers,
    # so the discover_plugins() side-effect in model_tools.py is NOT
    # guaranteed to have run by the time we reach this point.
    try:
        from hermes_cli.plugins import discover_plugins
        discover_plugins()
    except Exception:
        logger.warning(
            "plugin discovery failed at gateway startup", exc_info=True,
        )

    # Register declarative shell hooks from cli-config.yaml.  Gateway
    # has no TTY, so consent has to come from one of the three opt-in
    # channels (--accept-hooks on launch, HERMES_ACCEPT_HOOKS env var,
    # or hooks_auto_accept: true in config.yaml).  We pass
    # accept_hooks=False here and let register_from_config resolve
    # the effective value from env + config itself — the CLI-side
    # registration already honored --accept-hooks, and re-reading
    # hooks_auto_accept here would just duplicate that lookup.
    # Failures are logged but must never block gateway startup.
    try:
        from hermes_cli.config import load_config
        from agent.shell_hooks import register_from_config
        register_from_config(load_config(), accept_hooks=False)
    except Exception:
        logger.debug(
            "shell-hook registration failed at gateway startup",
            exc_info=True,
        )

    # Discover and load event hooks
    self.hooks.discover_and_load()


    # Recover background processes from checkpoint (crash recovery)
    try:
        from tools.process_registry import process_registry
        recovered = process_registry.recover_from_checkpoint()
        if recovered:
            logger.info("Recovered %s background process(es) from previous run", recovered)
    except Exception as e:
        logger.warning("Process checkpoint recovery: %s", e)

    # Suspend sessions that were active when the gateway last exited.
    # This prevents stuck sessions from being blindly resumed on restart,
    # which can create an unrecoverable loop (#7536).  Suspended sessions
    # auto-reset on the next incoming message, giving the user a clean start.
    #
    # SKIP suspension after a clean (graceful) shutdown — the previous
    # process already drained active agents, so sessions aren't stuck.
    # This prevents unwanted auto-resets after `hermes update`,
    # `hermes gateway restart`, or `/restart`.
    _clean_marker = _hermes_home / ".clean_shutdown"
    if _clean_marker.exists():
        logger.info("Previous gateway exited cleanly — skipping session suspension")
        try:
            _clean_marker.unlink()
        except Exception:
            logger.debug("Suppressed recoverable gateway exception", exc_info=True)
    else:
        try:
            suspended = self.session_store.suspend_recently_active()
            if suspended:
                logger.info("Marked %d in-flight session(s) as resumable from previous run", suspended)
        except Exception as e:
            logger.warning("Session suspension on startup failed: %s", e)

    # Stuck-loop detection (#7536): if a session has been active across
    # 3+ consecutive restarts, it's probably stuck in a loop (the same
    # history keeps causing the agent to hang).  Auto-suspend it so the
    # user gets a clean slate on the next message.
    try:
        stuck = self._suspend_stuck_loop_sessions()
        if stuck:
            logger.warning("Auto-suspended %d stuck-loop session(s)", stuck)
    except Exception as e:
        logger.debug("Stuck-loop detection failed: %s", e)

    connected_count = 0
    enabled_platform_count = 0
    startup_nonretryable_errors: list[str] = []
    startup_retryable_errors: list[str] = []

    # Initialize and connect each configured platform
    for platform, platform_config in self.config.platforms.items():
        if not platform_config.enabled:
            continue
        enabled_platform_count += 1

        adapter = self._create_adapter(platform, platform_config)
        if not adapter:
            # Distinguish between missing builtin deps and missing plugin
            _pval = platform.value
            _builtin_names = {m.value for m in Platform.__members__.values()}
            if _pval not in _builtin_names:
                logger.warning(
                    "No adapter for '%s' — is the plugin installed? "
                    "(platform is enabled in config.yaml but no plugin registered it)",
                    _pval,
                )
            else:
                logger.warning("No adapter available for %s", _pval)
            continue

        # Set up message + fatal error handlers
        adapter.set_message_handler(self._handle_message)
        adapter.set_fatal_error_handler(platform_runtime_for(self).handle_adapter_fatal_error)
        adapter.set_session_store(self.session_store)
        adapter.set_busy_session_handler(busy_session_runtime_for(self).handle_active_session_busy_message)

        # Try to connect
        logger.info("Connecting to %s...", platform.value)
        runtime_status_for(self).update_platform_runtime_status(
            platform.value,
            platform_state="connecting",
            error_code=None,
            error_message=None,
        )
        try:
            success = await self._connect_adapter_with_timeout(adapter, platform)
            if success:
                self.adapters[platform] = adapter
                from hermes_gateway.voice_runtime import voice_runtime_for

                voice_runtime_for(self).sync_voice_mode_state_to_adapter(adapter)
                connected_count += 1
                runtime_status_for(self).update_platform_runtime_status(
                    platform.value,
                    platform_state="connected",
                    error_code=None,
                    error_message=None,
                )
                logger.info("✓ %s connected", platform.value)
            else:
                logger.warning("✗ %s failed to connect", platform.value)
                # Defensive cleanup: a failed connect() may have
                # allocated resources (aiohttp.ClientSession, poll
                # tasks, bridge subprocesses) before giving up.
                # Without this call, those resources are orphaned
                # and Python logs "Unclosed client session" at
                # process exit. Adapter disconnect() implementations
                # are expected to be idempotent and tolerate
                # partial-init state.
                await self._safe_adapter_disconnect(adapter, platform)
                if adapter.has_fatal_error:
                    runtime_status_for(self).update_platform_runtime_status(
                        platform.value,
                        platform_state="retrying" if adapter.fatal_error_retryable else "fatal",
                        error_code=adapter.fatal_error_code,
                        error_message=adapter.fatal_error_message,
                    )
                    target = (
                        startup_retryable_errors
                        if adapter.fatal_error_retryable
                        else startup_nonretryable_errors
                    )
                    target.append(
                        f"{platform.value}: {adapter.fatal_error_message}"
                    )
                    # Queue for reconnection if the error is retryable
                    if adapter.fatal_error_retryable:
                        self._failed_platforms[platform] = {
                            "config": platform_config,
                            "attempts": 1,
                            "next_retry": time.monotonic() + 30,
                        }
                else:
                    runtime_status_for(self).update_platform_runtime_status(
                        platform.value,
                        platform_state="retrying",
                        error_code=None,
                        error_message="failed to connect",
                    )
                    startup_retryable_errors.append(
                        f"{platform.value}: failed to connect"
                    )
                    # No fatal error info means likely a transient issue — queue for retry
                    self._failed_platforms[platform] = {
                        "config": platform_config,
                        "attempts": 1,
                        "next_retry": time.monotonic() + 30,
                    }
        except Exception as e:
            logger.error("✗ %s error: %s", platform.value, e)
            # Same defensive cleanup path for exceptions — an adapter
            # that raised mid-connect may still have a live
            # aiohttp.ClientSession or child subprocess.
            await self._safe_adapter_disconnect(adapter, platform)
            runtime_status_for(self).update_platform_runtime_status(
                platform.value,
                platform_state="retrying",
                error_code=None,
                error_message=str(e),
            )
            startup_retryable_errors.append(f"{platform.value}: {e}")
            # Unexpected exceptions are typically transient — queue for retry
            self._failed_platforms[platform] = {
                "config": platform_config,
                "attempts": 1,
                "next_retry": time.monotonic() + 30,
            }

    if connected_count == 0:
        if startup_nonretryable_errors:
            reason = "; ".join(startup_nonretryable_errors)
            logger.error("Gateway hit a non-retryable startup conflict: %s", reason)
            try:
                from channels.runtime_status import write_runtime_status
                write_runtime_status(gateway_state="startup_failed", exit_reason=reason)
            except Exception:
                logger.debug("Suppressed recoverable gateway exception", exc_info=True)
            self._request_clean_exit(reason)
            return True
        if enabled_platform_count > 0:
            if startup_retryable_errors:
                # All enabled platforms hit retryable failures (network
                # blip, bridge not paired, npm install timeout, etc.).
                # Keep the gateway alive so:
                #   • cron jobs still run
                #   • the reconnect watcher gets a chance to recover the
                #     failing platforms once the underlying problem is
                #     fixed (e.g. user runs `hermes whatsapp`, fixes
                #     proxy, etc.)
                # Exiting here used to convert a single misconfigured
                # platform into an infinite systemd restart loop.
                reason = "; ".join(startup_retryable_errors)
                logger.warning(
                    "Gateway started with no connected platforms — "
                    "%d platform(s) queued for retry: %s",
                    len(self._failed_platforms), reason,
                )
                try:
                    from channels.runtime_status import write_runtime_status
                    write_runtime_status(
                        gateway_state="degraded",
                        exit_reason=None,
                    )
                except Exception:
                    logger.debug("Suppressed recoverable gateway exception", exc_info=True)
                # Fall through to the normal "running" state — reconnect
                # watcher takes it from here.
            # All enabled platforms had no adapter (missing library or credentials).
            # In fleet deployments the same config.yaml is shared across nodes that
            # may only have credentials for a subset of platforms.  Rather than
            # failing hard, degrade gracefully and allow cron jobs to run (#5196).
            logger.warning(
                "No adapter could be created for any of the %d configured platform(s). "
                "Check that required dependencies are installed and credentials are set. "
                "Gateway will continue for cron job execution.",
                enabled_platform_count,
            )
        else:
            logger.warning("No messaging platforms enabled.")
            logger.info("Gateway will continue running for cron job execution.")

    # Update delivery router with adapters
    self.delivery_router.adapters = self.adapters
    self._wire_teams_pipeline_runtime()

    self._running = True
    runtime_status_for(self).update_runtime_status("running")

    # Emit gateway:startup hook
    hook_count = len(self.hooks.loaded_hooks)
    if hook_count:
        logger.info("%s hook(s) loaded", hook_count)
    await self.hooks.emit("gateway:startup", {
        "platforms": [p.value for p in self.adapters.keys()],
    })

    if connected_count > 0:
        logger.info("Gateway running with %s platform(s)", connected_count)

    # Build initial channel directory for send_message name resolution
    try:
        from hermes_gateway.channel_directory import build_channel_directory
        directory = await build_channel_directory(self.adapters)
        ch_count = sum(len(chs) for chs in directory.get("platforms", {}).values())
        logger.info("Channel directory built: %d target(s)", ch_count)
    except Exception as e:
        logger.warning("Channel directory build failed: %s", e)

    # Check if we're restarting after a /update command. If the update is
    # still running, keep watching so we notify once it actually finishes.
    from hermes_gateway.update_lifecycle import update_lifecycle_for

    update_lifecycle = update_lifecycle_for(self)
    notified = await update_lifecycle.send_update_notification()
    if not notified and any(
        path.exists()
        for path in (
            _hermes_home / ".update_pending.json",
            _hermes_home / ".update_pending.claimed.json",
        )
    ):
        update_lifecycle.schedule_update_notification_watch()

    # Give freshly connected platform adapters a brief moment to settle
    # before sending restart/startup lifecycle messages. In practice this
    # helps Discord thread deliveries right after reconnect.
    if connected_count > 0:
        await asyncio.sleep(1.0)

    # Notify the chat that initiated /restart that the gateway is back.
    restart_notification_pending = _restart_notification_pending_for_home(_hermes_home)
    from hermes_gateway.restart_lifecycle import restart_lifecycle_for

    restart_lifecycle = restart_lifecycle_for(self)
    delivered_restart_target = await restart_lifecycle.send_restart_notification()

    # Broadcast a lightweight "gateway is back" message to configured
    # home channels only when this startup is resuming from /restart. If a
    # /restart requester already received a direct completion notice in the
    # same chat, skip the generic broadcast there to avoid duplicates while
    # still allowing a home-channel fallback when the direct send fails.
    if restart_notification_pending or delivered_restart_target is not None:
        skip_home_targets = (
            {delivered_restart_target} if delivered_restart_target else None
        )
        await restart_lifecycle.send_home_channel_startup_notifications(
            skip_targets=skip_home_targets,
        )

    # Automatically continue fresh sessions that were interrupted by the
    # previous gateway restart/shutdown.  The resume_pending flag is cleared
    # by the normal successful-turn path, so a failed auto-resume remains
    # visible for manual recovery on the next user message.
    self._schedule_resume_pending_sessions()

    # Drain any recovered process watchers (from crash recovery checkpoint)
    try:
        from tools.process_registry import process_registry
        while process_registry.pending_watchers:
            watcher = process_registry.pending_watchers.pop(0)
            asyncio.create_task(process_watcher_for(self).run_process_watcher(watcher))
            logger.info("Resumed watcher for recovered process %s", watcher.get("session_id"))
    except Exception as e:
        logger.error("Recovered watcher setup error: %s", e)

    # Start background session expiry watcher to finalize expired sessions
    asyncio.create_task(session_expiry_runtime_for(self).session_expiry_watcher())

    # Start background kanban notifier — delivers `completed`, `blocked`,
    # `spawn_auto_blocked`, and `crashed` events to gateway subscribers
    # so human-in-the-loop workflows hear back without polling.
    asyncio.create_task(self._kanban_notifier_watcher())

    # Start background kanban dispatcher — spawns workers for ready
    # tasks. Gated by `kanban.dispatch_in_gateway` (default True).
    # When false, users run `hermes kanban daemon` externally or
    # simply don't use kanban; this loop becomes a no-op.
    asyncio.create_task(self._kanban_dispatcher_watcher())

    # Start background reconnection watcher for platforms that failed at startup
    if self._failed_platforms:
        logger.info(
            "Starting reconnection watcher for %d failed platform(s): %s",
            len(self._failed_platforms),
            ", ".join(p.value for p in self._failed_platforms),
        )
    asyncio.create_task(platform_runtime_for(self).platform_reconnect_watcher())

    # Start background handoff watcher — picks up CLI sessions marked
    # handoff_state='pending' in state.db and re-binds them to the
    # destination platform's home channel, then forges a synthetic user
    # turn so the agent kicks off the new chat.
    asyncio.create_task(session_handoff_runtime_for(self).handoff_watcher())

    # Start background async-delegation watcher — drains completion events
    # from delegate_task(background=true) subagents and injects each
    # result back into its originating session as a new turn, covering the
    # idle case where the subagent finishes with no agent turn running.
    asyncio.create_task(process_watcher_for(self).async_delegation_watcher())

    logger.info("Press Ctrl+C to stop")

    return True
