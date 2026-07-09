"""Runtime status file writer service for the gateway."""

from __future__ import annotations

from typing import Optional


class GatewayRuntimeStatusService:
    def __init__(self, runner):
        self._runner = runner

    def update_runtime_status(
        self,
        gateway_state: Optional[str] = None,
        exit_reason: Optional[str] = None,
    ) -> None:
        try:
            from channels.runtime_status import write_runtime_status

            write_runtime_status(
                gateway_state=gateway_state,
                exit_reason=exit_reason,
                restart_requested=self._runner._restart_requested,
                active_agents=self._runner._running_agent_count(),
            )
        except Exception:
            pass

    def persist_active_agents(self) -> None:
        """Persist only the live in-flight agent count."""
        try:
            from channels.runtime_status import write_runtime_status

            write_runtime_status(active_agents=self._runner._running_agent_count())
        except Exception:
            pass

    def update_platform_runtime_status(
        self,
        platform: str,
        *,
        platform_state: Optional[str] = None,
        error_code: Optional[str] = None,
        error_message: Optional[str] = None,
    ) -> None:
        try:
            from channels.runtime_status import write_runtime_status

            write_runtime_status(
                platform=platform,
                platform_state=platform_state,
                error_code=error_code,
                error_message=error_message,
            )
        except Exception:
            pass


def runtime_status_for(runner) -> GatewayRuntimeStatusService:
    service = getattr(runner, "runtime_status", None)
    if isinstance(service, GatewayRuntimeStatusService):
        return service
    service = GatewayRuntimeStatusService(runner)
    runner.runtime_status = service
    return service
