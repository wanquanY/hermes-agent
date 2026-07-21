"""Gateway coordination for durable final-response delivery obligations."""

from __future__ import annotations

import logging
import os
from typing import Any, Optional

from channels.config import Platform
from channels.runtime_status import (
    get_process_start_time,
    process_identity_is_alive,
)
from channels.session_identity import SessionSource
from hermes_agent.composition.async_sqlite import run_sqlite_io
from hermes_agent.domain.delivery_obligation import RECOVERED_DELIVERY_MARKER

logger = logging.getLogger(__name__)


class GatewayDeliveryObligationService:
    """Bind the persistent aggregate to platform sends and startup recovery."""

    def __init__(self, runner) -> None:
        self._runner = runner

    @staticmethod
    def enabled() -> bool:
        try:
            from hermes_agent.gateway.runtime_config import (
                load_gateway_runtime_config,
            )

            config = load_gateway_runtime_config()
            gateway = config.get("gateway") or {}
            value = gateway.get("delivery_ledger", True)
            if isinstance(value, str):
                return value.strip().lower() not in {"false", "0", "no", "off"}
            return bool(value)
        except Exception:
            return True

    def _store(self):
        state_store = getattr(self._runner, "_session_db", None)
        return getattr(state_store, "delivery_obligations", None)

    @staticmethod
    def should_record(
        *,
        inbound_text: str,
        response_text: str,
        typed_command_prefix: str,
        ephemeral: bool,
    ) -> bool:
        if ephemeral or not response_text:
            return False
        stripped = str(inbound_text or "").lstrip()
        prefixes = {"/", str(typed_command_prefix or "!")}
        return not any(prefix and stripped.startswith(prefix) for prefix in prefixes)

    async def record_before_send(
        self,
        *,
        event,
        session_key: str,
        platform: str,
        content: str,
        reply_to: Optional[str],
        metadata: Optional[dict[str, Any]],
        typed_command_prefix: str,
        ephemeral: bool,
    ) -> Optional[str]:
        if not self.enabled() or not self.should_record(
            inbound_text=getattr(event, "text", ""),
            response_text=content,
            typed_command_prefix=typed_command_prefix,
            ephemeral=ephemeral,
        ):
            return None
        store = self._store()
        if store is None:
            return None
        pid = os.getpid()
        obligation_id = await run_sqlite_io(
            store.record,
            session_key=session_key,
            inbound_message_id=str(getattr(event, "message_id", "") or ""),
            platform=platform,
            chat_id=str(event.source.chat_id),
            thread_id=getattr(event.source, "thread_id", None),
            reply_to=reply_to,
            metadata=metadata,
            content=content,
            owner_pid=pid,
            owner_started_at=get_process_start_time(pid),
        )
        await run_sqlite_io(store.mark_attempting, obligation_id)
        return obligation_id

    async def settle(
        self,
        obligation_id: Optional[str],
        result,
    ) -> None:
        if not obligation_id:
            return
        store = self._store()
        if store is None:
            return
        if result is not None and getattr(result, "success", False):
            await run_sqlite_io(store.mark_delivered, obligation_id)
        else:
            await run_sqlite_io(
                store.mark_failed,
                obligation_id,
                str(getattr(result, "error", "") or "send failed"),
            )

    async def recover_startup(self) -> int:
        """Recover every served profile before auto-resume can rerun turns."""
        from hermes_cli.profiles import profiles_to_serve
        from hermes_gateway.profile_runtime import profile_runtime_scope

        multiplex = bool(
            getattr(getattr(self._runner, "config", None), "multiplex_profiles", False)
        )
        delivered = 0
        for profile, home in profiles_to_serve(multiplex=multiplex):
            with profile_runtime_scope(home):
                delivered += await self._recover_current_profile(profile)
        return delivered

    async def _recover_current_profile(self, profile: str) -> int:
        if not self.enabled():
            return 0
        store = self._store()
        if store is None:
            return 0
        profile_adapters = getattr(self._runner, "_profile_adapters", {}) or {}
        adapters = profile_adapters.get(profile)
        if adapters is None:
            adapters = getattr(self._runner, "adapters", {}) or {}
        deliverable_platforms = {
            getattr(platform, "value", str(platform))
            for platform in adapters
        }
        pid = os.getpid()
        claimed = await run_sqlite_io(
            store.claim_recoverable,
            owner_pid=pid,
            owner_started_at=get_process_start_time(pid),
            owner_alive=process_identity_is_alive,
            deliverable_platforms=deliverable_platforms,
        )
        delivered = 0
        for recovery in claimed:
            obligation = recovery.obligation
            result = None
            try:
                platform = Platform(obligation.platform)
                source = SessionSource(
                    platform=platform,
                    chat_id=obligation.chat_id,
                    thread_id=obligation.thread_id,
                    profile=profile,
                )
                adapter = self._runner._adapter_for_source(source)
                if adapter is None:
                    raise RuntimeError("platform adapter is not connected")
                content = obligation.content
                if recovery.needs_duplicate_marker:
                    content = RECOVERED_DELIVERY_MARKER + content
                result = await adapter._send_with_retry(
                    chat_id=obligation.chat_id,
                    content=content,
                    reply_to=obligation.reply_to,
                    metadata=dict(obligation.metadata),
                )
                if getattr(result, "success", False):
                    delivered += 1
                    logger.info(
                        "Recovered final response for %s via %s (attempt %d)",
                        obligation.session_key,
                        obligation.platform,
                        obligation.attempts,
                    )
            except Exception as exc:
                logger.warning(
                    "Delivery recovery failed for %s: %s",
                    obligation.obligation_id,
                    exc,
                )
                if result is None:
                    from channels.platforms.base_models import SendResult

                    result = SendResult(success=False, error=str(exc))
            try:
                await self.settle(obligation.obligation_id, result)
            except Exception:
                logger.debug("delivery obligation update failed", exc_info=True)

            # The completed turn's answer is held durably in this ledger even
            # when redelivery failed. Never also rerun and re-bill the turn.
            try:
                await run_sqlite_io(
                    self._runner.session_store.clear_resume_pending,
                    obligation.session_key,
                )
            except Exception:
                logger.debug(
                    "clear_resume_pending failed for %s",
                    obligation.session_key,
                    exc_info=True,
                )
        return delivered


def delivery_obligations_for(owner) -> GatewayDeliveryObligationService:
    runner = getattr(owner, "gateway_runner", None) or owner
    service = getattr(runner, "delivery_obligations", None)
    if isinstance(service, GatewayDeliveryObligationService):
        return service
    service = GatewayDeliveryObligationService(runner)
    runner.delivery_obligations = service
    return service


__all__ = [
    "GatewayDeliveryObligationService",
    "delivery_obligations_for",
]
