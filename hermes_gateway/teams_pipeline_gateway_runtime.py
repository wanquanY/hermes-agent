"""Gateway wiring for the optional Teams pipeline plugin."""

from __future__ import annotations

import logging
import sys

from hermes_agent.gateway.runtime_config import load_gateway_runtime_config
from hermes_cli.config import cfg_get
from hermes_constants import get_hermes_home
from hermes_gateway.config import Platform

logger = logging.getLogger(__name__)


class GatewayTeamsPipelineRuntime:
    def __init__(self, runner):
        self._runner = runner

    def wire_runtime(self) -> None:
        runner = self._runner
        if Platform.MSGRAPH_WEBHOOK not in runner.adapters:
            return
        if not self._plugin_enabled():
            logger.debug("Teams pipeline plugin is disabled; skipping runtime wiring")
            return
        try:
            from plugins.teams_pipeline.runtime import bind_gateway_runtime
        except Exception as exc:
            logger.warning("Teams pipeline runtime import failed: %s", exc)
            return
        try:
            bound = bind_gateway_runtime(runner)
        except Exception as exc:
            logger.warning("Teams pipeline runtime wiring failed: %s", exc)
            return
        if bound:
            logger.info("Teams pipeline runtime bound to msgraph webhook ingress")
        elif runner._teams_pipeline_runtime_error:
            logger.warning(
                "Teams pipeline runtime unavailable: %s",
                runner._teams_pipeline_runtime_error,
            )

    @staticmethod
    def _plugin_enabled() -> bool:
        config = _load_gateway_config()
        enabled = cfg_get(config, "plugins", "enabled", default=[])
        if not isinstance(enabled, list):
            return False
        return "teams_pipeline" in enabled or "teams-pipeline" in enabled


def _load_gateway_config() -> dict:
    legacy = sys.modules.get("gateway.run")
    patched = getattr(legacy, "_load_gateway_config", None) if legacy is not None else None
    if callable(patched):
        return patched()
    return load_gateway_runtime_config(get_hermes_home())


def teams_pipeline_runtime_for(runner) -> GatewayTeamsPipelineRuntime:
    service = getattr(runner, "teams_pipeline_gateway_runtime", None)
    if isinstance(service, GatewayTeamsPipelineRuntime):
        return service
    service = GatewayTeamsPipelineRuntime(runner)
    runner.teams_pipeline_gateway_runtime = service
    return service
