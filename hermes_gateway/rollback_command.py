"""Gateway /rollback command ownership."""

from __future__ import annotations

import logging
import os
from pathlib import Path

from agent.i18n import t
from channels.platforms.base import MessageEvent

logger = logging.getLogger(__name__)


def _gateway_home() -> Path:
    from hermes_constants import get_hermes_home

    return get_hermes_home()


class GatewayRollbackCommandService:
    def __init__(self, runner):
        self._runner = runner

    async def handle_rollback_command(self, event: MessageEvent) -> str:
        """Handle /rollback command — list or restore filesystem checkpoints."""
        from tools.checkpoint_manager import CheckpointManager, format_checkpoint_list

        # Read checkpoint config from config.yaml
        cp_cfg = {}
        try:
            import yaml as _y
            _cfg_path = _gateway_home() / "config.yaml"
            if _cfg_path.exists():
                with open(_cfg_path, encoding="utf-8") as _f:
                    _data = _y.safe_load(_f) or {}
                cp_cfg = _data.get("checkpoints", {})
                if isinstance(cp_cfg, bool):
                    cp_cfg = {"enabled": cp_cfg}
        except Exception as exc:
            logger.debug("Could not load checkpoint config for /rollback: %s", exc)

        if not cp_cfg.get("enabled", False):
            return t("gateway.rollback.not_enabled")

        mgr = CheckpointManager(
            enabled=True,
            max_snapshots=cp_cfg.get("max_snapshots", 50),
            max_total_size_mb=cp_cfg.get("max_total_size_mb", 500),
            max_file_size_mb=cp_cfg.get("max_file_size_mb", 10),
        )

        cwd = os.getenv("TERMINAL_CWD", str(Path.home()))
        arg = event.get_command_args().strip()

        if not arg:
            checkpoints = mgr.list_checkpoints(cwd)
            return format_checkpoint_list(checkpoints, cwd)

        # Restore by number or hash
        checkpoints = mgr.list_checkpoints(cwd)
        if not checkpoints:
            return t("gateway.rollback.none_found", cwd=cwd)

        target_hash = None
        try:
            idx = int(arg) - 1
            if 0 <= idx < len(checkpoints):
                target_hash = checkpoints[idx]["hash"]
            else:
                return t("gateway.rollback.invalid_number", max=len(checkpoints))
        except ValueError:
            target_hash = arg

        result = mgr.restore(cwd, target_hash)
        if result["success"]:
            return t(
                "gateway.rollback.restored",
                hash=result["restored_to"],
                reason=result["reason"],
            )
        return t("gateway.rollback.restore_failed", error=result["error"])


def rollback_command_for(runner) -> GatewayRollbackCommandService:
    service = getattr(runner, "rollback_command", None)
    if isinstance(service, GatewayRollbackCommandService):
        return service
    service = GatewayRollbackCommandService(runner)
    runner.rollback_command = service
    return service
