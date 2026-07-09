"""
Gateway runner - entry point for messaging platform integrations.

This module provides:
- start_gateway(): Start all configured platform adapters
- GatewayRunner: Main class managing the gateway lifecycle

Usage:
    # Start the gateway
    python -m gateway.run
    
    # Or from CLI
    python cli.py --gateway
"""

# IMPORTANT: hermes_bootstrap must be the very first import — UTF-8 stdio
# on Windows.  No-op on POSIX.  See hermes_bootstrap.py for full rationale.
try:
    import hermes_bootstrap  # noqa: F401
except ModuleNotFoundError:
    # Graceful fallback when hermes_bootstrap isn't registered in the venv
    # yet — happens during partial ``hermes update`` where git-reset landed
    # new code but ``uv pip install -e .`` didn't finish.  Missing bootstrap
    # means UTF-8 stdio setup is skipped on Windows; POSIX is unaffected.
    pass

import asyncio
import dataclasses
import json
import logging
import os
import re
import shlex
import sys
import signal
import tempfile
import time
import sqlite3
from collections import OrderedDict
from contextvars import copy_context
from pathlib import Path
from datetime import datetime
from typing import Dict, Optional, Any, List, Union

from agent.async_utils import safe_schedule_threadsafe
from agent.i18n import t
from hermes_agent.repositories.session_repo import sanitize_session_title
from hermes_agent.storage.session_availability import format_session_store_unavailable
from hermes_cli.config import cfg_get
from hermes_gateway.approval_commands import GatewayApprovalCommandMixin
from hermes_gateway.agent_turn_runtime import agent_turn_runtime_for
from hermes_gateway.agent_run_runtime import agent_run_runtime_for
from hermes_gateway.agent_run_supervision import agent_run_supervisor_for
from hermes_gateway.agent_streaming_runtime import agent_streaming_for
from hermes_gateway.agent_interaction_callbacks import agent_interaction_callbacks_for
from hermes_gateway.agent_input_preparation import agent_input_preparation_for
from hermes_gateway.agent_result_finalizer import agent_result_finalizer_for
from hermes_gateway.agent_execution_monitor import agent_execution_monitor_for
from hermes_gateway.agent_pending_followup_runtime import (
    PendingFollowupContext,
    agent_pending_followup_for,
)
from hermes_gateway.agent_turn_completion import (
    AgentTurnCleanupContext,
    agent_final_delivery_for,
    agent_turn_cleanup_for,
)
from hermes_gateway.agent_cache import (
    AGENT_PENDING_SENTINEL as _AGENT_PENDING_SENTINEL,
    agent_cache_for,
)
from hermes_gateway.assets import telegram_botfather_threads_settings_path
from hermes_gateway.background_tasks import GatewayBackgroundTaskMixin
from hermes_gateway.bootstrap import (
    ensure_ssl_certs as _ensure_ssl_certs,
    ensure_windows_gateway_venv_imports as _ensure_windows_gateway_venv_imports,
    float_env as _float_env,
    home_target_env_var as _home_target_env_var,
    home_thread_env_var as _home_thread_env_var,
    reload_runtime_env_preserving_config_authority,
    resolve_hermes_bin as _resolve_hermes_bin,
    restart_notification_pending as _restart_notification_pending_for_home,
)
from hermes_gateway.bundles_command import GatewayBundlesCommandMixin
from hermes_gateway.busy_session_runtime import busy_session_runtime_for
from hermes_gateway.codex_runtime_command import GatewayCodexRuntimeCommandMixin
from hermes_gateway.command_listing import GatewayCommandListingMixin
from hermes_gateway.conversation_editing_commands import conversation_editing_for
from hermes_gateway.compress_command import GatewayCompressCommandMixin
from hermes_gateway.debug_command import debug_command_for
from hermes_gateway.kanban_watchers import GatewayKanbanWatcherMixin
from hermes_gateway.gateway_media_warnings import media_warnings_for
from hermes_gateway.media_delivery import media_delivery_for
from hermes_gateway.message_runtime import message_runtime_for
from hermes_gateway.model_command import model_command_for
from hermes_gateway.inbound_media import GatewayInboundMediaMixin
from hermes_gateway.inbound_message_preparation import GatewayInboundMessagePreparationMixin
from hermes_gateway.insights_command import GatewayInsightsCommandMixin
from hermes_gateway.runner_initialization import initialize_gateway_runner_state
from hermes_gateway.profile_home_commands import GatewayProfileHomeCommandMixin
from hermes_gateway.process_notifications import (
    drain_gateway_watch_events as _drain_gateway_watch_events,
    format_gateway_process_notification as _format_gateway_process_notification,
)
from hermes_gateway.personality_command import personality_command_for
from hermes_gateway.platform_command import platform_command_for
from hermes_gateway.process_watcher import process_watcher_for
from hermes_gateway.proxy_mode import proxy_mode_for
from hermes_gateway.title_command import title_command_for
from hermes_gateway.tool_progress_runtime import tool_progress_for
from hermes_gateway.update_lifecycle import update_lifecycle_for
from hermes_gateway.update_restart import GatewayUpdateRestartMixin
from hermes_gateway.usage_command import usage_command_for
from hermes_gateway.verbose_command import verbose_command_for
from hermes_gateway.voice_runtime import voice_runtime_for
from hermes_gateway.yolo_command import yolo_command_for
from hermes_gateway.reset_command import GatewayResetCommandMixin
from hermes_gateway.response_normalization import (
    is_dovie_runtime_auth_failure as _is_dovie_runtime_auth_failure,
    normalize_empty_agent_response as _normalize_empty_agent_response,
)
from hermes_gateway.restart_lifecycle import restart_lifecycle_for
from hermes_gateway.runtime_status_command import runtime_status_command_for
from hermes_gateway.resume_pending import (
    should_clear_resume_pending_after_turn as _should_clear_resume_pending_after_turn,
)
from hermes_gateway.session_navigation_commands import session_navigation_for
from hermes_gateway.session_expiry_runtime import session_expiry_runtime_for
from hermes_gateway.session_handoff_runtime import session_handoff_runtime_for
from hermes_gateway.session_recovery_runtime import GatewaySessionRecoveryRuntimeMixin
from hermes_gateway.session_env_runtime import session_env_runtime_for
from hermes_gateway.session_runtime_state import session_runtime_state_for
from hermes_gateway.shutdown_runtime import GatewayShutdownRuntimeMixin
from hermes_gateway.teams_pipeline_gateway_runtime import teams_pipeline_runtime_for
from hermes_gateway.skill_hint import (
    check_unavailable_skill as _check_unavailable_skill_for_repo,
    skill_slug_from_frontmatter as _skill_slug_from_frontmatter,
)
from hermes_gateway.fast_command import fast_command_for
from hermes_gateway.footer_command import footer_command_for
from hermes_gateway.freshness import (
    auto_continue_freshness_window as _auto_continue_freshness_window,
    coerce_gateway_timestamp as _coerce_gateway_timestamp,
    is_fresh_gateway_interruption as _is_fresh_gateway_interruption,
    last_transcript_timestamp as _last_transcript_timestamp,
)
from hermes_gateway.gateway_runtime_config import runtime_config_for
from hermes_gateway.media import (
    collect_auto_append_media_tags as _collect_auto_append_media_tags,
    collect_history_media_paths as _collect_history_media_paths,
)
from hermes_gateway.output_policy import (
    gateway_platform_value as _gateway_platform_value,
    prepare_gateway_status_message as _prepare_gateway_status_message,
    redact_gateway_user_facing_secrets as _redact_gateway_user_facing_secrets,
    sanitize_gateway_final_response as _sanitize_gateway_final_response,
    telegramize_command_mentions as _telegramize_command_mentions,
)
from hermes_gateway.platform_authorization import GatewayPlatformAuthorizationMixin
from hermes_gateway.platform_notice import platform_notice_for
from hermes_gateway.platform_runtime import platform_runtime_for
from hermes_gateway.reasoning_command import reasoning_command_for
from hermes_gateway.reload_mcp_command import reload_mcp_command_for
from hermes_gateway.reload_skills_command import reload_skills_command_for
from hermes_gateway.rollback_command import rollback_command_for
from hermes_gateway.replay import (
    ASSISTANT_REPLAY_FIELDS as _ASSISTANT_REPLAY_FIELDS,
    build_replay_entry as _build_replay_entry,
)

