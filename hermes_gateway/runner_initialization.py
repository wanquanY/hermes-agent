"""GatewayRunner state initialization."""

from __future__ import annotations

import asyncio
import itertools
import logging
import threading
from collections import OrderedDict
from typing import Any, Dict, List, Optional

from channels.platforms.base import BasePlatformAdapter, MessageEvent
from hermes_gateway.config import GatewayConfig, Platform, load_gateway_config
from hermes_gateway.delivery import DeliveryRouter
from hermes_gateway.fast_command import fast_command_for
from hermes_gateway.gateway_runtime_config import runtime_config_for
from hermes_gateway.pairing import PairingStore
from hermes_gateway.reasoning_command import reasoning_command_for
from hermes_gateway.session import SessionSource, SessionStore
from hermes_gateway.voice_runtime import voice_runtime_for
from hermes_gateway.runner_ref import set_gateway_runner

logger = logging.getLogger(__name__)


def initialize_gateway_runner_state(
    runner,
    config: Optional[GatewayConfig] = None,
) -> None:
    runner.config = config or load_gateway_config()
    runner.adapters: Dict[Platform, BasePlatformAdapter] = {}
    runner._warn_if_docker_media_delivery_is_risky()
    set_gateway_runner(runner)

    runner._prefill_messages = runtime_config_for(runner).load_prefill_messages()
    runner._ephemeral_system_prompt = runtime_config_for(
        runner
    ).load_ephemeral_system_prompt()
    runner._reasoning_config = runtime_config_for(runner).load_reasoning_config()
    runner._service_tier = fast_command_for(runner).load_service_tier()
    runner._show_reasoning = reasoning_command_for(runner).load_show_reasoning()
    runner._busy_input_mode = runtime_config_for(runner).load_busy_input_mode()
    runner._restart_drain_timeout = runtime_config_for(
        runner
    ).load_restart_drain_timeout()
    runner._provider_routing = runtime_config_for(runner).load_provider_routing()
    runner._fallback_model = runtime_config_for(runner).load_fallback_model()

    from tools.process_registry import process_registry

    runner.session_store = SessionStore(
        runner.config.sessions_dir,
        runner.config,
        has_active_processes_fn=lambda key: process_registry.has_active_for_session(key),
    )
    runner.delivery_router = DeliveryRouter(runner.config)
    runner._running = False
    runner._gateway_loop: Optional[asyncio.AbstractEventLoop] = None
    runner._shutdown_event = asyncio.Event()
    runner._exit_cleanly = False
    runner._exit_with_failure = False
    runner._exit_reason: Optional[str] = None
    runner._exit_code: Optional[int] = None
    runner._draining = False
    runner._restart_requested = False
    runner._restart_task_started = False
    runner._restart_detached = False
    runner._restart_via_service = False
    runner._stop_task: Optional[asyncio.Task] = None

    runner._running_agents: Dict[str, Any] = {}
    runner._running_agents_ts: Dict[str, float] = {}
    runner._pending_messages: Dict[str, str] = {}
    runner._queued_events: Dict[str, List[MessageEvent]] = {}
    runner._pending_native_image_paths_by_session: Dict[str, List[str]] = {}
    runner._busy_ack_ts: Dict[str, float] = {}
    runner._session_run_generation: Dict[str, int] = {}
    runner._session_sources: OrderedDict[str, SessionSource] = OrderedDict()
    runner._session_sources_max = 512

    runner._agent_cache: OrderedDict[str, tuple] = OrderedDict()
    runner._agent_cache_lock = threading.Lock()

    runner._session_model_overrides: Dict[str, Dict[str, str]] = {}
    runner._session_reasoning_overrides: Dict[str, Dict[str, Any]] = {}
    runner._kanban_notifier_profile = runner._active_profile_name()
    runner._teams_pipeline_runtime = None
    runner._teams_pipeline_runtime_error: Optional[str] = None
    runner._pending_approvals: Dict[str, Dict[str, Any]] = {}
    runner._failed_platforms: Dict[Platform, Dict[str, Any]] = {}
    runner._update_prompt_pending: Dict[str, bool] = {}
    runner._slash_confirm_counter = itertools.count(1)

    _ensure_tirith_available()
    _initialize_session_db(runner)
    _maybe_prune_checkpoints()

    runner.pairing_store = PairingStore()

    from hermes_gateway.hooks import HookRegistry

    runner.hooks = HookRegistry()
    runner._voice_mode: Dict[str, str] = voice_runtime_for(runner).load_voice_modes()
    runner._recent_voice_transcripts: Dict[
        tuple[int, int],
        List[tuple[float, str]],
    ] = {}
    runner._background_tasks: set = set()


def _ensure_tirith_available() -> None:
    try:
        from tools.tirith_security import ensure_installed

        ensure_installed(log_failures=False)
    except Exception as exc:
        logger.debug("tirith security scanner install skipped: %s", exc)


def _initialize_session_db(runner) -> None:
    runner._session_db = None
    runner._session_db_error: Optional[str] = None
    try:
        from hermes_agent.composition.cli_session_store import open_cli_session_store

        runner._session_db = open_cli_session_store()
    except Exception as exc:
        runner._session_db_error = f"{type(exc).__name__}: {exc}"
        logger.warning("SQLite session store not available: %s", exc)
        return

    _maybe_auto_prune_session_db(runner)


def _maybe_auto_prune_session_db(runner) -> None:
    if runner._session_db is None:
        return
    try:
        from hermes_cli.config import load_config

        session_config = (load_config().get("sessions") or {})
        if session_config.get("auto_prune", False):
            runner._session_db.maintenance.maybe_auto_prune_and_vacuum(
                retention_days=int(session_config.get("retention_days", 90)),
                min_interval_hours=int(session_config.get("min_interval_hours", 24)),
                vacuum=bool(session_config.get("vacuum_after_prune", True)),
                sessions_dir=runner.config.sessions_dir,
            )
    except Exception as exc:
        logger.debug("state.db auto-maintenance skipped: %s", exc)


def _maybe_prune_checkpoints() -> None:
    try:
        from hermes_cli.config import load_config

        checkpoint_config = (load_config().get("checkpoints") or {})
        if not checkpoint_config.get("auto_prune", False):
            return
        from tools.checkpoint_manager import maybe_auto_prune_checkpoints

        maybe_auto_prune_checkpoints(
            retention_days=int(checkpoint_config.get("retention_days", 7)),
            min_interval_hours=int(checkpoint_config.get("min_interval_hours", 24)),
            delete_orphans=bool(checkpoint_config.get("delete_orphans", True)),
            max_total_size_mb=int(checkpoint_config.get("max_total_size_mb", 500)),
        )
    except Exception as exc:
        logger.debug("checkpoint auto-maintenance skipped: %s", exc)
