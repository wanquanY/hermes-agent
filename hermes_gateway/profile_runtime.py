"""Profile routing and context-local runtime isolation for the gateway."""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import logging
from contextlib import contextmanager, nullcontext
from pathlib import Path
from typing import Any

from channels.session_identity import SessionSource

logger = logging.getLogger(__name__)


@contextmanager
def profile_runtime_scope(profile_home: Path):
    """Scope home/config state and credentials without mutating global env."""
    from agent.secret_scope import (
        build_profile_secret_scope,
        reset_secret_scope,
        set_secret_scope,
    )
    from hermes_constants import (
        reset_hermes_home_override,
        set_hermes_home_override,
    )

    home = Path(profile_home)
    home_token = set_hermes_home_override(home)
    secret_token = set_secret_scope(build_profile_secret_scope(home))
    try:
        yield
    finally:
        reset_secret_scope(secret_token)
        reset_hermes_home_override(home_token)


class UnknownProfileError(RuntimeError):
    """A source requested a profile this gateway cannot safely serve."""


class MultiplexConfigError(RuntimeError):
    """A profile cannot be served safely by the shared gateway process."""


class SecondaryPortBindingConfigError(MultiplexConfigError):
    """A secondary profile attempted to own a process-wide listener."""


class GatewayProfileRuntimeService:
    def __init__(self, runner):
        self._runner = runner

    @property
    def multiplex_enabled(self) -> bool:
        config = getattr(self._runner, "config", None)
        return getattr(config, "multiplex_profiles", False) is True

    def active_profile_name(self) -> str:
        try:
            from hermes_cli.profiles import get_active_profile_name

            return get_active_profile_name() or "default"
        except Exception:
            return "default"

    def route_source(self, source: SessionSource) -> SessionSource:
        """Stamp the owning profile before auth and session-key derivation."""
        if not self.multiplex_enabled:
            return source
        profile = str(getattr(source, "profile", "") or "").strip()
        if not profile:
            from hermes_gateway.profile_routing import match_profile_route

            matched = match_profile_route(
                getattr(self._runner.config, "profile_routes", None) or [],
                platform=source.platform.value,
                scope_id=getattr(source, "scope_id", None),
                chat_id=source.chat_id,
                thread_id=getattr(source, "thread_id", None),
                parent_chat_id=getattr(source, "parent_chat_id", None),
            )
            profile = matched.profile if matched else self.active_profile_name()
        self.profile_home(profile, require_exists=True)
        if source.profile == profile:
            return source
        return dataclasses.replace(source, profile=profile)

    def profile_home(self, profile: str, *, require_exists: bool = True) -> Path:
        from hermes_cli.profiles import (
            get_profile_dir,
            normalize_profile_name,
            profile_exists,
            validate_profile_name,
        )

        normalized = normalize_profile_name(profile)
        validate_profile_name(normalized)
        if require_exists and not profile_exists(normalized):
            raise UnknownProfileError(
                f"Profile {normalized!r} does not exist or is not configured"
            )
        return get_profile_dir(normalized)

    def scope_for_profile(self, profile: str | None = None):
        if not self.multiplex_enabled:
            return nullcontext()
        name = str(profile or self.active_profile_name()).strip()
        return profile_runtime_scope(self.profile_home(name, require_exists=True))

    def scope_for_source(self, source: SessionSource):
        if not self.multiplex_enabled:
            return nullcontext()
        profile = str(getattr(source, "profile", "") or "").strip()
        if not profile:
            profile = self.active_profile_name()
        return self.scope_for_profile(profile)

    async def start_secondary_adapters(self) -> int:
        """Start every non-active profile under isolated runtime scopes."""
        if not self.multiplex_enabled:
            return 0

        from hermes_cli.profiles import profiles_to_serve
        from hermes_gateway.pairing import PairingStore

        runner = self._runner
        active = self.active_profile_name()
        profiles = profiles_to_serve(multiplex=True)
        for profile_name, profile_home in profiles:
            from hermes_gateway.profile_storage import profile_storage_for

            storage = profile_storage_for(runner)
            if storage is not None:
                storage.preload(profile_home)
            pairing_store = (
                runner.pairing_store
                if profile_name == active
                else PairingStore(base_dir=profile_home / "pairing")
            )
            runner.pairing_stores.setdefault(profile_name, pairing_store)

        claimed: dict[tuple[Any, str], str] = {}
        for platform, adapter in runner.adapters.items():
            fingerprint = self.adapter_credential_fingerprint(adapter)
            if fingerprint is not None:
                claimed[(platform, fingerprint)] = active

        connected = 0
        for profile_name, profile_home in profiles:
            if profile_name == active:
                continue
            try:
                connected += await self._start_profile_adapters(
                    profile_name,
                    profile_home,
                    claimed,
                )
            except SecondaryPortBindingConfigError as exc:
                logger.warning("Skipping profile %s: %s", profile_name, exc)
            except MultiplexConfigError:
                raise
            except Exception:
                logger.exception(
                    "Failed to start adapters for profile %s",
                    profile_name,
                )
        self._record_served_profiles(active)
        return connected

    async def stop_secondary_adapters(self) -> None:
        """Cancel reconnect ownership and drain every secondary adapter."""
        runner = self._runner
        failed_platforms = getattr(runner, "_profile_failed_platforms", {})
        reconnect_tasks = [
            task
            for pending in failed_platforms.values()
            for task in pending.values()
        ]
        for task in reconnect_tasks:
            task.cancel()
        if reconnect_tasks:
            await asyncio.gather(*reconnect_tasks, return_exceptions=True)
        failed_platforms.clear()

        profile_adapters = getattr(runner, "_profile_adapters", {})
        for profile_name, adapters in list(profile_adapters.items()):
            for platform, adapter in list(adapters.items()):
                try:
                    with self.scope_for_profile(profile_name):
                        await adapter.cancel_background_tasks()
                        await runner._safe_adapter_disconnect(adapter, platform)
                except Exception:
                    logger.exception(
                        "Failed to stop %s adapter for profile %s",
                        platform.value,
                        profile_name,
                    )
            adapters.clear()
        profile_adapters.clear()

    async def _start_profile_adapters(
        self,
        profile_name: str,
        profile_home: Path,
        claimed: dict[tuple[Any, str], str],
    ) -> int:
        from channels.config import platform_binds_port
        from hermes_gateway.config import load_gateway_config

        with profile_runtime_scope(profile_home):
            profile_config = load_gateway_config()

        port_owners = sorted(
            platform.value
            for platform, config in profile_config.platforms.items()
            if config.enabled
            and platform_binds_port(platform.value, config.extra)
        )
        if port_owners:
            raise SecondaryPortBindingConfigError(
                f"profile {profile_name!r} enables process-wide listener(s): "
                f"{', '.join(port_owners)}; configure them only on the active "
                "profile and route shared HTTP ingress by profile prefix"
            )

        runner = self._runner
        profile_map = runner._profile_adapters.setdefault(profile_name, {})
        connected = 0
        for platform, platform_config in profile_config.platforms.items():
            if not platform_config.enabled:
                continue
            adapter = None
            try:
                with profile_runtime_scope(profile_home):
                    adapter = runner._create_adapter(platform, platform_config)
                if adapter is None:
                    logger.warning(
                        "Profile %s: adapter unavailable for %s",
                        profile_name,
                        platform.value,
                    )
                    continue

                fingerprint = self.adapter_credential_fingerprint(adapter)
                if fingerprint is not None:
                    owner = claimed.get((platform, fingerprint))
                    if owner is not None:
                        logger.error(
                            "Profiles %s and %s configure %s with the same "
                            "credential; refusing duplicate polling",
                            owner,
                            profile_name,
                            platform.value,
                        )
                        await runner._safe_adapter_disconnect(adapter, platform)
                        continue
                    claimed[(platform, fingerprint)] = profile_name

                self.configure_profile_adapter(adapter, profile_name, platform)
                with profile_runtime_scope(profile_home):
                    success = await runner._connect_adapter_with_timeout(
                        adapter,
                        platform,
                    )
                if not success:
                    await runner._safe_adapter_disconnect(adapter, platform)
                    continue
                profile_map[platform] = adapter
                self._sync_voice_state(adapter)
                connected += 1
                logger.info(
                    "✓ %s connected (profile: %s)",
                    platform.value,
                    profile_name,
                )
            except asyncio.CancelledError:
                if adapter is not None:
                    await runner._safe_adapter_disconnect(adapter, platform)
                raise
            except Exception as exc:
                logger.error(
                    "✗ %s error (profile: %s): %s",
                    platform.value,
                    profile_name,
                    exc,
                )
                if adapter is not None:
                    await runner._safe_adapter_disconnect(adapter, platform)
        return connected

    def configure_profile_adapter(self, adapter, profile_name: str, platform) -> None:
        runner = self._runner
        adapter.set_message_handler(self._profile_message_handler(profile_name))
        adapter.set_fatal_error_handler(
            self._profile_fatal_error_handler(profile_name, platform)
        )
        adapter.set_session_store(runner.session_store)
        from hermes_gateway.busy_session_runtime import busy_session_runtime_for

        adapter.set_busy_session_handler(
            busy_session_runtime_for(runner).handle_active_session_busy_message
        )

    def _profile_message_handler(self, profile_name: str):
        runner = self._runner

        async def _handle(event):
            source = getattr(event, "source", None)
            if source is not None and source.profile != profile_name:
                source = dataclasses.replace(source, profile=profile_name)
                event = dataclasses.replace(event, source=source)
            with self.scope_for_profile(profile_name):
                return await runner._handle_message(event)

        return _handle

    def _profile_fatal_error_handler(self, profile_name: str, platform):
        async def _handle(adapter):
            await self.handle_profile_adapter_fatal_error(
                profile_name,
                platform,
                adapter,
            )

        return _handle

    async def handle_profile_adapter_fatal_error(
        self,
        profile_name: str,
        platform,
        adapter,
    ) -> None:
        runner = self._runner
        profile_map = runner._profile_adapters.get(profile_name)
        if not isinstance(profile_map, dict) or profile_map.get(platform) is not adapter:
            return
        profile_map.pop(platform, None)
        await runner._safe_adapter_disconnect(adapter, platform)
        if runner._running and adapter.fatal_error_retryable:
            self.schedule_reconnect(profile_name, platform)

    def schedule_reconnect(self, profile_name: str, platform) -> None:
        runner = self._runner
        pending = runner._profile_failed_platforms.setdefault(profile_name, {})
        existing = pending.get(platform)
        if existing is not None and not existing.done():
            return
        task = asyncio.create_task(
            self._reconnect(profile_name, platform),
            name=f"secondary-reconnect:{profile_name}:{platform.value}",
        )
        pending[platform] = task
        runner._background_tasks.add(task)
        task.add_done_callback(runner._background_tasks.discard)

    async def _reconnect(self, profile_name: str, platform) -> None:
        runner = self._runner
        current_task = asyncio.current_task()
        attempt = 0
        try:
            while runner._running:
                adapter = None
                try:
                    from hermes_gateway.config import load_gateway_config

                    with self.scope_for_profile(profile_name):
                        platform_config = load_gateway_config().platforms.get(platform)
                        if platform_config is None or not platform_config.enabled:
                            return
                        adapter = runner._create_adapter(platform, platform_config)
                        if adapter is None:
                            return
                        self.configure_profile_adapter(
                            adapter,
                            profile_name,
                            platform,
                        )
                        success = await runner._connect_adapter_with_timeout(
                            adapter,
                            platform,
                            is_reconnect=True,
                        )
                    if success and runner._running:
                        profile_map = runner._profile_adapters.setdefault(
                            profile_name,
                            {},
                        )
                        if platform not in profile_map:
                            profile_map[platform] = adapter
                            self._sync_voice_state(adapter)
                            return
                    if adapter is not None:
                        await runner._safe_adapter_disconnect(adapter, platform)
                    if (
                        adapter is not None
                        and adapter.has_fatal_error
                        and not adapter.fatal_error_retryable
                    ):
                        return
                except asyncio.CancelledError:
                    if adapter is not None:
                        await runner._safe_adapter_disconnect(adapter, platform)
                    raise
                except Exception:
                    if adapter is not None:
                        await runner._safe_adapter_disconnect(adapter, platform)
                    logger.debug(
                        "Secondary reconnect failed for %s/%s",
                        profile_name,
                        platform.value,
                        exc_info=True,
                    )
                attempt += 1
                await asyncio.sleep(min(30 * (2 ** max(0, attempt - 1)), 300))
        finally:
            profile_pending = runner._profile_failed_platforms.get(profile_name, {})
            if profile_pending.get(platform) is current_task:
                profile_pending.pop(platform, None)
            if not profile_pending:
                runner._profile_failed_platforms.pop(profile_name, None)

    @staticmethod
    def adapter_credential_fingerprint(adapter: Any) -> str | None:
        token = None
        for owner in (adapter, getattr(adapter, "config", None)):
            if owner is None:
                continue
            for attribute in (
                "token",
                "bot_token",
                "_token",
                "api_token",
                "_bot_token",
            ):
                value = getattr(owner, attribute, None)
                if isinstance(value, str) and value.strip():
                    token = value.strip()
                    break
            if token:
                break
        if token is None:
            return None
        digest = hashlib.sha256(f"hermes-mux:{token}".encode()).hexdigest()
        return digest[:16]

    def _sync_voice_state(self, adapter) -> None:
        from hermes_gateway.voice_runtime import voice_runtime_for

        voice_runtime_for(self._runner).sync_voice_mode_state_to_adapter(adapter)

    def _record_served_profiles(self, active: str) -> None:
        try:
            from channels.runtime_status import write_runtime_status

            write_runtime_status(
                served_profiles=[active, *sorted(
                    profile
                    for profile in self._runner.pairing_stores
                    if profile != active
                )]
            )
        except Exception:
            logger.debug("Could not record served profiles", exc_info=True)


def profile_runtime_for(runner) -> GatewayProfileRuntimeService:
    service = getattr(runner, "profile_runtime", None)
    if isinstance(service, GatewayProfileRuntimeService):
        return service
    service = GatewayProfileRuntimeService(runner)
    runner.profile_runtime = service
    return service