def _redact_approval_command(cmd: "str | None") -> str:
    """Redact credentials from a command before it enters an approval prompt."""
    from hermes_agent.gateway.runtime_config import redact_approval_command

    return redact_approval_command(cmd)


# Mark this process as a gateway so cli.py's module-level load_cli_config()
# knows not to clobber TERMINAL_CWD if lazily imported.
os.environ["_HERMES_GATEWAY"] = "1"

_ensure_ssl_certs()

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent))

# Resolve Hermes home directory (respects HERMES_HOME override)
from hermes_constants import get_hermes_home
from utils import atomic_json_write, atomic_yaml_write, base_url_host_matches, is_truthy_value
_hermes_home = get_hermes_home()

# Load environment variables from ~/.hermes/.env first.
# User-managed env files should override stale shell exports on restart.
from dotenv import load_dotenv  # backward-compat for tests that monkeypatch this symbol
from hermes_cli.env_loader import load_hermes_dotenv
_env_path = _hermes_home / '.env'
load_hermes_dotenv(hermes_home=_hermes_home, project_env=Path(__file__).resolve().parents[1] / '.env')


# Bridge config.yaml values into the environment so os.getenv() picks them up.
# config.yaml is authoritative for terminal settings — overrides .env.
_config_path = _hermes_home / 'config.yaml'
if _config_path.exists():
    try:
        import yaml as _yaml
        with open(_config_path, encoding="utf-8") as _f:
            _cfg = _yaml.safe_load(_f) or {}
        # Expand ${ENV_VAR} references before bridging to env vars.
        from hermes_cli.config import _expand_env_vars
        _cfg = _expand_env_vars(_cfg)
        # Top-level simple values (fallback only — don't override .env)
        for _key, _val in _cfg.items():
            if isinstance(_val, (str, int, float, bool)) and _key not in os.environ:
                os.environ[_key] = str(_val)
        # Terminal config is nested — bridge to TERMINAL_* env vars.
        # config.yaml overrides .env for these since it's the documented config path.
        _terminal_cfg = _cfg.get("terminal", {})
        if _terminal_cfg and isinstance(_terminal_cfg, dict):
            _terminal_env_map = {
                "backend": "TERMINAL_ENV",
                "cwd": "TERMINAL_CWD",
                "timeout": "TERMINAL_TIMEOUT",
                "lifetime_seconds": "TERMINAL_LIFETIME_SECONDS",
                "docker_image": "TERMINAL_DOCKER_IMAGE",
                "docker_forward_env": "TERMINAL_DOCKER_FORWARD_ENV",
                "singularity_image": "TERMINAL_SINGULARITY_IMAGE",
                "modal_image": "TERMINAL_MODAL_IMAGE",
                "daytona_image": "TERMINAL_DAYTONA_IMAGE",
                "vercel_runtime": "TERMINAL_VERCEL_RUNTIME",
                "ssh_host": "TERMINAL_SSH_HOST",
                "ssh_user": "TERMINAL_SSH_USER",
                "ssh_port": "TERMINAL_SSH_PORT",
                "ssh_key": "TERMINAL_SSH_KEY",
                "container_cpu": "TERMINAL_CONTAINER_CPU",
                "container_memory": "TERMINAL_CONTAINER_MEMORY",
                "container_disk": "TERMINAL_CONTAINER_DISK",
                "container_persistent": "TERMINAL_CONTAINER_PERSISTENT",
                "docker_volumes": "TERMINAL_DOCKER_VOLUMES",
                "docker_env": "TERMINAL_DOCKER_ENV",
                "docker_mount_cwd_to_workspace": "TERMINAL_DOCKER_MOUNT_CWD_TO_WORKSPACE",
                "docker_run_as_host_user": "TERMINAL_DOCKER_RUN_AS_HOST_USER",
                "sandbox_dir": "TERMINAL_SANDBOX_DIR",
                "persistent_shell": "TERMINAL_PERSISTENT_SHELL",
            }
            for _cfg_key, _env_var in _terminal_env_map.items():
                if _cfg_key in _terminal_cfg:
                    _val = _terminal_cfg[_cfg_key]
                    # Skip cwd placeholder values (".", "auto", "cwd") — the
                    # gateway resolves these to Path.home() later (line ~255).
                    # Writing the raw placeholder here would just be noise.
                    # Only bridge explicit absolute paths from config.yaml.
                    if _cfg_key == "cwd" and str(_val) in {".", "auto", "cwd"}:
                        continue
                    # Expand shell tilde in cwd so subprocess.Popen never
                    # receives a literal "~/" which the kernel rejects.
                    if _cfg_key == "cwd" and isinstance(_val, str):
                        _val = os.path.expanduser(_val)
                    if isinstance(_val, (list, dict)):
                        os.environ[_env_var] = json.dumps(_val)
                    else:
                        os.environ[_env_var] = str(_val)
        # Compression config is read directly from config.yaml by run_agent.py
        # and auxiliary_client.py — no env var bridging needed.
        # Auxiliary model/direct-endpoint overrides (vision, web_extract).
        # Each task has provider/model/base_url/api_key; bridge non-default values to env vars.
        _auxiliary_cfg = _cfg.get("auxiliary", {})
        if _auxiliary_cfg and isinstance(_auxiliary_cfg, dict):
            _aux_task_env = {
                "vision": {
                    "provider": "AUXILIARY_VISION_PROVIDER",
                    "model": "AUXILIARY_VISION_MODEL",
                    "base_url": "AUXILIARY_VISION_BASE_URL",
                    "api_key": "AUXILIARY_VISION_API_KEY",
                },
                "web_extract": {
                    "provider": "AUXILIARY_WEB_EXTRACT_PROVIDER",
                    "model": "AUXILIARY_WEB_EXTRACT_MODEL",
                    "base_url": "AUXILIARY_WEB_EXTRACT_BASE_URL",
                    "api_key": "AUXILIARY_WEB_EXTRACT_API_KEY",
                },
                "approval": {
                    "provider": "AUXILIARY_APPROVAL_PROVIDER",
                    "model": "AUXILIARY_APPROVAL_MODEL",
                    "base_url": "AUXILIARY_APPROVAL_BASE_URL",
                    "api_key": "AUXILIARY_APPROVAL_API_KEY",
                },
            }
            for _task_key, _env_map in _aux_task_env.items():
                _task_cfg = _auxiliary_cfg.get(_task_key, {})
                if not isinstance(_task_cfg, dict):
                    continue
                _prov = str(_task_cfg.get("provider", "")).strip()
                _model = str(_task_cfg.get("model", "")).strip()
                _base_url = str(_task_cfg.get("base_url", "")).strip()
                _api_key = str(_task_cfg.get("api_key", "")).strip()
                if _prov and _prov != "auto":
                    os.environ[_env_map["provider"]] = _prov
                if _model:
                    os.environ[_env_map["model"]] = _model
                if _base_url:
                    os.environ[_env_map["base_url"]] = _base_url
                if _api_key:
                    os.environ[_env_map["api_key"]] = _api_key
        # config.yaml is the documented, authoritative source for these
        # settings — it unconditionally wins over .env values. Previously
        # the guards below read `if X not in os.environ` and let stale
        # .env entries (e.g. HERMES_MAX_ITERATIONS=60 written by an old
        # `hermes setup` run) silently shadow the user's current config.
        # See PR #18413 / the 60-vs-500 max_turns incident.
        _agent_cfg = _cfg.get("agent", {})
        if _agent_cfg and isinstance(_agent_cfg, dict):
            if "max_turns" in _agent_cfg:
                os.environ["HERMES_MAX_ITERATIONS"] = str(_agent_cfg["max_turns"])
            if "gateway_timeout" in _agent_cfg:
                os.environ["HERMES_AGENT_TIMEOUT"] = str(_agent_cfg["gateway_timeout"])
            if "gateway_timeout_warning" in _agent_cfg:
                os.environ["HERMES_AGENT_TIMEOUT_WARNING"] = str(_agent_cfg["gateway_timeout_warning"])
            if "gateway_notify_interval" in _agent_cfg:
                os.environ["HERMES_AGENT_NOTIFY_INTERVAL"] = str(_agent_cfg["gateway_notify_interval"])
            if "restart_drain_timeout" in _agent_cfg:
                os.environ["HERMES_RESTART_DRAIN_TIMEOUT"] = str(_agent_cfg["restart_drain_timeout"])
            if "gateway_auto_continue_freshness" in _agent_cfg:
                os.environ["HERMES_AUTO_CONTINUE_FRESHNESS"] = str(
                    _agent_cfg["gateway_auto_continue_freshness"]
                )
        _display_cfg = _cfg.get("display", {})
        if _display_cfg and isinstance(_display_cfg, dict):
            if "busy_input_mode" in _display_cfg:
                os.environ["HERMES_GATEWAY_BUSY_INPUT_MODE"] = str(_display_cfg["busy_input_mode"])
            if "busy_ack_enabled" in _display_cfg:
                os.environ["HERMES_GATEWAY_BUSY_ACK_ENABLED"] = str(_display_cfg["busy_ack_enabled"])
        # Timezone: bridge config.yaml → HERMES_TIMEZONE env var.
        _tz_cfg = _cfg.get("timezone", "")
        if _tz_cfg and isinstance(_tz_cfg, str):
            os.environ["HERMES_TIMEZONE"] = _tz_cfg.strip()
        # Security settings
        _security_cfg = _cfg.get("security", {})
        if isinstance(_security_cfg, dict):
            _redact = _security_cfg.get("redact_secrets")
            if _redact is not None:
                os.environ["HERMES_REDACT_SECRETS"] = str(_redact).lower()
    except Exception as _bridge_err:
        # Previously this was silent (`except Exception: pass`), which
        # hid partial bridge failures and let .env defaults shadow
        # config.yaml values — users observed max_turns=500 in config
        # but a 60-iteration cap in practice. Surface the failure to
        # stderr so operators see it even though `logger` is not yet
        # initialized at module-import time (logger is defined further
        # down this module).
        print(
            f"  Warning: config.yaml → env bridge failed: "
            f"{type(_bridge_err).__name__}: {_bridge_err}",
            file=sys.stderr,
        )
        print(
            "  Gateway will fall back to .env values, which may not match "
            "your current config.yaml. Run `hermes doctor` to investigate.",
            file=sys.stderr,
        )

# Apply IPv4 preference if configured (before any HTTP clients are created).
try:
    from hermes_constants import apply_ipv4_preference
    _network_cfg = (_cfg if '_cfg' in dir() else {}).get("network", {})
    if isinstance(_network_cfg, dict) and _network_cfg.get("force_ipv4"):
        apply_ipv4_preference(force=True)
except Exception as _bootstrap_exc:
    print(f"  Warning: IPv4 preference application failed: {_bootstrap_exc}", file=sys.stderr)

# Validate config structure early — log warnings so gateway operators see problems
try:
    from hermes_cli.config import print_config_warnings
    print_config_warnings()
except Exception as _bootstrap_exc:
    print(f"  Warning: config validation failed: {_bootstrap_exc}", file=sys.stderr)

# Warn if user has deprecated MESSAGING_CWD / TERMINAL_CWD in .env
try:
    from hermes_cli.config import warn_deprecated_cwd_env_vars
    warn_deprecated_cwd_env_vars()
except Exception as _bootstrap_exc:
    print(f"  Warning: deprecation check failed: {_bootstrap_exc}", file=sys.stderr)

# Gateway runs in quiet mode - suppress debug output and use cwd directly (no temp dirs)
os.environ["HERMES_QUIET"] = "1"

# Enable interactive exec approval for dangerous commands on messaging platforms
os.environ["HERMES_EXEC_ASK"] = "1"

# Set terminal working directory for messaging platforms.
# config.yaml terminal.cwd is the canonical source (bridged to TERMINAL_CWD
# by the config bridge above).  When it's unset or a placeholder, default
# to home directory.  MESSAGING_CWD is accepted as a backward-compat
# fallback (deprecated — the warning above tells users to migrate).
_configured_cwd = os.environ.get("TERMINAL_CWD", "")
if not _configured_cwd or _configured_cwd in {".", "auto", "cwd"}:
    _fallback = os.getenv("MESSAGING_CWD") or str(Path.home())
    os.environ["TERMINAL_CWD"] = _fallback

from hermes_gateway.config import (
    Platform,
    _BUILTIN_PLATFORM_VALUES,
    GatewayConfig,
    HomeChannel,
    PlatformConfig,
    load_gateway_config,
)
from hermes_gateway.session import (
    SessionSource,
    SessionContext,
    build_session_context,
    build_session_context_prompt,
    build_session_key,
    is_shared_multi_user_session,
)
from channels.platforms.base import (
    BasePlatformAdapter,
    EphemeralReply,
    MessageEvent,
    MessageType,
    _reply_anchor_for_event,
)
from hermes_gateway.restart import (
    DEFAULT_GATEWAY_RESTART_DRAIN_TIMEOUT,
    GATEWAY_SERVICE_RESTART_EXIT_CODE,
    parse_restart_drain_timeout,
)


logger = logging.getLogger(__name__)


def _resolve_runtime_agent_kwargs() -> dict:
    """Resolve provider credentials for gateway-created AIAgent instances.

    If the primary provider fails with an authentication error, attempt to
    resolve credentials using the fallback provider chain from config.yaml
    before giving up.
    """
    from hermes_agent.gateway.runtime_config import resolve_runtime_agent_kwargs

    return resolve_runtime_agent_kwargs(_hermes_home)


def _try_resolve_fallback_provider() -> dict | None:
    """Attempt to resolve credentials from the fallback_model/fallback_providers config."""
    from hermes_agent.gateway.runtime_config import _try_resolve_fallback_provider

    return _try_resolve_fallback_provider(_hermes_home)


_INTERRUPT_REASON_STOP = "Stop requested"
_INTERRUPT_REASON_RESET = "Session reset requested"
_INTERRUPT_REASON_SSE_DISCONNECT = "SSE client disconnected"
_INTERRUPT_REASON_GATEWAY_SHUTDOWN = "Gateway shutting down"
_INTERRUPT_REASON_GATEWAY_RESTART = "Gateway restarting"


def _check_unavailable_skill(command_name: str) -> str | None:
    repo_root = Path(__file__).resolve().parent.parent
    return _check_unavailable_skill_for_repo(command_name, repo_root=repo_root)


def _platform_config_key(platform: "Platform") -> str:
    """Map a Platform enum to its config.yaml key (LOCAL→"cli", rest→enum value)."""
    return "cli" if platform == Platform.LOCAL else platform.value


def _load_gateway_config() -> dict:
    """Load and parse ~/.hermes/config.yaml, returning {} on any error.

    Uses the module-level ``_hermes_home`` (so tests that monkeypatch it
    still see their fixture) and shares the mtime-keyed raw-yaml cache
    from ``hermes_cli.config.read_raw_config`` when the paths match.
    """
    from hermes_agent.gateway.runtime_config import load_gateway_runtime_config

    return load_gateway_runtime_config(_hermes_home)


def _resolve_gateway_model(config: dict | None = None) -> str:
    """Read model from config.yaml — single source of truth.

    Without this, temporary AIAgent instances (e.g. /compress) fall
    back to the hardcoded default which fails when the active provider is
    openai-codex.
    """
    from hermes_agent.gateway.runtime_config import resolve_gateway_model

    return resolve_gateway_model(config)


# Module-level alias kept for gateway.run-internal tests while P5 retires this
# module. Production callers must import hermes_gateway.runner_ref directly.
from hermes_gateway.runner_ref import gateway_runner_ref as _gateway_runner_ref


class GatewayRunner(
    GatewayApprovalCommandMixin,
    GatewayBackgroundTaskMixin,
    GatewayBundlesCommandMixin,
    GatewayCodexRuntimeCommandMixin,
    GatewayCommandListingMixin,
    GatewayCompressCommandMixin,
    GatewayPlatformAuthorizationMixin,
    GatewayProfileHomeCommandMixin,
    GatewayInboundMediaMixin,
    GatewayInboundMessagePreparationMixin,
    GatewayKanbanWatcherMixin,
    GatewayInsightsCommandMixin,
    GatewayResetCommandMixin,
    GatewaySessionRecoveryRuntimeMixin,
    GatewayShutdownRuntimeMixin,
    GatewayUpdateRestartMixin,
):
    """
    Main gateway controller.

    Manages the lifecycle of all platform adapters and routes
    messages to/from the agent.
    """

    # Class-level defaults so partial construction in tests doesn't
    # blow up on attribute access.
    _running_agents_ts: Dict[str, float] = {}
    _busy_input_mode: str = "interrupt"
    _restart_drain_timeout: float = DEFAULT_GATEWAY_RESTART_DRAIN_TIMEOUT
    _exit_code: Optional[int] = None
    _draining: bool = False
    _restart_requested: bool = False
    _restart_task_started: bool = False
    _restart_detached: bool = False
    _restart_via_service: bool = False
    _stop_task: Optional[asyncio.Task] = None
    _session_model_overrides: Dict[str, Dict[str, str]] = {}
    _session_reasoning_overrides: Dict[str, Dict[str, Any]] = {}

    def __init__(self, config: Optional[GatewayConfig] = None):
        initialize_gateway_runner_state(self, config)


    def _wire_teams_pipeline_runtime(self) -> None:
        teams_pipeline_runtime_for(self).wire_runtime()


    def _warn_if_docker_media_delivery_is_risky(self) -> None:
        media_warnings_for(self).warn_if_docker_media_delivery_is_risky()



    # -- Setup skill availability ----------------------------------------

    def _has_setup_skill(self) -> bool:
        """Check if the hermes-agent-setup skill is installed."""
        try:
            from tools.skill_manager_tool import _find_skill
            return _find_skill("hermes-agent-setup") is not None
        except Exception:
            return False

    # -- Voice mode persistence ------------------------------------------








    async def _safe_adapter_disconnect(self, adapter, platform) -> None:
        await platform_runtime_for(self).safe_adapter_disconnect(adapter, platform)

    def _adapter_disconnect_timeout_secs(self) -> float:
        return platform_runtime_for(self).adapter_disconnect_timeout_secs()

    def _platform_connect_timeout_secs(self) -> float:
        return platform_runtime_for(self).platform_connect_timeout_secs()

    async def _connect_adapter_with_timeout(self, adapter, platform) -> bool:
        return await platform_runtime_for(self).connect_adapter_with_timeout(adapter, platform)

    @property
    def should_exit_cleanly(self) -> bool:
        return self._exit_cleanly

    @property
    def should_exit_with_failure(self) -> bool:
        return self._exit_with_failure

    @property
    def exit_reason(self) -> Optional[str]:
        return self._exit_reason

    @property
    def exit_code(self) -> Optional[int]:
        return self._exit_code

    def _session_key_for_source(self, source: SessionSource) -> str:
        """Resolve the current session key for a source, honoring gateway config when available."""
        if hasattr(self, "session_store") and self.session_store is not None:
            try:
                session_key = self.session_store._generate_session_key(source)
                if isinstance(session_key, str) and session_key:
                    return session_key
            except Exception:
                pass
        config = getattr(self, "config", None)
        return build_session_key(
            source,
            group_sessions_per_user=getattr(config, "group_sessions_per_user", True),
            thread_sessions_per_user=getattr(config, "thread_sessions_per_user", False),
        )

    def _format_session_db_unavailable(self) -> str:
        return format_session_store_unavailable(
            getattr(self, "_session_db_error", None),
            prefix=t("gateway.shared.session_db_unavailable_prefix"),
        )


    # Telegram's General (pinned top) topic in forum-enabled private chats.
    # Bot API behavior varies: some clients omit message_thread_id for
    # General, others send "1". Treat both as "root" for lobby/lane purposes.













    def _request_clean_exit(self, reason: str) -> None:
        self._exit_cleanly = True
        self._exit_reason = reason
        self._shutdown_event.set()

    def _running_agent_count(self) -> int:
        return len(self._running_agents)

    def _status_action_label(self) -> str:
        return "restart" if self._restart_requested else "shutdown"

    def _status_action_gerund(self) -> str:
        return "restarting" if self._restart_requested else "shutting down"


    # -------- /queue FIFO helpers --------------------------------------
    # /queue must produce one full agent turn per invocation, in FIFO
    # order, with no merging.  The adapter's _pending_messages dict is a
    # single "next-up" slot (shared with photo-burst follow-ups), so we
    # use it for the head of the queue and an overflow list for the
    # tail.  Enqueue puts new items in the slot when free, otherwise in
    # the overflow.  Promotion (called after each run's drain) moves the
    # next overflow item into the slot so the following recursion picks
    # it up.  Clearing happens on /new and /reset via
    # _handle_reset_command.







    # ------------------------------------------------------------------
    # Per-platform circuit breaker (pause/resume) — used by the reconnect
    # watcher when a retryable failure recurs past a threshold, and by the
    # /platform pause|resume slash command for manual control.
    # ------------------------------------------------------------------


    def _snapshot_running_agents(self) -> Dict[str, Any]:
        return {
            session_key: agent
            for session_key, agent in self._running_agents.items()
            if agent is not _AGENT_PENDING_SENTINEL
        }




    def _interrupt_running_agents(self, reason: str) -> None:
        for session_key, agent in list(self._running_agents.items()):
            if agent is _AGENT_PENDING_SENTINEL:
                continue
            try:
                agent.interrupt(reason)
                logger.debug("Interrupted running agent for session %s during shutdown", session_key)
            except Exception as e:
                logger.debug("Failed interrupting agent during shutdown: %s", e)



    def _should_emit_long_running_notification(
        self,
        session_key: Optional[str],
        agent: Any,
        executor_task: Optional[Any],
    ) -> bool:
        """Only emit the heartbeat while this task still owns the live run.

        Guards against a stale ``running: delegate_task`` heartbeat outliving the
        run that started it: stop once the executor finishes, the agent is gone,
        or the session key has been rebound to a different live agent (e.g. the
        user sent ``/new`` and a fresh agent took the slot mid-run, #12029).
        """
        if agent is None:
            return False
        if executor_task is not None and executor_task.done():
            return False
        if session_key and self._running_agents.get(session_key) is not agent:
            return False
        return True







    def request_restart(self, *, detached: bool = False, via_service: bool = False) -> bool:
        if self._restart_task_started:
            return False
        self._restart_requested = True
        self._restart_detached = detached
        self._restart_via_service = via_service
        self._restart_task_started = True

        async def _run_restart() -> None:
            await asyncio.sleep(0.05)
            await self.stop(restart=True, detached_restart=detached, service_restart=via_service)

        task = asyncio.create_task(_run_restart())
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)
        return True

    # Drain-timeout reasons set by _stop_impl() when a still-running turn is
    # force-interrupted; "restart_interrupted" is set by
    # SessionStore.suspend_recently_active() on crash recovery (no
    # .clean_shutdown marker).  All three mean "the agent was mid-turn and
    # we killed it" — eligible for startup auto-resume.


    async def start(self) -> bool:
        from hermes_gateway.runner_start import start_gateway_runner

        return await start_gateway_runner(self)




    def _active_profile_name(self) -> str:
        """Return the profile name this gateway represents."""
        try:
            from hermes_cli.profiles import get_active_profile_name
            return get_active_profile_name() or "default"
        except Exception:
            return "default"


    def _kanban_advance(
        self, sub: dict, cursor: int, board: Optional[str] = None,
    ) -> None:
        """Sync helper: advance a subscription's cursor. Runs in to_thread.

        ``board`` scopes the DB connection to the board that owns this
        subscription. Unsub cursors in one board can't touch another's.
        """
        from hermes_cli import kanban_db as _kb
        conn = _kb.connect(board=board)
        try:
            _kb.advance_notify_cursor(
                conn,
                task_id=sub["task_id"],
                platform=sub["platform"],
                chat_id=sub["chat_id"],
                thread_id=sub.get("thread_id") or "",
                new_cursor=cursor,
            )
        finally:
            conn.close()

    def _kanban_unsub(self, sub: dict, board: Optional[str] = None) -> None:
        from hermes_cli import kanban_db as _kb
        conn = _kb.connect(board=board)
        try:
            _kb.remove_notify_sub(
                conn,
                task_id=sub["task_id"],
                platform=sub["platform"],
                chat_id=sub["chat_id"],
                thread_id=sub.get("thread_id") or "",
            )
        finally:
            conn.close()

    def _kanban_rewind(
        self,
        sub: dict,
        claimed_cursor: int,
        old_cursor: int,
        board: Optional[str] = None,
    ) -> None:
        """Sync helper: undo a claimed notification cursor after send failure."""
        from hermes_cli import kanban_db as _kb
        conn = _kb.connect(board=board)
        try:
            _kb.rewind_notify_cursor(
                conn,
                task_id=sub["task_id"],
                platform=sub["platform"],
                chat_id=sub["chat_id"],
                thread_id=sub.get("thread_id") or "",
                claimed_cursor=claimed_cursor,
                old_cursor=old_cursor,
            )
        finally:
            conn.close()




    async def stop(
        self,
        *,
        restart: bool = False,
        detached_restart: bool = False,
        service_restart: bool = False,
    ) -> None:
        from hermes_gateway.runner_stop import stop_gateway_runner

        await stop_gateway_runner(
            self,
            restart=restart,
            detached_restart=detached_restart,
            service_restart=service_restart,
        )

    async def wait_for_shutdown(self) -> None:
        """Wait for shutdown signal."""
        await self._shutdown_event.wait()

    def _create_adapter(
        self,
        platform: Platform,
        config: Any,
    ) -> Optional[BasePlatformAdapter]:
        return platform_runtime_for(self).create_adapter(platform, config)



    async def _handle_message(self, event: MessageEvent) -> Optional[str]:
        return await message_runtime_for(self).handle_message(event)

    def _consume_pending_native_image_paths(self, session_key: str) -> List[str]:
        pending_native = getattr(self, "_pending_native_image_paths_by_session", None)
        if not pending_native:
            return []
        return list(pending_native.pop(session_key, []) or [])

    def _cache_session_source(self, session_key: str, source) -> None:
        if not session_key or source is None:
            return
        cached_sources = getattr(self, "_session_sources", None)
        if cached_sources is None:
            cached_sources = OrderedDict()
            self._session_sources = cached_sources
        try:
            cached_sources[session_key] = dataclasses.replace(source)
        except Exception:
            logger.debug("Failed to cache live session source for %s", session_key, exc_info=True)
            return
        # LRU: mark as most-recently-used and trim to max size.
        try:
            cached_sources.move_to_end(session_key)
            max_size = getattr(self, "_session_sources_max", 512)
            while len(cached_sources) > max_size:
                cached_sources.popitem(last=False)
        except Exception:
            pass

    def _get_cached_session_source(self, session_key: str):
        if not session_key:
            return None
        cached_sources = getattr(self, "_session_sources", None)
        if not cached_sources:
            return None
        source = cached_sources.get(session_key)
        if source is not None:
            try:
                cached_sources.move_to_end(session_key)
            except Exception:
                pass
        return source

    async def _handle_message_with_agent(self, event, source, _quick_key: str, run_generation: int):
        """Inner handler that runs under the _running_agents sentinel guard."""
        return await agent_turn_runtime_for(self).handle(event, source, _quick_key, run_generation)

    def _slash_confirmation_runtime(self):
        """Build slash-confirm dependencies without owning the runtime logic."""
        from channels.slash_commands.confirmation import (
            SlashConfirmationRuntime,
            counter_id_factory,
        )

        counter = getattr(self, "_slash_confirm_counter", None)
        if counter is None:
            import itertools as _itertools

            counter = _itertools.count(1)
            self._slash_confirm_counter = counter
        return SlashConfirmationRuntime(
            adapters=self.adapters,
            session_key_for_source=self._session_key_for_source,
            read_user_config=self._read_user_config,
            save_config_value=self._save_config_value,
            thread_metadata_for_source=self._thread_metadata_for_source,
            reply_anchor_for_event=self._reply_anchor_for_event,
            confirm_id_factory=counter_id_factory(counter),
        )

    @staticmethod
    def _save_config_value(key_path: str, value: Any) -> Any:
        from cli import save_config_value

        return save_config_value(key_path, value)

    def _read_user_config(self) -> Dict[str, Any]:
        """Read the user's raw config.yaml (cached) for gate lookups.

        Used by slash-confirm gates that must reflect on-disk state changes
        (e.g. a prior "Always Approve" click) without a gateway restart.
        """
        try:
            from hermes_cli.config import load_config
            cfg = load_config()
            return cfg if isinstance(cfg, dict) else {}
        except Exception:
            return {}

    def _thread_metadata_for_source(
        self,
        source,
        reply_to_message_id: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Build the metadata dict platforms need for thread-aware replies."""
        thread_id = getattr(source, "thread_id", None)
        if thread_id is None:
            return None
        metadata: Dict[str, Any] = {"thread_id": thread_id}
        if (
            getattr(source, "platform", None) == Platform.TELEGRAM
            and getattr(source, "chat_type", None) == "dm"
        ):
            metadata["telegram_dm_topic_reply_fallback"] = True
            # Telegram DM topic lanes need direct_messages_topic_id in metadata
            # so synthetic/queued messages (goal continuations, status notices)
            # route to the correct topic even when reply anchor is unavailable.
            tid = str(thread_id)
            if tid and tid not in {"", "1"}:
                metadata["direct_messages_topic_id"] = tid
            anchor = reply_to_message_id or getattr(source, "message_id", None)
            if anchor is not None:
                metadata["telegram_reply_to_message_id"] = str(anchor)
        return metadata

    @staticmethod
    def _reply_anchor_for_event(event: MessageEvent) -> Optional[str]:
        """Return the platform-specific reply anchor for GatewayRunner sends."""
        return _reply_anchor_for_event(event)


    # ------------------------------------------------------------------
    # /approve & /deny — explicit dangerous-command approval
    # ------------------------------------------------------------------

    _APPROVAL_TIMEOUT_SECONDS = 300  # 5 minutes



    # Platforms where /update is allowed.  ACP, API server, and webhooks are
    # programmatic interfaces that should not trigger system updates.
    _UPDATE_ALLOWED_PLATFORMS = frozenset({
        Platform.TELEGRAM, Platform.DISCORD, Platform.SLACK, Platform.WHATSAPP,
        Platform.SIGNAL, Platform.MATTERMOST, Platform.MATRIX,
        Platform.HOMEASSISTANT, Platform.EMAIL, Platform.SMS, Platform.DINGTALK,
        Platform.FEISHU, Platform.WECOM, Platform.WECOM_CALLBACK, Platform.WEIXIN, Platform.BLUEBUBBLES, Platform.QQBOT, Platform.LOCAL,
    })








    def _set_session_env(self, context: SessionContext) -> list:
        return session_env_runtime_for(self).set_session_env(context)

    def _clear_session_env(self, tokens: list) -> None:
        session_env_runtime_for(self).clear_session_env(tokens)

    async def _run_in_executor_with_context(self, func, *args):
        """Run blocking work in the thread pool while preserving session contextvars."""
        loop = asyncio.get_running_loop()
        ctx = copy_context()
        return await loop.run_in_executor(None, ctx.run, func, *args)

    def _decide_image_input_mode(self) -> str:
        """Resolve the image-input routing for the currently active model.

        Returns ``"native"`` (attach pixels on the user turn) or ``"text"``
        (pre-analyze with vision_analyze and prepend the description). See
        agent/image_routing.py for the full decision table.

        The active provider/model are read from config.yaml so the decision
        tracks ``/model`` switches automatically on the next message.
        """
        try:
            from agent.image_routing import decide_image_input_mode
            from agent.auxiliary_client import _read_main_model, _read_main_provider
            from hermes_cli.config import load_config

            cfg = load_config()
            provider = _read_main_provider()
            model = _read_main_model()
            return decide_image_input_mode(provider, model, cfg)
        except Exception as exc:
            logger.debug("image_routing: decision failed, falling back to text — %s", exc)
            return "text"

    _MAX_INTERRUPT_DEPTH = 3  # Cap recursive interrupt handling (#816)









    async def _interrupt_and_clear_session(
        self,
        session_key: str,
        source: SessionSource,
        *,
        interrupt_reason: str,
        invalidation_reason: str,
        release_running_state: bool = True,
    ) -> None:
        """Interrupt the current run and clear queued session state consistently."""
        if not session_key:
            return
        running_agent = self._running_agents.get(session_key)
        if running_agent and running_agent is not _AGENT_PENDING_SENTINEL:
            running_agent.interrupt(interrupt_reason)
        session_runtime_state_for(self).invalidate_session_run_generation(session_key, reason=invalidation_reason)
        adapter = self.adapters.get(source.platform)
        if adapter and hasattr(adapter, "interrupt_session_activity"):
            await adapter.interrupt_session_activity(session_key, source.chat_id)
        if adapter and hasattr(adapter, "get_pending_message"):
            adapter.get_pending_message(session_key)  # consume and discard
        self._pending_messages.pop(session_key, None)
        if release_running_state:
            session_runtime_state_for(self).release_running_agent_state(session_key)

    # ------------------------------------------------------------------
    # Proxy mode: forward messages to a remote Hermes API server
    # ------------------------------------------------------------------


    # ------------------------------------------------------------------

    async def _run_agent(
        self,
        message: str,
        context_prompt: str,
        history: List[Dict[str, Any]],
        source: SessionSource,
        session_id: str,
        session_key: str = None,
        run_generation: Optional[int] = None,
        _interrupt_depth: int = 0,
        event_message_id: Optional[str] = None,
        channel_prompt: Optional[str] = None,
    ) -> Dict[str, Any]:
        return await agent_run_runtime_for(self).run(
            message=message,
            context_prompt=context_prompt,
            history=history,
            source=source,
            session_id=session_id,
            session_key=session_key,
            run_generation=run_generation,
            _interrupt_depth=_interrupt_depth,
            event_message_id=event_message_id,
            channel_prompt=channel_prompt,
        )



async def start_gateway(config: Optional[GatewayConfig] = None, replace: bool = False, verbosity: Optional[int] = 0) -> bool:
    """Start the gateway process with the concrete GatewayRunner implementation."""
    from hermes_gateway.gateway_process import start_gateway_process

    return await start_gateway_process(
        runner_cls=GatewayRunner,
        config=config,
        replace=replace,
        verbosity=verbosity,
        hermes_home=_hermes_home,
        ensure_windows_gateway_venv_imports=_ensure_windows_gateway_venv_imports,
    )


def main():
    """CLI entry point for the gateway."""
    # Force UTF-8 stdio on Windows — gateway logs and startup banner would
    # otherwise UnicodeEncodeError on cp1252 consoles.  No-op on POSIX.
    try:
        from hermes_cli.stdio import configure_windows_stdio
        configure_windows_stdio()
    except Exception:
        pass

    import argparse
    
    parser = argparse.ArgumentParser(description="Hermes Gateway - Multi-platform messaging")
    parser.add_argument("--config", "-c", help="Path to gateway config file")
    parser.add_argument("--verbose", "-v", action="store_true", help="Verbose output")
    
    args = parser.parse_args()
    
    config = None
    if args.config:
        import yaml
        with open(args.config, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
            config = GatewayConfig.from_dict(data)
    
    # Run the gateway - exit with code 1 if no platforms connected,
    # so systemd Restart=on-failure will retry on transient errors (e.g. DNS)
    success = asyncio.run(start_gateway(config))
    if not success:
        sys.exit(1)


if __name__ == "__main__":
    main()
